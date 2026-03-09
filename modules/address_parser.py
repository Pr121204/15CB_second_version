from __future__ import annotations

import re
from typing import Dict, Optional

# Country names to strip from the end of address strings.
# Sorted longest-first so "UNITED KINGDOM" is tried before "UK".
_COUNTRY_SUFFIXES = sorted(
    [
        "CZECHIA", "CZECH REPUBLIC", "GERMANY", "JAPAN", "SINGAPORE",
        "INDIA", "THAILAND", "UNITED KINGDOM", "UK", "USA",
        "UNITED STATES", "FRANCE", "NETHERLANDS", "AUSTRIA",
        "SWITZERLAND", "SWEDEN", "FINLAND", "DENMARK", "POLAND",
        "HUNGARY", "ROMANIA", "ITALY", "SPAIN", "PORTUGAL",
        "AUSTRALIA", "BRAZIL", "SOUTH AFRICA", "RUSSIA", "CHINA",
        "KOREA", "VIETNAM", "INDONESIA", "MALAYSIA",
        "BELGIUM", "LUXEMBOURG", "IRELAND", "GREECE", "CYPRUS",
        "MALTA", "ESTONIA", "LATVIA", "LITHUANIA", "CROATIA",
        "SLOVENIA", "SERBIA", "UKRAINE", "BULGARIA", "SLOVAKIA",
        "UAE", "BAHRAIN", "KUWAIT", "OMAN", "QATAR", "SAUDI ARABIA",
        "SOUTH KOREA", "MEXICO",
    ],
    key=len,
    reverse=True,
)

# Fix 1: ISO 2-letter country codes stripped ONLY when they appear as the
# final token.  We check last-token only to avoid removing "MX" or "DE"
# that appear legitimately mid-address (e.g. "Robert-Bosch-Str MX Factory").
_ISO_CODES_TO_STRIP = {
    "MX", "DE", "JP", "SG", "CZ", "SK", "HU", "PL", "RO",
    "AT", "CH", "SE", "FI", "DK", "FR", "NL", "BE", "IT",
    "ES", "PT", "AU", "BR", "ZA", "RU", "CN", "KR", "VN",
    "ID", "MY", "TH", "US", "GB", "UK", "IN", "IE", "LV",
    "EG", "TR", "LU", "IL", "NO",
}

# Fix 2: Noise labels that can appear inside address strings from Gemini.
_NOISE_PATTERNS = [
    re.compile(r"POSTAL\s+CODE\s*:?\s*", re.IGNORECASE),
    re.compile(r"POST\s+CODE\s*:?\s*", re.IGNORECASE),
    re.compile(r"ZIP\s+CODE\s*:?\s*", re.IGNORECASE),
    re.compile(r"PIN\s+CODE\s*:?\s*", re.IGNORECASE),
    re.compile(r"P\.?O\.?\s*BOX\s+\w+", re.IGNORECASE),
]

# Fix 3: Known multi-word city names; sorted longest-first so more specific
# matches win ("HO CHI MINH CITY" before "HO CHI MINH").
_MULTI_WORD_CITIES = sorted(
    [
        "MEXICO CITY", "NEW YORK", "NEW DELHI", "LOS ANGELES",
        "SAN FRANCISCO", "HONG KONG", "KUALA LUMPUR", "HO CHI MINH",
        "HO CHI MINH CITY", "GEORGE TOWN", "CAPE TOWN", "SAO PAULO",
        "RIO DE JANEIRO", "BUENOS AIRES", "SAINT PETERSBURG",
        "WEST JAKARTA", "CENTRAL JAKARTA",
    ],
    key=len,
    reverse=True,
)

# Alphanumeric (UK-style) postcode: EC1M 5UX, SW1A 1AA
_ZIP_UK_RE = re.compile(r"\b[A-Z]{1,2}\d[0-9A-Z]?\s*\d[A-Z]{2}\b")

# Numeric postal codes: Czech "NNN NN" and standard 4-6 digit codes.
# The Czech pattern comes first so it is preferred over matching just the
# first three digits of a code like "370 04".
_ZIP_NUM_RE = re.compile(r"\b\d{3}\s\d{2}\b|\b\d{4,6}\b")

# Street-phrase keywords — when AreaLocality is blank these help derive a
# locality from the FlatDoorBuilding string.  Only match when the keyword is
# preceded by a space (or start of string) so that it is not part of a
# hyphenated compound like "Robert-Bosch-Platz".
_STREET_PHRASE_RE = re.compile(
    r"(?:^|\s)(Avenue|Rue|Boulevard|Blvd|Street|St\b|Road|Rd\b|Lane|"
    r"Park|Drive|Dr\b|Place|Pl\b|Way|Court|Ct\b|Gardens?|Square|Sq\b|"
    r"Terrace|Crescent|Close|Walk|Row|Hill|Rise|View|Allee|Zone|"
    r"Industrial\s+(?:Area|Park|Estate)|"
    r"Nagar|Marg|Vihar|Chowk|Bazar|Bazaar)",
    re.IGNORECASE,
)


def _strip_zips(s: str) -> str:
    """Remove ZIP-like tokens from a string and collapse extra whitespace."""
    s = _ZIP_UK_RE.sub("", s)
    s = _ZIP_NUM_RE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip().rstrip(",").strip()


def _repair_address(result: Dict[str, str]) -> Dict[str, str]:
    """
    Three-pass deterministic repair to ensure all three address sub-fields
    are non-blank when source material is available.

    Repair 1 — blank city:
        If TownCityDistrict is empty but FlatDoorBuilding has multiple tokens,
        peel the last token off FlatDoorBuilding and use it as the city.

    Repair 2 — blank locality (primary):
        If AreaLocality is empty and FlatDoorBuilding contains a recognisable
        street-phrase keyword (Avenue, Rue, Street, Road …), extract the text
        from that keyword to the end of FlatDoorBuilding as the locality.
        This is always grounded in actual source text.

    Repair 3 — blank locality (fallback):
        If AreaLocality is still empty and TownCityDistrict is non-empty,
        reuse TownCityDistrict as AreaLocality.  Duplication is acceptable
        when no distinct locality exists; mandatory XML fields must be filled.
    """
    flat = result["FlatDoorBuilding"]
    city = result["TownCityDistrict"]

    # Repair 1: peel city from FlatDoor when city is blank
    if not city and flat:
        tokens = flat.split()
        if len(tokens) >= 2:
            result["FlatDoorBuilding"] = " ".join(tokens[:-1])
            result["TownCityDistrict"] = tokens[-1]
            flat = result["FlatDoorBuilding"]
            city = result["TownCityDistrict"]

    # Repair 2: derive locality from street keyword in FlatDoor.
    # Only use the match when text follows the keyword (e.g. "Avenue Michelet",
    # not a bare "Street" with nothing after it).
    if not result["AreaLocality"] and flat:
        m = _STREET_PHRASE_RE.search(flat)
        if m:
            candidate = flat[m.start(1):].strip()
            # Accept only if the candidate contains more than the keyword alone
            if len(candidate.split()) >= 2:
                result["AreaLocality"] = candidate

    # Repair 3: last resort — reuse city as locality
    if not result["AreaLocality"] and result["TownCityDistrict"]:
        result["AreaLocality"] = result["TownCityDistrict"]

    return result


def _split_long_no_zip(tokens: list) -> tuple[str, str]:
    """Fix 4: split a token list (street + area, no city, no ZIP) by finding
    the last digit-containing token.  Everything up to and including that
    token → FlatDoorBuilding; everything after → AreaLocality.

    Only applied when the remaining string is > 50 characters.
    """
    last_digit_idx = max(
        (i for i, t in enumerate(tokens) if re.search(r"\d", t)),
        default=0,
    )
    flat = " ".join(tokens[: last_digit_idx + 1])
    area = " ".join(tokens[last_digit_idx + 1 :]).strip()
    return flat, area


def parse_beneficiary_address(address_str: str) -> Dict[str, str]:
    """
    Split a single-line beneficiary address into Form 15CB sub-fields.

    ZipCode is ALWAYS returned as "999999" — never parsed from the input.

    Returns a dict with keys:
      FlatDoorBuilding, AreaLocality, TownCityDistrict, ZipCode
    """
    result: Dict[str, str] = {
        "FlatDoorBuilding": "",
        "AreaLocality": "",
        "TownCityDistrict": "",
        "ZipCode": "999999",
    }

    if not address_str or str(address_str).strip().lower() in {"n/a", "na", ""}:
        return result

    work = str(address_str).strip()

    # --- Fix 1: Strip trailing ISO 2-letter country code (last token only) ---
    tokens = work.split()
    if tokens and tokens[-1].upper() in _ISO_CODES_TO_STRIP:
        tokens = tokens[:-1]
        work = " ".join(tokens)

    # --- Step 1: Strip trailing full country name ---
    upper = work.upper()
    for suffix in _COUNTRY_SUFFIXES:
        if upper.endswith(suffix):
            work = work[: -len(suffix)].strip().rstrip(",").strip()
            break

    if not work:
        return result

    # --- Fix 2: Remove noise labels ("POSTAL CODE:", "POST CODE:", etc.) ---
    for noise_re in _NOISE_PATTERNS:
        work = noise_re.sub("", work)
    work = re.sub(r"\s{2,}", " ", work).strip()

    if not work:
        return result

    # --- Step 2: Primary split into street / area / city ---

    if "," in work:
        # Keep both raw parts (for ZIP-position re-parsing) and ZIP-stripped
        # parts (for display / empty filtering).
        raw_parts = [p.strip() for p in work.split(",") if p.strip()]
        parts = [_strip_zips(p) for p in raw_parts]
        # Drop parts that became empty after stripping (pure ZIP-code parts).
        raw_parts = [r for r, s in zip(raw_parts, parts) if s]
        parts = [p for p in parts if p]

        # If the first part contains no digits but another part does, the first
        # part is likely a company / entity name that Gemini bundled into the
        # address field.  Skip it so it does not occupy FlatDoorBuilding.
        if (
            len(parts) >= 2
            and not re.search(r"\d", parts[0])
            and any(re.search(r"\d", p) for p in parts[1:])
        ):
            parts = parts[1:]
            raw_parts = raw_parts[1:]

        if not parts:
            pass
        elif len(parts) == 1:
            # Single remaining part — apply ZIP-position split on the raw
            # (un-stripped) version so street vs city split works correctly.
            raw_part = raw_parts[0]
            all_num = list(_ZIP_NUM_RE.finditer(raw_part))
            if all_num:
                m = all_num[-1]
                street = raw_part[: m.start()].strip().rstrip(",").strip()
                city = raw_part[m.end() :].strip().lstrip(",").strip()
                if street and city:
                    result["FlatDoorBuilding"] = street
                    result["TownCityDistrict"] = city
                elif street:
                    result["FlatDoorBuilding"] = street
                else:
                    result["FlatDoorBuilding"] = parts[0]
            else:
                tokens = parts[0].split()
                if len(tokens) >= 2:
                    result["FlatDoorBuilding"] = " ".join(tokens[:-1])
                    result["TownCityDistrict"] = tokens[-1]
                else:
                    result["FlatDoorBuilding"] = parts[0]
        elif len(parts) == 2:
            result["FlatDoorBuilding"] = parts[0]
            result["TownCityDistrict"] = parts[1]
        else:
            result["FlatDoorBuilding"] = parts[0]
            result["TownCityDistrict"] = parts[-1]
            result["AreaLocality"] = ", ".join(parts[1:-1])

    else:
        # No comma — use ZIP position to split street from city.

        # Prefer UK alphanumeric postcode first.
        m_uk = _ZIP_UK_RE.search(work)
        if m_uk:
            # UK convention: postcode follows the city.
            # City = last token before the postcode; street = everything before.
            pre_zip = work[: m_uk.start()].strip()
            tokens = pre_zip.split()
            if len(tokens) >= 2:
                result["TownCityDistrict"] = tokens[-1]
                result["FlatDoorBuilding"] = " ".join(tokens[:-1])
            elif tokens:
                result["FlatDoorBuilding"] = tokens[0]
        else:
            # Use the LAST numeric ZIP match as the divider.
            # Taking the last match avoids treating house numbers (e.g. "2678")
            # as the postal code when the real ZIP (e.g. "370 04") follows later.
            all_num = list(_ZIP_NUM_RE.finditer(work))
            if all_num:
                m = all_num[-1]
                street = work[: m.start()].strip().rstrip(",").strip()
                city = work[m.end() :].strip().lstrip(",").strip()
                if city:
                    result["FlatDoorBuilding"] = street
                    result["TownCityDistrict"] = city
                else:
                    # ZIP at end — last token of street becomes city
                    tokens = street.split()
                    if len(tokens) >= 2:
                        result["TownCityDistrict"] = tokens[-1]
                        result["FlatDoorBuilding"] = " ".join(tokens[:-1])
                    else:
                        result["FlatDoorBuilding"] = street
            else:
                # No ZIP at all.
                # Fix 3: check for known multi-word city suffix BEFORE
                # falling back to the single-last-token rule.
                city_found: Optional[str] = None
                for city_name in _MULTI_WORD_CITIES:
                    if work.upper().endswith(city_name):
                        # Preserve the original casing from the address string.
                        city_found = work[-len(city_name):]
                        work = work[: -len(city_name)].strip().rstrip(",").strip()
                        break

                if city_found:
                    result["TownCityDistrict"] = city_found
                    remaining_tokens = work.split()
                    if remaining_tokens:
                        remaining = " ".join(remaining_tokens)
                        if len(remaining) > 50 and re.search(r"\d", remaining):
                            # Fix 4: long address — split at last digit token
                            flat, area = _split_long_no_zip(remaining_tokens)
                            result["FlatDoorBuilding"] = flat
                            if area:
                                result["AreaLocality"] = area
                        else:
                            result["FlatDoorBuilding"] = remaining
                else:
                    # Standard fallback: last token is the city
                    tokens = work.split()
                    if len(tokens) >= 2:
                        result["FlatDoorBuilding"] = " ".join(tokens[:-1])
                        result["TownCityDistrict"] = tokens[-1]
                    else:
                        result["FlatDoorBuilding"] = work

    # --- Step 3: Deterministic repair to fill any blank sub-fields ---
    result = _repair_address(result)

    return result
