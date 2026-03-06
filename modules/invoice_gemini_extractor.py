from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

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
    client = google_genai.Client(api_key=GEMINI_API_KEY)
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

    client = google_genai.Client(api_key=GEMINI_API_KEY)
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


IMAGE_EXTRACTION_PROMPT = """You are an expert invoice analysis AI. Your task is to analyze this invoice image and extract all required fields with MAXIMUM ACCURACY.

CRITICAL REQUIREMENTS:
1. Extract COMPLETE addresses - do not truncate or abbreviate
2. Extract FULL company names - no abbreviations
3. Identify countries explicitly from addresses, not assumptions
4. Use ALL visual elements of the PDF (header, footer, letterhead, logo area, registration blocks) when identifying company names.
5. Return data as clean JSON only (no explanations or markdown)

EXTRACT THESE FIELDS:
{
  "remitter_name": "exact FULL legal company name of the sender/issuer/from-party (the company paying the invoice)",
  "remitter_address": "COMPLETE full address of the sender/issuer including street, city, postal code, and country. If multiple address lines exist, you MUST join them with commas AND spaces (e.g. 'Hosur Road, Adugodi, Bangalore, 560030, India').",
  "remitter_country": "country name of remitter extracted from their address",
  "beneficiary_name": "exact FULL legal company name of the recipient/to-party/bill-to party (the FOREIGN company being paid).",
  "beneficiary_address": "COMPLETE full address of the recipient as a single string in the format 'Street Name + Number, ZIP City' (e.g. 'Stollwerckstrasse 11, 51149 Koln'). If multiple address lines exist, you MUST join them with commas AND spaces (e.g. '60-61 Britton Street, London, EC1M 5UX'). Always put street first, then ZIP code, then city name. Do not include the country name here; that must go in beneficiary_country separately.",
  "beneficiary_country": "country name of beneficiary extracted from their address (NOT from remitter address)",
  "invoice_number": "invoice number or reference number as printed on the document",
  "invoice_date": "date in DD/MM/YYYY format",
  "amount": "total invoice amount as a plain number only (no symbols, no commas, no currency)",
  "currency": "3-letter ISO currency code (e.g., EUR, USD, GBP, JPY, INR)",
  "nature_of_remittance": "best matching label from this list - return EXACT label or empty string: ADVERTISEMENT FEE, AMC CHARGES, ARCHITECTURAL SERVICES, BANDWIDTH CHARGES, BROKERAGE CHARGES, CARGO HANDLING SERVICES INSPECTION & LOGISTICS SERVICES, COMMISSION, COMPUTER & INFORMATION SERVICES, CONSULTING SERVICES, DESIGNING CHARGES, ENGINEERING SERVICES, FEES FOR TECHNICAL SERVICES, FOREIGN INSURANCE, FREIGHT, GRAPHIC DESIGN CHARGES, INSURANCE PREMIUM, LEGAL SERVICES, SOFTWARE LICENCES, SUBSCRIPTION FEES, TECHNICAL SERVICES, TESTING CHARGES, TRAINING CHARGES, TRANSPORT SERVICES, TRAVEL & ACCOMMODATION CHARGES",
  "purpose_group": "best matching RBI purpose group (e.g., 'Other Business Services', 'Telecommunication, Computer & Information Services', 'Charges for the use of intellectual property n.i.e')",
  "purpose_code": "best matching RBI purpose code (e.g., S1023, S0014, S1005)",
  "classification_evidence": ["array of short exact strings from invoice text justifying the classification"],
  "review_required": false,
  "classification_confidence": "high"
}

CRITICAL INSTRUCTIONS FOR ACCURACY:

1. MULTILINGUAL CONTEXT:
   This invoice may be in English, Portuguese, German, French, Spanish, Italian, and other languages.
   Common invoice terms in other languages:
   - Portuguese: "Fatura" (Invoice), "Data" (Date), "Moeda" (Currency), "Valor" (Amount), "Sede/Morada" (Address)
   - German: "Rechnung" (Invoice), "Datum" (Date), "Adresse" (Address), "Betrag" (Amount)
   - French: "Facture" (Invoice), "Date" (Date), "Adresse" (Address), "Montant" (Amount)
   - Spanish: "Factura" (Invoice), "Fecha" (Date), "Dirección" (Address), "Importe" (Amount)

2. ADDRESS EXTRACTION (THIS IS CRITICAL):
   - Extract the FULL complete address for both remitter and beneficiary
   - Include: Street address, Building/House number, City/Town, Postal/Zip code, Country
   - Do NOT abbreviate street names, cities, or districts
   - If address spans multiple lines, combine them into one complete address
   - Pay attention to all address components visible on invoice
   - For Portugal: Include districts like "Lisboa", "Porto", etc.
   - For Germany: Include the ZIP code if available (format: DE-12345)

3. REMITTER vs BENEFICIARY IDENTIFICATION:
   - REMITTER = entity issuing the invoice (appears in letterhead/top)
   - BENEFICIARY = entity being invoiced (to-party/bill-to address on invoice)
   - remitter_country should be where the issuer is based
   - beneficiary_country should be where the customer/buyer is based

4. COUNTRY EXTRACTION:
   - Extract country name from addresses (not as assumption)
   - Common countries: Germany (DE), Portugal (PT), Spain (ES), France (FR), Italy (IT), Netherlands (NL), Poland (PL), Brazil (BR), USA (US), etc.
   - Look for country codes like "DE-", "FR-", "ES-", "IT-" in postal codes or explicitly stated
   - Return FULL country name (e.g., "Germany" not "DE")
   - Do NOT infer country from email domains or addresses containing only an email; rely on physical location text such as street, postal code, VAT ID, or country name.

5. BENEFICIARY NAME EXTRACTION RULES (VISUAL PDF INPUT):
   - You are reading the full PDF visually. Use ALL visual elements — header, footer, letterhead,
     logo area, and any registration or small-print legal blocks.
   - The beneficiary is the FOREIGN company who issued this invoice / is being paid (the seller/exporter),
     not the Indian remitter.
   - Look specifically in these locations to identify the beneficiary name:
     (a) Top-left or top-center: usually the company logo + brand + name.
     (b) Bottom footer: registration line such as
         "Expleo Technology Germany GmbH • HRB 98200 • ...".
     (c) Any block that contains HRB/HRA (German court registration), VAT ID (e.g. "DE..."),
         IBAN, or BIC/SWIFT codes — the full legal company name is almost always very close
         to these identifiers.
   - Legal suffixes to look for when deciding on the final beneficiary name include:
     GmbH, LLC, Ltd, PLC, AG, SA, BV, NV, SRL, AB, Corp, Inc, SAS, KG, SARL, Oy, AS.
   - NEVER return an email address or domain (like "EXPLEOGROUP.COM") as the beneficiary name.
     If you see an email or domain near the logo or footer, treat it as a clue and look nearby
     for the actual company name with a legal suffix, and return that company name instead.
   - If the top of the invoice shows only a logo/brand name without a legal suffix (for example
     "{ expleo }"), scan the footer and registration blocks for the full legal entity name
     (for example "Expleo Technology Germany GmbH") and return that full legal name.

6. EUROPEAN NUMBER FORMAT:
   - Some invoices use European format: comma for decimal (65,00 = 65.00), period for thousands (1.234,56 = 1234.56)
   - Should return normalized: "65.00" not "65,00"
   - Always extract NET TOTAL, not line items

7. INVOICE DATES:
   - Look for "Invoice Date", "Date", "Datum", "Data", "Fecha"
   - Return in DD/MM/YYYY format
   - Common formats you might see: DD/MM/YYYY, DD.MM.YYYY, YYYY-MM-DD

8. AMOUNT AND CURRENCY:
   - Extract TOTAL invoice amount (not line items)
   - Remove all currency symbols and formatting
   - Return pure number: "1245.67" (not "1.245,67 EUR")
   - Currency: Look for symbols (€, £, $, ¥) or 3-letter codes (EUR, GBP, USD, JPY)


9. CLASSIFICATION & PURPOSE CODE RULES (VERY IMPORTANT):
   - Prefer the actual item/service being paid for over generic terms.
   - "software", "license", "licence", "upgrade", "subscription", "renewal" strongly suggest SOFTWARE LICENCES and likely purpose code S0902. Do not classify these as registration charges.
   - "commissioning", "installation", "startup", "engineer visit", "user site", "service ticket", "site support" strongly suggest FEES FOR TECHNICAL SERVICES (e.g., S1009), not Research & Development (R&D).
   - Do not output broad guesses like "registration charges" unless the invoice text clearly says registration fee or conference/event registration.
   - If invoice text supports multiple interpretations, return the best fit but set review_required=true.
   - If unsure, never invent. Return best guess with evidence and review_required=true.
   - If the exact purpose code is uncertain but the nature is clear, keep the best-supported nature, set review_required=true, and do not force a wrong purpose_code.

   FEW-SHOT CLASSIFICATION EXAMPLES:
   
   Example 1: Software license / upgrade
   Invoice text snippet: "Winsam 8 Upgrade", "Software upgrade", "License renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   Reason: Payment for software rights/usage, not a registration charge.
   
   Example 2: Software subscription
   Invoice text snippet: "Annual software subscription", "Enterprise license renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   
   Example 3: Technical commissioning at user site
   Invoice text snippet: "Total commissioning charge user site", "Engineer visit", "Service ticket number"
   Interpretation: nature_of_remittance: "FEES FOR TECHNICAL SERVICES", purpose_group: "Other Business Services", purpose_code: "S1009"
   Reason: This is technical/on-site commissioning, not R&D.
   
   Example 4: Legal services
   Invoice text snippet: "Legal services", "Attorney fee", "Contract review services"
   Interpretation: nature_of_remittance: "LEGAL SERVICES"
   
   Example 5: True registration/event fee
   Invoice text snippet: "Conference registration fee", "Delegate registration", "Seminar registration charges"
   Interpretation: Map to registration/training category. Do not confuse with software upgrade/license.

Return ONLY valid JSON with no additional text or markdown formatting."""


IMAGE_ONLY_PROMPT = """This PDF could not be text-extracted. You are seeing raw rendered pages.
Read every visible character carefully from the images.

Extract all of the following by checking across ALL pages:
- invoice_number (Invoice No., Rechnungsnummer, No.)
- invoice_date (Date Invoice, Datum, DD.MM.YYYY-like dates)
- amount (Invoice amount, Total, Grand total, Rechnungsbetrag)
- currency (EUR/USD/GBP/etc.)
- remitter_name (CUSTOMER/BUYER paying)
- beneficiary_name (SUPPLIER/SELLER issuing invoice)
- beneficiary_address (supplier address)
- remitter_address (buyer/remitter address when visible)
- nature_of_remittance (service description from line items)
- purpose_group (best RBI group)
- purpose_code (best RBI purpose code)

Return JSON only.
If a value is clearly visible, do not leave it empty.
If truly not visible after careful review, return null.
"""


PROMPT = """Extract the following fields from this invoice as JSON only, no explanation:
{
  "remitter_name": "exact legal company name of the sender/issuer/from-party",
  "remitter_address": "full address of the sender/issuer. You MUST combine multiple lines with commas AND spaces (e.g., 'Street, City, Postcode, Country').",
  "remitter_country": "country name of remitter extracted from remitter address",
  "beneficiary_name": "exact legal company name of the recipient/to-party/bill-to party",
  "beneficiary_address": "full address of the recipient as a single string. You MUST combine multiple lines with commas AND spaces (e.g., 'Street Name, ZIP Code, City Name'). Do not include the country name here; it goes in beneficiary_country.",
  "beneficiary_country": "country name of beneficiary extracted from beneficiary address",
  "invoice_number": "invoice number or reference number as printed",
  "invoice_date": "date in DD/MM/YYYY format",
  "amount": "total invoice amount as a number only, no symbols, no commas",
  "currency": "3-letter ISO currency code (EUR, USD, GBP, JPY, etc.)",
  "nature_of_remittance": "best matching label from this list - return EXACT label text or empty string: ADVERTISEMENT FEE, AMC CHARGES, ARCHITECTURAL SERVICES, BANDWIDTH CHARGES, BROKERAGE CHARGES, BUSINESS INCOME OTHER THAN THAT COVERED BY CATEGORIES ABOVE, CARGO HANDLING SERVICES INSPECTION & LOGISTICS SERVICES, CELLULAR ROAMING CHARGES, CHARTER HIRE CHARGES (SHIPPING), CLEARING & FORWARDING CHARGES, COMMISSION, COMPUTER & INFORMATION SERVICES, CONSULTING SERVICES, CUSTOM HOUSE AGENT CHARGES, DESIGNING CHARGES, DISTRIBUTION SERVICES, EDUCATIONAL SERVICES, ENGINEERING SERVICES, FACILITY RELATED SERVICES, FEES FOR TECHNICAL SERVICES, FOREIGN COMMISSION, FOREIGN HOSPITALITY, FOREIGN INSURANCE, FOREIGN SERVICE CHARGES, FREIGHT, GENERAL INSURANCE CLAIMS, GENERAL INSURANCE PREMIUM, GRAPHIC DESIGN CHARGES, HEALTH SERVICES, HOSPITALITY SERVICES, HOTEL & LODGING CHARGES, ISD CHARGES, INSURANCE AGENCY & BROKERAGE, INSURANCE PREMIUM, INSURANCE, INTERNET CHARGES, KNOWLEDGE BASED CONSULTING, LEGAL SERVICES, LIFE INSURANCE, LIFE INSURANCE PREMIUM, LIFE INSURANCE CLAIMS, LODGING CHARGES, MAINTENANCE & REPAIR CHARGES, MANAGEMENT FEES, MARINE INSURANCE PREMIUM, MARINE INSURANCE CLAIMS, MISCELLANEOUS PERSONAL SERVICES, MISCELLANEOUS PUBLISHED WORK, MISCELLANEOUS SERVICES, MISCELLANEOUS TECHNICAL SERVICES, MISCELLANEOUS TRANSPORT SERVICES, OPERATIONAL CHARGES FOR OVERSEAS OFFICES, OTHER DISTRIBUTION SERVICES, OTHER FINANCE CHARGES, OTHER MARITIME TRAFFIC MANAGEMENT, OTHER MISCELLANEOUS BUSINESS SERVICES, OTHER MISCELLANEOUS EDUCATIONAL SERVICES, OTHER MISCELLANEOUS HEALTH SERVICES, OTHER MISCELLANEOUS PROFESSIONAL SERVICES, OTHER MISCELLANEOUS SERVICES, PATIENT TREATMENT CHARGES, PERSONAL SERVICES, PHOTOGRAPHY CHARGES, POSTAL SERVICES, PUBLICATION SERVICES, PUBLISHING SERVICES, RADIO AND TELEVISION SERVICES, REIMBURSEMENT OF EXPENSES, REPAIR & MAINTENANCE CHARGES, REPAIR SERVICES, RESEARCH & DEVELOPMENT CHARGES, RESEARCH SERVICES, ROYALTY, ROYALTY ON COPYRIGHTS AND LITERARY WORKS, ROYALTY ON DESIGN & TRADEMARK, ROYALTY ON INDUSTRIAL PROCESS, ROYALTY ON PATENT, ROYALTY ON TECHNICAL KNOW-HOW, SOFTWARE LICENCES, SPACE & TIME LEASING & RENTAL CHARGES, SPACE AND TIME LEASING, SPACE/BANDWIDTH LEASING FOR TELECOMMUNICATION SERVICES, SPECIAL DRAWING RIGHTS (SDRS), SUBSCRIPTION FEES, SUBSCRIPTION FOR PERIODICALS & NEWSPAPERS, SUBSCRIPTION FOR PROFESSIONAL ASSOCIATIONS, SUBSCRIPTION FOR TECHNICAL DATA, SUBSCRIPTION SERVICES, SUPPORT & MAINTENANCE CHARGES, TAX AUDIT FEES, TECHNICAL ASSISTANCE FEES, TECHNICAL SERVICE FEES, TECHNICAL SUPPORT & MAINTENANCE, TELECOMMUNICATION CHARGES, TESTING & ANALYSIS CHARGES, TESTING CHARGES, CONSULTING SERVICES FOR TECHNICAL SERVICES, CONSULTANCY, TRAINING CHARGES, TRANSPORT SERVICES CHARGES, TRANSPORT SERVICES, TRANSPORT/SHIPPING CHARGES, TRAVEL CHARGES, TRAVEL & ACCOMODATION CHARGES, TRAVEL & ACCOMMODATION CHARGES, TRAVEL INSURANCE, TRIBUNAL LEVY, TRIMMING CHARGES, VIDEO PRODUCTION CHARGES, VISA FEES, VISA AND VACCINATION & HEALTH CHARGES, VISIT VISA FEES",
  "purpose_group": "best matching RBI purpose group name - return EXACT group name or empty string (e.g. 'Other Business Services', 'Charges for the use of intellectual property n.i.e', 'Telecommunication, Computer & Information Services', 'Financial and Insurance Services')",
  "purpose_code": "best matching RBI purpose code from the group above - return EXACT code (e.g. S1023, S0014, S1005) or empty string if unsure",
  "classification_evidence": ["array of short exact strings from invoice text justifying the classification"],
  "review_required": false,
  "classification_confidence": "high"
}
Return only valid JSON. If a field cannot be found, return an empty string for it.

CRITICAL INSTRUCTIONS:

1. MULTILINGUAL CONTEXT:
   This invoice may contain text in a mix of English and local languages. We receive invoices from
   approximately 60 countries from these regions:
   - Europe: Portugal, Germany, France, Spain, Italy, Netherlands, Belgium, Austria, Switzerland, 
     Sweden, Norway, Denmark, Poland, Czech Republic, Hungary, Romania, Bulgaria, Greece, Finland
   - Asia: Japan, China, South Korea, Singapore, Malaysia, Thailand, Vietnam, Taiwan, Indonesia
   - Americas: Brazil, Mexico, Argentina, Colombia, Chile
   - Middle East: UAE, Saudi Arabia, Israel, Turkey
   - Others: Australia, South Africa, Canada, New Zealand
   
   Key multilingual invoice terms you may encounter:
   - "Fatura"/"Factura" (PT/ES) = Invoice
   - "Rechnung" (DE) = Invoice
   - "Facture" (FR) = Invoice
   - "Fattura" (IT) = Invoice
   - "Sede" (PT/ES) = Headquarters/Office Address
   - "NIPC"/"NIF" (PT) = Portuguese Tax ID
   - "Capital social" (PT/ES) = Share Capital
   - "ATCUD" (PT) = Portuguese Invoice Authentication Code (NOT invoice number)
   - "Doc No." / "Nr." / "N°" / "Nº" / "Número" = Invoice Number
   - "Doc Date" / "Datum" / "Data" = Invoice Date

2. EUROPEAN NUMBER FORMAT:
   Some invoices use European number format where:
   - Comma (,) is the DECIMAL separator: "65,00" means EUR 65.00 (not €65,000)
   - Period (.) is the THOUSANDS separator: "1.000" means one thousand
   - When you see "65,000" in a EUROPEAN invoice, it means 65.000 (sixty-five, not sixty-five thousand)
   
   CRITICAL: Always extract the NET TOTAL invoice amount (not line items).
   Look for keywords: "Net", "Total", "Invoice amount", "Amount due", "Total due", "Montant"
   Return the final invoice total as a pure number (e.g., "65.00" not "65,00").

3. PORTUGAL-SPECIFIC FIELDS:
   ATCUD (Portuguese Invoice Authentication Code) is a government-generated code like "JFXFDRRY-2881728917".
   - ATCUD is NOT the invoice number.
   - The invoice number appears separately after "Doc No." or "Número de Documento".
   - Never extract ATCUD as the invoice_number.

4. INVOICE NUMBER EXTRACTION:
   The invoice_number must come from explicit labels such as:
   - "Invoice No", "Invoice Number", "Invoice #", "Inv No", "Reference Number", "Ref No", "Nº", "Número".
   - Do NOT extract: ATCUD codes, PO numbers, order IDs, or authentication codes.
   - For Portuguese invoices, skip "ATCUD" and look for "Doc No." or similar.

5. CRITICAL RULE: INDIAN OUTWARD REMITTANCE ROLE ASSIGNMENT
   ⚠️ THIS IS THE MOST IMPORTANT RULE FOR THIS FORM ⚠️
   
   This form is for INDIAN COMPANIES making outward remittance payments to foreign beneficiaries.
   
   THUS:
   - remitter_name MUST BE an INDIAN entity (the company paying/sending money)
   - beneficiary_name MUST BE a FOREIGN entity (the company receiving/being paid)
   
   HOW TO IDENTIFY:
   - Look for the address that contains "INDIA" or Indian city names: 
     Mumbai, Bangalore, Bengaluru, New Delhi, Delhi, Chennai, Hyderabad, Jaipur, Pune, 
     Chandigarh, Kolkata, Ahmedabad, Agra, Indore, Surat, Nashik, Vadodara, Nagpur
   → This entity (with INDIA address) is ALWAYS the remitter
   
   - The other entity (with non-Indian address like Portugal, Germany, USA, etc.)
   → This is ALWAYS the beneficiary
   
   DO NOT assume the entity appearing in the invoice letterhead (top) is either the remitter or 
   beneficiary. IDENTIFY BASED ON ADDRESS LOCATION:
   - Indian address → remitter (they are paying)
   - Foreign address → beneficiary (they are being paid)
   
   SPECIAL NOTE ON LAYOUT:
   Foreign supplier invoices often have the supplier's details at the top (letterhead).
   The buyer's (Indian company's) details often appear lower on the invoice (bill-to section).
   DO NOT let letter head position confuse role assignment. ALWAYS assign by address location.
   

6. CLASSIFICATION & PURPOSE CODE RULES (VERY IMPORTANT):

   - Prefer the actual item/service being paid for over generic terms.
   - "software", "license", "licence", "upgrade", "subscription", "renewal" strongly suggest SOFTWARE LICENCES and likely purpose code S0902. Do not classify these as registration charges.
   - "commissioning", "installation", "startup", "engineer visit", "user site", "service ticket", "site support" strongly suggest FEES FOR TECHNICAL SERVICES (e.g., S1009), not Research & Development (R&D).
   - Do not output broad guesses like "registration charges" unless the invoice text clearly says registration fee or conference/event registration.
   - If invoice text supports multiple interpretations, return the best fit but set review_required=true.
   - If unsure, never invent. Return best guess with evidence and review_required=true.
   - If the exact purpose code is uncertain but the nature is clear, keep the best-supported nature, set review_required=true, and do not force a wrong purpose_code.

   FEW-SHOT CLASSIFICATION EXAMPLES:
   
   Example 1: Software license / upgrade
   Invoice text snippet: "Winsam 8 Upgrade", "Software upgrade", "License renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   Reason: Payment for software rights/usage, not a registration charge.
   
   Example 2: Software subscription
   Invoice text snippet: "Annual software subscription", "Enterprise license renewal"
   Interpretation: nature_of_remittance: "SOFTWARE LICENCES", purpose_code: "S0902"
   
   Example 3: Technical commissioning at user site
   Invoice text snippet: "Total commissioning charge user site", "Engineer visit", "Service ticket number"
   Interpretation: nature_of_remittance: "FEES FOR TECHNICAL SERVICES", purpose_group: "Other Business Services", purpose_code: "S1009"
   Reason: This is technical/on-site commissioning, not R&D.
   
   Example 4: Legal services
   Invoice text snippet: "Legal services", "Attorney fee", "Contract review services"
   Interpretation: nature_of_remittance: "LEGAL SERVICES"
   
   Example 5: True registration/event fee
   Invoice text snippet: "Conference registration fee", "Delegate registration", "Seminar registration charges"
   Interpretation: Map to registration/training category. Do not confuse with software upgrade/license.


7. BEST EFFORT:

   - For nature_of_remittance, purpose_group, purpose_code: return your best matching suggestion even if 
     not 100% certain. Always return something unless the invoice gives absolutely no clue.
    - Prefer returning a close match over returning empty.
"""

PROMPT_COMPACT = """Extract invoice fields as strict JSON only (no markdown, no explanation):
{
  "remitter_name": "",
  "remitter_address": "",
  "remitter_country": "",
  "beneficiary_name": "",
  "beneficiary_address": "",
  "beneficiary_country": "",
  "invoice_number": "",
  "invoice_date": "",
  "amount": "",
  "currency": "",
  "nature_of_remittance": "",
  "purpose_group": "",
  "purpose_code": "",
  "classification_evidence": [],
  "review_required": false,
  "classification_confidence": "high"
}
Rules:
1. Outward remittance policy: remitter is Indian payer, beneficiary is foreign payee.
2. Use invoice text only. Do not guess missing fields.
3. Return empty string for unknown fields.
4. Return valid JSON object only.
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


def _normalize_extracted_address(value: str) -> str:
    return fix_concatenated_words(_normalize_extracted_text(str(value or "")))


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


def _fallback_invoice_fields_from_text(text: str) -> Dict[str, str]:
    out = {"invoice_number": "", "invoice_date_raw": ""}
    t = str(text or "")
    if not t:
        return out

    number_patterns = [
        r"(?im)\b(?:invoice\s*(?:no\.?|number|#)|inv\.?\s*no\.?|reference\s*no\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\/\-_\.]{2,})",
        r"(?im)\binvoice\s*#\s*([A-Z0-9][A-Z0-9\/\-_\.]{2,})",
    ]
    for pat in number_patterns:
        m = re.search(pat, t)
        if m:
            out["invoice_number"] = m.group(1).strip().strip(".,;:")
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
        
        # Filter by group if provided
        filtered_codes = []
        for row in purpose_codes:
            if not isinstance(row, dict):
                continue
            code = str(row.get("purpose_code") or "").strip().upper()
            group = str(row.get("group_name") or "").strip()
            
            if not code:
                continue
            
            # If purpose_group is specified, only consider codes from that group
            if purpose_group and _normalize_for_matching(group) != _normalize_for_matching(purpose_group):
                continue
            
            filtered_codes.append(code)
        
        # First try exact match (case-insensitive)
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


def extract_invoice_core_fields(text: str, invoice_id: str = "") -> Dict[str, str]:
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
    for idx, attempt in enumerate(attempts, start=1):
        response_text, finish_reason = _generate_with_gemini_text(
            f"{attempt['prompt']}\n\nINVOICE TEXT:\n{text[:60000]}",
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
    out["remitter_country_text"] = _normalize_extracted_text(str(parsed.get("remitter_country") or "").strip())
    # CHANGE 1: Reject email domains as beneficiary_name
    beneficiary_raw = str(parsed.get("beneficiary_name") or "").strip()
    if beneficiary_raw and _is_email_domain(beneficiary_raw):
        logger.warning("beneficiary_name_rejected reason=email_domain raw=%s", beneficiary_raw)
        out["beneficiary_name"] = ""
    else:
        out["beneficiary_name"] = _normalize_company_name(beneficiary_raw)
    out["beneficiary_address"] = _normalize_extracted_address(str(parsed.get("beneficiary_address") or "").strip())
    out["beneficiary_country_text"] = _normalize_extracted_text(str(parsed.get("beneficiary_country") or "").strip())
    out["invoice_number"] = str(parsed.get("invoice_number") or "").strip()
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
    out["amount"] = _normalize_amount(str(parsed.get("amount") or ""))
    out["currency_short"] = str(parsed.get("currency") or "").strip().upper()
    
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
    }
    
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
        response_text = _generate_with_gemini_image(prompt, image_path_or_bytes, mime_type)
        parsed = _extract_json(response_text)
        logger.info("image_extract_response parsed_keys=%s", sorted(parsed.keys()))
        
        # Extract and normalize fields
        out["remitter_name"] = _normalize_company_name(str(parsed.get("remitter_name") or "").strip())
        out["remitter_address"] = _normalize_extracted_address(str(parsed.get("remitter_address") or "").strip())
        out["remitter_country_text"] = _normalize_extracted_text(str(parsed.get("remitter_country") or "").strip())
        out["beneficiary_name"] = _normalize_company_name(str(parsed.get("beneficiary_name") or "").strip())
        out["beneficiary_address"] = _normalize_extracted_address(str(parsed.get("beneficiary_address") or "").strip())
        out["beneficiary_country_text"] = _normalize_extracted_text(str(parsed.get("beneficiary_country") or "").strip())
        out["invoice_number"] = str(parsed.get("invoice_number") or "").strip()
        out["invoice_date_raw"] = str(parsed.get("invoice_date") or "").strip()
        
        # Parse date
        iso_date, display_date = parse_invoice_date(out["invoice_date_raw"])
        out["invoice_date_iso"] = iso_date
        out["invoice_date_display"] = display_date
        
        # Normalize amount
        out["amount"] = _normalize_amount(str(parsed.get("amount") or ""))
        out["currency_short"] = str(parsed.get("currency") or "").strip().upper()
        
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
