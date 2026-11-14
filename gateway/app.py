"""Aimo Agent Gateway: the agent-facing REST API and the web UI (served from ui/).

Run from the repo root:   python -m uvicorn gateway.app:app --reload --port 8000
"""
from __future__ import annotations

import statistics
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import rules_parser
from agent.service import claude_available, parse_conversation, parse_request
from contracts import vocab
from contracts.models import Customer, Offer, Order, ParsedRequest
from gateway.catalog import load_catalog
from gateway.config import TZ, settings
from gateway.planner import _availability, _parse_dt, _price_info, build_offer, describe_item, verify_offer
from gateway.store import Store
from gateway.valio_client import ValioAPIError, ValioClient
from trust.audit import AuditLog
from trust.auth import COOKIE, SESSION_SECONDS, Principal, SessionSigner, password_ok
from trust.customers import CustomerRegistry, trust_profile
from trust.policy import eur, evaluate, has_claim
from trust.signing import Signer

S = SimpleNamespace(catalog=None, catalog_error=None, valio=None, signer=None, customers=None, audit=None, store=None,
                    sessions=None, ping=(0.0, False))

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
    S.sessions = SessionSigner(settings.runtime_dir / "session_secret")
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


# ----------------------------------------------------------------------------- sign-in: buyer portal and Valio admin

OPEN_API = ("/api/health", "/api/session")


def _principal(request: Request) -> Principal | None:
    return S.sessions.read(request.cookies.get(COOKIE)) if S.sessions else None


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """Every API call except health and sign-in needs a session. The web UI and agent discovery stay public."""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(OPEN_API) and _principal(request) is None:
        return JSONResponse({"detail": "Sign in first"}, status_code=401)
    return await call_next(request)


def signed_in(request: Request) -> Principal:
    who = _principal(request)
    if who is None:
        raise HTTPException(401, "Sign in first")
    return who


def buyer(request: Request) -> Principal:
    who = signed_in(request)
    if who.role != "buyer":
        raise HTTPException(403, "Only the buyer portal can do this")
    return who


def valio_staff(request: Request) -> Principal:
    who = signed_in(request)
    if who.role != "valio":
        raise HTTPException(403, "Only Valio Aimo staff can do this")
    return who


def _passwords_required() -> bool:
    return bool(settings.buyer_password or settings.valio_password)


class LoginIn(BaseModel):
    role: Literal["buyer", "valio"]
    customer_id: str = Field(default="", max_length=20)  # buyer portal: which customer
    name: str = Field(default="", max_length=60)  # Valio admin: the staff member's name, recorded with decisions
    password: str = Field(default="", max_length=200)


@app.get("/api/session", tags=["session"])
def get_session(request: Request):
    """Who is signed in, and what the sign-in page needs (the demo customers, whether passwords are required)."""
    who = _principal(request)
    return {
        "signed_in": who is not None,
        "role": who.role if who else None,
        "name": who.name if who else None,
        "customer_id": who.customer_id if who else None,
        "password_required": _passwords_required(),
        "customers": [{"id": c.id, "name": c.name} for c in S.customers.all()],
    }


@app.post("/api/session/login", tags=["session"])
def login(body: LoginIn, request: Request, response: Response):
    """Sign in to the buyer portal (as one customer) or to the Valio Aimo admin (as a named staff member)."""
    if body.role == "buyer":
        c = S.customers.get(body.customer_id.strip())
        if not c:
            raise HTTPException(400, "Choose your company")
        who, expected = Principal(role="buyer", name=c.name, customer_id=c.id), settings.buyer_password
    else:
        name = " ".join(body.name.replace(":", " ").split())
        if not name:
            raise HTTPException(400, "Enter your name: it is recorded with your decisions")
        who, expected = Principal(role="valio", name=name), settings.valio_password
    if _passwords_required() and (not expected or not password_ok(body.password, expected)):
        raise HTTPException(401, "Wrong password" if expected else "This sign-in is not set up on the server")
    response.set_cookie(COOKIE, S.sessions.issue(who), max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
    portal = "the buyer portal" if who.role == "buyer" else "the Valio Aimo admin"
    S.audit.append(who.actor, "session.signed_in", who.customer_id or "valio-admin", f"{who.name} signed in to {portal}")
    return {"signed_in": True, "role": who.role, "name": who.name, "customer_id": who.customer_id}


@app.post("/api/session/logout", tags=["session"])
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"signed_in": False}


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


def _offer(offer_id: str, who: Principal | None = None) -> Offer:
    """The offer; a buyer only ever sees its own (others look like they don't exist)."""
    o = S.store.offers.get(offer_id)
    if not o or (who is not None and who.role == "buyer" and o.customer_id != who.customer_id):
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
            "POST /api/session/login {role: buyer, customer_id, password} - session cookie for the buyer portal",
            "POST /api/requests  - natural-language need -> signed fixed-price offer + policy preview",
            "POST /api/offers/{id}/evaluate - preview the decision for other selections",
            "POST /api/orders - commit an offer: placed | pending_approval | rejected",
            "GET  /api/audit - readable, hash-chained record of every step",
        ],
        "offer_signing": {
            "algorithm": "Ed25519",
            "public_key_base64": S.signer.public_key_b64(),
            "signed_message": "content_hash (hex), where content_hash = SHA-256 of the offer's canonical JSON "
            "(sorted keys, compact separators, UTF-8 without ASCII escaping) without the content_hash and signature fields; "
            "sign the UTF-8 bytes of the hexadecimal hash",
        },
        "audit_hashing": {
            "algorithm": "SHA-256",
            "genesis": "0" * 64,
            "message": "UTF-8 bytes of prev_hash followed by canonical JSON of the event excluding hash; "
            "prev_hash is included in that JSON. Canonical JSON uses sorted keys, compact separators and UTF-8 "
            "without ASCII escaping. Normalize ts to ISO 8601 with seconds and an explicit offset (+00:00 for Z).",
            "limit": "A hash chain needs an independently saved checkpoint to detect complete history rewrites.",
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


@app.get("/api/customers", tags=["customers"])
def list_customers(who: Principal = Depends(signed_in)):
    """Valio Aimo staff see every customer; a buyer sees only its own record."""
    if who.role == "buyer":
        return [_customer_out(_customer(who.customer_id))]
    return [_customer_out(c) for c in S.customers.all()]


@app.post("/api/customers", tags=["customers"])
def register_customer(body: CustomerIn, who: Principal = Depends(valio_staff)):
    c = S.customers.register(body.name, body.segment, body.business_id)
    S.audit.append(
        who.actor, "customer.registered", c.id,
        f"{who.name} registered new customer {c.name} (business ID {c.business_id or 'missing'}, checksum "
        f"{'OK' if c.business_id_valid else 'failed'}): not on the whitelist yet",
        {"business_id": c.business_id, "business_id_valid": c.business_id_valid},
    )
    return _customer_out(c)


@app.post("/api/customers/{customer_id}/verify", tags=["customers"])
def verify_customer(customer_id: str, who: Principal = Depends(valio_staff)):
    c = _customer(customer_id)
    if c.status == "trusted":
        return _customer_out(c)
    if not c.business_id_valid:
        raise HTTPException(400, "The business ID fails the Y-tunnus checksum: fix it before verifying")
    c = S.customers.verify(customer_id)
    S.audit.append(
        who.actor, "customer.whitelisted", c.id,
        f"{who.name} verified {c.name} and added it to the whitelist (auto-approval limit €{c.auto_approve_limit:,.0f})",
    )
    return _customer_out(c)


# ----------------------------------------------------------------------------- requests & offers


class RequestIn(BaseModel):
    customer_id: Optional[str] = None  # optional: the buyer's session decides who is ordering
    text: str = Field(min_length=1, max_length=2000)
    override: Optional[ParsedRequest] = None  # the person's corrections to what the agent understood


class SelectionIn(BaseModel):
    selections: dict[int, str] = Field(default_factory=dict)  # item index -> product id


@app.post("/api/requests", tags=["ordering"])
async def create_request(body: RequestIn, who: Principal = Depends(buyer)):
    """Natural-language need -> parsed intent -> catalog search -> Valio price & availability -> signed offer."""
    if body.customer_id and body.customer_id != who.customer_id:
        raise HTTPException(403, "You can only order for your own company")
    _need_catalog()
    customer = _customer(who.customer_id)
    text = body.text.strip()
    if body.override:
        parsed, parse_ms = body.override, 0
        if not parsed.clarification_required:
            parsed.warnings = []
        parsed.delivery_date_assumed = False
        S.audit.append(f"customer:{customer.id}", "request.edited", customer.id,
                       f"{customer.name} corrected the request: " + "; ".join(describe_item(i) for i in parsed.items))
    else:
        S.audit.append(f"customer:{customer.id}", "request.received", customer.id, f"{customer.name} asked: “{_clip(text, 180)}”")
        parsed, parse_ms = await parse_request(
            text, _today(), api_key=settings.anthropic_api_key, mode=settings.parser, model=settings.claude_model
        )
        _audit_understood(customer, parsed)
    return await _offer_response(customer, parsed, text, parse_ms)


def _audit_understood(customer: Customer, parsed: ParsedRequest) -> None:
    S.audit.append(
        "agent", "request.parsed", customer.id,
        "Understood: " + ("; ".join(describe_item(i) for i in parsed.items) or "no products")
        + f" · delivery {parsed.delivery_date:%a %d %b}" + (" (assumed)" if parsed.delivery_date_assumed else ""),
        {"parser": parsed.parser, "warnings": parsed.warnings},
    )


async def _offer_response(customer: Customer, parsed: ParsedRequest, text: str, parse_ms: int) -> dict:
    """Catalog search -> Valio price & availability -> signed offer, plus the policy preview."""
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


class ChatTurn(BaseModel):
    role: Literal["buyer", "agent"]
    text: str = Field(min_length=1, max_length=2000)


class ChatIn(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1, max_length=30)  # the conversation so far, oldest first
    understood: Optional[ParsedRequest] = None  # what the agent understood after the previous turn
    skip_questions: bool = False  # the buyer wants to see options now, with what is known so far


@app.post("/api/chat", tags=["ordering"])
async def chat(body: ChatIn, who: Principal = Depends(buyer)):
    """Talk to the ordering agent. While a quantity, unit or delivery date is missing (or a condition is
    unclear) it answers with a question; once the request is complete it returns a signed offer."""
    if body.messages[-1].role != "buyer":
        raise HTTPException(400, "The last message must be the buyer's")
    customer = _customer(who.customer_id)
    turns = [(m.role, m.text.strip()) for m in body.messages]
    request_text = " · ".join(text for role, text in turns if role == "buyer")
    if body.skip_questions and body.understood is not None:
        parsed, parse_ms = body.understood, 0  # nothing new was said: use what the agent already understood
    else:
        S.audit.append(who.actor, "request.received", customer.id, f"{customer.name} said: “{_clip(turns[-1][1], 180)}”")
        parsed, question, parse_ms = await parse_conversation(
            turns, body.understood, _today(), api_key=settings.anthropic_api_key, mode=settings.parser,
            model=settings.claude_model,
        )
        if question and not body.skip_questions:
            S.audit.append("agent", "agent.asked", customer.id, f"Agent asked {customer.name}: “{_clip(question, 180)}”",
                           {"parser": parsed.parser})
            return {"kind": "question", "question": question, "understood": parsed.model_dump(mode="json")}
    _need_catalog()
    _audit_understood(customer, parsed)
    return {"kind": "offer", **await _offer_response(customer, parsed, request_text, parse_ms)}


@app.get("/api/offers/{offer_id}", tags=["ordering"])
def get_offer(offer_id: str, who: Principal = Depends(signed_in)):
    return _offer(offer_id, who).model_dump(mode="json")


@app.post("/api/offers/{offer_id}/evaluate", tags=["ordering"])
def evaluate_offer(offer_id: str, body: SelectionIn, who: Principal = Depends(signed_in)):
    """Preview the policy decision for a selection, without placing anything."""
    offer = _offer(offer_id, who)
    decision, _ = evaluate(_customer(offer.customer_id), offer, body.selections, datetime.now(TZ), verify_offer(offer, S.signer))
    return decision.model_dump(mode="json")


# ----------------------------------------------------------------------------- orders & approvals


class OrderIn(BaseModel):
    offer_id: str
    selections: dict[int, str] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=2000)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]  # the reviewer is the signed-in Valio Aimo staff member
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
def place_order(body: OrderIn, who: Principal = Depends(buyer)):
    """Commit an offer. The policy decides: placed (within authority), pending_approval, or rejected."""
    offer = _offer(body.offer_id, who)
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
                       f"{order.id} for {customer.name} sent to Valio Aimo for approval ({eur(order.total)})", data)
    else:
        S.audit.append("agent", "order.rejected", order.id, f"{order.id} not placed: {decision.summary}", data)
    return _order_out(order)


@app.get("/api/orders", tags=["ordering"])
def list_orders(status: Optional[str] = None, who: Principal = Depends(signed_in)):
    """Valio Aimo staff see every order; a buyer sees only its own."""
    orders = S.store.list_orders(status)
    if who.role == "buyer":
        orders = [o for o in orders if o.customer_id == who.customer_id]
    return [_order_out(o) for o in orders]


@app.post("/api/orders/{order_id}/decision", tags=["approvals"])
def decide_order(order_id: str, body: DecisionIn, who: Principal = Depends(valio_staff)):
    """Valio Aimo staff approve or reject an order the agent could not place on its own."""
    order = S.store.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"Unknown order {order_id}")
    if order.status != "pending_approval":
        raise HTTPException(409, f"{order_id} is already {order.status}")
    customer = _customer(order.customer_id)
    reviewer = who.name.strip()
    comment = body.comment.strip()
    if not reviewer:
        raise HTTPException(400, "The reviewer needs a name: sign in again")
    now = datetime.now(TZ)
    if body.decision == "approve":
        # Approval grants authority; it cannot waive hard rules or revive an expired offer.
        offer = _offer(order.offer_id)
        selections = {line.item_index: line.product_id for line in order.lines}
        decision, lines = evaluate(customer, offer, selections, now, verify_offer(offer, S.signer))
        if decision.outcome == "reject":
            S.audit.append(f"human:{reviewer}", "order.approval_blocked", order.id,
                           f"Approval of {order.id} blocked: {decision.summary}")
            raise HTTPException(409, decision.summary)
        old_lines = [line.model_dump(mode="json") for line in order.lines]
        if decision.total != order.total or [line.model_dump(mode="json") for line in lines] != old_lines:
            raise HTTPException(409, "The order changed since review; request a new offer and approval")
        if body.verify_customer and customer.status == "unverified":
            if not customer.business_id_valid:
                raise HTTPException(400, "The business ID fails the checksum: can't add this customer to the whitelist")
            customer = S.customers.verify(customer.id)
            S.audit.append(f"human:{reviewer}", "customer.whitelisted", customer.id,
                           f"{reviewer} verified {customer.name} and added it to the whitelist (auto-approval limit €{customer.auto_approve_limit:,.0f})")
        order.status = "placed"
        order.decision = decision
        S.customers.record_order(customer.id, order.total)
        S.audit.append(f"human:{reviewer}", "order.approved", order.id,
                       f"{reviewer} approved {order.id} for {customer.name} ({eur(order.total)})" + (f": “{_clip(comment, 120)}”" if comment else ""),
                       {"comment": comment})
    else:
        order.status = "rejected"
        S.audit.append(f"human:{reviewer}", "order.rejected_by_person", order.id,
                       f"{reviewer} rejected {order.id} for {customer.name}" + (f": “{_clip(comment, 120)}”" if comment else ""),
                       {"comment": comment})
    order.reviewer, order.reviewed_at, order.review_comment = reviewer, now, comment or None
    S.store.save_order(order)
    return _order_out(order)


# ----------------------------------------------------------------------------- audit


@app.get("/api/audit", tags=["audit"])
def get_audit(subject: Optional[str] = None, who: Principal = Depends(valio_staff)):
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
    who: Principal = Depends(signed_in),
    q: str = Query(min_length=2, max_length=80),
    filters: str = Query(default="", description="comma-separated, e.g. claim:VEGAN,origin:fi"),
    category: str = "",
):
    """Explore the assortment using aggregate metrics only; product-level data is not exposed here."""
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
        groups.append({"category": cat, "count": len(ps), "co2": _mean(co2), "reliability": _mean(rel)})
    groups.sort(key=lambda g: (g["co2"] is None, -(g["co2"] or 0)))
    if category:
        filtered = [p for p in filtered if p.get("category") == category]
    rel = [p["reliability"] for p in filtered if p.get("reliability") is not None]
    co2 = [p["co2"] for p in filtered if p.get("co2") is not None]
    return {
        "total": len(filtered),
        "facets": facets,
        "groups": groups,
        "groups_total": len(groups),
        "kpis": {
            "total": len(filtered),
            "with_reliability": len(rel),
            "avg_reliability": _mean(rel),
            "with_co2": len(co2),
            "avg_co2": _mean(co2),
        },
    }


def _catalog_analytics(products: list[dict]) -> dict:
    categories: dict[str, list[dict]] = defaultdict(list)
    countries: dict[str, list[dict]] = defaultdict(list)
    for product in products:
        categories[product.get("category") or "unknown"].append(product)
        countries[(product.get("country") or "unknown").lower()].append(product)

    def measures(key: str, items: list[dict]) -> dict:
        values = [p[key] for p in items if p.get(key) is not None]
        return {"count": len(values), "average": _mean(values)}

    category_stats = []
    for code, items in categories.items():
        carbon = measures("co2", items)
        reliability = measures("reliability", items)
        category_stats.append({
            "category": code,
            "products": len(items),
            "carbon_products": carbon["count"],
            "avg_co2": carbon["average"],
            "reliability_products": reliability["count"],
            "avg_reliability": reliability["average"],
        })
    category_stats.sort(key=lambda x: (-x["products"], x["category"]))

    country_stats = []
    for code, items in countries.items():
        carbon = measures("co2", items)
        reliability = measures("reliability", items)
        country_stats.append({
            "code": code,
            "products": len(items),
            "carbon_products": carbon["count"],
            "avg_co2": carbon["average"],
            "reliability_products": reliability["count"],
            "avg_reliability": reliability["average"],
        })
    country_stats.sort(key=lambda x: (-x["products"], x["code"]))

    carbon = measures("co2", products)
    reliability = measures("reliability", products)
    return {
        "summary": {
            "products": len(products),
            "categories": len(category_stats),
            "countries": len(country_stats),
            "carbon_products": carbon["count"],
            "avg_co2": carbon["average"],
            "reliability_products": reliability["count"],
            "avg_reliability": reliability["average"],
        },
        "categories": category_stats,
        "countries": country_stats,
    }


@app.get("/api/admin/analytics", tags=["analytics"])
def admin_analytics(who: Principal = Depends(valio_staff)):
    """Full assortment aggregates for Valio Aimo staff; contains no customer or order records."""
    _need_catalog()
    return _catalog_analytics(S.catalog.products)


def _market_watchlist(products: list[dict], limit: int = 12) -> list[dict]:
    """Select a small, varied set for a live operational check."""
    selected: list[dict] = []
    seen_categories: set[str] = set()
    for product in products:
        category = product.get("category") or "unknown"
        if category not in seen_categories:
            selected.append(product)
            seen_categories.add(category)
        if len(selected) == limit:
            break
    return selected


@app.get("/api/admin/market-snapshot", tags=["analytics"])
async def admin_market_snapshot(who: Principal = Depends(valio_staff)):
    """Bounded live price and availability watchlist for Valio Aimo operators."""
    _need_catalog()
    products = _market_watchlist(S.catalog.products)
    requested = datetime.now(TZ).date()
    ids = [product["id"] for product in products]
    try:
        prices, _ = await S.valio.price(ids, requested, "C-0000")
        availability, meta = await S.valio.availability(ids, requested, "C-0000")
    except ValioAPIError as e:
        raise HTTPException(502, f"Live market snapshot unavailable: {e}") from e

    checked_at = _parse_dt(meta.get("time_now")) or datetime.now(TZ)
    items = []
    for product in products:
        price = _price_info(prices.get(product["id"]))
        stock = _availability(availability.get(product["id"]), None, requested, checked_at)
        items.append({
            "id": product["id"], "name": product["name"], "category": product.get("category") or "unknown",
            "sales_unit": product["sales_unit"], "unit_price": price.unit_price,
            "availability": stock.status, "logistic_model": stock.logistic_model,
            "available_amount": stock.available_amount,
        })

    priced = [item["unit_price"] for item in items if item["unit_price"] is not None]
    return {
        "requested_date": requested.isoformat(), "checked_at": checked_at.isoformat(), "items": items,
        "summary": {
            "monitored": len(items), "priced": len(priced), "avg_unit_price": _mean(priced),
            "available": sum(item["availability"] == "ok" for item in items),
            "terminal": sum(item["logistic_model"] == "Terminal" for item in items),
            "attention": sum(item["availability"] in {"partial", "late", "cutoff_passed", "unavailable", "unknown"} for item in items),
        },
    }


class QuoteIn(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=20)
    requested_date: date
    customer_id: str = "C-0000"


@app.post("/api/catalog/quote", tags=["catalog"])
async def catalog_quote(body: QuoteIn, who: Principal = Depends(signed_in)):
    """Live price & availability for a few hand-picked products (keeps Valio's API usage narrow)."""
    customer_id = who.customer_id if who.role == "buyer" else body.customer_id  # buyers get their own prices
    try:
        prices, _ = await S.valio.price(body.product_ids, body.requested_date, customer_id)
        avail, meta = await S.valio.availability(body.product_ids, body.requested_date, customer_id)
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
def reset_demo(who: Principal = Depends(valio_staff)):
    """Restore the demo customers and clear offers, orders and the audit trail (Valio Aimo staff only)."""
    S.customers.reset()
    S.store.reset()
    S.audit.reset()
    S.audit.append(who.actor, "demo.reset", "system",
                   f"{who.name} reset the demo data: demo customers restored, orders and audit trail cleared")
    return {"ok": True}


# The web UI. Mounted last so every /api route above takes precedence.
app.mount("/", StaticFiles(directory=settings.ui_dir, html=True), name="ui")
