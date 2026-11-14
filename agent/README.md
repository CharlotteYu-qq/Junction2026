# agent/: the ordering agent

**Owner:** Member C · Tasks: see [PLAN.md](../PLAN.md#member-c-agent--experience-agent-ui)

## Responsibility

- The ordering agent that Valio Aimo runs inside the gateway for its customers. A buyer talks to it in
  plain words (English or Finnish) in the buyer portal.
- It works as a conversation: when a quantity, unit or delivery day is missing, an amount is only an
  estimate ("for 120 guests"), or a condition is unclear, it asks before it checks prices and stock.
  "I need milk" → "How much milk do you need, and for which day?" → "200 litres, Friday" → offer.
- The agent only proposes. Offers are signed and every order is checked by the policy engine in
  [`trust/`](../trust/), so the agent can never approve anything itself.

## Files

| File | What it does |
|---|---|
| `dialogue.py` | Decides whether a follow-up question is needed (deterministic), phrases it from a template when Claude is off, and merges a buyer's answer into what was understood |
| `claude_parser.py` | Claude reads the whole conversation (structured outputs), returns the order lines and a suggested question in the buyer's language |
| `rules_parser.py` | Always-on parser: quantities and units, delivery dates (weekday names, "tomorrow", 20.11.), diet/allergen/label words, budgets, basic Finnish |
| `service.py` | Picks Claude when `ANTHROPIC_API_KEY` is set, otherwise (or on failure) the rules |

The gateway exposes the conversation as `POST /api/chat` (see [gateway/app.py](../gateway/app.py)).

Still to do: let the agent choose among the catalog candidates, handle exceptions (out of stock, late
delivery) by proposing alternatives, and re-order a customer's usual products.

## Run

See [Run the MVP locally](../README.md#run-the-mvp-locally) in the main README.
