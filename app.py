from __future__ import annotations

"""
Streamlit application entrypoint for the enhanced Form 15CB Batch Generator.

This version supersedes the original application by supporting batch
processing of invoices contained within a single ZIP archive accompanied
by an Excel spreadsheet.  The new workflow allows users to upload a ZIP
file, automatically derive the currency, exchange rate and date of
deduction from the spreadsheet, set global defaults for TDS/Non‑TDS
mode and gross‑up, and then process all invoices in one click.  Per
invoice overrides remain available for exceptional cases, and XML
generation is supported both individually and in batch.

Key enhancements:

* ZIP ingestion: the user uploads a single ZIP archive containing one
  Excel (.xlsx) file and one or more invoice documents (.pdf/.jpg/.png).
  The application reads the Excel to fetch currency, INR/FCY amounts,
  calculates the exchange rate and extracts the posting date for the
  TDS deduction.
* Global controls: a pair of toggles allow the CA to set the default
  TDS/Non‑TDS mode and whether gross‑up applies.  These values are
  automatically applied to all invoices but can be overridden per
  invoice.
* Per‑invoice overrides: within each invoice tab the user can change
  the mode and gross‑up settings if a particular invoice deviates from
  the batch default.  Changing the global defaults clears all
  overrides and recomputes derived values without re‑calling Gemini.
* Robust date parsing: the ``Posting Date`` column of the Excel may
  contain serial numbers, dates or strings in multiple formats.  The
  parsed date populates ``DednDateTds`` in the XML.  Proposed
  remittance date remains today+15 days.
* Partial downloads: generating XML for all invoices includes only
  those that have been processed successfully; invoices that failed or
  remain unprocessed are skipped with a summary explaining why.

Existing functionality—such as invoice text extraction via Gemini,
master data lookup, tax computation and XML generation—are preserved
and reused from the original modules.
"""

import io
import os
import time
from typing import Dict, List

import streamlit as st

from pdf2image import convert_from_bytes

from modules.zip_intake import parse_zip, read_excel, build_invoice_registry
from modules.form15cb_constants import IT_ACT_RATE_DEFAULT, IT_ACT_RATES, MODE_NON_TDS, MODE_TDS
from modules.invoice_state import build_invoice_state
from modules.invoice_calculator import invoice_state_to_xml_fields, recompute_invoice
from modules.invoice_gemini_extractor import (
    extract_invoice_core_fields,
    extract_invoice_core_fields_from_image,
    merge_multi_page_image_extractions,
)
from modules.pdf_reader import extract_text_from_pdf
from modules.ocr_engine import extract_text_from_image_file
from modules.xml_generator import (
    generate_xml_content,
    generate_zip_from_xmls,
    write_xml_content,
)
from modules.master_data import validate_bsr_code, validate_dtaa_rate, validate_pan
from modules.currency_mapping import is_currency_code_valid_for_xml
from modules.logger import get_logger


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Maximum size of uploaded files (used when extracting images from PDFs)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
# Maximum number of pages from a PDF to OCR when text extraction fails
MAX_SCANNED_PDF_PAGES = max(1, int(os.getenv("MAX_SCANNED_PDF_PAGES", "6")))
# Application version and last updated timestamp
VERSION = "4.0"
LAST_UPDATED = "March 2026"

logger = get_logger()


# -----------------------------------------------------------------------------
# Session state initialisation
# -----------------------------------------------------------------------------

def _ensure_session_state() -> None:
    """Initialise keys in ``st.session_state`` that this app relies on."""
    if "invoices" not in st.session_state:
        # Mapping of invoice_id -> invoice record (see zip_intake.build_invoice_registry)
        st.session_state["invoices"] = {}
    if "zip_context" not in st.session_state:
        # Metadata about the currently loaded ZIP (name, Excel name, timestamp)
        st.session_state["zip_context"] = None
    if "global_controls" not in st.session_state:
        # Defaults for mode and gross‑up that apply to all invoices
        st.session_state["global_controls"] = {
            "mode": MODE_TDS,
            "gross_up": False,
            "it_act_rate": IT_ACT_RATE_DEFAULT,
        }


# -----------------------------------------------------------------------------
# XML field validation
# -----------------------------------------------------------------------------

def _validate_xml_fields(fields: Dict[str, str], mode: str = MODE_TDS) -> List[str]:
    """Validate XML fields before generation.

    This function largely mirrors the behaviour of the original app,
    checking PAN format, BSR code, DTAA rate, currency, country, nature
    and basis selection.  The ``mode`` argument controls which TDS
    fields are required.
    """
    errors: List[str] = []

    # Basic field validations
    if fields.get("RemitterPAN") and not validate_pan(fields["RemitterPAN"]):
        errors.append("RemitterPAN format is invalid (expected AAAAA9999A).")
    if fields.get("BsrCode") and not validate_bsr_code(fields["BsrCode"]):
        errors.append("BsrCode must be exactly 7 digits.")
    if fields.get("RateTdsADtaa") and (fields.get("RateTdsADtaa") or "").strip() and not validate_dtaa_rate(fields["RateTdsADtaa"]):
        errors.append("RateTdsADtaa must be between 0 and 100.")
    if not is_currency_code_valid_for_xml(fields.get("CurrencySecbCode", "")):
        errors.append("Currency must be selected with a valid code before generating XML.")
    if not str(fields.get("CountryRemMadeSecb") or "").strip():
        errors.append("Country to which remittance is made must be selected.")
    if not str(fields.get("NatureRemCategory") or "").strip():
        errors.append("Nature of remittance must be selected.")

    basis = str(fields.get("BasisDeterTax") or "").strip()
    if not basis:
        errors.insert(0, "Please select the Basis of TDS determination (DTAA or Income Tax Act) before generating XML.")
    elif basis == "DTAA":
        for field in ["RateTdsADtaa", "TaxIncDtaa", "TaxLiablDtaa"]:
            if not str(fields.get(field) or "").strip():
                errors.append(f"{field} is required for DTAA basis.")
    elif basis == "Act":
        for field in ["RateTdsSecB", "TaxLiablIt"]:
            if not str(fields.get(field) or "").strip():
                errors.append(f"{field} is required for Income Tax Act basis.")

    if mode == MODE_TDS:
        if not str(fields.get("AmtPayForgnTds") or "").strip():
            errors.append("Amount of remittance must be entered.")
        if not str(fields.get("ActlAmtTdsForgn") or "").strip():
            errors.append("Actual amount remitted must be entered.")

    return errors


# -----------------------------------------------------------------------------
# Helper functions for overrides and recomputation
# -----------------------------------------------------------------------------

def _effective_mode(inv: Dict[str, object]) -> str:
    """Resolve the effective mode (TDS/Non‑TDS) for an invoice."""
    override = inv.get("mode_override")
    if override:
        return override
    return st.session_state["global_controls"].get("mode", MODE_TDS)


def _effective_gross(inv: Dict[str, object]) -> bool:
    """Resolve the effective gross‑up flag for an invoice.

    If the invoice is in Non‑TDS mode the gross‑up flag is forced to
    ``False`` regardless of overrides or global settings.  Otherwise it
    inherits the override (if set) or the global default.
    """
    mode = _effective_mode(inv)
    if mode == MODE_NON_TDS:
        return False
    override = inv.get("gross_override")
    if override is not None:
        return bool(override)
    return bool(st.session_state["global_controls"].get("gross_up", False))


def _effective_it_rate(inv: Dict[str, object]) -> float:
    """Resolve the effective IT Act rate for an invoice.

    Returns the per-invoice override if set, otherwise the global default.
    """
    override = inv.get("it_act_rate_override")
    if override is not None:
        return float(override)
    return float(st.session_state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))


def _reset_invoice_states() -> None:
    """Clear overrides and recompute invoices after a global change.

    When the user toggles the global mode or gross‑up controls we clear
    all per‑invoice overrides, mark invoices as needing recompute and
    rebuild their state from existing extracted data.  No Gemini calls
    occur during this function.
    """
    invoices = st.session_state["invoices"]
    for inv_id, inv in invoices.items():
        inv["mode_override"] = None
        inv["gross_override"] = None
        inv["it_act_rate_override"] = None
        # If the invoice has been extracted already, rebuild its state
        if inv.get("extracted"):
            effective_mode = _effective_mode(inv)
            effective_gross = _effective_gross(inv)
            effective_rate = _effective_it_rate(inv)
            config = {
                "currency_short": inv["excel"].get("currency", ""),
                "exchange_rate": inv["excel"].get("exchange_rate", 0),
                "mode": effective_mode,
                "is_gross_up": effective_gross,
                "tds_deduction_date": inv["excel"].get("dedn_date_tds", ""),
                "it_act_rate": effective_rate,
            }
            try:
                state = build_invoice_state(inv_id, inv["file_name"], inv["extracted"], config)
                state = recompute_invoice(state)
                inv["state"] = state
                inv["status"] = "processed"
                inv["error"] = None
            except Exception as exc:
                inv["state"] = None
                inv["status"] = "failed"
                inv["error"] = str(exc)
        else:
            # not yet processed
            inv["state"] = None
            inv["status"] = "new"
            inv["error"] = None
        # Clear any previously generated XML
        inv["xml_bytes"] = None
        inv["xml_status"] = "none"
        inv["xml_error"] = None


def _process_single_invoice(inv_id: str) -> None:
    """Run extraction, state building and recompute for one invoice.

    Updates the invoice record in place with extracted data, state and
    status.  Uses the current effective mode and gross‑up settings.
    """
    inv = st.session_state["invoices"][inv_id]
    if inv.get("status") == "processing":
        return
    file_bytes = inv["file_bytes"]
    file_name = inv["file_name"]
    # Guard: skip extremely large files
    if len(file_bytes) > MAX_FILE_SIZE:
        inv["status"] = "failed"
        inv["error"] = f"{file_name}: file too large."
        return
    # Determine effective config
    mode = _effective_mode(inv)
    gross_up = _effective_gross(inv)
    config = {
        "currency_short": inv["excel"].get("currency", ""),
        "exchange_rate": inv["excel"].get("exchange_rate", 0),
        "mode": mode,
        "is_gross_up": gross_up,
        "tds_deduction_date": inv["excel"].get("dedn_date_tds", ""),
    }
    # Extract core fields
    extracted: Dict[str, str] = {}
    # Use a spinner so users know work is in progress
    with st.spinner(f"Processing {file_name}…"):
        try:
            if file_name.lower().endswith(".pdf"):
                try:
                    text = extract_text_from_pdf(io.BytesIO(file_bytes)) or ""
                except Exception:
                    logger.exception("pdf_text_extraction_failed file=%s", file_name)
                    text = ""
                if text and len(text.strip()) >= 20:
                    extracted = extract_invoice_core_fields(text)
                else:
                    # Attempt page-by-page OCR
                    try:
                        images = convert_from_bytes(file_bytes, dpi=300)
                    except Exception as exc:
                        logger.exception("pdf_to_image_failed file=%s", file_name)
                        images = []
                    if images:
                        selected_pages = images[:MAX_SCANNED_PDF_PAGES]
                        page_results: List[Dict[str, str]] = []
                        page_ocr_texts: List[str] = []
                        for page_idx, page_img in enumerate(selected_pages, start=1):
                            buf = io.BytesIO()
                            page_img.save(buf, format="JPEG", quality=90)
                            image_bytes = buf.getvalue()
                            page_extracted = extract_invoice_core_fields_from_image(image_bytes)
                            # Free OCR for fallback
                            try:
                                page_ocr = extract_text_from_image_file(image_bytes) or ""
                            except Exception:
                                logger.exception("image_ocr_fallback_failed file=%s page=%s", file_name, page_idx)
                                page_ocr = ""
                            text_extracted: Dict[str, str] = {}
                            # Fallback: if Gemini extracted nothing, try text extraction on OCR text
                            if (
                                (not page_extracted or not any((page_extracted.get(k) or "").strip() for k in ("invoice_number", "amount", "currency_short", "beneficiary_name")))
                                and len(page_ocr.strip()) >= 50
                            ):
                                try:
                                    text_extracted = extract_invoice_core_fields(page_ocr)
                                except Exception:
                                    logger.exception("pdf_image_page_text_fallback_failed file=%s page=%s", file_name, page_idx)
                            merged_page = dict(text_extracted)
                            # Overwrite with non-empty vision outputs
                            merged_page.update({k: v for k, v in page_extracted.items() if v})
                            merged_page["_raw_invoice_text"] = page_ocr
                            page_results.append(merged_page)
                            page_ocr_texts.append(page_ocr)
                        if len(page_results) == 1:
                            extracted = page_results[0]
                        else:
                            extracted, _ = merge_multi_page_image_extractions(page_results)
                        # Combine OCR text from all pages
                        raw_text = "\n".join(t for t in page_ocr_texts if t.strip())
                        if not extracted.get("_raw_invoice_text"):
                            extracted["_raw_invoice_text"] = raw_text
                    else:
                        # Final fallback: treat as plain image
                        try:
                            extracted = extract_invoice_core_fields_from_image(file_bytes)
                            text = extract_text_from_image_file(file_bytes) or ""
                        except Exception:
                            logger.exception("pdf_image_ocr_fallback_failed file=%s", file_name)
                            extracted = {}
                            text = ""
                        if not extracted.get("_raw_invoice_text"):
                            extracted["_raw_invoice_text"] = text
            else:
                # Image uploads (jpg/png)
                extracted = extract_invoice_core_fields_from_image(file_bytes)
                try:
                    raw_text = extract_text_from_image_file(file_bytes) or ""
                except Exception:
                    logger.exception("image_ocr_fallback_failed file=%s", file_name)
                    raw_text = ""
                if not extracted.get("_raw_invoice_text"):
                    extracted["_raw_invoice_text"] = raw_text
            # Always ensure raw text exists
            extracted.setdefault("_raw_invoice_text", "")
            # Build state and recompute
            state = build_invoice_state(inv_id, file_name, extracted, config)
            state = recompute_invoice(state)
            inv["extracted"] = extracted
            inv["state"] = state
            inv["status"] = "processed"
            inv["error"] = None
            # Clear previous XML
            inv["xml_bytes"] = None
            inv["xml_status"] = "none"
            inv["xml_error"] = None
        except Exception as exc:
            logger.exception("invoice_processing_failed file=%s", file_name)
            inv["extracted"] = None
            inv["state"] = None
            inv["status"] = "failed"
            inv["error"] = str(exc)
            inv["xml_bytes"] = None
            inv["xml_status"] = "none"
            inv["xml_error"] = None


def _generate_xml_for_invoice(inv_id: str) -> None:
    """Generate XML for a single invoice record.

    Performs validation and generation.  Updates the invoice record
    ``xml_status`` and ``xml_bytes`` accordingly.
    """
    inv = st.session_state["invoices"][inv_id]
    if inv.get("state") is None:
        inv["xml_status"] = "failed"
        inv["xml_error"] = "Invoice has not been processed."
        return
    # Determine current mode (should match build state)
    mode = _effective_mode(inv)
    xml_fields = invoice_state_to_xml_fields(inv["state"])
    errors = _validate_xml_fields(xml_fields, mode=mode)
    if errors:
        inv["xml_status"] = "failed"
        inv["xml_error"] = "; ".join(errors)
        inv["xml_bytes"] = None
        return
    try:
        xml_content = generate_xml_content(xml_fields, mode=mode)
        inv["xml_bytes"] = xml_content.encode("utf8")
        inv["xml_status"] = "ok"
        inv["xml_error"] = None
    except Exception as exc:
        logger.exception("xml_generation_failed invoice_id=%s", inv_id)
        inv["xml_status"] = "failed"
        inv["xml_error"] = str(exc)
        inv["xml_bytes"] = None


# -----------------------------------------------------------------------------
# Streamlit UI
# -----------------------------------------------------------------------------

def main() -> None:
    _ensure_session_state()
    st.set_page_config(
        page_title="Form 15CB Batch Generator",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.title("Form 15CB Batch Generator (ZIP-enabled)")

    # Step 1 – Upload ZIP
    st.subheader("Step 1 – Upload ZIP of invoices and Excel")
    uploaded_zip = st.file_uploader(
        "Upload a ZIP file containing an Excel spreadsheet and one or more invoices (PDF/JPG/PNG)",
        type=["zip"],
        accept_multiple_files=False,
        key="zip_uploader",
    )
    if uploaded_zip is not None:
        # Load only if a different file has been uploaded
        if (
            st.session_state.get("zip_context") is None
            or st.session_state["zip_context"].get("zip_name") != uploaded_zip.name
        ):
            try:
                excel_name, excel_bytes, invoice_files = parse_zip(uploaded_zip.getvalue())
                df = read_excel(excel_bytes)
                invoices = build_invoice_registry(df, invoice_files)
                st.session_state["invoices"] = invoices
                st.session_state["zip_context"] = {
                    "zip_name": uploaded_zip.name,
                    "excel_name": excel_name,
                    "loaded_at": time.time(),
                }
                # Reset global controls to defaults
                st.session_state["global_controls"] = {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                }
                st.success(
                    f"Loaded {len(invoices)} invoices from {uploaded_zip.name}. "
                    f"Excel file: {excel_name}"
                )
            except Exception as exc:
                st.session_state["invoices"] = {}
                st.session_state["zip_context"] = None
                logger.exception("zip_upload_failed")
                st.error(f"Failed to parse ZIP: {exc}")

    invoices = st.session_state.get("invoices", {})
    if invoices:
        # Global controls
        st.subheader("Step 2 – Configure Defaults and Process")
        prev_mode = st.session_state["global_controls"].get("mode", MODE_TDS)
        prev_gross = st.session_state["global_controls"].get("gross_up", False)
        prev_it_rate = st.session_state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT)

        # Build display labels for IT Act Rate selectbox
        _IT_RATE_LABELS = [
            f"{r}% (Default)" if r == IT_ACT_RATE_DEFAULT else f"{r}%"
            for r in IT_ACT_RATES
        ]
        _IT_RATE_MAP = dict(zip(_IT_RATE_LABELS, IT_ACT_RATES))
        _prev_label = next(
            (lbl for lbl, val in _IT_RATE_MAP.items() if val == prev_it_rate),
            _IT_RATE_LABELS[0],
        )

        gc1, gc2, gc3 = st.columns([2, 2, 3])
        with gc1:
            new_mode = st.radio(
                "TDS Mode",
                [MODE_TDS, MODE_NON_TDS],
                index=0 if prev_mode == MODE_TDS else 1,
                horizontal=True,
                key="global_mode_radio",
            )
        with gc2:
            new_gross = st.checkbox(
                "Gross\u2011up Tax for all invoices?",
                value=bool(prev_gross),
                disabled=(new_mode == MODE_NON_TDS),
                key="global_gross_checkbox",
            )
        with gc3:
            new_it_label = st.selectbox(
                "IT Act Rate (%)",
                options=_IT_RATE_LABELS,
                index=_IT_RATE_LABELS.index(_prev_label),
                key="global_it_rate_select",
            )
            new_it_rate = _IT_RATE_MAP.get(new_it_label, IT_ACT_RATE_DEFAULT)
        # Check for changes and apply reset/recompute if needed
        if new_mode != prev_mode or new_gross != prev_gross or new_it_rate != prev_it_rate:
            st.session_state["global_controls"]["mode"] = new_mode
            st.session_state["global_controls"]["gross_up"] = new_gross
            st.session_state["global_controls"]["it_act_rate"] = new_it_rate
            # Reset overrides and recompute existing invoices from extracted data
            _reset_invoice_states()
            st.info("Global settings updated. All overrides cleared and invoices recomputed.")

        # Batch actions
        action_col1, action_col2, action_col3 = st.columns([2, 2, 2])
        with action_col1:
            if st.button("Process All Invoices", type="primary"):
                processed = 0
                failed = 0
                total = len(invoices)
                for inv_id in invoices.keys():
                    _process_single_invoice(inv_id)
                    if invoices[inv_id]["status"] == "processed":
                        processed += 1
                    else:
                        failed += 1
                if failed == 0:
                    st.success(f"All {processed} invoices processed successfully.")
                else:
                    st.warning(f"Processed {processed} invoices with {failed} failures.")
        with action_col2:
            if st.button("Generate XML for All", disabled=not any(
                inv.get("status") == "processed" for inv in invoices.values()
            )):
                ok = 0
                failed = 0
                for inv_id in invoices.keys():
                    if invoices[inv_id].get("status") == "processed":
                        _generate_xml_for_invoice(inv_id)
                        if invoices[inv_id]["xml_status"] == "ok":
                            ok += 1
                        else:
                            failed += 1
                if ok > 0 and failed == 0:
                    st.success(f"Generated XML for all {ok} invoices.")
                elif ok > 0:
                    st.warning(f"Generated XML for {ok} invoices. {failed} failed.")
                else:
                    st.error("No invoices were ready to generate XML.")
        with action_col3:
            # Prepare ZIP for download; include only invoices with xml_status==ok
            ready_files: List[tuple[str, bytes]] = []
            skipped: List[str] = []
            for inv_id, inv in invoices.items():
                if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                    filename_stub = (
                        (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id)
                        .replace(" ", "_")
                    )
                    xml_filename = f"form15cb_{filename_stub}.xml"
                    ready_files.append((xml_filename, inv["xml_bytes"]))
                else:
                    skipped.append(inv_id)
            disabled = len(ready_files) == 0
            if st.download_button(
                "Download All XMLs as ZIP",
                data=generate_zip_from_xmls(ready_files) if ready_files else None,
                file_name="form15cb_batch.zip",
                mime="application/zip",
                disabled=disabled,
                key="download_all_zip",
            ):
                pass
            if not disabled:
                st.caption(f"{len(ready_files)} invoices included. Skipped {len(skipped)}.")

        # Divider before invoice tabs
        st.divider()
        st.subheader("Step 3 – Review and Edit Invoices")

        # Render each invoice in its own tab
        tab_ids = list(invoices.keys())
        tabs = st.tabs([invoices[i]["file_name"] for i in tab_ids])
        for tab, inv_id in zip(tabs, tab_ids):
            inv = invoices[inv_id]
            with tab:
                st.markdown(f"### Invoice: {inv['file_name']}")
                # Status indicators
                status = inv.get("status", "new")
                if status == "processed":
                    st.success("Processed")
                elif status == "failed":
                    st.error(f"Failed: {inv.get('error')}")
                else:
                    st.info("Not processed yet")
                # Local controls – per‑invoice override
                mode_override = inv.get("mode_override") or st.session_state["global_controls"].get("mode", MODE_TDS)
                gross_override = inv.get("gross_override") if inv.get("gross_override") is not None else st.session_state["global_controls"].get("gross_up", False)
                it_rate_effective = _effective_it_rate(inv)

                # Per-invoice IT Act Rate selectbox labels
                _INV_IT_LABELS = [
                    f"{r}% (Default)" if r == IT_ACT_RATE_DEFAULT else f"{r}%"
                    for r in IT_ACT_RATES
                ]
                _INV_IT_MAP = dict(zip(_INV_IT_LABELS, IT_ACT_RATES))
                _eff_label = next(
                    (lbl for lbl, val in _INV_IT_MAP.items() if val == it_rate_effective),
                    _INV_IT_LABELS[0],
                )

                c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                with c1:
                    selected_mode = st.radio(
                        "Mode (TDS/Non\u2011TDS)",
                        [MODE_TDS, MODE_NON_TDS],
                        index=0 if mode_override == MODE_TDS else 1,
                        horizontal=True,
                        key=f"mode_override_{inv_id}",
                    )
                with c2:
                    selected_gross = st.checkbox(
                        "Gross\u2011up for this invoice?",
                        value=bool(gross_override),
                        disabled=(selected_mode == MODE_NON_TDS),
                        key=f"gross_override_{inv_id}",
                    )
                with c3:
                    selected_it_label = st.selectbox(
                        "IT Act Rate (%)",
                        options=_INV_IT_LABELS,
                        index=_INV_IT_LABELS.index(_eff_label),
                        key=f"it_rate_override_{inv_id}",
                    )
                    selected_it_rate = _INV_IT_MAP.get(selected_it_label, IT_ACT_RATE_DEFAULT)
                with c4:
                    st.write("")
                # Apply overrides if they differ from the inherited values
                if selected_mode != _effective_mode(inv):
                    inv["mode_override"] = selected_mode
                else:
                    inv["mode_override"] = None
                # Only allow gross override when TDS
                if selected_mode != MODE_NON_TDS:
                    if selected_gross != _effective_gross(inv):
                        inv["gross_override"] = selected_gross
                    else:
                        inv["gross_override"] = None
                else:
                    inv["gross_override"] = None
                # IT Act rate override
                global_it = float(st.session_state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))
                if selected_it_rate != global_it:
                    inv["it_act_rate_override"] = selected_it_rate
                else:
                    inv["it_act_rate_override"] = None
                # Buttons for processing and XML generation
                bc1, bc2, bc3 = st.columns([2, 2, 2])
                with bc1:
                    if st.button("Process this invoice", key=f"process_{inv_id}"):
                        _process_single_invoice(inv_id)
                        if invoices[inv_id]["status"] == "processed":
                            st.success("Processed successfully.")
                        else:
                            st.error(f"Processing failed: {invoices[inv_id]['error']}")
                with bc2:
                    # Generate XML button
                    if st.button(
                        "Generate XML",
                        key=f"generate_xml_{inv_id}",
                        disabled=(inv.get("status") != "processed"),
                    ):
                        _generate_xml_for_invoice(inv_id)
                        if inv.get("xml_status") == "ok":
                            st.success("XML generated successfully.")
                        else:
                            st.error(f"XML generation failed: {inv.get('xml_error')}")
                with bc3:
                    # Download XML if generated
                    if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                        filename_stub = (
                            (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id)
                            .replace(" ", "_")
                        )
                        xml_filename = f"form15cb_{filename_stub}.xml"
                        st.download_button(
                            "Download XML",
                            data=inv["xml_bytes"],
                            file_name=xml_filename,
                            mime="application/xml",
                            key=f"download_xml_{inv_id}",
                        )
                        if st.button(
                            "Save XML to output folder",
                            key=f"save_xml_{inv_id}",
                        ):
                            path = write_xml_content(inv["xml_bytes"].decode("utf8"), filename=xml_filename)
                            st.success(f"Saved: {path}")
                # If processed, render the invoice form for editing
                if inv.get("status") == "processed" and inv.get("state") is not None:
                    # Recompute the state if overrides changed but the user hasn't clicked process again
                    # This ensures on‑the‑fly updates when toggling mode/gross in the tab
                    effective_mode = _effective_mode(inv)
                    effective_gross = _effective_gross(inv)
                    effective_rate = _effective_it_rate(inv)
                    current_it = str(inv.get("state", {}).get("form", {}).get("ItActRateSelected") or "")
                    if (
                        inv.get("state", {}).get("meta", {}).get("mode") != effective_mode
                        or bool(inv.get("state", {}).get("meta", {}).get("is_gross_up")) != bool(effective_gross)
                        or current_it != str(effective_rate)
                    ):
                        # Rebuild state without re-extracting
                        config = {
                            "currency_short": inv["excel"].get("currency", ""),
                            "exchange_rate": inv["excel"].get("exchange_rate", 0),
                            "mode": effective_mode,
                            "is_gross_up": effective_gross,
                            "tds_deduction_date": inv["excel"].get("dedn_date_tds", ""),
                            "it_act_rate": effective_rate,
                        }
                        try:
                            new_state = build_invoice_state(inv_id, inv["file_name"], inv["extracted"], config)
                            new_state = recompute_invoice(new_state)
                            inv["state"] = new_state
                            # Clear XML since numbers changed
                            inv["xml_bytes"] = None
                            inv["xml_status"] = "none"
                            inv["xml_error"] = None
                        except Exception as exc:
                            logger.exception("state_rebuild_failed invoice=%s", inv_id)
                            inv["error"] = str(exc)
                            inv["status"] = "failed"
                    # Finally render the form using existing batch_form_ui helper
                    from modules.batch_form_ui import render_invoice_tab
                    try:
                        new_state = render_invoice_tab(inv["state"])
                        # Recompute again in case user edits fields in UI
                        new_state = recompute_invoice(new_state)
                        inv["state"] = new_state
                        st.session_state["invoices"][inv_id] = inv
                    except Exception as exc:
                        logger.exception("render_invoice_failed invoice=%s", inv_id)
                        st.error(f"Rendering form failed: {exc}")

    # Footer
    st.markdown("---")
    st.caption(f"Version: {VERSION} | Last Updated: {LAST_UPDATED}")


if __name__ == "__main__":
    main()