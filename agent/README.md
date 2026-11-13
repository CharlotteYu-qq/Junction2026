# agent/: demo buyer purchasing agent

**Owner:** Member C · Tasks: see [PLAN.md](../PLAN.md#member-c-agent--experience-agent-ui)

## Responsibility

- An external purchasing agent (for example, a hotel chain's) that only talks to the gateway's MCP/REST interface, just as a third-party agent would.
- Built with the Anthropic Python SDK using tool use (model `claude-opus-5-5`).
- Gives a short rationale with every decision, which is stored in the audit log.
- `scenarios/` holds scripted demo requests (see the demo scenarios in [PLAN.md](../PLAN.md#demo-scenarios)).

## Files (MVP)

| File | What it does |
|---|---|
| `rules_parser.py` | Always-on parser: quantities and units, delivery dates (weekday names, "tomorrow", 20.11.), diet/allergen/label words, basic Finnish |
| `claude_parser.py` | Optional Claude parser (structured outputs); estimates amounts from servings |
| `service.py` | Picks Claude when `ANTHROPIC_API_KEY` is set, otherwise or on failure the rules |

Still to do: an external buyer agent that calls the gateway through MCP, and scripted scenarios in `scenarios/`.

## Run

See [Run the MVP locally](../README.md#run-the-mvp-locally) in the main README.
