"""Orders outside the agent's authority need a named person in the customer's organisation, and that
approval never waives a hard rule (expired offer, allergen evidence, changed prices)."""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from gateway import app as gateway_app
from gateway.config import TZ
from gateway.store import Store
from trust.audit import AuditLog
from trust.auth import Principal
from trust.customers import CustomerRegistry
from trust.signing import Signer, content_hash
from contracts.models import Offer, OfferItem, Candidate, PriceInfo, Availability, ParsedRequest, RequestItem


class SignedOfferCase(unittest.TestCase):
    """Shared setup: temporary storage and a signed offer to order from."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        runtime = Path(self.temp.name)
        self.state = patch.multiple(gateway_app.S, store=Store(runtime),
                                    customers=CustomerRegistry(runtime / 'customers.json'),
                                    audit=AuditLog(runtime / 'audit.jsonl'), signer=Signer(runtime / 'key.pem'))
        self.state.start()
        self.addCleanup(self.state.stop)

    def offer(self, *, school=False, expired=False, total=1400.0):
        """A signed offer for 100 L milk; 1,400 EUR is above the hotel's 1,000 EUR auto-approval limit."""
        now = datetime.now(TZ)
        customer = gateway_app.S.customers.get('C-1002' if school else 'C-1001')
        req = RequestItem(text='100 L milk', product_query='milk', quantity=100, unit='L')
        cand = Candidate(product_id='milk', name='Milk', sales_unit='ST', quantity=100,
                         line_total=total, price=PriceInfo(unit_price=total / 100), availability=Availability(status='ok'))
        parsed = ParsedRequest(items=[req], delivery_date=now.date() + timedelta(days=2))
        offer = Offer(id='OF-review', customer_id=customer.id, created_at=now,
                      valid_until=now + timedelta(minutes=-1 if expired else 30), request_text='100 L milk',
                      parsed=parsed, items=[OfferItem(request=req, candidates=[cand], recommended_product_id='milk')],
                      operations=[])
        offer.content_hash = content_hash(offer.model_dump(mode='json', exclude={'content_hash', 'signature'}))
        offer.signature = gateway_app.S.signer.sign(offer.content_hash.encode())
        gateway_app.S.store.save_offer(offer)
        return offer, customer

    def place(self, offer, customer, approved_by=''):
        who = Principal(role='buyer', name=customer.name, customer_id=customer.id)
        return gateway_app.place_order(gateway_app.OrderIn(offer_id=offer.id, approved_by=approved_by), who)

class ApprovalTests(SignedOfferCase):
    def test_within_the_limit_the_agent_orders_without_a_person(self):
        offer, customer = self.offer(total=140.0)
        result = self.place(offer, customer)
        self.assertEqual((result['status'], result['reviewer']), ('placed', None))

    def test_over_the_limit_needs_a_named_approver(self):
        offer, customer = self.offer()
        before = customer.past_orders
        with self.assertRaises(HTTPException) as error:
            self.place(offer, customer)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(gateway_app.S.store.orders, {})  # nothing was ordered
        result = self.place(offer, customer, approved_by='  Anna   Virtanen ')
        self.assertEqual((result['status'], result['reviewer']), ('placed', 'Anna Virtanen'))
        self.assertEqual(customer.past_orders, before + 1)
        approvals = [e for e in gateway_app.S.audit.events() if e.action == 'order.approved']
        self.assertEqual([e.actor for e in approvals], ['human:Anna Virtanen'])

    def test_a_first_name_alone_is_not_an_approval(self):
        offer, customer = self.offer()
        for name in ('Anna', '   ', 'x'):
            with self.subTest(name=name), self.assertRaises(HTTPException):
                self.place(offer, customer, approved_by=name)
        self.assertEqual(gateway_app.S.store.orders, {})

    def test_approval_cannot_waive_hard_rules(self):
        for kwargs in ({'expired': True}, {'school': True}):  # the school needs allergen evidence the offer lacks
            with self.subTest(kwargs=kwargs):
                gateway_app.S.store.reset()
                offer, customer = self.offer(**kwargs)
                before = customer.past_orders
                result = self.place(offer, customer, approved_by='Anna Virtanen')
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(result['reviewer'])
                self.assertEqual(customer.past_orders, before)

    def test_signed_snapshot_does_not_allow_changed_price(self):
        offer, customer = self.offer()
        gateway_app.S.store.offers[offer.id].items[0].candidates[0].line_total = 999
        result = self.place(offer, customer, approved_by='Anna Virtanen')
        self.assertEqual(result['status'], 'rejected')


class CancelTests(SignedOfferCase):
    """A placed order can be cancelled by its own customer, until the order cut-off of its delivery."""

    def cancel(self, order_id, customer, reason=''):
        who = Principal(role='buyer', name=customer.name, customer_id=customer.id)
        return gateway_app.cancel_order(order_id, gateway_app.CancelIn(reason=reason), who)

    def placed(self):
        offer, customer = self.offer(total=140.0)
        before = (customer.past_orders, customer.avg_order_value)
        order = self.place(offer, customer)
        self.assertEqual(order['status'], 'placed')
        return order, offer, customer, before

    def test_cancel_a_placed_order(self):
        order, _, customer, before = self.placed()
        self.assertIsNotNone(order['cancellable_until'])
        result = self.cancel(order['id'], customer, reason='  menu changed ')
        self.assertEqual((result['status'], result['cancel_reason']), ('cancelled', 'menu changed'))
        self.assertIsNone(result['cancellable_until'])
        self.assertEqual((customer.past_orders, customer.avg_order_value), before)  # history as before the order
        self.assertIn('order.cancelled', [e.action for e in gateway_app.S.audit.events()])

    def test_only_a_placed_order_of_your_own(self):
        order, _, customer, _ = self.placed()
        school = gateway_app.S.customers.get('C-1002')
        with self.assertRaises(HTTPException) as other:
            self.cancel(order['id'], school)
        self.assertEqual(other.exception.status_code, 404)
        self.cancel(order['id'], customer)
        with self.assertRaises(HTTPException) as twice:
            self.cancel(order['id'], customer)
        self.assertEqual(twice.exception.status_code, 409)

    def test_a_rejected_order_cannot_be_cancelled(self):
        offer, customer = self.offer(expired=True)
        order = self.place(offer, customer)
        self.assertEqual(order['status'], 'rejected')
        with self.assertRaises(HTTPException) as error:
            self.cancel(order['id'], customer)
        self.assertEqual(error.exception.status_code, 409)

    def test_too_late_after_the_order_cut_off(self):
        order, offer, customer, _ = self.placed()
        gateway_app.S.store.offers[offer.id].items[0].candidates[0].availability.closing_time = \
            datetime.now(TZ) - timedelta(minutes=1)
        with self.assertRaises(HTTPException) as error:
            self.cancel(order['id'], customer)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn('Too late', error.exception.detail)
        self.assertEqual(gateway_app.S.store.orders[order['id']].status, 'placed')


if __name__ == '__main__':
    unittest.main()
