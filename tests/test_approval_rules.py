"""The purchase-history approval rules, and the fixes the agent proposes when an order cannot be placed."""
import unittest
from datetime import timedelta

from contracts.models import Availability, Candidate, PriceInfo
from gateway.alternatives import suggest_fixes
from tests.test_policy import NOW, decision, purchase
from trust.policy import PurchaseHistory, evaluate


def check(result, code):
    return next(c for c in result.checks if c.code == code)


class HistoryRuleTests(unittest.TestCase):
    def test_a_new_product_above_the_average_order_needs_approval(self):
        customer, offer, _ = purchase()  # the line costs 140
        customer.avg_order_value = 100
        new = PurchaseHistory(known_products=frozenset({'other'}), largest_order=5000)
        result = evaluate(customer, offer, {}, NOW, True, new)[0]
        self.assertEqual(result.outcome, 'needs_approval')
        self.assertFalse(check(result, 'NEW_PRODUCT').passed)

    def test_a_new_product_within_the_average_or_a_known_product_passes(self):
        customer, offer, _ = purchase()
        customer.avg_order_value = 500
        result = evaluate(customer, offer, {}, NOW, True, PurchaseHistory(frozenset(), 5000))[0]
        self.assertTrue(check(result, 'NEW_PRODUCT').passed)
        self.assertEqual(result.outcome, 'auto_approve')
        customer.avg_order_value = 100
        known = evaluate(customer, offer, {}, NOW, True, PurchaseHistory(frozenset({'milk'}), 5000))[0]
        self.assertFalse(any(c.code == 'NEW_PRODUCT' for c in known.checks))
        self.assertEqual(known.outcome, 'auto_approve')

    def test_more_than_the_largest_order_so_far_needs_approval(self):
        customer, offer, _ = purchase()  # total 140
        result = evaluate(customer, offer, {}, NOW, True, PurchaseHistory(frozenset({'milk'}), 120))[0]
        self.assertEqual(result.outcome, 'needs_approval')
        self.assertFalse(check(result, 'ORDER_MAX').passed)
        within = evaluate(customer, offer, {}, NOW, True, PurchaseHistory(frozenset({'milk'}), 200))[0]
        self.assertTrue(check(within, 'ORDER_MAX').passed)
        self.assertEqual(within.outcome, 'auto_approve')

    def test_without_history_the_rules_do_not_apply(self):
        customer, offer, _ = purchase()
        result = decision(customer, offer)
        self.assertFalse(any(c.code in ('NEW_PRODUCT', 'ORDER_MAX') for c in result.checks))


class FixTests(unittest.TestCase):
    def test_a_blocked_line_gets_an_orderable_switch(self):
        customer, offer, cand = purchase(status='partial')  # the recommended milk is short of stock
        offer.items[0].recommended_product_id = None
        offer.items[0].candidates.append(Candidate(
            product_id='milk2', name='Other milk', sales_unit='ST', quantity=100, line_total=150,
            price=PriceInfo(unit_price=1.5), availability=Availability(status='ok'), meets_constraints=True))
        self.assertEqual(decision(customer, offer).outcome, 'reject')
        fixes = suggest_fixes(customer, offer, {}, NOW, True)
        switch = next(f for f in fixes if f['kind'] == 'switch')
        self.assertEqual(switch['selections'], {'0': 'milk2'})
        self.assertNotEqual(decision(customer, offer, {0: 'milk2'}).outcome, 'reject')  # the fix really passes

    def test_late_delivery_gets_a_later_date(self):
        customer, offer, cand = purchase(status='late')
        offer.items[0].recommended_product_id = None
        later = offer.parsed.delivery_date + timedelta(days=3)
        cand.availability = Availability(status='late', available_date=later)
        fixes = suggest_fixes(customer, offer, {}, NOW, True)
        date_fix = next(f for f in fixes if f['kind'] == 'rerun')
        self.assertEqual(date_fix['override']['delivery_date'], later.isoformat())

    def test_a_proposed_day_is_only_kept_when_live_availability_confirms_it(self):
        import asyncio
        from gateway.alternatives import verify_date_fixes
        from gateway.planner import _availability, _parse_dt
        customer, offer, cand = purchase(status='late')
        offer.items[0].recommended_product_id = None
        later = offer.parsed.delivery_date + timedelta(days=3)
        cand.availability = Availability(status='late', available_date=later)
        fixes = suggest_fixes(customer, offer, {}, NOW, True)

        class FakeValio:
            def __init__(self, ok):
                self.ok = ok

            async def availability(self, ids, day, customer_id):
                line = {'availableAmount': 999, 'availableDate': day.isoformat() if self.ok else (day + timedelta(days=5)).isoformat()}
                return {pid: {'scheduleLines': [line], 'closingTime': (NOW + timedelta(days=1)).isoformat()} for pid in ids}, {'time_now': NOW.isoformat()}

        kept = asyncio.run(verify_date_fixes(fixes, offer, customer, FakeValio(True), _availability, _parse_dt))
        dropped = asyncio.run(verify_date_fixes(fixes, offer, customer, FakeValio(False), _availability, _parse_dt))
        self.assertTrue(any(f['kind'] == 'rerun' for f in kept))
        self.assertFalse(any('verify' in f for f in kept))
        self.assertFalse(any(f['kind'] == 'rerun' and 'Deliver on' in f['title'] for f in dropped))

    def test_over_budget_gets_a_higher_budget_or_a_smaller_quantity(self):
        customer, offer, _ = purchase(budget=100)  # 100 L for 140
        self.assertEqual(decision(customer, offer).outcome, 'reject')
        fixes = suggest_fixes(customer, offer, {}, NOW, True)
        raise_budget = next(f for f in fixes if f['title'].startswith('Raise the budget'))
        self.assertEqual(raise_budget['override']['budget_eur'], 140)
        smaller = next(f for f in fixes if f['title'].startswith('Order 71'))  # 100 / 1.40 per litre
        self.assertEqual(smaller['override']['items'][0]['quantity'], 71)

    def test_an_expired_offer_gets_a_refresh_only(self):
        customer, offer, _ = purchase()
        fixes = suggest_fixes(customer, offer, {}, NOW + timedelta(hours=2), True)
        self.assertEqual([f['kind'] for f in fixes], ['refresh'])

    def test_allergen_rules_are_never_fixed_by_a_substitute_that_breaks_them(self):
        customer, offer, cand = purchase(strict=['AP'])  # no explicit evidence: blocked
        self.assertEqual(decision(customer, offer).outcome, 'reject')
        fixes = suggest_fixes(customer, offer, {}, NOW, True)
        self.assertFalse(any(f['kind'] == 'switch' for f in fixes))

    def test_an_order_that_can_be_placed_gets_no_fixes(self):
        customer, offer, _ = purchase()
        self.assertEqual(suggest_fixes(customer, offer, {}, NOW, True), [])


if __name__ == '__main__':
    unittest.main()
