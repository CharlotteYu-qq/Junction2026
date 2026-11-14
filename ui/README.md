# ui/: approval & audit dashboard

**Owner:** Member C · Tasks: see [PLAN.md](../PLAN.md#member-c-agent--experience-agent-ui)

## Responsibility

- **Approver view (buyer side):** pending approvals, each showing the offer, the reasons it needs approval and the agent's rationale, with approve and reject buttons.
- **Audit timeline:** a readable record of what the agent did and why. There is a buyer view (own orders) and a Valio Aimo view (all agents).
- Uses the gateway and trust REST endpoints only.

## Files (MVP)

Plain HTML/CSS/JS modules, with no build step. FastAPI serves them at `/`.

| File | What it does |
|---|---|
| `index.html` | The pages Home (introduction, with sign-in when signed out), Order, My orders and Explore, and the approval dialog for orders outside the agent's authority. The page is in the address (`#order`, ...), so a refresh stays on it; no hash means Home |
| `app.js` | Page logic, API calls, the conversation with the agent and sign-in behaviour |
| `charts.js` | Ranked bar charts (selected option emphasised, tooltips, keyboard support), status chips |
| `dom.js` | Safe DOM builder (text only through `textContent`) and formatting helpers |
| `styles.css` | Valio Aimo portal colours, responsive down to phone width |

## Run

See [Run the MVP locally](../README.md#run-the-mvp-locally) in the main README.
