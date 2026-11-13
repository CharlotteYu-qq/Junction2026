# gateway/: agent interface, offers & orders

**Owner:** Member A · Tasks: see [PLAN.md](../PLAN.md#member-a-gateway--data-gateway)

## Responsibility

- The client for the Valio Aimo mock backend. The API key is read from `.env` and is never passed on to agents.
- Turns raw backend data into the curated models in [`contracts/`](../contracts/).
- The agent tools `search_products`, `get_product`, `request_offer`, `place_order` and `get_order_status`, offered as:
  - an **MCP server** for LLM agents
  - a **REST API** (FastAPI, OpenAPI docs at `/docs`)
- Calls `trust/` to sign offers, check orders against policy, and write audit events.

## Files (MVP)

| File | What it does |
|---|---|
| `app.py` | FastAPI app: all REST endpoints + serves `ui/` |
| `catalog.py` | Builds the slim catalog index from the raw 87 MB file; relevance search (en/fi/sv) |
| `valio_client.py` | Price & availability client: batching, 5-min cache, back-off on 429 |
| `planner.py` | Parsed request → shortlist → API calls → quantities in sales units → ranking → signed offer |
| `store.py` | Offers & orders (JSON in `data/runtime/`) |
| `config.py` | Settings from `.env` |

## Run

See [Run the MVP locally](../README.md#run-the-mvp-locally) in the main README.
