"""Regression cases for buyer controls that must not become order lines."""
from datetime import date
import unittest

from pydantic import ValidationError

from agent.claude_parser import to_parsed
from agent.rules_parser import parse
from contracts.models import ParsedRequest


TODAY = date(2026, 11, 14)


class RulesParserControlsTest(unittest.TestCase):
    def test_budget_and_negation_are_controls_not_products(self):
        request = parse(
            "I need 1,000 litres of lactose-free milk by 2026-11-16. "
            "Budget is 2000 EUR. Do not substitute with regular milk.", TODAY,
        )
        self.assertEqual(len(request.items), 1)
        self.assertEqual(request.items[0].product_query, "milk")
        self.assertEqual(request.items[0].quantity, 1000)
        self.assertEqual(request.items[0].unit, "L")
        self.assertEqual(request.items[0].constraints.required_claims, ["FREE_FROM_LACTOSE"])
        self.assertEqual(request.delivery_date, date(2026, 11, 16))
        self.assertEqual(request.budget_eur, 2000)
        self.assertFalse(request.substitution_allowed)
        self.assertFalse(request.clarification_required)

    def test_budget_formats_and_decimal_amounts(self):
        for control, amount in [
            ("Budget €2,000.50", 2000.50),
            ("budget: EUR 200", 200),
            ("budget of 200 euros", 200),
            ("budget is under €200", 200),
            ("spend no more than €200", 200),
            ("under €200", 200),
            ("budjetti 200 EUR", 200),
        ]:
            with self.subTest(control=control):
                request = parse(f"100 L milk by Monday. {control}.", TODAY)
                self.assertEqual(request.budget_eur, amount)
                self.assertFalse(request.clarification_required)
                self.assertEqual([it.product_query for it in request.items], ["milk"])

    def test_inline_budget_preserves_other_items(self):
        request = parse("100 L milk by Monday, budget €200 and 5 kg butter", TODAY)
        self.assertEqual([it.product_query for it in request.items], ["milk", "butter"])
        self.assertEqual([it.quantity for it in request.items], [100, 5])
        self.assertEqual(request.budget_eur, 200)

    def test_substitution_denials_preserve_explicit_date(self):
        for control in ["No substitutions", "Don't substitute", "Do not replace with regular milk"]:
            with self.subTest(control=control):
                request = parse(f"100 L milk. {control} by Monday.", TODAY)
                self.assertEqual(request.delivery_date, date(2026, 11, 16))
                self.assertFalse(request.delivery_date_assumed)
                self.assertFalse(request.substitution_allowed)
                self.assertEqual(len(request.items), 1)

    def test_unknown_invalid_or_foreign_budget_requires_clarification(self):
        for control in ["Budget 200", "Budget USD 200", "Budget £200", "Budget €0", "Budget €-10", "Budget EUR NaN"]:
            with self.subTest(control=control):
                request = parse(f"100 L milk by Monday. {control}.", TODAY)
                self.assertTrue(request.clarification_required)
                self.assertTrue(request.warnings)
                self.assertEqual([it.product_query for it in request.items], ["milk"])

    def test_conflicting_budgets_keep_restrictive_cap_but_block(self):
        request = parse("100 L milk by Monday. Budget €200. Budget €300.", TODAY)
        self.assertEqual(request.budget_eur, 200)
        self.assertTrue(request.clarification_required)

    def test_conflicting_substitution_controls_block(self):
        request = parse("100 L milk by Monday. No substitutions. Substitutions are allowed.", TODAY)
        self.assertFalse(request.substitution_allowed)
        self.assertTrue(request.clarification_required)
        self.assertEqual(len(request.items), 1)

    def test_unsupported_negative_condition_does_not_become_product(self):
        request = parse("100 L milk by Monday. No regular milk.", TODAY)
        self.assertTrue(request.clarification_required)
        self.assertEqual([it.product_query for it in request.items], ["milk"])

    def test_tax_inclusive_cap_requires_clarification(self):
        request = parse("100 L milk by Monday. Budget €200 including VAT.", TODAY)
        self.assertEqual(request.budget_eur, 200)
        self.assertTrue(request.clarification_required)

    def test_unsupported_money_phrase_is_not_a_product_quantity(self):
        request = parse("100 L milk by Monday. Maximum spend €200.", TODAY)
        self.assertTrue(request.clarification_required)
        self.assertEqual([it.product_query for it in request.items], ["milk"])

    def test_simple_multiline_request_is_unchanged(self):
        request = parse("20 kg potatoes and 10 kg tomatoes for Tuesday", TODAY)
        self.assertEqual([it.product_query for it in request.items], ["potato", "tomato"])
        self.assertEqual([it.quantity for it in request.items], [20, 10])
        self.assertEqual(request.delivery_date, date(2026, 11, 17))
        self.assertIsNone(request.budget_eur)
        self.assertTrue(request.substitution_allowed)
        self.assertFalse(request.clarification_required)

    def test_nonpositive_quantity_requires_clarification(self):
        for amount in [0, -10]:
            with self.subTest(amount=amount):
                request = parse(f"{amount} L milk by Monday", TODAY)
                self.assertIsNone(request.items[0].quantity)
                self.assertTrue(request.clarification_required)

    def test_no_added_sugar_remains_supported_requirement(self):
        request = parse("10 kg yogurt with no added sugar by Monday", TODAY)
        self.assertEqual(request.items[0].constraints.required_claims, ["NO_ADDED_SUGARS"])
        self.assertFalse(request.clarification_required)


class SharedAndClaudeControlsTest(unittest.TestCase):
    def test_claude_mapping_keeps_order_controls(self):
        request = to_parsed({
            "items": [{"text": "100 L milk", "product_query": "milk", "quantity": 100,
                       "quantity_known": True, "unit": "L"}],
            "delivery_date": "2026-11-16", "delivery_date_stated": True,
            "budget_eur": 200, "substitution_allowed": False, "clarification_required": True,
            "notes": "Please confirm the restriction",
        }, TODAY)
        self.assertEqual(request.budget_eur, 200)
        self.assertFalse(request.substitution_allowed)
        self.assertTrue(request.clarification_required)

    def test_claude_explanation_of_a_stated_quantity_is_not_an_assumption(self):
        def item(estimated):
            return {"text": "1,000 L milk", "product_query": "milk", "quantity": 1000, "quantity_known": True,
                    "quantity_estimated": estimated, "unit": "L", "assumption": "Quantity stated directly as 1,000 litres"}
        payload = {"delivery_date": "2026-11-16", "delivery_date_stated": True, "budget_eur": None,
                   "substitution_allowed": True, "clarification_required": False, "notes": ""}
        stated = to_parsed({**payload, "items": [item(False)]}, TODAY).items[0]
        self.assertEqual((stated.quantity, stated.unit, stated.assumption), (1000, "L", ""))
        estimate = to_parsed({**payload, "items": [item(True)]}, TODAY).items[0]
        self.assertTrue(estimate.assumption)  # a real estimate still needs a person to confirm it

    def test_old_payload_defaults_remain_compatible(self):
        request = ParsedRequest(items=[], delivery_date=date(2026, 11, 16))
        self.assertIsNone(request.budget_eur)
        self.assertTrue(request.substitution_allowed)
        self.assertFalse(request.clarification_required)

    def test_nonpositive_and_nonfinite_budget_is_invalid(self):
        for amount in [0, -1, float("inf"), float("nan")]:
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                ParsedRequest(items=[], delivery_date=date(2026, 11, 16), budget_eur=amount)


if __name__ == "__main__":
    unittest.main()
