"""When an order cannot be placed, the agent proposes ways that can.

Every proposal is checked with the same policy engine before it is shown, so the buyer only sees fixes
that actually pass the hard rules (allergens, stock, delivery date, budget, offer validity):

* switch   - other products from the same offer that can be ordered (on time, priced, safe, in budget)
* rerun    - the same request with one change, re-quoted with live prices: a later delivery date the
             products can make, the quantity that is in stock, or allowing substitutes
* refresh  - a fresh offer when the old one expired or no longer verifies

A proposal never waives a rule: a "switch" is evaluated like any selection, and a "rerun" gets a new
signed offer that is evaluated again.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from contracts.models import Customer, Offer
from trust.policy import PurchaseHistory, eur, evaluate

MAX_FIXES = 3
OUTCOME_TEXT = {"auto_approve": "can be placed automatically", "needs_approval": "needs a person's approval"}


def _orderable(c, substitutes_ok: bool) -> bool:
    return (c.availability.status == "ok" and not c.availability.sales_block
            and c.quantity is not None and math.isfinite(c.quantity) and c.quantity > 0
            and c.line_total is not None and not c.price.error
            and (c.meets_constraints or substitutes_ok))


def _blocks(decision) -> int:
    return sum(1 for c in decision.checks if not c.passed and c.severity == "block")


def _short(name: str) -> str:
    return name if len(name) <= 48 else name[:47] + "…"


def _after_cutoff(availability, requested: date, now: datetime) -> date:
    """The earliest delivery day still possible when the order cut-off for the requested day has passed.
    The cut-off says how far ahead this product must be ordered (e.g. Thu 15:00 for Monday: 4 days), so the
    next possible day is that lead time after the next cut-off we can still make (today, or tomorrow if
    today's has passed). An estimate: the re-quote checks it with live availability."""
    closing = availability.closing_time
    if closing is None:
        return requested + timedelta(days=1)
    closing = closing.astimezone(now.tzinfo) if closing.tzinfo and now.tzinfo else closing
    lead = max(1, (requested - closing.date()).days)
    next_cutoff = now.date() if now.time() < closing.time() else now.date() + timedelta(days=1)
    return max(requested + timedelta(days=1), next_cutoff + timedelta(days=lead))


def _unit_word(unit: str | None) -> str:
    return {"L": "litres", "KG": "kg", "PCS": "pieces", "SALES_UNIT": "units"}.get(unit or "", unit or "")


def suggest_fixes(customer: Customer, offer: Offer, selections: dict[int, str], now: datetime, integrity_ok: bool,
                  history: PurchaseHistory | None = None) -> list[dict]:
    base, _ = evaluate(customer, offer, selections, now, integrity_ok, history)
    if base.outcome != "reject":
        return []
    failing = {c.code for c in base.checks if not c.passed and c.severity == "block"}

    # An expired or altered offer: nothing in it can be committed, so a fresh one is the only fix.
    if "OFFER_VALID" in failing or "OFFER_INTEGRITY" in failing or not integrity_ok:
        return [{"kind": "refresh", "title": "Get a fresh offer", "detail": "The fixed prices expired. Check live prices and stock again.",
                 "override": offer.parsed.model_dump(mode="json")}]
    # Unconfirmed details are asked for by the agent itself; switching products cannot fix them.
    if failing & {"REQUEST_CLARIFICATION", "DELIVERY_DATE", "REQUEST_QUANTITY", "QUANTITY", "SELECTION"}:
        return []

    fixes: list[dict] = []
    substitutes_ok = offer.parsed.substitution_allowed
    current = {i: selections.get(i) or item.recommended_product_id for i, item in enumerate(offer.items)}

    # 1. Switch products: greedily, per line, the orderable option that leaves the fewest blocks (cheapest first).
    trial = dict(current)
    for i, item in enumerate(offer.items):
        options = sorted((c for c in item.candidates if _orderable(c, substitutes_ok)), key=lambda c: c.line_total)
        best, best_blocks = trial.get(i), None
        for c in options:
            d, _ = evaluate(customer, offer, {**{k: v for k, v in trial.items() if v}, i: c.product_id}, now, integrity_ok, history)
            if best_blocks is None or _blocks(d) < best_blocks:
                best, best_blocks = c.product_id, _blocks(d)
            if best_blocks == 0:
                break
        if best:
            trial[i] = best
    chosen = {k: v for k, v in trial.items() if v}
    if chosen != {k: v for k, v in current.items() if v}:
        d, _ = evaluate(customer, offer, chosen, now, integrity_ok, history)
        if d.outcome != "reject":
            changed = [(i, next(c for c in offer.items[i].candidates if c.product_id == pid))
                       for i, pid in chosen.items() if pid != current.get(i)]
            names = "; ".join(_short(c.name) for _, c in changed)
            fixes.append({
                "kind": "switch",
                "title": f"Switch to {names}" if len(changed) == 1 else f"Switch {len(changed)} products",
                "detail": f"{'; '.join(_short(c.name) for _, c in changed)} · total {eur(d.total)} · {OUTCOME_TEXT[d.outcome]}",
                "selections": {str(k): v for k, v in chosen.items()},
                "outcome": d.outcome, "total": d.total,
            })

    # 1b. Over the buyer's own budget, and no switch fits it: raise the budget, or buy less.
    budget = offer.parsed.budget_eur
    if "BUYER_BUDGET" in failing and budget and not any(f["kind"] == "switch" for f in fixes):
        cheapest = [min((c for c in item.candidates if _orderable(c, substitutes_ok)), key=lambda c: c.line_total, default=None)
                    for item in offer.items]
        if all(cheapest):
            needed = math.ceil(sum(c.line_total for c in cheapest))
            fixes.append({
                "kind": "rerun", "title": f"Raise the budget to €{needed:,}",
                "detail": f"The cheapest options that meet the request cost {eur(sum(c.line_total for c in cheapest))}. Re-checks live prices.",
                "override": offer.parsed.model_copy(update={"budget_eur": float(needed)}).model_dump(mode="json"),
            })
            item = offer.items[0].request
            if len(offer.items) == 1 and item.quantity and item.quantity > 0:
                per_unit = cheapest[0].line_total / item.quantity
                fits = math.floor(budget / per_unit) if per_unit > 0 else 0
                if 0 < fits < item.quantity:
                    smaller = item.model_copy(update={"quantity": float(fits), "assumption": ""})
                    fixes.append({
                        "kind": "rerun", "title": f"Order {fits:g} {_unit_word(item.unit)} {item.product_query} to stay within {eur(budget)}",
                        "detail": f"Less than the {item.quantity:g} asked, at the cheapest matching price. Re-checks live prices.",
                        "override": offer.parsed.model_copy(update={"items": [smaller]}).model_dump(mode="json"),
                    })

    # 2. Re-quote with one change, for lines no product in the offer can serve as asked.
    req_date = offer.parsed.delivery_date
    later_dates, date_lines, short_lines, substitute_lines = [], [], {}, []
    for i, item in enumerate(offer.items):
        if any(_orderable(c, substitutes_ok) for c in item.candidates):
            continue
        matching = [c for c in item.candidates if c.meets_constraints or substitutes_ok]
        dates = [c.availability.available_date for c in matching
                 if c.availability.status == "late" and c.availability.available_date]
        dates += [_after_cutoff(c.availability, req_date, now) for c in matching if c.availability.status == "cutoff_passed"]
        if dates:
            later_dates += dates
            date_lines.append(i)
        req = item.request
        in_stock = [c.availability.available_amount * c.content_per_sales_unit for c in matching
                    if c.availability.status == "partial" and c.availability.available_amount and c.content_per_sales_unit
                    and c.content_unit == req.unit]
        if in_stock and req.quantity:
            short_lines[i] = math.floor(max(in_stock) * 10) / 10
        if not substitutes_ok and any(_orderable(c, True) for c in item.candidates):
            substitute_lines.append(i)

    parsed = offer.parsed
    if later_dates:
        day = min(d for d in later_dates if d > req_date) if any(d > req_date for d in later_dates) else None
        if day:
            fixes.append({
                "kind": "rerun", "title": f"Deliver on {day:%a %d %b} instead",
                "detail": f"The earliest day the requested products can still arrive (asked: {req_date:%a %d %b}). Re-checks live prices and stock.",
                "override": parsed.model_copy(update={"delivery_date": day, "delivery_date_assumed": False}).model_dump(mode="json"),
                # checked against live availability before it is shown (see verify_date_fixes)
                "verify": {"date": day.isoformat(), "items": date_lines},
            })
    if short_lines:
        items = [it.model_copy(update={"quantity": short_lines[i], "assumption": ""}) if i in short_lines else it
                 for i, it in enumerate(parsed.items)]
        what = "; ".join(f"{q:g} {_unit_word(parsed.items[i].unit)} {parsed.items[i].product_query}" for i, q in short_lines.items())
        fixes.append({
            "kind": "rerun", "title": f"Order what is in stock: {what}",
            "detail": "Less than asked, but available on the requested day. Re-checks live prices and stock.",
            "override": parsed.model_copy(update={"items": items}).model_dump(mode="json"),
        })
    if substitute_lines:
        names = ", ".join(parsed.items[i].product_query for i in substitute_lines)
        fixes.append({
            "kind": "rerun", "title": f"Allow substitutes for {names}",
            "detail": "Only products outside your stated preferences can be delivered on time; a person then approves the order. Allergen rules still apply.",
            "override": parsed.model_copy(update={"substitution_allowed": True}).model_dump(mode="json"),
        })
    return fixes[:MAX_FIXES]


async def verify_date_fixes(fixes: list[dict], offer: Offer, customer: Customer, valio, classify, parse_dt) -> list[dict]:
    """Keep a "deliver on another day" fix only when live availability says every affected line has at least
    one product that can make that day. One batched availability call per fix (cached by the client).
    `classify` is the planner's availability classifier; when the API cannot answer, the fix is kept and
    the re-quote checks it anyway."""
    out = []
    for fix in fixes:
        check = fix.get("verify")
        if not check:
            out.append(fix)
            continue
        day = date.fromisoformat(check["date"])
        lines = [offer.items[i] for i in check["items"] if i < len(offer.items)]
        ids = [c.product_id for item in lines for c in item.candidates]
        try:
            rows, meta = await valio.availability(ids, day, customer.id)
        except Exception:  # noqa: BLE001 - unverifiable: keep it, the re-quote decides
            out.append({k: v for k, v in fix.items() if k != "verify"})
            continue
        now = parse_dt(meta.get("time_now")) or datetime.now().astimezone()
        possible = all(
            any(classify(rows.get(c.product_id), c.quantity, day, now).status in ("ok", "partial") for c in item.candidates)
            for item in lines
        )
        if possible:
            out.append({k: v for k, v in fix.items() if k != "verify"})
    return out
