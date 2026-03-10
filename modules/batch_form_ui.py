from __future__ import annotations

from typing import Dict, List
from datetime import date, datetime, timedelta

import streamlit as st

from modules.form15cb_constants import (
    CA_FIRM_OPTIONS,
    MODE_NON_TDS,
    MODE_TDS,
    PROPOSED_DATE_OFFSET_DAYS,
    REMITTEE_STATE,
    REMITTEE_ZIP_CODE,
    FIELD_MAX_LENGTH,
)
from modules.currency_mapping import load_currency_exact_index, resolve_currency_selection
from modules.invoice_calculator import recompute_invoice
from modules.logger import get_logger
from modules.master_lookups import (
    get_bank_options,
    get_country_options,
    get_currency_options,
    load_nature_options,
    load_purpose_grouped,
    match_remitter,
    resolve_bank_code,
    resolve_country_code,
    resolve_dtaa,
    split_dtaa_article_text,
)

logger = get_logger()


def _parse_iso_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _dtaa_rate_percent(raw: str) -> str:
    raw_s = str(raw or "").strip()
    if "i.t act" in raw_s.lower():
        return "it_act"
    try:
        return str(float(raw_s) * 100).rstrip("0").rstrip(".")
    except Exception:
        return ""


def _purpose_group_for_code(purpose_grouped: Dict[str, List[Dict[str, str]]], purpose_code: str) -> str:
    code = str(purpose_code or "").strip().upper()
    if not code:
        return ""
    for group_name, rows in purpose_grouped.items():
        for row in rows:
            if str(row.get("purpose_code") or "").strip().upper() == code:
                return group_name
    return ""


def _selectbox_index_from_value(options: List[str], value: str) -> int:
    if not options:
        return 0
    try:
        return options.index(value)
    except ValueError:
        return 0


def _apply_remitter_match(state: Dict[str, object], remitter_name: str) -> None:
    invoice_id = str(state.get("meta", {}).get("invoice_id") or "")
    form = state["form"]
    resolved = state["resolved"]
    rec = match_remitter(remitter_name)
    if rec:
        resolved["remitter_match"] = "1"
        resolved["pan"] = rec.get("pan", "")
        resolved["bank_name"] = rec.get("bank_name", "")
        resolved["branch"] = rec.get("branch", "")
        resolved["bsr"] = rec.get("bsr", "")
        resolved["bank_code"] = resolve_bank_code(rec.get("bank_name", ""))
        form["RemitterPAN"] = rec.get("pan", "")
        form["NameBankDisplay"] = rec.get("bank_name", "")
        form["NameBankCode"] = resolved["bank_code"]
        form["BranchName"] = rec.get("branch", "")
        form["BsrCode"] = rec.get("bsr", "")
        form["_lock_pan_bank_branch_bsr"] = "1"
        logger.info(
            "ui_remitter_match invoice_id=%s remitter_name=%s pan=%s bank=%s",
            invoice_id,
            remitter_name,
            rec.get("pan", ""),
            rec.get("bank_name", ""),
        )
    else:
        resolved["remitter_match"] = "0"
        form["_lock_pan_bank_branch_bsr"] = "0"
        logger.warning("ui_remitter_not_matched invoice_id=%s remitter_name=%s", invoice_id, remitter_name)


_AUTO_SPLIT_ADDRESS_FIELDS = {
    "RemitteePremisesBuildingVillage",
    "RemitteeFlatDoorBuilding",
    "RemitteeAreaLocality",
    "RemitteeRoadStreet",
    "RemitteeTownCityDistrict",
}


def check_field_length_warnings(form: Dict[str, str]) -> List[str]:
    """Identify user-editable fields that exceed their maximum allowed length.

    Address fields are excluded because they are automatically redistributed
    across adjacent fields by _redistribute_address_overflow.
    """
    warnings = []
    for field, max_len in FIELD_MAX_LENGTH.items():
        if field in _AUTO_SPLIT_ADDRESS_FIELDS:
            continue  # handled silently by address redistribution
        val = str(form.get(field) or "")
        if len(val) > max_len:
            warnings.append(
                f"⚠️ '{field}' is {len(val)} chars " f"(max {max_len}) — will be trimmed."
            )
    return warnings


def render_invoice_tab(state: Dict[str, object], *, show_header: bool = True, is_single_mode: bool = False) -> Dict[str, object]:
    meta = state["meta"]
    extracted = state["extracted"]
    form = state["form"]

    invoice_id = str(meta["invoice_id"])
    mode = str(meta.get("mode") or MODE_TDS)
    
    if show_header:
        st.markdown("### FORM NO. 15CB")
        st.caption("Certificate of an accountant")
        if not is_single_mode:
            st.caption(f"Mode: {'TDS' if mode == MODE_TDS else 'Non-TDS'}")

    # --- Extraction failure banner ---
    if meta.get("extraction_quality") == "failed":
        st.error(
            "⚠️ Automatic extraction failed for this invoice — the PDF format could not be read. "
            "Please fill in the fields manually. The amount and currency have been pre-filled where possible."
        )

    # --- Field Length Warnings ---
    length_warnings = check_field_length_warnings(form)
    if length_warnings:
        for w in length_warnings:
            st.warning(w)

    # ── Invoice Reference Section (at the TOP of the form) ──
    st.markdown("### 📄 Invoice Reference")
    col1, col2 = st.columns(2)
    with col1:
        invoice_number = st.text_input(
            "Invoice Number",
            value=form.get('InvoiceNumber', extracted.get('invoice_number', '')),
            key=f"{invoice_id}_invoice_number"
        )
        form['InvoiceNumber'] = invoice_number

    with col2:
        invoice_date = st.text_input(
            "Invoice Date",
            value=form.get('InvoiceDate', extracted.get('invoice_date_iso', '')),
            key=f"{invoice_id}_invoice_date",
            help="Format: YYYY-MM-DD"
        )
        form['InvoiceDate'] = invoice_date

    st.divider()

    extracted_currency = str(extracted.get("currency_short") or "").strip().upper()
    selected_currency = str(meta.get("source_currency_short") or "").strip().upper()
    currency_index = load_currency_exact_index()
    extracted_resolved = resolve_currency_selection(extracted_currency, currency_index)
    selected_resolved = resolve_currency_selection(selected_currency, currency_index)
    extracted_code = str(extracted_resolved.get("code") or "")
    selected_code = str(selected_resolved.get("code") or "")
    if extracted_currency and selected_currency and extracted_code and selected_code and extracted_code != selected_code:
        st.warning(
            f"Gemini detected {extracted_currency} from invoice but {selected_currency} was selected in Step 1. "
            "Please confirm which is correct."
        )
        logger.warning(
            "ui_currency_mismatch invoice_id=%s extracted_currency=%s selected_currency=%s",
            invoice_id,
            extracted_currency,
            selected_currency,
        )

    c1, c2 = st.columns(2)
    with c1:
        remitter_name = st.text_input(
            "Name of the Remitter",
            key=f"{invoice_id}_remitter_name",
            value=str(form.get("NameRemitterInput") or extracted.get("remitter_name") or ""),
        )
        form["NameRemitterInput"] = remitter_name
        _apply_remitter_match(state, remitter_name)
        lock = form.get("_lock_pan_bank_branch_bsr") == "1"
        # Allow PAN to be editable even when pre-filled/matched
        form["RemitterPAN"] = st.text_input(
            "PAN/TAN",
            key=f"{invoice_id}_pan",
            value=str(form.get("RemitterPAN") or ""),
            disabled=False,
        )
        _addr_raw = str(form.get("RemitterAddress") or extracted.get("remitter_address") or "")
        _name_prefix = remitter_name.upper().rstrip(". ")
        if _name_prefix and _addr_raw.upper().lstrip().startswith(_name_prefix):
            _addr_raw = _addr_raw[len(_name_prefix):].lstrip(" .,;:-").strip()
        form["RemitterAddress"] = st.text_input(
            "Remitter Address (as per invoice, appended to name in XML)",
            key=f"{invoice_id}_remitter_addr",
            value=_addr_raw,
        )
    with c2:
        form["NameRemitteeInput"] = st.text_input(
            "Name of the Beneficiary",
            key=f"{invoice_id}_benef_name",
            value=str(form.get("NameRemitteeInput") or extracted.get("beneficiary_name") or ""),
        )
        form["RemitteeFlatDoorBuilding"] = st.text_input(
            "Flat / Door / Building",
            key=f"{invoice_id}_remittee_flat",
            value=str(form.get("RemitteeFlatDoorBuilding") or ""),
        )
        form["RemitteeAreaLocality"] = st.text_input(
            "Area / Locality",
            key=f"{invoice_id}_remittee_area",
            value=str(form.get("RemitteeAreaLocality") or ""),
        )
        form["RemitteeTownCityDistrict"] = st.text_input(
            "Town / City / District",
            key=f"{invoice_id}_remittee_town",
            value=str(form.get("RemitteeTownCityDistrict") or ""),
        )
        # Make state and zip editable so users can override defaults if needed
        st.text_input("State", value=REMITTEE_STATE, disabled=False, key=f"{invoice_id}_remittee_state")
        st.text_input("ZIP Code", value=REMITTEE_ZIP_CODE, disabled=False, key=f"{invoice_id}_remittee_zip")

    st.markdown("#### Section B - Remittance")
    b1, b2, b3 = st.columns(3)
    country_opts = get_country_options()
    country_labels = ["SELECT"] + [f"{k} ({v})" for k, v in country_opts]
    current_country_code = str(form.get("CountryRemMadeSecb") or "")
    country_idx = 0
    for i, (_, code) in enumerate(country_opts):
        if code == current_country_code:
            country_idx = i + 1
            break
    with b1:
        country_sel = st.selectbox("Country to which remittance is made", country_labels or [""], index=country_idx, key=f"{invoice_id}_country")
        if country_sel and country_sel != "SELECT":
            country_name, country_code = country_sel.rsplit("(", 1)
            code = country_code.replace(")", "").strip()
            form["CountryRemMadeSecb"] = code
            form["RemitteeCountryCode"] = code
            form["_manual_dtaa_rate_required"] = "0"
            dtaa = resolve_dtaa(country_name.strip())
            if dtaa:
                dtaa_without_article, dtaa_with_article = split_dtaa_article_text(str(dtaa.get("dtaa_applicable") or ""))
                form["RelevantDtaa"] = dtaa_without_article
                form["RelevantArtDtaa"] = dtaa_with_article
                percent_raw = _dtaa_rate_percent(dtaa.get("percentage", ""))
                if percent_raw == "it_act":
                    form["dtaa_mode"] = "it_act"
                    state["resolved"]["dtaa_rate_percent"] = ""
                    form["RateTdsADtaa"] = ""
                    form["ArtDtaa"] = dtaa_with_article
                    # Force recompute to use IT Act rate
                    recompute_invoice(state)
                elif percent_raw:
                    form["dtaa_mode"] = "dtaa_rate"
                    # Persist DTAA rate into canonical places used by recompute and XML
                    state["resolved"]["dtaa_rate_percent"] = percent_raw
                    form["RateTdsADtaa"] = percent_raw
                    form["RateTdsSecB"] = percent_raw
                    form["dtaa_rate"] = percent_raw
                    form["ArtDtaa"] = dtaa_with_article
                    # Sync to session_state so Rate field widget displays it immediately
                    st.session_state[f"{invoice_id}_rate_tds_dtaa"] = str(percent_raw)
                    # Ensure recompute runs with updated state immediately
                    try:
                        recompute_invoice(state)
                    except Exception:
                        logger.exception("recompute_failed_after_dtaa_write invoice_id=%s", invoice_id)
                else:
                    form["dtaa_mode"] = ""
                    state["resolved"]["dtaa_rate_percent"] = ""
                    form["RateTdsADtaa"] = str(form.get("RateTdsADtaa") or "")
                    form["_manual_dtaa_rate_required"] = "1"
                    st.warning("No DTAA rate found for selected country. Please enter DTAA rate manually to proceed.")
                    logger.warning("ui_dtaa_rate_missing invoice_id=%s country=%s", invoice_id, country_name.strip())
                logger.info(
                    "ui_country_selected invoice_id=%s country_code=%s dtaa_article=%s dtaa_rate=%s",
                    invoice_id,
                    code,
                    form.get("RelevantDtaa", ""),
                    form.get("RateTdsADtaa", ""),
                )
            else:
                state["resolved"]["dtaa_rate_percent"] = ""
                form["RateTdsADtaa"] = str(form.get("RateTdsADtaa") or "")
                form["_manual_dtaa_rate_required"] = "1"
                form["RelevantDtaa"] = str(form.get("RelevantDtaa") or "NOT AVAILABLE")
                form["RelevantArtDtaa"] = str(form.get("RelevantArtDtaa") or "NOT AVAILABLE")
                form["ArtDtaa"] = str(form.get("ArtDtaa") or "NOT AVAILABLE")
                st.warning("No DTAA data found for selected country. Please enter DTAA rate manually to proceed.")
                logger.warning(
                    "ui_country_selected_no_dtaa invoice_id=%s country_code=%s",
                    invoice_id,
                    code,
                )
        elif mode == MODE_TDS:
            form["_manual_dtaa_rate_required"] = "0"
            st.error("Country required - select to enable tax calculations.")
            logger.warning("ui_country_not_selected invoice_id=%s", invoice_id)
    if mode == MODE_TDS and str(form.get("CountryRemMadeSecb") or "") == "91":
        st.warning("Detected remittance country as INDIA in TDS/outward flow. Verify remitter/beneficiary role assignment.")
        logger.warning("ui_country_india_tds invoice_id=%s country_code=91", invoice_id)
    currency_opts = get_currency_options()
    currency_labels = ["-- Select Currency --"] + [f"{k} ({v})" for k, v in currency_opts]
    current_currency_code = str(form.get("CurrencySecbCode") or "")
    currency_idx = 0  # defaults to "-- Select Currency --" placeholder
    if current_currency_code:
        for i, (_, code) in enumerate(currency_opts):
            if code == current_currency_code:
                currency_idx = i + 1  # +1 because placeholder is at index 0
                break
    with b2:
        currency_sel = st.selectbox("Currency", currency_labels, index=currency_idx, key=f"{invoice_id}_currency")
        if currency_sel and currency_sel != "-- Select Currency --":
            form["CurrencySecbCode"] = currency_sel.rsplit("(", 1)[1].replace(")", "").strip()
        else:
            st.warning("⚠️ Currency not detected — please select manually.")
    with b3:
        form["AmtPayForgnRem"] = st.text_input(
            "Amount in Foreign Currency",
            key=f"{invoice_id}_fcy_amt",
            value=str(form.get("AmtPayForgnRem") or ""),
        )
        # Allow INR amount to be editable so CA can correct computed conversion.
        # Write result back to form so the user-corrected value flows into the XML.
        form["AmtPayIndRem"] = st.text_input(
            "Amount in Indian ₹",
            key=f"{invoice_id}_inr_amt",
            value=str(form.get("AmtPayIndRem") or ""),
            disabled=False,
        )
        if float(meta.get("exchange_rate") or 0.0) == 0:
            st.warning("⚠️ Exchange rate not found in Excel. Please enter INR amount manually.")
        try:
            # Prefer any existing form value, otherwise default to today + configured offset.
            prop_default = date.fromisoformat(str(form.get("PropDateRem") or ""))
        except Exception:
            prop_default = date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)
        form["PropDateRem"] = st.date_input(
            "Proposed Date of Remittance",
            value=prop_default,
            key=f"{invoice_id}_prop_date",
        ).isoformat()

    bank_lock = form.get("_lock_pan_bank_branch_bsr") == "1"
    bank_opts = get_bank_options()
    bank_labels = [f"{k} ({v})" for k, v in bank_opts] + ["OTHER BANK"]
    cur_bank_code = str(form.get("NameBankCode") or "")
    b_idx = len(bank_labels) - 1
    for i, (_, code) in enumerate(bank_opts):
        if code == cur_bank_code:
            b_idx = i
            break
    # Always allow bank selection/editing
    bank_sel = st.selectbox("Name of Bank", bank_labels, index=b_idx, key=f"{invoice_id}_bank_dropdown")
    if bank_sel != "OTHER BANK":
        bank_name, bank_code = bank_sel.rsplit("(", 1)
        form["NameBankDisplay"] = bank_name.strip()
        form["NameBankCode"] = bank_code.replace(")", "").strip()
    else:
        form["NameBankDisplay"] = st.text_input(
            "Other Bank (manual)",
            key=f"{invoice_id}_bank_display",
            value=str(form.get("NameBankDisplay") or ""),
            disabled=False,
        )
        if not bank_lock:
            form["NameBankCode"] = resolve_bank_code(str(form.get("NameBankDisplay") or ""))
    # Branch and BSR should always be editable to allow overrides
    form["BranchName"] = st.text_input("Branch", key=f"{invoice_id}_branch", value=str(form.get("BranchName") or ""), disabled=False)
    form["BsrCode"] = st.text_input("BSR Code", key=f"{invoice_id}_bsr", value=str(form.get("BsrCode") or ""), disabled=False)

    nature_opts = [n for n in load_nature_options() if n.get("code") != "-1"]
    nature_labels = ["SELECT"] + [f"{n['code']} - {n['label']}" for n in nature_opts]
    nature_label_to_code = {str(n.get("label") or "").strip(): str(n.get("code") or "").strip() for n in nature_opts}
    nature_key = f"{invoice_id}_nature"
    current_nature = str(form.get("NatureRemCategory") or "").strip()
    if not current_nature and extracted.get("nature_of_remittance"):
        mapped_nature = nature_label_to_code.get(str(extracted.get("nature_of_remittance") or "").strip(), "")
        if mapped_nature:
            current_nature = mapped_nature
            form["NatureRemCategory"] = mapped_nature
    desired_nature_label = "SELECT"
    if current_nature:
        for n in nature_opts:
            if str(n.get("code") or "").strip() == current_nature:
                desired_nature_label = f"{n['code']} - {n['label']}"
                break
        if desired_nature_label == "SELECT":
            logger.warning("ui_nature_stale_value invoice_id=%s nature_code=%s", invoice_id, current_nature)
    n_idx = _selectbox_index_from_value(nature_labels, desired_nature_label)
    if nature_key in st.session_state and str(st.session_state.get(nature_key)) not in nature_labels:
        del st.session_state[nature_key]
    nature_sel = st.selectbox("Nature of Remittance", nature_labels or [""], index=n_idx if nature_labels else 0, key=nature_key)
    if nature_sel and nature_sel != "SELECT":
        form["NatureRemCategory"] = nature_sel.split(" - ", 1)[0].strip()

    purpose_grouped = load_purpose_grouped()
    groups = ["SELECT"] + sorted(purpose_grouped.keys())
    group_key = f"{invoice_id}_pur_group"
    code_key = f"{invoice_id}_pur_code"
    current_code = str(form.get("_purpose_code") or extracted.get("purpose_code") or "").strip().upper()
    current_group = str(form.get("_purpose_group") or extracted.get("purpose_group") or "").strip()
    if not current_group and current_code:
        current_group = _purpose_group_for_code(purpose_grouped, current_code)
        if current_group:
            form["_purpose_group"] = current_group
    if current_group and current_group not in purpose_grouped:
        logger.warning("ui_purpose_group_stale_value invoice_id=%s purpose_group=%s", invoice_id, current_group)
        current_group = ""
    desired_group = current_group if current_group else "SELECT"
    g_idx = _selectbox_index_from_value(groups, desired_group)
    if group_key in st.session_state and str(st.session_state.get(group_key)) not in groups:
        del st.session_state[group_key]
    group_sel = st.selectbox("Purpose Group", groups or [""], index=g_idx if groups else 0, key=group_key)
    form["_purpose_group"] = group_sel if group_sel != "SELECT" else ""
    rows = purpose_grouped.get(group_sel if group_sel != "SELECT" else "", [])
    row_labels = ["SELECT"] + [f"{r['purpose_code']} - {r['description']}" for r in rows]
    code_choices = {str(r.get("purpose_code") or "").strip().upper(): f"{r['purpose_code']} - {r['description']}" for r in rows}
    desired_code_label = code_choices.get(current_code, "SELECT")
    if current_code and desired_code_label == "SELECT":
        logger.warning(
            "ui_purpose_code_not_in_group invoice_id=%s purpose_code=%s purpose_group=%s",
            invoice_id,
            current_code,
            group_sel,
        )
    r_idx = _selectbox_index_from_value(row_labels, desired_code_label)
    if code_key in st.session_state and str(st.session_state.get(code_key)) not in row_labels:
        del st.session_state[code_key]
    row_sel = st.selectbox("Purpose Code", row_labels or [""], index=r_idx if row_labels else 0, key=code_key)
    if row_sel and row_sel != "SELECT":
        p_code = row_sel.split(" - ", 1)[0]
        form["_purpose_code"] = p_code
        chosen = next((r for r in rows if r.get("purpose_code") == p_code), None)
        if chosen:
            gr_val = chosen.get("gr_no")
            gr = str(gr_val) if gr_val is not None else "00"
            # Strip leading zeros to match government utility format (e.g. "09" → "9")
            gr = str(int(gr)) if gr.isdigit() else gr
            form["RevPurCategory"] = f"RB-{gr}.1"
            form["RevPurCode"] = f"RB-{gr}.1-{p_code}"
    else:
        form["_purpose_code"] = ""

    # In single mode the global control drives the value — sync form field here
    # so the display always reflects meta["is_gross_up"].
    if is_single_mode:
        form["TaxPayGrossSecb"] = "Y" if meta.get("is_gross_up") else "N"

    # Clear any stale session-state so the value= parameter takes effect cleanly
    # (avoids "created with a default value but also had its value set via the
    # Session State API" warning that fires when both are set simultaneously).
    st.session_state.pop(f"{invoice_id}_grossed_up_disp", None)

    # Grossed up Tax display (Read-only status display) — always sourced from meta
    # so it cannot be stale even if form["TaxPayGrossSecb"] was overwritten elsewhere.
    gross_up_display = "Y" if meta.get("is_gross_up") else "N"
    st.text_input(
        "Grossed up Tax",
        value=gross_up_display,
        disabled=True,
        key=f"{invoice_id}_grossed_up_disp"
    )

    mode_is_tds = mode == MODE_TDS
    # Editable Gross-up checkbox in the form (HIDDEN in single mode because it is at the top)
    current_gross_val = str(form.get("TaxPayGrossSecb") or "N").strip().upper() == "Y"
    if not is_single_mode:
        new_gross_val = st.checkbox(
            "Gross-up tax (company bears tax)",
            value=current_gross_val,
            key=f"{invoice_id}_gross_checkbox",
            disabled=(not mode_is_tds),
        )
        form["TaxPayGrossSecb"] = "Y" if new_gross_val else "N"
        meta["is_gross_up"] = new_gross_val
    else:
        # In single mode, the value comes from the global control at the top,
        # which is already synced into form["TaxPayGrossSecb"] by app.py
        pass
    form["RemittanceCharIndia"] = "Y"  # Remittance is chargeable in India per IT Act in both modes
    form["ReasonNot"] = st.text_input("Reason if not chargeable", key=f"{invoice_id}_reason_not", value=str(form.get("ReasonNot") or ""), disabled=mode_is_tds)
    form["RateTdsSecbFlg"] = "2" if mode_is_tds else ""
    form["RemForRoyFlg"] = "Y" if mode_is_tds else "N"
    form["OtherRemDtaa"] = "N" if mode_is_tds else "Y"
    form["RelArtDetlDDtaa"] = "NOT APPLICABLE" if mode_is_tds else str(form.get("RelArtDetlDDtaa") or "")
    form["NameFirmAcctnt"] = st.selectbox(
        "Firm / Proprietorship",
        CA_FIRM_OPTIONS,
        index=0 if str(form.get("NameFirmAcctnt") or CA_FIRM_OPTIONS[0]) == CA_FIRM_OPTIONS[0] else 1,
        key=f"{invoice_id}_firm",
    )

    if mode_is_tds:
        dtaa_mode = str(form.get("dtaa_mode") or "")
        country_name_disp = "selected country"
        # Try to recover country name for the info box if it's it_act mode
        if dtaa_mode == "it_act":
            country_label = st.session_state.get(f"{invoice_id}_country", "")
            if "(" in country_label:
                country_name_disp = country_label.rsplit("(", 1)[0].strip()

        if dtaa_mode == "it_act":
            st.info(
                f"ℹ️ **{country_name_disp} DTAA ({form.get('RelevantArtDtaa', 'Other Income')})** does not provide a reduced rate. "
                "Tax will be deducted at **IT Act rate (21.84%)** as per treaty provisions."
            )
        
        # show DTAA article and rate side-by-side so CA can verify both values
        art_col, rate_col = st.columns(2)
        with art_col:
            form["RelevantArtDtaa"] = st.text_input(
                "Relevant Article of DTAA",
                value=str(form.get("RelevantArtDtaa") or ""),
                key=f"{invoice_id}_relevant_art",
                disabled=False,
            )
        with rate_col:
            rate_key = f"{invoice_id}_rate_tds_dtaa"
            if dtaa_mode == "it_act":
                st.text_input("Rate of TDS per DTAA (%)", value="As per IT Act", disabled=True, key=f"{rate_key}_disabled")
                form["RateTdsADtaa"] = ""
                state["resolved"]["dtaa_rate_percent"] = ""
            else:
                if rate_key not in st.session_state:
                    st.session_state[rate_key] = str(form.get("RateTdsADtaa") or state.get("resolved", {}).get("dtaa_rate_percent") or "")
                form["RateTdsADtaa"] = st.text_input(
                    "Rate of TDS per DTAA (%)",
                    key=rate_key,
                )
                if str(form.get("_manual_dtaa_rate_required") or "") == "1":
                    st.caption("No DTAA data/rate found for selected country. Enter DTAA rate (%) manually to compute tax fields.")
                state["resolved"]["dtaa_rate_percent"] = str(form.get("RateTdsADtaa") or "").strip()
            
            if state["resolved"]["dtaa_rate_percent"]:
                logger.info("ui_rate_entered invoice_id=%s rate=%s", invoice_id, state["resolved"]["dtaa_rate_percent"])
            elif dtaa_mode != "it_act":
                logger.warning("ui_rate_missing invoice_id=%s", invoice_id)
        # date remains outside the two-column row so it spans full width
        dedn_key = f"{invoice_id}_dedn_date"
        dedn_default = _parse_iso_date(str(form.get("DednDateTds") or ""))
        if dedn_default is None:
            if dedn_key in st.session_state:
                del st.session_state[dedn_key]
            form["DednDateTds"] = ""
            st.warning("Deduction Date (Posting Date) missing or invalid; XML generation is blocked until corrected.")
            manual_key = f"{invoice_id}_dedn_date_manual"
            manual_value = st.text_input(
                "Date of deduction of TDS (YYYY-MM-DD)",
                value=str(form.get("DednDateTds") or ""),
                key=manual_key,
            ).strip()
            if _parse_iso_date(manual_value):
                form["DednDateTds"] = manual_value
        else:
            form["DednDateTds"] = st.date_input(
                "Date of deduction of TDS",
                value=dedn_default,
                key=dedn_key,
            ).isoformat()
    else:
        _active_rate_mode = str(form.get("NonTdsBasisRateMode") or "dtaa")
        _dtaa_rate_info = str(state.get("resolved", {}).get("dtaa_rate_percent") or "").strip()
        if _active_rate_mode == "it_act_2080":
            _rate_info = "20.80% (IT Act)"
        elif _dtaa_rate_info:
            _rate_info = f"DTAA Rate ({_dtaa_rate_info}%)"
        else:
            _rate_info = "DTAA Rate (fallback 20.80% — no treaty rate found)"
        st.caption(f"Non-TDS mode — documentation rate: **{_rate_info}**. No TDS deducted under DTAA.")

        form["NatureRemDtaa"] = st.text_input(
            "Nature of Remittance (as per agreement / DTAA)",
            value=str(form.get("NatureRemDtaa") or ""),
            key=f"{invoice_id}_nature_rem_dtaa",
            help="Describes the remittance type (e.g. 'SOFTWARE LICENSE'). Goes into NatureRemDtaa XML field.",
        )
        form["RelArtDetlDDtaa"] = st.text_area(
            "DTAA Exemption Comment",
            value=str(form.get("RelArtDetlDDtaa") or ""),
            key=f"{invoice_id}_rel_art_detl_dtaa",
            help="Explains why no TDS is deducted under the tax treaty. Goes into RelArtDetlDDtaa XML field.",
            height=100,
        )
        if not str(form.get("RelArtDetlDDtaa") or "").strip():
            st.warning("DTAA Exemption Comment is required for Non-TDS mode. It will be auto-filled on save.")

    state = recompute_invoice(state)
    logger.info(
        "ui_recompute_done invoice_id=%s snapshot=%s",
        invoice_id,
        {
            "RemitterPAN": form.get("RemitterPAN", ""),
            "CountryRemMadeSecb": form.get("CountryRemMadeSecb", ""),
            "RateTdsADtaa": form.get("RateTdsADtaa", ""),
            "TaxLiablIt": form.get("TaxLiablIt", ""),
            "AmtPayForgnTds": form.get("AmtPayForgnTds", ""),
        },
    )
    if not mode_is_tds:
        # Sync computed IT Act values for non-TDS mode (calculated for documentation)
        st.session_state[f"{invoice_id}_amt_inc_chrg_it"] = str(form.get("AmtIncChrgIt") or "")
        st.session_state[f"{invoice_id}_tax_liab_it"] = str(form.get("TaxLiablIt") or "")
        st.session_state[f"{invoice_id}_nature_rem_dtaa"] = str(form.get("NatureRemDtaa") or "")
        st.session_state[f"{invoice_id}_rel_art_detl_dtaa"] = str(form.get("RelArtDetlDDtaa") or "")
    if mode_is_tds:
        # Sync computed values to session_state so display widgets read current values, not stale cache
        st.session_state[f"{invoice_id}_amt_inc_chrg_it"] = str(form.get("AmtIncChrgIt") or "")
        st.session_state[f"{invoice_id}_tax_liab_it"] = str(form.get("TaxLiablIt") or "")
        st.session_state[f"{invoice_id}_tax_inc_dtaa"] = str(form.get("TaxIncDtaa") or "")
        st.session_state[f"{invoice_id}_tax_liab_dtaa"] = str(form.get("TaxLiablDtaa") or "")
        st.session_state[f"{invoice_id}_amt_tds_fcy"] = str(form.get("AmtPayForgnTds") or "")
        st.session_state[f"{invoice_id}_amt_tds_inr"] = str(form.get("AmtPayIndianTds") or "")
        st.session_state[f"{invoice_id}_rate_tds_secb"] = str(form.get("RateTdsSecB") or "")
        st.session_state[f"{invoice_id}_actl_amt_tds_forgn"] = str(form.get("ActlAmtTdsForgn") or "")
        st.session_state[f"{invoice_id}_basis_tax"] = str(form.get("BasisDeterTax") or "")
        st.markdown("#### TDS Computation")
        d1, d2, d3 = st.columns(3)
        with d1:
            # Use clearer label for gross-up tax
            it_tax_label = "Grossed up Tax (INR)" if form.get("TaxPayGrossSecb") == "Y" else "Tax liability under IT Act (INR)"
            st.text_input("Amount of income chargeable (INR)", disabled=False, key=f"{invoice_id}_amt_inc_chrg_it")
            st.text_input(it_tax_label, disabled=False, key=f"{invoice_id}_tax_liab_it")
            st.text_input("Taxable income per DTAA (INR)", disabled=False, key=f"{invoice_id}_tax_inc_dtaa")
            st.text_input("Tax liability per DTAA (INR)", disabled=False, key=f"{invoice_id}_tax_liab_dtaa")
        with d2:
            st.text_input("TDS Amount (foreign)", disabled=False, key=f"{invoice_id}_amt_tds_fcy")
            st.text_input("TDS Amount (INR)", disabled=False, key=f"{invoice_id}_amt_tds_inr")
            st.text_input("TDS Rate % (Section B)", disabled=is_single_mode, key=f"{invoice_id}_rate_tds_secb")
        with d3:
            st.text_input("Actual remittance after TDS (foreign)", disabled=False, key=f"{invoice_id}_actl_amt_tds_forgn")
            st.text_area("Basis of determining tax", disabled=False, key=f"{invoice_id}_basis_tax", height=80)
    return state
