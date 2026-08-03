# List filters: declared deviations from Frappe parity

Plan 08 aims at Frappe-parity list filtering. Where we deliberately differ, the
difference is written down HERE, with its rationale, so that:

* plan §11.5's golden matrix (derived from the pinned Frappe `FieldSelect` /
  `filter.js` behaviour) is not permanently red — it asserts parity *except* for
  this list;
* a Frappe upgrade that changes one of these behaviours forces a decision
  instead of silently drifting;
* reviewers have one place to argue with, rather than reading the compiler.

Reference: Frappe v16 @ `9a8daf343db69a0127f470bad8be0af192cd80c8`
(`frappe/public/js/frappe/ui/filters/filter.js`,
`.../filters/field_select.js`, `frappe/model/model.js`,
`frappe/model/db_query.py`). Implementation: `jarvis/chat/list_filters.py`.

---

## D1 — Permlevel IS enforced on filter fields (security; stricter)

**Frappe:** field-level permlevel is enforced on *reads* (`get_permitted_fields`)
but **not on filters**. `frappe.get_list(dt, filters={"secret": ["like", "a%"]})`
runs for a user who may not read `secret`, so any list becomes a
character-by-character oracle over a field the caller cannot see.

**Jarvis:** a field whose `permlevel` is not in the caller's readable set is
absent from the schema *and* rejected by the compiler.

**Why:** in this codebase the hole reaches the agents IP moat —
`Jarvis Agent Listing.skill_bundle` is permlevel 1, readable only by System
Manager, and it holds the bundle rules that are the product's moat. A plain
Jarvis User could otherwise reconstruct it through filter responses. Judgment
C08-1 makes this binding, and it is why the compiler may never be replaced by
"just pass the filters to the ORM".

**Note on error shape:** an above-permlevel field returns the *same*
`list_filter_unknown_field` error as a misspelled one. Distinguishing
"exists but you may not read it" from "does not exist" is itself the oracle.

**Tested:** `test_list_filters.py` — two roles, both directions, on a real
permlevel-1 field (main *and* child, since child permlevel access is evaluated
against the parent's permissions and can therefore be wrong independently).

### D1-a — permlevel access is `if_owner`-blind (inherited limitation)

Frappe's `Meta.get_permlevel_access()` checks only `perm.role in roles and
perm.read`. It **ignores `if_owner`**. So a DocPerm pair that means "you may read
your own row, and these extra columns on it" reads, to the permlevel machinery,
as "this role has level-1 access to the whole table".

The live example is `Jarvis User Settings`:

| role | permlevel | read | if_owner |
|---|---:|---:|---|
| Jarvis User | 0 | 1 | **1** |
| Jarvis User | 1 | 1 | — |

Intent: a user sees their own usage counters. Effect on the *catalog*: a plain
Jarvis User gets every permlevel-1 field (`month_tokens`, `monthly_token_limit`,
`total_tokens`, …) offered as filterable. The floor-role audit in
`test_list_registry.py` prints exactly this (`settings_user_usage_admin
floor_main=27` for a Jarvis User).

That is **not** currently a leak, because `admin_list_user_usage` is
`require_jarvis_admin()`-gated and the view is `PENDING`. It becomes one the
moment a view over that doctype is migrated with an owner-scoped SQL predicate,
because row scoping (`if_owner`) and column scoping (permlevel) are enforced by
two different mechanisms and only one of them is in the catalog.

We do **not** try to out-think Frappe here (re-deriving `if_owner` semantics into
the permlevel calculation would diverge from every other permlevel consumer in
the framework). Instead the risk is handled structurally, by the migration
invariant below and by `excluded_fields` (D1-b).

### D1-b — `excluded_fields`: what the ENDPOINT hides

Permlevel stops a field the *DocType* hides. It cannot stop a field the
*endpoint* hides. `Jarvis Trigger.condition`, `.script_body` and
`.llm_instruction` are permlevel **0** — every reader of the doctype may read
them — yet `triggers_api._trigger_detail` blanks all three for non-managers. A
filter over them would rebuild the redacted automation logic one `LIKE` at a
time, and no permlevel check would ever notice.

So a registered view declares `excluded_fields`, and those fields are absent from
the schema and rejected by the compiler with the same
`list_filter_unknown_field`. Triggers declares its three today, while still
`PENDING`, so the guard is in place before the surface can be flipped.

Withholding is **unconditional** — a manager loses filterability on those three
as well. A per-role exclusion would need the view to declare a predicate, which
is deliberately deferred: the conservative direction costs a manager one filter,
the permissive direction costs the customer their automation logic.

### The migration invariant (both of the above, as one rule)

> A view may be flipped to `MIGRATED` only if
> **(1)** its SQL scope is a subset of the root DocType's ORM read scope for
> every role that can call it, and
> **(2)** everything its projection withholds from the rows it returns appears in
> `excluded_fields`.

(1) is the answer to D1-a: the catalog is derived from doctype-level permissions,
so if the endpoint's hand-written `WHERE` is *narrower* than the doctype's own
read rule the schema is safe, and if it is *wider* the schema is a licence to
read rows the ORM would have refused. (2) is the answer to D1-b. Neither is
machine-checkable in general, which is precisely why it is written down and why
the checklist at the end of this file makes someone assert it per surface.

---

## D2 — Password fields are excluded entirely

**Frappe:** offers Password in the field picker with a restricted operator set
(`invalid_condition_map["Password"]` removes Between/Timespan/comparisons/in).

**Jarvis:** Password is not selectable at all.

**Why:** the restricted set still leaves `=`, `!=`, `like`, `not like` and `is`.
`like` over a stored secret is a brute-force oracle with a page-count side
channel. Plan §1/§9 already forbid exposing secret-bearing fields "through
schema, search, error messages, query logs, or telemetry"; excluding the field is
the only way to honour that.

---

## D3 — `_assign` / `_liked_by` are restricted to the `like` family

**Frappe:** defaults them to `like` (`get_default_condition` special-cases both,
because they are stored as JSON arrays), but still offers `=`, `!=`, `in`,
`not in`, `is`, and the comparison operators through the Data map.

**Jarvis:** operators are `like`, `not like`, `is`. Default `like`. The schema
entry carries `"json_array": true` so the UI can label the control honestly.

**Why:** the column holds `["a@x.com","b@x.com"]`. `=` can only match when
exactly one user is assigned *and* the client happens to send the JSON
representation — i.e. it is a promise the data shape cannot keep. C08-2 required
"like semantics or don't ship them"; this is the like-semantics option.

**Related, NOT deviated:** `_comments` is also a JSON blob, and Frappe treats it
as plain `Text` — so Frappe's default on it is `=`, which can essentially never
match. We do not extend D3's operator restriction to it (the parity surface stays
small), but D5-a moves `Text` to a `like` default, which incidentally makes
`_comments` behave sensibly. Its full operator set is still Frappe's, so an
equality filter remains selectable and remains useless.

---

## D4 — Child-table filters compile to one `EXISTS` per child DocType

**Frappe:** `db_query` LEFT JOINs the child table
(`... join tabChild on (tabChild.parenttype = 'Parent' and tabChild.parent = tabParent.name)`)
and then compensates for the resulting duplicate parent rows in its count paths.

**Jarvis:** one `EXISTS (SELECT 1 FROM tabChild ... )` subquery per child DocType.

**Why:** the join duplicates parent rows, which corrupts `total`, `has_more` and
OFFSET pagination for every list in this app — all of which return a frozen
`{rows, total, has_more, start, page_length}` envelope that the SPA trusts. Plan
§4.3 and §9 both require correct totals under child filters.

**Multi-clause semantics — decided (C08-2):** all clauses on the *same* child
DocType go inside ONE `EXISTS`, ANDed together. That preserves Frappe's meaning:
with a single join, two conditions on one child table must be satisfied by the
SAME child row. Two clauses on *different* child DocTypes produce two independent
`EXISTS` subqueries, which is also Frappe's meaning (two joins).

**Known ambiguity, at parity:** the subquery matches on `parent` + `parenttype`
only — not `parentfield` — exactly as Frappe's join does. If one child DocType is
ever referenced by two `Table` fields of the same parent, a filter matches rows in
either. The schema likewise identifies a child field by (DocType, fieldname), as
Frappe does, so the ambiguity is inherited rather than introduced. No current
Jarvis DocType does this.

---

## D5 — Default operator follows Frappe's code, not plan §5.1's table

Plan §5.1 says "Data **and user-facing text-like fields** → `like`". Frappe's
`get_default_condition` (filter.js:516-529) is narrower:

| Field | Plan §5.1 default | Frappe default | Jarvis |
|---|---|---|---|
| `Data` (small table) | `like` | `like` | `like` |
| `Data` (large table) | `like` | `=` | `=` |
| `Small Text`, `Long Text`, `Text`, `Text Editor` | `like` | `=` | `=` |
| `Attach`, `Attach Image`, `Phone`, `Barcode`, `Color`, `Code` | `like` | `=` | `=` |
| `Date`, `Datetime` | `Between` | `Between` | `Between` |
| `_assign`, `_liked_by` | (not covered) | `like` | `like` |
| everything else | `=` | `=` | `=` |

**Jarvis follows Frappe except for D5-a below.** `like` remains *selectable* on
every one of those families (it is only the default that differs), which is what
plan §5.1's own closing sentence requires. The large-table rule is honoured via
Frappe's own `Meta.check_if_large_table` heuristic and reported to the client as
`is_large_table` so the UI can explain the changed default.

## D5-a — free-text bodies default to `like` (deliberate divergence)

`Small Text`, `Long Text`, `Text` and `Text Editor` default to **`like`**, not
Frappe's `=`.

**Why.** These four are where a human types prose: `description`,
`instructions`, `question`, `review_note`. Frappe's `=` on them means a user who
types three words into a Description filter gets zero results and no explanation
— the control looks broken, and the recovery (open the operator menu, discover
"Like", re-run) is a step most people will not take. Frappe's `like` default for
`Data` exists for exactly this reason; the families it omits are the ones where
the argument is *stronger*, not weaker.

**Why not extend it further.** `Code`, `HTML Editor` and `Markdown Editor` stay
at `=`: their content is machine-shaped, and a substring match across a stored
script is both a worse question and a more expensive scan. `Data` keeps Frappe's
rule intact, including the large-table downgrade to `=`.

**Cost.** `like '%x%'` cannot use an index — but there is no cost *basis* for
treating these four as large-table risks: Frappe's own `is_large_table` heuristic
is table-level, not column-level, and none of these columns is indexed under
either default, so the query plan is a scan either way. If a surface ever proves
otherwise, the escape hatch already exists (`is_large_table` is reported in the
schema and `=` stays selectable).

---

## D6 — `Select` values are validated against metadata options

**Frappe:** does not validate; an unknown Select value simply matches nothing.

**Jarvis:** `=`, `!=`, `in`, `not in` on a Select whose metadata carries options
reject a value that is not one of them (`list_filter_invalid_value`).

**Why:** it is free, it makes a client bug loud instead of silently empty, and it
matches what the existing curated endpoints already do (`Invalid scope filter.`,
`Invalid schedule_frequency filter.`). Skipped when options are absent or
dynamically supplied (`link:` form).

---

## D7 — Nested-set operators are not offered yet

**Frappe:** offers `descendants of`, `descendants of (inclusive)`,
`not descendants of`, `ancestors of`, `not ancestors of` for a Link whose target
DocType is a tree.

**Jarvis:** not in `CONDITIONS` at all in Phase 1.

**Why:** no Jarvis list has a Link to a nested-set DocType today (the pilots'
Links target `Role` and `User`). Advertising an operator the compiler cannot
honour would be a schema that lies; omitting it keeps the contract truthful and
fails closed. They arrive with the first surface that has a tree Link, compiled
the way `db_query` does it (resolve `lft`/`rgt` to a name set, then `in`).

---

## D8 — Unknown / optional fields fail CLOSED

**Frappe:** `db_query` *silently drops* filters on `OPTIONAL_FIELDS`
(`_user_tags`, `_comments`, `_assign`, `_liked_by`) when the column does not
exist on the table — the query then returns a WIDER result set than the user
asked for, with no indication.

**Jarvis:** an optional field with no column is not in the schema, and a clause
naming a field that is not in the schema is rejected with a stable code. Nothing
is ever stripped.

**Why:** silently widening a filtered list is a correctness bug in general and a
disclosure bug on a list whose scope the user believes they narrowed. C08-2 makes
fail-closed binding.

---

## D9 — An empty `in` list is rejected

**Frappe:** compiles an empty `in` to `IN ('')`, i.e. "matches rows whose value is
the empty string".

**Jarvis:** `list_filter_invalid_value`.

**Why:** nobody means that. It is a client bug, and Frappe's rendering of it is a
silent wrong answer.

---

## D10 — Multi-value input is a real list, not a comma string

**Frappe's** Link control submits `in`/`not in` values as a comma-joined string,
which `db_query` splits on `,` — so a value containing a comma is silently split
into two.

**Jarvis** accepts a JSON array (canonical) and still tolerates a comma string for
compatibility, but the schema/contract is a list, capped at 100 entries. Plan §5.3
requires multi-value selection that does not "degrade to a raw comma string".

---

## D11 — Caps on client input (Frappe has none)

20 clauses, 100 values per `in`, 1,000 characters per scalar value, 100 rows per
page (plan §9's suggested initial limits).

**Server-authored `IN` lists are exempt** (C08-5): the skills list binds "every
skill shared with me" as an `IN` tuple. That set is derived server-side from the
caller's grants, is not attacker-controlled, and is legitimately unbounded. The
cap exists to bound request payloads, and the structural split in
`ListFilterQuery` is what makes the exemption expressible without a hole — server
predicates and client clauses are separate stores, so "exempt" is a property of
where the SQL came from, not of a flag someone can pass.

---

## D12 — Facet WHERE is "identical minus the facet's own dimension"

Plan §6.3.9 says rows, total, facets and `has_more` share an *identical* WHERE.
Taken literally that breaks two shipped UIs: the Approvals `document_type` facet
and the Learning `domain` facet deliberately drop their own filter so the tab
strip stays populated when you click a tab.

**Jarvis** implements standard faceting: rows / total / `has_more` share one
WHERE; a facet over dimension *d* uses the identical WHERE **minus the clauses on
*d***. `CompiledFilters.fragment(exclude_dimension=...)` is that operation, and it
re-groups the child `EXISTS` after exclusion so D4's semantics survive.
Judgment C08-3.

---

## D13 — `like` clause values keep the user's wildcards; the `search` box does not

The repo's `escape_like()` (the old `_lk`) escapes `%` and `_` — correct for a
free-text *search* box, which is not a query language.

A `like` **filter clause** is the query language: Jarvis matches Frappe's UI
(`filter.js` `get_selected_value`) and wraps the term in `%...%` unless the user
already supplied a wildcard, leaving any `%`/`_` they typed as wildcards.
Backslashes are doubled so a literal `\` survives LIKE's own escape processing,
exactly as `db_query` does.

Both behaviours ship, on different parameters (`search` vs `filters_v2`). This is
recorded because the asymmetry looks like a bug until you know it is not.

---

## Non-deviations worth stating

* **Hidden / not-in-list-view fields are filterable.** Frappe filters on readable
  metadata, not on the visible columns. Plan §4.2.
* **A field the fixed scope makes constant stays in the picker** (e.g. `owner` on
  an owner-scoped list). Plan §4.1.
* **`docstatus` appears only for submittable DocTypes**, matching
  `FieldSelect.add_field_option`.
* **`Table MultiSelect` exposes only its Link value field**, matching
  `FieldSelect.build_options`.

### `ifnull(...)` null-handling: mirrored, not copied

An earlier draft of this file claimed the null handling was "copied from
`db_query.prepare_filter_condition`". That was overstated, and the correction
matters because a reader would otherwise assume parity in branches we never
reproduced. What is actually mirrored:

| Rule | Source | Ours |
|---|---|---|
| `can_be_null` is False for `name` / `creation` / `modified` | db_query | same |
| `can_be_null` is False for Check/Int/Float/Currency/Percent | db_query | same |
| `can_be_null` is False for `>` / `>=` on Date/Datetime | db_query | same |
| `can_be_null` is False when the value is truthy and the op is `=` or `like` | db_query | same |
| `is set` → `ifnull(col,'') != ''`; `is not set` → `= ''` | db_query | same |
| `in` does **not** coalesce (non-empty list, no falsy member) | db_query | same |
| `not in` **does** coalesce | db_query | same |
| per-family fallback literal (`0`, `''`, `'0001-01-01'`, …) | db_query | same |

What is **not** reproduced: db_query's `not_nullable` DocField flag, its
`Column`-valued comparisons, its `previous`/`next` operators, its
`additional_filters_config` hook operators, and its index-friendly rewrite of
`ifnull(col, fb) = fb` into `(col IS NULL OR col = fb)`. None of those are
reachable through this contract today; if one becomes reachable it gets a row in
this table or a deviation of its own.

## D14 — a blank numeric filter means `= 0` (Frappe parity, stated)

`flt("")` and `cint("")` are `0`, so an `Int`/`Float`/`Currency`/`Percent` filter
submitted with an empty value compiles to `= 0` rather than being rejected. That
is what `db_query` does (`value = flt(f.value)`), and it is left at parity.

Concretely, `_as_number` returns `0` for **both** a blank/whitespace string
(`""`, `"  "`) **and** `None` (`if value is None or (isinstance(value, str) and
not value.strip()): return 0`). This is deliberate parity, not an oversight: an
earlier review expected `None`/`""` to be *rejected*, but `flt(None)` is `0.0`
just as `flt("")` is, so rejecting them would be the deviation — the code is
right, and it says `0` for the empty numeric on purpose.

The temporal families are **not** left at parity. Frappe's date helpers open with
`if not string_date:` and return *today/now*, so **falsiness is the only thing
they check** — `None`, `""`, `0`, `False`, `[]` and `{}` all mean "today" to
`getdate()`. A blank or malformed date bound would therefore return a
plausible-looking answer to a question nobody asked. Every one of those is
rejected with `list_filter_invalid_value`.

The asymmetry with numerics is deliberate: `= 0` is visibly odd in the result
set; "everything from today" is not.

### D14-a — `Between` requires both bounds (no `nowdate()` fallback)

`db_query.get_between_date_filter` documents its own fallback: *"If any of filter
part (to or from) are missing then start or end of current day is assumed."* We
do not inherit it. A `Between` with a missing, blank or falsy bound —
`None`, `""`, `[]`, `["", ""]`, `[None, None]`, `{}`, a one-element list, or a
pair with one blank end — is rejected.

This is the deviation that matters most in practice, because **`Between` is the
*default* operator for Date and Datetime** (D5 / `get_default_condition`). It is
the first thing a client hits on those families, so Frappe's fallback is not an
edge case here — it is the common path, and it silently narrows a range the user
believed was open-ended to a single day.

`Timespan` is unaffected: it feeds `_between_bounds` a server-computed
two-element range, which is why the tests assert it still resolves.

### D14-b — malformed / non-finite numerics fail closed (round-2 P1-01)

D14 keeps *blank* → `0`, but `flt`/`cint` also silently turn `"abc"` into `0` and
accept `inf`/`nan`. Those are NOT at parity: a nonblank value that is not a finite
number is rejected with `list_filter_invalid_value` (`_as_number` parses with
`float()` and checks `math.isfinite`), so a hand-edited URL or direct API call
cannot answer a different question from the one asked, and a non-finite value can
never reach the DB binder as a driver error outside the stable code. `Int`/
`Duration` truncate a *finite* fractional value toward zero (`"3.7"` → `3`,
`int(float(...))`, matching `cint`) — documented, not silent.

**Thousands separators (S10).** `frappe.utils.flt` strips commas before parsing
(`s.replace(",", "")`), so a human-shaped `"10,500.50"` is a valid number in
Frappe. `_as_number` now does the same strip BEFORE the strict `float()` +
`isfinite` check, and — critically — binds the SAME stripped value it validated
(one parser for validate and bind), so the number the DB sees is provably the
number that passed the finite check. `"abc"`, `"1,2,a"` and non-finite values are
still rejected after the strip; the comma strip widens acceptance to real numbers,
it does not open a hole.

## D15 — Long Text inherits Data's operator set (effective-Data, stricter)

Frappe's `Filter.set_fieldtype` scrubs string-like controls (Text, Small Text,
Text Editor, Attach, Attach Image, Tag, Phone, JSON, Comments, Barcode, Dynamic
Link, Read Only, Assign, Color) to an effective `Data` value control, and
`hide_invalid_conditions` then picks
`invalid_condition_map[original_type] || invalid_condition_map[effective]`. A
type with no row of its own therefore inherits Data's `(Between, Timespan)`
invalid set. The original implementation looked at the original type only
(round-2 P0-03), so Small Text / Text / JSON / Dynamic Link advertised `Between`
and the frontend rendered a **date range over a text column**. `invalid_conditions()`
now models the fallback, and the golden matrix in `test_list_filters_f8` asserts
`Between`/`Timespan` are absent at schema AND rejected by the compiler for every
converted type. `_comments` (a standard `Text` field) is covered by the same rule.

`Long Text` is a **deliberate Jarvis addition** to the effective-Data set: Frappe
neither scrubs it nor gives it a map row, i.e. it offers `Between` on a prose body
— the exact non-temporal-date-range nonsense this fix removes. Types that DO have
their own row (Code, HTML Editor, Markdown Editor, Color, Password) keep it
(`original_type` wins) and are unaffected.

## D16 — child `EXISTS` is scoped to the containers the caller may read (M2)

When a parent reaches ONE child DocType through TWO Table fields, the catalog
lists each child field ONCE (deduped by `(child DocType, fieldname)` via `seen`),
and a clause on that child compiles to a single `EXISTS` keyed by the child
DocType (D4). That `EXISTS` now binds
`parentfield IN (<containers this caller may read>)`, computed at schema build
from the readable containers for that child DocType and carried on the entry as
`parentfields`.

This CLOSES a container-permission bypass (round-2 M2). The old `EXISTS` matched
`parent = ... AND parenttype = ...` only, so it matched **any** row of the child
DocType attached to the parent — including rows under a *sibling* Table field the
caller cannot read (its own permlevel raised, or the endpoint withholding it via
`excluded_fields`). Because the same child DocType, same `parent` and same
`parenttype` are shared across containers and told apart only by `parentfield`,
a floor user filtering through a readable container leaked rows attached to the
hidden one, character by character. Binding `parentfield IN (readable containers)`
removes exactly those rows.

The same-row semantics survive: a matching child row has ONE `parentfield`, so two
clauses inside the one `EXISTS` are still satisfied by the SAME row under the SAME
readable container. The both-readable case is unchanged — a System Manager (or any
role that reads every container) gets every container in the bound set and keeps
matching a row under either. Verified by `TestContainerParentfieldLeak`
(behavioural leak + floor-user bind + privileged both-container + view-exclusion)
and `TestMultiSelectContainerParentfieldScoping` (the Table MultiSelect
equivalent, whose binding is keyed on the container fieldname identically).

## Runtime capability flag + telemetry (round-2 P1-05)

- **Per-view flag (default ON).** `view_filters_enabled(view_key)` reads the
  opt-OUT list `jarvis_list_filters_v2_off` from `site_config.json`
  (`frappe.conf`, the admission-flag pattern). A migrated view is live unless
  listed, so the switch is only ever a rollback. OFF makes `get_list_filter_schema`
  decline (the client degrades to legacy transport with the Filter action still
  visible). The COMPILE path deliberately does NOT consult the flag: a clause a
  stale client still sends is answered or coded, never silently dropped
  (do-not-regress rule 8). `list_filters_capabilities()` exposes the
  `{view_key: enabled}` map so a surface can pick transport at mount.
- **The kill switch fails CLOSED (M1).** The valid shapes for
  `jarvis_list_filters_v2_off` are a list of view-key strings, a single string, or
  absent/empty. ANYTHING else — a bool (`true`), an int, a dict, a list with a
  non-string member — is a misconfiguration, and a rollback switch must never fail
  open on one. `_disabled_views` used to raise inside its set comprehension on a
  bool and fall through to `set()` (silently re-enabling every view the operator
  meant to disable); it now disables EVERY migrated view and writes one deduped,
  loud `frappe.log_error`. Over-disabling is the safe direction; a typo can never
  silently re-enable.
- **Rolled-back ≠ never-migrated (M1).** `get_list_filter_schema` distinguishes a
  MIGRATED view rolled back by the flag (`list_filter_view_rolled_back` —
  "Filters are turned off for this list", no client retry) from a genuinely
  unmigrated one (`list_filter_view_not_filterable` — "not supported yet"). The two
  used to share one code and message, so a shared link's filters vanished behind an
  active-looking badge with no honest notice; the client now tells the truth about
  which case it is in.
- **Structural telemetry.** `emit_filter_telemetry` logs, through a dedicated
  INFO logger (the `latency.py` pattern), one line per compiled request: view,
  clause count, whether a child clause was used, the per-clause `{fieldtype,
  operator}` shape, a duration BUCKET, and — on rejection — the stable error
  code. It NEVER logs a filter value (do-not-regress rule 4). Query-level
  duration/row telemetry at each endpoint is deferred (ledgered) — the compile
  boundary is the one place that already sees every migrated request.

## `schema_revision` hashes the full contract (round-2 P1-10)

`_schema_digest` now hashes every field key the wire carries (doctype, fieldname,
label, fieldtype, options, default_operator, operators, standard/child/json_array
markers) plus the limits block — not the earlier five-key subset — so a relabel,
a Select-option edit, a Link-target change or a limits change all change the
public `schema_revision`. A caching/ETag consumer can trust it as a real revision.

## Discovery widening (round-2 P0-04)

The Phase-0 completeness sweep (`test_list_registry._whitelisted_list_callables`)
walks `jarvis.chat` AND `jarvis.support`, matches the `list_*` **and**
`admin_list_*` naming families (so `jarvis.support.api.list_tickets` and
`user_settings_api.admin_list_user_usage` are visible), and honours the
`list_registry.list_surface_endpoint` annotation for any differently-named
collection endpoint. Inventory / migration / wiring are asserted separately; the
"no pending active view" gate stays deferred to the final round — this round the
audit REPORTS the migrated-vs-pending split and fails only on an UNCLASSIFIED
collection.

---

# MIGRATION-CHECKLIST

Work through this before flipping a registered view's `status` to `MIGRATED`.
Most of it is not machine-checkable, which is why it is a list a person signs off
rather than a test.

## 1. Prove the scope invariant (D1 / D1-a)

- [ ] Write down the endpoint's fixed `WHERE`, and the root DocType's DocPerm
      rows (role, permlevel, `read`, `if_owner`).
- [ ] Assert **SQL scope ⊆ ORM read scope** for every role that can call it. If
      the endpoint is *wider* than the doctype's own read rule — a reviewer-gated
      raw-SQL list that deliberately sidesteps `if_owner`, e.g.
      `list_skill_promotion_requests` — the catalog is derived from the wrong
      authority and the view is **not** ready to migrate.
- [ ] Check the DocPerm table for the D1-a shape: a permlevel-0 row with
      `if_owner=1` **plus** a permlevel-N row without it. If present, every
      permlevel-N field is catalog-visible to that role regardless of ownership.

## 2. Run the per-role catalog diff

- [ ] `build_field_catalog(root, user=<floor role user>, view=view)` vs
      `user="Administrator"`. The floor number is what most staff will see.
- [ ] Reconcile every field in the diff: each one is either genuinely
      privileged (fine) or a permlevel that does not mean what its author thought
      (fix the doctype, not the filter layer).
- [ ] Confirm every **current curated filter key** survives at the floor role.
      If it does not, migrating silently removes a filter ordinary users have
      today. (`test_list_registry.test_floor_role_catalog` asserts this.)
- [ ] **The Approvals trap:** `Jarvis Approval Request.status` is **permlevel 1**,
      and it works today only because the doctype ships explicit permlevel-1
      DocPerm rows for `Jarvis User` and `System Manager`. Anyone "tidying up"
      those rows removes `status` from the schema and breaks the board. Do not
      remove them; do check they are still there at migration time.

## 3. Declare what the projection withholds (D1-b)

- [ ] List every field the endpoint blanks, redacts, or omits from its row
      payload for *any* role.
- [ ] Put each one in `excluded_fields` (`fieldname`, or
      `"Child DocType.fieldname"`).
- [ ] `test_list_registry.test_excluded_fields_name_real_fields` catches typos;
      nothing catches an omission, so this step is the control.

## 4. Wire the endpoint

- [ ] Additive, **type-annotated** `filters_v2: str | list | None = None`
      (`require_type_annotated_api_methods` is on; an un-annotated param 500s).
- [ ] Keep the legacy `filters` argument for the compatibility window.
- [ ] Decorate: `@frappe.whitelist()` → `@require_jarvis_user` →
      `@filter_errors_to_envelope`, in that order.
- [ ] Move the fixed predicate onto `ListFilterQuery.server_condition(...)`:
      named placeholders only (no `%s`), no parameter name starting with `jf`
      (that namespace belongs to compiled user values), and values passed as
      kwargs — never interpolated into the SQL string.
- [ ] Server-authored `IN` lists stay on the server side, where the §9 client
      caps do not apply.
- [ ] Rows, `total` and `has_more` all use `q.where()`.
- [ ] **Pin the SUCCESS envelope shape and do not change it.** The pilots
      (`list_custom_skills_page`, `list_macros_page`) return the bare
      `{rows, total, has_more, start, page_length}` dict, which is what
      `useListPage` reads directly — only the *error* path is an envelope.
      **Exception to reconcile:** `list_triggers_page`, `list_activity_page` and
      `list_dashboards_page` already return `{ok: true, data: {...}}`. Those
      surfaces keep their existing success shape at migration (changing it is a
      client break unrelated to filters); just make sure the error envelope the
      boundary returns is distinguishable from their success one — it is, since
      `ok` is `false` and there is no `data` key.

## 5. Facets (D12)

- [ ] A facet over dimension *d* uses `q.where(exclude_dimension=(doctype, d))`,
      **not** the full `where()` and not an unfiltered count. Dropping the
      facet's own dimension is what keeps the tab strip populated when a tab is
      selected.

## 6. Prove equality of scope

- [ ] `filters_v2` absent / `None` / `[]` / `"[]"` returns an envelope identical
      to the pre-migration one, at **two roles** (an ordinary user and a
      System Manager).
- [ ] A clause naming another user's rows returns nothing — it narrows, never
      widens.

## 7. UI notes for whoever builds the panel

- **Select with a leading blank option.** A `Select` whose options string starts
  with a newline has `""` as a legitimate value, and `""` means *not set*. The
  schema passes the options through verbatim, blank included; the control must
  render that entry as "Not set" rather than an empty row, and must not confuse
  it with "no filter". (Alternatively offer the `is` operator, which expresses
  the same question explicitly.)
- **Dynamic Link.** Fully catalogued (fieldtype, options = the controlling
  fieldname) and compiles correctly as a plain value comparison, but the *value
  control* cannot be a Link picker until the panel reads the controlling field's
  current value to know which DocType to search. Until then it renders as a text
  input. Not a deviation — an unbuilt UI affordance, recorded so nobody
  rediscovers it as a bug.
- **`is_large_table`** is reported per view; when true the `Data` default is `=`
  and the UI should say why.
- **Filters are sticky across navigation** (ruled, Frappe-consistent): bare
  sidebar / command-palette navigation back to a list preserves its active
  filters, and every tab switch within a list's own shell carries the `fv2`
  query param, because a tab is a view of one page and not a reset. **Clear All
  is the only unfilter affordance** — no navigation may quietly drop a filter
  set, which is why the URL param is the state of record and the panel refuses
  to write one too large to survive the round trip.

## 8. Track the sunset

- [ ] Note the legacy `filters` callers for this surface (SPA page, PWA, any
      other client — `personalise_notes` and `file_box` each have two).
- [ ] The legacy argument is removed only after every shipped client sends v2
      (plan §10 Phase 5). Until then both paths must keep working, and the
      per-surface test asserting that is what makes the sunset safe to schedule.

---

# PER-VIEW SIGN-OFF (Wave F8) — the five migrated views

Round-2 P1-12: a completed artefact for the five views flipped to `MIGRATED`,
replacing the empty template above for them. Catalog counts are Administrator
(the ceiling); floor-role width is reported by
`test_list_registry.TestCuratedFilterWidthGap.test_floor_role_catalog`.

| View | Endpoint | Fixed predicate (scope) | DocPerm/permlevel gate | admin main / child | Withheld fields | Wrapper-wire test | Telemetry |
|---|---|---|---|---|---|---|---|
| skills | `custom_skills_api.list_custom_skills_page` | owner OR shared-with-me (DocShare id set) | Jarvis User read; permlevel enforced (`skill_bundle` moat) | 19 / 2 | — | `frontend/tests/list-filters/wrapperWire.spec.js` (Skills) | compile emits view=`skills` |
| macros | `macros_api.list_macros_page` | owner-scoped | Jarvis User read; child `Jarvis Macro Step` via parent perms | 22 / 5 | — | `wrapperWire.spec.js` (Macros) | view=`macros` |
| saved_dashboards | `dashboards_api.list_dashboards_page` | scope (mine/role/org) visibility | Jarvis User read | 20 / 3 | — | `frontend/tests/list-filters/wave1Wrappers.spec.js` (Dashboards) | view=`saved_dashboards` |
| triggers | `triggers_api.list_triggers_page` | owner/manager scope | Jarvis User read; **excluded_fields** redact automation logic | 19 / 0 | `condition`, `script_body`, `llm_instruction` (D1-b) | `wave1Wrappers.spec.js` (Triggers) | view=`triggers` |
| wiki_pages | `wiki.list_wiki_pages_page` | scope_filter (all/org/role/mine) + write matrix | Jarvis User read | 27 / 0 | — | `wave1Wrappers.spec.js` (Wiki) | view=`wiki_pages` |

Transport: all five send `filters_v2` (JSON, omitted when empty) through the ONE
shared encoder `frontend/src/api/listPageArgs.js` — Skills/Macros via `api.js`,
Dashboards/Triggers via `api/dashboards.js`/`api/triggers.js`, Wiki via
`api/wiki.js`'s bespoke shape reusing `encodeFiltersV2`. Each wrapper has a
wire-contract test asserting the exact `frappe-ui.call` args for the empty and
nonempty cases (the round-2 P0-01 gap: Dashboards and Triggers rebuilt the
request without `filters_v2`).

Deferred for these views (ledgered, not this round): browser result-narrowing
matrix, projections (File Box/Support), Waves 2-3, per-endpoint query-duration
telemetry, Dynamic Link value picker, nested-set operators.
