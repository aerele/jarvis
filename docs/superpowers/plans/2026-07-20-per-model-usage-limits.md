# Per-Model Usage & Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`
> to execute this plan. Each task is an independent, testable unit: write the failing test
> FIRST, run it and confirm it fails for the stated reason, write the minimal implementation,
> run it and confirm it passes, then commit. Do NOT batch tasks. Do NOT skip the "run and
> confirm fail" step. Backend tasks (1–5) merge before UI tasks (6) — see Global Constraint G7.

Phase 4 of the fleet usage/limits feature (customer app `jarvis`). This is the **customer
bench** side only; the admin side (`jarvis_admin_v2`) ships separately (Phases 2–3). This
phase couples to admin Phase 2 solely through one outbound push endpoint (Task 5); it ships
and degrades independently of it.

Authoritative spec: `jarvis_admin_v2/docs/superpowers/specs/2026-07-20-fleet-usage-and-limits-design.md`
(sections 2, 3, 7, 8, 9, 10). Extends the shipped design
`jarvis/docs/superpowers/specs/2026-07-10-user-settings-usage-design.md`.

---

## Goal

Give each customer bench **per-model** token accounting and **per-model** monthly caps on top
of the existing aggregate per-user counter, and push a **month-to-date per-user × per-model
snapshot** to the admin control plane daily. Concretely:

1. Record, per turn, the actually-used model's token delta into a new child table on
   `Jarvis User Settings`.
2. Enforce a per-model monthly cap at send time (in addition to the existing aggregate cap),
   deterministically when a model is pinned/direct; skipped on pool "Auto" (accepted gap, spec §2).
3. Let a tenant admin set a per-model cap through the existing settings UI.
4. Push the month-to-date rollup to admin daily, best-effort.
5. Surface per-model usage + caps in `UsagePane.vue` (own) and `UsageAdminPane.vue` (team).

## Architecture

```
turn end (worker, holds pooled gateway conn)
  turn_handler.py:905  _usage.record_turn_usage(session_key, row)
        │  row carries "model" (actually-used model, post-hoc, accurate even on Auto)
        ▼
  usage.py: aggregate atomic UPDATE (existing)  +  NEW per-(user,model,month) child upsert
        │
        ▼
  Jarvis User Settings ──(child table user_model_usage)──▶ Jarvis User Model Usage
        │                                                     (model, month_key,
        │                                                      month_input_tokens,
        │                                                      month_output_tokens,
        │                                                      monthly_token_limit)
        │
   send time: api.send_message:816 validate_can_send(user)          ← aggregate gate FIRST
              api.send_message  validate_can_send(user, model=eff)  ← per-model gate (model resolved in api.py)
        │
   daily cron: usage_push.push_usage_rollup ──admin_client.push_usage_rollup──▶
              POST jarvis_admin_v2.api.tenant.ingest_usage_rollup {"rollup": {...}}
```

## Tech Stack

- **Backend:** Frappe v16 / Python 3.14. Whitelisted methods MUST be fully type-annotated
  (`hooks.py: require_type_annotated_api_methods = True`). Atomic counter writes use raw
  `frappe.db.sql` (never `doc.save`) — the established idiom in `chat/usage.py`.
- **Frontend:** Vue 3 SFC + Vite. `jv-*` CSS classes, `frappe-ui` `call()` + `toast`.
- **Tests:** `frappe.tests.utils.FrappeTestCase`, `unittest.mock.patch`. Gateway/admin I/O
  always mocked. Fixtures commit and are torn down explicitly (see `test_user_settings.py`).

## Global Constraints

- **G1 — Never raise into the turn.** All per-model recording lives inside the existing
  `try/except` of `record_turn_usage`; a per-model bug must never break chat. Log via
  `frappe.log_error` and continue.
- **G2 — Fail-open enforcement.** Every gate (`_over_model_limit`, `validate_can_send`) returns
  "allow" on any exception. An accounting/lookup bug must never block a legitimate send.
- **G3 — permlevel rules.** The new `user_model_usage` Table field on `Jarvis User Settings`
  is **permlevel 1**, `read_only`, `no_copy` — like every other usage counter. Only server
  code writes child rows (raw SQL). Owners get permlevel-1 **read** (they see their own
  usage/caps); only admins (`require_jarvis_admin`) write caps, via the whitelisted API.
- **G4 — Aggregate gate is the outer gate, checked FIRST.** The existing
  `_over_monthly_limit` stays unchanged and is evaluated before any per-model logic.
- **G5 — Policy stays import-light; NO import cycle.** `chat/policy.py` MUST NOT import
  `chat/turn_handler.py`. The effective model is resolved in `chat/api.py` (which already
  imports `turn_handler`) and passed as a plain string into `validate_can_send(user, model=...)`.
  This deliberately deviates from spec §7's `validate_can_send(user, conv)` signature — the
  clean boundary is a resolved model string, documented in code.
- **G6 — Pool "Auto" gap is accepted, documented in code.** When the effective model resolves
  to `""` (pool routes server-side), the per-model gate is skipped with an inline comment
  referencing spec §2. Attribution is still accurate (the recorded model is the actually-used
  one). The aggregate cap always applies.
- **G7 — MERGE RISK (do UI last).** `UsagePane.vue` / `UsageAdminPane.vue` / `api.js` are
  touched in parallel worktrees (`feat/settings-consolidation`, `fix/settings-followups`,
  `feat/user-settings-usage`/`usfix`). Backend tasks (1–5) touch none of those files and must
  merge first. Task 0 syncs branches; Task 6 (UI) runs last, rebased on the latest backend.
- **G8 — Month-to-date only on the bench; admin owns history.** The bench keeps only the
  current month's per-model rows (prune on rollover, Task 2). Admin persists history from the
  daily push. Configured caps are **carried forward** across rollover so a cap is never lost.
- **Style:** Python indent is **TAB** (ruff line-length 110, double quotes). Run
  `pre-commit run --all-files` before every commit; check `$?`.
- **Test site:** `$SITE` = the executor's bench test site (this repo uses `jarvis.proxy` for
  onboarded flows; `site.jarvis` is role-polluted — **CI is the authority** for the role-gate
  test in Task 4). New doctypes require `bench --site $SITE migrate` before their tests can run.

Test commands (from `CLAUDE.md`):

```bash
# after adding/altering a doctype:
bench --site $SITE migrate
# a single module:
bench --site $SITE run-tests --module jarvis.tests.<module>
# lint:
pre-commit run --all-files
```

---

## Interfaces (exact signatures — implemented across the tasks below)

```python
# jarvis/chat/usage.py  (module constants + helpers, all TAB-indented)
MODEL_USAGE = "Jarvis User Model Usage"
MODEL_USAGE_FIELD = "user_model_usage"          # the child-table fieldname on the parent

def record_turn_usage(session_key: str, row: dict | None) -> None: ...   # extended, signature unchanged
def set_model_limit(user: str, model: str, limit: int,
                    now: "datetime | None" = None) -> None: ...          # NEW (used by admin API)
# internal:
def _upsert_model_usage(user: str, model: str, month: str,
                        in_tokens: int, out_tokens: int, now) -> None: ...
def _current_model_row_name(user: str, model: str, month: str) -> str | None: ...
def _prior_model_limit(user: str, model: str, month: str) -> int: ...
def _insert_model_row(user: str, model: str, month: str, *,
                      in_tokens: int, out_tokens: int, limit: int, now) -> None: ...
def _next_child_idx(user: str) -> int: ...

# jarvis/chat/policy.py
def validate_can_send(user: str, model: str | None = None) -> tuple[bool, str | None]: ...  # model added
def _over_model_limit(user: str, model: str) -> bool: ...                # NEW

# jarvis/chat/user_settings_api.py
@frappe.whitelist()
def admin_set_user_model_limit(user: str, model: str,
                               monthly_token_limit: int = 0) -> dict: ...  # NEW
def _settings_payload(doc) -> dict: ...          # gains "per_model"
def admin_list_user_usage() -> dict: ...         # each user gains "per_model"

# jarvis/chat/api.py
def _measured_usage(user: str) -> dict | None: ...   # gains "per_model" list

# jarvis/chat/usage_push.py  (NEW module)
def push_usage_rollup() -> None: ...             # daily cron entry, never raises
def _build_rollup(cap: int = 500) -> "tuple[dict, bool]": ...
def _admin_configured() -> bool: ...

# jarvis/admin_client.py
def push_usage_rollup(rollup: dict) -> dict: ...  # NEW; POST api.tenant.ingest_usage_rollup

# frontend/src/api.js
export const adminSetUserModelLimit = (user, model, monthlyTokenLimit) => ...
```

**Pinned admin contract (Task 5 target — do not change):**
`POST /api/method/jarvis_admin_v2.api.tenant.ingest_usage_rollup`
body `{"rollup": {"month_key": "YYYY-MM", "users": [{"email", "tokens_in", "tokens_out",
"total_tokens", "per_model": {"<model>": {"in": int, "out": int}}}]}}`,
response the standard `{"ok": true}` envelope (admin's `_post` unwraps `data`).

---

## Task 0 — Merge-risk reconnaissance & branch sync (DO FIRST, no code)

- [ ] Fetch and inspect the parallel branches that also touch the two panes + `api.js`.

```bash
cd /Users/kavin/frappe/v16/bench-16/apps/jarvis
git fetch upstream
# Which parallel branches touch the UI files this plan will edit?
for b in feat/settings-consolidation fix/settings-followups feat/user-settings-usage; do
  echo "=== upstream/$b ==="
  git log --oneline -3 upstream/$b
  git diff --stat develop..upstream/$b -- \
    frontend/src/components/settings/UsagePane.vue \
    frontend/src/components/settings/UsageAdminPane.vue \
    frontend/src/components/settings/SettingsDialog.vue \
    frontend/src/api.js
done
git worktree list
```

Record for each branch: has it merged into `develop`/`beta` yet, and does it still have
unmerged pane changes? Findings at time of writing (verify — they drift):
- `develop` already carries the shipped `UsagePane.vue` / `UsageAdminPane.vue` (design 2026-07-10).
- The three parallel branches are **behind** current `develop` on these files (their diffs are
  subtractive), i.e. no large unmerged pane rewrite is pending. Confirm this still holds.

**Rebase expectation:** create the feature branch off the latest `develop`; land backend
Tasks 1–5 first. Before starting Task 6, `git fetch upstream && git rebase upstream/develop`
(or `beta` if that is where the pane branches land) so the UI edits sit on top of whatever
pane changes merged meanwhile. If a pane branch lands a conflicting rewrite, re-derive the
Task 6 SFCs against the new file rather than force-applying the diffs below.

- [ ] Create the working branch: `git switch -c feat/per-model-usage-limits` (off latest `develop`).

_No commit for this task — it's reconnaissance._

---

## Task 1 — Child doctype `Jarvis User Model Usage` + parent Table field

Child table on `Jarvis User Settings`, one row per `(model, month_key)`. Limit and usage share
the row (spec §7).

### 1a. Failing test

Create `jarvis/tests/test_usage_per_model.py` (start it here; Tasks 2 extend it):

```python
"""Tests for per-model usage accounting + caps (fleet usage spec §7, §9).

Hermetic like test_user_settings.py: disposable enabled users created in setUp,
and because record_turn_usage / set_model_limit COMMIT, every Jarvis User
Settings + Jarvis Chat Session + Jarvis User Model Usage row owned by a fixture
user is deleted in tearDown (a transaction rollback cannot undo a commit).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage

USETT = "Jarvis User Settings"
SESSION = "Jarvis Chat Session"
MODEL_USAGE = "Jarvis User Model Usage"

USER_A = "jarvis-permodel-a@example.test"
_ALL_USERS = (USER_A,)


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Jarvis",
			"last_name": "PerModelTest", "enabled": 1, "send_welcome_email": 0,
			"user_type": "System User",
		}).insert(ignore_permissions=True)


def _make_session(session_key: str, user: str) -> None:
	frappe.get_doc({
		"doctype": SESSION, "session_key": session_key, "user": user,
	}).insert(ignore_permissions=True)
	frappe.db.commit()


def _cleanup() -> None:
	for email in _ALL_USERS:
		for name in frappe.get_all(MODEL_USAGE, filters={"parent": email}, pluck="name"):
			frappe.delete_doc(MODEL_USAGE, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(SESSION, filters={"user": email}, pluck="name"):
			frappe.delete_doc(SESSION, name, ignore_permissions=True, force=True)


class _Base(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)


class TestChildDoctypeSchema(_Base):
	def test_child_doctype_exists_with_fields(self):
		meta = frappe.get_meta(MODEL_USAGE)
		self.assertTrue(meta.istable)
		fields = {f.fieldname: f for f in meta.fields}
		for fn in ("model", "month_key", "month_input_tokens",
				   "month_output_tokens", "monthly_token_limit"):
			self.assertIn(fn, fields, f"missing child field {fn}")
		self.assertEqual(fields["month_input_tokens"].fieldtype, "Int")
		self.assertEqual(fields["monthly_token_limit"].fieldtype, "Int")

	def test_parent_has_permlevel1_table_field(self):
		meta = frappe.get_meta(USETT)
		f = {x.fieldname: x for x in meta.fields}.get("user_model_usage")
		self.assertIsNotNone(f, "parent missing user_model_usage table field")
		self.assertEqual(f.fieldtype, "Table")
		self.assertEqual(f.options, MODEL_USAGE)
		self.assertEqual(int(f.permlevel or 0), 1)
```

### 1b. Run — expect FAIL

```bash
bench --site $SITE run-tests --module jarvis.tests.test_usage_per_model
```

Expected: `frappe.DoesNotExistError` / meta lookup failure — the doctype and field do not exist yet.

### 1c. Implementation

Create `jarvis/jarvis/doctype/jarvis_user_model_usage/__init__.py` (empty file).

Create `jarvis/jarvis/doctype/jarvis_user_model_usage/jarvis_user_model_usage.json`:

```json
{
  "doctype": "DocType",
  "name": "Jarvis User Model Usage",
  "module": "Jarvis",
  "istable": 1,
  "custom": 0,
  "engine": "InnoDB",
  "field_order": [
    "model",
    "month_key",
    "month_input_tokens",
    "month_output_tokens",
    "monthly_token_limit"
  ],
  "fields": [
    {
      "fieldname": "model",
      "fieldtype": "Data",
      "label": "Model",
      "reqd": 1,
      "in_list_view": 1,
      "description": "Bare model id actually used for the recorded turns (from the gateway sessions row)."
    },
    {
      "fieldname": "month_key",
      "fieldtype": "Data",
      "label": "Month Key",
      "in_list_view": 1,
      "description": "Usage month (YYYY-MM) these counters accumulate into."
    },
    {
      "fieldname": "month_input_tokens",
      "fieldtype": "Int",
      "label": "Month Input Tokens",
      "read_only": 1,
      "no_copy": 1
    },
    {
      "fieldname": "month_output_tokens",
      "fieldtype": "Int",
      "label": "Month Output Tokens",
      "read_only": 1,
      "no_copy": 1
    },
    {
      "fieldname": "monthly_token_limit",
      "fieldtype": "Int",
      "label": "Monthly Token Limit",
      "default": "0",
      "in_list_view": 1,
      "description": "Per-model monthly cap. 0 = unlimited."
    }
  ],
  "permissions": [],
  "sort_field": "modified",
  "sort_order": "DESC",
  "track_changes": 0
}
```

Create `jarvis/jarvis/doctype/jarvis_user_model_usage/jarvis_user_model_usage.py`:

```python
"""Jarvis User Model Usage — child table of Jarvis User Settings.

One row per (user, model, month_key). Holds the model's month-to-date token
counters plus the admin-set per-model cap (monthly_token_limit, 0 = unlimited).
Rows are written ONLY by server code in jarvis.chat.usage via atomic SQL (never
a desk save), so this controller stays minimal.
"""

from frappe.model.document import Document


class JarvisUserModelUsage(Document):
	# Identity is (parent, model, month_key). Counters + cap are mutated only by
	# jarvis.chat.usage; no validation hook needed.
	pass
```

Edit `jarvis/jarvis/doctype/jarvis_user_settings/jarvis_user_settings.json`:
add `"user_model_usage"` to `field_order` (immediately after `"last_synced_at"`), and append
this field object to the `fields` array (after the `last_synced_at` field):

```json
    {
      "fieldname": "user_model_usage",
      "fieldtype": "Table",
      "label": "Per-Model Usage",
      "options": "Jarvis User Model Usage",
      "permlevel": 1,
      "read_only": 1,
      "no_copy": 1,
      "description": "Per-model month-to-date token counters + per-model caps. Written only by server code (jarvis.chat.usage). 0 cap = unlimited. Bench keeps the current month only; admin persists history from the daily push."
    }
```

(Leave the parent `permissions` array unchanged — the permlevel-1 read grant to `Jarvis User`
already covers this field, so owners can read their own per-model usage.)

### 1d. Run — expect PASS

```bash
bench --site $SITE migrate
bench --site $SITE run-tests --module jarvis.tests.test_usage_per_model
pre-commit run --all-files
```

### 1e. Commit

```
feat(usage): Jarvis User Model Usage child table + permlevel-1 parent field
```

---

## Task 2 — Attribution: per-model upsert in `record_turn_usage` (+ helpers)

Record the actually-used model's delta into the child table, month-rollover-aware, pruning
old months but **carrying configured caps forward** (G8).

### 2a. Failing tests

Append to `jarvis/tests/test_usage_per_model.py`:

```python
class TestPerModelAttribution(_Base):
	def _row(self, **kw):
		base = {"totalTokensFresh": True, "inputTokens": 0, "outputTokens": 0,
				"totalTokens": 0, "model": "gpt-4o"}
		base.update(kw)
		return base

	def _child(self, model):
		return frappe.db.get_value(
			MODEL_USAGE,
			{"parent": USER_A, "parenttype": USETT, "parentfield": "user_model_usage",
			 "model": model, "month_key": usage.current_month_key()},
			["month_input_tokens", "month_output_tokens", "monthly_token_limit"],
			as_dict=True,
		)

	def test_upserts_and_accumulates_per_model(self):
		_make_session("agent:pm1", USER_A)
		usage.record_turn_usage("agent:pm1", self._row(model="gpt-4o", inputTokens=10, outputTokens=5))
		usage.record_turn_usage("agent:pm1", self._row(model="gpt-4o", inputTokens=8, outputTokens=12))
		usage.record_turn_usage("agent:pm1", self._row(model="claude-sonnet", inputTokens=3, outputTokens=4))
		g = self._child("gpt-4o")
		self.assertEqual(g.month_input_tokens, 18)
		self.assertEqual(g.month_output_tokens, 17)
		c = self._child("claude-sonnet")
		self.assertEqual(c.month_input_tokens, 3)
		self.assertEqual(c.month_output_tokens, 4)

	def test_missing_model_field_tolerated_no_child_row(self):
		_make_session("agent:pm2", USER_A)
		# No "model" key: aggregate still records; no child row is created; no raise.
		usage.record_turn_usage("agent:pm2", {"totalTokensFresh": True,
											   "inputTokens": 5, "outputTokens": 5})
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_value(USETT, {"user": USER_A}, "month_tokens")), 10
		)
		self.assertEqual(frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}), [])

	def test_month_rollover_prunes_stale_usage_and_carries_cap(self):
		_make_session("agent:pm3", USER_A)
		usage.record_turn_usage("agent:pm3", self._row(model="gpt-4o", inputTokens=10, outputTokens=5))
		# Give gpt-4o a cap and mark its row + the parent as a prior month.
		usage.set_model_limit(USER_A, "gpt-4o", 1000)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
			"month_key", "2020-01", update_modified=False,
		)
		# A zero-cap stale row that must be pruned on the next record.
		usage.record_turn_usage("agent:pm3", self._row(model="oldmodel", inputTokens=1, outputTokens=1))
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "oldmodel", "month_key": usage.current_month_key()},
			"month_key", "2020-01", update_modified=False,
		)
		frappe.db.commit()
		# New turn on gpt-4o this month: fresh current-month row, cap carried, delta reset.
		usage.record_turn_usage("agent:pm3", self._row(model="gpt-4o", inputTokens=7, outputTokens=3))
		g = self._child("gpt-4o")
		self.assertEqual(g.month_input_tokens, 7)         # reset to new delta
		self.assertEqual(g.month_output_tokens, 3)
		self.assertEqual(g.monthly_token_limit, 1000)     # cap survived rollover
		# Stale zero-cap row pruned; no stale gpt-4o row remains.
		self.assertEqual(
			frappe.get_all(MODEL_USAGE, filters={"parent": USER_A, "month_key": "2020-01"}), []
		)

	def test_set_model_limit_creates_row_without_usage(self):
		usage.get_or_create_user_settings(USER_A)
		usage.set_model_limit(USER_A, "gpt-4o", 500)
		g = self._child("gpt-4o")
		self.assertIsNotNone(g)
		self.assertEqual(g.monthly_token_limit, 500)
		self.assertEqual(g.month_input_tokens, 0)

	def test_never_raises_on_bad_model_write(self):
		# A malformed model value must not break the turn (G1). Empty string is
		# treated as "no model" and simply records the aggregate only.
		_make_session("agent:pm4", USER_A)
		usage.record_turn_usage("agent:pm4", self._row(model="", inputTokens=4, outputTokens=4))
		self.assertEqual(frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}), [])
```

### 2b. Run — expect FAIL

```bash
bench --site $SITE run-tests --module jarvis.tests.test_usage_per_model
```

Expected: `AttributeError: module 'jarvis.chat.usage' has no attribute 'set_model_limit'`,
and the attribution assertions fail (no child rows written).

### 2c. Implementation — edit `jarvis/chat/usage.py`

Add the module constants next to the existing ones (after line 29 `CHAT_SESSION = ...`):

```python
MODEL_USAGE = "Jarvis User Model Usage"
MODEL_USAGE_FIELD = "user_model_usage"
```

Add these helpers (place them after `record_turn_usage`, before `refresh_session_snapshots`):

```python
def _next_child_idx(user: str) -> int:
	"""Next 1-based idx for a new child row under ``user``'s settings. Child idx
	is ordering-only; correctness doesn't depend on it, but keep it monotone."""
	rows = frappe.db.sql(
		"""SELECT COALESCE(MAX(idx), 0) + 1
		   FROM `tabJarvis User Model Usage`
		   WHERE parent = %(user)s AND parenttype = %(ptype)s""",
		{"user": user, "ptype": USER_SETTINGS},
	)
	return int(rows[0][0]) if rows and rows[0] else 1


def _current_model_row_name(user: str, model: str, month: str) -> str | None:
	return frappe.db.get_value(
		MODEL_USAGE,
		{
			"parent": user, "parenttype": USER_SETTINGS,
			"parentfield": MODEL_USAGE_FIELD, "model": model, "month_key": month,
		},
		"name",
	)


def _prior_model_limit(user: str, model: str, month: str) -> int:
	"""Newest prior-month per-model cap for (user, model), so a configured cap
	survives the month rollover instead of resetting to 0. 0 when none exists."""
	rows = frappe.get_all(
		MODEL_USAGE,
		filters={
			"parent": user, "parenttype": USER_SETTINGS,
			"parentfield": MODEL_USAGE_FIELD, "model": model,
			"month_key": ["!=", month],
		},
		fields=["monthly_token_limit"],
		order_by="month_key desc",
		limit=1,
	)
	return int(rows[0].monthly_token_limit or 0) if rows else 0


def _insert_model_row(
	user: str, model: str, month: str, *,
	in_tokens: int, out_tokens: int, limit: int, now,
) -> None:
	"""Insert a fresh child row via raw SQL (the atomic idiom this module uses).
	Direct child-doc ORM insert is not used — Frappe routes child writes through
	the parent; a raw INSERT with an explicit hash name is the reliable path.
	``owner``/``modified_by`` are not permission-load-bearing for a child row
	(parent-row scoping governs child access), so ``Administrator`` is fine."""
	frappe.db.sql(
		"""
		INSERT INTO `tabJarvis User Model Usage`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 parent, parentfield, parenttype,
			 model, month_key, month_input_tokens, month_output_tokens, monthly_token_limit)
		VALUES
			(%(name)s, %(now)s, %(now)s, %(admin)s, %(admin)s, 0, %(idx)s,
			 %(user)s, %(pfield)s, %(ptype)s,
			 %(model)s, %(month)s, %(in)s, %(out)s, %(limit)s)
		""",
		{
			"name": frappe.generate_hash(length=10),
			"now": now, "admin": "Administrator", "idx": _next_child_idx(user),
			"user": user, "pfield": MODEL_USAGE_FIELD, "ptype": USER_SETTINGS,
			"model": model, "month": month,
			"in": int(in_tokens), "out": int(out_tokens), "limit": int(limit),
		},
	)


def _upsert_model_usage(
	user: str, model: str, month: str, in_tokens: int, out_tokens: int, now
) -> None:
	"""Upsert the (user, model, current-month) child row with this turn's delta.

	Month-to-date only on the bench (admin owns history, fleet spec §3): on
	rollover we drop this model's stale rows and start a fresh current-month row,
	inheriting any configured cap so a per-model limit is never lost. We also
	opportunistically drop the user's OTHER stale rows that carry no cap (pure
	usage history the admin already persisted); stale rows that DO carry a cap
	linger until their own model records a turn (which carries the cap forward),
	so an admin-set cap is never silently dropped."""
	name = _current_model_row_name(user, model, month)
	if name:
		frappe.db.sql(
			"""UPDATE `tabJarvis User Model Usage`
			   SET month_input_tokens = month_input_tokens + %(in)s,
				   month_output_tokens = month_output_tokens + %(out)s,
				   modified = %(now)s
			   WHERE name = %(name)s""",
			{"in": int(in_tokens), "out": int(out_tokens), "now": now, "name": name},
		)
	else:
		limit = _prior_model_limit(user, model, month)
		_insert_model_row(
			user, model, month, in_tokens=in_tokens, out_tokens=out_tokens,
			limit=limit, now=now,
		)
		# This model's stale-month rows (cap already carried forward) go now.
		frappe.db.sql(
			"""DELETE FROM `tabJarvis User Model Usage`
			   WHERE parent = %(user)s AND parenttype = %(ptype)s
				 AND parentfield = %(pfield)s AND model = %(model)s
				 AND month_key != %(month)s""",
			{"user": user, "ptype": USER_SETTINGS, "pfield": MODEL_USAGE_FIELD,
			 "model": model, "month": month},
		)
	# Opportunistic: drop stale usage-only rows (no cap) for all this user's models.
	frappe.db.sql(
		"""DELETE FROM `tabJarvis User Model Usage`
		   WHERE parent = %(user)s AND parenttype = %(ptype)s
			 AND parentfield = %(pfield)s AND month_key != %(month)s
			 AND COALESCE(monthly_token_limit, 0) = 0""",
		{"user": user, "ptype": USER_SETTINGS, "pfield": MODEL_USAGE_FIELD, "month": month},
	)


def set_model_limit(user: str, model: str, limit: int, now=None) -> None:
	"""Upsert the per-model cap on the current-month child row, creating the row
	(zero usage) when the model has no usage yet this month. Admin-gated by the
	caller (jarvis.chat.user_settings_api.admin_set_user_model_limit)."""
	now = now or frappe.utils.now_datetime()
	month = current_month_key()
	limit = max(0, int(limit or 0))
	name = _current_model_row_name(user, model, month)
	if name:
		frappe.db.sql(
			"""UPDATE `tabJarvis User Model Usage`
			   SET monthly_token_limit = %(limit)s, modified = %(now)s
			   WHERE name = %(name)s""",
			{"limit": limit, "now": now, "name": name},
		)
	else:
		_insert_model_row(user, model, month, in_tokens=0, out_tokens=0, limit=limit, now=now)
	frappe.db.commit()
```

Then wire the per-model upsert into `record_turn_usage`. Inside the existing `try` block,
after the aggregate `Jarvis User Settings` UPDATE and the `Jarvis Chat Session` UPDATE, and
**before** the final `frappe.db.commit()` (i.e. between the current lines 189 and 190), add:

```python
			# Per-model attribution (fleet spec §7): the gateway sessions row
			# carries the actually-used model (accurate even on pool "Auto", where
			# Bifrost picks server-side). Missing/blank model → aggregate only.
			model = (row.get("model") or "").strip()
			if model:
				_upsert_model_usage(user, model, month, input_tokens, output_tokens, now)
```

(This sits inside the same `try/except` that already guarantees G1 — never raises into the turn.)

### 2d. Run — expect PASS

```bash
bench --site $SITE run-tests --module jarvis.tests.test_usage_per_model
pre-commit run --all-files
```

### 2e. Commit

```
feat(usage): per-model token attribution with month-to-date pruning + cap carry-forward
```

---

## Task 3 — Enforcement: per-model gate in `policy.py` + `api.py` call site

### 3a. Failing tests

Create `jarvis/tests/test_policy_model_limit.py`:

```python
"""Per-model cap enforcement (fleet usage spec §7, §9). The aggregate gate is
the outer gate and is checked first; the per-model gate only fires when a
concrete model is passed. Pool "Auto" resolves to "" -> skipped (spec §2).
Fail-open on any error (G2)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import policy, usage, user_settings_api

USETT = "Jarvis User Settings"
MODEL_USAGE = "Jarvis User Model Usage"
USER_A = "jarvis-polmodel-a@example.test"


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Jarvis",
			"last_name": "PolModelTest", "enabled": 1, "send_welcome_email": 0,
			"user_type": "System User",
		}).insert(ignore_permissions=True)


def _cleanup() -> None:
	for name in frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}, pluck="name"):
		frappe.delete_doc(MODEL_USAGE, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(USETT, filters={"user": USER_A}, pluck="name"):
		frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)


class _Base(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _set_model_usage(self, model, used_in, used_out, limit):
		usage.get_or_create_user_settings(USER_A)
		usage.set_model_limit(USER_A, model, limit)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": model, "month_key": usage.current_month_key()},
			{"month_input_tokens": used_in, "month_output_tokens": used_out},
			update_modified=False,
		)
		frappe.db.commit()


class TestPerModelGate(_Base):
	def test_blocks_when_pinned_model_over_limit(self):
		self._set_model_usage("gpt-4o", 60, 50, 100)   # used 110 >= 100
		ok, reason = policy.validate_can_send(USER_A, model="gpt-4o")
		self.assertFalse(ok)
		self.assertEqual(reason, "usage_limit")

	def test_allows_when_pinned_model_under_limit(self):
		self._set_model_usage("gpt-4o", 20, 20, 100)   # used 40 < 100
		ok, reason = policy.validate_can_send(USER_A, model="gpt-4o")
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_empty_model_skips_gate(self):
		# Pool "Auto" resolves to "" -> per-model gate skipped even if another
		# model is over its cap.
		self._set_model_usage("gpt-4o", 999, 999, 100)
		ok, reason = policy.validate_can_send(USER_A, model="")
		self.assertTrue(ok)
		ok2, _ = policy.validate_can_send(USER_A, model=None)
		self.assertTrue(ok2)

	def test_zero_model_limit_is_unlimited(self):
		self._set_model_usage("gpt-4o", 999, 999, 0)
		ok, reason = policy.validate_can_send(USER_A, model="gpt-4o")
		self.assertTrue(ok)

	def test_aggregate_gate_fires_first(self):
		# Aggregate over-limit blocks regardless of model, and even with no
		# per-model row for the passed model.
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.db.set_value(
			USETT, {"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 150},
			update_modified=False,
		)
		frappe.db.commit()
		ok, reason = policy.validate_can_send(USER_A, model="some-model-with-no-row")
		self.assertFalse(ok)
		self.assertEqual(reason, "usage_limit")

	def test_fail_open_on_db_error(self):
		self._set_model_usage("gpt-4o", 999, 0, 100)   # would block if it ran
		with patch("frappe.db.get_value", side_effect=RuntimeError("boom")):
			ok, reason = policy.validate_can_send(USER_A, model="gpt-4o")
		# _over_model_limit swallowed the error and allowed the send (G2).
		self.assertTrue(ok)

	def test_no_row_for_model_allows(self):
		usage.get_or_create_user_settings(USER_A)
		ok, reason = policy.validate_can_send(USER_A, model="never-used")
		self.assertTrue(ok)
```

Also extend the existing `jarvis/tests/test_chat_policy.py` with a backward-compat assertion
(the added `model` kwarg must default to no-op):

```python
	def test_model_kwarg_defaults_to_no_gate(self):
		ok, reason = validate_can_send("nobody@example.invalid", model=None)
		self.assertTrue(ok)
		self.assertIsNone(reason)
```

### 3b. Run — expect FAIL

```bash
bench --site $SITE run-tests --module jarvis.tests.test_policy_model_limit
```

Expected: `TypeError: validate_can_send() got an unexpected keyword argument 'model'`.

### 3c. Implementation — edit `jarvis/chat/policy.py`

Replace `validate_can_send` and add `_over_model_limit`:

```python
def validate_can_send(user: str, model: str | None = None) -> tuple[bool, str | None]:
	if not user:
		return False, "no authenticated user"
	if user == "Guest":
		return False, "Guest users cannot use Jarvis chat"
	# Aggregate monthly cap is the OUTER gate — checked first (fleet spec §7).
	if _over_monthly_limit(user):
		return False, "usage_limit"
	# Per-model cap: only when a concrete model is known. ``model`` is resolved
	# in chat.api (which knows the conversation) and passed in as a plain string,
	# so policy never imports turn_handler (no import cycle). Pool "Auto" resolves
	# to "" -> per-model gate skipped (accepted enforcement gap, spec §2).
	if model and _over_model_limit(user, model):
		return False, "usage_limit"
	return True, None


def _over_model_limit(user: str, model: str) -> bool:
	"""True iff ``user`` has a positive per-model cap for ``model`` this month and
	this month's per-model usage has reached it. Rollover-aware: the lookup is
	scoped to the current month_key, so a stale row reads as no cap. Fails open on
	any error — an accounting lookup bug must never block a legitimate send (G2)."""
	try:
		if not model:
			return False
		from jarvis.chat.usage import (
			MODEL_USAGE,
			MODEL_USAGE_FIELD,
			current_month_key,
		)

		row = frappe.db.get_value(
			MODEL_USAGE,
			{
				"parent": user, "parenttype": "Jarvis User Settings",
				"parentfield": MODEL_USAGE_FIELD, "model": model,
				"month_key": current_month_key(),
			},
			["monthly_token_limit", "month_input_tokens", "month_output_tokens"],
			as_dict=True,
		)
		if not row:
			return False
		limit = int(row.monthly_token_limit or 0)
		if limit <= 0:
			return False
		used = int(row.month_input_tokens or 0) + int(row.month_output_tokens or 0)
		return used >= limit
	except Exception:
		frappe.log_error(
			title="jarvis usage: per-model limit check failed (allowing send)",
			message=frappe.get_traceback(),
		)
		return False
```

### 3d. Implementation — edit `jarvis/chat/api.py` (the send_message call site)

The aggregate gate at line **816** stays exactly as-is (`ok, reason = validate_can_send(user)`
— model unknown at that point, so this is aggregate-only, fail-fast, "FIRST").

Add the per-model gate **after** the conversation is resolved and any `model_override` is
applied. Insert immediately after the `thinking_override` block ends (after the current
line 882 `conv_doc.thinking_override = level`), before the "Non-image files..." comment:

```python
	# Per-model enforcement (fleet spec §7): now that the conversation (and any
	# fresh model_override) is settled, resolve the effective model and re-check
	# the caps. Resolved HERE (not in policy) so policy stays import-light and
	# never imports turn_handler (import cycle). Pool "Auto" -> "" -> the per-model
	# gate is skipped inside validate_can_send (spec §2). The aggregate gate is
	# re-evaluated (cheap, idempotent, fail-open) so there is one validated entry.
	try:
		from jarvis.chat.turn_handler import _resolve_model_and_provider

		eff_model, _prov = _resolve_model_and_provider(conv_doc)
	except Exception:
		eff_model = ""
	ok, reason = validate_can_send(user, model=eff_model)
	if not ok:
		return {"ok": False, "reason": reason}
```

_Note:_ the existing `test_user_settings.py::test_send_message_surfaces_usage_limit` still
passes — the aggregate gate at 816 fires first for an over-aggregate user, before the new
block is reached.

### 3e. Run — expect PASS

```bash
bench --site $SITE run-tests --module jarvis.tests.test_policy_model_limit
bench --site $SITE run-tests --module jarvis.tests.test_chat_policy
bench --site $SITE run-tests --module jarvis.tests.test_user_settings   # regression: aggregate gate + send_message
pre-commit run --all-files
```

### 3f. Commit

```
feat(policy): per-model send-time cap enforcement (aggregate stays outer gate; Auto skipped)
```

---

## Task 4 — Admin API: `admin_set_user_model_limit` + per-model in payloads

### 4a. Failing tests

Create `jarvis/tests/test_user_model_limit_api.py`:

```python
"""admin_set_user_model_limit + per-model rows in the settings/admin payloads
(fleet usage spec §7). Admin-gated; mirrors admin_set_user_limit."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage, user_settings_api
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

USETT = "Jarvis User Settings"
MODEL_USAGE = "Jarvis User Model Usage"
USER_A = "jarvis-umlapi-a@example.test"
USER_ADMIN = "jarvis-umlapi-admin@example.test"
USER_PLAIN = "jarvis-umlapi-plain@example.test"
_ALL = (USER_A, USER_ADMIN, USER_PLAIN)


def _ensure_user(email, roles=()):
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Jarvis",
			"last_name": "UmlApi", "enabled": 1, "send_welcome_email": 0,
			"user_type": "System User",
		}).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)


def _strip_admin(email):
	doc = frappe.get_doc("User", email)
	drop = {r.role for r in doc.get("roles", [])} & {"System Manager", JARVIS_ADMIN_ROLE}
	if drop:
		doc.remove_roles(*drop)


def _cleanup():
	for email in _ALL:
		for n in frappe.get_all(MODEL_USAGE, filters={"parent": email}, pluck="name"):
			frappe.delete_doc(MODEL_USAGE, n, ignore_permissions=True, force=True)
		for n in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, n, ignore_permissions=True, force=True)


class _Base(FrappeTestCase):
	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_user(USER_A, (JARVIS_USER_ROLE,))
		_ensure_user(USER_ADMIN, (JARVIS_ADMIN_ROLE,))
		_ensure_user(USER_PLAIN, (JARVIS_USER_ROLE,))
		_strip_admin(USER_A)
		_strip_admin(USER_PLAIN)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig)


class TestAdminSetModelLimit(_Base):
	def test_creates_row_and_sets_cap(self):
		out = user_settings_api.admin_set_user_model_limit(
			user=USER_A, model="gpt-4o", monthly_token_limit=500)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["model"], "gpt-4o")
		self.assertEqual(out["data"]["monthly_token_limit"], 500)
		self.assertEqual(
			frappe.db.get_value(
				MODEL_USAGE,
				{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
				"monthly_token_limit"),
			500,
		)

	def test_unknown_user_refused(self):
		out = user_settings_api.admin_set_user_model_limit(
			user="nobody@example.invalid", model="gpt-4o", monthly_token_limit=1)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "unknown_user")

	def test_blank_model_refused(self):
		out = user_settings_api.admin_set_user_model_limit(
			user=USER_A, model="  ", monthly_token_limit=1)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_model")

	def test_plain_user_refused(self):
		frappe.set_user(USER_PLAIN)
		with self.assertRaises(frappe.PermissionError):
			user_settings_api.admin_set_user_model_limit(
				user=USER_A, model="gpt-4o", monthly_token_limit=1)

	def test_admin_list_includes_per_model(self):
		_make = usage.get_or_create_user_settings(USER_A)  # noqa: F841
		usage.set_model_limit(USER_A, "gpt-4o", 500)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
			{"month_input_tokens": 30, "month_output_tokens": 10}, update_modified=False)
		frappe.db.commit()
		out = user_settings_api.admin_list_user_usage()
		row = {r["user"]: r for r in out["data"]}[USER_A]
		self.assertIn("per_model", row)
		pm = {m["model"]: m for m in row["per_model"]}
		self.assertIn("gpt-4o", pm)
		self.assertEqual(pm["gpt-4o"]["month_tokens"], 40)
		self.assertEqual(pm["gpt-4o"]["monthly_token_limit"], 500)

	def test_get_my_settings_includes_per_model(self):
		usage.set_model_limit(USER_A, "gpt-4o", 500)
		frappe.set_user(USER_A)
		out = user_settings_api.get_my_settings()
		self.assertIn("per_model", out["data"])
		self.assertTrue(any(m["model"] == "gpt-4o" for m in out["data"]["per_model"]))
```

Add the role-gate registration test by extending `jarvis/tests/test_role_gates.py`
`GATED_ENDPOINTS` (after the `admin_set_user_limit` entry, ~line 62):

```python
	("jarvis.chat.user_settings_api", "admin_set_user_model_limit",
		{"user": "Administrator", "model": "gpt-4o", "monthly_token_limit": 0}),
```

### 4b. Run — expect FAIL

```bash
bench --site $SITE run-tests --module jarvis.tests.test_user_model_limit_api
```

Expected: `AttributeError: ... has no attribute 'admin_set_user_model_limit'`, and the
`per_model` assertions fail.

### 4c. Implementation — edit `jarvis/chat/user_settings_api.py`

Add a small helper (place after `_month_tokens_effective`, before `_settings_payload`):

```python
def _per_model_rows(user: str) -> list[dict]:
	"""Current-month per-model usage + caps for ``user`` (fleet spec §7). Empty
	list when no rows. Ordered by usage descending for a stable UI."""
	rows = frappe.get_all(
		usage.MODEL_USAGE,
		filters={
			"parent": user, "parenttype": USER_SETTINGS,
			"parentfield": usage.MODEL_USAGE_FIELD, "month_key": usage.current_month_key(),
		},
		fields=["model", "month_input_tokens", "month_output_tokens", "monthly_token_limit"],
		order_by="month_input_tokens desc",
	)
	out = []
	for r in rows:
		mi = int(r.month_input_tokens or 0)
		mo = int(r.month_output_tokens or 0)
		out.append({
			"model": r.model,
			"month_input_tokens": mi,
			"month_output_tokens": mo,
			"month_tokens": mi + mo,
			"monthly_token_limit": int(r.monthly_token_limit or 0),
		})
	return out
```

Extend `_settings_payload` — add one line before the closing `}`:

```python
		"per_model": _per_model_rows(doc.user),
```

Extend `admin_list_user_usage` — in the per-user `out.append({...})` dict, add:

```python
			"per_model": _per_model_rows(r.user),
```

(Leave the batched `user_map` / `frappe.get_all` structure as-is; `_per_model_rows` is one
indexed child query per returned user — acceptable at the tenant-user scale this board serves.)

Add the new whitelisted method (place after `admin_set_user_limit`):

```python
@frappe.whitelist()
def admin_set_user_model_limit(
	user: str, model: str, monthly_token_limit: int = 0
) -> dict:
	"""Set a user's PER-MODEL monthly token cap (0 = unlimited), creating the
	settings row + current-month child row if absent. Mirrors admin_set_user_limit.
	Admins only (server re-checks; the SPA gate is UX)."""
	require_jarvis_admin()
	if not user or not frappe.db.exists("User", user):
		return {"ok": False, "reason": "unknown_user"}
	model = (model or "").strip()
	if not model:
		return {"ok": False, "reason": "invalid_model"}
	limit = max(0, cint(monthly_token_limit))
	usage.get_or_create_user_settings(user)
	usage.set_model_limit(user, model, limit)
	return {"ok": True, "data": {"user": user, "model": model, "monthly_token_limit": limit}}
```

### 4d. Implementation — edit `jarvis/chat/api.py` `_measured_usage`

So `UsagePane.vue` can render per-model bars from `get_usage()`. In `_measured_usage`, add
`"per_model": []` to the initial `measured` dict, and populate it when a row exists. Concretely:

- In the `measured = { ... }` literal (after `"last_usage_at": None,`) add:

```python
		"per_model": [],
```

- The no-row managed path already returns this `measured` (with empty `per_model`) — fine.
- In the `row`-exists branch, after the existing `measured.update({...})` call (after the
  line `return measured` region — i.e. just before the final `return measured` at the end of
  the function), add:

```python
	measured["per_model"] = [
		{
			"model": r.model,
			"month_input_tokens": int(r.month_input_tokens or 0),
			"month_output_tokens": int(r.month_output_tokens or 0),
			"month_tokens": int(r.month_input_tokens or 0) + int(r.month_output_tokens or 0),
			"monthly_token_limit": int(r.monthly_token_limit or 0),
		}
		for r in frappe.get_all(
			"Jarvis User Model Usage",
			filters={
				"parent": user, "parenttype": "Jarvis User Settings",
				"parentfield": "user_model_usage", "month_key": _usage_month_key(),
			},
			fields=["model", "month_input_tokens", "month_output_tokens", "monthly_token_limit"],
			order_by="month_input_tokens desc",
		)
	]
	return measured
```

(`_usage_month_key()` is the same helper the surrounding measured block already uses.)

### 4e. Run — expect PASS

```bash
bench --site $SITE run-tests --module jarvis.tests.test_user_model_limit_api
bench --site $SITE run-tests --module jarvis.tests.test_role_gates      # CI is authority; run locally as smoke
bench --site $SITE run-tests --module jarvis.tests.test_user_settings   # regression: _measured_usage
pre-commit run --all-files
```

### 4f. Commit

```
feat(usage-api): admin_set_user_model_limit + per-model rows in settings/admin/measured payloads
```

---

## Task 5 — Daily push job + `admin_client.push_usage_rollup`

Build the month-to-date snapshot and POST it to admin. Best-effort; never affects chat.

### 5a. Failing tests

Create `jarvis/tests/test_usage_push.py`:

```python
"""Daily month-to-date usage rollup push (Architecture A, fleet spec §3/§5).
admin_client is mocked; a push failure is swallowed; self-hosted / unconfigured
skip; payload cap logged."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage, usage_push

USETT = "Jarvis User Settings"
MODEL_USAGE = "Jarvis User Model Usage"
USER_A = "jarvis-push-a@example.test"


def _ensure_user(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Jarvis",
			"last_name": "PushTest", "enabled": 1, "send_welcome_email": 0,
			"user_type": "System User",
		}).insert(ignore_permissions=True)


def _cleanup():
	for n in frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}, pluck="name"):
		frappe.delete_doc(MODEL_USAGE, n, ignore_permissions=True, force=True)
	for n in frappe.get_all(USETT, filters={"user": USER_A}, pluck="name"):
		frappe.delete_doc(USETT, n, ignore_permissions=True, force=True)


class _Base(FrappeTestCase):
	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig)

	def _seed(self):
		usage.get_or_create_user_settings(USER_A)
		month = usage.current_month_key()
		frappe.db.set_value(
			USETT, {"user": USER_A},
			{"usage_month": month, "month_input_tokens": 30,
			 "month_output_tokens": 20, "month_tokens": 50}, update_modified=False)
		usage.set_model_limit(USER_A, "gpt-4o", 0)
		frappe.db.set_value(
			MODEL_USAGE, {"parent": USER_A, "model": "gpt-4o", "month_key": month},
			{"month_input_tokens": 18, "month_output_tokens": 12}, update_modified=False)
		frappe.db.commit()


class TestBuildRollup(_Base):
	def test_payload_shape(self):
		self._seed()
		rollup, truncated = usage_push._build_rollup()
		self.assertFalse(truncated)
		self.assertEqual(rollup["month_key"], usage.current_month_key())
		u = {x["email"]: x for x in rollup["users"]}[USER_A]
		self.assertEqual(u["tokens_in"], 30)
		self.assertEqual(u["tokens_out"], 20)
		self.assertEqual(u["total_tokens"], 50)
		self.assertEqual(u["per_model"], {"gpt-4o": {"in": 18, "out": 12}})

	def test_cap_truncates_and_flags(self):
		self._seed()
		rollup, truncated = usage_push._build_rollup(cap=0)
		self.assertTrue(truncated)
		self.assertEqual(rollup["users"], [])


class TestPushJob(_Base):
	def test_pushes_when_configured(self):
		self._seed()
		with patch("jarvis.selfhost.is_self_hosted", return_value=False), \
			 patch.object(usage_push, "_admin_configured", return_value=True), \
			 patch("jarvis.admin_client.push_usage_rollup", return_value={"ok": True}) as push:
			usage_push.push_usage_rollup()
		push.assert_called_once()
		sent = push.call_args.args[0]
		self.assertEqual(sent["month_key"], usage.current_month_key())
		self.assertTrue(any(x["email"] == USER_A for x in sent["users"]))

	def test_self_hosted_skips(self):
		self._seed()
		with patch("jarvis.selfhost.is_self_hosted", return_value=True), \
			 patch("jarvis.admin_client.push_usage_rollup") as push:
			usage_push.push_usage_rollup()
		push.assert_not_called()

	def test_unconfigured_skips(self):
		self._seed()
		with patch("jarvis.selfhost.is_self_hosted", return_value=False), \
			 patch.object(usage_push, "_admin_configured", return_value=False), \
			 patch("jarvis.admin_client.push_usage_rollup") as push:
			usage_push.push_usage_rollup()
		push.assert_not_called()

	def test_failure_is_swallowed(self):
		self._seed()
		from jarvis.exceptions import AdminUnreachableError
		with patch("jarvis.selfhost.is_self_hosted", return_value=False), \
			 patch.object(usage_push, "_admin_configured", return_value=True), \
			 patch("jarvis.admin_client.push_usage_rollup",
				   side_effect=AdminUnreachableError("down")), \
			 patch("frappe.log_error") as logged:
			usage_push.push_usage_rollup()   # must NOT raise
		self.assertTrue(logged.called)

	def test_not_onboarded_is_quiet_skip(self):
		self._seed()
		from jarvis.exceptions import AdminAuthError
		with patch("jarvis.selfhost.is_self_hosted", return_value=False), \
			 patch.object(usage_push, "_admin_configured", return_value=True), \
			 patch("jarvis.admin_client.push_usage_rollup",
				   side_effect=AdminAuthError("not onboarded")), \
			 patch("frappe.log_error") as logged:
			usage_push.push_usage_rollup()   # must NOT raise, and not log_error
		self.assertFalse(logged.called)
```

### 5b. Run — expect FAIL

```bash
bench --site $SITE run-tests --module jarvis.tests.test_usage_push
```

Expected: `ModuleNotFoundError: No module named 'jarvis.chat.usage_push'`.

### 5c. Implementation — create `jarvis/chat/usage_push.py`

```python
"""Daily month-to-date usage rollup push to admin (Architecture A, fleet usage
spec §3/§5).

The bench holds month-to-date running counters (per user + per model), not a
per-day ledger, so the push is an idempotent month-to-date SNAPSHOT: admin
upserts on (tenant, user, month) and owns history. Best-effort — a push failure
never affects chat. Self-hosted benches (no managed container) and un-onboarded
benches (no admin credentials) simply don't push; admin then shows "no usage".
"""

from __future__ import annotations

import frappe

from jarvis.chat import usage
from jarvis.exceptions import AdminAuthError

USER_SETTINGS = usage.USER_SETTINGS
MODEL_USAGE = usage.MODEL_USAGE
MODEL_USAGE_FIELD = usage.MODEL_USAGE_FIELD

# Hard cap on users per push (spec §7). Bounds payload size; extra users are
# dropped (highest-usage first kept) and the truncation is logged.
_MAX_USERS = 500


def _admin_configured() -> bool:
	"""jarvis_admin_url is set (site config outranks the Settings field). Mirrors
	installed_apps_sync._admin_configured. Credential *presence* is not checked
	here — admin_client raises AdminAuthError when unonboarded, which the caller
	treats as a quiet skip."""
	try:
		if (frappe.conf.get("jarvis_admin_url") or "").strip():
			return True
		settings = frappe.get_cached_doc(USER_SETTINGS if False else "Jarvis Settings")
		return bool((settings.get("jarvis_admin_url") or "").strip())
	except Exception:
		return False


def _build_rollup(cap: int = _MAX_USERS) -> tuple[dict, bool]:
	"""Month-to-date snapshot: every settings row on the CURRENT month, highest
	usage first, capped. Returns (rollup, truncated). per_model is a dict keyed by
	model -> {in, out} (the pinned ingest contract)."""
	month = usage.current_month_key()
	rows = frappe.get_all(
		USER_SETTINGS,
		filters={"usage_month": month},
		fields=["name as user", "month_input_tokens", "month_output_tokens", "month_tokens"],
		order_by="month_tokens desc",
	)
	truncated = len(rows) > cap
	rows = rows[:cap]
	users = []
	for s in rows:
		per_model: dict[str, dict] = {}
		for r in frappe.get_all(
			MODEL_USAGE,
			filters={
				"parent": s.user, "parenttype": USER_SETTINGS,
				"parentfield": MODEL_USAGE_FIELD, "month_key": month,
			},
			fields=["model", "month_input_tokens", "month_output_tokens"],
		):
			if not r.model:
				continue
			per_model[r.model] = {
				"in": int(r.month_input_tokens or 0),
				"out": int(r.month_output_tokens or 0),
			}
		users.append({
			"email": s.user,
			"tokens_in": int(s.month_input_tokens or 0),
			"tokens_out": int(s.month_output_tokens or 0),
			"total_tokens": int(s.month_tokens or 0),
			"per_model": per_model,
		})
	return {"month_key": month, "users": users}, truncated


def push_usage_rollup() -> None:
	"""Daily scheduler entry. Self-gating + best-effort; NEVER raises."""
	try:
		from jarvis import selfhost

		if selfhost.is_self_hosted():
			return
		if not _admin_configured():
			return
		rollup, truncated = _build_rollup()
		if truncated:
			frappe.logger("jarvis.usage_push").warning(
				"usage rollup truncated at %s users", _MAX_USERS)
		if not rollup["users"]:
			return
		from jarvis import admin_client

		admin_client.push_usage_rollup(rollup)
	except AdminAuthError:
		# Not onboarded / no admin credentials (self-hosted-ish). Nothing to push;
		# not an error condition, so don't log_error.
		return
	except Exception:
		frappe.log_error(
			title="jarvis usage: rollup push failed",
			message=frappe.get_traceback(),
		)
```

> Note: the `_admin_configured` body above mirrors `jarvis/installed_apps_sync.py`
> `_admin_configured` (URL check via `frappe.conf` then `Jarvis Settings`). Simplify the
> `get_cached_doc(...)` line to `frappe.get_cached_doc("Jarvis Settings")` — the `if False`
> ternary is only there to make the constant reuse obvious; drop it when typing it out.

### 5d. Implementation — add `push_usage_rollup` to `jarvis/admin_client.py`

Place next to `get_llm_usage` (~line 658), following the same `_post` / `_m` idiom:

```python
def push_usage_rollup(rollup: dict) -> dict:
	"""Push the bench's month-to-date per-user + per-model usage rollup to admin
	(Architecture A, fleet usage spec §3). Idempotent snapshot; admin upserts on
	(tenant, user, month). Called best-effort from the usage_push daily cron.
	Raises AdminAuthError / AdminUnreachableError / AdminValidationError."""
	return _post(path=_m("api.tenant.ingest_usage_rollup"), body={"rollup": rollup})
```

### 5e. Implementation — register the cron in `jarvis/hooks.py`

In `scheduler_events["daily"]` (the list starting ~line 287), add:

```python
		# Architecture A (fleet usage spec §3/§5): best-effort daily push of the
		# bench's month-to-date per-user + per-model usage rollup to admin. Self-
		# gating (skips self-hosted / unconfigured / not-onboarded); never raises.
		"jarvis.chat.usage_push.push_usage_rollup",
```

### 5f. Run — expect PASS

```bash
bench --site $SITE run-tests --module jarvis.tests.test_usage_push
pre-commit run --all-files
```

### 5g. Commit

```
feat(usage): daily month-to-date rollup push to admin (best-effort, self-gating)
```

---

## Task 6 — UI (DO LAST; rebase first per G7 / Task 0)

Rebase on latest `develop`/`beta` before starting. These are frontend-only changes; there is
no Python TDD loop — verify by build + manual smoke. Full SFCs below.

### 6a. `frontend/src/api.js` — one-line wrapper

Add immediately after the `adminSetUserLimit` export (~line 55):

```javascript
export const adminSetUserModelLimit = (user, model, monthlyTokenLimit) =>
	call(US + "admin_set_user_model_limit", { user, model, monthly_token_limit: monthlyTokenLimit })
```

### 6b. `frontend/src/components/settings/UsagePane.vue` — full SFC

Adds a per-model section under the measured block and relabels the legacy budget block.

```vue
<template>
	<div class="jv-settings-body">
		<template v-if="measured">
			<div class="jv-set-sec">Measured usage</div>
			<div class="jv-set-row"><span>{{ usage.month_label || "This month" }}</span><b>{{ fmtTokens(measured.month_tokens) }}</b></div>
			<div class="jv-set-row"><span>All time</span><b>{{ fmtTokens(measured.total_tokens) }}</b></div>
			<div v-if="measured.last_usage_at" class="jv-set-row"><span>Last activity</span><b>{{ timeAgo(measured.last_usage_at) }}</b></div>
			<template v-if="measured.monthly_token_limit > 0">
				<div class="jv-usage-bar"><div class="jv-usage-fill" :style="{ width: measuredPct + '%' }"></div></div>
				<div class="jv-set-hint">{{ fmtTokens(measured.month_tokens) }} / {{ fmtTokens(measured.monthly_token_limit) }} this month · {{ measuredPct }}%</div>
			</template>
			<div v-else class="jv-set-hint">No monthly limit set on your account.</div>

			<template v-if="perModel.length">
				<div class="jv-set-sec" style="margin-top:20px;">By model · this month</div>
				<div v-for="m in perModel" :key="m.model" class="jv-model-row">
					<div class="jv-model-head">
						<span class="jv-model-name">{{ m.model }}</span>
						<span class="jv-model-tok">{{ fmtTokens(m.month_tokens) }}<span class="jv-model-io"> · {{ fmtTokens(m.month_input_tokens) }} in / {{ fmtTokens(m.month_output_tokens) }} out</span></span>
					</div>
					<template v-if="m.monthly_token_limit > 0">
						<div class="jv-usage-bar"><div class="jv-usage-fill" :style="{ width: modelPct(m) + '%' }"></div></div>
						<div class="jv-set-hint">{{ fmtTokens(m.month_tokens) }} / {{ fmtTokens(m.monthly_token_limit) }} · {{ modelPct(m) }}%</div>
					</template>
					<div v-else class="jv-set-hint">unlimited</div>
				</div>
			</template>
		</template>

		<div style="font-size:12px;color:var(--text-3);margin-bottom:14px;" :style="{ marginTop: measured ? '20px' : '0' }">Estimated tokens, messages and tool activity for your workspace. <span class="jv-est">est.</span></div>
		<div class="jv-statgrid">
			<div class="jv-stat"><div class="jv-stat-label">Messages</div><div class="jv-stat-val">{{ s ? s.msgCount : "—" }}</div><div class="jv-stat-sub">{{ s ? `${s.userMsgCount} you · ${s.assistantMsgCount} Jarvis` : "no chat" }}</div></div>
			<div class="jv-stat"><div class="jv-stat-label">Tool calls</div><div class="jv-stat-val">{{ s ? s.sessionToolCalls : "—" }}</div><div class="jv-stat-sub">this session</div></div>
			<div class="jv-stat"><div class="jv-stat-label">Avg tokens / msg</div><div class="jv-stat-val">{{ s ? s.avgTokensPerMsg : "—" }}</div><div class="jv-stat-sub">this chat</div></div>
			<div class="jv-stat"><div class="jv-stat-label">Conversations</div><div class="jv-stat-val">{{ s ? s.convCount : "—" }}</div><div class="jv-stat-sub">{{ s ? `${s.starredCount} starred` : "no chat" }}</div></div>
			<div class="jv-stat"><div class="jv-stat-label">This chat</div><div class="jv-stat-val">{{ usage ? fmtTokens(usage.chat_tokens) : "—" }}</div><div class="jv-stat-sub">tokens</div></div>
			<div class="jv-stat"><div class="jv-stat-label">{{ usage ? usage.month_label : "This month" }}</div><div class="jv-stat-val">{{ usage ? fmtTokens(usage.month_tokens) : "—" }}</div><div class="jv-stat-sub">tokens</div></div>
			<div class="jv-stat"><div class="jv-stat-label">All time</div><div class="jv-stat-val">{{ usage ? fmtTokens(usage.total_tokens) : "—" }}</div><div class="jv-stat-sub">tokens</div></div>
			<div class="jv-stat"><div class="jv-stat-label">Tools</div><div class="jv-stat-val">{{ s ? s.toolCount : "—" }}</div><div class="jv-stat-sub">available</div></div>
		</div>
		<template v-if="usage && usage.budget_monthly">
			<div class="jv-set-sec" style="margin-top:20px;">Tenant monthly budget (informational)</div>
			<div class="jv-usage-bar"><div class="jv-usage-fill" :style="{ width: usagePct + '%' }"></div></div>
			<div class="jv-set-hint">{{ fmtTokens(usage.month_tokens) }} / {{ fmtTokens(usage.budget_monthly) }} this month · {{ usagePct }}%</div>
		</template>
		<div v-else class="jv-set-hint" style="margin-top:14px;">No monthly budget set · token counts are estimated from message text.</div>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from "vue"
import { useShellStore } from "@/stores/shell"
import { timeAgo } from "@/utils/datetime"
import * as api from "@/api"

const shell = useShellStore()
const s = computed(() => shell.chatContext?.sessionStats || null)

const usage = ref(null)

// Real (gateway-recorded) usage, added to get_usage()'s response. null until the
// backend ships it or the user has no recorded usage yet (self-hosted stays null).
const measured = computed(() => (usage.value && usage.value.measured) || null)
const measuredPct = computed(() => {
	const m = measured.value
	if (!m || !m.monthly_token_limit) return 0
	return Math.min(100, Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100))
})

// Per-model current-month usage + caps (fleet usage spec §7).
const perModel = computed(() => (measured.value && measured.value.per_model) || [])
function modelPct(m) {
	if (!m || !m.monthly_token_limit) return 0
	return Math.min(100, Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100))
}

async function loadUsage() {
	try {
		usage.value = await api.getUsage(shell.chatContext?.conversationId)
	} catch {
		usage.value = null
	}
}

function fmtTokens(n) {
	n = Number(n || 0)
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M"
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k"
	return String(n)
}

const usagePct = computed(() => {
	const u = usage.value
	if (!u || !u.budget_monthly) return 0
	return Math.min(100, Math.round((u.month_tokens / u.budget_monthly) * 100))
})

onMounted(loadUsage)
watch(() => shell.chatContext?.conversationId, loadUsage)
</script>

<style scoped>
.jv-model-row { margin-top: 12px; }
.jv-model-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.jv-model-name { font-size: 13px; font-weight: 600; color: var(--text); }
.jv-model-tok { font-size: 12px; color: var(--text-2); }
.jv-model-io { color: var(--text-3); }
</style>
```

### 6c. `frontend/src/components/settings/UsageAdminPane.vue` — full SFC

Adds a chevron toggle per user row that expands per-model usage + an editable per-model cap.

```vue
<template>
	<div class="jv-settings-body">
		<div class="jv-usr-head">
			<div class="jv-set-sec" style="margin:0;">Team usage</div>
			<button class="jv-btn jv-btn--sm jv-btn--ghost" :disabled="syncing" @click="onSync">
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2v6h6M21 12a9 9 0 1 1-3-6.7L21 8" /></svg>
				{{ syncing ? "Syncing…" : "Sync from agent" }}
			</button>
		</div>

		<div v-if="syncReason" class="jv-run-err" style="margin-bottom:12px;">
			<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4M12 17h.01" /></svg>
			{{ syncReason }}
		</div>
		<div v-else-if="syncResult" class="jv-set-hint" style="color:var(--green);margin-bottom:12px;">{{ syncResult }}</div>

		<div v-if="loadError" class="jv-mon-note">
			Could not load usage. <button type="button" class="jv-mon-retry" @click="loadUsers">Retry</button>
		</div>
		<div v-else-if="loading && !users.length" class="jv-mon-note">Loading…</div>
		<div v-else-if="!users.length" class="jv-set-empty" style="text-align:center;padding:30px 0;">No users with settings or usage yet.</div>

		<template v-else>
			<div class="jv-usr-row jv-usr-headrow">
				<div>User</div>
				<div>This month</div>
				<div>Monthly limit</div>
				<div>Last activity</div>
			</div>
			<template v-for="u in users" :key="u.user">
				<div class="jv-usr-row">
					<div class="jv-usr-id">
						<button
							v-if="(u.per_model || []).length"
							type="button" class="jv-usr-chev" :class="{ 'jv-usr-chev--open': expanded[u.user] }"
							@click="toggle(u.user)" :aria-expanded="!!expanded[u.user]" title="Per-model usage"
						>
							<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6" /></svg>
						</button>
						<span v-else class="jv-usr-chev jv-usr-chev--placeholder"></span>
						<div>
							<div class="jv-usr-name">{{ u.full_name || u.user }}</div>
							<div class="jv-usr-email">{{ u.user }}</div>
						</div>
					</div>
					<div class="jv-usr-meter">
						<template v-if="u.monthly_token_limit > 0">
							<div class="jv-usage-bar"><div class="jv-usage-fill" :style="{ width: pct(u) + '%' }"></div></div>
							<div class="jv-set-hint">{{ fmtTokens(u.month_tokens) }} / {{ fmtTokens(u.monthly_token_limit) }} · {{ pct(u) }}%</div>
						</template>
						<div v-else class="jv-set-hint">{{ fmtTokens(u.month_tokens) }} this month · unlimited</div>
						<div class="jv-usr-totalhint">{{ fmtTokens(u.total_tokens) }} total</div>
					</div>
					<div class="jv-usr-limit">
						<input
							type="number" min="0" step="1000" class="jv-usr-limitinput"
							v-model.number="u._limitDraft" :disabled="u._saving" placeholder="0 = unlimited"
						/>
						<button
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="u._saving || Number(u._limitDraft || 0) === Number(u.monthly_token_limit || 0)"
							@click="saveLimit(u)"
						>{{ u._saving ? "…" : "Save" }}</button>
					</div>
					<div class="jv-usr-last">{{ u.last_usage_at ? timeAgo(u.last_usage_at) : "—" }}</div>
				</div>

				<div v-if="expanded[u.user]" class="jv-model-block">
					<div v-for="m in (u.per_model || [])" :key="m.model" class="jv-model-erow">
						<div class="jv-model-ename">{{ m.model }}</div>
						<div class="jv-model-emeter">
							<template v-if="m.monthly_token_limit > 0">
								<div class="jv-usage-bar"><div class="jv-usage-fill" :style="{ width: modelPct(m) + '%' }"></div></div>
								<div class="jv-set-hint">{{ fmtTokens(m.month_tokens) }} / {{ fmtTokens(m.monthly_token_limit) }} · {{ modelPct(m) }}%</div>
							</template>
							<div v-else class="jv-set-hint">{{ fmtTokens(m.month_tokens) }} · unlimited</div>
						</div>
						<div class="jv-usr-limit">
							<input
								type="number" min="0" step="1000" class="jv-usr-limitinput"
								v-model.number="m._limitDraft" :disabled="m._saving" placeholder="0 = unlimited"
							/>
							<button
								class="jv-btn jv-btn--sm jv-btn--ghost"
								:disabled="m._saving || Number(m._limitDraft || 0) === Number(m.monthly_token_limit || 0)"
								@click="saveModelLimit(u, m)"
							>{{ m._saving ? "…" : "Save" }}</button>
						</div>
					</div>
				</div>
			</template>
		</template>
	</div>
</template>

<script setup>
// Tenant-admin usage table (fleet usage spec §7). Gated at the SettingsDialog
// level by window.is_jarvis_admin — the server re-checks require_jarvis_admin()
// independently on every call, so a stale client gate can only hide the nav
// item, never bypass the real permission.
import { ref, reactive, onMounted } from "vue"
import { toast } from "frappe-ui"
import { timeAgo } from "@/utils/datetime"
import * as api from "@/api"

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong."
}

const users = ref([])
const loading = ref(false)
const loadError = ref(false)
const expanded = reactive({})

function toggle(user) {
	expanded[user] = !expanded[user]
}

async function loadUsers() {
	loading.value = true
	loadError.value = false
	try {
		const res = await api.adminListUserUsage()
		if (res && res.ok === false) {
			loadError.value = true
			return
		}
		const rows = (res && res.data) || []
		users.value = rows.map((u) => ({
			...u,
			_limitDraft: u.monthly_token_limit || 0,
			_saving: false,
			per_model: (u.per_model || []).map((m) => ({
				...m, _limitDraft: m.monthly_token_limit || 0, _saving: false,
			})),
		}))
	} catch (e) {
		loadError.value = true
	} finally {
		loading.value = false
	}
}

function fmtTokens(n) {
	n = Number(n || 0)
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M"
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k"
	return String(n)
}
function pct(u) {
	if (!u || !u.monthly_token_limit) return 0
	return Math.min(100, Math.round((Number(u.month_tokens || 0) / Number(u.monthly_token_limit)) * 100))
}
function modelPct(m) {
	if (!m || !m.monthly_token_limit) return 0
	return Math.min(100, Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100))
}

async function saveLimit(u) {
	const val = Math.max(0, Math.round(Number(u._limitDraft) || 0))
	u._saving = true
	try {
		const res = await api.adminSetUserLimit(u.user, val)
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not update the limit.")
			return
		}
		const d = (res && res.data) || {}
		u.monthly_token_limit = d.monthly_token_limit != null ? d.monthly_token_limit : val
		u._limitDraft = u.monthly_token_limit
		toast.success("Limit updated")
	} catch (e) {
		toast.error(errMsg(e))
	} finally {
		u._saving = false
	}
}

async function saveModelLimit(u, m) {
	const val = Math.max(0, Math.round(Number(m._limitDraft) || 0))
	m._saving = true
	try {
		const res = await api.adminSetUserModelLimit(u.user, m.model, val)
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not update the model limit.")
			return
		}
		const d = (res && res.data) || {}
		m.monthly_token_limit = d.monthly_token_limit != null ? d.monthly_token_limit : val
		m._limitDraft = m.monthly_token_limit
		toast.success(`Limit updated for ${m.model}`)
	} catch (e) {
		toast.error(errMsg(e))
	} finally {
		m._saving = false
	}
}

// "Sync from agent" — sweeps the openclaw gateway's sessions.list to refresh
// per-session snapshots, then reloads the table.
const syncing = ref(false)
const syncReason = ref("")
const syncResult = ref("")
async function onSync() {
	syncing.value = true
	syncReason.value = ""
	syncResult.value = ""
	try {
		const res = await api.adminSyncUsage()
		if (res && res.ok === false) {
			syncReason.value = res.reason || "Sync failed."
			return
		}
		const d = (res && res.data) || {}
		syncResult.value = `Synced ${d.synced_sessions ?? 0} session${d.synced_sessions === 1 ? "" : "s"} · ${d.users_updated ?? 0} user${d.users_updated === 1 ? "" : "s"} updated`
		await loadUsers()
	} catch (e) {
		syncReason.value = errMsg(e)
	} finally {
		syncing.value = false
	}
}

onMounted(loadUsers)
</script>

<style scoped>
.jv-usr-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px; }
.jv-usr-row { display: grid; grid-template-columns: 1.5fr 1.8fr 1.3fr 0.9fr; gap: 14px; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); }
.jv-usr-row:last-child { border-bottom: 0; }
.jv-usr-headrow { padding-top: 0; padding-bottom: 8px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); }
.jv-usr-id { display: flex; align-items: flex-start; gap: 8px; }
.jv-usr-name { font-size: 13.5px; font-weight: 600; color: var(--text); }
.jv-usr-email { font-size: 11.5px; color: var(--text-3); margin-top: 1px; }
.jv-usr-chev { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; margin-top: 1px; border: 0; background: transparent; color: var(--text-3); cursor: pointer; border-radius: 5px; transition: transform .12s ease; }
.jv-usr-chev:hover { color: var(--text); background: var(--surface-2, rgba(0,0,0,.05)); }
.jv-usr-chev--open { transform: rotate(90deg); }
.jv-usr-chev--placeholder { cursor: default; pointer-events: none; }
.jv-usr-meter .jv-usage-bar { margin-top: 0; }
.jv-usr-totalhint { font-size: 11px; color: var(--text-3); margin-top: 4px; }
.jv-usr-limit { display: flex; align-items: center; gap: 6px; }
.jv-usr-limitinput { width: 74px; flex: none; padding: 6px 8px; font-size: 12.5px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); color: var(--text); font-family: inherit; box-sizing: border-box; }
.jv-usr-limitinput:focus { outline: none; border-color: var(--cta-bd); }
.jv-usr-limitinput::-webkit-outer-spin-button, .jv-usr-limitinput::-webkit-inner-spin-button { margin: 0; }
.jv-usr-last { font-size: 12px; color: var(--text-3); text-align: right; }
.jv-model-block { padding: 4px 0 12px 26px; border-bottom: 1px solid var(--border); }
.jv-model-erow { display: grid; grid-template-columns: 1.2fr 1.8fr 1.3fr; gap: 14px; align-items: center; padding: 7px 0; }
.jv-model-ename { font-size: 12.5px; font-weight: 600; color: var(--text-2); }
.jv-model-emeter .jv-usage-bar { margin-top: 0; }
</style>
```

### 6d. Verify

```bash
cd frontend && node_modules/.bin/vite build; echo "build exit: $?"
```

Confirm exit 0 (per CLAUDE.md, judge by `$?`, not the tail). Then, on a running onboarded
bench (`jarvis.proxy`), open Settings → Usage (per-model bars render under Measured usage;
the tenant budget block reads "Tenant monthly budget (informational)") and, as a Jarvis
Admin, Settings → Team usage (chevron expands per-model rows; editing a per-model cap +
Save toasts success and persists).

### 6e. Commit

```
feat(ui): per-model usage bars + admin per-model cap editor; relabel tenant budget block
```

---

## Self-Review — spec §7 requirement → task mapping

| Spec §7 / prompt requirement | Task | Evidence |
|---|---|---|
| Child doctype `Jarvis User Model Usage` (model, month_key, month_input_tokens, month_output_tokens, monthly_token_limit 0=unlimited, description "Per-model monthly cap. 0 = unlimited.") | 1 | doctype JSON + schema test |
| Parent gains Table field `user_model_usage`, permlevel 1 like other usage fields | 1 | parent JSON edit + `test_parent_has_permlevel1_table_field` |
| `record_turn_usage` reads row `model`, upserts (user, model, month) child row, month-rollover-aware, consistent with aggregate; child rows for old months PRUNED (keep current only), caps carried; never raises into turn | 2, G1, G8 | `_upsert_model_usage` inside existing try/except; rollover + prune + carry-forward tests |
| Missing model field tolerated | 2 | `test_missing_model_field_tolerated_no_child_row` |
| `validate_can_send(user, model=None)` backward-compatible; `_over_model_limit`; nonzero cap + usage ≥ limit → block "usage_limit" | 3 | policy rewrite; `test_blocks_when_pinned_model_over_limit`; `test_model_kwarg_defaults_to_no_gate` |
| Resolve model in api.py, pass string; policy does NOT import turn_handler (clean boundary, documented) | 3, G5 | api.py `_resolve_model_and_provider` at call site; policy has no turn_handler import |
| Pool "Auto" → "" → per-model gate skipped (comment referencing spec §2) | 3, G6 | `test_empty_model_skips_gate` + inline comment |
| Aggregate gate unchanged, checked FIRST; fail-open on error | 3, G2, G4 | 816 call unchanged; `test_aggregate_gate_fires_first`; `test_fail_open_on_db_error` |
| `admin_set_user_model_limit(user, model, monthly_token_limit)` mirrors `admin_set_user_limit`; upsert even without usage | 4 | new method; `test_set_model_limit_creates_row_without_usage`, `test_creates_row_and_sets_cap` |
| Extend `_settings_payload` + `admin_list_user_usage` with per-model (current month only) | 4 | `_per_model_rows`; `test_get_my_settings_includes_per_model`, `test_admin_list_includes_per_model` |
| Daily push job builds month-to-date snapshot, POST via admin_client, best-effort, skip when self-hosted/no creds, cap 500 (log if truncated) | 5 | `usage_push.py`; skip/failure/cap tests |
| New admin_client method `push_usage_rollup(rollup)` following `get_llm_usage` pattern; pinned body `{"rollup": {...}}` | 5 | `push_usage_rollup` via `_post`/`_m`; `test_pushes_when_configured` asserts payload |
| Register in `hooks.py scheduler_events` daily | 5 | hooks daily edit |
| Not-onboarded quiet skip (reuse admin_client credential detection) | 5, G-note | `AdminAuthError` → return (no log); `test_not_onboarded_is_quiet_skip` |
| UsagePane per-model section + relabel legacy budget "Tenant monthly budget (informational)" | 6 | full SFC |
| UsageAdminPane expandable per-user rows + per-model cap editor + Save via `adminSetUserModelLimit`; api.js one-liner | 6 | full SFC + api.js export |
| Tests: `test_usage_per_model.py`, `test_policy_model_limit.py`, push-job test | 2, 3, 5 | three test modules + role-gate registration |
| Merge risk as a Global Constraint; UI last; git fetch + rebase | G7, 0, 6 | Task 0 recon; Task 6 gated on rebase |

### Accepted / documented gaps (call out in review)

- **Auto enforcement gap (spec §2):** per-model caps enforce only when a model is pinned or
  direct; pool "Auto" (`""`) is skipped. Aggregate cap still applies; attribution is accurate.
  Documented inline in `policy.validate_can_send`.
- **First-send-of-month rollover edge (G2/G8):** a per-model cap set in a prior month is not
  re-enforced until the model's first turn this month records (which carries the cap forward);
  the very first send is not per-model-gated. Consistent with the aggregate gate's own
  rollover-as-zero behavior and the fail-open philosophy. Documented in `_over_model_limit`.
- **Stale limited rows for untouched models (G8):** a model with a nonzero cap but no activity
  keeps its stale-month row until it next records (bounded by #models); pure usage-only stale
  rows are pruned immediately. This is the deliberate "never silently drop a configured cap"
  trade-off. Documented in `_upsert_model_usage`.
```
