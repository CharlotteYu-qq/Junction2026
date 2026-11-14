"""Parsed request -> signed, fixed-price offer with ranked options for every item.

The steps are recorded as `Operation`s so people can see exactly what the agent executed:
parse -> search catalog -> get prices -> check availability -> rank -> sign.
"""
from __future__ import annotations

import asyncio
import math
import re
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
from gateway.catalog import Catalog, is_capacity_product, matches_product_kind
from gateway.config import TZ
from gateway.valio_client import ValioAPIError, ValioClient
from trust.policy import constraint_check
from trust.signing import Signer, content_hash

SHORTLIST = 12
UNLIMITED = 1_000_000  # the mock returns 9 999 999 999 for stocked items, 999 999 for supplier items
USUAL_POINTS = 30.0   # the customer's usual product first, unless it misses a requirement or is less available
USUAL_PRODUCTS = 3    # the usual products added to the shortlist of an item
UNKNOWN_STOCK = 999_999  # Valio: availableAmount >= this means "stock not known", not a real count
STATUS_POINTS = {"ok": 3, "partial": 1, "late": 1, "cutoff_passed": 1, "unknown": 0, "unavailable": -2}
# Carbon footprint as a tie-breaker: options whose totals differ by at most this share count as the same price,
# and their other points (match, availability, relevance, reliability) may differ by at most CO2_OTHER_POINTS.
CO2_PRICE_TOLERANCE = 0.02
CO2_OTHER_POINTS = 5.0


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
    # The API sends offsets like +0200; Python < 3.11 only parses +02:00
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip())
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
    if is_capacity_product(p):
        return None, None
    content, unit = p.get("content"), p.get("content_unit")
    if (price.unit_price is not None and price.unit_price > 0
            and price.comparison_price is not None and price.comparison_price > 0
            and price.comparison_unit in ("L", "KG")):
        ratio = price.unit_price / price.comparison_price
        if math.isfinite(ratio) and (not content or unit != price.comparison_unit or abs(content - ratio) / ratio > 0.1):
            content, unit = round(ratio, 6), price.comparison_unit
    return content, unit


PIECES_IN_NAME = re.compile(r"\b(\d{1,4}) ?(?:kpl|pcs|pc|st|stk)\b", re.I)  # "napkin 125pcs", "125kpl"
EGG_COUNT = re.compile(r"\b(?:XS|S|M|L|XL)(\d{1,3})\b")  # Finnish egg packs: size and count, "M10" = 10 medium eggs
EGG_WORD = re.compile(r"\beggs?\b|kanan ?muna", re.I)  # not "munakoiso" (aubergine)


def pieces_per_pack(p: dict) -> int:
    """How many pieces one base unit holds when the product name says so (a box of 10 eggs, a pack of
    125 napkins); otherwise 1, so a pack counts as one piece."""
    if p.get("base_unit") == "ST":
        return 1
    names = [p.get("name") or "", p.get("name_fi") or ""]
    for name in names:
        m = PIECES_IN_NAME.search(name)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    if any(EGG_WORD.search(n) for n in names):
        m = EGG_COUNT.search(names[0]) or EGG_COUNT.search(names[1])
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return 1


def order_quantity(req: RequestItem, p: dict, content: float | None, unit: str | None) -> tuple[float | None, str]:
    if req.quantity is None or req.unit is None:
        return None, "quantity missing"
    if not math.isfinite(req.quantity) or req.quantity <= 0:
        return None, "quantity must be positive"
    sales_unit, base_unit = p["sales_unit"], p["base_unit"]
    if req.unit in ("L", "KG"):
        if is_capacity_product(p):
            return None, "container capacity is not ingredient content; order utensils in pieces"
        if content and math.isfinite(content) and content > 0 and unit == req.unit:
            raw = req.quantity / content
        else:
            return None, f"can't convert {req.unit} into {SALES_UNITS.get(sales_unit, sales_unit)}"
    elif req.unit == "PCS":
        size = 1.0 if sales_unit == base_unit else p["units"].get(sales_unit)
        if base_unit in ("KG", "L") or not size or size <= 0:
            return None, "piece count per sales unit is unknown"
        raw = req.quantity / (size * pieces_per_pack(p))
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


def cutoff_for(closing: datetime | None, available_date: date | None, day: date) -> datetime | None:
    """The order cut-off for delivery on `day`. Valio's closingTime is the cut-off for availableDate, the earliest
    delivery, whatever day was asked for (e.g. Thu 15:00 for a Monday). A later day is assumed to need the same
    lead time (Sun 15:00 for Thursday): an estimate, checked again when the order is placed."""
    if closing and available_date and day > available_date:
        return closing + timedelta(days=(day - available_date).days)
    return closing


def _availability(row: dict | None, qty: float | None, requested: date, time_now: datetime) -> Availability:
    if not row:
        return Availability(status="unknown", note="no answer from the availability API")
    lines = row.get("scheduleLines") or []
    closing = _parse_dt(row.get("closingTime"))
    model = row.get("logisticModel")
    if row.get("salesBlock") or (lines and lines[0].get("salesBlock")):
        return Availability(status="unavailable", note="sales block", closing_time=closing, logistic_model=model,
                            sales_block=bool(row.get("salesBlock")))
    if not lines:
        return Availability(status="unavailable", note="cannot currently be delivered on any date", closing_time=closing,
                            logistic_model=model)
    line = lines[0]
    amount = line.get("availableAmount")
    available_date = date.fromisoformat(line["availableDate"][:10]) if line.get("availableDate") else None
    closing = cutoff_for(closing, available_date, requested)
    out = Availability(available_amount=amount, available_date=available_date, closing_time=closing, logistic_model=model)
    terminal = model == "Terminal"
    # Valio: an amount of 999999 or more means the stock is not known and any quantity can be ordered
    # (typically Terminal products that are not restocked by Valio Aimo).
    unknown_stock = amount is not None and amount >= UNKNOWN_STOCK
    if terminal:
        out.note = "Delivered from the supplier's warehouse: Valio Aimo can't see the actual stock"
    elif unknown_stock:
        out.note = "Stock is not tracked for this product: any quantity can be ordered"
    if amount is not None and amount <= 0:
        out.status, out.note = "unavailable", "0 available"
    elif available_date and available_date > requested:
        out.status = "late"
    elif closing and closing < time_now:
        out.status = "cutoff_passed"
    elif qty is not None and amount is not None and not terminal and not unknown_stock and amount < qty:
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


def _rank(cands: list[Candidate], usual: dict[str, int] | None = None) -> None:
    """Score and sort the options. `usual`: how often the customer bought each product for this item; the
    product they buy most gets up to USUAL_POINTS, enough to beat a cheaper or closer match but never a
    missing requirement (100) or a worse availability (25 a step); it also counts as the best match."""
    totals = [c.line_total for c in cands if c.line_total is not None]
    lo, hi = (min(totals), max(totals)) if totals else (0.0, 0.0)
    good = [c.line_total for c in cands if c.line_total is not None and c.meets_constraints and c.availability.status == "ok"]
    good_lo = min(good) if good else None
    other_points: dict[str, float] = {}  # score without the price part, for the CO2e tie-breaker
    bought = sum((usual or {}).values())
    top = max((usual or {}).values(), default=0)  # the product bought most gets all the points, others in proportion
    usual_pts = {pid: USUAL_POINTS * n / top for pid, n in (usual or {}).items()} if top else {}
    best_match = max((c.match_score for c in cands), default=0.0)
    for c in cands:
        cost_pts = 0.0
        if c.line_total is not None:
            cost_pts = 25.0 if hi == lo else 25.0 * (hi - c.line_total) / (hi - lo)
        rel = c.delivery_reliability
        c.score = round(
            (100 if c.meets_constraints else 0)
            + 25 * STATUS_POINTS.get(c.availability.status, 0)
            # relevance first: the right product beats a cheaper wrong one (the usual product is the right one)
            + 4 * min(best_match if c.product_id in usual_pts else c.match_score, 10)
            + cost_pts
            + (10 * rel if rel is not None else 5)
            + (0 if c.line_total is not None else -60)
            + usual_pts.get(c.product_id, 0.0),
            2,
        )
        other_points[c.product_id] = c.score - cost_pts
        reasons = []
        if usual and usual.get(c.product_id):
            reasons.append(f"your usual choice ({usual[c.product_id]} of your {bought} orders)")
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
    _co2_tiebreak(cands, other_points)


def _same_price(a: Candidate, b: Candidate) -> bool:
    if a.line_total is None or b.line_total is None:
        return False
    return abs(a.line_total - b.line_total) <= CO2_PRICE_TOLERANCE * max(a.line_total, b.line_total, 0.01)


def _co2_tiebreak(cands: list[Candidate], other_points: dict[str, float]) -> None:
    """At (nearly) the same price, put the option with the lower carbon footprint first.

    Only between otherwise comparable options: both match the request or both don't, same availability,
    and similar relevance and reliability. The CO2e figure is a product-group average, so this reorders
    options from different product groups; options in the same group have the same figure.
    """
    for _ in range(len(cands)):
        swapped = False
        for i in range(len(cands) - 1):
            a, b = cands[i], cands[i + 1]
            if (a.co2_per_kg is not None and b.co2_per_kg is not None and b.co2_per_kg < a.co2_per_kg
                    and a.meets_constraints == b.meets_constraints and a.availability.status == b.availability.status
                    and _same_price(a, b)
                    and abs(other_points[a.product_id] - other_points[b.product_id]) <= CO2_OTHER_POINTS):
                cands[i], cands[i + 1] = b, a
                swapped = True
                if "lower CO₂e" not in b.rank_reason:
                    b.rank_reason += (f" · lower CO₂e than a similarly priced option "
                                      f"({b.co2_per_kg:.2g} vs {a.co2_per_kg:.2g} kg CO₂e/kg, product-group average)")
        if not swapped:
            break


def _recommend(cands: list[Candidate]) -> str | None:
    # Keep exceptions visible, but do not preselect an infeasible commitment.
    feasible = [
        c for c in cands
        if c.meets_constraints and c.availability.status == "ok" and not c.availability.sales_block
        and c.quantity is not None and math.isfinite(c.quantity) and c.quantity > 0
        and c.line_total is not None and math.isfinite(c.line_total) and c.line_total >= 0
        and c.price.unit_price is not None and not c.price.error
    ]
    return feasible[0].product_id if feasible else None


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
    preferred: list | None = None,
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

    # The customer's usual products, without any the request rules out ("cheese, not cream cheese" drops the
    # usual mascarpone), so they are neither added as options nor ranked first.
    if preferred:
        preferred = [
            {pid: n for pid, n in usual.items()
             if pid in catalog.by_id and not catalog.excluded(catalog.by_id[pid], it.search_phrases or [it.product_query], it.exclude_words)}
            for usual, it in zip(preferred, parsed.items)
        ]

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
                if sp and sid not in ids and matches_product_kind(sp, phrases):
                    ok, notes, hard = constraint_check(sp, item.constraints, customer)
                    if not hard:
                        shortlist.append((sp, max(score - 1, 0.5), ok, notes, f"Valio Aimo substitute for {p['id']}"))
                        ids.add(sid)
        # the products this customer usually buys for this item, so the usual one can be recommended
        usual = preferred[len(shortlists)] if preferred and len(shortlists) < len(preferred) else {}
        for pid, _ in sorted(usual.items(), key=lambda kv: -kv[1])[:USUAL_PRODUCTS]:
            up = catalog.by_id.get(pid)
            if up and pid not in ids:
                ok, notes, hard = constraint_check(up, item.constraints, customer)
                if not hard:
                    shortlist.append((up, 1.0, ok, notes, ""))
                    ids.add(pid)
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
        # price and availability are independent: ask both at once
        price_res, avail_res = await asyncio.gather(
            valio.price(ids, parsed.delivery_date, customer.id),
            valio.availability(ids, parsed.delivery_date, customer.id),
            return_exceptions=True,
        )
        ms = int((time.perf_counter() - t) * 1000)
        if isinstance(price_res, ValioAPIError):
            op("get_price", f"Price API failed: {price_res}", "failed", ms)
        elif isinstance(price_res, BaseException):
            raise price_res
        else:
            prices, meta = price_res
            found = sum(1 for pid in ids if not _price_info(prices.get(pid)).error)
            op("get_price", f"POST /api/price for {len(ids)} products on {parsed.delivery_date:%d %b}: {found} prices"
               + (f" ({meta['from_cache']} from cache)" if meta.get("from_cache") else ""), ms=ms)
            source["currency"] = meta.get("currency") or "EUR"
        if isinstance(avail_res, ValioAPIError):
            op("check_availability", f"Availability API failed: {avail_res}", "failed", ms)
        elif isinstance(avail_res, BaseException):
            raise avail_res
        else:
            avail, meta = avail_res
            time_now = _parse_dt(meta.get("time_now")) or now
            source["time_now"] = time_now.isoformat()
            op("check_availability", f"POST /api/availability for {len(ids)} products: answers for {len(avail)}"
               + (f" ({meta['from_cache']} from cache)" if meta.get("from_cache") else ""), ms=ms)
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
        _rank(cands, preferred[len(items)] if preferred and len(items) < len(preferred) else None)
        rec = _recommend(cands)
        n_meet = sum(1 for c in cands if c.meets_constraints)
        n_on_time = sum(1 for c in cands if c.availability.status == "ok")
        if not cands:
            note = "No matching products found. Try other words, or remove a requirement."
        else:
            note = f"{n_meet} of {len(cands)} shortlisted options meet the request; {n_on_time} can be delivered on {parsed.delivery_date:%a %d %b}."
            if rec is None:
                note += " No feasible option is selected. Adjust the request or evaluate an alternative."
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
