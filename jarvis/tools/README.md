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

The create / update tools (`create_doc`, `create_docs`, `update_doc`; `api._PREVIEWABLE`)
are trial-run when they park: the operation runs through all DocType validations
with **every DB write rolled back** - commits are neutralized for the duration and
the work is undone via a savepoint, so even a tool that calls `frappe.db.commit()`
internally cannot persist. (A model-passed `preview: true` on a gated write is
ignored: the gate parks it and builds this preview itself.)

`submit_doc`, `cancel_doc`, `amend_doc` and `delete_doc` are **not** trial-run
(round-2 decision R2-2): a trial fires their on_submit / on_cancel hooks, and what
those send (email, calls to other systems, live notifications) cannot be rolled
back. They park a described card (`{"preview": false, "described": true, ...}`)
whose verb card carries a `consequence` line ("Submitting posts its accounting and
stock entries, if any, and locks the document.", "Cancelling reverses its
entries.", "Deleting removes it permanently.", "Amending creates a new draft from
the cancelled document."); a failure shows at Confirm. The trial-run preview:

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
depends on the card. Neither client shows it when a structured card exists (chat
card, phone decision sheet and Approval Board alike); there the card's own
`warning` (below) carries what the trial found, and the rest reaches the user only
when the model relays it. Both clients render it on the plain fallback, for a call
that has no structured card. It is stored with the pending record and in the
transcript either way.

The card (`confirm_card.build_card`) gets `warning: {"jobs": N, "rolled_back":
bool}` only when the trial found something: `jobs` is the real number of
background jobs Confirm will start (`will_queue_jobs`), `rolled_back` is
`hook_rolled_back`. All three surfaces render it as ONE line under the risk
banner, from the shared `cardWarningOf` / `cardBannerOf`
(`frontend/src/lib/actionSummary.js`).
Four more keys appear only when they apply:

- `will_queue`: background jobs a hook enqueued during the dry run. They are
  **not sent**; these are their names (distinct, at most 20 plus a `+N more`
  marker, each one line and at most 140 characters). `note` then also says how
  many jobs Confirm will start. Frappe's own clean-up job after a delete
  (`delete_dynamic_links`, enqueued on every delete) is dropped but not
  listed or counted.
- `will_queue_jobs`: how many jobs that is (`will_queue` names each kind once);
  the card's warning line shows it.
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
- **A child row on its own** (`update_doc` / `delete_doc` on a row of a child
  table, or a root save / delete of one through `run_method`): judged by the row's
  PARENT, read from the stored row and never from the call. Under a structure
  parent (DocField, DocPerm, Workflow Transition ...) or a sensitive one (Has Role
  and Block Module of a User, Webhook Header, Notification Recipient; every
  conditional doctype; a Customer's / Supplier's portal users; Jarvis's own
  configuration in the argument layer) it is refused on every route and in every
  mode, with the parent's code (`structure_refused` / `sensitive_refused`) and
  "Change this through its record: User x, field Roles." No card admits it: the
  change goes through the parent, which gets its own card or refusal. A row under
  an ordinary parent (a Sales Invoice Item) is unchanged, except a ROLE LIST (a
  child table whose only value field is a Link to Role: Has Role, OAuth Client
  Role, User Role, Workflow Action Permitted Role, Onboarding Permission, Jarvis's
  allowed-role tables): its rows grant access by what they are, so they are
  refused under every parent (a Page, a Dashboard Chart, a Workspace). The parent's
  own write is sensitive when it SETS such a table (owner decision R2-18,
  `_role_tables` / `_sets_role_table`): an `update_doc` that names the role list
  at all (an empty list too: emptying a Page's roles opens it to everyone) or a
  `create_doc` that lists a role parks the access card in every mode, on any
  form, listed or not, a custom app's included; anything else on that record
  stays ordinary. The same rule holds at the save (`_doc_changes_roles`: a root
  save that adds a role list or leaves one different from what is stored), so
  `frappe.client` through `run_method` is covered too. A row with no parent
  record is refused when its child table can sit under a structure or sensitive
  parent at all. A row the parent's save writes, or another document's save, is
  nested and passes, as from Desk. What the refusal tells the assistant to do
  instead follows the parent: `update_doc` on the parent (each kept row with its
  name) where that update is carded: a sensitive parent (a User, a Webhook, a
  Script Report), a parent with a role list (a Dashboard Chart, a Workspace, a
  Report Builder report) or one changed through a guarded card (a Workflow, CRM
  Settings, Domain Settings, while the site switch allows those); Desk, with the
  parent's page, under any other structure parent and under a Page (Frappe lets
  only the Administrator save one; a site sets a page's roles through Role
  Permission for Page and Report). It never promises a card the parent would
  not get. The role rule also applies where a form is otherwise classified by a
  condition or by a field (a Report Builder report, an Employee with a role
  table added by Custom Field); such a card carries the access line, not the
  doctype's own. The roles Frappe itself gives a new Report Builder report
  (its form's permlevel-0 roles, `Report.before_insert`) are not a list the
  caller set, so creating one stays ordinary. At the save the rule is not
  applied to Jarvis's own configuration (the argument layer only). Not covered: a Data Import of such a
  form.
- **Guarded structure writes** (R2-10; `_guarded_structure.py`, shared, with
  `_custom_field_guard.py`, `_workflow_guard.py` and `_settings_guard.py`): the
  structure changes chat may make, each only through a confirmation card on an
  ordinary chat (never File Box, the Approval Board, a sheet, `preview_doc` or an
  uncarded run), with NO trial run. A Custom Field first; Workflow and the two
  settings documents follow it below.
  - After every guarded write is saved, and before anything says so, the record
    is read back and held to the card (owner decision R2-17,
    `chat/pending_actions/_verify.py`): each value the sealed call named and each
    cell of each row of a table it sent, through the card's own value rule
    (`same_value`). What the call did not name (timestamps, defaults, what a
    controller derives) and the record counts on the card are not compared. A
    difference fails the confirm: it is rolled back where the write was one
    transaction and cleaned up as after any failed guarded write where a column
    had been added (the record is removed or put back; a column that was added
    stays, and a NEW Custom Field with it, as the clean-up text then says), the
    card ends Failed with "What was saved did not match the confirmation card",
    and one Error Log (`jarvis.pending_action.post_write_mismatch`) names the
    field, never a value. A record that cannot be read back counts as a
    difference. So the sealed call carries the form Frappe's save stores, and
    the card shows it: markup in a text value as the XSS pass rewrites it
    (`_guarded_structure.as_sanitized`: `<br/>` is stored as `<br>`), a Custom
    Field's `insert_after: "append"` as the form's last fieldname, its
    "translatable" off on a type with no translation, and a null on a new field
    or a new workflow row as the default it takes. Text the card cannot show as
    stored is refused at the card: markup that comes out different on every
    sanitising pass (the save sanitises the stored form again), and a workflow
    update value the HTML filter would rewrite (`doc.a<doc.b and doc.c>1` is
    read as a tag; with spaces around the signs it is left alone).
  - ONE new Custom Field (`create_doc`, not a batch), and an edit of ONE Custom
    Field that changes no column (`update_doc`: label, description, hidden,
    mandatory ...). A delete, a change of type / length / name / `unique` / index,
    a virtual, Table or HTML field, an app-made (`is_system_generated`) field, and
    any field on a core, virtual, security, audit or Jarvis DocType (or one the
    structure / sensitive lists name): refused, with the Desk page.
  - Every check reads the values as the write stores them (`unique: "true"` is 1,
    `length: "1,000"` is 1000), and the schema diff runs on a private `Meta` no
    cache holds.
  - `fetch_from` only as `<Link field of the form>.<ordinary field of the linked
    form>` the requester may read (never a Password / secret-named / permlevel
    field); a default with a quote or backslash is refused (it would leave the
    table drifting).
  - Park checks refuse what is known to fail, naming the field: a fieldname that
    is already a field or a column of the real table (`information_schema`, so a
    leftover column counts), leftover Property Setters for it, a row with no room
    (MariaDB's 65,535-byte limit less a margin), no Custom Field create / DocType
    write permission, and a schema diff (`schema_changes`: Frappe's own
    `DBTable.alter` with `sql_ddl` recorded, inside a rolled-back savepoint) that is
    not exactly the one expected `ADD COLUMN` (nothing, for an edit).
  - The card is the sensitive full card with the banner "Confirming changes the
    database structure for every user and cannot be undone." (`card.structural`),
    followed by "This runs code for every user." for an `eval:` expression, the
    access line for permlevel / ignore_user_permissions / mask / reqd / hidden /
    Link options / fetch_from, and a line saying a fetch copies data from the
    linked record.
  - Confirm (`pending_actions.execute`): the form's structure lock (a MariaDB
    `GET_LOCK` on its own connection: no cache clear or commit drops it, a dead
    worker lets go at once) is taken BEFORE the claim (held: "try again", the card
    stays), the checks run again under it, the ALTER waits 5 s at most for the
    table (`lock_wait_timeout`, put back), and a failure after the field row
    committed is cleaned up under the same lock. The row this confirmation wrote
    is known exactly: its name and timestamp are stamped into `sealed_undo` in the
    save's own transaction. A new field's row is removed when its column is
    missing; an edit is put back from the before-image sealed at the claim.
    Outcome: `failed` ("nothing was changed") when the clean-up proved nothing is
    left, `partial` when something may remain (a complete field kept, a later edit
    standing, a clean-up that failed twice); never fixable. The reconciler does
    the same clean-up, under the same lock, for a row whose worker died and for a
    clean-up that failed (retried for 24 h, then logged as needing a person).
  - Shown in full on the card but NOT flagged or refused (owner-accepted): markup
    in a label or description, and a fetch from an ordinary readable field of the
    linked record (a User's mobile number, say). The person confirming reads every
    value the field carries.
  - The call is carded, sealed and run in its stored form (`_stored`: `"TRUE"` is
    1, `"1,000"` is 1000, the doctype by its exact name), so the card, the checks
    and the write never disagree. Re-pointing a Link re-checks every fetch through
    it against the new target.
  - Site config `jarvis_structure_writes_disabled: 1` turns every guarded write
    off (refused like any structure write). Set it with `bench --site <site>
    set-config jarvis_structure_writes_disabled 1`; it is read from the config
    files at park and at Confirm, so no restart is needed. Only a number counts
    (Frappe's own rule): a hand-typed `"yes"` does not switch it on. Either file
    switches it off: a `0` in `site_config.json` does not switch back on what
    `common_site_config.json` set off. A config file that cannot be read is off
    too, with one Error Log (`jarvis.structure.switch_unreadable`). Needs
    `bench migrate` (`sealed_undo`); until then they are refused too.
  - ONE Workflow created (`create_doc`) or updated (`update_doc` by name); a
    delete, a batch, a rename and a move to another form stay refused. Saving a
    Workflow sets every other workflow of the form inactive when it is active,
    adds a hidden state field (a committed row, then `ALTER TABLE`) when the form
    has none by that name, and fills the state on every record that has none. So
    the card (in stored form: every state and transition whole, Frappe's defaults
    spelled out, each linked name exact; an update always carries both tables)
    says which active workflow it replaces, how many records get a state and how
    many keep one this workflow lacks and so cannot be saved until their state is
    changed (what Frappe's `get_workflow_state_count` counts; both counts stop at
    a bound, the second reads "more than 10000"). What is particular to this
    workflow comes first: an active workflow with no transitions, which states
    submit or cancel, how many transitions allow self-approval (a transition
    written from chat has it OFF unless the request sets `allow_self_approval`,
    owner decision R2-14: Frappe's default is on; a stored row keeps what it
    has; with none, the card says self-approval is off and that the workflow
    action refuses the record's creator, the Administrator excepted), what
    `update_field` writes, an existing text field used as the state field, a state
    or action open to everyone signed in (role All) or to the Administrator only.
    Then the lines every workflow card carries: the access line, and that it
    emails. "This runs code for every user." is said only for a condition or
    expression chat could not have written (set up in Desk, kept unchanged and
    not checked here), which is named. A condition
    written from chat is ONE restricted expression over the document
    (`_workflow_guard.expression_problem`): a comparison, or and / or / not of
    comparisons, of this form's real fields (`doc.field`, `doc["field"]`,
    `doc.get("field")`), text, numbers and arithmetic on numbers and number
    fields; `in` / `not in` take a literal list of at most 100 values. Refused,
    with the Desk pointer: `frappe.`, any other call or name, a field the form
    does not have, a bare value, arithmetic on text or on a list, a huge number,
    a lambda, a comprehension, an f-string, a hidden or look-alike character,
    more than 500 characters (controller default; the owner may relax it). Also
    refused, because Frappe evaluates a condition with nothing to catch an error
    and it would reach every user who opens or moves a record: ordering (`<`
    `>` `<=` `>=`) anything but two numbers, a number being an Int, Float,
    Currency, Percent or Check field (the types Frappe never leaves empty; a
    Duration, Long Int or Rating can be) (a Date field against text raises,
    so a date rule is set up in Desk; text against text raises when the field is
    empty), and dividing by anything but a plain non-zero number. A Frappe 16 update value marked as an expression follows the
    same rule but may be a plain value. An edit may keep a richer condition a row already has, as it
    is. Guest is never an approving or editing role. It leads with the
    structural line only when a column is added. Refused at park: a form chat
    never customises (the Custom Field list), a child table or Single, a missing
    or differently spelled Workflow State / Action / Role, an expression that does
    not compile, a submit / cancel state on a form that cannot be submitted, an
    active workflow with no state, a state listed twice, a virtual form or one
    with no table yet, an `update_field` that gives access (`_FIELD_SENSITIVE`),
    a change of the state field on an edit, `transition_tasks` (Frappe 16:
    scripts and webhooks), a state field that cannot be added (the Custom Field checks) or
    that Frappe would rename or cannot fill (`parent`, `select`), and a fill of
    more than 5,000 records, whether the state column is new or not. Every state
    and action that does not exist yet is named in one refusal. Under the lock,
    before the claim, a card that no longer says which workflow is replaced ends
    `stale`, and so does one whose check could not be made at all. A snapshot
    that cannot be read whole stops the write before its record is saved
    (`_guarded_structure.snapshot_failed`): nothing is changed and the card stays
    to be confirmed again. When the confirm
    does not end as done, the workflow is removed (or put back from its
    before-image), the one that was active is active again, a state field whose
    column is missing is removed and the states filled in are emptied: a reported
    failure never leaves a workflow live. A confirm whose worker died is cleaned
    up as soon as its form's lock is free (after 60 s, not the 10 minutes other
    rows wait: the lock is held for the whole confirm; a confirm that outlives 60 s
    AND lost its lock connection could be cleaned up beside itself, as one that
    outlived 10 minutes could before, and `structure_lock_lost` reports that). One exception: a workflow
    that people have used since it was saved (any record of the form carrying a
    state was saved since, or a filled record carries another state) is NOT
    taken away, and a record saved since never has its state emptied; the row ends
    `partial`, the workflow stays, and one Error Log
    (`jarvis.pending_action.structure_needs_a_person`) names it.
  - Operator recovery aid, not a product feature: for a Workflow and for Domain
    Settings the snapshot stays sealed on the confirmation row after a successful
    confirm, and `_guarded_structure.undo_confirmation` puts it back:
    `bench --site <site> execute jarvis.tools._guarded_structure.undo_confirmation
    --kwargs "{'name': '<confirmation id>'}"`. It removes the workflow (or
    restores the edited one), re-activates the one it replaced and empties the
    states it filled; for Domain Settings it restores the list, the removed role
    assignments and the switched roles and modules. It changes nothing when the
    record was changed since or the workflow has been used. Not whitelisted,
    refused inside a tool call, System Manager only when a request calls it, same
    locks, one `Jarvis Agent Write` row, idempotent. The snapshot exists only in
    `Jarvis Pending Action.sealed_undo` and goes when `pending_actions.purge`
    deletes the settled row (seven days). Nothing on a card or in chat says an
    undo exists.
  - CRM Settings (`update_doc`). With `enable_frappe_crm_data_synchronization` off
    in the saved document the save changes no structure: ordinary sensitive
    configuration (risk line "This changes a setting for every user."), trial-run
    like any update. With it on, every save runs ERPNext's `create_custom_fields`
    for Quotation and Customer: the guarded class `crm_settings_sync`, which locks
    the settings and both forms, pre-checks each table (exactly the missing
    fields' columns), names the fields on the card and says what the sync lets the
    allowed users do; on failure the settings are put back and a field without a
    column is removed. ERPNext also writes its own values over an EXISTING field
    of the same name: the card names that field and what changes ("It also
    rewrites the existing field ..."), and a failed confirm puts it back exactly. A card parked as plain settings never runs once the sync
    is on.
  - A row of these records saved by its own name (a Workflow Document State or
    Workflow Transition, a Has Domain, a Frappe CRM Allowed User) falls under the
    child-row rule above: refused by its stored parent, changed through its
    record. A Workflow Transition Tasks list (Frappe 16: the scripts and webhooks
    a transition runs) is sensitive (code), so its Workflow Transition Task rows
    are covered the same way.
  - Domain Settings (`update_doc`, the whole `active_domains` list). Refused where
    any declared domain (the `domains` hook; none in stock frappe / erpnext /
    hrms) carries `on_setup`, `set_value` or `properties`, or where the save would
    delete a domain's fields. Otherwise the card names the roles and modules
    turned off, how many role assignments are removed, and the roles given to the
    person confirming; the `Has Role` rows it removes are kept on the confirmation
    row (the operator aid above). As the confirmed save begins, Frappe's cached
    list of active domains is dropped: left stale, Frappe treats the domain being
    switched on as inactive (its roles removed, its fields deleted).
- **Sensitive** (Server Script, Client Script, Webhook, Notification, Auto Email
  Report, Custom DocPerm, Property Setter, Scheduled Job Type, Website Script,
  Custom HTML Block, Email Account / Domain, User, Role, Role Profile, Module
  Profile, User Permission, Social Login Key, OAuth Client, Connected App, LDAP
  Settings, Automation Flow, Custom Role; Report of type Script / Query and Jarvis Trigger with a Script action;
  Web Page / Web Form / Website Theme / Website Settings / custom Web Template when
  a write puts content in their script / HTML fields; Auto Repeat when it emails
  people): parks behind its own card in EVERY mode; the draft panel turns it into
  that card. A File Box write held for the Approval Board gets the same card; with
  approval sheets on, a sensitive record is refused from the sheet instead (the
  sheet's editor has no full view), plain words, audited. The
  card (`card.risk = "sensitive"`, `card.risk_line` naming every risk the call
  carries, such as "This runs code for every user.", rendered as a banner on the
  chat card, the phone sheet and the Approval Board) shows the record in FULL:
  - no 200-character clip, no 20-row cap, every record open;
  - a password as "set" / "changed" / "cleared" ("unchanged" for Frappe's all-`*`
    dummy); a whitespace-only value is kept and every character of it marked
    (spaces included, so it never draws as an empty cell);
  - every character that draws as nothing or hides a line (by Unicode category:
    format, control other than newline and tab, line / paragraph separators,
    spaces other than U+0020, unassigned, private use, plus variation selectors,
    the combining grapheme joiner, Hangul fillers and the braille blank) as a
    `⟦U+XXXX⟧` marker; a typed `⟦` is itself marked, so a marker cannot be forged;
    a line break other than `\n` (a LONE `\r`, U+2028 ...) also forces a block, the
    line diff and a note; a CRLF line ending is ordinary and is not marked (a
    CRLF / LF-only update says "Only the line endings change.");
  - a long value marked `multiline`; an update's long value with a line diff
    (`lines`, split on `\n` only, gaps counting the unchanged lines left out,
    linear above 500 lines) beside both full texts;
  - a child-table change as the current and the new table (`from_table` /
    `to_table`, an empty side shown as "none"); a JSON value written out;
  - a delete shows every non-empty field of the record (a Server Script's script)
    and its child tables as full tables (cells marked, a hidden-break note).
  A call or card over 256 KB is refused (`sensitive_refused`, sent to Desk), never
  clipped; so is a sensitive write whose card could not be built. A `run_import`
  is refused in every mode, and again at Confirm, only when it would write
  something sensitive: a doctype on the sensitive list (or a conditional / Jarvis
  configuration doctype), or a file with a non-empty access-granting column
  (`_write_risk._import_grants`, reusing the field rules: Customer / Supplier
  portal users, Employee `user_id` / `create_user_permission` /
  `create_user_automatically`, and Employee `status` on an update import), or a
  file that was read but whose columns cannot be classified. A file that cannot be
  read or parsed at all (missing, mistyped, header-only, a bad mapping key) gets
  the import's own fixable error instead. Its card samples rows and cannot show
  every record: Data Import in Desk. An ordinary Customer / Supplier / Employee
  import parks its card as before.
- A refusal written for the model ("Do not retry it with another tool; tell the
  user ...") also carries `error.person_message`, which the chat's failed tool
  row, the action error banner, the Approval Board and the phone show instead
  (`personError` in `frontend/src/lib/actionSummary.js`).
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
