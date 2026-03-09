from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict


NAMESPACES = {
    "Form": "http://incometaxindiaefiling.gov.in/common",
    "FORM15CB": "http://incometaxindiaefiling.gov.in/FORM15CAB",
}


TAG_MAP = {
    "Form:CreationInfo/Form:SWVersionNo": "SWVersionNo",
    "Form:CreationInfo/Form:SWCreatedBy": "SWCreatedBy",
    "Form:CreationInfo/Form:XMLCreatedBy": "XMLCreatedBy",
    "Form:CreationInfo/Form:XMLCreationDate": "XMLCreationDate",
    "Form:CreationInfo/Form:IntermediaryCity": "IntermediaryCity",
    "Form:Form_Details/Form:FormName": "FormName",
    "Form:Form_Details/Form:Description": "Description",
    "Form:Form_Details/Form:AssessmentYear": "AssessmentYear",
    "Form:Form_Details/Form:SchemaVer": "SchemaVer",
    "Form:Form_Details/Form:FormVer": "FormVer",
    "FORM15CB:RemitterDetails/FORM15CB:IorWe": "IorWe",
    "FORM15CB:RemitterDetails/FORM15CB:RemitterHonorific": "RemitterHonorific",
    "FORM15CB:RemitterDetails/FORM15CB:NameRemitter": "NameRemitter",
    "FORM15CB:RemitterDetails/FORM15CB:PAN": "RemitterPAN",
    "FORM15CB:RemitterDetails/FORM15CB:BeneficiaryHonorific": "BeneficiaryHonorific",
    "FORM15CB:RemitteeDetls/FORM15CB:NameRemittee": "NameRemittee",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:PremisesBuildingVillage": "RemitteePremisesBuildingVillage",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:TownCityDistrict": "RemitteeTownCityDistrict",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:FlatDoorBuilding": "RemitteeFlatDoorBuilding",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:AreaLocality": "RemitteeAreaLocality",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:RoadStreet": "RemitteeRoadStreet",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:ZipCode": "RemitteeZipCode",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/Form:State": "RemitteeState",
    "FORM15CB:RemitteeDetls/FORM15CB:RemitteeAddrs/FORM15CB:Country": "RemitteeCountryCode",
    "FORM15CB:RemittanceDetails/FORM15CB:CountryRemMadeSecb": "CountryRemMadeSecb",
    "FORM15CB:RemittanceDetails/FORM15CB:CurrencySecbCode": "CurrencySecbCode",
    "FORM15CB:RemittanceDetails/FORM15CB:AmtPayForgnRem": "AmtPayForgnRem",
    "FORM15CB:RemittanceDetails/FORM15CB:AmtPayIndRem": "AmtPayIndRem",
    "FORM15CB:RemittanceDetails/FORM15CB:NameBankCode": "NameBankCode",
    "FORM15CB:RemittanceDetails/FORM15CB:BranchName": "BranchName",
    "FORM15CB:RemittanceDetails/FORM15CB:BsrCode": "BsrCode",
    "FORM15CB:RemittanceDetails/FORM15CB:PropDateRem": "PropDateRem",
    "FORM15CB:RemittanceDetails/FORM15CB:NatureRemCategory": "NatureRemCategory",
    "FORM15CB:RemittanceDetails/FORM15CB:RevPurCategory": "RevPurCategory",
    "FORM15CB:RemittanceDetails/FORM15CB:RevPurCode": "RevPurCode",
    "FORM15CB:RemittanceDetails/FORM15CB:TaxPayGrossSecb": "TaxPayGrossSecb",
    "FORM15CB:ItActDetails/FORM15CB:RemittanceCharIndia": "RemittanceCharIndia",
    "FORM15CB:ItActDetails/FORM15CB:ReasonNot": "ReasonNot",
    "FORM15CB:ItActDetails/FORM15CB:SecRemCovered": "SecRemCovered",
    "FORM15CB:ItActDetails/FORM15CB:AmtIncChrgIt": "AmtIncChrgIt",
    "FORM15CB:ItActDetails/FORM15CB:TaxLiablIt": "TaxLiablIt",
    "FORM15CB:ItActDetails/FORM15CB:BasisDeterTax": "BasisDeterTax",
    "FORM15CB:DTAADetails/FORM15CB:TaxResidCert": "TaxResidCert",
    "FORM15CB:DTAADetails/FORM15CB:RelevantDtaa": "RelevantDtaa",
    "FORM15CB:DTAADetails/FORM15CB:RelevantArtDtaa": "RelevantArtDtaa",
    "FORM15CB:DTAADetails/FORM15CB:TaxIncDtaa": "TaxIncDtaa",
    "FORM15CB:DTAADetails/FORM15CB:TaxLiablDtaa": "TaxLiablDtaa",
    "FORM15CB:DTAADetails/FORM15CB:RemForRoyFlg": "RemForRoyFlg",
    "FORM15CB:DTAADetails/FORM15CB:ArtDtaa": "ArtDtaa",
    "FORM15CB:DTAADetails/FORM15CB:RateTdsADtaa": "RateTdsADtaa",
    "FORM15CB:DTAADetails/FORM15CB:RemAcctBusIncFlg": "RemAcctBusIncFlg",
    "FORM15CB:DTAADetails/FORM15CB:IncLiabIndiaFlg": "IncLiabIndiaFlg",
    "FORM15CB:DTAADetails/FORM15CB:RemOnCapGainFlg": "RemOnCapGainFlg",
    "FORM15CB:DTAADetails/FORM15CB:OtherRemDtaa": "OtherRemDtaa",
    "FORM15CB:DTAADetails/FORM15CB:NatureRemDtaa": "NatureRemDtaa",
    "FORM15CB:DTAADetails/FORM15CB:TaxIndDtaaFlg": "TaxIndDtaaFlg",
    "FORM15CB:DTAADetails/FORM15CB:RelArtDetlDDtaa": "RelArtDetlDDtaa",
    "FORM15CB:TDSDetails/FORM15CB:AmtPayForgnTds": "AmtPayForgnTds",
    "FORM15CB:TDSDetails/FORM15CB:AmtPayIndianTds": "AmtPayIndianTds",
    "FORM15CB:TDSDetails/FORM15CB:RateTdsSecbFlg": "RateTdsSecbFlg",
    "FORM15CB:TDSDetails/FORM15CB:RateTdsSecB": "RateTdsSecB",
    "FORM15CB:TDSDetails/FORM15CB:ActlAmtTdsForgn": "ActlAmtTdsForgn",
    "FORM15CB:TDSDetails/FORM15CB:DednDateTds": "DednDateTds",
    "FORM15CB:AcctntDetls/FORM15CB:NameAcctnt": "NameAcctnt",
    "FORM15CB:AcctntDetls/FORM15CB:NameFirmAcctnt": "NameFirmAcctnt",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:PremisesBuildingVillage": "PremisesBuildingVillage",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:TownCityDistrict": "AcctntTownCityDistrict",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:FlatDoorBuilding": "AcctntFlatDoorBuilding",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:AreaLocality": "AcctntAreaLocality",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:Pincode": "AcctntPincode",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/Form:State": "AcctntState",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:RoadStreet": "AcctntRoadStreet",
    "FORM15CB:AcctntDetls/FORM15CB:AcctntAddrs/FORM15CB:Country": "AcctntCountryCode",
    "FORM15CB:AcctntDetls/FORM15CB:MembershipNumber": "MembershipNumber",
}


def parse_xml_to_fields(xml_path: str) -> Dict[str, str]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    out: Dict[str, str] = {}
    for xpath, field_key in TAG_MAP.items():
        node = root.find(xpath, NAMESPACES)
        if node is not None and node.text is not None:
            out[field_key] = node.text.strip()
    return out
