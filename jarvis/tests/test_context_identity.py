"""The [Context:] bracket must carry the chat user's roles, so the agent answers
"who am I" / "what permissions do I have" straight from context — persona TOOLS.md
tells it to answer that in one line with NO lookup, but until the roles are in the
bracket it has nothing to answer from and improvises an unbounded permission lookup
that runs into the turn wall-clock (the reported "That took too long")."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import turn_handler
from jarvis.permissions import ensure_jarvis_user_role

ROLED_USER = "jarvis-ctxid-roled@example.com"
BASE_USER = "jarvis-ctxid-base@example.com"


def _user_with_roles(email: str, first: str, roles: list[str]) -> None:
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	have = set(frappe.get_roles(email))
	want = set(roles)
	if want - have:
		doc.add_roles(*(want - have))
	frappe.db.commit()


class TestChatUserIdentity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A real chat user: Jarvis User (app access) + one business role, no System Manager.
		_user_with_roles(ROLED_USER, "Roled", ["Jarvis User", "Accounts User"])
		# A bare System User with none of the meaningful roles.
		_user_with_roles(BASE_USER, "Base", [])

	def test_identity_carries_id_name_and_meaningful_roles(self):
		line = turn_handler._chat_user_identity(ROLED_USER, "what are my permissions?")
		self.assertIn(ROLED_USER, line)  # the Frappe id the dispatcher acts as
		self.assertIn("Roled", line)  # full name, so "who am I" needs no lookup
		self.assertIn("roles:", line)
		self.assertIn("Accounts User", line)
		self.assertIn("Jarvis User", line)

	def test_base_all_guest_roles_are_dropped(self):
		# Every user has All/Guest; they are noise, never the answer to "my permissions".
		line = turn_handler._chat_user_identity(ROLED_USER, "what roles do I have")
		self.assertNotIn("All", line.split("roles:")[1].split(","))
		self.assertNotIn("Guest", line)

	def test_user_with_no_special_roles_says_so(self):
		line = turn_handler._chat_user_identity(BASE_USER, "what access do I have")
		self.assertIn("no special roles", line)

	def test_normal_turn_stays_a_bare_id_no_roles(self):
		# Token economy: a turn that is NOT about identity/permissions pays zero extra
		# tokens — the bracket carries just the id, byte-identical to before.
		line = turn_handler._chat_user_identity(ROLED_USER, "create a sales invoice for Acme")
		self.assertEqual(line, ROLED_USER)

	def _assemble_for(self, content: str) -> str:
		"""Run the real assemble_prompt for a message with ``content`` owned by
		ROLED_USER; returns the assembled [Context:] prompt. conv/msg cleaned up."""
		conv = frappe.get_doc({"doctype": turn_handler.CONV, "title": "ctxid", "auto_apply": 0}).insert(
			ignore_permissions=True
		)
		self.addCleanup(
			lambda: frappe.delete_doc(turn_handler.CONV, conv.name, force=True, ignore_permissions=True)
		)
		msg = frappe.get_doc(
			{
				"doctype": turn_handler.MSG,
				"conversation": conv.name,
				"seq": 1,
				"role": "user",
				"content": content,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.delete_doc(turn_handler.MSG, msg.name, force=True, ignore_permissions=True)
		)
		frappe.db.set_value(turn_handler.MSG, msg.name, "owner", ROLED_USER, update_modified=False)
		return turn_handler.assemble_prompt(
			conv,
			message_id=msg.name,
			conversation_id=conv.name,
			context={},
			attachments=[],
			user=ROLED_USER,
		).user_message

	def test_bracket_wires_the_roles_in_on_a_permission_turn(self):
		# The whole point: a permission turn's [Context:] line carries the roles.
		prompt = self._assemble_for("what are all my permissions?")
		self.assertIn("; chat user:", prompt)  # the prefix persona/other tests rely on
		self.assertIn("Accounts User", prompt)  # the user's role reached the bracket

	def test_bracket_omits_roles_on_a_normal_turn(self):
		# Token economy: an ordinary turn must NOT carry the role list.
		prompt = self._assemble_for("draft a quotation for Acme Corp")
		self.assertIn("; chat user:", prompt)
		self.assertNotIn("roles:", prompt)
		self.assertNotIn("Accounts User", prompt)
