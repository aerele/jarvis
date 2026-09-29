"""Snapshot-isolation races on per-user chat state: settings-row creation
(W3), usage accrual (J1), suggestion writes (J2), and the runtime-profile
receipt (J3). See jarvis.chat.txn for the one mechanism every fix here goes
through.

Every race test arms the real race (jarvis.tests.race_harness): a plain read
opens the snapshot the real code would hold, a second connection commits the
competing write, then the real unit runs. assert_fix_closes_race runs each
scenario with the defences in jarvis.chat.txn switched off (the race must
bite) and on (the outcome must be right), so none of these can pass without
the fix.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import finalize, suggestions, usage
from jarvis.chat import runtime_profile as rp
from jarvis.tests._gateway_fixtures import TEST_PROFILE
from jarvis.tests.race_harness import (
	as_job,
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)
from jarvis.tests.test_runtime_profile import connection, envelope

USETT = "Jarvis User Settings"
SESSION = "Jarvis Chat Session"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"


def _race_once(real, after=None, before=None):
	"""Wrap ``real`` so its FIRST call runs the race around it: ``before`` ahead of
	the call, ``after`` right after it. Later calls (a replay) pass straight
	through. Same idiom as ``test_snapshot_races_pump.py``'s helper of the same
	name."""
	fired: list = []

	def wrapper(*args, **kwargs):
		first = not fired
		fired.append(1)
		if first and before:
			before()
		result = real(*args, **kwargs)
		if first and after:
			after()
		return result

	return wrapper


def _ensure_user(email: str) -> None:
	"""A real, minimal User row: Jarvis User Settings' ``user`` field is a
	mandatory Link, so a race fixture needs a genuine target, not just a string."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "RaceTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()


def _cleanup_user_state(user: str) -> None:
	"""Every row a scenario's unit could have committed for ``user``, INCLUDING the
	disposable ``User`` doc itself (reviewer MINOR): every scenario here generates a
	fresh, uniquely-hashed email per run so a race always starts from a clean
	settings row, so unlike the FIXED, reused test users elsewhere in this suite
	(e.g. ``test_snapshot_races_jobs.py``'s ``JOBS_TEST_USER``), leaving the User
	behind would leak one per run. The units under test commit on purpose, so
	FrappeTestCase rollback cannot undo any of this - explicit cleanup only."""
	for dt in (USETT, SESSION, usage.TURN_USAGE):
		for name in frappe.get_all(dt, filters={"user": user}, pluck="name"):
			frappe.delete_doc(dt, name, ignore_permissions=True, force=True)
	if frappe.db.exists("User", user):
		frappe.delete_doc("User", user, ignore_permissions=True, force=True)
	frappe.db.commit()


def _insert_settings_row(other, user: str) -> None:
	"""A committed Jarvis User Settings row via the OTHER connection, matching the
	live schema's own column defaults. Autoname is ``field:user``, so name == user."""
	other.sql(
		"""INSERT INTO `tabJarvis User Settings`
			(name, creation, modified, modified_by, owner, docstatus, idx, user)
		   VALUES (%s, NOW(6), NOW(6), %s, %s, 0, 0, %s)""",
		(user, user, user, user),
	)


def _insert_session_row(other, name: str, session_key: str, user: str) -> None:
	other.sql(
		"""INSERT INTO `tabJarvis Chat Session`
			(name, creation, modified, modified_by, owner, docstatus, idx, session_key, user)
		   VALUES (%s, NOW(6), NOW(6), %s, %s, 0, 0, %s, %s)""",
		(name, user, user, session_key, user),
	)


class _RaceCase(FrappeTestCase):
	def setUp(self):
		frappe.db.rollback()

	def tearDown(self):
		frappe.db.rollback()


# --------------------------------------------------------------------------- #
# W3: usage.get_or_create_user_settings: exists()+insert() as one unit
# --------------------------------------------------------------------------- #
class TestGetOrCreateUserSettingsRace(_RaceCase):
	def test_a_concurrent_creator_wins_and_we_read_their_row(self):
		def scenario():
			# assert_fix_closes_race runs this twice; check() below reads the DB
			# AFTER scenario() returns, so cleanup must NOT happen here - it is
			# registered on the test instance instead and runs once, at the end
			# of the whole test method.
			user = f"jarvis-race-w3a-{frappe.generate_hash(length=8)}@example.test"
			_ensure_user(user)
			self.addCleanup(_cleanup_user_state, user)
			with snapshot_isolation_on():
				open_read_view()  # the exists() this unit is about to run
				with other_connection() as other:
					_insert_settings_row(other, user)
					other.commit()
				return user, usage.get_or_create_user_settings(user)

		def check(res):
			user, doc = res
			self.assertEqual(doc.user, user)
			self.assertEqual(frappe.db.count(USETT, {"user": user}), 1)

		assert_fix_closes_race(self, scenario, check)

	def test_a_prior_write_in_the_transaction_makes_the_conflict_reraise(self):
		"""The unsafe case the module docstring calls out: a caller that already
		wrote earlier in this same transaction (e.g. a finalize effect) must see
		the conflict, not a silently replayed insert that would publish OUR row
		while the caller's own now-aborted write is gone."""
		user = f"jarvis-race-w3b-{frappe.generate_hash(length=8)}@example.test"
		_ensure_user(user)
		scratch_parent = "__jarvis_race_user_state_scratch__"
		scratch_name = frappe.generate_hash(length=10)
		try:
			with other_connection() as other:
				other.sql(
					"""INSERT INTO `tabDefaultValue`
						(name, parent, parenttype, parentfield, defkey, defvalue, creation, modified)
					   VALUES (%s, %s, '__default', 'system_defaults', %s, 'a', NOW(), NOW())""",
					(scratch_name, scratch_parent, scratch_name),
				)
				other.commit()
			with snapshot_isolation_on():
				open_read_view()
				# Stand-in for a caller's own earlier write in this transaction.
				frappe.db.sql("UPDATE `tabDefaultValue` SET defvalue='mine' WHERE name=%s", scratch_name)
				with other_connection() as other:
					_insert_settings_row(other, user)
					other.commit()
				with self.assertRaises(frappe.QueryDeadlockError):
					usage.get_or_create_user_settings(user)
			frappe.db.rollback()
			# The server aborted the WHOLE transaction: our own earlier write is
			# gone too - exactly why replaying just the insert here would be wrong.
			with other_connection() as other:
				value = other.sql("SELECT defvalue FROM `tabDefaultValue` WHERE name=%s", scratch_name)[0][0]
			self.assertEqual(value, "a")
			self.assertEqual(frappe.db.count(USETT, {"user": user}), 1)  # the racer's row only
		finally:
			with other_connection() as other:
				other.sql("DELETE FROM `tabDefaultValue` WHERE name=%s", scratch_name)
				other.commit()
			_cleanup_user_state(user)


# --------------------------------------------------------------------------- #
# J1: usage.record_turn_usage - the atomic UPDATE, after the (already-run) poll
#
# BLOCKER regression (reviewer, full-diff): record_turn_usage used to open with
# txn.fresh_snapshot() as its OWN first statement, so under finalize (a job) it
# committed _effect_usage's uncommitted guard CAS (usage_recorded=1) before any
# retry path could veto it - the runner's rollback then had nothing left to
# undo and a RETRY turn's usage was lost forever, never retried. Fixed:
# record_turn_usage no longer commits early (this test still covers its own
# standalone-caller contract below: called directly, its accrual UPDATE alone
# replays a lost race, same as before); finalize._effect_usage now takes ONE
# fresh snapshot itself, BEFORE bundling the guard CAS + reply-model stamp +
# this call into a single replayed unit. See TestEffectUsageRetryContract and
# TestEffectUsageRace below for the finalize-level regression coverage.
# --------------------------------------------------------------------------- #
class TestRecordTurnUsageRace(_RaceCase):
	def test_a_competing_commit_between_the_poll_and_the_update_counts_once(self):
		def scenario():
			user = f"jarvis-race-j1-{frappe.generate_hash(length=8)}@example.test"
			session_key = f"race-j1-{frappe.generate_hash(length=8)}"
			session_name = frappe.generate_hash(length=10)
			_ensure_user(user)
			self.addCleanup(_cleanup_user_state, user)
			with other_connection() as other:
				_insert_settings_row(other, user)
				_insert_session_row(other, session_name, session_key, user)
				other.commit()

			def compete():
				with other_connection() as other:
					other.sql(
						"UPDATE `tabJarvis User Settings` SET total_tokens=total_tokens+1000 WHERE user=%s",
						user,
					)
					other.commit()

			# get_or_create_user_settings runs right before the UPDATE and is
			# ALREADY a plain read here (the row exists) - firing the competing
			# commit right after it lands the race exactly where record_turn_usage
			# would hit it live: after its own reads, at the accrual UPDATE.
			real_goc = usage.get_or_create_user_settings
			goc = _race_once(real_goc, after=compete)
			row = {"totalTokensFresh": True, "inputTokens": 10, "outputTokens": 5, "totalTokens": 100}
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(usage, "get_or_create_user_settings", goc),
			):
				outcome = usage.record_turn_usage(session_key, row, run_id=None)
			total = frappe.db.get_value(USETT, {"user": user}, "total_tokens")
			return outcome, total

		def check(res):
			outcome, total = res
			self.assertEqual(outcome, usage.USAGE_RECORDED)
			# 1000 from the competitor + 15 (10 in + 5 out) from this turn, applied
			# exactly once - not lost (USAGE_RETRY) and not double-counted.
			self.assertEqual(total, 1015)

		assert_fix_closes_race(self, scenario, check)


class TestRecordTurnUsageConflictPropagation(_RaceCase):
	"""A write conflict inside the per-model upsert or the turn-usage-row write has
	already aborted the WHOLE transaction (the aggregate accrual above included);
	record_turn_usage must re-raise it for the caller to replay, never swallow it
	into a log line plus a false USAGE_RECORDED. No real engine race needed here -
	the conflict is injected directly, so this is deterministic and fast."""

	def test_a_conflict_in_the_per_model_upsert_is_reraised(self):
		user = f"jarvis-race-j1c-{frappe.generate_hash(length=8)}@example.test"
		session_key = f"race-j1c-{frappe.generate_hash(length=8)}"
		session_name = frappe.generate_hash(length=10)
		_ensure_user(user)
		self.addCleanup(_cleanup_user_state, user)
		with other_connection() as other:
			_insert_settings_row(other, user)
			_insert_session_row(other, session_name, session_key, user)
			other.commit()
		row = {
			"totalTokensFresh": True,
			"inputTokens": 10,
			"outputTokens": 5,
			"totalTokens": 100,
			"model": "gpt-x",
			"modelProvider": "openai",
		}
		with patch.object(
			usage, "_upsert_model_usage", side_effect=frappe.QueryDeadlockError("simulated 1020")
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				usage.record_turn_usage(session_key, row, run_id=None)
		frappe.db.rollback()
		# The aggregate delta computed earlier in the SAME (now-aborted)
		# transaction never landed - the whole unit is gone, not just the upsert.
		total = frappe.db.get_value(USETT, {"user": user}, "total_tokens")
		self.assertEqual(int(total or 0), 0)

	def test_a_conflict_in_the_turn_usage_row_write_is_reraised(self):
		user = f"jarvis-race-j1d-{frappe.generate_hash(length=8)}@example.test"
		session_key = f"race-j1d-{frappe.generate_hash(length=8)}"
		session_name = frappe.generate_hash(length=10)
		_ensure_user(user)
		self.addCleanup(_cleanup_user_state, user)
		with other_connection() as other:
			_insert_settings_row(other, user)
			_insert_session_row(other, session_name, session_key, user)
			other.commit()
		row = {"totalTokensFresh": True, "inputTokens": 10, "outputTokens": 5, "totalTokens": 100}
		with patch.object(
			usage, "_insert_turn_usage_row", side_effect=frappe.QueryDeadlockError("simulated 1020")
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				usage.record_turn_usage(session_key, row, run_id="race-j1d-run")
		frappe.db.rollback()
		total = frappe.db.get_value(USETT, {"user": user}, "total_tokens")
		self.assertEqual(int(total or 0), 0)


FINALIZE_TEST_USER = "jarvis-race-finalize@example.com"


def _ensure_finalize_test_user() -> None:
	"""A FIXED, reused test user (like test_snapshot_races_jobs.py's
	JOBS_TEST_USER) for the finalize._effect_usage tests below - never deleted,
	so this whole group leaks nothing (contrast the fresh-per-run users above,
	which DO need per-run deletion; see _cleanup_user_state)."""
	if frappe.db.exists("User", FINALIZE_TEST_USER):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": FINALIZE_TEST_USER,
			"first_name": "Race",
			"last_name": "Finalize",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager", "Jarvis User")
	frappe.db.commit()


def _cleanup_finalize_fixtures() -> None:
	"""The units under test commit (finalize effects are job code), so a
	FrappeTestCase rollback cannot undo their fixtures - delete explicitly, by
	owner/user, same as test_snapshot_races_jobs.py's _cleanup_jobs_fixtures.
	FINALIZE_TEST_USER itself is FIXED and reused across every test here -
	never deleted."""
	convs = frappe.get_all(CONV, filters={"owner": FINALIZE_TEST_USER}, pluck="name")
	if convs:
		frappe.db.delete(TURN, {"conversation": ["in", convs]})
		frappe.db.delete(MSG, {"conversation": ["in", convs]})
	for name in convs:
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	for dt in (USETT, SESSION, usage.TURN_USAGE):
		for name in frappe.get_all(dt, filters={"user": FINALIZE_TEST_USER}, pluck="name"):
			frappe.delete_doc(dt, name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _mk_finalize_turn(session_key: str) -> tuple["finalize._Ctx", str, str]:
	"""A minimal, real (conversation, assistant message, turn) triple that
	finalize._effect_usage can run directly against, plus the _Ctx it needs.
	Owned by FINALIZE_TEST_USER - frappe.session.user must already be set to it
	(set_user_and_timestamps stamps ``owner`` from the session user on insert).
	session_key rides the dispatch_payload (not a Conversation field), which is
	exactly how a real turn's payload carries it."""
	conv = frappe.get_doc({"doctype": CONV, "title": "snapshot-race finalize"})
	conv.insert(ignore_permissions=True)
	amsg = frappe.get_doc(
		{"doctype": MSG, "conversation": conv.name, "seq": 1, "role": "assistant", "content": "Here you go."}
	)
	amsg.insert(ignore_permissions=True)
	run_id = f"race-fin-{frappe.generate_hash(length=10)}"
	frappe.get_doc(
		{
			"doctype": TURN,
			"run_id": run_id,
			"conversation": conv.name,
			"relay_target_id": "race_finalize",
			"turn_class": "interactive",
			"state": "finalizing",
			"seed_message": amsg.name,
			"assistant_message": amsg.name,
			"dispatch_payload": json.dumps({"session_key": session_key}),
			"enqueued_at": frappe.utils.now(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	ctx = finalize._Ctx(
		run_id=run_id,
		turn={"assistant_message": amsg.name},
		conversation=conv.name,
		owner=FINALIZE_TEST_USER,
		errored=False,
		payload={"session_key": session_key},
	)
	return ctx, run_id, amsg.name


class _FinalizeRaceCase(_RaceCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_finalize_test_user()

	def setUp(self):
		super().setUp()
		self._orig_user = frappe.session.user
		frappe.set_user(FINALIZE_TEST_USER)

	def tearDown(self):
		_cleanup_finalize_fixtures()
		frappe.set_user(self._orig_user)
		super().tearDown()


class TestEffectUsageRetryContract(_FinalizeRaceCase):
	"""CONTRACT regression for the BLOCKER (see the note above
	TestRecordTurnUsageRace): fresh_snapshot() must never run before the guard
	CAS it protects, or a retry's rollback has nothing left to undo and the
	guard stays committed - a turn's usage silently lost forever instead of
	retried next cycle."""

	def test_a_retry_never_commits_the_guard_the_runner_cannot_then_undo(self):
		session_key = f"race-fin-notfresh-{frappe.generate_hash(length=8)}"
		ctx, run_id, _amsg = _mk_finalize_turn(session_key)
		not_fresh_row = {"totalTokensFresh": False}
		with (
			as_job(),
			patch("jarvis.chat.agent_session_pool.checkout") as co,
			patch.object(usage, "fetch_fresh_session_row", return_value=not_fresh_row),
		):
			co.return_value.__enter__.return_value = MagicMock()
			with self.assertRaises(finalize._UsageRetry):
				finalize._effect_usage(ctx)
			# What run_finalize's except-branch does on ANY effect exception
			# (jarvis/chat/finalize.py: "Roll back this effect's uncommitted work").
			frappe.db.rollback()
		with other_connection() as other:
			recorded = other.sql(f"SELECT usage_recorded FROM `tab{TURN}` WHERE name=%s", run_id)[0][0]
		self.assertEqual(int(recorded), 0)

	def test_a_fresh_row_records_usage_exactly_once(self):
		session_key = f"race-fin-fresh-{frappe.generate_hash(length=8)}"
		ctx, run_id, amsg = _mk_finalize_turn(session_key)
		session_name = frappe.generate_hash(length=10)
		with other_connection() as other:
			_insert_settings_row(other, FINALIZE_TEST_USER)
			_insert_session_row(other, session_name, session_key, FINALIZE_TEST_USER)
			other.commit()
		fresh_row = {
			"totalTokensFresh": True,
			"inputTokens": 10,
			"outputTokens": 5,
			"totalTokens": 100,
			"model": "gpt-x",
			"modelProvider": "openai",
		}
		with (
			as_job(),
			patch("jarvis.chat.agent_session_pool.checkout") as co,
			patch.object(usage, "fetch_fresh_session_row", return_value=fresh_row),
		):
			co.return_value.__enter__.return_value = MagicMock()
			finalize._effect_usage(ctx)  # record_turn_usage commits internally on RECORDED
		recorded = frappe.db.get_value(TURN, run_id, "usage_recorded")
		self.assertEqual(int(recorded), 1)
		total = frappe.db.get_value(USETT, {"user": FINALIZE_TEST_USER}, "total_tokens")
		self.assertEqual(total, 15)
		model = frappe.db.get_value(MSG, amsg, "model")
		self.assertEqual(model, "gpt-x")


class TestEffectUsageRace(_FinalizeRaceCase):
	def test_a_competing_settings_commit_during_the_unit_is_replayed_exactly_once(self):
		def scenario():
			session_key = f"race-fin-race-{frappe.generate_hash(length=8)}"
			ctx, run_id, amsg = _mk_finalize_turn(session_key)
			session_name = frappe.generate_hash(length=10)
			# FINALIZE_TEST_USER is FIXED and reused across BOTH of
			# assert_fix_closes_race's runs, so its billed rows are reset to a
			# clean baseline here each time (the conversation/turn/message above
			# are already fresh per call - a new run_id/conversation every time -
			# so only the billed user's settings/session rows need resetting).
			frappe.db.delete(USETT, {"user": FINALIZE_TEST_USER})
			frappe.db.delete(SESSION, {"user": FINALIZE_TEST_USER})
			frappe.db.commit()
			with other_connection() as other:
				_insert_settings_row(other, FINALIZE_TEST_USER)
				_insert_session_row(other, session_name, session_key, FINALIZE_TEST_USER)
				other.commit()

			def compete():
				with other_connection() as other:
					other.sql(
						f"UPDATE `tab{USETT}` SET total_tokens=total_tokens+1000 WHERE user=%s",
						FINALIZE_TEST_USER,
					)
					other.commit()

			# get_or_create_user_settings runs inside record_turn_usage, itself
			# inside the guard+stamp+accrual unit _effect_usage replays whole -
			# firing the competing commit right after it returns lands the race
			# at the accrual UPDATE, same interleaving as TestRecordTurnUsageRace.
			real_goc = usage.get_or_create_user_settings
			goc = _race_once(real_goc, after=compete)
			fresh_row = {
				"totalTokensFresh": True,
				"inputTokens": 10,
				"outputTokens": 5,
				"totalTokens": 100,
				"model": "gpt-x",
				"modelProvider": "openai",
			}
			with (
				snapshot_isolation_on(),
				as_job(),
				patch("jarvis.chat.agent_session_pool.checkout") as co,
				patch.object(usage, "fetch_fresh_session_row", return_value=fresh_row),
				patch.object(usage, "get_or_create_user_settings", goc),
			):
				co.return_value.__enter__.return_value = MagicMock()
				try:
					finalize._effect_usage(ctx)
				except finalize._UsageRetry:
					# The swallowed-conflict outcome fix_disabled() reproduces
					# (is_write_conflict forced False, same as the pre-fix
					# behaviour): the guard+stamp+accrual unit never committed,
					# so roll back and read the pre-race state check() below
					# must reject - mirroring what run_finalize itself does on
					# any effect exception.
					frappe.db.rollback()
			recorded = frappe.db.get_value(TURN, run_id, "usage_recorded")
			total = frappe.db.get_value(USETT, {"user": FINALIZE_TEST_USER}, "total_tokens")
			model = frappe.db.get_value(MSG, amsg, "model")
			return recorded, total, model

		def check(res):
			recorded, total, model = res
			self.assertEqual(int(recorded or 0), 1)
			# 1000 from the competitor + 15 (10 in + 5 out) from this turn,
			# applied exactly once.
			self.assertEqual(total, 1015)
			self.assertEqual(model, "gpt-x")

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# J2: suggestions.store: the settings write, after the (already-run) gateway call
# --------------------------------------------------------------------------- #
class TestSuggestionsStoreRace(_RaceCase):
	def test_a_competing_commit_before_the_write_still_lands_our_content(self):
		def scenario():
			user = f"jarvis-race-j2-{frappe.generate_hash(length=8)}@example.test"
			_ensure_user(user)
			self.addCleanup(_cleanup_user_state, user)
			with other_connection() as other:
				_insert_settings_row(other, user)
				other.commit()

			def compete():
				with other_connection() as other:
					other.sql(
						"UPDATE `tabJarvis User Settings` SET prompt_suggestions='[]' WHERE user=%s", user
					)
					other.commit()

			real_goc = usage.user_settings_name
			goc = _race_once(real_goc, after=compete)
			items = [{"title": "Race check", "prompt": "show my race check suggestion"}]
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(usage, "user_settings_name", goc),
			):
				suggestions.store(user, items)
			stored = frappe.db.get_value(USETT, {"user": user}, "prompt_suggestions")
			return json.loads(stored or "[]")

		assert_fix_closes_race(
			self,
			scenario,
			lambda rows: self.assertEqual(
				rows, [{"title": "Race check", "prompt": "show my race check suggestion"}]
			),
		)


class TestSuggestionsTouchRace(_RaceCase):
	"""touch() is the one-field sibling of store() - same fix shape
	(fresh_snapshot + frappe.db.set_value + replay_on_conflict), proven
	load-bearing on its own rather than assumed from store()'s coverage."""

	def test_a_competing_commit_before_the_write_still_lands_our_stamp(self):
		def scenario():
			user = f"jarvis-race-j2b-{frappe.generate_hash(length=8)}@example.test"
			_ensure_user(user)
			self.addCleanup(_cleanup_user_state, user)
			with other_connection() as other:
				_insert_settings_row(other, user)
				other.commit()

			sentinel = frappe.utils.add_days(frappe.utils.now_datetime(), -30)

			def compete():
				with other_connection() as other:
					other.sql(
						"UPDATE `tabJarvis User Settings` SET prompt_suggestions_at=%s WHERE user=%s",
						(sentinel, user),
					)
					other.commit()

			real_goc = usage.user_settings_name
			goc = _race_once(real_goc, after=compete)
			before = frappe.utils.now_datetime()
			with (
				snapshot_isolation_on(),
				as_job(),
				patch.object(usage, "user_settings_name", goc),
			):
				suggestions.touch(user)
			stamp = frappe.utils.get_datetime(
				frappe.db.get_value(USETT, {"user": user}, "prompt_suggestions_at")
			)
			return stamp, sentinel, before

		def check(res):
			stamp, sentinel, before = res
			# Our own write must have landed on TOP of the competitor's (not lost,
			# not stuck at the stale sentinel the competitor wrote mid-race).
			self.assertNotEqual(stamp, sentinel)
			self.assertGreaterEqual(stamp, before)

		assert_fix_closes_race(self, scenario, check)


# --------------------------------------------------------------------------- #
# J3: runtime_profile.persist: non-locking compare, then a locked write only
# when something actually changed. These tests touch Jarvis Settings /
# DefaultValue only through a ROW/PARENT/KEY/CACHE_KEY namespace unique to
# each test run (patched on rp), and _anchor is patched to a fixed dict, so
# the real site's runtime-profile receipt and Jarvis Settings field values
# are never read or written - nothing to snapshot/restore for those. The one
# real-table touch (frappe.db.get_singles_dict("Jarvis Settings", ...)) is a
# lock, never a write, released with the transaction.
# --------------------------------------------------------------------------- #
class TestRuntimeProfilePersistNoLockWhenUnchanged(_RaceCase):
	def test_unchanged_profile_takes_no_lock_and_writes_nothing(self):
		suffix = frappe.generate_hash(length=8)
		row_name = f"runtime-race-unchanged-{suffix}"
		parent = f"__runtime_race_unchanged_{suffix}"
		key = "runtime_profile_v1"
		cache_key = f"jarvis:runtime_race_unchanged:{suffix}"
		anchor = {**connection(), "admin_url": "https://admin.example.test", "customer": "race@example.test"}
		raw = envelope()
		blob = rp._json({"profile": raw, "anchor": anchor, "fetched_at": "2020-01-01T00:00:00+00:00"})
		try:
			with other_connection() as other:
				other.sql(
					"""INSERT INTO `tabDefaultValue`
						(name, parent, parenttype, parentfield, defkey, defvalue, creation, modified)
					   VALUES (%s, %s, '__default', 'system_defaults', %s, %s, NOW(), NOW())""",
					(row_name, parent, key, blob),
				)
				other.commit()
			with (
				patch.object(rp, "ROW", row_name),
				patch.object(rp, "PARENT", parent),
				patch.object(rp, "KEY", key),
				patch.object(rp, "CACHE_KEY", cache_key),
				patch.object(rp, "_anchor", return_value=anchor),
			):
				rp._forget_snapshot()
				with patch.object(frappe.db, "get_singles_dict", wraps=frappe.db.get_singles_dict) as locks:
					rp.persist(raw, frappe._dict(), unavailable=False)
				locks.assert_not_called()
				self.assertEqual(getattr(frappe.local, rp._MEMO)[1], rp.validate(raw, anchor))
			# Untouched: still exactly the blob we seeded (fetched_at didn't move).
			self.assertEqual(frappe.db.get_value("DefaultValue", row_name, "defvalue"), blob)
		finally:
			rp._forget_snapshot()
			# Release anything our own connection might still hold before a
			# DIFFERENT connection (other_connection) touches the same row -
			# otherwise that DELETE waits on our own uncommitted lock.
			frappe.db.rollback()
			with other_connection() as other:
				other.sql("DELETE FROM `tabDefaultValue` WHERE parent=%s", parent)
				other.commit()


class TestRuntimeProfilePersistRace(_RaceCase):
	def test_a_changed_profile_replays_a_race_on_the_receipt_row(self):
		def scenario():
			suffix = frappe.generate_hash(length=8)
			row_name = f"runtime-race-changed-{suffix}"
			parent = f"__runtime_race_changed_{suffix}"
			key = "runtime_profile_v1"
			cache_key = f"jarvis:runtime_race_changed:{suffix}"
			anchor = {
				**connection(),
				"admin_url": "https://admin.example.test",
				"customer": "race@example.test",
			}
			raw = envelope()
			# The accepted state BEFORE this poll is "unavailable", so this poll's
			# real profile is a genuine change and persist() must take the locked
			# write path (an identical-profile poll never reaches it at all - see
			# TestRuntimeProfilePersistNoLockWhenUnchanged).
			seed = rp._json(
				{
					"profile": None,
					"anchor": anchor,
					"fetched_at": "2020-01-01T00:00:00+00:00",
					"unavailable": True,
				}
			)
			try:
				with other_connection() as other:
					other.sql(
						"""INSERT INTO `tabDefaultValue`
							(name, parent, parenttype, parentfield, defkey, defvalue, creation, modified)
						   VALUES (%s, %s, '__default', 'system_defaults', %s, %s, NOW(), NOW())""",
						(row_name, parent, key, seed),
					)
					other.commit()

				with (
					patch.object(rp, "ROW", row_name),
					patch.object(rp, "PARENT", parent),
					patch.object(rp, "KEY", key),
					patch.object(rp, "CACHE_KEY", cache_key),
					patch.object(rp, "_anchor", return_value=anchor),
					snapshot_isolation_on(),
				):
					rp._forget_snapshot()
					# A plain read opens the snapshot persist()'s own first read
					# would already hold - BEFORE the competing write commits, so
					# the race is armed with no lock our transaction holds yet on
					# row_name: the competitor's UPDATE below always runs against
					# an unlocked row and returns immediately, never a lock wait.
					# NOT as_job(): outside a job, replay_on_conflict's fresh=True
					# is a no-op, so this exercises the ACTUAL rollback+rerun path
					# rather than fresh_snapshot() sidestepping the race entirely.
					open_read_view()
					with other_connection() as other:
						other.sql("UPDATE `tabDefaultValue` SET modified=NOW(6) WHERE name=%s", row_name)
						other.commit()
					rp.persist(raw, frappe._dict(), unavailable=False)
					rp._forget_snapshot()  # force get_profile() to decode the DB row, not the memo
					profile = rp.get_profile()
				return profile
			finally:
				rp._forget_snapshot()
				# A successful persist() never commits (the caller owns that), so
				# our transaction can still hold the FOR UPDATE lock this test's
				# own write took. Release it before other_connection's DELETE
				# below, or that DELETE waits on our own uncommitted lock instead
				# of racing anything. Safe to discard: the seed row was already
				# committed by a SEPARATE connection, so rolling back only drops
				# OUR local, already-captured-into-`profile`, uncommitted write.
				frappe.db.rollback()
				with other_connection() as other:
					other.sql("DELETE FROM `tabDefaultValue` WHERE parent=%s", parent)
					other.commit()

		assert_fix_closes_race(self, scenario, lambda profile: self.assertEqual(profile, TEST_PROFILE))


class TestRuntimeProfilePersistNoEarlyCommit(_RaceCase):
	"""BLOCKER regression (round-2 review): persist() used to pass fresh=True to
	replay_on_conflict, and a fresh snapshot COMMITS under as_job() (a job or a
	migrate) - see txn.fresh_snapshot(). persist() runs inside a LARGER caller
	transaction: onboarding.write_connection does db_set("agent_url") ->
	persist(profile) -> set_settings_password(agent_token) -> the
	chat_was_ready_at/chat_ready_authority resets, all as ONE transaction; and
	after_migrate guards the very same chain with a savepoint that a mid-chain
	commit would invalidate (ROLLBACK TO SAVEPOINT fails once the transaction has
	already committed past it). With fresh=True, persist's OWN commit landed the
	caller's EARLIER write (agent_url) durably before persist even ran its own
	write; if a LATER statement in that same caller chain then raised, the
	caller's rollback (mimicking a job runner's exception handling) had nothing
	left to undo - a half-applied connection write would have survived a failed
	transaction. Fixed: persist() never passes fresh=True, so nothing it does
	commits early; a lost race inside it is only replayed when
	replay_is_safe() (nothing written yet in the WHOLE transaction, caller's
	writes included) - and when it is not safe, the conflict goes back to the
	caller, which alone decides when to end its own transaction.

	A faithful stand-in for onboarding.write_connection (calling it directly
	pulls in tenant_authority.guard, admin credentials and the chat-readiness
	resets): a first Jarvis Settings write, then runtime_profile.persist with a
	genuinely CHANGED profile (so it takes the write path), then a later write
	that raises."""

	def test_a_failure_after_persist_leaves_nothing_from_the_transaction_durable(self):
		suffix = frappe.generate_hash(length=8)
		row_name = f"runtime-race-nocommit-{suffix}"
		parent = f"__runtime_race_nocommit_{suffix}"
		key = "runtime_profile_v1"
		cache_key = f"jarvis:runtime_race_nocommit:{suffix}"
		anchor = {
			**connection(),
			"admin_url": "https://admin.example.test",
			"customer": "race@example.test",
		}
		raw = envelope()
		sentinel_url = f"https://sentinel-{suffix}.example.test"

		# The site is live: snapshot the one real Jarvis Settings field this test
		# writes, restore it in finally regardless of outcome - the assertions
		# below prove the fix does this on its own via rollback, but the restore
		# itself must not depend on the code under test being correct.
		original_agent_url = frappe.db.get_single_value("Jarvis Settings", "agent_url", cache=False)
		try:
			with (
				patch.object(rp, "ROW", row_name),
				patch.object(rp, "PARENT", parent),
				patch.object(rp, "KEY", key),
				patch.object(rp, "CACHE_KEY", cache_key),
				patch.object(rp, "_anchor", return_value=anchor),
				as_job(),
			):
				rp._forget_snapshot()
				# Stand-in for write_connection's FIRST statement, s.db_set("agent_url", ...).
				frappe.db.set_single_value(
					"Jarvis Settings", "agent_url", sentinel_url, update_modified=False
				)
				# persist() must take the WRITE path: no stored receipt yet for this
				# ROW, so this is a genuine change (an identical-profile poll never
				# reaches _persist_locked at all - see
				# TestRuntimeProfilePersistNoLockWhenUnchanged).
				rp.persist(raw, frappe._dict(), unavailable=False)

				def _later_write_then_raise():
					# Stand-in for set_settings_password(agent_token) / the
					# chat-ready resets: a LATER write in the SAME caller
					# transaction that then fails.
					frappe.db.set_single_value(
						"Jarvis Settings", "agent_url", "unused-should-never-land", update_modified=False
					)
					raise RuntimeError("simulated later failure in write_connection")

				with self.assertRaises(RuntimeError):
					_later_write_then_raise()
				# What the job runner's except-branch does on any exception.
				frappe.db.rollback()
				# Read via a genuinely SEPARATE connection: nothing from the whole
				# sequence - the caller's earlier write, persist's own write, the
				# later failed write - is durable. With the old fresh=True,
				# agent_url would read back as sentinel_url here (committed by
				# persist's own premature commit before the later failure ever
				# happened).
				with other_connection() as other:
					live_agent_url = other.sql(
						"SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s",
						("Jarvis Settings", "agent_url"),
					)
					receipt_rows = other.sql(
						"SELECT COUNT(*) FROM `tabDefaultValue` WHERE parent=%s", parent
					)[0][0]
			# An unset field reads back as SQL NULL (or no row) over the raw connection but
			# as "" through get_single_value, which casts it; both mean "unset".
			live_value = live_agent_url[0][0] if live_agent_url else None
			self.assertNotEqual(live_value, sentinel_url)
			self.assertEqual(live_value or "", original_agent_url or "")
			self.assertEqual(receipt_rows, 0)
		finally:
			rp._forget_snapshot()
			with other_connection() as other:
				other.sql("DELETE FROM `tabDefaultValue` WHERE parent=%s", parent)
				other.commit()
			frappe.db.set_single_value(
				"Jarvis Settings", "agent_url", original_agent_url, update_modified=False
			)
			frappe.db.commit()
