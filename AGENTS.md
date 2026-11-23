# Jarvis: rules for coding agents

This is the shared rules file for every coding agent working in this repo. Most tools
load `AGENTS.md` by themselves. Claude Code loads `CLAUDE.md`, which imports this file.
The rules apply to people too. Branch and release rules are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Frappe code rules

- No `frappe.db.commit()` unless the runtime does not commit for you. If one is needed,
  handle rollback in `try/except` and comment why. Never in a patch.
- Do not use `frappe.get_doc` to read. Use `frappe.db.get_value`,
  `frappe.get_cached_doc` or `frappe.get_list`. Use `get_doc` only to insert, save,
  submit, delete or call a Document method.
- Multi-table reads use `frappe.qb` with explicit joins. No `get_value` in a loop, no
  raw SQL.
- `frappe.get_all` skips permissions. In any user path use `frappe.get_list`. Use
  `get_all` only in system context (patches, scheduler, webhooks), with a comment.

## Writing tests

A test earns its place when it catches a break that no other test catches. Before
you add one, name that break in one sentence. If you cannot, do not add the test.

Do not add a test that:

- has no assertion, or only checks "does not raise" on code that catches its own errors
- mocks the function that does the work, then asserts the value the mock returned
- compares a constant with a copy of itself, or with the formula that defines it
- repeats the input of an existing test and checks less than that test does
- only checks a state that the class `setUp` already needs for every other test

When you move a behaviour's tests to a new file, delete the old copies in the same
change. When one table-style test covers many rows, do not add a one-row twin.

Keep tests that guard permissions, ownership, money or budget limits, stored data
shapes, races, and defaults a user sees, even when they look like duplicates.

Run tests on a dedicated test site, never on a site you test by hand: tests reset
`Jarvis Settings`.

## SPA UI

Every UI change in `frontend/` uses frappe-ui components (Autocomplete, Select, Dialog,
Button, Badge, ListView, FormControl) and copies the sibling control on the same page.
Do not hand-roll a dropdown or popover when a frappe-ui component exists.

## Runtime branding

User-visible text and code comments must pass the runtime-branding check
(`.github/workflows/runtime-branding.yml`). Run it locally before you push; the
commands are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
