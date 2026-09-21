"""Tests for the REMOVAL of admin Auto-Apply (action-card overhaul, design A2).

Admin "Auto-Apply" (Jarvis Conversation.auto_apply) let reversible create/update
tool calls run without a confirmation card. It is removed: every real change now
asks, and request-scoped "confirm all" is the user-facing replacement. This suite
proves the bypass is gone, File Box (the one remaining direct-apply path) still
works, the retirement patch flips stale flags + posts a one-time teaching notice,
and the set_auto_apply endpoint no longer exists.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user


class TestAutoApplyRemoved(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_stale_auto_apply_flag_no_longer_bypasses_the_gate(self):
		# Even with auto_apply=1 lingering on the row, a reversible create must PARK -
		# the bypass that read this flag is gone.
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "auto_apply", 1, update_modified=False)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "auto-apply-removed-park"}},
				conversation=conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a stale auto_apply must not fast-path")
		self.assertFalse(disp.called, "nothing runs uncarded")
		self.assertFalse(frappe.db.exists("ToDo", {"description": "auto-apply-removed-park"}))

	def test_file_box_still_fast_paths_create(self):
		# File Box is the ONE remaining direct-apply path and must keep working.
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "file_box", 1, update_modified=False)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
			api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "file-box-fast-path"}},
				conversation=conv,
			)
		self.assertTrue(disp.called, "File Box still fast-paths a reversible create")
		self.assertEqual(disp.call_args.kwargs.get("provenance"), "auto_apply")

	def test_set_auto_apply_endpoint_is_removed(self):
		from jarvis.chat import api as chat_api

		self.assertFalse(hasattr(chat_api, "set_auto_apply"), "the endpoint must be gone")

	def test_retirement_patch_flips_flag_and_posts_one_notice(self):
		from jarvis.patches import v2_20_retire_admin_auto_apply as retire

		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "auto_apply", 1, update_modified=False)
		frappe.db.commit()

		retire.execute()

		# Flag flipped, notice posted.
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "auto_apply") or 0), 0)
		notices = frappe.get_all(
			"Jarvis Chat Message",
			filters={
				"conversation": conv,
				"role": "assistant",
				"content": ["like", "%Auto-Apply has been turned off%"],
			},
			pluck="name",
		)
		self.assertEqual(len(notices), 1, "exactly one teaching notice")

		# Idempotent: re-running posts no duplicate (the selector is now empty).
		retire.execute()
		notices2 = frappe.get_all(
			"Jarvis Chat Message",
			filters={
				"conversation": conv,
				"role": "assistant",
				"content": ["like", "%Auto-Apply has been turned off%"],
			},
			pluck="name",
		)
		self.assertEqual(len(notices2), 1, "re-running the patch must not duplicate the notice")

	def test_retirement_patch_flips_archived_conv_without_a_notice(self):
		from jarvis.patches import v2_20_retire_admin_auto_apply as retire

		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, {"auto_apply": 1, "status": "Archived"}, update_modified=False)
		frappe.db.commit()

		retire.execute()

		self.assertEqual(
			int(frappe.db.get_value(CONV, conv, "auto_apply") or 0),
			0,
			"the stale flag is flipped even on an archived thread (hygiene + idempotency)",
		)
		notices = frappe.get_all(
			"Jarvis Chat Message",
			filters={
				"conversation": conv,
				"role": "assistant",
				"content": ["like", "%Auto-Apply has been turned off%"],
			},
			pluck="name",
		)
		self.assertEqual(len(notices), 0, "an archived thread its owner may never reopen gets no notice")

	def test_retirement_patch_appends_notice_onto_a_populated_transcript(self):
		# Coverage gap (review): the happy-path test uses a fresh conv (seq=1). Prove the
		# MAX(seq)+1 append lands on a conversation that already has messages.
		from jarvis.patches import v2_20_retire_admin_auto_apply as retire

		conv = _make_conv(TEST_USER)
		for i in range(3):
			frappe.get_doc(
				{"doctype": "Jarvis Chat Message", "conversation": conv, "seq": i + 1, "role": "user"}
			).insert(ignore_permissions=True)
		frappe.db.set_value(CONV, conv, "auto_apply", 1, update_modified=False)
		frappe.db.commit()

		retire.execute()

		notice = frappe.get_all(
			"Jarvis Chat Message",
			filters={
				"conversation": conv,
				"role": "assistant",
				"content": ["like", "%Auto-Apply has been turned off%"],
			},
			fields=["seq"],
		)
		self.assertEqual(len(notice), 1)
		self.assertEqual(notice[0]["seq"], 4, "the notice appends after the existing messages")
