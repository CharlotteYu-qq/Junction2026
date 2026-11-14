"""The ordering agent asks for what is missing before it prepares an offer."""
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent import dialogue
from agent.rules_parser import parse, stated_amounts
from contracts.models import ParsedRequest, RequestItem
from gateway import app as gateway_app
from gateway.config import settings
from gateway.store import Store
from trust.audit import AuditLog
from trust.auth import SessionSigner
from trust.customers import CustomerRegistry
from trust.signing import Signer

TODAY = date(2026, 11, 14)  # a Saturday


class FollowUpTests(unittest.TestCase):
    def test_missing_quantity_and_date_are_asked_for(self):
        question = dialogue.follow_up_question(parse("I need milk", TODAY))
        self.assertIn("how much milk", question)
        self.assertIn("which day", question)

    def test_complete_request_needs_no_question(self):
        self.assertEqual(dialogue.follow_up_question(parse("200 litres of milk by Friday", TODAY)), "")

    def test_nothing_recognised_asks_what_to_order(self):
        self.assertIn("What would you like to order", dialogue.follow_up_question(parse("hello", TODAY)))

    def test_estimate_must_be_confirmed(self):
        estimate = ParsedRequest(items=[RequestItem(text="milk for 120", product_query="milk", quantity=30, unit="L",
                                                    assumption="0.25 L per person")], delivery_date=date(2026, 11, 20))
        self.assertIn("whether 30 litres of milk is right", dialogue.follow_up_question(estimate))
        confirmed = dialogue.merge_answer(estimate, "Yes, that's right", TODAY)
        self.assertTrue(dialogue.is_complete(confirmed))

    def test_claude_phrasing_is_used_only_when_something_is_missing(self):
        self.assertEqual(dialogue.follow_up_question(parse("I need milk", TODAY), "Kuinka paljon maitoa?"), "Kuinka paljon maitoa?")
        self.assertEqual(dialogue.follow_up_question(parse("200 l milk by Friday", TODAY), "Anything else?"), "")


class MergeAnswerTests(unittest.TestCase):
    def test_answer_without_product_fills_the_open_item(self):
        merged = dialogue.merge_answer(parse("I need milk", TODAY), "200 litres, Friday", TODAY)
        item = merged.items[0]
        self.assertEqual((item.product_query, item.quantity, item.unit), ("milk", 200, "L"))
        self.assertEqual(merged.delivery_date, date(2026, 11, 20))
        self.assertFalse(merged.delivery_date_assumed)
        self.assertTrue(dialogue.is_complete(merged))

    def test_amounts_go_to_open_items_in_order(self):
        merged = dialogue.merge_answer(parse("milk and butter for Monday", TODAY), "200 litres and 10 kg", TODAY)
        self.assertEqual([(i.product_query, i.quantity, i.unit) for i in merged.items], [("milk", 200, "L"), ("butter", 10, "KG")])

    def test_named_product_updates_it_and_new_products_are_added(self):
        previous = parse("200 litres of milk by Friday", TODAY)
        merged = dialogue.merge_answer(previous, "make it 300 litres of milk, and 5 kg butter", TODAY)
        self.assertEqual([(i.product_query, i.quantity) for i in merged.items], [("milk", 300), ("butter", 5)])
        self.assertEqual(merged.delivery_date, previous.delivery_date)  # not repeated, still known

    def test_stated_amounts_ignore_dates_and_money(self):
        self.assertEqual(stated_amounts("200 litres on 20.11., budget 500 EUR", TODAY), [(200, "L")])


class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        for p in (
            patch.multiple(gateway_app.S, store=Store(runtime), customers=CustomerRegistry(runtime / 'customers.json'),
                           audit=AuditLog(runtime / 'audit.jsonl'), signer=Signer(runtime / 'key.pem'),
                           sessions=SessionSigner(runtime / 'session_secret')),
            patch.object(gateway_app, 'settings', replace(settings, buyer_password='', valio_password='', anthropic_api_key='')),
            patch.object(gateway_app, '_today', lambda: TODAY),
            patch.object(gateway_app, '_need_catalog', lambda: None),
            patch.object(gateway_app, '_offer_response', AsyncMock(return_value={'offer': {'id': 'OF-test'}})),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(gateway_app.app)
        self.client.post('/api/session/login', json={'role': 'buyer', 'customer_id': 'C-1001'}).raise_for_status()

    def test_agent_asks_then_prepares_the_offer(self):
        first = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'I need milk'}]}).json()
        self.assertEqual(first['kind'], 'question')
        self.assertIn('how much milk', first['question'])
        messages = [{'role': 'buyer', 'text': 'I need milk'}, {'role': 'agent', 'text': first['question']},
                    {'role': 'buyer', 'text': '200 litres, Friday'}]
        second = self.client.post('/api/chat', json={'messages': messages, 'understood': first['understood']}).json()
        self.assertEqual(second['kind'], 'offer')
        parsed = gateway_app._offer_response.await_args.args[1]
        self.assertEqual((parsed.items[0].quantity, parsed.items[0].unit, parsed.delivery_date), (200, 'L', date(2026, 11, 20)))
        actions = [e.action for e in gateway_app.S.audit.events()]
        self.assertIn('agent.asked', actions)

    def test_buyer_can_skip_the_questions(self):
        first = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'I need milk'}]}).json()
        skipped = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'I need milk'}],
                                                      'understood': first['understood'], 'skip_questions': True}).json()
        self.assertEqual(skipped['kind'], 'offer')

    def test_valio_staff_cannot_use_the_buyer_chat(self):
        self.client.post('/api/session/login', json={'role': 'valio', 'name': 'Anna'})
        self.assertEqual(self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'milk'}]}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
