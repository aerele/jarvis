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

1. **Transport (TypeScript):** The Jarvis agent plugin exposes each tool to the
   agent runtime - descriptors in `src/tool-defs.ts`, typed params in
   `src/schemas.ts`, the contract list in the plugin manifest. It calls back
   to the bench over HTTP.
2. **Execution (Python, in-process):** `jarvis.api.call_tool` is the whitelisted
   entry point. It authenticates the caller, resolves the user, runs
   `frappe.set_user(<that user>)`, and dispatches to the tool function via
   `jarvis/tools/registry.py`. Tools call the Frappe ORM directly, **under the
   chat user's own permissions** - so Frappe's permission model is the security
   boundary, not the tool code.

A **3-way invariant** must hold for every change:
`tool-defs.ts pythonNames == plugin manifest contracts == registry _TOOL_NAMES`.

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
  `agent_token`, proving the request came from inside the agent container) and
  `X-Jarvis-Session` (the agent sessionKey). The user is resolved from
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

## `run_method` - call a whitelisted method, controller method, or Server Script API

The escape hatch for `@frappe.whitelist()` methods the dedicated tools don't wrap
(most often ERPNext's `make_*` document mappers), for whitelisted **controller
methods** on a DocType, **and** for tenant-authored Server Script API endpoints.

```python
run_method(method: str, args: dict | None = None,
           doctype: str | None = None, name: str | int | None = None)
```

- `method` is dispatched **by shape**, not tried-and-fallen-back:
  - with `doctype` set, `method` is a whitelisted **controller method** on that
    DocType's class, run against the `(doctype, name)` document - the same
    dispatch Desk uses for a doc action (`name` defaults to the doctype for a
    Single);
  - else a bare name registered as a **Server Script API** method (its
    `api_method`) runs via the server-script executor - `args` reach it through
    `frappe.form_dict`, exactly as the `/api/method` HTTP handler feeds them, and
    its output (`frappe.flags` or `frappe.response['message']`) comes back as a dict;
  - else a dotted path to a module-level `@frappe.whitelist()` method, e.g.
    `erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice`, called
    directly and returned verbatim (often a document dict).
- Absent `doctype`, the server-script map is the source of truth for which kind a
  name is; a whitelisted dotted path can never collide with a bare `api_method`.
- **Only registered API server scripts / whitelisted methods run.** Non-whitelisted
  or unresolvable names are rejected with `PermissionDeniedError` /
  `InvalidArgumentError`. Each target's own permission checks still apply (it runs
  as the chat user).

**Examples** - a module mapper, and a controller method on a specific document:

```jsonc
// module-level whitelisted method
{ "method": "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
  "args": { "source_name": "SAL-ORD-2026-00042" } }
// whitelisted controller method on one document
{ "method": "set_status", "doctype": "Sales Order", "name": "SAL-ORD-2026-00042",
  "args": { "status": "Closed" } }
```

**Blocklist.** `Jarvis Settings.run_method_blocklist` (comma/newline-separated
fnmatch patterns) categorically refuses matching targets before dispatch - a
whitelisted method's dotted path, a Server Script API method's name, or
`<doctype>.<method>` for a controller method:

```
frappe.*
*.delete_doc
Sales Order.set_status
```

A match raises `PermissionDeniedError`. An empty field blocks nothing (fail-open);
every `run_method` call still parks for a human confirmation card regardless, so
the blocklist is the "never even offer these" hardstop, not the only boundary.

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
`amend_doc`, `delete_doc`) accept `preview: true`. The operation runs through all
DocType validations with **every DB write rolled back** - commits are neutralized
for the duration and the work is undone via a savepoint, so even a tool that
calls `frappe.db.commit()` internally cannot persist:

```jsonc
// args
{ "doctype": "Sales Invoice", "values": { "customer": "Acme Corp", "items": [...] }, "preview": true }
// data
{ "preview": true,
  "would": { "name": "<resolved name>", "grand_total": 1180.0, ... },
  "note": "Trial run: the change was checked and every database write was rolled back. Nothing is saved until you confirm. A trial run cannot undo email or calls to other systems sent by the document's own code, live notifications, or error log entries." }
```

Use it to show the user exactly what a write would produce (resolved name,
fetched/computed fields) - or the validation error it would hit - before they
confirm. Preview runs are never audited (nothing is committed). The same sandbox
(`jarvis/tools/_preview_sandbox.py`) builds the preview on a confirmation card.

`note` is written for a person: plain language, no key names. Where it is shown
depends on the card. Neither client shows it when a structured card exists, and
one is built for create / update and for submit / cancel / amend / delete (chat
card, phone decision sheet and Approval Board alike); there it reaches the user
only when the model relays it. Both clients render it on the plain fallback, for
a call that has no structured card. It is stored with the pending record and in
the transcript either way.
Three more keys appear only when they apply:

- `will_queue`: background jobs a hook enqueued during the dry run. They are
  **not sent**; these are their names (distinct, at most 20 plus a `+N more`
  marker, each one line and at most 140 characters). `note` then also says how
  many jobs Confirm will start. Frappe's own clean-up job after a delete
  (`delete_dynamic_links`, enqueued on every delete) is dropped but not
  listed or counted.
- `will_queue_note`: a sentence for the model, saying what to tell the user.
  When one of the jobs is Frappe's `queue_action`
  (`frappe.model.document.execute_action`, e.g. a Stock Reconciliation over 100
  rows) it adds that the action itself runs in the background on confirm and was
  not validated by the preview; `note` then says the same and does not call the
  change "checked". What counts is the method a job really runs, never the
  `job_name` its caller gave it.
- `hook_rolled_back: true`: the document's own code called a bare
  `frappe.db.rollback()` during the dry run. It took a failure path, so `would`
  may not be what Confirm produces, and `note` says so.

**The one job that is still sent.** `SURVIVES_DRY_RUN` (in the sandbox module)
lists `jarvis.triggers.engine.write_activity`: a Jarvis Trigger that blocks a
save enqueues its Blocked audit row immediately so that it outlives the
rollback, and it does so from a dry run too. Only when Frappe's own `enqueue`
built the job, and only when it is enqueued immediately; the same method
enqueued after commit (Success / Failed activity) is dropped and is not listed
in `will_queue` (a job that is merely NAMED like it is listed). The card's
`note` does not mention this job: it is sent when an automation blocks the save,
which raises, so no note is built for that dry run. Add a method to that set
only together with a test.

**When the dry run cannot be undone.** If the sandbox's savepoint is gone when it
comes to roll back (a deadlock aborted the transaction, or the write ran DDL,
which commits implicitly: a Custom Field, a DocType), the sandbox rolls the
transaction back in full (with a bare `ROLLBACK` if Frappe's own did not send
one), writes one Error Log row, and raises `PreviewSandboxLost`
(`jarvis/exceptions.py`). If the dry run itself raised, its own error is what
the caller sees; the row is still written.
No card is parked. The model gets a `ConfirmationUnavailableError` refusal saying
that part of the change may already be saved when it alters database structure,
and to retry at most once. `preview_doc` and `get_creation_context` answer with
the error code `PreviewSandboxLost` and the same message.

**Not sandboxed** (the dry run really does these): HTTP or email sent directly
inside a hook; a realtime event published immediately; a write to a
non-transactional table (Error Log is MyISAM); what a write did before its own
DDL; `enqueue(deduplicate=True)` deleting a finished job of the same id; and
jobs that only come into being when a commit callback runs (a webhook's delivery
job) are neither sent nor listed in `will_queue`.

**Telling from the logs that it works.** The sandbox writes to
`logs/jarvis.preview.log` (bench and site):

| Line | Level | Written on a production bench |
|---|---|---|
| `dry run: job sent for real (it must outlive a rollback) ... job='...'` | WARNING | yes |
| `dry run: a bare frappe.db.rollback() was redirected to the savepoint ...` | WARNING | yes |
| `dry run: job not sent ... job='...'` (one per dropped job) | INFO | only with the log level lowered to INFO |
| a lost savepoint, a displaced `get_queue` wrap, a failed cleanup step | ERROR | yes |

Frappe hands out loggers at ERROR outside a dev server; the sandbox pins this
one to WARNING so the two rare events above are kept. A lost savepoint and a
displaced wrap also write an Error Log row, titled
`jarvis preview sandbox: savepoint lost` and
`jarvis preview sandbox: get_queue guard was displaced` (the latter once per
process). A quiet log on a production bench therefore means no job left a dry
run and no hook rolled one back, not that the sandbox is idle; to see every
dropped job, lower the level and look for `job not sent`.

`run_method` is **not** in this set: it is always gated, so `preview: true` is
rejected (`InvalidArgumentError`). Call it directly and the bench parks a
confirmation card built from a described-intent summary of the blast radius -
there is no sandboxed dry-run for it, since its target's inline non-DB side
effects could fire unconfirmed. See the `run_method` section above.

## Write-risk guard - structure refused, sensitive always carded

`jarvis/tools/_write_risk.py` holds the lists and both layers (round-2 decisions
R2-4 REVISED AGAIN, R2-8, R2-10, R2-12).

- **Structure** (Custom Field, DocType, Workflow, Inventory Dimension, Service
  Level Agreement, CRM Settings, Domain Settings, Permission Type, Accounting
  Dimension): refused from chat on every route - the gated card, the draft panel
  (`apply_action`), auto mode, "confirm all", an armed macro, an approved skill
  run, File Box, the Approval Board edit, `preview_doc`, and at Confirm for a card
  parked before the guard. Code `structure_refused`, `error.desk_path` names the
  Desk page. Any structure doctype in a batch refuses the whole batch.
- **Sensitive** (Server Script, Client Script, Webhook, Notification, Auto Email
  Report, Custom DocPerm, Property Setter, Scheduled Job Type, Website Script,
  Custom HTML Block, Email Account / Domain, User, Role, Role Profile, Module
  Profile, User Permission, Social Login Key, OAuth Client, Connected App, LDAP
  Settings, Automation Flow, Custom Role; Report of type Script / Query and Jarvis Trigger with a Script action;
  Web Page / Web Form / Website Theme / Website Settings / custom Web Template when
  a write puts content in their script / HTML fields; Auto Repeat when it emails
  people): parks behind its own card in EVERY mode; the draft panel turns it into
  that card.
- **`run_method`** stays open (R2-13): a card in ordinary chat, none in the
  uncarded modes, no method allow- or deny-list beyond run_method's own (Jarvis's
  gate / turn / decision endpoints). What it SAVES is judged by the ORM guard below:
  a structure root write is refused even after Confirm; a sensitive root write is
  refused uncarded and admitted once the user confirmed the run_method card.
  Accepted gap (aerele/jarvis#1635): a whitelisted method that changes state
  without saving a document (Google Drive `authorize_access`, the permission
  manager, the two-factor reset, Jarvis's workspace reset ...) is not seen and runs
  uncarded in the uncarded modes.
- **ORM guard** (`doc_events["*"]` on before_validate / before_change /
  before_rename / on_trash): while a tool call runs, a ROOT write of a structure
  document is refused, and of a sensitive one unless the confirmed card admitted
  it (`structure_refused` / `sensitive_refused`). Saves a document's own
  controller makes during another document's save (Employee -> User, Stock
  Settings -> Property Setter) pass, as from Desk. Inert for Desk, REST and
  scheduler saves. A refusal a method swallows (`reset_password` catches every
  exception) still turns the call into the refusal.
- **Deletes that call no hook** (a DocType delete, `ignore_on_trash`) are judged
  at `frappe.model.delete_doc.delete_from_table` the same way.
- **Background jobs** a tool call enqueues run under the same guard in the
  worker, with the confirmed card's allow entries (never structure): the preview
  sandbox's `get_queue` wrap runs them through `_write_risk.execute_guarded_job`;
  the queued job keeps its real method, so `get_jobs` dedupe and monitoring see it.
  Jobs enqueued outside a tool call are untouched.
- **No-commit fence**: an uncarded create / update runs with `frappe.db.commit`
  neutralised and a write counted, so a doctype outside the list whose save runs
  DDL (even as its first statement) hits Frappe's `ImplicitCommitError` before
  the ALTER; that is refused as a structure change (also when an app re-raises it
  as another error). Frappe 16's `_disable_transaction_control` is put back on
  exit by the fence and the preview sandbox. An uncarded run_method is not fenced.
- **Access-granting fields** (Employee `user_id` / `create_user_permission`, an
  Employee `status` change that turns its user's login on or off, Customer /
  Supplier `portal_users`), judged when a call changes them, in the argument
  layer and at the save (so `frappe.client` through run_method too). Jarvis's own
  configuration (Jarvis Settings, Connector, Agent Installation / Listing, User
  Settings, Macro, MCP OAuth Client / Token: argument layer only) and a Data
  Import of any conditional doctype park a card too.
- Every refusal is a `refused` row in Jarvis Agent Write plus a log line
  `jarvis.risky_write risk=... doctype="..." name="..." user="..." outcome=...`.

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
   (`src/schemas.ts`) and the plugin manifest contract, and
   rebuild `dist`. Nothing in CI can see both repos, so this copy is the one
   manual step - the plugin PR is where a stale copy surfaces.
6. Add a row to `jarvis-persona/TOOLS.md`.
7. Add tests under `jarvis/tests/` (happy path + validation + permission).

## Workspace integration reference

Exact upstream commands, configuration filenames, source citations and historical
migration explanations are maintained outside this app checkout in the workspace
file `docs/jarvis-runtime-integration-reference.md`. References to the workspace
integration reference in source comments refer to that document. It is maintained
with the workspace and is not included in the standalone app distribution.
