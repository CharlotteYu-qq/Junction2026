"""What a customer usually orders, used to fill gaps in a request and to put the usual product first.

Built from the same order history as the reorder suggestions (portal orders included, so it learns from
every new order). All deterministic, and the same for the Claude parser and the rule-based fallback:

* "the usual milk", "our usual amount of butter"  -> the usual item and its usual amount (median of the
  last 8 ordinary orders), as the buyer asked for it: no question
* "same as last time", "repeat my last order"     -> the lines of the last ordinary order
* a product without an amount                     -> the usual amount, proposed for the buyer to confirm
  ("You usually order 200 litres of lactose-free milk. Same again?") instead of "how much?"
* the product the customer bought most for that item is recommended first, when it can be ordered

Nothing here decides an order: the result goes through the same offer and policy as anything typed.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from agent import rules_parser
from contracts.models import ParsedRequest, RequestItem
from gateway.recommend import _ordinary, _round_amount, item_key, item_phrase

USUAL_PREFIX = "your usual amount"
UNIT_WORD = {"L": "litres", "KG": "kg"}
MIN_ORDERS = 2  # an item bought once is not a habit yet

_USUAL = re.compile(r"\b(?:usual(?:ly)?|regular|normal|as always|tavallinen|tavallista|tavallisesti|vanha tuttu)\b", re.I)
_REPEAT = re.compile(
    r"\b(?:same\s+(?:order\s+|again\s+|thing\s+|stuff\s+)?(?:as\s+)?(?:last|previous|before|the\s+last)"
    r"|same\s+again|repeat\w*\s+(?:my\s+|our\s+|the\s+)?(?:last|previous)|re-?order\s+(?:my\s+|our\s+|the\s+)?(?:last|previous)"
    r"|(?:my|our|the)\s+usual\s+order|the\s+usual\b(?!\s+(?:amount|quantity))|samat?\s+kuin\s+(?:viime|edellinen))", re.I)
_YES = re.compile(r"^\s*(?:yes|yep|yeah|ok(?:ay)?|sure|correct|right|same|that'?s (?:right|fine|ok)|sounds good|kyllä|joo)\b", re.I)
_NUMBER = re.compile(r"\d")
_FILLER = {"usual", "usually", "regular", "normal", "our", "my", "the", "same", "tavallinen", "tavallista"}


@dataclass
class Habit:
    key: str
    phrase: str
    unit: str
    orders: int
    usual_quantity: float
    last_quantity: float
    last_delivery: date
    products: Counter = field(default_factory=Counter)

    @property
    def words(self) -> set[str]:
        return set(self.key.split())


def build(orders: list[dict]) -> dict[str, Habit]:
    """item key -> habit, from ordinary orders (no cancellations or one-off events)."""
    seen: dict[str, list] = {}
    for o in sorted((o for o in orders if _ordinary(o)), key=lambda o: o["delivery_date"]):
        for line in o["lines"]:
            if line.get("unit") not in UNIT_WORD:
                continue
            seen.setdefault(item_key(line["request"]), []).append((o, line))
    habits = {}
    for key, rows in seen.items():
        if len(rows) < MIN_ORDERS:
            continue
        o, last = rows[-1]
        habits[key] = Habit(
            key=key, phrase=last["request"], unit=last["unit"], orders=len(rows),
            usual_quantity=_round_amount(statistics.median(line["quantity"] for _, line in rows[-8:])),
            last_quantity=last["quantity"], last_delivery=date.fromisoformat(o["delivery_date"]),
            products=Counter(line["product_id"] for _, line in rows if line.get("product_id")),
        )
    return habits


def match(item: RequestItem, habits: dict[str, Habit]) -> Habit | None:
    """The habit this request item refers to: the same item, or the customer's most frequent item that
    "milk" can mean ("lactose-free milk"). Every word the buyer used has to be in it."""
    if not habits or not item.product_query.strip():
        return None
    key = item_key(item_phrase(item))
    if key in habits:
        return habits[key]
    words = set(key.split()) - _FILLER  # "usual milk" from the rule-based reader is milk
    if not words:
        return None
    options = [h for h in habits.values() if words <= h.words and (item.unit is None or item.unit == h.unit)]
    return max(options, key=lambda h: h.orders, default=None)


def last_order(orders: list[dict]) -> dict | None:
    ordinary = [o for o in orders if _ordinary(o) and any(line.get("unit") in UNIT_WORD for line in o["lines"])]
    return max(ordinary, key=lambda o: o["ordered_at"], default=None)


def _item_from(phrase: str, quantity: float, unit: str, today: date, text: str) -> RequestItem | None:
    parsed = rules_parser.parse(f"{quantity:g} {'litres of' if unit == 'L' else 'kg'} {phrase} for Monday", today)
    if len(parsed.items) != 1:
        return None
    item = parsed.items[0]
    item.text = text or phrase
    item.quantity, item.unit, item.assumption = quantity, unit, ""
    return item


def dialogue_same(a: str, b: str) -> bool:
    wa, wb = set(a.lower().split()) - _FILLER, set(b.lower().split()) - _FILLER
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)


def _amount(h: Habit, q: float | None = None) -> str:
    return f"{(h.usual_quantity if q is None else q):g} {UNIT_WORD[h.unit]}"


def apply(parsed: ParsedRequest, buyer_messages: list[str], previous: ParsedRequest | None,
          orders: list[dict], today: date) -> list[str]:
    """Fill what the buyer left to their habits. Changes `parsed` in place; returns what was filled in,
    in words for the agent's reply (empty: nothing was used)."""
    if not orders or not buyer_messages:
        return []
    habits = build(orders)
    latest = buyer_messages[-1]
    said = " ".join(buyer_messages)  # "same as last time" may be answered over several messages
    notes: list[str] = []

    # "Same as last time": the lines of the last order, when the buyer named no product of their own.
    if not parsed.items and _REPEAT.search(said):
        last = last_order(orders)
        if last:
            lines = [line for line in last["lines"] if line.get("unit") in UNIT_WORD]
            items = [_item_from(line["request"], line["quantity"], line["unit"], today, line["request"]) for line in lines]
            parsed.items = [i for i in items if i]
            parsed.warnings = [w for w in parsed.warnings if not w.startswith(("No products", "No quantity"))]
            parsed.clarification_required = parsed.clarification_required and not parsed.items
            day = date.fromisoformat(last["delivery_date"])
            notes.append(f"I took your last order (delivery {day:%a %d %b}): "
                         + "; ".join(f"{i.quantity:g} {UNIT_WORD[i.unit]} {item_phrase(i)}" for i in parsed.items))
        return notes

    single = len(parsed.items) == 1
    confirmed = previous is not None and bool(_YES.match(latest))
    before = previous.items if previous else []
    for idx, item in enumerate(parsed.items):
        h = match(item, habits)
        if h is None:
            continue
        if item.quantity is None or item.unit is None:
            # already settled earlier in this conversation (the history was used then): keep that amount
            kept = next((p for p in before if p.quantity is not None and p.unit
                         and (match(p, habits) is h or dialogue_same(p.product_query, item.product_query))), None)
            if kept is not None and not (confirmed and kept.assumption.startswith(USUAL_PREFIX)):
                item.quantity, item.unit, item.assumption = kept.quantity, kept.unit, kept.assumption
                item.constraints = rules_parser._merge(item.constraints, kept.constraints)
                continue
        # "the usual" counts in the message that says it, not in every later turn of the conversation
        usual = bool(_USUAL.search(latest)) and (bool(_USUAL.search(item.text or "")) or single)
        if usual and not (_NUMBER.search(item.text or "") and item.quantity is not None and not item.assumption):
            # "the usual milk": the usual item with its requirements, at the usual amount; nothing to ask
            filled = _item_from(h.phrase, h.usual_quantity, h.unit, today, item.text)
            if filled:
                filled.constraints = rules_parser._merge(filled.constraints, item.constraints)
                parsed.items[idx] = filled
                notes.append(f"your usual {_amount(h)} {h.phrase}")
            continue
        if item.quantity is None or item.unit is None:
            # no amount: propose the usual one for the buyer to confirm, rather than asking "how much?"
            was = next((p for p in (previous.items if previous else [])
                        if match(p, habits) is h and p.quantity == h.usual_quantity and p.assumption.startswith(USUAL_PREFIX)), None)
            item.quantity, item.unit = h.usual_quantity, h.unit
            item.assumption = "" if (was and confirmed) else (
                f"{USUAL_PREFIX}; last order {_amount(h, h.last_quantity)} on {h.last_delivery:%a %d %b}")
            parsed.warnings = [w for w in parsed.warnings if not (w.startswith("No quantity") and item.product_query in w)]
            notes.append(f"your usual {_amount(h)} {item.product_query}")
    return notes


def preferences(parsed: ParsedRequest, orders: list[dict]) -> list[Counter]:
    """For each request item: how often the customer bought each product for it (product id -> orders)."""
    habits = build(orders) if orders else {}
    out = []
    for item in parsed.items:
        h = match(item, habits)
        out.append(Counter(h.products) if h else Counter())
    return out
