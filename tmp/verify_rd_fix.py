import sys
import os
sys.path.append(os.getcwd())

from modules.text_remittance_ai_helper import classify_text_field
from modules.remittance_classifier import classify_remittance
from modules.invoice_gemini_extractor import _fuzzy_match_nature

def test_rd_classification_logic():
    print("Testing R&D Detection Rule...")
    text = "Charging of R&D services based on hours"
    
    # 1. Test the text classifier directly
    res = classify_text_field(text=text)
    print(f"Text Classifier Result: {res['purpose_code']}, Source: {res['source']}, Nature: {res['nature_of_remittance']}")
    
    assert res['purpose_code'] == "S1008"
    assert res['source'] == "rd_rule"
    assert res['nature_of_remittance'] == "FEES FOR TECHNICAL SERVICES / R&D SERVICES"
    print("Text Classifier Test PASSED!")

def test_nature_normalization():
    print("\nTesting Nature Normalization Fix...")
    suggestion = "Charging of R&D services based on hours"
    matched = _fuzzy_match_nature(suggestion)
    print(f"Nature Normalization: '{suggestion}' -> '{matched}'")
    assert matched == "FEES FOR TECHNICAL SERVICES"
    print("Nature Normalization Test PASSED!")

def test_classifier_confidence_fallback():
    print("\nTesting Classifier Confidence Fallback (0.7 threshold)...")
    # Mock data where classifier would have low confidence
    extracted = {
        "purpose_code": "S0802", # Gemini extracted correctly
        "nature_of_remittance": "Software consultancy",
        "invoice_id": "TEST_INV_001",
        "_excel_text": "xyz abc" # No keywords
    }
    
    # We want to ensure it keeps Gemini's S0802 if classifier is low confidence
    # Note: classify_remittance internally calls classify_text_field
    from modules.remittance_classifier import _purpose_records, _nature_records
    
    classification = classify_remittance(invoice_text="Random text info info info", extracted=extracted)
    
    print(f"Final Classification: Code={classification.purpose.purpose_code}, Confidence={classification.confidence:.2f}")
    
    # Since "Generic services invoice" won't match a rule, it should fall back to Gemini's S0802
    assert classification.purpose.purpose_code == "S0802"
    print("Confidence Fallback Test PASSED!")

if __name__ == "__main__":
    try:
        test_rd_classification_logic()
        test_nature_normalization()
        test_classifier_confidence_fallback()
        print("\nALL R&D FIX TESTS PASSED!")
    except Exception as e:
        print(f"\nTest FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
