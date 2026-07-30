# Jarvis

AI superpowers for Frappe/ERPNext, powered by [agent](https://github.com/openclaw/openclaw).

Jarvis lets ERPNext users — especially business owners and execs — ask plain-English questions over their ERP data and get correct, permission-aware answers grounded in the actual records. It pairs an in-bench Frappe app (settings, permission-aware tool layer, HTTP API, on-save credentials propagation) with an agent runtime hosted per-tenant on Aerele's infrastructure. Data stays on the customer's bench; the agent brain lives off the customer's bench; permissions inherit from Frappe's own per-user checks.

**Status:** The end-to-end agent loop + chat UI are live, and the **Phase 3 SaaS control plane is built**: `jarvis_admin` (signup, Razorpay billing, fleet orchestration), the per-host `jarvis-fleet-agent` + Traefik TLS edge, `jarvis-openclaw-plugin` (the agent calling back into Frappe), and a RO-mounted `jarvis-persona`. **11 tools** (5 read + 6 write), identity via a single `X-Jarvis-Session` header (Path A v2). Customers connect via **Jarvis Cloud** (onboarding page); a single-bench dev path also exists. Docs are maintained in the `jarvis_admin` repo (`docs/customer-app/` for this app; `docs/production-deploy.md` for the operator bring-up) — see Documentation below.

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Aerele-RnD/jarvis --branch main
bench install-app jarvis
```

## Quick start (Jarvis Cloud — production)

```bash
# 1. Install the app on your site
bench --site <your-site> install-app jarvis
```
Then open **`/app/jarvis-onboarding`** in Desk → sign up + pay → the control
plane assigns you a managed agent container and stores its connection in
Jarvis Settings. Set your LLM provider/model/key in **Jarvis Settings**, then
chat at **`/app/jarvis-chat`**. Full walkthrough: **getting-started** (see Documentation).

**Local single-bench dev** (run agent yourself, no control plane) → the
**local-dev** guide (see Documentation).

## Optional: parallel chat turns (dedicated worker queue)

Each in-flight chat turn occupies one background (RQ) worker for the turn's
whole duration, and turns run on the shared `long` queue by default — so on a
bench with one long worker, multiple conversations (e.g. a batch of File Box
documents) process **one at a time**, and other long-queue jobs wait behind
them.

To process turns in parallel and isolate them from the rest of the bench,
declare a dedicated `jarvis_chat` queue in `common_site_config.json`:

```json
"workers": {
    "jarvis_chat": {"timeout": 720, "background_workers": 4}
}
```

then regenerate the process config and reload:

```bash
bench setup supervisor && sudo supervisorctl reread && sudo supervisorctl update
# (dev benches without supervisor: bench setup procfile, then restart bench start)
```

- **Frappe Cloud**: add the same `workers` block through your bench's
  configuration (dedicated/private benches; contact Frappe Cloud support if
  the key isn't editable on your plan). If the queue can't be provisioned,
  nothing breaks — see below.
- **This is opt-in and self-disabling.** Jarvis routes turns to `jarvis_chat`
  only when the queue is *declared* **and** a live worker is *listening on
  it*; otherwise every turn uses the `long` queue exactly as before. A
  declared-but-dead queue therefore never strands chats, and benches that
  skip this section need no changes.
- Chat workers mostly wait on network I/O (they relay the agent's event
  stream), so they are cheap: ~100–150 MB RAM each, negligible CPU. Size
  `background_workers` to the number of simultaneous conversations you want.
- Escape hatch: set `jarvis_chat_queue` in a site's `site_config.json` to
  force a specific queue (e.g. `"long"` to opt one site out).

## Architecture at a glance

In production the customer site never runs agent — saving Jarvis Settings
POSTs to Aerele's control plane, which provisions/updates the container on the
fleet; the agent calls back into Frappe (`call_tool`) with per-user identity.
See the **architecture** guide for the full picture (production vs dev shapes,
identity flow, trust boundaries).

## Documentation

All Jarvis docs are maintained in the **`jarvis_admin`** repo (internal), under
`jarvis_admin/docs/`:

- **Customer-app docs** (this app) — `jarvis_admin/docs/customer-app/`:
  getting-started, architecture, configuration, tools-api, local-dev,
  development, decisions.
- **Operator/platform docs** — `jarvis_admin/docs/` (start at `production-deploy.md`).

(They lived here under `jarvis/docs/` previously; consolidated so all docs are in
one place. See [`jarvis/docs/README.md`](jarvis/docs/README.md) for the pointer.)

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/jarvis
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

See the **development** guide (`jarvis_admin/docs/customer-app/development.md`) for the full dev workflow, recipes, and project layout.

## License

MIT
