"""remember is a write tool, refused in File Box, delegated and scheduled runs."""

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import PermissionDeniedError
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tools import _agent_run_ctx, _user_memory
from jarvis.tools import remember as remember_module
from jarvis.tools.remember import UNATTENDED_MESSAGE, remember

USER = "gh_mem@example.com"
SESSION = "gh-remember-session"


def _ensure_user() -> None:
	if not frappe.db.exists("User", USER):
		frappe.get_doc(
			{"doctype": "User", "email": USER, "first_name": "ghmem", "send_welcome_email": 0}
		).insert(ignore_permissions=True)


class TestRememberIsAWrite(FrappeTestCase):
	def setUp(self):
		_ensure_user()
		frappe.db.delete("Jarvis User Memory", {"user": USER})
		self.conv = _make_conv(USER)
		self.addCleanup(self._cleanup)
		frappe.set_user(USER)

	def _cleanup(self):
		frappe.set_user("Administrator")
		_agent_run_ctx.clear_session_key()
		frappe.db.rollback()
		frappe.delete_doc(CONV, self.conv, force=True, ignore_permissions=True)
		frappe.db.delete("Jarvis User Memory", {"user": USER})
		frappe.db.commit()

	def _stored(self):
		return frappe.db.get_value("Jarvis User Memory", {"user": USER}, "content")

	def _bind(self, **fields):
		frappe.db.set_value(CONV, self.conv, {"session_key": SESSION, **fields}, update_modified=False)
		_agent_run_ctx.set_session_key(SESSION)

	def test_it_is_classed_as_a_write(self):
		self.assertIn("remember", api._WRITE_TOOLS)
		self.assertNotIn("remember", api._GATED_WRITES)  # no card in a normal chat

	def test_a_normal_chat_still_saves(self):
		res = api._run_tool("remember", {"content": "- likes tabs"}, conversation=self.conv)
		self.assertTrue(res["ok"], res)
		self.assertEqual(self._stored(), "- likes tabs")

	def test_a_file_box_run_is_refused_at_the_gate(self):
		frappe.db.set_value(CONV, self.conv, "file_box", 1, update_modified=False)
		res = api._run_tool("remember", {"content": "- from a document"}, conversation=self.conv)
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "FileBoxRefusedError")
		self.assertIn("not available", res["error"]["message"])
		self.assertIsNone(self._stored())

	def test_the_tool_refuses_a_file_box_session(self):
		self._bind(file_box=1)
		with self.assertRaises(PermissionDeniedError) as caught:
			remember(content="- from a document")
		self.assertEqual(str(caught.exception), UNATTENDED_MESSAGE)
		self.assertIsNone(self._stored())

	def test_the_tool_refuses_a_delegated_run(self):
		self._bind()
		with (
			mock.patch("jarvis.tools._delegate_capability.resolve", return_value={"run": "RUN-1"}),
			self.assertRaises(PermissionDeniedError),
		):
			remember(content="- from an agent")
		self.assertIsNone(self._stored())

	def test_the_tool_refuses_a_scheduled_macro_run(self):
		self._bind()
		with (
			mock.patch.object(remember_module, "_scheduled_macro_run", return_value=True),
			self.assertRaises(PermissionDeniedError),
		):
			remember(content="- from a scheduled macro")
		self.assertIsNone(self._stored())

	def test_a_chat_session_is_not_refused(self):
		self._bind()
		self.addCleanup(frappe.cache.delete_value, _user_memory._seen_key())
		out = remember(content="- likes tabs", base_version=_user_memory.EMPTY_VERSION)
		self.assertTrue(out["saved"], out)
