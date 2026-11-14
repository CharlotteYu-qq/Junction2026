"""Reorder suggestions from a customer's order history, ranked by one published formula.

For every item i the customer buys regularly (at least 6 deliveries; cancelled orders and one-off event
orders are left out):

    n  = number of deliveries in the history          T = median days between deliveries
    d  = days since the last delivery                 s = median absolute deviation of those intervals
    r  = deliveries per day in the last 56 days ÷ deliveries per day over the whole history

    Due         D = min(d / T, 2) / 2      0.5 = due today by the usual rhythm, 1 = twice as late
    Frequency   F = n / n_max              n_max = the customer's most frequent item
    Regularity  R = max(0, 1 − s / T)      1 = always the same interval (a holiday gap barely moves s)
    Trend       M = min(r, 2) / 2          0.5 = as usual, above = buying more often lately

    Score = 0.50·D + 0.20·F + 0.15·R + 0.15·M          (between 0 and 1)

Items not bought for more than 3·T are treated as paused (out of season, dropped from the menu) and are
not suggested. The amount is the median of the last 8 ordinary orders; the delivery day is the last
delivery + T, no earlier than tomorrow, on a weekday the customer takes deliveries (or the item's own
weekday when at least 70% of its deliveries fall on it). Every suggestion carries these numbers, so the
ranking can be checked by hand.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

WEIGHTS = {"due": 0.50, "frequency": 0.20, "regularity": 0.15, "trend": 0.15}
RECENT_DAYS = 56
PAUSED_AFTER = 3  # × the usual interval
MIN_DELIVERIES = 6  # fewer: an occasional purchase, not a regular item
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
UNIT_SHORT = {"L": "L", "KG": "kg"}

FORMULA = {
    "score": "Score = 0.50·Due + 0.20·Frequency + 0.15·Regularity + 0.15·Trend",
    "weights": WEIGHTS,
    "components": {
        "due": "Due = min(d / T, 2) / 2, where d = days since the last delivery and T = median days between deliveries",
        "frequency": "Frequency = n / n_max, where n = deliveries of this item and n_max = deliveries of your most frequent item",
        "regularity": "Regularity = max(0, 1 − s / T), where s = median absolute deviation of the days between deliveries",
        "trend": f"Trend = min(r, 2) / 2, where r = delivery rate in the last {RECENT_DAYS} days ÷ delivery rate over the whole history",
    },
    "rules": [
        f"Only items with at least {MIN_DELIVERIES} deliveries; cancelled orders and one-off event orders are left out.",
        f"Items not bought for more than {PAUSED_AFTER}·T are paused (for example out of season) and not suggested.",
        "Amount = median of the last 8 ordinary orders. Delivery = last delivery + T, no earlier than tomorrow, on one of your delivery days.",
    ],
}


def load_history(folder: Path) -> dict[str, list[dict]]:
    """customer_id -> orders (oldest first). A missing folder means no history."""
    history: dict[str, list[dict]] = {}
    if not folder.is_dir():
        return history
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        history[payload["customer_id"]] = sorted(payload["orders"], key=lambda o: o["ordered_at"])
    return history


def _ordinary(order: dict) -> bool:
    return order.get("status") != "cancelled" and not order.get("note")


def _round_amount(q: float) -> float:
    if q >= 50:
        return float(round(q / 10) * 10)
    return float(round(q / 5) * 5) if q >= 10 else float(max(1, round(q)))


def _request(phrase: str, quantity: float, unit: str) -> str:
    q = f"{quantity:g}"
    return f"{q} litres of {phrase}" if unit == "L" else f"{q} kg {phrase}"


def _when(day: date, today: date) -> str:
    """"Monday" within the coming week; a numeric date when the weekday would be ambiguous."""
    return WEEKDAYS[day.weekday()] if 0 < (day - today).days < 7 else f"{day.day}.{day.month}."


def delivery_weekdays(orders: list[dict]) -> set[int]:
    """The weekdays this customer takes deliveries on (at least 5% of its orders)."""
    counts = Counter(date.fromisoformat(o["delivery_date"]).weekday() for o in orders)
    total = sum(counts.values()) or 1
    return {wd for wd, n in counts.items() if n / total >= 0.05} or set(range(7))


def next_delivery(due: date, today: date, weekdays: set[int]) -> date:
    day = max(due, today + timedelta(days=1))
    while day.weekday() not in weekdays:
        day += timedelta(days=1)
    return day


def score_items(orders: list[dict], today: date) -> list[dict]:
    """Every regular item with its inputs, components and score, best first (paused items included,
    marked, so the table can show why they are not suggested)."""
    ordinary = [o for o in orders if _ordinary(o) and date.fromisoformat(o["delivery_date"]) <= today]
    if not ordinary:
        return []
    first_day = min(date.fromisoformat(o["delivery_date"]) for o in ordinary)
    span = max(RECENT_DAYS, (today - first_day).days)
    rows = defaultdict(list)  # (phrase, unit) -> [(delivery date, quantity)]
    for o in ordinary:
        day = date.fromisoformat(o["delivery_date"])
        for line in o["lines"]:
            rows[(line["request"], line["unit"])].append((day, line["quantity"]))

    items = []
    for (phrase, unit), seen in rows.items():
        days = sorted({d for d, _ in seen})
        if len(days) < MIN_DELIVERIES:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        interval = max(1.0, float(statistics.median(gaps)))
        spread = statistics.median(abs(g - interval) for g in gaps)  # median absolute deviation
        since = (today - days[-1]).days
        recent = sum(1 for d in days if (today - d).days <= RECENT_DAYS)
        rate_ratio = (recent / RECENT_DAYS) / (len(days) / span)
        weekday_counts = Counter(d.weekday() for d in days)
        own_weekday, own_share = weekday_counts.most_common(1)[0]
        items.append({
            "item": phrase, "unit": unit, "n": len(days), "interval": interval, "spread": round(spread, 1),
            "since": since, "last": days[-1].isoformat(), "recent": recent, "rate_ratio": round(rate_ratio, 2),
            "quantity": _round_amount(statistics.median(q for _, q in sorted(seen)[-8:])),
            "own_weekday": own_weekday if own_share / len(days) >= 0.7 else None,
            "paused": since > PAUSED_AFTER * interval,
        })
    if not items:
        return []
    n_max = max(i["n"] for i in items)
    for i in items:
        c = {
            "due": min(i["since"] / i["interval"], 2) / 2,
            "frequency": i["n"] / n_max,
            "regularity": max(0.0, 1 - i["spread"] / i["interval"]),
            "trend": min(i["rate_ratio"], 2) / 2,
        }
        i["components"] = {k: round(v, 2) for k, v in c.items()}
        i["score"] = round(sum(WEIGHTS[k] * v for k, v in c.items()), 3)
    items.sort(key=lambda i: (i["paused"], -i["score"]))
    return items


def suggest(orders: list[dict], today: date, limit: int = 4) -> list[dict]:
    weekdays = delivery_weekdays(orders)
    out = []
    for i in [i for i in score_items(orders, today) if not i["paused"]][:limit]:
        due = date.fromisoformat(i["last"]) + timedelta(days=round(i["interval"]))
        day = next_delivery(due, today, {i["own_weekday"]} if i["own_weekday"] is not None else weekdays)
        c = i["components"]
        out.append({
            "item": i["item"],
            "label": f"{i['item'][:1].upper()}{i['item'][1:]} · {i['quantity']:g} {UNIT_SHORT[i['unit']]}",
            "text": f"{_request(i['item'], i['quantity'], i['unit'])} for {_when(day, today)}",
            "delivery_date": day.isoformat(),
            "score": i["score"],
            "components": c,
            "explain": (f"Score {i['score']:.2f} = 0.50×Due {c['due']:.2f} + 0.20×Frequency {c['frequency']:.2f} "
                        f"+ 0.15×Regularity {c['regularity']:.2f} + 0.15×Trend {c['trend']:.2f}. "
                        f"About every {i['interval']:g} days; last delivery {i['since']} days ago; "
                        f"usual amount {i['quantity']:g} {UNIT_SHORT[i['unit']]}."),
        })
    return out
