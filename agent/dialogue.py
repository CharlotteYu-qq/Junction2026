"""Follow-up questions: the agent asks for what is missing before it prepares an offer.

Whether to ask is decided here, deterministically: a product without a quantity or unit, an amount the
parser only estimated, no delivery date, or a purchasing condition that needs clarification. Claude may
phrase the question (in the buyer's language); otherwise a template is used. Without Claude, the buyer's
answer is merged into what the agent already understood.
"""
from __future__ import annotations

import re
from datetime import date

from agent import rules_parser
from contracts.models import ParsedRequest

UNIT_WORDS = {"L": "litres", "KG": "kg", "PCS": "pieces", "SALES_UNIT": "units"}
# Warnings that only restate a missing detail; the question asks for those directly.
_ROUTINE = ("No delivery date", "No quantity", "No products", "Ignored", "The delivery date could not be read",
            "Claude parser unavailable")
_YES = re.compile(r"^\s*(?:yes|yep|yeah|ok(?:ay)?|sure|correct|right|that'?s (?:right|fine|ok)|sounds good|kyllä|joo)\b", re.I)


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _same_product(a: str, b: str) -> bool:
    """"milk" and "make milk" (from "make it 300 litres of milk") are the same item."""
    wa, wb = set(a.split()), set(b.split())
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)


def _needs_amount(item) -> bool:
    return item.quantity is None or item.unit is None or bool(item.assumption)


def _open_points(parsed: ParsedRequest) -> tuple[list[str], list[str]]:
    """(details to ask for, conditions to clarify). Both empty: the request is complete."""
    if not parsed.items:
        return ["what you would like to order"], []
    points: list[str] = []
    missing = [it.product_query for it in parsed.items if it.quantity is None or it.unit is None]
    if len(missing) == 1:
        points.append(f"how much {missing[0]} you need")
    elif missing:
        points.append(f"how much of each you need ({_join(missing)})")
    for it in parsed.items:
        if it.quantity is not None and it.unit and it.assumption:
            why = f" ({it.assumption.rstrip('.')})" if len(it.assumption) <= 90 else ""
            points.append(f"whether {it.quantity:,.10g} {UNIT_WORDS.get(it.unit, it.unit)} of {it.product_query} is right{why}")
    if parsed.delivery_date_assumed:
        points.append("which day it should be delivered")
    notes: list[str] = []
    if parsed.clarification_required:
        notes = [w for w in parsed.warnings if not w.startswith(_ROUTINE)][:2] or ["one of the purchasing conditions"]
    return points, notes


def is_complete(parsed: ParsedRequest) -> bool:
    points, notes = _open_points(parsed)
    return not points and not notes


def follow_up_question(parsed: ParsedRequest, suggested: str = "") -> str:
    """The question for the buyer, or "" when nothing is missing. `suggested` is Claude's phrasing."""
    points, notes = _open_points(parsed)
    if not points and not notes:
        return ""
    if suggested.strip():
        return suggested.strip()
    if not parsed.items:
        return "What would you like to order? For example: “200 litres of lactose-free milk for Monday”."
    parts = [f"Could you tell me {_join(points)}?"] if points else []
    if notes:
        parts.append(("Please also clarify: " if points else "Please clarify: ") + " ".join(n.rstrip(".") + "." for n in notes))
    return " ".join(parts)


def merge_answer(previous: ParsedRequest, answer: str, today: date) -> ParsedRequest:
    """Rule-based: fill in what the agent asked for from the buyer's next message."""
    new = rules_parser.parse(answer, today)
    if previous.clarification_required and new.items:
        return new  # an unclear condition can only be fixed by restating the request
    out = previous.model_copy(deep=True)
    if not new.delivery_date_assumed:
        out.delivery_date, out.delivery_date_assumed = new.delivery_date, False
    if new.budget_eur is not None:
        out.budget_eur = new.budget_eur
    out.substitution_allowed = out.substitution_allowed and new.substitution_allowed
    out.clarification_required = out.clarification_required or new.clarification_required

    waiting = [it for it in out.items if _needs_amount(it)]
    for item in new.items:
        same = next((it for it in out.items if _same_product(it.product_query, item.product_query)), None)
        if same is None:
            out.items.append(item)  # the buyer added another product
            continue
        if item.quantity is not None:
            same.quantity, same.unit, same.assumption = item.quantity, item.unit, item.assumption
        same.constraints = rules_parser._merge(same.constraints, item.constraints)
    if not new.items:
        # "200 litres, Friday" names no product: give the amounts to the items still waiting for one
        amounts = rules_parser.stated_amounts(answer, today)
        for item, (quantity, unit) in zip(waiting, amounts):
            item.quantity, item.unit, item.assumption = quantity, unit, ""
        if not amounts and _YES.match(answer):  # "yes" confirms the estimates the agent proposed
            for item in waiting:
                if item.quantity is not None and item.unit:
                    item.assumption = ""

    out.warnings = [w for w in [*previous.warnings, *new.warnings] if not w.startswith(_ROUTINE)]
    if out.delivery_date_assumed:
        out.warnings.append("No delivery date given: assumed the next weekday")
    out.warnings += [f"No quantity for “{it.product_query}”" for it in out.items if it.quantity is None]
    return out
