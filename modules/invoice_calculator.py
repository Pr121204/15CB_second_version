from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Optional

from modules.form15cb_constants import (
    ASSESSMENT_YEAR,
    CA_DEFAULTS,
    FORM_DESCRIPTION,
    FORM_NAME,
    FORM_VER,
    HONORIFIC_M_S,
    INC_LIAB_INDIA_ALWAYS,
    INTERMEDIARY_CITY,
    IOR_WE_CODE,
    IT_ACT_BASIS,
    IT_ACT_RATE_DEFAULT,
    IT_ACT_RATES,
    MODE_NON_TDS,
    MODE_TDS,
    FIELD_MAX_LENGTH,
    NAME_REMITTEE_DATE_FORMAT,
    PROPOSED_DATE_OFFSET_DAYS,
    RATE_TDS_SECB_FLG_DTAA,
    RATE_TDS_SECB_FLG_IT_ACT,
    RATE_TDS_SECB_FLG_TDS,
    REMITTEE_STATE,
    REMITTEE_ZIP_CODE,
    SCHEMA_VER,
    SEC_REM_COVERED_DEFAULT,
    SW_CREATED_BY,
    SW_VERSION_NO,
    TAX_IND_DTAA_ALWAYS,
    TAX_RESID_CERT_Y,
    XML_CREATED_BY,
)
from modules.logger import get_logger
from modules.master_lookups import split_dtaa_article_text


logger = get_logger()


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw or "").strip())
    except Exception:
        return None


def _parse_date(raw: str) -> Optional[date]:
    t = str(raw or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def format_dotted_date(raw: str) -> str:
    d = _parse_date(raw)
    if not d:
        return str(raw or "").strip()
    return d.strftime(NAME_REMITTEE_DATE_FORMAT)


def _fmt_num(n: Optional[float]) -> str:
    if n is None:
        return ""
    return str(int(n)) if float(n).is_integer() else f"{n:.2f}".rstrip("0").rstrip(".")


def _round_to_int(value: float) -> int:
    try:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return int(round(value))


def _is_integer_rate(value: float | None) -> bool:
    if value is None:
        return False
    return abs(value - round(value)) < 1e-9


def _build_name_remittee(beneficiary: str, invoice_no: str, dotted_date: str) -> str:
    b = str(beneficiary or "").strip().upper()
    inv = str(invoice_no or "").strip()
    d = str(dotted_date or "").strip()
    if b and inv and d:
        return f"{b} INVOICE NO. {inv} DT {d}"
    if b and inv:
        return f"{b} INVOICE NO. {inv}"
    if b and d:
        return f"{b} DT {d}"
    return b


def get_effective_it_rate(rate: float | None = None) -> tuple[float, str]:
    """Return (effective_rate_percent, basis_text) for a user-selected IT Act rate.

    If *rate* is ``None`` the default rate (21.84%) is used.
    """
    if rate is None:
        rate = IT_ACT_RATE_DEFAULT
    basis = IT_ACT_BASIS.get(
        rate,
        f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
        f"AND TAX LIABILITY IS CALCULATED AT {rate} PERCENTAGE OF ABOVE.",
    )
    return rate, basis


def recompute_invoice(state: Dict[str, object]) -> Dict[str, object]:
    meta = state.setdefault("meta", {})
    extracted = state.setdefault("extracted", {})
    form = state.setdefault("form", {})
    resolved = state.setdefault("resolved", {})
    computed = state.setdefault("computed", {})

    mode = str(meta.get("mode") or MODE_TDS)
    invoice_id = str(meta.get("invoice_id") or "")
    exchange_rate = _to_float(str(meta.get("exchange_rate") or "")) or 0.0
    fcy = _to_float(str(form.get("AmtPayForgnRem") or extracted.get("amount") or "")) or 0.0
    inr_exact = fcy * exchange_rate
    inr = float(_round_to_int(inr_exact))
    computed["inr_amount"] = str(int(inr))
    form["AmtPayIndRem"] = computed["inr_amount"]
    if not form.get("AmtPayForgnRem"):
        form["AmtPayForgnRem"] = _fmt_num(fcy)

    prop = date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)
    form.setdefault("PropDateRem", prop.isoformat())

    form["RemitteeZipCode"] = REMITTEE_ZIP_CODE
    form["RemitteeState"] = REMITTEE_STATE
    form.setdefault("SecRemCovered", SEC_REM_COVERED_DEFAULT)
    # Keep consistent with government utility output even for gross-up mode.
    form["TaxPayGrossSecb"] = "N"
    form.setdefault("TaxResidCert", TAX_RESID_CERT_Y)
    # Income chargeable should mirror INR equivalent in XML output.
    form["AmtIncChrgIt"] = computed["inr_amount"]

    # Read canonical DTAA rate from form first (editable by user/tests), then resolved fallback
    dtaa_rate_percent = _to_float(
        str(
            form.get("RateTdsADtaa")
            or form.get("dtaa_rate")
            or resolved.get("dtaa_rate_percent")
            or ""
        )
    )
    computed["dtaa_rate_percent"] = _fmt_num(dtaa_rate_percent) if dtaa_rate_percent is not None else ""
    
    # Convert key values to Decimal for precise calculations early to avoid NameErrors in logs
    invoice_fcy = Decimal(str(fcy))
    invoice_inr_exact = Decimal(str(inr_exact))
    invoice_inr = Decimal(str(inr)) # Rounded INR amount
    exchange_rate_dec = Decimal(str(exchange_rate))

    logger.info(
        "recompute_start invoice_id=%s mode=%s fcy=%s inr=%s fx=%s dtaa_rate=%s",
        invoice_id,
        mode,
        _fmt_num(fcy),
        computed["inr_amount"],
        _fmt_num(exchange_rate),
        computed["dtaa_rate_percent"],
    )
    
    if exchange_rate == 0 and fcy > 0:
        logger.warning(
            "recompute_fx_missing invoice_id=%s fcy=%s currency=%r reason=currency_blank_cannot_lookup_excel action=inr_set_to_zero_pending_manual_entry",
            invoice_id, fcy, meta.get("source_currency_short") or ""
        )

    is_gross_up = bool(meta.get("is_gross_up", False))

    # ── Read user-selected IT Act rate ────────────────────────────────────
    raw_rate = _to_float(form.get("ItActRateSelected"))
    if raw_rate not in IT_ACT_RATES:
        selected_it_rate = IT_ACT_RATE_DEFAULT
    else:
        selected_it_rate = raw_rate
    form["ItActRateSelected"] = str(selected_it_rate)

    # --- PRIORITY 1: GROSS-UP FLOW ---
    if mode == MODE_TDS and is_gross_up:
        effective_rate, basis_text = get_effective_it_rate(selected_it_rate)
        # R is the percentage
        r = Decimal(str(effective_rate))

        if r < 100:
            # 1. GrossINR_exact = NetINR * 100 / (100 - R)
            gross_inr_exact = invoice_inr_exact * Decimal("100") / (Decimal("100") - r)
            # 2. Round Gross INR to nearest rupee
            gross_inr_rounded = gross_inr_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            # 3. TDSINR_exact = GrossINR_rounded * R / 100
            tds_inr_exact = gross_inr_rounded * r / Decimal("100")
            # 4. TDSINR_rounded = nearest rupee
            tds_inr_rounded = tds_inr_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            # 5. TDS_FCY = TDSINR_exact / FX (rounded 2dp)
            if exchange_rate_dec > 0:
                tds_fcy = (tds_inr_exact / exchange_rate_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                tds_fcy = Decimal("0.00")

            # AmtIncChrgIt should align to the actual GROSSED UP INR amount.
            form["AmtIncChrgIt"] = str(int(gross_inr_rounded))
            form["TaxLiablIt"] = str(int(tds_inr_rounded))
            form["AmtPayIndianTds"] = str(int(tds_inr_rounded))
            form["TaxPayGrossSecb"] = "Y"
            
            # FCY amounts format with exactly 2 decimal places where needed
            form["AmtPayForgnTds"] = f"{tds_fcy:.2f}"
            actual_fcy = max(invoice_fcy - tds_fcy, Decimal("0"))
            form["ActlAmtTdsForgn"] = _fmt_num(float(actual_fcy))
            
            form["BasisDeterTax"] = basis_text
            form["RateTdsSecB"] = "{:.2f}".format(effective_rate)
            form.setdefault("RateTdsSecbFlg", RATE_TDS_SECB_FLG_TDS)
            form.setdefault("RemittanceCharIndia", "Y")
            form["OtherRemDtaa"] = "Y"

            # Clear DTAA fallback paths entirely since Gross-up implies IT Act exclusively
            form["TaxIncDtaa"] = ""
            form["TaxLiablDtaa"] = ""
            form["RateTdsADtaa"] = ""

            logger.info(
                "recompute_gross_up_done invoice_id=%s rate=%s net_inr_exact=%s gross_inr_rounded=%s tds_inr_exact=%s tds_fcy=%s",
                invoice_id,
                effective_rate,
                invoice_inr_exact,
                gross_inr_rounded,
                tds_inr_exact,
                tds_fcy,
            )

    elif mode == MODE_TDS and (dtaa_rate_percent is not None or form.get("dtaa_mode") == "it_act"):
        it_factor, it_basis = get_effective_it_rate(selected_it_rate)

        dtaa_mode = form.get("dtaa_mode")
        if dtaa_mode == "it_act":
            dtaa_claimed = False
            applied_rate_dec = Decimal(str(it_factor))
        else:
            dtaa_rate_dec = Decimal(str(dtaa_rate_percent))
            it_rate_dec = Decimal(str(it_factor))
            dtaa_claimed = _is_integer_rate(float(dtaa_rate_dec)) and dtaa_rate_dec <= it_rate_dec
            applied_rate_dec = dtaa_rate_dec if dtaa_claimed else it_rate_dec

        it_rate_dec = Decimal(str(it_factor))
        it_liab = invoice_inr * (it_rate_dec / Decimal("100"))
        dtaa_liab = invoice_inr * (applied_rate_dec / Decimal("100")) if dtaa_claimed else Decimal("0")
        tds_fcy_dec = invoice_fcy * (applied_rate_dec / Decimal("100"))
        tds_inr_dec = invoice_inr * (applied_rate_dec / Decimal("100"))
        actual_fcy = max(invoice_fcy - tds_fcy_dec, Decimal("0"))

        # INR tax amounts should be whole rupees (rounded)
        form["AmtIncChrgIt"] = _fmt_num(_round_to_int(float(invoice_inr)))
        form["TaxLiablIt"] = _fmt_num(_round_to_int(float(it_liab)))
        if dtaa_claimed:
            form["TaxIncDtaa"] = _fmt_num(_round_to_int(float(invoice_inr)))
            form["TaxLiablDtaa"] = _fmt_num(_round_to_int(float(dtaa_liab)))
            form["RateTdsADtaa"] = str(int(round(float(applied_rate_dec))))
            form["RateTdsSecB"] = form["RateTdsADtaa"]
            form["OtherRemDtaa"] = "N"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_DTAA
        else:
            form["TaxIncDtaa"] = ""
            form["TaxLiablDtaa"] = ""
            form["RateTdsADtaa"] = ""
            form["RateTdsSecB"] = _fmt_num(float(applied_rate_dec))
            form["OtherRemDtaa"] = "Y"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT

        # Foreign currency TDS and actual remittance keep up to 2 decimals
        form["AmtPayForgnTds"] = _fmt_num(float(tds_fcy_dec))
        form["AmtPayIndianTds"] = _fmt_num(_round_to_int(float(tds_inr_dec)))
        form["ActlAmtTdsForgn"] = _fmt_num(float(actual_fcy))

        form.setdefault("BasisDeterTax", it_basis)
        form.setdefault("RemittanceCharIndia", "Y")
        logger.info(
            "recompute_tds_done invoice_id=%s dtaa_claimed=%s values=%s",
            invoice_id,
            dtaa_claimed,
            {
                "TaxLiablIt": form.get("TaxLiablIt", ""),
                "TaxIncDtaa": form.get("TaxIncDtaa", ""),
                "TaxLiablDtaa": form.get("TaxLiablDtaa", ""),
                "AmtPayForgnTds": form.get("AmtPayForgnTds", ""),
                "AmtPayIndianTds": form.get("AmtPayIndianTds", ""),
                "RateTdsSecB": form.get("RateTdsSecB", ""),
                "ActlAmtTdsForgn": form.get("ActlAmtTdsForgn", ""),
            },
        )
    elif mode == MODE_TDS and str(form.get("BasisDeterTax") or "").strip() == "Act":
        # Income Tax Act Section 195 path – uses user-selected rate
        effective_rate, basis_text = get_effective_it_rate(selected_it_rate)
        tax_liable_it = _round_to_int(inr * (effective_rate / 100.0))
        tax_fcy = float(tax_liable_it) / exchange_rate if exchange_rate else 0.0
        
        form["TaxLiablIt"] = _fmt_num(tax_liable_it)
        form["BasisDeterTax"] = basis_text
        form["RateTdsSecB"] = "{:.2f}".format(effective_rate)
        form.setdefault("RateTdsSecbFlg", RATE_TDS_SECB_FLG_TDS)
        form.setdefault("RemittanceCharIndia", "Y")
        form["OtherRemDtaa"] = "Y"
        # Clear DTAA-specific fields since we're using IT Act
        form["TaxIncDtaa"] = ""
        form["TaxLiablDtaa"] = ""
        form["RateTdsADtaa"] = ""
        form["AmtPayForgnTds"] = f"{tax_fcy:.2f}"
        form["AmtPayIndianTds"] = str(tax_liable_it)
        form["ActlAmtTdsForgn"] = _fmt_num(max(fcy - tax_fcy, 0.0))
        logger.info(
            "recompute_it_act_done invoice_id=%s rate=%s inr_amount=%s tax_liable=%s",
            invoice_id,
            effective_rate,
            inr,
            tax_liable_it,
        )
    elif mode == MODE_NON_TDS:
        form["RemittanceCharIndia"] = "N"
        form["AmtPayForgnTds"] = "0"
        form["AmtPayIndianTds"] = "0"
        form["ActlAmtTdsForgn"] = _fmt_num(fcy)
        form["OtherRemDtaa"] = "Y"
        form["RateTdsSecbFlg"] = ""
        form["RateTdsSecB"] = ""
        form["DednDateTds"] = ""
        logger.info("recompute_non_tds_done invoice_id=%s", invoice_id)
    elif mode == MODE_TDS:
        country_code = str(form.get("CountryRemMadeSecb") or "").strip()
        skip_reason = "country_blank" if not country_code else "country_selected_rate_missing"
        logger.warning(
            "recompute_tds_skipped invoice_id=%s reason=%s country=%s remitter_pan=%s",
            invoice_id,
            skip_reason,
            country_code,
            str(form.get("RemitterPAN") or ""),
        )
    return state


def _enforce_field_limits(out: Dict[str, str]) -> Dict[str, str]:
    """Truncate fields to their maximum allowed lengths defined in FIELD_MAX_LENGTH."""
    for field, max_len in FIELD_MAX_LENGTH.items():
        if field in out:
            val = str(out[field])
            if len(val) > max_len:
                logger.warning(
                    "field_truncated field=%s original_len=%s max=%s",
                    field,
                    len(val),
                    max_len,
                )
                out[field] = val[:max_len]
    return out


def invoice_state_to_xml_fields(state: Dict[str, object]) -> Dict[str, str]:
    meta = state.get("meta", {})
    extracted = state.get("extracted", {})
    form = state.get("form", {})
    resolved = state.get("resolved", {})
    mode = str(meta.get("mode") or MODE_TDS)

    remitter_name = str(form.get("NameRemitterInput") or extracted.get("remitter_name") or form.get("NameRemitter", "")).strip()
    remitter_address = str(extracted.get("remitter_address") or form.get("RemitterAddress", "")).strip()
    beneficiary = str(form.get("NameRemitteeInput") or extracted.get("beneficiary_name") or form.get("NameRemittee", "")).strip()
    # Read invoice number and date from form (user-editable), with fallback to extracted
    invoice_no = str(form.get("InvoiceNumber") or extracted.get("invoice_number") or "").strip()
    invoice_date_iso = str(form.get("InvoiceDate") or extracted.get("invoice_date_iso") or extracted.get("invoice_date_display") or extracted.get("invoice_date_raw") or "").strip()
    # Convert YYYY-MM-DD → DD.MM.YYYY for XML
    dotted = ""
    if invoice_date_iso:
        try:
            parsed_date = datetime.strptime(invoice_date_iso, "%Y-%m-%d").date()
            dotted = parsed_date.strftime("%d.%m.%Y")
        except Exception:
            dotted = format_dotted_date(invoice_date_iso)

    name_remitter = f"{remitter_name}. {remitter_address}".strip(". ").strip()
    name_remittee = _build_name_remittee(beneficiary, invoice_no, dotted)
    raw_relevant_dtaa = str(form.get("RelevantDtaa") or "").strip()
    raw_relevant_article = str(form.get("RelevantArtDtaa") or form.get("ArtDtaa") or "").strip()
    dtaa_source = raw_relevant_article or raw_relevant_dtaa
    dtaa_without_article, dtaa_with_article = split_dtaa_article_text(dtaa_source)
    if not dtaa_without_article:
        dtaa_without_article = raw_relevant_dtaa
    if not dtaa_with_article:
        dtaa_with_article = raw_relevant_article

    out: Dict[str, str] = {
        "SWVersionNo": SW_VERSION_NO,
        "SWCreatedBy": SW_CREATED_BY,
        "XMLCreatedBy": XML_CREATED_BY,
        "XMLCreationDate": datetime.now().strftime("%Y-%m-%d"),
        "IntermediaryCity": INTERMEDIARY_CITY,
        "FormName": FORM_NAME,
        "Description": FORM_DESCRIPTION,
        "AssessmentYear": ASSESSMENT_YEAR,
        "SchemaVer": SCHEMA_VER,
        "FormVer": FORM_VER,
        "IorWe": IOR_WE_CODE,
        "RemitterHonorific": HONORIFIC_M_S,
        "BeneficiaryHonorific": HONORIFIC_M_S,
        "NameRemitter": name_remitter,
        "RemitterPAN": str(form.get("RemitterPAN") or resolved.get("pan") or ""),
        "NameRemittee": name_remittee,
        "RemitteePremisesBuildingVillage": str(form.get("RemitteePremisesBuildingVillage") or ""),
        "RemitteeFlatDoorBuilding": str(form.get("RemitteeFlatDoorBuilding") or ""),
        "RemitteeAreaLocality": str(form.get("RemitteeAreaLocality") or ""),
        "RemitteeTownCityDistrict": str(form.get("RemitteeTownCityDistrict") or ""),
        "RemitteeRoadStreet": str(form.get("RemitteeRoadStreet") or ""),
        "RemitteeZipCode": REMITTEE_ZIP_CODE,
        "RemitteeState": REMITTEE_STATE,
        "RemitteeCountryCode": str(form.get("RemitteeCountryCode") or ""),
        "CountryRemMadeSecb": str(form.get("CountryRemMadeSecb") or ""),
        "CurrencySecbCode": str(form.get("CurrencySecbCode") or ""),
        "AmtPayForgnRem": str(form.get("AmtPayForgnRem") or ""),
        "AmtPayIndRem": str(form.get("AmtPayIndRem") or ""),
        "NameBankCode": str(form.get("NameBankCode") or ""),
        "BranchName": str(form.get("BranchName") or ""),
        "BsrCode": str(form.get("BsrCode") or ""),
        "PropDateRem": str(form.get("PropDateRem") or ""),
        "NatureRemCategory": str(form.get("NatureRemCategory") or ""),
        "RevPurCategory": str(form.get("RevPurCategory") or ""),
        "RevPurCode": str(form.get("RevPurCode") or ""),
        "TaxPayGrossSecb": str(form.get("TaxPayGrossSecb") or "N"),
        "RemittanceCharIndia": str(form.get("RemittanceCharIndia") or ("Y" if mode == MODE_TDS else "N")),
        "ReasonNot": str(form.get("ReasonNot") or ""),
        "SecRemCovered": str(form.get("SecRemCovered") or SEC_REM_COVERED_DEFAULT),
        "AmtIncChrgIt": str(form.get("AmtIncChrgIt") or ""),
        "TaxLiablIt": str(form.get("TaxLiablIt") or ""),
        "BasisDeterTax": str(form.get("BasisDeterTax") or ""),
        "TaxResidCert": str(form.get("TaxResidCert") or TAX_RESID_CERT_Y),
        "RelevantDtaa": dtaa_without_article,
        "RelevantArtDtaa": dtaa_with_article,
        "TaxIncDtaa": str(form.get("TaxIncDtaa") or ""),
        "TaxLiablDtaa": str(form.get("TaxLiablDtaa") or ""),
        "RemForRoyFlg": str(form.get("RemForRoyFlg") or ("Y" if mode == MODE_TDS else "N")),
        "ArtDtaa": dtaa_with_article,
        "RateTdsADtaa": str(form.get("RateTdsADtaa") or ""),
        "RemAcctBusIncFlg": str(form.get("RemAcctBusIncFlg") or "N"),
        "IncLiabIndiaFlg": INC_LIAB_INDIA_ALWAYS,
        "RemOnCapGainFlg": str(form.get("RemOnCapGainFlg") or "N"),
        "OtherRemDtaa": str(form.get("OtherRemDtaa") or ("N" if mode == MODE_TDS else "Y")),
        "NatureRemDtaa": str(form.get("NatureRemDtaa") or ""),
        "TaxIndDtaaFlg": TAX_IND_DTAA_ALWAYS,
        "RelArtDetlDDtaa": str(form.get("RelArtDetlDDtaa") or ("NOT APPLICABLE" if mode == MODE_TDS else "")),
        "AmtPayForgnTds": str(form.get("AmtPayForgnTds") or ("0" if mode == MODE_NON_TDS else "")),
        "AmtPayIndianTds": str(form.get("AmtPayIndianTds") or ("0" if mode == MODE_NON_TDS else "")),
        "RateTdsSecbFlg": str(form.get("RateTdsSecbFlg") or (RATE_TDS_SECB_FLG_TDS if mode == MODE_TDS else "")),
        "RateTdsSecB": str(form.get("RateTdsSecB") or ""),
        "ActlAmtTdsForgn": str(form.get("ActlAmtTdsForgn") or ""),
        "DednDateTds": str(form.get("DednDateTds") or ""),
    }
    out.update(CA_DEFAULTS)
    out["NameFirmAcctnt"] = str(form.get("NameFirmAcctnt") or CA_DEFAULTS["NameFirmAcctnt"])
    out["NameAcctnt"] = str(form.get("NameAcctnt") or CA_DEFAULTS["NameAcctnt"])

    # Enforce canonical field relationships to match utility output.
    out["TaxPayGrossSecb"] = "N"
    out["AmtIncChrgIt"] = str(out.get("AmtPayIndRem") or "")

    gross_fcy = _to_float(out.get("AmtPayForgnRem", ""))
    tds_fcy = _to_float(out.get("AmtPayForgnTds", ""))
    if mode == MODE_TDS and gross_fcy is not None and tds_fcy is not None:
        net_fcy = max(gross_fcy - tds_fcy, 0.0)
        out["ActlAmtTdsForgn"] = _fmt_num(net_fcy)

    tax_resid_cert = str(out.get("TaxResidCert") or "N").strip().upper()
    other_rem_dtaa = str(out.get("OtherRemDtaa") or ("N" if mode == MODE_TDS else "Y")).strip().upper()
    rate_secb = _to_float(out.get("RateTdsSecB", ""))
    rate_dtaa = _to_float(out.get("RateTdsADtaa", ""))
    dtaa_claimed = mode == MODE_TDS and tax_resid_cert == "Y" and other_rem_dtaa == "N"

    if dtaa_claimed:
        rate_for_claim = rate_dtaa if rate_dtaa is not None else rate_secb
        if not _is_integer_rate(rate_for_claim):
            dtaa_claimed = False
            other_rem_dtaa = "Y"
        else:
            integer_rate = str(int(round(float(rate_for_claim))))
            out["RateTdsADtaa"] = integer_rate
            out["RateTdsSecB"] = integer_rate
            out["TaxIncDtaa"] = str(out.get("TaxIncDtaa") or out.get("AmtPayIndRem") or "")
            out["TaxLiablDtaa"] = str(out.get("TaxLiablDtaa") or out.get("AmtPayIndianTds") or out.get("TaxLiablIt") or "")

    if not dtaa_claimed:
        out["TaxIncDtaa"] = ""
        out["TaxLiablDtaa"] = ""
        out["RateTdsADtaa"] = ""

    out["OtherRemDtaa"] = other_rem_dtaa

    return _enforce_field_limits(out)
