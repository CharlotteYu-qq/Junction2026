"""Regression scenarios for buyer limits and non-overridable purchase requirements."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from contracts.models import (
    Availability, Candidate, Constraints, Customer, Offer, OfferItem, ParsedRequest,
    PriceInfo, RequestItem,
)
from trust.policy import evaluate

NOW = datetime(2026, 11, 14, 12, tzinfo=ZoneInfo('Europe/Helsinki'))


def purchase(*, strict=None, constraints=None, quantity=100, unit='L', budget=None,
             substitution=True, assumed=False, clarification=False, status='ok', meets=True):
    customer = Customer(id='C-test', name='Test kitchen', segment='school', business_id='1618033-3',
                        business_id_valid=True, status='trusted', auto_approve_limit=1000,
                        strict_allergens=strict or [])
    request = RequestItem(text='100 L milk', product_query='milk', quantity=quantity, unit=unit,
                          constraints=constraints or Constraints())
    candidate = Candidate(product_id='milk', name='Milk', sales_unit='ST', quantity=100,
                          line_total=140, price=PriceInfo(unit_price=1.4),
                          availability=Availability(status=status, available_amount=27),
                          meets_constraints=meets, constraint_notes=[] if meets else ['not lactose-free'])
    parsed = ParsedRequest(items=[request], delivery_date=NOW.date() + timedelta(days=2),
                           budget_eur=budget, substitution_allowed=substitution,
                           delivery_date_assumed=assumed, clarification_required=clarification)
    offer = Offer(id='OF-test', customer_id=customer.id, created_at=NOW,
                  valid_until=NOW + timedelta(minutes=30), request_text='100 L milk',
                  parsed=parsed, items=[OfferItem(request=request, candidates=[candidate],
                                                recommended_product_id='milk')], operations=[])
    return customer, offer, candidate


def decision(customer, offer, selections=None, now=NOW):
    return evaluate(customer, offer, selections or {}, now, True)[0]


class PolicyTests(unittest.TestCase):
    def test_normal_order_and_authority_limit(self):
        customer, offer, _ = purchase()
        self.assertEqual(decision(customer, offer).outcome, 'auto_approve')
        customer.auto_approve_limit = 100
        self.assertEqual(decision(customer, offer).outcome, 'needs_approval')

    def test_unknown_or_merely_undeclared_allergen_blocks(self):
        for contains in ([], ['AM']):
            customer, offer, cand = purchase(strict=['AP'])
            cand.allergens_contains = contains
            result = decision(customer, offer)
            self.assertEqual(result.outcome, 'reject')
            self.assertTrue(any(c.code == 'ALLERGEN_SAFETY' and c.severity == 'block' and not c.passed
                                for c in result.checks))

    def test_explicit_allergen_evidence_passes_but_conflicting_data_blocks(self):
        customer, offer, cand = purchase(strict=['AP'])
        cand.free_from = ['AP']
        self.assertEqual(decision(customer, offer).outcome, 'auto_approve')
        cand.allergens_may_contain = ['AP']
        self.assertEqual(decision(customer, offer).outcome, 'reject')

    def test_request_allergen_rule_blocks_even_without_customer_rule(self):
        customer, offer, _ = purchase(constraints=Constraints(free_from_allergens=['AM']))
        self.assertEqual(decision(customer, offer).outcome, 'reject')

    def test_budget_is_distinct_from_auto_approval_limit(self):
        customer, offer, _ = purchase(budget=100)
        self.assertEqual(decision(customer, offer).outcome, 'reject')
        offer.parsed.budget_eur = 200
        customer.auto_approve_limit = 100
        self.assertEqual(decision(customer, offer).outcome, 'needs_approval')

    def test_no_substitution_blocks_nonmatching_option(self):
        customer, offer, _ = purchase(substitution=False, meets=False)
        self.assertEqual(decision(customer, offer).outcome, 'reject')
        offer.parsed.substitution_allowed = True
        self.assertEqual(decision(customer, offer).outcome, 'needs_approval')

    def test_incomplete_or_ambiguous_request_blocks(self):
        for kwargs in ({'assumed': True}, {'clarification': True}, {'quantity': None}, {'unit': None}):
            with self.subTest(kwargs=kwargs):
                customer, offer, _ = purchase(**kwargs)
                self.assertEqual(decision(customer, offer).outcome, 'reject')

    def test_unfulfillable_stock_or_date_requires_revised_offer(self):
        for status in ('partial', 'late', 'cutoff_passed', 'unknown', 'unavailable'):
            with self.subTest(status=status):
                customer, offer, _ = purchase(status=status)
                self.assertEqual(decision(customer, offer).outcome, 'reject')

    def test_missing_line_cannot_be_approved_as_partial_request(self):
        customer, offer, _ = purchase()
        request = RequestItem(text='10 kg potatoes', product_query='potato', quantity=10, unit='KG')
        offer.items.append(OfferItem(request=request, candidates=[]))
        self.assertEqual(decision(customer, offer).outcome, 'reject')

    def test_cutoff_and_expiry_are_rechecked(self):
        customer, offer, cand = purchase()
        cand.availability.closing_time = NOW + timedelta(minutes=1)
        self.assertEqual(decision(customer, offer).outcome, 'auto_approve')
        self.assertEqual(decision(customer, offer, now=NOW + timedelta(minutes=2)).outcome, 'reject')
        cand.availability.closing_time = None
        self.assertEqual(decision(customer, offer, now=NOW + timedelta(minutes=31)).outcome, 'reject')

    def test_unknown_selection_is_blocked(self):
        customer, offer, _ = purchase()
        self.assertEqual(decision(customer, offer, {5: 'milk'}).outcome, 'reject')


if __name__ == '__main__':
    unittest.main()
