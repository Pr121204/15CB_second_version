from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MASTER_PATH = ROOT / "data" / "master" / "master_data.json"
NATURE_PATH = ROOT / "lookups" / "nature_codes.json"
NATURE_FULL_PATH = ROOT / "lookups" / "nature_codes_full.json"


def main() -> None:
    with open(MASTER_PATH, "r", encoding="utf8") as f:
        master = json.load(f)

    compact = {}
    full = {}
    for row in master.get("nature_map", []):
        if not isinstance(row, dict):
            continue
        nature = str(row.get("agreement_nature") or "").strip()
        service = str(row.get("service_category") or "").strip()
        purpose = str(row.get("purpose_code") or "").strip()
        if not nature:
            continue
        key = nature.lower()
        if purpose:
            compact[key] = purpose
        full[key] = {
            "agreement_nature": nature,
            "service_category": service,
            "purpose_code": purpose,
        }

    NATURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NATURE_PATH, "w", encoding="utf8") as f:
        json.dump(compact, f, indent=2)
    with open(NATURE_FULL_PATH, "w", encoding="utf8") as f:
        json.dump(full, f, indent=2)

    print(f"Wrote {len(compact)} nature code mappings to {NATURE_PATH}")
    print(f"Wrote {len(full)} full nature mappings to {NATURE_FULL_PATH}")


if __name__ == "__main__":
    main()
