import unittest
from unittest.mock import MagicMock
import pandas as pd
from modules.zip_intake import build_invoice_registry

class TestDeductionDateExtraction(unittest.TestCase):
    def test_posting_date_mapping(self):
        # Mock DataFrame with both columns, but we only want Posting Date
        df = pd.DataFrame([{
            "Reference": "INV001",
            "Deduction Date": "2024-01-01",
            "Posting Date": "2023-12-25",
            "Document currency": "USD",
            "Amount in doc. curr.": 100,
            "Amount in local currency": 8000
        }])
        
        invoice_files = [("INV001.pdf", b"fake pdf content")]
        
        registry = build_invoice_registry(df, invoice_files)
        
        self.assertIn("INV001", registry)
        inv = registry["INV001"]
        self.assertIn("dedn_date_tds", inv["excel"])
        # It should use Posting Date (2023-12-25) even if Deduction Date is present
        self.assertEqual(inv["excel"]["dedn_date_tds"], "2023-12-25")

    def test_fallback_to_posting_date(self):
        # Mock DataFrame with only Posting Date
        df = pd.DataFrame([{
            "Reference": "INV002",
            "Posting Date": "2023-12-25",
            "Document currency": "USD",
            "Amount in doc. curr.": 100,
            "Amount in local currency": 8000
        }])
        
        invoice_files = [("INV002.pdf", b"fake pdf content")]
        
        registry = build_invoice_registry(df, invoice_files)
        
        self.assertIn("INV002", registry)
        inv = registry["INV002"]
        self.assertIn("dedn_date_tds", inv["excel"])
        self.assertEqual(inv["excel"]["dedn_date_tds"], "2023-12-25")

    def test_missing_posting_date_is_empty_string(self):
        df = pd.DataFrame([{
            "Reference": "INV003",
            "Document currency": "USD",
            "Amount in doc. curr.": 100,
            "Amount in local currency": 8000
        }])
        invoice_files = [("INV003.pdf", b"fake pdf content")]
        registry = build_invoice_registry(df, invoice_files)
        self.assertIn("INV003", registry)
        self.assertIn("dedn_date_tds", registry["INV003"]["excel"])
        self.assertEqual(registry["INV003"]["excel"]["dedn_date_tds"], "")

if __name__ == "__main__":
    unittest.main()
