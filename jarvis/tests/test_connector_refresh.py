"""Hermetic unit tests for ``jarvis.connectors.refresh``: staleness math, the
debounce + dedupe of the enqueue, the exact set of trigger codes, the background
job's never-flip-Failed contract, and ``reprobe_all``'s per-row queueing + credential
context. ``frappe`` is faked at the module boundary; no bench, no socket, no redis.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from jarvis.connectors import refresh


class _Row(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc


class _DoesNotExist(Exception):
	"""A real class so ``except frappe.DoesNotExistError`` works under a fake frappe."""


def _fake_frappe(**overrides):
	fake = mock.MagicMock()
	fake.DoesNotExistError = _DoesNotExist
	for key, value in overrides.items():
		setattr(fake, key, value)
	return fake


# --------------------------------------------------------------------------- #
# staleness math
# --------------------------------------------------------------------------- #
class TestStaleness(unittest.TestCase):
	def test_ttl_seconds_uses_server_hint_when_positive(self):
		self.assertEqual(refresh.ttl_seconds(60000), 60.0)

	def test_ttl_seconds_falls_to_default_when_absent_zero_or_negative(self):
		for bad in (None, 0, -1, "", "nope"):
			self.assertEqual(refresh.ttl_seconds(bad), refresh.DEFAULT_TTL_S)

	def test_never_stale_without_a_cache(self):
		now = datetime(2026, 9, 16, 12, 0, 0)
		# No cached_at, or cached_at but no cache: the first fill is the Test button's.
		self.assertFalse(refresh.is_stale(_Row(tools_cache='{"tools": []}'), now))
		self.assertFalse(refresh.is_stale(_Row(tools_cached_at=now), now))

	def test_fresh_within_ttl_is_not_stale(self):
		now = datetime(2026, 9, 16, 12, 0, 0)
		row = _Row(
			tools_cache='{"tools": []}', tools_cached_at=now - timedelta(seconds=30), tools_ttl_ms=60000
		)
		self.assertFalse(refresh.is_stale(row, now))

	def test_past_ttl_is_stale(self):
		now = datetime(2026, 9, 16, 12, 0, 0)
		row = _Row(
			tools_cache='{"tools": []}', tools_cached_at=now - timedelta(seconds=90), tools_ttl_ms=60000
		)
		self.assertTrue(refresh.is_stale(row, now))

	def test_no_ttl_hint_uses_the_daily_default(self):
		now = datetime(2026, 9, 16, 12, 0, 0)
		# 2h old with no hint: not yet stale under the 24h default.
		fresh = _Row(tools_cache='{"tools": []}', tools_cached_at=now - timedelta(hours=2))
		self.assertFalse(refresh.is_stale(fresh, now))
		stale = _Row(tools_cache='{"tools": []}', tools_cached_at=now - timedelta(hours=25))
		self.assertTrue(refresh.is_stale(stale, now))


# --------------------------------------------------------------------------- #
# after_call trigger codes
# --------------------------------------------------------------------------- #
class TestAfterCallTriggers(unittest.TestCase):
	def _row(self):
		return _Row(name="conn-1", tools_cache='{"tools": []}', tools_cached_at=datetime(2026, 9, 16, 12, 0))

	def test_only_drift_codes_trigger_a_refresh(self):
		row = self._row()
		with mock.patch.object(refresh, "is_stale", return_value=False):
			for code in ("action_unknown", "rpc_error", "protocol_error"):
				with mock.patch.object(refresh, "request") as req:
					refresh.after_call(row, code)
				req.assert_called_once_with("conn-1")

	def test_non_drift_codes_do_not_trigger_when_fresh(self):
		row = self._row()
		with mock.patch.object(refresh, "is_stale", return_value=False):
			# http_error (would loop on 401/403) and invalid_arguments (our own local
			# check) are deliberately excluded, as are a clean success and a tool_error.
			for code in ("http_error", "invalid_arguments", "tool_error", "transport_error", ""):
				with mock.patch.object(refresh, "request") as req:
					refresh.after_call(row, code)
				req.assert_not_called()

	def test_staleness_triggers_regardless_of_code(self):
		row = self._row()
		with mock.patch.object(refresh, "is_stale", return_value=True):
			with mock.patch.object(refresh, "request") as req:
				refresh.after_call(row, "")  # a clean success, but the cache is stale
			req.assert_called_once_with("conn-1")

	def test_after_call_never_raises_even_when_logging_fails(self):
		row = self._row()
		fake = _fake_frappe()
		fake.logger.side_effect = RuntimeError("no log dir")
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch.object(refresh, "is_stale", side_effect=RuntimeError("boom")),
		):
			# is_stale blows up AND the fallback logger blows up: still no exception.
			refresh.after_call(row, "action_unknown")


# --------------------------------------------------------------------------- #
# request: debounce (NX) + dedupe (job_id)
# --------------------------------------------------------------------------- #
class TestRequestDebounce(unittest.TestCase):
	def test_first_caller_enqueues_with_dedupe_and_after_commit(self):
		fake = _fake_frappe()
		fake.cache.return_value.set.return_value = True  # NX claim won
		fake.cache.return_value.make_key.side_effect = lambda k: f"site|{k}"
		with mock.patch.object(refresh, "frappe", fake):
			self.assertTrue(refresh.request("conn-1"))
		fake.enqueue.assert_called_once()
		_, kwargs = fake.enqueue.call_args
		self.assertEqual(fake.enqueue.call_args[0][0], refresh.JOB_METHOD)
		self.assertEqual(kwargs["queue"], "short")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], refresh._job_id("conn-1"))
		self.assertEqual(kwargs["name"], "conn-1")

	def test_second_caller_in_window_does_not_enqueue(self):
		fake = _fake_frappe()
		fake.cache.return_value.set.return_value = None  # NX claim lost (key present)
		fake.cache.return_value.make_key.side_effect = lambda k: f"site|{k}"
		with mock.patch.object(refresh, "frappe", fake):
			self.assertFalse(refresh.request("conn-1"))
		fake.enqueue.assert_not_called()

	def test_debounce_key_uses_set_nx_with_the_window(self):
		fake = _fake_frappe()
		fake.cache.return_value.set.return_value = True
		fake.cache.return_value.make_key.side_effect = lambda k: k
		with mock.patch.object(refresh, "frappe", fake):
			refresh.request("conn-1")
		_, set_kwargs = fake.cache.return_value.set.call_args
		self.assertTrue(set_kwargs["nx"])
		self.assertEqual(set_kwargs["ex"], refresh.DEBOUNCE_S)

	def test_job_id_is_colon_free(self):
		self.assertNotIn(":", refresh._job_id("conn-1"))


# --------------------------------------------------------------------------- #
# refresh_tools_cache: background persist
# --------------------------------------------------------------------------- #
class TestRefreshToolsCache(unittest.TestCase):
	def test_missing_row_is_a_no_op(self):
		fake = _fake_frappe()
		fake.get_doc.side_effect = _DoesNotExist("gone")
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch("jarvis.connectors.broker.test_connector") as probe,
			mock.patch("jarvis.chat.connectors_api.persist_probe_result") as persist,
		):
			refresh.refresh_tools_cache("conn-1")
		probe.assert_not_called()
		persist.assert_not_called()

	def test_disabled_row_is_a_no_op(self):
		fake = _fake_frappe()
		fake.get_doc.return_value = _Row(name="conn-1", enabled=0)
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch("jarvis.connectors.broker.test_connector") as probe,
			mock.patch("jarvis.chat.connectors_api.persist_probe_result") as persist,
		):
			refresh.refresh_tools_cache("conn-1")
		probe.assert_not_called()
		persist.assert_not_called()

	def test_enabled_row_probes_and_persists_in_background_mode(self):
		fake = _fake_frappe()
		doc = _Row(name="conn-1", enabled=1)
		fake.get_doc.return_value = doc
		probe_result = {"ok": True, "tools": []}
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch.object(refresh, "now_datetime", return_value=datetime(2026, 9, 16, 12, 0)),
			mock.patch("jarvis.connectors.broker.test_connector", return_value=probe_result) as probe,
			mock.patch("jarvis.chat.connectors_api.persist_probe_result") as persist,
		):
			refresh.refresh_tools_cache("conn-1")
		probe.assert_called_once_with(doc)
		_, kwargs = persist.call_args
		self.assertTrue(kwargs["can_write"])
		self.assertTrue(kwargs["background"])
		self.assertIs(persist.call_args[0][0], doc)
		self.assertIs(persist.call_args[0][1], probe_result)


# --------------------------------------------------------------------------- #
# reprobe_all: per-row credential context
# --------------------------------------------------------------------------- #
class TestReprobeUserSelection(unittest.TestCase):
	def test_token_and_open_rows_run_as_administrator(self):
		self.assertEqual(refresh._reprobe_user(_Row(name="a", auth_method="API Key"), {}), "Administrator")
		self.assertEqual(refresh._reprobe_user(_Row(name="a", auth_method=""), {}), "Administrator")

	def test_personal_oauth_runs_as_the_owner(self):
		row = _Row(name="a", auth_method="OAuth", scope="Personal", owner="u@example.com")
		self.assertEqual(refresh._reprobe_user(row, {}), "u@example.com")

	def test_shared_oauth_runs_as_the_latest_token_holder(self):
		row = _Row(name="shared-1", auth_method="OAuth", scope="Shared")
		self.assertEqual(refresh._reprobe_user(row, {"shared-1": "holder@example.com"}), "holder@example.com")

	def test_shared_oauth_with_no_signin_is_skipped(self):
		row = _Row(name="shared-1", auth_method="OAuth", scope="Shared")
		self.assertIsNone(refresh._reprobe_user(row, {}))

	def test_shared_owners_takes_first_seen_over_modified_desc(self):
		fake = _fake_frappe()
		# order_by modified desc, so the FIRST row per connector is the most recent.
		fake.get_all.return_value = [
			{"connector": "s1", "user": "recent@example.com"},
			{"connector": "s1", "user": "older@example.com"},
			{"connector": "s2", "user": "only@example.com"},
		]
		with mock.patch.object(refresh, "frappe", fake):
			owners = refresh._shared_oauth_owners(["s1", "s2"])
		self.assertEqual(owners, {"s1": "recent@example.com", "s2": "only@example.com"})

	def test_shared_owners_empty_input_makes_no_query(self):
		fake = _fake_frappe()
		with mock.patch.object(refresh, "frappe", fake):
			self.assertEqual(refresh._shared_oauth_owners([]), {})
		fake.get_all.assert_not_called()


class TestReprobeAll(unittest.TestCase):
	def _fake(self, rows, tokens=None):
		fake = _fake_frappe()
		fake.session.user = "Administrator"
		# reprobe_all queries connectors first, then MCP OAuth Token for shared-oauth rows.
		fake.get_all.side_effect = [rows, tokens or []]
		return fake

	def test_queues_each_row_under_its_user_and_isolates_failures(self):
		rows = [
			{"name": "tok", "scope": "Personal", "auth_method": "API Key", "owner": "x"},
			{"name": "pers", "scope": "Personal", "auth_method": "OAuth", "owner": "owner@example.com"},
			{"name": "shar", "scope": "Shared", "auth_method": "OAuth", "owner": "creator"},
		]
		tokens = [{"connector": "shar", "user": "holder@example.com"}]
		fake = self._fake(rows, tokens)

		# Queueing the Personal OAuth row raises; the sweep must continue.
		def _request(name):
			if name == "pers":
				raise RuntimeError("redis blew up")
			return True

		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch.object(refresh, "request", side_effect=_request) as queued,
			mock.patch.object(refresh, "refresh_tools_cache") as probe,
		):
			refresh.reprobe_all()
		# Every row queued despite the middle failure; nothing probed inline.
		self.assertEqual([c.args[0] for c in queued.call_args_list], ["tok", "pers", "shar"])
		probe.assert_not_called()
		# set_user drove each row's context and always restored Administrator.
		users = [c.args[0] for c in fake.set_user.call_args_list]
		self.assertEqual(
			users,
			[
				"Administrator",
				"Administrator",
				"owner@example.com",
				"Administrator",
				"holder@example.com",
				"Administrator",
			],
		)

	def test_shared_oauth_without_signin_is_skipped_entirely(self):
		rows = [{"name": "shar", "scope": "Shared", "auth_method": "OAuth", "owner": "creator"}]
		fake = self._fake(rows, tokens=[])  # no token for shar
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch.object(refresh, "request") as queued,
		):
			refresh.reprobe_all()
		queued.assert_not_called()
		fake.set_user.assert_not_called()

	def test_no_rows_is_a_no_op(self):
		fake = _fake_frappe()
		fake.session.user = "Administrator"
		fake.get_all.return_value = []
		with (
			mock.patch.object(refresh, "frappe", fake),
			mock.patch.object(refresh, "request") as queued,
		):
			refresh.reprobe_all()
		queued.assert_not_called()


if __name__ == "__main__":
	unittest.main()
