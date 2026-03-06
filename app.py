from __future__ import annotations

# """
# Streamlit application entrypoint for the enhanced Form 15CB Batch Generator.

# This version supersedes the original application by supporting batch
# processing of invoices contained within a single ZIP archive accompanied
# by an Excel spreadsheet.  The new workflow allows users to upload a ZIP
# file, automatically derive the currency, exchange rate and date of
# deduction from the spreadsheet, set global defaults for TDS/Non‑TDS
# mode and gross‑up, and then process all invoices in one click.  Per
# invoice overrides remain available for exceptional cases, and XML
# generation is supported both individually and in batch.

# Key enhancements:

# * ZIP ingestion: the user uploads a single ZIP archive containing one
#   Excel (.xlsx) file and one or more invoice documents (.pdf/.jpg/.png).
#   The application reads the Excel to fetch currency, INR/FCY amounts,
#   calculates the exchange rate and extracts the posting date for the
#   TDS deduction.
# * Global controls: a pair of toggles allow the CA to set the default
#   TDS/Non‑TDS mode and whether gross‑up applies.  These values are
#   automatically applied to all invoices but can be overridden per
#   invoice.
# * Per‑invoice overrides: within each invoice tab the user can change
#   the mode and gross‑up settings if a particular invoice deviates from
#   the batch default.  Changing the global defaults clears all
#   overrides and recomputes derived values without re‑calling Gemini.
# * Robust date parsing: the ``Posting Date`` column of the Excel may
#   contain serial numbers, dates or strings in multiple formats.  The
#   parsed date populates ``DednDateTds`` in the XML.  Proposed
#   remittance date remains today+15 days.
# * Partial downloads: generating XML for all invoices includes only
#   those that have been processed successfully; invoices that failed or
#   remain unprocessed are skipped with a summary explaining why.

# Existing functionality—such as invoice text extraction via Gemini,
# master data lookup, tax computation and XML generation—are preserved
# and reused from the original modules.
# """

import io
import os
import time
from datetime import datetime
from typing import Dict, List

import streamlit as st

# Must be the absolute first Streamlit command — placed before any local
# module import so that modules which touch st.secrets on import (e.g.
# field_extractor) cannot race ahead of this call.
st.set_page_config(
    page_title="Form 15CB Batch Generator",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Custom CSS for the invoice details card
st.markdown("""
<style>
.excel-card {
    background-color: #262730;
    color: #ffffff;
    padding: 15px;
    border-radius: 10px;
    border: 1px solid #464855;
    margin-bottom: 20px;
}
.excel-card div {
    margin-bottom: 8px;
    display: flex;
    align-items: center;
}
.excel-card div:last-child {
    margin-bottom: 0;
}
.excel-card .label {
    font-weight: 600;
    margin-right: 10px;
    width: 140px;
    display: inline-block;
}
.excel-card .arrow {
    margin-right: 15px;
    color: #00d4ff;
}
.excel-card code {
    background-color: #1e1e26;
    color: #00ffcc;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 1.25em;
    font-weight: 600;
}

</style>
""", unsafe_allow_html=True)

from pdf2image import convert_from_bytes

from modules.zip_intake import parse_zip, read_excel, build_invoice_registry
from modules.form15cb_constants import IT_ACT_RATE_DEFAULT, IT_ACT_RATES, MODE_NON_TDS, MODE_TDS
from modules.invoice_state import build_invoice_state
from modules.invoice_calculator import invoice_state_to_xml_fields, recompute_invoice
from modules.invoice_gemini_extractor import (
    TEXT_EXTRACTION_MIN_THRESHOLD,
    extract_invoice_core_fields,
    extract_invoice_core_fields_from_image,
    gemini_extract_from_images_only,
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
LAST_UPDATED = "March 2026"

logger = get_logger()


# -----------------------------------------------------------------------------
# Session state initialisation
# -----------------------------------------------------------------------------

def _ensure_session_state() -> None:
    """Initialise keys in ``st.session_state`` that this app relies on."""
    if "mode" not in st.session_state:
        st.session_state["mode"] = "single"
    for mode in ["single_mode", "bulk_mode"]:
        if mode not in st.session_state:
            st.session_state[mode] = {
                "invoices": {},
                "global_controls": {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                },
                "ui_epoch": 0,
                "zip_context": None,
                "single_context": None,
            }

def _get_current_state() -> dict:
    mode = st.session_state.get("mode", "single")
    return st.session_state[f"{mode}_mode"]


def _validate_xml_fields(fields: Dict[str, str], mode: str = MODE_TDS, dedn_date_iso: str = "") -> List[str]:
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

    dtaa_claimed = (
        str(fields.get("TaxResidCert") or "").strip().upper() == "Y"
        and str(fields.get("OtherRemDtaa") or "").strip().upper() == "N"
    )
    if dtaa_claimed:
        for field in ["RateTdsADtaa", "TaxIncDtaa", "TaxLiablDtaa"]:
            if not str(fields.get(field) or "").strip():
                errors.append(f"{field} is required when DTAA is claimed.")
        rate_dtaa = str(fields.get("RateTdsADtaa") or "").strip()
        if rate_dtaa:
            try:
                if not float(rate_dtaa).is_integer():
                    errors.append("RateTdsADtaa must be an integer when DTAA is claimed.")
            except Exception:
                errors.append("RateTdsADtaa must be numeric.")
    else:
        for field in ["RateTdsSecB", "TaxLiablIt"]:
            if not str(fields.get(field) or "").strip():
                errors.append(f"{field} is required for non-DTAA computation.")

    if mode == MODE_TDS:
        if not str(fields.get("AmtPayForgnTds") or "").strip():
            errors.append("Amount of remittance must be entered.")
        if not str(fields.get("ActlAmtTdsForgn") or "").strip():
            errors.append("Actual amount remitted must be entered.")
        if not _is_valid_iso_date(dedn_date_iso):
            errors.append("Deduction Date (Posting Date) missing in Excel; cannot generate XML")

    return errors


def _is_valid_iso_date(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _get_invoice_dedn_date(inv: Dict[str, object]) -> str:
    excel = inv.get("excel") or {}
    if isinstance(excel, dict):
        return str(excel.get("dedn_date_tds") or "").strip()
    return ""


# -----------------------------------------------------------------------------
# Helper functions for overrides and recomputation
# -----------------------------------------------------------------------------

def _effective_mode(inv: Dict[str, object]) -> str:
    """Resolve the effective mode (TDS/Non‑TDS) for an invoice.
    Overrides the global setting only if an override is explicitly set in the inv record
    (legacy support, though UI no longer sets these).
    """
    return inv.get("mode_override") or _get_current_state()["global_controls"].get("mode", MODE_TDS)


def _effective_gross(inv: Dict[str, object]) -> bool:
    """Resolve the effective gross‑up flag for an invoice."""
    mode = _effective_mode(inv)
    if mode == MODE_NON_TDS:
        return False
    override = inv.get("gross_override")
    if override is not None:
        return bool(override)
    return bool(_get_current_state()["global_controls"].get("gross_up", False))


def _effective_it_rate(inv: Dict[str, object]) -> float:
    """Resolve the effective IT Act rate for an invoice."""
    override = inv.get("it_act_rate_override")
    if override is not None:
        return float(override)
    return float(_get_current_state()["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))


def _compute_config_sig(inv: Dict[str, object]) -> tuple:
    """Signature of config inputs that affect state rebuild from extracted data.

    Includes mode, gross-up, IT rate, currency, exchange rate and deduction
    date.  Does NOT include form edits — those are handled by
    ``recompute_invoice`` without a full rebuild.
    """
    ex = inv.get("excel") or {}
    try:
        currency = str(ex.get("currency") or "")
    except Exception:
        currency = ""
    try:
        fx = float(ex.get("exchange_rate") or 0.0)
    except Exception:
        fx = 0.0
    dedn = _get_invoice_dedn_date(inv)

    return (
        _effective_mode(inv),
        bool(_effective_gross(inv)),
        float(_effective_it_rate(inv)),
        currency,
        fx,
        dedn,
    )


def _rebuild_state_from_extracted(inv_id: str, inv: Dict[str, object]) -> None:
    """Rebuild invoice state from existing inv["extracted"] (NO Gemini calls).

    Clears XML because computed values may change.
    Updates inv["config_sig"].
    """
    if not inv.get("extracted"):
        return

    ex = inv.get("excel") or {}
    config = {
        "currency_short": ex.get("currency", ""),
        "exchange_rate": ex.get("exchange_rate", 0),
        "mode": _effective_mode(inv),
        "is_gross_up": _effective_gross(inv),
        "tds_deduction_date": _get_invoice_dedn_date(inv),  # Posting Date -> DednDateTds
        "it_act_rate": _effective_it_rate(inv),
    }

    state = build_invoice_state(inv_id, inv["file_name"], inv["extracted"], config)
    state = recompute_invoice(state)
    inv["state"] = state
    inv["status"] = "processed"
    inv["error"] = None

    # Clear XML because numbers could change
    inv["xml_bytes"] = None
    inv["xml_status"] = "none"
    inv["xml_error"] = None

    inv["config_sig"] = _compute_config_sig(inv)


def _reset_invoice_states() -> None:
    """Recompute invoices after a global change, preserving per-invoice overrides.

    When the user toggles the global mode, gross-up or IT Act rate controls
    we recompute derived state from existing extracted data.  Per-invoice
    mode and gross-up overrides are intentionally preserved so that
    individual invoice customisations survive global changes.  Only the
    IT Act rate override is cleared because there is no per-invoice IT
    rate UI yet.  No Gemini calls occur during this function.
    """
    invoices = _get_current_state()["invoices"]
    for inv_id, inv in invoices.items():
        # Per-invoice mode_override and gross_override are intentionally
        # preserved so that individual invoice customisations survive
        # global changes.  IT Act rate override is cleared because there
        # is no per-invoice IT Act rate UI yet.
        inv["it_act_rate_override"] = None

        if inv.get("extracted"):
            # memoized rebuild: only rebuild if config signature changed
            new_sig = _compute_config_sig(inv)
            old_sig = inv.get("config_sig")
            if new_sig != old_sig:
                try:
                    _rebuild_state_from_extracted(inv_id, inv)
                except Exception as exc:
                    inv["state"] = None
                    inv["status"] = "failed"
                    inv["error"] = str(exc)
                    inv["xml_bytes"] = None
                    inv["xml_status"] = "none"
                    inv["xml_error"] = None
            else:
                # no change; keep existing state
                inv["status"] = inv.get("status") or "processed"
                if inv.get("status") != "failed":
                    inv["error"] = None
        else:
            # not yet processed
            inv["state"] = None
            inv["status"] = "new"
            inv["error"] = None
            inv["xml_bytes"] = None
            inv["xml_status"] = "none"
            inv["xml_error"] = None


def _process_single_invoice(inv_id: str) -> None:
    """Run extraction, state building and recompute for one invoice.

    Updates the invoice record in place with extracted data, state and
    status.  Uses the current effective mode and gross‑up settings.
    """
    inv = _get_current_state()["invoices"][inv_id]
    if inv.get("status") == "processing":
        return
    file_bytes = inv["file_bytes"]
    file_name = inv["file_name"]
    inv["status"] = "processing"
    inv["error"] = None
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
        "tds_deduction_date": _get_invoice_dedn_date(inv),
        "it_act_rate": _effective_it_rate(inv),
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
                    extracted = extract_invoice_core_fields(text, invoice_id=inv_id)
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
                            page_extracted = extract_invoice_core_fields_from_image(image_bytes, invoice_id=inv_id)
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
                                    text_extracted = extract_invoice_core_fields(page_ocr, invoice_id=inv_id)
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
                            extracted = extract_invoice_core_fields_from_image(file_bytes, invoice_id=inv_id)
                            text = extract_text_from_image_file(file_bytes) or ""
                        except Exception:
                            logger.exception("pdf_image_ocr_fallback_failed file=%s", file_name)
                            extracted = {}
                            text = ""
                        if not extracted.get("_raw_invoice_text"):
                            extracted["_raw_invoice_text"] = text
            else:
                # Image uploads (jpg/png)
                extracted = extract_invoice_core_fields_from_image(file_bytes, invoice_id=inv_id)
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
            # Set config signature so per-tab memoization doesn't re-rebuild
            inv["config_sig"] = _compute_config_sig(inv)
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
    inv = _get_current_state()["invoices"][inv_id]
    if inv.get("state") is None:
        inv["xml_status"] = "failed"
        inv["xml_error"] = "Invoice has not been processed."
        return
    # Determine current mode (should match build state)
    mode = _effective_mode(inv)
    xml_fields = invoice_state_to_xml_fields(inv["state"])
    dedn_iso = str(inv.get("state", {}).get("form", {}).get("DednDateTds") or "").strip()
    errors = _validate_xml_fields(xml_fields, mode=mode, dedn_date_iso=dedn_iso)
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

def render_bulk_invoice_page() -> None:
    st.title("Form 15CB Batch Generator (ZIP-enabled)")
    state = _get_current_state()

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
            state.get("zip_context") is None
            or state["zip_context"].get("zip_name") != uploaded_zip.name
        ):
            try:
                excel_name, excel_bytes, invoice_files = parse_zip(uploaded_zip.getvalue())
                df = read_excel(excel_bytes)
                invoices = build_invoice_registry(df, invoice_files)
                state["invoices"] = invoices
                # Defensive: explicitly clear per-invoice overrides in case of ID collisions between ZIPs
                for inv in state["invoices"].values():
                    inv["mode_override"] = None
                    inv["gross_override"] = None
                    inv["it_act_rate_override"] = None
                    inv["config_sig"] = None

                state["zip_context"] = {
                    "zip_name": uploaded_zip.name,
                    "excel_name": excel_name,
                    "loaded_at": time.time(),
                }
                # Reset global controls to defaults
                state["global_controls"] = {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                }
                st.success(
                    f"Loaded {len(invoices)} invoices from {uploaded_zip.name}. "
                    f"Excel file: {excel_name}"
                )
                # Clear stale global widget states so they reset to defaults
                st.session_state.pop("global_mode_radio", None)
                st.session_state.pop("global_gross_checkbox", None)
                st.session_state.pop("global_it_rate_select", None)
                state["ui_epoch"] = state.get("ui_epoch", 0) + 1
                st.rerun()
            except Exception as exc:
                state["invoices"] = {}
                state["zip_context"] = None
                logger.exception("zip_upload_failed")
                st.error(f"Failed to parse ZIP: {exc}")

    invoices = state.get("invoices", {})
    if invoices:
        # Global controls
        st.subheader("Step 2 – Configure Defaults and Process")
        prev_mode = state["global_controls"].get("mode", MODE_TDS)
        prev_gross = state["global_controls"].get("gross_up", False)
        prev_it_rate = state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT)

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
                "Tax Mode",
                [MODE_TDS, MODE_NON_TDS],
                index=0 if prev_mode == MODE_TDS else 1,
                horizontal=True,
                key="global_mode_radio",
            )
        with gc2:
            new_gross = st.checkbox(
                "💰 Gross\u2011up tax (company bears tax)",
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
            state["global_controls"]["mode"] = new_mode
            state["global_controls"]["gross_up"] = new_gross
            state["global_controls"]["it_act_rate"] = new_it_rate
            state["ui_epoch"] += 1
            # Reset overrides and recompute existing invoices from extracted data
            _reset_invoice_states()
            st.info("Global settings updated. Invoices recomputed. Existing per-invoice overrides were preserved.")
            st.rerun()

        # Batch actions
        def _is_pending(inv: Dict[str, object]) -> bool:
            return inv.get("status") not in ("processed", "failed")

        def _is_processed(inv: Dict[str, object]) -> bool:
            return inv.get("status") == "processed"

        def _is_xml_missing(inv: Dict[str, object]) -> bool:
            return inv.get("xml_status") != "ok" or not inv.get("xml_bytes")

        def _is_xml_ready(inv: Dict[str, object]) -> bool:
            return inv.get("xml_status") == "ok" and bool(inv.get("xml_bytes"))

        action_col1, action_col2, action_col3, action_col4 = st.columns([2, 2, 2, 2])
        with action_col1:
            if st.button("Process Pending Only", type="primary"):
                processed_n = 0
                failed_n = 0
                pending_ids = [inv_id for inv_id, inv in invoices.items() if _is_pending(inv)]
                if not pending_ids:
                    st.info("No pending invoices.")
                else:
                    for inv_id in pending_ids:
                        _process_single_invoice(inv_id)
                        if invoices[inv_id]["status"] == "processed":
                            processed_n += 1
                        else:
                            failed_n += 1
                    if failed_n == 0:
                        st.success(f"Processed {processed_n} pending invoices.")
                    else:
                        st.warning(f"Processed {processed_n} pending invoices. {failed_n} failed.")

        with action_col2:
            if st.button("Process All Invoices"):
                processed_n = 0
                failed_n = 0
                for inv_id in invoices.keys():
                    _process_single_invoice(inv_id)
                    if invoices[inv_id]["status"] == "processed":
                        processed_n += 1
                    else:
                        failed_n += 1
                if failed_n == 0:
                    st.success(f"All {processed_n} invoices processed successfully.")
                else:
                    st.warning(f"Processed {processed_n} invoices with {failed_n} failures.")

        with action_col3:
            if st.button(
                "Generate XML (Missing Only)",
                disabled=not any(_is_processed(inv) and _is_xml_missing(inv) for inv in invoices.values()),
            ):
                ok_n = 0
                failed_n = 0
                target_ids = [
                    inv_id for inv_id, inv in invoices.items()
                    if _is_processed(inv) and _is_xml_missing(inv)
                ]
                if not target_ids:
                    st.info("No invoices need XML generation.")
                else:
                    for inv_id in target_ids:
                        _generate_xml_for_invoice(inv_id)
                        if invoices[inv_id]["xml_status"] == "ok":
                            ok_n += 1
                        else:
                            failed_n += 1
                    if failed_n == 0:
                        st.success(f"Generated XML for {ok_n} invoices.")
                    else:
                        st.warning(f"Generated XML for {ok_n} invoices. {failed_n} failed.")

        with action_col4:
            ready_files: List[tuple[str, bytes]] = []
            skipped: List[str] = []
            for inv_id, inv in invoices.items():
                if _is_xml_ready(inv):
                    filename_stub = (
                        (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id)
                        .replace(" ", "_")
                    )
                    xml_filename = f"form15cb_{filename_stub}.xml"
                    ready_files.append((xml_filename, inv["xml_bytes"]))
                else:
                    skipped.append(inv_id)

            zip_data = generate_zip_from_xmls(ready_files) if ready_files else b""
            st.download_button(
                "Download XML ZIP",
                data=zip_data,
                file_name="form15cb_batch.zip",
                mime="application/zip",
                disabled=(len(ready_files) == 0),
                key="download_all_zip",
            )
            if ready_files:
                st.caption(f"{len(ready_files)} included. {len(skipped)} skipped.")

        # Divider before invoice tabs
        st.divider()
        st.subheader("Step 3 – Review and Edit Invoices")

        # --- Batch summary + filter (CA-friendly) ---
        total = len(invoices)
        processed = sum(1 for inv in invoices.values() if inv.get("status") == "processed")
        failed = sum(1 for inv in invoices.values() if inv.get("status") == "failed")
        xml_ready = sum(1 for inv in invoices.values() if inv.get("xml_status") == "ok" and inv.get("xml_bytes"))
        not_processed = sum(1 for inv in invoices.values() if inv.get("status") not in ("processed", "failed"))

        # Count "Deduction date missing" only when effective mode is TDS (since Non-TDS doesn't need it)
        dedn_missing = 0
        for inv in invoices.values():
            if _effective_mode(inv) != MODE_TDS:
                continue
            ex = inv.get("excel", {}) or {}
            state_meta = (inv.get("state", {}) or {}).get("meta", {}) if isinstance(inv.get("state"), dict) else {}
            flag = bool((state_meta or {}).get("dedn_date_missing"))
            if flag or not _is_valid_iso_date(str(ex.get("dedn_date_tds") or "")):
                dedn_missing += 1

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Total", total)
        c2.metric("Processed", processed)
        c3.metric("Failed", failed)
        c4.metric("XML Ready", xml_ready)
        c5.metric("Not processed", not_processed)
        c6.metric("Deduction date missing", dedn_missing)

        filter_choice = st.selectbox(
            "Show invoices",
            ["All", "Not processed", "Processed", "Failed", "XML Ready", "Deduction date missing"],
            index=0,
            key="invoice_filter_choice",
        )

        tab_ids_all = list(invoices.keys())

        def _matches_filter(inv: Dict[str, object]) -> bool:
            if filter_choice == "All":
                return True
            if filter_choice == "Not processed":
                return inv.get("status") not in ("processed", "failed")
            if filter_choice == "Processed":
                return inv.get("status") == "processed"
            if filter_choice == "Failed":
                return inv.get("status") == "failed"
            if filter_choice == "XML Ready":
                return bool(inv.get("xml_status") == "ok" and inv.get("xml_bytes"))
            if filter_choice == "Deduction date missing":
                if _effective_mode(inv) != MODE_TDS:
                    return False
                ex = inv.get("excel", {}) or {}
                state_meta = (inv.get("state", {}) or {}).get("meta", {}) if isinstance(inv.get("state"), dict) else {}
                flag = bool((state_meta or {}).get("dedn_date_missing"))
                return flag or not _is_valid_iso_date(str(ex.get("dedn_date_tds") or ""))
            return True

        tab_ids = [inv_id for inv_id in tab_ids_all if _matches_filter(invoices[inv_id])]
        if not tab_ids:
            st.info("No invoices match the selected filter.")

        tabs = st.tabs([invoices[i]["file_name"] for i in tab_ids]) if tab_ids else []
        for tab, inv_id in zip(tabs, tab_ids):
            inv = invoices[inv_id]
            with tab:
                st.markdown(f"### Invoice: {inv['file_name']}")
                # Status indicators
                status = inv.get("status", "new")
                if status == "processed":
                    st.success("✅ Invoice processed successfully")
                elif status == "failed":
                    st.error(f"❌ Processing failed: {inv.get('error')}")
                else:
                    st.info("⏳ Invoice not processed yet")
                # Excel metadata block
                st.markdown("#### 📊 Invoice details (from Excel)")
                ex = inv.get("excel", {})
                
                currency = ex.get("currency") or "—"
                exchange_rate = ex.get("exchange_rate")
                exchange_rate_str = f"{float(exchange_rate):.4f}" if exchange_rate and float(exchange_rate) > 0 else "—"
                dedn_date = ex.get("dedn_date_tds") or "—"

                with st.container(border=True):
                    st.markdown(f'''
                    <div class="excel-card">
                        <div><span class="label">Currency</span> <span class="arrow">→</span> <code>{currency}</code></div>
                        <div><span class="label">Exchange Rate</span> <span class="arrow">→</span> <code>{exchange_rate_str}</code></div>
                        <div><span class="label">Deduction Date</span> <span class="arrow">→</span> <code>{dedn_date}</code></div>
                    </div>
                    ''', unsafe_allow_html=True)
                state_meta = inv.get("state", {}).get("meta", {}) if isinstance(inv.get("state"), dict) else {}
                dedn_missing_flag = bool((state_meta if isinstance(state_meta, dict) else {}).get("dedn_date_missing"))
                if dedn_missing_flag or not _is_valid_iso_date(str(ex.get("dedn_date_tds") or "")):
                    st.warning("Deduction Date (Posting Date) missing in Excel; XML generation is blocked for this invoice.")

                # ── Per-invoice Control Card ──
                st.markdown("#### ✅ Invoice controls")
                with st.container(border=True):
                    global_mode = state["global_controls"]["mode"]
                    global_gross = state["global_controls"]["gross_up"]

                    # Use effective values so radio/checkbox reflect existing overrides
                    effective_mode_val = _effective_mode(inv)
                    effective_gross_val = _effective_gross(inv)
                    epoch = state.get("ui_epoch", 0)

                    ov_c1, ov_c2 = st.columns(2)
                    with ov_c1:
                        selected_mode = st.radio(
                            "Tax Mode",
                            [MODE_TDS, MODE_NON_TDS],
                            index=0 if effective_mode_val == MODE_TDS else 1,
                            horizontal=True,
                            key=f"ov_mode_{inv_id}_{epoch}",
                        )

                    gross_key = f"ov_gross_{inv_id}_{epoch}"
                    last_mode_key = f"ov_last_mode_{inv_id}_{epoch}"

                    # Track previous mode for this invoice in this epoch
                    prev_mode = st.session_state.get(last_mode_key, effective_mode_val)

                    prev_gross_key = f"ov_prev_gross_{inv_id}_{epoch}"
                    prev_gross_val = st.session_state.get(gross_key, effective_gross_val)

                    # If switching into NON_TDS, remember last gross (from TDS)
                    if selected_mode == MODE_NON_TDS and prev_mode != MODE_NON_TDS:
                        st.session_state[prev_gross_key] = bool(prev_gross_val)

                    if selected_mode == MODE_NON_TDS:
                        st.session_state[gross_key] = False
                    else:
                        # Coming back from NON_TDS -> TDS, re-seed gross once to remembered/default
                        if prev_mode == MODE_NON_TDS:
                            desired_default = st.session_state.get(prev_gross_key, global_gross)
                            st.session_state[gross_key] = bool(desired_default)

                    st.session_state[last_mode_key] = selected_mode

                    with ov_c2:
                        selected_gross = st.checkbox(
                            "💰 Gross\u2011up tax (company bears tax)",
                            value=st.session_state.get(gross_key, effective_gross_val),
                            disabled=(selected_mode == MODE_NON_TDS),
                            key=gross_key,
                        )

                    # Write overrides (None = inherit global)
                    inv["mode_override"] = selected_mode if selected_mode != global_mode else None
                    if selected_mode == MODE_NON_TDS:
                        inv["gross_override"] = None  # forced off
                    else:
                        inv["gross_override"] = selected_gross if selected_gross != global_gross else None

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
                            data=inv["xml_bytes"] if inv.get("xml_bytes") else b"",
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
                    # Memoized rebuild: only rebuild from extracted when config
                    # (mode/gross/IT rate/currency/fx/dedn_date) changed.
                    # User form edits are handled by recompute_invoice below.
                    new_sig = _compute_config_sig(inv)
                    old_sig = inv.get("config_sig")
                    if new_sig != old_sig:
                        try:
                            _rebuild_state_from_extracted(inv_id, inv)
                            st.caption("🔄 Recomputed with custom settings (no re-extraction).")
                        except Exception as exc:
                            logger.exception("state_rebuild_failed invoice=%s", inv_id)
                            inv["error"] = str(exc)
                            inv["status"] = "failed"
                    # Render the form using existing batch_form_ui helper
                    from modules.batch_form_ui import render_invoice_tab
                    try:
                        old_form = dict(inv["state"].get("form", {}))
                        new_state = render_invoice_tab(inv["state"], show_header=False)
                        new_form = new_state.get("form", {})
                        for k in ["CountryRemMadeSecb", "NatureRemCategory", "RevPurCategory", "RevPurCode", "RateTdsADtaa", "BasisDeterTax", "TaxPayGrossSecb"]:
                            if k in new_form and k in old_form and new_form[k] != old_form[k]:
                                logger.info("ui_field_changed invoice_id=%s field=%s old=%r new=%r", inv_id, k, old_form[k], new_form[k])
                        # Snapshot key computed fields before recompute
                        form = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                        _snap_keys = (
                            "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                            "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                            "BasisDeterTax", "RateTdsADtaa",
                        )
                        before = tuple(str(form.get(k) or "") for k in _snap_keys)
                        # Recompute tax fields in case user edits (e.g. DTAA rate)
                        new_state = recompute_invoice(new_state)
                        form_after = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                        after = tuple(str(form_after.get(k) or "") for k in _snap_keys)
                        inv["state"] = new_state
                        # Only clear XML when computed values actually changed
                        if after != before:
                            inv["xml_bytes"] = None
                            inv["xml_status"] = "none"
                            inv["xml_error"] = None
                        state["invoices"][inv_id] = inv
                    except Exception as exc:
                        logger.exception("render_invoice_failed invoice=%s", inv_id)
                        st.error(f"Rendering form failed: {exc}")

    # Footer
    st.markdown("---")
    st.caption(f"Version: {VERSION} | Last Updated: {LAST_UPDATED}")



import io
import os
import re
import math
from modules.zip_intake import read_excel, _normalize_reference, _to_float, parse_excel_date

def render_mode_switcher() -> None:
    mode = st.session_state.get("mode", "single")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📄 Process One Invoice", type="primary" if mode == "single" else "secondary", use_container_width=True):
            st.session_state["mode"] = "single"
            st.rerun()
    with col2:
        if st.button("🗂 Process Many Invoices", type="primary" if mode == "bulk" else "secondary", use_container_width=True):
            st.session_state["mode"] = "bulk"
            st.rerun()

def render_single_invoice_page() -> None:
    st.title("Form 15CB - Single Invoice")
    state = _get_current_state()
    invoices = state["invoices"]
    
    st.subheader("Step 1 – Upload Invoice & Excel")
    col1, col2 = st.columns(2)
    with col1:
        uploaded_inv = st.file_uploader("Upload Invoice", type=["pdf", "jpg", "jpeg", "png"], key="single_inv_upload")
    with col2:
        uploaded_excel = st.file_uploader("Upload Excel", type=["xlsx"], key="single_excel_upload")
        
    if uploaded_inv and uploaded_excel:
        if state.get("single_context") != uploaded_inv.name + "|" + uploaded_excel.name:
            try:
                df = read_excel(uploaded_excel.getvalue())
                
                stem = os.path.splitext(uploaded_inv.name)[0]
                norm_stem = _normalize_reference(stem)
                
                # Check matches
                matches = 0
                if not df.empty:
                    for _, row in df.fillna("").iterrows():
                        if _normalize_reference(row.get("Reference")) == norm_stem:
                            matches += 1
                
                if matches == 0:
                    st.error(f"Could not find matching row in Excel for invoice reference: {stem}")
                    return
                elif matches > 1:
                    st.warning(f"Multiple rows found in Excel for reference {stem}. Using the first one.")
                
                from modules.zip_intake import build_invoice_registry
                invoices_dict = build_invoice_registry(df, [(uploaded_inv.name, uploaded_inv.getvalue())])
                
                if stem not in invoices_dict:
                    st.error(f"Process failed. Could not prepare invoice state for {stem}.")
                    return
                
                state["invoices"] = {stem: invoices_dict[stem]}
                state["single_context"] = uploaded_inv.name + "|" + uploaded_excel.name
                state["global_controls"] = {
                    "mode": MODE_TDS,
                    "gross_up": False,
                    "it_act_rate": IT_ACT_RATE_DEFAULT,
                }
                st.success("Files loaded and matched successfully.")
                st.rerun()
            except Exception as e:
                import traceback
                st.error(f"Error processing files: {e}\n{traceback.format_exc()}")
                return

        invoices = state.get("invoices", {})
        if invoices:
            inv_id = list(invoices.keys())[0]
            inv = invoices[inv_id]
            
            if inv["status"] == "new":
                st.subheader("Step 2 – Configure and Process")
                
                prev_mode = state["global_controls"].get("mode", MODE_TDS)
                prev_gross = state["global_controls"].get("gross_up", False)
                
                gc1, gc2 = st.columns(2)
                with gc1:
                    new_mode = st.radio(
                        "Tax Mode",
                        [MODE_TDS, MODE_NON_TDS],
                        index=0 if prev_mode == MODE_TDS else 1,
                        horizontal=True,
                        key="single_mode_radio",
                    )
                with gc2:
                    new_gross = st.checkbox(
                        "💰 Gross\u2011up tax (company bears tax)",
                        value=bool(prev_gross),
                        disabled=(new_mode == MODE_NON_TDS),
                        key="single_gross_checkbox",
                    )
                    if new_mode == MODE_NON_TDS:
                        new_gross = False
                
                if new_mode != prev_mode or new_gross != prev_gross:
                    state["global_controls"]["mode"] = new_mode
                    state["global_controls"]["gross_up"] = new_gross
                    
                if st.button("Process Invoice", type="primary"):
                    _process_single_invoice(inv_id)
                    st.rerun()
                st.info("Processing...")
            elif inv["status"] == "failed":
                st.error(f"Processing failed: {inv.get('error')}")
            elif inv["status"] == "processed":
                st.subheader("Step 3 – Review and Generate XML")
                
                ex = inv.get("excel", {})
                currency = ex.get("currency") or "—"
                exchange_rate = ex.get("exchange_rate")
                exchange_rate_str = f"{float(exchange_rate):.4f}" if exchange_rate and float(exchange_rate) > 0 else "—"
                dedn_date = ex.get("dedn_date_tds") or "—"
                with st.container(border=True):
                    st.markdown(f'''
                    <div class="excel-card">
                        <div><span class="label">Currency</span> <span class="arrow">→</span> <code>{currency}</code></div>
                        <div><span class="label">Exchange Rate</span> <span class="arrow">→</span> <code>{exchange_rate_str}</code></div>
                        <div><span class="label">Deduction Date</span> <span class="arrow">→</span> <code>{dedn_date}</code></div>
                    </div>
                    ''', unsafe_allow_html=True)
                
                # Render the invoice form for editing
                from modules.batch_form_ui import render_invoice_tab
                try:
                    old_form = dict(inv["state"].get("form", {}))
                    new_state = render_invoice_tab(inv["state"], show_header=False)
                    new_form = new_state.get("form", {})
                    
                    form = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                    _snap_keys = (
                        "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                        "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                        "BasisDeterTax", "RateTdsADtaa",
                    )
                    before = tuple(str(form.get(k) or "") for k in _snap_keys)
                    new_state = recompute_invoice(new_state)
                    form_after = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                    after = tuple(str(form_after.get(k) or "") for k in _snap_keys)
                    inv["state"] = new_state
                    if after != before:
                        inv["xml_bytes"] = None
                        inv["xml_status"] = "none"
                        inv["xml_error"] = None
                    state["invoices"][inv_id] = inv
                except Exception as exc:
                    st.error(f"Rendering form failed: {exc}")

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Generate XML", type="primary"):
                        _generate_xml_for_invoice(inv_id)
                        if inv.get("xml_status") == "ok":
                            st.success("XML generated successfully.")
                        else:
                            st.error(f"XML generation failed: {inv.get('xml_error')}")
                with c2:
                    if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                        filename_stub = (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id).replace(" ", "_")
                        st.download_button(
                            "Download XML",
                            data=inv["xml_bytes"],
                            file_name=f"form15cb_{filename_stub}.xml",
                            mime="application/xml"
                        )

def main() -> None:
    _ensure_session_state()
    render_mode_switcher()
    mode = st.session_state.get("mode", "single")
    if mode == "single":
        render_single_invoice_page()
    else:
        render_bulk_invoice_page()


if __name__ == "__main__":
    main()

