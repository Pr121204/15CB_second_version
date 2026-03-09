
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from modules.text_remittance_ai_helper import classify_text_field
import json

def verify_rd_rule():
    print("Verifying R&D Priority Rule...\n")
    
    test_cases = [
        {
            "name": "Direct 'r&d' match",
            "text": "r&d services",
            "expected_code": "S1008",
            "expected_source": "rd_rule"
        },
        {
            "name": "Direct 'research and development' match",
            "text": "research and development cost",
            "expected_code": "S1008",
            "expected_source": "rd_rule"
        },
        {
            "name": "Mixed case 'R&D'",
            "text": "Invoice for R&D",
            "expected_code": "S1008",
            "expected_source": "rd_rule"
        },
        {
            "name": "Overriding software (S0802) keyword",
            "text": "software r&d",
            "expected_code": "S1008",
            "expected_source": "rd_rule"
        },
        {
            "name": "Overriding legal (S1004) keyword",
            "text": "legal research and development",
            "expected_code": "S1008",
            "expected_source": "rd_rule"
        }
    ]
    
    all_passed = True
    for case in test_cases:
        print(f"Testing: {case['name']} - Input: {case['text']!r}")
        res = classify_text_field(case['text'])
        actual_code = res.get("purpose_code")
        actual_source = res.get("source")
        
        passed = (actual_code == case['expected_code'] and actual_source == case['expected_source'])
        status = "PASSED" if passed else "FAILED"
        print(f"  Result: {actual_code} via {actual_source} -> {status}")
        if not passed:
            print(f"  Expected: {case['expected_code']} via {case['expected_source']}")
            all_passed = False
        print("-" * 30)
    
    if all_passed:
        print("\nAll R&D priority tests PASSED.")
    else:
        print("\nSome R&D priority tests FAILED.")
        sys.exit(1)

if __name__ == "__main__":
    verify_rd_rule()
