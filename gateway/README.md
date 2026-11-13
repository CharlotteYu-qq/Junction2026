# gateway/: agent interface, offers & orders

**Owner:** Member A · Tasks: see [PLAN.md](../PLAN.md#member-a-gateway--data-gateway)

## Responsibility

- The client for the Valio Aimo mock backend. The API key is read from `.env` and is never passed on to agents.
- Turns raw backend data into the curated models in [`contracts/`](../contracts/).
- The agent tools `search_products`, `get_product`, `request_offer`, `place_order` and `get_order_status`, offered as:
  - an **MCP server** for LLM agents
  - a **REST API** (FastAPI, OpenAPI docs at `/docs`)
- Calls `trust/` to sign offers, check orders against policy, and write audit events.

## Run

*TBD when the code lands.*
