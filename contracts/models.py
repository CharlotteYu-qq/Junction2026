"""Shared data models (v0, MVP).

Every module builds against these. Change them only via a PR that both other
members have approved (see PLAN.md).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- request

RequestUnit = Literal["L", "KG", "PCS", "SALES_UNIT"]


class Constraints(BaseModel):
    """What the customer asked for beyond the product itself."""

    required_claims: list[str] = Field(default_factory=list)  # e.g. FREE_FROM_LACTOSE, VEGAN
    free_from_allergens: list[str] = Field(default_factory=list)  # GS1 allergen codes, e.g. AN, AP
    required_labels: list[str] = Field(default_factory=list)  # e.g. EU_ORGANIC_FARMING
    origin_country: Optional[str] = None  # ISO alpha-2, e.g. "fi"


class RequestItem(BaseModel):
    text: str  # the fragment of the request this item came from
    product_query: str  # short noun phrase, e.g. "milk"
    search_phrases: list[str] = Field(default_factory=list)  # alternatives; words inside one phrase are ANDed
    exclude_words: list[str] = Field(default_factory=list)  # words that point to a different product
    quantity: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    unit: Optional[RequestUnit] = None
    constraints: Constraints = Field(default_factory=Constraints)
    assumption: str = ""  # anything the parser had to assume


class ParsedRequest(BaseModel):
    items: list[RequestItem]
    delivery_date: date
    delivery_date_assumed: bool = False
    budget_eur: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False,
        description="Buyer's maximum order total in EUR, excluding VAT; separate from the agent's approval limit.",
    )
    substitution_allowed: bool = True  # false: no substitutes outside the stated requirements
    clarification_required: bool = False  # unsupported/conflicting controls must be resolved before ordering
    parser: Literal["rules", "claude"] = "rules"
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- customers

TrustStatus = Literal["trusted", "unverified", "blocked"]


class Customer(BaseModel):
    id: str
    name: str
    segment: str  # hotel, school, restaurant, ...
    business_id: str  # Finnish Y-tunnus, e.g. 1618033-3
    business_id_valid: bool
    status: TrustStatus
    whitelisted_at: Optional[datetime] = None
    past_orders: int = 0
    avg_order_value: Optional[float] = None  # EUR excl. VAT
    auto_approve_limit: float = 0.0  # EUR excl. VAT; 0 = every order needs a human
    strict_allergens: list[str] = Field(default_factory=list)  # never acceptable, e.g. ["AN", "AP"]
    notes: str = ""


# --------------------------------------------------------------------------- offers

AvailabilityStatus = Literal["ok", "partial", "late", "cutoff_passed", "unavailable", "unknown"]


class PriceInfo(BaseModel):
    unit_price: Optional[float] = None  # per sales unit, excl. VAT
    unit_price_with_tax: Optional[float] = None
    comparison_price: Optional[float] = None  # per comparison_unit, excl. VAT
    comparison_unit: Optional[str] = None  # "L" or "KG"
    tax_rate: Optional[float] = None
    list_price: Optional[float] = None
    net_price_found: bool = False
    valid_to: Optional[str] = None
    error: bool = False


class Availability(BaseModel):
    status: AvailabilityStatus = "unknown"
    available_amount: Optional[float] = None  # in sales units
    available_date: Optional[date] = None
    closing_time: Optional[datetime] = None
    logistic_model: Optional[str] = None  # "Stock" or "Terminal"
    sales_block: bool = False
    note: str = ""


class Candidate(BaseModel):
    product_id: str
    name: str
    brand: Optional[str] = None
    vendor: Optional[str] = None
    category: Optional[str] = None
    sales_unit: str
    content_per_sales_unit: Optional[float] = None  # in content_unit
    content_unit: Optional[str] = None  # "L" or "KG"
    quantity: Optional[float] = None  # sales units to order
    line_total: Optional[float] = None  # EUR excl. VAT
    price: PriceInfo = Field(default_factory=PriceInfo)
    availability: Availability = Field(default_factory=Availability)
    delivery_reliability: Optional[float] = None  # 0..1
    co2_per_kg: Optional[float] = None  # kg CO2e per kg, product-group average
    claims: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    allergens_contains: list[str] = Field(default_factory=list)
    allergens_may_contain: list[str] = Field(default_factory=list)
    free_from: list[str] = Field(default_factory=list)
    country: Optional[str] = None
    match_score: float = 0.0
    meets_constraints: bool = True
    constraint_notes: list[str] = Field(default_factory=list)
    score: float = 0.0
    rank_reason: str = ""


class OfferItem(BaseModel):
    request: RequestItem
    candidates: list[Candidate]
    recommended_product_id: Optional[str] = None
    note: str = ""


class Operation(BaseModel):
    """One executable step the agent ran, shown to people as the 'how' of the result."""

    step: int
    name: str  # parse_request, search_catalog, get_price, check_availability, select_options, ...
    detail: str
    status: Literal["done", "warning", "failed", "skipped"] = "done"
    duration_ms: Optional[int] = None


class Offer(BaseModel):
    id: str
    customer_id: str
    created_at: datetime
    valid_until: datetime
    request_text: str
    parsed: ParsedRequest
    items: list[OfferItem]
    operations: list[Operation]
    source: dict = Field(default_factory=dict)  # where the price/stock claims came from, and when
    content_hash: str = ""
    signature: str = ""  # Ed25519 over content_hash, base64


# --------------------------------------------------------------------------- decisions & orders

CheckSeverity = Literal["info", "review", "block"]


class Check(BaseModel):
    code: str
    passed: bool
    severity: CheckSeverity  # what a failure means: info (note only), review (human), block (reject)
    message: str


Outcome = Literal["auto_approve", "needs_approval", "reject"]


class Decision(BaseModel):
    outcome: Outcome
    summary: str
    checks: list[Check]
    total: float  # EUR excl. VAT
    currency: str = "EUR"


class OrderLine(BaseModel):
    item_index: int
    product_id: str
    name: str
    quantity: float
    sales_unit: str
    unit_price: float
    line_total: float
    delivery_date: date
    is_recommended: bool = True


OrderStatus = Literal["placed", "pending_approval", "rejected"]


class Order(BaseModel):
    id: str
    offer_id: str
    customer_id: str
    customer_name: str
    created_at: datetime
    status: OrderStatus
    lines: list[OrderLine]
    total: float
    decision: Decision
    rationale: str = ""
    request_text: str = ""
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_comment: Optional[str] = None


# --------------------------------------------------------------------------- audit


class AuditEvent(BaseModel):
    seq: int
    ts: datetime
    actor: str  # "customer:C-1001", "agent", "valio-api", "policy", "human:Anna"
    action: str
    subject: str  # offer / order / customer id
    summary: str  # one readable sentence
    data: dict = Field(default_factory=dict)
    prev_hash: str
    hash: str
