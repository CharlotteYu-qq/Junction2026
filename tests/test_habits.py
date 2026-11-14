"""The agent uses the customer's order history: "the usual", "same as last time", the usual amount when
none is given, the usual product first, and changes to the order being discussed."""
import unittest
from collections import Counter
from datetime import date

from agent import dialogue, rules_parser
from gateway import habits
from gateway.planner import _rank
from tests.test_policy import purchase

TODAY = date(2026, 11, 14)


def order(n, day, lines):
    return {"id": f"H-{n}", "ordered_at": f"{day}T10:00:00+02:00", "delivery_date": day, "status": "delivered",
            "note": None, "lines": [{"request": r, "quantity": q, "unit": u, "product_id": pid} for r, q, u, pid in lines]}


ORDERS = [
    order(1, "2026-10-02", [("lactose-free milk", 100, "L", "eila"), ("butter", 20, "KG", "b1")]),
    order(2, "2026-10-09", [("lactose-free milk", 120, "L", "eila"), ("butter", 20, "KG", "b1")]),
    order(3, "2026-10-16", [("lactose-free milk", 120, "L", "eila")]),
    order(4, "2026-10-23", [("lactose-free milk", 120, "L", "other")]),
    order(5, "2026-11-06", [("lactose-free milk", 110, "L", "eila"), ("butter", 25, "KG", "b1")]),
]


def parse(text):
    return rules_parser.parse(text, TODAY)


class HabitTests(unittest.TestCase):
    def test_the_usual_milk_is_the_usual_item_and_amount_without_a_question(self):
        p = parse("the usual milk for Friday")
        used = habits.apply(p, ["the usual milk for Friday"], None, ORDERS, TODAY)
        self.assertTrue(used)
        self.assertEqual((p.items[0].quantity, p.items[0].unit), (120, "L"))
        self.assertIn("FREE_FROM_LACTOSE", p.items[0].constraints.required_claims)
        self.assertEqual(p.items[0].assumption, "")
        self.assertTrue(dialogue.is_complete(p))

    def test_a_missing_amount_becomes_the_usual_one_to_confirm(self):
        p = parse("lactose-free milk for Friday")
        habits.apply(p, ["lactose-free milk for Friday"], None, ORDERS, TODAY)
        self.assertEqual(p.items[0].quantity, 120)
        question = dialogue.follow_up_question(p)
        self.assertIn("your usual 120 litres of lactose-free milk", question)
        answered = dialogue.merge_answer(p, "yes", TODAY)  # the buyer confirms: nothing left to ask
        self.assertTrue(dialogue.is_complete(answered))

    def test_same_as_last_time_repeats_the_last_order(self):
        p = parse("same as last time, for Friday")
        habits.apply(p, ["same as last time, for Friday"], None, ORDERS, TODAY)
        self.assertEqual(sorted((i.product_query, i.quantity) for i in p.items), [("butter", 25), ("milk", 110)])

    def test_a_stated_amount_is_kept(self):
        p = parse("50 litres of lactose-free milk for Friday")
        self.assertEqual(habits.apply(p, ["50 litres of lactose-free milk for Friday"], None, ORDERS, TODAY), [])
        self.assertEqual(p.items[0].quantity, 50)

    def test_no_history_changes_nothing(self):
        p = parse("milk for Friday")
        self.assertEqual(habits.apply(p, ["milk for Friday"], None, [], TODAY), [])
        self.assertIsNone(p.items[0].quantity)

    def test_the_usual_product_is_recommended_first(self):
        _, offer, cand = purchase()
        cheaper = cand.model_copy(update={"product_id": "cheap", "line_total": cand.line_total / 2})
        cands = [cheaper, cand]
        _rank(cands, Counter({cand.product_id: 5}))
        self.assertEqual(cands[0].product_id, cand.product_id)
        self.assertIn("your usual choice", cands[0].rank_reason)
        _rank(cands)  # without history the cheaper one wins
        self.assertEqual(cands[0].product_id, "cheap")


class EditTests(unittest.TestCase):
    def setUp(self):
        self.current = parse("40 kg butter and 200 litres of lactose-free milk for Friday")

    def test_more_adds_to_the_amount(self):
        out = dialogue.merge_answer(self.current, "add 10 kg more butter", TODAY)
        self.assertEqual(next(i.quantity for i in out.items if i.product_query == "butter"), 50)

    def test_remove_takes_a_product_out(self):
        out = dialogue.merge_answer(self.current, "remove the milk", TODAY)
        self.assertEqual([i.product_query for i in out.items], ["butter"])

    def test_another_day_keeps_the_products(self):
        out = dialogue.merge_answer(self.current, "deliver on Thursday instead", TODAY)
        self.assertEqual(out.delivery_date.weekday(), 3)
        self.assertEqual(len(out.items), 2)


if __name__ == "__main__":
    unittest.main()
