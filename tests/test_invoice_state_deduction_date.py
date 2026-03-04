import unittest
from unittest.mock import patch

from modules.invoice_state import build_invoice_state


class TestInvoiceStateDeductionDate(unittest.TestCase):
    def _base_extracted(self):
        return {
            "remitter_name": "",
            "beneficiary_name": "",
            "invoice_number": "",
            "amount": "",
            "currency_short": "",
            "_raw_invoice_text": "",
        }

    @patch("modules.invoice_state.recompute_invoice", side_effect=lambda state: state)
    @patch("modules.invoice_state.resolve_currency_selection", return_value={"code": "USD"})
    @patch("modules.invoice_state.load_currency_exact_index", return_value={})
    def test_uses_valid_config_deduction_date(
        self,
        _mock_currency_index,
        _mock_currency_resolve,
        _mock_recompute,
    ):
        state = build_invoice_state(
            "INV001",
            "INV001.pdf",
            self._base_extracted(),
            {
                "mode": "TDS",
                "currency_short": "USD",
                "exchange_rate": 80,
                "tds_deduction_date": "2025-11-27",
            },
        )
        self.assertEqual(state["form"]["DednDateTds"], "2025-11-27")
        self.assertFalse(state["meta"]["dedn_date_missing"])
        self.assertFalse(state["meta"]["dedn_date_invalid"])

    @patch("modules.invoice_state.recompute_invoice", side_effect=lambda state: state)
    @patch("modules.invoice_state.resolve_currency_selection", return_value={"code": "USD"})
    @patch("modules.invoice_state.load_currency_exact_index", return_value={})
    def test_blank_config_deduction_date_stays_empty(
        self,
        _mock_currency_index,
        _mock_currency_resolve,
        _mock_recompute,
    ):
        state = build_invoice_state(
            "INV002",
            "INV002.pdf",
            self._base_extracted(),
            {
                "mode": "TDS",
                "currency_short": "USD",
                "exchange_rate": 80,
                "tds_deduction_date": "",
            },
        )
        self.assertEqual(state["form"]["DednDateTds"], "")
        self.assertTrue(state["meta"]["dedn_date_missing"])
        self.assertFalse(state["meta"]["dedn_date_invalid"])

    @patch("modules.invoice_state.recompute_invoice", side_effect=lambda state: state)
    @patch("modules.invoice_state.resolve_currency_selection", return_value={"code": "USD"})
    @patch("modules.invoice_state.load_currency_exact_index", return_value={})
    def test_invalid_config_deduction_date_is_rejected(
        self,
        _mock_currency_index,
        _mock_currency_resolve,
        _mock_recompute,
    ):
        state = build_invoice_state(
            "INV003",
            "INV003.pdf",
            self._base_extracted(),
            {
                "mode": "TDS",
                "currency_short": "USD",
                "exchange_rate": 80,
                "tds_deduction_date": "27/11/2025",
            },
        )
        self.assertEqual(state["form"]["DednDateTds"], "")
        self.assertTrue(state["meta"]["dedn_date_missing"])
        self.assertTrue(state["meta"]["dedn_date_invalid"])


if __name__ == "__main__":
    unittest.main()

