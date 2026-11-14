"""The ordering agent asks for what is missing before it prepares an offer."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent import dialogue
from agent.rules_parser import parse, stated_amounts
from contracts.models import Decision, Order, OrderLine, ParsedRequest, RequestItem
from gateway import app as gateway_app
from gateway.config import TZ, settings
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

    def test_a_bare_amount_corrects_the_item_in_that_unit(self):
        merged = dialogue.merge_answer(parse("200 litres of milk for Friday", TODAY), "Actually make it 300 litres", TODAY)
        self.assertEqual([(i.product_query, i.quantity) for i in merged.items], [("milk", 300)])


class RulesDateTests(unittest.TestCase):
    def test_recently_passed_date_is_asked_for_not_moved_a_year(self):
        request = parse("100 litres of milk for 10.11.", TODAY)
        self.assertTrue(request.delivery_date_assumed)
        self.assertIn("which day", dialogue.follow_up_question(request))

    def test_day_of_month_with_ordinal(self):
        self.assertEqual(parse("30 kg rice on the 25th", TODAY).delivery_date, date(2026, 11, 25))
        self.assertEqual(parse("30 kg rice on the 3rd", TODAY).delivery_date, date(2026, 12, 3))
        self.assertEqual(parse("25 kg rice for Friday", TODAY).items[0].quantity, 25)  # "25 kg" is not a date


class ClaudeAmountGuardTests(unittest.TestCase):
    def _parsed(self, *items):
        return ParsedRequest(items=[RequestItem(text="", product_query=p, quantity=q, unit=u) for p, q, u in items],
                             delivery_date=date(2026, 11, 20))

    def test_amount_written_with_a_unit_overrides_a_misread(self):
        from agent.claude_parser import check_amounts
        parsed = self._parsed(("dill", 500, "KG"))
        check_amounts(parsed, "500 g fresh dill for Friday", TODAY)
        self.assertEqual(parsed.items[0].quantity, 0.5)

    def test_ambiguous_misread_must_be_confirmed(self):
        from agent.claude_parser import check_amounts
        parsed = self._parsed(("dill", 500, "KG"), ("parsley", 0.3, "KG"))
        check_amounts(parsed, "500 g dill and 300 g parsley", TODAY)
        self.assertTrue(parsed.items[0].assumption)
        self.assertFalse(parsed.items[1].assumption)

    def test_matching_and_unitless_amounts_are_left_alone(self):
        from agent.claude_parser import check_amounts
        parsed = self._parsed(("milk", 200, "L"), ("egg", 200, "PCS"))
        check_amounts(parsed, "200 litres of milk and 200 eggs", TODAY)
        self.assertEqual([(i.quantity, i.assumption) for i in parsed.items], [(200, ""), (200, "")])


class ChatCase(unittest.TestCase):
    """Shared setup: a signed-in buyer (C-1001), rules parser, no catalog or Valio calls."""

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        for p in (
            patch.multiple(gateway_app.S, store=Store(runtime), customers=CustomerRegistry(runtime / 'customers.json'),
                           audit=AuditLog(runtime / 'audit.jsonl'), signer=Signer(runtime / 'key.pem'),
                           sessions=SessionSigner(runtime / 'session_secret')),
            patch.object(gateway_app, 'settings', replace(settings, buyer_password='', anthropic_api_key='')),
            patch.object(gateway_app, '_today', lambda: TODAY),
            patch.object(gateway_app, '_need_catalog', lambda: None),
            patch.object(gateway_app, '_offer_response', AsyncMock(return_value={'offer': {'id': 'OF-test'}})),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(gateway_app.app)
        self.client.post('/api/session/login', json={'role': 'buyer', 'customer_id': 'C-1001'}).raise_for_status()

class ChatEndpointTests(ChatCase):
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
        # the rule-based parser understood it (Claude unreachable): the buyer is reminded every time
        self.assertIn('double-check', first['notice'])
        self.assertIn('double-check', second['notice'])

    def test_no_reminder_when_claude_understood_the_request(self):
        parsed = ParsedRequest(items=[RequestItem(text='200 l milk', product_query='milk', quantity=200, unit='L')],
                               delivery_date=date(2026, 11, 20), parser='claude')
        with patch.object(gateway_app, 'parse_conversation', AsyncMock(return_value=(parsed, '', 5))):
            result = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': '200 l milk Friday'}]}).json()
        self.assertEqual(result['kind'], 'offer')
        self.assertIsNone(result['notice'])

    def test_buyer_can_skip_the_questions(self):
        first = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'I need milk'}]}).json()
        skipped = self.client.post('/api/chat', json={'messages': [{'role': 'buyer', 'text': 'I need milk'}],
                                                      'understood': first['understood'], 'skip_questions': True}).json()
        self.assertEqual(skipped['kind'], 'offer')


class CancelIntentTests(unittest.TestCase):
    def test_recognised_in_three_languages_with_or_without_a_number(self):
        cases = {"Cancel my last order": None, "cancel order O-0009": "O-0009", "please cancel o9": "O-0009",
                 "Peruuta tilaus": None, "Jag vill avbeställa min beställning": None}
        for text, order_id in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dialogue.cancel_intent(text), {"order_id": order_id})

    def test_not_a_cancellation(self):
        for text in ("cancel", "Don't cancel my order", "200 kg perunaa for Friday", "I want to order milk"):
            with self.subTest(text=text):
                self.assertIsNone(dialogue.cancel_intent(text))


class CancelInChatTests(ChatCase):
    def setUp(self):
        super().setUp()
        now = datetime.now(TZ)
        line = OrderLine(item_index=0, product_id="milk", name="Milk", quantity=10, sales_unit="ST", unit_price=1.0,
                         line_total=10.0, delivery_date=now.date() + timedelta(days=5))
        gateway_app.S.store.save_order(Order(
            id="O-0001", offer_id="OF-gone", customer_id="C-1001", customer_name="Hotel Aurora Helsinki", created_at=now,
            status="placed", lines=[line], total=10.0,
            decision=Decision(outcome="auto_approve", summary="ok", checks=[], total=10.0)))

    def say(self, text):
        return self.client.post("/api/chat", json={"messages": [{"role": "buyer", "text": text}]}).json()

    def test_cancel_the_latest_order_after_confirming(self):
        reply = self.say("Please cancel my last order, the event was called off")
        self.assertEqual((reply["kind"], reply["order"]["id"]), ("cancel", "O-0001"))
        self.assertIn("Do you want me to cancel order O-0001", reply["question"])
        self.assertEqual(gateway_app.S.store.orders["O-0001"].status, "placed")  # nothing happens before confirming
        self.client.post("/api/orders/O-0001/cancel", json={"reason": "the event was called off"}).raise_for_status()
        again = self.say("cancel my order")
        self.assertEqual(again["kind"], "answer")
        self.assertIn("no placed order", again["text"])

    def test_unknown_or_finished_orders(self):
        self.assertIn("can't find order O-0099", self.say("cancel order O-0099")["text"])
        gateway_app.S.store.orders["O-0001"].status = "rejected"
        self.assertIn("was not placed", self.say("cancel order O-1")["text"])


if __name__ == '__main__':
    unittest.main()
