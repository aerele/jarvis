"""Holiday touches: the operator policy mirror.

The payload contract is ``holiday_policy_supported`` plus ``holiday_policy: {disabled,
disabled_designs}``. Gate and daily-sync wiring run through the real ``account`` and
``onboarding`` functions with ``admin_client.get_connection`` mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client, onboarding
from jarvis.holiday_touch import cache, force, policy, resolve
from jarvis.tests.test_holiday_touch_resolve import SETTINGS, _set, _user, clear_log_markers
from jarvis.tests.test_onboarding_sync import _restore_settings, _set_token, _snapshot_settings

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
MIRROR = ("holiday_policy_disabled", "holiday_policy_disabled_designs", "holiday_policy_synced_at")
_MISSING = object()


def _payload(policy_dict=_MISSING, marker=True, **extra) -> dict:
	body = {"holiday_policy_supported": True} if marker else {}
	if policy_dict is not _MISSING:
		body["holiday_policy"] = policy_dict
	body.update(extra)
	return body


def _raw(field):
	frappe.db.value_cache.pop(SETTINGS, None)
	return frappe.db.get_singles_dict(SETTINGS).get(field)


def _designs():
	return json.loads(_raw("holiday_policy_disabled_designs") or "[]")


def _is_holiday_write(args, kwargs) -> bool:
	data = args[1] if len(args) > 1 else kwargs.get("dt_or_field")
	fields = data if isinstance(data, dict) else {args[1]: None} if len(args) > 1 else {}
	return any(str(f).startswith("holiday_policy") for f in fields)


class _Base(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._conf = {k: frappe.conf.get(k, _MISSING) for k in ("developer_mode", force.CONFIG_KEY)}
		cache.rotate()
		clear_log_markers()
		for field in ("holiday_touches_enabled", "birthday_greetings_enabled"):
			_set(field, 1)
		for field in (*MIRROR, "brand_logo"):
			_set(field, None)
		_set("agent_name", "Jarvis")

	def tearDown(self):
		for key, value in self._conf.items():
			if value is _MISSING:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		frappe.set_user("Administrator")
		frappe.db.rollback()  # first: a commit below must not keep this test's users
		for field in MIRROR:
			_set(field, None)
		frappe.db.commit()
		frappe.db.value_cache.pop(SETTINGS, None)
		clear_log_markers()

	def holiday_writes(self):
		"""Patch ``set_single_value`` and count the calls that touch a mirror field."""
		real = frappe.db.set_single_value
		calls = []

		def spy(*args, **kwargs):
			if _is_holiday_write(args, kwargs):
				calls.append(args[1:])
			return real(*args, **kwargs)

		return calls, mock.patch.object(frappe.db, "set_single_value", side_effect=spy)


class TestPersist(_Base):
	def test_a_never_synced_site_is_on(self):
		self.assertIsNone(_raw("holiday_policy_synced_at"))
		user = _user()
		frappe.conf.developer_mode = 1
		frappe.conf[force.CONFIG_KEY] = "diwali:day"
		self.assertEqual(resolve.answer(user, NOW)["kind"], "day")

	def test_absent_marker_keeps_the_stored_policy(self):
		_set("holiday_policy_disabled", 1)
		_set("holiday_policy_disabled_designs", '["eid"]')
		calls, spy = self.holiday_writes()
		with spy:
			policy.persist({}, daily=True)
			policy.persist(_payload({"disabled": False, "disabled_designs": []}, marker=False), daily=True)
			policy.persist(_payload({"disabled": False}, holiday_policy_supported=False), daily=True)
		self.assertEqual(calls, [])
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(_designs(), ["eid"])
		self.assertIsNone(_raw("holiday_policy_synced_at"))

	def test_missing_policy_or_subkey_keeps_the_stored_value(self):
		_set("holiday_policy_disabled", 1)
		_set("holiday_policy_disabled_designs", '["eid"]')
		policy.persist(_payload(), daily=False)  # marker, no holiday_policy
		policy.persist(_payload({}), daily=False)
		policy.persist(_payload({"disabled_designs": ["holi"]}), daily=False)  # no disabled
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(_designs(), ["holi"])
		policy.persist(_payload({"disabled": False}), daily=False)  # no designs
		self.assertEqual(_raw("holiday_policy_disabled"), "0")
		self.assertEqual(_designs(), ["holi"])

	def test_disabled_applies_even_when_designs_are_malformed(self):
		_set("holiday_policy_disabled_designs", '["eid"]')
		with mock.patch.object(frappe, "log_error") as log:
			policy.persist(_payload({"disabled": True, "disabled_designs": "diwali"}), daily=False)
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(_designs(), ["eid"])  # kept
		self.assertEqual(log.call_count, 1)

	def test_designs_apply_even_when_disabled_is_malformed(self):
		_set("holiday_policy_disabled", 1)
		with mock.patch.object(frappe, "log_error") as log:
			policy.persist(_payload({"disabled": "yes", "disabled_designs": ["holi"]}), daily=False)
		self.assertEqual(_raw("holiday_policy_disabled"), "1")  # kept
		self.assertEqual(_designs(), ["holi"])
		self.assertEqual(log.call_count, 1)

	def test_unknown_designs_are_dropped_and_names_are_ignored(self):
		policy.persist(
			_payload(
				{
					"disabled": False,
					"disabled_designs": ["pongal", "nope", 7, None, "diwali", "pongal"],
					"aliases": {"diwali": ["lights"]},
					"near_misses": ["x"],
				}
			),
			daily=False,
		)
		self.assertEqual(_designs(), ["diwali", "pongal"])  # canonical order, deduped
		self.assertIsNone(_raw("holiday_policy_names"))

	def test_dropped_unknown_designs_are_reported_without_their_names(self):
		with mock.patch("frappe.log_error") as log:
			policy.persist(_payload({"disabled_designs": ["diwali", "SECRET-NAME", 7]}), daily=False)
			self.assertEqual(_designs(), ["diwali"])
			policy.persist(_payload({"disabled_designs": ["nope"]}), daily=False)  # same hour: held
		self.assertEqual(log.call_count, 1)
		kwargs = log.call_args.kwargs
		self.assertEqual(kwargs["title"], "Holiday touch: admin policy names unknown designs")
		self.assertIn("2 unknown", kwargs["message"])
		self.assertNotIn("SECRET-NAME", kwargs["message"])

	def test_only_known_designs_report_nothing(self):
		with mock.patch("frappe.log_error") as log:
			policy.persist(_payload({"disabled_designs": ["diwali", "eid"]}), daily=False)
		log.assert_not_called()

	def test_malformed_and_unknown_design_reports_are_separate_kinds(self):
		with mock.patch("frappe.log_error") as log:
			policy.persist(_payload({"disabled": "yes", "disabled_designs": ["nope"]}), daily=False)
		self.assertEqual(
			sorted(c.kwargs["title"] for c in log.call_args_list),
			["Holiday touch: admin policy malformed", "Holiday touch: admin policy names unknown designs"],
		)

	def test_a_cleared_list_is_authoritative(self):
		_set("holiday_policy_disabled_designs", '["eid"]')
		policy.persist(_payload({"disabled_designs": []}), daily=False)
		self.assertEqual(_designs(), [])

	def test_a_non_dict_policy_keeps_and_logs_once(self):
		_set("holiday_policy_disabled", 1)
		with mock.patch.object(frappe, "log_error") as log:
			policy.persist(_payload("off"), daily=True)
			policy.persist(_payload("off"), daily=True)  # the hourly marker holds the second
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(log.call_count, 1)

	def test_malformed_log_carries_title_and_message_and_no_payload(self):
		with mock.patch.object(frappe, "log_error") as log:
			policy.persist(_payload({"disabled_designs": "SECRET-TEXT"}), daily=False)
		kwargs = log.call_args.kwargs
		self.assertTrue(kwargs["title"])
		self.assertNotIn("SECRET-TEXT", kwargs["message"])
		self.assertIn("disabled_designs", kwargs["message"])

	def test_100_gate_refreshes_with_an_unchanged_policy_write_nothing(self):
		body = _payload({"disabled": True, "disabled_designs": ["diwali"]})
		calls, spy = self.holiday_writes()
		with spy:
			policy.persist(body, daily=False)
			self.assertEqual(len(calls), 1)  # the first sight of the policy is one write
			for _ in range(100):
				policy.persist(body, daily=False)
		self.assertEqual(len(calls), 1)

	def test_100_real_gate_refreshes_write_nothing(self):
		snap = _snapshot_settings()
		marker = frappe.db.get_single_value(SETTINGS, "chat_was_ready_at")
		key = frappe.db.get_single_value(SETTINGS, "jarvis_admin_api_key")
		frappe.db.set_single_value(
			SETTINGS, "chat_was_ready_at", "2026-01-01 00:00:00", update_modified=False
		)
		conn = _payload({"disabled": True, "disabled_designs": ["diwali", "eid"]}, chat_readiness="Ready")
		calls, spy = self.holiday_writes()
		try:
			with spy, mock.patch.object(admin_client, "get_connection", return_value=conn):
				for _ in range(100):
					account._bust_chat_gate()
					self.assertTrue(account._admin_chat_gate()["ready"])
		finally:
			account._bust_chat_gate()
			frappe.db.set_single_value(SETTINGS, "chat_was_ready_at", marker, update_modified=False)
			frappe.db.set_single_value(SETTINGS, "jarvis_admin_api_key", key, update_modified=False)
			_restore_settings(snap)
		self.assertEqual(len(calls), 1)
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(_designs(), ["diwali", "eid"])
		self.assertIsNone(_raw("holiday_policy_synced_at"))  # the gate never stamps

	def test_synced_at_is_stamped_by_the_daily_path_only(self):
		body = _payload({"disabled": False, "disabled_designs": []})
		policy.persist(body, daily=False)
		self.assertIsNone(_raw("holiday_policy_synced_at"))
		policy.persist(body, daily=True)
		self.assertTrue(_raw("holiday_policy_synced_at"))

	def test_a_daily_run_within_the_hour_does_not_restamp(self):
		body = _payload({"disabled": True, "disabled_designs": ["holi"]})
		policy.persist(body, daily=True)
		first = _raw("holiday_policy_synced_at")
		self.assertTrue(first)
		calls, spy = self.holiday_writes()
		with spy:
			policy.persist(body, daily=True)  # e.g. an onboarding-wizard poll
		self.assertEqual(calls, [])
		self.assertEqual(_raw("holiday_policy_synced_at"), first)

	def test_a_daily_run_restamps_when_the_stamp_is_over_an_hour_old(self):
		body = _payload({"disabled": True, "disabled_designs": ["holi"]})
		policy.persist(body, daily=True)
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2)
		_set("holiday_policy_synced_at", old)
		calls, spy = self.holiday_writes()
		with spy:
			policy.persist(body, daily=True)
		self.assertEqual(len(calls), 1)
		self.assertGreater(
			frappe.utils.get_datetime(_raw("holiday_policy_synced_at")), frappe.utils.get_datetime(old)
		)

	def test_a_daily_run_with_an_unchanged_policy_writes_only_the_stamp(self):
		body = _payload({"disabled": True, "disabled_designs": ["holi"]})
		policy.persist(body, daily=True)
		_set("holiday_policy_synced_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2))
		calls, spy = self.holiday_writes()
		with spy:
			policy.persist(body, daily=True)
		self.assertEqual(len(calls), 1)
		self.assertEqual(list(calls[0][0]), ["holiday_policy_synced_at"])

	def test_a_write_failure_never_propagates(self):
		def boom(*args, **kwargs):
			if _is_holiday_write(args, kwargs):
				raise RuntimeError("db down")
			return real(*args, **kwargs)

		real = frappe.db.set_single_value
		with mock.patch.object(frappe.db, "set_single_value", side_effect=boom):
			policy.persist(_payload({"disabled": True}), daily=True)  # no raise
			policy.persist(None, daily=True)
			policy.persist("junk", daily=False)

	def test_an_unexpected_fault_logs_the_traceback_without_the_payload(self):
		with (
			mock.patch.object(frappe.db, "get_singles_dict", side_effect=RuntimeError("db down")),
			mock.patch.object(frappe, "log_error") as log,
		):
			policy.persist(_payload({"disabled": True}, agent_token="Quokka"), daily=True)
		self.assertEqual(log.call_count, 1)
		kwargs = log.call_args.kwargs
		self.assertEqual(kwargs["title"], "Holiday touch: policy mirror failed")
		self.assertIn("RuntimeError: db down", kwargs["message"])
		self.assertNotIn("Quokka", kwargs["message"])

	def test_a_raising_write_does_not_break_sync_connection(self):
		snap = _snapshot_settings()
		_set_token("tok")
		real = frappe.db.set_single_value

		def boom(*args, **kwargs):
			if _is_holiday_write(args, kwargs):
				raise RuntimeError("db down")
			return real(*args, **kwargs)

		conn = _payload(
			{"disabled": True}, agent_url="ws://localhost:19000", agent_token="k", tenant_status="running"
		)
		try:
			with (
				mock.patch.object(admin_client, "get_connection", return_value=conn),
				mock.patch.object(frappe.db, "set_single_value", side_effect=boom),
			):
				out = onboarding.sync_connection()
		finally:
			_restore_settings(snap)
		self.assertTrue(out["synced"])

	def test_a_raising_write_does_not_break_the_gate(self):
		real = frappe.db.set_single_value

		def boom(*args, **kwargs):
			if _is_holiday_write(args, kwargs):
				raise RuntimeError("db down")
			return real(*args, **kwargs)

		snap = _snapshot_settings()
		marker = frappe.db.get_single_value(SETTINGS, "chat_was_ready_at")
		frappe.db.set_single_value(
			SETTINGS, "chat_was_ready_at", "2026-01-01 00:00:00", update_modified=False
		)
		conn = _payload({"disabled": True}, chat_readiness="Ready")
		try:
			account._bust_chat_gate()
			with (
				mock.patch.object(admin_client, "get_connection", return_value=conn),
				mock.patch.object(frappe.db, "set_single_value", side_effect=boom),
			):
				out = account._admin_chat_gate()
		finally:
			account._bust_chat_gate()
			frappe.db.set_single_value(SETTINGS, "chat_was_ready_at", marker, update_modified=False)
			_restore_settings(snap)
		self.assertTrue(out["ready"])

	def _gate(self, conn, durable):
		"""One real ``_admin_chat_gate`` run; returns the mocked ``persist``."""
		snap = _snapshot_settings()
		marker = frappe.db.get_single_value(SETTINGS, "chat_was_ready_at")
		frappe.db.set_single_value(
			SETTINGS, "chat_was_ready_at", "2026-01-01 00:00:00", update_modified=False
		)
		try:
			account._bust_chat_gate()
			with (
				mock.patch.object(admin_client, "get_connection", return_value=conn),
				mock.patch.object(account, "_write_is_durable", return_value=durable),
				mock.patch.object(account.holiday_policy, "persist") as persist,
			):
				account._admin_chat_gate()
		finally:
			account._bust_chat_gate()
			frappe.db.set_single_value(SETTINGS, "chat_was_ready_at", marker, update_modified=False)
			_restore_settings(snap)
		return persist

	def test_the_gate_mirrors_the_policy_when_the_request_will_commit(self):
		conn = _payload({"disabled": True}, chat_readiness="Ready")
		persist = self._gate(conn, durable=True)
		persist.assert_called_once_with(conn, daily=False)

	def test_the_gate_does_not_mirror_when_the_request_will_be_rolled_back(self):
		# The desk's boot GET is rolled back at the end of the request: the write would be
		# discarded after taking a row lock on Jarvis Settings.
		persist = self._gate(_payload({"disabled": True}, chat_readiness="Ready"), durable=False)
		persist.assert_not_called()

	def test_the_durable_check_is_the_request_method(self):
		with mock.patch.object(frappe.local, "request", mock.Mock(method="GET"), create=True):
			self.assertFalse(account._write_is_durable())
		with mock.patch.object(frappe.local, "request", mock.Mock(method="POST"), create=True):
			self.assertTrue(account._write_is_durable())

	def test_the_daily_sync_stamps_and_applies(self):
		snap = _snapshot_settings()
		_set_token("tok")
		conn = _payload(
			{"disabled": True, "disabled_designs": ["eid"]},
			agent_url="ws://localhost:19000",
			agent_token="k",
			tenant_status="running",
		)
		try:
			with mock.patch.object(admin_client, "get_connection", return_value=conn):
				onboarding.sync_connection()
		finally:
			_restore_settings(snap)
		self.assertEqual(_raw("holiday_policy_disabled"), "1")
		self.assertEqual(_designs(), ["eid"])
		self.assertTrue(_raw("holiday_policy_synced_at"))


class TestAnswerHonoursTheMirror(_Base):
	def setUp(self):
		super().setUp()
		frappe.conf.developer_mode = 1
		frappe.conf[force.CONFIG_KEY] = "diwali:day"
		self.user = _user()

	def test_off_then_on(self):
		self.assertEqual(resolve.answer(self.user, NOW)["kind"], "day")
		policy.persist(_payload({"disabled": True}), daily=False)
		self.assertEqual(resolve.answer(self.user, NOW)["kind"], "none")
		policy.persist(_payload({"disabled": False}), daily=False)
		self.assertEqual(resolve.answer(self.user, NOW)["kind"], "day")

	def test_a_disabled_design_is_off_and_the_others_stay_on(self):
		policy.persist(_payload({"disabled": False, "disabled_designs": ["diwali"]}), daily=False)
		self.assertEqual(resolve.answer(self.user, NOW)["kind"], "none")
		frappe.conf[force.CONFIG_KEY] = "eid:day"
		self.assertEqual(resolve.answer(self.user, NOW)["design"], "eid")
		policy.persist(_payload({"disabled_designs": []}), daily=False)
		frappe.conf[force.CONFIG_KEY] = "diwali:day"
		self.assertEqual(resolve.answer(self.user, NOW)["kind"], "day")
