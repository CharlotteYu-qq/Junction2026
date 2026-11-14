"""Valio's answers about the mock API: lot size 0.01 means 1, 999999+ means unknown stock, 429 blocks for 30 s."""
import asyncio
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from gateway import valio_client
from gateway.planner import _availability

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 20)


def row(amount):
    return {"scheduleLines": [{"availableAmount": amount, "availableDate": DAY.isoformat()}],
            "closingTime": "2026-11-18T12:00:00+00:00", "logisticModel": "Warehouse"}


class StockTests(unittest.TestCase):
    def test_999999_or_more_is_unknown_stock_and_any_quantity_is_orderable(self):
        for amount in (999999, 9999999999):
            a = _availability(row(amount), 5_000_000, DAY, NOW)
            self.assertEqual(a.status, "ok")
            self.assertIn("not tracked", a.note)

    def test_a_real_count_below_the_order_is_partial(self):
        self.assertEqual(_availability(row(600), 800, DAY, NOW).status, "partial")

    def test_no_schedule_lines_is_unavailable(self):
        self.assertEqual(_availability({"scheduleLines": []}, 1, DAY, NOW).status, "unavailable")


class LotTests(unittest.TestCase):
    def test_lot_size_001_is_treated_as_1(self):
        from gateway.catalog import load_catalog
        from gateway.config import settings
        try:
            products = load_catalog(settings.valio_dir).products
        except FileNotFoundError:
            self.skipTest("product data not present")
        self.assertFalse(any(p["lot"] <= 0.01 for p in products))
        self.assertTrue(any(p["lot"] == 1 and p["sales_unit"] == "KG" for p in products))


class RateLimitTests(unittest.TestCase):
    def test_a_429_pauses_calls_for_the_block_and_then_retries(self):
        client = valio_client.ValioClient("http://x", "key")
        responses = [mock.Mock(status_code=429, headers={}), mock.Mock(status_code=200, headers={}, json=lambda: {"ok": 1})]
        slept = []

        async def fake_sleep(s):
            slept.append(s)

        async def fake_post(path, json):
            return responses.pop(0)

        async def run():
            with mock.patch.object(client._client, "post", fake_post), \
                 mock.patch("gateway.valio_client.asyncio.sleep", fake_sleep):
                return await client._post("/api/price", {})

        self.assertEqual(asyncio.run(run()), {"ok": 1})
        self.assertTrue(any(s >= valio_client.BLOCK_SECONDS - 1 for s in slept))


if __name__ == "__main__":
    unittest.main()
