# contracts/: shared interface

**Owner:** everyone (Member A coordinates). **Changes need a 👍 from both other members.**

This folder holds the data models and the interface spec that every module shares. If it's written here, it's the agreement. Each module builds against it independently, using stubs until the real thing exists.

Plan: write these as Pydantic models (`contracts/models.py`). FastAPI then generates the OpenAPI spec, and the MCP tools reuse the same schemas.

## Draft v0 (discuss at kickoff)

**Product**: `sku`, `name`, `category`, `unit`, `pack_size`, `labels[]` (e.g. organic, Finnish origin), `allergens[]`, `lead_time_days`

**Offer**: the response to `request_offer`
- `offer_id`, `buyer_id`, `agent_id`, `created_at`, `valid_until`, `requested_delivery_date`
- `lines[]`: `sku`, `qty`, `unit`, `unit_price`, `currency`, `available_qty`, `earliest_delivery_date`
- `total`, `source` (`system`, `as_of`), `signature` (Ed25519 over the canonical JSON)

**OrderIntent**: `offer_id`, `lines[]` (subset of the offer), `rationale` (the agent's own explanation)

**Order**: `order_id`, `offer_id`, `status` (`placed | pending_approval | rejected | cancelled`), `decision`

**Mandate**: issued by the buyer organisation to one agent
- `mandate_id`, `buyer_org`, `agent_id`, `issued_by`, `valid_until`
- `limits`: `max_order_total`, `max_weekly_spend`, `currency`
- `allowed_categories[]`, `required_labels[]`, `excluded_allergens[]`
- `substitution`: `allowed`, `max_price_increase_pct`, `must_keep_labels`
- `delivery`: `max_days_late`

**PolicyDecision**: `outcome` (`auto_approve | needs_approval | reject`), `reasons[]` (`code`, `message`), `mandate_id`

**ApprovalRequest**: `approval_id`, `order_id`, `reasons[]`, `status` (`pending | approved | rejected | expired`), `decided_by`, `decided_at`, `comment`

**AuditEvent**: `seq`, `ts`, `actor` (`agent | human | system`), `actor_id`, `action`, `subject_id`, `summary` (readable sentence), `rationale`, `payload_hash`, `prev_hash`, `hash`
