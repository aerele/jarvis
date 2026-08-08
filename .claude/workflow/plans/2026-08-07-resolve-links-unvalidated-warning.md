# Plan: warn on Autocomplete values Frappe never validates
STATUS: APPROVED
Date: 2026-08-07
Approved: 2026-08-07 by the user ("build the narrow version - just the validated_by_server warning")
Supersedes: 2026-08-07-resolve-links-choice-fields.md (full choice resolution, abandoned after
four RED reviews; that plan's edge-case list remains the record of the traps found)
Owner: Fable (team leader)

## Goal

`jarvis__resolve_links` tells the agent, before a create, which of the values it is about to
write land on fields Frappe will **not** validate on insert. Done means an agent drafting a
GST port code, or any other Autocomplete, is told the server will not catch a wrong value -
and is told nothing else.

## Context

Verified in source and live: `BaseDocument._validate_selects` covers `Select` only
(`meta.get_select_fields()` filters `fieldtype == "Select"`), `_validate_data_fields` covers
`Data` only (`get_data_fields()`), and `Autocomplete` sits in `data_fieldtypes` so neither
touches it. A wrong Autocomplete value is stored verbatim, with no error at insert or later -
confirmed by a garbage value passing `preview_doc` with `valid: true`. On india_compliance's
`port_code` that is a filed shipping bill carrying a bogus port.

`resolve_links` is already mandated pre-create by the persona, so the warning costs no extra
turn and adds no tool-selection surface.

### Why this shape and not the previous one

The abandoned plan also resolved values against the field's option list. Four review rounds
found five defects, every one of them in the option-list semantics rather than the warning:
`naming_series` is skipped by Frappe and runtime-extensible; a bare single token is a
server-loaded source, not a one-item list; blank options are legal values on ~200 shipped
Selects; Frappe strips for Select but not Autocomplete; whitespace normalises to blank.

**This version parses no option lists.** It reads `fieldtype` and nothing else, so none of
those five traps exist here. That is the entire point of the narrowing.

## Architecture / approach

A third list on the response, `unvalidated_choices`, alongside the untouched `links`. Each
record names a field the caller supplied a value for whose `fieldtype` is `Autocomplete`:

```json
{"field": "port_code", "value": "INMAA9", "validated_by_server": false}
```

Child-table fields carry `table` and `row`, matching the Link records.

- **`Select` is never reported.** The server does police it, so there is nothing to warn about.
  This also removes `naming_series` from scope for free - it is a Select.
- **No option list is read**, so no parsing, matching, casing, stripping, candidate or
  `unchecked` semantics exist. The value is echoed for context only; it is never judged.
- **Permission**: gated on the parent doctype's read permission, wrapped, failing closed -
  which field is an Autocomplete is metadata `get_schema` refuses without read. Denied means
  no records, which is exactly today's behaviour, so no regression.
- `links` and its existing note wording are untouched; the note gains one clause only when
  there is something to say.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | The pass + note clause in `resolve_links.py` | Heavy | Lead (Fable) | — | All 7 edge cases below behave as specified; `links` output byte-identical to `develop` for every existing test |
| T2 | Tests, one per edge case | Heavy | Lead (Fable) | T1 | Each EC has a named test; module reruns cleanly twice; **no Custom Field, no DDL**; a test asserts `tabToDo` gains no column |
| T3 | Plugin descriptor + persona text | Light | Lead (Fable) | T1 | Describes only what ships; `npm test` green; no manifest change |

Delegation note: T3 is Light but I am doing it myself. Both Light doc tasks in the previous
attempt came back with defects their own reports called clean (a silently deleted instruction,
an invented example), and this text is small enough that reviewing a subagent's diff costs
more than writing it.

## Edge cases and failure modes (reviewer will verify each one)

1. **`Select` is never reported** - `{"status": "anything"}` on ToDo yields no record, because
   `_validate_selects` will police it.
2. **`naming_series` is never reported** - falls out of EC-1 (it is a Select), with no special
   case. Assert it anyway, since it was a BLOCKER in the previous design.
3. **Autocomplete with a value IS reported**, whatever the value - correct, wrong, or of the
   wrong type. The claim is about the FIELD, not the value, so nothing is parsed or judged.
4. **Empty value is not reported** - `""` and `None` mean nothing is being written. `"   "`
   IS reported: it is stored, and on a field nothing validates it stays stored.
5. **Non-string values are reported, not dropped** - `12345`, `True`, `{"value": "x"}`. Echoed
   as supplied; never coerced, matched, or used to decide anything.
6. **Child rows** - a child Autocomplete carries correct `table`/`row` across rows, non-dict
   rows are skipped without raising, and indices survive the skip.
7. **No read permission** - no records at all, and `has_permission` raising must not discard
   the Link results already gathered (`System Health Report` raises from
   `frappe/permissions.py:52`). Gate on the PARENT for child tables: asking `has_permission`
   about a child DocType routes to `has_child_permission`, which returns False without a
   `parent_doctype` and would silently kill every child field for non-Administrators.

**Concurrency / dependency failure**: not applicable - pure in-memory reads over `meta`, no
DB writes, no cache, no network, no shared mutable state.

## Test plan

Real shipped fields only where they exist: `ToDo.status`/`priority` (Select), `Desktop Icon.app`
(a real Autocomplete). An in-memory `frappe._dict` docfield with `frappe.get_meta` patched for
the child-table case. **No Custom Field and no DDL** - the previous attempt's `ALTER TABLE`
committed implicitly, survived the test rollback, and invalidated a whole regression baseline.

At least one test must leave the Administrator session: Administrator short-circuits
permissions at `permissions.py:104`, which is how the previous attempt's child-permission
BLOCKER stayed green through a full suite.

Regression evidence: capture the `develop` baseline before any change, diff **fully-qualified**
test IDs with one parser (bare names collide on `setUpClass`).

Flow review: draft a create with a wrong Autocomplete value and confirm the agent is warned and
does not silently write it; confirm a Select draft is NOT warned about; confirm a
non-Administrator sees child-table warnings.

## Open questions

None.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens
