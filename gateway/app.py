"""Aimo Agent Gateway: the agent-facing REST API and the web UI (served from ui/).

Run from the repo root:   python -m uvicorn gateway.app:app --reload --port 8000
"""
from __future__ import annotations

import base64
import secrets
import statistics
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import rules_parser
from agent.service import claude_available, parse_request
from contracts import vocab
from contracts.models import Customer, Offer, Order, ParsedRequest
from gateway.catalog import load_catalog
from gateway.config import TZ, settings
from gateway.planner import _availability, _parse_dt, _price_info, build_offer, describe_item, verify_offer
from gateway.store import Store
from gateway.valio_client import ValioAPIError, ValioClient
from trust.audit import AuditLog
from trust.customers import CustomerRegistry, trust_profile
from trust.policy import eur, evaluate, has_claim
from trust.signing import Signer

S = SimpleNamespace(catalog=None, catalog_error=None, valio=None, signer=None, customers=None, audit=None, store=None, ping=(0.0, False))

OUTCOME_LABEL = {"auto_approve": "approved automatically", "needs_approval": "needs a person", "reject": "rejected"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        S.catalog = load_catalog(settings.valio_dir)
        print(f"Catalog loaded: {len(S.catalog):,} products", flush=True)
    except FileNotFoundError as e:
        S.catalog_error = str(e)
        print("WARNING:", e, flush=True)
    S.valio = ValioClient(settings.aimo_api_base_url, settings.aimo_api_key)
    S.signer = Signer(settings.runtime_dir / "signing_key.pem")
    S.customers = CustomerRegistry(settings.runtime_dir / "customers.json")
    S.audit = AuditLog(settings.runtime_dir / "audit.jsonl")
    S.store = Store(settings.runtime_dir)
    yield
    await S.valio.close()


app = FastAPI(
    title="Aimo Agent Gateway",
    version="0.1.0-mvp",
    lifespan=lifespan,
    description=(
        "Junction 2026 prototype. An interface through which an external purchasing agent (or a person) can turn a "
        "need into a fixed-price, signed offer and an order that is either within the customer's authority or sent "
        "to a human for approval. Synthetic data only."
    ),
)


# ----------------------------------------------------------------------------- site password (public deployments)

OPEN_PATHS = ("/api/health", "/.well-known/")


def _authorised(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user.encode(), settings.demo_user.encode()) and secrets.compare_digest(
        password.encode(), settings.demo_password.encode()
    )


@app.middleware("http")
async def demo_password(request: Request, call_next):
    """HTTP Basic auth for the whole site when DEMO_PASSWORD is set; off for local development."""
    if settings.demo_password and not request.url.path.startswith(OPEN_PATHS):
        if not _authorised(request.headers.get("authorization", "")):
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Aimo Agent Gateway"'})
    return await call_next(request)


# ----------------------------------------------------------------------------- helpers


def _today() -> date:
    return datetime.now(TZ).date()


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _customer(customer_id: str) -> Customer:
    c = S.customers.get(customer_id)
    if not c:
        raise HTTPException(404, f"Unknown customer {customer_id}")
    return c


def _offer(offer_id: str) -> Offer:
    o = S.store.offers.get(offer_id)
    if not o:
        raise HTTPException(404, f"Unknown offer {offer_id}")
    return o


def _need_catalog() -> None:
    if S.catalog is None:
        raise HTTPException(503, S.catalog_error or "The product catalog is not loaded")


def _customer_out(c: Customer) -> dict:
    return {**c.model_dump(mode="json"), "trust": trust_profile(c)}


# ----------------------------------------------------------------------------- meta


@app.get("/api/health", tags=["meta"])
async def health():
    now = time.time()
    if now - S.ping[0] > 60:
        S.ping = (now, await S.valio.ping())
    return {
        "catalog": {"loaded": S.catalog is not None, "products": len(S.catalog) if S.catalog else 0, "error": S.catalog_error},
        "valio_api": {"ok": S.ping[1], "base_url": settings.aimo_api_base_url, "key_configured": bool(settings.aimo_api_key)},
        "parser": "claude" if claude_available(settings.anthropic_api_key, settings.parser) else "rules",
        "model": settings.claude_model,
        "today": _today().isoformat(),
    }


@app.get("/api/vocab", tags=["meta"])
def get_vocab():
    return vocab.as_dict()


@app.get("/.well-known/aimo-agent-gateway.json", tags=["meta"])
def well_known():
    """Discovery document for external agents: what the gateway offers and how to verify its claims."""
    return {
        "name": "Aimo Agent Gateway (Junction 2026 prototype)",
        "openapi": "/openapi.json",
        "docs": "/docs",
        "flow": [
            "POST /api/requests  - natural-language need -> signed fixed-price offer + policy preview",
            "POST /api/offers/{id}/evaluate - preview the decision for other selections",
            "POST /api/orders - commit an offer: placed | pending_approval | rejected",
            "GET  /api/audit - readable, hash-chained record of every step",
        ],
        "offer_signing": {
            "algorithm": "Ed25519",
            "public_key_base64": S.signer.public_key_b64(),
            "signed_message": "content_hash (hex), where content_hash = SHA-256 of the offer's canonical JSON "
            "(sorted keys, compact separators) without the content_hash and signature fields",
        },
        "data_boundary": [
            "Only curated product data, customer-specific prices and availability are exposed",
            "No purchase prices, margins, supplier terms or other customers' data",
            "The Valio Aimo backend key stays on the server",
        ],
    }


# ----------------------------------------------------------------------------- customers


class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    segment: str = Field(default="restaurant", max_length=60)
    business_id: str = Field(default="", max_length=20)


class ReviewerIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=60)


@app.get("/api/customers", tags=["customers"])
def list_customers():
    return [_customer_out(c) for c in S.customers.all()]


@app.post("/api/customers", tags=["customers"])
def register_customer(body: CustomerIn):
    c = S.customers.register(body.name, body.segment, body.business_id)
    S.audit.append(
        "system", "customer.registered", c.id,
        f"New customer {c.name} registered (business ID {c.business_id or 'missing'}, checksum "
        f"{'OK' if c.business_id_valid else 'failed'}): not on the whitelist yet",
        {"business_id": c.business_id, "business_id_valid": c.business_id_valid},
    )
    return _customer_out(c)


@app.post("/api/customers/{customer_id}/verify", tags=["customers"])
def verify_customer(customer_id: str, body: ReviewerIn):
    c = _customer(customer_id)
    if c.status == "trusted":
        return _customer_out(c)
    if not c.business_id_valid:
        raise HTTPException(400, "The business ID fails the Y-tunnus checksum: fix it before verifying")
    c = S.customers.verify(customer_id)
    S.audit.append(
        f"human:{body.reviewer.strip()}", "customer.whitelisted", c.id,
        f"{body.reviewer.strip()} verified {c.name} and added it to the whitelist (auto-approval limit €{c.auto_approve_limit:,.0f})",
    )
    return _customer_out(c)


# ----------------------------------------------------------------------------- requests & offers


class RequestIn(BaseModel):
    customer_id: str
    text: str = Field(min_length=1, max_length=2000)
    override: Optional[ParsedRequest] = None  # the person's corrections to what the agent understood


class SelectionIn(BaseModel):
    selections: dict[int, str] = Field(default_factory=dict)  # item index -> product id


@app.post("/api/requests", tags=["ordering"])
async def create_request(body: RequestIn):
    """Natural-language need -> parsed intent -> catalog search -> Valio price & availability -> signed offer."""
    _need_catalog()
    customer = _customer(body.customer_id)
    text = body.text.strip()
    if body.override:
        parsed, parse_ms = body.override, 0
        parsed.warnings = []
        parsed.delivery_date_assumed = False
        S.audit.append(f"customer:{customer.id}", "request.edited", customer.id,
                       f"{customer.name} corrected the request: " + "; ".join(describe_item(i) for i in parsed.items))
    else:
        S.audit.append(f"customer:{customer.id}", "request.received", customer.id, f"{customer.name} asked: “{_clip(text, 180)}”")
        parsed, parse_ms = await parse_request(
            text, _today(), api_key=settings.anthropic_api_key, mode=settings.parser, model=settings.claude_model
        )
        S.audit.append(
            "agent", "request.parsed", customer.id,
            "Understood: " + ("; ".join(describe_item(i) for i in parsed.items) or "no products")
            + f" · delivery {parsed.delivery_date:%a %d %b}" + (" (assumed)" if parsed.delivery_date_assumed else ""),
            {"parser": parsed.parser, "warnings": parsed.warnings},
        )
    offer = await build_offer(
        customer=customer, parsed=parsed, request_text=text, parse_ms=parse_ms, catalog=S.catalog,
        valio=S.valio, signer=S.signer, ttl_minutes=settings.offer_ttl_minutes,
    )
    S.store.save_offer(offer)
    n_options = sum(len(i.candidates) for i in offer.items)
    S.audit.append(
        "agent", "offer.created", offer.id,
        f"Offer {offer.id} for {customer.name}: compared {n_options} options using Valio Aimo's price and availability APIs; "
        f"fixed prices until {offer.valid_until:%H:%M}",
        {"customer_id": customer.id, "content_hash": offer.content_hash,
         "operations": [f"{o.name}: {o.detail}" for o in offer.operations]},
    )
    decision, _ = evaluate(customer, offer, {}, datetime.now(TZ), verify_offer(offer, S.signer))
    return {"offer": offer.model_dump(mode="json"), "decision": decision.model_dump(mode="json"), "customer": _customer_out(customer)}


@app.get("/api/offers/{offer_id}", tags=["ordering"])
def get_offer(offer_id: str):
    return _offer(offer_id).model_dump(mode="json")


@app.post("/api/offers/{offer_id}/evaluate", tags=["ordering"])
def evaluate_offer(offer_id: str, body: SelectionIn):
    """Preview the policy decision for a selection, without placing anything."""
    offer = _offer(offer_id)
    decision, _ = evaluate(_customer(offer.customer_id), offer, body.selections, datetime.now(TZ), verify_offer(offer, S.signer))
    return decision.model_dump(mode="json")


# ----------------------------------------------------------------------------- orders & approvals


class OrderIn(BaseModel):
    offer_id: str
    selections: dict[int, str] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=2000)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=60)
    comment: str = Field(default="", max_length=500)
    verify_customer: bool = False


def _order_out(o: Order) -> dict:
    c = S.customers.get(o.customer_id)
    offer = S.store.offers.get(o.offer_id)
    return {
        **o.model_dump(mode="json"),
        "customer_trust": trust_profile(c) if c else None,
        "offer_valid_until": offer.valid_until.isoformat() if offer else None,
    }


@app.post("/api/orders", tags=["ordering"])
def place_order(body: OrderIn):
    """Commit an offer. The policy decides: placed (within authority), pending_approval, or rejected."""
    offer = _offer(body.offer_id)
    customer = _customer(offer.customer_id)
    used = next((o for o in S.store.orders.values() if o.offer_id == offer.id), None)
    if used:
        raise HTTPException(409, f"Offer {offer.id} was already used for order {used.id}")
    now = datetime.now(TZ)
    decision, lines = evaluate(customer, offer, body.selections, now, verify_offer(offer, S.signer))
    status = {"auto_approve": "placed", "needs_approval": "pending_approval", "reject": "rejected"}[decision.outcome]
    order = Order(
        id=S.store.next_order_id(), offer_id=offer.id, customer_id=customer.id, customer_name=customer.name,
        created_at=now, status=status, lines=lines, total=decision.total, decision=decision,
        rationale=body.rationale.strip(), request_text=offer.request_text,
    )
    S.store.save_order(order)
    failed = [c for c in decision.checks if not c.passed and c.severity != "info"]
    S.audit.append(
        "policy", "policy.evaluated", order.id,
        f"Policy: {order.id} {OUTCOME_LABEL[decision.outcome]} ({eur(order.total)})"
        + (": " + "; ".join(c.message for c in failed[:3]) if failed else ""),
        {"offer_id": offer.id, "outcome": decision.outcome, "failed_checks": [c.code for c in failed]},
    )
    lines_text = "; ".join(f"{l.quantity:,.10g} × {l.name}" for l in lines)
    data = {"offer_id": offer.id, "rationale": order.rationale, "lines": [l.model_dump(mode="json") for l in lines]}
    if status == "placed":
        S.customers.record_order(customer.id, order.total)
        S.audit.append("agent", "order.placed", order.id,
                       f"{order.id} placed automatically for {customer.name}: {lines_text} = {eur(order.total)} (simulated: the mock backend has no order endpoint)", data)
    elif status == "pending_approval":
        S.audit.append("agent", "order.sent_for_approval", order.id,
                       f"{order.id} for {customer.name} sent to a person for approval ({eur(order.total)})", data)
    else:
        S.audit.append("agent", "order.rejected", order.id, f"{order.id} not placed: {decision.summary}", data)
    return _order_out(order)


@app.get("/api/orders", tags=["ordering"])
def list_orders(status: Optional[str] = None):
    return [_order_out(o) for o in S.store.list_orders(status)]


@app.post("/api/orders/{order_id}/decision", tags=["approvals"])
def decide_order(order_id: str, body: DecisionIn):
    """A person approves or rejects an order the agent could not place on its own."""
    order = S.store.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"Unknown order {order_id}")
    if order.status != "pending_approval":
        raise HTTPException(409, f"{order_id} is already {order.status}")
    customer = _customer(order.customer_id)
    reviewer = body.reviewer.strip()
    comment = body.comment.strip()
    order.reviewer, order.reviewed_at, order.review_comment = reviewer, datetime.now(TZ), comment or None
    if body.decision == "approve":
        if body.verify_customer and customer.status == "unverified":
            if not customer.business_id_valid:
                raise HTTPException(400, "The business ID fails the checksum: can't add this customer to the whitelist")
            customer = S.customers.verify(customer.id)
            S.audit.append(f"human:{reviewer}", "customer.whitelisted", customer.id,
                           f"{reviewer} verified {customer.name} and added it to the whitelist (auto-approval limit €{customer.auto_approve_limit:,.0f})")
        order.status = "placed"
        S.customers.record_order(customer.id, order.total)
        S.audit.append(f"human:{reviewer}", "order.approved", order.id,
                       f"{reviewer} approved {order.id} for {customer.name} ({eur(order.total)})" + (f": “{_clip(comment, 120)}”" if comment else ""),
                       {"comment": comment})
    else:
        order.status = "rejected"
        S.audit.append(f"human:{reviewer}", "order.rejected_by_person", order.id,
                       f"{reviewer} rejected {order.id} for {customer.name}" + (f": “{_clip(comment, 120)}”" if comment else ""),
                       {"comment": comment})
    S.store.save_order(order)
    return _order_out(order)


# ----------------------------------------------------------------------------- audit


@app.get("/api/audit", tags=["audit"])
def get_audit(subject: Optional[str] = None):
    events = S.audit.events(subject)
    return {"chain": S.audit.verify(), "events": [e.model_dump(mode="json") for e in reversed(events)]}


# ----------------------------------------------------------------------------- catalog explorer

FACETS = [
    ("claim", "FREE_FROM_LACTOSE", "Lactose-free"),
    ("claim", "FREE_FROM_GLUTEN", "Gluten-free"),
    ("claim", "VEGAN", "Vegan"),
    ("label", "EU_ORGANIC_FARMING", "EU organic"),
    ("origin", "fi", "Finnish"),
    ("label", "MARINE_STEWARDSHIP_COUNCIL_LABEL", "MSC"),
]


def _passes(p: dict, key: str) -> bool:
    kind, _, code = key.partition(":")
    if kind == "claim":
        return has_claim(p, code)
    if kind == "label":
        return code in (p.get("labels") or [])
    if kind == "origin":
        return p.get("country") == code or bool(set(p.get("labels") or []) & vocab.FINNISH_LABELS)
    return True


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


@app.get("/api/catalog/search", tags=["catalog"])
def catalog_search(
    q: str = Query(min_length=2, max_length=80),
    filters: str = Query(default="", description="comma-separated, e.g. claim:VEGAN,origin:fi"),
    category: str = "",
    limit: int = Query(default=40, ge=1, le=100),
):
    """Explore the static assortment (no live API calls): facets, product groups with CO2e, results."""
    _need_catalog()
    phrases = rules_parser.query_phrases(q)
    matches = [p for p, _ in S.catalog.search(phrases, limit=800)]
    active = [f for f in filters.split(",") if f.strip()]
    filtered = [p for p in matches if all(_passes(p, f) for f in active)]
    facets = [
        {"key": f"{k}:{c}", "label": label, "count": sum(1 for p in filtered if _passes(p, f"{k}:{c}")), "active": f"{k}:{c}" in active}
        for k, c, label in FACETS
    ]
    groups_map: dict[str, list[dict]] = defaultdict(list)
    for p in filtered:
        groups_map[p.get("category") or "?"].append(p)
    groups = []
    for cat, ps in groups_map.items():
        co2 = [p["co2"] for p in ps if p.get("co2") is not None]
        rel = [p["reliability"] for p in ps if p.get("reliability") is not None]
        groups.append({"category": cat, "count": len(ps), "co2": _mean(co2), "reliability": _mean(rel), "example": ps[0]["name"]})
    groups.sort(key=lambda g: (g["co2"] is None, -(g["co2"] or 0)))
    if category:
        filtered = [p for p in filtered if p.get("category") == category]
    rel = [p["reliability"] for p in filtered if p.get("reliability") is not None]
    keep = ("id", "name", "brand", "vendor", "category", "sales_unit", "content", "content_unit", "claims", "labels",
            "contains", "may_contain", "country", "reliability", "co2")
    return {
        "query": q,
        "phrases": phrases,
        "total": len(filtered),
        "facets": facets,
        "groups": groups[:12],
        "groups_total": len(groups),
        "kpis": {
            "total": len(filtered),
            "with_reliability": len(rel),
            "avg_reliability": _mean(rel),
            "with_co2": sum(1 for p in filtered if p.get("co2") is not None),
        },
        "results": [{k: p.get(k) for k in keep} for p in filtered[:limit]],
    }


class QuoteIn(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=20)
    requested_date: date
    customer_id: str = "C-0000"


@app.post("/api/catalog/quote", tags=["catalog"])
async def catalog_quote(body: QuoteIn):
    """Live price & availability for a few hand-picked products (keeps Valio's API usage narrow)."""
    try:
        prices, _ = await S.valio.price(body.product_ids, body.requested_date, body.customer_id)
        avail, meta = await S.valio.availability(body.product_ids, body.requested_date, body.customer_id)
    except ValioAPIError as e:
        raise HTTPException(502, str(e))
    time_now = _parse_dt(meta.get("time_now")) or datetime.now(TZ)
    return {
        pid: {
            "price": _price_info(prices.get(pid)).model_dump(mode="json"),
            "availability": _availability(avail.get(pid), None, body.requested_date, time_now).model_dump(mode="json"),
        }
        for pid in body.product_ids
    }


# ----------------------------------------------------------------------------- admin


@app.post("/api/admin/reset", tags=["meta"])
def reset_demo():
    """Restore the demo customers and clear offers, orders and the audit trail."""
    S.customers.reset()
    S.store.reset()
    S.audit.reset()
    S.audit.append("system", "demo.reset", "system", "Demo data reset: demo customers restored, orders and audit trail cleared")
    return {"ok": True}


# The web UI. Mounted last so every /api route above takes precedence.
app.mount("/", StaticFiles(directory=settings.ui_dir, html=True), name="ui")
