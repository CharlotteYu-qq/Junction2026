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

from agent import dialogue, rules_parser
from agent.service import claude_available, parse_conversation, parse_request
from contracts import vocab
from contracts.models import Customer, Offer, Order, ParsedRequest
from gateway.catalog import DEPARTMENT_GROUPS, DEPARTMENT_NAMES, GROUP_DEPARTMENTS, category_name, department_of, load_catalog
from gateway.config import TZ, settings
from gateway import habits, history, recommend
from gateway.alternatives import suggest_fixes, verify_date_fixes
from gateway.planner import _availability, _parse_dt, _price_info, build_offer, describe_item, verify_offer
from gateway.store import Store
from gateway.valio_client import ValioAPIError, ValioClient
from trust.audit import AuditLog
from trust.auth import COOKIE, SESSION_SECONDS, Principal, SessionSigner, password_ok
from trust.customers import CustomerRegistry, trust_profile
from trust.policy import PurchaseHistory, eur, evaluate, has_claim
from trust.signing import Signer

S = SimpleNamespace(catalog=None, catalog_error=None, valio=None, signer=None, customers=None, audit=None, store=None,
                    sessions=None, history={}, ping=(0.0, False))

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
    S.history = recommend.load_history(settings.history_dir)
    if S.history:
        print(f"Order history loaded: {sum(map(len, S.history.values())):,} orders for {len(S.history)} customers", flush=True)
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


# ----------------------------------------------------------------------------- sign-in: the buyer portal

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


def _passwords_required() -> bool:
    return bool(settings.buyer_password)


class LoginIn(BaseModel):
    role: Literal["buyer"] = "buyer"
    customer_id: str = Field(max_length=20)  # which customer the buyer orders for
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
        # Only on a public demo (SHOW_BUYER_PASSWORD): the sign-in page shows it so visitors can sign in.
        "password_hint": settings.buyer_password if _passwords_required() and settings.show_buyer_password else None,
        "customers": [{"id": c.id, "name": c.name} for c in S.customers.all()],
    }


@app.post("/api/session/login", tags=["session"])
def login(body: LoginIn, request: Request, response: Response):
    """Sign in to the buyer portal as one customer."""
    c = S.customers.get(body.customer_id.strip())
    if not c:
        raise HTTPException(400, "Choose your company")
    who = Principal(role="buyer", name=c.name, customer_id=c.id)
    if _passwords_required() and not password_ok(body.password, settings.buyer_password):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(COOKIE, S.sessions.issue(who), max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
    S.audit.append(who.actor, "session.signed_in", c.id, f"{c.name} signed in to the buyer portal")
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
            "POST /api/session/login {customer_id, password} - session cookie for the buyer portal",
            "POST /api/chat - talk to the ordering agent: a follow-up question, or a signed fixed-price offer + policy preview",
            "POST /api/offers/{id}/evaluate - preview the decision for other selections",
            "POST /api/orders - commit an offer: placed | rejected; outside the agent's authority it needs "
            "approved_by (the full name of the approving person in the customer's organisation)",
            "POST /api/orders/{id}/cancel - cancel a placed order until the order cut-off of its delivery",
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


@app.get("/api/customers", tags=["customers"])
def list_customers(who: Principal = Depends(buyer)):
    """The signed-in buyer's own customer record (never anyone else's)."""
    return [_customer_out(_customer(who.customer_id))]


def _orders_for(customer_id: str) -> list[dict]:
    """The customer's order history: the synthetic history file plus every order placed in this portal
    (data/runtime/orders.json), in one shape, oldest first."""
    today = _today()
    live = [history.from_portal(o, S.store.offers.get(o.offer_id), today)
            for o in S.store.list_orders() if o.customer_id == customer_id]
    return sorted(S.history.get(customer_id, []) + [o for o in live if o], key=lambda o: o["ordered_at"])


def _purchase_history(customer_id: str) -> PurchaseHistory | None:
    """What the customer has bought before (cancelled orders left out), for the policy's new-product and
    largest-order checks. None for a customer without history: those checks then do not apply."""
    orders = [o for o in _orders_for(customer_id) if o.get("status") != "cancelled"]
    if not orders:
        return None
    totals = [o["total"] for o in orders if o.get("total")]
    return PurchaseHistory(
        known_products=frozenset(line["product_id"] for o in orders for line in o["lines"] if line.get("product_id")),
        largest_order=max(totals) if totals else None,
    )


@app.get("/api/suggestions", tags=["history"])
def suggestions(who: Principal = Depends(buyer)):
    """Reorder suggestions from the signed-in customer's own order history, ranked by a published formula
    (see `formula`; every suggestion carries its components). Clicking one still runs the full flow:
    a fresh offer with live prices and the usual checks. `ranked` lists every regular item with its score."""
    orders = _orders_for(who.customer_id)
    today = _today()
    return {
        "formula": recommend.FORMULA,
        "suggestions": recommend.suggest(orders, today) if orders else [],
        "ranked": recommend.score_items(orders, today)[:10] if orders else [],
    }


@app.get("/api/history/stats", tags=["history"])
def history_stats(who: Principal = Depends(buyer)):
    """Statistics over the signed-in customer's own order history (synthetic demo data)."""
    orders = _orders_for(who.customer_id)
    return {"available": bool(orders), **(history.stats(orders, _today()) if orders else {})}


@app.get("/api/history/orders", tags=["history"])
def history_orders(q: str = Query("", max_length=80), status: Literal["", "delivered", "cancelled", "confirmed", "short"] = "",
                   offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), who: Principal = Depends(buyer)):
    """The signed-in customer's own past orders, newest first, filtered and paged."""
    return history.page(_orders_for(who.customer_id), q.strip(), status, offset, limit)


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
        used = _apply_habits(customer, parsed, [text], None)
        _audit_understood(customer, parsed)
    return {**await _offer_response(customer, parsed, text, parse_ms), "notice": _parser_notice(parsed),
            "habits": used if not body.override else []}


RULES_NOTICE = ("Our AI assistant is unavailable right now, so a simpler rule-based reader understood this request. "
                "Please double-check the products, quantities and delivery day before you order.")


def _parser_notice(parsed: ParsedRequest) -> Optional[str]:
    """Claude is always configured, so the rule-based parser means it could not be reached: remind the buyer."""
    return RULES_NOTICE if parsed.parser == "rules" else None


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
        preferred=habits.preferences(parsed, _orders_for(customer.id)),
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
    return {"offer": offer.model_dump(mode="json"), "decision": await _decision_out(customer, offer, {}), "customer": _customer_out(customer)}


async def _decision_out(customer: Customer, offer: Offer, selections: dict[int, str]) -> dict:
    """The policy decision for a selection; when it cannot be placed, also the agent's proposed fixes
    (each one already checked against the same rules)."""
    now, integrity_ok = datetime.now(TZ), verify_offer(offer, S.signer)
    past = _purchase_history(customer.id)
    decision, _ = evaluate(customer, offer, selections, now, integrity_ok, past)
    fixes = suggest_fixes(customer, offer, selections, now, integrity_ok, past) if decision.outcome == "reject" else []
    if fixes:  # a proposed delivery day is only shown when live availability says it can work
        fixes = await verify_date_fixes(fixes, offer, customer, S.valio, _availability, _parse_dt)
    return {**decision.model_dump(mode="json"), "fixes": fixes}


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
    unclear) it answers with a question; once the request is complete it returns a signed offer. Asking to
    cancel an order ("cancel my last order") returns that order for the buyer to confirm (kind "cancel")."""
    if body.messages[-1].role != "buyer":
        raise HTTPException(400, "The last message must be the buyer's")
    customer = _customer(who.customer_id)
    turns = [(m.role, m.text.strip()) for m in body.messages]
    request_text = " · ".join(text for role, text in turns if role == "buyer")
    used: list[str] = []  # what the agent filled in from the order history
    if body.skip_questions and body.understood is not None:
        parsed, parse_ms = body.understood, 0  # nothing new was said: use what the agent already understood
    else:
        S.audit.append(who.actor, "request.received", customer.id, f"{customer.name} said: “{_clip(turns[-1][1], 180)}”")
        cancel = dialogue.cancel_intent(turns[-1][1])
        if cancel is not None:
            return _cancel_reply(who, cancel["order_id"])
        parsed, question, parse_ms = await parse_conversation(
            turns, body.understood, _today(), api_key=settings.anthropic_api_key, mode=settings.parser,
            model=settings.claude_model,
        )
        used = _apply_habits(customer, parsed, [t for r, t in turns if r == "buyer"], body.understood)
        if used:  # the question (Claude's phrasing) did not know the history: ask again from what is now open
            question = dialogue.follow_up_question(parsed)
        if question and not body.skip_questions:
            S.audit.append("agent", "agent.asked", customer.id, f"Agent asked {customer.name}: “{_clip(question, 180)}”",
                           {"parser": parsed.parser})
            return {"kind": "question", "question": question, "understood": parsed.model_dump(mode="json"),
                    "notice": _parser_notice(parsed)}
    _need_catalog()
    _audit_understood(customer, parsed)
    return {"kind": "offer", **await _offer_response(customer, parsed, request_text, parse_ms),
            "notice": _parser_notice(parsed), "habits": used}


def _apply_habits(customer: Customer, parsed: ParsedRequest, buyer_messages: list[str],
                  previous: Optional[ParsedRequest]) -> list[str]:
    """Fill "the usual", "same as last time" and missing amounts from the customer's own orders."""
    used = habits.apply(parsed, buyer_messages, previous, _orders_for(customer.id), _today())
    if used:
        S.audit.append("agent", "agent.used_history", customer.id,
                       f"Agent used {customer.name}'s order history: " + "; ".join(used))
    return used


def _cancel_reply(who: Principal, order_id: Optional[str]) -> dict:
    """The order the buyer asked to cancel, for them to confirm; or why there is nothing to cancel."""
    mine = [o for o in S.store.list_orders() if o.customer_id == who.customer_id]  # newest first
    now = datetime.now(TZ)
    if order_id:
        order = next((o for o in mine if o.id == order_id), None)
        if order is None:
            return {"kind": "answer", "text": f"I can't find order {order_id} among your orders."}
    else:
        order = next((o for o in mine if o.status == "placed" and now < _cancel_deadline(o)), None)
        if order is None:
            return {"kind": "answer", "text": "You have no placed order that can still be cancelled."}
    if order.status != "placed":
        state = {"cancelled": "is already cancelled", "rejected": "was not placed",
                 "pending_approval": "is still waiting for approval"}.get(order.status, f"is {order.status}")
        return {"kind": "answer", "text": f"Order {order.id} {state}, so there is nothing to cancel."}
    deadline = _cancel_deadline(order)
    if now >= deadline:
        return {"kind": "answer", "text": f"Order {order.id} can no longer be cancelled: the order cut-off was {deadline:%a %d %b %H:%M}."}
    what = "; ".join(f"{line.quantity:,.10g} × {line.name}" for line in order.lines)
    day = min((line.delivery_date for line in order.lines), default=None)
    question = (f"Do you want me to cancel order {order.id} ({what}, {eur(order.total)}"
                + (f", delivery {day:%a %d %b}" if day else "") + f")? You can cancel it until {deadline:%a %d %b %H:%M}.")
    S.audit.append("agent", "agent.asked", order.id, f"Agent asked {who.name} to confirm cancelling {order.id}")
    return {"kind": "cancel", "question": question, "order": _order_out(order)}


@app.get("/api/offers/{offer_id}", tags=["ordering"])
def get_offer(offer_id: str, who: Principal = Depends(signed_in)):
    return _offer(offer_id, who).model_dump(mode="json")


@app.post("/api/offers/{offer_id}/evaluate", tags=["ordering"])
async def evaluate_offer(offer_id: str, body: SelectionIn, who: Principal = Depends(signed_in)):
    """Preview the policy decision for a selection, without placing anything."""
    offer = _offer(offer_id, who)
    return await _decision_out(_customer(offer.customer_id), offer, body.selections)


# ----------------------------------------------------------------------------- orders & approvals


class OrderIn(BaseModel):
    offer_id: str
    selections: dict[int, str] = Field(default_factory=dict)  # item index -> product id; default: the agent's pick
    rationale: str = Field(default="", max_length=2000)
    # Above the agent's authority, a person in the customer's organisation approves by typing their full name.
    approved_by: str = Field(default="", max_length=80)


def _approver_name(raw: str) -> str:
    """The approving person's full name: at least a first and a last name (a deliberate double-check)."""
    name = " ".join(raw.replace(":", " ").split())
    if len(name.split()) < 2:
        raise HTTPException(400, "Type the approving person's first and last name")
    return name


def _order_out(o: Order) -> dict:
    c = S.customers.get(o.customer_id)
    offer = S.store.offers.get(o.offer_id)
    return {
        **o.model_dump(mode="json"),
        "customer_trust": trust_profile(c) if c else None,
        "offer_valid_until": offer.valid_until.isoformat() if offer else None,
        "cancellable_until": _cancel_deadline(o).isoformat() if o.status == "placed" else None,
    }


def _cancel_deadline(order: Order) -> datetime:
    """A placed order can be cancelled until the order cut-off of its delivery (the earliest across its lines).
    Without a known cut-off, until the start of the delivery day."""
    offer = S.store.offers.get(order.offer_id)
    cutoffs = []
    for line in order.lines if offer else []:
        item = offer.items[line.item_index] if line.item_index < len(offer.items) else None
        cand = next((c for c in item.candidates if c.product_id == line.product_id), None) if item else None
        if cand and cand.availability.closing_time:
            cutoffs.append(cand.availability.closing_time)
    if cutoffs:
        return min(cutoffs)
    first_day = min((line.delivery_date for line in order.lines), default=order.created_at.date())
    return datetime.combine(first_day, datetime.min.time(), tzinfo=TZ)


@app.post("/api/orders", tags=["ordering"])
def place_order(body: OrderIn, who: Principal = Depends(buyer)):
    """Commit an offer. Within the agent's authority it is placed at once. Above it, a person in the customer's
    organisation must approve: `approved_by` is their full name. Hard rules are never waived (rejected)."""
    offer = _offer(body.offer_id, who)
    customer = _customer(offer.customer_id)
    used = next((o for o in S.store.orders.values() if o.offer_id == offer.id), None)
    if used:
        raise HTTPException(409, f"Offer {offer.id} was already used for order {used.id}")
    now = datetime.now(TZ)
    decision, lines = evaluate(customer, offer, body.selections, now, verify_offer(offer, S.signer), _purchase_history(customer.id))
    approver = None
    if decision.outcome == "needs_approval":
        if not body.approved_by.strip():
            raise HTTPException(409, "This order is outside the agent's authority: a person must approve it with their full name")
        approver = _approver_name(body.approved_by)
    status = "rejected" if decision.outcome == "reject" else "placed"
    order = Order(
        id=S.store.next_order_id(), offer_id=offer.id, customer_id=customer.id, customer_name=customer.name,
        created_at=now, status=status, lines=lines, total=decision.total, decision=decision,
        rationale=body.rationale.strip(), request_text=offer.request_text,
        reviewer=approver, reviewed_at=now if approver else None,
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
    simulated = "(simulated: the mock backend has no order endpoint)"
    if status == "placed" and approver:
        reasons = [c.message for c in decision.checks if not c.passed and c.severity == "review"]
        S.customers.record_order(customer.id, order.total)
        S.audit.append(f"human:{approver}", "order.approved", order.id,
                       f"{approver} approved {order.id} for {customer.name} ({eur(order.total)}), outside the agent's "
                       f"authority: " + "; ".join(reasons[:3]), {**data, "approved_by": approver, "reasons": reasons})
        S.audit.append(who.actor, "order.placed", order.id,
                       f"{order.id} placed for {customer.name} after approval by {approver}: {lines_text} = {eur(order.total)} {simulated}", data)
    elif status == "placed":
        S.customers.record_order(customer.id, order.total)
        S.audit.append(who.actor, "order.placed", order.id,
                       f"{order.id} placed for {customer.name}, within the agent's authority: {lines_text} = {eur(order.total)} {simulated}", data)
    else:
        S.audit.append("agent", "order.rejected", order.id, f"{order.id} not placed: {decision.summary}", data)
    return _order_out(order)


@app.get("/api/orders", tags=["ordering"])
def list_orders(status: Optional[str] = None, who: Principal = Depends(buyer)):
    """The signed-in buyer's own orders."""
    return [_order_out(o) for o in S.store.list_orders(status) if o.customer_id == who.customer_id]


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@app.post("/api/orders/{order_id}/cancel", tags=["ordering"])
def cancel_order(order_id: str, body: Optional[CancelIn] = None, who: Principal = Depends(buyer)):
    """Cancel one of your placed orders, until the order cut-off of its delivery."""
    order = S.store.orders.get(order_id)
    if not order or order.customer_id != who.customer_id:
        raise HTTPException(404, f"Unknown order {order_id}")
    if order.status != "placed":
        raise HTTPException(409, f"{order_id} is {order.status.replace('_', ' ')}: only a placed order can be cancelled")
    now = datetime.now(TZ)
    deadline = _cancel_deadline(order)
    if now >= deadline:
        raise HTTPException(409, f"Too late to cancel {order_id}: the order cut-off was {deadline:%a %d %b %H:%M}")
    reason = (body.reason if body else "").strip()
    order.status, order.cancelled_at, order.cancel_reason = "cancelled", now, reason or None
    S.store.save_order(order)
    S.customers.undo_order(order.customer_id, order.total)
    S.audit.append(who.actor, "order.cancelled", order.id,
                   f"{who.name} cancelled {order.id} ({eur(order.total)})" + (f": “{_clip(reason, 120)}”" if reason else "")
                   + " (simulated: the mock backend has no order endpoint)",
                   {"reason": reason, "cancellable_until": deadline.isoformat()})
    return _order_out(order)


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


@app.get("/api/catalog/departments", tags=["catalog"])
def catalog_departments(who: Principal = Depends(signed_in)):
    """The assortment in a few top-level groups, each with its departments: product and product-type counts,
    average carbon footprint and delivery reliability. Aggregates only."""
    _need_catalog()
    by_dept: dict[str, list[dict]] = defaultdict(list)
    for p in S.catalog.products:
        by_dept[department_of(p.get("category"))].append(p)

    def summary(ident: str, name: str, ps: list[dict]) -> dict:
        return {
            "id": ident, "name": name, "products": len(ps),
            "types": len({p.get("category") for p in ps}),
            "avg_co2": _mean([p["co2"] for p in ps if p.get("co2") is not None]),
            "avg_reliability": _mean([p["reliability"] for p in ps if p.get("reliability") is not None]),
        }

    groups = []
    for gid, gname, dept_ids in DEPARTMENT_GROUPS:
        depts = [summary(d, DEPARTMENT_NAMES[d], by_dept[d]) for d in dept_ids if by_dept.get(d)]
        if depts:
            groups.append({**summary(gid, gname, [p for d in dept_ids for p in by_dept.get(d, [])]), "departments": depts})
    return groups


@app.get("/api/catalog/search", tags=["catalog"])
def catalog_search(
    who: Principal = Depends(signed_in),
    q: str = Query(default="", max_length=80),
    filters: str = Query(default="", description="comma-separated, e.g. claim:VEGAN,origin:fi"),
    category: str = "",
    department: str = Query(default="", description="a group or department id from /api/catalog/departments"),
):
    """Explore the assortment using aggregate metrics only; product-level data is not exposed here.
    Browse a group or a department, search by words, or both (a search within a department)."""
    _need_catalog()
    q = q.strip()
    if department and department not in DEPARTMENT_NAMES and department not in GROUP_DEPARTMENTS:
        raise HTTPException(400, f"Unknown department {department}")
    wanted = GROUP_DEPARTMENTS.get(department, {department})
    if q and len(q) < 2:
        raise HTTPException(400, "Type at least two letters")
    if not q and not department:
        raise HTTPException(400, "Choose a department or type at least two letters")
    if q:
        matches = [p for p, _ in S.catalog.search(rules_parser.query_phrases(q), limit=800)]
    else:
        matches = S.catalog.products
    if department:
        matches = [p for p in matches if department_of(p.get("category")) in wanted]
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
        groups.append({"category": cat, "name": category_name(cat), "count": len(ps), "co2": _mean(co2), "reliability": _mean(rel)})
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


# The web UI. Mounted last so every /api route above takes precedence.
app.mount("/", StaticFiles(directory=settings.ui_dir, html=True), name="ui")
