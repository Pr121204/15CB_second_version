# modules/remittance_classifier.py
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TypedDict, cast

from modules.logger import get_logger
from modules.master_lookups import load_nature_options, load_purpose_grouped
from modules.text_remittance_ai_helper import classify_text_field

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
    high_signal_hit: bool = False

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
    # Explicit Bosch-style R&D wording should map to technical services (S1023).
    {"purpose_code": "S1023", "nature_code": "16.21", "weight": 130,
     "patterns": [
         r"\bcharging\s+of\s+r\s*&\s*d\s+services?\s+based\s+on\s+hours?\b",
         r"\br\s*&\s*d\s+services?\s+based\s+on\s+hours?\b",
         r"\bcharging\s+of\s+research\s+and\s+development\s+services?\s+based\s+on\s+hours?\b",
     ]},
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

    # Payroll / Social Security / Compensation
    {"purpose_code": "S1401", "nature_code": "16.99", "weight": 80,
     "patterns": [
         r"\bsocial\s+security\b", r"\bpayroll\b", r"\bsalary\s+recharge\b", 
         r"\bemployee\s+cost\b", r"\bpersonnel\s+cost\b", 
         r"\bservice\s+paid\s+for\s+other\s+entity\b",
         r"\bpayroll\s+allocation\b", r"\bemployee\s+contribution\b"
     ]},

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

    # Software project execution / technical delivery (S1023 — office default for project keywords).
    # These terms signal technical service delivery, NOT data processing (S0803) or
    # pure software consultancy (S0802).  Weight 100 > S0803 (82) > S0802 (75).
    {"purpose_code": "S1023", "nature_code": "16.21", "weight": 100,
     "patterns": [
         r"\bbackend\b",
         r"\buat\b",                                      # User Acceptance Testing
         r"\bprod\b",                                     # Production environment/deployment
         r"\bplatform\b",
         r"\bdeployment\b",
         r"\bsoftware\s+project\b",
         r"\benvironment\b",
         r"\bdevops\b",
         r"\bci[-/]?cd\b",
         r"\bsystem\s+integration\b",
         r"\bqa\s+services?\b|\bquality\s+assurance\b",
         r"\btesting\s+services?\b",
         r"\bperformance\s+testing\b",
         r"\bload\s+testing\b",
         r"\bregression\s+testing\b",
         r"\binfrastructure\s+(?:setup|services?|management)\b",
         r"\btechnical\s+(?:project|delivery|programme|program)\b",
         r"\bapplication\s+(?:support|management|maintenance)\b",
         r"\brelease\s+management\b",
         r"\bsprint\b",
     ]},

    # IT/software consultancy/implementation (use when clearly IT/app).
    # deployment removed — it belongs to S1023 project execution.
    {"purpose_code": "S0802", "nature_code": "16.21", "weight": 75,
     "patterns": [
         r"\bsoftware\s+consult\w*\b",
         r"\bsoftware\s+implementation\b",
         r"\bapplication\s+implementation\b",
         r"\bapp\s+development\b",
         r"\bsap\b|\boracle\b|\bsalesforce\b|\bmicrosoft\s+dyn\w*\b",
         r"\bconfiguration\b|\bonboarding\b|\bimplementation\b|\bintegration\b",
     ]},

    # Data processing / database / managed hosting — genuinely S0803.
    # uat, prod, environment, platform, backend removed; those belong to S1023.
    {"purpose_code": "S0803", "nature_code": "16.54", "weight": 82,
     "patterns": [
         r"\bhosting\b",
         r"\bcloud\s+(?:hosting|infrastructure|storage)\b",
         r"\bdata\s+processing\b",
         r"\bdatabase\s+services?\b",
         r"\bdata\s+storage\b",
         r"\bdata\s+management\b",
         r"\bdata\s+(?:processing\s+)?charges?\b",
     ]},
    {"purpose_code": "S0803", "nature_code": "16.21", "weight": 64,
     "patterns": [
         r"\bcloud\s+support\b",
         r"\bdata\s+cent(?:re|er)\b",
         r"\bmanaged\s+hosting\b",
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
_GENERIC_HIT_TOKEN_RE = re.compile(r"^(service|services|fee|fees|charge|charges)$", re.IGNORECASE)

def _explicit_s_code(text: str) -> Optional[str]:
    m = _S_CODE_RE.search(text or "")
    return m.group(0).upper() if m else None


def _has_specific_signal(hits: List[str]) -> bool:
    for hit in hits:
        tokens = [t for t in re.split(r"[^\w&]+", str(hit or "").lower()) if t]
        if not tokens:
            continue
        if len(tokens) == 1 and _GENERIC_HIT_TOKEN_RE.match(tokens[0]):
            continue
        return True
    return False

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

    # 0) Priority 1: Excel Text Column Classifier (Structured Source Data)
    excel_text = str(extracted.get("_excel_text") or "").strip()
    if excel_text:
        text_result = classify_text_field(
            excel_text, 
            pdf_text=invoice_text, 
            vendor=extracted.get("beneficiary_name"),
            invoice_id=extracted.get("invoice_id"),
            line_items=extracted.get("line_items")
        )
        conf_str = text_result.get("confidence", "LOW")
        # Map string confidence to float
        conf_map = {"HIGH": 0.95, "MEDIUM": 0.80, "LOW": 0.40}
        conf_val = conf_map.get(conf_str, 0.40)
        
        pcode = text_result.get("purpose_code")
        if pcode and pcode in purpose_map and conf_str in ("HIGH", "MEDIUM"):
            p = purpose_map[pcode]
            # Infer nature: try same keywords from local rules
            _, n_scores, _ = _score_by_rules(_norm(excel_text))
            ncode, _, _ = _pick_best(n_scores)
            if not ncode or ncode not in nature_map:
                ncode = "16.6" if "16.6" in nature_map else next(iter(nature_map.keys()))
            n = nature_map[ncode]
            
            if text_result.get("source") in ("rd_rule", "payroll_rule"):
                logger.info("classification_priority_rule_triggered invoice_id=%s code=%s source=%s", 
                            extracted.get("invoice_id", ""), pcode, text_result.get("source"))
                # Override nature label if provided by rule
                rule_nature = text_result.get("nature_of_remittance")
                if rule_nature:
                    n = NatureRecord(code=n.code, label=rule_nature)
                
                return Classification(
                    purpose=p,
                    nature=n,
                    confidence=conf_val,
                    needs_review=False,
                    evidence=text_result.get("matched_keywords", []) or [f"Rule: {text_result.get('source')}"],
                    high_signal_hit=True,
                )

            logger.info("classification_excel_text_priority invoice_id=%s code=%s conf=%s", extracted.get("invoice_id", ""), pcode, conf_str)
            return Classification(
                purpose=p,
                nature=n,
                confidence=conf_val,
                needs_review=text_result.get("manual_review", False),
                evidence=text_result.get("matched_keywords", []) or [f"Excel Text: {excel_text}"],
                high_signal_hit=conf_val >= 0.95,
            )

    # 2) Explicit S#### wins (if valid), but detect only from base invoice text.
    explicit = _explicit_s_code(base_norm)
    if explicit and explicit in purpose_map:
        p = purpose_map[explicit]
        p_scores, n_scores, hits = _score_by_rules(base_norm)

        ncode, _, _ = _pick_best(n_scores)
        if not ncode or ncode not in nature_map:
            ncode = "16.6" if "16.6" in nature_map else next(iter(nature_map.keys()))
        n = nature_map[ncode]

        return Classification(
            purpose=p,
            nature=n,
            confidence=0.95,
            needs_review=False,
            evidence=hits[:2] or [explicit],
            high_signal_hit=True,
        )

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

    specific_signal = _has_specific_signal(hits)
    no_signal = not p_scores and not sim_scores and not hits

    best_code, best, second = _pick_best(p_scores)
    if not best_code or best_code not in purpose_map:
        best_code = "S1099" if "S1099" in purpose_map else next(iter(purpose_map.keys()))
        best, second = 0.0, 0.0

    p = purpose_map[best_code]

    # 6) Nature selection: generic fallback when there is no clear nature evidence.
    ncode, _, _ = _pick_best(n_scores)
    if not ncode or ncode not in nature_map:
        ncode = "16.6" if "16.6" in nature_map else next(iter(nature_map.keys()))

    n = nature_map[ncode]

    conf = _confidence(best, second, explicit=False)
    if no_signal:
        conf = 0.30
    elif best_code == "S1099" and not specific_signal:
        conf = min(conf, 0.45)

    # 7. Hybrid fallback: if confidence < 0.7, prefer Gemini's original extraction
    if conf < 0.70:
        gem_code = str(extracted.get("purpose_code") or "").strip().upper()
        if gem_code and gem_code in purpose_map:
            logger.info("classification_low_confidence_fallback invoice_id=%s conf=%.2f using_gemini=%s", 
                        extracted.get("invoice_id", ""), conf, gem_code)
            p = purpose_map[gem_code]
            # Try to get nature from gemini as well
            gem_nature = str(extracted.get("nature_of_remittance") or "").strip()
            # If we can't find exact match for gemini nature, keep current best 'n'
            return Classification(
                purpose=p,
                nature=n,
                confidence=conf,
                needs_review=True,
                evidence=["Low confidence fallback to Gemini"],
                high_signal_hit=False,
            )

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

    return Classification(
        purpose=p,
        nature=n,
        confidence=conf,
        needs_review=needs_review,
        evidence=evidence,
        high_signal_hit=specific_signal,
    )
