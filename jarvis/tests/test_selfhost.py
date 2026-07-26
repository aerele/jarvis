"""Unit tests for jarvis.selfhost.validate_connection (the self-hosted
openclaw pre-connect checks). HTTP is mocked - no real openclaw needed.

Run: bench --site <site> run-tests --module jarvis.tests.test_selfhost
"""

import unittest
from unittest import mock

import frappe

from jarvis import selfhost


def _resp(status, json_data=None, text=""):
	m = mock.Mock()
	m.status_code = status
	m.json.return_value = json_data if json_data is not None else {}
	m.text = text
	return m


class TestValidateConnection(unittest.TestCase):
	def _checks(self, result):
		return {c["check"]: c["ok"] for c in result["checks"]}

	def test_bad_url_shape_fails_fast(self):
		r = selfhost.validate_connection("not-a-url", "tok")
		self.assertFalse(r["ok"])
		self.assertFalse(self._checks(r)["url_shape"])

	def test_ws_url_is_normalized_to_http(self):
		# ws:// should be accepted (converted to http for the HTTP checks).
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = [
				_resp(200),  # /healthz
				_resp(200, {"data": [{"id": "openclaw"}]}),  # /v1/models
			]
			r = selfhost.validate_connection("ws://host:19060", "tok")
		self.assertTrue(r["ok"])
		self.assertTrue(self._checks(r)["url_shape"])

	def test_unreachable_fails(self):
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = Exception("connection refused")
			r = selfhost.validate_connection("http://host:19099", "tok")
		self.assertFalse(r["ok"])
		self.assertFalse(self._checks(r)["reachable"])

	def test_bad_token_fails_auth(self):
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = [_resp(200), _resp(401)]  # healthz ok, models 401
			r = selfhost.validate_connection("http://host:19060", "wrong")
		self.assertFalse(r["ok"])
		checks = self._checks(r)
		self.assertTrue(checks["reachable"])
		self.assertFalse(checks["auth"])

	def test_no_models_fails_llm_ready(self):
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = [_resp(200), _resp(200, {"data": []})]
			r = selfhost.validate_connection("http://host:19060", "tok")
		self.assertFalse(r["ok"])
		checks = self._checks(r)
		self.assertTrue(checks["auth"])
		self.assertFalse(checks["llm_ready"])

	def test_happy_path_all_green(self):
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = [
				_resp(200),
				_resp(200, {"data": [{"id": "openclaw"}, {"id": "openclaw/main"}]}),
			]
			r = selfhost.validate_connection("http://host:19060", "tok")
		self.assertTrue(r["ok"])
		self.assertEqual(
			self._checks(r), {"url_shape": True, "reachable": True, "auth": True, "llm_ready": True}
		)
		self.assertEqual(r["models"], ["openclaw", "openclaw/main"])

	def test_deep_chat_ping(self):
		with mock.patch("jarvis.selfhost.requests") as rq:
			rq.RequestException = Exception
			rq.get.side_effect = [_resp(200), _resp(200, {"data": [{"id": "openclaw"}]})]
			rq.post.return_value = _resp(200, {"choices": [{"message": {"content": "pong"}}]})
			r = selfhost.validate_connection("http://host:19060", "tok", deep=True)
		self.assertTrue(r["ok"])
		self.assertTrue(self._checks(r)["deep_chat"])


class TestActiveTurnMarker(unittest.TestCase):
	"""Concurrency safety of the self-host tool-card attribution marker.

	Uses the real frappe cache (Redis). Two turns for one tool user must never
	let a tool callback be attributed to the wrong conversation.
	"""

	TOOL_USER = "selfhost-marker-test@example.com"

	def setUp(self):
		selfhost.clear_active_turn(self.TOOL_USER)

	def tearDown(self):
		selfhost.clear_active_turn(self.TOOL_USER)

	def test_single_turn_attributes(self):
		selfhost.set_active_turn(self.TOOL_USER, conversation="C1", owner="u1@x", run_id="r1")
		turn = selfhost.get_active_turn(self.TOOL_USER)
		self.assertIsNotNone(turn)
		self.assertEqual(turn["conversation"], "C1")
		self.assertEqual(turn["run_id"], "r1")

	def test_concurrent_turns_are_ambiguous_and_return_none(self):
		# Two overlapping turns -> no unambiguous turn -> a tool callback is
		# NOT mis-attributed (fail safe, no cross-conversation leak).
		selfhost.set_active_turn(self.TOOL_USER, conversation="C1", owner="u1@x", run_id="r1")
		selfhost.set_active_turn(self.TOOL_USER, conversation="C2", owner="u2@x", run_id="r2")
		self.assertIsNone(selfhost.get_active_turn(self.TOOL_USER))

	def test_clearing_one_leaves_the_surviving_turn_not_a_stale_slot(self):
		selfhost.set_active_turn(self.TOOL_USER, conversation="C1", owner="u1@x", run_id="r1")
		selfhost.set_active_turn(self.TOOL_USER, conversation="C2", owner="u2@x", run_id="r2")
		# r2 (last writer) finishes first; the survivor r1 must be returned -
		# a single last-writer-wins slot would have wrongly returned C2/None.
		selfhost.clear_active_turn(self.TOOL_USER, "r2")
		turn = selfhost.get_active_turn(self.TOOL_USER)
		self.assertIsNotNone(turn)
		self.assertEqual(turn["conversation"], "C1")

	def test_clear_by_run_id_does_not_wipe_concurrent_turn(self):
		selfhost.set_active_turn(self.TOOL_USER, conversation="C1", owner="u1@x", run_id="r1")
		selfhost.set_active_turn(self.TOOL_USER, conversation="C2", owner="u2@x", run_id="r2")
		selfhost.clear_active_turn(self.TOOL_USER, "r1")
		turn = selfhost.get_active_turn(self.TOOL_USER)
		self.assertIsNotNone(turn)
		self.assertEqual(turn["conversation"], "C2")

	def test_stale_run_id_pruned_when_record_expired(self):
		# Simulate a worker killed before clear ran: run id is still in the set
		# but its per-run record (TTL) is gone. It must be ignored + pruned.
		selfhost.set_active_turn(self.TOOL_USER, conversation="C1", owner="u1@x", run_id="r1")
		frappe.cache().delete_value(selfhost._turn_key("r1"))
		self.assertIsNone(selfhost.get_active_turn(self.TOOL_USER))
		# A fresh turn is then unambiguous (the stale id was pruned).
		selfhost.set_active_turn(self.TOOL_USER, conversation="C2", owner="u2@x", run_id="r2")
		turn = selfhost.get_active_turn(self.TOOL_USER)
		self.assertIsNotNone(turn)
		self.assertEqual(turn["conversation"], "C2")

	def test_empty_tool_user_is_noop(self):
		selfhost.set_active_turn("", conversation="C1", owner="u1@x", run_id="r1")
		self.assertIsNone(selfhost.get_active_turn(""))


class _FakeSettings:
	def __init__(self, **kw):
		self.__dict__.update(kw)

	def db_set(self, key, val):
		setattr(self, key, val)


class TestSaveSelfHostedToolUser(unittest.TestCase):
	"""save_self_hosted tool-user defaulting + the unset warning (#2).

	Fully mocked - no Jarvis Settings writes - so it never flips the live
	bench into Self-Hosted mode.
	"""

	def _save(self, *, session_user, existing_tool_user="", tool_user="", enabled=True):
		fake = _FakeSettings(selfhost_tool_user=existing_tool_user)
		validation = {"ok": True, "checks": [], "openclaw_version": None, "models": ["openclaw"]}
		# set_settings_password is patched to a plain setattr: the real one
		# writes __Auth via frappe.utils.password (a DB write this fully-mocked
		# suite must never make).
		with (
			mock.patch("jarvis.selfhost.validate_connection", return_value=validation),
			mock.patch(
				"jarvis.selfhost.set_settings_password",
				side_effect=lambda doc, field, value, **kw: setattr(doc, field, value),
			),
			mock.patch("jarvis.selfhost.frappe") as fr,
		):
			fr.get_single.return_value = fake
			fr.session.user = session_user
			# User.enabled lookup: 1 = exists+enabled, None = missing/disabled.
			fr.db.get_value.return_value = 1 if enabled else None
			out = selfhost.save_self_hosted("http://host:19060", "tok", tool_user=tool_user)
		return out, fake

	def test_defaults_tool_user_to_configurer(self):
		out, fake = self._save(session_user="alice@example.com")
		self.assertTrue(out["ok"])
		self.assertEqual(fake.selfhost_tool_user, "alice@example.com")
		self.assertNotIn("warning", out)

	def test_administrator_configurer_leaves_unset_and_warns(self):
		out, fake = self._save(session_user="Administrator")
		self.assertTrue(out["ok"])
		self.assertEqual((fake.selfhost_tool_user or ""), "")
		self.assertIn("warning", out)
		self.assertIn("Self-Host Tool User", out["warning"])

	def test_explicit_tool_user_used(self):
		out, fake = self._save(session_user="Administrator", tool_user="bob@example.com")
		self.assertTrue(out["ok"])
		self.assertEqual(fake.selfhost_tool_user, "bob@example.com")
		self.assertNotIn("warning", out)

	def test_explicit_admin_tool_user_rejected(self):
		out, _ = self._save(session_user="alice@example.com", tool_user="Administrator")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"], "invalid_tool_user")

	def test_explicit_unknown_or_disabled_user_rejected(self):
		out, _ = self._save(session_user="alice@example.com", tool_user="ghost@example.com", enabled=False)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"], "invalid_tool_user")

	def test_existing_tool_user_preserved(self):
		out, fake = self._save(session_user="Administrator", existing_tool_user="carol@example.com")
		self.assertTrue(out["ok"])
		self.assertEqual(fake.selfhost_tool_user, "carol@example.com")
		self.assertNotIn("warning", out)

	def test_blank_token_clears_stored_secret_via_module_seam(self):
		"""A blank token (no-auth openclaw that validated clean) must route
		through the MODULE-LEVEL clear_settings_password seam - never
		set_settings_password - so this fully-mocked suite can intercept it
		and a real __Auth delete is never performed against the DB."""
		fake = _FakeSettings(selfhost_tool_user="carol@example.com")
		validation = {"ok": True, "checks": [], "openclaw_version": None, "models": ["openclaw"]}
		with (
			mock.patch("jarvis.selfhost.validate_connection", return_value=validation),
			mock.patch("jarvis.selfhost.set_settings_password") as set_mock,
			mock.patch("jarvis.selfhost.clear_settings_password") as clear_mock,
			mock.patch("jarvis.selfhost.frappe") as fr,
		):
			fr.get_single.return_value = fake
			fr.session.user = "alice@example.com"
			fr.db.get_value.return_value = 1
			out = selfhost.save_self_hosted("http://host:19060", "")
		self.assertTrue(out["ok"])
		clear_mock.assert_called_once_with(fake, "agent_token")
		set_mock.assert_not_called()


class TestSelfhostToolUserResolver(unittest.TestCase):
	"""Use-time guard: _selfhost_tool_user refuses Administrator/Guest/disabled,
	so a direct Jarvis Settings edit (bypassing save_self_hosted) can't escalate
	tool execution to a privilege-bypassing or disabled account."""

	def _resolve(self, configured, *, enabled=1):
		from jarvis import api

		fake = _FakeSettings(selfhost_tool_user=configured)
		with (
			mock.patch("jarvis.selfhost.is_self_hosted", return_value=True),
			mock.patch("jarvis.api.frappe") as fr,
		):
			fr.get_single.return_value = fake
			fr.db.get_value.return_value = enabled
			return api._selfhost_tool_user()

	def test_normal_enabled_user_allowed(self):
		self.assertEqual(self._resolve("alice@example.com"), "alice@example.com")

	def test_administrator_refused(self):
		self.assertIsNone(self._resolve("Administrator"))

	def test_guest_refused(self):
		self.assertIsNone(self._resolve("Guest"))

	def test_disabled_user_refused(self):
		self.assertIsNone(self._resolve("bob@example.com", enabled=0))

	def test_missing_user_refused(self):
		self.assertIsNone(self._resolve("ghost@example.com", enabled=None))

	def test_unset_returns_none(self):
		self.assertIsNone(self._resolve(""))


class TestConfigChecks(unittest.TestCase):
	"""Deterministic, no-network config checks surfaced in 'Test connection':
	gateway_token present + tool_user set/non-admin/enabled. Reuses the same
	tool-user invariant as api._selfhost_tool_user."""

	def _run(self, token, tool_user="", stored="", enabled=1):
		fake = _FakeSettings(selfhost_tool_user=stored)
		with mock.patch("jarvis.selfhost.frappe") as fr:
			fr.get_single.return_value = fake
			fr.db.get_value.return_value = enabled
			checks = selfhost.config_checks(token, tool_user)
		return {c["check"]: c["ok"] for c in checks}

	def test_token_present_and_valid_tool_user(self):
		r = self._run("tok", tool_user="alice@example.com")
		self.assertTrue(r["gateway_token"])
		self.assertTrue(r["tool_user"])

	def test_blank_token_fails(self):
		self.assertFalse(self._run("   ", tool_user="alice@example.com")["gateway_token"])

	def test_unset_tool_user_fails(self):
		self.assertFalse(self._run("tok")["tool_user"])

	def test_administrator_tool_user_fails(self):
		self.assertFalse(self._run("tok", tool_user="Administrator")["tool_user"])

	def test_guest_tool_user_fails(self):
		self.assertFalse(self._run("tok", tool_user="Guest")["tool_user"])

	def test_disabled_tool_user_fails(self):
		self.assertFalse(self._run("tok", tool_user="bob@example.com", enabled=None)["tool_user"])

	def test_stored_tool_user_used_when_arg_blank(self):
		self.assertTrue(self._run("tok", stored="carol@example.com")["tool_user"])


class TestUnsignedPluginAdvisory(unittest.TestCase):
	"""JF-014 review UX P1-1: an operator who turned the unsigned-plugin
	escape hatch on must see it (and how much it is being used) in the
	product's own "Test connection" checks, not only in the bench log."""

	CHECK = "plugin_signature_enforced"

	def _row(self, status):
		with mock.patch("jarvis._plugin_auth.unsigned_escape_hatch_status", return_value=status):
			return selfhost._unsigned_plugin_check()

	def _rows_via_config_checks(self, status):
		fake = _FakeSettings(selfhost_tool_user="alice@example.com")
		with (
			mock.patch("jarvis._plugin_auth.unsigned_escape_hatch_status", return_value=status),
			mock.patch("jarvis.selfhost.frappe") as fr,
		):
			fr.get_single.return_value = fake
			fr.db.get_value.return_value = 1
			return selfhost.config_checks("tok", "alice@example.com")

	def test_no_row_while_enforcing(self):
		"""The default bench must not grow a permanent scary row."""
		self.assertIsNone(self._row(None))
		checks = self._rows_via_config_checks(None)
		self.assertNotIn(self.CHECK, [c["check"] for c in checks])

	def test_row_reports_recent_count_and_sunset(self):
		row = self._row(
			{
				"conf_key": "jarvis_plugin_allow_unsigned",
				"sunset_date": "2026-10-01",
				"window_days": 7,
				"recent_allowed": 12,
			}
		)
		self.assertEqual(row["check"], self.CHECK)
		self.assertFalse(row["ok"])
		self.assertIn("jarvis_plugin_allow_unsigned is ON", row["detail"])
		self.assertIn("12 unsigned plugin call(s) allowed in the last 7 days", row["detail"])
		self.assertIn("2026-10-01", row["detail"])

	def test_zero_recent_calls_says_safe_to_turn_off(self):
		row = self._row(
			{
				"conf_key": "jarvis_plugin_allow_unsigned",
				"sunset_date": "2026-10-01",
				"window_days": 7,
				"recent_allowed": 0,
			}
		)
		self.assertFalse(row["ok"])
		self.assertIn("safe to turn off", row["detail"])

	def test_unknown_count_is_not_reported_as_zero(self):
		row = self._row(
			{
				"conf_key": "jarvis_plugin_allow_unsigned",
				"sunset_date": "2026-10-01",
				"window_days": 7,
				"recent_allowed": None,
			}
		)
		self.assertIn("call count unavailable", row["detail"])
		self.assertNotIn("safe to turn off", row["detail"])

	def test_row_is_advisory_in_test_connection(self):
		"""It must never flip the connection verdict - it is a warning about
		a deprecation, not a broken openclaw."""
		checks = self._rows_via_config_checks(
			{
				"conf_key": "jarvis_plugin_allow_unsigned",
				"sunset_date": "2026-10-01",
				"window_days": 7,
				"recent_allowed": 3,
			}
		)
		names = [c["check"] for c in checks]
		self.assertIn(self.CHECK, names)
		# config_checks rows are tagged advisory by test_connection, which is
		# what keeps result["ok"] about the openclaw connection alone.
		self.assertEqual([c["check"] for c in checks if not c["ok"]], [self.CHECK])

	def test_status_lookup_failure_never_breaks_test_connection(self):
		with mock.patch("jarvis._plugin_auth.unsigned_escape_hatch_status", side_effect=RuntimeError("boom")):
			self.assertIsNone(selfhost._unsigned_plugin_check())


class TestProbeToolCallback(unittest.TestCase):
	"""Opt-in end-to-end probe: induce a jarvis__* tool call over HTTP and
	confirm the openclaw->Frappe callback landed (counter advanced)."""

	def test_skipped_when_not_self_hosted(self):
		with mock.patch("jarvis.selfhost.is_self_hosted", return_value=False):
			row = selfhost.probe_tool_callback("http://h:19060", "tok")
		self.assertFalse(row["ok"])
		self.assertIn("save", row["detail"].lower())

	def _probe(self, before, after, post=None):
		with (
			mock.patch("jarvis.selfhost.is_self_hosted", return_value=True),
			mock.patch("jarvis.selfhost.requests") as rq,
			mock.patch("jarvis.selfhost.frappe") as fr,
		):
			rq.RequestException = Exception
			if post is None:
				rq.post.return_value = _resp(200, {"choices": [{"message": {"content": "DONE"}}]})
			else:
				rq.post.side_effect = post
			fr.cache.return_value.get_value.side_effect = [before, after]
			return selfhost.probe_tool_callback("http://h:19060", "tok")

	def test_confirmed_when_counter_advances(self):
		self.assertTrue(self._probe(3, 4)["ok"])

	def test_none_to_set_counts_as_advanced(self):
		self.assertTrue(self._probe(None, 1)["ok"])

	def test_not_ok_when_counter_unchanged(self):
		self.assertFalse(self._probe(3, 3)["ok"])

	def test_post_failure_reports_not_ok(self):
		row = self._probe(None, None, post=Exception("connection refused"))
		self.assertFalse(row["ok"])


class TestNoteCallbackSeen(unittest.TestCase):
	"""The self-host callback marker written by api.call_tool (self-host branch
	only). Increments a cache counter; must never raise on a cache blip."""

	def test_writes_incremented_counter(self):
		with mock.patch("jarvis.selfhost.frappe") as fr:
			fr.cache.return_value.get_value.return_value = 4
			selfhost.note_callback_seen()
			fr.cache.return_value.set_value.assert_called_once()
			args, _ = fr.cache.return_value.set_value.call_args
			self.assertEqual(args[0], selfhost._CALLTOOL_SEEN_KEY)
			self.assertEqual(args[1], 5)

	def test_first_call_from_empty(self):
		with mock.patch("jarvis.selfhost.frappe") as fr:
			fr.cache.return_value.get_value.return_value = None
			selfhost.note_callback_seen()
			args, _ = fr.cache.return_value.set_value.call_args
			self.assertEqual(args[1], 1)

	def test_suppresses_cache_errors(self):
		with mock.patch("jarvis.selfhost.frappe") as fr:
			fr.cache.return_value.set_value.side_effect = Exception("redis down")
			selfhost.note_callback_seen()  # must not raise


if __name__ == "__main__":
	unittest.main()
