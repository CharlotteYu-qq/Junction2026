"""Generate synthetic order history for the three demo customers (500 orders each).

What is real: every product is an entry of the challenge product data (the same 17,546 products as
data/valio_aimo_junction26_product_data.json / .xlsx), picked with the gateway's own catalog search;
short deliveries follow each product's real delivery reliability; prices start from ONE call to the
live price API for the products used (current comparison price per kg / litre).
What is invented: dates, quantities, buying rhythm, seasons, events, people, kitchens, channels and the
price drift back in time. Every file says so ("synthetic": true).

    python scripts/generate_order_history.py            # writes data/Userdata/order_history/<customer>.json
    python scripts/generate_order_history.py --check    # only show which products each phrase finds
    python scripts/generate_order_history.py --no-prices  # skip the price API (totals left empty)

Deterministic: a fixed seed per customer (and the price API's answer) decides everything.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import rules_parser  # noqa: E402
from gateway.catalog import load_catalog  # noqa: E402
from gateway.config import TZ, settings  # noqa: E402

ORDERS_PER_CUSTOMER = 500
FIRST_DAY, LAST_DAY = date(2025, 11, 14), date(2026, 11, 13)  # one year, up to the day before the demo
PRICE_DATE = date(2026, 11, 16)
OUT_DIR = ROOT / "data" / "Userdata" / "order_history"

ALL_YEAR = {m: 1.0 for m in range(1, 13)}


def season(**months: float) -> dict[int, float]:
    """Month multipliers, e.g. season(jun=1.4, jul=1.5). Unlisted months are 1.0; 0 means not bought."""
    names = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    return {i + 1: months.get(n, 1.0) for i, n in enumerate(names)}


SUMMER_PEAK = season(jun=1.3, jul=1.45, aug=1.25, dec=1.15, jan=0.85)
WINTER_ROOTS = season(oct=1.2, nov=1.25, dec=1.3, jan=1.3, feb=1.2, jun=0.7, jul=0.6, aug=0.8)

# Staple: (request phrase, usual quantity, unit, days between orders, months, preferred weekdays or None)
# A phrase is what a buyer would type; it must parse as one item and find products in the catalog.
PROFILES = {
    "C-1001": {  # Hotel Aurora Helsinki: three hotel kitchens, breakfast buffet, banquets, summer tourists
        "weekdays": {0, 1, 2, 3, 4, 5},
        "strict_allergens": set(),
        "sites": ["Kamppi kitchen", "Töölö kitchen", "Katajanokka kitchen"],
        "people": ["Mikko Laine (head chef)", "Sanna Virtanen (breakfast)", "Jussi Korhonen (purchasing)", "Elina Salo (banquets)"],
        "staples": [
            ("lactose-free milk", 120, "L", 3, SUMMER_PEAK, None),
            ("oat drink", 30, "L", 5, SUMMER_PEAK, None),
            ("lactose-free whipping cream", 40, "L", 4, ALL_YEAR, None),
            ("butter", 20, "KG", 7, ALL_YEAR, None),
            ("yoghurt", 30, "KG", 4, SUMMER_PEAK, None),
            ("quark", 15, "KG", 7, ALL_YEAR, None),
            ("emmental", 15, "KG", 6, ALL_YEAR, None),
            ("gouda", 10, "KG", 8, ALL_YEAR, None),
            ("cream cheese", 8, "KG", 10, ALL_YEAR, None),
            ("ham", 12, "KG", 6, SUMMER_PEAK, None),
            ("bacon", 20, "KG", 5, SUMMER_PEAK, None),
            ("sausages", 15, "KG", 7, ALL_YEAR, None),
            ("smoked salmon", 10, "KG", 7, season(dec=1.6), None),
            ("salmon", 25, "KG", 7, ALL_YEAR, None),
            ("orange juice", 40, "L", 4, SUMMER_PEAK, None),
            ("apple juice", 30, "L", 6, SUMMER_PEAK, None),
            ("coffee", 10, "KG", 12, ALL_YEAR, None),
            ("croissants", 20, "KG", 3, SUMMER_PEAK, None),
            ("rye bread", 15, "KG", 4, ALL_YEAR, None),
            ("muesli", 10, "KG", 10, ALL_YEAR, None),
            ("jam", 6, "KG", 14, ALL_YEAR, None),
            ("cucumbers", 15, "KG", 4, ALL_YEAR, None),
            ("tomatoes", 20, "KG", 4, season(jul=1.3, aug=1.3), None),
            ("cod", 15, "KG", 8, ALL_YEAR, None),
            ("crayfish", 6, "KG", 5, season(jan=0, feb=0, mar=0, apr=0, may=0, jun=0, jul=1.6, aug=2.0, sep=0.6, oct=0, nov=0, dec=0), None),
            ("potatoes", 50, "KG", 6, WINTER_ROOTS, None),
            ("chicken", 25, "KG", 7, ALL_YEAR, None),
        ],
        # Banquets and conferences: a large one-off order of the listed items, a few times a year.
        "events": {"name": "banquet", "count": 14, "factor": (2.5, 4.0),
                   "items": ["salmon", "lactose-free whipping cream", "potatoes", "butter", "emmental", "tomatoes"]},
        "one_offs": ["duck breast", "venison", "shrimps", "olive oil", "honey", "chanterelles", "lingonberry jam", "mozzarella", "arctic char"],
    },
    "C-1002": {  # Kallio School Kitchen: school days only, never tree nuts or peanuts; pea soup on Thursdays
        "weekdays": {0, 1, 2, 3, 4},
        "strict_allergens": {"AN", "AP"},
        "sites": ["Main kitchen"],
        "people": ["Päivi Nieminen (kitchen manager)", "Teemu Mäkelä (cook)", "Riikka Ojala (cook)"],
        "staples": [
            ("milk", 150, "L", 2, ALL_YEAR, None),
            ("lactose-free milk", 40, "L", 3, ALL_YEAR, None),
            ("oat drink", 15, "L", 5, ALL_YEAR, None),
            ("potatoes", 80, "KG", 4, WINTER_ROOTS, None),
            ("carrots", 40, "KG", 5, WINTER_ROOTS, None),
            ("beetroot", 15, "KG", 10, WINTER_ROOTS, None),
            ("cabbage", 20, "KG", 9, WINTER_ROOTS, None),
            ("minced meat", 30, "KG", 7, ALL_YEAR, None),
            ("chicken", 30, "KG", 7, ALL_YEAR, None),
            ("fish fillet", 25, "KG", 7, ALL_YEAR, {2}),
            ("pea soup", 60, "KG", 7, ALL_YEAR, {3}),
            ("oat flakes", 25, "KG", 10, ALL_YEAR, None),
            ("rice", 20, "KG", 10, season(dec=1.6), None),
            ("macaroni", 25, "KG", 8, ALL_YEAR, None),
            ("rye bread", 30, "KG", 3, ALL_YEAR, None),
            ("crispbread", 10, "KG", 10, ALL_YEAR, None),
            ("margarine", 10, "KG", 10, ALL_YEAR, None),
            ("grated cheese", 12, "KG", 7, ALL_YEAR, None),
            ("yoghurt", 25, "KG", 5, ALL_YEAR, None),
            ("cucumbers", 20, "KG", 4, ALL_YEAR, None),
            ("frozen peas", 15, "KG", 9, ALL_YEAR, None),
            ("broccoli", 12, "KG", 8, ALL_YEAR, None),
            ("cauliflower", 10, "KG", 10, ALL_YEAR, None),
            ("meatballs", 25, "KG", 10, ALL_YEAR, None),
            ("sausage", 20, "KG", 12, ALL_YEAR, None),
            ("lingonberry jam", 8, "KG", 14, ALL_YEAR, None),
        ],
        "events": {"name": "school party", "count": 5, "factor": (1.8, 2.5),
                   "items": ["juice", "buns", "yoghurt", "pancake"]},
        "one_offs": ["gingerbread", "buns", "juice", "pancake", "sweet potato", "lentils"],
    },
    "C-2001": {  # Bistro Uusi: open Tuesday to Saturday, fish and seasonal produce, bigger weekend orders
        "weekdays": {1, 2, 3, 4, 5},
        "strict_allergens": set(),
        "sites": ["Bistro kitchen"],
        "people": ["Aino Lehtonen (owner, chef)", "Ville Heikkinen (sous chef)"],
        "staples": [
            ("salmon", 15, "KG", 3, ALL_YEAR, None),
            ("whitefish", 8, "KG", 6, season(sep=1.3, oct=1.3), None),
            ("pike perch", 8, "KG", 7, ALL_YEAR, None),
            ("shrimps", 5, "KG", 8, ALL_YEAR, None),
            ("whipping cream", 20, "L", 4, ALL_YEAR, None),
            ("creme fraiche", 6, "KG", 7, ALL_YEAR, None),
            ("butter", 10, "KG", 6, ALL_YEAR, None),
            ("parmesan", 5, "KG", 12, ALL_YEAR, None),
            ("mozzarella", 6, "KG", 8, season(jun=1.3, jul=1.4, aug=1.3), None),
            ("tomatoes", 25, "KG", 3, season(jul=1.3, aug=1.3), None),
            ("potatoes", 40, "KG", 5, WINTER_ROOTS, None),
            ("onions", 20, "KG", 6, ALL_YEAR, None),
            ("garlic", 3, "KG", 10, ALL_YEAR, None),
            ("mushrooms", 10, "KG", 4, ALL_YEAR, None),
            ("beef", 15, "KG", 5, ALL_YEAR, None),
            ("pork", 12, "KG", 7, ALL_YEAR, None),
            ("wheat flour", 25, "KG", 14, ALL_YEAR, None),
            ("olive oil", 10, "L", 12, ALL_YEAR, None),
            ("risotto rice", 10, "KG", 10, ALL_YEAR, None),
            ("pasta", 12, "KG", 8, ALL_YEAR, None),
            ("chanterelles", 6, "KG", 4, season(jan=0, feb=0, mar=0, apr=0, may=0, jun=0, jul=1.0, aug=1.6, sep=1.6, oct=0.8, nov=0, dec=0), None),
            ("asparagus", 8, "KG", 4, season(jan=0, feb=0, mar=0, apr=0.6, may=1.6, jun=1.4, jul=0, aug=0, sep=0, oct=0, nov=0, dec=0), None),
            ("crayfish", 5, "KG", 5, season(jan=0, feb=0, mar=0, apr=0, may=0, jun=0, jul=1.4, aug=2.0, sep=0.8, oct=0, nov=0, dec=0), None),
            ("reindeer", 6, "KG", 6, season(jan=0.6, feb=0.4, mar=0.3, apr=0, may=0, jun=0, jul=0, aug=0, sep=0.3, oct=0.8, nov=1.4, dec=1.8), None),
        ],
        "events": {"name": "private dinner", "count": 8, "factor": (2.0, 3.0),
                   "items": ["salmon", "beef", "whipping cream", "potatoes", "butter"]},
        "one_offs": ["duck breast", "lamb", "mussels", "halibut", "truffle", "arctic char", "goat cheese", "venison", "cod"],
    },
}

CHANNELS_BEFORE_AGENT = (("web shop", 0.8), ("phone", 0.2))
CHANNELS_WITH_AGENT = (("agent", 0.55), ("web shop", 0.4), ("phone", 0.05))
AGENT_SINCE = date(2026, 9, 1)  # the pilot: the ordering agent became a channel in September


def school_holiday(d: date) -> bool:
    """Finnish school breaks (approximate): Christmas, winter break, Easter, summer, autumn break."""
    return (
        (d.month == 12 and d.day >= 21) or (d.month == 1 and d.day <= 6)
        or (d.year == 2026 and d.month == 2 and 23 <= d.day <= 27)
        or (d.year == 2026 and d.month == 4 and 3 <= d.day <= 6)
        or (d.month == 6 and d.day >= 1) or d.month == 7 or (d.month == 8 and d.day < 12)
        or (d.year == 2026 and d.month == 10 and 19 <= d.day <= 23)
    )


def delivery_days(customer_id: str, weekdays: set[int]) -> list[date]:
    days, d = [], FIRST_DAY
    while d <= LAST_DAY:
        if d.weekday() in weekdays and not (customer_id == "C-1002" and school_holiday(d)):
            days.append(d)
        d += timedelta(days=1)
    return days


def unit_text(quantity: float, unit: str, phrase: str) -> str:
    q = f"{quantity:g}"
    return f"{q} litres of {phrase}" if unit == "L" else f"{q} kg {phrase}"


PRICED: set[str] | None = None  # when set, only products the price API returned a price for


def candidate_products(catalog, phrase: str, unit: str, strict: set[str]) -> list[dict]:
    """The products the gateway itself would consider for this phrase, filtered by the request's
    requirements and the customer's strict allergens (explicit free-from evidence preferred)."""
    parsed = rules_parser.parse(f"{unit_text(10, unit, phrase)} for Monday", date(2026, 11, 14))
    if len(parsed.items) != 1:
        return []
    item = parsed.items[0]
    found = [p for p, _ in catalog.search(item.search_phrases or [item.product_query], item.exclude_words, limit=40)]
    found = [p for p in found if set(item.constraints.required_claims) <= set(p["claims"])]
    if strict:
        safe = [p for p in found if not strict & (set(p["contains"]) | set(p["may_contain"]))]
        evidenced = [p for p in safe if strict <= set(p["free_from"])]
        found = evidenced or safe
    if PRICED is not None:
        found = [p for p in found if p["id"] in PRICED]
    return found[:8]


def round_quantity(q: float) -> float:
    if q >= 50:
        return float(round(q / 10) * 10)
    if q >= 10:
        return float(max(10, round(q / 5) * 5))
    return float(max(1, round(q)))


def pick(rng: random.Random, weighted) -> str:
    return rng.choices([v for v, _ in weighted], weights=[w for _, w in weighted])[0]


def line_for(rng, s: dict, day: date, factor: float = 1.0) -> dict:
    # Favourite products, with one permanent switch of favourite during the year for some items.
    favourites = s["favourites_after"] if s["switch_on"] and day >= s["switch_on"] else s["favourites"]
    product = rng.choice(favourites) if rng.random() < 0.85 else rng.choice(s["others"])
    growth = 1 + s["trend"] * (day - FIRST_DAY).days / 365  # some items grow, some shrink over the year
    quantity = round_quantity(s["qty"] * s["months"][day.month] ** 0.5 * growth * factor * rng.uniform(0.7, 1.3))
    delivered = quantity
    reliability = product.get("reliability")
    if reliability is not None and rng.random() < min(0.5, 4 * (1 - reliability)):  # short delivery, as its record suggests
        delivered = round_quantity(quantity * rng.uniform(0.6, 0.95))
    return {
        "request": s["phrase"], "quantity": quantity, "delivered_quantity": min(delivered, quantity), "unit": s["unit"],
        "product_id": product["id"], "product_name": product["name"], "sales_unit": product["sales_unit"],
        "vendor": product.get("vendor"), "category": product.get("category"),
    }


def generate(customer_id: str, profile: dict, catalog, rng: random.Random) -> list[dict]:
    strict = profile["strict_allergens"]
    staples = []
    for phrase, qty, unit, interval, months, weekdays in profile["staples"]:
        products = candidate_products(catalog, phrase, unit, strict)
        if not products:
            raise SystemExit(f"{customer_id}: no catalog product for '{phrase}'")
        switch = len(products) > 3 and rng.random() < 0.3
        staples.append({
            "phrase": phrase, "qty": qty, "unit": unit, "interval": interval, "months": months, "weekdays": weekdays,
            "favourites": products[:2], "others": products[2:] or products,
            "favourites_after": products[2:4] if switch else products[:2],
            "switch_on": FIRST_DAY + timedelta(days=rng.randint(90, 300)) if switch else None,
            "trend": rng.choice((-0.15, 0.0, 0.0, 0.1, 0.25)), "next_due": FIRST_DAY,
        })
    by_phrase = {s["phrase"]: s for s in staples}
    for phrase in profile["events"]["items"] + profile["one_offs"]:
        if phrase not in by_phrase:
            products = candidate_products(catalog, phrase, "KG", strict)
            if products:
                by_phrase[phrase] = {"phrase": phrase, "qty": 8, "unit": "KG", "months": ALL_YEAR, "favourites": products[:2],
                                     "others": products[2:] or products, "favourites_after": products[:2], "switch_on": None, "trend": 0.0}

    days = delivery_days(customer_id, profile["weekdays"])
    # Weekend-heavy restaurants and summer-heavy hotels get more orders on those days.
    day_weight = [max(0.2, sum(s["months"][d.month] for s in staples) / len(staples)) * (1.25 if d.weekday() >= 4 else 1.0) for d in days]
    per_day = [0] * len(days)
    for i in rng.choices(range(len(days)), weights=day_weight, k=ORDERS_PER_CUSTOMER):
        per_day[i] += 1
    event_days = set(rng.sample([i for i, n in enumerate(per_day) if n], profile["events"]["count"]))

    orders = []
    for i, (day, count) in enumerate(zip(days, per_day)):
        for k in range(count):
            note = None
            if i in event_days and k == 0:
                ev = profile["events"]
                factor = rng.uniform(*ev["factor"])
                lines = [line_for(rng, by_phrase[p], day, factor) for p in ev["items"] if p in by_phrase]
                note = f"{ev['name'].capitalize()} ({rng.randint(40, 220)} guests)"
            else:
                due = [s for s in staples if s["next_due"] <= day and s["months"][day.month] > 0
                       and (s["weekdays"] is None or day.weekday() in s["weekdays"])]
                rng.shuffle(due)
                chosen = due[: rng.choice((1, 1, 2, 2, 3, 3, 4, 5, 6, 8))]
                if not chosen:  # a top-up order
                    pool = [s for s in staples if s["months"][day.month] > 0]
                    chosen = [rng.choices(pool, weights=[s["months"][day.month] / s["interval"] for s in pool])[0]]
                lines = []
                for s in chosen:
                    lines.append(line_for(rng, s, day))
                    s["next_due"] = day + timedelta(days=max(1, round(s["interval"] / max(0.5, s["months"][day.month]) * rng.uniform(0.75, 1.35))))
                if rng.random() < 0.04:  # something new or special now and then
                    extra = [p for p in profile["one_offs"] if p in by_phrase]
                    if extra:
                        lines.append(line_for(rng, by_phrase[rng.choice(extra)], day, rng.uniform(0.3, 0.8)))
            channel = pick(rng, CHANNELS_WITH_AGENT if day >= AGENT_SINCE else CHANNELS_BEFORE_AGENT)
            ordered = datetime.combine(day - timedelta(days=rng.choice((1, 1, 1, 2, 3))),
                                       time(rng.randint(6, 15), rng.choice((0, 10, 20, 30, 40, 50))), TZ)
            status = "cancelled" if rng.random() < 0.03 else "delivered"
            if status == "delivered" and day > LAST_DAY - timedelta(days=1):
                status = "confirmed"
            orders.append({
                "ordered_at": ordered.isoformat(), "delivery_date": day.isoformat(), "status": status,
                "channel": channel, "ordered_by": rng.choice(profile["people"]), "site": rng.choice(profile["sites"]),
                "note": note, "lines": lines,
            })

    orders.sort(key=lambda o: (o["ordered_at"], o["delivery_date"]))
    out = []
    for n, o in enumerate(orders, 1):
        out.append({"id": f"H-{customer_id}-{n:04d}", **o})
    return out


async def fetch_prices(product_ids: list[str]) -> dict[str, dict]:
    """One batch call to the live price API for the products the history uses (<= 1,000 per request)."""
    from gateway.valio_client import ValioClient
    client = ValioClient(settings.aimo_api_base_url, settings.aimo_api_key)
    try:
        rows, _ = await client.price(product_ids, PRICE_DATE, "history-generator")
    finally:
        await client.close()
    prices = {}
    for pid, row in rows.items():
        comp, cat = row.get("comparisonPrice") or {}, row.get("catalogPrice") or {}
        if not row.get("error") and comp.get("value") and row.get("comparisonUnit") in ("KG", "L"):
            prices[pid] = {"per_unit": comp["value"], "unit": row["comparisonUnit"], "sales_unit_price": cat.get("value")}
    return prices


def apply_prices(orders: list[dict], prices: dict[str, dict], rng: random.Random) -> None:
    """Historical price per kg / litre: today's comparison price, about 4% lower a year ago, +-4% noise.
    Lines without a usable comparison price keep no price (shown as such)."""
    for o in orders:
        age = (PRICE_DATE - date.fromisoformat(o["delivery_date"])).days / 365
        total, priced = 0.0, True
        for line in o["lines"]:
            p = prices.get(line["product_id"])
            if not p:
                line["unit_price"] = line["line_total"] = None
                priced = False
                continue
            unit_price = round(p["per_unit"] * (1 - 0.04 * age) * rng.uniform(0.96, 1.04), 2)
            billed = line["delivered_quantity"] if o["status"] != "cancelled" else 0
            line["unit_price"] = unit_price  # EUR per kg / litre, excl. VAT
            line["line_total"] = round(unit_price * billed, 2)
            total += line["line_total"]
        o["total"] = round(total, 2) if priced else None


def main() -> None:
    check, no_prices = "--check" in sys.argv, "--no-prices" in sys.argv
    catalog = load_catalog(settings.valio_dir)
    if check:
        for cid, profile in PROFILES.items():
            for phrase in [s[0] for s in profile["staples"]] + profile["one_offs"] + profile["events"]["items"]:
                unit = next((s[2] for s in profile["staples"] if s[0] == phrase), "KG")
                found = candidate_products(catalog, phrase, unit, profile["strict_allergens"])
                print(f"{cid} {phrase:30} {len(found)}  {[p['name'][:34] for p in found[:3]]}")
        return

    customers = {c["id"]: c for c in json.loads((settings.runtime_dir / "customers.json").read_text(encoding="utf-8"))}
    prices = {}
    if not no_prices:
        # Every product the history could use, priced in one call; then only priced products are picked.
        ids = sorted({p["id"] for cid, profile in PROFILES.items()
                      for phrase, unit in [(s[0], s[2]) for s in profile["staples"]]
                      + [(x, "KG") for x in profile["one_offs"] + profile["events"]["items"]]
                      for p in candidate_products(catalog, phrase, unit, profile["strict_allergens"])})
        try:
            prices = asyncio.run(fetch_prices(ids))
            print(f"Prices: {len(prices)} of {len(ids)} candidate products priced by one call to the live price API")
            global PRICED
            PRICED = set(prices)
        except Exception as e:  # no key, offline: the history is still useful without totals
            print(f"WARNING: no prices ({e}); totals left empty")
    histories = {cid: generate(cid, profile, catalog, random.Random(f"aimo-history-v2-{cid}")) for cid, profile in PROFILES.items()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cid, orders in histories.items():
        if prices:
            apply_prices(orders, prices, random.Random(f"aimo-prices-{cid}"))
        payload = {
            "customer_id": cid,
            "customer_name": customers.get(cid, {}).get("name"),
            "synthetic": True,
            "note": ("Synthetic demo history. Products and their delivery reliability come from the challenge data; "
                     f"prices start from the live price API on {PRICE_DATE} (per kg / litre, excl. VAT) with an invented "
                     "drift back in time; dates, quantities, seasons, events, people and channels are invented."),
            "orders": orders,
        }
        path = OUT_DIR / f"{cid}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        n_lines = sum(len(o["lines"]) for o in orders)
        items = len({line["request"] for o in orders for line in o["lines"]})
        spend = sum(o["total"] or 0 for o in orders)
        print(f"{cid}: {len(orders)} orders, {n_lines} lines, {items} different items, EUR {spend:,.0f} "
              f"-> {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
