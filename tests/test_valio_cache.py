"""Customer-specific price and availability lookups must not share cached answers."""
import unittest

from datetime import date
from gateway.valio_client import ValioClient


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_cache_isolation_and_same_customer_hit(self):
        client = ValioClient('https://example.invalid', 'synthetic-test-key')
        self.addAsyncCleanup(client.close)
        calls = []

        async def post(path, body):
            calls.append((path, body['customer']))
            return {'products': [{'productId': 'milk', 'catalogPrice': {'value': len(calls)}}]}

        client._post = post
        day = date(2026, 11, 16)
        for lookup in (client.price, client.availability):
            first, _ = await lookup(['milk'], day, 'customer-A')
            second, _ = await lookup(['milk'], day, 'customer-B')
            cached, meta = await lookup(['milk'], day, 'customer-A')
            self.assertNotEqual(first, second)
            self.assertEqual(cached, first)
            self.assertEqual(meta['from_cache'], 1)
        self.assertEqual(len(calls), 4)


if __name__ == '__main__':
    unittest.main()
