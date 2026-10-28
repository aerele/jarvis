"""The owner-approved limit counts visible grants, preserving hidden recipients."""

import frappe

from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user, make_user
from jarvis.tests.test_chat_sharing import SharingCase


class TestVisibleShareCap(SharingCase):
	def test_hidden_disabled_recipient_does_not_consume_visible_capacity(self):
		users = [make_user(f"visible-cap-{n}@example.com") for n in range(25)]
		self.share(users)
		frappe.db.set_value("User", users[0], "enabled", 0)
		self.share(users + [RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 26)
		with as_user(OWNER):
			from jarvis.chat.sharing import get_shares

			rows = get_shares(self.conversation)["users"]
		self.assertEqual(sum(not row["hidden"] for row in rows), 25)
		self.assertEqual(sum(row["hidden"] for row in rows), 1)

	def test_hidden_recipient_without_access_does_not_consume_capacity(self):
		users = [make_user(f"visible-role-cap-{n}@example.com") for n in range(25)]
		self.share(users)
		frappe.db.delete("Has Role", {"parent": users[0], "role": "Jarvis User"})
		self.share(users + [RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 26)

	def test_twenty_six_visible_recipients_refused_without_removing_anyone(self):
		users = [make_user(f"visible-refused-cap-{n}@example.com") for n in range(25)]
		self.share(users)
		with self.assertRaises(frappe.ValidationError):
			self.share(users + [RECIPIENT])
		self.assertEqual(frappe.db.count(SHARE, {"conversation": self.conversation}), 25)
