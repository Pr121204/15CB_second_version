import unittest
from modules.invoice_gemini_extractor import _normalize_company_name

class TestInvoiceGeminiExtractor(unittest.TestCase):
    def test_normalize_company_name_collapses_underscore_letter_artifacts(self):
        # The exact case reported by the user
        input_name = "E_T_A__S _G_M_B_H_"
        expected_output = "ETAS GMBH"
        self.assertEqual(_normalize_company_name(input_name), expected_output)

    def test_normalize_company_name_mixed_underscores(self):
        # Case with some underscores that shouldn't be collapsed if they are not single letters
        input_name = "COMPANY_NAME_LIMITED"
        # Since _normalize_company_name handles underscores by replacing them with spaces in some cases
        # let's see what the current implementation does.
        # _normalize_company_name replaces .-_ with space if suffix was removed or it looks like a domain.
        # Otherwise it keeps them? Let's check.
        # Actually _collapse_underscored_letter_tokens only acts if ALL parts are single chars.
        # "COMPANY_NAME_LIMITED" -> tokens ["COMPANY_NAME_LIMITED"]. _ in t? Yes. matched? No (more than 1 char parts).
        # Then _normalize_company_name continues.
        self.assertEqual(_normalize_company_name(input_name), "COMPANY NAME LIMITED")

if __name__ == "__main__":
    unittest.main()
