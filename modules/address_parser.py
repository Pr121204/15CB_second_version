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

    # Normalize — strip country prefix from ZIP (e.g., "D-70376" -> "70376").
    text = re.sub(r"\b[A-Z]{1,3}-(\d{4,6})\b", r"\1", text)

    # Pattern 1: "Street Name 12, 70376 Stuttgart"
    m = re.match(r"^(.+?),\s*(\d{4,6})\s+(.+)$", text)
    if m:
        result["FlatDoorBuilding"] = m.group(1).strip()
        result["ZipCode"] = m.group(2).strip()
        result["TownCityDistrict"] = m.group(3).strip()
        result["AreaLocality"] = result["ZipCode"]
        return result

    # Pattern 2: "70376 Stuttgart, Street Name 12"
    m = re.match(r"^(\d{4,6})\s+([A-Za-z][^,]+),\s*(.+)$", text)
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
            zip_city = re.match(r"^(\d{4,6})\s+(.+)$", line)
            if zip_city:
                result["ZipCode"] = zip_city.group(1).strip()
                result["TownCityDistrict"] = zip_city.group(2).strip()
                result["AreaLocality"] = result["ZipCode"]
            else:
                # treat any non-zip line as locality
                result["AreaLocality"] = line
        return result

    # Pattern 4: Only ZIP + City, no street
    m = re.match(r"^(\d{4,6})\s+(.+)$", text)
    if m:
        result["ZipCode"] = m.group(1).strip()
        result["TownCityDistrict"] = m.group(2).strip()
        result["AreaLocality"] = result["ZipCode"]
        return result

    # Fallback: put everything in FlatDoorBuilding
    result["FlatDoorBuilding"] = text
    return result

