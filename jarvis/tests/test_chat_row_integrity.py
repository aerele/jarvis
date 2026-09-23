"""Chat-row integrity + origin (P0a, §4.3 / AC-U15).

Guarded fields on ``Jarvis Chat Message`` (pending_card, tool_call_id, tool_status,
action_outcome, tool_args, origin) can only be set on insert, or changed on save,
by a server writer that sets ``flags.jarvis_server_write``. ``origin`` comes only
from server context (``message_origin`` / ``delegated_send`` / the required
``_enqueue_turn(origin=)``), never from a request argument."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import actions_api, pump, turn_handler
from jarvis.chat import api as chat_api
from jarvis.permissions import delegated_send, message_origin

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"
USER = "p0a-owner@example.com"
TITLE = "p0a-integrity"


def _ensure_user() -> None:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", USER):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": USER,
				"first_name": "p0a",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
	if "Jarvis User" not in frappe.get_roles(USER):
		frappe.get_doc("User", USER).add_roles("Jarvis User")
	frappe.db.commit()


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _no_dispatch():
	"""A real send/enqueue that persists its user row but runs no turn."""
	with (
		patch("jarvis.chat.api._dispatch_turn", return_value=None),
		patch("jarvis.chat.admission.turn_machine_enabled", return_value=False),
		patch("jarvis.chat.api.validate_can_send", return_value=(True, None)),
	):
		yield


def _cleanup() -> None:
	for conv in frappe.get_all(CONV, filters={"owner": USER}, pluck="name"):
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.db.delete(APPROVAL, {"conversation": conv})
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	frappe.db.commit()


class _Base(FrappeTestCase):
	def setUp(self):
		super().setUp()
		_ensure_user()
		_cleanup()
		self.addCleanup(_cleanup)
		self.conv = self._conv()

	def _conv(self) -> str:
		with _as(USER):
			doc = frappe.get_doc({"doctype": CONV, "title": TITLE, "status": "Active"})
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def _server_row(self, **fields) -> str:
		doc = frappe.get_doc({"doctype": MSG, "conversation": self.conv, "seq": 1, **fields})
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def _latest_user_row(self, conv=None):
		return frappe.get_all(
			MSG,
			filters={"conversation": conv or self.conv, "role": "user"},
			fields=["name", "origin", "hidden", "content"],
			order_by="seq desc",
			limit=1,
		)[0]


class TestGuard(_Base):
	def test_owner_update_doc_of_pending_card_is_refused(self):
		from jarvis.tools.update_doc import update_doc

		name = self._server_row(role="tool", tool_status="pending", tool_call_id="tok-a", pending_card="{}")
		with _as(USER), self.assertRaises(frappe.PermissionError):
			update_doc(MSG, name, {"pending_card": '{"verb": "delete"}'})
		self.assertEqual(frappe.db.get_value(MSG, name, "pending_card"), "{}")

	def test_ignore_permissions_does_not_unlock_a_guarded_change(self):
		name = self._server_row(role="tool", tool_status="pending", tool_call_id="tok-b")
		doc = frappe.get_doc(MSG, name)
		doc.tool_status = "completed"
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.PermissionError):
			doc.save()

	def test_create_doc_of_a_pending_row_is_refused(self):
		from jarvis.tools.create_doc import create_doc

		values = {
			"conversation": self.conv,
			"seq": 5,
			"role": "tool",
			"tool_status": "pending",
			"tool_call_id": "forged-token",
		}
		with _as(USER), self.assertRaises(frappe.PermissionError):
			create_doc(doctype=MSG, values=values)
		self.assertFalse(frappe.db.exists(MSG, {"tool_call_id": "forged-token"}))

	def test_forged_origin_insert_is_refused(self):
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": self.conv, "seq": 2, "role": "user", "origin": "board_answer"}
		)
		with _as(USER), self.assertRaises(frappe.PermissionError):
			doc.insert(ignore_permissions=True)

	def test_unflagged_plain_insert_succeeds_with_origin_unset(self):
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": self.conv, "seq": 3, "role": "assistant", "content": "hi"}
		)
		doc.insert(ignore_permissions=True)
		# The blank first option means no "human" default: unset ('' like tool_status).
		self.assertIn(frappe.db.get_value(MSG, doc.name, "origin"), (None, ""))

	def test_blank_guarded_values_count_as_unset(self):
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": 4,
				"role": "tool",
				"tool_status": "",
				"tool_call_id": None,
				"origin": "",
			}
		)
		doc.insert(ignore_permissions=True)  # no raise

	def test_save_that_leaves_guarded_fields_alone_passes(self):
		name = self._server_row(
			role="tool", tool_status="pending", tool_call_id="tok-c", pending_card='{"a": 1}'
		)
		doc = frappe.get_doc(MSG, name)
		doc.content = "edited"
		doc.save(ignore_permissions=True)  # no raise
		self.assertEqual(frappe.db.get_value(MSG, name, "content"), "edited")


class TestOrigin(_Base):
	def test_human_send_works_and_is_stamped_human(self):
		with _as(USER), _no_dispatch():
			res = chat_api.send_message(self.conv, "hello")
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "origin"), "human")

	def test_delegated_send_is_stamped_delegated(self):
		with _as(USER), _no_dispatch(), delegated_send():
			res = chat_api.send_message(self.conv, "from the scheduler")
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "origin"), "delegated")

	def test_message_origin_overrides_delegated(self):
		with _as(USER), _no_dispatch(), delegated_send(), message_origin("agent"):
			res = chat_api.send_message(self.conv, "seed")
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "origin"), "agent")
		self.assertIsNone(frappe.flags.get("jarvis_message_origin"))  # restored

	def test_origin_is_not_a_request_argument(self):
		with _as(USER), _no_dispatch(), self.assertRaises(TypeError):
			chat_api.send_message(self.conv, "hello", origin="board_answer")

	def test_forged_origin_over_http_is_dropped_and_stamped_human(self):
		from frappe.handler import execute_cmd
		from frappe.utils import set_request

		prev_request = getattr(frappe.local, "request", None)
		self.addCleanup(setattr, frappe.local, "request", prev_request)
		self.addCleanup(setattr, frappe.local, "form_dict", frappe.local.form_dict)
		set_request(method="POST", path="/api/method/jarvis.chat.api.send_message")
		with _as(USER), _no_dispatch():
			# set_user resets form_dict, so the forged body goes in after it.
			frappe.local.form_dict = frappe._dict(conversation=self.conv, message="hi", origin="board_answer")
			res = execute_cmd("jarvis.chat.api.send_message")
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "origin"), "human")

	def test_board_decide_resume_is_stamped_board_answer(self):
		with _as(USER):
			ap = frappe.get_doc(
				{
					"doctype": APPROVAL,
					"title": "p0a-ar",
					"status": "Pending",
					"conversation": self.conv,
					"question": "Which supplier?",
					"options": '["Acme","Globex"]',
					"source": "Chat",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
		from jarvis.chat.approvals_api import decide

		with _as(USER), _no_dispatch():
			res = decide(ap.name, "Acme", 1)
		self.assertTrue(res["resumed"])
		row = self._latest_user_row()
		self.assertIn("Acme", row.content)
		self.assertEqual(row.origin, "board_answer")

	def test_drop_file_is_stamped_file_box(self):
		from jarvis.chat.filebox import drop_file

		with _as(USER):
			f = frappe.get_doc(
				{"doctype": "File", "file_name": "p0a-invoice.txt", "content": "x", "is_private": 1}
			).insert(ignore_permissions=True)
			with _no_dispatch():
				res = drop_file(f.file_url, "p0a-invoice.txt")
		self.assertTrue(res["ok"])
		self.assertEqual(self._latest_user_row(res["conversation_id"]).origin, "file_box")

	def test_continuation_is_stamped_continuation(self):
		with _no_dispatch():
			out = chat_api.enqueue_continuation(self.conv, "created ToDo abc")
		row = frappe.db.get_value(MSG, out["message_id"], ["origin", "hidden"], as_dict=True)
		self.assertEqual(row.origin, "continuation")
		self.assertEqual(row.hidden, 1)

	def test_enqueue_turn_requires_an_origin(self):
		with _no_dispatch(), self.assertRaises(TypeError):
			chat_api._enqueue_turn(self.conv, "step", hidden=True)
		with _no_dispatch():
			out = chat_api._enqueue_turn(self.conv, "step", origin="macro", hidden=True)
		self.assertEqual(frappe.db.get_value(MSG, out["message_id"], "origin"), "macro")

	def test_macro_and_learning_callers_pass_their_origin(self):
		from jarvis.chat import macros
		from jarvis.learning import app_analysis

		step = frappe._dict(prompt="p1", model_override=None, thinking_override=None, skills=None)
		macro_doc = frappe._dict(steps=[step], name="m", owner=USER)
		run = frappe._dict(conversation=self.conv, name="r")
		with (
			patch("jarvis.chat.api._enqueue_turn", return_value={"overloaded": True}) as enq,
			patch.object(macros, "_defer_capacity"),
		):
			macros._run_step(run, macro_doc, 0)
			macros._run_merged(run, macro_doc, "merged")
		self.assertEqual([c.kwargs["origin"] for c in enq.call_args_list], ["macro", "macro"])

		lrun = frappe._dict(conversation=self.conv, app="x", zip_path="z", batches_total=1)
		with (
			patch("jarvis.chat.api._enqueue_turn", return_value={}) as enq,
			patch.object(app_analysis, "_legacy_retired", return_value=False),
			patch.object(app_analysis, "_load_notes", return_value={}),
			patch.object(app_analysis, "_final_prompt", return_value="final"),
			patch.object(app_analysis, "_clear_capacity_wait"),
		):
			app_analysis._send_final_turn(lrun)
		self.assertEqual(enq.call_args.kwargs["origin"], "system")


class TestWritersStillWrite(_Base):
	"""Each server writer sets ``flags.jarvis_server_write`` (one test each)."""

	def test_locked_insert_chat_message(self):
		name = api._locked_insert_chat_message(
			self.conv, {"role": "tool", "tool_status": "pending", "tool_call_id": "tok-lock"}
		)
		self.assertEqual(frappe.db.get_value(MSG, name, "tool_call_id"), "tok-lock")

	def test_persist_pending_action_and_receipt(self):
		api.persist_pending_action(self.conv, "delete_doc", {"card": {"verb": "delete"}}, "tok-pa", 9e9)
		self.assertTrue(frappe.db.exists(MSG, {"conversation": self.conv, "tool_call_id": "tok-pa"}))
		with patch("jarvis.api.publish_realtime_tool_result"):
			api.persist_tool_receipt(
				self.conv, "create_doc", {"doctype": "ToDo"}, {"ok": True}, tool_call_id="tc-rcpt"
			)
		self.assertTrue(frappe.db.exists(MSG, {"conversation": self.conv, "tool_call_id": "tc-rcpt"}))

	def test_pump_tool_start_row(self):
		name = pump._insert_tool_start_row(self.conv, "tc-pump", "browser_click")
		row = frappe.db.get_value(MSG, name, ["tool_status", "tool_call_id"], as_dict=True)
		self.assertEqual((row.tool_status, row.tool_call_id), ("running", "tc-pump"))

	def test_turn_handler_builtin_tool_start_row(self):
		by_call: dict[str, str] = {}
		with patch.object(turn_handler, "_publish_to_user"):
			turn_handler._handle_event_inner(
				{"kind": "tool", "phase": "start", "tool_name": "browser_click", "tool_call_id": "tc-th"},
				conversation_id=self.conv,
				assistant_msg_name="",
				tool_msg_by_call_id=by_call,
				user=USER,
				run_id="r-th",
				batcher=MagicMock(),
			)
		self.assertEqual(frappe.db.get_value(MSG, by_call["tc-th"], "tool_status"), "running")

	def test_actions_api_append_receipt(self):
		actions_api._append_receipt(self.conv, "create", "ToDo", "td-1", {"description": "x"}, "Created.")
		self.assertEqual(
			frappe.db.get_value(MSG, {"conversation": self.conv, "role": "tool"}, "action_outcome"),
			"confirmed",
		)

	def test_send_message_and_enqueue_turn_inserts(self):
		with _as(USER), _no_dispatch():
			human = chat_api.send_message(self.conv, "one")
			with delegated_send():
				delegated = chat_api.send_message(self.conv, "two")
		with _no_dispatch():
			enq = chat_api._enqueue_turn(self.conv, "three", origin="system", hidden=True)
		for name in (human["message_id"], delegated["message_id"], enq["message_id"]):
			self.assertTrue(frappe.db.exists(MSG, name))
