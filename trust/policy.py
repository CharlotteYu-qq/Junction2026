"""Guardrails: what the agent may decide alone, what needs a person, and what is never allowed.

Every check is returned - passed ones too - so people can see *why* a decision was made.
Severity says what a failed check means:
    info   -> shown as a note, does not stop the order
    review -> a person must approve
    block  -> the order is rejected
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from contracts.models import Candidate, Check, Constraints, Customer, Decision, Offer, OrderLine
from contracts.vocab import ALLERGENS, CLAIMS, FINNISH_LABELS, LABELS, SALES_UNITS

RELIABILITY_REVIEW_BELOW = 0.80
RELIABILITY_NOTE_BELOW = 0.95
UNUSUAL_ORDER_FACTOR = 3.0


@dataclass(frozen=True)
class PurchaseHistory:
    """What the customer has bought before (delivered or placed orders, cancelled ones left out)."""
    known_products: frozenset[str]  # product ids it has ordered
    largest_order: float | None  # its largest order total so far, EUR excl. VAT


def eur(value: float) -> str:
    return f"€{value:,.2f}"


def _short(name: str, n: int = 42) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


# ----------------------------------------------------------------------------- product facts


def has_claim(p: dict, claim: str) -> bool:
    claims = set(p.get("claims") or [])
    free = set(p.get("free_from") or [])
    if claim == "FREE_FROM_LACTOSE":
        return "FREE_FROM_LACTOSE" in claims or "ML" in free
    if claim == "FREE_FROM_GLUTEN":
        return "FREE_FROM_GLUTEN" in claims or "AW" in free
    if claim == "FREE_FROM_EGGS":
        return "FREE_FROM_EGGS" in claims or "AE" in free
    if claim == "VEGETARIAN":
        return "VEGETARIAN" in claims or "VEGAN" in claims
    return claim in claims


def allergen_status(p: dict, code: str) -> str:
    """contains | may_contain | free | not_declared | unknown (no allergen data at all)."""
    contains, may, free = set(p.get("contains") or []), set(p.get("may_contain") or []), set(p.get("free_from") or [])
    if code in contains:
        return "contains"
    if code in may:
        return "may_contain"
    if code in free:
        return "free"
    if contains or may or free:
        return "not_declared"
    return "unknown"


def constraint_check(p: dict, c: Constraints, customer: Customer | None = None) -> tuple[bool, list[str], bool]:
    """Does product dict `p` (catalog record) meet the request? Returns (ok, notes, hard_fail).

    hard_fail means the product must never be offered (e.g. it contains a forbidden allergen).
    """
    ok, hard, notes = True, False, []
    for claim in c.required_claims:
        if not has_claim(p, claim):
            ok = False
            notes.append(f"not marked {CLAIMS.get(claim, claim)}")
    allergen_codes = list(dict.fromkeys([*c.free_from_allergens, *(customer.strict_allergens if customer else [])]))
    for code in allergen_codes:
        st = allergen_status(p, code)
        name = ALLERGENS.get(code, code)
        if st in ("contains", "may_contain"):
            ok, hard = False, True
            notes.append(f"{'contains' if st == 'contains' else 'may contain'} {name}")
        elif st in ("unknown", "not_declared"):
            ok = False
            notes.append(f"no explicit allergen evidence to confirm it is free from {name}")
    for label in c.required_labels:
        if label not in (p.get("labels") or []):
            ok = False
            notes.append(f"no {LABELS.get(label, label)} label")
    if c.origin_country:
        finnish_label = bool(set(p.get("labels") or []) & FINNISH_LABELS)
        if not (p.get("country") == c.origin_country or (c.origin_country == "fi" and finnish_label)):
            ok = False
            notes.append(f"origin is {(p.get('country') or 'unknown').upper()}, not {c.origin_country.upper()}")
    return ok, notes, hard


def _candidate_facts(cand: Candidate) -> dict:
    return {
        "claims": cand.claims,
        "free_from": cand.free_from,
        "contains": cand.allergens_contains,
        "may_contain": cand.allergens_may_contain,
        "labels": cand.labels,
        "country": cand.country,
    }


# ----------------------------------------------------------------------------- decision


def evaluate(
    customer: Customer,
    offer: Offer,
    selections: dict[int, str],
    now: datetime,
    integrity_ok: bool,
    history: PurchaseHistory | None = None,
) -> tuple[Decision, list[OrderLine]]:
    checks: list[Check] = []

    def add(code: str, passed: bool, severity: str, message: str) -> None:
        checks.append(Check(code=code, passed=passed, severity=severity, message=message))

    # -- who is asking
    if customer.status == "trusted":
        add("CUSTOMER_TRUST", True, "review", f"{customer.name} is on the whitelist ({customer.past_orders} previous orders)")
    elif customer.status == "unverified":
        add("CUSTOMER_TRUST", False, "review", f"{customer.name} is a new customer, not on the whitelist yet: a person verifies the first order")
    else:
        add("CUSTOMER_TRUST", False, "block", f"{customer.name}'s account is blocked")
    add(
        "BUSINESS_ID",
        customer.business_id_valid,
        "review",
        f"Business ID {customer.business_id} passes the Y-tunnus checksum"
        if customer.business_id_valid
        else f"Business ID {customer.business_id or '(missing)'} fails the Y-tunnus checksum",
    )

    # -- is the offer still the one we issued
    if now > offer.valid_until:
        add("OFFER_VALID", False, "block", f"The offer expired at {offer.valid_until:%H:%M}; fixed prices are only guaranteed while it is valid. Request a new offer")
    else:
        add("OFFER_VALID", True, "block", f"Fixed prices are guaranteed until {offer.valid_until:%H:%M}")
    add(
        "OFFER_INTEGRITY",
        integrity_ok,
        "block",
        "Offer signature verified (Ed25519): prices and stock data are unchanged" if integrity_ok else "Offer signature does not match: the data was changed",
    )
    if offer.parsed.clarification_required:
        add("REQUEST_CLARIFICATION", False, "block", "Clarify the request before ordering: " + "; ".join(offer.parsed.warnings))
    if offer.parsed.delivery_date_assumed:
        add("DELIVERY_DATE", False, "block", "Confirm the delivery date before ordering; the current date was assumed")
    elif offer.parsed.delivery_date < now.date():
        add("DELIVERY_DATE", False, "block", "The requested delivery date is in the past; request a new offer")
    if not selections.keys() <= set(range(len(offer.items))):
        add("SELECTION", False, "block", "One or more selected lines are not part of this offer")

    # -- each line
    lines: list[OrderLine] = []
    total = 0.0
    for idx, item in enumerate(offer.items):
        req = item.request
        label = req.product_query or req.text
        pid = selections.get(idx) or item.recommended_product_id
        if not pid:
            add("LINE_OPTION", False, "block", f"No orderable option was found for “{label}”: revise the request or find a valid alternative")
            continue
        cand = next((c for c in item.candidates if c.product_id == pid), None)
        if cand is None:
            add("LINE_OPTION", False, "block", f"Product {pid} is not part of this offer")
            continue
        short = _short(cand.name)
        if (req.quantity is None or not math.isfinite(req.quantity) or req.quantity <= 0
                or req.unit is None or req.assumption):
            add("REQUEST_QUANTITY", False, "block", f"Confirm a positive quantity and unit for “{label}” before ordering")
        if cand.quantity is None or not math.isfinite(cand.quantity) or cand.quantity <= 0:
            add("QUANTITY", False, "block", f"How much “{label}” is needed is unclear: add a positive quantity and unit")
            continue
        if (cand.price.error or cand.price.unit_price is None or cand.line_total is None
                or not math.isfinite(cand.price.unit_price) or not math.isfinite(cand.line_total)
                or cand.price.unit_price < 0 or cand.line_total < 0):
            add("PRICE", False, "block", f"No valid price was found for {short}")
            continue

        a = cand.availability
        unit = SALES_UNITS.get(cand.sales_unit, cand.sales_unit)
        req_date = offer.parsed.delivery_date
        if a.closing_time and a.closing_time < now:
            add("AVAILABILITY", False, "block", f"{short}: the order cut-off has passed; request a new offer")
        elif a.status == "ok":
            extra = " (supplier stock: Valio Aimo can't see the actual quantity)" if a.logistic_model == "Terminal" else ""
            add("AVAILABILITY", True, "review", f"{short}: can be delivered on {req_date:%a %d %b}{extra}")
        elif a.status == "partial":
            amount = f"{a.available_amount:,.0f}" if a.available_amount is not None else "unknown"
            add("AVAILABILITY", False, "block", f"{short}: only {amount} {unit} available, {cand.quantity:,.0f} needed; revise the quantity")
        elif a.status == "late":
            when = f"{a.available_date:%a %d %b}" if a.available_date else "unknown"
            add("AVAILABILITY", False, "block", f"{short}: earliest delivery {when}, after the requested {req_date:%a %d %b}; confirm a new date")
        elif a.status == "cutoff_passed":
            when = f"{a.closing_time:%a %d %b %H:%M}" if a.closing_time else "?"
            add("AVAILABILITY", False, "block", f"{short}: the order cut-off ({when}) for {req_date:%a %d %b} has passed")
        elif a.status == "unavailable":
            add("AVAILABILITY", False, "block", f"{short}: cannot be ordered right now ({a.note or 'no delivery date'})")
        else:
            add("AVAILABILITY", False, "block", f"{short}: availability could not be checked; refresh the offer")

        is_recommended = pid == item.recommended_product_id
        if not cand.meets_constraints:
            severity = "review" if offer.parsed.substitution_allowed else "block"
            add("PREFERENCES", False, severity, f"{short} is a substitute that doesn't fully match the request: {', '.join(cand.constraint_notes)}")
        elif not is_recommended:
            add("PREFERENCES", True, "review", f"{short} was chosen instead of the recommended option and still matches the request")
        else:
            add("PREFERENCES", True, "review", f"{short} matches the request")

        for code in dict.fromkeys([*customer.strict_allergens, *req.constraints.free_from_allergens]):
            st = allergen_status(_candidate_facts(cand), code)
            name = ALLERGENS.get(code, code)
            if st in ("contains", "may_contain"):
                verb = "contains" if st == "contains" else "may contain"
                add("ALLERGEN_SAFETY", False, "block", f"{short} {verb} {name}, which is never allowed for {customer.name}")
            elif st in ("unknown", "not_declared"):
                add("ALLERGEN_SAFETY", False, "block", f"{short} has no explicit evidence that it is free from {name}; obtain evidence and request a new offer")
            else:
                add("ALLERGEN_SAFETY", True, "block", f"{short}: {'declared free from' if st == 'free' else 'not declared to contain'} {name}")

        rel = cand.delivery_reliability
        if rel is not None and rel < RELIABILITY_REVIEW_BELOW:
            add("RELIABILITY", False, "review", f"{short} has historically been delivered in full only {rel:.0%} of the time")
        elif rel is not None and rel < RELIABILITY_NOTE_BELOW:
            add("RELIABILITY", False, "info", f"{short}: delivery reliability {rel:.0%} (some orders arrived short)")

        # a product the customer has never bought, costing more than its average order, needs a person
        avg_order = customer.avg_order_value
        if history is not None and cand.product_id not in history.known_products:
            if avg_order and cand.line_total > avg_order:
                add("NEW_PRODUCT", False, "review",
                    f"{short} is new for {customer.name} and costs {eur(cand.line_total)}, more than its average order ({eur(avg_order)})")
            else:
                add("NEW_PRODUCT", True, "review",
                    f"{short} is new for {customer.name}; its cost stays within the average order"
                    + (f" ({eur(avg_order)})" if avg_order else ""))

        lines.append(
            OrderLine(
                item_index=idx,
                product_id=cand.product_id,
                name=cand.name,
                quantity=cand.quantity,
                sales_unit=cand.sales_unit,
                unit_price=cand.price.unit_price,
                line_total=cand.line_total,
                delivery_date=a.available_date if a.available_date and a.available_date > req_date else req_date,
                is_recommended=is_recommended,
            )
        )
        total += cand.line_total

    total = round(total, 2)
    if not lines:
        if any(item.candidates for item in offer.items):
            add("LINES", False, "block", "Nothing can be ordered until the missing details are added")
        else:
            add("LINES", False, "block", "No matching products were found")

    if offer.parsed.budget_eur is not None:
        budget = offer.parsed.budget_eur
        add("BUYER_BUDGET", total <= budget, "block",
            f"Total {eur(total)} is within the buyer's budget of {eur(budget)}"
            if total <= budget else f"Total {eur(total)} exceeds the buyer's budget of {eur(budget)}; revise the request")

    # -- authority: how much may be spent without a person
    limit = customer.auto_approve_limit
    if limit > 0:
        add(
            "SPEND_LIMIT",
            total <= limit,
            "review",
            f"Total {eur(total)} is within the auto-approval limit of €{limit:,.0f}"
            if total <= limit
            else f"Total {eur(total)} exceeds the auto-approval limit of €{limit:,.0f}",
        )
    else:
        add("SPEND_LIMIT", False, "review", "No auto-approval limit for this customer: a person approves every order")

    avg = customer.avg_order_value
    if avg and total > UNUSUAL_ORDER_FACTOR * avg:
        add("ORDER_SIZE", False, "review", f"{total / avg:.1f}× this customer's average order ({eur(avg)})")
    elif avg:
        add("ORDER_SIZE", True, "review", f"Order size is in line with this customer's history (average {eur(avg)})")

    # more than the customer has ever spent on one order needs a person
    if history is not None and history.largest_order:
        add("ORDER_MAX", total <= history.largest_order, "review",
            f"Total {eur(total)} is within the largest order so far ({eur(history.largest_order)})"
            if total <= history.largest_order
            else f"Total {eur(total)} is more than {customer.name}'s largest order so far ({eur(history.largest_order)})")

    blocking = [c for c in checks if not c.passed and c.severity == "block"]
    review = [c for c in checks if not c.passed and c.severity == "review"]
    if blocking:
        outcome, summary = "reject", "Blocked: " + blocking[0].message
    elif review:
        outcome = "needs_approval"
        summary = f"{len(review)} check{'s' if len(review) > 1 else ''} need{'s' if len(review) == 1 else ''} a person before this order can be placed"
    else:
        outcome, summary = "auto_approve", "All checks passed: within this customer's authority, so the order is placed automatically"
    return Decision(outcome=outcome, summary=summary, checks=checks, total=total), lines
