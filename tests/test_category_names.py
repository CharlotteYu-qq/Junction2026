import json
import unittest
from pathlib import Path

from gateway.catalog import category_name

NAMES = json.loads((Path(__file__).resolve().parent.parent / "gateway" / "category_names.json").read_text(encoding="utf-8"))


class CategoryNameTests(unittest.TestCase):
    def test_every_code_has_a_readable_name(self):
        self.assertEqual(len(NAMES), 349)  # every category code in the challenge data
        for code, name in NAMES.items():
            self.assertTrue(name and not name.isdigit(), code)

    def test_known_codes(self):
        self.assertEqual(category_name("18720"), "Beef")
        self.assertEqual(category_name("18760"), "Minced meat")
        self.assertEqual(category_name("10400"), "Milk")

    def test_departments_follow_the_code_prefix(self):
        from gateway.catalog import DEPARTMENT_NAMES, department_of
        cases = {"10400": "dairy", "10590": "dairy", "10500": "fats", "10900": "bakery", "17401": "produce",
                 "17200": "deli", "18701": "fish", "18720": "meat", "41820": "frozen", "21400": "drinks",
                 "21450": "dry", "21990": "sweets", "22300": "coffee", "63100": "cleaning", "90011": "other"}
        for code, dept in cases.items():
            self.assertEqual(department_of(code), dept, code)
        self.assertTrue(all(department_of(code) in DEPARTMENT_NAMES for code in NAMES))

    def test_unknown_code_still_reads_as_a_category(self):
        self.assertEqual(category_name("99999"), "Category 99999")
        self.assertEqual(category_name(None), "Uncategorised")


if __name__ == "__main__":
    unittest.main()
