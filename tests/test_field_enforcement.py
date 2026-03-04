import unittest
from modules.invoice_calculator import invoice_state_to_xml_fields
from modules.form15cb_constants import FIELD_MAX_LENGTH

class TestFieldEnforcement(unittest.TestCase):
    def test_truncation(self):
        # Create a mock state with over-length fields
        state = {
            "meta": {"mode": "TDS"},
            "extracted": {},
            "form": {
                "NameRemitter": "A" * 200,  # Max 120
                "NameRemittee": "B" * 200,  # Max 120
                "BranchName": "C" * 100,    # Max 75
                "BasisDeterTax": "D" * 300, # Max 250
            },
            "resolved": {}
        }
        
        out = invoice_state_to_xml_fields(state)
        
        # Verify truncation
        self.assertEqual(len(out["NameRemitter"]), 120)
        self.assertEqual(out["NameRemitter"], "A" * 120)
        
        self.assertEqual(len(out["NameRemittee"]), 120)
        self.assertEqual(out["NameRemittee"], "B" * 120)
        
        self.assertEqual(len(out["BranchName"]), 75)
        self.assertEqual(out["BranchName"], "C" * 75)
        
        self.assertEqual(len(out["BasisDeterTax"]), 250)
        self.assertEqual(out["BasisDeterTax"], "D" * 250)
        
    def test_no_truncation_when_within_limits(self):
        state = {
            "meta": {"mode": "TDS"},
            "extracted": {},
            "form": {
                "NameRemitter": "Short Name",
                "BranchName": "Main Branch",
            },
            "resolved": {}
        }
        
        out = invoice_state_to_xml_fields(state)
        
        self.assertEqual(out["NameRemitter"], "Short Name")
        self.assertEqual(out["BranchName"], "Main Branch")

if __name__ == "__main__":
    unittest.main()
