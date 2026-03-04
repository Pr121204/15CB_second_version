from __future__ import annotations

import re
from typing import Dict


def parse_beneficiary_address(address_str: str) -> Dict[str, str]:
    """
    Split a single-line beneficiary address string into Form 15CB-style sub-fields.

    Returns a dict with keys:
      - FlatDoorBuilding
      - AreaLocality
      - TownCityDistrict
      - ZipCode

    This is especially tuned for common European formats like:
      "Musterstraße 12, 70376 Stuttgart"
      "Musterstraße 12, D-70376 Stuttgart"
      "70376 Stuttgart, Musterstraße 12"
    """
    result: Dict[str, str] = {
        "FlatDoorBuilding": "",
        "AreaLocality": "",
        "TownCityDistrict": "",
        "ZipCode": "999999",  # sensible default
    }

    if not address_str or str(address_str).strip().lower() in {"n/a", "na", ""}:
        return result

    text = str(address_str).strip()

    # ZIP Regex: Supports standard numeric (4-6 digits) AND Alphanumeric (UK/Canada/EU)
    # This covers "70376", "DE-70376", "EC1M 5UX", "SW1A 1AA", etc.
    ZIP_RE = r"(\b\d{4,6}\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b)"

    # Normalize — strip country prefix from numeric ZIP (e.g., "D-70376" -> "70376").
    text = re.sub(r"\b[A-Z]{1,3}-(\d{4,6})\b", r"\1", text)

    # Pattern 1: "Street Name 12, ZIP City" (with comma)
    m = re.match(rf"^(.+?),\s*{ZIP_RE}\s+(.+)$", text, flags=re.IGNORECASE)
    if m:
        result["FlatDoorBuilding"] = m.group(1).strip()
        result["ZipCode"] = m.group(2).strip()
        result["TownCityDistrict"] = m.group(3).strip()
        result["AreaLocality"] = result["ZipCode"]
        return result

    # Pattern 2: "ZIP City, Street Name 12" (with comma)
    m = re.match(rf"^{ZIP_RE}\s+([A-Za-z][^,]+),\s*(.+)$", text, flags=re.IGNORECASE)
    if m:
        result["ZipCode"] = m.group(1).strip()
        result["TownCityDistrict"] = m.group(2).strip()
        result["FlatDoorBuilding"] = m.group(3).strip()
        result["AreaLocality"] = result["ZipCode"]
        return result

    # Pattern 3: Multi-line (newline separated)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 2:
        result["FlatDoorBuilding"] = lines[0]
        for line in lines[1:]:
            zip_city = re.match(rf"^{ZIP_RE}\s+(.+)$", line, flags=re.IGNORECASE)
            if zip_city:
                result["ZipCode"] = zip_city.group(1).strip()
                result["TownCityDistrict"] = zip_city.group(2).strip()
                result["AreaLocality"] = result["ZipCode"]
            else:
                # treat any non-zip line as locality
                result["AreaLocality"] = line
        return result

    # Pattern 5: Single line NO COMMAS - Greedy ZIP search
    # Find the ZIP code anywhere in the string.
    zip_match = re.search(ZIP_RE, text, flags=re.IGNORECASE)
    if zip_match:
        zip_str = zip_match.group(0)
        pre_zip = text[:zip_match.start()].strip(" ,")
        post_zip = text[zip_match.end():].strip(" ,")
        
        result["ZipCode"] = zip_str
        result["AreaLocality"] = zip_str
        
        # Heuristic for City splitting
        # If post_zip is short and not a country name, it's likely the city.
        # Otherwise, look at the tail of pre_zip.
        if post_zip and len(post_zip.split()) <= 2 and not re.search(r"\b(United Kingdom|UK|USA|United States|Germany|France|India)\b", post_zip, re.I):
            result["TownCityDistrict"] = post_zip
            result["FlatDoorBuilding"] = pre_zip
        else:
            pre_parts = pre_zip.split()
            if len(pre_parts) >= 2:
                result["TownCityDistrict"] = pre_parts[-1]
                result["FlatDoorBuilding"] = " ".join(pre_parts[:-1])
            else:
                result["FlatDoorBuilding"] = pre_zip
                result["TownCityDistrict"] = post_zip
        return result

    # Pattern 4: Only ZIP + City, no street
    m = re.match(rf"^{ZIP_RE}\s+(.+)$", text, flags=re.IGNORECASE)
    if m:
        result["ZipCode"] = m.group(1).strip()
        result["TownCityDistrict"] = m.group(2).strip()
        result["AreaLocality"] = result["ZipCode"]
        return result

    # Fallback: put everything in FlatDoorBuilding
    result["FlatDoorBuilding"] = text
    return result

