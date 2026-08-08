# Flow review — resolve-links-choice-fields — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-07
Scope: `resolve_links` driven as a running system, end to end.

**How it was executed** — `resolve_links` has no UI of its own; its real flow is
`HTTP POST /api/method/jarvis.api.call_tool` → `jarvis.api.call_tool` → `jarvis.tools.registry`
→ the tool, and then the agent acting on the result via `preview_doc` / `create_doc`.
Driving method (flow-review.md step 2, method B + C):

- The bench web server was already running (`jarvis.local:8002`, PID listening, `/api/method/ping` → 200).
- **Claude in Chrome** against the authenticated desk session, calling the real whitelisted
  endpoint with the session's CSRF token — full dispatch path, not a direct Python import.
  Site `jarvis.local` has frappe + erpnext + hrms + india_compliance + insights + lending + jarvis,
  so the 1005-entry `Sales Invoice.port_code` Autocomplete the design cites is really present.
- **Direct driving** for the cases the browser could not express (in-process
  `_validate_selects` comparison, non-string option metadata, timing/scale) via
  `frappe.init(site="jarvis.local")` scripts, all wrapped in `frappe.db.rollback()`.
- Plugin suite `npm test` → 250/250 (6 files) on `jarvis-openclaw-plugin` @ e50c2d7.
- Targeted bench module on `test_site` → 22/22, run three times.

No Playwright specs were written: the change adds no UI surface, and the flows it
affects are tool calls, which the two methods above exercise directly and repeatably.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Happy path: `resolve_links(Sales Invoice, {port_code: "Chennai"})` over real HTTP | candidates matched on label, `validated_by_server: false` | `status: candidates`, 5 ports incl. `INMAA1 - Chennai (Ex Madras)`; note carries the "saves silently" warning | PASS |
| Exact Autocomplete value `"INMAA4"` | not reported (silence = fine) | `choices: []` | PASS |
| Premise check: does an Autocomplete really save unvalidated? `preview_doc(Sales Invoice, port_code: "BOGUSPORT12")` | server accepts garbage | `valid: true`, `resolved.port_code = "BOGUSPORT12"`, no error — and `resolve_links` correctly flagged it `missing`/`validated_by_server:false` first | PASS (feature justified) |
| Child-row choice: `items[0].gst_treatment = "taxable"` | record tagged with `table` + `row` | `{"status":"candidates","candidates":[{"value":"Taxable"}],"table":"items","row":0}` | PASS (functional; but untested in the suite — code finding 6) |
| `naming_series = "TOTALLY-BOGUS-"` on Sales Invoice | flagged as server-unvalidated, since Frappe skips `naming_series` | `validated_by_server: **true**`, note omits the "saves silently" warning. Direct call to `si._validate_selects()` **accepted** the bogus series → the server does NOT catch it | **BREAK** (code finding 2) |
| Valid value on a dynamic-option Autocomplete: `resolve_links(Desktop Icon, {app: "erpnext"})` | silence — `erpnext` is a valid app | `status: "missing"`, `validated_by_server: false`, note `"a wrong value saves silently"`. The `"Installed Applications"` sentinel is parsed as a literal one-entry option list | **BREAK** (code finding 3) |
| Wrong type: `resolve_links(Sales Invoice, {port_code: 12345})` | flagged, or at minimum not reported as fine | `choices: []`, note `"all links resolved to existing records"` — an affirmative all-clear | **BREAK** (code finding 4) |
| …then act on that all-clear: `preview_doc(Sales Invoice, {port_code: 12345, …})` | rejected, since resolve_links said all-clear | `valid: true`, `resolved.port_code = 12345` — the unchecked value would be stored | **BREAK** (same finding, confirms real consequence) |
| Malformed option metadata: `_resolve_choice(df, [{"value":"A","label":1}], "B", 5)` | degrade gracefully | `AttributeError: 'int' object has no attribute 'casefold'` — the whole `resolve_links` call fails, taking pre-existing Link resolution with it | **BREAK** (code finding 5) |
| Option text that is a JSON object / truncated array (`'{"a":1}'`, `'["A", "B"'`) | treated as "no static options" | one garbage option synthesised from the raw text; every value then reports `missing` | **BREAK** (minor — code finding 9) |
| `\r\n`-separated options (`"A\r\nB"`) | mirror `_validate_selects` | tool strips to `["A","B"]` and calls `"A"` exact; Frappe compares against `["A\r","B"]` and would throw | **BREAK** (minor — code finding 8) |
| Hostile strings into a choice field: `' OR 1=1 --`, `%`, `_`, `🚢`, 20 000×`A` | no injection, no crash, sane output | all returned `status: missing`, `candidates: []`; no SQL is involved in the choice path; long value echoed back verbatim only | PASS |
| Broad needle `port_code = "a"` against 1005 options, `limit=5` | capped, with some indication of elision | 5 arbitrary alphabetically-first ports, no `total`, no truncation flag | PASS with reservation (code finding 10) |
| `limit` boundaries: 0, −1, 21, 20 | rejected / accepted at the right edges | 0/−1/21 → `InvalidArgumentError: limit must be between 1 and 20`; 20 → 20 candidates | PASS |
| `limit = "5"` (JSON string, an LLM-plausible payload) | `InvalidArgumentError` envelope | **HTTP 500** (`TypeError` comparing `str` to `int`) | **BREAK** (pre-existing — code finding 11) |
| `values = "not a dict"` | clean rejection | `ok:false, InvalidArgumentError: values must be a non-empty dict`, HTTP 200 envelope | PASS |
| `doctype` omitted | clean rejection | `ok:false, InvalidArgumentError: missing a required argument: 'doctype'` | PASS |
| Null / empty choice values (`{port_code: null, naming_series: ""}`) | skipped, no records | `choices: []` | PASS |
| Deeply nested value (`{port_code: [["a"]]}`) | no crash | `choices: []` (inner non-str dropped) | PASS |
| Scale: 500 child rows each with a wrong choice | bounded, fast | 500 records in 0.00 s, 102 029-byte payload — correct but a large single tool result for an agent context | PASS with reservation |
| Scale: 200 values × 1005 options | bounded, fast | 200 records in 0.04 s, 88 620 bytes | PASS |
| Permission: option lists returned to a low-privilege caller (`frappe.set_user("Guest")`) | no new leak beyond existing schema tools | full port list returned — but `get_schema` already exposes Select option values for the same doctype, so no new exposure | PASS |
| Test-suite hygiene: purge `Custom Field ToDo-jarvis_test_port` + commit → run the module once → re-check | site left as found | 22/22 OK, and the Custom Field **and** `tabToDo.jarvis_test_port varchar(140)` are back — `tearDownClass`'s delete is rolled back by `super().tearDownClass()` | **BREAK** (code finding 7) |
| Idempotency: run the targeted module three consecutive times | 22/22 each time | 22/22, 22/22, 22/22 | PASS |
| Plugin descriptor change does not break the tool contract | 250/250 | 250/250 across 6 files, incl. `tool-contract.test.ts` | PASS |
| Full bench suite re-verification of the "215 pre-existing, zero new" claim | independently reproduced | **NOT RUN** — not reproduced independently, and finding 7 shows the baseline was captured against a site whose `ToDo` schema this change had already mutated, so the claim is not sound as stated | NOT RUN |

## Notes

- Nothing was persisted on `jarvis.local`: every write probe ran through `preview_doc`
  (dry-run) or was wrapped in `frappe.db.rollback()`. No `jarvis_admin` pool/provision/fleet
  test was run.
- `test_site` is left in the state I found it: `Custom Field ToDo-jarvis_test_port` present,
  because that is the guaranteed post-condition of the change's own test module (finding 7).
  I did not hand-clean it, so the defect stays observable.
- Nothing was committed, staged, or pushed in any of the three repos.

VERDICT: RED
