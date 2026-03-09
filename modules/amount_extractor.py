import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Ordered from most explicit/reliable to least.
_AMOUNT_VALUE_RE = r"([0-9]{1,3}(?:(?:[.,\s])[0-9]{3})*[.,][0-9]{2})(?![0-9])"
AMOUNT_PATTERNS = [
    ("gross_value", re.compile(rf"Gross\s+value[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("net_value", re.compile(rf"Net\s+value[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("total_amount", re.compile(rf"Total\s+amount[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("invoice_total", re.compile(rf"Invoice\s+total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("invoice_amount", re.compile(rf"Invoice\s+amount[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("amount_due", re.compile(rf"Amount\s+due[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("grand_total", re.compile(rf"Grand\s+total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("net_amount_final", re.compile(rf"Net\s+amount\s+final[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("total", re.compile(rf"Total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
]

INFORMATIONAL_PAGE_PATTERNS = [
    re.compile(r"for\s+information\s+only", re.IGNORECASE),
    re.compile(r"amounts?\s+in\s+[A-Z]{3}\s+only\s+for\s+information", re.IGNORECASE),
    re.compile(r"exchange\s+rate", re.IGNORECASE),
]

_CURRENCY_TOKEN_RE = re.compile(r"\b[A-Z]{3}\b")
_INVOICE_AMOUNT_LABEL_RE = re.compile(r"Invoice\s+amount", re.IGNORECASE)
_KNOWN_CURRENCY_CODES = {
    "AED",
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "DKK",
    "EUR",
    "GBP",
    "HKD",
    "INR",
    "JPY",
    "NOK",
    "NZD",
    "QAR",
    "SAR",
    "SEK",
    "SGD",
    "USD",
    "ZAR",
}


def _normalize_amount(amount_str: str) -> str:
    s = str(amount_str or "").replace(" ", "")
    if not s:
        return ""
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")
    if "," in s:
        return s.replace(",", ".")
    return s


def _amount_as_float(amount_str: str) -> float:
    try:
        return float(_normalize_amount(amount_str))
    except Exception:
        return -1.0


def _is_informational_page(text: str) -> bool:
    value = str(text or "")
    for pattern in INFORMATIONAL_PAGE_PATTERNS:
        if pattern.search(value):
            return True
    return False


def _extract_currency_near(text_upper: str, start: int, end: int) -> str:
    window_start = max(0, start - 120)
    window_end = min(len(text_upper), end + 120)
    snippet = text_upper[window_start:window_end]
    for token in _CURRENCY_TOKEN_RE.findall(snippet):
        if token in _KNOWN_CURRENCY_CODES:
            return token
    return ""


def extract_amount_candidate_from_pages(
    pages_text: List[str],
    expected_currency: str = "",
) -> Optional[Dict[str, object]]:
    """
    Return best deterministic amount candidate with metadata.
    """
    expected = str(expected_currency or "").strip().upper()
    candidates: List[Dict[str, object]] = []
    total_pages = len(pages_text)

    for page_idx, text in enumerate(pages_text, start=1):
        if not text:
            continue
        informational = _is_informational_page(text)
        text_upper = str(text).upper()
        for pattern_index, (label, pattern) in enumerate(AMOUNT_PATTERNS):
            for match in pattern.finditer(text):
                amount_raw = match.group(1)
                amount = _normalize_amount(amount_raw)
                if not amount:
                    continue
                currency = _extract_currency_near(text_upper, match.start(), match.end())
                currency_match = bool(expected and currency and currency == expected)
                candidates.append(
                    {
                        "amount": amount,
                        "currency": currency,
                        "label": label,
                        "is_informational": informational,
                        "page_number": page_idx,
                        "page_from_end": (total_pages - page_idx + 1),
                        "currency_match": currency_match,
                        "_pattern_index": pattern_index,
                    }
                )

        # Fallback for tables where label and amount are separated by columns/newlines.
        for label_match in _INVOICE_AMOUNT_LABEL_RE.finditer(text):
            window_start = label_match.end()
            window_end = min(len(text), window_start + 1200)
            window = text[window_start:window_end]
            amount_matches = list(re.finditer(_AMOUNT_VALUE_RE, window))
            if not amount_matches:
                continue
            best_amount_match = max(
                amount_matches,
                key=lambda m: _amount_as_float(m.group(1)),
            )
            amount_raw = best_amount_match.group(1)
            amount = _normalize_amount(amount_raw)
            if not amount:
                continue
            amount_start = window_start + best_amount_match.start(1)
            amount_end = window_start + best_amount_match.end(1)
            currency = _extract_currency_near(text_upper, amount_start, amount_end)
            currency_match = bool(expected and currency and currency == expected)
            candidates.append(
                {
                    "amount": amount,
                    "currency": currency,
                    "label": "invoice_amount_window",
                    "is_informational": informational,
                    "page_number": page_idx,
                    "page_from_end": (total_pages - page_idx + 1),
                    "currency_match": currency_match,
                    "_pattern_index": 4,
                }
            )

    if not candidates:
        return None

    def _sort_key(row: Dict[str, object]) -> tuple:
        # Prefer non-informational rows and expected-currency matches.
        return (
            1 if bool(row.get("is_informational")) else 0,
            0 if bool(row.get("currency_match")) else 1,
            int(row.get("_pattern_index") or 99),
            -int(row.get("page_number") or 0),
        )

    best = sorted(candidates, key=_sort_key)[0]
    pattern_idx = int(best.get("_pattern_index") or 0)
    if 0 <= pattern_idx < len(AMOUNT_PATTERNS):
        best["pattern"] = AMOUNT_PATTERNS[pattern_idx][1].pattern
    best.pop("_pattern_index", None)
    best["expected_currency"] = expected

    logger.info(
        "deterministic_amount_candidate amount=%s currency=%s label=%s page=%s informational=%s expected_currency=%s currency_match=%s",
        best.get("amount", ""),
        best.get("currency", ""),
        best.get("label", ""),
        best.get("page_number", 0),
        best.get("is_informational", False),
        expected,
        best.get("currency_match", False),
    )
    return best


def extract_amount_from_pages(pages_text: List[str]) -> Optional[str]:
    """
    Backward-compatible helper returning only amount.
    """
    candidate = extract_amount_candidate_from_pages(pages_text, expected_currency="")
    if not candidate:
        return None
    amount = str(candidate.get("amount") or "").strip()
    return amount or None
