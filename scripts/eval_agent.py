"""Evaluate the ordering agent's conversations: does it ask for what's missing, and understand the order?

Each case in evals/agent-dialogue/cases.json is a scripted conversation. The runner sends the buyer's first
message through the real entry point (agent.service.parse_conversation, as POST /api/chat does), and the
next scripted answer whenever the agent asks a question. Grading is programmatic on what the agent
understood: how many questions it asked, whether it ended complete, and the items, quantities, units,
dates, requirements and order controls. No prices, stock or orders are touched.

    python scripts/eval_agent.py --self-check                 # grader sanity: oracle ~100 %, null ~0 % (free)
    python scripts/eval_agent.py --cases-md                   # write cases.md for review (free)
    python scripts/eval_agent.py --parser rules --variant v1  # rule-based fallback only (free)
    python scripts/eval_agent.py --variant baseline --reps 2  # Claude (CLAUDE_MODEL from .env): paid

Results go to .claude/hillclimb/agent-dialogue/<variant>/ (results.jsonl, traces/, errors.jsonl), one row
per (case, rep) as it completes; re-running resumes and skips rows already written.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.service import claude_available, parse_conversation  # noqa: E402
from contracts.models import ParsedRequest  # noqa: E402
from gateway.config import settings  # noqa: E402

CASES = ROOT / "evals" / "agent-dialogue" / "cases.json"
FLOW = ROOT / ".claude" / "hillclimb" / "agent-dialogue"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
STATE = {
    "metrics": [
        {"id": "pass", "label": "Pass", "kind": "binary"},
        {"id": "dialogue", "label": "Dialogue", "kind": "binary"},
        {"id": "understanding", "label": "Understanding", "kind": "binary"},
    ],
    "perf_fields": [
        {"id": "latency_s", "label": "Latency", "unit": "s"},
        {"id": "turns", "label": "Agent turns"},
        {"id": "in_tokens", "label": "In tokens"},
        {"id": "out_tokens", "label": "Out tokens"},
    ],
    "prices": {"claude-haiku-5-5": {"in": 0.10, "out": 0.50}},
    "metrics_md": "pass = every check passed. dialogue = asked the expected number of questions and ended in the "
                  "expected state (complete / still asking). understanding = items, quantities, units, delivery "
                  "date, requirements and controls match.",
}


class HarnessError(RuntimeError):
    """A failed attempt that must not be scored (fallback to rules, model mismatch)."""

    def __init__(self, failure_class: str, message: str, meta: dict | None = None):
        super().__init__(message)
        self.failure_class, self.meta = failure_class, meta or {}


# ----------------------------------------------------------------------------- running a case


async def run_case(case: dict, mode: str, model: str, today: date) -> tuple[dict, list, dict]:
    """Play the scripted conversation. Returns (outcome, trace, meta)."""
    turns: list[tuple[str, str]] = []
    understood: ParsedRequest | None = None
    questions: list[str] = []
    trace = [{"role": "system", "content": f"parser mode={mode} model={model if mode != 'rules' else 'rules'} today={today}"}]
    usage = dict.fromkeys(USAGE_KEYS, 0)
    served, stop_reason = set(), None
    script = list(case["turns"])
    after_offer = list(case.get("then", []))  # messages the buyer sends once the agent has made an offer
    complete, unused = False, 0
    while script or (complete and after_offer):
        if not script:
            script, after_offer, complete = after_offer, [], False
            trace.append({"role": "system", "content": "(offer shown; the buyer keeps talking)"})
        message = script.pop(0)
        turns.append(("buyer", message))
        trace.append({"role": "user", "content": message})
        meta: dict = {}
        parsed, question, _ = await parse_conversation(
            turns, understood, today, api_key=settings.anthropic_api_key, mode=mode, model=model, meta=meta)
        if mode != "rules":
            if meta.get("fallback"):
                raise HarnessError("serving", meta["fallback"], meta)
            if not str(meta.get("model", "")).startswith(model):
                raise HarnessError("model_mismatch", f"asked for {model}, served {meta.get('model')}", meta)
            served.add(meta["model"])
            stop_reason = meta.get("stop_reason")
            for k in USAGE_KEYS:
                usage[k] += meta.get("usage", {}).get(k, 0)
        understood = parsed
        if question:
            questions.append(question)
            turns.append(("agent", question))
            trace.append({"role": "assistant", "content": question})
            continue
        complete = True
        if not after_offer:
            break
        unused += len(script)  # scripted answers the agent never asked for
        script = []
    trace.append({"role": "assistant", "content": "Understood (structured):\n"
                  + json.dumps(understood.model_dump(mode="json"), indent=2, ensure_ascii=False)})
    outcome = {"questions": questions, "complete": complete, "unused_turns": unused + len(script),
               "parsed": understood.model_dump(mode="json")}
    return outcome, trace, {"usage": usage, "model": ",".join(sorted(served)) or "rules", "stop_reason": stop_reason}


# ----------------------------------------------------------------------------- grading


def _match_item(want: dict, items: list[dict]) -> tuple[bool, str]:
    names = [w.lower() for w in want["product_any"]]
    for it in items:
        words = " ".join([it.get("product_query") or "", *(it.get("search_phrases") or [])]).lower()
        if not any(n in words for n in names):
            continue
        problems = []
        q = it.get("quantity")
        if "quantity" in want and (q is None or not math.isclose(q, want["quantity"], rel_tol=0.01)):
            problems.append(f"quantity {q} != {want['quantity']}")
        if "quantity_range" in want:
            lo, hi = want["quantity_range"]
            if q is None or not lo <= q <= hi:
                problems.append(f"quantity {q} not in [{lo}, {hi}]")
        if want.get("unit") and it.get("unit") != want["unit"]:
            problems.append(f"unit {it.get('unit')} != {want['unit']}")
        if it.get("assumption"):
            problems.append(f"still an unconfirmed assumption: {it['assumption'][:60]}")
        c = it.get("constraints") or {}
        for key, field in (("claims", "required_claims"), ("free_from", "free_from_allergens"), ("labels", "required_labels")):
            missing = set(want.get(key, [])) - set(c.get(field) or [])
            if missing:
                problems.append(f"missing {key} {sorted(missing)}")
        if want.get("origin") and c.get("origin_country") != want["origin"]:
            problems.append(f"origin {c.get('origin_country')} != {want['origin']}")
        return not problems, "; ".join(problems)
    return False, f"no item matching {names}"


def grade(case: dict, outcome: dict) -> tuple[dict, list[str]]:
    """Returns ({metric: 0/1}, failure reasons)."""
    e, p = case["expect"], outcome["parsed"]
    dialogue, understanding = [], []  # (ok, reason)

    expected_final = e.get("final", "complete")
    actual_final = "complete" if outcome["complete"] else "question"
    dialogue.append((actual_final == expected_final, f"ended with {actual_final}, expected {expected_final}"))
    if "questions" in e:
        n = len(outcome["questions"])
        dialogue.append((n == e["questions"], f"asked {n} question(s), expected {e['questions']}"))
    if e.get("question_any"):
        text = " ".join(outcome["questions"]).lower()
        dialogue.append((any(w in text for w in e["question_any"]), "question not in the buyer's language"))

    if expected_final == "complete":
        items = p.get("items") or []
        if "items" in e:
            understanding.append((len(items) == len(e["items"]), f"{len(items)} item(s), expected {len(e['items'])}"))
            for want in e["items"]:
                understanding.append(_match_item(want, items))
        if "delivery_date" in e:
            ok = p.get("delivery_date") == e["delivery_date"] and not p.get("delivery_date_assumed")
            understanding.append((ok, f"delivery {p.get('delivery_date')}{' (assumed)' if p.get('delivery_date_assumed') else ''}, expected {e['delivery_date']}"))
        understanding.append((not p.get("clarification_required"), "marked as needing clarification"))
    if "budget_eur" in e:
        understanding.append((p.get("budget_eur") == e["budget_eur"], f"budget {p.get('budget_eur')}, expected {e['budget_eur']}"))
    if "substitution_allowed" in e:
        understanding.append((p.get("substitution_allowed") == e["substitution_allowed"],
                              f"substitution_allowed {p.get('substitution_allowed')}, expected {e['substitution_allowed']}"))

    reasons = [r for ok, r in dialogue + understanding if not ok]
    scores = {"dialogue": float(all(ok for ok, _ in dialogue)), "understanding": float(all(ok for ok, _ in understanding))}
    scores["pass"] = float(scores["dialogue"] and scores["understanding"])
    return {"pass": scores["pass"], "dialogue": scores["dialogue"], "understanding": scores["understanding"]}, reasons


# ----------------------------------------------------------------------------- self-check (no model calls)


def _oracle(case: dict) -> dict:
    e = case["expect"]
    final = e.get("final", "complete")
    items = [{"product_query": w["product_any"][0], "search_phrases": [], "assumption": "",
              "quantity": w.get("quantity", sum(w.get("quantity_range", [0, 0])) / 2), "unit": w.get("unit"),
              "constraints": {"required_claims": w.get("claims", []), "free_from_allergens": w.get("free_from", []),
                              "required_labels": w.get("labels", []), "origin_country": w.get("origin")}}
             for w in e.get("items", [])]
    parsed = {"items": items, "delivery_date": e.get("delivery_date"), "delivery_date_assumed": False,
              "budget_eur": e.get("budget_eur"), "substitution_allowed": e.get("substitution_allowed", True),
              "clarification_required": final == "question"}
    words = " ".join(e.get("question_any", ["question"]))
    return {"questions": [words] * e.get("questions", 0), "complete": final == "complete", "parsed": parsed}


def self_check(cases: list[dict]) -> int:
    empty = {"items": [], "delivery_date": None, "delivery_date_assumed": True, "budget_eur": -1,
             "substitution_allowed": None, "clarification_required": True}
    nulls = {"silent": {"questions": [], "complete": True, "parsed": empty},
             "always-asks": {"questions": ["?"] * 5, "complete": False, "parsed": empty}}
    oracle = sum(grade(c, _oracle(c))[0]["pass"] for c in cases) / len(cases)
    print(f"oracle  pass rate {oracle:.0%} (expect 100%)")
    bad = [c["id"] for c in cases if not grade(c, _oracle(c))[0]["pass"]]
    if bad:
        print("  oracle fails:", bad, [grade(c, _oracle(c))[1] for c in cases if c["id"] in bad])
    worst = 0.0
    for name, outcome in nulls.items():
        rate = sum(grade(c, outcome)[0]["pass"] for c in cases) / len(cases)
        worst = max(worst, rate)
        print(f"null    pass rate {rate:.0%} ({name}, expect ~0%)")
    return 0 if oracle == 1.0 and worst == 0.0 else 1


# ----------------------------------------------------------------------------- reporting helpers


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def write_cases_md(data: dict) -> Path:
    out = CASES.with_name("cases.md")
    lines = [f"# Agent dialogue eval: {len(data['cases'])} cases", "", data["description"], "",
             "| id | tags | buyer turns | expected |", "|---|---|---|---|"]
    for c in data["cases"]:
        e = c["expect"]
        exp = [f"{e.get('final', 'complete')}, {e.get('questions', '?')} question(s)"]
        for it in e.get("items", []):
            q = it.get("quantity", "-".join(map(str, it.get("quantity_range", []))))
            extra = [*it.get("claims", []), *[f"free from {a}" for a in it.get("free_from", [])], *it.get("labels", [])]
            if it.get("origin"):
                extra.append(f"origin {it['origin']}")
            exp.append(f"{it['product_any'][0]} {q} {it.get('unit', '')}" + (f" ({', '.join(extra)})" if extra else ""))
        for key in ("delivery_date", "budget_eur", "substitution_allowed"):
            if key in e:
                exp.append(f"{key}={e[key]}")
        turns = " → ".join([*(f"“{t}”" for t in c["turns"]), *(f"(after the offer) “{t}”" for t in c.get("then", []))])
        turns = turns.replace("|", "\\|")
        lines.append(f"| `{c['id']}` | {', '.join(c['tags'])} | {turns} | {'; '.join(exp)} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ----------------------------------------------------------------------------- main


async def main_async(args, cases: list[dict], today: date) -> int:
    mode = "rules" if args.parser == "rules" else "auto"
    model = args.model or settings.claude_model
    if mode != "rules" and not claude_available(settings.anthropic_api_key, mode):
        print("Claude is not configured (ANTHROPIC_API_KEY): use --parser rules", file=sys.stderr)
        return 2
    vdir = FLOW / args.variant
    (vdir / "traces").mkdir(parents=True, exist_ok=True)
    if not (FLOW / "_state.json").exists():
        (FLOW / "_state.json").write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    if args.variant != "baseline" and not (vdir / "change.md").exists():
        (vdir / "change.md").write_text(f"{'Rule-based parser only (no Claude)' if mode == 'rules' else f'Model {model}'}\n",
                                        encoding="utf-8")
    results, errors = vdir / "results.jsonl", vdir / "errors.jsonl"
    done = set()
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done.add((row["prompt_id"], row["rep"]))
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    async def one(case: dict, rep: int) -> None:
        if (case["id"], rep) in done:
            return
        async with sem:
            t = time.perf_counter()
            try:
                outcome, trace, meta = await asyncio.wait_for(run_case(case, mode, model, today), args.timeout_s)
            except asyncio.TimeoutError:
                await _append(errors, {"prompt_id": case["id"], "rep": rep, "failure_class": "timeout",
                                       "error": f"over {args.timeout_s}s"}, lock)
                return
            except HarnessError as e:
                await _append(errors, {"prompt_id": case["id"], "rep": rep, "failure_class": e.failure_class,
                                       "error": str(e), "model": e.meta.get("model"), "usage": e.meta.get("usage")}, lock)
                return
            except Exception as e:  # noqa: BLE001 - any harness failure goes to the sidecar, never a zero grade
                await _append(errors, {"prompt_id": case["id"], "rep": rep, "failure_class": "harness",
                                       "error": f"{type(e).__name__}: {e}"}, lock)
                return
            scores, reasons = grade(case, outcome)
            trace_name = f"{case['id']}_rep{rep}.json"
            (vdir / "traces" / trace_name).write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
            await _append(results, {
                "prompt_id": case["id"], "rep": rep, "prompt": " → ".join([*case["turns"], *case.get("then", [])]),
                "tags": case["tags"],
                "status": "ok" if meta["stop_reason"] != "max_tokens" else "truncated", "stop_reason": meta["stop_reason"],
                "grade": scores, "explanation": {"pass": "; ".join(reasons) or "all checks passed"},
                "model": meta["model"], "usage": meta["usage"], "latency_s": round(time.perf_counter() - t, 2),
                "turns": len(outcome["questions"]) + (1 if outcome["complete"] else 0),
                "in_tokens": meta["usage"]["input_tokens"], "out_tokens": meta["usage"]["output_tokens"],
                "trace": f"traces/{trace_name}",
                "meta": {"questions": outcome["questions"], "unused_turns": outcome["unused_turns"]},
            }, lock)
            print(f"{'PASS' if scores['pass'] else 'FAIL'}  {case['id']} rep{rep}" + ("" if scores["pass"] else f"  - {'; '.join(reasons)}"),
                  flush=True)

    await asyncio.gather(*(one(c, r) for c in cases for r in range(args.reps)))
    return summarize(vdir, {c["id"] for c in cases})


async def _append(path: Path, row: dict, lock: asyncio.Lock) -> None:
    async with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def summarize(vdir: Path, ids: set[str]) -> int:
    rows = [json.loads(l) for l in (vdir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r["prompt_id"] in ids and r["status"] == "ok"]
    errs = [json.loads(l) for l in (vdir / "errors.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (vdir / "errors.jsonl").exists() else []
    n = len(rows)
    print(f"\n{vdir.name}: {n} scored rows, {len(errs)} failed attempts (errors.jsonl)")
    for metric in ("pass", "dialogue", "understanding"):
        k = int(sum(r["grade"][metric] for r in rows))
        lo, hi = wilson(k, n)
        print(f"  {metric:<14} {k}/{n} = {k / n:.0%}  (95% CI {lo:.0%}-{hi:.0%})" if n else f"  {metric}: no rows")
    tin = sum(r["usage"]["input_tokens"] for r in rows)
    tout = sum(r["usage"]["output_tokens"] for r in rows)
    if tin or tout:
        print(f"  tokens         {tin:,} in / {tout:,} out · median latency "
              f"{sorted(r['latency_s'] for r in rows)[n // 2]:.1f}s per conversation")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default="baseline", help="output folder: baseline, v1, v2, ...")
    ap.add_argument("--parser", choices=["claude", "rules"], default="claude")
    ap.add_argument("--model", default="", help="default: CLAUDE_MODEL from .env")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout-s", type=float, default=120.0)
    ap.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    ap.add_argument("--self-check", action="store_true", help="grade oracle and null outcomes; no model calls")
    ap.add_argument("--cases-md", action="store_true", help="write evals/agent-dialogue/cases.md")
    args = ap.parse_args()
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.cases_md:
        print("wrote", write_cases_md(data))
        return 0
    if args.self_check:
        return self_check(cases)
    return asyncio.run(main_async(args, cases, date.fromisoformat(data["today"])))


if __name__ == "__main__":
    sys.exit(main())
