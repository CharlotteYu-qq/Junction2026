"""LLM parser (optional): used when ANTHROPIC_API_KEY is set and the `anthropic` package is installed.

Claude returns JSON that matches SCHEMA (structured outputs), so the result is always machine-
readable. Any failure falls back to the rule-based parser - the demo never depends on it.
Server-side refusal fallbacks (`fallbacks="default"`) are enabled for the models that support them;
Claude Haiku 5.5 has none, so a refusal there falls back to the rule-based parser.
"""
from __future__ import annotations

import json
from datetime import date

from contracts.models import Constraints, ParsedRequest, RequestItem
from contracts.vocab import ALLERGENS, CLAIMS, LABELS

# Models that accept the server-side refusal fallback; others (e.g. claude-haiku-5-5) must not send it.
SERVER_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

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
                    "quantity_estimated": {
                        "type": "boolean",
                        "description": "True only if you estimated the amount (e.g. from people or portions) or assumed the unit",
                    },
                    "unit": {"type": "string", "enum": ["L", "KG", "PCS", "SALES_UNIT", "UNKNOWN"]},
                    "required_claims": {"type": "array", "items": {"type": "string", "enum": sorted(CLAIMS)}},
                    "free_from_allergens": {"type": "array", "items": {"type": "string", "enum": sorted(ALLERGENS)}},
                    "required_labels": {"type": "array", "items": {"type": "string", "enum": sorted(LABELS)}},
                    "origin_country": {"type": "string", "description": "ISO alpha-2 lowercase, or empty"},
                    "assumption": {"type": "string"},
                },
                "required": [
                    "text", "product_query", "search_phrases", "exclude_words", "quantity", "quantity_known", "quantity_estimated", "unit",
                    "required_claims", "free_from_allergens", "required_labels", "origin_country", "assumption",
                ],
                "additionalProperties": False,
            },
        },
        "delivery_date": {"type": "string", "description": "YYYY-MM-DD"},
        "delivery_date_stated": {"type": "boolean"},
        "budget_eur": {"type": ["number", "null"], "description": "Maximum order total in EUR excluding VAT, or null if no budget is stated"},
        "substitution_allowed": {"type": "boolean"},
        "clarification_required": {"type": "boolean"},
        "notes": {"type": "string"},
        "follow_up_question": {
            "type": "string",
            "description": "One short question to the buyer for everything that is still missing, or empty",
        },
    },
    "required": ["items", "delivery_date", "delivery_date_stated", "budget_eur", "substitution_allowed",
                 "clarification_required", "notes", "follow_up_question"],
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
  professional amount, set quantity_known and quantity_estimated true and explain the estimate in
  assumption. If there is no amount at all, set quantity_known false, quantity 0 and unit UNKNOWN.
- assumption: empty unless quantity_estimated is true. A stated amount needs no explanation.
- required_claims, free_from_allergens, required_labels: only what the request states. Nut-free means
  free from AN and AP. origin_country "fi" for Finnish/domestic, otherwise "".
- delivery_date: a weekday name means the next such day after today. "Tomorrow" is today + 1.
  If no date is stated, use the next weekday (Monday-Friday) and set delivery_date_stated false.
- Budgets and substitution instructions are order controls, never products. budget_eur is a
  positive maximum total in EUR excluding VAT; it is independent of the agent's approval limit.
  Use null if no budget is stated. Never convert another currency without asking.
- substitution_allowed: false for "no substitutions", "do not substitute" or "do not replace",
  including "do not substitute with regular milk". This conservatively disallows substitutes
  outside the stated requirements; other catalog options that fully match remain allowed.
- clarification_required: true for conflicting/invalid budgets, non-EUR budgets, unknown currency,
  conflicting substitution instructions, unsupported restrictions or contradictory requirements.
  Explain what needs clarification in notes. Do not silently drop these conditions.
- Ignore sentences that are not about products (e.g. "find the best option").
- The input may be a conversation between the buyer and you, the ordering agent. Combine everything
  the buyer said: later messages complete or correct earlier ones ("200 litres" answers "how much
  milk?"). If the buyer confirmed an amount you estimated, it is no longer estimated.
- follow_up_question: if any item has no quantity or unit, an amount is only estimated, no delivery
  date was stated, or something needs clarification, write one short, friendly question in the
  buyer's language that asks for every missing detail at once, including the delivery day when none
  was stated (propose your estimate if you made one; don't mention defaults you assumed). Otherwise
  an empty string."""


async def parse(text: str, today: date, model: str) -> ParsedRequest:
    """One request in plain words -> structured order lines."""
    return (await parse_dialogue([("buyer", text)], today, model))[0]


async def parse_dialogue(turns: list[tuple[str, str]], today: date, model: str) -> tuple[ParsedRequest, str]:
    """A conversation [("buyer" | "agent", text), ...] -> (order lines so far, suggested follow-up question)."""
    import anthropic  # optional dependency

    buyer_text = "\n".join(text for role, text in turns if role == "buyer")
    if len(turns) == 1:
        content = buyer_text
    else:
        content = "Conversation so far:\n" + "\n".join(
            f"{'Buyer' if role == 'buyer' else 'Agent'}: {text}" for role, text in turns)

    client = anthropic.AsyncAnthropic()
    fallback = (
        {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        if model in SERVER_FALLBACK_MODELS else {}
    )
    response = await client.beta.messages.create(
        model=model,
        max_tokens=16000,
        **fallback,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM.format(today=today.isoformat(), weekday=today.strftime("%A")),
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the model's answer was cut off")
    raw = next(b.text for b in response.content if b.type == "text")
    data = json.loads(raw)
    parsed = to_parsed(data, today)
    # The deterministic guard independently preserves explicit controls even if the
    # language model overlooks them. More restrictive results always take precedence.
    from agent.rules_parser import extract_controls, extract_date, normalise_numbers

    _, controls_text = extract_date(normalise_numbers(buyer_text), today)
    _, budget, substitutions, clarification, notes = extract_controls(controls_text)
    if budget is not None:
        if parsed.budget_eur is not None and parsed.budget_eur != budget:
            parsed.clarification_required = True
            parsed.warnings.append("The parsed budget differs from the explicit EUR cap; please confirm the amount")
        parsed.budget_eur = budget
    parsed.substitution_allowed = parsed.substitution_allowed and substitutions
    parsed.clarification_required = parsed.clarification_required or clarification
    parsed.warnings.extend(notes)
    return parsed, str(data.get("follow_up_question") or "")


def to_parsed(data: dict, today: date) -> ParsedRequest:
    warnings: list[str] = []
    items = []
    for it in data.get("items", []):
        known = it.get("quantity_known") and it.get("unit") != "UNKNOWN" and (it.get("quantity") or 0) > 0
        # The policy asks a person to confirm any item with an assumption, so keep it only for real
        # estimates: models also write explanations ("stated directly as 1,000 L") into that field.
        estimated = bool(it.get("quantity_estimated")) or not known
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
                assumption=it.get("assumption", "") if estimated else "",
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
    return ParsedRequest(
        items=items, delivery_date=delivery, delivery_date_assumed=not stated,
        budget_eur=data.get("budget_eur"), substitution_allowed=data.get("substitution_allowed", True),
        clarification_required=data.get("clarification_required", False), parser="claude", warnings=warnings,
    )
