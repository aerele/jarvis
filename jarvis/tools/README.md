# Jarvis tool layer

The thin execution layer the agent uses to operate a Frappe/ERPNext site.
Knowledge (fields, flows, gotchas) lives in the per-DocType **Skills**
(`jarvis-persona`); tools are generic primitives that read and write through the
live Frappe ORM. **Tools never hardcode a DocType schema** - the agent calls
`get_schema` to read the live one.

This README documents the layer's contract plus the extensions added in
`feat/tool-layer-extensions` (`run_method`, `get_schema` metadata + cache,
`preview` dry-run, write auditing). The full agent-facing inventory, with
confirmation discipline, is in `jarvis-persona/TOOLS.md`; the authoritative name
set is `jarvis/tools/tool-names.json` (see the 3-way invariant below).

## Architecture

Two tiers, one call mechanism:

1. **Transport (TypeScript):** `jarvis-openclaw-plugin` exposes each tool to the
   openclaw agent runtime - descriptors in `src/tool-defs.ts`, typed params in
   `src/schemas.ts`, the contract list in `openclaw.plugin.json`. It calls back
   to the bench over HTTP.
2. **Execution (Python, in-process):** `jarvis.api.call_tool` is the whitelisted
   entry point. It authenticates the caller, resolves the user, runs
   `frappe.set_user(<that user>)`, and dispatches to the tool function via
   `jarvis/tools/registry.py`. Tools call the Frappe ORM directly, **under the
   chat user's own permissions** - so Frappe's permission model is the security
   boundary, not the tool code.

A **3-way invariant** must hold for every change:
`tool-defs.ts pythonNames == openclaw.plugin.json contracts == registry _TOOL_NAMES`.

It is no longer maintained by discipline. `jarvis/tools/tool-names.json` is the
single artifact both repos test themselves against - generated from the registry
by `jarvis/tools/_tool_contract.py`, copied verbatim into the plugin repo at
`contracts/tool-names.json`. `jarvis/tests/test_tool_contract.py` fails if the
registry and the artifact disagree in either direction; the plugin's
`tests/tool-contract.test.ts` fails if its descriptors or manifest contracts do.
The registry is the source of truth, because a descriptor with no implementation
is a tool the model can call and the bench can only answer with
`ToolNotFoundError`. Registry tools that deliberately have no descriptor are
listed, with reasons, in the artifact's `backend_only` map.

## Auth

`call_tool` accepts two modes (see `jarvis/api.py`):

- **Standard Frappe auth** - session cookie or
  `Authorization: token <api_key>:<api_secret>`. The tool runs as that user.
- **Plugin auth** - the plugin presents `X-Jarvis-Token` (the shared
  `agent_token`, proving the request came from inside the openclaw container) and
  `X-Jarvis-Session` (the openclaw sessionKey). The user is resolved from
  `Jarvis Chat Session` and dispatch runs under that user via `set_user`.

Guest is rejected. Secrets come from site config / env only.

## Result envelope

Every call returns one shape (`jarvis/api.py:_run_tool`):

```jsonc
{ "ok": true,  "data": <result> }
// or
{ "ok": false, "error": { "code": "PermissionDeniedError", "message": "<Frappe's message verbatim>" } }
```

Frappe validation/permission messages are surfaced **verbatim**, never swallowed.
Common codes: `InvalidArgumentError` (bad input / link / mandatory / unknown
DocType), `PermissionDeniedError` (403), `ToolNotFoundError`.

## Layer-1 primitives (generic, cover every DocType)

`get_schema`, `get_doc`, `get_list`, `query`, `run_report`, `get_report_filters`,
`run_method`, `create_doc`, `update_doc`, `submit_doc`, `cancel_doc`,
`delete_doc`, `amend_doc`, plus artifact/computed-read tools. The Frappe API is
uniform, so these cover all DocTypes - reach for a specialized tool only when it
carries domain logic the schema doesn't express.

---

## `run_method` - call a whitelisted server method

The escape hatch for `@frappe.whitelist()` methods the dedicated tools don't wrap
- most often ERPNext's `make_*` document mappers.

```python
run_method(method: str, args: dict | None = None)
```

- `method` is a dotted path, e.g.
  `erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice`.
- **Only whitelisted methods run.** Non-whitelisted (or unresolvable) methods are
  rejected with `PermissionDeniedError` / `InvalidArgumentError`. The method's own
  permission checks still apply (it runs as the chat user).
- Returns the method's return value verbatim (often a document dict).

**Example** - create a draft Sales Invoice from a Sales Order:

```jsonc
// tool args
{ "method": "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
  "args": { "source_name": "SAL-ORD-2026-00042" } }
```

**Allowlist (recommended in production).** Set a site-config list of fnmatch
patterns to narrow what may ever be called:

```jsonc
// site_config.json
{ "jarvis_run_method_allowlist": ["erpnext.*.make_*", "frappe.client.*"] }
```

When set, any method not matching a pattern is rejected even if it is whitelisted.

## `get_schema` - live introspection (the backbone)

```python
get_schema(doctype: str, verbose: bool = False, refresh: bool = False)
```

Returns the live schema, not a stored copy:

```jsonc
{ "doctype": "Sales Invoice",
  "is_submittable": true,
  "autoname": "naming_series:",
  "naming_rule": "By \"Naming Series\" field",
  "title_field": "title",
  "workflow": { "name": "...", "state_field": "workflow_state", "states": ["Draft", "Approved", ...] },
  "fields": [ { "fieldname": "customer", "fieldtype": "Link", "label": "Customer", "options": "Customer", "reqd": true }, ... ] }
```

- `verbose=true` inlines `child_fields` for Table fields (one level).
- Result is cached in Redis for **300 s** (schema is user-independent; the
  **read-permission check runs on every call regardless of cache**, so caching
  never leaks a schema to a user who can't read the DocType).
- `refresh=true` busts the cache - use after a Customize Form / Custom Field
  change.

## `preview` - dry-run on writes

The write tools (`create_doc`, `update_doc`, `submit_doc`, `cancel_doc`,
`amend_doc`, `delete_doc`) and `run_method` accept `preview: true`. The operation
runs through all DocType validations with **every DB write rolled back** -
commits are neutralized for the duration and the work is undone via a savepoint,
so even a tool (or a `run_method` target) that calls `frappe.db.commit()`
internally cannot persist:

```jsonc
// args
{ "doctype": "Sales Invoice", "values": { "customer": "Acme Corp", "items": [...] }, "preview": true }
// data
{ "preview": true,
  "would": { "name": "<resolved name>", "grand_total": 1180.0, ... },
  "note": "Validated with all DB writes rolled back; nothing was committed. External side effects (emails/webhooks in on_submit / on_cancel) are not sandboxed by preview." }
```

Use it to show the user exactly what a write would produce (resolved name,
fetched/computed fields) - or the validation error it would hit - before they
confirm. Preview runs are never audited (nothing is committed). **Caveat:** DB
effects are sandboxed, but external side effects in `on_submit` / `on_cancel`
(emails, webhooks) are not rolled back.

## Audit logging

Every **mutating** tool call is logged from the `_run_tool` choke-point
(`jarvis/audit.py`) - reads are not logged, `preview` runs are not logged
(nothing is committed). Each entry records who / what / when / result:

```jsonc
{ "ts": "2026-06-29 10:15:02", "user": "navin@aerele.in", "tool": "create_doc",
  "doctype": "Sales Invoice", "name": "ACC-SINV-2026-00131", "status": "ok",
  "error_code": null, "error_message": null, "args": { ... } }
```

- Sink: the `jarvis.tool_audit` Frappe logger (the bench `logs/` directory). A
  structured logger is used rather than a DocType insert so auditing is
  transaction-safe and can never entangle or break the tool's own DB transaction;
  a queryable DocType sink can be layered on later by swapping the body of
  `audit.record`.
- Secret-shaped keys (`password`, `api_key`, `token`, ...) are redacted; the args
  summary is size-capped.

---

## Adding a tool

1. Write `jarvis/tools/<name>.py` exposing `def <name>(...)`. Validate inputs;
   raise `InvalidArgumentError` / `PermissionDeniedError` from
   `jarvis.exceptions`; let Frappe validation errors propagate (the envelope
   translates them).
2. Register the name in `jarvis/tools/registry.py` `_TOOL_NAMES`.
3. If it mutates, add it to `_WRITE_TOOLS` in `jarvis/api.py` (and `_PREVIEWABLE`
   if a dry-run makes sense).
4. Regenerate the contract artifact from the bench root:
   `env/bin/python -m jarvis.tools._tool_contract --write`. Until you do,
   `test_tool_contract` fails. (A tool that must stay bench-side goes in
   `_tool_contract.BACKEND_ONLY` with its reason instead.)
5. Copy `jarvis/tools/tool-names.json` **verbatim** into the plugin repo at
   `contracts/tool-names.json` - same bytes, same `digest`. Its contract test
   then fails until you add the descriptor (`src/tool-defs.ts`), the typed schema
   (`src/schemas.ts`) and the manifest contract (`openclaw.plugin.json`), and
   rebuild `dist`. Nothing in CI can see both repos, so this copy is the one
   manual step - the plugin PR is where a stale copy surfaces.
6. Add a row to `jarvis-persona/TOOLS.md`.
7. Add tests under `jarvis/tests/` (happy path + validation + permission).
