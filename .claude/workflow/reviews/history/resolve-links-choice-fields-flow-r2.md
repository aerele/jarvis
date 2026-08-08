# Flow review — resolve-links-choice-fields — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-07
Scope: `resolve_links` exercised as a running system, end to end, on the live `jarvis.local` site.

**How it was executed.** `resolve_links` has no UI of its own, so this is method C from
`references/flow-review.md` (direct driving of a no-UI service). The bench dev server was
already up on `127.0.0.1:8002` serving `jarvis.local`; the auth boundary was exercised over
real HTTP with `curl`, and every functional scenario was driven through the full
`jarvis.api.call_tool` dispatch — registry lookup, tool invocation, and the `{ok, data}` /
`{ok, error, hint}` envelope — on `jarvis.local`, which unlike `test_jarvis` has
`india_compliance` installed and therefore carries the real 1005-entry `port_code`
Autocomplete the feature was designed around, plus real customers, items and users.

**What was not driven.** Claude in Chrome was available (one connected local browser) but its
required browser-selection confirmation cannot be issued from a subagent, and harvesting API
credentials for a scripted HTTP session was declined. So the HTTP hop itself was exercised
only for the auth boundary; the functional scenarios ran in-process through the same dispatch
function that HTTP calls. Everything below is a real execution against real data — nothing
here is inferred from reading code. No scenario is recorded as NOT RUN.

**Site hygiene.** Every write in this review was a `preview_doc` dry-run (rolled back by
`preview_sandbox`) or a probe `User` insert followed by `frappe.db.rollback()`; confirmed
afterwards that the probe user did not persist. `test_jarvis` schema re-checked clean.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| **F1** Correct Autocomplete: `resolve_links(Sales Invoice, {port_code: "INMAA1"})` | `exact`, emitted (unguarded class), no spurious warning | `{status: exact, kind: autocomplete, validated_by_server: false, candidates: []}`; note carries no warning | PASS |
| **F2** Wrong Autocomplete: `{port_code: "BOGUSPORT12"}` | `missing` + explicit silent-save warning | `status: missing`; note `"1 choice value(s) not an exact option …; 1 on a field the server does NOT validate - a wrong value saves silently"` | PASS |
| **F2b** Premise check — would the bogus value otherwise pass? `preview_doc(Sales Invoice, {customer, port_code: "BOGUSPORT12", items:[…]})` | server accepts garbage, proving the feature is needed | `valid: true`, `resolved.port_code = "BOGUSPORT12"`, no error anywhere | PASS (feature justified) |
| **F3** Label-only match: `{port_code: "Chennai"}` against the real 1005-entry list | resolves the name to the code, truncated with a total | `candidates` = `INFCH5, INMAA1, INMAA4, INMAA5, INMAA6`, `candidates_total: 6` | PASS |
| **F4** Break attempt: malformed/awkward choice metadata must not take the Link pass down | Link results still returned, no 500 | `links` returned normally alongside `choices`; separately, an injected `"[not json"` Autocomplete degrades to `unchecked` while `links` stays byte-identical to baseline | PASS |
| **F5** `naming_series` supplied bogus: `{naming_series: "TOTALLY-BOGUS-"}` | no false warning at all | `choices: []`, note `"all links resolved to existing records"` | PASS |
| **B1–B6** `limit` = `"5"`, `0`, `999`, `True`, `None`, `5.0` | clean `InvalidArgumentError`, never a 500 | all six → `{ok:false, error:{code:"InvalidArgumentError", message:"limit must be an integer between 1 and 20"}}` | PASS |
| **B7** SQL fragment as value: `"' OR 1=1 --"` | no SQL error, judged as data | `status: missing`, echoed verbatim | PASS |
| **B8** Path traversal: `"../../etc/passwd"` | judged as data | `status: missing` | PASS |
| **B9** Script tag: `"<script>alert(1)</script>"` | judged as data, no escaping surprises | `status: missing`, echoed verbatim | PASS |
| **B10** 100 000-character value | no blowup, no timeout | `status: missing`, returned promptly | PASS |
| **B11** Zero-width + RTL override + emoji inside a real code | no crash, not a false match | `status: missing` | PASS |
| **B12** `{port_code: 12345}` (the r1 corruption case) | coerced and judged, never an all-clear | `value: "12345"`, `status: missing`, note carries the silent-save warning | PASS |
| **B13/B14** `{port_code: False}` / `{port_code: 0}` (falsy non-strings) | coerced, not dropped | `value: "False"` / `"0"`, both `missing` | PASS |
| **B15** `{port_code: {"a": 1}}` — a dict value | flagged, or at minimum not reported as fine | `choices: []`, note `"all links resolved to existing records"` — an affirmative all-clear on an unjudged value | **BREAK** (code finding 3) |
| **B15b** Follow-through: the dict is the tool's own candidate shape — `{port_code: {"value":"INMAA1","label":"INMAA1 - Chennai (Ex Madras)"}}` | same all-clear, then a write | `resolve_links` → all-clear; `preview_doc` → `valid: false, "…will get truncated, as max characters allowed is 15"`. Caught only by the length cap; on the uncapped `Bill of Entry Item.gst_treatment` the same dict also yields `choices: []` + all-clear with nothing downstream to catch it | **BREAK** (code finding 3) |
| **B16** List of values `["INMAA1", "NOPE"]` | one record each | two records: `exact` and `missing` | PASS |
| **B17–B19** `values` not a dict; empty `doctype`; backtick-injected doctype | `InvalidArgumentError` before any meta read | `"values must be a non-empty dict"`, `"doctype is required"`, `"unknown doctype: ToDo`; drop table x; --"` | PASS |
| **B20** Unknown tool name through the dispatch | clean envelope, not a 500 | `{ok:false, error:{code:"ToolNotFoundError"}}` | PASS |
| **B21** Whitespace-padded value: `{port_code: "  INMAA1  "}` | not blessed as correct, since nothing strips an Autocomplete | `status: exact`; then `preview_doc` → `valid: true` with `resolved.port_code = "  INMAA1  "` stored padded. On `Bill of Entry Item.gst_treatment` (uncapped) `"  Taxable  "` → `exact` and `new_doc(...).gst_treatment` keeps `'  Taxable  '`. `india_compliance` `e_invoice.py` then fails `if self.doc.port_code in PORT_CODES` and silently drops the Port from the payload | **BREAK** (code finding 2) |
| **B22** Fullwidth unicode case: `{status: "Ｏpen"}` on ToDo | not a false `exact` | `status: missing`, `validated_by_server: true` | PASS |
| **B23** 300 child rows each carrying a choice value | bounded time, per-row records, options parsed once | 300 choice + 300 link records in **0.24 s**; `table: "items"`, `row` correct | PASS |
| **B24** Non-dict rows mixed into the child list (`[dict, "junk", None, dict]`) | skipped without raising, indices preserved | records at `row: 0` and `row: 3`; rows 1/2 absent | PASS |
| **B25** Unauthenticated over real HTTP: `POST /api/method/jarvis.api.call_tool` as Guest | rejected | HTTP 401, `{"ok":false,"error":{"code":"AuthenticationError","message":"authentication required"}}` | PASS |
| **B26** **Read-permission bypass.** Same session, same user (System User holding a Jarvis access role, `has_permission("Sales Invoice","read") == False`): call `get_schema` then `resolve_links` | both refuse, or at least neither hands over the doctype's option list | `get_schema` → `{ok:false, PermissionDeniedError, "no read permission on Sales Invoice"}`. `resolve_links(Sales Invoice, {port_code:"Chennai"})` → `{ok:true}` with `INFCH5/INMAA1/INMAA4/INMAA5/INMAA6` **and `candidates_total: 6`**. Substring matching + the returned total make this a full enumeration oracle over any doctype's Select/Autocomplete options | **BREAK — BLOCKER** (code finding 1) |
| **B27** Same probe against a payroll doctype: `resolve_links(Salary Slip, {status: "xyz"})` with no read | refused | `{ok:true}`, choice record returned for `Salary Slip.status` | **BREAK** (same finding) |
| **B28** Repeat/concurrent invocation — identical call issued repeatedly | idempotent, no shared state, no writes | identical output every time; 5 consecutive Sales Invoice calls in 0.43 s total; no DB writes on the path | PASS |
| **B29** Site mutation from a full run | none | `tabToDo` has 0 `jarvis_test%` columns and `tabCustom Field` 0 `jarvis_test%` rows on `test_jarvis` after two consecutive module runs; probe user did not survive rollback on `jarvis.local` | PASS |

## Summary

29 scenarios executed, 0 not run. 4 BREAKs, covering 3 distinct defects: the
read-permission bypass (BLOCKER), the false `exact` on padded Autocomplete values with a
demonstrated e-invoice consequence (MAJOR), and the dict-valued field silently dropped
behind an affirmative all-clear (MAJOR). The five scenarios the plan itself specified
(F1–F5) all pass, and every r1 BREAK is genuinely fixed — the dynamic sentinel, the
non-string all-clear, and the `casefold()` crash are all gone.

VERDICT: RED
