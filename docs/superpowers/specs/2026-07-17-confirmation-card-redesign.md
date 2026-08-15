# Confirmation card redesign — record summaries on every gated action

Status: **rev 3 — Fable-approved, ready to plan** · Date: 2026-07-17 ·
Repo: `Aerele-RnD/jarvis` (app only)

Two Fable review rounds. Rev 1 returned **don't-ship** with three blocking findings; rev 2
returned **ship after five text corrections**, all applied here. The review ledgers at the
bottom list every error so none is silently buried. Load-bearing claims from both rounds were
independently verified against source before acceptance — including one where Fable corrected a
false citation of mine, and one where Fable corrected its own rev-1 recommendation.

## Problem

Every gated write parks behind a "Confirm before this runs" card. The cards tell you the
**identifier** of what is about to change, not the **thing**. A delete card says
`Will delete this Sales Order SO-0001` and nothing about the order; a bulk-create card lists
`{doctype, name}` per record and throws the field values away even though they are sitting in
`args.docs[i].values`; **eight of the 24 gated shapes** render no card at all and fall back to a
technical string from `_describe_call`.

`bulk_update` (PR #317) is the exception: it shows per-record content, collapsibly. It is the
only card that answers "what am I actually approving". This spec brings every other card up to
that standard — and fixes two defects the review found in the cards that already ship.

## Scope

**In:** phases 1-4 below, all inside the `jarvis` app. No plugin build, no persona change, no
container restart.

**Out (own round):** `summary_fields` — letting the model choose which fields a card shows.
See "Round 2".

## What exists today (verified 2026-07-17)

14 tools gate (`api.py:469` `_GATED_WRITES`). They present **24 shapes**, because most have a
single and a bulk path (`_BULK_ARG_KEYS = ("names", "updates", "docs", "messages")`,
`api.py:505`). `build_card` (`confirm_card.py:51`) covers 16:

| Card kind | Tools | Renders |
|---|---|---|
| `create` | `create_doc` single | field list from perm-filtered `would` |
| `batch_create` | `create_doc(docs=[])` | `{doctype, name}` bullets + model-authored notes |
| `update` | `update_doc` single | from→to diff |
| `bulk_update` | `update_doc(updates=[])` | per-record from→to, collapsible |
| `verb` | `submit_doc`, `cancel_doc`, `delete_doc`, `amend_doc`, `apply_workflow_action` (single + `names=[]`) | "Will {verb} …" + bare target names |
| `email` | `send_email` single | to / subject / body |
| `method` | `run_method` | dotted path + args, secret keys masked |

Eight shapes get **no card** — `build_card` returns `None` and the SPA falls back to
`_describe_call`'s string plus "not a dry run":

| Shape | What the human sees today |
|---|---|
| `send_email(messages=[])` | `send_email count=20 targets=[SI-0001, …]` — document names, **no recipients** |
| `share_doc` single / bulk | `share_doc doctype=X name=Y` — **no user, no permission flags** |
| `assign_to` single / bulk | `assign_to doctype=X name=Y` — **no assignee, no description** |
| `create_custom_skill` | `create_custom_skill` — **bare tool name** |
| `update_wiki` | `update_wiki` — **bare tool name** |
| `create_docs` (deprecated shim) | older batch bullets (`build_card` dispatches on `tool == "create_doc"` only) |

`_describe_call` (`api.py:610`) surfaces only `doctype`, `name`, `docname`, `target_doctype`,
`target_name`, `method`, `action`, `recipients`, `to`, `subject`. It has no `user`,
`skill_name` or `slug`, which is why the last four rows above are that thin.

### Two pre-existing defects this spec must fix, not inherit

**A. Model-authored prose already renders on the trust surface.** `create_doc`'s `notes` is a
**tool argument the model writes** (`create_doc.py:87`), echoed verbatim into `would["notes"]`
(`create_doc.py:134`) and rendered by `_batch_create_card` (`confirm_card.py:244-247`) and both
frontends. A prompt-injected agent can render "these records already exist — confirming changes
nothing" above a card that creates 20 rows. Escaped rendering stops XSS, not lying. **This spec
removes `notes` from the card.**

**B. Card doc reads are not permission-checked.** `frappe.get_doc` checks nothing **unless** a
`check_permission` kwarg is passed: `get_doc_str` forwards it into `get_doc_permission_check`
(`frappe/model/document.py:141-145` → `:336-349`), which runs `doc.check_permission("read")`
only when truthy. It defaults to `None`, so the bare call the cards make performs no check. And
`apply_fieldlevel_read_permissions` (`document.py:1248`) only does permlevel `delattr` plus
`mask_fields`. **Both `_update_card` (`confirm_card.py:143`) and `_bulk_update_card`
(`confirm_card.py:203`)** already read docs this way, so a user who cannot read a record can
already see its old values on an update card — single or bulk. **This spec adds a doc-level read
check** rather than spreading the hole to fourteen more shapes.

(Rev 3 named only `_bulk_update_card`; `_update_card` has the identical unchecked read and was
caught while writing the phase-1 plan, after two review rounds had missed it. Both are fixed in
phase 1.)

We use **`doc.has_permission("read")`** (returns a bool) rather than `get_doc`'s
`check_permission` kwarg (which throws): the card must degrade to a header-only row, not raise,
and throwing would conflate "missing" with "unreadable".

Frappe's own `link_preview.py` reads via `frappe.get_list`, which **is** permission-checked —
rev 1 took its field-selection idea and dropped the permission model that came with it.

### Three facts that shape the design

1. **The card never reaches the model.** `api.py:1083` strips `card` out of `model_preview`
   before returning. Richer cards cost **zero tokens**. The only budget is human attention and
   the Redis token / realtime payload — which is the argument for collapsing, not for hiding.

2. **`_fmt` is a raw `str()`** (`confirm_card.py:102`). Today a Currency renders `125000.0`,
   a Check renders `1`, a Link renders the docname. A live defect in cards that already ship.

3. **Tool args are not the write.** Tools normalize. `create_custom_skill` computes a
   `requested` scope and then hardcodes `"scope": "User"` (`create_custom_skill.py:44-57`), so a
   card echoing `args.scope="Org"` would misdescribe its own payload. `share_doc` coerces flags
   via `int(bool(flag))` (`share_doc.py:92-94`), and `bool("false")` is `True`.

## Design decisions

**1. Model chooses, server renders, meta is the floor.** The model may (in round 2) name which
fields a card shows. It must never author the values.

This is a trust boundary, not a preference. The confirmation card is the human's *independent*
check on the agent: the one surface that reports what will happen rather than what the agent
says will happen. If the agent authors the card, the card stops verifying the agent and merely
repeats it. Every other component honours this — `_run_preview` sandbox-executes the real call,
`build_card` reads only from the perm-filtered `would` and the caller's args, and the token
never goes near the model. Defect A above is the one existing violation; it is removed here.

**2. Cards render EFFECTIVE values, not raw args.** Where a card is built from args, it must
show what the tool will actually write after its own normalization — never the raw request. A
card that echoes a value the tool overrides is a card that lies. Concretely: `skill` renders
scope as **"User (private)"** unconditionally; `share` renders flags through the same
`int(bool(...))` the tool applies.

**3. Stored content is selected; proposed content is shown whole.** When summarizing a record
that exists (delete, submit, cancel, amend, workflow, share/assign targets), the doc has 50-200
fields and the floor **selects** the few worth showing — hiding the rest is the point. When
rendering content the caller **proposed** (bulk create's values, an email's body), every key
renders in caller order, capped with an explicit remainder count. Filtering proposed content to
a floor subset would hide a field the caller set, so you could approve a create without seeing a
value it will write.

**Rejected: Frappe's `in_preview` / link-preview field set.** `frappe/desk/link_preview.py` is
gated on `meta.show_preview_popup` and most doctypes leave `in_preview` unset, so it degrades to
mandatory-fields-only anyway. We take its `reqd` fallback idea and skip the `in_preview` lookup —
but we keep its permission model (see B above).

**Known risk, from measured evidence.** PR #329 added `actions` to `get_schema`; a production
transcript showed `get_schema` called **zero times** on the create path, so the hint shipped
unreachable. `summary_fields` shares that failure mode: it is optional, so if the persona does
not mandate it the meta floor is what renders. The floor must be good on its own — it is the
product, not a fallback.

## The primitive

New module `jarvis/chat/_record_summary.py`. `confirm_card.py` is already 291 lines and this
roughly doubles it; the helper holds all the real branching, so it earns its own test surface.

### Caps

```
_MAX_VAL    = 200     # a scalar display value (existing)
_MAX_ROWS   = 20      # records / fields per record / child rows (existing)
_MAX_COLS   = 8       # columns in a rendered child table (a 20-column table is unusable)
_MAX_TABLES = 3       # child tables rendered per record; the rest degrade to "N rows"
_MAX_BODY   = 8_000   # a single long-form body (skill instructions, wiki body, one email)
_MAX_BULK_BODY = 2_000  # per-message body in a bulk-email card (20 x 2k bounds the payload)
```

`_MAX_TABLES` bounds the one dimension the other caps miss. Without it the pathological
batch_create is 20 records × several table fields × 20 rows × ~6 columns × 200 chars ≈ 1MB+.
Not a live problem — realistic P99 is tens of KB and today's `bulk_update` already ships ~160KB
worst case — but the "Bounded" invariant should have no unbounded axis. **Payload landing
verified:** the token is a single pickled Redis key with a 15-min TTL (`pending_confirm.py:93`),
comfortable at MBs; the `action:pending` event goes `frappe.publish_realtime` → Redis pub/sub →
node socketio (`chat/events.py:65-67`, `api.py:1064-1074`) with no server→client rejection
threshold; and the model pays nothing (`card` stripped at `api.py:1083`).

`_MAX_BODY` exists because a 200-char cap on `create_custom_skill.instructions` makes the
approval theater: you would be confirming persistent agent instructions — the canonical
prompt-injection persistence vector — while structurally unable to read them. Long-form bodies
live inside a collapsible, so the cost is payload, not attention.

### `pick_fields(meta) -> list[str]`

Selection only — used by `summary_rows`, never by `values_rows`. Pure given a meta. The **meta
floor**, in order, deduped, capped at 8:

1. `meta.get_title_field()` when it is not `"name"` (`frappe/model/meta.py:371`)
2. `meta.get_list_fields()` — Frappe's list-view set: `["name"]` + `in_list_view` fields
   restricted to `data_fieldtypes` + the title field (`frappe/model/meta.py:359`)
3. `reqd` fields (the record's essence — Frappe's own link-preview fallback)
4. `status` and/or `docstatus` when `meta.is_submittable`
5. `grand_total`, else `total`, when the field exists

`name` is dropped (it is the row header). **Long-text fieldtypes are deprioritized to last**:
`data_fieldtypes` includes `Text Editor`, `Markdown Editor`, `Code`, `HTML Editor`
(`frappe/model/__init__.py:8`), so a 10k-char body field can legitimately be `in_list_view` and
would otherwise win a floor slot over the customer and the total. Safety is handled by `fmt`'s
carve-out; this is about summary quality.

No `requested` parameter. Round 2 adds it as a kwarg — a one-line change — rather than carrying
an unused seam through four phases.

### `summary_rows(doctype, name) -> {"title", "rows"} | None`

For stored records. In order:

1. `frappe.get_doc(doctype, name)` — fresh DB load, **not** `get_cached_doc`, so no cache can
   serve sandbox-mutated state. A `DoesNotExistError` catch must call `frappe.clear_messages()`:
   `frappe.throw` leaves an entry in `message_log` that would otherwise leak into the turn (the
   same latent bug exists in today's `_bulk_update_card` catch).
2. **`doc.has_permission("read")`** — the fix for defect B. On failure return `None`; the caller
   renders the header-only row it already defines for a missing target. Fieldlevel perms are not
   doc-level perms.
3. `doc.apply_fieldlevel_read_permissions()`, then skip any field no longer present
   (permlevel-restricted fields are `delattr`-ed, `document.py:1264`).
4. Skip empty values; mask via `_is_secret` (`confirm_card.py:163`); format via `fmt`.

One `get_doc` per record, callers capped at `_MAX_ROWS`. `has_permission` adds negligible cost.

It returns `{"title": str, "rows": [{"label", "value"}]}` — the title travels **inside** the
return, not as a separate lookup, which is what puts it inside the permission boundary below.

**The header title is part of the perm boundary.** A record's `title` may only be produced by
this function's successful, perm-checked path — it is returned alongside the rows. When
`summary_rows` returns `None`, the caller renders **name only**. Do not source the header from a
separate `frappe.db.get_value(doctype, name, title_field)`: that read is unchecked, and the
title field is typically the customer or party name — i.e. precisely the data being protected.
The obvious implementation of the `{name, title, rows}` shape leaks; this sentence exists to
stop it.

**Administrator caveat:** both `apply_fieldlevel_read_permissions` (`document.py:1250`) and the
permission layer early-return for Administrator. `FrappeTestCase` runs as Administrator, so
neither guard is unit-testable — the same limitation already recorded for `_bulk_update_card`.
The tests assert the call is made (via mock), not its effect.

### `values_rows(meta, values: dict) -> {rows, extra}`

For proposed content. Renders **every** key in `values`, in **caller order** (no reordering —
the card should be comparable against the request), capped at `_MAX_ROWS`, with `extra` =
the number of keys not shown so the card can say "+N more fields". Never filtered to the floor.

Same `fmt` and `_is_secret` masking. No perm filter is possible or needed: the values are the
caller's own args. The single-doc `create` path continues to read from the perm-filtered `would`.

`meta` may be `None` (unknown doctype) — labels fall back to fieldnames and formatting to the
`df=None` path rather than failing the card.

### `table_rows(meta, fieldname, rows) -> {label, count, columns, rows, extra, extra_columns}`

Proposed child tables, rendered as a real table rather than "5 rows". Up to `_MAX_ROWS` rows,
`extra` for the remainder; every cell through `fmt` and `_is_secret`.

**Columns are `in_list_view` order ∪ every key the caller actually set**, capped with
`extra_columns` = "+N more columns". `in_list_view` alone would re-break design decision 3
inside the new primitive: Sales Invoice Item's list-view columns are item/qty/rate/amount, so a
proposed batch that also sets `income_account` or `cost_center` on every row would write them
invisibly. `in_list_view` is a sound **ordering** source; it is not a sufficient **selection**
one for proposed content.

For the single-`create` card, whose rows come from `would` with every default populated, the
union is `in_list_view` ∪ **caller-set** keys — not ∪ all `would` keys, which would be unbounded
and is not what the human is approving.

A proposed key that is not a real child field is dropped by the save (it fails `valid_columns`).
Rendering it as a written value would violate the effective-values invariant, so such keys are
**counted, not rendered as values**.

This exists because "N rows" on a proposed create hides the economically load-bearing content —
a Sales Invoice's line items and amounts. The old model-driven **draft** card already renders
child tables with columns (`actionSummary.js:19-27`), so without this the *gated* card would be
strictly weaker than the *draft* card on exactly the invoice content that motivated this work.

**Stored** summaries keep "N rows": the floor is a summary, and a delete card does not need the
line items to identify the order.

### `fmt(value, df=None, doc=None, limit=_MAX_VAL) -> str`

Replaces `_fmt`. Routes through
`frappe.format_value(value, df=df, doc=doc, translated=False)`
(`frappe/utils/formatters.py:26`) so Currency renders `₹ 1,25,000.00`, Date uses the user
format, Percent gets its sign, and Link resolves to its title when `doc.__link_titles` is
populated (raw docname otherwise — acceptable).

Rules, each load-bearing:

- **`translated=False`.** `format_value` runs `value = frappe._(value)` on **every string
  value** before the fieldtype switch (`formatters.py:59-60`) — docnames, subjects, Data. On a
  non-English session any value colliding with a translation msgid would render as something
  other than what is stored, and a one-sided collision corrupts no-op detection. Select options
  still translate inside their own branch (`formatters.py:142-144`), which is the only
  translation wanted.
- **HTML-emitting fieldtypes bypass `format_value`**: `Text`, `Small Text`, `Long Text`
  (newlines → `<br>`), `Markdown Editor` (→ HTML), `Text Editor` (→ `<div class='ql-snow'>`).
  Both frontends render through **escaped interpolation, never `v-html`**
  (`PendingCard.vue:6`), so these would show literal tags. `HTML Editor` and `Code` fall through
  `format_value` raw, so bypassing them too is equivalent and simpler.
- **`Check` → `"Yes" if cint(value) else "No"`.** `format_value` has no Check branch; it falls
  to `return value` (`formatters.py:146`). Naive truthiness breaks on the string `"0"`: a model
  sending `changes={"disabled": "0"}` against a DB `0` would render from `No` → to `Yes`, a
  **phantom change on a gated card**. `cint` is mandatory.
- **`cstr` the result.** `format_value` returns non-strings on the Check/Data/unknown paths.
- **Lists → `"N rows"`** for stored content (child tables in proposed content go through
  `table_rows`).
- **Truncate at `limit`** after formatting. Callers pass `_MAX_BODY` / `_MAX_BULK_BODY` for
  long-form fields; everything else takes the 200 default.
- **Per-field `try/except → str(value)`.** `format_value` genuinely raises: `get_field_currency`
  does attribute access on the doc (`meta.py:886`) and `get_cached_value` on a bad link value
  can throw (`meta.py:897`); `get_field_precision` asserts (`meta.py:936`). One odd field must
  never blank a card. Note `doc.as_dict()` returns a `frappe._dict` so attribute access works
  in-process; a plain JSON dict must be passed as `doc=None`.

### No-op detection — cast-compare (rev 1 and rev 2 both got this wrong)

Baseline, stated correctly: `_update_card` compares **raw** values (`confirm_card.py:154`); only
`_bulk_update_card` compares **display** forms (`confirm_card.py:216-217`). Rev 1 claimed both
compared display and said to preserve it, which would have been a silent behavior change.

**Display forms must never participate in equality.** `fmt_money` at precision 2 renders both
`100.005` and `100.001` as `100.00`, so a display compare **drops a real change and omits it
from a gated card**. Percent (`flt(value, 2)`) and post-truncation compare are the same class.

Rev 2's "raw first, display fallback on type mismatch" does not fix this, and the hole sits in
the *common* branch: an LLM sends numbers as strings, so `from=100.005` (typed float from the
DB) vs `to="100.001"` (string arg) is a type mismatch → falls to display → both `"100.00"` →
**hidden**. Same for an exchange rate `83.1234` vs `"83.1236"` at display precision 3.

**The rule is cast-compare.** Coerce both sides with the same coercion the save applies, chosen
by DocField type, and compare the cast values — never their display forms:

| Fieldtype | Cast |
|---|---|
| Currency, Float, Percent | `flt` at the **column's** precision, not display precision |
| Int, Check | `cint` |
| Date | `getdate` |
| Datetime | `get_datetime` |
| everything else | `cstr` |

This answers the question the guard actually exists for — *will the save change the stored
value?* — rather than "do the two render the same":

- `flt(100.0) == flt("100")` → no-op (the phantom-diff fix `_bulk_update_card` was given, kept)
- `flt(100.005) != flt("100.001")` → shown (the hiding hole, closed)
- `cint(0) == cint("0")` → no-op
- `cstr(None) == cstr("")` → no-op

A cast failure (garbage string) counts as **changed** — show the row. Garbage bounces pre-card
anyway via `_DRY_RUN_ON_PARK` (`api.py:1033-1039`), so this branch is a safety net, not a path.

## Per-card changes

### Phase 1 — primitive + formatter + the two pre-existing defects

- Add `_record_summary.py` (`pick_fields`, `summary_rows`, `values_rows`, `table_rows`, `fmt`).
- Repoint `_create_card`, `_update_card`, `_bulk_update_card`, `_email_card`, `_method_card`
  from `_fmt` to `fmt`.
- Apply the no-op rule above to both diff cards.
- Add `has_permission("read")` to `_bulk_update_card`'s doc read (**defect B**, pre-existing).
- `create`: render child tables via `table_rows`.
- `update` / `bulk_update`: add the record title to the header (`SO-0001 · Acme Corp`).

Ships a visible improvement plus a real permission fix, with no new card kinds and no frontend
whitelist change.

### Phase 2 — `verb` card (10 shapes; the delete case)

`_verb_card` keeps its sentence as the headline and gains `records: [{name, title, rows}]` via
`summary_rows`, capped at 20 with `extra` for the remainder, rendered in the `bulk_update`
grammar: collapsible rows, first open, 320px scroll, "+N more". A record whose `summary_rows`
returns `None` (unreadable or missing) renders header-only — never an error, never a leak.

A single delete becomes one open row headed `SO-0001 · Acme Corp` with customer, date, grand
total and status inside.

**Verified:** no verb shape renders post-action state. Bulk verbs route to described-intent
(`api.py:665-667`, nothing runs); single submit/cancel/delete/amend go through `_run_preview`,
whose rollback is in a `finally` (`_preview_sandbox.py:32-37`) so it holds even when the tool
raises; `apply_workflow_action` single is not in `_PREVIEWABLE` → described-intent. `build_card`
has exactly one call site, post-rollback (`api.py:1051`), and never runs at resync.

### Phase 3 — `batch_create`

`_batch_create_card` gains per-record `rows` from `values_rows(meta_i, args.docs[i].values)` plus
`table_rows` for child tables. **Removes `notes`** (defect A).

Three implementation caveats:

- **`zip` the two lists; never filter-then-index.** Today's `_batch_create_card` filters
  non-dicts out of `would.created` (`confirm_card.py:241-243`), which would desync the pairing.
- **Meta is per-item** (`args.docs[i].doctype`) — a batch can mix doctypes.
- **The displayed name may shift.** It is a sandbox-consumed series number; a concurrent insert
  can take it before confirm. The card says so in one line.

Alignment is otherwise guaranteed: `run_atomic_batch` appends results in `enumerate` order
(`_bulk.py:61-62`), items are validated up front (`create_doc.py:120-127`), any failure bounces
pre-card via `_DRY_RUN_ON_PARK` (`api.py:1033-1039`), and batch size ≤ 20 = `_MAX_ROWS`.

Add `create_docs` to `build_card`'s dispatch (one line) so the deprecated shim gets the card.

### Phase 4 — the five missing kinds + `_describe_call`

| New kind | Tool | Contents |
|---|---|---|
| `bulk_email` | `send_email(messages=[])` | per-message collapsible; recipients + subject on the header, body inside at `_MAX_BULK_BODY` |
| `share` | `share_doc` single + bulk | grantee (user, or **Everyone**), permission flags as chips rendered through the tool's own `int(bool(...))` coercion, notify, + target `summary_rows` |
| `assign` | `assign_to` single + bulk | assignee, the description **that gets emailed**, priority, date, notify, + target `summary_rows` |
| `skill` | `create_custom_skill` | name, **scope rendered as "User (private)" unconditionally** (the tool hardcodes it, `create_custom_skill.py:57`), user_invocable, description, + instructions body at `_MAX_BODY` in a collapsible |
| `wiki` | `update_wiki` | slug, title, scope, page_type, ref doc, body at `_MAX_BODY`, with `replace_body_md` flagged as a **full replace** vs `append_md` |

`email` (single) also gains **cc, bcc and the attached print format**, and its body budget rises
to `_MAX_BODY`.

`_describe_call` gains `user`, `skill_name`, `slug`, `title`, `scope`. It remains the summary
line above the card and the `action:pending` event's `summary`.

### Frontend (phases 1-4)

Each new kind needs, in lockstep:

1. an entry in `CARD_KINDS` (`frontend/src/lib/actionSummary.js:61`) — **omitting this silently
   rejects the card and falls back to raw**;
2. a branch in desktop `frontend/src/components/PendingCard.vue`;
3. a branch in PWA `pwa/src/components/PendingCard.vue` (mounted by `DecisionSheet.vue`).

Phase 1 also needs the child-table sub-template in both. The PWA reuses desktop helpers via
`@shared` → `frontend/src`, so the whitelist is shared but the templates are not.

**Commit the frontend with plain `git`, matching the existing 2-space / no-semicolon style.**
`pre-commit` runs prettier, which reformats whole files to tabs+semicolons; no CI enforces it.

## Invariants (must hold for every card)

- **Values are server-derived.** From the perm-filtered `would`, a perm-checked + perm-filtered
  doc read, or the caller's own args. **Never model-authored prose** — this is why `notes` goes.
- **Doc-level read permission is checked** on every stored-record read, not just fieldlevel.
- **Effective values, not raw args.** A card never echoes a value the tool will override.
- **No phantom changes, and no hidden real ones.** See the no-op rule.
- **Secrets masked.** `_is_secret` covers Password fieldtypes and
  `password|secret|token|api[_-]?key` names. `data_fieldtypes` includes `Password`, so a Password
  field can legitimately be `in_list_view` and reach the floor — masking is not optional.
- **Escaped rendering.** No `v-html`, ever. (The PWA's one `v-html`, `DecisionSheet.vue:132`, is
  safe: `renderMarkdown` escapes source before markup, `frontend/src/markdown.js:5-7`.)
- **Best-effort.** `build_card` never raises; failure yields `None` and the SPA falls back.
- **Built once at park.** Attached at `api.py:1051`, returned verbatim on resync (F2). Never
  rebuilt — rebuilding re-fires unsandboxed side effects.
- **Bounded, with no unbounded axis.** 20 records, 8 floor fields, 20 rows/record, 3 child
  tables/record, 20 child rows, 200 chars/scalar, 8k/body, 2k/bulk-email body.
- **The header title is inside the perm boundary** — name-only when the record is unreadable.

## Testing

`jarvis/tests/test_confirm_card.py` extends (currently 20 tests). Run against
`jarvis-test.localhost`, not the dev site:

```bash
cd bench && bench --site jarvis-test.localhost run-tests --app jarvis --module jarvis.tests.test_confirm_card
```

- **1** — `pick_fields` floor order, dedupe, cap, long-text deprioritized; `values_rows` renders
  every proposed key in caller order with `extra` (never floor-filtered); `table_rows` columns
  are `in_list_view` ∪ caller-set keys (**a caller-set column outside `in_list_view` must
  render**), `_MAX_TABLES` degrades the rest to "N rows", a non-field key is counted not
  rendered; `fmt` per fieldtype (Currency symbol, **Check with string `"0"` → No**, Date,
  HTML-type bypass, `cstr`, truncation, raising field → `str`); `translated=False` (a value
  colliding with a msgid is not translated); **cast-compare**: `flt(100.0)` vs `"100"` is a
  no-op, `100.005` vs `"100.001"` **renders** (the display-compare hiding case), `cint(0)` vs
  `"0"` is a no-op, `cstr(None)` vs `""` is a no-op, an uncastable value renders;
  `has_permission` is called on the `bulk_update` doc read.
- **2** — `verb` card carries `records`; cap + `extra`; an unreadable target degrades to
  **name-only** — no `title`, no field, and no second unchecked read for the title;
  unreadable and missing render identically (no existence oracle).
- **3** — `batch_create` rows come from args; `zip` alignment survives a non-dict in
  `would.created`; per-item meta on a mixed-doctype batch; **`notes` absent from the card**.
- **4** — one test per new kind; `share` renders flags through the tool's coercion; `assign`
  renders the description; **`skill` renders scope "User (private)" even when args say "Org"**;
  bodies honour `_MAX_BODY`; secret masking on each; **`CARD_KINDS` parity** (every kind
  `build_card` can emit is whitelisted).

No frontend test framework exists; the Vue branches are verified by manual smoke (desktop +
PWA), which is the standing gap on this subsystem.

Beware `frappe`'s FS-order test discovery flake (`test_settings` admin-dispatch is a known
pre-existing order-dependent failure that adding a test file can trip). Prove any red is
pre-existing by stashing and re-running on `origin/main` before diagnosing.

## Decomposition

Four phases, one PR each. Phases 2-4 depend on phase 1's primitive; 2, 3 and 4 are independent
of each other afterwards. Phase 1 alone is shippable and user-visible (formatter + the
permission fix), which de-risks the primitive against real cards before four kinds depend on it.

## Round 2 (deferred)

`summary_fields` — model chooses the fields:

- `_run_tool` **pops** `summary_fields` from args before dispatch and passes it to `build_card`.
  It is presentational, not a tool concern; the tools would reject an unknown kwarg; and the
  token must store the cleaned args so confirm dispatches unchanged.
- `pick_fields` gains a `requested` kwarg (keep the ones `meta.get_field` resolves, drop the
  rest — never an error).
- Plugin descriptors + typebox schemas gain the param; persona guidance so it is actually passed.
- Requires a plugin build + container roll; phases 1-4 do not.

## Open questions

1. **Park→confirm staleness (unresolved, flagged deliberately).** Phase 2 puts decision weight
   on values snapshotted at park; the doc can change inside the 15-minute TTL and confirm does
   not re-verify. Storing `doc.modified` at park and refusing or flagging on mismatch at confirm
   is cheap optimistic concurrency. **Not in this spec** — it changes `confirm_tool`, not the
   cards, and deserves its own decision. Recorded so it is not mistaken for an oversight.
2. Floor capped at **8** fields — chosen to fill a collapsed row without scrolling; unverified
   against a wide doctype.
3. `update_wiki`'s `replace_body_md` shows the new body flagged as a replace, **not** a diff
   against the current body. A diff is the better card; it needs the current body loaded.
   Deferred.

## What rev 1 got wrong

Fable's review, all eight confirmed; the first three were blocking. Three were independently
verified against source before acceptance (`get_doc` perm, `notes` provenance, skill scope).

1. **`summary_rows` never checked doc-level read permission** — `get_doc` checks nothing and
   fieldlevel perms are not doc perms. Would have spread a pre-existing hole to 14 shapes.
2. **Kept `notes`** — model-authored prose on the trust surface, three sections after declaring
   values must never be model-authored.
3. **200-char cap on `create_custom_skill.instructions`** — rubber-stamp approval of a
   prompt-injection persistence vector.
4. **`translated=True`** — translates data values, not just labels.
5. **Check → Yes/No without `cint`** — string `"0"` renders "Yes" and fabricates a diff.
6. **Claimed `_update_card` compares display forms** — it compares raw. "Preserve it" was
   actually specifying a silent behavior change; and display comparison can hide a real change
   through rounding.
7. **Cards echoing raw args** — `create_custom_skill` hardcodes scope, so the card would lie.
8. **`values_rows` capped at 20 with no remainder indicator** — silently hid proposed fields,
   the exact defect its own rule forbids.

Also missed entirely: proposed child-table content (the gated card was weaker than the draft
card on invoice line items), long-text fieldtypes winning floor slots, park→confirm staleness,
and the Problem section said "five" shapes where the verified table says eight.

Cut as YAGNI: `pick_fields`'s unused `requested` seam; floor-first reordering in `values_rows`.

## What rev 2 got wrong

Second Fable round: **ship after five text corrections**, all applied.

1. **The no-op rule still hid real changes — in its common branch.** "Raw first, display
   fallback on type mismatch" leaves the hiding hole inside the type-mismatch branch, and type
   mismatch is the *normal* case because LLMs send numbers as strings. Replaced with
   cast-compare. Notably this was Fable's own rev-1 suggestion, which it retracted on attack —
   the reason a fix gets re-reviewed rather than assumed correct.
2. **`table_rows` selected proposed child columns by `in_list_view`** — reintroducing rev 1's
   finding 8 inside the new primitive. A batch setting `income_account` on every line would
   have written it invisibly. Columns are now `in_list_view` ∪ caller-set keys.
3. **The `{name, title, rows}` header left `title`'s source unpinned**, and the obvious
   implementation (`db.get_value(title_field)`) is an unchecked read of exactly the protected
   data. Pinned to name-only on an unreadable record.
4. **The `get_doc` citation was false.** `get_doc_str` *does* forward a `check_permission` kwarg
   (`document.py:141-145` → `:336-349`); it defaults to `None`, so the conclusion held but the
   evidence did not. Verified and corrected. (The window I read stopped one line short of the
   forwarding call — the failure mode of citing from a partial read.)
5. **No cap on child tables per record** — the one unbounded axis in a "Bounded" invariant.

Nit also applied: `summary_rows`' `DoesNotExistError` catch clears `message_log`.

Confirmed clean on the second pass: `has_permission` on `_bulk_update_card`, skill
"User (private)", share flags through the tool's coercion, `translated=False`, `cint` Check,
`values_rows` shape, both YAGNI cuts, the staleness deferral, and the phase-2/3 park-state
verification (independently re-traced and matching). Payload sizing verified as a non-issue.
