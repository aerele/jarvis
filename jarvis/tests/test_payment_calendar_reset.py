"""Purge follows accepted workspace resets, independently of credential revocation."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from jarvis import onboarding, settings_reset


class TestPaymentCalendarReset(TestCase):
	def test_dev_onboarding_reset_explicitly_purges_feature_state(self):
		from jarvis import dev

		settings = MagicMock()
		settings.get.return_value = ""
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(settings_reset, "apply"),
			patch.object(frappe.db, "commit"),
			patch("jarvis.payment_calendar.lifecycle.reset_workspace") as purge,
		):
			dev.reset_onboarding(wipe_data=False)
			purge.assert_called_once_with()

	def test_self_serve_reset_without_wipe_or_llm_revoke_still_purges(self):
		with (
			patch.object(onboarding, "require_jarvis_admin"),
			patch.object(frappe, "get_single", return_value=frappe._dict(agent_url="")),
			patch.object(onboarding, "_surface", return_value={"status": "Applied"}),
			patch.object(onboarding, "_disconnect_agent_transport"),
			patch("jarvis.payment_calendar.lifecycle.reset_workspace") as purge,
		):
			onboarding.request_workspace_reset()
			purge.assert_called_once_with()

	def test_control_plane_refusal_cannot_purge_workspace_preferences(self):
		with (
			patch.object(onboarding, "require_jarvis_admin"),
			patch.object(frappe, "get_single", return_value=frappe._dict(agent_url="")),
			patch.object(onboarding, "_surface", side_effect=frappe.PermissionError),
			patch("jarvis.payment_calendar.lifecycle.reset_workspace") as purge,
		):
			with self.assertRaises(frappe.PermissionError):
				onboarding.request_workspace_reset()
			purge.assert_not_called()

	def test_generic_credential_reset_does_not_purge_calendar_preferences(self):
		with (
			patch.object(settings_reset, "clear_settings_password"),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(frappe.db, "delete"),
			patch("jarvis.admin_client.clear_cached_token"),
			patch("jarvis.payment_calendar.lifecycle.reset_workspace") as purge,
		):
			settings_reset.apply(MagicMock(), settings_reset.LLM)
			purge.assert_not_called()
