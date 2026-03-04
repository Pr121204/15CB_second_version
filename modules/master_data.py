from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple


def normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _default_master_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "master" / "master_data.json"


def _default_aliases_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "master" / "aliases.json"


@functools.lru_cache(maxsize=1)
def load_master(path: str = "") -> Dict[str, Any]:
    source = Path(path) if path else _default_master_path()
    with open(source, "r", encoding="utf8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


@functools.lru_cache(maxsize=1)
def load_aliases(path: str = "") -> Dict[str, Dict[str, str]]:
    source = Path(path) if path else _default_aliases_path()
    if not source.exists():
        return {
            "indian_company_aliases": {},
            "foreign_party_aliases": {},
            "party_bank_aliases": {},
            "nature_aliases": {},
            "country_aliases": {},
        }
    with open(source, "r", encoding="utf8") as f:
        data = json.load(f)
    out = data if isinstance(data, dict) else {}
    for key in ("indian_company_aliases", "foreign_party_aliases", "party_bank_aliases", "nature_aliases", "country_aliases"):
        if key not in out or not isinstance(out[key], dict):
            out[key] = {}
    return out


def _build_indexes(master: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    indian_idx: Dict[str, Dict[str, Any]] = {}
    foreign_idx: Dict[str, Dict[str, Any]] = {}
    banks_idx: Dict[str, Dict[str, Any]] = {}
    nature_idx: Dict[str, Dict[str, Any]] = {}
    dtaa_idx: Dict[str, Dict[str, Any]] = {}

    for rec in master.get("indian_companies", []):
        if isinstance(rec, dict):
            key = normalize(str(rec.get("name") or ""))
            if key:
                indian_idx[key] = rec

    for rec in master.get("foreign_companies", []):
        if isinstance(rec, dict):
            key = normalize(str(rec.get("name") or ""))
            if key:
                foreign_idx[key] = rec

    for party_name, rows in (master.get("banks_by_party", {}) or {}).items():
        key = normalize(str(party_name or ""))
        if key:
            banks_idx[key] = {"party_name": party_name, "rows": rows if isinstance(rows, list) else []}

    for rec in master.get("nature_map", []):
        if not isinstance(rec, dict):
            continue
        for source_key in ("invoice_nature", "agreement_nature"):
            val = normalize(str(rec.get(source_key) or ""))
            if val and val not in nature_idx:
                nature_idx[val] = rec

    for rec in master.get("dtaa_rates", []):
        if isinstance(rec, dict):
            country = normalize(str(rec.get("country") or ""))
            if country and country not in dtaa_idx:
                dtaa_idx[country] = rec

    return {
        "indian": indian_idx,
        "foreign": foreign_idx,
        "party": banks_idx,
        "nature": nature_idx,
        "country": dtaa_idx,
    }


@functools.lru_cache(maxsize=1)
def _cached_indexes() -> Dict[str, Dict[str, Dict[str, Any]]]:
    return _build_indexes(load_master())


def resolve_name(raw: str, domain: Literal["indian", "foreign", "party", "nature", "country"]) -> str:
    canonical = normalize(raw)
    if not canonical:
        return ""

    aliases = load_aliases()
    alias_map_name = {
        "indian": "indian_company_aliases",
        "foreign": "foreign_party_aliases",
        "party": "party_bank_aliases",
        "nature": "nature_aliases",
        "country": "country_aliases",
    }[domain]
    alias_map = aliases.get(alias_map_name, {})
    if canonical in alias_map:
        return normalize(alias_map[canonical])
    return canonical


def find_indian_company(name: str) -> Optional[Dict[str, Any]]:
    key = resolve_name(name, "indian")
    return _cached_indexes()["indian"].get(key)


def find_foreign_company(name: str) -> Optional[Dict[str, Any]]:
    key = resolve_name(name, "foreign")
    return _cached_indexes()["foreign"].get(key)


def find_party_banks(party_name: str) -> List[Dict[str, Any]]:
    key = resolve_name(party_name, "party")
    rec = _cached_indexes()["party"].get(key)
    rows = rec.get("rows", []) if rec else []
    return [r for r in rows if isinstance(r, dict)]


def find_bank_by_name(bank_name: str, party_name: str = "") -> Optional[Dict[str, Any]]:
    bank_key = normalize(bank_name)
    if not bank_key:
        return None

    rows: List[Dict[str, Any]] = []
    if party_name:
        rows = find_party_banks(party_name)
    if not rows:
        master = load_master()
        for _, group_rows in (master.get("banks_by_party", {}) or {}).items():
            if isinstance(group_rows, list):
                rows.extend([r for r in group_rows if isinstance(r, dict)])

    for row in rows:
        name = normalize(str(row.get("bank_name") or ""))
        if name and (name == bank_key or bank_key in name or name in bank_key):
            return row
    return None


def find_nature_row(nature_text: str) -> Optional[Dict[str, Any]]:
    key = resolve_name(nature_text, "nature")
    return _cached_indexes()["nature"].get(key)


def find_dtaa(country_text: str) -> Optional[Dict[str, Any]]:
    key = resolve_name(country_text, "country")
    return _cached_indexes()["country"].get(key)


def safe_master_view(master: Dict[str, Any]) -> Dict[str, Any]:
    # Expose only non-sensitive sections used by this app.
    return {
        "indian_companies": master.get("indian_companies", []),
        "foreign_companies": master.get("foreign_companies", []),
        "banks_by_party": master.get("banks_by_party", {}),
        "nature_map": master.get("nature_map", []),
        "dtaa_rates": master.get("dtaa_rates", []),
        "reasons": master.get("reasons", []),
    }


def validate_pan(pan: str) -> bool:
    return bool(re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", (pan or "").strip().upper()))


def validate_bsr_code(bsr: str) -> bool:
    return bool(re.match(r"^\d{7}$", re.sub(r"\D", "", bsr or "")))


def validate_purpose_code(purpose: str) -> bool:
    p = (purpose or "").strip().upper()
    return bool(re.match(r"^RB-\d{2}\.\d(?:-S\d{4})?$", p))


def validate_dtaa_rate(rate: str) -> bool:
    s = (rate or "").strip()
    if not s:
        return False
    try:
        n = float(s)
    except ValueError:
        return False
    return 0 <= n <= 100


def mask_pan_for_log(pan: str) -> str:
    p = (pan or "").strip().upper()
    if len(p) != 10:
        return p
    return f"{p[:2]}******{p[-2:]}"


def classify_match(raw: str, resolved: str) -> str:
    return "alias_matched" if normalize(raw) != resolved else "matched"


def suggest_from_master(
    extracted: Dict[str, str],
    bank_code_lookup: Dict[str, str],
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    suggestions: Dict[str, str] = {}
    events: List[Dict[str, str]] = []

    remitter_input = extracted.get("NameRemitter", "")
    remitter = find_indian_company(remitter_input)
    if remitter:
        remitter_name = str(remitter.get("name") or "").strip()
        remitter_pan = str(remitter.get("pan") or "").strip().upper()
        if remitter_name:
            suggestions["NameRemitter"] = remitter_name
        if remitter_pan:
            suggestions["RemitterPAN"] = remitter_pan
        events.append(
            {
                "lookup_domain": "indian",
                "input": remitter_input,
                "resolved": remitter_name or remitter_input,
                "match_type": classify_match(remitter_input, resolve_name(remitter_input, "indian")),
                "source": "master.indian_companies",
            }
        )
    elif remitter_input:
        events.append(
            {
                "lookup_domain": "indian",
                "input": remitter_input,
                "resolved": "",
                "match_type": "not_found",
                "source": "master.indian_companies",
            }
        )

    remittee_input = extracted.get("NameRemittee", "")
    remittee = find_foreign_company(remittee_input)
    if remittee:
        remittee_name = str(remittee.get("name") or "").strip()
        if remittee_name:
            suggestions["NameRemittee"] = remittee_name
        events.append(
            {
                "lookup_domain": "foreign",
                "input": remittee_input,
                "resolved": remittee_name or remittee_input,
                "match_type": classify_match(remittee_input, resolve_name(remittee_input, "foreign")),
                "source": "master.foreign_companies",
            }
        )
    elif remittee_input:
        events.append(
            {
                "lookup_domain": "foreign",
                "input": remittee_input,
                "resolved": "",
                "match_type": "not_found",
                "source": "master.foreign_companies",
            }
        )

    party_seed = suggestions.get("NameRemitter") or extracted.get("NameRemitter", "")
    bank_rows = find_party_banks(party_seed)
    if bank_rows:
        primary = bank_rows[0]
        bank_name = str(primary.get("bank_name") or "").strip()
        bank_code = bank_code_lookup.get(normalize(bank_name), bank_name)
        if bank_code:
            suggestions["NameBankCode"] = bank_code
        if primary.get("branch"):
            suggestions["BranchName"] = str(primary["branch"]).strip()
        if primary.get("bsr_code"):
            suggestions["BsrCode"] = re.sub(r"\D", "", str(primary["bsr_code"]))
        events.append(
            {
                "lookup_domain": "party",
                "input": party_seed,
                "resolved": str(primary.get("bank_name") or ""),
                "match_type": classify_match(party_seed, resolve_name(party_seed, "party")),
                "source": "master.banks_by_party",
            }
        )
    elif party_seed:
        events.append(
            {
                "lookup_domain": "party",
                "input": party_seed,
                "resolved": "",
                "match_type": "not_found",
                "source": "master.banks_by_party",
            }
        )

    nature_seed = extracted.get("NatureRemCategory", "")
    nature_row = find_nature_row(nature_seed)
    if nature_row:
        agreement_nature = str(nature_row.get("agreement_nature") or "").strip()
        service_category = str(nature_row.get("service_category") or "").strip()
        purpose = str(nature_row.get("purpose_code") or "").strip()
        if agreement_nature:
            suggestions["NatureRemCategory"] = agreement_nature
        if service_category:
            suggestions["RevPurCategory"] = service_category
        if purpose:
            suggestions["RevPurCode"] = purpose
        events.append(
            {
                "lookup_domain": "nature",
                "input": nature_seed,
                "resolved": agreement_nature or nature_seed,
                "match_type": classify_match(nature_seed, resolve_name(nature_seed, "nature")),
                "source": "master.nature_map",
            }
        )
    elif nature_seed:
        events.append(
            {
                "lookup_domain": "nature",
                "input": nature_seed,
                "resolved": "",
                "match_type": "not_found",
                "source": "master.nature_map",
            }
        )

    country_seed = extracted.get("CountryRemMadeSecb") or extracted.get("RemitteeTownCityDistrict") or ""
    dtaa = find_dtaa(country_seed)
    if dtaa:
        country = str(dtaa.get("country") or "").strip()
        article = str(dtaa.get("article") or "").strip()
        rate = dtaa.get("rate")
        if country:
            suggestions["RelevantDtaa"] = country
        if article:
            suggestions["RelevantArtDtaa"] = article
        if rate is not None:
            try:
                suggestions["RateTdsADtaa"] = str(round(float(rate) * 100, 2)).rstrip("0").rstrip(".")
            except Exception:
                pass
        events.append(
            {
                "lookup_domain": "country",
                "input": country_seed,
                "resolved": country or country_seed,
                "match_type": classify_match(country_seed, resolve_name(country_seed, "country")),
                "source": "master.dtaa_rates",
            }
        )
    elif country_seed:
        events.append(
            {
                "lookup_domain": "country",
                "input": country_seed,
                "resolved": "",
                "match_type": "not_found",
                "source": "master.dtaa_rates",
            }
        )

    return suggestions, events
