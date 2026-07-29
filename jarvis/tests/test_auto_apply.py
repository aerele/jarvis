"""Tests for per-conversation, admin-gated auto-apply (issue #186, Task 4).

Auto-apply moved from an ungated site-wide switch to a per-conversation flag
(``Jarvis Conversation.auto_apply``) that:
  - Requires the System Manager role to ENABLE (disabling is always allowed).
  - Is owner-scoped: you can only toggle your own conversation.
  - When ON, fast-paths the write-safety gate for REVERSIBLE writes only -
	destructive tools (delete/cancel/amend/send_email) STILL park.
  - Only trusts the acting user's OWN matching conversation.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import openclaw_session_pool
from jarvis.chat.api import create_conversation, send_message, set_auto_apply
from jarvis.chat.worker import run_agent_turn
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)

CONV = "Jarvis Conversation"

# A plain System User WITHOUT System Manager, used for the non-admin cases.
NON_ADMIN_USER = "jarvis-autoapply-plain@example.com"


def _ensure_non_admin_user() -> None:
	"""Create a plain Jarvis chat user: has the "Jarvis User" app-access role (as
	every real chat user does, granted at onboarding/migration) but NOT System
	Manager — so the gating tests still exercise the non-admin path while getting
	past the chat-API app-access gate. Idempotent."""
	ensure_jarvis_user_role()  # the test site may not have run after_migrate
	if frappe.db.exists("User", NON_ADMIN_USER):
		if "System Manager" in frappe.get_roles(NON_ADMIN_USER):
			frappe.get_doc("User", NON_ADMIN_USER).remove_roles("System Manager")
			frappe.db.commit()
		if "Jarvis User" not in frappe.get_roles(NON_ADMIN_USER):
			frappe.get_doc("User", NON_ADMIN_USER).add_roles("Jarvis User")
			frappe.db.commit()
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": NON_ADMIN_USER,
			"first_name": "Plain",
			"last_name": "User",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("Jarvis User")
	frappe.db.commit()


def _make_conv(owner: str) -> str:
	"""Create a Jarvis Conversation owned by ``owner`` and return its name."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc({"doctype": CONV, "title": "auto-apply test"})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestSetAutoApplyGating(FrappeTestCase):
	"""set_auto_apply: owner-scoped + admin-gated to enable."""

	def setUp(self):
		_ensure_test_user()  # TEST_USER has System Manager
		_ensure_non_admin_user()
		self._orig_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig_user)
		_cleanup_user_conversations(TEST_USER)
		_cleanup_user_conversations(NON_ADMIN_USER)

	def test_non_admin_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		with self.assertRaises(frappe.PermissionError):
			set_auto_apply(conv, 1)
		# The flag must stay 0 - the write never happened.
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)

	def test_admin_enables_own_conversation(self):
		conv = _make_conv(TEST_USER)
		frappe.set_user(TEST_USER)
		res = set_auto_apply(conv, 1)
		self.assertTrue(res["ok"])
		self.assertEqual(res["data"]["auto_apply"], 1)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply")), 1)

	def test_owner_check_precedes_admin_check(self):
		# Conversation owned by the non-admin; TEST_USER (an admin) tries to
		# enable it -> PermissionError for NOT being the owner, even though the
		# caller has System Manager.
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(TEST_USER)
		with self.assertRaises(frappe.PermissionError):
			set_auto_apply(conv, 1)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)

	def test_disable_allowed_for_owner_without_admin(self):
		# Owner (non-admin) turning it OFF is always allowed - no admin needed.
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		res = set_auto_apply(conv, 0)
		self.assertTrue(res["ok"])
		self.assertEqual(res["data"]["auto_apply"], 0)
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)

	def test_unknown_conversation_raises(self):
		frappe.set_user(TEST_USER)
		with self.assertRaises(frappe.DoesNotExistError):
			set_auto_apply("no-such-conversation-zzz", 1)


class TestAutoApplyControllerGuard(FrappeTestCase):
	"""Doctype-layer backstop: a non-admin owner cannot flip ``auto_apply``
	0 -> 1 through a generic ``doc.save()`` path (update_doc, frappe.client,
	desk) that never touches ``set_auto_apply``. This is the defense-in-depth
	fix for the exploit where a non-admin owner asks the agent to
	``update_doc("Jarvis Conversation", <their conv>, {"auto_apply": 1})``
	and self-confirms it - the confirmed write runs as the owner and used to
	succeed via if_owner, since only ``set_auto_apply`` was gated.
	"""

	def setUp(self):
		_ensure_test_user()  # TEST_USER has System Manager
		_ensure_non_admin_user()
		self._orig_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig_user)
		_cleanup_user_conversations(TEST_USER)
		_cleanup_user_conversations(NON_ADMIN_USER)

	def test_non_admin_owner_save_cannot_enable(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.auto_apply = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)

	def test_system_manager_save_can_enable(self):
		conv = _make_conv(TEST_USER)
		frappe.set_user(TEST_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.auto_apply = 1
		doc.save()
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply")), 1)

	def test_non_admin_owner_save_can_disable(self):
		conv = _make_conv(NON_ADMIN_USER)
		# Enable directly at the DB layer (bypasses the controller, same as
		# an admin-approved set_auto_apply call would have done).
		frappe.db.set_value(CONV, conv, "auto_apply", 1, update_modified=False)
		frappe.db.commit()
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.auto_apply = 0
		doc.save()  # disabling never requires System Manager
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply")), 0)

	def test_non_admin_owner_unrelated_edit_not_blocked(self):
		# A save that leaves auto_apply unchanged (still 0) must not be
		# blocked - no false positive on ordinary edits like the title.
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.title = "renamed by owner"
		doc.save()  # should not raise
		self.assertEqual(frappe.db.get_value(CONV, conv, "title"), "renamed by owner")
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)

	def test_set_auto_apply_admin_enable_still_works(self):
		# Regression: set_auto_apply's frappe.db.set_value path bypasses the
		# controller entirely and must be unaffected by this change.
		conv = _make_conv(TEST_USER)
		frappe.set_user(TEST_USER)
		res = set_auto_apply(conv, 1)
		self.assertTrue(res["ok"])
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply")), 1)


class TestGateAutoApplyBypass(FrappeTestCase):
	"""The gate's auto-apply bypass in ``jarvis.api._run_tool``."""

	def setUp(self):
		_ensure_test_user()
		_ensure_non_admin_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig_user)
		_cleanup_user_conversations(TEST_USER)
		_cleanup_user_conversations(NON_ADMIN_USER)
		for name in frappe.get_all(
			"ToDo",
			filters={"description": ("like", "jarvis-autoapply-%")},
			pluck="name",
		):
			frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _enable(self, conv: str) -> None:
		frappe.db.set_value(CONV, conv, "auto_apply", 1, update_modified=False)
		frappe.db.commit()

	def test_reversible_write_executes_immediately_when_on(self):
		conv = _make_conv(TEST_USER)
		self._enable(conv)
		desc = "jarvis-autoapply-exec-001"
		r = api._run_tool(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": desc},
			},
			conversation=conv,
		)
		self.assertTrue(r["ok"])
		# NOT parked - the row exists and there is no pending status.
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

	def test_update_doc_executes_immediately_when_on(self):
		conv = _make_conv(TEST_USER)
		self._enable(conv)
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-autoapply-upd-006",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		r = api._run_tool(
			"update_doc",
			{
				"doctype": "ToDo",
				"name": todo.name,
				"changes": {"description": "jarvis-autoapply-upd-006-changed"},
			},
			conversation=conv,
		)
		self.assertTrue(r["ok"])
		# create/update are the ONLY auto-applyable tools -> fast-path, no park.
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")
		self.assertEqual(
			frappe.db.get_value("ToDo", todo.name, "description"),
			"jarvis-autoapply-upd-006-changed",
		)

	def test_submit_doc_still_parks_when_on(self):
		# submit_doc is reversible-ish but NOT in _AUTO_APPLYABLE: it always
		# parks, even with auto_apply ON. (Previously it fast-pathed as a
		# non-destructive write - the Fix 1 tightening closes that.)
		conv = _make_conv(TEST_USER)
		self._enable(conv)
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-autoapply-submit-007",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		r = api._run_tool(
			"submit_doc",
			{
				"doctype": "ToDo",
				"name": todo.name,
			},
			conversation=conv,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")

	def test_run_method_still_parks_when_on(self):
		# run_method NEVER fast-paths under auto_apply (default-unrestricted
		# allowlist + injection = unconfirmed arbitrary whitelisted call). It
		# parks, and (Fix 2) its preview is described-only: dispatch is never
		# called to build the park preview.
		conv = _make_conv(TEST_USER)
		self._enable(conv)
		with patch("jarvis.api.dispatch") as disp:
			r = api._run_tool(
				"run_method",
				{
					"method": "frappe.ping",
				},
				conversation=conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(disp.called)

	def test_destructive_write_still_parks_when_on(self):
		conv = _make_conv(TEST_USER)
		self._enable(conv)
		# Something to (attempt to) delete.
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-autoapply-del-002",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		r = api._run_tool(
			"delete_doc",
			{
				"doctype": "ToDo",
				"name": todo.name,
			},
			conversation=conv,
		)
		# Destructive tools ALWAYS park, even with auto_apply ON.
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertTrue(frappe.db.exists("ToDo", todo.name))

	def test_reversible_write_parks_when_off(self):
		conv = _make_conv(TEST_USER)  # auto_apply defaults to 0
		desc = "jarvis-autoapply-off-003"
		r = api._run_tool(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": desc},
			},
			conversation=conv,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

	def test_auto_apply_fires_when_actor_differs_from_owner(self):
		# #5: the bypass compares auto_apply against owner_user (the CONVERSATION
		# OWNER) rather than the acting session user, so it fires when the acting
		# user differs from the owner who enabled auto-apply. Modelled here by
		# acting as a DIFFERENT user than the conversation owner: the
		# reversible write fast-paths (reaches dispatch, does not park) and runs
		# under the acting/exec user's scope. dispatch is spied so the test is
		# not coupled to the exec user's DocType permissions.
		conv = _make_conv(TEST_USER)  # owner (operator) enabled auto-apply
		self._enable(conv)
		acting = {}

		def _spy(tool, args):
			acting["user"] = frappe.session.user
			acting["tool"] = tool
			return {"name": "TODO-FAKE"}

		frappe.set_user(NON_ADMIN_USER)  # the distinct exec/tool user
		try:
			with patch("jarvis.api.dispatch", side_effect=_spy):
				r = api._run_tool(
					"create_doc",
					{
						"doctype": "ToDo",
						"values": {"description": "cross-004"},
					},
					conversation=conv,
				)
		finally:
			frappe.set_user(TEST_USER)
		# Fast-pathed to execution (not parked) - owner_user enabled the flag.
		self.assertTrue(r["ok"])
		self.assertNotEqual(r["data"].get("status"), "pending_confirmation")
		self.assertEqual(acting["tool"], "create_doc")
		# Ran under the acting/exec user, not the owner.
		self.assertEqual(acting["user"], NON_ADMIN_USER)

	def test_bypass_off_when_conversation_empty(self):
		# No conversation binding -> conv resolves to "" -> auto_apply cannot be
		# trusted -> parks.
		desc = "jarvis-autoapply-empty-005"
		r = api._run_tool(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": desc},
			},
			conversation=None,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))


class TestTurnContextReflectsFlag(FrappeTestCase):
	"""The turn_handler [Context: ...] line reflects THIS conversation's flag."""

	def setUp(self):
		openclaw_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations(TEST_USER)
		self.conv = create_conversation()
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				result = send_message(self.conv, "make a todo please")
		# Pre-seed a session_key so the worker's pool path skips the real
		# _ensure_session_key handshake (turn_handler.py:631 guard).
		frappe.db.set_value(CONV, self.conv, "session_key", "agent:fake")
		frappe.db.commit()
		self.user_msg = result["message_id"]

	def tearDown(self):
		_cleanup_user_conversations(TEST_USER)
		frappe.set_user(self._orig_user)

	def _capture_message_sent(self) -> str:
		"""Run one worker turn against a mocked pooled session and return the
		``user_message`` (positional arg 1) passed to ``sess.chat_send`` - the
		string carrying the [Context: ...] bracket the worker builds."""
		fake_sess = MagicMock()
		fake_sess.get_session_messages.return_value = []
		# status ok -> the worker treats it as a completed replay and skips the
		# relay stream; chat_send has already been called with the message.
		fake_sess.chat_send.return_value = {"status": "ok"}

		@contextmanager
		def _fake_checkout(url):
			yield fake_sess

		with patch("jarvis.chat.openclaw_session_pool.checkout", _fake_checkout):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		first = fake_sess.chat_send.call_args_list[0]
		pos = first.args
		return pos[1] if len(pos) >= 2 else first.kwargs.get("message")

	def test_context_line_on_when_flag_set(self):
		frappe.db.set_value(CONV, self.conv, "auto_apply", 1, update_modified=False)
		frappe.db.commit()
		msg = self._capture_message_sent()
		self.assertIn("auto-apply changes: ON", msg)

	def test_context_line_absent_when_flag_clear(self):
		frappe.db.set_value(CONV, self.conv, "auto_apply", 0, update_modified=False)
		frappe.db.commit()
		msg = self._capture_message_sent()
		self.assertNotIn("auto-apply changes: ON", msg)
