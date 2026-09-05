"""Tests for per-turn usage capture (usage-dashboard Part A, task U1).

Hermetic like test_usage_per_model.py: disposable fixture users/sessions
created in setUp, and because record_turn_usage COMMITS, every Jarvis User
Settings + Jarvis Chat Session + Jarvis Turn Usage row (plus, for the
tool_calls tests, the Jarvis Conversation / Jarvis Chat Turn / Jarvis Chat
Message rows) owned or referencing a fixture user is deleted in tearDown - a
transaction rollback cannot undo a commit.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage, usage_push

USETT = "Jarvis User Settings"
SESSION = "Jarvis Chat Session"
TURN_USAGE = "Jarvis Turn Usage"
CONV = "Jarvis Conversation"
CHAT_TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"

USER_A = "jarvis-turnusage-a@example.test"
USER_B = "jarvis-turnusage-b@example.test"
_ALL_USERS = (USER_A, USER_B)


def _ensure_user(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "TurnUsageTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)


def _make_session(session_key: str, user: str, *, profile_agent_id: str = "", profile_tier: str = "") -> None:
	frappe.get_doc(
		{
			"doctype": SESSION,
			"session_key": session_key,
			"user": user,
			"profile_agent_id": profile_agent_id,
			"profile_tier": profile_tier,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _cleanup() -> None:
	for name in frappe.get_all(TURN_USAGE, filters={"user": ["in", list(_ALL_USERS)]}, pluck="name"):
		frappe.delete_doc(TURN_USAGE, name, ignore_permissions=True, force=True)
	# Blank-user Turn Usage fixtures (unmapped-session rollup test) aren't
	# caught by the user filter above - clean by the shared session_key prefix
	# every fixture in this file uses instead.
	for name in frappe.get_all(TURN_USAGE, filters={"session_key": ["like", "agent:tu-%"]}, pluck="name"):
		frappe.delete_doc(TURN_USAGE, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(
		CHAT_TURN, filters={"relay_target_id": ["like", "test-turnusage-%"]}, pluck="name"
	):
		frappe.delete_doc(CHAT_TURN, name, ignore_permissions=True, force=True)
	for name in frappe.get_all(
		CONV, filters={"title": ["like", "turnusage-fixture%"]}, pluck="name"
	) + frappe.get_all(CONV, filters={"title": ["like", "rollup-fixture%"]}, pluck="name"):
		for msg in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
			frappe.delete_doc(MSG, msg, ignore_permissions=True, force=True)
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	for email in _ALL_USERS:
		for name in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(SESSION, filters={"user": email}, pluck="name"):
			frappe.delete_doc(SESSION, name, ignore_permissions=True, force=True)


class TestTurnUsage(FrappeTestCase):
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

	def _row(self, **kw):
		base = {
			"totalTokensFresh": True,
			"inputTokens": 0,
			"outputTokens": 0,
			"totalTokens": 0,
			"contextTokens": 0,
			"model": "gpt-5.5",
			"modelProvider": "openai",
		}
		base.update(kw)
		return base

	# -- (a) RECORDED path writes one row with profile fields + in/out ------- #
	def test_recorded_row_carries_profile_and_tokens(self):
		_make_session("agent:tu-rec", USER_A, profile_agent_id="role-hr", profile_tier="lite")
		outcome = usage.record_turn_usage(
			"agent:tu-rec", self._row(inputTokens=10, outputTokens=5, totalTokens=100)
		)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		rows = frappe.get_all(
			TURN_USAGE,
			filters={"session_key": "agent:tu-rec"},
			fields=[
				"user",
				"profile_agent_id",
				"profile_tier",
				"model",
				"tokens_in",
				"tokens_out",
				"cache_read",
				"cache_write",
				"cache_reported",
			],
		)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.user, USER_A)
		self.assertEqual(row.profile_agent_id, "role-hr")
		self.assertEqual(row.profile_tier, "lite")
		self.assertEqual(row.model, "gpt-5.5")
		self.assertEqual(row.tokens_in, 10)
		self.assertEqual(row.tokens_out, 5)
		# Live-checked 2026-08-17: the gateway build in use reports no cache
		# token fields on sessions.list rows, so these are always the honest
		# "not reported" state, never a fabricated zero.
		self.assertEqual(row.cache_read, 0)
		self.assertEqual(row.cache_write, 0)
		self.assertEqual(row.cache_reported, 0)

	# -- (a2) C1: contextTokens -> context_capacity / context_pct on the session #
	def test_recorded_row_stores_context_capacity_and_pct(self):
		# Live fact (2026-09-04): contextTokens is the model's context-window
		# CAPACITY, distinct from totalTokens (context tokens actually USED).
		_make_session("agent:tu-ctx", USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-ctx",
			self._row(inputTokens=10, outputTokens=5, totalTokens=116399, contextTokens=200000),
		)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-ctx"},
			["last_total_tokens", "context_capacity", "context_pct"],
			as_dict=True,
		)
		self.assertEqual(row.last_total_tokens, 116399)
		self.assertEqual(row.context_capacity, 200000)
		self.assertEqual(row.context_pct, 58.2)

	def test_recorded_row_no_capacity_reported_leaves_pct_zero(self):
		# A row that never reports contextTokens (missing/zero) must not
		# fabricate a percentage.
		_make_session("agent:tu-ctx-none", USER_A)
		usage.record_turn_usage(
			"agent:tu-ctx-none", self._row(inputTokens=1, outputTokens=1, totalTokens=500)
		)
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-ctx-none"},
			["context_capacity", "context_pct"],
			as_dict=True,
		)
		self.assertEqual(row.context_capacity, 0)
		self.assertEqual(row.context_pct, 0)

	def test_refresh_session_snapshots_stores_context_capacity_and_pct(self):
		_make_session("agent:tu-ctx-sync", USER_A)
		usage.refresh_session_snapshots(
			[{"key": "agent:tu-ctx-sync", "totalTokens": 50000, "contextTokens": 200000}]
		)
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-ctx-sync"},
			["last_total_tokens", "context_capacity", "context_pct"],
			as_dict=True,
		)
		self.assertEqual(row.last_total_tokens, 50000)
		self.assertEqual(row.context_capacity, 200000)
		self.assertEqual(row.context_pct, 25.0)

	# -- review fix 1: a sweep row with no capacity must not clobber a ------ #
	# -- previously known one with 0 ----------------------------------------- #
	def test_refresh_session_snapshots_keeps_stored_capacity_when_row_has_none(self):
		_make_session("agent:tu-ctx-keep", USER_A)
		usage.refresh_session_snapshots(
			[{"key": "agent:tu-ctx-keep", "totalTokens": 50000, "contextTokens": 200000}]
		)
		# A later sweep row for the same session that reports no contextTokens
		# (e.g. the gateway omitted it this cycle) - last_total_tokens still
		# moves (it is the row's own honest snapshot), but capacity/pct must
		# NOT be zeroed out just because this particular row didn't carry one.
		usage.refresh_session_snapshots([{"key": "agent:tu-ctx-keep", "totalTokens": 60000}])
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-ctx-keep"},
			["last_total_tokens", "context_capacity", "context_pct"],
			as_dict=True,
		)
		self.assertEqual(row.last_total_tokens, 60000, "the fresh snapshot always moves")
		self.assertEqual(row.context_capacity, 200000, "stored capacity must survive a capacity-less row")
		self.assertEqual(row.context_pct, 25.0, "stored pct must survive a capacity-less row")

	# -- review fix 3: last_usage_at/modified reliably order "newest" even -- #
	# -- when the gateway row carries no updatedAt --------------------------- #
	def test_refresh_session_snapshots_stamps_last_usage_at_without_updated_at(self):
		# last_usage_at means "last REAL usage" (test_user_settings.
		# TestAdminSync.test_refreshes_snapshots_without_accumulating pins
		# this: a session with no updatedAt keeps last_usage_at exactly as it
		# was - a sweep must never fabricate a usage time from sync time).
		# `modified`, unrelated to that meaning, must still advance on every
		# sweep so "just synced" stays orderable.
		_make_session("agent:tu-ctx-stamp", USER_A)
		before = frappe.db.get_value(
			SESSION, {"session_key": "agent:tu-ctx-stamp"}, ["last_usage_at", "modified"], as_dict=True
		)
		self.assertIsNone(before.last_usage_at, "a fresh session has never had a usage stamp")
		# No "updatedAt" key at all - last_usage_at must stay None, not get
		# fabricated from sync time.
		usage.refresh_session_snapshots([{"key": "agent:tu-ctx-stamp", "totalTokens": 1000}])
		after = frappe.db.get_value(
			SESSION, {"session_key": "agent:tu-ctx-stamp"}, ["last_usage_at", "modified"], as_dict=True
		)
		self.assertIsNone(after.last_usage_at, "no updatedAt must not stamp last_usage_at")
		self.assertNotEqual(after.modified, before.modified, "modified must still advance on every sweep")
		# Once a real usage stamp exists (a row that DOES carry updatedAt), a
		# later no-updatedAt sweep must PRESERVE it, not clobber it - COALESCE
		# keeps the real stored value over a missing one, and `modified` keeps
		# advancing regardless.
		usage.refresh_session_snapshots(
			[{"key": "agent:tu-ctx-stamp", "totalTokens": 1500, "updatedAt": 1700000000000}]
		)
		stamped = frappe.db.get_value(
			SESSION, {"session_key": "agent:tu-ctx-stamp"}, ["last_usage_at", "modified"], as_dict=True
		)
		self.assertIsNotNone(stamped.last_usage_at)
		usage.refresh_session_snapshots([{"key": "agent:tu-ctx-stamp", "totalTokens": 2000}])
		again = frappe.db.get_value(
			SESSION, {"session_key": "agent:tu-ctx-stamp"}, ["last_usage_at", "modified"], as_dict=True
		)
		self.assertEqual(again.last_usage_at, stamped.last_usage_at, "last_usage_at unchanged, not clobbered")
		self.assertNotEqual(again.modified, stamped.modified, "modified must still advance on every sweep")

	# -- (b) VALID_ZERO path still writes a row (attribution, zero tokens) -- #
	def test_valid_zero_row_writes_attribution(self):
		_make_session("agent:tu-zero", USER_A, profile_agent_id="", profile_tier="full")
		outcome = usage.record_turn_usage("agent:tu-zero", self._row(inputTokens=0, outputTokens=0))
		self.assertEqual(outcome, usage.USAGE_VALID_ZERO)
		rows = frappe.get_all(
			TURN_USAGE,
			filters={"session_key": "agent:tu-zero"},
			fields=["user", "tokens_in", "tokens_out"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].user, USER_A)
		self.assertEqual(rows[0].tokens_in, 0)
		self.assertEqual(rows[0].tokens_out, 0)

	# -- review fix 4: VALID_ZERO must still refresh the context snapshot --- #
	def test_valid_zero_row_refreshes_context_snapshot(self):
		_make_session("agent:tu-zero-ctx", USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-zero-ctx",
			self._row(inputTokens=0, outputTokens=0, totalTokens=42000, contextTokens=200000),
		)
		self.assertEqual(outcome, usage.USAGE_VALID_ZERO)
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-zero-ctx"},
			["last_total_tokens", "context_capacity", "context_pct", "last_usage_at"],
			as_dict=True,
		)
		self.assertEqual(row.last_total_tokens, 42000)
		self.assertEqual(row.context_capacity, 200000)
		self.assertEqual(row.context_pct, 21.0)
		self.assertIsNotNone(row.last_usage_at)

	def test_valid_zero_row_no_capacity_does_not_clobber_stored_one(self):
		# Same no-clobber guard as refresh_session_snapshots (fix 1), on the
		# record_turn_usage VALID_ZERO path.
		_make_session("agent:tu-zero-keep", USER_A)
		usage.record_turn_usage(
			"agent:tu-zero-keep",
			self._row(inputTokens=1, outputTokens=1, totalTokens=50000, contextTokens=200000),
		)
		usage.record_turn_usage(
			"agent:tu-zero-keep", self._row(inputTokens=0, outputTokens=0, totalTokens=51000)
		)
		row = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:tu-zero-keep"},
			["last_total_tokens", "context_capacity", "context_pct"],
			as_dict=True,
		)
		self.assertEqual(row.last_total_tokens, 51000)
		self.assertEqual(row.context_capacity, 200000)
		self.assertEqual(row.context_pct, 25.0)

	# -- (c) RETRY path writes nothing ---------------------------------------- #
	def test_retry_path_writes_nothing(self):
		_make_session("agent:tu-retry", USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-retry", self._row(totalTokensFresh=False, inputTokens=10, outputTokens=5)
		)
		self.assertEqual(outcome, usage.USAGE_RETRY)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-retry"}))

	def test_retry_no_session_mapping_writes_nothing(self):
		outcome = usage.record_turn_usage("agent:tu-unmapped", self._row(inputTokens=5, outputTokens=5))
		self.assertEqual(outcome, usage.USAGE_RETRY)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-unmapped"}))

	# -- (d) a raising turn-usage write must not break the accrual ---------- #
	def test_row_write_failure_does_not_change_outcome(self):
		_make_session("agent:tu-fail", USER_A)
		title = "jarvis usage: turn usage row write failed"
		before = frappe.db.count("Error Log", {"method": title})
		with patch("jarvis.chat.usage._insert_turn_usage_row", side_effect=RuntimeError("boom")):
			outcome = usage.record_turn_usage(
				"agent:tu-fail", self._row(inputTokens=7, outputTokens=3, totalTokens=50)
			)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		# The aggregate accrual still landed even though the per-turn row write failed.
		s = frappe.db.get_value(USETT, {"user": USER_A}, "month_tokens")
		self.assertEqual(s, 10)
		after = frappe.db.count("Error Log", {"method": title})
		self.assertGreater(after, before)
		self.assertFalse(frappe.db.exists(TURN_USAGE, {"session_key": "agent:tu-fail"}))

	# -- (e) tool_calls counts tool-role messages seq-bounded to the turn --- #
	def _make_turn_with_tools(self, user: str) -> str:
		"""Conversation: [tool(before), user(seed), tool, tool, assistant, tool(after)].
		Returns the Jarvis Chat Turn's run_id. Only the two tool rows strictly
		between seed_message and assistant_message (by seq) must count."""
		conv = frappe.get_doc(
			{"doctype": CONV, "title": "turnusage-fixture", "status": "Active", "owner": user}
		)
		conv.insert(ignore_permissions=True)

		def _msg(seq: int, role: str, **kw) -> str:
			doc = frappe.get_doc(
				{"doctype": MSG, "conversation": conv.name, "seq": seq, "role": role, "content": "", **kw}
			)
			doc.insert(ignore_permissions=True)
			return doc.name

		_msg(1, "tool", tool_name="before_turn")
		seed_name = _msg(2, "user", content="do the thing")
		_msg(3, "tool", tool_name="in_turn_1")
		_msg(4, "tool", tool_name="in_turn_2")
		asst_name = _msg(5, "assistant", content="done")
		_msg(6, "tool", tool_name="after_turn")

		run_id = f"test-turnusage-{frappe.generate_hash(length=10)}"
		frappe.get_doc(
			{
				"doctype": CHAT_TURN,
				"run_id": run_id,
				"conversation": conv.name,
				"relay_target_id": run_id,
				"seed_message": seed_name,
				"assistant_message": asst_name,
				"state": "done",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return run_id

	def test_tool_calls_counts_only_rows_within_the_turn(self):
		_make_session("agent:tu-tools", USER_A)
		run_id = self._make_turn_with_tools(USER_A)
		outcome = usage.record_turn_usage(
			"agent:tu-tools",
			self._row(inputTokens=4, outputTokens=2, totalTokens=60),
			run_id=run_id,
		)
		self.assertEqual(outcome, usage.USAGE_RECORDED)
		tool_calls = frappe.db.get_value(TURN_USAGE, {"session_key": "agent:tu-tools"}, "tool_calls")
		self.assertEqual(tool_calls, 2)

	def test_tool_calls_zero_when_no_run_id(self):
		_make_session("agent:tu-norun", USER_A)
		usage.record_turn_usage("agent:tu-norun", self._row(inputTokens=1, outputTokens=1, totalTokens=10))
		tool_calls = frappe.db.get_value(TURN_USAGE, {"session_key": "agent:tu-norun"}, "tool_calls")
		self.assertEqual(tool_calls, 0)

	# -- budget/compaction snapshot fields (context-meter task 2) ------------ #
	def test_budget_fields_from_row(self):
		row = {
			"contextBudgetStatus": {"route": "compact_only", "reserveTokens": 20000},
			"compactionCheckpointCount": 2,
		}
		self.assertEqual(usage.budget_fields_from_row(row), ("compact_only", 20000, 2))
		self.assertEqual(usage.budget_fields_from_row({}), ("", 0, 0))
		self.assertEqual(usage.budget_fields_from_row({"contextBudgetStatus": "junk"}), ("", 0, 0))

	def test_record_turn_usage_writes_budget_fields(self):
		# USER_A (not Administrator) so the RECORDED path's commit - the
		# Chat Session row, its Turn Usage row, and the Jarvis User Settings
		# row get_or_create_user_settings creates - is fully hermetic: the
		# shared _cleanup() helper (setUp/tearDown) already removes all three
		# for USER_A, including the settings row a raw Administrator fixture
		# would otherwise leave behind permanently on a fresh (CI) DB.
		key = "agent:tu-budget"
		_make_session(key, USER_A)
		row = {
			"key": key,
			"inputTokens": 100,
			"outputTokens": 10,
			"totalTokens": 5000,
			"totalTokensFresh": True,
			"contextTokens": 200000,
			"contextBudgetStatus": {"route": "fits", "reserveTokens": 16384},
			"compactionCheckpointCount": 1,
			"model": "m",
			"modelProvider": "p",
		}
		usage.record_turn_usage(key, row)
		got = frappe.db.get_value(
			SESSION,
			{"session_key": key},
			["budget_route", "reserve_tokens", "compaction_count"],
			as_dict=True,
		)
		self.assertEqual((got.budget_route, got.reserve_tokens, got.compaction_count), ("fits", 16384, 1))


def _seed_turn_row(
	session_key: str,
	user: str,
	*,
	profile_agent_id: str = "",
	tokens_in: int = 0,
	tokens_out: int = 0,
	cache_read: int = 0,
	cache_write: int = 0,
	cache_reported: int = 0,
	tool_calls: int = 0,
	creation=None,
	day=None,
	model: str = "gpt-5.5",
) -> str:
	"""Insert a bare Jarvis Turn Usage row (task U2 rollup tests): the rollup
	builder consumes these rows directly, so seeding them straight (rather
	than round-tripping through record_turn_usage's gateway-row shape) keeps
	the fixtures focused on the aggregation being tested. ``day`` defaults to
	today (the real current month); pass an explicit day to land a row in a
	synthetic month a test has pinned via current_month_key, keeping it
	isolated from this bench's real Turn Usage traffic in the real current
	month (see test_empty_month_yields_old_shape_plus_empty_new_fields).
	``model`` (C1: per-model users_daily rollup) defaults to the same
	"gpt-5.5" every other fixture in this module used before C1, so no
	existing call site needs to change."""
	doc = frappe.get_doc(
		{
			"doctype": TURN_USAGE,
			"session_key": session_key,
			"user": user,
			"profile_agent_id": profile_agent_id,
			"profile_tier": "full",
			"model": model,
			"tokens_in": tokens_in,
			"tokens_out": tokens_out,
			"cache_read": cache_read,
			"cache_write": cache_write,
			"cache_reported": cache_reported,
			"tool_calls": tool_calls,
			"day": day or frappe.utils.today(),
			"run_id": "",
		}
	)
	doc.insert(ignore_permissions=True)
	if creation is not None:
		# Controls the tie-break ("most recent turn wins") deterministically -
		# ORM inserts a few lines apart can otherwise land in the same second.
		frappe.db.set_value(TURN_USAGE, doc.name, "creation", creation, update_modified=False)
	frappe.db.commit()
	return doc.name


def _seed_tool_messages(owner: str, title: str, tool_names: list[str]) -> None:
	"""One Jarvis Conversation (owned by ``owner``) with one role=tool message
	per entry in ``tool_names`` (repeat a name to raise its count). Frappe
	stamps ``owner`` from the session user at insert (Administrator, here) -
	same trap ``get_or_create_user_settings`` works around - so it is forced
	back to ``owner`` afterwards; the rollup's top_tools attribution reads
	exactly this field."""
	conv = frappe.get_doc({"doctype": CONV, "title": title, "status": "Active"})
	conv.insert(ignore_permissions=True)
	frappe.db.set_value(CONV, conv.name, "owner", owner, update_modified=False)
	for i, tool_name in enumerate(tool_names, start=1):
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv.name,
				"seq": i,
				"role": "tool",
				"content": "",
				"tool_name": tool_name,
				"tool_status": "completed",
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


class TestUsageRollup(FrappeTestCase):
	"""Task U2: the MTD rollup builder's profile/cache/tool-call extension.
	Hermetic like TestTurnUsage - fixtures are disposable and committed rows
	(Turn Usage inserts, conversation/message inserts) are removed in
	tearDown via the shared ``_cleanup``."""

	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _seed_settings(self, user: str, tokens: int) -> None:
		"""A Jarvis User Settings row for the CURRENT month - the rollup's
		users[] list is settings-sourced (task U1's builder), so a user with
		no settings row this month never appears there even if Turn Usage
		rows exist for them."""
		doc = usage.get_or_create_user_settings(user)
		frappe.db.set_value(
			USETT,
			doc.name,
			{
				"usage_month": usage.current_month_key(),
				"month_input_tokens": tokens,
				"month_output_tokens": tokens,
				"month_tokens": tokens * 2,
			},
			update_modified=False,
		)
		frappe.db.commit()

	# -- (a) empty month: old shape + empty/zero new fields ------------------ #
	def test_empty_month_yields_old_shape_plus_empty_new_fields(self):
		# This bench is live-used (real Turn Usage rows land from actual dev
		# chat traffic), so the CURRENT month is never genuinely empty here -
		# pin the builder to a month key no fixture or real traffic can have
		# written to, which is what "empty month" actually needs to exercise.
		# users_daily (C1) is windowed on the last 35 REAL days, not on
		# current_month_key, so it can't be pinned away the same way - it is
		# patched here for the same live-traffic-pollution reason; its own
		# grouping/window/cap behaviour gets dedicated hermetic tests below.
		with (
			patch("jarvis.chat.usage.current_month_key", return_value="2000-01"),
			patch.object(usage_push, "_users_daily_rollup", return_value=[]),
		):
			rollup, truncated = usage_push._build_rollup()
		self.assertFalse(truncated)
		self.assertEqual(rollup["month_key"], "2000-01")
		self.assertEqual(rollup["schema_version"], 2)
		self.assertEqual(rollup["users"], [])
		self.assertEqual(rollup["by_profile"], [])
		self.assertEqual(rollup["top_tools"], [])
		self.assertEqual(rollup["users_daily"], [])

	# -- (b) per-user sums + profile attribution + cache_reported ----------- #
	def test_per_user_sums_and_profile_attribution(self):
		self._seed_settings(USER_A, 100)
		_seed_turn_row(
			"agent:tu-roll-a1",
			USER_A,
			profile_agent_id="role-hr",
			tokens_in=10,
			tokens_out=5,
			cache_read=2,
			cache_write=1,
			cache_reported=1,
			# Turn Usage's OWN tool_calls (per-turn, seq-bounded) is a
			# different field from the rollup's per-user tool_calls - see
			# test_tool_calls_reflects_tool_messages_not_turn_usage below.
			tool_calls=3,
		)
		_seed_turn_row(
			"agent:tu-roll-a2",
			USER_A,
			profile_agent_id="role-hr",
			tokens_in=4,
			tokens_out=1,
			cache_read=0,
			cache_write=0,
			cache_reported=0,
			tool_calls=1,
		)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertIn(USER_A, users)
		u = users[USER_A]
		self.assertEqual(u["profile"], "role-hr")
		self.assertEqual(u["turns"], 2)
		self.assertEqual(u["cache_read"], 2)
		self.assertEqual(u["cache_write"], 1)
		# bool OR over the month's rows - one row reported, one did not.
		self.assertTrue(u["cache_reported"])
		self.assertIs(u["cache_reported"], True)
		# No Chat Message role=tool rows were seeded, so the rollup's
		# tool_calls (sourced from Chat Message, not Turn Usage - review
		# finding #2) is 0 despite Turn Usage rows carrying tool_calls=3/1.
		self.assertEqual(u["tool_calls"], 0)

	# -- (b2) tool_calls tracks Chat Message, not Turn Usage (finding #2) --- #
	def test_tool_calls_reflects_tool_messages_not_turn_usage(self):
		self._seed_settings(USER_A, 10)
		# A completed turn whose OWN Turn Usage row says tool_calls=5 - this
		# must NOT leak into the rollup's per-user tool_calls.
		_seed_turn_row("agent:tu-roll-tc1", USER_A, profile_agent_id="role-hr", tool_calls=5)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertEqual(users[USER_A]["tool_calls"], 0)
		# Now add role=tool messages (the population top_tools/tool_calls
		# actually describe) - tool_calls must track THIS count instead.
		_seed_tool_messages(USER_A, "rollup-fixture-toolcalls", ["get_list", "get_list", "read_doc"])
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertEqual(users[USER_A]["tool_calls"], 3)

	# -- (c) blank profile_agent_id maps to "full" --------------------------- #
	def test_blank_profile_maps_to_full(self):
		self._seed_settings(USER_A, 10)
		_seed_turn_row("agent:tu-roll-full", USER_A, profile_agent_id="")
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertEqual(users[USER_A]["profile"], "full")

	# -- (d) profile tie -> most recent turn wins ---------------------------- #
	def test_profile_tie_breaks_to_most_recent_turn(self):
		self._seed_settings(USER_A, 10)
		older = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10)
		newer = frappe.utils.now_datetime()
		# role-p1: 2 turns, both older. role-p2: 2 turns, one newer - equal
		# counts, role-p2's most recent turn is later, so role-p2 wins.
		_seed_turn_row("agent:tu-roll-tie1", USER_A, profile_agent_id="role-p1", creation=older)
		_seed_turn_row(
			"agent:tu-roll-tie2",
			USER_A,
			profile_agent_id="role-p1",
			creation=frappe.utils.add_to_date(older, minutes=1),
		)
		_seed_turn_row("agent:tu-roll-tie3", USER_A, profile_agent_id="role-p2", creation=older)
		_seed_turn_row("agent:tu-roll-tie4", USER_A, profile_agent_id="role-p2", creation=newer)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertEqual(users[USER_A]["profile"], "role-p2")

	# -- (e) blank-user Turn Usage rows are filtered out everywhere ---------- #
	def test_blank_user_rows_are_filtered(self):
		self._seed_settings(USER_A, 10)
		_seed_turn_row("agent:tu-roll-a", USER_A, profile_agent_id="role-hr", tokens_in=1, tokens_out=1)
		_seed_turn_row("agent:tu-roll-blank", "", profile_agent_id="role-hr", tokens_in=999, tokens_out=999)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		self.assertEqual(users[USER_A]["turns"], 1)
		by_profile = {b["profile"]: b for b in rollup["by_profile"]}
		self.assertEqual(by_profile["role-hr"]["turns"], 1)
		self.assertEqual(by_profile["role-hr"]["tokens_in"], 1)

	# -- (f) by_profile: users/turns/token sums + n_skills/n_tools ----------- #
	def test_by_profile_block(self):
		self._seed_settings(USER_A, 10)
		self._seed_settings(USER_B, 10)
		_make_session("agent:tu-roll-p1", USER_A, profile_agent_id="role-p1", profile_tier="full")
		frappe.db.set_value(
			SESSION, {"session_key": "agent:tu-roll-p1"}, {"profile_n_skills": 4, "profile_n_tools": 7}
		)
		_make_session("agent:tu-roll-p2", USER_B, profile_agent_id="role-p2", profile_tier="full")
		frappe.db.set_value(
			SESSION, {"session_key": "agent:tu-roll-p2"}, {"profile_n_skills": 2, "profile_n_tools": 3}
		)
		_seed_turn_row(
			"agent:tu-roll-p1", USER_A, profile_agent_id="role-p1", tokens_in=10, tokens_out=5, cache_read=1
		)
		_seed_turn_row(
			"agent:tu-roll-p2", USER_B, profile_agent_id="role-p2", tokens_in=20, tokens_out=8, cache_read=3
		)
		rollup, _ = usage_push._build_rollup()
		by_profile = {b["profile"]: b for b in rollup["by_profile"]}
		self.assertEqual(by_profile["role-p1"]["users"], 1)
		self.assertEqual(by_profile["role-p1"]["turns"], 1)
		self.assertEqual(by_profile["role-p1"]["tokens_in"], 10)
		self.assertEqual(by_profile["role-p1"]["tokens_out"], 5)
		self.assertEqual(by_profile["role-p1"]["cache_read"], 1)
		self.assertEqual(by_profile["role-p1"]["n_skills"], 4)
		self.assertEqual(by_profile["role-p1"]["n_tools"], 7)
		self.assertEqual(by_profile["role-p2"]["n_skills"], 2)
		self.assertEqual(by_profile["role-p2"]["n_tools"], 3)

	# -- (f2) by_profile users dedups a user split across raw values that --- #
	# -- both normalize to the same bucket (finding #1) --------------------- #
	def test_by_profile_users_deduped_after_normalization(self):
		# Pinned to a synthetic month (see test_empty_month_...): this bench's
		# real Turn Usage traffic in the ACTUAL current month already has
		# other users landing in the "full" bucket, which would make a users
		# count assertion against the real month flaky/wrong.
		with patch("jarvis.chat.usage.current_month_key", return_value="2000-02"):
			self._seed_settings(USER_A, 10)
			# "" and an invalid raw profile_agent_id (fails the role-*
			# validator) both normalize to "full" - the SAME user must only
			# be counted once.
			_seed_turn_row("agent:tu-roll-norm1", USER_A, profile_agent_id="", day="2000-02-10")
			_seed_turn_row(
				"agent:tu-roll-norm2", USER_A, profile_agent_id="not-a-valid-profile", day="2000-02-11"
			)
			rollup, _ = usage_push._build_rollup()
		by_profile = {b["profile"]: b for b in rollup["by_profile"]}
		self.assertEqual(by_profile["full"]["users"], 1)
		self.assertEqual(by_profile["full"]["turns"], 2)

	# -- (g) top_tools: per-user ordering by count, cap 10 -------------------- #
	def test_top_tools_per_user_ordering_and_cap(self):
		self._seed_settings(USER_A, 10)
		names = []
		for i in range(1, 12):  # 11 distinct tools, tied count (1 each) -> alpha order, cap 10
			name = f"cap_tool_{i:02d}"
			names.append(name)
		_seed_tool_messages(USER_A, "rollup-fixture-cap", names)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		top = users[USER_A]["top_tools"]
		self.assertEqual(len(top), 10)
		self.assertEqual([t["tool"] for t in top], sorted(names)[:10])
		self.assertTrue(all(t["count"] == 1 for t in top))
		# tool_calls is the UNCAPPED total (11), not len(top_tools) (10) -
		# the cap only bounds the list, not the count (findings #2 / #5).
		self.assertEqual(users[USER_A]["tool_calls"], 11)

	def test_top_tools_ordering_by_count(self):
		self._seed_settings(USER_A, 10)
		_seed_tool_messages(
			USER_A,
			"rollup-fixture-order",
			["get_list"] * 3 + ["read_doc"] * 2 + ["submit_doc"] * 1,
		)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		top = users[USER_A]["top_tools"]
		self.assertEqual(
			top,
			[
				{"tool": "get_list", "count": 3},
				{"tool": "read_doc", "count": 2},
				{"tool": "submit_doc", "count": 1},
			],
		)

	def test_top_tools_drops_invalid_names(self):
		self._seed_settings(USER_A, 10)
		_seed_tool_messages(
			USER_A,
			"rollup-fixture-invalid",
			["get_list", "bad tool!", "jarvis__" + ("x" * 80)],
		)
		rollup, _ = usage_push._build_rollup()
		users = {u["email"]: u for u in rollup["users"]}
		tools = {t["tool"] for t in users[USER_A]["top_tools"]}
		self.assertIn("get_list", tools)
		self.assertIn("jarvis__" + ("x" * 80), tools)
		self.assertNotIn("bad tool!", tools)

	# -- (h) top_tools: tenant-wide top 15 ------------------------------------ #
	def test_top_tools_tenant_wide_cap(self):
		self._seed_settings(USER_A, 10)
		self._seed_settings(USER_B, 10)
		names = [f"tenant_tool_{i:02d}" for i in range(1, 17)]  # 16 distinct, tied count
		_seed_tool_messages(USER_A, "rollup-fixture-tenant-a", names[:8])
		_seed_tool_messages(USER_B, "rollup-fixture-tenant-b", names[8:])
		rollup, _ = usage_push._build_rollup()
		self.assertEqual(len(rollup["top_tools"]), 15)
		self.assertEqual([t["tool"] for t in rollup["top_tools"]], sorted(names)[:15])


class TestUsageRollupContextAndUsersDaily(FrappeTestCase):
	"""Sub-project C1, schema_version 2: the per-user ``context`` block and the
	top-level ``users_daily`` list. Hermetic like TestUsageRollup - fixtures
	are disposable and cleaned up via the module-level ``_cleanup``."""

	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _seed_settings(self, user: str, tokens: int) -> None:
		doc = usage.get_or_create_user_settings(user)
		frappe.db.set_value(
			USETT,
			doc.name,
			{
				"usage_month": usage.current_month_key(),
				"month_input_tokens": tokens,
				"month_output_tokens": tokens,
				"month_tokens": tokens * 2,
			},
			update_modified=False,
		)
		frappe.db.commit()

	# -- context block: sourced from Jarvis Chat Session snapshots ---------- #
	def test_context_block_from_chat_sessions(self):
		self._seed_settings(USER_A, 10)
		_make_session("agent:tu-roll-ctx1", USER_A)
		_make_session("agent:tu-roll-ctx2", USER_A)
		frappe.db.set_value(
			SESSION,
			{"session_key": "agent:tu-roll-ctx1"},
			{
				"last_total_tokens": 50000,
				"context_capacity": 200000,
				"context_pct": 25.0,
				"last_usage_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10),
			},
			update_modified=False,
		)
		frappe.db.set_value(
			SESSION,
			{"session_key": "agent:tu-roll-ctx2"},
			{
				"last_total_tokens": 170000,
				"context_capacity": 200000,
				"context_pct": 85.0,
				"last_usage_at": frappe.utils.now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
		rollup, _ = usage_push._build_rollup()
		ctx = {u["email"]: u for u in rollup["users"]}[USER_A]["context"]
		self.assertEqual(ctx["used_max"], 170000)
		self.assertEqual(ctx["capacity"], 200000)
		self.assertEqual(ctx["pct_max"], 85.0)
		self.assertEqual(ctx["sessions_over_80"], 1)
		self.assertIsNotNone(ctx["last_seen_at"])
		self.assertTrue(ctx["last_seen_at"].endswith("Z"))

	def test_context_block_null_when_no_session_reported_capacity(self):
		# A user with a settings row but no Chat Session that ever reported a
		# capacity (e.g. pre-C1 sessions) must not fabricate one.
		self._seed_settings(USER_A, 10)
		rollup, _ = usage_push._build_rollup()
		ctx = {u["email"]: u for u in rollup["users"]}[USER_A]["context"]
		self.assertEqual(ctx, usage_push._EMPTY_CONTEXT)

	# -- review fix 2: pct is computed PER SESSION, not used_max paired with -#
	# -- a DIFFERENT session's capacity --------------------------------------#
	def test_context_block_picks_highest_pct_session_not_highest_used_session(self):
		self._seed_settings(USER_A, 10)
		# Session 1: a huge-context model, LOW pct despite the highest raw
		# used_max (190000 tokens).
		_make_session("agent:tu-roll-pct1", USER_A)
		frappe.db.set_value(
			SESSION,
			{"session_key": "agent:tu-roll-pct1"},
			{
				"last_total_tokens": 190000,
				"context_capacity": 1000000,
				"last_usage_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10),
			},
			update_modified=False,
		)
		# Session 2: a small-context model, HIGH pct despite fewer raw tokens.
		# The buggy pairing (highest used_max + "newest" capacity) would have
		# reported used_max=190000 against capacity=100000 -> a nonsensical
		# 190% - or, if session 1 happened to be "newest", the right capacity
		# by luck; this fixture makes session 2 the newest too, so the OLD
		# code's "capacity from the newest row" would have picked session 2's
		# capacity (100000) but still used_max from session 1 (190000).
		_make_session("agent:tu-roll-pct2", USER_A)
		frappe.db.set_value(
			SESSION,
			{"session_key": "agent:tu-roll-pct2"},
			{
				"last_total_tokens": 90000,
				"context_capacity": 100000,
				"last_usage_at": frappe.utils.now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
		rollup, _ = usage_push._build_rollup()
		ctx = {u["email"]: u for u in rollup["users"]}[USER_A]["context"]
		# Session 2 has the higher OWN pct (90% vs 19%), so it must win -
		# never a used_max/capacity pair that spans two different sessions.
		self.assertEqual(ctx["used_max"], 90000)
		self.assertEqual(ctx["capacity"], 100000)
		self.assertEqual(ctx["pct_max"], 90.0)
		self.assertEqual(ctx["sessions_over_80"], 1)

	# -- review fix 5: users[].email lowercased, matching users_daily -------- #
	def test_users_email_lowercased_in_rollup(self):
		mixed_email = "MixedCase@Example.Test"
		lower_email = mixed_email.lower()
		_ensure_user(lower_email)
		doc = frappe.get_doc({"doctype": USETT, "user": mixed_email, "owner": lower_email})
		doc.insert(ignore_permissions=True)
		# Frappe's own Link-field validation canonicalizes `user` to the DB's
		# stored (lowercase) docname BEFORE autoname runs (base_document.py:
		# "MySQL is case insensitive. Preserve case of the original docname"),
		# so doc.name is already lowercase here - a normal insert can never
		# seed a mixed-case row. Force the STORED row to mixed case directly
		# (a legacy row / data anomaly is exactly what this fix defends
		# against) so the test exercises _build_rollup's own lowering, not
		# Frappe's.
		self.assertEqual(doc.name, lower_email, "sanity: Frappe already lowercased this on insert")
		frappe.db.sql(
			"UPDATE `tabJarvis User Settings` SET name=%(mixed)s, user=%(mixed)s WHERE name=%(lower)s",
			{"mixed": mixed_email, "lower": lower_email},
		)

		def _cleanup_mixed_case_row():
			frappe.db.sql(
				"DELETE FROM `tabJarvis User Settings` WHERE name=%(mixed)s", {"mixed": mixed_email}
			)
			frappe.db.commit()

		self.addCleanup(_cleanup_mixed_case_row)
		frappe.db.set_value(
			USETT,
			mixed_email,
			{
				"usage_month": usage.current_month_key(),
				"month_input_tokens": 1,
				"month_output_tokens": 1,
				"month_tokens": 2,
			},
			update_modified=False,
		)
		frappe.db.commit()
		rollup, _ = usage_push._build_rollup()
		emails = [u["email"] for u in rollup["users"]]
		self.assertIn(lower_email, emails)
		self.assertNotIn(mixed_email, emails)

	# -- review fix 6: the UTC ZoneInfo is built once at module level -------- #
	def test_iso_utc_z_reuses_module_level_utc_zoneinfo(self):
		from zoneinfo import ZoneInfo as RealZoneInfo

		calls = []

		def spy(key):
			calls.append(key)
			return RealZoneInfo(key)

		with patch("jarvis.chat.usage_push.ZoneInfo", side_effect=spy):
			usage_push._iso_utc_z(frappe.utils.now_datetime())
			usage_push._iso_utc_z(frappe.utils.now_datetime())
		self.assertNotIn("UTC", calls, "UTC must come from the module-level _UTC, not a fresh ZoneInfo call")
		self.assertIsInstance(usage_push._UTC, RealZoneInfo)

	# -- users_daily: grouping, window, per_model cap, email case ----------- #
	def test_users_daily_rollup_groups_by_user_day_model(self):
		today = frappe.utils.today()
		yesterday = frappe.utils.add_days(today, -1)
		_seed_turn_row("agent:tu-roll-ud1", USER_A, tokens_in=100, tokens_out=20, model="gpt-5.5", day=today)
		_seed_turn_row(
			"agent:tu-roll-ud2", USER_A, tokens_in=50, tokens_out=10, model="claude-sonnet", day=today
		)
		_seed_turn_row(
			"agent:tu-roll-ud3", USER_A, tokens_in=30, tokens_out=5, model="gpt-5.5", day=yesterday
		)
		rows = usage_push._users_daily_rollup()
		by_key = {(r["email"], r["day"]): r for r in rows}
		today_row = by_key[(USER_A.lower(), str(today))]
		self.assertEqual(today_row["turns"], 2)
		self.assertEqual(today_row["tokens_in"], 150)
		self.assertEqual(today_row["tokens_out"], 30)
		self.assertEqual(
			today_row["per_model"],
			{"gpt-5.5": {"in": 100, "out": 20}, "claude-sonnet": {"in": 50, "out": 10}},
		)
		yest_row = by_key[(USER_A.lower(), str(yesterday))]
		self.assertEqual(yest_row["turns"], 1)
		self.assertEqual(yest_row["tokens_in"], 30)

	def test_users_daily_rollup_window_is_35_days_including_today(self):
		today = frappe.utils.today()
		in_window_day = frappe.utils.add_days(today, -34)  # 35th day back - still inside
		out_of_window_day = frappe.utils.add_days(today, -35)  # 36 days back - outside
		_seed_turn_row("agent:tu-roll-win-in", USER_A, tokens_in=5, tokens_out=1, day=in_window_day)
		_seed_turn_row("agent:tu-roll-win-out", USER_A, tokens_in=999, tokens_out=999, day=out_of_window_day)
		rows = usage_push._users_daily_rollup()
		days = {r["day"] for r in rows if r["email"] == USER_A.lower()}
		self.assertIn(str(in_window_day), days)
		self.assertNotIn(str(out_of_window_day), days)

	def test_users_daily_email_lowercased(self):
		today = frappe.utils.today()
		_seed_turn_row("agent:tu-roll-case", "MixedCase@Example.Test", tokens_in=5, tokens_out=1, day=today)
		rows = usage_push._users_daily_rollup()
		emails = {r["email"] for r in rows if r["day"] == str(today)}
		self.assertIn("mixedcase@example.test", emails)

	def test_users_daily_per_model_capped_at_20_but_day_totals_cover_all(self):
		today = frappe.utils.today()
		for i in range(25):
			_seed_turn_row(
				f"agent:tu-roll-pm{i:02d}",
				USER_A,
				tokens_in=100 - i,
				tokens_out=1,
				model=f"model-{i:02d}",
				day=today,
			)
		rows = usage_push._users_daily_rollup()
		today_row = next(r for r in rows if r["email"] == USER_A.lower() and r["day"] == str(today))
		self.assertEqual(today_row["turns"], 25)
		self.assertEqual(len(today_row["per_model"]), usage_push._USERS_DAILY_PER_MODEL_CAP)
		self.assertEqual(today_row["tokens_in"], sum(100 - i for i in range(25)))

	def test_users_daily_rollup_empty_returns_empty_list(self):
		# Hermetic (no real Turn Usage rows touched): the query itself is
		# mocked away, isolating the "no rows at all" shape from this bench's
		# live traffic.
		with patch.object(frappe.db, "sql", return_value=[]):
			self.assertEqual(usage_push._users_daily_rollup(), [])

	# -- users_daily row cap: trims lowest-token rows first, no DB needed --- #
	def test_trim_users_daily_rows_keeps_highest_token_rows(self):
		rows = [
			{
				"email": f"u{i}@example.test",
				"day": "2026-09-01",
				"turns": 1,
				"tokens_in": i,
				"tokens_out": 0,
				"per_model": {},
			}
			for i in range(10)
		]
		with patch.object(frappe, "logger") as mock_logger:
			kept = usage_push._trim_users_daily_rows(rows, cap=5)
		self.assertEqual(len(kept), 5)
		self.assertEqual({r["tokens_in"] for r in kept}, {5, 6, 7, 8, 9})
		mock_logger.return_value.warning.assert_called_once()

	def test_trim_users_daily_rows_no_op_under_cap(self):
		rows = [{"email": "u@example.test", "day": "2026-09-01", "turns": 1, "tokens_in": 1, "tokens_out": 0}]
		with patch.object(frappe, "logger") as mock_logger:
			kept = usage_push._trim_users_daily_rows(rows, cap=5000)
		self.assertEqual(kept, rows)
		mock_logger.assert_not_called()
