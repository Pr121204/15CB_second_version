from __future__ import annotations

import io
import os
import time
from datetime import datetime
from typing import Any, Dict, List

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
from modules.form15cb_constants import ALL_CURRENCY_OPTIONS, IT_ACT_RATE_DEFAULT, IT_ACT_RATES, MODE_NON_TDS, MODE_TDS, SHORT_CURRENCY_OPTIONS
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
from modules.amount_extractor import extract_amount_candidate_from_pages


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
    for mode in ["single_mode", "bulk_mode", "no_excel_mode"]:
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
            errors.append("Date of Deduction of TDS is missing or invalid; cannot generate XML")

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


def _get_invoice_dedn_date(inv: Dict[str, Any]) -> str:
    excel = inv.get("excel") or {}
    if isinstance(excel, dict):
        return str(excel.get("dedn_date_tds") or "").strip()
    return ""


# -----------------------------------------------------------------------------
# Helper functions for overrides and recomputation
# -----------------------------------------------------------------------------

def _effective_mode(inv: Dict[str, Any]) -> str:
    """Resolve the effective mode (TDS/Non‑TDS) for an invoice.
    Overrides the global setting only if an override is explicitly set in the inv record
    (legacy support, though UI no longer sets these).
    """
    return inv.get("mode_override") or _get_current_state()["global_controls"].get("mode", MODE_TDS)


def _effective_gross(inv: Dict[str, Any]) -> bool:
    """Resolve the effective gross‑up flag for an invoice."""
    mode = _effective_mode(inv)
    if mode == MODE_NON_TDS:
        return False
    override = inv.get("gross_override")
    if override is not None:
        return bool(override)
    return bool(_get_current_state()["global_controls"].get("gross_up", False))


def _effective_it_rate(inv: Dict[str, Any]) -> float:
    """Resolve the effective IT Act rate for an invoice."""
    override = inv.get("it_act_rate_override")
    if override is not None:
        return float(override)
    return float(_get_current_state()["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT))


def _compute_config_sig(inv: Dict[str, Any]) -> tuple:
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


def _rebuild_state_from_extracted(inv_id: str, inv: Dict[str, Any]) -> None:
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
    state_ref = _get_current_state()
    logger.info("reset_invoice_states_started mode=%s", st.session_state.get("mode"))
    invoices = state_ref["invoices"]
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
                    logger.info("rebuilding_state invoice_id=%s", inv_id)
                    _rebuild_state_from_extracted(inv_id, inv)
                except Exception as exc:
                    logger.exception("rebuild_failed invoice_id=%s", inv_id)
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


import threading
from streamlit.runtime.scriptrunner import add_script_run_ctx


def _resolve_expected_billing_currency(config: Dict[str, Any], extracted: Dict[str, str]) -> str:
    source_currency = str(config.get("currency_short") or "").strip().upper()
    if source_currency:
        return source_currency
    return str(extracted.get("currency_short") or "").strip().upper()


def _apply_safe_deterministic_amount_override(
    *,
    extracted: Dict[str, Any],
    pages_text: List[str],
    expected_currency: str,
    invoice_id: str,
    file_name: str,
    source_mode: str,
) -> None:
    candidate = extract_amount_candidate_from_pages(
        pages_text,
        expected_currency=expected_currency,
    )
    if not candidate:
        return

    candidate_amount = str(candidate.get("amount") or "").strip()
    if not candidate_amount:
        return

    gemini_amount = str(extracted.get("amount") or "").strip()
    gemini_currency = str(extracted.get("currency_short") or "").strip().upper()
    billing_currency = str(expected_currency or gemini_currency).strip().upper()
    candidate_currency = str(candidate.get("currency") or "").strip().upper()
    
    if not billing_currency and candidate_currency:
        billing_currency = candidate_currency
        
    informational = bool(candidate.get("is_informational"))
    currency_mismatch = bool(billing_currency and candidate_currency and billing_currency != candidate_currency)
    
    # --- GUARD 1: Date pattern rejection ---
    date_patterns = [
        r'^\d{1,2}\.\d{2}$',          # e.g. "31.01"
        r'^\d{1,2}\.\d{2}\.\d{4}$',  # e.g. "31.01.2026"
        r'^\d{1,2}/\d{2}/\d{4}$'     # e.g. "31/01/2026"
    ]
    is_date_like = any(re.match(pat, candidate_amount) for pat in date_patterns)
    if is_date_like:
        logger.warning(
            "amount_override_rejected invoice_id=%s file=%s reason=date_pattern_match gemini_amount=%s candidate=%s label=%s",
            invoice_id, file_name, gemini_amount, candidate_amount, candidate.get("label", "")
        )
        return

    # --- GUARD 2: Minimum plausibility by currency ---
    try:
        val_float = float(candidate_amount)
        if billing_currency == 'JPY' and val_float < 1000:
            logger.warning(
                "amount_override_rejected invoice_id=%s file=%s reason=jpy_below_1000 gemini_amount=%s candidate=%s label=%s",
                invoice_id, file_name, gemini_amount, candidate_amount, candidate.get("label", "")
            )
            return
        if billing_currency in ('USD', 'EUR', 'GBP', 'SGD') and val_float < 10:
            logger.warning(
                "amount_override_rejected invoice_id=%s file=%s reason=major_ccy_below_10 gemini_amount=%s candidate=%s label=%s",
                invoice_id, file_name, gemini_amount, candidate_amount, candidate.get("label", "")
            )
            return
        if billing_currency == 'INR' and val_float < 100:
            logger.warning(
                "amount_override_rejected invoice_id=%s file=%s reason=inr_below_100 gemini_amount=%s candidate=%s label=%s",
                invoice_id, file_name, gemini_amount, candidate_amount, candidate.get("label", "")
            )
            return
    except (ValueError, TypeError):
        pass

    # --- PLOTTING GUARD CONTEXT ---
    try:
        g_float = float(gemini_amount) if gemini_amount and str(gemini_amount).strip() else 0.0
    except (ValueError, TypeError):
        g_float = 0.0
    
    gemini_has_value = g_float > 0

    # --- GUARD 3: Don't downgrade a large Gemini amount ---
    if gemini_has_value:
        try:
            c_float = float(candidate_amount)
            if c_float < (g_float * 0.01):
                logger.warning(
                    "amount_override_rejected invoice_id=%s file=%s reason=candidate_too_small_vs_gemini gemini=%s candidate=%s label=%s",
                    invoice_id, file_name, gemini_amount, candidate_amount, candidate.get("label", "")
                )
                return
        except (ValueError, TypeError):
            pass

    # --- GUARD 4: Source label trust hierarchy ---
    if gemini_has_value:
        trusted_labels = {'invoice_total', 'grand_total', 'net_amount_final'}
        current_label = candidate.get("label", "")
        if current_label not in trusted_labels:
            logger.warning(
                "amount_override_rejected invoice_id=%s file=%s reason=untrusted_label gemini_amount=%s candidate=%s label=%s",
                invoice_id, file_name, gemini_amount, candidate_amount, current_label
            )
            return

    safe_override = (not informational) and (not currency_mismatch)

    if safe_override:
        if gemini_amount != candidate_amount:
            logger.info(
                "amount_override_applied invoice_id=%s file=%s mode=%s old=%s new=%s candidate_currency=%s billing_currency=%s label=%s page=%s",
                invoice_id,
                file_name,
                source_mode,
                gemini_amount,
                candidate_amount,
                candidate_currency,
                billing_currency,
                candidate.get("label", ""),
                candidate.get("page_number", 0),
            )
        extracted["amount"] = candidate_amount
        if candidate_currency and not extracted.get("currency_short"):
            extracted["currency_short"] = candidate_currency
        elif billing_currency and not extracted.get("currency_short"):
            extracted["currency_short"] = billing_currency
            logger.info(
                "currency_propagated_from_override invoice_id=%s currency=%s",
                invoice_id, billing_currency,
            )
        extracted["amount_source"] = "deterministic_total"
        extracted["_deterministic_amount_page"] = str(candidate.get("page_number") or "")
        return

    reason = "informational_amount" if informational else "currency_mismatch"
    logger.warning(
        "amount_override_skipped invoice_id=%s file=%s mode=%s reason=%s gemini_amount=%s candidate_amount=%s candidate_currency=%s billing_currency=%s label=%s page=%s",
        invoice_id,
        file_name,
        source_mode,
        reason,
        gemini_amount,
        candidate_amount,
        candidate_currency,
        billing_currency,
        candidate.get("label", ""),
        candidate.get("page_number", 0),
    )
    extracted["requires_review_ai"] = True


def _process_invoice_worker(inv: dict, inv_id: str, file_bytes: bytes, file_name: str, config: dict) -> None:
    try:
        extracted: Dict[str, Any] = {}
        if file_name.lower().endswith(".pdf"):
            try:
                _pt = extract_text_from_pdf(io.BytesIO(file_bytes), return_pages=True)
                pages_text: List[str] = list(_pt) if isinstance(_pt, list) else []
                text = "\n".join(pages_text) if pages_text else ""
            except Exception:
                logger.exception("pdf_text_extraction_failed file=%s", file_name)
                pages_text = []
                text = ""
            text_len = len(text.strip())
            route_mode = "text" if text_len >= TEXT_EXTRACTION_MIN_THRESHOLD else "image_multi"
            logger.info(
                "pdf_extraction_route invoice_id=%s file=%s text_len=%s threshold=%s mode=%s",
                inv_id,
                file_name,
                text_len,
                TEXT_EXTRACTION_MIN_THRESHOLD,
                route_mode,
            )
            if route_mode == "text":
                extracted = extract_invoice_core_fields(text, invoice_id=inv_id, excel_data=inv.get("excel", {}))
                extracted["_raw_invoice_text"] = text
                _apply_safe_deterministic_amount_override(
                    extracted=extracted,
                    pages_text=pages_text,
                    expected_currency=_resolve_expected_billing_currency(config, extracted),
                    invoice_id=inv_id,
                    file_name=file_name,
                    source_mode="pdf_text",
                )
            else:
                # Attempt consolidated multi-page Gemini extraction
                try:
                    images = convert_from_bytes(file_bytes, dpi=300)
                except Exception as exc:
                    logger.exception("pdf_to_image_failed file=%s", file_name)
                    images = []

                if images:
                    selected_pages = images[:MAX_SCANNED_PDF_PAGES]
                    page_image_bytes_list: List[bytes] = []
                    page_ocr_texts: List[str] = []
                    
                    for page_img in selected_pages:
                        buf = io.BytesIO()
                        page_img.save(buf, format="JPEG", quality=90)
                        img_bytes = buf.getvalue()
                        page_image_bytes_list.append(img_bytes)
                        
                        # Background OCR for fallback text
                        try:
                            page_ocr = extract_text_from_image_file(img_bytes) or ""
                            page_ocr_texts.append(page_ocr)
                        except Exception:
                            logger.exception("image_ocr_fallback_failed file=%s", file_name)
                    
                    from modules.invoice_gemini_extractor import extract_invoice_core_fields_from_multi_images
                    extracted = extract_invoice_core_fields_from_multi_images(
                        page_image_bytes_list, 
                        invoice_id=inv_id, 
                        excel_data=inv.get("excel", {})
                    )
                    
                    # Combine OCR text from all pages
                    raw_text = "\n".join(t for t in page_ocr_texts if t.strip())
                    extracted["_raw_invoice_text"] = raw_text

                    # OCR-text fallback: if vision returned nothing but OCR produced text,
                    # run the standard text-based Gemini extractor on the OCR output.
                    if extracted.get("_extraction_quality") == "failed" and len(raw_text.strip()) >= TEXT_EXTRACTION_MIN_THRESHOLD:
                        logger.info(
                            "ocr_text_fallback_start invoice_id=%s ocr_text_len=%s",
                            inv_id, len(raw_text.strip()),
                        )
                        ocr_extracted = extract_invoice_core_fields(
                            raw_text, invoice_id=inv_id, excel_data=inv.get("excel", {})
                        )
                        _CORE_FIELDS = [
                            "remitter_name", "remitter_address", "remitter_country_text",
                            "beneficiary_name", "beneficiary_address", "beneficiary_country_text",
                            "invoice_number", "invoice_date_raw", "invoice_date_iso",
                            "invoice_date_display", "amount", "currency_short",
                            "nature_of_remittance", "purpose_group", "purpose_code",
                        ]
                        filled = [f for f in _CORE_FIELDS if ocr_extracted.get(f) and not extracted.get(f)]
                        for field in filled:
                            extracted[field] = ocr_extracted[field]
                        if filled:
                            extracted.pop("_extraction_quality", None)
                            logger.info(
                                "ocr_text_fallback_applied invoice_id=%s filled_fields=%s",
                                inv_id, filled,
                            )
                        else:
                            logger.warning(
                                "ocr_text_fallback_empty invoice_id=%s reason=no_fields_from_ocr_text",
                                inv_id,
                            )

                    _apply_safe_deterministic_amount_override(
                        extracted=extracted,
                        pages_text=page_ocr_texts,
                        expected_currency=_resolve_expected_billing_currency(config, extracted),
                        invoice_id=inv_id,
                        file_name=file_name,
                        source_mode="pdf_image_multi",
                    )
                else:
                    # Final fallback: treat as plain image
                    try:
                        extracted = extract_invoice_core_fields_from_image(file_bytes, invoice_id=inv_id, excel_data=inv.get("excel", {}))
                        text = extract_text_from_image_file(file_bytes) or ""
                    except Exception:
                        logger.exception("pdf_image_ocr_fallback_failed file=%s", file_name)
                        extracted = {}
                        text = ""
                    if not extracted.get("_raw_invoice_text"):
                        extracted["_raw_invoice_text"] = text
        else:
            # Image uploads (jpg/png)
            extracted = extract_invoice_core_fields_from_image(file_bytes, invoice_id=inv_id, excel_data=inv.get("excel", {}))
            try:
                raw_text = extract_text_from_image_file(file_bytes) or ""
            except Exception:
                logger.exception("image_ocr_fallback_failed file=%s", file_name)
                raw_text = ""
            if not extracted.get("_raw_invoice_text"):
                extracted["_raw_invoice_text"] = raw_text
        # Always ensure raw text exists
        extracted.setdefault("_raw_invoice_text", "")

        # Prefill invoice number from filename when Gemini extraction failed
        if not extracted.get("invoice_number"):
            stem = os.path.splitext(file_name)[0]
            if re.match(r'^[\w\-\.]+$', stem) and len(stem) >= 5:
                extracted["invoice_number"] = stem
                logger.info(
                    "prefill_invoice_number_from_filename invoice_id=%s value=%s",
                    inv_id, stem,
                )

        # Prefill remitter from master when Gemini returned no remitter name,
        # or when Gemini's name doesn't match any known master entry (hallucination
        # from scanned PDFs — e.g. "INNOVARE..." instead of the actual Bosch entity).
        from modules.master_lookups import load_bank_details, match_remitter
        bank_entries = load_bank_details()
        gemini_remitter = extracted.get("remitter_name", "")
        remitter_matched = bool(gemini_remitter and match_remitter(gemini_remitter))
        if (not gemini_remitter or not remitter_matched) and len(bank_entries) == 1:
            default_rem = bank_entries[0]
            extracted["remitter_name"] = default_rem.get("name", "")
            logger.info(
                "remitter_prefilled_from_default invoice_id=%s gemini_name=%r remitter=%s",
                inv_id, gemini_remitter, extracted["remitter_name"],
            )

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
    finally:
        if inv["status"] == "processing":
             inv["status"] = "failed"
             inv["error"] = "Process exited unexpectedly."
        logger.info("invoice_processing_done file=%s status=%s", file_name, inv["status"])




def _process_single_invoice(inv_id: str, *, wait: bool = False) -> None:
    """Run extraction, state building and recompute for one invoice.

    When *wait=True* the function joins the worker thread before returning,
    making it synchronous from the caller's perspective.  Use this for
    single-invoice flows so the UI can wrap the call in st.spinner and skip
    the time.sleep / st.rerun polling loop entirely.

    When *wait=False* (default) the thread is left running in the background;
    this is what the batch mode needs so multiple invoices are processed in
    parallel.
    """
    logger.info("process_single_invoice_started invoice_id=%s", inv_id)
    state = _get_current_state()
    invoices = state["invoices"]
    if inv_id not in invoices:
        logger.error("process_single_invoice_failed inv_id_missing=%s", inv_id)
        return

    inv = invoices[inv_id]
    inv["status"] = "processing"

    # Build full config merging global controls with per-invoice Excel data
    ex = inv.get("excel") or {}
    config = dict(state["global_controls"])
    config["currency_short"] = str(ex.get("currency") or "").strip()
    try:
        config["exchange_rate"] = float(ex.get("exchange_rate") or 0)
    except (TypeError, ValueError):
        config["exchange_rate"] = 0.0
    config["tds_deduction_date"] = _get_invoice_dedn_date(inv)

    # Start the worker in a background thread to keep UI responsive
    t = threading.Thread(
        target=_process_invoice_worker,
        args=(inv, inv_id, inv["file_bytes"], inv["file_name"], config),
    )
    add_script_run_ctx(t)
    t.start()
    if wait:
        t.join()


def _generate_xml_for_invoice(inv_id: str) -> None:
    """Validate and generate XML for one invoice."""
    logger.info("generate_xml_started invoice_id=%s", inv_id)
    state = _get_current_state()
    inv = state["invoices"].get(inv_id)
    if not inv or not inv.get("state"):
        logger.error("generate_xml_failed source=missing_state invoice_id=%s", inv_id)
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
    st.subheader("Upload ZIP of invoices and Excel")
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
        st.subheader("Configure Defaults and Process")
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
        def _is_pending(inv: Dict[str, Any]) -> bool:
            return inv.get("status") not in ("processed", "failed")

        def _is_processed(inv: Dict[str, Any]) -> bool:
            return inv.get("status") == "processed"

        def _is_xml_missing(inv: Dict[str, Any]) -> bool:
            return inv.get("xml_status") != "ok" or not inv.get("xml_bytes")

        def _is_xml_ready(inv: Dict[str, Any]) -> bool:
            return inv.get("xml_status") == "ok" and bool(inv.get("xml_bytes"))

        action_col1, action_col2, action_col3, action_col4 = st.columns([2, 2, 2, 2])
        with action_col1:
            if st.button("Process Pending Only", type="primary"):
                pending_ids = [inv_id for inv_id, inv in invoices.items() if _is_pending(inv)]
                if not pending_ids:
                    st.info("No pending invoices.")
                else:
                    for inv_id in pending_ids:
                        _process_single_invoice(inv_id)
                    st.success(f"Started processing {len(pending_ids)} pending invoices.")
                    st.rerun()

        with action_col2:
            if st.button("Process All Invoices"):
                for inv_id in invoices.keys():
                    _process_single_invoice(inv_id)
                st.success(f"Started processing {len(invoices)} invoices.")
                st.rerun()

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
        st.subheader("Review and Edit Invoices")

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

        def _matches_filter(inv: Dict[str, Any]) -> bool:
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
                _inv_state = inv.get("state")
                state_meta: Dict[str, Any] = _inv_state.get("meta", {}) if isinstance(_inv_state, dict) else {}
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
                elif status == "processing":
                    st.info("⏳ Processing...")
                    time.sleep(1)
                    st.rerun()
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
                        with st.spinner("Processing..."):
                            _process_single_invoice(inv_id, wait=True)
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
                        old_form: Dict[str, Any] = dict(inv["state"].get("form", {}))
                        new_state: Dict[str, Any] = render_invoice_tab(inv["state"], show_header=False)
                        new_form: Dict[str, Any] = new_state.get("form", {})
                        for k in ["CountryRemMadeSecb", "NatureRemCategory", "RevPurCategory", "RevPurCode", "RateTdsADtaa", "BasisDeterTax", "TaxPayGrossSecb"]:
                            if k in new_form and k in old_form and new_form[k] != old_form[k]:
                                logger.info("ui_field_changed invoice_id=%s field=%s old=%r new=%r", inv_id, k, old_form[k], new_form[k])
                        # Snapshot key computed fields before recompute
                        form: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                        _snap_keys = (
                            "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                            "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                            "BasisDeterTax", "RateTdsADtaa",
                        )
                        before = tuple(str(form.get(k) or "") for k in _snap_keys)
                        # Recompute tax fields in case user edits (e.g. DTAA rate)
                        new_state = recompute_invoice(new_state)
                        form_after: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
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
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("📄 Process One Invoice", type="primary" if mode == "single" else "secondary", use_container_width=True):
            st.session_state["mode"] = "single"
            st.rerun()
    with col2:
        if st.button("📋 Single Invoice (No Excel)", type="primary" if mode == "no_excel" else "secondary", use_container_width=True):
            st.session_state["mode"] = "no_excel"
            st.rerun()
    with col3:
        if st.button("🗂 Process Many Invoices", type="primary" if mode == "bulk" else "secondary", use_container_width=True):
            st.session_state["mode"] = "bulk"
            st.rerun()

def render_single_invoice_page() -> None:
    state = _get_current_state()
    
    # 1. Start with a new invoice button
    col_t1, col_t2 = st.columns([6, 2])
    with col_t1:
        st.title("📄 Process One Invoice")
    with col_t2:
        st.write("") # vertical alignment
        if st.button("Start with a new invoice", type="secondary", use_container_width=True):
            logger.info("button_clicked label='Start with a new invoice'")
            state["invoices"] = {}
            state["single_context"] = None
            state["ui_epoch"] = state.get("ui_epoch", 0) + 1
            st.rerun()

    # 2. Upload Invoice & Excel (MOVED TO TOP for better stability during reruns)
    # 2. Upload Invoice & Excel
    st.subheader("Upload Invoice & Excel")
    col1, col2 = st.columns(2)
    with col1:
        uploaded_inv = st.file_uploader("Upload Invoice", type=["pdf", "jpg", "jpeg", "png"], key=f"single_inv_upload_{state.get('ui_epoch', 0)}")
    with col2:
        uploaded_excel = st.file_uploader("Upload Excel", type=["xlsx"], key=f"single_excel_upload_{state.get('ui_epoch', 0)}")
    
    # Audit logging for file upload received
    if uploaded_inv and not state.get("_last_uploaded_inv_name"):
        logger.info("file_upload_received type=invoice filename=%s size_kb=%d", uploaded_inv.name, len(uploaded_inv.getvalue()) // 1024)
        state["_last_uploaded_inv_name"] = uploaded_inv.name
    if uploaded_excel and not state.get("_last_uploaded_excel_name"):
        logger.info("file_upload_received type=excel filename=%s size_kb=%d", uploaded_excel.name, len(uploaded_excel.getvalue()) // 1024)
        state["_last_uploaded_excel_name"] = uploaded_excel.name

    if uploaded_inv and uploaded_excel:
        current_context = uploaded_inv.name + "|" + uploaded_excel.name
        if state.get("single_context") != current_context:
            try:
                # Detect if it's a new invoice file or just a new excel
                old_context = state.get("single_context") or "|"
                old_inv_name = old_context.split("|")[0]
                is_new_invoice = uploaded_inv.name != old_inv_name
                
                if is_new_invoice:
                    logger.info("files_uploaded inv=%s excel=%s", uploaded_inv.name, uploaded_excel.name)
                else:
                    logger.info("excel_updated filename=%s", uploaded_excel.name)

                df = read_excel(uploaded_excel.getvalue())
                stem = os.path.splitext(uploaded_inv.name)[0]
                norm_stem = _normalize_reference(stem)
                
                matches = 0
                excel_row = {}
                if not df.empty:
                    for _, row in df.fillna("").iterrows():
                        if _normalize_reference(row.get("Reference")) == norm_stem:
                            matches += 1
                            if matches == 1:
                                excel_row = row.to_dict()
                
                if matches == 0:
                    st.error(f"Could not find matching row in Excel for invoice reference: {stem}")
                    return
                
                # If it's the same invoice, we only want to update the excel metadata and recompute
                if not is_new_invoice and state["invoices"].get(stem):
                    inv = state["invoices"][stem]
                    old_fx = inv.get("excel", {}).get("exchange_rate")
                    
                    # Update excel info
                    from modules.zip_intake import _extract_excel_metadata
                    new_excel_meta = _extract_excel_metadata(excel_row)
                    inv["excel"] = new_excel_meta
                    
                    new_fx = new_excel_meta.get("exchange_rate")
                    if old_fx != new_fx:
                        logger.info("excel_rate_updated invoice_id=%s old_fx=%s new_fx=%s", stem, old_fx, new_fx)
                    
                    # Trigger recompute if processed
                    if inv["status"] == "processed" and inv.get("state"):
                        logger.info("recompute_triggered_by_excel_update invoice_id=%s", stem)
                        inv["state"]["meta"]["exchange_rate"] = str(new_fx)
                        inv["state"]["meta"]["tds_deduction_date"] = new_excel_meta.get("dedn_date_tds")
                        inv["state"]["meta"]["currency_short"] = new_excel_meta.get("currency")
                        inv["state"] = recompute_invoice(inv["state"])
                        inv["config_sig"] = _compute_config_sig(inv)
                        inv["xml_bytes"] = None
                        inv["xml_status"] = "none"
                else:
                    # New invoice or first time
                    from modules.zip_intake import build_invoice_registry
                    invoices_dict = build_invoice_registry(df, [(uploaded_inv.name, uploaded_inv.getvalue())])
                    state["invoices"] = {stem: invoices_dict[stem]}
                
                state["single_context"] = current_context
                st.success("Files loaded and matched successfully.")
                st.rerun()
            except Exception as e:
                logger.exception("file_processing_failed")
                st.error(f"Error processing files: {e}")
                return

    # 3. Configure Defaults (MOVED BELOW UPLOAD)
    st.divider()
    st.subheader("Configure Defaults")
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
    with gc3:
        new_it_label = st.selectbox(
            "IT Act Rate (%)",
            options=_IT_RATE_LABELS,
            index=_IT_RATE_LABELS.index(_prev_label),
            key="single_it_rate_select",
        )
        new_it_rate = _IT_RATE_MAP.get(new_it_label, IT_ACT_RATE_DEFAULT)
    
    if new_mode != prev_mode or new_gross != prev_gross or new_it_rate != prev_it_rate:
        field_changed = "mode" if new_mode != prev_mode else ("gross_up" if new_gross != prev_gross else "it_rate")
        old_val = prev_mode if field_changed == "mode" else (prev_gross if field_changed == "gross_up" else prev_it_rate)
        new_val = new_mode if field_changed == "mode" else (new_gross if field_changed == "gross_up" else new_it_rate)
        
        invoices = state.get("invoices", {})
        inv_id = list(invoices.keys())[0] if invoices else None
        is_processed = bool(inv_id and invoices[inv_id].get("status") == "processed")
        
        logger.info("ui_control_changed invoice_id=%s field=%s old=%s new=%s invoice_loaded=%s invoice_processed=%s", 
                    inv_id or "none", field_changed, old_val, new_val, bool(inv_id), is_processed)
        
        state["global_controls"]["mode"] = new_mode
        state["global_controls"]["gross_up"] = new_gross
        state["global_controls"]["it_act_rate"] = new_it_rate
        
        if is_processed and inv_id:
            logger.info("ui_control_recompute_start invoice_id=%s field=%s", inv_id, field_changed)
            inv = invoices[inv_id]
            # Update meta in the state
            inv["state"]["meta"]["mode"] = new_mode
            inv["state"]["meta"]["is_gross_up"] = new_gross
            inv["state"]["meta"]["it_act_rate"] = new_it_rate
            # Recompute
            inv["state"] = recompute_invoice(inv["state"])
            inv["config_sig"] = _compute_config_sig(inv)
            inv["xml_bytes"] = None
            inv["xml_status"] = "none"
            
            form = inv["state"].get("form", {})
            snap = {k: form.get(k) for k in ["TaxLiablIt", "TaxLiablDtaa", "AmtPayForgnTds", "AmtPayIndianTds"]}
            logger.info("ui_control_recompute_done invoice_id=%s values=%s", inv_id, snap)
        else:
            if inv_id:
                logger.info("ui_control_applied_pending invoice_id=%s", inv_id)
        
        st.rerun()

    # 4. Invoices display logic (independent of uploaded_inv/uploaded_excel objects)
    invoices = state.get("invoices", {})
    if invoices:
        inv_id = list(invoices.keys())[0]
        inv = invoices[inv_id]
        
        if inv["status"] == "new":
            pass # Config is now handled by the global controls at the top of the page
                
            if st.button("Process Invoice", type="primary"):
                logger.info("process_invoice_clicked invoice_id=%s mode=%s gross=%s it_rate=%s",
                            inv_id, state["global_controls"]["mode"], state["global_controls"]["gross_up"], state["global_controls"]["it_act_rate"])
                with st.spinner("Processing invoice..."):
                    _process_single_invoice(inv_id, wait=True)
                st.rerun()
        elif inv["status"] == "failed":
            st.error(f"Processing failed: {inv.get('error')}")
        elif inv["status"] == "processed":
            st.subheader("Review and Generate XML")
            
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
                old_form: Dict[str, Any] = dict(inv["state"].get("form", {}))
                new_state: Dict[str, Any] = render_invoice_tab(inv["state"], show_header=False, is_single_mode=True)
                new_form: Dict[str, Any] = new_state.get("form", {})

                # Log field changes
                for k in ["CountryRemMadeSecb", "NatureRemCategory", "RevPurCategory", "RevPurCode", "RateTdsADtaa", "BasisDeterTax", "TaxPayGrossSecb", "AmtPayForgnRem", "AmtPayForgnTds"]:
                    if k in new_form and k in old_form and new_form[k] != old_form[k]:
                        logger.info("ui_field_edited invoice_id=%s field=%s value=%r", inv_id, k, new_form[k])
                        if k in ["AmtPayForgnRem", "AmtPayForgnTds", "RateTdsADtaa", "CountryRemMadeSecb"]:
                            logger.info("recompute_triggered_by_field_edit invoice_id=%s field=%s", inv_id, k)

                form: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                _snap_keys = (
                    "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                    "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                    "BasisDeterTax", "RateTdsADtaa",
                )
                before = tuple(str(form.get(k) or "") for k in _snap_keys)
                new_state = recompute_invoice(new_state)
                form_after: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
                after = tuple(str(form_after.get(k) or "") for k in _snap_keys)
                inv["state"] = new_state
                if after != before:
                    inv["xml_bytes"] = None
                    inv["xml_status"] = "none"
                    inv["xml_error"] = None
                state["invoices"][inv_id] = inv
            except Exception as exc:
                st.error(f"Rendering form failed: {exc}")

            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Generate XML", type="primary", use_container_width=True):
                    logger.info("generate_xml_clicked invoice_id=%s", inv_id)
                    _generate_xml_for_invoice(inv_id)
                    if inv.get("xml_status") == "ok":
                        st.success("XML generated successfully.")
                    else:
                        st.error(f"XML generation failed: {inv.get('xml_error')}")
            with c2:
                if st.button("Process invoice again", type="secondary", use_container_width=True):
                    logger.info("button_clicked label='Process invoice again' invoice_id=%s", inv_id)
                    with st.spinner("Processing invoice..."):
                        _process_single_invoice(inv_id, wait=True)
                    st.rerun()
            with c3:
                if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                    filename_stub = (inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id).replace(" ", "_")
                    st.download_button(
                        "Download XML",
                        data=inv["xml_bytes"],
                        file_name=f"form15cb_{filename_stub}.xml",
                        mime="application/xml",
                        use_container_width=True,
                        on_click=lambda: logger.info("xml_downloaded invoice_id=%s", inv_id)
                    )

# -----------------------------------------------------------------------------
# No-Excel single-invoice mode
# -----------------------------------------------------------------------------

def _nex_write_excel_proxy(inv: dict, currency: str, exchange_rate: float, dedn_date_iso: str) -> None:
    """Write manually-entered transaction details into inv['excel'].

    This makes a No-Excel invoice record structurally identical to an
    Excel-derived one so that every downstream function that reads
    inv['excel'] (build_invoice_state, recompute_invoice, _compute_config_sig,
    _generate_xml_for_invoice) works without modification.
    """
    inv["excel"]["currency"] = currency or ""
    inv["excel"]["exchange_rate"] = exchange_rate
    inv["excel"]["dedn_date_tds"] = dedn_date_iso or ""


def render_no_excel_invoice_page() -> None:
    """Single Invoice (No Excel) mode.

    The user uploads an invoice and manually provides currency, exchange
    rate and date of deduction.  Processing starts only on an explicit
    button click.  The existing backend pipeline is reused verbatim —
    manual inputs are normalised into inv['excel'] via
    _nex_write_excel_proxy() before _process_single_invoice() is called.
    """
    from modules.zip_intake import build_invoice_record_no_excel

    state = _get_current_state()
    epoch = state.get("ui_epoch", 0)

    # ── Title + Reset ──────────────────────────────────────────────────────────
    col_t1, col_t2 = st.columns([6, 2])
    with col_t1:
        st.title("📋 Single Invoice (No Excel)")
    with col_t2:
        st.write("")
        if st.button("Start with a new invoice", type="secondary", use_container_width=True,
                     key=f"nex_reset_btn_{epoch}"):
            state["invoices"] = {}
            state["single_context"] = None
            state["ui_epoch"] = epoch + 1
            # Remove all nex_ widget keys so widgets reset to defaults on next render
            for k in list(st.session_state.keys()):
                if isinstance(k, str) and k.startswith("nex_"):
                    del st.session_state[k]
            st.rerun()

    # ── Section 1: Upload ──────────────────────────────────────────────────────
    st.subheader("1. Upload Invoice")
    uploaded_inv = st.file_uploader(
        "Upload Invoice (PDF / JPG / PNG)",
        type=["pdf", "jpg", "jpeg", "png"],
        accept_multiple_files=False,
        key=f"nex_inv_upload_{epoch}",
    )

    # ── Section 2: Transaction Details ────────────────────────────────────────
    st.subheader("2. Transaction Details")
    st.caption("Enter the values that would normally come from the Excel sheet.")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        currency_sel = st.selectbox(
            "Currency",
            options=ALL_CURRENCY_OPTIONS,
            key=f"nex_currency_{epoch}",
        )
        if currency_sel == "Other":
            currency = st.text_input(
                "Enter currency code (e.g. CHF)",
                key=f"nex_currency_other_{epoch}",
            ).strip().upper()
        else:
            currency = currency_sel

    with col_b:
        exchange_rate_str = st.text_input(
            "Exchange Rate (1 FCY = ? INR)",
            value="",
            placeholder="e.g. 89.50",
            key=f"nex_exchange_rate_{epoch}",
        )
        exchange_rate = 0.0
        exchange_rate_valid = False
        if exchange_rate_str.strip():
            try:
                exchange_rate = float(exchange_rate_str.replace(",", ""))
                if exchange_rate <= 0:
                    st.error("Exchange rate must be greater than 0.")
                else:
                    exchange_rate_valid = True
            except ValueError:
                st.error("Exchange rate must be a number.")

    with col_c:
        dedn_date_input = st.date_input(
            "Date of Deduction of TDS *",
            value=None,
            key=f"nex_dedn_date_{epoch}",
            help="Required in TDS mode to generate XML.",
        )
        dedn_date_iso = dedn_date_input.isoformat() if dedn_date_input else ""

    # ── Section 3: Tax Configuration ──────────────────────────────────────────
    st.subheader("3. Tax Configuration")
    _IT_RATE_LABELS = [
        f"{r}% (Default)" if r == IT_ACT_RATE_DEFAULT else f"{r}%"
        for r in IT_ACT_RATES
    ]
    _IT_RATE_MAP = dict(zip(_IT_RATE_LABELS, IT_ACT_RATES))

    prev_mode = state["global_controls"].get("mode", MODE_TDS)
    prev_gross = state["global_controls"].get("gross_up", False)
    prev_it_rate = state["global_controls"].get("it_act_rate", IT_ACT_RATE_DEFAULT)
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
            key=f"nex_mode_radio_{epoch}",
        )
    with gc2:
        new_gross = st.checkbox(
            "💰 Gross\u2011up tax (company bears tax)",
            value=bool(prev_gross),
            disabled=(new_mode == MODE_NON_TDS),
            key=f"nex_gross_checkbox_{epoch}",
        )
        if new_mode == MODE_NON_TDS:
            new_gross = False
    with gc3:
        new_it_label = st.selectbox(
            "IT Act Rate (%)",
            options=_IT_RATE_LABELS,
            index=_IT_RATE_LABELS.index(_prev_label),
            key=f"nex_it_rate_select_{epoch}",
        )
        new_it_rate = _IT_RATE_MAP.get(new_it_label, IT_ACT_RATE_DEFAULT)

    # Apply control changes and recompute any already-processed invoice
    if new_mode != prev_mode or new_gross != prev_gross or new_it_rate != prev_it_rate:
        state["global_controls"]["mode"] = new_mode
        state["global_controls"]["gross_up"] = new_gross
        state["global_controls"]["it_act_rate"] = new_it_rate
        _existing = state.get("invoices", {})
        if _existing:
            _inv_id = list(_existing.keys())[0]
            _inv = _existing[_inv_id]
            if _inv.get("status") == "processed" and _inv.get("state"):
                _inv["state"]["meta"]["mode"] = new_mode
                _inv["state"]["meta"]["is_gross_up"] = new_gross
                _inv["state"]["meta"]["it_act_rate"] = new_it_rate
                _inv["state"] = recompute_invoice(_inv["state"])
                _inv["xml_bytes"] = None
                _inv["xml_status"] = "none"
                _inv["xml_error"] = None
        st.rerun()

    st.divider()

    # ── Handle file upload — create or refresh invoice record ─────────────────
    invoices = state.get("invoices", {})
    inv_id = list(invoices.keys())[0] if invoices else None
    inv = invoices.get(inv_id) if inv_id else None

    if uploaded_inv is not None:
        stem = os.path.splitext(uploaded_inv.name)[0]
        if inv_id != stem:
            # New or different file — create a fresh record
            record = build_invoice_record_no_excel(uploaded_inv.name, uploaded_inv.getvalue())
            state["invoices"] = {stem: record}
            state["single_context"] = stem
            inv_id = stem
            inv = record
        # Sync the current widget values into the excel proxy on every render
        # so that _compute_config_sig always sees up-to-date values.
        if inv is not None:
            _nex_write_excel_proxy(inv, currency, exchange_rate, dedn_date_iso)

    # Refresh local references after possible state mutation above
    invoices = state.get("invoices", {})
    inv = invoices.get(inv_id) if inv_id else None
    # Narrow inv_id: if inv is not None, inv_id must be a valid str key.
    inv_id = str(inv_id) if inv_id is not None else ""

    if not inv:
        st.info("Upload an invoice and fill in the transaction details above, then click **Process Invoice**.")
        return

    # ── Status routing ─────────────────────────────────────────────────────────
    status = inv.get("status", "new")

    if status == "new":
        missing = []
        if not currency:
            missing.append("currency")
        if not exchange_rate_valid:
            missing.append("a valid exchange rate (> 0)")
        if new_mode == MODE_TDS and not dedn_date_iso:
            missing.append("date of deduction of TDS")
        if missing:
            st.warning(f"Please provide: {', '.join(missing)} before processing.")
        ready = not missing
        if st.button("Process Invoice", type="primary", disabled=not ready,
                     key=f"nex_process_btn_{epoch}"):
            _nex_write_excel_proxy(inv, currency, exchange_rate, dedn_date_iso)
            with st.spinner("Processing invoice..."):
                _process_single_invoice(inv_id, wait=True)
            st.rerun()

    elif status == "failed":
        st.error(f"Processing failed: {inv.get('error')}")
        if st.button("Retry", type="secondary", key=f"nex_retry_btn_{epoch}"):
            _nex_write_excel_proxy(inv, currency, exchange_rate, dedn_date_iso)
            with st.spinner("Processing invoice..."):
                _process_single_invoice(inv_id, wait=True)
            st.rerun()

    elif status == "processed":
        st.subheader("Review and Generate XML")

        # Show transaction details card (mirrors the Excel card in single mode)
        ex = inv.get("excel", {})
        ex_rate = ex.get("exchange_rate")
        ex_rate_str = f"{float(ex_rate):.4f}" if ex_rate and float(ex_rate) > 0 else "—"
        with st.container(border=True):
            st.markdown(f'''
            <div class="excel-card">
                <div><span class="label">Currency</span> <span class="arrow">→</span>
                     <code>{ex.get("currency") or "—"}</code></div>
                <div><span class="label">Exchange Rate</span> <span class="arrow">→</span>
                     <code>{ex_rate_str}</code></div>
                <div><span class="label">Deduction Date</span> <span class="arrow">→</span>
                     <code>{ex.get("dedn_date_tds") or "—"}</code></div>
            </div>
            ''', unsafe_allow_html=True)

        # Render the editable form (same as single mode)
        from modules.batch_form_ui import render_invoice_tab
        try:
            old_form: Dict[str, Any] = dict(inv["state"].get("form", {}))
            new_state: Dict[str, Any] = render_invoice_tab(inv["state"], show_header=False, is_single_mode=True)
            form: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
            _snap_keys = (
                "RateTdsSecB", "TaxLiablIt", "TaxLiablDtaa",
                "AmtPayForgnTds", "AmtPayIndianTds", "ActlAmtTdsForgn",
                "BasisDeterTax", "RateTdsADtaa",
            )
            before = tuple(str(form.get(k) or "") for k in _snap_keys)
            # Sync the current exchange rate widget value into meta so recompute
            # always uses the exact rate the user has entered (not the stale value
            # from the original processing run).
            _cur_ex: Dict[str, Any] = inv.get("excel", {})
            _cur_rate = _cur_ex.get("exchange_rate")
            if _cur_rate and float(_cur_rate) > 0:
                _meta: Dict[str, Any] = new_state.get("meta", {})
                _meta["exchange_rate"] = str(float(_cur_rate))
                _meta["source_currency_short"] = str(_cur_ex.get("currency") or _meta.get("source_currency_short") or "")
                _meta["tds_deduction_date"] = str(_cur_ex.get("dedn_date_tds") or _meta.get("tds_deduction_date") or "")
                new_state["meta"] = _meta
            new_state = recompute_invoice(new_state)
            form_after: Dict[str, Any] = new_state.get("form", {}) if isinstance(new_state, dict) else {}
            after = tuple(str(form_after.get(k) or "") for k in _snap_keys)
            inv["state"] = new_state
            if after != before:
                inv["xml_bytes"] = None
                inv["xml_status"] = "none"
                inv["xml_error"] = None
            state["invoices"][inv_id] = inv
        except Exception as exc:
            st.error(f"Rendering form failed: {exc}")

        # Action buttons
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Generate XML", type="primary", use_container_width=True,
                         key=f"nex_gen_xml_{epoch}"):
                _generate_xml_for_invoice(inv_id)
                if inv.get("xml_status") == "ok":
                    st.success("XML generated successfully.")
                else:
                    st.error(f"XML generation failed: {inv.get('xml_error')}")
        with c2:
            if st.button("Process invoice again", type="secondary", use_container_width=True,
                         key=f"nex_reprocess_btn_{epoch}"):
                _nex_write_excel_proxy(inv, currency, exchange_rate, dedn_date_iso)
                with st.spinner("Processing invoice..."):
                    _process_single_invoice(inv_id, wait=True)
                st.rerun()
        with c3:
            if inv.get("xml_status") == "ok" and inv.get("xml_bytes"):
                _inv_no = inv.get("state", {}).get("extracted", {}).get("invoice_number") or inv_id or ""
                filename_stub = str(_inv_no).replace(" ", "_")
                st.download_button(
                    "Download XML",
                    data=inv["xml_bytes"],
                    file_name=f"form15cb_{filename_stub}.xml",
                    mime="application/xml",
                    use_container_width=True,
                )


def main() -> None:
    _ensure_session_state()
    render_mode_switcher()
    mode = st.session_state.get("mode", "single")
    if mode == "single":
        render_single_invoice_page()
    elif mode == "no_excel":
        render_no_excel_invoice_page()
    else:
        render_bulk_invoice_page()


if __name__ == "__main__":
    main()

