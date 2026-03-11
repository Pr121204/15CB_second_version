from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from dotenv import load_dotenv

try:
    import google.generativeai as genai
except Exception:  # pragma: no cover
    genai = None  # type: ignore

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
except Exception:  # pragma: no cover
    google_genai = None  # type: ignore
    google_genai_types = None  # type: ignore


load_dotenv()

def _get_gemini_config() -> tuple:
    """Lazily read GEMINI_API_KEY and GEMINI_MODEL_NAME at call time.

    Deferring the st.secrets access until this function is called (rather
    than at module import time) prevents Streamlit from raising
    "set_page_config must be called before any other Streamlit command"
    when invoice_gemini_extractor is imported before st.set_page_config() runs.
    """
    try:
        import streamlit as st
        return (
            st.secrets.get("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
            st.secrets.get("GEMINI_MODEL_NAME", os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")),
        )
    except Exception:
        return (
            os.getenv("GEMINI_API_KEY"),
            os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash"),
        )

from modules.master_lookups import load_country_code_map, match_remitter, resolve_country_code
from modules.logger import get_logger
from modules.text_normalizer import fix_concatenated_words, normalize_invoice_text, normalize_single_line_text


logger = get_logger()

MASTER_DIR = Path(__file__).resolve().parent.parent / "data" / "master"
try:
    TEXT_EXTRACTION_MIN_THRESHOLD = max(20, int(os.getenv("TEXT_EXTRACTION_MIN_THRESHOLD", "300")))
except Exception:
    TEXT_EXTRACTION_MIN_THRESHOLD = 300


def _load_json(path: Path, default):
    """Load JSON file with fallback to default value."""
    try:
        with open(path, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return default


def _json_serializable(obj):
    """JSON serializer for objects not serializable by default json code"""
    import datetime
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if hasattr(obj, 'isoformat'):
        # This handles pandas Timestamp and other similar objects
        return obj.isoformat()
    # Fallback to string representation for anything else before failing
    try:
        return str(obj)
    except Exception:
        raise TypeError(f"Type {type(obj)} not serializable")


def _ensure_genai_loaded() -> bool:
    """Try to import the google.generativeai SDK at runtime if it isn't loaded yet.

    Returns True if the SDK is available after this call.
    """
    global genai
    if genai is not None:
        return True
    try:
        import importlib

        genai = importlib.import_module("google.generativeai")
        return True
    except Exception:
        genai = None  # type: ignore
        return False


def _ensure_google_genai_loaded() -> bool:
    """Try to import the google-genai SDK at runtime if it isn't loaded yet."""
    global google_genai, google_genai_types
    if google_genai is not None and google_genai_types is not None:
        return True
    try:
        import importlib

        google_genai = importlib.import_module("google.genai")
        google_genai_types = importlib.import_module("google.genai.types")
        return True
    except Exception:
        google_genai = None  # type: ignore
        google_genai_types = None  # type: ignore
        return False


def _gemini_backend() -> str:
    """Return available Gemini SDK backend: 'legacy', 'modern', or ''."""
    if _ensure_genai_loaded():
        return "legacy"
    if _ensure_google_genai_loaded():
        return "modern"
    return ""


# Module-level singleton for the google-genai Client to avoid
# re-creating connection overhead on every Gemini call.
_cached_google_genai_client = None
_cached_google_genai_api_key: Optional[str] = None


def _get_google_genai_client():
    """Return a cached google-genai Client, creating one if needed."""
    global _cached_google_genai_client, _cached_google_genai_api_key
    GEMINI_API_KEY, _ = _get_gemini_config()
    if _cached_google_genai_client is not None and _cached_google_genai_api_key == GEMINI_API_KEY:
        return _cached_google_genai_client
    _cached_google_genai_client = google_genai.Client(api_key=GEMINI_API_KEY)
    _cached_google_genai_api_key = GEMINI_API_KEY
    return _cached_google_genai_client


def _format_finish_reason(value: object) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value)
    if "." in text:
        return text.split(".")[-1]
    return text


def _extract_modern_response_text(response: object) -> str:
    direct_text = str(getattr(response, "text", "") or "")
    if direct_text.strip():
        return direct_text
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        chunks: List[str] = []
        for part in parts:
            txt = getattr(part, "text", None)
            if txt:
                chunks.append(str(txt))
        merged = "".join(chunks).strip()
        if merged:
            return merged
    return ""


def _generate_with_gemini_text(prompt: str, max_output_tokens: int = 2048) -> Tuple[str, str]:
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        return "", ""
    if backend == "legacy":
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0,
                top_p=1,
                top_k=1,
                max_output_tokens=max_output_tokens,
            ),
        )
        finish_reason = ""
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            finish_reason = _format_finish_reason(getattr(candidates[0], "finish_reason", None))
        return str(getattr(response, "text", "") or ""), finish_reason

    # google-genai SDK path
    client = _get_google_genai_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=prompt,
        config=google_genai_types.GenerateContentConfig(
            temperature=0,
            top_p=1,
            top_k=1,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
        ),
    )
    finish_reason = ""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish_reason = _format_finish_reason(getattr(candidates[0], "finish_reason", None))
    return _extract_modern_response_text(response), finish_reason


def _generate_with_gemini_image(prompt: str, image_path_or_bytes: Union[str, bytes, Path], mime_type: str) -> str:
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        return ""

    if backend == "legacy":
        # Legacy SDK expects base64 content payload.
        base64_image = _encode_image_to_base64(image_path_or_bytes)
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        # Pass image content as a simple dict payload to avoid extra type-only imports.
        image_part = {"mime_type": mime_type, "data": base64_image}
        response = model.generate_content(
            [prompt, image_part],
            generation_config=genai.types.GenerationConfig(
                temperature=0,
                top_p=1,
                top_k=1,
                max_output_tokens=2048,
            ),
        )
        return str(getattr(response, "text", "") or "")

    # google-genai SDK path (native bytes).
    image_bytes: bytes
    if isinstance(image_path_or_bytes, (bytes, bytearray)):
        image_bytes = bytes(image_path_or_bytes)
    else:
        with open(str(image_path_or_bytes), "rb") as f:
            image_bytes = f.read()

    client = _get_google_genai_client()
    image_part = google_genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=[prompt, image_part],
        config=google_genai_types.GenerateContentConfig(
            temperature=0,
            top_p=1,
            top_k=1,
            max_output_tokens=2048,
            response_mime_type="application/json",
        ),
    )
    return str(getattr(response, "text", "") or "")


def _generate_with_gemini_multi_images(prompt: str, images_list: List[Union[str, bytes, Path]], mime_type: str) -> str:
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        return ""

    if backend == "legacy":
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        contents = [prompt]
        for img_source in images_list:
            base64_image = _encode_image_to_base64(img_source)
            contents.append({"mime_type": mime_type, "data": base64_image})
        
        response = model.generate_content(
            contents,
            generation_config=genai.types.GenerationConfig(
                temperature=0,
                top_p=1,
                top_k=1,
                max_output_tokens=4096,
            ),
        )
        return str(getattr(response, "text", "") or "")

    # google-genai SDK path
    client = _get_google_genai_client()
    contents = [prompt]
    for img_source in images_list:
        if isinstance(img_source, (bytes, bytearray)):
            image_bytes = bytes(img_source)
        else:
            with open(str(img_source), "rb") as f:
                image_bytes = f.read()
        contents.append(google_genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type))

    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=contents,
        config=google_genai_types.GenerateContentConfig(
            temperature=0,
            top_p=1,
            top_k=1,
            max_output_tokens=4096,
            response_mime_type="application/json",
        ),
    )
    return str(getattr(response, "text", "") or "")


def _load_purpose_rows() -> List[Dict[str, str]]:
    raw = _load_json(MASTER_DIR / "Purpose_code_List.json", {"purpose_codes": []})
    rows = raw.get("purpose_codes", []) if isinstance(raw, dict) else []
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("purpose_code") or "").strip().upper()
        group = str(row.get("group_name") or "").strip()
        if code:
            out.append({"purpose_code": code, "group_name": group})
    return out


def _purpose_indexes() -> Tuple[set[str], Dict[str, str]]:
    valid_codes: set[str] = set()
    code_to_group: Dict[str, str] = {}
    for row in _load_purpose_rows():
        code = str(row.get("purpose_code") or "").strip().upper()
        group = str(row.get("group_name") or "").strip()
        if not code:
            continue
        valid_codes.add(code)
        if group and code not in code_to_group:
            code_to_group[code] = group
    return valid_codes, code_to_group


def _is_valid_purpose_code(code: str) -> bool:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return False
    valid_codes, _ = _purpose_indexes()
    return normalized in valid_codes


def _purpose_group_for_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return ""
    _, code_to_group = _purpose_indexes()
    return str(code_to_group.get(normalized) or "")


def _encode_image_to_base64(image_path_or_bytes: Union[str, bytes, Path]) -> str:
    """Encode image file or bytes to base64 string."""
    if isinstance(image_path_or_bytes, (bytes, bytearray)):
        return base64.b64encode(image_path_or_bytes).decode('utf-8')
    else:
        with open(str(image_path_or_bytes), 'rb') as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')


def _get_image_mime_type(image_path_or_ext: Union[str, Path]) -> str:
    """Determine MIME type from file path or extension."""
    path_str = str(image_path_or_ext).lower()
    if path_str.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg'
    elif path_str.endswith('.png'):
        return 'image/png'
    elif path_str.endswith('.gif'):
        return 'image/gif'
    elif path_str.endswith('.webp'):
        return 'image/webp'
    else:
        # Default to JPEG for bytes
        return 'image/jpeg'


PROMPT = """You are a financial document analysis engine specialized in Form 15CB remittance preparation.

Your task is to extract payer, payee, remittance nature, purpose code, and invoice details from invoice text.

Accuracy is critical.
You must never guess values.

All outputs must be based on explicit evidence from the invoice text or Excel row data provided.

MULTILINGUAL HANDLING
The invoice may be written in any language (German, Japanese, French, Vietnamese, Czech, Thai, Korean, Chinese, Malay, etc.).
Extract all fields and return them in English.
For amounts: always use period as decimal separator in your output (e.g. 12347.32, not 12.347,32 or 12,347).
German invoices use comma as decimal separator and period as thousands separator (12.347,32 = twelve thousand three hundred forty-seven point thirty-two).
For dates: always return ISO format YYYY-MM-DD regardless of the source format.

DEFINITIONS

Remitter
The Indian entity that is sending money abroad.
Indicators:
- Indian address
- Appears in sections like: Bill To, Customer, Client, Buyer, Payer
The remitter is NOT the invoice issuer.

Beneficiary
The foreign entity receiving the payment.
Indicators:
- Invoice issuer, Supplier, Vendor
- Appears in invoice header or sender block.

Nature of Remittance
A short business description of the service being paid for. 
It must describe the actual service or product.
Good examples: Software consultancy services, Cloud infrastructure subscription services, Data processing services, Business management consultancy.
Avoid generic terms like "invoice payment", "fees", or "professional charges".

Purpose Code
RBI remittance purpose code corresponding to the service (e.g., S0802, S0803, S1006).

Purpose Group
Higher-level category for the purpose code (e.g., Telecommunication, Computer & Information Services, Other Business Services).

INVOICE NUMBER EXTRACTION POLICY
For "invoice_number", extract the document reference number that uniquely identifies this invoice:
- Preferred labels: "Invoice No.", "Invoice #", "Folio", "Serie / Folio", "Serie/Folio", "Folio Fiscal", "Billing Document", "Document No.", "Ref No."
- For Mexican (CFDI) invoices: use the "Serie / Folio" or "Folio" value (e.g. "W7120004157"), NOT the SAT Certificate number, SAT UUID, Cadena Original, or any long numeric/hexadecimal fiscal seal.
- Do NOT use: SAT Certificate numbers, UUID/GUID strings, Cadena Original codes, or any identifier described as a government seal or digital certificate.
- If both a folio and a SAT Certificate are present, always use the folio.

AMOUNT EXTRACTION POLICY
You must determine Amount in Foreign Currency (FCY) using the following strict order:
1. Stage 1 — Invoice totals block (preferred): Search for "Amount Due", "Balance Due", "Grand Total", "Gross Value", "Net Value", "Total Payable" near the end of the invoice.
2. Stage 2 — Invoice summary inference: If multiple totals exist, choose the final summary block.
3. Stage 3 — Totals block interpretation: If totals are unclear, infer from subtotal/tax/net/gross values.
4. Stage 4 — Excel fallback (LAST STAGE): If text does not provide a reliable amount, use the FCY amount from the Excel data provided. Set "amount_source": "excel_last_fallback" and "requires_review": true.

ADDRESS RULES
Do not merge addresses from different parties. Each party must have its own exact address block.

BENEFICIARY ADDRESS & NAME RULES
For beneficiary_address:
- Extract the POSTAL ADDRESS (street name, building number, postal code, city, country).
- The postal address typically contains a street name, building number, postal code, and city.
- DO NOT return the company name or division/department name (e.g. "Bosch Digital", "BD", "Finance Dept") as the address.
- If the invoice shows a "Company address:", "Registered address:", or "Registered office:" label, use that address.
- If the address contains a department name like "Bosch Digital (BD)", strip it — return only the street/city/country portion.
- If no postal address is visible, return "".
For beneficiary_name:
- Return ONLY the legal company name (e.g. "Robert Bosch GmbH").
- Do NOT include department or division names (e.g. NOT "Robert Bosch GmbH Bosch Digital").

SERVICE CLASSIFICATION RULES
Look for keywords in service descriptions, line items, and notes (e.g., software, consulting, cloud, subscription, database, technical support).

CONFIDENCE RULE
If the description is vague (e.g., "consulting services", "platform fee"), set "confidence": "LOW".

OUTPUT FORMAT
Return ONLY JSON.

{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount_foreign": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_code": "",
  "purpose_group": "",
  "line_items": [
    {"description": "Description of individual service or item", "amount": 0.0}
  ],
  "amount_source": "invoice_total | invoice_summary_block | excel_last_fallback",
  "confidence": "HIGH | MEDIUM | LOW",
  "requires_review": false,
  "evidence_phrases": []
}

EVIDENCE REQUIREMENT
Include evidence phrases from the text that justify the classification.

FINAL TASK
Analyze the following input.

{input_data}

Return the structured JSON output.
"""

IMAGE_EXTRACTION_PROMPT = PROMPT.replace(
    "Analyze the following input.",
    "Analyze this invoice image and the provided Excel data."
)

IMAGE_ONLY_PROMPT = IMAGE_EXTRACTION_PROMPT

MULTI_PAGE_IMAGE_EXTRACTION_PROMPT = IMAGE_EXTRACTION_PROMPT.replace(
    "Analyze this invoice image",
    "Analyze these invoice images (which are multiple pages of a single invoice)"
).replace(
    "Return the structured JSON output.",
    """You are given multiple pages of a single invoice.
Read ALL pages together to find:
- The TOTAL invoice amount in the primary billing currency (usually on the LAST page - ignore subtotals and intermediate amounts).
- Invoice number and date (usually on the first page).
- Supplier and customer details (usually on the first page).
- Line item descriptions (from any page).

Important amount rules:
- Ignore informational conversion blocks such as \"for information only\", \"exchange rate\", or \"Amounts in XXX only for information\".
- If totals conflict across currencies or pages, choose the primary billing currency total and set \"requires_review\": true.

Return ONE consolidated JSON object combining information from all pages.
"""
)
MULTI_PAGE_IMAGE_EXTRACTION_PROMPT_STRONG = """IMPORTANT: A previous extraction attempt returned no data. Look extremely carefully at ALL text visible in these invoice images.

The invoice may be in any language (German, Japanese, French, Vietnamese, etc.). Extract all fields and return them in English.
For amounts use period as decimal separator (e.g. 12347.32, not 12.347,32). For dates use YYYY-MM-DD.

This is a business invoice. You MUST find and extract every field that is visible:
- Invoice number (look for "Invoice No.", "Billing Document", "Invoice #", "Folio", "Serie / Folio", "Serie/Folio", or any document reference number near the top — for Mexican CFDI invoices use the Serie/Folio value, NOT the SAT Certificate, UUID, or Cadena Original fiscal seal)
- Invoice date (any date format near the invoice number or document header)
- Supplier/seller company name (top of page, large text or letterhead — this is the beneficiary/foreign party receiving payment)
- Customer/buyer company name ("Bill to", "Ship to", "Customer", "Client" section — this is the Indian remitter sending payment)
- Customer address (all address lines under customer name)
- Supplier address (registered office or company address block — extract the POSTAL address with street/number/postal-code/city, NOT the department or division name)
- Beneficiary name is the legal entity name ONLY, do not include departments or divisions
- Total invoice amount (FINAL total only, NOT subtotals — check last page for "Invoice amount", "Grand Total", "Net amount", "Total due", "Amount Payable", "Invoice Total")
- Currency (3-letter ISO code near the amount: EUR, USD, JPY, GBP, SGD, CHF, SEK, NOK etc.)
- Service description (what was charged — line item descriptions or service title)

Return valid JSON only with exactly this structure:
{{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount_foreign": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_code": "",
  "purpose_group": "",
  "line_items": [],
  "amount_source": "invoice_total",
  "confidence": "HIGH",
  "requires_review": false,
  "evidence_phrases": []
}}

Excel data provided: {input_data}

Return ONLY the JSON object, no markdown, no explanation."""

PROMPT_COMPACT = """Extract invoice fields as strict JSON only (no markdown, no explanation).
The invoice may be in any language. Extract all fields and return them in English.
For amounts use period as decimal separator (e.g. 12347.32). For dates use YYYY-MM-DD.
Required JSON Structure:
{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount_foreign": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_code": "",
  "purpose_group": "",
  "line_items": [
    {"description": "", "amount": 0.0}
  ],
  "amount_source": "",
  "confidence": "",
  "requires_review": false,
  "evidence_phrases": []
}
Rules:
1. Follow the 4-stage amount extraction policy (Invoice Total -> Summary Block -> Inference -> Excel fallback).
2. Use Excel data ONLY if invoice text fails for amount.
3. Accurate name/address extraction is mandatory.
4. For beneficiary_address: extract POSTAL address only (street, building number, postal code, city). Do NOT return company name or division/department as address. If "Company address:" or "Registered address:" label exists, use it.
5. For beneficiary_name: legal company name ONLY, no divisions or departments.
6. Return valid JSON only.
"""



def _norm_country_token(raw: str) -> str:
    t = re.sub(r"[^A-Za-z\s]", " ", str(raw or "").upper())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _resolve_country_candidate(raw: str) -> str:
    aliases = {
        "USA": "UNITED STATES OF AMERICA",
        "US": "UNITED STATES OF AMERICA",
        "U S A": "UNITED STATES OF AMERICA",
        "UK": "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
        "U K": "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
    }
    c = _norm_country_token(raw)
    if not c:
        return ""
    c = aliases.get(c, c)
    if resolve_country_code(c):
        return c
    if c in load_country_code_map():
        return c
    return ""


def _extract_country_from_text(text: str) -> str:
    for line in str(text or "").splitlines():
        s = " ".join(line.split()).strip()
        if not s:
            continue
        m = re.search(r"\bCOUNTRY\b\s*[:\-]\s*([A-Za-z][A-Za-z .&\-]{2,80})$", s, flags=re.IGNORECASE)
        if m:
            country = _resolve_country_candidate(m.group(1))
            if country:
                return country
        m = re.search(r"\bCOUNTRY OF (?:BENEFICIARY|REMITTANCE|DESTINATION)\b\s*[:\-]?\s*([A-Za-z][A-Za-z .&\-]{2,80})$", s, flags=re.IGNORECASE)
        if m:
            country = _resolve_country_candidate(m.group(1))
            if country:
                return country
        m = re.search(r"\b\d{4,6}\b[,\s]+([A-Za-z][A-Za-z .&\-]{2,50})\s*$", s)
        if m:
            country = _resolve_country_candidate(m.group(1))
            if country:
                return country
    return ""


def _country_from_free_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    normalized = _norm_country_token(raw)
    if not normalized:
        return ""

    # Prefer direct country-name tokens from master data.
    country_names = sorted(load_country_code_map().keys(), key=len, reverse=True)
    for country in country_names:
        if len(country) < 4:
            continue
        if re.search(rf"\b{re.escape(country)}\b", normalized):
            return country

    # Common postal-code prefixes seen in invoice addresses.
    prefix_country = {
        "DE": "GERMANY",
        "FR": "FRANCE",
        "ES": "SPAIN",
        "PT": "PORTUGAL",
        "IT": "ITALY",
        "NL": "NETHERLANDS",
        "BE": "BELGIUM",
        "AT": "AUSTRIA",
        "CH": "SWITZERLAND",
        "PL": "POLAND",
        "UK": "UNITED KINGDOM",
        "US": "UNITED STATES OF AMERICA",
    }
    upper_raw = raw.upper()
    for prefix, country in prefix_country.items():
        if re.search(rf"\b{prefix}\s*-\s*\d{{4,6}}\b", upper_raw):
            return country
    return ""


def _infer_beneficiary_address_from_text(text: str, beneficiary_name: str = "") -> str:
    lines = [ln.strip(" ,;\t") for ln in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""

    stop_re = re.compile(r"(?i)\b(invoice|inv\.?\s*no|date|gst|tax|amount|total|po\s*no|email|phone|hsn|sac)\b")

    beneficiary_norm = _normalize_for_matching(beneficiary_name)
    if beneficiary_norm:
        for idx, line in enumerate(lines):
            if beneficiary_norm and beneficiary_norm in _normalize_for_matching(line):
                chunk: List[str] = []
                for nxt in lines[idx + 1 : idx + 7]:
                    if stop_re.search(nxt):
                        if chunk:
                            break
                        continue
                    chunk.append(nxt)
                    if len(chunk) >= 3:
                        break
                if chunk:
                    return ", ".join(chunk)

    label_patterns = [
        r"(?is)\b(?:bill\s*to|billed\s*to|beneficiary|customer|consignee|sold\s*to)\b\s*[:\-]?\s*(.{20,240})",
        r"(?is)\bto\b\s*[:\-]\s*(.{20,200})",
    ]
    for pat in label_patterns:
        m = re.search(pat, str(text or ""))
        if not m:
            continue
        block_lines = [ln.strip(" ,;\t") for ln in m.group(1).splitlines()]
        chunk: List[str] = []
        for ln in block_lines:
            if not ln:
                if chunk:
                    break
                continue
            if stop_re.search(ln):
                if chunk:
                    break
                continue
            chunk.append(ln)
            if len(chunk) >= 3:
                break
        if chunk:
            return ", ".join(chunk)
    return ""


def _infer_nature_from_text(text: str) -> str:
    corpus = _normalize_for_matching(text)
    if not corpus:
        return ""
    try:
        nature_data = _load_json(MASTER_DIR / "nature_rem_category.json", [])
        if not isinstance(nature_data, list):
            return ""

        for row in nature_data:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            if not label:
                continue
            if _normalize_for_matching(label) in corpus:
                return label

        ignore = {
            "AND",
            "FOR",
            "THE",
            "OTHER",
            "MISCELLANEOUS",
            "SERVICES",
            "SERVICE",
            "CHARGES",
            "CHARGE",
            "FEES",
            "FEE",
        }
        best_label = ""
        best_score = 0.0
        for row in nature_data:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            tokens = [t for t in _normalize_for_matching(label).split() if len(t) >= 4 and t not in ignore]
            if not label or not tokens:
                continue
            hit = sum(1 for t in tokens if t in corpus)
            if hit <= 0:
                continue
            score = hit / float(len(tokens))
            if score > best_score and (hit >= 2 or score >= 0.75):
                best_score = score
                best_label = label
        return best_label
    except Exception:
        return ""


def _finalize_extracted_fields(extracted: Dict[str, str], context_text: str = "") -> Dict[str, str]:
    out = dict(extracted or {})
    context = " ".join(str(context_text or "").split())

    if not str(out.get("beneficiary_address") or "").strip():
        addr = _infer_beneficiary_address_from_text(context_text, str(out.get("beneficiary_name") or ""))
        if addr:
            out["beneficiary_address"] = addr
        else:
            parts = [
                str(out.get("beneficiary_street") or "").strip(),
                str(out.get("beneficiary_city") or "").strip(),
                str(out.get("beneficiary_zip_text") or "").strip(),
            ]
            joined = ", ".join([p for p in parts if p])
            if joined:
                out["beneficiary_address"] = joined

    # Clean up obvious domain/email patterns if Gemini misidentified them as the
    # beneficiary's legal name.  These tend to be noisy and confuse later
    # country-inference steps, so strip the suffix and attempt to space the
    # remaining text for readability.
    name_val = str(out.get("beneficiary_name") or "").strip()
    if name_val:
        # domain-like if it contains a dot and ends with a known TLD
        if re.fullmatch(r"[A-Z0-9\-_.]+\.(COM|NET|ORG|IO|CO|DE|EU|IN|UK)", name_val, flags=re.IGNORECASE):
            cleaned = re.sub(r"\.(?:COM|NET|ORG|IO|CO|DE|EU|IN|UK)$", "", name_val, flags=re.IGNORECASE)
            # insert a space before GROUP if necessary
            if cleaned.upper().endswith("GROUP") and not cleaned.upper().endswith(" GROUP"):
                cleaned = cleaned[:-5] + " GROUP"
            out["beneficiary_name"] = cleaned

    if not str(out.get("beneficiary_country_text") or "").strip():
        # First try the heuristics defined within this module (postal prefixes,
        # explicit mentions, signal detection, etc.).
        country = _country_from_free_text(
            f"{out.get('beneficiary_address', '')} {out.get('beneficiary_name', '')} {context}"
        )
        if not country:
            country = _extract_country_from_text(context)
        if not country:
            country = _detect_country_signals_from_text(context)
        # If all of the above failed, fall back to the more comprehensive
        # country-inference logic from master_lookups which includes VAT ID and
        # phone-prefix rules.  We pass the same context as the "address"
        # argument so that it can scan freely for patterns.
        if not country:
            try:
                from modules.master_lookups import infer_country_from_beneficiary_name, resolve_country_code

                code = infer_country_from_beneficiary_name(
                    str(out.get("beneficiary_name") or ""),
                    f"{out.get('beneficiary_address','')} {context}".strip(),
                )
                if code:
                    # convert numeric code to a human-readable country name
                    country = resolve_country_code(code)
            except Exception:
                country = ""
        if country:
            out["beneficiary_country_text"] = str(country).title()

    if not str(out.get("remitter_country_text") or "").strip():
        country = _country_from_free_text(f"{out.get('remitter_address', '')} {out.get('remitter_name', '')}")
        if country:
            out["remitter_country_text"] = str(country).title()
        elif _likely_indian_entity(str(out.get("remitter_name") or "")):
            out["remitter_country_text"] = "India"

    if not str(out.get("nature_of_remittance") or "").strip():
        nature = _infer_nature_from_text(
            " ".join(
                [
                    context,
                    str(out.get("invoice_number") or ""),
                    str(out.get("beneficiary_address") or ""),
                    str(out.get("remitter_address") or ""),
                ]
            )
        )
        if nature:
            out["nature_of_remittance"] = nature

    # Validate beneficiary_address: reject values that look like company/division names.
    raw_addr = str(out.get("beneficiary_address") or "").strip()
    raw_name = str(out.get("beneficiary_name") or "").strip()
    if raw_addr and not _is_valid_postal_address(raw_addr, raw_name):
        logger.warning(
            "beneficiary_address_rejected reason=looks_like_company_name raw=%r name=%r",
            raw_addr, raw_name,
        )
        out["beneficiary_address"] = ""

    return out


def _extract_json(text: str) -> Dict[str, str]:
    if not text:
        return {}
    s = text.strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _looks_like_truncated_json(text: str) -> bool:
    s = str(text or "").strip()
    if not s or "{" not in s:
        return False
    if s.startswith("{") and not s.endswith("}"):
        return True
    open_count = s.count("{")
    close_count = s.count("}")
    return open_count > close_count


def _core_fields_all_empty(parsed: Dict[str, str]) -> bool:
    keys = ("remitter_name", "beneficiary_name", "invoice_number", "amount", "currency")
    return not any(str(parsed.get(k) or "").strip() for k in keys)


def _is_invalid_gemini_extraction(parsed: Dict[str, str], response_text: str) -> bool:
    if not parsed:
        return True
    if _core_fields_all_empty(parsed):
        return True
    return _looks_like_truncated_json(response_text)


def _normalize_european_amount(amount_str: str) -> str:
    """
    Normalize European number format to standard decimal format.
    
    Handles:
    - "65,00" (European: comma decimal) → "65.00"
    - "1.234,56" (European: period thousands, comma decimal) → "1234.56"
    - "1,234.56" (US: comma thousands, period decimal) → "1234.56"
    - "65,000" (ambiguous: could be thousands or decimal in European) → "65.00" (context-based)
    
    Note: This only handles 2 decimal places for now (common for invoices).
    """
    if not amount_str:
        return ""
    
    # Strip currency symbols and spaces
    s = str(amount_str).strip()
    s = re.sub(r'[^\d.,]', '', s)  # Remove currency symbols, spaces, etc.
    
    if not s:
        return ""
    
    # If there are both . and ,
    if '.' in s and ',' in s:
        dot_pos = s.rfind('.')
        comma_pos = s.rfind(',')
        
        if dot_pos > comma_pos:
            # Pattern: "1,234.56" → US format (already correct)
            # Just remove comma
            return s.replace(',', '')
        else:
            # Pattern: "1.234,56" → European format
            # Remove dots (thousands sep), replace comma with dot (decimal sep)
            s = s.replace('.', '').replace(',', '.')
            return s
    
    # Only comma present
    if ',' in s:
        # Could be European decimal: "65,00"
        # Or European thousands: "65,000" (if 3 digits after comma)
        parts = s.split(',')
        
        if len(parts) == 2:
            # Single comma - check if it looks like a decimal
            integer_part = parts[0]
            decimal_part = parts[1]
            
            # If decimal part has exactly 2 digits (common invoice pattern), treat as decimal
            if len(decimal_part) == 2 or len(decimal_part) <= 3:
                # Likely European decimal separator
                return s.replace(',', '.')
            elif len(decimal_part) == 3 and decimal_part.isdigit():
                # Could be thousands separator (1,000)
                # But in European context with 2-decimal items, likely NOT thousands
                # Default to decimal for conservative (smaller) amounts
                return s.replace(',', '.')
        
        # Fallback: treat comma as decimal
        return s.replace(',', '.')
    
    # Only period (or neither)
    return s


def _normalize_amount(raw: str) -> str:
    """
    Normalize amount from invoice (handles both European and US formats).
    Returns a string representation of the decimal number.
    """
    # First convert European format to standard decimal
    normalized = _normalize_european_amount(str(raw or ""))
    # Then strip any remaining non-numeric characters (except decimal point)
    return re.sub(r"[^0-9.]", "", normalized)


def _is_email_domain(text: str) -> bool:
    """
    CHANGE 1: Check if text looks like an email domain (not a legal company name).
    A domain is: no spaces, contains dot, ends with known TLD.
    """
    s = str(text or "").strip()
    if not s or " " in s or "." not in s:
        return False
    tlds = ["COM", "NET", "ORG", "IO", "DE", "FR", "UK", "IN", "CO"]
    for tld in tlds:
        if s.upper().endswith("." + tld):
            return True
    return False


def _collapse_underscored_letter_tokens(text: str) -> str:
    """
    Collapses tokens that look like single letters separated by underscores.
    Example: "E_T_A__S _G_M_B_H_" -> "ETAS GMBH"
    """
    if "_" not in text:
        return text
    
    tokens = text.split()
    new_tokens = []
    for t in tokens:
        # Only act on tokens with underscores that are letters + underscores
        if "_" in t and re.fullmatch(r"[A-Za-z_]+", t):
            parts = [p for p in t.split("_") if p]
            # If we have at least 2 parts and all are single characters, collapse them
            if len(parts) >= 2 and all(len(p) == 1 for p in parts):
                new_tokens.append("".join(parts))
            else:
                new_tokens.append(t)
        else:
            new_tokens.append(t)
    return " ".join(new_tokens)


def _normalize_company_name(name: str) -> str:
    # First collapse underscored letter tokens (OCR/LLM artifacts)
    n = _collapse_underscored_letter_tokens(str(name or ""))
    # Then continue with standard normalization
    n = normalize_single_line_text(n)
    if not n:
        return ""
    
    # Priority 1: Check explicit beneficiary domain → company name mapping
    from modules.master_lookups import normalize_beneficiary_company_name
    mapped = normalize_beneficiary_company_name(n)
    if mapped != n:
        # Mapping was applied, return the result
        return mapped
    
    # Common OCR confusion in Bosch IO invoices: lIO vs IO.
    n = re.sub(r"Bosch[\.\s]*lIO", "Bosch.IO", n, flags=re.IGNORECASE)

    # Strip obvious web/domain suffixes that are not part of a legal name.
    # We prefer to keep the core identifier and let downstream heuristics
    # possibly split into words.
    original = n
    n = re.sub(r"\.(?:COM|NET|ORG|IO|CO|DE|EU|IN|UK)$", "", n, flags=re.IGNORECASE)
    # Replace leftover domain-style punctuation with spaces *only* if we removed
    # a suffix or if the name otherwise looks like a compact domain (i.e. no
    # internal whitespace).  This avoids mangling valid names such as
    # "Bosch.IO GmbH" or "S.L".
    if n != original or ("." in n and " " not in n):
        n = re.sub(r"[\.\-_]+", " ", n)

    # If the result still looks like a concatenated company name with a
    # familiar suffix, insert a space to improve readability.
    # This handles cases such as "EXPLEOGROUP" -> "EXPLEO GROUP".
    suffixes = ["GROUP", "LTD", "LLC", "PVT", "GMBH", "AG", "SA", "PLC", "CORP", "INC"]
    for suf in suffixes:
        if n.upper().endswith(suf) and not n.upper().endswith(" " + suf):
            n = n[: -len(suf)] + " " + suf
            break

    # Repair common missing-space artifacts from OCR/LLM output.
    n = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", n)
    n = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", n)
    n = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", n)
    n = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n.upper()


def _fix_address_spacing(text: str) -> str:
    """Ensure a space exists after every comma if followed by a non-space character."""
    if not text:
        return ""
    # Add space after comma if followed by a non-space char
    return re.sub(r",([^\s])", r", \1", text)


def _normalize_extracted_text(value: str) -> str:
    s = normalize_single_line_text(str(value or ""))
    if not s:
        return ""
    return _fix_address_spacing(s)


# Values that Gemini returns when it cannot extract the country — treat as blank.
_INVALID_COUNTRY_VALUES: frozenset = frozenset({
    "", "N/A", "NA", "N.A.", "N.A", "NONE", "UNKNOWN",
    "NOT AVAILABLE", "NOT_AVAILABLE", "-", "--", "NULL",
    "UNSPECIFIED", "NOT SPECIFIED", "NOT PROVIDED",
})


def _sanitize_country_text(raw: str) -> str:
    """
    Normalise a raw country string from Gemini.
    Returns empty string for any junk/sentinel value so that downstream
    fallback logic is correctly triggered.
    """
    s = normalize_single_line_text(str(raw or "")).strip()
    if s.upper() in _INVALID_COUNTRY_VALUES:
        return ""
    return s


def recover_country_from_address(address: str) -> str:
    """
    Deterministically extract a country name from a raw address string.

    Strategy — try each rule in order and return the first hit:
      1. Last comma-separated token if it matches a known country name.
      2. Last space-separated token of the whole string if it matches.

    The match is case-insensitive against the address_parser country suffix
    list so we reuse the same vocabulary.
    """
    if not address:
        return ""

    from modules.address_parser import _COUNTRY_SUFFIXES  # already sorted longest-first

    # Build a fast lookup set (upper-cased)
    country_set = {s.upper() for s in _COUNTRY_SUFFIXES}

    # Rule 1 — last comma-token
    parts = [p.strip(" ,.;") for p in address.split(",") if p.strip()]
    if parts:
        candidate = parts[-1].upper()
        if candidate in country_set:
            return parts[-1].strip().title()

    # Rule 2 — last space-token of full string (handles "... Japan" no-comma)
    tokens = address.strip().split()
    if tokens:
        candidate = tokens[-1].strip(".,;").upper()
        if candidate in country_set:
            return tokens[-1].strip(".,;").title()

    return ""


# Patterns that signal the "address" is actually a company name or division header.
_COMPANY_TAIL_RE = re.compile(
    r"(?:"
    r"\(BD\)|\(IT\)|\(HR\)|\(FIN\)|\(RD\)|\(R\s*&\s*D\)|\(MFG\)"
    r"|DIVISION|DEPARTMENT|DEPT"
    r"|GMBH|LTD\.?|PVT\.?|INC\.?|LLC|CORP\.?|S\.?R\.?O\.?|S\.?A\.?|AG|AB|B\.?V\.?"
    r")\s*$",
    re.IGNORECASE,
)


def _is_valid_postal_address(address: str, beneficiary_name: str = "") -> bool:
    """Return False if *address* looks like a company/division name rather than a postal address.

    Real street addresses virtually always contain at least one digit
    (building number, postal code).  Strings that end with legal-entity
    suffixes (GmbH, Ltd, Inc) or department abbreviations ((BD), (IT))
    are company names, not addresses.
    """
    addr = str(address or "").strip()
    if not addr:
        return True  # blank is "valid" — nothing to reject

    addr_upper = addr.upper()

    # Red flag 1: no digit at all → not a street address
    has_digit = bool(re.search(r"\d", addr_upper))

    # Red flag 2: ends with a company/division suffix
    ends_corporate = bool(_COMPANY_TAIL_RE.search(addr_upper))

    # Red flag 3: address is essentially a subset of the beneficiary name
    name_upper = " ".join(str(beneficiary_name or "").upper().split())
    addr_norm = " ".join(addr_upper.split())
    is_name_subset = bool(name_upper and addr_norm and (
        addr_norm in name_upper or name_upper in addr_norm
    ))

    if (not has_digit and ends_corporate) or is_name_subset:
        return False
    # If ONLY one red flag, still accept (some addresses legitimately lack
    # digits, e.g. "One Microsoft Way", or have a trailing corporate suffix
    # because the address genuinely starts with the company name).
    return True


def _normalize_extracted_address(value: str) -> str:
    return fix_concatenated_words(_normalize_extracted_text(str(value or ""))).upper()


def _detect_country_signals_from_text(text: str) -> str:
    """
    Scan full OCR text for country-specific signals that are unique to certain countries.
    Returns the detected country name if found, otherwise empty string.
    
    This is mainly useful for countries with unique government codes or terminology.
    """
    if not text:
        return ""
    
    text_upper = str(text or "").upper()
    
    # Portugal-specific signals (conservative to avoid false positives).
    if "NIPC" in text_upper or "ATCUD" in text_upper:
        return "PORTUGAL"
    has_portugal_explicit = "PORTUGAL" in text_upper or bool(re.search(r"\bPT\s*-\s*\d{4,5}\b", text_upper))
    portugal_markers = [
        "FATURA",
        "FACTURA",
        "SEDE",
        "CAPITAL SOCIAL",
        "LISBOA",
        "AVEIRO",
        "COVILHA",
        "BRAGA",
        "PORTO",
        "MADEIRA",
        "ACORES",
    ]
    marker_hits = sum(1 for marker in portugal_markers if marker in text_upper)
    if has_portugal_explicit and marker_hits >= 1:
        return "PORTUGAL"
    if marker_hits >= 2 and ("FATURA" in text_upper or "FACTURA" in text_upper):
        return "PORTUGAL"
    
    # Germany-specific signals
    if re.search(r'\bDE\s*-?\s*\d{5}\b', text, re.IGNORECASE):  # German ZIP code format
        return "GERMANY"
    
    # Spain-specific signals
    if re.search(r'\bES\s*-?\s*\d{5}\b', text, re.IGNORECASE):
        return "SPAIN"
    
    # France-specific signals
    if re.search(r'\bFR\s*-?\s*\d{5}\b', text, re.IGNORECASE):
        return "FRANCE"
    
    # More general country markers
    if "UNITED KINGDOM" in text_upper or "UNITED STATES" in text_upper:
        if "UNITED KINGDOM" in text_upper:
            return "UNITED KINGDOM"
        else:
            return "UNITED STATES OF AMERICA"
    
    return ""


def parse_invoice_date(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            d = datetime.strptime(text, fmt).date()
            return d.isoformat(), d.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return "", text


def _looks_like_sat_certificate(value: str) -> bool:
    """Return True if `value` looks like a Mexican SAT fiscal certificate/UUID rather than a real invoice number.

    SAT identifiers come in two forms:
    - Long purely-numeric seals: e.g. "00001000000700047508" (16+ digits, no letters)
    - UUID/GUID format: e.g. "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    """
    v = value.strip()
    # UUID / GUID pattern (8-4-4-4-12 hex groups)
    if re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', v):
        return True
    # Long purely-numeric string (16+ digits) — typical SAT certificate serial
    if re.match(r'^\d{16,}$', v):
        return True
    return False


def _recover_folio_from_text(text: str) -> str:
    """Try to extract Serie/Folio from raw invoice text (used for Mexican CFDI invoices)."""
    folio_patterns = [
        r"(?im)\bserie\s*/?\s*folio\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        r"(?im)\bfolio\s*fiscal\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        r"(?im)\bfolio\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        r"(?im)\bserie\s*[:\-]?\s*([A-Z0-9]{1,4})\s+([0-9]{4,})",  # "Serie W Folio 7120004157"
    ]
    for pat in folio_patterns:
        m = re.search(pat, text)
        if m:
            if m.lastindex and m.lastindex >= 2:
                # Pattern with separate serie + folio groups
                return (m.group(1).strip() + m.group(2).strip()).strip(".,;:")
            return m.group(1).strip().strip(".,;:")
    return ""


def _fallback_invoice_fields_from_text(text: str) -> Dict[str, str]:
    out = {"invoice_number": "", "invoice_date_raw": ""}
    t = str(text or "")
    if not t:
        return out

    number_patterns = [
        # Serie / Folio patterns (Mexican CFDI invoices) — highest priority
        r"(?im)\bserie\s*/?\s*folio\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        r"(?im)\bfolio\s*fiscal\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        r"(?im)\bfolio\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{1,})",
        # Standard invoice number patterns
        r"(?im)\b(?:invoice\s*(?:no\.?|number|#)|inv\.?\s*no\.?|reference\s*no\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{2,})",
        r"(?im)\binvoice\s*#\s*([A-Z0-9][A-Z0-9\/\-_\.]{2,})",
    ]
    for pat in number_patterns:
        m = re.search(pat, t)
        if m:
            candidate = m.group(1).strip().strip(".,;:")
            if not _looks_like_sat_certificate(candidate):
                out["invoice_number"] = candidate
                break

    date_patterns = [
        r"(?im)\b(?:invoice\s*date|date)\s*[:\-]?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)?\d{2})",
        r"(?im)\bDT\.?\s*([0-3]?\d[./-][01]?\d[./-](?:19|20)?\d{2})\b",
        r"(?im)\b(?:invoice\s*date|date)\s*[:\-]?\s*((?:19|20)\d{2}[./-][01]?\d[./-][0-3]?\d)\b",
    ]
    for pat in date_patterns:
        m = re.search(pat, t)
        if m:
            out["invoice_date_raw"] = m.group(1).strip().strip(".,;:")
            break
    return out


def _likely_indian_entity(name: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    if match_remitter(n):
        return True
    u = n.upper()
    cues = [
        "PRIVATE LIMITED",
        "PVT LTD",
        "PVT. LTD",
        "INDIA",
        "INDIAN",
    ]
    return any(c in u for c in cues)


def _likely_foreign_entity(name: str) -> bool:
    u = str(name or "").upper()
    cues = [
        " GMBH",
        " S.L",
        " SP Z O O",
        " LLC",
        " B.V",
        " AG",
        " BV",
        " PLC",
    ]
    return any(c in u for c in cues)


def _is_india_country(raw: str) -> bool:
    return _country_from_free_text(raw) == "INDIA"


def _is_foreign_country(raw: str) -> bool:
    c = _country_from_free_text(raw)
    return bool(c and c != "INDIA")


def normalize_party_roles(extracted: Dict[str, str]) -> Dict[str, str]:
    """
    Ensure remitter is the likely Indian payer and beneficiary is the likely foreign party.
    This is a post-processing guard for invoices where issuer/receiver roles are flipped by AI.
    """
    out = dict(extracted or {})
    remitter = _normalize_company_name(str(out.get("remitter_name") or "").strip())
    beneficiary = _normalize_company_name(str(out.get("beneficiary_name") or "").strip())
    out["remitter_name"] = remitter
    out["beneficiary_name"] = beneficiary
    if not remitter or not beneficiary:
        return out

    rem_country_probe = " ".join(
        [
            str(out.get("remitter_country_text") or ""),
            str(out.get("remitter_address") or ""),
            remitter,
        ]
    )
    ben_country_probe = " ".join(
        [
            str(out.get("beneficiary_country_text") or ""),
            str(out.get("beneficiary_address") or ""),
            beneficiary,
        ]
    )
    rem_country = _country_from_free_text(rem_country_probe)
    ben_country = _country_from_free_text(ben_country_probe)

    rem_is_indian = _likely_indian_entity(remitter)
    ben_is_indian = _likely_indian_entity(beneficiary)
    rem_is_foreign = _likely_foreign_entity(remitter)
    ben_is_foreign = _likely_foreign_entity(beneficiary)
    if rem_country:
        rem_is_indian = rem_is_indian or _is_india_country(rem_country)
        rem_is_foreign = rem_is_foreign or _is_foreign_country(rem_country)
    if ben_country:
        ben_is_indian = ben_is_indian or _is_india_country(ben_country)
        ben_is_foreign = ben_is_foreign or _is_foreign_country(ben_country)

    should_swap = (
        (ben_is_indian and rem_is_foreign)
        or (ben_is_indian and not rem_is_indian)
        or (rem_is_foreign and not ben_is_foreign)
    )
    if should_swap:
        out["remitter_name"], out["beneficiary_name"] = beneficiary, remitter
        out["remitter_address"], out["beneficiary_address"] = (
            str(out.get("beneficiary_address") or ""),
            str(out.get("remitter_address") or ""),
        )
        out["remitter_country_text"], out["beneficiary_country_text"] = (
            str(out.get("beneficiary_country_text") or ""),
            str(out.get("remitter_country_text") or ""),
        )
        # Structured beneficiary components refer to old beneficiary; clear to avoid wrong mapping.
        out["beneficiary_street"] = ""
        out["beneficiary_zip_text"] = ""
        out["beneficiary_city"] = ""
    return out


def _enrich_addresses_from_text(text: str, extracted: Dict[str, str]) -> Dict[str, str]:
    out = dict(extracted)
    t = str(text or "")
    if not t:
        return out

    def _clean(s: str) -> str:
        return " ".join(str(s or "").split()).strip(" ,.;:-")

    # Prefer legal/company address lines over correspondence PO box lines.
    legal_patterns = [
        r"(?im)\b([A-Za-z][A-Za-z0-9\.\-\/ ]{3,120}\d{1,6})\s*,\s*DE\s*-\s*(\d{5})\s*([A-Za-z][A-Za-z\-\s]{2,60})(?=[,\n]|$)",
        r"(?im)\b([A-Za-z][A-Za-z0-9\.\-\/ ]{3,120}\d{1,6})\s*,\s*(?:[A-Z]{2}\s*-\s*)?(\d{4,6})\s*([A-Za-z][A-Za-z\-\s]{2,60})(?=[,\n]|$)",
    ]
    po_box_patterns = [
        r"(?im)\b(Postfach\s+[0-9 ]{3,30})\s*,\s*DE\s*-\s*(\d{5})\s*([A-Za-z][A-Za-z\-\s]{2,60})(?=[,\n]|$)",
        r"(?im)\b(Postfach\s+[0-9 ]{3,30})\s*,\s*(?:[A-Z]{2}\s*-\s*)?(\d{4,6})\s*([A-Za-z][A-Za-z\-\s]{2,60})(?=[,\n]|$)",
    ]

    chosen = None
    for pat in legal_patterns:
        m = re.search(pat, t)
        if m:
            chosen = (m.group(1), m.group(2), m.group(3), "legal")
            break
    if chosen is None:
        for pat in po_box_patterns:
            m = re.search(pat, t)
            if m:
                chosen = (m.group(1), m.group(2), m.group(3), "po_box")
                break

    if chosen is not None:
        street, pin, city, _source = chosen
        out["beneficiary_street"] = _clean(street)
        out["beneficiary_zip_text"] = _clean(pin)
        out["beneficiary_city"] = _clean(city)
        if re.search(r"\bDE\s*-\s*\d{5}\b", t, flags=re.IGNORECASE):
            out["beneficiary_country_text"] = "Germany"
    if not str(out.get("beneficiary_country_text") or "").strip():
        explicit_country = _extract_country_from_text(t)
        if explicit_country:
            out["beneficiary_country_text"] = explicit_country.title()
            logger.info("country_text_extracted_from_ocr country=%s", out["beneficiary_country_text"])
    if not str(out.get("beneficiary_country_text") or "").strip():
        if re.search(r"\bGERMANY\b", t, flags=re.IGNORECASE):
            out["beneficiary_country_text"] = "Germany"
        elif re.search(r"\bDE\s*-\s*\d{5}\b", t, flags=re.IGNORECASE):
            out["beneficiary_country_text"] = "Germany"
    
    # Scan full text for unique country signals (Portugal NIPC/ATCUD, etc.)
    if not str(out.get("beneficiary_country_text") or "").strip():
        detected_country = _detect_country_signals_from_text(t)
        if detected_country:
            out["beneficiary_country_text"] = detected_country
            logger.info("country_signal_detected country=%s", detected_country)

    # Remitter (Indian) address pattern from ship/services block with 6-digit PIN.
    if not str(out.get("remitter_address") or "").strip():
        m_india = re.search(
            r"(Cyber park[^\n,]*,\s*No\.\s*76,\s*77[^\n]*?(?:Bangalore|Bengaluru)[^\n]*)",
            t,
            flags=re.IGNORECASE,
        )
        if not m_india:
            m_india = re.search(
                r"([A-Za-z0-9,\-\s]{10,120}\b(?:Bangalore|Bengaluru)\b[^\n]{0,40}\b\d{6}\b)",
                t,
                flags=re.IGNORECASE,
            )
        if m_india:
            out["remitter_address"] = " ".join(m_india.group(1).split()).strip(" ,")
    return out


def _normalize_for_matching(s: str) -> str:
    """Normalize string for fuzzy matching: uppercase, remove non-alphanumeric, collapse spaces."""
    t = re.sub(r"[^A-Za-z0-9\s]", " ", normalize_single_line_text(str(s or ""))).upper()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _fuzzy_match_nature(gemini_suggestion: str) -> str:
    """
    Fuzzy-match Gemini's nature_of_remittance suggestion against nature_rem_category.json.
    Returns the exact label from the JSON if a match is found, otherwise empty string.
    """
    if not gemini_suggestion or not gemini_suggestion.strip():
        return ""
    
    try:
        nature_data = _load_json(MASTER_DIR / "nature_rem_category.json", [])
        if not isinstance(nature_data, list):
            return ""
        
        gemini_norm = _normalize_for_matching(gemini_suggestion)
        if not gemini_norm:
            return ""
        
        # First try exact match
        for row in nature_data:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            if _normalize_for_matching(label) == gemini_norm:
                return label
        
        # If no exact match, try substring/partial match
        gemini_words = set(gemini_norm.split())
        best_match = None
        best_score = 0
        
        for row in nature_data:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            label_norm = _normalize_for_matching(label)
            label_words = set(label_norm.split())
            
            # Calculate overlap score
            overlap = len(gemini_words & label_words)
            if overlap > best_score:
                best_score = overlap
                best_match = label
        
        # Return match only if significant overlap (at least 50% of words match)
        if best_match and best_score >= max(2, len(gemini_words) // 2):
            return best_match
        
        # Backup: check for technical keywords if no fuzzy match found
        if any(kw in gemini_norm for kw in ["R D", "RESEARCH", "ENGINEERING", "TECHNICAL"]):
            return "FEES FOR TECHNICAL SERVICES"
        
        return ""
    
    except Exception as e:
        logger.warning("fuzzy_match_nature_error error=%s", str(e))
        return ""


def _fuzzy_match_purpose_group(gemini_suggestion: str) -> str:
    """
    Fuzzy-match Gemini's purpose_group suggestion against Purpose_code_List.json group_names.
    Returns the exact group_name from the JSON if a match is found, otherwise empty string.
    """
    if not gemini_suggestion or not gemini_suggestion.strip():
        return ""
    
    try:
        purpose_data = _load_json(MASTER_DIR / "Purpose_code_List.json", {"purpose_codes": []})
        purpose_codes = purpose_data.get("purpose_codes", [])
        
        if not isinstance(purpose_codes, list):
            return ""
        
        # Extract unique group names
        group_names = set()
        for row in purpose_codes:
            if isinstance(row, dict):
                group = str(row.get("group_name") or "").strip()
                if group:
                    group_names.add(group)
        
        gemini_norm = _normalize_for_matching(gemini_suggestion)
        if not gemini_norm:
            return ""
        
        # First try exact match
        for group in group_names:
            if _normalize_for_matching(group) == gemini_norm:
                return group
        
        # If no exact match, try substring/partial match
        gemini_words = set(gemini_norm.split())
        best_match = None
        best_score = 0
        
        for group in group_names:
            group_norm = _normalize_for_matching(group)
            group_words = set(group_norm.split())
            
            # Calculate overlap score
            overlap = len(gemini_words & group_words)
            if overlap > best_score:
                best_score = overlap
                best_match = group
        
        # Return match only if significant overlap
        if best_match and best_score >= max(2, len(gemini_words) // 2):
            return best_match
        
        return ""
    
    except Exception as e:
        logger.warning("fuzzy_match_purpose_group_error error=%s", str(e))
        return ""


def _fuzzy_match_purpose_code(gemini_suggestion: str, purpose_group: str = "") -> str:
    """
    Fuzzy-match Gemini's purpose_code suggestion against Purpose_code_List.json codes.
    If purpose_group is provided, only match within that group.
    Returns the exact code from the JSON if a match is found, otherwise empty string.
    """
    if not gemini_suggestion or not gemini_suggestion.strip():
        return ""
    
    try:
        purpose_data = _load_json(MASTER_DIR / "Purpose_code_List.json", {"purpose_codes": []})
        purpose_codes = purpose_data.get("purpose_codes", [])
        
        if not isinstance(purpose_codes, list):
            return ""
        
        gemini_norm = _normalize_for_matching(gemini_suggestion)
        if not gemini_norm:
            return ""
        
        # Filter by group if provided; fall back to all codes if group filter yields nothing
        def _collect_codes(rows, restrict_group):
            result = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("purpose_code") or "").strip().upper()
                group = str(row.get("group_name") or "").strip()
                if not code:
                    continue
                if restrict_group and _normalize_for_matching(group) != _normalize_for_matching(restrict_group):
                    continue
                result.append(code)
            return result

        filtered_codes = _collect_codes(purpose_codes, purpose_group)

        # If group filter returned nothing, fall back to group-agnostic search
        if not filtered_codes and purpose_group:
            logger.info(
                "fuzzy_match_purpose_code_group_miss gemini_suggestion=%s purpose_group=%s fallback=all_codes",
                gemini_suggestion, purpose_group,
            )
            filtered_codes = _collect_codes(purpose_codes, None)

        # Exact match (case-insensitive)
        for code in filtered_codes:
            if _normalize_for_matching(code) == gemini_norm:
                return code

        return ""
    
    except Exception as e:
        logger.warning("fuzzy_match_purpose_code_error error=%s", str(e))
        return ""


# Keyword-based fallback mappers for when Gemini returns empty
KEYWORD_NATURE_MAP = {
    "participant fee": "FEES FOR TECHNICAL SERVICES",
    "training": "FEES FOR TECHNICAL SERVICES",
    "trainer": "FEES FOR TECHNICAL SERVICES",
    "professional serv": "FEES FOR TECHNICAL SERVICES",
    "swe-re": "FEES FOR TECHNICAL SERVICES",
    "coaching": "FEES FOR TECHNICAL SERVICES",
    "workshop": "FEES FOR TECHNICAL SERVICES",
    "seminar": "FEES FOR TECHNICAL SERVICES",
    "subscription": "SUBSCRIPTION FEES",
    "licence": "SOFTWARE LICENCES",
    "license": "SOFTWARE LICENCES",
    "royalt": "ROYALTY",
    "consult": "CONSULTING SERVICES",
    "amc": "AMC CHARGES",
    "maintenance": "AMC CHARGES",
    "reimburs": "REIMBURSEMENT OF EXPENSES",
    "advertisement": "ADVERTISEMENT FEE",
    "software": "SOFTWARE LICENCES",
    "support": "TECHNICAL SERVICES",
    "professional service": "CONSULTING SERVICES",
    "consultancy": "CONSULTING SERVICES",
    "engineering": "ENGINEERING SERVICES",
    "testing": "TESTING CHARGES",
    "analysis": "TESTING CHARGES",
    "design": "DESIGNING CHARGES",
    "management fee": "MANAGEMENT FEES",
    "commission": "COMMISSION",
    "insurance": "INSURANCE PREMIUM",
    "freight": "FREIGHT",
    "travel": "TRAVEL & ACCOMMODATION CHARGES",
    "accommodation": "TRAVEL & ACCOMMODATION CHARGES",
    "legal": "LEGAL SERVICES",
    "audit": "TAX AUDIT FEES",
    "bandwidth": "BANDWIDTH CHARGES",
    "internet": "INTERNET CHARGES",
}

KEYWORD_PURPOSE_MAP = {
    "participant fee": ("Other Business Services", "S1023"),
    "training": ("Other Business Services", "S1023"),
    "trainer": ("Other Business Services", "S1023"),
    "professional serv": ("Other Business Services", "S1023"),
    "swe-re": ("Other Business Services", "S1023"),
    "coaching": ("Other Business Services", "S1023"),
    "workshop": ("Other Business Services", "S1023"),
    "seminar": ("Other Business Services", "S1023"),
    "subscription": ("Telecommunication, Computer & Information Services", "S1022"),
    "software": ("Telecommunication, Computer & Information Services", "S1022"),
    "licence": ("Telecommunication, Computer & Information Services", "S1022"),
    "royalt": ("Charges for the use of intellectual property n.i.e", "S1010"),
    "consult": ("Other Business Services", "S1005"),
    "amc": ("Other Business Services", "S1023"),
    "maintenance": ("Other Business Services", "S1023"),
}


def keyword_fallback(ocr_text: str) -> tuple[str, str, str]:
    """
    Fallback keyword matcher for when Gemini returns empty for nature/group/code.
    Searches OCR text for common keywords and returns best matching nature/group/code.
    Returns tuple: (nature_label, purpose_group, purpose_code)
    """
    text_lower = str(ocr_text or "").lower()
    nature = ""
    group = ""
    code = ""
    
    # Find matching nature
    for kw, val in KEYWORD_NATURE_MAP.items():
        if kw in text_lower:
            nature = val
            break
    
    # Find matching group and code
    for kw, (grp, cd) in KEYWORD_PURPOSE_MAP.items():
        if kw in text_lower:
            group = grp
            code = cd
            break
    
    return nature, group, code


def merge_multi_page_image_extractions(page_results: List[Dict[str, str]]) -> Tuple[Dict[str, str], Dict[str, object]]:
    """
    Merge per-page image extraction results for scanned multi-page PDFs.

    Rules:
    - amount: choose highest-confidence non-empty candidate; if tie, earliest page.
    - currency_short: prefer the currency from amount-selected page, else first non-empty.
    - invoice_number/date: first non-empty stable value.
    - party/address/country fields: longest non-empty value wins.
    - nature/purpose: first non-empty value.
    """
    merged: Dict[str, str] = {
        "remitter_name": "",
        "remitter_address": "",
        "beneficiary_name": "",
        "invoice_number": "",
        "invoice_date_raw": "",
        "invoice_date_iso": "",
        "invoice_date_display": "",
        "amount": "",
        "currency_short": "",
        "nature_of_remittance": "",
        "purpose_group": "",
        "purpose_code": "",
        "beneficiary_country_text": "",
        "remitter_country_text": "",
    }
    meta: Dict[str, object] = {
        "pages_considered": len(page_results),
        "amount_selected_page": 0,
        "currency_selected_page": 0,
        "amount_conflict": False,
        "amount_candidates": [],
    }
    if not page_results:
        return merged, meta

    amount_candidates: List[Dict[str, object]] = []
    for idx, row in enumerate(page_results, start=1):
        amount = _normalize_amount(str(row.get("amount") or ""))
        if not amount:
            continue
        score = 0
        if str(row.get("currency_short") or "").strip():
            score += 2
        if str(row.get("invoice_number") or "").strip():
            score += 2
        if str(row.get("invoice_date_iso") or row.get("invoice_date_raw") or "").strip():
            score += 1
        if str(row.get("beneficiary_name") or "").strip():
            score += 1
        if str(row.get("remitter_name") or "").strip():
            score += 1
        amount_candidates.append(
            {
                "page": idx,
                "amount": amount,
                "score": score,
                "currency_short": str(row.get("currency_short") or "").strip().upper(),
                "amount_source": str(row.get("amount_source") or "").strip(),
                "requires_review_ai": bool(row.get("requires_review_ai", False)),
            }
        )
    meta["amount_candidates"] = amount_candidates
    unique_amounts = {str(c["amount"]) for c in amount_candidates}
    meta["amount_conflict"] = len(unique_amounts) > 1
    if meta["amount_conflict"]:
        logger.warning("image_multi_page_amount_conflict candidates=%s", amount_candidates)

    selected_amount_page = 0
    selected_currency_page = 0
    if amount_candidates:
        best = sorted(amount_candidates, key=lambda c: (-int(c["score"]), int(c["page"])))[0]
        selected_amount_page = int(best["page"])
        merged["amount"] = str(best["amount"])
        meta["amount_selected_page"] = selected_amount_page
        if str(best.get("currency_short") or "").strip():
            merged["currency_short"] = str(best.get("currency_short") or "").strip().upper()
            selected_currency_page = selected_amount_page
            meta["currency_selected_page"] = selected_currency_page
        merged["amount_source"] = str(best.get("amount_source") or "").strip()
        merged["requires_review_ai"] = bool(best.get("requires_review_ai", False))

    def _pick_first_non_empty(keys: List[str]) -> str:
        for row in page_results:
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v:
                    return v
        return ""

    def _pick_longest(key: str) -> str:
        best_val = ""
        for row in page_results:
            v = str(row.get(key) or "").strip()
            if len(v) > len(best_val):
                best_val = v
        return best_val

    if not merged["currency_short"]:
        for idx, row in enumerate(page_results, start=1):
            v = str(row.get("currency_short") or "").strip().upper()
            if v:
                merged["currency_short"] = v
                selected_currency_page = idx
                meta["currency_selected_page"] = selected_currency_page
                break

    merged["invoice_number"] = _pick_first_non_empty(["invoice_number"])
    merged["invoice_date_iso"] = _pick_first_non_empty(["invoice_date_iso"])
    merged["invoice_date_raw"] = _pick_first_non_empty(["invoice_date_raw"])
    merged["invoice_date_display"] = _pick_first_non_empty(["invoice_date_display"])
    merged["remitter_name"] = _pick_longest("remitter_name")
    merged["beneficiary_name"] = _pick_longest("beneficiary_name")
    merged["remitter_address"] = _pick_longest("remitter_address")
    merged["beneficiary_address"] = _pick_longest("beneficiary_address")
    merged["remitter_country_text"] = _pick_longest("remitter_country_text")
    merged["beneficiary_country_text"] = _pick_longest("beneficiary_country_text")
    merged["nature_of_remittance"] = _pick_first_non_empty(["nature_of_remittance"])
    merged["purpose_code"] = _pick_first_non_empty(["purpose_code"])
    if merged["purpose_code"]:
        derived_group = _purpose_group_for_code(merged["purpose_code"])
        merged["purpose_group"] = derived_group or _pick_first_non_empty(["purpose_group"])
    else:
        merged["purpose_group"] = _pick_first_non_empty(["purpose_group"])

    return merged, meta


def gemini_extract_from_images_only(
    page_images: List[Union[str, bytes, Path]],
    *,
    invoice_id: str = "",
    prompt: str = IMAGE_ONLY_PROMPT,
) -> Dict[str, str]:
    """Run Gemini vision extraction across all provided page images and merge results."""
    if not page_images:
        return {}
    page_results: List[Dict[str, str]] = []
    for idx, page_image in enumerate(page_images, start=1):
        page_out = extract_invoice_core_fields_from_image(
            page_image,
            invoice_id=invoice_id,
            prompt=prompt,
        )
        page_results.append(page_out)
        logger.info(
            "image_only_page_done invoice_id=%s page=%s populated=%s",
            invoice_id,
            idx,
            any(str(page_out.get(k) or "").strip() for k in ("invoice_number", "amount", "currency_short", "beneficiary_name")),
        )
    if len(page_results) == 1:
        merged = dict(page_results[0])
    else:
        merged, _ = merge_multi_page_image_extractions(page_results)
    merged["_extraction_mode"] = "image_only"
    merged["_image_pages_used"] = str(len(page_results))
    return merged


def extract_invoice_core_fields(text: str, invoice_id: str = "", excel_data: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    text = normalize_invoice_text(str(text or ""), keep_newlines=True)
    out = {
        "remitter_name": "",
        "remitter_address": "",
        "remitter_country_text": "",
        "beneficiary_name": "",
        "beneficiary_address": "",
        "beneficiary_country_text": "",
        "invoice_number": "",
        "invoice_date_raw": "",
        "invoice_date_iso": "",
        "invoice_date_display": "",
        "amount": "",
        "currency_short": "",
        "nature_of_remittance": "",
        "purpose_group": "",
        "purpose_code": "",
        "_excel_text": "",
    }
    if not text or len(text.strip()) < 20:
        fallback_fields = _fallback_invoice_fields_from_text(text)
        out["invoice_number"] = fallback_fields.get("invoice_number", "")
        out["invoice_date_raw"] = fallback_fields.get("invoice_date_raw", "")
        iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
        out["invoice_date_iso"] = iso_date
        out["invoice_date_display"] = display_date
        logger.warning("gemini_extract_skipped reason=empty_or_short_text text_len=%s", len(str(text or "")))
        return out
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        fallback_fields = _fallback_invoice_fields_from_text(text)
        out["invoice_number"] = fallback_fields.get("invoice_number", "")
        out["invoice_date_raw"] = fallback_fields.get("invoice_date_raw", "")
        iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
        out["invoice_date_iso"] = iso_date
        out["invoice_date_display"] = display_date
        logger.warning(
            "gemini_extract_skipped reason=missing_client_or_key has_key=%s legacy_loaded=%s modern_loaded=%s",
            bool(GEMINI_API_KEY),
            bool(genai is not None),
            bool(google_genai is not None and google_genai_types is not None),
        )
        return out
    logger.info("gemini_extract_start text_len=%s model=%s backend=%s", len(text), GEMINI_MODEL_NAME, backend)
    parsed: Dict[str, str] = {}
    response_text = ""
    attempts = [
        {"name": "primary", "prompt": PROMPT, "max_output_tokens": 4096},
        {"name": "compact_retry", "prompt": PROMPT_COMPACT, "max_output_tokens": 8192},
    ]
    input_data = f"Invoice Text:\n{text[:60000]}"
    if excel_data:
        input_data += f"\n\nExcel Data:\n{json.dumps(excel_data, indent=2, default=_json_serializable)}"
        out["_excel_text"] = str(excel_data.get("Text") or "").strip()
    
    for idx, attempt in enumerate(attempts, start=1):
        full_prompt = attempt['prompt'].replace("{input_data}", input_data)
        response_text, finish_reason = _generate_with_gemini_text(
            full_prompt,
            max_output_tokens=int(attempt["max_output_tokens"]),
        )
        parsed = _extract_json(response_text)
        invalid = _is_invalid_gemini_extraction(parsed, response_text)
        logger.info(
            "gemini_extract_attempt attempt=%s/%s profile=%s finish_reason=%s response_text_len=%s parsed_keys=%s invalid=%s",
            idx,
            len(attempts),
            attempt["name"],
            finish_reason,
            len(response_text or ""),
            sorted(parsed.keys()),
            invalid,
        )
        if not invalid:
            break
        if idx < len(attempts):
            logger.warning(
                "gemini_extract_retry reason=invalid_or_truncated_response next_profile=%s",
                attempts[idx]["name"],
            )
    logger.info("gemini_extract_response parsed_keys=%s", sorted(parsed.keys()))
    if invoice_id:
        logger.info(
            "classification_gemini_raw invoice_id=%s nature=%r purpose_group=%r purpose_code=%r beneficiary=%r",
            invoice_id,
            parsed.get("nature_of_remittance", ""),
            parsed.get("purpose_group", ""),
            parsed.get("purpose_code", ""),
            parsed.get("beneficiary_name", ""),
        )
    out["remitter_name"] = _normalize_company_name(str(parsed.get("remitter_name") or "").strip())
    out["remitter_address"] = _normalize_extracted_address(str(parsed.get("remitter_address") or "").strip())
    out["remitter_country_text"] = _sanitize_country_text(str(parsed.get("remitter_country") or ""))
    # CHANGE 1: Reject email domains as beneficiary_name
    beneficiary_raw = str(parsed.get("beneficiary_name") or "").strip()
    if beneficiary_raw and _is_email_domain(beneficiary_raw):
        logger.warning("beneficiary_name_rejected reason=email_domain raw=%s", beneficiary_raw)
        out["beneficiary_name"] = ""
    else:
        out["beneficiary_name"] = _normalize_company_name(beneficiary_raw)
    out["beneficiary_address"] = _normalize_extracted_address(str(parsed.get("beneficiary_address") or "").strip())
    out["beneficiary_country_text"] = _sanitize_country_text(str(parsed.get("beneficiary_country") or ""))
    gemini_inv_num = str(parsed.get("invoice_number") or "").strip()
    # Reject SAT Certificate / UUID values that Gemini may mistake for an invoice number
    if gemini_inv_num and _looks_like_sat_certificate(gemini_inv_num):
        logger.info(
            "invoice_number_sat_rejected raw=%r — attempting folio recovery from text",
            gemini_inv_num,
        )
        gemini_inv_num = _recover_folio_from_text(text)
    out["invoice_number"] = gemini_inv_num
    out["invoice_date_raw"] = str(parsed.get("invoice_date") or "").strip()
    # Keep Gemini as primary extraction, but recover from OCR text when it misses date/number.
    fallback_fields = _fallback_invoice_fields_from_text(text)
    if not out["invoice_number"]:
        out["invoice_number"] = fallback_fields.get("invoice_number", "")
    if not out["invoice_date_raw"]:
        out["invoice_date_raw"] = fallback_fields.get("invoice_date_raw", "")
    iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
    out["invoice_date_iso"] = iso_date
    out["invoice_date_display"] = display_date
    # Handle amount_foreign and amount_source from new prompt
    det_amount = str(parsed.get("amount_foreign") or parsed.get("amount") or "").strip()
    out["amount"] = _normalize_amount(det_amount)
    out["currency_short"] = str(parsed.get("currency") or "").strip().upper()
    out["amount_source"] = str(parsed.get("amount_source") or "").strip()
    out["requires_review_ai"] = bool(parsed.get("requires_review", False))
    
    # Fuzzy-match and set nature_of_remittance
    nature_suggestion = str(parsed.get("nature_of_remittance") or "").strip()
    if nature_suggestion:
        matched_nature = _fuzzy_match_nature(nature_suggestion)
        out["nature_of_remittance"] = matched_nature
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s nature_before=%r nature_after=%r", invoice_id, nature_suggestion, matched_nature)
        if matched_nature:
            logger.info("nature_of_remittance_matched suggestion=%s matched=%s", nature_suggestion, matched_nature)
    
    # Fuzzy-match and set purpose_group
    group_suggestion = str(parsed.get("purpose_group") or "").strip()
    if group_suggestion:
        matched_group = _fuzzy_match_purpose_group(group_suggestion)
        out["purpose_group"] = matched_group
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s purpose_group_before=%r purpose_group_after=%r", invoice_id, group_suggestion, matched_group)
        if matched_group:
            logger.info("purpose_group_matched suggestion=%s matched=%s", group_suggestion, matched_group)
    
    # Fuzzy-match and set purpose_code (filtered by group if available)
    code_suggestion = str(parsed.get("purpose_code") or "").strip()
    if code_suggestion:
        matched_code = _fuzzy_match_purpose_code(code_suggestion, out["purpose_group"])
        out["purpose_code"] = matched_code
        if invoice_id:
            logger.info("classification_normalized invoice_id=%s purpose_code_before=%r purpose_code_after=%r", invoice_id, code_suggestion, matched_code)
        if matched_code:
            logger.info("purpose_code_matched suggestion=%s matched=%s group=%s", code_suggestion, matched_code, out["purpose_group"])
    if out["purpose_code"] and not _is_valid_purpose_code(out["purpose_code"]):
        logger.warning("purpose_code_discarded_invalid source=gemini code=%s", out["purpose_code"])
        out["purpose_code"] = ""
    
    # Keyword fallback for any empty fields
    # IMPORTANT: Only apply fallback if fields are actually empty. Never mix code/group from different sources.
    if not out["nature_of_remittance"] or not out["purpose_code"]:
        fallback_nature, fallback_group, fallback_code = keyword_fallback(text)
        if not out["nature_of_remittance"] and fallback_nature:
            # Fuzzy-match the fallback value to get exact master label
            matched = _fuzzy_match_nature(fallback_nature)
            out["nature_of_remittance"] = matched if matched else fallback_nature
            if invoice_id:
                logger.info("classification_normalized invoice_id=%s nature_before='' nature_after=%r source='keyword_fallback'", invoice_id, out["nature_of_remittance"])
            logger.info("nature_of_remittance_fallback keyword=%s matched=%s", fallback_nature, out["nature_of_remittance"])
        # Only apply code fallback if no code was matched from Gemini
        if not out["purpose_code"] and fallback_code:
            # Fuzzy-match the fallback value to get exact master code
            matched = _fuzzy_match_purpose_code(fallback_code, out["purpose_group"])
            out["purpose_code"] = matched if matched and _is_valid_purpose_code(matched) else ""
            if invoice_id:
                logger.info("classification_normalized invoice_id=%s purpose_code_before='' purpose_code_after=%r source='keyword_fallback'", invoice_id, out["purpose_code"])
            logger.info("purpose_code_fallback keyword=%s matched=%s group=%s", fallback_code, out["purpose_code"], out.get("purpose_group", ""))
    if out["purpose_code"]:
        # Keep group/code pair deterministic. Code is authoritative.
        derived_group = _purpose_group_for_code(out["purpose_code"])
        if derived_group:
            out["purpose_group"] = derived_group
    
    # NEVER apply fallback to purpose_group independently
    # If purpose_code exists, state_build will derive group from the code's JSON record
    # If purpose_code is empty, the form UI will let user select group manually

    
    out = normalize_party_roles(out)
    out = _enrich_addresses_from_text(text, out)
    out = _finalize_extracted_fields(out, text)
    # Enforce final display normalization for key party fields.
    out["remitter_name"] = _normalize_company_name(str(out.get("remitter_name") or ""))
    out["beneficiary_name"] = _normalize_company_name(str(out.get("beneficiary_name") or ""))
    out["remitter_address"] = _normalize_extracted_address(str(out.get("remitter_address") or ""))
    out["beneficiary_address"] = _normalize_extracted_address(str(out.get("beneficiary_address") or ""))
    logger.info(
        "gemini_extract_done summary=%s",
        {
            "remitter_name": out.get("remitter_name", ""),
            "remitter_country_text": out.get("remitter_country_text", ""),
            "beneficiary_name": out.get("beneficiary_name", ""),
            "beneficiary_address": out.get("beneficiary_address", ""),
            "invoice_number": out.get("invoice_number", ""),
            "amount": out.get("amount", ""),
            "currency_short": out.get("currency_short", ""),
            "beneficiary_country_text": out.get("beneficiary_country_text", ""),
            "nature_of_remittance": out.get("nature_of_remittance", ""),
            "purpose_group": out.get("purpose_group", ""),
            "purpose_code": out.get("purpose_code", ""),
        },
    )
    # Store raw invoice text for later country inference (e.g., phone prefix lookup)
    out["_raw_invoice_text"] = text
    return out


def extract_invoice_core_fields_from_image(
    image_path_or_bytes: Union[str, bytes, Path],
    invoice_id: str = "",
    prompt: str = IMAGE_EXTRACTION_PROMPT,
    excel_data: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Extract invoice fields directly from an image using Gemini's vision capabilities.
    This bypasses OCR and sends the image directly to Gemini for better accuracy.
    
    Args:
        image_path_or_bytes: Path to image file or image bytes (JPEG, PNG, etc.)
    
    Returns:
        Dictionary with extracted invoice fields
    """
    out = {
        "remitter_name": "",
        "remitter_address": "",
        "beneficiary_name": "",
        "invoice_number": "",
        "invoice_date_raw": "",
        "invoice_date_iso": "",
        "invoice_date_display": "",
        "amount": "",
        "currency_short": "",
        "nature_of_remittance": "",
        "purpose_group": "",
        "purpose_code": "",
        "beneficiary_country_text": "",
        "remitter_country_text": "",
        "_excel_text": "",
    }
    
    if excel_data:
        out["_excel_text"] = str(excel_data.get("Text") or "").strip()
    
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        logger.warning(
            "image_extract_skipped reason=missing_client_or_key has_key=%s legacy_loaded=%s modern_loaded=%s",
            bool(GEMINI_API_KEY),
            bool(genai is not None),
            bool(google_genai is not None and google_genai_types is not None),
        )
        return out
    
    try:
        logger.info(
            "image_extract_start image_source=%s backend=%s",
            "bytes" if isinstance(image_path_or_bytes, (bytes, bytearray)) else "path",
            backend,
        )
        mime_type = _get_image_mime_type(image_path_or_bytes)

        logger.info("image_extract_call model=%s mime_type=%s backend=%s", GEMINI_MODEL_NAME, mime_type, backend)
        input_data = "Image analysis requested."
        if excel_data:
            input_data = f"Excel Data:\n{json.dumps(excel_data, indent=2, default=_json_serializable)}"
        
        full_prompt = prompt.replace("{input_data}", input_data)
        response_text = _generate_with_gemini_image(full_prompt, image_path_or_bytes, mime_type)
        parsed = _extract_json(response_text)
        logger.info("image_extract_response parsed_keys=%s", sorted(parsed.keys()))
        
        # Extract and normalize fields
        out["remitter_name"] = _normalize_company_name(str(parsed.get("remitter_name") or "").strip())
        out["remitter_address"] = _normalize_extracted_address(str(parsed.get("remitter_address") or "").strip())
        out["remitter_country_text"] = _sanitize_country_text(str(parsed.get("remitter_country") or ""))
        out["beneficiary_name"] = _normalize_company_name(str(parsed.get("beneficiary_name") or "").strip())
        out["beneficiary_address"] = _normalize_extracted_address(str(parsed.get("beneficiary_address") or "").strip())
        out["beneficiary_country_text"] = _sanitize_country_text(str(parsed.get("beneficiary_country") or ""))
        out["invoice_number"] = str(parsed.get("invoice_number") or "").strip()
        out["invoice_date_raw"] = str(parsed.get("invoice_date") or "").strip()
        
        # Parse date
        iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
        out["invoice_date_iso"] = iso_date
        out["invoice_date_display"] = display_date
        
        # Normalize amount
        # Handle amount_foreign and amount_source from new prompt
        det_amount = str(parsed.get("amount_foreign") or parsed.get("amount") or "").strip()
        out["amount"] = _normalize_amount(det_amount)
        out["currency_short"] = str(parsed.get("currency") or "").strip().upper()
        out["amount_source"] = str(parsed.get("amount_source") or "").strip()
        out["requires_review_ai"] = bool(parsed.get("requires_review", False))
        out["line_items"] = parsed.get("line_items", [])
        
        # Fuzzy-match nature_of_remittance
        nature_suggestion = str(parsed.get("nature_of_remittance") or "").strip()
        if nature_suggestion:
            matched_nature = _fuzzy_match_nature(nature_suggestion)
            out["nature_of_remittance"] = matched_nature
            if matched_nature:
                logger.info("image_nature_matched suggestion=%s matched=%s", nature_suggestion, matched_nature)
        
        # Fuzzy-match purpose_group
        group_suggestion = str(parsed.get("purpose_group") or "").strip()
        if group_suggestion:
            matched_group = _fuzzy_match_purpose_group(group_suggestion)
            out["purpose_group"] = matched_group
            if matched_group:
                logger.info("image_purpose_group_matched suggestion=%s matched=%s", group_suggestion, matched_group)
        
        # Fuzzy-match purpose_code
        code_suggestion = str(parsed.get("purpose_code") or "").strip()
        if code_suggestion:
            matched_code = _fuzzy_match_purpose_code(code_suggestion, out["purpose_group"])
            out["purpose_code"] = matched_code
            if matched_code:
                logger.info("image_purpose_code_matched suggestion=%s matched=%s group=%s", code_suggestion, matched_code, out["purpose_group"])
        
        if out["purpose_code"] and not _is_valid_purpose_code(out["purpose_code"]):
            logger.warning("image_purpose_code_discarded_invalid source=gemini code=%s", out["purpose_code"])
            out["purpose_code"] = ""
        
        # Keyword fallback for empty fields
        if not out["nature_of_remittance"] or not out["purpose_code"]:
            # Create a temporary text representation from extracted fields for fallback
            temp_text = normalize_invoice_text(
                f"{out['beneficiary_name']} {out['beneficiary_address']} {out['remitter_name']} {out['remitter_address']}",
                keep_newlines=False,
            )
            fallback_nature, fallback_group, fallback_code = keyword_fallback(temp_text)
            
            if not out["nature_of_remittance"] and fallback_nature:
                matched = _fuzzy_match_nature(fallback_nature)
                out["nature_of_remittance"] = matched if matched else fallback_nature
                logger.info("image_nature_fallback keyword=%s matched=%s", fallback_nature, out["nature_of_remittance"])
            
            if not out["purpose_code"] and fallback_code:
                matched = _fuzzy_match_purpose_code(fallback_code, out["purpose_group"])
                out["purpose_code"] = matched if matched and _is_valid_purpose_code(matched) else ""
                logger.info("image_purpose_code_fallback keyword=%s matched=%s group=%s", fallback_code, out["purpose_code"], out.get("purpose_group", ""))
        
        if out["purpose_code"]:
            derived_group = _purpose_group_for_code(out["purpose_code"])
            if derived_group:
                out["purpose_group"] = derived_group
        
        # Normalize party roles
        out = normalize_party_roles(out)
        out = _finalize_extracted_fields(
            out,
            " ".join(
                [
                    str(out.get("remitter_name") or ""),
                    str(out.get("remitter_address") or ""),
                    str(out.get("beneficiary_name") or ""),
                    str(out.get("beneficiary_address") or ""),
                    str(out.get("invoice_number") or ""),
                ]
            ),
        )
        out["remitter_name"] = _normalize_company_name(str(out.get("remitter_name") or ""))
        out["beneficiary_name"] = _normalize_company_name(str(out.get("beneficiary_name") or ""))
        out["remitter_address"] = _normalize_extracted_address(str(out.get("remitter_address") or ""))
        out["beneficiary_address"] = _normalize_extracted_address(str(out.get("beneficiary_address") or ""))
        
        logger.info(
            "image_extract_done summary=%s",
            {
                "remitter_name": out.get("remitter_name", ""),
                "remitter_country": out.get("remitter_country_text", ""),
                "beneficiary_name": out.get("beneficiary_name", ""),
                "beneficiary_country": out.get("beneficiary_country_text", ""),
                "invoice_number": out.get("invoice_number", ""),
                "amount": out.get("amount", ""),
                "currency_short": out.get("currency_short", ""),
                "nature_of_remittance": out.get("nature_of_remittance", ""),
                "purpose_group": out.get("purpose_group", ""),
                "purpose_code": out.get("purpose_code", ""),
            },
        )
        
        return out
        
    except Exception as e:
        logger.exception("image_extract_error error=%s", str(e))
        return out


def extract_invoice_core_fields_from_multi_images(
    images_list: Sequence[Union[str, bytes, Path]],
    invoice_id: str = "",
    prompt: str = MULTI_PAGE_IMAGE_EXTRACTION_PROMPT,
    excel_data: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Extract consolidated invoice fields from multiple images in a SINGLE Gemini call.
    """
    out = {
        "remitter_name": "",
        "remitter_address": "",
        "beneficiary_name": "",
        "invoice_number": "",
        "invoice_date_raw": "",
        "invoice_date_iso": "",
        "invoice_date_display": "",
        "amount": "",
        "currency_short": "",
        "nature_of_remittance": "",
        "purpose_group": "",
        "purpose_code": "",
        "remitter_country_text": "",
        "_excel_text": "",
    }
    
    if not images_list:
        return out
    
    if excel_data:
        out["_excel_text"] = str(excel_data.get("Text") or "").strip()
    
    backend = _gemini_backend()
    GEMINI_API_KEY, GEMINI_MODEL_NAME = _get_gemini_config()
    if not GEMINI_API_KEY or not backend:
        return out
    
    try:
        logger.info("image_extract_multi_start count=%s backend=%s", len(images_list), backend)
        mime_type = _get_image_mime_type(images_list[0]) # Assume consistency

        input_data = "Analysis of multi-page invoice requested."
        if excel_data:
            input_data = f"Excel Data:\n{json.dumps(excel_data, indent=2, default=_json_serializable)}"
        
        full_prompt = prompt.replace("{input_data}", input_data)
        response_text = _generate_with_gemini_multi_images(full_prompt, images_list, mime_type)
        parsed = _extract_json(response_text)
        
        # Retry on empty response — handles all page counts (not just > 3)
        if _core_fields_all_empty(parsed):
            if len(images_list) <= 3:
                retry_images = images_list          # small invoice — retry all pages
            else:
                retry_images = [images_list[0], images_list[1], images_list[-1]]  # large — key pages only
            retry_full_prompt = MULTI_PAGE_IMAGE_EXTRACTION_PROMPT_STRONG.replace("{input_data}", input_data)
            logger.info(
                "image_extract_multi_retry invoice_id=%s reason=empty_response full_count=%s retry_pages=%s",
                invoice_id, len(images_list), len(retry_images),
            )
            response_text = _generate_with_gemini_multi_images(retry_full_prompt, retry_images, mime_type)
            parsed = _extract_json(response_text)

        logger.info("image_extract_multi_response parsed_keys=%s", sorted(parsed.keys()))

        # Mark extraction as failed if still empty after retry
        if _core_fields_all_empty(parsed):
            out["_extraction_quality"] = "failed"
            logger.warning(
                "image_extract_multi_failed invoice_id=%s extraction_quality=failed",
                invoice_id,
            )
        
        # Field extraction logic (same as single image but unified)
        out["remitter_name"] = _normalize_company_name(str(parsed.get("remitter_name") or "").strip())
        out["remitter_address"] = _normalize_extracted_address(str(parsed.get("remitter_address") or "").strip())
        out["remitter_country_text"] = _sanitize_country_text(str(parsed.get("remitter_country") or ""))
        out["beneficiary_name"] = _normalize_company_name(str(parsed.get("beneficiary_name") or "").strip())
        out["beneficiary_address"] = _normalize_extracted_address(str(parsed.get("beneficiary_address") or "").strip())
        out["beneficiary_country_text"] = _sanitize_country_text(str(parsed.get("beneficiary_country") or ""))
        out["invoice_number"] = str(parsed.get("invoice_number") or "").strip()
        out["invoice_date_raw"] = str(parsed.get("invoice_date") or "").strip()
        
        iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
        out["invoice_date_iso"] = iso_date
        out["invoice_date_display"] = display_date
        
        det_amount = str(parsed.get("amount_foreign") or parsed.get("amount") or "").strip()
        out["amount"] = _normalize_amount(det_amount)
        out["currency_short"] = str(parsed.get("currency") or "").strip().upper()
        out["amount_source"] = str(parsed.get("amount_source") or "").strip()
        out["requires_review_ai"] = bool(parsed.get("requires_review", False))
        
        # Fuzzy matches
        nature_sug = str(parsed.get("nature_of_remittance") or "").strip()
        if nature_sug:
            out["nature_of_remittance"] = _fuzzy_match_nature(nature_sug)
        
        group_sug = str(parsed.get("purpose_group") or "").strip()
        if group_sug:
            out["purpose_group"] = _fuzzy_match_purpose_group(group_sug)
        
        code_sug = str(parsed.get("purpose_code") or "").strip()
        if code_sug:
            out["purpose_code"] = _fuzzy_match_purpose_code(code_sug, out["purpose_group"])

        if out["purpose_code"]:
            derived_group = _purpose_group_for_code(out["purpose_code"])
            if derived_group:
                out["purpose_group"] = derived_group

        out = normalize_party_roles(out)
        out = _finalize_extracted_fields(out, "") # No single text dump here
        
        logger.info("image_extract_multi_done summary_invoice_num=%s amount=%s", out.get("invoice_number"), out.get("amount"))
        return out
        
    except Exception as e:
        logger.exception("image_extract_multi_error error=%s", str(e))
        return out
