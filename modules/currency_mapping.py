from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Tuple


MASTER_CURRENCY_PATH = Path(__file__).resolve().parent.parent / "data" / "master" / "currency_codes.json"
MASTER_CURRENCY_SHORT_PATH = Path(__file__).resolve().parent.parent / "data" / "master" / "currency_short_codes.json"

# Confirmed mappings from production data.
CONFIRMED_SHORT_CODE_TO_CODE: Dict[str, str] = {
    "EUR": "50",
    "USD": "167",
    "GBP": "133",
    "JPY": "78",
}

# Short-code targets must match exact currency names from master JSON.
SHORT_CODE_TARGET_NAME: Dict[str, str] = {
    "EUR": "EURO",
    "USD": "US DOLLAR",
    "GBP": "POUND STERLING",
    "JPY": "JAPANESE YEN",
    "AUD": "AUSTRALIAN DOLLAR",
    "SGD": "SINGAPORE DOLLAR",
    "CHF": "SWISS FRANC",
    "CAD": "CANADIAN DOLLAR",
    "CNY": "YUAN RENMINBI",
    "SEK": "SWEDISH KRONA",
    "NOK": "NORWEGIAN KRONES",
    "DKK": "DANISH KRONE",
    "NZD": "NEW ZEALAND DOLLAR",
}


def load_currency_exact_index(path: Path | None = None) -> Dict[str, str]:
    source = path or MASTER_CURRENCY_PATH
    try:
        with open(source, "r", encoding="utf8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    index: Dict[str, str] = {}
    if not isinstance(raw, list):
        return index
    for row in raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("currency") or "").strip().upper()
        code = str(row.get("code") or "").strip()
        if name and code:
            index[name] = code
    return index


def load_currency_rows(path: Path | None = None) -> List[Dict[str, str]]:
    source = path or MASTER_CURRENCY_PATH
    try:
        with open(source, "r", encoding="utf8") as f:
            raw = json.load(f)
    except Exception:
        return []

    rows: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return rows
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        currency = str(row.get("currency") or "").strip().upper()
        if code and currency:
            rows.append({"code": code, "currency": currency})
    return rows


def load_currency_short_index(path: Path | None = None) -> Dict[str, str]:
    source = path or MASTER_CURRENCY_SHORT_PATH
    try:
        with open(source, "r", encoding="utf8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    short_index: Dict[str, str] = {}
    if not isinstance(raw, list):
        return short_index
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        short = str(row.get("short") or "").strip().upper()
        if code and short:
            short_index[code] = short
    return short_index


def validate_short_code_targets(currency_index: Mapping[str, str]) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    resolved: Dict[str, Dict[str, str]] = {}
    missing: Dict[str, str] = {}
    for short_code, target_name in SHORT_CODE_TARGET_NAME.items():
        code = currency_index.get(target_name)
        if code:
            resolved[short_code] = {"name": target_name, "code": code}
        else:
            missing[short_code] = target_name
    return resolved, missing


def resolve_short_code_currency(
    short_code: str,
    currency_index: Mapping[str, str],
) -> Dict[str, str]:
    short = (short_code or "").strip().upper()
    target_name = SHORT_CODE_TARGET_NAME.get(short)
    if not target_name:
        return {}
    code = currency_index.get(target_name)
    if not code:
        return {}
    return {"short_code": short, "name": target_name, "code": code}


def resolve_currency_selection(
    selection: str,
    currency_index: Mapping[str, str],
) -> Dict[str, str]:
    """
    Resolve a currency selection from Step-1 upload config.
    Supports:
    - short code (USD, EUR, ...)
    - exact master currency name (US DOLLAR, EURO, ...)
    - direct numeric code (167, 50, ...)
    """
    raw = (selection or "").strip()
    normalized = raw.upper()
    if not normalized:
        return {}

    by_short = resolve_short_code_currency(normalized, currency_index)
    if by_short:
        return by_short

    by_name_code = currency_index.get(normalized)
    if by_name_code and by_name_code != "-1":
        return {"short_code": normalized, "name": normalized, "code": by_name_code}

    for name, code in currency_index.items():
        if code == raw and code != "-1":
            return {"short_code": name, "name": name, "code": code}
    return {}


def get_upload_currency_options(currency_index: Mapping[str, str] | None = None) -> List[str]:
    """
    Build Step-1 upload currency dropdown options from master data.
    Excludes placeholder entries with invalid code (-1).
    """
    index = dict(currency_index or load_currency_exact_index())
    names = [name for name, code in index.items() if code and code != "-1"]
    return sorted(names)


def get_upload_currency_select_options() -> List[Dict[str, str]]:
    """
    Build Step-1 upload currency dropdown options using short codes for display.
    Returns records with:
    - value: master numeric currency code
    - label: short code, disambiguated when duplicates exist
    """
    rows = load_currency_rows()
    short_index = load_currency_short_index()
    option_rows: List[Dict[str, str]] = []
    short_counts: Dict[str, int] = {}

    for row in rows:
        code = row.get("code", "")
        name = row.get("currency", "")
        if not code or code == "-1":
            continue
        short = short_index.get(code) or name
        short = short.strip().upper()
        option_rows.append({"value": code, "short": short, "currency": name})
        short_counts[short] = short_counts.get(short, 0) + 1

    options: List[Dict[str, str]] = []
    for row in option_rows:
        short = row["short"]
        name = row["currency"]
        label = short if short_counts.get(short, 0) == 1 else f"{short} ({name})"
        options.append({"value": row["value"], "label": label})

    return sorted(options, key=lambda r: (r["label"], r["value"]))


def preselect_currency_code(
    current_code: str,
    short_code: str,
    currency_index: Mapping[str, str],
) -> Tuple[str, bool]:
    code = (current_code or "").strip()
    if code:
        return code, code == "-1"
    resolved = resolve_currency_selection(short_code, currency_index)
    if resolved:
        return resolved["code"], False
    if (short_code or "").strip():
        return "", True
    return "", False


def is_currency_code_valid_for_xml(code: str) -> bool:
    cleaned = (code or "").strip()
    return bool(cleaned and cleaned != "-1")
