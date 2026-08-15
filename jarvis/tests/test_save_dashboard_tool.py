"""#884: the ``jarvis__save_dashboard`` chat tool — agent-delivered dashboards.

The dashboards flow no longer publishes hosted canvas documents; the agent
delivers the authored HTML through this tool, which resolves the conversation
from the caller's session bearer and delegates persistence to the one shared
save path (``dashboards_api.save_dashboard``). These tests drive the tool
exactly as the plugin dispatch would (session_key stashed on frappe.local,
user impersonated) without a live model turn.

Run: bench --site <site> run-tests --module jarvis.tests.test_save_dashboard_tool
"""

import unittest
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._agent_run_ctx import clear_session_key, set_session_key
from jarvis.tools.save_dashboard import save_dashboard

CONVERSATION = "Jarvis Conversation"
DASHBOARD = "Jarvis Dashboard"

USER = "dash884@example.com"
SESSION_KEY = "test-884-session-key"

_HTML = "<!doctype html><html><body><h1>Total Items</h1><p>10</p></body></html>"
_HTML_V2 = "<!doctype html><html><body><h1>Total Items</h1><p>11</p></body></html>"
_HTML_CONNECTED = (
	"<!doctype html><html><body>"
	'<script type="application/json" id="jarvis-sources">'
	'{"sources":[{"source_name":"items","tool":"get_list",'
	'"spec":{"doctype":"Item","limit":5}}]}'
	"</script><h1>Items</h1></body></html>"
)


def _ensure_user(email: str) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
	if "Jarvis User" not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles("Jarvis User")
	return email


def _make_conversation(owner: str, session_key: str) -> str:
	conv = frappe.get_doc({"doctype": CONVERSATION, "title": "dash 884 test"})
	conv.flags.ignore_permissions = True
	conv.insert()
	# owner is stamped from the acting session; pin it plus the permlevel-1
	# session_key the worker normally sets via db.set_value.
	frappe.db.set_value(
		CONVERSATION,
		conv.name,
		{"owner": owner, "session_key": session_key},
		update_modified=False,
	)
	return conv.name


class TestSaveDashboardTool(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		_ensure_user(USER)
		cls.conversation = _make_conversation(USER, SESSION_KEY)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			DASHBOARD, filters={"source_conversation": cls.conversation}, pluck="name"
		):
			frappe.delete_doc(DASHBOARD, name, force=True, ignore_permissions=True)
		frappe.delete_doc(CONVERSATION, cls.conversation, force=True, ignore_permissions=True)
		frappe.set_user(cls._orig_user)

	def setUp(self):
		frappe.set_user(USER)
		set_session_key(SESSION_KEY)

	def tearDown(self):
		clear_session_key()
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			DASHBOARD, filters={"source_conversation": self.conversation}, pluck="name"
		):
			frappe.delete_doc(DASHBOARD, name, force=True, ignore_permissions=True)

	def test_first_save_creates_user_scoped_row_and_publishes(self):
		with patch("jarvis.chat.events.publish_to_user") as pub:
			out = save_dashboard(html=_HTML, dashboard_title="Total Items")
		self.assertTrue(out["dashboard"])
		row = frappe.db.get_value(
			DASHBOARD,
			out["dashboard"],
			["dashboard_title", "scope", "source_conversation", "dashboard_type", "owner"],
			as_dict=True,
		)
		self.assertEqual(row.dashboard_title, "Total Items")
		self.assertEqual(row.scope, "User")
		self.assertEqual(row.source_conversation, self.conversation)
		self.assertEqual(row.dashboard_type, "Static")
		self.assertEqual(row.owner, USER)
		pub.assert_called_once()
		user, payload = pub.call_args.args
		self.assertEqual(user, USER)
		self.assertEqual(payload["kind"], "dashboard")
		self.assertEqual(payload["conversation_id"], self.conversation)
		self.assertEqual(payload["name"], out["dashboard"])

	def test_revision_updates_in_place(self):
		with patch("jarvis.chat.events.publish_to_user"):
			first = save_dashboard(html=_HTML, dashboard_title="Total Items")
			second = save_dashboard(html=_HTML_V2, name=first["dashboard"])
		self.assertEqual(first["dashboard"], second["dashboard"])
		rows = frappe.get_all(
			DASHBOARD, filters={"source_conversation": self.conversation}, pluck="name"
		)
		self.assertEqual(len(rows), 1)
		self.assertIn("11", frappe.db.get_value(DASHBOARD, rows[0], "html"))

	def test_sources_block_yields_connected_dashboard(self):
		with patch("jarvis.chat.events.publish_to_user"):
			out = save_dashboard(html=_HTML_CONNECTED, dashboard_title="Items Live")
		self.assertEqual(
			frappe.db.get_value(DASHBOARD, out["dashboard"], "dashboard_type"),
			"Connected",
		)

	def test_saved_probe_flips_after_first_save(self):
		from jarvis.chat.turn_handler import _dashboard_saved

		self.assertFalse(_dashboard_saved(self.conversation))
		with patch("jarvis.chat.events.publish_to_user"):
			save_dashboard(html=_HTML, dashboard_title="Total Items")
		self.assertTrue(_dashboard_saved(self.conversation))

	def test_create_requires_title(self):
		with self.assertRaises(InvalidArgumentError):
			save_dashboard(html=_HTML)

	def test_empty_html_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			save_dashboard(html="  ", dashboard_title="x")

	def test_missing_session_key_rejected(self):
		clear_session_key()
		with self.assertRaises(InvalidArgumentError):
			save_dashboard(html=_HTML, dashboard_title="x")

	def test_unknown_session_key_rejected(self):
		set_session_key("some-other-session")
		with self.assertRaises(InvalidArgumentError):
			save_dashboard(html=_HTML, dashboard_title="x")

	def test_cannot_revise_another_conversations_dashboard(self):
		frappe.set_user("Administrator")
		other_conv = _make_conversation(USER, "other-884-session")
		frappe.set_user(USER)
		try:
			set_session_key("other-884-session")
			with patch("jarvis.chat.events.publish_to_user"):
				theirs = save_dashboard(html=_HTML, dashboard_title="Other Conv Dash")
			set_session_key(SESSION_KEY)
			with self.assertRaises(InvalidArgumentError):
				save_dashboard(html=_HTML_V2, name=theirs["dashboard"])
		finally:
			frappe.set_user("Administrator")
			for name in frappe.get_all(
				DASHBOARD, filters={"source_conversation": other_conv}, pluck="name"
			):
				frappe.delete_doc(DASHBOARD, name, force=True, ignore_permissions=True)
			frappe.delete_doc(CONVERSATION, other_conv, force=True, ignore_permissions=True)
			frappe.set_user(USER)

	def test_validation_error_surfaces_as_invalid_argument(self):
		# 141-char title trips the DocType controller's cap; the tool must hand
		# the model a clean InvalidArgumentError, never a raw ValidationError.
		with self.assertRaises(InvalidArgumentError):
			save_dashboard(html=_HTML, dashboard_title="x" * 141)


if __name__ == "__main__":
	unittest.main()
