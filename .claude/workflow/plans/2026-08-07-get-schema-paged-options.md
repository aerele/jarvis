# Plan: page large option lists instead of inlining them
STATUS: APPROVED
Date: 2026-08-07
Approved: 2026-08-07 by the user ("go with the paged approach")
Owner: Fable (team leader)

## Goal

`get_schema` stops inlining option lists big enough to blow the tool-result cap, and instead
returns them **on demand, filtered and paged, through the same tool**. Done means Sales
Invoice and Delivery Note schemas fit comfortably, and the agent can still reach every option
it could before - nothing is lost, only deferred.

## Context

`india_compliance` attaches `port_code` to Sales Invoice and Delivery Note as an
`Autocomplete` whose `options` is a JSON array of **1,005 Indian ports**
(`gst_india/constants/custom_fields.py:14`). Measured: that one field is ~75,600 chars
(~18,900 tokens) inside a single `get_schema` result - **more than the 64,000-char result cap
by itself**. Both doctypes therefore truncate mid-response, and the agent never reaches the
fields it needs. A tenant hit this twice in 2m16s, completing 0 of 15 steps.

`get_schema` emits `options` verbatim with no length bound (`_field_record` is an
inverse-allowlist: every truthy docfield property rides along).

### Alternatives rejected

- **Plain cap** (elide, no way back). Rejected by the user: the agent loses sight of the list
  entirely, so a genuine export invoice can no longer be filled.
- **Resolve values against the options** (the abandoned
  `2026-08-07-resolve-links-choice-fields.md`). Four RED reviews, five defects, every one in
  the *comparison* semantics: `naming_series` is not a real enum, a bare token is a
  server-loaded source, a blank option is legal on ~200 shipped Selects, Frappe strips for
  Select but not Autocomplete, whitespace normalises to blank. **This plan compares nothing.**
- **A separate lookup tool.** Rejected: grows the tool-selection surface, which is the
  program's quality gate. A parameter on an existing tool does not.

## Architecture / approach

**Emit side** - in `_field_record`, the single place every field record is built:

- `options` longer than `_OPTIONS_INLINE_MAX` (2,048 chars) is replaced by a sentinel naming
  the exact call to make, plus `options_count` when it parses as a list.
- Threshold is data-derived: a sweep of every shipped `options` value across
  frappe/erpnext/hrms/india_compliance found the largest legitimate list is **1,263 chars**
  (`Workflow State.icon`), against `port_code`'s ~63,500 raw. 2,048 sits in a 40x gap, so
  nothing legitimate is touched.
- Applied by LENGTH, not by fieldtype. Link/Table options are DocType names and can never
  reach the threshold, so a length rule needs no fieldtype allowlist and cannot miss a type.
- `_build_schema` calls `_field_record` for parent AND (under `verbose`) child fields, so the
  child path is covered by construction - no second call site.

**Retrieval side** - new parameters on `get_schema`:

```
get_schema("Sales Invoice", field_options="port_code")                    -> first page
get_schema("Sales Invoice", field_options="port_code", search="chennai")  -> matching entries
get_schema("Sales Invoice", field_options="port_code", offset=50)         -> next page
```

Returns a distinct shape: `{doctype, fieldname, fieldtype, total, matched, returned, offset,
options: [{value, label}]}`. It **returns** options; it never judges a value, so none of the
five comparison traps exist here.

`search` is a plain case-insensitive substring over value AND label. **If it matches nothing,
the unfiltered page is returned** (with `matched: 0`) - a bad search must never hide the list,
which is the one way this could reintroduce "the agent can't see the options".

**Cache**: the retrieval branch returns before the cache is touched (different shape, and
filtered results must never be stored under a doctype key). The cached full-schema shape does
change, so `_SCHEMA_CACHE_VERSION` goes 2 -> 3; `clear_cache_for` already deletes both verbose
variants and needs no change.

**Permissions**: the existing `PermissionDeniedError` gate at the top of `get_schema` covers
both paths - the retrieval branch sits after it.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Elide in `_field_record` + cache version bump | Heavy | Lead | — | Sales Invoice / Delivery Note under 64,000 chars; every option value <= 2,048 emitted byte-identical to today |
| T2 | Retrieval branch + params on `get_schema` | Heavy | Lead | T1 | All EC below; never cached; never reachable without the existing permission gate |
| T3 | Tests | Heavy | Lead | T1,T2 | One per EC; mutation-tested; no DDL; reruns twice clean |
| T4 | Plugin `schemas.ts` + descriptor | Light | Lead | T2 | `npm test` green; no manifest change (it carries names, not params) |

All Lead: the change lands in the most-used tool in the registry, and both Light doc tasks
delegated in the previous attempt returned defects their own reports called clean.

## Edge cases and failure modes (reviewer will verify each one)

1. **Under-threshold options are untouched** - byte-identical to `develop` for every field
   whose `options` is <= 2,048 chars. The anti-overreach gate; assert on real fields
   (`Workflow State.icon` at 1,263, `place_of_supply` at ~456).
2. **Over-threshold is elided with a usable pointer** - sentinel names `field_options` and the
   fieldname; `options_count` present when the blob parses as a list.
3. **`verbose=true` child fields are elided too** - otherwise the bomb returns via the child
   path. Must hold without a second call site.
4. **Retrieval returns the real list** - `field_options="port_code"` yields entries whose
   values match the shipped `PORT_CODES`, with correct `total`.
5. **`search` filters on label AND value** - `"chennai"` finds `INMAA1` (label carries the
   name, value the code).
6. **A search matching nothing returns the unfiltered page, not empty** - `matched: 0` plus
   options. A bad search must never hide the list.
7. **Paging** - `offset`/`limit` walk the list without gaps or repeats; `offset` past the end
   returns empty options with the true `total`, not an error.
8. **Unknown / non-option fieldname** - `field_options="not_a_field"` and a Data field raise
   `InvalidArgumentError`, never a 500.
9. **Bad params** - `limit`/`offset` as strings, negatives, oversized: clean
   `InvalidArgumentError`. (`resolve_links` shipped a 500 on `limit="5"`; do not repeat it.)
10. **Permission** - retrieval is behind the same `PermissionDeniedError` gate as the schema
    itself; a caller without read gets nothing, not a filtered list.
11. **Cache isolation** - a retrieval call must never populate or serve the schema cache key,
    and a stale v2 entry must not be served after deploy (version bump).
12. **Both option wire-forms parse** - JSON array (of strings or `{label,value}` dicts) and
    newline-separated. Returned verbatim, never stripped or normalised - this path does not
    judge values, so it must not silently alter them either.

**Concurrency / dependency failure**: not applicable - reads `frappe.get_meta` and Redis, no
writes, no network. Cache is read-through with a TTL and was already concurrent.

## Test plan

Real shipped fields wherever they exist. `india_compliance` is NOT installed on `test_jarvis`,
so `port_code` does not exist there: the large-list cases use an in-memory `frappe._dict`
docfield with `frappe.get_meta` patched. **No Custom Field, no DDL** - the abandoned attempt's
`ALTER TABLE` committed implicitly, survived the rollback, left an orphan column and
invalidated a whole baseline. A test asserts `tabToDo` gains no column.

**Mutation-test the load-bearing tests**: raise the threshold so nothing elides, and break the
empty-search fallback; both must turn a named test red. Skipping this is how the previous
attempt shipped a regression test that could not fail.

Regression evidence: capture the `develop` baseline first, diff **fully-qualified** IDs with
one parser. Note `test_turn_state.TestEffectVocabulary.test_doctype_description_matches_the_canon`
is flaky in the full suite (appeared in 1 of 2 runs, passes in isolation, unrelated) - run the
suite twice before calling any single new failure real.

Flow review: a schema read on a bombed doctype completes without truncation; the agent
retrieves a port by name and fills it; a search with no matches still shows options.

## Open questions

None.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN; Flow review VERDICT: GREEN
- Committed only after both greens
