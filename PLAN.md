# Plan & Progress Tracker

**Status:** ⬜ todo · 🟨 in progress · ✅ done · ❌ dropped

**How we use this file:** each person updates their own section in their own PRs: tick boxes, add 🟨, and link a GitHub Issue if a task grows. Read it at every sync. It is our single source of truth.

⏰ **Submission deadline:** `____` *(fill in from the official Junction schedule)*
🧊 **Feature freeze:** deadline minus 4 h. After that, only bug fixes, the demo and the pitch.

---

## Timeline

| Phase | When | Goal | Done when |
|---|---|---|---|
| **0. Kickoff & contracts** | Fri evening | Explore the mock API, agree on contracts, get the skeletons running | `contracts/` v1 merged, every module starts, and the mock API can be called |
| **1. Happy path** | Sat morning | Search → offer → order placed automatically, end to end | The agent orders a small quantity through MCP without help |
| **2. Guardrails** | Sat afternoon | Mandate, approval flow, audit log | The 1,000 L milk scenario goes to approval, is approved in the UI, and the audit timeline shows it |
| **3. Differentiators** | Sat evening / night | Offer verification, substitutes, delivery & sustainability comparison | At least one optional direction works in the demo |
| **4. Freeze & ship** | Sun morning | Polish, demo recording, pitch, submission | Submitted ✅ |

## Shared milestones

- ⬜ **M0** Repo on GitHub, all three members can push, `.env` set up locally
- ⬜ **M1** `contracts/` v1 agreed: Product, Offer, Order, Mandate, PolicyDecision, ApprovalRequest, AuditEvent
- ⬜ **M2** End-to-end happy path (agent → gateway → mock backend → order)
- ⬜ **M3** Human approval flow working in the UI
- ⬜ **M4** Demo scenario runs reliably **3× in a row**
- ⬜ **M5** Submission done (repo, video, pitch)

---

## Member A: Gateway & data (`gateway/`)

**Phase 0**
- ⬜ Read the API docs on the resource portal and write `docs/mock-api-notes.md` (endpoints, fields, units, quirks, rate limits)
- ⬜ Download the datasets into `data/` and note the key fields (units, pack sizes, lead times, labels, certificates)
- ⬜ Mock backend client that reads the API key from `.env`
- ⬜ Draft the `contracts/` models together with B and C

**Phase 1**
- ⬜ `search_products` (text + filters: category, labels, allergens)
- ⬜ `get_product`
- ⬜ `request_offer`: fixed prices, availability per delivery date, validity window
- ⬜ `place_order`: calls B's policy engine before committing
- ⬜ Expose the tools as an MCP server and through REST (FastAPI)

**Phase 3 (optional)**
- ⬜ Unit conversion (litres ↔ packs) and splitting an order across SKUs when stock is partial
- ⬜ `suggest_substitutes` with good / better / best ranking
- ⬜ Delivery reliability and sustainability labels in the offer comparison

## Member B: Trust & guardrails (`trust/`)

**Phase 0**
- ⬜ Mandate schema: limits per order and per week, allowed categories, required labels, excluded allergens, how far substitutes may differ, delivery window
- ⬜ PolicyDecision schema: `auto_approve | needs_approval | reject` plus reason codes

**Phase 1–2**
- ⬜ Policy engine: check an order against the mandate
- ⬜ Approval state machine (`pending → approved / rejected / expired`) plus REST endpoints for the UI
- ⬜ Audit log: events are only ever added and are hash-chained (actor, action, inputs, decision, rationale)
- ⬜ Offer signing (Ed25519), a verification helper, and a public-key endpoint

**Phase 3 (optional / stretch)**
- ⬜ Plain-language audit summaries ("Agent X chose Y because …")
- ⬜ A credential for each agent, with scopes and revocation
- ⬜ Stretch: mandate as a signed verifiable credential (inspired by W3C VC)
- ⬜ Stretch: spot drift in standing orders (portions creeping up, rising cost)

## Member C: Agent & experience (`agent/`, `ui/`)

**Phase 0**
- ⬜ Buyer agent skeleton: Claude tool-use loop against stub tools
- ⬜ Write the demo scenarios in `agent/scenarios/` (see below)
- ⬜ UI skeleton

**Phase 1–2**
- ⬜ Connect the agent to the gateway's MCP server
- ⬜ The agent passes a rationale with `place_order`, and it ends up in the audit log
- ⬜ Approval UI: pending list, details (offer, policy reasons), approve and reject buttons
- ⬜ Audit timeline view (buyer view and Valio Aimo view)

**Phase 4**
- ⬜ Demo script and screen recording
- ⬜ Pitch deck (draft early, refine Sunday)

## Everyone

- ⬜ Ask the mentors about trading rules (minimum order, delivery days, typical approval limits) and data governance (what may be exposed to agents)
- ⬜ Pitch story: *"Make Valio Aimo safely readable by agents before someone else's platform defines how."*
- ⬜ Final README polish, screenshots, architecture diagram
- ⬜ Fill in the submission form

---

## Demo scenarios

| # | Scenario | Shows | Priority |
|---|---|---|---|
| 1 | Hotel chain: *1,000 L lactose-free milk by Monday*. Over the auto limit → human approval | Lookup, offer → order, approval | **Must** |
| 2 | Small re-order within the mandate → placed with no human involved | The agent decides alone | **Must** |
| 3 | School kitchen: product out of stock → substitute must stay allergen-safe; a disallowed substitute is rejected | Substitution, hard rules | Should |
| 4 | Compare two offers on delivery date and sustainability labels, not just price | Optional direction | Could |

## Decisions log

| When | Decision | Why |
|---|---|---|
| Fri | Tech stack: Python/FastAPI + MCP + React *(proposed)* | Fast to build, MCP is an open standard. **Confirm at kickoff** |
| | | |

## Open questions

- [ ] What exactly does the mock API offer? Does it have an order endpoint, or do we simulate order placement ourselves?
- [ ] Is stock given per warehouse or per delivery date? Is there a delivery calendar?
- [ ] When do we get the webshop test accounts?
- [ ] What spend limits are realistic for a hotel chain or a school kitchen? *(ask the mentors)*

## Working agreement

- `main` must always run. Work on branches named `<name>/<short-topic>`.
- Keep PRs small, and have one other person take a quick look. Changes to `contracts/` need a 👍 from **both** others.
- **Never commit secrets.** `.env` is git-ignored. Share keys only in the team chat.
- Sync every ~4 h (10 min max): what's done, what's next, what's blocking. Update this file at each sync.
- If you're stuck for 30+ minutes, say so in the chat.
