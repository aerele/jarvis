# Plan: resolve_links resolves choice (Select / Autocomplete) values
STATUS: APPROVED
Date: 2026-08-07
Approved: 2026-08-07 by the user
Amended: 2026-08-07 after review r2 — EC-3 widened to dict/list-of-dict, EC-12 rewritten
(the "already safe" assessment was false and unverified; a read-permission gate is now
required), EC-13 added (padded Autocomplete values must not report `exact`).
Owner: Fable (team leader)

## Goal

`jarvis__resolve_links` — already mandated before every create — additionally tells the
agent whether each **Select / Autocomplete** value it intends to write is a real option,
and, when it is not, which real options match the text it supplied. Done means: an agent
that follows the persona can no longer silently write a bogus Autocomplete value, and no
correct value is ever reported as wrong.

## Context

Frappe enforces `Select` membership on insert (`BaseDocument._validate_selects`) but does
**not** enforce `Autocomplete`: `meta.get_select_fields()` filters `fieldtype == "Select"`,
`get_data_fields()` filters `"Data"`, and `Autocomplete` sits in `data_fieldtypes`, so
neither validator covers it. A wrong Autocomplete value is stored verbatim with no error —
verified end to end: a garbage value passes `preview_doc` with `valid: true`. On
india_compliance's `port_code` (1005-entry Autocomplete on Sales Invoice / Delivery Note)
that is a filed shipping bill carrying a bogus port.

`resolve_links` is the right home: the persona already calls it pre-create, so the check
costs no extra turn, and extending it adds no new tool and no tool-selection surface
(~70 tools already compete there, and selection accuracy is the program's quality gate).

**A prior ad-hoc attempt was reviewed RED** (1 BLOCKER, 6 MAJOR, 7 MINOR) and unwound. Its
findings are requirements here, not suggestions. Reference patches live in the session
scratchpad under `wip-c/`; they are *reference only* and contain known defects.

### Alternatives rejected

- **A new `search_field_options` / `get_field_options` tool.** Rejected: grows the
  selection surface for a capability the persona flow already has a natural home for.
  Revisit only if telemetry shows demand for *browsing* option lists, which is a
  different capability from validating a value.
- **Validating inside `create_doc`.** Rejected: it would reject at write time, after the
  user has already confirmed a card. The whole value is catching it while drafting.
- **Patching Frappe to validate Autocomplete.** Rejected: upstream behaviour change with
  fleet-wide blast radius, and it would break legitimate free-text Autocomplete use.

## Architecture / approach

A second pass in `resolve_links`, after the existing Link and child-Link passes, emitting a
**new top-level `choices` key**. `links[]` is untouched — verified that no consumer
anywhere reads the response shape, so this is additive.

### The core problem: not every option list is an authoritative enum

This is what sank the prior attempt. A docfield's `options` string is only sometimes a
literal, server-enforced list. Survey of every DocType JSON in frappe / erpnext / hrms /
india_compliance:

| Shape | Autocomplete | Select |
|---|---|---|
| empty options (client-populated) | 20 | — |
| JSON array | 0 | — |
| multiline literal list | 2 (both india_compliance) | the norm |
| **single-token** | **3 — all `"Installed Applications"`** | **70 — 69 `naming_series` + 1 `invoice_series`** |

Two conclusions drive the design:

1. **A single-token, non-JSON `options` on an Autocomplete is a dynamic source, not a
   one-item list.** 3 of 3 in the corpus are the `"Installed Applications"` sentinel that
   `autocomplete.js:14` resolves server-side. Treating it literally reports the *correct*
   value `erpnext` as `missing`.
2. **`naming_series` is not a validated enum.** `_validate_selects` skips it by name, it is
   user-extensible at runtime via Document Naming Settings, and it accounts for 69 of the
   70 single-token Selects. Reporting on it is both wrong and high-volume (76 doctypes).

### Resolution rules

Exclusions — no `choices` record emitted at all:
- `fieldname == "naming_series"` (mirrors `_validate_selects` exactly)
- `options` in `{"[Select]", "Loading..."}` (mirrors `meta.get_select_fields`)
- empty `options`

Status vocabulary (reuses the Link pass's words, so the model learns one contract):
- `exact` — value is a member.
- `candidates` — case-insensitive substring match on value **or label**, capped at `limit`.
- `missing` — a literal list, no match.
- `unchecked` — **the list could not be read**: an Autocomplete whose `options` is a
  single non-JSON token (dynamic/server-loaded). Never `missing`, so a correct value is
  never reported as wrong.

Exactness stays **case-sensitive**, mirroring `_validate_selects`, so `"open"` surfaces as
a candidate for `"Open"` rather than a false match that would throw on insert.

### Emission policy

- **Select**: emit only when NOT `exact`. The schema showed the list and the server will
  reject a bad value; confirming a good one is noise.
- **Autocomplete**: emit **always**, including `exact`. This is the unguarded class — a
  positive confirmation is the only signal the agent gets, and silence is indistinguishable
  from "not checked". This also pre-satisfies the forward-compat requirement below.

`validated_by_server` is `true` only for a `Select` that `_validate_selects` will actually
police — i.e. `fieldtype == "Select"` **and** not an excluded field. Given the exclusions
above, this reduces to `fieldtype == "Select"`, but it is derived from one predicate shared
with the exclusion logic so the two can never drift.

### Forward compatibility with the later `options` cap ("B")

A planned change will elide very large option lists in `get_schema` /
`get_creation_context`. At that point the agent cannot see the list, so silence becomes
ambiguous and capped fields must always report. Emitting `exact` for Autocomplete **now**
satisfies that for the dangerous class, and the emission decision is isolated in one
predicate (`_should_emit`) so B extends it in one place. No speculative flag is added.

### Robustness

The whole choice pass is wrapped so a malformed option list degrades to `unchecked` for
that field and can never break Link resolution — the prior attempt crashed on
`label.casefold()` and took the pre-existing behaviour down with it.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Resolver core in `resolve_links.py`: exclusion predicate, option parsing (JSON array of str/dict, newline list, dynamic-token detection), status vocabulary incl. `unchecked`, non-string coercion, per-field error isolation, `_should_emit` seam | Heavy | Lead (Fable) | — | All 12 edge cases below behave as specified; Link-pass output byte-identical to `develop` for every existing test; no unguarded attribute access on parsed options |
| T2 | `_summary_note` + response assembly: `choices` key, note wording that can never say "all clear" while an unresolved or coerced value exists | Heavy | Lead (Fable) | T1 | Note names choice problems distinctly from link problems; EC-3 and EC-7 both produce a non-all-clear note |
| T3 | Test suite: one test per edge case, real fixtures where they exist, in-memory docfield only where none does | Medium | dev-sonnet | T1, T2 | Every EC has a named test; suite reruns cleanly twice; **zero schema mutation** (assert no new `tabToDo` column after a run) |
| T4 | Drop the orphan `tabToDo.jarvis_test_port` column left by the prior attempt, then re-establish a clean full-suite baseline on `test_jarvis` | Light | dev-haiku | — | Column absent; baseline captured with fully-qualified test IDs and stored for the reviewer to diff against |
| T5 | Plugin descriptor text in `src/tool-defs.ts` | Light | dev-haiku | T1 | Describes `choices`, label-matching, `unchecked`, and the `validated_by_server` consequence; `npm test` green; no manifest change |
| T6 | Persona alignment: `AGENTS.md`, `TOOLS.md`, `skills/jarvis-drafting/SKILL.md` | Light | dev-haiku | T1 | States the Autocomplete-is-unvalidated rule and "never invent a code"; no contradiction with the tool descriptor |

## Edge cases and failure modes (reviewer will verify each one)

1. **`naming_series`** — `{"naming_series": "TOTALLY-BOGUS-"}` on a doctype that has one.
   Required: **no `choices` record at all**. `_validate_selects` skips it and it is
   runtime-extensible; reporting it would be a false positive on 76 doctypes.
2. **Dynamic option source** — `resolve_links("Desktop Icon", {"app": "erpnext"})`, whose
   `options` is the literal `"Installed Applications"`. Required: `unchecked`, never
   `missing`, and the note must not claim a silent-save risk.
3. **Non-string value** — `{"port_code": 12345}` / `True` / `1.5` / `None`, **and `dict` /
   `list-of-dict`**. Required: scalars coerced to `str` and resolved normally (`12345` →
   `missing`); a `dict` or list-of-dict is **never silently dropped** — it reports
   `unchecked` (or resolves via its `value` key) and the note must not read "all resolved".

   *Amended 2026-08-07 after review r2 (MAJOR).* `{value, label}` is the shape this tool
   itself returns in `candidates`, and both the descriptor and `SKILL.md` instruct the agent
   to "pick one" — so handing a candidate dict straight back is the MOST likely wrong type
   on this path, not an exotic one. The r2 implementation coerced only int/float/bool and
   let dicts fall off the end of `_iter_choice_values` into a false all-clear.
4. **Malformed JSON options** — `"[not json"`. Required: `unchecked` for that field; must
   not become a single garbage option, must not raise.
5. **JSON array of dicts with a non-string or absent `label`** — e.g. `{"value": "X"}` or
   `{"value": "X", "label": null}`. Required: label defaults to the value; no
   `AttributeError`. (The prior attempt crashed here.)
6. **One bad field must not break the tool** — a doctype with both a valid Link and a
   malformed choice field. Required: Link results identical to today; the choice field
   degrades to `unchecked`.
7. **Empty / whitespace-only value** — `{"status": ""}` is absent (no record), consistent
   with `_iter_values`. **`{"status": "   "}` is NOT absent and must be reported.**

   *Amended 2026-08-07 after review r3 (MAJOR).* The original entry said whitespace-only
   was "treated as absent". That is wrong against Frappe: `_validate_selects` skips only
   FALSY values, and `"   "` is truthy — it strips to `""`, matches no option, and throws
   `ValidationError: Status cannot be ""`. Reporting all-clear on a value that provably
   fails on insert is the false-clear class this feature exists to eliminate. A
   whitespace-only value must surface (`missing`, or `candidates` if the stripped form
   matches).
8. **Child-row choice** — a choice field inside a child table. Required: record carries
   `table` and `row`, indices correct across multiple rows, and rows that are not dicts are
   skipped without raising.
9. **Case-only mismatch** — `{"status": "open"}`. Required: `candidates` → `["Open"]`, not
   `exact`, because `_validate_selects` is case-sensitive and would throw.
10. **Large option list** — 1005-entry JSON list. Required: `candidates` truncated to
    `limit` **and** the record carries the total so the agent knows more exist; options
    parsed once per field, not once per child row.
11. **Bad `limit`** — `limit="5"` (string), `limit=0`, `limit=999`. Required: a clean
    `InvalidArgumentError`, not an HTTP 500. (String case is a pre-existing defect; fixing
    it is in scope since this pass newly depends on `limit`.)
12. **Permissions** — a choice field on a doctype the user cannot read. Required: **the
    pass MUST gate on the parent doctype's read permission**. No read → the field reports
    `unchecked` with NO `candidates` and NO `candidates_total`; nothing about the option
    list may be inferable.

    *Amended 2026-08-07 after review r2 (BLOCKER).* The original entry claimed this was
    already safe because "`get_schema` exposes the same options". **That was false and was
    never verified.** `get_schema` raises `PermissionDeniedError` on a doctype the caller
    cannot read, and every sibling metadata tool is fenced the same way. Unfenced, this
    pass is an enumeration oracle: case-insensitive substring matching plus
    `candidates_total` lets a caller probe an option list they are not allowed to see.
    Mirror `_resolve_one`'s existing gate. The r2 test asserted the leak was correct
    behaviour and must be inverted, not kept.

13. **A padded value must not be reported `exact` on an Autocomplete.** Frappe strips the
    value only for `Select` (`_validate_selects` does `cstr(...).strip()` before comparing);
    nothing strips an Autocomplete, so `"  INMAA1  "` is stored with its whitespace.
    Reporting `exact` there is a false clear that leads exactly where this feature exists to
    prevent: india_compliance's `e_invoice.py` tests `port_code in PORT_CODES`, fails, and
    silently drops the Port from the payload. Required: strip before comparing ONLY for
    `Select`; for `Autocomplete` compare the value as supplied, and surface a
    whitespace-only difference as `candidates` (the untrimmed value is genuinely not a
    member).

**Concurrency**: not applicable — the pass is pure in-memory computation over `meta`, with
no DB writes, no caching, and no shared mutable state. Stated explicitly so the reviewer
can confirm rather than wonder.

**Dependency failure**: no network or external API. `frappe.get_meta` failure is the only
dependency, and it already propagates today from the Link pass; behaviour is unchanged.

## Test plan

**Unit / integration tests** (one per edge case above, named for it). Fixture strategy,
decided from data rather than preference:

`india_compliance` is **not installed on `test_jarvis`** (apps: frappe, erpnext, hrms,
jarvis), and frappe/erpnext/hrms ship **zero** literal-list Autocompletes. So no real
JSON-options Autocomplete exists on the test site.

- **Real fixtures wherever they exist** — `ToDo.status` / `ToDo.priority` (Select),
  `Desktop Icon.app` (the real dynamic sentinel, EC-2), an erpnext `naming_series` (EC-1).
- **In-memory docfield for the literal-list Autocomplete only** — build a synthetic
  docfield and patch `frappe.get_meta` for that test. **No Custom Field, no DDL.**
  Justification: the prior attempt's `ALTER TABLE` commits implicitly and cannot be rolled
  back by the test transaction, which left a permanent orphan column *and* invalidated the
  regression baseline. The code under test reads only `df.fieldtype/fieldname/options`, so
  an in-memory docfield exercises the identical path at zero schema cost.
- **Acceptance criterion**: a test asserts the run adds no column to `tabToDo`, so this
  class of defect cannot recur silently.

Regression evidence must be re-established honestly: capture the `develop` baseline
*before* any change, compare **fully-qualified** test IDs (bare names collide on
`setUpClass` and hid a real failure last time), and state the site's schema was unmutated.

**Flow review scenarios** (executed, not described):
1. Draft a create with a correct Autocomplete value → agent proceeds, no spurious warning.
2. Draft one with a wrong Autocomplete value → agent must not write it; it either picks a
   real candidate or asks. Confirm via `preview_doc` that the bogus value would otherwise
   have passed `valid: true`.
3. Draft one with a value matching only by **label** ("Northgate" for `PORTA - Northgate`)
   → resolves to the code.
4. Break-attempt: a doctype whose choice field has malformed options → tool still returns
   Link results and does not 500.
5. Break-attempt: `naming_series` supplied → no false warning.

## Open questions

None blocking. Two judgement calls made explicitly above, flagged for the approver to
overrule if they disagree:
1. **Autocomplete always emits, including `exact`** — small payload cost, buys positive
   confirmation on the only unguarded class and pre-satisfies the later cap work.
2. **EC-11 (`limit="5"` → 500) is pulled into scope** — pre-existing, but this pass newly
   depends on `limit`, so leaving it would mean shipping a known 500 on a path we just
   started using.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on the final state
