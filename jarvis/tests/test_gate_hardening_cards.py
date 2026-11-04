"""Auto mode and "confirm all" always show the confirm card for submit_doc,
send_email, run_import and run_method. An armed macro and an approved skill run, and
every other tool, keep their behaviour."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user

MSG = "Jarvis Chat Message"
CARDED = ("submit_doc", "send_email", "run_import", "run_method")
CALLS = {
	"submit_doc": {"doctype": "ToDo", "name": "gh-none"},
	"send_email": {"recipients": "nobody@example.com", "subject": "hi", "content": "body"},
	"run_import": {"doctype": "ToDo", "file_url": "/private/files/gh-none.csv"},
	"run_method": {"method": "frappe.ping"},
}


def _cleanup():
	for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
		pending_confirm.clear_for_conversation(TEST_USER, conv)
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestAutoModesCardTheFourTools(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		self.addCleanup(_cleanup)
		self.addCleanup(frappe.set_user, self._orig)
		published = patch("jarvis.chat.events.publish_to_user")
		published.start()
		self.addCleanup(published.stop)

	def conv(self, mode: str) -> str:
		conv = _make_conv(TEST_USER)
		if mode == "auto_mode":
			frappe.db.set_value(CONV, conv, "auto_mode", 1, update_modified=False)
		elif mode == "confirm_all":
			api._request_autorun_arm(conv, "")
		else:
			frappe.db.set_value(CONV, conv, mode, 1, update_modified=False)
		frappe.db.commit()
		return conv

	def run_tool(self, tool, args, conv):
		with patch("jarvis.api._run_covered_write", return_value={"ok": True, "data": {}}) as covered:
			res = api._run_tool(tool, dict(args), conversation=conv)
		return res, covered

	def test_the_four_never_run_uncarded_in_auto_mode_or_confirm_all(self):
		for mode in ("auto_mode", "confirm_all"):
			for tool in CARDED:
				with self.subTest(mode=mode, tool=tool):
					res, covered = self.run_tool(tool, CALLS[tool], self.conv(mode))
					self.assertFalse(covered.called, (tool, res))

	def test_run_method_and_send_email_show_their_card(self):
		for mode in ("auto_mode", "confirm_all"):
			for tool in ("run_method", "send_email"):
				with self.subTest(mode=mode, tool=tool):
					res, _ = self.run_tool(tool, CALLS[tool], self.conv(mode))
					self.assertEqual((res.get("data") or {}).get("status"), "pending_confirmation", res)

	def test_other_tools_still_run_uncarded_in_both_modes(self):
		args = {"doctype": "ToDo", "values": {"description": "gh-cards"}}
		for mode in ("auto_mode", "confirm_all"):
			with self.subTest(mode=mode):
				res, covered = self.run_tool("create_doc", args, self.conv(mode))
				self.assertTrue(covered.called, res)

	def test_an_armed_macro_still_runs_them_uncarded(self):
		# run_import is left out: its call reads a real file before the gate decides.
		for tool in ("submit_doc", "send_email", "run_method"):
			with self.subTest(tool=tool):
				_, covered = self.run_tool(tool, CALLS[tool], self.conv("skip_confirmation"))
				self.assertTrue(covered.called)

	def test_the_sets_agree(self):
		self.assertEqual(api._CARDED_IN_AUTO, frozenset(CARDED))
		self.assertFalse(api._AUTO_COVERED & api._CARDED_IN_AUTO)
		self.assertEqual(api._AUTO_COVERED | api._CARDED_IN_AUTO, api._COVERED)
		for tool in ("apply_workflow_action", "share_doc", "assign_to", "update_wiki"):
			self.assertIn(tool, api._AUTO_COVERED, tool)
		for tool in CARDED:
			self.assertIn(tool, api._SKILL_AUTORUN_COVERED, tool)  # the skill run is unchanged
			self.assertEqual(api._gating_badge(tool), "always_asks", tool)
