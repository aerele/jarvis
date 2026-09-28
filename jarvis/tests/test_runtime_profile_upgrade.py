"""Readiness and upgrade lifecycle with real profile validation, not a synthetic profile patch."""

import json
import unittest
from unittest.mock import patch

import frappe

from jarvis.chat import runtime_profile as rp
from jarvis.tests import test_runtime_profile as fixtures
from jarvis.tests._gateway_fixtures import TEST_PROFILE
from jarvis.tests.test_runtime_profile import connection, envelope


class TestRuntimeProfileUpgrade(unittest.TestCase):
	record = fixtures.TestRuntimeProfileStorage.record

	def setUp(self):
		fixtures.TestRuntimeProfileStorage.setUp(self)
		self.conf = patch.object(frappe, "conf", frappe._dict())
		self.conf.start()
		self.addCleanup(self.conf.stop)

	def test_ready_response_without_profile_is_blocked_but_existing_profile_survives(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import admin_client

		for blob, readiness in ((None, "Unavailable"), (json.dumps(self.record()), "Ready")):
			self.db.get_value.return_value = blob
			rp._forget_snapshot()
			with patch.object(
				admin_client, "_post", return_value={**connection(), "chat_readiness": "Ready"}
			):
				self.assertEqual(admin_client.get_connection()["chat_readiness"], readiness)

	def test_cached_ready_verdict_hydrates_before_reporting_ready(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import account, admin_client

		data = rp.ConnectionData({**connection(), "chat_readiness": "Ready"}, envelope())
		with (
			patch.object(account, "_ready_verdict", return_value={"ready": True, "reason": None}),
			patch.object(admin_client, "_post", return_value=data) as post,
			patch.object(frappe, "get_doc"),
			patch("jarvis.chat.pump.chat_worker_status", return_value={}),
		):
			self.assertTrue(account.is_ready_for_chat()["ready"])
			self.assertEqual(rp.get_profile(), TEST_PROFILE)
			self.assertEqual(post.call_count, 1)

	def test_cached_ready_cannot_override_missing_profile_on_old_admin(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import account, admin_client

		with (
			patch.object(account, "_ready_verdict", return_value={"ready": True, "reason": None}),
			patch.object(admin_client, "_post", return_value={**connection(), "chat_readiness": "Ready"}),
			patch("jarvis.chat.pump.chat_worker_status", return_value={}),
		):
			verdict = account.is_ready_for_chat()
		self.assertFalse(verdict["ready"])
		self.assertTrue(verdict["retryable"])
		self.assertEqual(verdict["reason"], "container_unavailable")

	def test_upgrade_bootstrap_fetches_missing_profile_but_does_not_replace_valid_one(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import admin_client

		data = rp.ConnectionData({**connection(), "chat_readiness": "Ready"}, envelope())
		with (
			patch.object(admin_client, "_post", return_value=data) as post,
			patch.object(frappe, "get_doc"),
			patch("jarvis.onboarding.write_connection") as write_connection,
		):
			rp.after_migrate()
			self.assertEqual(rp.get_profile(), TEST_PROFILE)
			rp.after_migrate()
			self.assertEqual(post.call_count, 1)
			write_connection.assert_not_called()

	def test_upgrade_is_not_blocked_by_control_plane_outage(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import admin_client

		with (
			patch.object(admin_client, "get_connection", side_effect=TimeoutError),
			patch.object(frappe, "logger"),
		):
			rp.after_migrate()
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()

	def test_configured_site_does_not_contact_admin_when_checking_local_readiness(self):
		self.db.get_value.return_value = json.dumps(self.record())
		with patch("jarvis.admin_client.get_connection") as get_connection:
			self.assertEqual(rp.ensure_ready(), TEST_PROFILE)
			get_connection.assert_not_called()
