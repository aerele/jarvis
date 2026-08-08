# Flow review — resolve-links-choice-fields — round 4
Reviewer: Opus (strict-reviewer)
Date: 2026-08-07
Scope: `jarvis__resolve_links` driven end to end against the running bench (method C — direct API driving, the tool has no UI surface of its own).

## How it was executed

- App: the already-running bench dev server, `frappe serve --port 8002`, site `test_jarvis` (frappe, erpnext, hrms, jarvis; no india_compliance). Reachability confirmed with `frappe.ping` → `pong`.
- Driver: `curl` against `POST /api/method/jarvis.api.call_tool` with `Authorization: token <api_key>:<api_secret>`, i.e. the real HTTP dispatch path through `jarvis/api.py` including the Jarvis-access gate and the error envelope — **not** the test harness.
- **Identity: a purpose-made non-Administrator** (`Desk User` + `Sales User` + `Jarvis User`, later + `HR Manager`), created because r3's BLOCKER only surfaced off the Administrator short-circuit at `frappe/permissions.py:107`. A second Desk-User-only account was used for the child-permission probes. Administrator was used only where a scenario needed a doctype the probe user is (correctly) denied.
- Fixtures: the plan forbids DDL, and `test_jarvis` ships **no** literal-list Autocomplete. To exercise the Autocomplete scenarios for real I installed a **Property Setter** on `Holiday List.subdivision` (metadata only, no `ALTER TABLE` — the column already exists), and a second on `Sales Order.order_type` for the malformed-options scenario. Both deleted afterwards and verified gone; `tabHoliday List` column count 20 before and after; `tabToDo` carries no `%jarvis%` column.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| **Plan FS-1** — draft with a correct Autocomplete value (`subdivision: "INMAA1"` against a real 4-entry option list) | `exact`, no spurious warning | `status: exact`, `validated_by_server: false`, note = `all links and choice values resolved` | PASS |
| **Plan FS-2** — draft with a wrong Autocomplete value (`"INMAA9"`) | flagged, agent must not write it | `status: missing`; note `…not an exact option…; 1 on a field the server does NOT validate - a wrong value saves silently` | PASS |
| **Plan FS-2b** — confirm the premise: the same bogus value passes `preview_doc` | `valid: true` (silent save) | `{"valid": true, "resolved": {… "subdivision": "INMAA9"}}` — the feature is the only guard | PASS |
| **Plan FS-3** — value matching only by **label** (`"Northgate"` for `PORTA - Northgate`) | resolves to the code | `status: candidates`, `[{"value":"PORTA","label":"PORTA - Northgate"}]` | PASS |
| **Plan FS-4** — malformed options on one field, alongside a Link and a child table in the same call | Link results intact, no 500 | `links` resolved normally; `order_type` → `unchecked`; child `charge_type` → `candidates`; HTTP 200 | PASS |
| **Plan FS-5** — `naming_series: "TOTALLY-BOGUS-"` supplied | no record, no false warning | `choices: []` | PASS |
| **r3 BLOCKER re-test** — child-table choice field as a real non-Administrator over HTTP | child choice record resolves with `table`/`row` | `Sales Order` + `taxes: [{"charge_type":"actual"}…]` → `{"field":"charge_type","status":"candidates","table":"taxes","row":0}` | PASS |
| Child rows: 3 rows, one non-dict interleaved | correct indices, non-dict skipped without raising | indices preserved, no exception | PASS |
| **Whitespace-only value on a Select whose option list contains the blank option** (`Sales Invoice.apply_discount_on = "   "`) | Frappe accepts it (`_validate_selects` normalises to `""`) → must **not** be reported wrong | `_validate_selects()` **passed**, stored `''`; `resolve_links` returned `status: candidates`, `[Grand Total, Net Total]`, note "pick from candidates or ask the user". 197 header Selects on this site have a blank option (142 in erpnext/hrms) | **BREAK — BLOCKER-1** |
| Whitespace-only value on a Select with no blank option (`ToDo.status = "   "`) | reported (Frappe throws) | reported; `_validate_selects()` confirmed to raise `ValidationError` | PASS |
| **Dict value with no usable `value` key** (`{"status": {"label": "…"}}`, `{"status": {"value": ""}}`) | plan EC-3: `unchecked` | `{"value": "<unusable dict>", "status": "missing"}` — an affirmative claim about a value never examined; the placeholder is substring-matched against real options | **BREAK — MAJOR-2** |
| Dict value with a usable `value` key (`{"value":"open","label":"Open"}`) | resolves via `value` | `value: "open"`, `status: candidates` | PASS |
| **`resolve_links` on a doctype whose `has_permission` raises** (`System Health Report`, found by sweeping all 308 doctypes carrying choice fields) | degrade to `unchecked`, Link results preserved | whole call fails with `PermissionDeniedError`; the header-Link results computed moments earlier are discarded. develop returned that link as `unchecked` | **BREAK — MAJOR-3** |
| Enumeration oracle: probe an option list on a doctype the caller cannot read (`Notification`, `Server Script`) | `unchecked`, no candidates, no count | exactly that, plus no `_server_messages` leakage in the HTTP body | PASS |
| Link pass under denial | keeps its own gate | `allocated_to` → `unchecked`; note `"1 link(s) NOT checked (no read permission on the target)"` | PASS |
| Deep nesting (12 levels of list) | bounded, no `RecursionError`/500 | 200, one `<unusable list>` record | PASS (status is MAJOR-2's) |
| `limit` = `"5"`, `0`, `-1`, `999`, `true`, `1.5`, `null` | clean `InvalidArgumentError` each | all 7 → `InvalidArgumentError`, HTTP 200 envelope, no 500 | PASS |
| `values` = list / string; `doctype` = null / `ToDo' OR 1=1 --` | clean `InvalidArgumentError` | exactly that; no SQL reached | PASS |
| Hostile value strings: `' OR 1=1 --`, `%`, RTL + zero-width unicode | echoed verbatim, `missing`, no injection, no crash | as expected | PASS |
| Oversized payload: 2000-row child table (106 KB in) | no 500, bounded work | 200, 2000 choice records, 367 KB out, sub-second | PASS |
| **Child table supplied as a dict** (`{"taxes": {"charge_type": "bogus-xyz"}}`) | must not claim all-clear | `choices: []`, note `all links and choice values resolved`. Bounded: the same payload hard-fails at write (`TypeError: 'str' object does not support item assignment`) | **BREAK (minor) — finding 7** |
| Child table supplied as a list of strings | rows skipped (plan EC-8 sanctions this) | skipped, no raise | PASS |
| Metadata fuzz: all 308 doctypes with choice fields, every choice field poisoned, every child table given mixed garbage rows | no 500 anywhere | 1 failure, and it is MAJOR-3 | PASS except MAJOR-3 |
| Large option list over HTTP (real 1005-entry Property Setter list) | truncated to `limit`, `candidates_total` carries the true total | 5 candidates, `candidates_total: 1000` | PASS |
| Case-only mismatch over HTTP (`"actual"`, `"open"`) | `candidates`, never `exact` | `candidates ["Actual"]`, `candidates ["Open"]` | PASS |
| Padded Autocomplete (`"  INMAA1  "`) vs padded Select (`"  Open  "`) | Autocomplete → `candidates`; Select → `exact`, suppressed | exactly that | PASS |
| Concurrency / state | pass is pure in-memory, no writes | confirmed by inspection and by repeated identical responses; no shared mutable state, cached `Meta` never mutated | PASS |
| Site left clean | no residue | 2 probe Users deleted and verified gone; 2 Property Setters deleted, both fields' options restored to shipped values; `tabHoliday List` 20 columns before and after; no `%jarvis%` column on `tabToDo` | PASS |

## Scenarios not run

None. Every plan flow scenario and every attack category in the playbook that applies to a read-only metadata tool was executed against the running bench.

VERDICT: RED
