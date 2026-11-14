# trust/: mandates, policy, approvals, signing & audit

**Owner:** Member B · Tasks: see [PLAN.md](../PLAN.md#member-b-trust--guardrails-trust)

## Responsibility

- **Mandates:** what each agent is allowed to do on behalf of its buyer organisation.
- **Policy engine:** `evaluate(order_intent, mandate) -> PolicyDecision` (`auto_approve | needs_approval | reject` + reasons).
- **Approvals:** state machine plus REST endpoints that the UI uses to list, approve and reject.
- **Signing:** Ed25519 signatures on offers, a verification helper, and a public key served at `/.well-known/`.
- **Audit log:** events are only ever added and are hash-chained, with readable summaries for buyers and for Valio Aimo.

Kept as a library that `gateway/` calls, so it can be unit-tested without the backend.

## Files (MVP)

| File | What it does |
|---|---|
| `auth.py` | Buyer sign-in: signed session cookies, one account per customer |
| `customers.py` | Whitelist & trust tiers, Y-tunnus checksum, demo customers |
| `policy.py` | `evaluate()` → auto_approve / needs_approval / reject, with every check explained; `constraint_check()` for allergens and preferences |
| `signing.py` | Ed25519 key, offer hashing and verification |
| `audit.py` | Append-only, hash-chained audit log + `verify()` |
| `storage.py` | Tiny JSON persistence |

## Run

See [Run the MVP locally](../README.md#run-the-mvp-locally) in the main README.
