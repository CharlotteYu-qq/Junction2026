"""LLM parser (optional): used when ANTHROPIC_API_KEY is set and the `anthropic` package is installed.

Claude returns JSON that matches SCHEMA (structured outputs), so the result is always machine-
readable. Any failure falls back to the rule-based parser - the demo never depends on it.
Server-side refusal fallbacks are enabled (`fallbacks="default"`), as recommended for this model.
"""
from __future__ import annotations

import json
from datetime import date

from contracts.models import Constraints, ParsedRequest, RequestItem
from contracts.vocab import ALLERGENS, CLAIMS, LABELS

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The part of the request this item comes from"},
                    "product_query": {"type": "string", "description": "Short English noun phrase, as in a product name"},
                    "search_phrases": {"type": "array", "items": {"type": "string"}},
                    "exclude_words": {"type": "array", "items": {"type": "string"}},
                    "quantity": {"type": "number"},
                    "quantity_known": {"type": "boolean"},
                    "unit": {"type": "string", "enum": ["L", "KG", "PCS", "SALES_UNIT", "UNKNOWN"]},
                    "required_claims": {"type": "array", "items": {"type": "string", "enum": sorted(CLAIMS)}},
                    "free_from_allergens": {"type": "array", "items": {"type": "string", "enum": sorted(ALLERGENS)}},
                    "required_labels": {"type": "array", "items": {"type": "string", "enum": sorted(LABELS)}},
                    "origin_country": {"type": "string", "description": "ISO alpha-2 lowercase, or empty"},
                    "assumption": {"type": "string"},
                },
                "required": [
                    "text", "product_query", "search_phrases", "exclude_words", "quantity", "quantity_known", "unit",
                    "required_claims", "free_from_allergens", "required_labels", "origin_country", "assumption",
                ],
                "additionalProperties": False,
            },
        },
        "delivery_date": {"type": "string", "description": "YYYY-MM-DD"},
        "delivery_date_stated": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["items", "delivery_date", "delivery_date_stated", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You convert a professional kitchen's purchasing request into structured order lines for
Valio Aimo, a Finnish food-service wholesaler. Product names in the catalog are in English, Finnish
and Swedish. Today is {today} ({weekday}), Helsinki time.

Rules:
- One item per distinct product. product_query: short English noun phrase as it would appear in a
  product name ("milk", "whipping cream", "butter"), without diet words.
- search_phrases: 1-3 alternatives, English first; you may add the Finnish product word ("maito").
  All words of one phrase must appear in the name, so keep phrases short.
- exclude_words: words in a product name that would mean a different product (for milk: chocolate,
  coffee, powder, portion).
- quantity/unit: L for volume, KG for weight, PCS for pieces, SALES_UNIT for cases/packs/bottles.
  Convert dl, cl, ml, g and tonnes. If the amount is given as people or portions, estimate a sensible
  professional amount, set quantity_known true and explain it in assumption. If there is no amount
  at all, set quantity_known false, quantity 0 and unit UNKNOWN.
- required_claims, free_from_allergens, required_labels: only what the request states. Nut-free means
  free from AN and AP. origin_country "fi" for Finnish/domestic, otherwise "".
- delivery_date: a weekday name means the next such day after today. "Tomorrow" is today + 1.
  If no date is stated, use the next weekday (Monday-Friday) and set delivery_date_stated false.
- Ignore sentences that are not about products (e.g. "find the best option")."""


async def parse(text: str, today: date, model: str) -> ParsedRequest:
    import anthropic  # optional dependency

    client = anthropic.AsyncAnthropic()
    response = await client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM.format(today=today.isoformat(), weekday=today.strftime("%A")),
        messages=[{"role": "user", "content": text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the model's answer was cut off")
    raw = next(b.text for b in response.content if b.type == "text")
    return to_parsed(json.loads(raw), today)


def to_parsed(data: dict, today: date) -> ParsedRequest:
    warnings: list[str] = []
    items = []
    for it in data.get("items", []):
        known = it.get("quantity_known") and it.get("unit") != "UNKNOWN" and (it.get("quantity") or 0) > 0
        items.append(
            RequestItem(
                text=it.get("text", ""),
                product_query=it.get("product_query", "").strip(),
                search_phrases=[p for p in it.get("search_phrases", []) if p.strip()] or [it.get("product_query", "")],
                exclude_words=it.get("exclude_words", []),
                quantity=float(it["quantity"]) if known else None,
                unit=it.get("unit") if known else None,
                constraints=Constraints(
                    required_claims=[c for c in it.get("required_claims", []) if c in CLAIMS],
                    free_from_allergens=[a for a in it.get("free_from_allergens", []) if a in ALLERGENS],
                    required_labels=[lbl for lbl in it.get("required_labels", []) if lbl in LABELS],
                    origin_country=(it.get("origin_country") or "").lower() or None,
                ),
                assumption=it.get("assumption", ""),
            )
        )
        if not known:
            warnings.append(f"No quantity for “{items[-1].product_query}”")
    try:
        delivery = date.fromisoformat(data.get("delivery_date", ""))
    except ValueError:
        delivery = None
    if delivery is None or delivery < today:
        from agent.rules_parser import next_delivery_day

        warnings.append("The delivery date could not be read: assumed the next weekday")
        delivery = next_delivery_day(today)
        stated = False
    else:
        stated = bool(data.get("delivery_date_stated"))
        if not stated:
            warnings.append("No delivery date given: assumed the next delivery day")
    if data.get("notes"):
        warnings.append(f"Claude: {data['notes']}")
    return ParsedRequest(items=items, delivery_date=delivery, delivery_date_assumed=not stated, parser="claude", warnings=warnings)
