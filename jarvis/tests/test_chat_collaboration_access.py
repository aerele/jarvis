"""C2 access checks against actual users/grants on disposable test_jarvis."""

from unittest.mock import patch

import frappe

from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, SHARE, as_user
from jarvis.tests.test_chat_sharing import SharingCase


class TestCollaborationAccess(SharingCase):
	def setUp(self):
		super().setUp()
		self.share()
		self.rollout = patch.dict(
			frappe.conf,
			{
				"chat_collaboration_enabled": 1,
				"chat_collaboration_reader_floor": 1,
				"chat_collaboration_runtime_floor": 3,
			},
		)
		self.rollout.start()
		self.addCleanup(self.rollout.stop)

	def mode(self, value):
		from jarvis.chat import collaboration_access as access

		with as_user(OWNER):
			return access.set_access(self.conversation, RECIPIENT, value)

	def grant(self):
		return frappe.get_doc(SHARE, {"conversation": self.conversation, "user": RECIPIENT})

	def test_admin_metadata_tracks_the_current_recipient_access_mode(self):
		from jarvis.chat import sharing

		for mode in ("Chat", "View"):
			self.mode(mode)
			with as_user("Administrator"):
				rows = sharing.list_all_shares(search=RECIPIENT, include_modes=1)["rows"]
			row = next(row for row in rows if row.name == self.grant().name)
			self.assertEqual(row.get("access_mode"), mode)

	def test_existing_view_grant_cannot_send(self):
		from jarvis.chat import collaboration_access as access

		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)
		self.assertEqual(self.grant().access_mode, "View")

	def test_owner_can_grant_chat_without_activating_a_thread(self):
		self.mode("Chat")
		row = self.grant()
		self.assertEqual((row.access_mode, row.access_revision), ("Chat", 2))
		self.assertFalse(
			frappe.db.get_value("Jarvis Conversation", self.conversation, "collaboration_active")
		)

	def test_tool_cannot_reenter_the_human_permission_save_endpoint(self):
		with patch("jarvis.tools.registry.in_tool_dispatch", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				self.mode("Chat")
		self.assertEqual(self.grant().access_mode, "View")

	def test_recipient_is_the_authorized_actor(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		with as_user(RECIPIENT):
			result = access.authorize(self.conversation)
		self.assertEqual(result.actor, RECIPIENT)
		self.assertEqual(result.conversation, self.conversation)
		self.assertEqual(result.share, self.grant().name)
		self.assertEqual(result.grant_revision, 2)

	def test_repeated_permission_save_is_idempotent(self):
		self.mode("Chat")
		self.mode("Chat")
		self.assertEqual(self.grant().access_revision, 2)

	def test_recipient_cannot_upgrade_or_downgrade_any_grant(self):
		from jarvis.chat import collaboration_access as access

		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.set_access(self.conversation, RECIPIENT, "Chat")
		self.assertEqual((self.grant().access_mode, self.grant().access_revision), ("View", 1))

	def test_outsider_cannot_send(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		with as_user(OUTSIDER), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)

	def test_unknown_modes_do_not_upgrade_a_grant(self):
		for value in ("Write", "chat", "", None, {"mode": "Chat"}):
			with (
				self.subTest(value=value),
				self.assertRaises((frappe.ValidationError, frappe.FrappeTypeError)),
			):
				self.mode(value)
		self.assertEqual(self.grant().access_mode, "View")

	def test_downgrade_increments_revision_and_refuses_future_work(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		self.mode("View")
		self.assertEqual((self.grant().access_mode, self.grant().access_revision), ("View", 3))
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)

	def test_downgrade_remains_available_with_rollout_off(self):
		self.mode("Chat")
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}):
			self.mode("View")
		self.assertEqual(self.grant().access_mode, "View")

	def test_disabled_actor_is_refused_after_grant(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		frappe.db.set_value("User", RECIPIENT, "enabled", 0)
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)

	def test_live_role_removal_overrides_cached_roles(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		frappe.get_roles(RECIPIENT)
		frappe.db.delete("Has Role", {"parent": RECIPIENT, "parenttype": "User"})
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)

	def test_site_off_refuses_already_granted_chat(self):
		from jarvis.chat import collaboration_access as access

		self.mode("Chat")
		with (
			patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}),
			as_user(RECIPIENT),
			self.assertRaises(frappe.PermissionError),
		):
			access.authorize(self.conversation)

	def test_old_reader_attestation_alone_does_not_authorize_new_writes(self):
		from jarvis.chat import collaboration_access as access

		with patch.dict(frappe.conf, {"chat_collaboration_runtime_floor": 0}), as_user(OWNER):
			with self.assertRaises(frappe.PermissionError):
				access.set_access(self.conversation, RECIPIENT, "Chat")

	def test_armed_conversation_cannot_gain_chat_mode(self):
		frappe.db.set_value("Jarvis Conversation", self.conversation, "skip_confirmation", 1)
		with self.assertRaises(frappe.PermissionError):
			self.mode("Chat")
		self.assertEqual(self.grant().access_mode, "View")

	def test_revoked_grant_cannot_send_or_be_silently_recreated(self):
		from jarvis.chat import collaboration_access as access
		from jarvis.chat.sharing import set_shares

		self.mode("Chat")
		with as_user(OWNER):
			set_shares(self.conversation, [])
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			access.authorize(self.conversation)
		self.assertFalse(frappe.db.exists(SHARE, {"conversation": self.conversation, "user": RECIPIENT}))
