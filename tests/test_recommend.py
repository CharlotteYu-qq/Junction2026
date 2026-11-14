import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from agent import rules_parser
from gateway import recommend

TODAY = date(2026, 11, 14)  # a Saturday


def order(day: date, *lines: tuple[str, float, str]) -> dict:
    return {"id": f"H-{day}-{lines[0][0]}", "ordered_at": f"{day - timedelta(days=1)}T09:00:00+02:00", "delivery_date": day.isoformat(),
            "lines": [{"request": r, "quantity": q, "unit": u} for r, q, u in lines]}


def history() -> list[dict]:
    orders = []
    for n in range(20):  # milk every 3 days (last 4 days ago: overdue), butter weekly (last 2 days ago)
        orders.append(order(TODAY - timedelta(days=4 + 3 * n), ("lactose-free milk", 120 + (n % 3) * 5, "L")))
    for n in range(10):
        orders.append(order(TODAY - timedelta(days=2 + 7 * n), ("butter", 20, "KG")))
    orders.append(order(TODAY - timedelta(days=30), ("saffron", 1, "KG")))  # bought once: not regular
    return sorted(orders, key=lambda o: o["ordered_at"])


class RecommendTests(unittest.TestCase):
    def test_most_due_item_first_and_one_offs_ignored(self):
        out = recommend.suggest(history(), TODAY)
        self.assertIn("lactose-free milk", out[0]["text"])  # 4 days since, every 3 days: the most due
        self.assertFalse(any("saffron" in s["text"] for s in out))
        self.assertLessEqual(len(out), 4)

    def test_suggestions_parse_completely_for_the_agent(self):
        for s in recommend.suggest(history(), TODAY):
            parsed = rules_parser.parse(s["text"], TODAY)
            self.assertTrue(parsed.items, s["text"])
            self.assertFalse(parsed.delivery_date_assumed, s["text"])
            self.assertFalse(parsed.clarification_required, s["text"])
            self.assertTrue(all(i.quantity and i.unit for i in parsed.items), s["text"])
            self.assertGreater(parsed.delivery_date, TODAY)

    def test_delivery_only_on_the_customers_delivery_days(self):
        weekdays = {0, 2}  # Monday, Wednesday
        self.assertEqual(recommend.next_delivery(TODAY - timedelta(days=3), TODAY, weekdays), date(2026, 11, 16))
        self.assertEqual(recommend.next_delivery(date(2026, 11, 17), TODAY, weekdays), date(2026, 11, 18))

    def test_score_is_the_published_weighted_sum(self):
        for i in recommend.score_items(history(), TODAY):
            c = i["components"]
            self.assertTrue(all(0 <= v <= 1 for v in c.values()))
            expected = 0.50 * c["due"] + 0.20 * c["frequency"] + 0.15 * c["regularity"] + 0.15 * c["trend"]
            self.assertAlmostEqual(i["score"], expected, delta=0.01)  # components are shown rounded to 2 decimals
        milk = next(i for i in recommend.score_items(history(), TODAY) if i["item"] == "lactose-free milk")
        self.assertEqual(milk["interval"], 3)
        self.assertEqual(milk["since"], 4)
        self.assertAlmostEqual(milk["components"]["due"], round(min(4 / 3, 2) / 2, 2))
        self.assertEqual(milk["components"]["frequency"], 1.0)  # the most frequent item

    def test_paused_items_are_not_suggested(self):
        orders = history() + [order(TODAY - timedelta(days=60 + 5 * n), ("chanterelles", 5, "KG")) for n in range(8)]
        ranked = {i["item"]: i for i in recommend.score_items(orders, TODAY)}
        self.assertTrue(ranked["chanterelles"]["paused"])  # 60 days since, usually every 5: out of season
        self.assertFalse(any(s["item"] == "chanterelles" for s in recommend.suggest(orders, TODAY)))

    def test_a_holiday_gap_does_not_ruin_regularity(self):
        days = [TODAY - timedelta(days=2 + 3 * n) for n in range(10)] + [TODAY - timedelta(days=100 + 3 * n) for n in range(10)]
        item = recommend.score_items([order(d, ("milk", 100, "L")) for d in sorted(days)], TODAY)[0]
        self.assertGreater(item["components"]["regularity"], 0.9)

    def test_history_stats_and_paging(self):
        from gateway import history as hist
        orders = history()
        orders[0] = {**orders[0], "status": "cancelled"}
        st = hist.stats(orders, TODAY)
        self.assertEqual(st["kpis"]["orders"], len(orders) - 1)
        self.assertEqual(st["kpis"]["cancelled"], 1)
        page = hist.page(orders, q="butter", limit=3)
        self.assertEqual(page["total"], 10)
        self.assertEqual(len(page["orders"]), 3)
        self.assertGreaterEqual(page["orders"][0]["delivery_date"], page["orders"][1]["delivery_date"])  # newest first
        self.assertEqual(hist.page(orders, status="cancelled")["total"], 1)

    def test_plural_and_singular_are_one_item(self):
        self.assertEqual(recommend.item_key("potatoes"), recommend.item_key("potato"))
        self.assertEqual(recommend.item_key("lactose-free milk"), "lactose-free milk")

    def test_an_item_already_ordered_for_a_later_day_is_not_suggested(self):
        orders = history() + [order(TODAY + timedelta(days=2), ("lactose-free milk", 120, "L"))]
        milk = next(i for i in recommend.score_items(orders, TODAY) if i["item"] == "lactose-free milk")
        self.assertTrue(milk["scheduled"])
        self.assertEqual(milk["components"]["due"], 0.0)
        self.assertFalse(any(s["item"] == "lactose-free milk" for s in recommend.suggest(orders, TODAY)))

    def test_a_portal_order_joins_the_history(self):
        from types import SimpleNamespace as NS
        from datetime import datetime
        from gateway import history as hist
        item = NS(product_query="milk", quantity=130.0, unit="L", constraints=NS(required_claims=["FREE_FROM_LACTOSE"]))
        line = NS(item_index=0, product_id="P1", name="Valio milk 1 l lactose free", quantity=130.0, sales_unit="ST",
                  line_total=182.0, delivery_date=TODAY + timedelta(days=2))
        placed = NS(id="O-0009", offer_id="OF-1", status="placed", lines=[line], total=182.0, reviewer=None,
                    created_at=datetime(2026, 11, 14, 10, 0))
        row = hist.from_portal(placed, NS(parsed=NS(items=[item])), TODAY)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["ordered_by"], "Auto-approval")
        approved = hist.from_portal(NS(**{**placed.__dict__, "reviewer": "Anna Virtanen"}), NS(parsed=NS(items=[item])), TODAY)
        self.assertEqual(approved["ordered_by"], "Approved by Anna Virtanen")
        self.assertEqual((row["lines"][0]["request"], row["lines"][0]["quantity"], row["lines"][0]["unit"]), ("lactose-free milk", 130.0, "L"))
        self.assertIsNone(hist.from_portal(NS(**{**placed.__dict__, "status": "rejected"}), None, TODAY))
        # it counts at once: milk is no longer suggested, and the order is in the list
        orders = history() + [row]
        self.assertFalse(any(s["item"] == "lactose-free milk" for s in recommend.suggest(orders, TODAY)))
        self.assertEqual(hist.page(orders, limit=1)["orders"][0]["id"], "O-0009")

    def test_no_history_no_suggestions(self):
        self.assertEqual(recommend.suggest([], TODAY), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(recommend.load_history(Path(tmp) / "missing"), {})
            (Path(tmp) / "C-1.json").write_text(json.dumps({"customer_id": "C-1", "orders": history()}), encoding="utf-8")
            self.assertEqual(len(recommend.load_history(Path(tmp))["C-1"]), len(history()))


if __name__ == "__main__":
    unittest.main()
