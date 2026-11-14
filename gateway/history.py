"""Order history for the My orders page: statistics and a filtered, paged list.

Totals count delivered quantities (what was billed); cancelled orders count as orders cancelled, never
as spend. Everything is computed from the customer's own history file.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date


def _billed(o: dict) -> bool:
    return o.get("status") != "cancelled"


def stats(orders: list[dict], today: date) -> dict:
    past = [o for o in orders if date.fromisoformat(o["delivery_date"]) <= today]
    billed = [o for o in past if _billed(o)]
    totals = [o["total"] for o in billed if o.get("total") is not None]
    lines = [line for o in billed for line in o["lines"]]
    first = date.fromisoformat(past[0]["delivery_date"]) if past else today
    weeks = max(1.0, (today - first).days / 7)

    monthly = defaultdict(lambda: {"orders": 0, "spend": 0.0})
    if past:  # every month of the period, so a month without orders (a school holiday) shows as 0
        y, m = first.year, first.month
        while (y, m) <= (date.fromisoformat(past[-1]["delivery_date"]).year, date.fromisoformat(past[-1]["delivery_date"]).month):
            monthly[f"{y}-{m:02d}"]
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    for o in billed:
        m = monthly[o["delivery_date"][:7]]
        m["orders"] += 1
        m["spend"] += o.get("total") or 0.0

    items = defaultdict(lambda: {"orders": 0, "quantity": 0.0, "spend": 0.0, "unit": None, "last": None, "products": Counter()})
    for o in billed:
        for line in o["lines"]:
            it = items[line["request"]]
            it["orders"] += 1
            it["quantity"] += line.get("delivered_quantity", line["quantity"])
            it["spend"] += line.get("line_total") or 0.0
            it["unit"] = line["unit"]
            it["last"] = max(it["last"] or "", o["delivery_date"])
            it["products"][line.get("product_name") or line["request"]] += 1
    top_items = sorted(
        ({"item": k, "orders": v["orders"], "quantity": round(v["quantity"], 1), "unit": v["unit"],
          "spend": round(v["spend"], 2), "last": v["last"], "usual_product": v["products"].most_common(1)[0][0]}
         for k, v in items.items()),
        key=lambda r: -r["spend"])

    def share(key: str) -> list[dict]:
        c = Counter(o.get(key) or "–" for o in billed)
        return [{"label": k, "orders": n, "share": round(n / len(billed), 3)} for k, n in c.most_common()]

    full = sum(1 for line in lines if line.get("delivered_quantity", line["quantity"]) >= line["quantity"])
    return {
        "period": {"from": past[0]["delivery_date"] if past else None, "to": past[-1]["delivery_date"] if past else None},
        "kpis": {
            "orders": len(billed),
            "cancelled": len(past) - len(billed),
            "spend": round(sum(totals), 2),
            "avg_order": round(statistics.mean(totals), 2) if totals else None,
            "orders_per_week": round(len(billed) / weeks, 1),
            "lines": len(lines),
            "items": len(items),
            "delivered_in_full": round(full / len(lines), 4) if lines else None,
        },
        "monthly": [{"month": k, "orders": v["orders"], "spend": round(v["spend"], 2)} for k, v in sorted(monthly.items())],
        "top_items": top_items[:12],
        "channels": share("channel"),
        "people": share("ordered_by"),
        "sites": share("site"),
    }


def page(orders: list[dict], q: str = "", status: str = "", offset: int = 0, limit: int = 25) -> dict:
    """Newest first. `q` matches items, product names, people, kitchens and notes."""
    rows = list(reversed(orders))
    if status == "short":
        rows = [o for o in rows if any(l.get("delivered_quantity", l["quantity"]) < l["quantity"] for l in o["lines"])]
    elif status:
        rows = [o for o in rows if o.get("status") == status]
    if q:
        needle = q.lower()
        rows = [o for o in rows if needle in " ".join(
            [o.get("ordered_by") or "", o.get("site") or "", o.get("note") or "", o.get("channel") or "", o["id"]]
            + [f"{l['request']} {l.get('product_name', '')}" for l in o["lines"]]).lower()]
    return {"total": len(rows), "offset": offset, "orders": rows[offset: offset + limit]}
