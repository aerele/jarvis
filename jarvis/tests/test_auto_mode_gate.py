"""Tests for per-chat auto mode's write gate, receipt marker and model context (#581).

A conversation with ``auto_mode=1`` runs COVERED writes (``_GATED_WRITES - _BRAKE``)
uncarded through ``_run_covered_write`` (audit provenance ``auto_mode``), the BRAKE
still parks, Halt refuses the next write but leaves the mode on, and a failed write
returns its error unchanged and leaves the mode on (unlike request_autorun, which
clears). The flag is set here with ``frappe.db.set_value``; the only writer in
production is send_message's locked ``_write_conv`` (see test_auto_mode.py).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api
from jarvis.chat import pending_confirm
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user

MSG = "Jarvis Chat Message"
PENDING = "Jarvis Pending Action"


def _auto_mode(conv: str) -> int:
	return int(frappe.db.get_value(CONV, conv, "auto_mode") or 0)


def _cleanup():
	for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
		pending_confirm.clear_for_conversation(TEST_USER, conv)
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	for name in frappe.get_all("ToDo", filters={"owner": TEST_USER}, pluck="name"):
		frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestAutoModeGate(FrappeTestCase):
	"""The _run_tool gate branch for a conversation with auto_mode=1."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		from jarvis.tools import _agent_run_ctx

		_agent_run_ctx.take_request_autorun_applied()  # clear any test-set marker
		_cleanup()

	def _auto_conv(self) -> str:
		conv = _make_conv(TEST_USER)
		# set_value bypasses the controller guard, exactly like send_message's _write_conv
		frappe.db.set_value(CONV, conv, "auto_mode", 1, update_modified=False)
		return conv

	def test_provenance_is_registered_in_both_places(self):
		self.assertIn("auto_mode", agent_audit.PROVENANCES)
		options = frappe.get_meta("Jarvis Agent Write").get_field("provenance").options.split("\n")
		self.assertEqual(tuple(options), agent_audit.PROVENANCES)

	def test_covered_write_runs_directly_in_auto_mode(self):
		from jarvis.tools import _agent_run_ctx

		conv = self._auto_conv()
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "am-direct"}},
				conversation=conv,
			)
		self.assertTrue(disp.called, "a covered write runs uncarded in an auto-mode chat")
		self.assertEqual(disp.call_args.kwargs.get("provenance"), "auto_mode")
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}), "no confirmation card is parked")
		self.assertTrue(
			_agent_run_ctx.take_request_autorun_applied(),
			"the receipt marker is set so the row labels auto_applied",
		)
		self.assertEqual(_auto_mode(conv), 1)

	def test_brake_write_still_parks_in_auto_mode(self):
		conv = self._auto_conv()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "am-brake"}).insert(ignore_permissions=True)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a brake tool still cards")
		self.assertFalse(disp.called)
		self.assertEqual(_auto_mode(conv), 1)

	def test_halt_refuses_next_write_and_keeps_mode(self):
		from jarvis.chat import turn_message_binding

		conv = self._auto_conv()
		turn_message_binding.request_run_cancel(conv)
		try:
			with patch("jarvis.api.dispatch_confirmed") as disp:
				r = api._run_tool(
					"create_doc",
					{"doctype": "ToDo", "values": {"description": "am-halt"}},
					conversation=conv,
				)
			self.assertFalse(r["ok"])
			self.assertEqual(r["error"]["code"], api.RunHaltedError.__name__)
			self.assertFalse(disp.called, "Halt refuses the covered write without executing")
			self.assertFalse(
				turn_message_binding.is_run_cancel_requested(conv), "the cancel signal is cleared"
			)
			self.assertEqual(_auto_mode(conv), 1, "Halt does not turn the chat's mode off")
			# The signal is consumed: the next covered write runs again.
			with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
				api._run_tool(
					"create_doc",
					{"doctype": "ToDo", "values": {"description": "am-after-halt"}},
					conversation=conv,
				)
			self.assertTrue(disp.called)
		finally:
			turn_message_binding.clear_run_cancel(conv)

	def test_failed_covered_write_keeps_mode(self):
		conv = self._auto_conv()
		failure = {"ok": False, "error": {"message": "boom"}}
		with patch("jarvis.api.dispatch_confirmed", return_value=failure):
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "am-err"}},
				conversation=conv,
			)
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["message"], "boom", "the error comes back unchanged")
		self.assertEqual(_auto_mode(conv), 1, "a failed write does not end auto mode")

	def test_normal_chat_still_parks_covered_write(self):
		conv = _make_conv(TEST_USER)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "am-normal"}},
				conversation=conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(disp.called)

	def test_over_cap_message_is_autorun_aware(self):
		from jarvis.tools._bulk import _MAX_BATCH

		conv = self._auto_conv()
		docs = [{"doctype": "ToDo", "values": {"description": f"o{i}"}} for i in range(_MAX_BATCH + 1)]
		r = api._run_tool("create_docs", {"docs": docs}, conversation=conv)
		self.assertFalse(r["ok"])
		self.assertIn("too many records", r["error"]["message"])
		self.assertNotIn("confirm each one", r["error"]["message"])


class TestAutoModeReceiptLabel(FrappeTestCase):
	"""The auto_mode marker labels the receipt auto_applied with no armer name."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		from jarvis.tools import _agent_run_ctx

		_agent_run_ctx.take_request_autorun_applied()
		_cleanup()

	def test_auto_mode_write_labels_auto_applied_without_an_armer(self):
		from jarvis.tools import _agent_run_ctx

		conv = _make_conv(TEST_USER)
		sk = "sk-auto-mode-receipt"
		frappe.db.set_value(CONV, conv, {"session_key": sk, "auto_mode": 1}, update_modified=False)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "am-receipt"}},
				conversation=conv,
			)
		api._persist_and_publish_tool_call(
			session_key=sk,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "am-receipt"}},
			result={"ok": True, "data": {}},
			tool_call_id="tc-auto-1",
		)
		row = frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "tool", "tool_call_id": "tc-auto-1"},
			fields=["action_outcome", "armed_by_macro", "armed_by_skill"],
		)
		self.assertEqual(len(row), 1)
		self.assertEqual(row[0].action_outcome, "auto_applied")
		self.assertFalse(row[0].armed_by_macro)
		self.assertFalse(row[0].armed_by_skill)
		# Consumed: a later ordinary receipt must NOT inherit the label.
		self.assertFalse(_agent_run_ctx.take_request_autorun_applied())


class TestAutoModeTurnContext(FrappeTestCase):
	"""assemble_prompt carries the auto mode token only for an auto-mode chat."""

	TOKEN = "; auto mode: apply changes directly"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		_cleanup()

	def _assembled(self, conv_doc, seq: int) -> str:
		from jarvis.chat.turn_handler import assemble_prompt

		msg = frappe.get_doc(
			{"doctype": MSG, "conversation": conv_doc.name, "seq": seq, "role": "user", "content": "hello"}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		ap = assemble_prompt(
			conv_doc,
			message_id=msg.name,
			conversation_id=conv_doc.name,
			context={},
			attachments=[],
			user=TEST_USER,
		)
		return ap.user_message

	def test_turn_context_carries_auto_mode_token_only_when_on(self):
		conv = _make_conv(TEST_USER)
		doc = frappe.get_doc(CONV, conv)
		self.assertNotIn(self.TOKEN, self._assembled(doc, 1))
		doc.auto_mode = 1  # in memory only, like the sibling armed-run context tests
		self.assertIn(self.TOKEN, self._assembled(doc, 2))
