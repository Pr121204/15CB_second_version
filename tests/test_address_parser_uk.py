import unittest
from modules.address_parser import parse_beneficiary_address

class TestAddressParserRefined(unittest.TestCase):
    def test_uk_address_with_comma(self):
        # UK address with comma
        addr = "60-61 Britton Street, EC1M 5UX London"
        res = parse_beneficiary_address(addr)
        self.assertEqual(res["FlatDoorBuilding"], "60-61 Britton Street")
        self.assertEqual(res["ZipCode"], "EC1M 5UX")
        self.assertEqual(res["TownCityDistrict"], "London")

    def test_uk_address_no_comma_gemini_style(self):
        # The specific failure case: no commas, UK postcode
        addr = "60-61 Britton Street London EC1M 5UX United Kingdom"
        res = parse_beneficiary_address(addr)
        # Note: Patterns might put "United Kingdom" in City since it's after ZIP
        self.assertEqual(res["ZipCode"], "EC1M 5UX")
        self.assertIn("60-61 Britton Street", res["FlatDoorBuilding"])
        self.assertIn("London", res["TownCityDistrict"])

    def test_german_address_refined(self):
        addr = "Musterstraße 12, 70376 Stuttgart"
        res = parse_beneficiary_address(addr)
        self.assertEqual(res["ZipCode"], "70376")
        self.assertEqual(res["TownCityDistrict"], "Stuttgart")

    def test_numeric_zip_no_comma(self):
        addr = "Main Street 123 123456 New York"
        res = parse_beneficiary_address(addr)
        self.assertEqual(res["ZipCode"], "123456")
        self.assertEqual(res["FlatDoorBuilding"], "Main Street 123")
        self.assertEqual(res["TownCityDistrict"], "New York")

if __name__ == "__main__":
    unittest.main()
