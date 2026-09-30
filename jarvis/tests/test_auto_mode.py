"""Tests for per-chat auto mode (#581): the Jarvis Conversation.auto_mode flag, the
controller guard, send_message's first-message-only set, get_conversation's payload
and the two Jarvis User Settings keys.

Hermetic in the same way as ``test_send_message_voice_flag``: a dedicated Jarvis User
fixture (never Administrator as the owner), ``frappe.enqueue`` patched out so no real
turn runs. send_message COMMITS, so the FrappeTestCase rollback cannot undo it: every
conversation, message and settings row a test creates is tracked and deleted in
``addCleanup`` (then committed). No test saves Jarvis Settings or pool rows.

Run ONLY this class (a bare --module can skip classes):
    bench --site <site> run-tests --app jarvis \\
        --module jarvis.tests.test_auto_mode --case TestAutoMode
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import admission, user_settings_api
from jarvis.chat.api import create_conversation, get_conversation, rename_conversation, send_message
from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
USETT = "Jarvis User Settings"
OWNER = "jarvis-automode-owner@example.test"


def _ensure_owner() -> None:
	"""Create the fixture user if missing (idempotent): a plain Jarvis User, so the
	guard is exercised for a non-admin owner."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", OWNER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": OWNER,
				"first_name": "Jarvis",
				"last_name": "AutoMode",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", OWNER)
	if JARVIS_USER_ROLE not in {r.role for r in doc.get("roles", [])}:
		doc.add_roles(JARVIS_USER_ROLE)
	frappe.db.commit()


def _send(conv, message="hello", **kwargs):
	"""Persist a user message without enqueueing a real turn."""
	with patch("frappe.enqueue"):
		return send_message(conv, message, **kwargs)


class TestAutoMode(FrappeTestCase):
	def setUp(self):
		_ensure_owner()
		self._orig_user = frappe.session.user
		self._convs: list[str] = []
		self.addCleanup(self._cleanup)
		frappe.set_user(OWNER)

	def _cleanup(self):
		frappe.set_user("Administrator")
		for name in self._convs:
			for child in frappe.get_all(
				MSG, filters={"conversation": name}, pluck="name"
			):  # test cleanup, system context
				frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
			if frappe.db.exists(CONV, name):
				frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(
			USETT, filters={"user": OWNER}, pluck="name"
		):  # test cleanup, system context
			frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		frappe.db.commit()
		frappe.set_user(self._orig_user)

	def _new_conv(self) -> str:
		name = create_conversation()
		self._convs.append(name)
		return name

	def _seed_message(self, conv: str) -> None:
		"""A finished message row, so the chat is no longer 'first message'."""
		msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 1,
				"role": "assistant",
				"content": "earlier reply",
				"streaming": 0,
			}
		)
		msg.flags.jarvis_server_write = True
		msg.flags.ignore_permissions = True
		msg.insert()
		frappe.db.commit()

	def test_first_human_send_with_auto_mode_sets_flag(self):
		conv = self._new_conv()
		res = _send(conv, auto_mode=1)
		self.assertTrue(res.get("ok"))
		self.assertEqual(res["auto_mode"], 1)
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 1)
		self.assertTrue(frappe.db.get_value(CONV, conv, "auto_mode_at"))

	def test_first_send_without_flag_leaves_it_off(self):
		conv = self._new_conv()
		res = _send(conv)
		self.assertTrue(res.get("ok"))
		self.assertEqual(res["auto_mode"], 0)
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 0)
		self.assertFalse(frappe.db.get_value(CONV, conv, "auto_mode_at"))

	def test_send_into_chat_with_messages_never_sets_flag(self):
		"""Review Focus 1: a second tab / raced reused-empty chat must not flip it."""
		conv = self._new_conv()
		self._seed_message(conv)
		res = _send(conv, auto_mode=1)
		self.assertTrue(res.get("ok"))
		self.assertEqual(res["auto_mode"], 0)
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 0)
		# ...and a chat that is already on stays on (0 stays 0, 1 stays 1).
		conv_on = self._new_conv()
		frappe.db.set_value(CONV, conv_on, "auto_mode", 1)
		self._seed_message(conv_on)
		res_on = _send(conv_on, auto_mode=0)
		self.assertEqual(res_on["auto_mode"], 1)
		self.assertEqual(frappe.db.get_value(CONV, conv_on, "auto_mode"), 1)

	def test_delegated_first_send_never_sets_flag(self):
		"""Review Focus 2: a delegated/background first message never arms it."""
		conv = self._new_conv()
		frappe.flags["jarvis_delegated_send"] = True
		try:
			res = _send(conv, auto_mode=1)
		finally:
			frappe.flags["jarvis_delegated_send"] = False
		self.assertTrue(res.get("ok"))
		self.assertEqual(res["auto_mode"], 0)
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 0)
		# A background send is not an interactive human either.
		conv_bg = self._new_conv()
		res_bg = _send(conv_bg, auto_mode=1, background=1)
		self.assertEqual(res_bg["auto_mode"], 0)
		self.assertEqual(frappe.db.get_value(CONV, conv_bg, "auto_mode"), 0)

	def test_guard_refuses_turning_off_for_owner_and_administrator(self):
		conv = self._new_conv()
		frappe.db.set_value(CONV, conv, "auto_mode", 1)
		frappe.db.commit()
		for user in (OWNER, "Administrator"):
			frappe.set_user(user)
			doc = frappe.get_doc(CONV, conv)
			doc.auto_mode = 0
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				doc.save(ignore_permissions=True)
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 1)

	def test_guard_refuses_enabling_through_save(self):
		conv = self._new_conv()
		doc = frappe.get_doc(CONV, conv)
		doc.auto_mode = 1
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			doc.save(ignore_permissions=True)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 0)
		# ...and an insert that arrives with auto_mode=1 is refused too.
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			frappe.get_doc({"doctype": CONV, "auto_mode": 1}).insert(ignore_permissions=True)

	def test_get_conversation_reports_auto_mode(self):
		conv = self._new_conv()
		self.assertEqual(get_conversation(conv)["conversation"]["auto_mode"], 0)
		frappe.db.set_value(CONV, conv, "auto_mode", 1)
		self.assertEqual(get_conversation(conv)["conversation"]["auto_mode"], 1)

	def test_stale_doc_rename_keeps_auto_mode(self):
		"""The rename path loads the doc, then sync_file_box_fields re-reads the
		server-stamped fields: a stale in-memory 0 must not trip the guard or revert
		a flag stamped behind it."""
		conv = self._new_conv()
		stale = frappe.get_doc(CONV, conv)
		self.assertEqual(stale.auto_mode, 0)
		frappe.db.set_value(CONV, conv, "auto_mode", 1)
		frappe.db.commit()
		stale.title = "renamed"
		stale.sync_file_box_fields()
		self.assertEqual(stale.auto_mode, 1)
		# The real endpoint reloads the doc, so it must succeed and keep the flag.
		res = rename_conversation(conv, "Renamed chat")
		self.assertTrue(res.get("ok"))
		self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 1)
		self.assertEqual(frappe.db.get_value(CONV, conv, "title"), "Renamed chat")

	def test_user_settings_round_trip(self):
		before = user_settings_api.get_my_settings()["data"]
		self.assertEqual(before["default_auto_mode"], 0)
		self.assertEqual(before["auto_mode_acknowledged"], 0)
		out = user_settings_api.update_my_settings(default_auto_mode=1, auto_mode_acknowledged=1)
		self.assertEqual(out["data"]["default_auto_mode"], 1)
		self.assertEqual(out["data"]["auto_mode_acknowledged"], 1)
		after = user_settings_api.get_my_settings()["data"]
		self.assertEqual(after["default_auto_mode"], 1)
		self.assertEqual(after["auto_mode_acknowledged"], 1)
		# An omitted key leaves the stored value alone.
		user_settings_api.update_my_settings(notify_enabled=1)
		self.assertEqual(user_settings_api.get_my_settings()["data"]["default_auto_mode"], 1)

	def test_enabling_the_default_records_the_acknowledgement(self):
		# The default pre-arms new chats with no dialog, so it can never be on while the
		# one-time warning is unacknowledged, whatever the caller sent.
		out = user_settings_api.update_my_settings(default_auto_mode=1)
		self.assertEqual(out["data"]["default_auto_mode"], 1)
		self.assertEqual(out["data"]["auto_mode_acknowledged"], 1)
		out = user_settings_api.update_my_settings(auto_mode_acknowledged=0)
		self.assertEqual(out["data"]["auto_mode_acknowledged"], 1, "still on while the default is on")

	def _overloaded_send(self, conv, branch, **kwargs):
		"""A send that admission rejects as overloaded, through either cleanup branch:
		'machine' (accept_or_queue) or 'legacy' (the _dispatch_turn reroute)."""
		rejected = {"ok": False, "overloaded": True, "reason": "busy"}
		if branch == "machine":
			with (
				patch.object(admission, "turn_machine_enabled", return_value=True),
				patch.object(admission, "shard_overloaded", return_value=False),
				patch.object(admission, "accept_or_queue", return_value=rejected),
			):
				return _send(conv, **kwargs)
		with (
			patch.object(admission, "turn_machine_enabled", return_value=False),
			patch("jarvis.chat.api._dispatch_turn", return_value=rejected),
		):
			return _send(conv, **kwargs)

	def test_overload_rejected_first_send_undoes_auto_mode(self):
		"""The rejected first send deletes its seed message, so the chat never started:
		auto mode must not stay on an empty row create_or_focus_empty would reuse."""
		for branch in ("machine", "legacy"):
			with self.subTest(branch=branch):
				conv = self._new_conv()
				res = self._overloaded_send(conv, branch, auto_mode=1)
				self.assertFalse(res.get("ok"))
				self.assertEqual(frappe.db.count(MSG, {"conversation": conv}), 0)
				self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 0)
				self.assertIsNone(frappe.db.get_value(CONV, conv, "auto_mode_at"))

	def test_overload_reject_keeps_auto_mode_set_earlier(self):
		"""A chat already in auto mode with a message: an overloaded later send must not
		touch the flag (this send did not set it)."""
		for branch in ("machine", "legacy"):
			with self.subTest(branch=branch):
				conv = self._new_conv()
				frappe.db.set_value(CONV, conv, "auto_mode", 1)
				self._seed_message(conv)
				res = self._overloaded_send(conv, branch, auto_mode=1)
				self.assertFalse(res.get("ok"))
				self.assertEqual(frappe.db.count(MSG, {"conversation": conv}), 1)
				self.assertEqual(frappe.db.get_value(CONV, conv, "auto_mode"), 1)

	def test_new_message_clears_stale_halt_in_auto_mode(self):
		# A Stop on a reply that made no write leaves the 120s run-cancel signal set; the
		# next message in an auto-mode chat must not have its first covered write refused.
		from jarvis.chat import turn_message_binding

		conv = self._new_conv()
		frappe.db.set_value(CONV, conv, "auto_mode", 1)
		self._seed_message(conv)
		turn_message_binding.request_run_cancel(conv)
		self.addCleanup(turn_message_binding.clear_run_cancel, conv)
		res = _send(conv, "create a todo")
		self.assertTrue(res.get("ok"))
		self.assertFalse(turn_message_binding.is_run_cancel_requested(conv))

	def test_new_message_keeps_halt_in_a_normal_chat(self):
		# Unchanged outside auto mode: a normal chat still parks cards, so its signal is
		# left to the gate that consumes it.
		from jarvis.chat import turn_message_binding

		conv = self._new_conv()
		self._seed_message(conv)
		turn_message_binding.request_run_cancel(conv)
		self.addCleanup(turn_message_binding.clear_run_cancel, conv)
		res = _send(conv, "hello again")
		self.assertTrue(res.get("ok"))
		self.assertTrue(turn_message_binding.is_run_cancel_requested(conv))
