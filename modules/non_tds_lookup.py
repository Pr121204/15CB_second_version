"""Lookup helper for Non-TDS mode DTAA documentation fields.

Matches an invoice's extracted nature text against the entries in
``data/master/non_tds_reference.json`` and returns two XML fields that
document why no TDS is deducted under the applicable tax treaty:

    NatureRemDtaa   ← nature_of_remittance_as_per_agreement_document
    RelArtDetlDDtaa ← comment predicted from the resolved nature

Matching order:
  1. Fuzzy match on ``nature_of_remittance_as_per_agreement_document``
     (Gemini extraction typically returns standardised names).
  2. Fuzzy match on ``nature_mentioned_in_invoices`` (raw invoice text).
  3. Purpose-code prefix fallback (e.g. "S1023").
  4. Hard default → "FEES FOR TECHNICAL SERVICES".

Defaults when nothing is found:
    NatureRemDtaa   = "FEES FOR TECHNICAL SERVICES"
    RelArtDetlDDtaa = generated template for that nature
"""
from __future__ import annotations

import json
import os
import re
import functools
from difflib import SequenceMatcher
from typing import Dict, List, Optional

_REFERENCE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "master", "non_tds_reference.json")
)

_DEFAULT_NATURE = "FEES FOR TECHNICAL SERVICES"

# Similarity thresholds
_STD_NATURE_THRESHOLD = 0.55   # match against nature_of_remittance_as_per_agreement_document
_INV_NATURE_THRESHOLD = 0.60   # match against nature_mentioned_in_invoices (needs to be tighter)

# Minimum comment length to count as a real explanation (filters bare company names)
_MIN_COMMENT_LEN = 40


@functools.lru_cache(maxsize=1)
def _load_reference() -> List[dict]:
    try:
        with open(_REFERENCE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _normalise(text: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _clean_comment(comment: str) -> str:
    """Clean up formatting artefacts from stored comments."""
    # Replace placeholder underscores (e.g. "INCOME____ PAYMENT") with " - "
    comment = re.sub(r"_{2,}", " - ", comment)
    # Collapse multiple spaces
    comment = re.sub(r"  +", " ", comment).strip()
    return comment


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word sets (handles partial/reordered matches)."""
    wa = set(re.findall(r"[a-z0-9&]+", a))
    wb = set(re.findall(r"[a-z0-9&]+", b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _score(query: str, target: str) -> float:
    """Combined similarity: max of character-level and word-overlap scores."""
    q, t = _normalise(query), _normalise(target)
    return max(_similarity(q, t), _word_overlap(q, t))


@functools.lru_cache(maxsize=1)
def _build_nature_comment_map() -> Dict[str, str]:
    """Map normalised nature → best explanatory comment.

    Preference: sentence-style comments (≥ MIN_COMMENT_LEN) that begin with
    generic phrases, falling back to the longest available comment.
    """
    mapping: Dict[str, List[str]] = {}
    for rec in _load_reference():
        raw_nature = str(rec.get("nature_of_remittance_as_per_agreement_document") or "").strip()
        no_tds = rec.get("no_tds")
        if not raw_nature or not no_tds:
            continue
        comment = _clean_comment(str(no_tds).strip())
        if len(comment) < _MIN_COMMENT_LEN:
            continue
        mapping.setdefault(_normalise(raw_nature), []).append(comment)

    # Prefer comments that start with generic explanatory phrases; exclude "REMITTER OPINES"
    # which often leads to vendor-specific sentences.
    generic_starts = ("REMITTANCE", "PAYMENT", "PAYER", "IT IS ", "PAYMENTS")
    result: Dict[str, str] = {}
    for key, comments in mapping.items():
        preferred = [c for c in comments if any(c.upper().startswith(p) for p in generic_starts)]
        result[key] = preferred[0] if preferred else max(comments, key=len)
    return result


def _comment_for_nature(nature: str) -> str:
    """Return the canonical comment for a given nature, or a generated template."""
    mapping = _build_nature_comment_map()
    key = _normalise(nature)

    # Exact match
    if key in mapping:
        return mapping[key]

    # Fuzzy match among known natures.
    # Use _score() (max of character-level and word-overlap) with a high threshold
    # so that near-identical strings (e.g. "software licenses" vs "software license")
    # are matched, while coincidentally similar short strings (e.g. "r&d charges"
    # vs "freight charges") are rejected.
    best_score, best_comment = 0.0, ""
    for known_key, comment in mapping.items():
        s = _score(key, known_key)
        if s > best_score:
            best_score, best_comment = s, comment
    if best_score >= 0.70 and best_comment:
        return best_comment

    # Generated template
    return (
        f"REMITTANCE TOWARDS {nature.upper()} OF NR IS NOT TAXABLE AS PER "
        f"APPLICABLE TAX TREATY WITH INDIA. HENCE, NO TDS."
    )


def lookup_non_tds(nature_text: str, purpose_code: str = "") -> Dict[str, str]:
    """Return NatureRemDtaa and RelArtDetlDDtaa for a Non-TDS invoice.

    NatureRemDtaa is resolved through a priority-ordered search:
      1. Fuzzy match on ``nature_of_remittance_as_per_agreement_document``
         (standardised names — most likely to match Gemini output).
      2. Fuzzy match on ``nature_mentioned_in_invoices`` (raw invoice text).
      3. Purpose-code prefix fallback (first 5 chars, e.g. "S1023").
      4. Hard default: "FEES FOR TECHNICAL SERVICES".

    RelArtDetlDDtaa is then predicted from the resolved NatureRemDtaa via
    _comment_for_nature(), which looks up the canonical comment for that
    nature across all reference entries.
    """
    records = _load_reference()
    nature_rem_dtaa: str = ""

    query = nature_text.strip()
    code_prefix = purpose_code.strip().upper()[:5]

    # --- Step 1: match against nature_of_remittance_as_per_agreement_document ---
    if query:
        best_score, best_nat = 0.0, ""
        for rec in records:
            std_nature = str(rec.get("nature_of_remittance_as_per_agreement_document") or "")
            s = _score(query, std_nature)
            if s > best_score:
                best_score, best_nat = s, std_nature
        if best_score >= _STD_NATURE_THRESHOLD and best_nat:
            nature_rem_dtaa = best_nat.strip()

    # --- Step 2: match against nature_mentioned_in_invoices ---
    if not nature_rem_dtaa and query:
        best_score, best_rec = 0.0, None
        for rec in records:
            inv_nature = str(rec.get("nature_mentioned_in_invoices") or "")
            s = _score(query, inv_nature)
            if s > best_score:
                best_score, best_rec = s, rec
        if best_score >= _INV_NATURE_THRESHOLD and best_rec:
            nature_rem_dtaa = str(
                best_rec.get("nature_of_remittance_as_per_agreement_document") or ""
            ).strip()

    # --- Step 3: purpose-code prefix fallback ---
    if not nature_rem_dtaa and code_prefix:
        for rec in records:
            rec_code = str(rec.get("purpose_code") or "").upper()
            if rec_code.startswith(code_prefix):
                candidate = str(
                    rec.get("nature_of_remittance_as_per_agreement_document") or ""
                ).strip()
                if candidate:
                    nature_rem_dtaa = candidate
                    break

    # --- Step 4: hard default ---
    if not nature_rem_dtaa:
        nature_rem_dtaa = _DEFAULT_NATURE

    # Predict comment from the resolved nature
    rel_art_detl = _comment_for_nature(nature_rem_dtaa)

    return {
        "NatureRemDtaa": nature_rem_dtaa,
        "RelArtDetlDDtaa": rel_art_detl,
    }
