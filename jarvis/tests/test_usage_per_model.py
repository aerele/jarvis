"""Tests for per-model usage accounting + caps (fleet usage spec §7, §9).

Hermetic like test_user_settings.py: disposable enabled users created in setUp,
and because record_turn_usage / set_model_limit COMMIT, every Jarvis User
Settings + Jarvis Chat Session + Jarvis User Model Usage row owned by a fixture
user is deleted in tearDown (a transaction rollback cannot undo a commit).
"""

from __future__ import annotations

from unittest.mock import patch

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
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "PerModelTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)


def _make_session(session_key: str, user: str) -> None:
	frappe.get_doc(
		{
			"doctype": SESSION,
			"session_key": session_key,
			"user": user,
		}
	).insert(ignore_permissions=True)
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
		for fn in ("model", "month_key", "month_input_tokens", "month_output_tokens", "monthly_token_limit"):
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


class TestPerModelAttribution(_Base):
	def _row(self, **kw):
		base = {
			"totalTokensFresh": True,
			"inputTokens": 0,
			"outputTokens": 0,
			"totalTokens": 0,
			"model": "gpt-4o",
		}
		base.update(kw)
		return base

	def _child(self, model):
		return frappe.db.get_value(
			MODEL_USAGE,
			{
				"parent": USER_A,
				"parenttype": USETT,
				"parentfield": "user_model_usage",
				"model": model,
				"month_key": usage.current_month_key(),
			},
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
		usage.record_turn_usage("agent:pm2", {"totalTokensFresh": True, "inputTokens": 5, "outputTokens": 5})
		self.assertEqual(frappe.utils.cint(frappe.db.get_value(USETT, {"user": USER_A}, "month_tokens")), 10)
		self.assertEqual(frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}), [])

	def test_month_rollover_prunes_stale_usage_and_carries_cap(self):
		_make_session("agent:pm3", USER_A)
		usage.record_turn_usage("agent:pm3", self._row(model="gpt-4o", inputTokens=10, outputTokens=5))
		# Give gpt-4o a cap and mark its row + the parent as a prior month.
		usage.set_model_limit(USER_A, "gpt-4o", 1000)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
			"month_key",
			"2020-01",
			update_modified=False,
		)
		# A zero-cap stale row that must be pruned on the next record.
		usage.record_turn_usage("agent:pm3", self._row(model="oldmodel", inputTokens=1, outputTokens=1))
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "oldmodel", "month_key": usage.current_month_key()},
			"month_key",
			"2020-01",
			update_modified=False,
		)
		frappe.db.commit()
		# New turn on gpt-4o this month: fresh current-month row, cap carried, delta reset.
		usage.record_turn_usage("agent:pm3", self._row(model="gpt-4o", inputTokens=7, outputTokens=3))
		g = self._child("gpt-4o")
		self.assertEqual(g.month_input_tokens, 7)  # reset to new delta
		self.assertEqual(g.month_output_tokens, 3)
		self.assertEqual(g.monthly_token_limit, 1000)  # cap survived rollover
		# Stale zero-cap row pruned; no stale gpt-4o row remains.
		self.assertEqual(frappe.get_all(MODEL_USAGE, filters={"parent": USER_A, "month_key": "2020-01"}), [])

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


class TestPoolAutoAttribution(_Base):
	"""Auto-mode (BIFROST pool, unpinned) turns get their agent SESSION
	patched to turn_handler.POOL_VIRTUAL_MODEL ("jarvis-pool") - see
	turn_handler._session_model_for - so the gateway's sessions.list row
	reports that sentinel as `model` for the turn, not the real per-request
	model Bifrost picked server-side (that data isn't available bench-side;
	those tenants get true per-model data from Bifrost logs admin-side). An
	agent-DIRECT pool RESETS its session instead, so it never lands here.
	record_turn_usage records the turn under that same sentinel - the
	honest bucket for "pool auto-routed" - and usage.py must reference the
	shared constant rather than an implicit magic string, so the two
	modules can't drift apart on what the sentinel is."""

	def test_auto_turn_records_under_shared_pool_sentinel_constant(self):
		from jarvis.chat.turn_handler import POOL_VIRTUAL_MODEL

		self.assertEqual(usage.POOL_VIRTUAL_MODEL, POOL_VIRTUAL_MODEL)

		_make_session("agent:pmauto", USER_A)
		usage.record_turn_usage(
			"agent:pmauto",
			{
				"totalTokensFresh": True,
				"inputTokens": 10,
				"outputTokens": 5,
				"totalTokens": 15,
				"model": POOL_VIRTUAL_MODEL,
			},
		)
		row = frappe.db.get_value(
			MODEL_USAGE,
			{
				"parent": USER_A,
				"parenttype": USETT,
				"parentfield": "user_model_usage",
				"model": POOL_VIRTUAL_MODEL,
				"month_key": usage.current_month_key(),
			},
			["month_input_tokens", "month_output_tokens"],
			as_dict=True,
		)
		self.assertIsNotNone(row)
		self.assertEqual(row.month_input_tokens, 10)
		self.assertEqual(row.month_output_tokens, 5)


class TestPerModelWriteFailureIsolation(_Base):
	"""_upsert_model_usage runs between the aggregate UPDATEs and
	record_turn_usage's own commit. If it raises, the CURRENT (buggy) code
	falls straight into the function's outer except and returns WITHOUT
	committing - the aggregate deltas that already executed only exist in
	the uncommitted transaction, at the mercy of whatever runs next. The fix
	must isolate the per-model call so record_turn_usage's own
	frappe.db.commit() is still reached (and the outer except's log_error,
	titled "record_turn_usage failed", must NOT fire - that title firing
	would mean the commit was skipped again)."""

	def test_aggregate_still_committed_when_per_model_write_raises(self):
		_make_session("agent:pmfail", USER_A)
		real_commit = frappe.db.commit
		commit_calls = []

		def counting_commit(*args, **kwargs):
			commit_calls.append(True)
			return real_commit(*args, **kwargs)

		with (
			patch.object(usage, "_upsert_model_usage", side_effect=RuntimeError("boom")),
			patch.object(frappe.db, "commit", counting_commit),
			patch("frappe.log_error") as mock_log_error,
		):
			usage.record_turn_usage(
				"agent:pmfail",
				{
					"totalTokensFresh": True,
					"inputTokens": 10,
					"outputTokens": 5,
					"totalTokens": 15,
					"model": "gpt-4o",
				},
			)  # must not raise

		# The function's own commit was reached despite the per-model failure.
		self.assertEqual(len(commit_calls), 1)
		# The per-model failure was logged, but NOT under the outer handler's
		# title - that title only fires when the whole function bailed early
		# (pre-fix behavior, which skips the commit).
		self.assertTrue(mock_log_error.called)
		titles = [
			c.kwargs.get("title") or (c.args[0] if c.args else None) for c in mock_log_error.call_args_list
		]
		self.assertNotIn("jarvis usage: record_turn_usage failed", titles)

		s = frappe.db.get_value(
			USETT,
			{"user": USER_A},
			["month_input_tokens", "month_output_tokens", "month_tokens", "total_tokens"],
			as_dict=True,
		)
		self.assertEqual(s.month_input_tokens, 10)
		self.assertEqual(s.month_output_tokens, 5)
		self.assertEqual(s.month_tokens, 15)
		self.assertEqual(s.total_tokens, 15)
		sess = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:pmfail"},
			["input_tokens", "output_tokens", "run_count"],
			as_dict=True,
		)
		self.assertEqual(sess.input_tokens, 10)
		self.assertEqual(sess.output_tokens, 5)
		self.assertEqual(sess.run_count, 1)
		# No per-model row: the write raised and was swallowed before insert/merge.
		self.assertEqual(frappe.get_all(MODEL_USAGE, filters={"parent": USER_A}), [])


class TestConcurrentFirstInsertRace(_Base):
	"""Two turns in DIFFERENT conversations can race on a model's FIRST use in
	a month (turn_handler's single-flight guard in chat/api.py is only
	per-conversation), both missing the SELECT-then-INSERT existence check and
	both attempting to insert. A single-process test can't reproduce the raw
	thread interleaving, but it CAN call the atomic insert-or-merge primitive
	twice in a row exactly as two racing callers would each call it once,
	after both observed no existing row — this is the same code path either
	way, and is what the unique index (parent, parentfield, model, month_key)
	added by jarvis.patches.v2_02_unique_model_usage_row is for."""

	def test_atomic_insert_does_not_duplicate_row_on_racing_first_insert(self):
		usage.get_or_create_user_settings(USER_A)
		now = frappe.utils.now_datetime()
		month = usage.current_month_key()
		first = usage._atomic_insert_or_merge_model_usage(USER_A, "gpt-4o", month, 10, 5, 0, now)
		second = usage._atomic_insert_or_merge_model_usage(USER_A, "gpt-4o", month, 8, 12, 0, now)
		self.assertTrue(first)
		self.assertFalse(second)
		rows = frappe.get_all(
			MODEL_USAGE,
			filters={"parent": USER_A, "model": "gpt-4o", "month_key": month},
			fields=["month_input_tokens", "month_output_tokens"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].month_input_tokens, 18)
		self.assertEqual(rows[0].month_output_tokens, 17)
