import unittest
import re
from modules.invoice_gemini_extractor import _fix_address_spacing

class TestAddressSpacing(unittest.TestCase):
    def test_fix_address_spacing(self):
        # Case 1: Commas without spaces
        input_1 = "HosurRoad,AdugodiBangalore560030,India"
        expected_1 = "HosurRoad, AdugodiBangalore560030, India"
        self.assertEqual(_fix_address_spacing(input_1), expected_1)
        
        # Case 2: Commas with spaces (should not change)
        input_2 = "Hosur Road, Adugodi, Bangalore, 560030, India"
        expected_2 = "Hosur Road, Adugodi, Bangalore, 560030, India"
        self.assertEqual(_fix_address_spacing(input_2), expected_2)
        
        # Case 3: Mixed commas
        input_3 = "Line 1,Line 2, Line 3"
        expected_3 = "Line 1, Line 2, Line 3"
        self.assertEqual(_fix_address_spacing(input_3), expected_3)

        # Case 4: No commas
        input_4 = "No commas here"
        expected_4 = "No commas here"
        self.assertEqual(_fix_address_spacing(input_4), expected_4)

if __name__ == "__main__":
    unittest.main()
