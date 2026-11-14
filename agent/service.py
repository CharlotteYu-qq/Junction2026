"""Pick the parser: Claude when configured, the rule-based parser otherwise (or as a fallback)."""
from __future__ import annotations

import importlib.util
import time
from datetime import date

from agent import dialogue, rules_parser
from contracts.models import ParsedRequest


def claude_available(api_key: str, mode: str) -> bool:
    if mode == "rules":
        return False
    return bool(api_key) and importlib.util.find_spec("anthropic") is not None


async def parse_request(text: str, today: date, *, api_key: str, mode: str, model: str) -> tuple[ParsedRequest, int]:
    t = time.perf_counter()
    if claude_available(api_key, mode):
        from agent import claude_parser

        try:
            parsed = await claude_parser.parse(text, today, model)
            return parsed, int((time.perf_counter() - t) * 1000)
        except Exception as e:  # never let the LLM path break ordering
            parsed = rules_parser.parse(text, today)
            parsed.warnings.insert(0, f"Claude parser unavailable ({type(e).__name__}: {str(e)[:120]}): used the rule-based parser")
            return parsed, int((time.perf_counter() - t) * 1000)
    return rules_parser.parse(text, today), int((time.perf_counter() - t) * 1000)


async def parse_conversation(
    turns: list[tuple[str, str]], previous: ParsedRequest | None, today: date, *, api_key: str, mode: str, model: str,
    meta: dict | None = None,
) -> tuple[ParsedRequest, str, int]:
    """Understand a conversation with the buyer. Returns (what was understood, follow-up question or "", ms).

    Claude reads the whole conversation. The rule-based fallback merges the latest answer into what the
    agent understood after the previous turn. Either way, dialogue.py decides whether a question is needed.
    `meta`, if given, receives the served model, usage and stop reason, and `fallback` when Claude failed.
    """
    t = time.perf_counter()
    buyer = [text for role, text in turns if role == "buyer"]
    parsed, suggested, note = None, "", ""
    if claude_available(api_key, mode):
        from agent import claude_parser

        try:
            parsed, suggested = await claude_parser.parse_dialogue(turns, today, model, meta, current=previous)
        except Exception as e:  # never let the LLM path break ordering
            note = f"Claude parser unavailable ({type(e).__name__}: {str(e)[:120]}): used the rule-based parser"
            if meta is not None:
                meta["fallback"] = note
    if parsed is None:
        if previous is not None and len(buyer) > 1:
            parsed = dialogue.merge_answer(previous, buyer[-1], today)
        else:
            parsed = rules_parser.parse("\n".join(buyer), today)
        if note:
            parsed.warnings.insert(0, note)
    return parsed, dialogue.follow_up_question(parsed, suggested), int((time.perf_counter() - t) * 1000)
