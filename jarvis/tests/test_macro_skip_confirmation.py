"""Tests for admin-armed macro "skip confirmation".

An admin arms a Jarvis Macro (``skip_confirmation``); its runs then execute the
covered gated writes without a confirmation card. The single source of truth the
write-confirmation gate reads is ``Jarvis Conversation.skip_confirmation``, stamped
onto an armed macro's run conversation. This module covers the admin guards on both
flags (this file), the gate branch, the run stamp + human-inert block, the
clear-on-terminal, and D5 stop-and-report.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.api import _ARMED_SKIP_COVERED, _ARMED_SKIP_NEVER, _GATED_WRITES
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_auto_apply import (
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user

CONV = "Jarvis Conversation"
MACRO = "Jarvis Macro"

# A Jarvis Admin who is NOT a System Manager - proves the guard admits the
# Jarvis Admin tier specifically, not only System Manager (edge-case review).
ADMIN_USER = "jarvis-skip-admin@example.com"


def _ensure_admin_user() -> None:
	"""A Jarvis Admin (+ Jarvis User so it can own/write a macro), NOT System Manager."""
	ensure_jarvis_user_role()
	if not frappe.db.exists("User", ADMIN_USER):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": ADMIN_USER,
				"first_name": "Skip",
				"last_name": "Admin",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		doc.insert(ignore_permissions=True)
	user = frappe.get_doc("User", ADMIN_USER)
	roles = set(frappe.get_roles(ADMIN_USER))
	if "System Manager" in roles:
		user.remove_roles("System Manager")
	for role in ("Jarvis Admin", "Jarvis User"):
		if role not in roles:
			user.add_roles(role)  # add_roles auto-vivifies a missing Role row
	frappe.db.commit()


def _make_macro(owner: str, *, armed: bool = False, name: str = "skip-conf test macro") -> str:
	"""Create a Jarvis Macro owned by ``owner`` with one step; return its name.
	``armed`` stamps skip_confirmation via a raw db write (bypassing the guard),
	simulating an already-armed macro."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"steps": [{"prompt": "do the thing"}],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		if armed:
			frappe.db.set_value(MACRO, doc.name, "skip_confirmation", 1, update_modified=False)
			frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestConversationSkipConfirmationGuard(FrappeTestCase):
	"""The LOAD-BEARING guard: the gate reads Jarvis Conversation.skip_confirmation,
	which is owner-writable with no permlevel. A non-admin owner must not flip it
	0 -> 1 through a generic save; only a Jarvis Admin / System Manager may."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)

	def test_non_admin_owner_save_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0), 0)

	def test_jarvis_admin_save_can_enable(self):
		conv = _make_conv(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)

	def test_non_admin_owner_save_can_disable(self):
		conv = _make_conv(NON_ADMIN_USER)
		# Server stamped it on (the run_macro raw path), then the owner disarms.
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.skip_confirmation = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0), 0)

	def test_raw_stamp_bypasses_the_guard(self):
		"""The sanctioned run_macro path (raw db.set_value) sets the flag on a
		non-admin owner's conversation without tripping the controller guard."""
		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)


class TestMacroSkipConfirmationGuard(FrappeTestCase):
	"""Arming a macro (Jarvis Macro.skip_confirmation 0 -> 1) requires admin;
	editing steps while armed keeps it armed (D6 trust model)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()
		_ensure_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for owner in (NON_ADMIN_USER, ADMIN_USER):
			for name in frappe.get_all(MACRO, filters={"owner": owner}, pluck="name"):
				frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_cannot_arm(self):
		macro = _make_macro(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation") or 0), 0)

	def test_jarvis_admin_can_arm_own_macro(self):
		macro = _make_macro(ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation")), 1)

	def test_editing_steps_keeps_arm_for_non_admin_owner(self):
		"""D6 trust model: an admin armed the macro; its NON-admin owner can later
		edit the steps (a no-op 1 -> 1 transition on skip_confirmation) without admin
		rights and without disarming - arming trusts the owner for future steps too."""
		macro = _make_macro(NON_ADMIN_USER, armed=True)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.steps[0].prompt = "do a different thing"
		doc.save()  # must not raise; arm persists
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation")), 1)

	def test_owner_can_disarm(self):
		macro = _make_macro(ADMIN_USER, armed=True)
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(MACRO, macro)
		doc.skip_confirmation = 0
		doc.save()
		self.assertEqual(int(frappe.db.get_value(MACRO, macro, "skip_confirmation") or 0), 0)


class TestArmedSkipPartition(FrappeTestCase):
	"""Fail-closed allowlist: COVERED and NEVER partition _GATED_WRITES exactly.
	A new gated tool added to neither set makes this RED, forcing a conscious
	skip-vs-park classification instead of a silent fail-open default."""

	def test_covered_and_never_partition_gated_writes(self):
		self.assertEqual(_ARMED_SKIP_COVERED & _ARMED_SKIP_NEVER, frozenset(), "sets must be disjoint")
		self.assertEqual(
			_ARMED_SKIP_COVERED | _ARMED_SKIP_NEVER,
			_GATED_WRITES,
			"every gated write must be classified as covered (skips when armed) or "
			"never (always parks) - a tool in neither is an unclassified fail-open gap",
		)

	def test_irreversible_trio_never_skips(self):
		self.assertEqual(_ARMED_SKIP_NEVER, frozenset({"cancel_doc", "delete_doc", "amend_doc"}))

	def test_run_method_is_covered(self):
		# run_method gates in ordinary chat but skips inside an armed macro (D5).
		self.assertIn("run_method", _ARMED_SKIP_COVERED)


class TestArmedSkipGate(FrappeTestCase):
	"""The gate: an armed conversation (skip_confirmation=1) runs the covered set
	uncarded; the irreversible trio still parks; the kill switch re-gates."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.db.set_value("Jarvis Settings", None, "disable_armed_skip", 0, update_modified=False)
		frappe.set_user(self._orig)

	def _armed_conv(self) -> str:
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		return conv

	def test_run_method_runs_when_armed(self):
		conv = self._armed_conv()
		with patch("jarvis.api.dispatch", return_value={"ok": True}) as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertTrue(disp.called, "armed run_method must reach dispatch, not park")
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")

	def test_run_method_parks_when_not_armed(self):
		conv = _make_conv(TEST_USER)  # skip_confirmation defaults 0
		with patch("jarvis.api.dispatch") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(disp.called)

	def test_send_email_runs_when_armed(self):
		conv = self._armed_conv()
		with patch("jarvis.api.dispatch", return_value={"ok": True}) as disp:
			r = api._run_tool(
				"send_email",
				{"recipients": "x@example.com", "subject": "s", "content": "b"},
				conversation=conv,
			)
		self.assertTrue(disp.called)
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")

	def test_delete_doc_still_parks_when_armed(self):
		conv = self._armed_conv()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "skip-del-x"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "delete always parks")
		self.assertTrue(frappe.db.exists("ToDo", todo.name))

	def test_kill_switch_regates(self):
		conv = self._armed_conv()
		frappe.db.set_value("Jarvis Settings", None, "disable_armed_skip", 1, update_modified=False)
		with patch("jarvis.api.dispatch") as disp:
			r = api._run_tool("run_method", {"method": "frappe.ping"}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "kill switch must re-gate")
		self.assertFalse(disp.called)

	def test_bulk_covered_runs_when_armed(self):
		conv = self._armed_conv()
		with patch("jarvis.api.dispatch", return_value={"ok": True}) as disp:
			r = api._run_tool(
				"create_docs",
				{"docs": [{"doctype": "ToDo", "values": {"description": "b1"}}]},
				conversation=conv,
			)
		self.assertTrue(disp.called, "a bulk covered write skips the batch card when armed")
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")


class TestRunMacroStampAndInert(FrappeTestCase):
	"""run_macro stamps the run conversation when the macro is armed; the armed
	conversation is human-inert (send_message / retry_message refuse it), keyed on
	the flag itself so deleting the Macro Run row cannot re-open it."""

	MSG = "Jarvis Chat Message"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for dt in ("Jarvis Macro Run", MACRO, CONV):
			for name in frappe.get_all(dt, filters={"owner": NON_ADMIN_USER}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _run(self, macro_name: str) -> str:
		from jarvis.chat import macros

		frappe.set_user(NON_ADMIN_USER)
		with (
			patch.object(macros, "_run_step"),
			patch.object(macros, "_run_merged"),
			patch.object(macros, "entitlement_block", return_value=None),
		):
			res = macros.run_macro(macro_name)
		return res["data"]["conversation"]

	def test_run_macro_stamps_armed_conversation(self):
		macro = _make_macro(NON_ADMIN_USER, armed=True, name="armed-run")
		conv = self._run(macro)
		# A non-admin owner ran their own admin-armed macro: the raw stamp set the
		# flag without tripping the conversation controller guard.
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation")), 1)

	def test_run_macro_unarmed_does_not_stamp(self):
		macro = _make_macro(NON_ADMIN_USER, armed=False, name="plain-run")
		conv = self._run(macro)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0), 0)

	def test_send_message_rejected_on_armed_conversation(self):
		from jarvis.chat import api as chat_api

		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		with patch.object(chat_api, "validate_can_send", return_value=(True, None)):
			with self.assertRaises(frappe.ValidationError):
				chat_api.send_message(conversation=conv, message="let me in")

	def test_send_message_allowed_after_disarm(self):
		"""Disarming (flag -> 0) re-opens the conversation to interactive sends."""
		from jarvis.chat import api as chat_api

		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 0, update_modified=False)
		frappe.set_user(NON_ADMIN_USER)
		# The reject helper must NOT fire on an unarmed conversation.
		doc = chat_api._get_owned_conversation(conv)
		chat_api._reject_send_into_armed_conversation(doc)  # no raise

	def test_armed_conversation_with_no_run_row_still_rejects(self):
		"""The block keys on the conversation flag, NOT on a Jarvis Macro Run row, so
		deleting the run (owner has delete rights) cannot re-open an armed conv."""
		from jarvis.chat import api as chat_api

		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		self.assertFalse(frappe.db.exists("Jarvis Macro Run", {"conversation": conv}))
		frappe.set_user(NON_ADMIN_USER)
		doc = chat_api._get_owned_conversation(conv)
		with self.assertRaises(frappe.ValidationError):
			chat_api._reject_send_into_armed_conversation(doc)

	def test_retry_message_rejected_on_armed_conversation(self):
		from jarvis.chat import api as chat_api

		conv = _make_conv(NON_ADMIN_USER)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		msg = frappe.get_doc(
			{
				"doctype": self.MSG,
				"conversation": conv,
				"seq": 2,
				"role": "assistant",
				"content": "boom",
				"error": "some error",
			}
		)
		msg.flags.ignore_permissions = True
		msg.insert()
		frappe.db.commit()
		frappe.set_user(NON_ADMIN_USER)
		with patch.object(chat_api, "validate_can_send", return_value=(True, None)):
			with self.assertRaises(frappe.ValidationError):
				chat_api.retry_message(msg.name)


class TestClearOnTerminal(FrappeTestCase):
	"""The armed flag is cleared on EVERY run-terminal transition (T5): _finish,
	stop_macro_run, and any _cas terminal (reap / resume-compensate); a non-terminal
	_cas transition leaves it armed."""

	RUN = "Jarvis Macro Run"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for dt in ("Jarvis Macro Run", MACRO, CONV):
			for name in frappe.get_all(dt, filters={"owner": NON_ADMIN_USER}, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _make_run(self, *, armed: bool = True, status: str = "running", name: str = "term-macro"):
		macro = _make_macro(NON_ADMIN_USER, name=name)
		frappe.set_user(NON_ADMIN_USER)
		conv = frappe.get_doc({"doctype": CONV, "title": "run conv"})
		conv.flags.ignore_permissions = True
		conv.insert()
		if armed:
			frappe.db.set_value(CONV, conv.name, "skip_confirmation", 1, update_modified=False)
		run = frappe.get_doc(
			{
				"doctype": self.RUN,
				"macro": macro,
				"conversation": conv.name,
				"status": status,
				"current_step": 0,
				"total_steps": 1,
				"trigger": "manual",
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.commit()
		return run.name, conv.name

	def _armed(self, conv) -> int:
		return int(frappe.db.get_value(CONV, conv, "skip_confirmation") or 0)

	def test_finish_clears_flag(self):
		from jarvis.chat import macros

		run_name, conv = self._make_run(name="fin-macro")
		macros._finish(frappe.get_doc(self.RUN, run_name), "completed")
		self.assertEqual(self._armed(conv), 0)

	def test_stop_macro_run_clears_flag(self):
		from jarvis.chat import macros

		run_name, conv = self._make_run(name="stop-macro")
		frappe.set_user(NON_ADMIN_USER)
		macros.stop_macro_run(run_name)
		self.assertEqual(self._armed(conv), 0)

	def test_cas_terminal_clears_flag(self):
		from jarvis.chat import macros

		run_name, conv = self._make_run(name="cas-macro")
		self.assertTrue(macros._cas_run_status(run_name, "running", "failed"))
		frappe.db.commit()
		self.assertEqual(self._armed(conv), 0)

	def test_cas_nonterminal_keeps_flag(self):
		from jarvis.chat import macros

		run_name, conv = self._make_run(name="cas2-macro")
		self.assertTrue(macros._cas_run_status(run_name, "running", "waiting_capacity"))
		frappe.db.commit()
		self.assertEqual(self._armed(conv), 1, "a non-terminal transition must not disarm")
