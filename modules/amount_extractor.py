import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Ordered from most explicit/reliable to least.
_AMOUNT_VALUE_RE = r"([0-9]{1,3}(?:(?:[.,\s])[0-9]{3})*[.,][0-9]{2})(?![0-9])"

# Patterns that look like dates — must NOT be picked up as amounts.
# Fix 4: extended to cover German (27.02.2026), Japanese (2026年2月27日),
# and slash-separated (27/02/2026) date formats.
_DATE_GUARD_PATTERNS = [
    re.compile(r"^\d{1,2}\.\d{2}$"),                    # 31.01
    re.compile(r"^\d{1,2}\.\d{2}\.\d{4}$"),             # 31.01.2026
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"),          # 2026年2月27日
    re.compile(r"^\d{1,2}/\d{2}/\d{4}$"),               # 27/02/2026
]


def _looks_like_date(s: str) -> bool:
    """Return True if the string matches a known date pattern."""
    v = s.strip()
    return any(p.match(v) for p in _DATE_GUARD_PATTERNS)


AMOUNT_PATTERNS = [
    # English — highest priority
    ("gross_value",      re.compile(rf"Gross\s+value[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("net_value",        re.compile(rf"Net\s+value[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("total_amount",     re.compile(rf"Total\s+amount[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("invoice_total",    re.compile(rf"Invoice\s+total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("invoice_amount",   re.compile(rf"Invoice\s+amount[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("amount_due",       re.compile(rf"Amount\s+due[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("grand_total",      re.compile(rf"Grand\s+total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("net_amount_final", re.compile(rf"Net\s+amount\s+final[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("total",            re.compile(rf"Total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    # German (Fix 1)
    ("de_rechnungsbetrag", re.compile(rf"Rechnungsbetrag[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_rechnungssumme",  re.compile(rf"Rechnungssumme[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_gesamtbetrag",    re.compile(rf"Gesamtbetrag[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_nettobetrag",     re.compile(rf"Nettobetrag[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_bruttobetrag",    re.compile(rf"Bruttobetrag[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_zahlbetrag",      re.compile(rf"Zahlbetrag[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("de_summe",           re.compile(rf"Summe[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    # French (Fix 1)
    ("fr_montant_total",   re.compile(rf"Montant\s+total[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("fr_total_ttc",       re.compile(rf"Total\s+TTC[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("fr_total_ht",        re.compile(rf"Total\s+HT[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("fr_montant_net",     re.compile(rf"Montant\s+net[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    # Japanese (Fix 1) — amount follows label; no separator expected between label and digits
    ("ja_seikyu_kingaku",  re.compile(rf"請求金額[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("ja_gokei_kingaku",   re.compile(rf"合計金額[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("ja_gokei",           re.compile(rf"合計[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("ja_shiharai",        re.compile(rf"支払金額[\s:：]*{_AMOUNT_VALUE_RE}")),
    # Chinese (Fix 1)
    ("zh_jiage_hejixij",   re.compile(rf"价税合计[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("zh_heji",            re.compile(rf"合计[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("zh_yingfu",          re.compile(rf"应付金额[\s:：]*{_AMOUNT_VALUE_RE}")),
    # Korean (Fix 1)
    ("ko_hapgye",          re.compile(rf"합계금액[\s:：]*{_AMOUNT_VALUE_RE}")),
    ("ko_chonggeumak",     re.compile(rf"청구금액[\s:：]*{_AMOUNT_VALUE_RE}")),
    # Vietnamese (Fix 1)
    ("vi_tong_cong",       re.compile(rf"Tổng\s+cộng[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("vi_tong_tien",       re.compile(rf"Tổng\s+tiền[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("vi_so_tien",         re.compile(rf"Số\s+tiền[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    # Czech/Slovak (Fix 1)
    ("cs_celkem",          re.compile(rf"Celkem[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    ("cs_k_uhrade",        re.compile(rf"K\s+úhradě[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
    # Malay (Fix 1)
    ("ms_jumlah",          re.compile(rf"Jumlah[\s:]*{_AMOUNT_VALUE_RE}", re.IGNORECASE)),
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
    """Normalise an amount string to a plain decimal string (period as separator).

    Fix 2: handles German format (12.347,32 → 12347.32), JPY-style
    comma-as-thousands (1,630,798 → 1630798), and German dot-as-thousands
    with no decimal part (12.347 → 12347).
    """
    s = str(amount_str or "").replace(" ", "")
    if not s:
        return ""
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # German format: 12.347,32 — dot=thousands, comma=decimal
            return s.replace(".", "").replace(",", ".")
        # English format: 12,347.32 — comma=thousands, dot=decimal
        return s.replace(",", "")
    if "," in s:
        # Ambiguous — decide by digit count after the last comma.
        after_last_comma = s.rsplit(",", 1)[-1]
        if len(after_last_comma) == 2:
            # Treat comma as decimal separator: 1,63 → 1.63
            return s.replace(",", ".")
        # Treat comma as thousands separator: 1,630,798 → 1630798
        return s.replace(",", "")
    if "." in s:
        # Ambiguous — decide by digit count after the last dot.
        after_last_dot = s.rsplit(".", 1)[-1]
        if len(after_last_dot) == 3:
            # German thousands: 12.347 → 12347 (no decimal part)
            return s.replace(".", "")
        # English decimal: 12.34 — keep as is
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
                # Fix 4: skip values that look like dates (27.02, 31.01.2026, etc.)
                if _looks_like_date(amount_raw):
                    continue
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
            if _looks_like_date(amount_raw):
                continue
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
