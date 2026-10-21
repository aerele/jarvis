"""Explicit Save atomically changes membership and modes, owner only."""

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user


class TestCollaborationRecipients(CommittedCollaborationCase):
	def save(self, recipients):
		from jarvis.chat.collaboration_access import set_recipients

		return set_recipients(self.conversation, recipients)

	def read(self):
		from jarvis.chat.collaboration_access import get_access

		return get_access(self.conversation)

	def test_owner_metadata_contains_current_modes_and_disclosure(self):
		with as_user(OWNER):
			data = self.read()
		self.assertEqual(len(data["users"]), 1)
		self.assertEqual(data["users"][0]["access_mode"], "Chat")
		self.assertEqual(data["users"][0]["access_revision"], 2)
		self.assertIn("visible to everyone", data["notice"])
		self.assertTrue(data["can_configure_chat"])

	def test_explicit_save_applies_membership_and_modes_as_one_unit(self):
		with as_user(OWNER):
			result = self.save(
				[{"user": RECIPIENT, "access_mode": "View"}, {"user": OUTSIDER, "access_mode": "Chat"}]
			)
		frappe.db.commit()
		modes = {row["name"]: row["access_mode"] for row in result["users"]}
		self.assertEqual(modes, {RECIPIENT: "View", OUTSIDER: "Chat"})
		self.assertEqual(frappe.db.get_value(c.SHARE, self.share, "access_revision"), 3)

	def test_invalid_mode_cannot_partially_revoke_or_add_anyone(self):
		with as_user(OWNER), self.assertRaises(frappe.ValidationError):
			self.save([{"user": OUTSIDER, "access_mode": "Editor"}])
		frappe.db.rollback()
		self.assertTrue(frappe.db.exists(c.SHARE, self.share))
		self.assertFalse(frappe.db.exists(c.SHARE, {"conversation": self.conversation, "user": OUTSIDER}))

	def test_recipient_cannot_read_owner_metadata_or_save_modes(self):
		for actor in (RECIPIENT, OUTSIDER):
			with as_user(actor), self.assertRaises(frappe.PermissionError):
				self.read()
			frappe.db.rollback()
			with as_user(actor), self.assertRaises(frappe.PermissionError):
				self.save([])
			frappe.db.rollback()
		self.assertTrue(frappe.db.exists(c.SHARE, self.share))

	def test_flag_off_allows_downgrade_but_upgrade_rolls_back_entire_save(self):
		frappe.conf.chat_collaboration_enabled = 0
		with as_user(OWNER):
			self.save([{"user": RECIPIENT, "access_mode": "View"}])
		frappe.db.commit()
		with as_user(OWNER), self.assertRaises(frappe.PermissionError):
			self.save([{"user": OUTSIDER, "access_mode": "Chat"}])
		frappe.db.rollback()
		self.assertTrue(frappe.db.exists(c.SHARE, self.share))
		self.assertEqual(frappe.db.get_value(c.SHARE, self.share, "access_mode"), "View")
		self.assertFalse(frappe.db.exists(c.SHARE, {"conversation": self.conversation, "user": OUTSIDER}))

	def test_legacy_membership_client_preserves_existing_modes_and_new_grants_are_view(self):
		from jarvis.chat.sharing import set_shares

		with as_user(OWNER):
			set_shares(self.conversation, [RECIPIENT, OUTSIDER])
		self.assertEqual(frappe.db.get_value(c.SHARE, self.share, "access_mode"), "Chat")
		self.assertEqual(
			c.effective_mode(
				frappe.db.get_value(
					c.SHARE, {"conversation": self.conversation, "user": OUTSIDER}, "access_mode"
				)
			),
			"View",
		)
