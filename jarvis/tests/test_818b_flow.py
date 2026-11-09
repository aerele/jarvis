"""Real-backend flow proof for jarvis-admin-v2#818, PR 1b (T4).

The chat agent's ``create_doc`` of a Server Script or a Report script with a
sandbox-blocked ``.format`` read is refused BEFORE a confirmation card is parked,
with every offending line in one message. The clean form is not refused by this check.
Same entry point as ``test_818_flow``: ``api._run_tool``.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm

PENDING = "Jarvis Pending Action"
THREE_FORMAT = 'a = "PO {}".format(doc.name)\nb = "{} x".format(1)\nc = "{a}".format_map({"a": 1})\n'
CLEAN = 'a = f"PO {doc.name}"\n'


class TestAgentCreateSandboxCodeFlow(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.tag = frappe.generate_hash(length=8)
		self.addCleanup(frappe.db.rollback)

	def _server_script(self, script: str) -> dict:
		return {
			"doctype": "Server Script",
			"values": {
				"script_type": "DocType Event",
				"reference_doctype": "ToDo",
				"doctype_event": "Before Save",
				"script": script,
			},
		}

	def _report(self, script: str) -> dict:
		return {
			"doctype": "Report",
			"values": {
				"report_name": f"zz818b rep {self.tag}",
				"ref_doctype": "ToDo",
				"report_type": "Script Report",
				"is_standard": "No",
				"report_script": script,
			},
		}

	def _create(self, args: dict) -> tuple[dict, dict]:
		spy = {}
		real = pending_confirm.mint

		def mint(**kwargs):
			token = real(**kwargs)
			spy["token"] = token
			return token

		with (
			patch("jarvis.chat.pending_confirm.mint", side_effect=mint),
			patch("jarvis.chat.events.publish_to_user") as pub,
		):
			res = api._run_tool("create_doc", json.loads(json.dumps(args)))
		spy["published"] = pub.called
		return res, spy

	def assert_refused_without_a_card(self, res: dict, spy: dict, pending_before: int, field: str) -> None:
		self.assertFalse(res["ok"], res)
		message = res["error"]["message"]
		self.assertNotIn("&quot;", message)
		self.assertIn(field, message)
		for line in ("line 1", "line 2", "line 3"):
			self.assertIn(line, message)
		self.assertIn("f-string", message)
		self.assertIn("validate_script", message)
		self.assertIsNone(spy.get("token"), "a token was minted")
		self.assertFalse(spy["published"], "a card was published")
		self.assertEqual(frappe.db.count(PENDING), pending_before)

	def test_server_script_with_format_is_refused_before_a_card_is_parked(self):
		pending_before = frappe.db.count(PENDING)
		res, spy = self._create(self._server_script(THREE_FORMAT))
		self.assert_refused_without_a_card(res, spy, pending_before, "Server Script script")
		self.assertFalse(frappe.db.exists("Server Script", {"script": THREE_FORMAT}))

	def test_report_script_with_format_is_refused_before_a_card_is_parked(self):
		pending_before = frappe.db.count(PENDING)
		res, spy = self._create(self._report(THREE_FORMAT))
		self.assert_refused_without_a_card(res, spy, pending_before, "Report report_script")
		self.assertFalse(frappe.db.exists("Report", f"zz818b rep {self.tag}"))

	def test_clean_script_is_not_refused_by_the_check(self):
		for args in (self._server_script(CLEAN), self._report(CLEAN)):
			with self.subTest(doctype=args["doctype"]):
				res, _spy = self._create(args)
				self.assertNotIn("server-script sandbox", json.dumps(res), res)

	def test_deep_script_gets_a_normal_envelope(self):
		deep = "x = " + "(" * 100000 + "1" + ")" * 100000
		for args in (self._server_script(deep), self._report(deep)):
			with self.subTest(doctype=args["doctype"]):
				res, _spy = self._create(args)
				self.assertIn("ok", res)
				self.assertNotIn("RecursionError", json.dumps(res))
				self.assertNotIn("MemoryError", json.dumps(res))
