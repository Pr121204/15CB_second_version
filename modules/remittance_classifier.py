# modules/remittance_classifier.py
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TypedDict, cast

from modules.logger import get_logger
from modules.master_lookups import load_nature_options, load_purpose_grouped

logger = get_logger()


class HighSignalRule(TypedDict):
    purpose_code: str
    nature_code: str
    weight: float
    patterns: List[str]

# -----------------------------
# Text normalization
# -----------------------------

STOPWORDS = {
    "the", "and", "or", "to", "of", "for", "in", "on", "a", "an", "by", "with",
    "fee", "fees", "charge", "charges", "amount", "total", "invoice", "inv",
    "services", "service", "payment", "paid", "bill", "billing",
    # very common corp suffixes (reduce noise)
    "ltd", "limited", "gmbh", "inc", "llc", "corp", "co",
}

def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\u00A0\t\r]+", " ", t)
    # keep word chars, space, -, /, &, .
    t = re.sub(r"[^\w\s\-/&.]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _tokens(text: str) -> List[str]:
    t = _norm(text)
    toks: List[str] = []
    for w in re.split(r"[ \-/&.]+", t):
        w = w.strip()
        if not w or w in STOPWORDS or len(w) <= 2:
            continue
        toks.append(w)
    return toks

# -----------------------------
# Data models
# -----------------------------

@dataclass(frozen=True)
class PurposeRecord:
    gr_no: str
    group_name: str
    purpose_code: str
    description: str

@dataclass(frozen=True)
class NatureRecord:
    code: str
    label: str

@dataclass(frozen=True)
class Classification:
    purpose: PurposeRecord
    nature: NatureRecord
    confidence: float
    needs_review: bool
    evidence: List[str]

# -----------------------------
# Load + indexes
# -----------------------------

@functools.lru_cache(maxsize=1)
def _purpose_records() -> Dict[str, PurposeRecord]:
    grouped = load_purpose_grouped()
    out: Dict[str, PurposeRecord] = {}
    for group_name, rows in grouped.items():
        for r in rows:
            code = str(r.get("purpose_code") or "").strip().upper()
            if not code:
                continue
            out[code] = PurposeRecord(
                gr_no=str(r.get("gr_no") or "").strip(),
                group_name=str(group_name or r.get("group_name") or "").strip(),
                purpose_code=code,
                description=str(r.get("description") or "").strip(),
            )
    return out

@functools.lru_cache(maxsize=1)
def _nature_records() -> Dict[str, NatureRecord]:
    out: Dict[str, NatureRecord] = {}
    for r in load_nature_options():
        code = str(r.get("code") or "").strip()
        label = str(r.get("label") or "").strip()
        if not code or not label or code == "-1":  # ignore Select
            continue
        out[code] = NatureRecord(code=code, label=label)
    return out

@functools.lru_cache(maxsize=1)
def _idf_for_purpose_desc() -> Dict[str, float]:
    # IDF across purpose descriptions (N~137) for robust fallback
    recs = list(_purpose_records().values())
    N = len(recs) or 1
    df: Dict[str, int] = {}
    for r in recs:
        seen = set(_tokens(r.description))
        for tok in seen:
            df[tok] = df.get(tok, 0) + 1
    return {tok: (math.log((N + 1) / (c + 1)) + 1.0) for tok, c in df.items()}

# -----------------------------
# Focus / Boilerplate stripping
# -----------------------------

FOCUS_START_PATTERNS = [
    # common line-item/table headers
    r"\bitem\b.*\bquantity\b.*\bunit\b",
    r"\bpos\.\b.*\bqty\b",
    r"\bdescription\b.*\bqty\b",
]

FOCUS_STOP_PATTERNS = [
    # typical footer / terms / banking blocks
    r"\bpayment\s+term\b",
    r"\bterms?\s+and\s+conditions\b",
    r"\bplace\s+of\s+jurisdiction\b",
    r"\bretention\s+of\s+ownership\b",
    r"\biban\b|\bswift\b|\bbic\b|\bifsc\b",
    r"\bhrb\b|\bsteu(er)?-?nr\b|\bust-?id\b",
    r"\bgf/ceo\b|\bmanaging\s+director\b",
]

NEGATIVE_BOILERPLATE_PATTERNS = [
    # these caused false positives (legal, etc.)
    r"\blegal obligations\b",
    r"\blegal provision\b",
    r"\bgoverning law\b",
    r"\bjursidiction\b|\bjurisdiction\b",
]

def _focus_invoice_text(raw: str) -> str:
    """
    Returns a reduced text focusing on line-items / description.
    If no table header is detected, removes obvious boilerplate lines.
    """
    if not raw:
        return ""

    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    if not lines:
        return raw

    start_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if any(re.search(p, ln, flags=re.IGNORECASE) for p in FOCUS_START_PATTERNS):
            start_idx = i
            break

    # If table header not found, keep most lines but drop obvious footers/bank/boilerplate.
    if start_idx is None:
        kept: List[str] = []
        for ln in lines:
            if any(re.search(p, ln, flags=re.IGNORECASE) for p in FOCUS_STOP_PATTERNS):
                continue
            if any(re.search(p, ln, flags=re.IGNORECASE) for p in NEGATIVE_BOILERPLATE_PATTERNS):
                continue
            kept.append(ln)
        return "\n".join(kept) if kept else raw

    # include a small window before the table header for “project scope” lines
    safe_start_idx = max(0, start_idx - 10)

    kept = []
    for ln in lines[safe_start_idx:]:
        if any(re.search(p, ln, flags=re.IGNORECASE) for p in FOCUS_STOP_PATTERNS):
            break
        if any(re.search(p, ln, flags=re.IGNORECASE) for p in NEGATIVE_BOILERPLATE_PATTERNS):
            continue
        kept.append(ln)

    return "\n".join(kept) if kept else raw

# -----------------------------
# CA-office priors (Nature -> Purpose)
# -----------------------------

# Soft boosts only (do not hard override).
# Matches what CA staff commonly do for these “major natures”.
NATURE_PURPOSE_PRIOR: Dict[str, Dict[str, float]] = {
    "16.21": {"S1023": 35.0, "S0802": 12.0},  # technical services → other technical by default
    "16.54": {"S0803": 28.0},
    "16.52": {"S0902": 40.0},
    "16.42": {"S1008": 50.0},
    "16.18": {"S1014": 45.0},
    "16.60": {"S1107": 40.0},
}
PRIOR_MIN_NATURE_SCORE = 45.0

# -----------------------------
# High-signal CA-style rules
# -----------------------------

# NOTE:
# - Keep patterns specific. Avoid single-word broad matches that appear in boilerplate.
# - Prefer “dominant intent” triggers.
HIGH_SIGNAL_RULES: List[HighSignalRule] = [
    # Advertising / marketing
    {"purpose_code": "S1007", "nature_code": "16.1", "weight": 60,
     "patterns": [r"\bgoogle\s+ads\b", r"\bfacebook\s+ads\b", r"\blinkedin\s+ads\b",
                  r"\badwords\b", r"\badvertis\w*\b", r"\bmedia\s+buy\b", r"\btrade\s+fair\b", r"\bexhibition\b"]},
    {"purpose_code": "S1007", "nature_code": "16.49", "weight": 45,
     "patterns": [r"\bmarketing\b", r"\bpromotion\b", r"\blead\s*gen", r"\bmarket\s+research\b"]},

    # Consulting / management / PR
    {"purpose_code": "S1006", "nature_code": "16.13", "weight": 55,
     "patterns": [r"\bconsult\w*\b", r"\badvisory\b", r"\bmanagement\s+fee\b", r"\bpublic\s+relations\b", r"\bpr\s+services\b"]},
    {"purpose_code": "S1006", "nature_code": "16.46", "weight": 40,
     "patterns": [r"\bretainer\b", r"\bretainership\b"]},
    {"purpose_code": "S1006", "nature_code": "16.47", "weight": 40,
     "patterns": [r"\bretention\s+fee\b"]},

    # Legal / accounting / audit -> Professional services
    # IMPORTANT: do NOT match bare "legal" (boilerplate risk)
    {"purpose_code": "S1004", "nature_code": "16.40", "weight": 75,
     "patterns": [
         r"\blegal\s+(services?|fee|fees|advice|counsel)\b",
         r"\blaw\s+firm\b",
         r"\battorney\b|\bsolicitor\b|\bcounsel\b",
         r"\blitigation\b|\barbitration\b",
     ]},
    {"purpose_code": "S1005", "nature_code": "16.40", "weight": 70,
     "patterns": [r"\baudit\b", r"\bbook\s*keeping\b", r"\bbook-keeping\b", r"\baccounting\b"]},

    # Architecture / engineering / R&D
    {"purpose_code": "S1009", "nature_code": "16.3", "weight": 60,
     "patterns": [r"\barchitect\w*\b", r"\barchitectural\b"]},
    {"purpose_code": "S1014", "nature_code": "16.18", "weight": 55,
     "patterns": [r"\bengineering\s+services?\b", r"\bcad\b", r"\bcae\b", r"\bdesign\s+engineering\b"]},
    {"purpose_code": "S1008", "nature_code": "16.42", "weight": 65,
     "patterns": [r"\br&d\b", r"\bresearch\s+and\s+development\b", r"\bprototype\b", r"\blab\b", r"\bexperiment\w*\b"]},

    # FEES FOR TECHNICAL SERVICES (office default: S1023)
    # Industrial / technical / automation / PLC
    {"purpose_code": "S1023", "nature_code": "16.21", "weight": 90,
     "patterns": [
         r"\bplc\b",
         r"\bplc[-\s]?programm\w*\b",
         r"\bscada\b",
         r"\bautomation\b",
         r"\bcommissioning\b",
         r"\bcontrols?\b",
         r"\bcontrol\s+panel\b",
         r"\binstallation\b",
         r"\btechnical\s+service(s)?\b",
         r"\bremote\s+integration\b",
         r"\bplc\b.*\bintegrat\w*\b|\bintegrat\w*\b.*\bplc\b",  # plc + integration together
         r"\bprogramming\b",
     ]},

    # IT/software consultancy/implementation (use when clearly IT/app)
    {"purpose_code": "S0802", "nature_code": "16.21", "weight": 75,
     "patterns": [
         r"\bsoftware\s+consult\w*\b",
         r"\bsoftware\s+implementation\b",
         r"\bapplication\s+implementation\b",
         r"\bapp\s+development\b",
         r"\bsap\b|\boracle\b|\bsalesforce\b|\bmicrosoft\s+dyn\w*\b",
         r"\bconfiguration\b|\bdeployment\b|\bonboarding\b|\bimplementation\b|\bintegration\b",
     ]},

    # Hosting/cloud/platform environment services
    {"purpose_code": "S0803", "nature_code": "16.54", "weight": 82,
     "patterns": [
         r"\buat\b",
         r"\bprod\b",
         r"\benvironment\b",
         r"\bhosting\b",
         r"\bcloud\b",
         r"\bplatform\b",
     ]},
    {"purpose_code": "S0803", "nature_code": "16.21", "weight": 64,
     "patterns": [
         r"\bbackend\b",
         r"\bbackend\s+support\b",
         r"\bcloud\s+support\b",
     ]},

    # Maintenance / warranty / purchase / license/subscription
    {"purpose_code": "S0804", "nature_code": "16.2", "weight": 65,
     "patterns": [r"\bamc\b", r"\bannual\s+maintenance\b", r"\bmaintenance\s+fee\b", r"\bsupport\s+and\s+maintenance\b"]},
    {"purpose_code": "S0804", "nature_code": "16.61", "weight": 55,
     "patterns": [r"\bwarranty\b", r"\bextended\s+warranty\b"]},
    {"purpose_code": "S0807", "nature_code": "16.41", "weight": 70,
     "patterns": [r"\boff-?site\s+software\b", r"\bsoftware\s+purchase\b", r"\bdownload\s+software\b", r"\blicen[cs]e\s+key\b"]},
    {"purpose_code": "S0902", "nature_code": "16.54", "weight": 60,
     "patterns": [r"\bsaas\b", r"\bsubscription\b", r"\baccess\s+fee\b", r"\bper\s+seat\b", r"\bper\s+user\b"]},
    {"purpose_code": "S0902", "nature_code": "16.52", "weight": 55,
     "patterns": [r"\bsoftware\s+licen[cs]e(s)?\b", r"\bsoftware\s+licen[cs]es\b", r"\blicen[cs]ing\b"]},
    {"purpose_code": "S0902", "nature_code": "16.48", "weight": 80,
     "patterns": [r"\broyalty\b"]},

    # Telecom
    {"purpose_code": "S0808", "nature_code": "16.4", "weight": 60,
     "patterns": [r"\bbandwidth\b", r"\bleased\s+line\b", r"\bmpls\b"]},
    {"purpose_code": "S0808", "nature_code": "16.8", "weight": 60,
     "patterns": [r"\broaming\b"]},
    {"purpose_code": "S0808", "nature_code": "16.12", "weight": 50,
     "patterns": [r"\btelecom\b", r"\bcommunication\s+charges\b", r"\bcall\s+charges\b", r"\bvoip\b"]},

    # Freight / logistics
    {"purpose_code": "S0220", "nature_code": "16.22", "weight": 70,
     "patterns": [r"\bfreight\b", r"\bawb\b", r"\bair\s+waybill\b", r"\bbill\s+of\s+lading\b",
                  r"\bcourier\b", r"\bdhl\b", r"\bfedex\b", r"\bups\b"]},
    {"purpose_code": "S0220", "nature_code": "16.10", "weight": 60,
     "patterns": [r"\bcustoms\s+clearance\b", r"\bcha\b", r"\bc&f\b", r"\bforwarding\b"]},
    {"purpose_code": "S0220", "nature_code": "16.7", "weight": 55,
     "patterns": [r"\blogistics\b", r"\bcargo\s+handling\b", r"\binspection\b", r"\bterminal\s+handling\b"]},

    # Commission / brokerage / insurance commission
    {"purpose_code": "S1002", "nature_code": "16.11", "weight": 65,
     "patterns": [r"\bcommission\b", r"\breferral\b", r"\bagency\s+commission\b"]},
    {"purpose_code": "S0702", "nature_code": "16.5", "weight": 65,
     "patterns": [r"\bbrokerage\b", r"\bunderwriting\b"]},
    {"purpose_code": "S0605", "nature_code": "16.26", "weight": 70,
     "patterns": [r"\binsurance\s+commission\b"]},

    # Training vs tuition/student payments
    {"purpose_code": "S1107", "nature_code": "16.60", "weight": 70,
     "patterns": [r"\btraining\b", r"\bworkshop\b", r"\bseminar\b", r"\bbootcamp\b"]},
    {"purpose_code": "S1107", "nature_code": "16.37", "weight": 70,
     "patterns": [r"\btuition\b", r"\buniversity\b", r"\bcourse\s+fee\b", r"\bstudent\b", r"\beducation\s+fee\b"]},

    # Telecast / tender
    {"purpose_code": "S1103", "nature_code": "16.57", "weight": 60,
     "patterns": [r"\btelecast\b", r"\bbroadcast\b", r"\bradio\b", r"\btelevision\b"]},
    {"purpose_code": "S1503", "nature_code": "16.58", "weight": 65,
     "patterns": [r"\btender\s+fee\b", r"\bbid\s+fee\b", r"\brfp\b"]},

    # Generic fallback bucket (last resort)
    {"purpose_code": "S1099", "nature_code": "16.6", "weight": 5,
     "patterns": [r"\bservice\b", r"\bcharges\b", r"\bfee\b"]},
]

_S_CODE_RE = re.compile(r"\bS\d{4}\b", re.IGNORECASE)

def _explicit_s_code(text: str) -> Optional[str]:
    m = _S_CODE_RE.search(text or "")
    return m.group(0).upper() if m else None

def _score_by_rules(norm_text: str) -> Tuple[Dict[str, float], Dict[str, float], List[str]]:
    """
    Returns:
      purpose_scores, nature_scores, evidence_hits
    evidence_hits contains matched snippets (not regex patterns).
    """
    p_scores: Dict[str, float] = {}
    n_scores: Dict[str, float] = {}
    hits: List[str] = []

    for rule in HIGH_SIGNAL_RULES:
        patterns = cast(List[str], rule.get("patterns", []))
        weight = float(rule.get("weight", 0.0))
        pcode = str(rule.get("purpose_code", "")).upper().strip()
        ncode = str(rule.get("nature_code", "")).strip()

        matched = False
        for pat in patterns:
            m = re.search(pat, norm_text, flags=re.IGNORECASE)
            if m:
                matched = True
                hits.append(m.group(0))
                break
        if not matched:
            continue

        if pcode:
            p_scores[pcode] = p_scores.get(pcode, 0.0) + weight
        if ncode:
            n_scores[ncode] = n_scores.get(ncode, 0.0) + weight

    return p_scores, n_scores, hits[:6]

def _score_by_description_similarity(evidence_tokens: set) -> Dict[str, float]:
    # IDF-weighted token overlap between evidence and purpose descriptions
    idf = _idf_for_purpose_desc()
    scores: Dict[str, float] = {}
    for code, rec in _purpose_records().items():
        desc_toks = set(_tokens(rec.description))
        inter = evidence_tokens.intersection(desc_toks)
        if not inter:
            continue
        scores[code] = sum(idf.get(t, 1.0) for t in inter)
    return scores

def _pick_best(scores: Dict[str, float]) -> Tuple[str, float, float]:
    if not scores:
        return "", 0.0, 0.0
    items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_code, best = items[0]
    second = items[1][1] if len(items) > 1 else 0.0
    return best_code, best, second

def _confidence(best: float, second: float, explicit: bool) -> float:
    if explicit:
        return 0.95
    if best <= 0.0:
        return 0.30
    ratio = best / (best + second + 1e-6)
    margin = min(1.0, max(0.0, (best - second) / (best + 1e-6)))
    return float(max(0.0, min(1.0, 0.55 * ratio + 0.45 * margin)))

def classify_remittance(invoice_text: str, extracted: Optional[Dict[str, str]] = None) -> Optional[Classification]:
    """
    Returns best (purpose, nature) with confidence + needs_review.
    Always returns something as long as master lists are present.
    """
    extracted = extracted or {}

    # 1) Focus ONLY the real invoice content (line items).
    base = _focus_invoice_text(str(invoice_text or ""))
    base_norm = _norm(base)

    purpose_map = _purpose_records()
    nature_map = _nature_records()
    if not purpose_map or not nature_map:
        logger.warning("remittance_classifier_missing_masters purpose=%s nature=%s", bool(purpose_map), bool(nature_map))
        return None

    # 2) Explicit S#### wins (if valid), but detect only from base invoice text.
    explicit = _explicit_s_code(base_norm)
    if explicit and explicit in purpose_map:
        p = purpose_map[explicit]
        p_scores, n_scores, hits = _score_by_rules(base_norm)

        ncode, _, _ = _pick_best(n_scores)
        if not ncode or ncode not in nature_map:
            ncode = "16.6" if "16.6" in nature_map else next(iter(nature_map.keys()))
        n = nature_map[ncode]

        return Classification(purpose=p, nature=n, confidence=0.95, needs_review=False, evidence=hits[:2] or [explicit])

    # 3) Score on base + safe enrichment (exclude purpose_code/group strings).
    enrich = " ".join(
        [
            str(extracted.get("nature_of_remittance") or ""),
            str(extracted.get("beneficiary_name") or ""),
        ]
    ).strip()
    combined = base + ("\n" + enrich if enrich else "")
    norm = _norm(combined)

    # 4) Rule scoring
    p_scores, n_scores, hits = _score_by_rules(norm)

    # Optional: treat Gemini purpose_code as a weak prior (not explicit).
    gem_pcode = str(extracted.get("purpose_code") or "").strip().upper()
    if gem_pcode in purpose_map:
        p_scores[gem_pcode] = p_scores.get(gem_pcode, 0.0) + 5.0

    # 4a) Apply CA-office nature -> purpose prior (soft boost)
    n_best, n_best_score, _ = _pick_best(n_scores)
    if n_best and n_best_score >= PRIOR_MIN_NATURE_SCORE:
        for pcode, bonus in NATURE_PURPOSE_PRIOR.get(n_best, {}).items():
            p_scores[pcode] = p_scores.get(pcode, 0.0) + float(bonus)

    # 5) Description similarity fallback (covers all codes)
    ev_tokens = set(_tokens(norm))
    sim_scores = _score_by_description_similarity(ev_tokens)
    for code, sc in sim_scores.items():
        # rules dominate; similarity is a backstop
        p_scores[code] = p_scores.get(code, 0.0) + 0.35 * sc

    best_code, best, second = _pick_best(p_scores)
    if not best_code or best_code not in purpose_map:
        best_code = "S1099" if "S1099" in purpose_map else next(iter(purpose_map.keys()))
        best, second = 0.1, 0.0

    p = purpose_map[best_code]

    # 6) Nature selection: rule-based if available; else token overlap against labels.
    ncode, _, _ = _pick_best(n_scores)
    if not ncode or ncode not in nature_map:
        best_n = "16.6" if "16.6" in nature_map else next(iter(nature_map.keys()))
        best_sc = -1.0
        for code, nr in nature_map.items():
            sc = len(set(_tokens(nr.label)).intersection(ev_tokens))
            if sc > best_sc:
                best_sc = sc
                best_n = code
        ncode = best_n

    n = nature_map[ncode]

    conf = _confidence(best, second, explicit=False)

    # review heuristic:
    # - low conf
    # - generic purpose or generic nature buckets
    # - nature says “OTHER…”
    needs_review = (
        conf < 0.75
        or best_code == "S1099"
        or n.code in {"16.6", "16.99"}
    )

    evidence = hits[:2]
    if not evidence:
        evidence = [" ".join(list(ev_tokens)[:6])] if ev_tokens else []

    return Classification(purpose=p, nature=n, confidence=conf, needs_review=needs_review, evidence=evidence)
