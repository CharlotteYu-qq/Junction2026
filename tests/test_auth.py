"""Sign-in: the buyer portal and the Valio Aimo admin are separate, and the server enforces it."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from contracts.models import Decision, Offer, Order, ParsedRequest
from gateway import app as gateway_app
from gateway.config import TZ, settings
from gateway.store import Store
from trust.audit import AuditLog
from trust.auth import COOKIE, Principal, SessionSigner
from trust.customers import CustomerRegistry
from trust.signing import Signer


class SessionTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer = SessionSigner(Path(self.temp.name) / 'secret')

    def test_round_trip(self):
        who = Principal(role='buyer', name='Hotel Aurora Helsinki', customer_id='C-1001')
        self.assertEqual(self.signer.read(self.signer.issue(who)), who)

    def test_tampered_expired_or_foreign_tokens_are_rejected(self):
        token = self.signer.issue(Principal(role='buyer', name='Hotel', customer_id='C-1001'))
        payload, mac = token.split('.')
        forged = self.signer.issue(Principal(role='valio', name='Mallory')).split('.')[0]
        self.assertIsNone(self.signer.read(f'{forged}.{mac}'))  # someone else's signature
        self.assertIsNone(self.signer.read(token, now=datetime.now().timestamp() + 13 * 3600))  # expired
        other = SessionSigner(Path(self.temp.name) / 'other-secret')
        self.assertIsNone(other.read(token))  # signed with a different secret
        for junk in ('', 'abc', 'a.b.c', None):
            self.assertIsNone(self.signer.read(junk))


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        runtime = Path(self.temp.name)
        state = patch.multiple(gateway_app.S, store=Store(runtime), customers=CustomerRegistry(runtime / 'customers.json'),
                               audit=AuditLog(runtime / 'audit.jsonl'), signer=Signer(runtime / 'key.pem'),
                               sessions=SessionSigner(runtime / 'session_secret'))
        state.start()
        self.addCleanup(state.stop)
        self.use_passwords(buyer='buyer-pw', valio='valio-pw')
        now = datetime.now(TZ)
        for customer, n in (('C-1001', 1), ('C-1002', 2)):
            offer = Offer(id=f'OF-{customer}', customer_id=customer, created_at=now, valid_until=now + timedelta(minutes=30),
                          request_text='milk', parsed=ParsedRequest(items=[], delivery_date=now.date()), items=[], operations=[])
            gateway_app.S.store.save_offer(offer)
            gateway_app.S.store.save_order(Order(
                id=f'O-000{n}', offer_id=offer.id, customer_id=customer, customer_name=customer, created_at=now,
                status='pending_approval', lines=[], total=0.0,
                decision=Decision(outcome='needs_approval', summary='needs a person', checks=[], total=0.0)))
        self.client = TestClient(gateway_app.app)

    def use_passwords(self, buyer='', valio=''):
        p = patch.object(gateway_app, 'settings', replace(settings, buyer_password=buyer, valio_password=valio))
        p.start()
        self.addCleanup(p.stop)

    def login(self, **body):
        return self.client.post('/api/session/login', json=body)

    def test_api_needs_a_session(self):
        self.assertEqual(self.client.get('/api/orders').status_code, 401)
        session = self.client.get('/api/session').json()
        self.assertFalse(session['signed_in'])
        self.assertTrue(session['password_required'])
        self.assertEqual({c['id'] for c in session['customers']}, {'C-1001', 'C-1002', 'C-2001'})

    def test_wrong_password_is_rejected(self):
        self.assertEqual(self.login(role='buyer', customer_id='C-1001', password='nope').status_code, 401)
        self.assertEqual(self.login(role='valio', name='Anna', password='buyer-pw').status_code, 401)
        self.assertEqual(self.client.get('/api/orders').status_code, 401)

    def test_buyer_sees_only_its_own_data_and_cannot_use_admin(self):
        self.assertEqual(self.login(role='buyer', customer_id='C-1001', password='buyer-pw').status_code, 200)
        self.assertEqual([c['id'] for c in self.client.get('/api/customers').json()], ['C-1001'])
        self.assertEqual([o['id'] for o in self.client.get('/api/orders').json()], ['O-0001'])
        self.assertEqual(self.client.get('/api/offers/OF-C-1002').status_code, 404)
        self.assertEqual(self.client.post('/api/requests', json={'customer_id': 'C-1002', 'text': 'milk'}).status_code, 403)
        self.assertEqual(self.client.post('/api/orders/O-0001/decision', json={'decision': 'approve'}).status_code, 403)
        self.assertEqual(self.client.get('/api/audit').status_code, 403)
        self.assertEqual(self.client.post('/api/admin/reset').status_code, 403)
        self.assertEqual(self.client.post('/api/customers/C-2001/verify').status_code, 403)

    def test_valio_staff_review_but_do_not_order(self):
        self.assertEqual(self.login(role='valio', name='Anna', password='valio-pw').status_code, 200)
        self.assertEqual(len(self.client.get('/api/customers').json()), 3)
        self.assertEqual(len(self.client.get('/api/orders').json()), 2)
        self.assertEqual(self.client.post('/api/requests', json={'text': 'milk'}).status_code, 403)
        result = self.client.post('/api/orders/O-0002/decision', json={'decision': 'reject', 'comment': 'test'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['reviewer'], 'Anna')
        actors = [e.actor for e in gateway_app.S.audit.events()]
        self.assertIn('human:Anna', actors)

    def test_sign_out_ends_the_session(self):
        self.login(role='valio', name='Anna', password='valio-pw')
        self.client.post('/api/session/logout')
        self.assertEqual(self.client.get('/api/orders').status_code, 401)

    def test_forged_cookie_is_ignored(self):
        self.client.cookies.set(COOKIE, 'eyJyIjoidmFsaW8ifQ.forged')
        self.assertEqual(self.client.get('/api/orders').status_code, 401)

    def test_local_development_needs_no_password(self):
        self.use_passwords()
        self.assertEqual(self.login(role='buyer', customer_id='C-1002').status_code, 200)
        self.assertEqual([c['id'] for c in self.client.get('/api/customers').json()], ['C-1002'])

    def test_unset_valio_password_disables_admin_sign_in(self):
        self.use_passwords(buyer='buyer-pw')
        self.assertEqual(self.login(role='valio', name='Anna', password='').status_code, 401)
        self.assertEqual(self.login(role='valio', name='Anna', password='buyer-pw').status_code, 401)


if __name__ == '__main__':
    unittest.main()
