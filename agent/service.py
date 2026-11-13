"""Pick the parser: Claude when configured, the rule-based parser otherwise (or as a fallback)."""
from __future__ import annotations

import importlib.util
import time
from datetime import date

from agent import rules_parser
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
