"""
text_remittance_ai_helper.py

Production-minded classifier for mapping an invoice Excel `Text` column (and optional PDF text)
to:
  - purpose_code (e.g. "S0802")
  - purpose_group (human readable)
  - nature_of_remittance (human readable)
  - confidence ("HIGH","MEDIUM","LOW")
  - matched_keywords, reason, and structured audit fields.

Features:
- Prioritized keyword rules (most-specific -> most-generic).
- Instructional text detection (common SAP notes) -> fallback.
- Blank/generic handling -> fallback.
- Multiple-match resolution and keyword scoring.
- Vendor-based overrides / mapping.
- Amount thresholding: large payments force manual review if confidence < HIGH.
- Optional semantic fallback using sentence-transformers (if installed).
- Logging to a JSONL audit log and to CSV results.
"""

import re
import json
import csv
import logging
import datetime
import os
from typing import Optional, Dict, List, Tuple, Any
from modules.purpose_rich_master import PURPOSE_RICH_MASTER, PURCHASE_GOODS_CODE

# ---------- Configuration ----------
AUDIT_LOG_FILE = "remittance_audit.jsonl"
RESULT_CSV_FILE = "remittance_results.csv"

# Amount (INR) threshold that triggers mandatory manual review unless confidence HIGH
MANUAL_REVIEW_AMOUNT_THRESHOLD = 500000  # ₹5,00,000 (adjustable)

# Vendor-specific override mapping: vendor_name_lower -> purpose_code
VENDOR_OVERRIDE = {
    # "vendor ltd": "S0802",  # example
}

# Optional: prefer goods detection -> map to imports/payment for goods
GOODS_KEYWORDS = ["hsn", "qty", "pcs", "piece", "shipment", "invoice for goods", "batch", "carton", "consignment"]

# Instructional / non-descriptive phrases (common SAP notes)
INSTRUCTIONAL_PATTERNS = [
    r"\btds\b",
    r"\bdocs received\b",
    r"\bpayment after\b",
    r"\bproceed for payment\b",
    r"\bonly after\b",
    r"check and proceed",
    r"pending tds",
    r"attach tds",
    r"tcs\b",
]

# Normalize / clean settings
MIN_TOKEN_LEN_FOR_MATCH = 2

# ---------- Logging ----------
logger = logging.getLogger("remittance_text_classifier")
logger.setLevel(logging.INFO)

if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(console_handler)

# Audit logger writes JSON lines for each decision (append mode)
audit_logger = logging.getLogger("remittance_audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    try:
        fh = logging.FileHandler(AUDIT_LOG_FILE)
        fh.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"Could not initialize audit file handler: {e}")

# ---------- RBI Purpose Master (Representative Subset / Legacy Fallback) ----------
# We prioritize PURPOSE_RICH_MASTER when available for advanced logic.
PURPOSE_MASTER = {
    "S0802": {"group": "Telecommunication, Computer & Information Services", "nature": "Software consultancy / implementation"},
    "S0803": {"group": "Telecommunication, Computer & Information Services", "nature": "Data base, data processing charges"},
    "S1004": {"group": "Other Business Services", "nature": "Legal and tax consultancy services"},
    "S1006": {"group": "Other Business Services", "nature": "Business and management consultancy services"},
    "S1007": {"group": "Other Business Services", "nature": "Advertising, trade fair services"},
    "S1008": {"group": "Other Business Services", "nature": "Fees for Technical Services / Research & Development services"},
    "S1023": {"group": "Other Business Services", "nature": "Other Technical Services (engineering/maintenance)"},
    "S0902": {"group": "Charges for the use of intellectual property n.i.e", "nature": "Payment for use through licensing / royalty"},
    "S0102": {"group": "Imports", "nature": "Payment towards imports settlement"},
    "S1099": {"group": "Other Business Services", "nature": "Other services not included elsewhere"},
    "S1401": {"group": "Primary Income", "nature": "COMPENSATION OF EMPLOYEES / PAYROLL COST"},
    "S1502": {"group": "Others", "nature": "Reversal of wrong entries / refunds"},
    "S0804": {"group": "Telecommunication, Computer & Information Services", "nature": "Repair and maintenance of computer/software"},
    "S0203": {"group": "Transport", "nature": "Freight on imports - Shipping companies"},
}

# ---------- RULES (ordered by priority: top = more specific) ----------
RULES = [
    (["r&d", "research and development", "research", "rnd"], "S1008"),
    (["royalty", "license", "licence", "licensing", "patent", "copyright"], "S0902"),
    (["software industrialisation", "software", "saas", "hosting", "cloud", "application development", "app development"], "S0802"),
    (["database", "data processing", "data program", "data programs", "data processing charges", "data analytics"], "S0803"),
    (["tax service", "tax consultancy", "tax service fee", "tax progression"], "S1004"),
    (["consulting", "consultancy", "management consultancy", "business and management", "cost sharing", "transfer price", "transfer pricing"], "S1006"),
    (["advertising", "marketing", "promotion", "ads", "trade fair"], "S1007"),
    (["installation", "installation service", "maintenance", "repair", "disassembly", "grinding", "drums", "dismantl", "engineer", "engineering"], "S1023"),
    (["personnel", "payroll", "salary", "salaries", "social security", "employee benefits"], "S1401"),
    (["equipment", "hardware", "machine", "machinery", "device", "goods", "spare parts", "material supply"], "S0102"),
    (["reimbursement", "reimburse", "refund", "refunds"], "S1502"),
    (["servicebill", "service bill", "service bill-"], "S1099"),
]

def is_bosch_vendor(name: Optional[str]) -> bool:
    """True if name contains 'bosch'."""
    if not name:
        return False
    return "bosch" in str(name).lower()

# ---------- 8 DETERMINISTIC BOSCH RULES ----------
BOSCH_DETERMINISTIC_RULES = [
    {
        "name": "R&D Engineering Services",
        "keywords": ["r&d", "research and development", "charging of r&d services", "engineering services", "product development"],
        "purpose_code": "S1023",
        "purpose_group": "Other Business Services",
        "nature": "FEES FOR TECHNICAL SERVICES"
    },
    {
        "name": "Payroll / Social Security recharge",
        "keywords": ["social security", "employee cost", "personnel cost", "service paid for other entity - person", "payroll recharge", "payroll"],
        "purpose_code": "S1401",
        "purpose_group": "Primary Income",
        "nature": "COMPENSATION OF EMPLOYEES"
    },
    {
        "name": "IT / SAP Support",
        "keywords": ["sap", "sap support", "it support", "system support", "software maintenance"],
        "purpose_code": "S0802",
        "purpose_group": "Telecommunication, Computer & Information Services",
        "nature": "SOFTWARE SERVICES"
    },
    {
        "name": "Data Processing",
        "keywords": ["data processing", "database services", "hosting", "data management"],
        "purpose_code": "S0803",
        "purpose_group": "Telecommunication, Computer & Information Services",
        "nature": "DATA PROCESSING SERVICES"
    },
    {
        "name": "Marketing / Sales Support",
        "keywords": ["marketing", "sales support", "advertising", "market research"],
        "purpose_code": "S1007",
        "purpose_group": "Other Business Services",
        "nature": "MARKETING SERVICES"
    }
]

GENERIC_TOKENS = ["servicebill", "service bill", "service", "invoice", "bill", "payment", "payment note", "remark", "n/a", "-"]

# ---------- UTILITIES ----------

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "") + "Z"

def normalize_text(txt: Optional[str]) -> str:
    """Lowercase, strip, normalize hyphens and common punctuation, reduce multiple spaces."""
    if not txt:
        return ""
    s = str(txt).lower().strip()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[^\w\s\-/&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_instructional_text(normalized: str) -> bool:
    """Detect non-descriptive or instruction-like notes that are not service descriptions."""
    if not normalized:
        return False
    for pat in INSTRUCTIONAL_PATTERNS:
        if re.search(pat, normalized):
            return True
    # If text is extremely short (e.g. "-", "."), it's not a description
    if len(normalized) < 2:
        return True
    return False

def contains_goods_signals(normalized: str) -> bool:
    for k in GOODS_KEYWORDS:
        if k in normalized:
            return True
    if "hsn" in normalized or "gst" in normalized:
        return True
    return False

# ---------- CORE CLASSIFICATION ----------

def rule_based_classify(normalized_text: str, line_items: Optional[List[Dict[str, Any]]] = None) -> Tuple[Optional[str], List[str]]:
    """
    Advanced classification using PURPOSE_RICH_MASTER scoring.
    Logic:
    1. Check for Dominant Service in line items.
    2. Score codes based on Keyword Strength (High=3, Medium=2, Weak=1).
    3. Check Intercompany Boost.
    4. Handle Exclusions.
    """
    if not normalized_text:
        return None, []

    # 0. Specialized R&D Detection Rule
    rd_keywords = ["engineering development", "charging of r&d services"]
    if any(kw in normalized_text for kw in rd_keywords):
        logger.info("rd_service_detected input=%r matched=S1008", normalized_text)
        return "S1008", ["rd_rule"]

    # 0.1 Specialized Payroll/Social Security Detection Rule
    payroll_keywords = [
        "social security", "payroll", "salary recharge", "employee cost", 
        "personnel cost", "service paid for other entity - person",
        "payroll allocation", "employee contribution"
    ]
    if any(kw in normalized_text for kw in payroll_keywords):
        logger.info("payroll_remittance_detected input=%r matched=S1401", normalized_text)
        return "S1401", ["payroll_rule"]

    scores: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = {}
    
    # 1. Dominant Service Detection
    dominant_code = None
    if line_items:
        try:
            # Sort items by amount descending to find the dominant one
            sorted_items = sorted(line_items, key=lambda x: float(x.get("amount", 0) or 0), reverse=True)
            if sorted_items:
                dominant_item = sorted_items[0]
                dominant_desc = normalize_text(dominant_item.get("description", ""))
                
                for code, meta in PURPOSE_RICH_MASTER.items():
                    dominant_keywords = meta.get("dominant_service_keywords", [])
                    for kw in dominant_keywords:
                        if kw in dominant_desc:
                            dominant_code = code
                            logger.info("dominant_service_detected code=%s matched=%r in principal line item", code, kw)
                            break
                    if dominant_code: break
        except Exception as e:
            logger.debug("dominant_service_check_failed: %s", e)

    # 2. Keyword Strength Scoring
    for code, meta in PURPOSE_RICH_MASTER.items():
        score = 0
        matched = []
        kw_config = meta.get("keywords", {})
        
        # High Strength (3 pts)
        for kw in kw_config.get("high", []):
            if kw in normalized_text:
                score += 3
                matched.append(f"{kw}(HIGH)")
                
        # Medium Strength (2 pts)
        for kw in kw_config.get("medium", []):
            if kw in normalized_text:
                score += 2
                matched.append(f"{kw}(MED)")
                
        # Weak Strength (1 pt)
        for kw in kw_config.get("weak", []):
            if kw in normalized_text:
                score += 1
                matched.append(f"{kw}(WEAK)")

        # Intercompany Boost (+2 pts)
        for pat in meta.get("intercompany_patterns", []):
            if pat in normalized_text:
                score += 2
                matched.append(f"{pat}(INTERCOMPANY_BOOST)")
                break # Only one boost

        # Dominant Service Boost (+5 pts)
        if dominant_code == code:
            score += 5
            matched.append("DOMINANT_SERVICE_MATCH")

        # Exclusions (Clear score if excluded)
        for excl in meta.get("exclusions", []):
            if excl in normalized_text:
                score = 0
                matched = []
                break

        if score > 0:
            scores[code] = score
            evidence[code] = matched

    # Fallback to legacy RULES if no rich match
    if not scores:
        for keywords, code in RULES:
            matched_legacy = []
            for kw in keywords:
                if kw in normalized_text:
                    matched_legacy.append(kw)
            if matched_legacy:
                return code, matched_legacy

    if not scores:
        return None, []

    # Pick highest scoring code
    best_code = max(scores, key=scores.get)
    return best_code, evidence[best_code]

def build_result(code: Optional[str], matched_keywords: List[str], source: str, confidence: str, notes: List[str]) -> Dict:
    now = now_iso()
    now = now_iso()
    # Prioritize Rich Master
    if code and code in PURPOSE_RICH_MASTER:
        meta = PURPOSE_RICH_MASTER[code]
        return {
            "timestamp": now,
            "purpose_code": code,
            "purpose_group": meta["group"],
            "nature_of_remittance": meta.get("dtaa_category", meta["nature"]), # Prefer DTAA Category for nature
            "confidence": confidence,
            "matched_keywords": matched_keywords,
            "source": source,
            "notes": notes,
        }
    elif code and code in PURPOSE_MASTER:
        meta = PURPOSE_MASTER[code]
        return {
            "timestamp": now,
            "purpose_code": code,
            "purpose_group": meta["group"],
            "nature_of_remittance": meta["nature"],
            "confidence": confidence,
            "matched_keywords": matched_keywords,
            "source": source,
            "notes": notes,
        }
    else:
        return {
            "timestamp": now,
            "purpose_code": None,
            "purpose_group": None,
            "nature_of_remittance": None,
            "confidence": confidence,
            "matched_keywords": matched_keywords,
            "source": source,
            "notes": notes,
        }

# ---------- Optional Semantic Fallback ----------
SEMANTIC_SIMILARITY_THRESHOLD = 0.65

def semantic_fallback(normalized_text: str) -> Tuple[Optional[str], float]:
    """Attempt semantic matching using sentence-transformers if available."""
    try:
        from sentence_transformers import SentenceTransformer, util
    except Exception:
        return None, 0.0

    codes = list(PURPOSE_MASTER.keys())
    descriptions = [PURPOSE_MASTER[c]["nature"] for c in codes]

    try:
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception as e:
        logger.warning("SentenceTransformer model load failed: %s", e)
        return None, 0.0

    emb_desc = model.encode(descriptions, convert_to_tensor=True)
    emb_query = model.encode([normalized_text], convert_to_tensor=True)
    import torch
    hits = util.semantic_search(emb_query, emb_desc, top_k=1)[0]
    if hits and len(hits) > 0:
        top = hits[0]
        score = float(top["score"])
        if score >= SEMANTIC_SIMILARITY_THRESHOLD:
            matched_code = codes[top["corpus_id"]]
            return matched_code, score
    return None, 0.0

# ---------- PUBLIC API ----------

def classify_text_field(text: Optional[str],
                        pdf_text: Optional[str] = None,
                        vendor: Optional[str] = None,
                        amount: Optional[float] = None,
                        invoice_id: Optional[str] = None,
                        enable_semantic_fallback: bool = False,
                        line_items: Optional[List[Dict[str, Any]]] = None) -> Dict:
    """
    High level classification pipeline that tries vendor override, rules, PDF fallback, and semantic fallback.
    """
    text_norm = normalize_text(text)
    pdf_norm = normalize_text(pdf_text) if pdf_text else ""
    vendor_norm = vendor.lower().strip() if vendor else ""

    audit = {
        "invoice_id": invoice_id,
        "input_text": text if text is not None else "",
        "pdf_text_preview": (pdf_text[:500] + "...") if pdf_text else "",
        "vendor": vendor,
        "amount": amount,
        "timestamp": now_iso(),
        "steps": []
    }

    # 0) Stage 1: Detect Bosch Vendor First
    if is_bosch_vendor(vendor):
        # Apply Bosch keyword rules on text (Excel column) first, then PDF text
        targets = [("excel_text", text_norm), ("pdf_text", pdf_norm)]
        for source_name, target_text in targets:
            if not target_text:
                continue
            for rule in BOSCH_DETERMINISTIC_RULES:
                for kw in rule["keywords"]:
                    if kw in target_text:
                        audit["steps"].append(f"BOSCH_DETERMINISTIC_RULE_MATCH: {rule['name']} (source: {source_name}, kw: {kw})")
                        res = {
                            "timestamp": now_iso(),
                            "purpose_code": rule["purpose_code"],
                            "purpose_group": rule["purpose_group"],
                            "nature_of_remittance": rule["nature"],
                            "confidence": "HIGH",
                            "matched_keywords": [f"bosch_rule:{kw}"],
                            "source": f"bosch_{source_name}_rule",
                            "notes": [f"Matched Bosch rule: {rule['name']}"],
                        }
                        _audit_write(audit, res)
                        return _postprocess_with_amount(res, amount)

    # 1) Vendor override
    if vendor_norm and vendor_norm in VENDOR_OVERRIDE:
        code = VENDOR_OVERRIDE[vendor_norm]
        audit["steps"].append(f"VENDOR_OVERRIDE -> {code}")
        res = build_result(code, ["VENDOR_OVERRIDE"], "VENDOR_OVERRIDE", "HIGH", ["Vendor-specific mapping applied"])
        res["manual_review"] = False
        _audit_write(audit, res)
        return _postprocess_with_amount(res, amount)

    # 1) Blank / missing text
    if not text_norm:
        audit["steps"].append("TEXT_BLANK")
        if pdf_norm:
            audit["steps"].append("PDF_FALLBACK_ATTEMPT")
            code_pdf, matched_pdf = rule_based_classify(pdf_norm, line_items)
            if code_pdf:
                audit["steps"].append(f"PDF_RULE_MATCH -> {code_pdf} via {matched_pdf}")
                res = build_result(code_pdf, matched_pdf, "PDF_RULE", "MEDIUM", ["Fallback from PDF text"])
                _audit_write(audit, res)
                return _postprocess_with_amount(res, amount)
        if enable_semantic_fallback:
            audit["steps"].append("SEMANTIC_FALLBACK_ATTEMPT")
            target_text = pdf_norm or text_norm
            code_sem, score = semantic_fallback(target_text)
            if code_sem:
                audit["steps"].append(f"SEMANTIC_MATCH -> {code_sem} score={score:.3f}")
                res = build_result(code_sem, [f"semantic:{score:.3f}"], "SEMANTIC_FALLBACK", "MEDIUM", ["Semantic fallback applied"])
                _audit_write(audit, res)
                return _postprocess_with_amount(res, amount)
        res = build_result(None, [], "TEXT_BLANK", "LOW", ["Text blank, no PDF match"])
        res["manual_review"] = True
        _audit_write(audit, res)
        return res

    # 2) Instructional text detection
    if is_instructional_text(text_norm):
        audit["steps"].append("TEXT_INSTRUCTIONAL")
        if pdf_norm:
            audit["steps"].append("PDF_FALLBACK_ATTEMPT")
            code_pdf, matched_pdf = rule_based_classify(pdf_norm, line_items)
            if code_pdf:
                audit["steps"].append(f"PDF_RULE_MATCH -> {code_pdf} via {matched_pdf}")
                res = build_result(code_pdf, matched_pdf, "PDF_RULE", "MEDIUM", ["Instructional text; used PDF fallback"])
                _audit_write(audit, res)
                return _postprocess_with_amount(res, amount)
        if enable_semantic_fallback and pdf_norm:
            audit["steps"].append("SEMANTIC_FALLBACK_ATTEMPT")
            code_sem, score = semantic_fallback(pdf_norm)
            if code_sem:
                audit["steps"].append(f"SEMANTIC_MATCH -> {code_sem} score={score:.3f}")
                res = build_result(code_sem, [f"semantic:{score:.3f}"], "SEMANTIC_FALLBACK", "MEDIUM", ["Instructional text; semantic fallback used on PDF"])
                _audit_write(audit, res)
                return _postprocess_with_amount(res, amount)
        res = build_result(None, [], "TEXT_INSTRUCTIONAL", "LOW", ["Instructional text; manual review required"])
        res["manual_review"] = True
        _audit_write(audit, res)
        return res

    # 3) Rule-based classification on text
    code, matched = rule_based_classify(text_norm, line_items)
    if code:
        source = "TEXT_RULE"
        if matched and matched[0] == "rd_rule":
            source = "rd_rule"
        elif matched and matched[0] == "payroll_rule":
            source = "payroll_rule"
        
        audit["steps"].append(f"{source}_MATCH -> {code} via {matched}")
        res = build_result(code, matched, source, "HIGH", [f"Matched via {source}"])
        _audit_write(audit, res)
        return _postprocess_with_amount(res, amount)

    # 4) Generic fallback
    if "service" in text_norm or any(gt in text_norm for gt in GENERIC_TOKENS):
        audit["steps"].append("TEXT_GENERIC_SERVICE_FALLBACK")
        res = build_result("S1099", ["service"], "TEXT_FALLBACK", "MEDIUM", ["Generic 'service' fallback"])
        _audit_write(audit, res)
        return _postprocess_with_amount(res, amount)

    # 5) PDF fallback
    if pdf_norm:
        audit["steps"].append("PDF_RULE_FALLBACK")
        code_pdf, matched_pdf = rule_based_classify(pdf_norm, line_items)
        if code_pdf:
            audit["steps"].append(f"PDF_RULE_MATCH -> {code_pdf} via {matched_pdf}")
            res = build_result(code_pdf, matched_pdf, "PDF_RULE", "MEDIUM", ["Fallback from PDF"])
            _audit_write(audit, res)
            return _postprocess_with_amount(res, amount)

    # 6) Semantic fallback
    if enable_semantic_fallback:
        audit["steps"].append("SEMANTIC_FALLBACK_ATTEMPT")
        code_sem, score = semantic_fallback(text_norm or pdf_norm)
        if code_sem:
            audit["steps"].append(f"SEMANTIC_MATCH -> {code_sem} score={score:.3f}")
            res = build_result(code_sem, [f"semantic:{score:.3f}"], "SEMANTIC_FALLBACK", "MEDIUM", ["Semantic fallback applied"])
            _audit_write(audit, res)
            return _postprocess_with_amount(res, amount)

    # 7) No match
    audit["steps"].append("NO_MATCH_ANYWHERE")
    res = build_result("S1099", [], "NO_MATCH", "LOW", ["No rule matched; defaulted to S1099"])
    res["manual_review"] = True
    _audit_write(audit, res)
    return res

def _postprocess_with_amount(result: Dict, amount: Optional[float]) -> Dict:
    manual = result.get("manual_review", False)
    conf = result.get("confidence", "LOW")
    if amount is not None:
        try:
            amt = float(amount)
            if amt >= MANUAL_REVIEW_AMOUNT_THRESHOLD and conf != "HIGH":
                manual = True
                result.setdefault("notes", []).append(f"AMOUNT_THRESHOLD_EXCEEDED({amount})")
        except:
            pass
    result["manual_review"] = manual
    return result

def _audit_write(audit_context: Dict, result: Dict):
    entry = {"audit": audit_context, "result": result}
    try:
        audit_logger.info(json.dumps(entry, ensure_ascii=False))
    except:
        pass

def write_result_csv(rows: List[Dict], csv_file: str = RESULT_CSV_FILE):
    if not rows:
        return
    fieldnames = [
        "timestamp", "invoice_id", "vendor", "amount",
        "input_text", "pdf_text_preview",
        "purpose_code", "purpose_group", "nature_of_remittance",
        "confidence", "matched_keywords", "source", "notes", "manual_review"
    ]
    with open(csv_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = {
                "timestamp": r.get("result", {}).get("timestamp") or now_iso(),
                "invoice_id": r.get("audit", {}).get("invoice_id"),
                "vendor": r.get("audit", {}).get("vendor"),
                "amount": r.get("audit", {}).get("amount"),
                "input_text": r.get("audit", {}).get("input_text"),
                "pdf_text_preview": r.get("audit", {}).get("pdf_text_preview"),
                "purpose_code": r.get("result", {}).get("purpose_code"),
                "purpose_group": r.get("result", {}).get("purpose_group"),
                "nature_of_remittance": r.get("result", {}).get("nature_of_remittance"),
                "confidence": r.get("result", {}).get("confidence"),
                "matched_keywords": ",".join(r.get("result", {}).get("matched_keywords") or []),
                "source": r.get("result", {}).get("source"),
                "notes": ";".join(r.get("result", {}).get("notes") or []),
                "manual_review": r.get("result", {}).get("manual_review", False)
            }
            writer.writerow(row)
