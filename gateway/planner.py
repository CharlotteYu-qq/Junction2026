"""Parsed request -> signed, fixed-price offer with ranked options for every item.

The steps are recorded as `Operation`s so people can see exactly what the agent executed:
parse -> search catalog -> get prices -> check availability -> rank -> sign.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import date, datetime, timedelta

from contracts.models import (
    Availability,
    Candidate,
    Customer,
    Offer,
    OfferItem,
    Operation,
    ParsedRequest,
    PriceInfo,
    RequestItem,
)
from contracts.vocab import ALLERGENS, CLAIMS, LABELS, SALES_UNITS
from gateway.catalog import Catalog
from gateway.config import TZ
from gateway.valio_client import ValioAPIError, ValioClient
from trust.policy import constraint_check
from trust.signing import Signer, content_hash

SHORTLIST = 12
UNLIMITED = 1_000_000  # the mock returns 9 999 999 999 for stocked items, 999 999 for supplier items
STATUS_POINTS = {"ok": 3, "partial": 1, "late": 1, "cutoff_passed": 1, "unknown": 0, "unavailable": -2}


# ----------------------------------------------------------------------------- helpers


def describe_constraints(item: RequestItem) -> str:
    c = item.constraints
    parts = [CLAIMS.get(x, x) for x in c.required_claims]
    parts += [f"free from {ALLERGENS.get(x, x)}" for x in c.free_from_allergens]
    parts += [LABELS.get(x, x) for x in c.required_labels]
    if c.origin_country:
        parts.append("Finnish" if c.origin_country == "fi" else f"origin {c.origin_country.upper()}")
    return ", ".join(parts)


def describe_item(item: RequestItem) -> str:
    qty = f"{item.quantity:,.10g} {item.unit.replace('SALES_UNIT', 'units').replace('PCS', 'pcs')} " if item.quantity and item.unit else ""
    cons = describe_constraints(item)
    return f"{qty}{item.product_query}" + (f" ({cons})" if cons else "")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _price_info(row: dict | None) -> PriceInfo:
    if not row:
        return PriceInfo(error=True)
    cat = row.get("catalogPrice") or {}
    comp = row.get("comparisonPrice") or {}
    tax = row.get("tax") or {}
    lst = row.get("listPrice") or {}
    return PriceInfo(
        unit_price=cat.get("value"),
        unit_price_with_tax=cat.get("valueWithTax"),
        comparison_price=comp.get("value"),
        comparison_unit=row.get("comparisonUnit"),
        tax_rate=tax.get("taxRate"),
        list_price=lst.get("value"),
        net_price_found=bool(row.get("netPriceFound")),
        valid_to=cat.get("validTo"),
        error=bool(row.get("error")) or cat.get("value") is None,
    )


def _content(p: dict, price: PriceInfo) -> tuple[float | None, str | None]:
    """Content of one sales unit in L or KG: from the product name, cross-checked with the
    ratio of unit price to comparison price returned by Valio's price API."""
    content, unit = p.get("content"), p.get("content_unit")
    if price.unit_price and price.comparison_price and price.comparison_unit in ("L", "KG"):
        ratio = price.unit_price / price.comparison_price
        if not content or unit != price.comparison_unit or abs(content - ratio) / ratio > 0.1:
            content, unit = round(ratio, 2), price.comparison_unit
    return content, unit


def order_quantity(req: RequestItem, p: dict, content: float | None, unit: str | None) -> tuple[float | None, str]:
    if req.quantity is None or req.unit is None:
        return None, "quantity missing"
    sales_unit, base_unit = p["sales_unit"], p["base_unit"]
    if req.unit in ("L", "KG"):
        if content and unit == req.unit:
            raw = req.quantity / content
        else:
            return None, f"can't convert {req.unit} into {SALES_UNITS.get(sales_unit, sales_unit)}"
    elif req.unit == "PCS":
        size = 1.0 if sales_unit == base_unit else (p["units"].get(sales_unit) or 1.0)
        raw = req.quantity / size
    else:
        raw = req.quantity
    lot = float(p.get("lot") or 1)
    if sales_unit == "KG":
        qty = math.ceil(raw / lot - 1e-9) * lot
    else:
        qty = max(1, math.ceil(raw - 1e-9))
        if lot > 1:
            qty = math.ceil(qty / lot) * lot
    return round(qty, 3), ""


def _availability(row: dict | None, qty: float | None, requested: date, time_now: datetime) -> Availability:
    if not row:
        return Availability(status="unknown", note="no answer from the availability API")
    lines = row.get("scheduleLines") or []
    closing = _parse_dt(row.get("closingTime"))
    model = row.get("logisticModel")
    base = dict(closing_time=closing, logistic_model=model, sales_block=bool(row.get("salesBlock")))
    if row.get("salesBlock") or (lines and lines[0].get("salesBlock")):
        return Availability(status="unavailable", note="sales block", **base)
    if not lines:
        return Availability(status="unavailable", note="cannot currently be delivered on any date", **base)
    line = lines[0]
    amount = line.get("availableAmount")
    available_date = date.fromisoformat(line["availableDate"][:10]) if line.get("availableDate") else None
    out = Availability(available_amount=amount, available_date=available_date, **base)
    terminal = model == "Terminal"
    if terminal:
        out.note = "Delivered from the supplier's warehouse: Valio Aimo can't see the actual stock"
    if amount is not None and amount <= 0:
        out.status, out.note = "unavailable", "0 available"
    elif available_date and available_date > requested:
        out.status = "late"
    elif closing and closing < time_now:
        out.status = "cutoff_passed"
    elif qty is not None and amount is not None and not terminal and amount < qty:
        out.status = "partial"
    else:
        out.status = "ok"
    return out


def _candidate(p: dict, match: float, meets: bool, notes: list[str], req: RequestItem, price_row, avail_row, requested: date, time_now: datetime) -> Candidate:
    price = _price_info(price_row)
    content, cunit = _content(p, price)
    qty, _ = order_quantity(req, p, content, cunit)
    line_total = round(qty * price.unit_price, 2) if qty is not None and price.unit_price is not None else None
    return Candidate(
        product_id=p["id"],
        name=p["name"],
        brand=p.get("brand"),
        vendor=p.get("vendor"),
        category=p.get("category"),
        sales_unit=p["sales_unit"],
        content_per_sales_unit=content,
        content_unit=cunit,
        quantity=qty,
        line_total=line_total,
        price=price,
        availability=_availability(avail_row, qty, requested, time_now),
        delivery_reliability=p.get("reliability"),
        co2_per_kg=p.get("co2"),
        claims=p.get("claims") or [],
        labels=p.get("labels") or [],
        allergens_contains=p.get("contains") or [],
        allergens_may_contain=p.get("may_contain") or [],
        free_from=p.get("free_from") or [],
        country=p.get("country"),
        match_score=match,
        meets_constraints=meets,
        constraint_notes=notes,
    )


def _rank(cands: list[Candidate]) -> None:
    totals = [c.line_total for c in cands if c.line_total is not None]
    lo, hi = (min(totals), max(totals)) if totals else (0.0, 0.0)
    good = [c.line_total for c in cands if c.line_total is not None and c.meets_constraints and c.availability.status == "ok"]
    good_lo = min(good) if good else None
    for c in cands:
        cost_pts = 0.0
        if c.line_total is not None:
            cost_pts = 25.0 if hi == lo else 25.0 * (hi - c.line_total) / (hi - lo)
        rel = c.delivery_reliability
        c.score = round(
            (100 if c.meets_constraints else 0)
            + 25 * STATUS_POINTS.get(c.availability.status, 0)
            + 4 * min(c.match_score, 10)  # relevance first: the right product beats a cheaper wrong one
            + cost_pts
            + (10 * rel if rel is not None else 5)
            + (0 if c.line_total is not None else -60),
            2,
        )
        reasons = []
        reasons.append("meets the request" if c.meets_constraints else "substitute: " + ", ".join(c.constraint_notes))
        reasons.append(
            {
                "ok": "on time",
                "partial": "not enough stock",
                "late": "arrives late",
                "cutoff_passed": "order cut-off passed",
                "unavailable": "not orderable",
                "unknown": "availability unknown",
            }[c.availability.status]
        )
        if c.line_total is not None and c.line_total == lo and len(totals) > 1:
            reasons.append("lowest cost of all options")
        elif c.line_total is not None and c.line_total == good_lo and len(good) > 1:
            reasons.append(f"lowest cost of the {len(good)} on-time options that match")
        if rel is not None:
            reasons.append(f"{rel:.0%} delivery reliability")
        c.rank_reason = (c.rank_reason + " · " if c.rank_reason else "") + " · ".join(reasons)
    cands.sort(key=lambda c: c.score, reverse=True)


def _recommend(cands: list[Candidate]) -> str | None:
    usable = [c for c in cands if c.availability.status != "unavailable"]
    priced = [c for c in usable if c.line_total is not None]
    pool = priced or usable
    return pool[0].product_id if pool else None


# ----------------------------------------------------------------------------- main entry


async def build_offer(
    *,
    customer: Customer,
    parsed: ParsedRequest,
    request_text: str,
    parse_ms: int,
    catalog: Catalog,
    valio: ValioClient,
    signer: Signer,
    ttl_minutes: int,
) -> Offer:
    now = datetime.now(TZ)
    ops: list[Operation] = []

    def op(name: str, detail: str, status: str = "done", ms: int | None = None) -> None:
        ops.append(Operation(step=len(ops) + 1, name=name, detail=detail, status=status, duration_ms=ms))

    understood = "; ".join(describe_item(i) for i in parsed.items) or "no products recognised"
    op(
        "parse_request",
        f"{'Claude' if parsed.parser == 'claude' else 'Rule-based'} parser understood: {understood} · delivery {parsed.delivery_date:%a %d %b}"
        + (" (assumed)" if parsed.delivery_date_assumed else ""),
        "warning" if parsed.warnings or not parsed.items or any(i.quantity is None for i in parsed.items) else "done",
        parse_ms,
    )

    # 1) narrow down using static data only
    shortlists: list[list[tuple[dict, float, bool, list[str], str]]] = []
    for item in parsed.items:
        t = time.perf_counter()
        phrases = item.search_phrases or [item.product_query]
        matches = catalog.search(phrases, item.exclude_words, limit=80)
        evaluated = []
        excluded: list[str] = []
        for p, score in matches:
            ok, notes, hard = constraint_check(p, item.constraints, customer)
            if hard:
                excluded += [n for n in notes if "contain" in n]
            else:
                evaluated.append((p, score, ok, notes, ""))
        meeting = [e for e in evaluated if e[2]]
        shortlist = meeting[:SHORTLIST]
        if len(shortlist) < 4:  # offer the closest substitutes so a person can decide
            shortlist += [e for e in evaluated if not e[2]][: 6 - len(shortlist)]
        # Valio Aimo's own substitution rules from the product data
        ids = {e[0]["id"] for e in shortlist}
        for p, score, *_ in list(shortlist):
            for sid in p.get("substitutes") or []:
                sp = catalog.by_id.get(sid)
                if sp and sid not in ids:
                    ok, notes, hard = constraint_check(sp, item.constraints, customer)
                    if not hard:
                        shortlist.append((sp, max(score - 1, 0.5), ok, notes, f"Valio Aimo substitute for {p['id']}"))
                        ids.add(sid)
        shortlists.append(shortlist)
        cons = describe_constraints(item)
        n_excluded = len(matches) - len(evaluated)
        why = ""
        if n_excluded:
            top_reasons = ", ".join(dict.fromkeys(excluded))[:120]
            why = f", {n_excluded} excluded by allergen rules ({top_reasons})"
        op(
            "search_catalog",
            f"“{' / '.join(phrases)}”{' + ' + cons if cons else ''}: {len(matches)} relevant products out of {len(catalog):,}"
            f"{why}, {len(meeting)} meet the request, {len(shortlist)} shortlisted",
            "done" if meeting else "warning",
            int((time.perf_counter() - t) * 1000),
        )

    # 2) ask Valio Aimo only about the shortlisted products
    ids = list(dict.fromkeys(e[0]["id"] for sl in shortlists for e in sl))
    prices: dict = {}
    avail: dict = {}
    time_now = now
    source = {
        "system": "Valio Aimo mock API (Junction 2026)",
        "base_url": valio.base_url,
        "requested_date": parsed.delivery_date.isoformat(),
    }
    if ids:
        t = time.perf_counter()
        try:
            prices, meta = await valio.price(ids, parsed.delivery_date, customer.id)
            found = sum(1 for pid in ids if not _price_info(prices.get(pid)).error)
            op("get_price", f"POST /api/price for {len(ids)} products on {parsed.delivery_date:%d %b}: {found} prices"
               + (f" ({meta['from_cache']} from cache)" if meta.get("from_cache") else ""), ms=int((time.perf_counter() - t) * 1000))
            source["currency"] = meta.get("currency") or "EUR"
        except ValioAPIError as e:
            op("get_price", f"Price API failed: {e}", "failed", int((time.perf_counter() - t) * 1000))
        t = time.perf_counter()
        try:
            avail, meta = await valio.availability(ids, parsed.delivery_date, customer.id)
            time_now = _parse_dt(meta.get("time_now")) or now
            source["time_now"] = time_now.isoformat()
            op("check_availability", f"POST /api/availability for {len(ids)} products: answers for {len(avail)}"
               + (f" ({meta['from_cache']} from cache)" if meta.get("from_cache") else ""), ms=int((time.perf_counter() - t) * 1000))
        except ValioAPIError as e:
            op("check_availability", f"Availability API failed: {e}", "failed", int((time.perf_counter() - t) * 1000))
    else:
        op("get_price", "Nothing to price: no products matched", "skipped")
        op("check_availability", "Nothing to check", "skipped")

    # 3) build, rank and recommend
    items: list[OfferItem] = []
    t = time.perf_counter()
    summaries = []
    for item, shortlist in zip(parsed.items, shortlists):
        cands = []
        for p, score, ok, notes, origin in shortlist:
            c = _candidate(p, score, ok, notes, item, prices.get(p["id"]), avail.get(p["id"]), parsed.delivery_date, time_now)
            c.rank_reason = origin
            cands.append(c)
        _rank(cands)
        rec = _recommend(cands)
        n_meet = sum(1 for c in cands if c.meets_constraints)
        n_on_time = sum(1 for c in cands if c.availability.status == "ok")
        if not cands:
            note = "No matching products found. Try other words, or remove a requirement."
        else:
            note = f"{n_meet} of {len(cands)} shortlisted options meet the request; {n_on_time} can be delivered on {parsed.delivery_date:%a %d %b}."
        items.append(OfferItem(request=item, candidates=cands, recommended_product_id=rec, note=note))
        best = next((c for c in cands if c.product_id == rec), None)
        if best and best.quantity is not None and best.line_total is not None:
            summaries.append(f"{best.name} × {best.quantity:,.10g} {SALES_UNITS.get(best.sales_unit, best.sales_unit)} = €{best.line_total:,.2f}")
        elif best:
            summaries.append(f"{best.name} (quantity unclear)")
    op("rank_options", "Recommended: " + ("; ".join(summaries) if summaries else "nothing"), "done" if summaries else "warning",
       int((time.perf_counter() - t) * 1000))

    offer = Offer(
        id="OF-" + uuid.uuid4().hex[:6].upper(),
        customer_id=customer.id,
        created_at=now,
        valid_until=now + timedelta(minutes=ttl_minutes),
        request_text=request_text,
        parsed=parsed,
        items=items,
        operations=ops,
        source=source,
    )
    op("sign_offer", f"Offer {offer.id} hashed (SHA-256) and signed (Ed25519): fixed prices until {offer.valid_until:%H:%M}")
    offer.operations = ops
    sign_offer(offer, signer)
    return offer


def offer_payload(offer: Offer) -> dict:
    return offer.model_dump(mode="json", exclude={"content_hash", "signature"})


def sign_offer(offer: Offer, signer: Signer) -> None:
    offer.content_hash = content_hash(offer_payload(offer))
    offer.signature = signer.sign(offer.content_hash.encode())


def verify_offer(offer: Offer, signer: Signer) -> bool:
    return content_hash(offer_payload(offer)) == offer.content_hash and signer.verify(offer.content_hash.encode(), offer.signature)
