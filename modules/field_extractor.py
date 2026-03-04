"""
Gemini-only Field Extraction Module for Form 15CB
=================================================

This module removes regex extraction entirely and relies on Google Gemini
to extract ALL fields as structured JSON.

Requirements:
- google-genai (new SDK)
- python-dotenv
- python-dateutil (optional, not required here but may be used elsewhere)

Env (.env):
- GEMINI_API_KEY=...
- GEMINI_MODEL_NAME=gemini-2.5-flash   (confirmed working in your test)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, List

from dotenv import load_dotenv
from modules.master_data import suggest_from_master

try:
    from google import genai  # google-genai SDK
except Exception:
    genai = None  # type: ignore

logger = logging.getLogger(__name__)

# Load env
load_dotenv()
try:
    import streamlit as st
    GEMINI_API_KEY: Optional[str] = st.secrets.get("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY"))
    GEMINI_MODEL_NAME: str = st.secrets.get("GEMINI_MODEL_NAME", os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash"))
except Exception:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

# You can tune these if needed
GEMINI_TEMPERATURE: float = float(os.getenv("GEMINI_TEMPERATURE", "0"))
GEMINI_MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))

# If your OCR text is huge, keep a cap to control token cost
MAX_INPUT_CHARS: int = int(os.getenv("GEMINI_MAX_INPUT_CHARS", "60000"))

# ---------------------------------------------------------------------
# All placeholders expected by your XML template
# ---------------------------------------------------------------------
XML_FIELD_KEYS: List[str] = [
    "NameRemittee",
    "RemitteeFlatDoorBuilding",
    "RemitteeAreaLocality",
    "RemitteeTownCityDistrict",
    "RemitteeZipCode",
    "RemitteeState",
    "RemitteeCountryCode",
    "CountryRemMadeSecb",
    "CurrencySecbCode",
    "AmtPayForgnRem",
    "AmtPayIndRem",
    "NameBankCode",
    "BranchName",
    "BsrCode",
    "PropDateRem",
    "NatureRemCategory",
    "RevPurCategory",
    "RevPurCode",
    "TaxPayGrossSecb",
    "RemittanceCharIndia",
    "SecRemCovered",
    "AmtIncChrgIt",
    "TaxLiablIt",
    "BasisDeterTax",
    "TaxResidCert",
    "RelevantDtaa",
    "RelevantArtDtaa",
    "TaxIncDtaa",
    "TaxLiablDtaa",
    "RemForRoyFlg",
    "ArtDtaa",
    "RateTdsADtaa",
    "RemAcctBusIncFlg",
    "IncLiabIndiaFlg",
    "RemOnCapGainFlg",
    "OtherRemDtaa",
    "TaxIndDtaaFlg",
    "RelArtDetlDDtaa",
    "AmtPayForgnTds",
    "AmtPayIndianTds",
    "RateTdsSecbFlg",
    "RateTdsSecB",
    "ActlAmtTdsForgn",
    "DednDateTds",
    "NameAcctnt",
    "NameFirmAcctnt",
    "PremisesBuildingVillage",
    "AcctntTownCityDistrict",
    "AcctntFlatDoorBuilding",
    "AcctntAreaLocality",
    "AcctntPincode",
    "AcctntState",
    "AcctntRoadStreet",
    "AcctntCountryCode",
    "MembershipNumber",
    "RemitterPAN",
    "NameRemitter",
]

def _load_lookup(file_name: str) -> Dict[str, str]:
    try:
        path = Path(__file__).resolve().parent.parent / "lookups" / file_name
        with open(path, "r", encoding="utf8") as f:
            data: Dict[str, str] = json.load(f)
        return {k.lower(): str(v) for k, v in data.items()}
    except Exception:
        return {}


COUNTRY_LOOKUP = _load_lookup("country_codes.json")
CURRENCY_LOOKUP = _load_lookup("currency_codes.json")
BANK_LOOKUP = _load_lookup("bank_codes.json")
STATE_LOOKUP = _load_lookup("state_codes.json")
NATURE_LOOKUP = _load_lookup("nature_codes.json")
PURPOSE_LOOKUP = _load_lookup("purpose_codes.json")


def load_purpose_code_list() -> Dict[str, List[Dict[str, str]]]:
    path = Path(__file__).resolve().parent.parent / "lookups" / "purpose_code_list.json"
    try:
        with open(path, "r", encoding="utf8") as f:
            data = json.load(f)
    except Exception:
        return {}

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for entry in data.get("purpose_codes", []):
        if not isinstance(entry, dict):
            continue
        group = str(entry.get("group_name") or "").strip()
        code = str(entry.get("purpose_code") or "").strip().upper()
        description = str(entry.get("description") or "").strip()
        if not group or not code:
            continue
        grouped.setdefault(group, []).append({"code": code, "description": description})
    return grouped


def _ensure_all_keys(data: Dict[str, object]) -> Dict[str, str]:
    """Return a dict with exactly XML_FIELD_KEYS, missing -> ''."""
    out: Dict[str, str] = {}
    for k in XML_FIELD_KEYS:
        v = data.get(k, "")
        if v is None:
            out[k] = ""
        elif isinstance(v, str):
            out[k] = v.strip()
        else:
            out[k] = str(v).strip()
    return out


def _extract_json_object(text: str) -> Optional[Dict[str, object]]:
    """
    Robustly parse a JSON object from model output.
    Handles:
    - pure JSON
    - markdown code fences ```json ... ```
    - extra leading/trailing text
    """
    if not text:
        return None

    s = text.strip()

    # Strip code fences if present
    if "```" in s:
        parts = [p.strip() for p in s.split("```") if p.strip()]
        # Prefer a part that looks like JSON object
        for p in parts:
            if p.startswith("{") and p.endswith("}"):
                s = p
                break

    # First attempt: direct JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Second attempt: slice between first { and last }
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None

    return None


def extract_fields(text: str) -> Dict[str, str]:
    """
    Gemini-only extraction. Always returns all keys in XML_FIELD_KEYS.
    Missing values are empty strings.

    Args:
        text: OCR/PDF extracted text from the Form 15CB certificate.

    Returns:
        Dict[str, str] of extracted placeholders.
    """
    logger.info("=" * 80)
    logger.info("FORM 15CB FIELD EXTRACTION (GEMINI ONLY)")
    logger.info("=" * 80)

    if not text or len(text.strip()) < 50:
        logger.warning("Text too short; returning blanks")
        return {k: "" for k in XML_FIELD_KEYS}

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set. Cannot run Gemini-only extraction.")
        return {k: "" for k in XML_FIELD_KEYS}

    if genai is None:
        logger.error("google-genai not installed/importable. Install: pip install -U google-genai")
        return {k: "" for k in XML_FIELD_KEYS}

    # Keep input under control (tokens/cost)
    doc = text.strip()
    if len(doc) > MAX_INPUT_CHARS:
        doc = doc[:MAX_INPUT_CHARS]

    # Strong prompt: JSON only, exact keys only.
    keys_list = ", ".join(XML_FIELD_KEYS)

    prompt = (
        "You are extracting fields from OCR/text for Indian Form 15CB.\n"
        "Return ONLY a single JSON object that strictly follows the provided schema.\n"
        "Rules:\n"
        f"- Keys MUST be exactly these (no extra keys): {keys_list}\n"
        "- Return ALL keys; no omissions.\n"
        "- For missing values, use empty string \"\".\n"
        "- Do NOT add explanations.\n"
        "- Keep numeric fields as digits only (remove commas, currency symbols, ₹ signs).\n"
        "- Convert dates to YYYY-MM-DD format.\n"
        "- For Y/N fields: output Y or N.\n"
        "\n"
        "Section-aware extraction rules:\n"
        "- Remitter = the party marked '(Remitters)' in opening paragraphs; PAN near this party is RemitterPAN.\n"
        "- Remittee/Beneficiary = the foreign party marked '(Beneficiary)' and Section A beneficiary details.\n"
        "- Use Section A for remittee address fields and remittee country.\n"
        "- Use Section B for country/currency/bank/branch/BSR/remittance amounts/proposed date/nature/purpose fields.\n"
        "- Use Section 10 for ItActDetails fields.\n"
        "- Use Section 11 for DTAADetails fields.\n"
        "- Use Sections 12-15 for TDSDetails fields.\n"
        "- RemitterPAN format hint: 5 letters + 4 digits + 1 letter (example: AAACR7108R).\n"
        "- PropDateRem and DednDateTds must be YYYY-MM-DD if present.\n"
        "- ActlAmtTdsForgn = actual remittance amount AFTER TDS deduction (field 14), not the TDS amount.\n"
        "- RemitteeZipCode for foreign addresses should be 999999.\n"
        "- RemitteeTownCityDistrict should capture country name (example: Germany) and RemitteeAreaLocality can hold city+zip.\n"
        "- IncLiabIndiaFlg: if business income is N and field is not applicable, use -1.\n"
        "- RateTdsSecbFlg must be numeric code: 1=IT Act rate, 2=DTAA rate, 3=Lower deduction certificate.\n"
        "\n"
        "Document text:\n"
        f"{doc}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        resp = client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=prompt,
            config={
                "temperature": 0,
                "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
                "response_mime_type": "application/json",
            },
        )

        raw = (resp.text or "").strip()
        logger.info("Gemini raw output (first 1200 chars): %s", raw[:1200])
        if not raw:
            logger.error("Gemini returned empty output.")
            return {k: "" for k in XML_FIELD_KEYS}

        data = None
        try:
            data = json.loads(raw)
        except Exception:
            # try extracting JSON object between first { and last }
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(raw[start:end+1])
                except Exception:
                    data = None

        # If we got a list, try to find the first object element
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    data = item
                    break

        # If parsing still failed and braces are unmatched, attempt a single retry
        if not isinstance(data, dict):
            open_braces = raw.count("{")
            close_braces = raw.count("}")
            if open_braces > close_braces:
                logger.info("Detected truncated JSON (open %d > close %d). Attempting one continuation retry.", open_braces, close_braces)
                retry_prompt = (
                    "The previous response appears to have been truncated. "
                    "Continue the same JSON from where you left off. "
                    "Output ONLY a single valid JSON object with the same keys: "
                    f"{keys_list}. Do not add any explanation."
                )
                try:
                    resp2 = client.models.generate_content(
                        model=GEMINI_MODEL_NAME,
                        contents=retry_prompt,
                        config={
                            "temperature": 0,
                            "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
                            "response_mime_type": "application/json",
                        },
                    )
                    raw2 = (resp2.text or "").strip()
                    logger.info("Gemini retry raw output (first 1200 chars): %s", raw2[:1200])
                    try:
                        data = json.loads(raw2)
                    except Exception:
                        start2 = raw2.find("{")
                        end2 = raw2.rfind("}")
                        if start2 != -1 and end2 != -1 and end2 > start2:
                            try:
                                data = json.loads(raw2[start2:end2+1])
                            except Exception:
                                data = None
                except Exception as e:
                    logger.warning("Retry request failed: %s", e)

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    data = item
                    break

        if not isinstance(data, dict):
            logger.error("Gemini output was not valid JSON object after retry. Returning blanks.")
            return {k: "" for k in XML_FIELD_KEYS}

        cleaned = _ensure_all_keys(data)

        # Normalize descriptor fields to expected lookup codes with fallback sources.
        def _lkp(table: Dict[str, str], raw: str) -> str:
            if not raw:
                return ""
            key = raw.strip().lower()
            return table.get(key, raw.strip())

        country_raw = (
            cleaned.get("CountryRemMadeSecb")
            or cleaned.get("RemitteeCountryCode")
            or cleaned.get("RemitteeTownCityDistrict")
            or ""
        )
        mapped_country = _lkp(COUNTRY_LOOKUP, country_raw)
        if mapped_country:
            cleaned["CountryRemMadeSecb"] = mapped_country
            cleaned["RemitteeCountryCode"] = mapped_country

        currency_raw = cleaned.get("CurrencySecbCode", "")
        if currency_raw:
            cleaned["CurrencySecbCode"] = _lkp(CURRENCY_LOOKUP, currency_raw)

        bank_raw = cleaned.get("NameBankCode", "")
        if bank_raw:
            cleaned["NameBankCode"] = _lkp(BANK_LOOKUP, bank_raw)

        state_raw = cleaned.get("AcctntState", "")
        if state_raw:
            cleaned["AcctntState"] = _lkp(STATE_LOOKUP, state_raw)

        acctnt_country_raw = cleaned.get("AcctntCountryCode", "")
        if acctnt_country_raw:
            cleaned["AcctntCountryCode"] = _lkp(COUNTRY_LOOKUP, acctnt_country_raw)

        nature_raw = cleaned.get("NatureRemCategory", "")
        if nature_raw:
            cleaned["NatureRemCategory"] = _lkp(NATURE_LOOKUP, nature_raw)

        purpose_category_raw = cleaned.get("RevPurCategory", "")
        if purpose_category_raw:
            cleaned["RevPurCategory"] = _lkp(PURPOSE_LOOKUP, purpose_category_raw)

        purpose_code_raw = cleaned.get("RevPurCode", "")
        if purpose_code_raw:
            mapped_purpose_code = _lkp(PURPOSE_LOOKUP, purpose_code_raw)
            if mapped_purpose_code.startswith("RB-"):
                cleaned["RevPurCode"] = mapped_purpose_code
            elif mapped_purpose_code.upper().startswith("S") and cleaned.get("RevPurCategory", "").startswith("RB-"):
                cleaned["RevPurCode"] = f"{cleaned['RevPurCategory']}-{mapped_purpose_code.upper()}"
            else:
                cleaned["RevPurCode"] = mapped_purpose_code

        orig_r_zip = cleaned.get("RemitteeZipCode", "").strip()
        orig_r_town = cleaned.get("RemitteeTownCityDistrict", "").strip()
        remittee_country_code = cleaned.get("RemitteeCountryCode", "")
        is_foreign_remittee = bool(remittee_country_code and remittee_country_code != "91")

        # If area/locality is blank but original zip+city exists, preserve both in locality.
        if not cleaned.get("RemitteeAreaLocality") and orig_r_zip and orig_r_town and orig_r_zip != "999999":
            cleaned["RemitteeAreaLocality"] = f"{orig_r_zip}, {orig_r_town.lower()}"

        if is_foreign_remittee:
            # For foreign remittee, zipcode should be placeholder and town/city should carry country.
            cleaned["RemitteeZipCode"] = "999999"
            country_name = next(
                (name for name, code in COUNTRY_LOOKUP.items() if code == remittee_country_code and " " in name),
                next((name for name, code in COUNTRY_LOOKUP.items() if code == remittee_country_code), ""),
            )
            if country_name:
                cleaned["RemitteeTownCityDistrict"] = country_name.title()

        # Mark not-applicable explicitly for business-income dependent field.
        if cleaned.get("RemAcctBusIncFlg", "").upper() == "N" and cleaned.get("IncLiabIndiaFlg", "").upper() in {"", "N"}:
            cleaned["IncLiabIndiaFlg"] = "-1"

        # Normalize RateTdsSecbFlg to numeric code when model returns Y/N style output.
        rate_flag = cleaned.get("RateTdsSecbFlg", "").strip().upper()
        if rate_flag in {"Y", "N"}:
            if cleaned.get("RateTdsADtaa", "").strip():
                cleaned["RateTdsSecbFlg"] = "2"
            elif cleaned.get("RateTdsSecB", "").strip():
                cleaned["RateTdsSecbFlg"] = "1"

        # Ensure ActlAmtTdsForgn is net of TDS when base and TDS amounts are available.
        def _to_number(raw: str) -> Optional[float]:
            if not raw:
                return None
            digits = re.sub(r"[^0-9.]", "", raw)
            if not digits:
                return None
            try:
                return float(digits)
            except ValueError:
                return None

        gross_forgn = _to_number(cleaned.get("AmtPayForgnRem", ""))
        tds_forgn = _to_number(cleaned.get("AmtPayForgnTds", ""))
        if gross_forgn is not None and tds_forgn is not None and gross_forgn >= tds_forgn:
            net_forgn = gross_forgn - tds_forgn
            cleaned["ActlAmtTdsForgn"] = str(int(net_forgn)) if net_forgn.is_integer() else str(net_forgn)

        # Optional non-destructive enrichment from master data: fill only missing keys.
        try:
            master_suggestions, _ = suggest_from_master(cleaned, BANK_LOOKUP)
            for key, value in master_suggestions.items():
                if key in cleaned and (not cleaned[key]) and isinstance(value, str) and value.strip():
                    cleaned[key] = value.strip()
        except Exception as enrich_err:
            logger.warning("Master-data enrichment skipped: %s", enrich_err)

        populated = sum(1 for v in cleaned.values() if v)
        logger.info(f"Gemini extraction populated {populated}/{len(XML_FIELD_KEYS)} fields")
        return cleaned

    except Exception as e:
        logger.error(f"Gemini extraction failed: {type(e).__name__}: {e}")
        return {k: "" for k in XML_FIELD_KEYS}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python field_extractor.py <path_to_text_file>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf8") as f:
        t = f.read()

    result = extract_fields(t)
    print(json.dumps(result, indent=2, ensure_ascii=False))



























# """
# Field Extraction Module for Form 15CB
# ====================================

# Two-phase extraction:
# 1) Regex extraction (cheap, deterministic)
# 2) Gemini AI fallback (ONLY when regex doesn't extract enough)

# Gemini is used only for structured field extraction and is controlled via:
# - GEMINI_API_KEY (required for AI fallback)
# - GEMINI_MODEL_NAME (default: gemini-2.5-flash)

# NOTE: This file uses the NEW Google GenAI SDK (google-genai):
#     from google import genai
# """

# from __future__ import annotations

# import json
# import logging
# import os
# import re
# from pathlib import Path
# from typing import Dict, Optional, List

# from dateutil import parser as date_parser  # type: ignore
# from dotenv import load_dotenv

# # NEW SDK
# try:
#     from google import genai  # type: ignore
# except Exception:
#     genai = None  # type: ignore

# logger = logging.getLogger(__name__)

# # -----------------------------------------------------------------------------
# # Load environment variables
# # -----------------------------------------------------------------------------
# load_dotenv()
# GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")

# # IMPORTANT: You confirmed this model works in your environment
# # (returns OK in test_gemini.py)
# GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

# # Gemini call should be rare; keep output small but enough to fit all fields.
# GEMINI_MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "1200"))

# # Extra safety to reduce cost: only call Gemini if regex extracts very little
# # (because OCR text is messy and regex patterns fail).
# AI_MIN_REGEX_FIELDS_TRIGGER: int = int(os.getenv("AI_MIN_REGEX_FIELDS_TRIGGER", "12"))

# # -----------------------------------------------------------------------------
# # Load lookup tables
# # -----------------------------------------------------------------------------
# _LOOKUP_DIR = Path(__file__).resolve().parent.parent / "lookups"


# def _load_lookup(file_name: str) -> Dict[str, str]:
#     path = _LOOKUP_DIR / file_name
#     try:
#         with open(path, "r", encoding="utf8") as f:
#             data: Dict[str, str] = json.load(f)
#         return {k.lower(): str(v) for k, v in data.items()}
#     except FileNotFoundError:
#         logger.warning(f"Lookup file not found: {file_name}")
#         return {}
#     except Exception as e:
#         logger.error(f"Failed to load lookup {file_name}: {e}")
#         return {}


# COUNTRY_CODES: Dict[str, str] = _load_lookup("country_codes.json")
# CURRENCY_CODES: Dict[str, str] = _load_lookup("currency_codes.json")
# BANK_CODES: Dict[str, str] = _load_lookup("bank_codes.json")

# # -----------------------------------------------------------------------------
# # Expected fields (placeholders)
# # -----------------------------------------------------------------------------
# XML_FIELD_KEYS: List[str] = [
#     "NameRemittee",
#     "RemitteeFlatDoorBuilding",
#     "RemitteeAreaLocality",
#     "RemitteeTownCityDistrict",
#     "RemitteeZipCode",
#     "RemitteeState",
#     "RemitteeCountryCode",
#     "CountryRemMadeSecb",
#     "CurrencySecbCode",
#     "AmtPayForgnRem",
#     "AmtPayIndRem",
#     "NameBankCode",
#     "BranchName",
#     "BsrCode",
#     "PropDateRem",
#     "NatureRemCategory",
#     "RevPurCategory",
#     "RevPurCode",
#     "TaxPayGrossSecb",
#     "RemittanceCharIndia",
#     "SecRemCovered",
#     "AmtIncChrgIt",
#     "TaxLiablIt",
#     "BasisDeterTax",
#     "TaxResidCert",
#     "RelevantDtaa",
#     "RelevantArtDtaa",
#     "TaxIncDtaa",
#     "TaxLiablDtaa",
#     "RemForRoyFlg",
#     "ArtDtaa",
#     "RateTdsADtaa",
#     "RemAcctBusIncFlg",
#     "IncLiabIndiaFlg",
#     "RemOnCapGainFlg",
#     "OtherRemDtaa",
#     "TaxIndDtaaFlg",
#     "RelArtDetlDDtaa",
#     "AmtPayForgnTds",
#     "AmtPayIndianTds",
#     "RateTdsSecbFlg",
#     "RateTdsSecB",
#     "ActlAmtTdsForgn",
#     "DednDateTds",
#     "NameAcctnt",
#     "NameFirmAcctnt",
#     "PremisesBuildingVillage",
#     "AcctntTownCityDistrict",
#     "AcctntFlatDoorBuilding",
#     "AcctntAreaLocality",
#     "AcctntPincode",
#     "AcctntState",
#     "AcctntRoadStreet",
#     "AcctntCountryCode",
#     "MembershipNumber",
#     "RemitterPAN",
#     "NameRemitter",
# ]

# _CRITICAL_KEYS: List[str] = [
#     "NameRemitter",
#     "RemitterPAN",
#     "CountryRemMadeSecb",
#     "CurrencySecbCode",
#     "AmtPayForgnRem",
#     "AmtPayIndRem",
#     "NameBankCode",
#     "BsrCode",
# ]

# # -----------------------------------------------------------------------------
# # Helpers
# # -----------------------------------------------------------------------------
# def _clean_amount(value: str) -> str:
#     if not value:
#         return ""
#     return re.sub(r"[^0-9]", "", value)


# def _parse_date(date_str: str) -> str:
#     if not date_str:
#         return ""
#     try:
#         dt = date_parser.parse(date_str, dayfirst=True, fuzzy=True)
#         return dt.date().isoformat()
#     except Exception:
#         return ""


# def _map_country(name: str) -> str:
#     if not name:
#         return ""
#     return COUNTRY_CODES.get(name.strip().lower(), "")


# def _map_currency(code: str) -> str:
#     if not code:
#         return ""
#     return CURRENCY_CODES.get(code.strip().lower(), "")


# def _map_bank(name: str) -> str:
#     if not name:
#         return ""
#     return BANK_CODES.get(name.strip().lower(), "")


# def _count_populated(d: Dict[str, str]) -> int:
#     return sum(1 for v in d.values() if str(v).strip())


# def _safe_model_name(model: str) -> str:
#     """
#     google-genai supports model ids like:
#       - gemini-2.5-flash
#       - models/gemini-2.5-flash
#     We'll accept either.
#     """
#     m = (model or "").strip()
#     if not m:
#         return "gemini-2.5-flash"
#     return m


# def extract_with_regex(text: str) -> Dict[str, str]:
#     fields: Dict[str, str] = {}
#     if not text:
#         return fields

#     normal_text = re.sub(r"\s+", " ", text)

#     remitter_match = re.search(r"M/?s\.?\s*([^\n,]+?)\s+with\s+PAN", text, re.IGNORECASE)
#     if remitter_match:
#         fields["NameRemitter"] = remitter_match.group(1).strip()

#     pan_match = re.search(r"\bPAN\b\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])", text, re.IGNORECASE)
#     if pan_match:
#         fields["RemitterPAN"] = pan_match.group(1).strip().upper()

#     country_match = re.search(
#         r"Country to which remittance is made\s*[-–:]?\s*([A-Za-z ]{2,})",
#         normal_text,
#         re.IGNORECASE,
#     )
#     if country_match:
#         country_name = country_match.group(1).strip()
#         # keep as raw in CountryRemMadeSecb; mapping can happen downstream if needed
#         fields["CountryRemMadeSecb"] = country_name
#         code = _map_country(country_name)
#         if code:
#             fields["RemitteeCountryCode"] = code

#     currency_match = re.search(r"\bCurrency\b\s*[-–:]?\s*([A-Z]{3})", normal_text)
#     if currency_match:
#         currency_code = currency_match.group(1).strip().upper()
#         fields["CurrencySecbCode"] = _map_currency(currency_code) or currency_code

#     foreign_amt_match = re.search(
#         r"Amount payable[^\n]*?foreign currency[^\d]*(\d[\d,]+)",
#         normal_text,
#         re.IGNORECASE,
#     )
#     if foreign_amt_match:
#         fields["AmtPayForgnRem"] = _clean_amount(foreign_amt_match.group(1))

#     inr_match = re.search(r"In\s+Indian.*?₹?\s*([\d,]+)", normal_text, re.IGNORECASE)
#     if inr_match:
#         fields["AmtPayIndRem"] = _clean_amount(inr_match.group(1))

#     bank_match = re.search(r"Name of Bank\s*[-–:]?\s*([A-Za-z &]+)", normal_text, re.IGNORECASE)
#     if bank_match:
#         bank_name = bank_match.group(1).strip()
#         fields["NameBankCode"] = _map_bank(bank_name) or bank_name

#     branch_match = re.search(r"Branch of the bank\s*[-–:]?\s*([A-Za-z ,]+)", normal_text, re.IGNORECASE)
#     if branch_match:
#         fields["BranchName"] = branch_match.group(1).strip()

#     bsr_match = re.search(r"BSR code[^\d]*(\d{7})", normal_text, re.IGNORECASE)
#     if bsr_match:
#         fields["BsrCode"] = bsr_match.group(1)

#     date_match = re.search(
#         r"Proposed date of remittance[^\d]*(\d{1,2}[\-/ ]\w{3,9}[\-/ ]\d{2,4})",
#         normal_text,
#         re.IGNORECASE,
#     )
#     if date_match:
#         iso = _parse_date(date_match.group(1))
#         if iso:
#             fields["PropDateRem"] = iso

#     rate_tds_match = re.search(r"Rate of TDS[^\d]*(\d+)\s*%", normal_text, re.IGNORECASE)
#     if rate_tds_match:
#         fields["RateTdsSecB"] = rate_tds_match.group(1)

#     return fields


# def extract_with_gemini_api(text: str) -> Optional[Dict[str, str]]:
#     """
#     AI extraction using the NEW google-genai SDK.

#     Returns a dict with keys in XML_FIELD_KEYS or None on failure.
#     """
#     if not GEMINI_API_KEY:
#         logger.warning("GEMINI_API_KEY not set; skipping AI extraction")
#         return None
#     if genai is None:
#         logger.warning("google-genai not installed / import failed; skipping AI extraction")
#         return None

#     # Limit text to reduce token cost. (Increase if needed.)
#     doc_snippet = (text or "")[:20000]

#     keys_list = ", ".join(XML_FIELD_KEYS)

#     # Prompt designed to force JSON only.
#     prompt = (
#         "Extract fields from the Form 15CB text and return STRICT JSON only.\n"
#         "Rules:\n"
#         "- Output must be a single JSON object (no markdown).\n"
#         "- Keys must EXACTLY match the field names provided.\n"
#         "- Use empty string \"\" for missing values.\n"
#         "- Do NOT include any extra keys.\n"
#         "- Keep values as they appear in the document (no explanations).\n\n"
#         f"Fields: {keys_list}\n\n"
#         "Text:\n"
#         f"{doc_snippet}"
#     )

#     try:
#         client = genai.Client(api_key=GEMINI_API_KEY)
#         model_name = _safe_model_name(GEMINI_MODEL_NAME)

#         resp = client.models.generate_content(
#             model=model_name,
#             contents=prompt,
#             # Note: google-genai uses generation_config in some versions;
#             # this form works reliably with current releases.
#             config={
#                 "temperature": 0,
#                 "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
#             },
#         )

#         result_text = (resp.text or "").strip()
#         if not result_text:
#             logger.error("Gemini returned empty response text")
#             return None

#         # If Gemini accidentally includes code fences, strip them.
#         if "```" in result_text:
#             parts = [p.strip() for p in result_text.split("```") if p.strip()]
#             for p in parts:
#                 if p.startswith("{") and p.endswith("}"):
#                     result_text = p
#                     break

#         # Parse JSON. If model returns some leading text, attempt to slice braces.
#         data: Optional[Dict[str, object]] = None
#         try:
#             data = json.loads(result_text)
#         except Exception:
#             start = result_text.find("{")
#             end = result_text.rfind("}")
#             if start != -1 and end != -1 and end > start:
#                 try:
#                     data = json.loads(result_text[start : end + 1])
#                 except Exception as e:
#                     logger.error(f"Failed to parse Gemini JSON fragment: {e}")
#                     return None
#             else:
#                 logger.error("Gemini output did not contain a JSON object")
#                 return None

#         if not isinstance(data, dict):
#             logger.error("Gemini output JSON is not an object/dict")
#             return None

#         cleaned: Dict[str, str] = {}
#         for key in XML_FIELD_KEYS:
#             v = data.get(key, "")
#             if v is None:
#                 cleaned[key] = ""
#             elif isinstance(v, str):
#                 cleaned[key] = v.strip()
#             else:
#                 cleaned[key] = str(v).strip()

#         return cleaned

#     except Exception as e:
#         logger.error(f"Gemini extraction failed: {type(e).__name__}: {e}")
#         return None


# def extract_fields(text: str) -> Dict[str, str]:
#     logger.info("=" * 80)
#     logger.info("FORM 15CB FIELD EXTRACTION (REGEX + AI Fallback)")
#     logger.info("=" * 80)

#     if not text or len(text.strip()) < 50:
#         logger.warning("Text too short for extraction; returning blanks")
#         return {k: "" for k in XML_FIELD_KEYS}

#     regex_fields = extract_with_regex(text)
#     logger.info(f"Regex extraction found {len(regex_fields)} fields")

#     missing_critical = [k for k in _CRITICAL_KEYS if not regex_fields.get(k)]
#     ai_fields: Optional[Dict[str, str]] = None

#     # Extra guard: if regex extracted at least AI_MIN_REGEX_FIELDS_TRIGGER fields,
#     # don't call Gemini (saves cost).
#     if len(regex_fields) < AI_MIN_REGEX_FIELDS_TRIGGER and missing_critical:
#         logger.info(
#             f"{len(missing_critical)} critical fields missing and only "
#             f"{len(regex_fields)} regex fields found; invoking Gemini AI..."
#         )
#         ai_fields = extract_with_gemini_api(text)
#         if ai_fields:
#             logger.info(f"AI extraction returned {sum(1 for v in ai_fields.values() if v)} populated fields")
#         else:
#             logger.warning("AI extraction failed or returned no data")
#     else:
#         logger.info("Skipping AI fallback (regex extraction deemed sufficient or no critical missing)")

#     combined: Dict[str, str] = {}
#     for key in XML_FIELD_KEYS:
#         if key in regex_fields and str(regex_fields[key]).strip():
#             combined[key] = str(regex_fields[key]).strip()
#         elif ai_fields and key in ai_fields and str(ai_fields[key]).strip():
#             combined[key] = str(ai_fields[key]).strip()
#         else:
#             combined[key] = ""

#     logger.info(f"Extraction complete: {_count_populated(combined)} fields populated")
#     return combined


# if __name__ == "__main__":
#     import sys

#     logging.basicConfig(level=logging.INFO)

#     if len(sys.argv) < 2:
#         print("Usage: python field_extractor.py <path_to_text_or_pdf>")
#         sys.exit(1)

#     input_path = sys.argv[1]
#     if input_path.lower().endswith(".pdf"):
#         from modules.pdf_reader import extract_text_from_pdf  # type: ignore

#         text_data = extract_text_from_pdf(input_path)
#     else:
#         with open(input_path, "r", encoding="utf8") as f:
#             text_data = f.read()

#     result = extract_fields(text_data)
#     print(json.dumps(result, indent=2, ensure_ascii=False))
