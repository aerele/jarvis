"""Tests for the Jarvis Chat Message DocType."""

import frappe
from frappe.tests.utils import FrappeTestCase

DOCTYPE = "Jarvis Chat Message"
CONV_DOCTYPE = "Jarvis Conversation"


def _make_conversation(title: str = "T") -> str:
	doc = frappe.get_doc(
		{
			"doctype": CONV_DOCTYPE,
			"title": title,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _cleanup_conversation(name: str) -> None:
	for child in frappe.get_all(DOCTYPE, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(DOCTYPE, child, ignore_permissions=True, force=True)
	frappe.delete_doc(CONV_DOCTYPE, name, ignore_permissions=True, force=True)
	frappe.db.commit()


class TestJarvisChatMessageDocType(FrappeTestCase):
	def test_doctype_exists(self):
		self.assertTrue(frappe.db.exists("DocType", DOCTYPE))

	def test_required_fields_exist(self):
		meta = frappe.get_meta(DOCTYPE)
		field_names = {f.fieldname for f in meta.fields}
		expected = {
			"conversation",
			"seq",
			"role",
			"content",
			"tool_name",
			"tool_args",
			"tool_result",
			"tool_status",
			"streaming",
			"error",
		}
		missing = expected - field_names
		self.assertFalse(missing, f"missing fields: {missing}")

	def test_role_select_options(self):
		meta = frappe.get_meta(DOCTYPE)
		f = next(f for f in meta.fields if f.fieldname == "role")
		options = (f.options or "").split("\n")
		for r in ("user", "assistant", "tool"):
			self.assertIn(r, options)

	def test_tool_status_select_options(self):
		meta = frappe.get_meta(DOCTYPE)
		f = next(f for f in meta.fields if f.fieldname == "tool_status")
		options = (f.options or "").split("\n")
		for s in ("running", "completed", "error"):
			self.assertIn(s, options)

	def test_conversation_is_required_link(self):
		meta = frappe.get_meta(DOCTYPE)
		f = next(f for f in meta.fields if f.fieldname == "conversation")
		self.assertEqual(f.fieldtype, "Link")
		self.assertEqual(f.options, CONV_DOCTYPE)
		self.assertTrue(f.reqd)

	def test_insert_and_link_to_conversation(self):
		conv = _make_conversation("link-test")
		try:
			msg = frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"conversation": conv,
					"seq": 1,
					"role": "user",
					"content": "hello",
					"streaming": 0,
				}
			)
			msg.insert(ignore_permissions=True)
			frappe.db.commit()
			self.assertEqual(msg.conversation, conv)
		finally:
			_cleanup_conversation(conv)

	def test_read_scoped_to_jarvis_user_role(self):
		"""Security review TASK 7: Jarvis Chat Message read is granted only to
		"Jarvis User" (+ System Manager), NOT to ``role: "All"``, and is NOT
		row-``owner`` ``if_owner`` scoped. The ownership axis is the LINKED
		conversation's owner, enforced at the ORM by
		``jarvis.chat.chat_permissions`` (permission_query_conditions +
		has_permission) — the message row's own owner is not the authority, so
		``if_owner`` must NOT be set on these rows (it would wrongly hide the
		worker-owned assistant/tool rows of the caller's own conversation)."""
		meta = frappe.get_meta(DOCTYPE)
		read_roles = {p.role for p in meta.permissions if p.read and (p.permlevel or 0) == 0}
		self.assertNotIn("All", read_roles, "chat message read must not be granted to role 'All'")
		self.assertEqual(
			read_roles,
			{"Jarvis User", "System Manager"},
			f"unexpected read roles on Jarvis Chat Message: {read_roles}",
		)
		for perm in meta.permissions:
			if perm.read and (perm.permlevel or 0) == 0:
				self.assertFalse(
					perm.if_owner,
					f"role {perm.role} uses if_owner, but the axis is the linked "
					f"conversation's owner (chat_permissions hooks), not the row owner",
				)

	def test_cross_conversation_injection_blocked(self):
		"""TASK 2: a message pointing at a conversation the caller does not own
		is rejected (has_permission hook + controller validate)."""
		conv = _make_conversation("victim-conv")
		attacker = "cm-inject-attacker@example.test"
		try:
			if not frappe.db.exists("User", attacker):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": attacker,
						"first_name": "atk",
						"send_welcome_email": 0,
						"enabled": 1,
						"user_type": "System User",
					}
				).insert(ignore_permissions=True)
			frappe.get_doc("User", attacker).add_roles("Jarvis User")
			# conv is owned by Administrator (inserted ignore_permissions); attacker
			# is a different user, so the injection must be denied.
			frappe.db.commit()
			frappe.set_user(attacker)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": DOCTYPE,
						"conversation": conv,
						"seq": 42,
						"role": "user",
						"content": "injected",
					}
				).insert()
		finally:
			frappe.set_user("Administrator")
			_cleanup_conversation(conv)
			if frappe.db.exists("User", attacker):
				frappe.delete_doc("User", attacker, ignore_permissions=True, force=True)
			frappe.db.commit()
