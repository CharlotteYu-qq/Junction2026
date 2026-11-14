"""Customer registry and trust tiers - the whitelist.

Returning customers (who have bought before and are verified) are on the whitelist and get an
auto-approval limit. New customers are 'unverified': every order goes to a person until someone
verifies the business and adds it to the whitelist.

Next step (not in the MVP): check the business ID against the Finnish Trade Register
(PRH / YTJ open data) instead of collecting personal information from the web.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from contracts.models import Customer
from trust.storage import read_json, write_json

_Y_WEIGHTS = (7, 9, 10, 5, 8, 4, 2)


def valid_business_id(value: str) -> bool:
    """Finnish business ID (Y-tunnus) checksum: 7 digits, hyphen, check digit."""
    m = re.fullmatch(r"(\d{7})-(\d)", (value or "").strip())
    if not m:
        return False
    remainder = sum(int(d) * w for d, w in zip(m.group(1), _Y_WEIGHTS)) % 11
    if remainder == 1:
        return False
    return int(m.group(2)) == (0 if remainder == 0 else 11 - remainder)


# Fictional demo customers (synthetic, not real companies).
SEED = [
    {
        "id": "C-1001",
        "name": "Hotel Aurora Helsinki",
        "segment": "hotel chain",
        "business_id": "1618033-3",
        "status": "trusted",
        "whitelisted_at": "2024-03-12T09:00:00+02:00",
        "past_orders": 48,
        "avg_order_value": 820.0,
        "auto_approve_limit": 1000.0,
        "strict_allergens": [],
        "notes": "Three hotel kitchens in Helsinki. Buys dairy weekly.",
    },
    {
        "id": "C-1002",
        "name": "Kallio School Kitchen",
        "segment": "school kitchen",
        "business_id": "2236067-2",
        "status": "trusted",
        "whitelisted_at": "2023-08-21T09:00:00+03:00",
        "past_orders": 31,
        "avg_order_value": 540.0,
        "auto_approve_limit": 800.0,
        "strict_allergens": ["AN", "AP"],
        "notes": "Nut-free kitchen: tree nuts and peanuts are never allowed.",
    },
    {
        "id": "C-2001",
        "name": "Bistro Uusi",
        "segment": "restaurant",
        "business_id": "1414213-0",
        "status": "unverified",
        "whitelisted_at": None,
        "past_orders": 0,
        "avg_order_value": None,
        "auto_approve_limit": 0.0,
        "strict_allergens": [],
        "notes": "Signed up this week. Not verified yet.",
    },
]

DEFAULT_LIMIT_AFTER_VERIFY = 500.0


class CustomerRegistry:
    def __init__(self, path: Path):
        self.path = path
        rows = read_json(path, None)
        if rows is None:
            rows = SEED
        self._customers = {r["id"]: self._to_model(r) for r in rows}
        self._save()

    @staticmethod
    def _to_model(row: dict) -> Customer:
        row = dict(row)
        row["business_id_valid"] = valid_business_id(row.get("business_id", ""))
        return Customer(**row)

    def _save(self) -> None:
        write_json(self.path, [c.model_dump(mode="json") for c in self._customers.values()])

    def reset(self) -> None:
        self._customers = {r["id"]: self._to_model(r) for r in SEED}
        self._save()

    def all(self) -> list[Customer]:
        return list(self._customers.values())

    def get(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def register(self, name: str, segment: str, business_id: str) -> Customer:
        next_no = 2001 + sum(1 for c in self._customers if c.startswith("C-2"))
        c = self._to_model(
            {
                "id": f"C-{next_no}",
                "name": name.strip(),
                "segment": segment.strip() or "restaurant",
                "business_id": business_id.strip(),
                "status": "unverified",
                "notes": "Registered through the gateway.",
            }
        )
        self._customers[c.id] = c
        self._save()
        return c

    def verify(self, customer_id: str) -> Customer:
        c = self._customers[customer_id]
        c.status = "trusted"
        c.whitelisted_at = datetime.now().astimezone()
        if c.auto_approve_limit <= 0:
            c.auto_approve_limit = DEFAULT_LIMIT_AFTER_VERIFY
        self._save()
        return c

    def record_order(self, customer_id: str, total: float) -> None:
        c = self._customers[customer_id]
        prev_total = (c.avg_order_value or 0.0) * c.past_orders
        c.past_orders += 1
        c.avg_order_value = round((prev_total + total) / c.past_orders, 2)
        self._save()

    def undo_order(self, customer_id: str, total: float) -> None:
        """A cancelled order no longer counts towards the customer's history."""
        c = self._customers[customer_id]
        if c.past_orders <= 0:
            return
        remaining = (c.avg_order_value or 0.0) * c.past_orders - total
        c.past_orders -= 1
        c.avg_order_value = round(max(remaining, 0.0) / c.past_orders, 2) if c.past_orders else None
        self._save()


def trust_profile(c: Customer) -> dict:
    """Readable explanation of why we trust (or don't yet trust) this customer."""
    reasons: list[str] = []
    if c.status == "trusted":
        since = c.whitelisted_at.strftime("%b %Y") if c.whitelisted_at else "?"
        reasons.append(f"On the whitelist since {since}")
        reasons.append(f"{c.past_orders} previous orders")
        reasons.append(
            f"Orders up to €{c.auto_approve_limit:,.0f} can be approved automatically"
            if c.auto_approve_limit > 0
            else "No auto-approval limit: a person approves every order"
        )
        label = "Trusted · on whitelist"
    elif c.status == "unverified":
        reasons.append("New customer, not on the whitelist yet")
        reasons.append("Every order goes to a person until the business is verified")
        label = "New · needs verification"
    else:
        reasons.append("Account blocked")
        label = "Blocked"
    reasons.append(
        f"Business ID {c.business_id} passes the Y-tunnus checksum"
        if c.business_id_valid
        else f"Business ID {c.business_id or '(missing)'} fails the Y-tunnus checksum"
    )
    if c.strict_allergens:
        from contracts.vocab import ALLERGENS

        names = ", ".join(ALLERGENS.get(a, a) for a in c.strict_allergens)
        reasons.append(f"Strict allergen rule: never {names}")
    return {"status": c.status, "label": label, "reasons": reasons}
