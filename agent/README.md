# agent/: demo buyer purchasing agent

**Owner:** Member C · Tasks: see [PLAN.md](../PLAN.md#member-c-agent--experience-agent-ui)

## Responsibility

- An external purchasing agent (for example, a hotel chain's) that only talks to the gateway's MCP/REST interface, just as a third-party agent would.
- Built with the Anthropic Python SDK using tool use (model `claude-opus-5-5`).
- Gives a short rationale with every decision, which is stored in the audit log.
- `scenarios/` holds scripted demo requests (see the demo scenarios in [PLAN.md](../PLAN.md#demo-scenarios)).

## Run

*TBD when the code lands.*
