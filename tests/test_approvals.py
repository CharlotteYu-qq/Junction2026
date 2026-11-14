"""Approval must not bypass the current hard checks, including legacy pending orders."""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from gateway import app as gateway_app
from gateway.config import TZ
from gateway.planner import verify_offer
from gateway.store import Store
from trust.audit import AuditLog
from trust.auth import Principal
from trust.customers import CustomerRegistry
from trust.policy import evaluate
from trust.signing import Signer, content_hash
from contracts.models import Offer, OfferItem, Order, Candidate, PriceInfo, Availability, ParsedRequest, RequestItem

STAFF = Principal(role='valio', name='Reviewer')


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        runtime = Path(self.temp.name)
        self.state = patch.multiple(gateway_app.S, store=Store(runtime),
                                    customers=CustomerRegistry(runtime / 'customers.json'),
                                    audit=AuditLog(runtime / 'audit.jsonl'), signer=Signer(runtime / 'key.pem'))
        self.state.start()
        self.addCleanup(self.state.stop)

    def pending(self, *, school=False, expired=False):
        now = datetime.now(TZ)
        customer = gateway_app.S.customers.get('C-1002' if school else 'C-1001')
        req = RequestItem(text='100 L milk', product_query='milk', quantity=100, unit='L')
        cand = Candidate(product_id='milk', name='Milk', sales_unit='ST', quantity=100,
                         line_total=1400, price=PriceInfo(unit_price=14), availability=Availability(status='ok'))
        parsed = ParsedRequest(items=[req], delivery_date=now.date() + timedelta(days=2))
        offer = Offer(id='OF-review', customer_id=customer.id, created_at=now,
                      valid_until=now + timedelta(minutes=-1 if expired else 30), request_text='100 L milk',
                      parsed=parsed, items=[OfferItem(request=req, candidates=[cand], recommended_product_id='milk')],
                      operations=[])
        offer.content_hash = content_hash(offer.model_dump(mode='json', exclude={'content_hash', 'signature'}))
        offer.signature = gateway_app.S.signer.sign(offer.content_hash.encode())
        gateway_app.S.store.save_offer(offer)
        result, lines = evaluate(customer, offer, {}, now, verify_offer(offer, gateway_app.S.signer))
        order = Order(id='O-review', offer_id=offer.id, customer_id=customer.id, customer_name=customer.name,
                      created_at=now, status='pending_approval', lines=lines, total=result.total, decision=result)
        gateway_app.S.store.save_order(order)
        return order, customer

    def test_over_limit_order_can_be_approved(self):
        order, customer = self.pending()
        before = customer.past_orders
        result = gateway_app.decide_order(order.id, gateway_app.DecisionIn(decision='approve'), STAFF)
        self.assertEqual(result['status'], 'placed')
        self.assertEqual(customer.past_orders, before + 1)

    def test_expired_or_unsafe_legacy_pending_order_cannot_be_approved(self):
        for kwargs in ({'expired': True}, {'school': True}):
            with self.subTest(kwargs=kwargs):
                order, customer = self.pending(**kwargs)
                before = customer.past_orders
                with self.assertRaises(HTTPException) as error:
                    gateway_app.decide_order(order.id, gateway_app.DecisionIn(decision='approve'), STAFF)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(order.status, 'pending_approval')
                self.assertIsNone(order.reviewer)
                self.assertEqual(customer.past_orders, before)

    def test_rejecting_expired_order_remains_possible(self):
        order, _ = self.pending(expired=True)
        result = gateway_app.decide_order(order.id, gateway_app.DecisionIn(decision='reject'), STAFF)
        self.assertEqual(result['status'], 'rejected')

    def test_blank_reviewer_cannot_decide(self):
        order, _ = self.pending()
        with self.assertRaises(HTTPException):
            gateway_app.decide_order(order.id, gateway_app.DecisionIn(decision='approve'), Principal(role='valio', name='   '))
        self.assertEqual(order.status, 'pending_approval')

    def test_signed_snapshot_does_not_allow_changed_price(self):
        order, _ = self.pending()
        gateway_app.S.store.offers[order.offer_id].items[0].candidates[0].line_total = 999
        with self.assertRaises(HTTPException):
            gateway_app.decide_order(order.id, gateway_app.DecisionIn(decision='approve'), STAFF)


if __name__ == '__main__':
    unittest.main()
