"""Unit test for the 9.3 reprovision bench-restore patch (mocked, no bench state)."""

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.patches.v2_19_clear_chat_device_for_9_3_reprovision import execute

FIELDS = {"chat_device_id", "chat_device_public_key", "chat_device_token", "chat_device_private_key"}


class TestClearChatDevicePatch(FrappeTestCase):
	def test_clears_device_and_triggers_resync(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.db.set_single_value") as sv,
			patch("frappe.clear_cache"),
			patch("frappe.get_single", return_value=MagicMock()) as gs,
			patch("jarvis.onboarding._resync_after_rebuild") as resync,
		):
			execute()
		# device pairing cleared (all four fields emptied)
		sv.assert_called_once()
		args = sv.call_args.args
		self.assertEqual(args[0], "Jarvis Settings")
		cleared = args[1]
		self.assertEqual(set(cleared), FIELDS)
		self.assertTrue(all(v == "" for v in cleared.values()))
		# and the bench-owned skills + LLM credential are re-pushed
		resync.assert_called_once_with(gs.return_value)

	def test_noop_when_settings_doctype_absent(self):
		with (
			patch("frappe.db.exists", return_value=False),
			patch("frappe.db.set_single_value") as sv,
			patch("frappe.clear_cache"),
			patch("jarvis.onboarding._resync_after_rebuild") as resync,
		):
			execute()
		sv.assert_not_called()
		resync.assert_not_called()
