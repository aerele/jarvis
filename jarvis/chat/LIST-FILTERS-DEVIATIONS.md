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
permlevel-1 field.

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
as plain `Text` with an `=` default. We keep Frappe's behaviour there rather than
extending D3, so the parity surface stays small — but the same "an equality
filter on `_comments` will not match" caveat applies. Revisit if a surface ever
exposes it prominently.

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

**Jarvis follows Frappe.** `like` remains *selectable* on every one of those
families (it is only the default that differs), which is what plan §5.1's own
closing sentence requires. The large-table rule is honoured via Frappe's own
`Meta.check_if_large_table` heuristic and reported to the client as
`is_large_table` so the UI can explain the changed default.

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
* **`ifnull(...)` null-handling** per operator/family is copied from
  `db_query.prepare_filter_condition`, so `!=` still matches NULL rows the way
  Frappe's does.
