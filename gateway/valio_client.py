"""Client for the Valio Aimo mock APIs (price & availability).

Valio's docs ask clients to query only products they have already narrowed down, and there are
undisclosed rate limits. So: batch requests, cache answers for a few minutes, back off on 429.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date

import httpx

CACHE_SECONDS = 300


class ValioAPIError(RuntimeError):
    pass


class ValioClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, headers={"X-API-Key": api_key}, timeout=25.0)
        self._cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
        self.has_key = bool(api_key)
        self.calls = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            r = await self._client.get("/api/ping")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _post(self, path: str, body: dict) -> dict:
        if not self.has_key:
            raise ValioAPIError("AIMO_API_KEY is not set: add it to .env")
        last = ""
        for attempt in range(3):
            try:
                self.calls += 1
                r = await self._client.post(path, json=body)
            except httpx.HTTPError as e:
                last = f"network error: {e}"
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise ValioAPIError("The Valio Aimo API rejected the API key: check AIMO_API_KEY in .env")
            if r.status_code >= 400:
                raise ValioAPIError(f"{path} returned HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise ValioAPIError(f"{path} failed after 3 attempts ({last})")

    async def _lookup(self, kind: str, path: str, ids: list[str], requested: date, customer: str) -> tuple[dict, dict]:
        now = time.time()
        day = requested.isoformat()
        out: dict[str, dict] = {}
        missing: list[str] = []
        for pid in dict.fromkeys(ids):
            hit = self._cache.get((kind, pid, day))
            if hit and hit[0] > now:
                out[pid] = hit[1]
            else:
                missing.append(pid)
        meta = {"endpoint": path, "requested": len(missing), "from_cache": len(out), "time_now": None}
        for i in range(0, len(missing), 1000):  # API limit: 1-1000 products per request
            chunk = missing[i : i + 1000]
            data = await self._post(path, {"customer": customer, "requestedDate": day, "products": chunk})
            meta["time_now"] = data.get("timeNow") or meta["time_now"]
            meta["currency"] = data.get("currency") or meta.get("currency")
            for row in data.get("products", []):
                pid = str(row.get("productId"))
                out[pid] = row
                self._cache[(kind, pid, day)] = (now + CACHE_SECONDS, row)
        return out, meta

    async def price(self, ids: list[str], requested: date, customer: str) -> tuple[dict, dict]:
        return await self._lookup("price", "/api/price", ids, requested, customer)

    async def availability(self, ids: list[str], requested: date, customer: str) -> tuple[dict, dict]:
        return await self._lookup("availability", "/api/availability", ids, requested, customer)
