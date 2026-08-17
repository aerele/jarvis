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
) -> str:
	"""Insert a bare Jarvis Turn Usage row (task U2 rollup tests): the rollup
	builder consumes these rows directly, so seeding them straight (rather
	than round-tripping through record_turn_usage's gateway-row shape) keeps
	the fixtures focused on the aggregation being tested. ``day`` defaults to
	today (the real current month); pass an explicit day to land a row in a
	synthetic month a test has pinned via current_month_key, keeping it
	isolated from this bench's real Turn Usage traffic in the real current
	month (see test_empty_month_yields_old_shape_plus_empty_new_fields)."""
	doc = frappe.get_doc(
		{
			"doctype": TURN_USAGE,
			"session_key": session_key,
			"user": user,
			"profile_agent_id": profile_agent_id,
			"profile_tier": "full",
			"model": "gpt-5.5",
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
		with patch("jarvis.chat.usage.current_month_key", return_value="2000-01"):
			rollup, truncated = usage_push._build_rollup()
		self.assertFalse(truncated)
		self.assertEqual(rollup["month_key"], "2000-01")
		self.assertEqual(rollup["users"], [])
		self.assertEqual(rollup["by_profile"], [])
		self.assertEqual(rollup["top_tools"], [])

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
