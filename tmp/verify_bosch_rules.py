
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from modules.text_remittance_ai_helper import classify_text_field
import json

def verify_bosch_rules():
    print("Verifying 8 Deterministic Bosch Rules...\n")
    
    test_cases = [
        {
            "name": "1. R&D Engineering Services",
            "text": "charging of r&d services based on hours",
            "expected_code": "S1008",
            "expected_nature": "FEES FOR TECHNICAL SERVICES / R&D SERVICES",
            "expected_group": "Other Business Services"
        },
        {
            "name": "2. Payroll / Social Security Recharge",
            "text": "Service paid for other entity – person",
            "expected_code": "S1401",
            "expected_nature": "COMPENSATION OF EMPLOYEES",
            "expected_group": "Primary Income"
        },
        {
            "name": "3. Global Services / Shared Services",
            "text": "Shared service allocation for Q1",
            "expected_code": "S1008",
            "expected_nature": "FEES FOR TECHNICAL SERVICES / R&D SERVICES",
            "expected_group": "Other Business Services"
        },
        {
            "name": "4. SAP / IT Support Services",
            "text": "sap support services for March",
            "expected_code": "S0802",
            "expected_nature": "SOFTWARE / IT SERVICES",
            "expected_group": "Telecommunication, Computer & Information Services"
        },
        {
            "name": "5. Data Processing / Hosting",
            "text": "cloud service charges",
            "expected_code": "S0803",
            "expected_nature": "DATA PROCESSING SERVICES",
            "expected_group": "Telecommunication, Computer & Information Services"
        },
        {
            "name": "6. Marketing / Sales Support",
            "text": "marketing service for new product",
            "expected_code": "S1007",
            "expected_nature": "MARKETING SERVICES",
            "expected_group": "Other Business Services"
        },
        {
            "name": "7. Consulting / Advisory",
            "text": "management consulting fee",
            "expected_code": "S1006",
            "expected_nature": "CONSULTING SERVICES",
            "expected_group": "Other Business Services"
        },
        {
            "name": "8. Import of Goods",
            "text": "material supply for factory",
            "expected_code": "S0102",
            "expected_nature": "IMPORT OF GOODS",
            "expected_group": "Imports"
        }
    ]
    
    all_passed = True
    for case in test_cases:
        print(f"Testing: {case['name']}")
        print(f"  Input: {case['text']!r}")
        res = classify_text_field(case['text'])
        
        actual_code = res.get("purpose_code")
        actual_nature = res.get("nature_of_remittance")
        actual_group = res.get("purpose_group")
        actual_source = res.get("source")
        
        code_match = (actual_code == case['expected_code'])
        nature_match = (actual_nature == case['expected_nature'])
        group_match = (actual_group == case['expected_group'])
        source_match = (actual_source == "bosch_deterministic_rule")
        
        passed = all([code_match, nature_match, group_match, source_match])
        status = "PASSED" if passed else "FAILED"
        
        print(f"  Result: {actual_code} | {actual_group} | {actual_nature}")
        print(f"  Source: {actual_source} ({res.get('confidence')})")
        print(f"  Status: {status}")
        
        if not passed:
            if not code_match: print(f"    Expected Code: {case['expected_code']}")
            if not nature_match: print(f"    Expected Nature: {case['expected_nature']}")
            if not group_match: print(f"    Expected Group: {case['expected_group']}")
            if not source_match: print(f"    Expected Source: bosch_deterministic_rule")
            all_passed = False
        print("-" * 50)
    
    if all_passed:
        print("\nAll 8 Bosch deterministic rules PASSED.")
    else:
        print("\nSome Bosch deterministic rules FAILED.")
        sys.exit(1)

if __name__ == "__main__":
    verify_bosch_rules()
