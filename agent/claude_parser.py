"""LLM parser (optional): used when ANTHROPIC_API_KEY is set and the `anthropic` package is installed.

Claude returns JSON that matches SCHEMA (structured outputs), so the result is always machine-
readable. Any failure falls back to the rule-based parser - the demo never depends on it.
Server-side refusal fallbacks (`fallbacks="default"`) are enabled for the models that support them;
Claude Haiku 5.5 has none, so a refusal there falls back to the rule-based parser.
"""
from __future__ import annotations

import json
import math
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
- Only include products the buyer actually named. Never guess a product: if an amount comes without
  one ("200 litres for Friday"), return no item for it and ask which product they mean.
- The input may be a conversation between the buyer and you, the ordering agent. Combine everything
  the buyer said: later messages complete or correct earlier ones ("200 litres" answers "how much
  milk?"). If the buyer confirmed an amount you estimated, it is no longer estimated.
- If a "Current order" is given, the latest buyer message changes it: add a product ("also 10 kg
  butter"), remove one ("remove the cream", "no more butter"), change an amount ("make the milk 300
  litres"; "10 kg more butter" adds to the current amount), or change the day, budget or
  substitution rule. Return the whole order after the change, keeping everything the buyer did not
  change exactly as it is in the current order (products, amounts, requirements, day). A stated
  delivery day in the current order counts as stated.
- "The usual", "the usual milk" or "same as last time": the buyer's purchase history is applied
  after you, so return the product they named with quantity_known false (no item if they named
  none), and do not ask about it. Once the current order has an amount for it, keep that amount.
- follow_up_question: if any item has no quantity or unit, an amount is only estimated, no delivery
  date was stated, or something needs clarification, write one short, friendly question in the
  buyer's language that asks for every missing detail at once, including the delivery day when none
  was stated (propose your estimate if you made one; don't mention defaults you assumed). Otherwise
  an empty string."""


async def parse(text: str, today: date, model: str) -> ParsedRequest:
    """One request in plain words -> structured order lines."""
    return (await parse_dialogue([("buyer", text)], today, model))[0]


async def parse_dialogue(
    turns: list[tuple[str, str]], today: date, model: str, meta: dict | None = None,
    current: ParsedRequest | None = None,
) -> tuple[ParsedRequest, str]:
    """A conversation [("buyer" | "agent", text), ...] -> (order lines so far, suggested follow-up question).

    `current` is what the agent understood after the previous turn (the order being discussed), so a
    message like "add 10 kg butter" changes that order instead of starting a new one.
    If `meta` is given, the served model, token usage and stop reason are recorded in it (for evals).
    """
    import anthropic  # optional dependency

    buyer_text = "\n".join(text for role, text in turns if role == "buyer")
    if len(turns) == 1:
        content = buyer_text
    else:
        content = "Conversation so far:\n" + "\n".join(
            f"{'Buyer' if role == 'buyer' else 'Agent'}: {text}" for role, text in turns)
        if current is not None and current.items:
            content = f"Current order:\n{json.dumps(current_order(current), ensure_ascii=False)}\n\n{content}"

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
    if meta is not None:
        u = response.usage
        meta.update(model=response.model, stop_reason=response.stop_reason, usage={
            k: getattr(u, k, 0) or 0
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
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
    check_amounts(parsed, buyer_text, today, current)
    return parsed, str(data.get("follow_up_question") or "")


def current_order(parsed: ParsedRequest) -> dict:
    """The order being discussed, compactly, for the model to change."""
    return {
        "items": [{
            "product_query": it.product_query, "quantity": it.quantity, "unit": it.unit or "UNKNOWN",
            "required_claims": it.constraints.required_claims, "free_from_allergens": it.constraints.free_from_allergens,
            "required_labels": it.constraints.required_labels, "origin_country": it.constraints.origin_country or "",
            **({"assumption": it.assumption} if it.assumption else {}),
        } for it in parsed.items],
        "delivery_date": parsed.delivery_date.isoformat(), "delivery_date_stated": not parsed.delivery_date_assumed,
        "budget_eur": parsed.budget_eur, "substitution_allowed": parsed.substitution_allowed,
    }


def check_amounts(parsed: ParsedRequest, buyer_text: str, today: date, current: ParsedRequest | None = None) -> None:
    """Deterministic guard on amounts. An amount written with a unit ("500 g") is unambiguous, so when the
    model reads it differently (500 kg) it is corrected if only one item uses that unit, and otherwise the
    buyer has to confirm it (the assumption makes the agent ask). Amounts already in the current order,
    and a stated amount added to or taken from one ("10 kg more butter"), are as written too."""
    from agent.rules_parser import stated_amounts

    amounts = stated_amounts(buyer_text, today)
    known = [(it.quantity, it.unit) for it in (current.items if current else []) if it.quantity is not None]
    for it in parsed.items:
        if it.quantity is None or it.unit is None or it.assumption:
            continue
        before = [q for q, u in known if u == it.unit]
        stated = [q for q, u in amounts if u == it.unit]
        if any(math.isclose(it.quantity, b + s * sign, rel_tol=0.01) for b in before for s in [0.0, *stated] for sign in (1, -1)):
            continue
        written = stated
        if not written or any(math.isclose(q, it.quantity, rel_tol=0.01) for q in written):
            continue
        if len(written) == 1 and sum(1 for x in parsed.items if x.unit == it.unit) == 1:
            parsed.warnings.append(f"Read {it.product_query} as {written[0]:g} {it.unit}, as written (the model said {it.quantity:g})")
            it.quantity = written[0]
        else:
            it.assumption = f"please confirm the amount ({it.quantity:g} {it.unit})"


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
