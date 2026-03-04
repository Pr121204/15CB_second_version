from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict
import re

from modules.currency_mapping import load_currency_exact_index, resolve_currency_selection
from modules.address_parser import parse_beneficiary_address
from modules.form15cb_constants import (
    IT_ACT_RATE_DEFAULT,
    MODE_NON_TDS,
    MODE_TDS,
    PROPOSED_DATE_OFFSET_DAYS,
)
from modules.invoice_calculator import recompute_invoice
from modules.logger import get_logger
from modules.master_lookups import (
    infer_country_from_beneficiary_name,
    load_nature_options,
    load_purpose_grouped,
    match_remitter,
    resolve_bank_code,
    resolve_country_name,
    resolve_dtaa,
    split_dtaa_article_text,
)
from modules.text_normalizer import fix_concatenated_words, normalize_single_line_text

# CHANGE 2: Phone prefix to country code mapping for low-confidence inference
# Internal numeric country codes used by this project (e.g., "49" for Germany).
PHONE_PREFIX_TO_COUNTRY = {
    "+1": "1",      # United States
    "+7": "146",    # Russia
    "+20": "45",    # Egypt
    "+27": "164",   # South Africa
    "+30": "72",    # Greece
    "+31": "131",   # Netherlands
    "+32": "29",    # Belgium
    "+33": "64",    # France
    "+34": "166",   # Spain
    "+39": "112",   # Italy
    "+41": "169",   # Switzerland
    "+43": "20",    # Austria
    "+44": "114",   # United Kingdom
    "+49": "49",    # Germany
    "+51": "143",   # Peru
    "+52": "127",   # Mexico
    "+54": "12",    # Argentina
    "+55": "35",    # Brazil
    "+56": "39",    # Chile
    "+60": "126",   # Malaysia
    "+61": "22",    # Australia
    "+62": "107",   # Indonesia
    "+63": "141",   # Philippines
    "+64": "133",   # New Zealand
    "+65": "157",   # Singapore
    "+66": "177",   # Thailand
    "+81": "111",   # Japan
    "+82": "121",   # South Korea
    "+84": "188",   # Vietnam
    "+86": "38",    # China
    "+90": "179",   # Turkey
    "+91": "91",    # India
    "+92": "138",   # Pakistan
    "+93": "19",    # Afghanistan
    "+94": "158",   # Sri Lanka
    "+95": "130",   # Myanmar
    "+98": "109",   # Iran
}

# Optional: Phone prefix to ISO alpha-2 country codes for reference and future use.
# NOTE: Current inference logic uses PHONE_PREFIX_TO_COUNTRY above (numeric codes)
# to remain compatible with existing DTAA/country master data.
PHONE_PREFIX_TO_COUNTRY_ALPHA2 = {
    # North America (shared +1)
    "+1": "US",      # United States (also Canada, Caribbean)
    # Russia / Kazakhstan
    "+7": "RU",      # Russia (also Kazakhstan)
    # Europe
    "+20": "EG",     # Egypt
    "+27": "ZA",     # South Africa
    "+30": "GR",     # Greece
    "+31": "NL",     # Netherlands
    "+32": "BE",     # Belgium
    "+33": "FR",     # France
    "+34": "ES",     # Spain
    "+36": "HU",     # Hungary
    "+39": "IT",     # Italy
    "+40": "RO",     # Romania
    "+41": "CH",     # Switzerland
    "+43": "AT",     # Austria
    "+44": "GB",     # United Kingdom
    "+45": "DK",     # Denmark
    "+46": "SE",     # Sweden
    "+47": "NO",     # Norway
    "+48": "PL",     # Poland
    "+49": "DE",     # Germany
    "+51": "PE",     # Peru
    "+52": "MX",     # Mexico
    "+53": "CU",     # Cuba
    "+54": "AR",     # Argentina
    "+55": "BR",     # Brazil
    "+56": "CL",     # Chile
    "+57": "CO",     # Colombia
    "+58": "VE",     # Venezuela
    "+60": "MY",     # Malaysia
    "+61": "AU",     # Australia
    "+62": "ID",     # Indonesia
    "+63": "PH",     # Philippines
    "+64": "NZ",     # New Zealand
    "+65": "SG",     # Singapore
    "+66": "TH",     # Thailand
    "+81": "JP",     # Japan
    "+82": "KR",     # South Korea
    "+84": "VN",     # Vietnam
    "+86": "CN",     # China
    "+90": "TR",     # Turkey
    "+91": "IN",     # India
    "+92": "PK",     # Pakistan
    "+93": "AF",     # Afghanistan
    "+94": "LK",     # Sri Lanka
    "+95": "MM",     # Myanmar
    "+98": "IR",     # Iran
    "+211": "SS",    # South Sudan
    "+212": "MA",    # Morocco
    "+213": "DZ",    # Algeria
    "+216": "TN",    # Tunisia
    "+218": "LY",    # Libya
    "+220": "GM",    # Gambia
    "+221": "SN",    # Senegal
    "+222": "MR",    # Mauritania
    "+223": "ML",    # Mali
    "+224": "GN",    # Guinea
    "+225": "CI",    # Côte d'Ivoire
    "+226": "BF",    # Burkina Faso
    "+227": "NE",    # Niger
    "+228": "TG",    # Togo
    "+229": "BJ",    # Benin
    "+230": "MU",    # Mauritius
    "+231": "LR",    # Liberia
    "+232": "SL",    # Sierra Leone
    "+233": "GH",    # Ghana
    "+234": "NG",    # Nigeria
    "+235": "TD",    # Chad
    "+236": "CF",    # Central African Republic
    "+237": "CM",    # Cameroon
    "+238": "CV",    # Cape Verde
    "+239": "ST",    # São Tomé and Príncipe
    "+240": "GQ",    # Equatorial Guinea
    "+241": "GA",    # Gabon
    "+242": "CG",    # Republic of the Congo
    "+243": "CD",    # Democratic Republic of the Congo
    "+244": "AO",    # Angola
    "+245": "GW",    # Guinea-Bissau
    "+248": "SC",    # Seychelles
    "+249": "SD",    # Sudan
    "+250": "RW",    # Rwanda
    "+251": "ET",    # Ethiopia
    "+252": "SO",    # Somalia
    "+253": "DJ",    # Djibouti
    "+254": "KE",    # Kenya
    "+255": "TZ",    # Tanzania
    "+256": "UG",    # Uganda
    "+257": "BI",    # Burundi
    "+258": "MZ",    # Mozambique
    "+260": "ZM",    # Zambia
    "+261": "MG",    # Madagascar
    "+262": "RE",    # Réunion (France)
    "+263": "ZW",    # Zimbabwe
    "+264": "NA",    # Namibia
    "+265": "MW",    # Malawi
    "+266": "LS",    # Lesotho
    "+267": "BW",    # Botswana
    "+268": "SZ",    # Eswatini (Swaziland)
    "+269": "YT",    # Mayotte (France)
    "+290": "SH",    # Saint Helena
    "+291": "ER",    # Eritrea
    "+297": "AW",    # Aruba
    "+298": "FO",    # Faroe Islands
    "+299": "GL",    # Greenland
    "+350": "GI",    # Gibraltar
    "+352": "LU",    # Luxembourg
    "+353": "IE",    # Ireland
    "+354": "IS",    # Iceland
    "+355": "AL",    # Albania
    "+356": "MT",    # Malta
    "+357": "CY",    # Cyprus
    "+358": "FI",    # Finland
    "+359": "BG",    # Bulgaria
    "+370": "LT",    # Lithuania
    "+371": "LV",    # Latvia
    "+372": "EE",    # Estonia
    "+373": "MD",    # Moldova
    "+374": "AM",    # Armenia
    "+375": "BY",    # Belarus
    "+376": "AD",    # Andorra
    "+377": "MC",    # Monaco
    "+378": "SM",    # San Marino
    "+380": "UA",    # Ukraine
    "+381": "RS",    # Serbia
    "+382": "ME",    # Montenegro
    "+385": "HR",    # Croatia
    "+386": "SI",    # Slovenia
    "+387": "BA",    # Bosnia and Herzegovina
    "+389": "MK",    # North Macedonia
    "+420": "CZ",    # Czech Republic
    "+421": "SK",    # Slovakia
    "+423": "LI",    # Liechtenstein
    "+500": "FK",    # Falkland Islands
    "+501": "BZ",    # Belize
    "+502": "GT",    # Guatemala
    "+503": "SV",    # El Salvador
    "+504": "HN",    # Honduras
    "+505": "NI",    # Nicaragua
    "+506": "CR",    # Costa Rica
    "+507": "PA",    # Panama
    "+508": "PM",    # Saint Pierre and Miquelon
    "+509": "HT",    # Haiti
    "+590": "GP",    # Guadeloupe (France)
    "+591": "BO",    # Bolivia
    "+592": "GY",    # Guyana
    "+593": "EC",    # Ecuador
    "+594": "GF",    # French Guiana
    "+595": "PY",    # Paraguay
    "+596": "MQ",    # Martinique (France)
    "+597": "SR",    # Suriname
    "+598": "UY",    # Uruguay
    "+670": "TL",    # Timor-Leste
    "+672": "NF",    # Norfolk Island (also other territories)
    "+673": "BN",    # Brunei
    "+674": "NR",    # Nauru
    "+675": "PG",    # Papua New Guinea
    "+676": "TO",    # Tonga
    "+677": "SB",    # Solomon Islands
    "+678": "VU",    # Vanuatu
    "+679": "FJ",    # Fiji
    "+680": "PW",    # Palau
    "+681": "WF",    # Wallis and Futuna
    "+682": "CK",    # Cook Islands
    "+683": "NU",    # Niue
    "+684": "AS",    # American Samoa (older code, now +1-684)
    "+685": "WS",    # Samoa
    "+686": "KI",    # Kiribati
    "+687": "NC",    # New Caledonia
    "+688": "TV",    # Tuvalu
    "+689": "PF",    # French Polynesia
    "+690": "TK",    # Tokelau
    "+691": "FM",    # Micronesia
    "+692": "MH",    # Marshall Islands
    "+850": "KP",    # North Korea
    "+852": "HK",    # Hong Kong
    "+853": "MO",    # Macau
    "+855": "KH",    # Cambodia
    "+856": "LA",    # Laos
    "+880": "BD",    # Bangladesh
    "+886": "TW",    # Taiwan
    "+960": "MV",    # Maldives
    "+961": "LB",    # Lebanon
    "+962": "JO",    # Jordan
    "+963": "SY",    # Syria
    "+964": "IQ",    # Iraq
    "+965": "KW",    # Kuwait
    "+966": "SA",    # Saudi Arabia
    "+967": "YE",    # Yemen
    "+968": "OM",    # Oman
    "+970": "PS",    # Palestine
    "+971": "AE",    # United Arab Emirates
    "+972": "IL",    # Israel
    "+973": "BH",    # Bahrain
    "+974": "QA",    # Qatar
    "+975": "BT",    # Bhutan
    "+976": "MN",    # Mongolia
    "+977": "NP",    # Nepal
    "+992": "TJ",    # Tajikistan
    "+993": "TM",    # Turkmenistan
    "+994": "AZ",    # Azerbaijan
    "+995": "GE",    # Georgia
    "+996": "KG",    # Kyrgyzstan
    "+998": "UZ",    # Uzbekistan
}


def _infer_country_from_phone_prefix(text: str) -> str:
    """
    CHANGE 2: Search text for international phone prefixes and infer country code.
    Returns country code (e.g., '49' for Germany) or empty string if not found.
    
    Note: Skips +91 (India) because +91 phone numbers on invoices are typically the remitter's
    (always from India) phone numbers, not the beneficiary's. The beneficiary is the foreign party.
    """
    if not text:
        return ""
    text_upper = str(text or "").upper()
    # Look for patterns like +49, +1, etc.
    for prefix, country_code in PHONE_PREFIX_TO_COUNTRY.items():
        # Skip +91 (India) — always the remitter's country in this workflow
        if prefix == "+91":
            continue
        # Build pattern to match the prefix followed by optional space and digit
        escaped_prefix = re.escape(prefix)
        pattern = escaped_prefix + r"\s*\d"
        if re.search(pattern, text_upper):
            return country_code
    return ""

logger = get_logger()


def _is_valid_iso_date(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _split_beneficiary_address(address: str) -> tuple[str, str, str]:
    text = " ".join(str(address or "").split()).strip(" ,")
    if not text:
        return "", "", ""

    # replace bullet-like separators with commas so later splitting works
    # common bullet codepoints include • (U+2022) and variants
    text = re.sub(r"[•\u2022\u2023\u25E6]+", ",", text)

    # Mexico-style fallback: "... <LOCALITY> C.P.:<ZIP> <CITY/DISTRICT>"
    # Example:
    #   CircuitoG.GonzalezCamarena333 SANTAFE ALVAROOBREGON C.P.:01210 DISTRITOFEDERAL
    cp_match = re.search(r"\bC\.?\s*P\.?\s*:?s*\d{4,6}\b.*$", text, flags=re.IGNORECASE)
    if cp_match:
        cp_segment = cp_match.group(0).strip(" ,")
        pre_cp = text[: cp_match.start()].strip(" ,")
        pre_tokens = [tok for tok in pre_cp.split() if tok]
        street = pre_cp
        locality = ""
        if len(pre_tokens) >= 2:
            # Detect tail block of uppercase locality words.
            split_idx = None
            for i in range(1, len(pre_tokens)):
                head = pre_tokens[:i]
                tail = pre_tokens[i:]
                if not tail:
                    continue
                if all(re.fullmatch(r"[A-Z0-9][A-Z0-9.&'/-]*", t) for t in tail):
                    split_idx = i
                    break
            if split_idx is not None:
                street = " ".join(pre_tokens[:split_idx]).strip(" ,")
                locality = " ".join(pre_tokens[split_idx:]).strip(" ,")
        return street or pre_cp, locality, cp_segment

    parts = [p.strip(" ,") for p in re.split(r",|\n", text) if p.strip(" ,")]
    if not parts:
        return text, "", ""

    # Drop trailing country token if present ("Germany", "UNITED STATES OF AMERICA", etc.)
    last = parts[-1].upper()
    if len(last) >= 4 and infer_country_from_beneficiary_name(last):
        parts = parts[:-1]

    if not parts:
        return text, "", ""
    if len(parts) == 1:
        single = parts[0]
        # Handle slash-separated foreign addresses, e.g. "... Nilüfer/Bursa/16140"
        if "/" in single:
            slash_parts = [p.strip(" ,") for p in single.split("/") if p.strip(" ,")]
            if len(slash_parts) >= 3:
                street = "/".join(slash_parts[:-2]).strip(" ,")
                city = slash_parts[-2]
                locality = slash_parts[-1]
                return street or single, locality, city
            if len(slash_parts) == 2:
                street = slash_parts[0]
                city_or_zip = slash_parts[1]
                return street, city_or_zip, city_or_zip
        return single, "", ""
    if len(parts) == 2:
        return parts[0], parts[1], parts[1]

    # default assignment
    street = parts[0]
    locality = ", ".join(parts[1:-1]).strip(" ,")
    city = parts[-1]

    # heuristic: if an earlier segment (before the city) contains a digit, it's
    # very likely the street/flat information; shift accordingly so that the
    # company name (or other prefix) is ignored.
    if len(parts) >= 2:
        for idx, seg in enumerate(parts[:-1]):
            if re.search(r"\d", seg):
                street = seg
                # locality becomes any segments between this one and the city
                mids = parts[idx+1:-1]
                locality = ", ".join(mids).strip(" ,")
                break
    return street, locality, city


def build_invoice_state(invoice_id: str, file_name: str, extracted: Dict[str, str], config: Dict[str, str]) -> Dict[str, object]:
    mode = config.get("mode", MODE_TDS)
    source_short = config.get("currency_short", "")
    resolved_currency = resolve_currency_selection(source_short, load_currency_exact_index())
    state: Dict[str, object] = {
        "meta": {
            "invoice_id": invoice_id,
            "file_name": file_name,
            "mode": MODE_NON_TDS if mode == MODE_NON_TDS else MODE_TDS,
            "exchange_rate": str(config.get("exchange_rate", "")),
            "source_currency_short": source_short,
            "is_gross_up": bool(config.get("is_gross_up", False)),
        },
        "extracted": extracted,
        "resolved": {},
        "form": {},
        "computed": {},
        "xml_fields": {},
    }

    form = state["form"]
    resolved = state["resolved"]
    logger.info(
        "state_build_start invoice_id=%s file=%s mode=%s source_currency=%s extracted_summary=%s",
        invoice_id,
        file_name,
        state["meta"]["mode"],
        source_short,
        {
            "remitter_name": extracted.get("remitter_name", ""),
            "beneficiary_name": extracted.get("beneficiary_name", ""),
            "amount": extracted.get("amount", ""),
            "invoice_date_iso": extracted.get("invoice_date_iso", ""),
            "is_gross_up": state["meta"]["is_gross_up"],
        },
    )
    form["AmtPayForgnRem"] = extracted.get("amount", "")
    form["CurrencySecbCode"] = resolved_currency.get("code", "")
    form["RemitteeZipCode"] = "999999"
    form["RemitteeState"] = "OUTSIDE INDIA"
    form["TaxPayGrossSecb"] = "Y" if state["meta"]["is_gross_up"] else "N"
    # Seed user-selectable IT Act rate (from config override or default)
    it_rate_cfg = config.get("it_act_rate")
    form["ItActRateSelected"] = str(it_rate_cfg) if it_rate_cfg else str(IT_ACT_RATE_DEFAULT)
    # CHANGE: populate DednDateTds from configuration if provided.  The Excel
    # lookup passes ``tds_deduction_date`` through ``config``.  If the
    # configuration contains a non-empty string, use it; otherwise fall
    # back to today's date to preserve legacy behaviour.
    dedn_cfg = str(config.get("tds_deduction_date") or "").strip()
    dedn_valid = _is_valid_iso_date(dedn_cfg)
    form["DednDateTds"] = dedn_cfg if dedn_valid else ""
    state["meta"]["dedn_date_missing"] = not dedn_valid
    state["meta"]["dedn_date_invalid"] = bool(dedn_cfg) and not dedn_valid
    # Proposed date of remittance is always today + offset, as per
    # specifications.  Do not attempt to derive from the Excel sheet.
    form["PropDateRem"] = (date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)).isoformat()

    # Infer country from beneficiary name/country text/address combined.
    beneficiary_country_text = normalize_single_line_text(str(extracted.get("beneficiary_country_text") or ""))
    beneficiary_address = fix_concatenated_words(
        normalize_single_line_text(str(extracted.get("beneficiary_address") or ""))
    )
    remitter_address = fix_concatenated_words(
        normalize_single_line_text(str(extracted.get("remitter_address") or ""))
    )
    beneficiary_name = normalize_single_line_text(str(extracted.get("beneficiary_name") or ""))
    extraction_core_empty = not any(
        str(extracted.get(k) or "").strip()
        for k in ("remitter_name", "beneficiary_name", "invoice_number", "amount", "currency_short")
    )
    country_probe = " ".join([beneficiary_country_text, beneficiary_address, beneficiary_name]).strip()
    inferred_country_code = infer_country_from_beneficiary_name(
        country_probe,
        beneficiary_address,
    )
    logger.info(
        "state_country_inference invoice_id=%s beneficiary=%s country_text=%s inferred_country_code=%s",
        invoice_id,
        beneficiary_name,
        beneficiary_country_text,
        inferred_country_code,
    )
    india_disallowed = False
    if inferred_country_code == "91" and mode == MODE_TDS:
        # Outward remittance guard: beneficiary must be foreign for this workflow.
        # If beneficiary resolves to India, retry from remitter side first.
        remitter_probe = " ".join(
            [
                str(extracted.get("remitter_country_text") or ""),
                remitter_address,
                str(extracted.get("remitter_name") or ""),
            ]
        )
        alternate_country_code = infer_country_from_beneficiary_name(
            remitter_probe,
            remitter_address,
        )
        if alternate_country_code and alternate_country_code != "91":
            logger.warning(
                "state_country_india_safeguard invoice_id=%s old_country=%s alternate_country=%s",
                invoice_id,
                inferred_country_code,
                alternate_country_code,
            )
            inferred_country_code = alternate_country_code
        else:
            # Keep country foreign-only: never finalize India here.
            logger.warning(
                "state_country_india_disallowed invoice_id=%s old_country=%s fallback=9999",
                invoice_id,
                inferred_country_code,
            )
            inferred_country_code = ""
            india_disallowed = True

    if inferred_country_code:
        form["RemitteeCountryCode"] = inferred_country_code
        form["CountryRemMadeSecb"] = inferred_country_code
        # Seed DTAA fields so tax values can auto-calculate before manual country selection.
        country_hint = resolve_country_name(inferred_country_code) or beneficiary_country_text
        if extraction_core_empty:
            logger.warning(
                "state_dtaa_seed_skipped invoice_id=%s reason=core_extraction_empty country_hint=%s",
                invoice_id,
                country_hint,
            )
        else:
            dtaa = resolve_dtaa(country_hint) or None
            if dtaa:
                dtaa_without_article, dtaa_with_article = split_dtaa_article_text(str(dtaa.get("dtaa_applicable") or ""))
                form["RelevantDtaa"] = dtaa_without_article
                form["RelevantArtDtaa"] = dtaa_with_article
                try:
                    resolved["dtaa_rate_percent"] = str(float(str(dtaa.get("percentage"))) * 100).rstrip("0").rstrip(".")
                    form["RateTdsADtaa"] = resolved["dtaa_rate_percent"]
                    form["ArtDtaa"] = dtaa_with_article
                except Exception:
                    pass
            else:
                logger.warning("state_dtaa_not_found invoice_id=%s country_hint=%s", invoice_id, country_hint)
    else:
        # CHANGE 2: Before falling back, try phone prefix inference on the full invoice text.
        # IMPORTANT: Use the full raw invoice text when available so phone numbers like "+49..."
        # that appear outside the extracted address fields can still be detected.
        raw_invoice_text = str(extracted.get("_raw_invoice_text") or "")
        phone_probe = raw_invoice_text or f"{beneficiary_address} {beneficiary_country_text} {beneficiary_name}"
        phone_country = _infer_country_from_phone_prefix(phone_probe)
        if phone_country:
            form["RemitteeCountryCode"] = phone_country
            form["CountryRemMadeSecb"] = phone_country
            form["_country_inference_confidence"] = "low"
            logger.info(
                "state_country_inferred_from_phone invoice_id=%s country_code=%s confidence=low",
                invoice_id,
                phone_country,
            )
        else:
            # If India was explicitly disallowed for outward remittance, keep prior behaviour and
            # fall back to 'OTHERS' (9999). Otherwise, leave the country blank so the user must pick.
            if india_disallowed:
                form["RemitteeCountryCode"] = "9999"
                form["CountryRemMadeSecb"] = "9999"
                logger.warning(
                    "state_country_fallback_others invoice_id=%s beneficiary=%s country_text=%s",
                    invoice_id,
                    beneficiary_name,
                    beneficiary_country_text,
                )
            else:
                form["RemitteeCountryCode"] = ""
                form["CountryRemMadeSecb"] = ""
                logger.warning(
                    "state_country_blank_no_inference invoice_id=%s beneficiary=%s country_text=%s",
                    invoice_id,
                    beneficiary_name,
                    beneficiary_country_text,
                )

    # Seed remittee address fields from OCR/Gemini enrichment when available.
    if extracted.get("beneficiary_street"):
        form.setdefault("RemitteeFlatDoorBuilding", str(extracted.get("beneficiary_street") or ""))
    if extracted.get("beneficiary_zip_text"):
        form.setdefault("RemitteeAreaLocality", str(extracted.get("beneficiary_zip_text") or ""))
    if extracted.get("beneficiary_city"):
        form.setdefault("RemitteeTownCityDistrict", str(extracted.get("beneficiary_city") or ""))

    # Structured parse of single-line beneficiary_address for common patterns like:
    # "Musterstraße 12, 70376 Stuttgart" or "70376 Stuttgart, Musterstraße 12".
    if beneficiary_address:
        try:
            parsed_addr = parse_beneficiary_address(beneficiary_address)
        except Exception:
            parsed_addr = {}
        if isinstance(parsed_addr, dict):
            flat = str(parsed_addr.get("FlatDoorBuilding") or "").strip()
            area = str(parsed_addr.get("AreaLocality") or "").strip()
            city = str(parsed_addr.get("TownCityDistrict") or "").strip()
            zip_code = str(parsed_addr.get("ZipCode") or "").strip()
            # Treat as structured only if we found a real ZIP/city or locality,
            # not just the raw string echoed back.
            orig = beneficiary_address.strip()
            has_structure = (
                (zip_code and zip_code != "999999")
                or (area and area != orig)
                or (city and city != orig)
            )
            if has_structure:
                if flat and not form.get("RemitteeFlatDoorBuilding"):
                    form["RemitteeFlatDoorBuilding"] = flat
                if area and not form.get("RemitteeAreaLocality"):
                    form["RemitteeAreaLocality"] = area
                if city and not form.get("RemitteeTownCityDistrict"):
                    form["RemitteeTownCityDistrict"] = city
                if zip_code and (not form.get("RemitteeZipCode") or str(form.get("RemitteeZipCode") or "") in {"", "999999"}):
                    form["RemitteeZipCode"] = zip_code
    # Fallback split from full beneficiary_address when granular components are missing.
    if beneficiary_address and (
        not form.get("RemitteeFlatDoorBuilding")
        or not form.get("RemitteeTownCityDistrict")
    ):
        flat, area, city = _split_beneficiary_address(beneficiary_address)
        if flat and not form.get("RemitteeFlatDoorBuilding"):
            form["RemitteeFlatDoorBuilding"] = flat
        if area and not form.get("RemitteeAreaLocality"):
            form["RemitteeAreaLocality"] = area
        if city and not form.get("RemitteeTownCityDistrict"):
            form["RemitteeTownCityDistrict"] = city
    # Final fallback for area/locality from zip text if available.
    if not form.get("RemitteeAreaLocality") and extracted.get("beneficiary_zip_text"):
        form["RemitteeAreaLocality"] = str(extracted.get("beneficiary_zip_text") or "")

    if extracted.get("beneficiary_country_text") and str(form.get("RemitteeCountryCode") or "") in {"", "9999"}:
        inferred_by_text = infer_country_from_beneficiary_name(
            str(extracted.get("beneficiary_country_text") or ""),
            beneficiary_address  # Also scan address
        )
        if mode == MODE_TDS and inferred_by_text == "91":
            inferred_by_text = ""
        if inferred_by_text:
            form["RemitteeCountryCode"] = inferred_by_text
            form["CountryRemMadeSecb"] = inferred_by_text
            logger.info(
                "state_remittee_country_from_text invoice_id=%s beneficiary_country_text=%s inferred_code=%s",
                invoice_id,
                extracted.get("beneficiary_country_text", ""),
                inferred_by_text,
            )

    # --- Improved Nature/Purpose selection (CA-style keyword classifier) ---
    try:
        from modules.remittance_classifier import classify_remittance

        raw_text = str(extracted.get("_raw_invoice_text") or "")
        if not raw_text.strip():
            # If OCR failed entirely, build a synthetic probe from high-signal fields
            raw_text = " ".join(
                [
                    str(extracted.get("invoice_number") or ""),
                    str(extracted.get("beneficiary_name") or ""),
                    str(extracted.get("purpose_code") or ""),
                    str(extracted.get("nature_of_remittance") or ""),
                ]
            )
        cls = classify_remittance(raw_text, extracted)
        if cls:
            # Set purpose (authoritative for group/gr_no)
            form["_purpose_group"] = cls.purpose.group_name
            form["_purpose_code"] = cls.purpose.purpose_code
            form["RevPurCategory"] = f"RB-{cls.purpose.gr_no}.1"
            form["RevPurCode"] = f"RB-{cls.purpose.gr_no}.1-{cls.purpose.purpose_code}"

            # Set nature
            form["NatureRemCategory"] = cls.nature.code

            # Keep extracted consistent for UI mapping/logs
            extracted["purpose_code"] = cls.purpose.purpose_code
            extracted["purpose_group"] = cls.purpose.group_name
            extracted["nature_of_remittance"] = cls.nature.label

            resolved["remittance_confidence"] = str(cls.confidence)
            resolved["remittance_needs_review"] = "1" if cls.needs_review else "0"
            resolved["remittance_evidence"] = " | ".join(cls.evidence[:2])

            logger.info(
                "remittance_classified invoice_id=%s purpose=%s nature=%s conf=%.2f review=%s evidence=%s",
                invoice_id, cls.purpose.purpose_code, cls.nature.code, cls.confidence, cls.needs_review, cls.evidence[:2],
            )
    except Exception:
        logger.exception("remittance_classify_failed invoice_id=%s", invoice_id)
        # Fallback to old Gemini-direct approach
        if extracted.get("nature_of_remittance"):
            nature_label = str(extracted.get("nature_of_remittance", "")).strip()
            nature_opts = load_nature_options()
            for opt in nature_opts:
                if str(opt.get("label", "")).strip() == nature_label:
                    form["NatureRemCategory"] = str(opt.get("code", ""))
                    break

        if extracted.get("purpose_code"):
            purpose_code = str(extracted.get("purpose_code", "")).strip().upper()
            purpose_grouped = load_purpose_grouped()
            for group_name, codes in purpose_grouped.items():
                for code_record in codes:
                    if str(code_record.get("purpose_code", "")).strip().upper() == purpose_code:
                        gr_no = str(code_record.get("gr_no", "00") or "00").strip()
                        form["_purpose_group"] = group_name
                        form["_purpose_code"] = purpose_code
                        form["RevPurCategory"] = f"RB-{gr_no}.1"
                        form["RevPurCode"] = f"RB-{gr_no}.1-{purpose_code}"
                        break
                else:
                    continue
                break

    rem = match_remitter(extracted.get("remitter_name", ""))
    if rem:
        resolved["remitter_match"] = "1"
        resolved["pan"] = rem.get("pan", "")
        resolved["bank_name"] = rem.get("bank_name", "")
        resolved["branch"] = rem.get("branch", "")
        resolved["bsr"] = rem.get("bsr", "")
        resolved["bank_code"] = resolve_bank_code(rem.get("bank_name", ""))
        form["RemitterPAN"] = rem.get("pan", "")
        form["NameBankDisplay"] = rem.get("bank_name", "")
        form["NameBankCode"] = resolved["bank_code"]
        form["BranchName"] = rem.get("branch", "")
        form["BsrCode"] = rem.get("bsr", "")
        form["_lock_pan_bank_branch_bsr"] = "1"
        logger.info(
            "state_remitter_match invoice_id=%s remitter_name=%s pan=%s bank=%s",
            invoice_id,
            extracted.get("remitter_name", ""),
            rem.get("pan", ""),
            rem.get("bank_name", ""),
        )
    else:
        form["_lock_pan_bank_branch_bsr"] = "0"
        logger.warning(
            "state_remitter_not_matched invoice_id=%s remitter_name=%s",
            invoice_id,
            extracted.get("remitter_name", ""),
        )
    state = recompute_invoice(state)
    logger.info(
        "state_build_done invoice_id=%s form_snapshot=%s",
        invoice_id,
        {
            "RemitterPAN": form.get("RemitterPAN", ""),
            "CountryRemMadeSecb": form.get("CountryRemMadeSecb", ""),
            "RateTdsADtaa": form.get("RateTdsADtaa", ""),
            "TaxLiablIt": form.get("TaxLiablIt", ""),
            "AmtPayForgnTds": form.get("AmtPayForgnTds", ""),
        },
    )
    return state
