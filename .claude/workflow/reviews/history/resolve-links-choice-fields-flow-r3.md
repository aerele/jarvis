# Flow review — resolve-links-choice-fields — round 3
Reviewer: Opus (strict-reviewer)
Date: 2026-08-07
Scope: `jarvis__resolve_links` (and `get_schema` / `preview_doc` for contract comparison) driven end to end through the **running bench** at `http://jarvis.local:8002/api/method/jarvis.api.call_tool` — the real dispatch, real auth, real permissions. `jarvis.local` has `india_compliance` installed, so the flagship 1005-entry `Sales Invoice.port_code` Autocomplete is a real field, not a fixture.

Method: driving method **C** (direct API driving) per `references/flow-review.md` — this change has no UI surface; the flow that matters is the agent's tool call through `call_tool`. Two identities were used:
- **restricted user** — a purpose-made System User (`Sales User`, `Sales Manager`, `Accounts User`, `Item Manager`, `Jarvis User`), authenticated with `Authorization: token <api_key>:<api_secret>`. `has_permission` for this user: `Sales Order` True, **`Sales Order Item` False**, `Sales Invoice` True, **`Sales Invoice Item` False**, `Email Account` False, `Desktop Icon` True.
- **Administrator** — for A/B comparison only, via `bench console`.

A second battery ran on `test_jarvis` via `bench console` where an insert had to be attempted (`_validate_selects` behaviour) — noted per row.

Cleanup: the probe user and its API key were **deleted** after the run and verified gone (`frappe.db.exists("User", ...) -> None`, `like 'rl_r%'` -> `[]`). Zero schema mutation: 0 `jarvis_test%` columns on `tabToDo`, 0 `jarvis_test%` rows in `tabCustom Field`. All `bench console` probes ended in `frappe.db.rollback()`.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| F1 Plan flow 1 — correct Autocomplete `port_code: "INMAA1"`, restricted user | `exact`, emitted, no spurious warning | `{"status":"exact","validated_by_server":false}`, note `all links and choice values resolved` | PASS |
| F2 Plan flow 2 — wrong Autocomplete `port_code: "BOGUSPORT"` | flagged, and the note must escalate the silent-save risk | `missing`; note `... not an exact option ...; 1 on a field the server does NOT validate - a wrong value saves silently` | PASS |
| F2b `preview_doc` control for the bogus value | would otherwise pass `valid: true` | blocked earlier on the missing Customer (`valid:false, "Could not find Customer"`), so the silent-save claim was not re-proven here — it was proven in r2 and is unchanged code | PASS (control inconclusive, non-blocking) |
| F3 Plan flow 3 — label-only match `port_code: "Chennai"` | resolves to the code | `candidates`: `INFCH5, INMAA1, INMAA4, INMAA5, INMAA6` + `candidates_total: 6` — real india_compliance codes, capped at `limit` | PASS |
| F4 Plan flow 5 — `naming_series: "TOTALLY-BOGUS-"` on a real Sales Invoice | no `choices` record, no false warning | `choices: []`, all-clear note | PASS |
| F5 Plan flow 4 — dynamic sentinel `Desktop Icon.app: "erpnext"` | `unchecked`, never `missing`, no silent-save wording | `unchecked`; note `NOT verified (option list unavailable)`, no "silently" | PASS |
| **B1 child-table choice, CORRECT value, restricted user** — `Sales Order.items[0].margin_type = "Percentage"` | `exact` → Select-exact emits nothing | `{"status":"unchecked","validated_by_server":true,"table":"items","row":0}`; note `1 choice value(s) NOT verified (option list unavailable) - confirm before writing`. **Administrator, identical call → `choices: []`.** A correct value is reported unverified, forcing a needless confirmation on every order line | **BREAK — BLOCKER (code finding 1)** |
| **B2 child-table choice, WRONG value, restricted user** — `margin_type: "PercentageX"` | `missing` — Frappe will reject this on insert | `unchecked`. The pass catches nothing at all in child tables for real users | **BREAK — BLOCKER (code finding 1)** |
| **C11 second child table** — `Sales Invoice.taxes[0].charge_type = "On Net Total"` | `exact` → nothing emitted | `unchecked` + "NOT verified" note. Confirms the break is systemic, not one field | **BREAK — BLOCKER (code finding 1)** |
| B3 header Select wrong value (control) — `Sales Order.order_type: "NotARealOrderType"` | `missing`, no silent-save wording (server validates it) | exactly that | PASS |
| **B4 whitespace-only Select — `Sales Order.order_type: "   "`** | flagged, or at minimum not blessed | `choices: []`, note `all links and choice values resolved`. On `test_jarvis` the equivalent insert `ToDo{status:"   "}` raises `ValidationError: Status cannot be "". It should be one of "Open","Closed","Cancelled"` — the tool blessed a value that provably fails | **BREAK — MAJOR (code finding 5)** |
| **B5 dict with an empty `value` — `port_code: {"value":"","label":"INMAA1"}`** | a record, never silence | `choices: []`, note `all links and choice values resolved` — silently dropped | **BREAK — MAJOR (code finding 2)** |
| **C5 / C6 same shape, other empties — `{"value":"   "}`, `{"value":[]}`** | a record | `choices: []`, full all-clear both times | **BREAK — MAJOR (code finding 2)** |
| **C4 list-of-dict with one empty — `[{"value":"INMAA1"},{"value":""},{"label":"x"}]`** | 3 records | 2 records; the middle element vanished without trace | **BREAK — MAJOR (code finding 2)** |
| B6 candidate dict handed straight back — `{"value":"INMAA1","label":"INMAA1 - Chennai"}` | resolved via its `value` | `value: "INMAA1"`, `exact` | PASS |
| B7 dict with no `value` key — `{"label":"INMAA1"}` | `unchecked` per plan EC-3 | `missing` with `"value": "{'label': 'INMAA1'}"` — a Python repr presented as the value the agent is about to write | BREAK — MINOR (code finding 6) |
| B8 padded Autocomplete — `port_code: "  INMAA1  "` | `candidates`, not `exact` (nothing strips Autocomplete) | `candidates: [INMAA1]`, `value` echoed padded so the difference is visible | PASS (r2 MAJOR fixed) |
| B9 padded Select — `order_type: "  Sales  "` | `exact` → nothing emitted (Frappe strips it) | `choices: []` | PASS (mirror direction correct) |
| **C1 link `unchecked`, no choice values — `Sales Invoice{"project":"ghost-project"}` (Project unreadable)** | note must not claim all-clear | `links:[{"status":"unchecked"}]`, note `all links and choice values resolved` | **BREAK — MAJOR (code finding 4)** |
| C2 same plus one clean choice value | same | same false all-clear, now alongside a populated `choices` | **BREAK — MAJOR (code finding 4)** |
| C3 child key supplied as a dict not a list — `{"items": {...}}` | not silently all-clear | `links: []`, `choices: []`, all-clear | BREAK — MINOR (code finding 10) |
| **C7/C8 deeply nested value — `port_code` nested 600 / 5000 levels** | clean error, never a 500 | 600 → HTTP 200 (unwrapped to `"x"`); 5000 → orjson rejects at the request layer, HTTP 417 `DataError` | PASS at these depths |
| **Depth sweep 950 / 990 / 1010** | clean error, never a 500 | 950 → HTTP 200. **990 and 1010 → HTTP 500 `RecursionError: maximum recursion depth exceeded`, 4.8 MB Werkzeug traceback, 83 s of worker time**, from a ~10 KB body | **BREAK — MAJOR (code finding 3)** |
| B11/B12/B13 bad `limit` — `"5"`, `0`, `999` | clean `InvalidArgumentError`, not a 500 | `{"ok":false,"error":{"code":"InvalidArgumentError","message":"limit must be an integer between 1 and 20"}}` all three | PASS |
| B14 SQL fragment — `port_code: "' OR 1=1 --"` | echoed, no injection | `missing`, value echoed verbatim, HTTP 200 | PASS |
| B15 LIKE wildcard — `port_code: "%"` | no wildcard expansion into the option match | `missing` (in-memory substring match, no SQL involved) | PASS |
| B16 zero-width + RTL unicode inside a real code | echoed, `missing`, no crash | `"IN​MAA1‮"` → `missing` | PASS |
| B17 / C9 numeric and boolean on a choice field — `12345`, `true` | coerced and judged, never dropped | `"12345"` → `missing`; `"True"` → `missing` | PASS |
| C10 `null` choice value alongside a real link | choice skipped, link still resolved | `choices: []`, link `missing` reported | PASS |
| B18 empty `values` dict / B19 unknown doctype | `InvalidArgumentError` before any meta read | both exactly that | PASS |
| **B20 option list on a doctype the user CANNOT read — `Email Account{"service":"G"}`** | no candidates, no count, nothing inferable | `{"status":"unchecked","candidates":[]}`, no `candidates_total` | PASS (r2 BLOCKER fixed) |
| **B20b same doctype through `get_schema`** | the two tools must agree on the boundary | `PermissionDeniedError: no read permission on Email Account` — boundary now consistent | PASS |
| B21 200 values on one choice field against the real 1005-entry list | no timeout, no blowup | HTTP 200, 200 records each capped at 5 candidates + `candidates_total: 6`; response is large but bounded per record | PASS |
| B22 200 KB string as a choice value | no blowup | `missing`, HTTP 200, value echoed in full (unbounded echo — see code finding 6) | PASS |
| B23 unauthenticated `call_tool` | rejected | HTTP 401 `AuthenticationError` | PASS |
| C12 two identical requests fired in parallel | identical responses, no shared state | byte-identical; pass is pure in-memory over `meta` | PASS |
| Schema mutation across the whole battery | none | 0 `jarvis_test%` columns on `tabToDo`, 0 `jarvis_test%` custom fields | PASS |
| Probe-user cleanup | removed | user deleted, `like 'rl_r%'` → `[]`, all console probes rolled back | PASS |

## Summary

38 scenarios executed against the running application; **9 BREAKs** — 3 of them the same BLOCKER on a third of the tool's surface, 5 MAJOR-class false-clears and one 500, 2 MINOR.

The two r2 defects that were the reason for this round are genuinely fixed and were re-proven live: the option-list enumeration oracle is closed and now agrees with `get_schema` (B20/B20b), and the padded-Autocomplete false `exact` is gone in both directions (B8/B9). But the child-table permission gate added alongside the fix silently kills the feature for every non-Administrator user (B1, B2, C11), and the note the agent actually reads still says "all links and choice values resolved" in four separate situations where nothing of the kind is true (B4, B5, C1, C3).

VERDICT: RED
