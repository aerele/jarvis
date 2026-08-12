"""Tests for per-user settings, the Jarvis Admin gate, and real usage
tracking (design sections 1-5, 7).

Hermetic: disposable enabled System Users (one per role shape) are created in
``setUp`` and deleted in ``tearDown``. Because ``record_turn_usage`` /
``admin_set_user_limit`` / ``refresh_session_snapshots`` COMMIT (they must
persist real usage), ``tearDown`` explicitly deletes every ``Jarvis User
Settings`` + ``Jarvis Chat Session`` row owned by a fixture user — the
FrappeTestCase transaction rollback cannot undo a commit.

Gateway I/O is always mocked; no test requires a live container. Negative
role cases explicitly strip roles from the fixture user so they hold on CI's
fresh DB as well as the role-polluted local ``site.jarvis``.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import policy, usage, user_settings_api
from jarvis.exceptions import AgentUnreachableError
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
	has_jarvis_admin_access,
	require_jarvis_admin,
)

USETT = "Jarvis User Settings"
SESSION = "Jarvis Chat Session"

USER_A = "jarvis-usett-a@example.test"
USER_B = "jarvis-usett-b@example.test"
USER_ADMIN = "jarvis-usett-admin@example.test"
USER_PLAIN = "jarvis-usett-plain@example.test"
_ALL_USERS = (USER_A, USER_B, USER_ADMIN, USER_PLAIN)


def _ensure_user(email: str, roles: tuple[str, ...] = ()) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "UsageTest",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	if roles:
		doc.add_roles(*roles)


def _strip_admin_roles(email: str) -> None:
	"""Guarantee the fixture user is NOT an admin, so a negative gate assertion
	holds on the role-polluted local site too."""
	doc = frappe.get_doc("User", email)
	present = {r.role for r in doc.get("roles", [])}
	drop = present & {"System Manager", JARVIS_ADMIN_ROLE}
	if drop:
		doc.remove_roles(*drop)


def _make_session(session_key: str, user: str) -> None:
	frappe.get_doc(
		{
			"doctype": SESSION,
			"session_key": session_key,
			"user": user,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _cleanup_fixture_rows() -> None:
	for email in _ALL_USERS:
		for name in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(SESSION, filters={"user": email}, pluck="name"):
			frappe.delete_doc(SESSION, name, ignore_permissions=True, force=True)


class _UsageTestBase(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_user(USER_A, (JARVIS_USER_ROLE,))
		_ensure_user(USER_B, (JARVIS_USER_ROLE,))
		_ensure_user(USER_ADMIN, (JARVIS_ADMIN_ROLE,))
		_ensure_user(USER_PLAIN, (JARVIS_USER_ROLE,))
		_strip_admin_roles(USER_A)
		_strip_admin_roles(USER_B)
		_strip_admin_roles(USER_PLAIN)
		_cleanup_fixture_rows()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup_fixture_rows()
		frappe.db.commit()
		frappe.set_user(self._orig_user)


# --------------------------------------------------------------------------- #
# 1. Lazy creation + owner
# --------------------------------------------------------------------------- #
class TestLazyCreation(_UsageTestBase):
	def test_creates_row_with_explicit_owner(self):
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		doc = usage.get_or_create_user_settings(USER_A)
		self.assertEqual(doc.user, USER_A)
		# owner must be the settings user even though Administrator triggered it.
		self.assertEqual(frappe.db.get_value(USETT, doc.name, "owner"), USER_A)
		# Defaults. activity_detail defaults ON to match the SPA's default for
		# fresh devices (upstream/main product decision — see stores/shell.js).
		self.assertEqual(frappe.utils.cint(doc.notify_enabled), 1)
		self.assertEqual(frappe.utils.cint(doc.activity_detail), 1)
		self.assertEqual(frappe.utils.cint(doc.monthly_token_limit), 0)

	def test_idempotent(self):
		a = usage.get_or_create_user_settings(USER_A)
		b = usage.get_or_create_user_settings(USER_A)
		self.assertEqual(a.name, b.name)
		self.assertEqual(len(frappe.get_all(USETT, filters={"user": USER_A})), 1)

	def test_get_my_settings_lazy_creates(self):
		frappe.set_user(USER_A)
		out = user_settings_api.get_my_settings()
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["user"], USER_A)
		self.assertEqual(out["data"]["notify_enabled"], 1)
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists(USETT, {"user": USER_A}))


# --------------------------------------------------------------------------- #
# 2. Preference update ownership (A cannot write B) + permlevel via API
# --------------------------------------------------------------------------- #
class TestPreferenceOwnership(_UsageTestBase):
	def test_update_touches_only_own_row(self):
		# B starts with notify on.
		usage.get_or_create_user_settings(USER_B)
		frappe.db.commit()
		frappe.set_user(USER_A)
		out = user_settings_api.update_my_settings(notify_enabled=0, activity_detail=1)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["notify_enabled"], 0)
		self.assertEqual(out["data"]["activity_detail"], 1)
		frappe.set_user("Administrator")
		# B untouched.
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_value(USETT, {"user": USER_B}, "notify_enabled")),
			1,
		)

	def test_if_owner_blocks_cross_user_write(self):
		b_name = usage.get_or_create_user_settings(USER_B).name
		a_name = usage.get_or_create_user_settings(USER_A).name
		frappe.db.commit()
		# A may write its own row but not B's (permlevel-0 grant is if_owner).
		self.assertTrue(frappe.has_permission(USETT, "write", doc=a_name, user=USER_A))
		self.assertFalse(frappe.has_permission(USETT, "write", doc=b_name, user=USER_A))

	def test_owner_cannot_change_own_limit_via_prefs_api(self):
		# Admin sets a limit; the owner's pref update must not disturb it
		# (monthly_token_limit is permlevel 1 and not an update_my_settings arg).
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(notify_enabled=0)
		out = user_settings_api.get_my_settings()
		# Owner can READ the limit (permlevel-1 read granted to All)...
		self.assertEqual(out["data"]["monthly_token_limit"], 100)
		frappe.set_user("Administrator")
		# ...and it is unchanged in the DB.
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_value(USETT, {"user": USER_A}, "monthly_token_limit")),
			100,
		)


# --------------------------------------------------------------------------- #
# 3. Admin gating + set-limit flow
# --------------------------------------------------------------------------- #
class TestAdminGate(_UsageTestBase):
	def test_admin_role_passes(self):
		self.assertTrue(has_jarvis_admin_access(USER_ADMIN))
		frappe.set_user(USER_ADMIN)
		require_jarvis_admin()  # must not raise
		out = user_settings_api.admin_list_user_usage()
		self.assertTrue(out["ok"])
		self.assertIsInstance(out["data"], list)

	def test_plain_user_refused(self):
		self.assertFalse(has_jarvis_admin_access(USER_PLAIN))
		frappe.set_user(USER_PLAIN)
		with self.assertRaises(frappe.PermissionError):
			require_jarvis_admin()
		with self.assertRaises(frappe.PermissionError):
			user_settings_api.admin_list_user_usage()

	def test_administrator_always_admin(self):
		self.assertTrue(has_jarvis_admin_access("Administrator"))

	def test_set_limit_creates_row(self):
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		out = user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=250)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["monthly_token_limit"], 250)
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_value(USETT, {"user": USER_A}, "monthly_token_limit")),
			250,
		)

	def test_set_limit_unknown_user(self):
		out = user_settings_api.admin_set_user_limit(user="nobody@example.invalid", monthly_token_limit=10)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "unknown_user")

	def test_admin_limit_coerces_non_string_user_and_model(self):
		# N10/I10: `user`/`model` must be string-coerced (_s) BEFORE the identity
		# check. A raw dict `user` would be read by frappe.db.exists as FILTERS, so
		# a crafted {"name": <real user>} could match and mutate a row the caller
		# never named. With _s() the dict stringifies and never matches; a list
		# `model` would 500 on .strip() without it. A revert at either site fails
		# here.
		out = user_settings_api.admin_set_user_limit(user={"name": USER_A}, monthly_token_limit=999)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "unknown_user")
		# The filter-match path must not have created / mutated USER_A's row.
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))
		out2 = user_settings_api.admin_set_user_model_limit(
			user={"name": USER_A}, model="gpt-x", monthly_token_limit=999
		)
		self.assertFalse(out2["ok"])
		self.assertEqual(out2["reason"], "unknown_user")
		# A list `model` must be coerced (not .strip()ed raw, which 500s). The point
		# of _s() at this site is crash-safety, so the call must simply not raise -
		# it accepts the string coercion. A revert makes this line raise instead.
		out3 = user_settings_api.admin_set_user_model_limit(
			user=USER_A, model=["gpt-x"], monthly_token_limit=999
		)
		self.assertTrue(out3["ok"])

	def test_admin_list_includes_row(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=99)
		out = user_settings_api.admin_list_user_usage()
		users = {r["user"]: r for r in out["data"]}
		self.assertIn(USER_A, users)
		self.assertEqual(users[USER_A]["monthly_token_limit"], 99)


# --------------------------------------------------------------------------- #
# 4. record_turn_usage — accumulation math + month rollover + skips
# --------------------------------------------------------------------------- #
class TestRecordTurnUsage(_UsageTestBase):
	def _row(self, **kw):
		base = {"totalTokensFresh": True, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
		base.update(kw)
		return base

	def test_accumulates_across_turns(self):
		_make_session("agent:acc", USER_A)
		usage.record_turn_usage("agent:acc", self._row(inputTokens=10, outputTokens=5, totalTokens=100))
		usage.record_turn_usage("agent:acc", self._row(inputTokens=8, outputTokens=12, totalTokens=140))
		s = frappe.db.get_value(
			USETT,
			{"user": USER_A},
			["month_input_tokens", "month_output_tokens", "month_tokens", "total_tokens", "usage_month"],
			as_dict=True,
		)
		self.assertEqual(s.month_input_tokens, 18)
		self.assertEqual(s.month_output_tokens, 17)
		self.assertEqual(s.month_tokens, 35)
		self.assertEqual(s.total_tokens, 35)
		self.assertEqual(s.usage_month, usage.current_month_key())
		sess = frappe.db.get_value(
			SESSION,
			{"session_key": "agent:acc"},
			["input_tokens", "output_tokens", "run_count", "last_total_tokens"],
			as_dict=True,
		)
		self.assertEqual(sess.input_tokens, 18)
		self.assertEqual(sess.output_tokens, 17)
		self.assertEqual(sess.run_count, 2)
		self.assertEqual(sess.last_total_tokens, 140)

	def test_month_rollover_resets_month_buckets(self):
		_make_session("agent:roll", USER_A)
		usage.record_turn_usage("agent:roll", self._row(inputTokens=10, outputTokens=5, totalTokens=50))
		# Simulate a stale month (previous accumulation was a prior month).
		frappe.db.set_value(USETT, {"user": USER_A}, "usage_month", "2020-01", update_modified=False)
		frappe.db.commit()
		usage.record_turn_usage("agent:roll", self._row(inputTokens=12, outputTokens=8, totalTokens=80))
		s = frappe.db.get_value(
			USETT,
			{"user": USER_A},
			["month_tokens", "total_tokens", "usage_month"],
			as_dict=True,
		)
		# Month buckets reset to the new delta (20); total is all-time (15+20=35).
		self.assertEqual(s.month_tokens, 20)
		self.assertEqual(s.total_tokens, 35)
		self.assertEqual(s.usage_month, usage.current_month_key())

	def test_skips_when_not_fresh(self):
		_make_session("agent:stale", USER_A)
		usage.record_turn_usage(
			"agent:stale", self._row(totalTokensFresh=False, inputTokens=10, outputTokens=5)
		)
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))

	def test_skips_null_token_fields(self):
		_make_session("agent:null", USER_A)
		usage.record_turn_usage(
			"agent:null", {"totalTokensFresh": True, "inputTokens": None, "outputTokens": None}
		)
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))

	def test_skips_zero_delta(self):
		_make_session("agent:zero", USER_A)
		usage.record_turn_usage("agent:zero", self._row(inputTokens=0, outputTokens=0))
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))

	def test_no_session_mapping_is_noop(self):
		# Unknown session_key: no settings row created, no raise.
		usage.record_turn_usage("agent:unknown", self._row(inputTokens=5, outputTokens=5))
		self.assertFalse(frappe.db.exists(USETT, {"user": USER_A}))

	def test_none_row_is_noop(self):
		usage.record_turn_usage("agent:whatever", None)  # must not raise


# --------------------------------------------------------------------------- #
# 4b. fetch_fresh_session_row — bounded freshness retry (live-reproduced gap:
#    a session's first completed run can read back stale on the first poll)
# --------------------------------------------------------------------------- #
class _PollingSess:
	"""Fake gateway session whose list_sessions() returns a different rows
	list on each call, so the poll loop can be observed call-by-call."""

	def __init__(self, rows_by_call):
		self._rows_by_call = rows_by_call
		self.calls = 0

	def list_sessions(self):
		idx = min(self.calls, len(self._rows_by_call) - 1)
		self.calls += 1
		return self._rows_by_call[idx]


class TestFetchFreshSessionRow(_UsageTestBase):
	def test_retries_until_fresh(self):
		stale = {"key": "agent:poll", "totalTokensFresh": False, "inputTokens": 1, "outputTokens": 1}
		fresh = {"key": "agent:poll", "totalTokensFresh": True, "inputTokens": 5, "outputTokens": 3}
		sess = _PollingSess([[stale], [fresh]])
		with patch("jarvis.chat.usage.time.sleep", return_value=None) as mock_sleep:
			row = usage.fetch_fresh_session_row(sess, "agent:poll")
		self.assertEqual(row, fresh)
		self.assertEqual(sess.calls, 2)
		mock_sleep.assert_called_once()

	def test_never_fresh_returns_last_row_after_attempts(self):
		stale = {"key": "agent:neverfresh", "totalTokensFresh": False, "inputTokens": 1, "outputTokens": 1}
		sess = _PollingSess([[stale]])
		with patch("jarvis.chat.usage.time.sleep", return_value=None):
			row = usage.fetch_fresh_session_row(sess, "agent:neverfresh", attempts=3)
		self.assertEqual(row, stale)
		self.assertEqual(sess.calls, 3)


# --------------------------------------------------------------------------- #
# 5. admin_sync_usage — snapshot refresh, no accumulation, unreachable
# --------------------------------------------------------------------------- #
class _FakeSess:
	def __init__(self, rows):
		self._rows = rows

	def list_sessions(self):
		return self._rows


class TestAdminSync(_UsageTestBase):
	def _patch_gateway(self, sess_or_exc):
		"""Patch a non-empty agent_url + the pooled checkout so no real WS ever
		opens."""

		@contextmanager
		def _fake_checkout(url):
			if isinstance(sess_or_exc, Exception):
				raise sess_or_exc
			yield sess_or_exc

		orig_agent_url = frappe.db.get_single_value("Jarvis Settings", "agent_url")
		frappe.db.set_single_value("Jarvis Settings", "agent_url", "http://gw.test")
		self.addCleanup(
			lambda: frappe.db.set_single_value("Jarvis Settings", "agent_url", orig_agent_url or "")
		)
		p = patch.object(user_settings_api.agent_session_pool, "checkout", _fake_checkout)
		p.start()
		self.addCleanup(p.stop)

	def test_refreshes_snapshots_without_accumulating(self):
		_make_session("agent:sa", USER_A)
		_make_session("agent:sb", USER_B)
		# sa carries the gateway's updatedAt (ms epoch) → last_usage_at must be
		# converted from THAT stamp, not sync time; sb has none → last_usage_at
		# stays untouched (an idle session must not look freshly active).
		updated_ms = 1700000000000  # 2023-11-14T22:13:20Z
		rows = [
			{"key": "agent:sa", "totalTokens": 500, "totalTokensFresh": True, "updatedAt": updated_ms},
			{"key": "agent:sb", "totalTokens": 300, "totalTokensFresh": True},
			{"key": "agent:unknown", "totalTokens": 999},
		]
		self._patch_gateway(_FakeSess(rows))
		out = user_settings_api.admin_sync_usage()
		self.assertTrue(out["ok"])
		# 3 gateway rows, but only the 2 mapped to a Jarvis Chat Session count
		# as synced (the pane renders these two counters verbatim).
		self.assertEqual(out["data"]["synced_sessions"], 2)
		self.assertEqual(out["data"]["users_updated"], 2)
		self.assertIn(USER_A, out["data"]["users"])
		self.assertIn(USER_B, out["data"]["users"])
		# Snapshot fields refreshed.
		self.assertEqual(frappe.db.get_value(SESSION, {"session_key": "agent:sa"}, "last_total_tokens"), 500)
		self.assertEqual(frappe.db.get_value(SESSION, {"session_key": "agent:sb"}, "last_total_tokens"), 300)
		# updatedAt → last_usage_at conversion (naive system-tz, like Frappe).
		from datetime import datetime as _dt

		self.assertEqual(
			frappe.db.get_value(SESSION, {"session_key": "agent:sa"}, "last_usage_at"),
			_dt.fromtimestamp(updated_ms / 1000),
		)
		self.assertIsNone(frappe.db.get_value(SESSION, {"session_key": "agent:sb"}, "last_usage_at"))
		# last_synced_at stamped; counters NOT accumulated (sync never counts).
		a = frappe.db.get_value(
			USETT,
			{"user": USER_A},
			["last_synced_at", "month_tokens", "total_tokens"],
			as_dict=True,
		)
		self.assertIsNotNone(a.last_synced_at)
		self.assertEqual(a.month_tokens, 0)
		self.assertEqual(a.total_tokens, 0)

	def test_gateway_unreachable(self):
		self._patch_gateway(AgentUnreachableError("down"))
		out = user_settings_api.admin_sync_usage()
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "gateway_unreachable")


# --------------------------------------------------------------------------- #
# 6. Enforcement in validate_can_send
# --------------------------------------------------------------------------- #
class TestEnforcement(_UsageTestBase):
	def test_no_row_allows(self):
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_over_limit_rejects_with_usage_limit(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 150},
			update_modified=False,
		)
		frappe.db.commit()
		ok, reason = policy.validate_can_send(USER_A)
		self.assertFalse(ok)
		self.assertEqual(reason, "usage_limit")

	def test_under_limit_allows(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 50},
			update_modified=False,
		)
		frappe.db.commit()
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_stale_month_allows_despite_high_count(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": "2020-01", "month_tokens": 9999},
			update_modified=False,
		)
		frappe.db.commit()
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)  # rollover ⇒ this month's usage is 0
		self.assertIsNone(reason)

	def test_zero_limit_is_unlimited(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=0)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 999999},
			update_modified=False,
		)
		frappe.db.commit()
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_send_message_surfaces_usage_limit(self):
		user_settings_api.admin_set_user_limit(user=USER_A, monthly_token_limit=100)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 150},
			update_modified=False,
		)
		frappe.db.commit()
		from jarvis.chat.api import send_message

		frappe.set_user(USER_A)
		# validate_can_send fires before any conversation lookup, so the soft
		# error returns immediately with the machine reason the SPA maps.
		out = send_message(conversation="JCONV-does-not-matter", message="hi")
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "usage_limit")


# --------------------------------------------------------------------------- #
# 7. get_usage()'s "measured" block
# --------------------------------------------------------------------------- #
class TestMeasuredUsage(_UsageTestBase):
	def test_no_row_returns_zeros(self):
		from jarvis.chat.api import _measured_usage

		m = _measured_usage(USER_A)
		self.assertIsNotNone(m)
		self.assertEqual(m["month_tokens"], 0)
		self.assertEqual(m["monthly_token_limit"], 0)

	def test_existing_row_wins(self):
		from jarvis.chat.api import _measured_usage

		usage.get_or_create_user_settings(USER_A)
		frappe.db.set_value(
			USETT,
			{"user": USER_A},
			{"usage_month": usage.current_month_key(), "month_tokens": 42, "total_tokens": 42},
			update_modified=False,
		)
		frappe.db.commit()
		m = _measured_usage(USER_A)
		self.assertEqual(m["month_tokens"], 42)
		self.assertEqual(m["total_tokens"], 42)

	def test_per_model_delegates_to_user_settings_api_helper(self):
		"""_measured_usage's per_model block must reuse
		user_settings_api._per_model_rows rather than reimplementing the same
		query + row-shaping inline (they had drifted into two copies of the
		same logic)."""
		from jarvis.chat import user_settings_api
		from jarvis.chat.api import _measured_usage

		usage.get_or_create_user_settings(USER_A)
		sentinel = [
			{
				"model": "sentinel-model",
				"month_input_tokens": 1,
				"month_output_tokens": 2,
				"month_tokens": 3,
				"monthly_token_limit": 4,
			}
		]
		with patch.object(user_settings_api, "_per_model_rows", return_value=sentinel) as mock_rows:
			m = _measured_usage(USER_A)
		mock_rows.assert_called_once_with(USER_A)
		self.assertEqual(m["per_model"], sentinel)


# --------------------------------------------------------------------------- #
# 8. Persona preference (per-user voice) + the trusted [Context:] clause
# --------------------------------------------------------------------------- #
class TestPersonaPreference(_UsageTestBase):
	"""preferred_persona picks Jarvis (default) or Jara. Only the non-default
	value rides the trusted [Context:] line (turn_handler._persona_clause), and
	the default path stays byte-identical to before the feature."""

	def test_default_is_jarvis_and_clause_empty(self):
		from jarvis.chat.turn_handler import _persona_clause

		# No row at all: clause is empty (default) and never raises.
		self.assertEqual(_persona_clause(USER_A), "")
		# Lazy-created row defaults to Jarvis; the payload reflects it.
		frappe.set_user(USER_A)
		out = user_settings_api.get_my_settings()
		self.assertEqual(out["data"]["preferred_persona"], "Jarvis")
		frappe.set_user("Administrator")
		self.assertEqual(_persona_clause(USER_A), "")

	def test_update_to_jara_persists_and_emits_clause(self):
		from jarvis.chat.turn_handler import _persona_clause

		frappe.set_user(USER_A)
		out = user_settings_api.update_my_settings(preferred_persona="Jara")
		self.assertEqual(out["data"]["preferred_persona"], "Jara")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "preferred_persona"), "Jara")
		self.assertEqual(_persona_clause(USER_A), "; persona: Jara")

	def test_switch_back_to_jarvis_clears_clause(self):
		from jarvis.chat.turn_handler import _persona_clause

		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		user_settings_api.update_my_settings(preferred_persona="Jarvis")
		frappe.set_user("Administrator")
		self.assertEqual(_persona_clause(USER_A), "")

	def test_unknown_persona_rejected(self):
		frappe.set_user(USER_A)
		with self.assertRaises(frappe.ValidationError):
			user_settings_api.update_my_settings(preferred_persona="Loki")
		frappe.set_user("Administrator")
		# Nothing persisted from the rejected write.
		self.assertIn(
			frappe.db.get_value(USETT, {"user": USER_A}, "preferred_persona") or "Jarvis",
			("Jarvis", None),
		)

	def test_unknown_persona_message_does_not_reflect_input(self):
		# The rejection is a fixed-enum sentence that echoes NOTHING back, so a
		# hostile value can never be reflected - the self-XSS F11 guards against.
		# A refactor to an f-string that interpolates the value would fail here.
		frappe.set_user(USER_A)
		with self.assertRaises(frappe.ValidationError) as cm:
			user_settings_api.update_my_settings(preferred_persona="<img src=x onerror=alert(1)>")
		frappe.set_user("Administrator")
		msg = str(cm.exception)
		self.assertNotIn("<img", msg)
		self.assertNotIn("onerror", msg)
		self.assertIn("Jarvis or Jara", msg)

	def test_non_string_persona_does_not_500(self):
		# This module's arg-type gate is not enforced at runtime (from __future__
		# import annotations), so a hostile non-string reaches the handler raw. It
		# must be coerced, not .strip()ed directly (which 500s on AttributeError).
		frappe.set_user(USER_A)
		# Structured values reject cleanly as a ValidationError, never a 500.
		for bad in (["Jara"], {"persona": "Jara"}):
			with self.assertRaises(frappe.ValidationError):
				user_settings_api.update_my_settings(preferred_persona=bad)
		# Falsy non-strings take the blank-clears-to-default path (store Jarvis).
		for falsy in (0, False, []):
			user_settings_api.update_my_settings(preferred_persona=falsy)
			self.assertEqual(user_settings_api.get_my_settings()["data"]["preferred_persona"], "Jarvis")
		frappe.set_user("Administrator")

	def test_persona_update_touches_only_own_row(self):
		usage.get_or_create_user_settings(USER_B)
		frappe.db.commit()
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.get_value(USETT, {"user": USER_B}, "preferred_persona") or "Jarvis", "Jarvis"
		)

	def test_persona_pref_leaves_other_prefs_untouched(self):
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(notify_enabled=0)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		out = user_settings_api.get_my_settings()
		# The persona-only update must not disturb the earlier notify choice.
		self.assertEqual(out["data"]["notify_enabled"], 0)
		self.assertEqual(out["data"]["preferred_persona"], "Jara")
		frappe.set_user("Administrator")

	def _assembled_prompt_for(self, persona_user):
		"""Insert a real conversation + message owned by ``persona_user`` and run the
		shared, read-only ``assemble_prompt`` (the ONE place the [Context:] bracket
		is built). Returns the assembled prompt string. conv/msg are cleaned up."""
		from jarvis.chat.turn_handler import CONV, MSG, assemble_prompt

		conv = frappe.get_doc({"doctype": CONV, "title": "persona-test", "auto_apply": 0}).insert(
			ignore_permissions=True
		)
		self.addCleanup(lambda: frappe.delete_doc(CONV, conv.name, force=True, ignore_permissions=True))
		msg = frappe.get_doc(
			{"doctype": MSG, "conversation": conv.name, "seq": 1, "role": "user", "content": "hello"}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc(MSG, msg.name, force=True, ignore_permissions=True))
		# chat_user - and thus the persona lookup - derives from the message owner.
		frappe.db.set_value(MSG, msg.name, "owner", persona_user, update_modified=False)
		ap = assemble_prompt(
			conv,
			message_id=msg.name,
			conversation_id=conv.name,
			context={},
			attachments=[],
			user=persona_user,
		)
		return ap.user_message

	def test_context_line_positions_persona_and_pins_byte_identical_default(self):
		# Jara: the clause lands in the trusted [Context:] bracket immediately
		# before "; chat user:", i.e. AFTER the date / locale / assistant-name
		# clauses (the fixed f-string order in assemble_prompt). This pins both the
		# presence AND the position.
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		frappe.set_user("Administrator")
		jara_prompt = self._assembled_prompt_for(USER_A)
		self.assertIn("; persona: Jara; chat user:", jara_prompt)
		# EXACTLY one persona segment: a duplicated {persona_clause}{persona_clause}
		# merge slip still satisfies the assertIn above, so pin the count too.
		self.assertEqual(jara_prompt.count("persona:"), 1)

		# Default user (USER_B, untouched): the assembled turn is byte-identical to
		# before the feature - the bracket carries NO persona segment at all.
		default_prompt = self._assembled_prompt_for(USER_B)
		self.assertNotIn("persona:", default_prompt)
		self.assertIn("; chat user:", default_prompt)  # assembly sanity

	def test_chat_ui_settings_exposes_persona(self):
		# get_chat_ui_settings feeds the SPA pill: it must reflect the server's
		# current persona (for the cross-device reconcile) and the enabled flag (N11).
		from jarvis.chat.api import get_chat_ui_settings

		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		ui = get_chat_ui_settings()
		self.assertEqual(ui["preferred_persona"], "Jara")
		self.assertTrue(ui["persona_enabled"])  # default on
		frappe.set_user("Administrator")

	def test_persona_kill_switch_stops_pill_and_clause(self):
		# Flipping Jarvis Settings.persona_enabled off must BOTH hide the pill and
		# silence the clause, so an already-opted-in user stops getting the token
		# (N7). Driven via set_single_value, NOT mock.patch (py3.14 import trap).
		from jarvis.chat.api import get_chat_ui_settings
		from jarvis.chat.turn_handler import _persona_clause

		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 0)
		try:
			self.assertEqual(_persona_clause(USER_A), "")
			frappe.set_user(USER_A)
			self.assertFalse(get_chat_ui_settings()["persona_enabled"])
			frappe.set_user("Administrator")
		finally:
			frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 1)
		# Re-enabled: the clause returns for the opted-in user.
		self.assertEqual(_persona_clause(USER_A), "; persona: Jara")

	def test_persona_enabled_defaults_on_without_singles_row(self):
		# The C1 regression: frappe.db.get_single_value coerces an unset Check to 0,
		# so an un-backfilled bench and a fresh install (which never runs patches)
		# both read the switch as OFF. With NO tabSingles row, NULL=ON must hold at
		# every reader: pill shown, clause active. Deleting the row is what the old
		# test never did - a migrate-written row made ON look real.
		from jarvis.chat.api import _persona_feature_enabled as boot_enabled
		from jarvis.chat.api import get_chat_ui_settings
		from jarvis.chat.turn_handler import _persona_clause, persona_feature_enabled

		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(preferred_persona="Jara")
		frappe.set_user("Administrator")
		frappe.db.sql(
			"delete from tabSingles where doctype=%s and field=%s",
			("Jarvis Settings", "persona_enabled"),
		)
		self.assertTrue(persona_feature_enabled())  # NULL=ON at the source probe
		self.assertTrue(boot_enabled())  # boot pill fails to ON, not OFF
		frappe.set_user(USER_A)
		self.assertTrue(get_chat_ui_settings()["persona_enabled"])
		frappe.set_user("Administrator")
		self.assertEqual(_persona_clause(USER_A), "; persona: Jara")  # clause active

	def test_persona_readers_fail_open_on_db_error(self):
		# A transient read failure must neither 500 the boot nor silently flip the
		# feature: the pill defaults ON, the clause stays silent. Swap frappe.db.sql
		# directly rather than mock.patch, which these persona tests avoid for the
		# py3.14 walk_packages import trap.
		from jarvis.chat.api import _persona_feature_enabled as boot_enabled
		from jarvis.chat.turn_handler import _persona_clause

		orig_sql = frappe.db.sql

		def boom(*a, **k):
			raise RuntimeError("db down")

		frappe.db.sql = boom
		try:
			self.assertTrue(boot_enabled())  # boot pill fails open ON
			self.assertEqual(_persona_clause(USER_A), "")  # clause fails silent
		finally:
			frappe.db.sql = orig_sql

	def test_current_user_persona_none_on_error_omits_boot_key(self):
		# I7: a failed persona read must return None so get_chat_ui_settings OMITS
		# preferred_persona - the SPA reconciles (and caches) only when the key is
		# present, so omitting it keeps the current pill instead of pinning it to a
		# wrong default. The old code returned the "Jarvis" sentinel, which the
		# persist:false reconcile wrote to localStorage.
		from jarvis.chat import api as chat_api

		orig_gv = frappe.db.get_value

		def boom(*a, **k):
			raise RuntimeError("boom")

		frappe.db.get_value = boom
		try:
			self.assertIsNone(chat_api._current_user_persona())
		finally:
			frappe.db.get_value = orig_gv

		# And the payload drops the key entirely (not a None value) when unreadable.
		frappe.set_user(USER_A)
		orig_helper = chat_api._current_user_persona
		chat_api._current_user_persona = lambda: None
		try:
			ui = chat_api.get_chat_ui_settings()
			self.assertNotIn("preferred_persona", ui)
			self.assertTrue(ui["persona_enabled"])  # switch still reported
		finally:
			chat_api._current_user_persona = orig_helper
			frappe.set_user("Administrator")

	def test_backfill_seeds_when_absent_and_never_clobbers_admin_zero(self):
		# The backfill protects the WRITE path: a full Jarvis Settings.save()
		# coerces an unset Check to 0 (get_valid_dict), which would flip the switch
		# OFF. Seeding a real 1 row prevents that - but ONLY when the row is absent,
		# so an admin's explicit 0 is never clobbered on a re-run (idempotent).
		from jarvis.patches.v2_09_backfill_persona_enabled import execute as backfill

		q = ("Jarvis Settings", "persona_enabled")
		sel = "select value from tabSingles where doctype=%s and field=%s"
		frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", q)
		backfill()
		self.assertEqual(int(frappe.db.sql(sel, q)[0][0]), 1)  # absent -> seeded 1
		frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 0)
		backfill()
		self.assertEqual(int(frappe.db.sql(sel, q)[0][0]), 0)  # explicit 0 preserved
		frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 1)

	def test_full_settings_save_keeps_persona_on_after_backfill(self):
		# End-to-end write-path regression: on an un-backfilled bench a full
		# Jarvis Settings save would coerce persona_enabled to 0; once the backfill
		# has seeded a real 1 row, the same save preserves ON.
		from jarvis.chat.turn_handler import persona_feature_enabled
		from jarvis.patches.v2_09_backfill_persona_enabled import execute as backfill

		frappe.db.sql(
			"delete from tabSingles where doctype=%s and field=%s",
			("Jarvis Settings", "persona_enabled"),
		)
		backfill()
		frappe.get_single("Jarvis Settings").save(ignore_permissions=True)
		self.assertTrue(persona_feature_enabled())
		frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 1)


# --------------------------------------------------------------------------- #
# 8.5 support_context_copy_pref: the "copy this chat into your ticket?"
#     don't-ask-again answer ("" | "Yes" | "No"). Direct analog of
#     preferred_persona above (same owner-scoped save/echo path, same
#     fixed-enum-message validation), but with no [Context:]/turn_handler
#     integration - it never reaches the LLM prompt at all.
# --------------------------------------------------------------------------- #
class TestSupportContextCopyPreference(_UsageTestBase):
	def test_default_is_blank(self):
		# No row at all: the lazy-created default is "" (ask every time), not
		# some other falsy value that would read as an explicit answer.
		frappe.set_user(USER_A)
		out = user_settings_api.get_my_settings()
		self.assertEqual(out["data"]["support_context_copy_pref"], "")
		frappe.set_user("Administrator")

	def test_update_to_yes_persists_and_echoes(self):
		frappe.set_user(USER_A)
		out = user_settings_api.update_my_settings(support_context_copy_pref="Yes")
		self.assertEqual(out["data"]["support_context_copy_pref"], "Yes")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "support_context_copy_pref"), "Yes")

	def test_update_to_no_persists_and_echoes(self):
		frappe.set_user(USER_A)
		out = user_settings_api.update_my_settings(support_context_copy_pref="No")
		self.assertEqual(out["data"]["support_context_copy_pref"], "No")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "support_context_copy_pref"), "No")

	def test_blank_explicitly_resets_to_ask_every_time(self):
		# Unlike preferred_persona (blank -> "Jarvis" default), blank here is
		# itself a valid, meaningful value - "go back to asking" - not merely
		# "no change". A user who answered "No" must be able to opt back in.
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(support_context_copy_pref="No")
		out = user_settings_api.update_my_settings(support_context_copy_pref="")
		self.assertEqual(out["data"]["support_context_copy_pref"], "")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "support_context_copy_pref"), "")

	def test_unknown_value_rejected(self):
		frappe.set_user(USER_A)
		with self.assertRaises(frappe.ValidationError):
			user_settings_api.update_my_settings(support_context_copy_pref="Maybe")
		frappe.set_user("Administrator")
		# Nothing persisted from the rejected write.
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_A}, "support_context_copy_pref") or "", "")

	def test_unknown_value_message_does_not_reflect_input(self):
		# Fixed-enum message, same self-XSS guard as preferred_persona's (F11):
		# a hostile value must never be reflected back.
		frappe.set_user(USER_A)
		with self.assertRaises(frappe.ValidationError) as cm:
			user_settings_api.update_my_settings(support_context_copy_pref="<img src=x onerror=alert(1)>")
		frappe.set_user("Administrator")
		msg = str(cm.exception)
		self.assertNotIn("<img", msg)
		self.assertNotIn("onerror", msg)
		self.assertIn("Yes, No, or blank", msg)

	def test_non_string_value_does_not_500(self):
		# Same arg-type-gate gap as preferred_persona (from __future__ import
		# annotations disables runtime coercion here - see the module NOTE): a
		# hostile non-string must reject cleanly, never 500 on AttributeError.
		frappe.set_user(USER_A)
		for bad in (["Yes"], {"pref": "Yes"}):
			with self.assertRaises(frappe.ValidationError):
				user_settings_api.update_my_settings(support_context_copy_pref=bad)
		# Falsy non-strings take the blank path (store "").
		for falsy in (0, False, []):
			user_settings_api.update_my_settings(support_context_copy_pref=falsy)
			self.assertEqual(user_settings_api.get_my_settings()["data"]["support_context_copy_pref"], "")
		frappe.set_user("Administrator")

	def test_update_touches_only_own_row(self):
		usage.get_or_create_user_settings(USER_B)
		frappe.db.commit()
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(support_context_copy_pref="Yes")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(USETT, {"user": USER_B}, "support_context_copy_pref") or "", "")

	def test_leaves_other_prefs_untouched(self):
		frappe.set_user(USER_A)
		user_settings_api.update_my_settings(notify_enabled=0)
		user_settings_api.update_my_settings(support_context_copy_pref="Yes")
		out = user_settings_api.get_my_settings()
		self.assertEqual(out["data"]["notify_enabled"], 0)
		self.assertEqual(out["data"]["support_context_copy_pref"], "Yes")
		frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# 9. set_sidebar_order — per-user nav order (UI state, permlevel 0). It takes a
#    JSON *string* from the client and must bound/clean it before it is stored,
#    since it is written straight to the owner's own row.
# --------------------------------------------------------------------------- #
class TestSidebarOrder(_UsageTestBase):
	def _stored(self, user: str) -> str:
		return frappe.db.get_value(USETT, {"user": user}, "sidebar_order") or ""

	def test_valid_order_persists_and_round_trips(self):
		import json

		frappe.set_user(USER_A)
		out = user_settings_api.set_sidebar_order(
			json.dumps({"top": ["Chat", "Dashboards"], "more": ["Skills"]})
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["sidebar_order"], {"top": ["Chat", "Dashboards"], "more": ["Skills"]})
		# get_my_settings hands the SPA the raw stored JSON string to reconcile.
		got = user_settings_api.get_my_settings()
		self.assertEqual(
			json.loads(got["data"]["sidebar_order"]), {"top": ["Chat", "Dashboards"], "more": ["Skills"]}
		)
		frappe.set_user("Administrator")

	def test_malformed_json_rejected_and_nothing_written(self):
		frappe.set_user(USER_A)
		out = user_settings_api.set_sidebar_order("{not json")
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_order")
		frappe.set_user("Administrator")
		# The early return must not have created a row carrying a bad value.
		self.assertEqual(self._stored(USER_A), "")

	def test_non_dict_json_rejected(self):
		import json

		frappe.set_user(USER_A)
		# Valid JSON but not an object (a bare list) is refused, not coerced.
		out = user_settings_api.set_sidebar_order(json.dumps(["Chat", "Dashboards"]))
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_order")
		frappe.set_user("Administrator")

	def test_non_string_arg_does_not_500(self):
		# A hostile non-string (a dict) reaches the handler raw (annotations are not
		# runtime-enforced here). json.loads on it raises, which the try/except must
		# turn into a clean invalid_order, never a 500.
		frappe.set_user(USER_A)
		out = user_settings_api.set_sidebar_order({"top": ["Chat"]})
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_order")
		frappe.set_user("Administrator")

	def test_labels_are_bounded_and_typed(self):
		import json

		frappe.set_user(USER_A)
		out = user_settings_api.set_sidebar_order(
			json.dumps(
				{
					"top": ["x" * 200] + [f"L{i}" for i in range(40)],  # long label + over the 20 cap
					"more": ["ok", 123, None, {"a": 1}, "also-ok"],  # non-strings dropped
				}
			)
		)
		self.assertTrue(out["ok"])
		top = out["data"]["sidebar_order"]["top"]
		more = out["data"]["sidebar_order"]["more"]
		self.assertEqual(len(top), 20)  # capped at 20 entries
		self.assertEqual(len(top[0]), 60)  # each label clipped to 60 chars
		self.assertEqual(more, ["ok", "also-ok"])  # only the strings survive
		frappe.set_user("Administrator")

	def test_missing_keys_default_to_empty_lists(self):
		import json

		frappe.set_user(USER_A)
		out = user_settings_api.set_sidebar_order(json.dumps({"top": ["Chat"]}))
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["sidebar_order"], {"top": ["Chat"], "more": []})
		frappe.set_user("Administrator")

	def test_writes_only_own_row(self):
		import json

		usage.get_or_create_user_settings(USER_B)
		frappe.db.commit()
		frappe.set_user(USER_A)
		user_settings_api.set_sidebar_order(json.dumps({"top": ["Chat"], "more": []}))
		frappe.set_user("Administrator")
		# B's row must be untouched by A's write.
		self.assertEqual(self._stored(USER_B), "")
		self.assertEqual(json.loads(self._stored(USER_A))["top"], ["Chat"])
