"""Server-owned sidebar activity; run only on a disposable test site."""

from unittest.mock import patch

import frappe

from jarvis.chat.api import create_conversation, get_conversation, list_conversations
from jarvis.tests.test_chat_api import _ChatTestCase


class TestSidebarActivity(_ChatTestCase):
	def message(self, conv, seq=1, role="user", **values):
		return (
			frappe.get_doc(
				{
					"doctype": "Jarvis Chat Message",
					"conversation": conv,
					"seq": seq,
					"role": role,
					"content": "sidebar fixture",
					**values,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def turn(self, conv, seed, state, message=None):
		run = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": run,
				"conversation": conv,
				"relay_target_id": "sidebar-test",
				"turn_class": "interactive",
				"state": state,
				"seed_message": seed,
				"assistant_message": message,
			}
		).insert(ignore_permissions=True, ignore_links=True)
		return run

	def activity(self, conv):
		return next(row for row in list_conversations(refresh=True) if row.name == conv)["activity"]

	def test_returns_all_run_identities_without_choosing_an_old_finalizer(self):
		conv = create_conversation()
		seed = self.message(conv)
		ids = {self.turn(conv, seed, state): state for state in ("finalizing", "streaming", "queued")}
		self.assertEqual({row["run_id"]: row["state"] for row in self.activity(conv)}, ids)

	def test_legacy_streaming_and_old_recovery_are_not_expired_by_silence(self):
		conv = create_conversation()
		self.message(conv)
		old = self.message(conv, 2, "assistant", streaming=1, recovering=1)
		frappe.db.set_value(
			"Jarvis Chat Message", old, "modified", "2020-01-01 00:00:00", update_modified=False
		)
		new = self.message(conv, 3, "assistant", streaming=1)
		activity = self.activity(conv)
		self.assertEqual(
			{row["message_id"]: row["state"] for row in activity}, {old: "recovering", new: "streaming"}
		)

	def test_terminal_turn_owns_stale_streaming_message_so_it_is_not_legacy(self):
		conv = create_conversation()
		seed = self.message(conv)
		msg = self.message(conv, 2, "assistant", streaming=1)
		self.turn(conv, seed, "done", msg)
		self.assertEqual(self.activity(conv), [])

	def test_stopped_legacy_message_is_not_busy(self):
		conv = create_conversation()
		self.message(conv, 1, "assistant", streaming=1, stopped=1)
		self.assertEqual(self.activity(conv), [])

	def test_status_refresh_does_not_prewarm_but_default_load_preserves_it(self):
		with patch("jarvis.chat.prewarm.enqueue_warm_if_due") as warm:
			list_conversations(refresh=True)
			warm.assert_not_called()
			list_conversations()
			warm.assert_called_once()

	def test_activity_is_batched_and_respects_list_owner_and_dashboard_filter(self):
		visible = create_conversation()
		seed = self.message(visible)
		self.turn(visible, seed, "streaming")
		second = create_conversation()
		self.message(second)
		hidden = create_conversation()
		self.message(hidden)
		frappe.db.set_value("Jarvis Conversation", hidden, "origin_page", "dashboards")
		foreign = create_conversation()
		self.message(foreign)
		frappe.db.set_value("Jarvis Conversation", foreign, "owner", "Administrator")
		try:
			with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
				rows = list_conversations(refresh=True)
			queries = [str(call.args[0]) for call in sql.call_args_list]
			self.assertEqual(len([q for q in queries if "tabJarvis Chat Turn" in q]), 2)
			self.assertEqual({row.name for row in rows}, {visible, second})
		finally:
			frappe.db.set_value("Jarvis Conversation", foreign, "owner", frappe.session.user)

	def test_open_chat_reads_the_same_legacy_recovery_fact(self):
		conv = create_conversation()
		self.message(conv)
		msg = self.message(conv, 2, "assistant", streaming=1, recovering=1)
		frappe.db.set_value(
			"Jarvis Chat Message", msg, "modified", "2020-01-01 00:00:00", update_modified=False
		)
		row = next(row for row in get_conversation(conv)["messages"] if row["name"] == msg)
		self.assertEqual(row["activity_state"], "recovering")

	def test_open_chat_does_not_resume_a_settled_turns_stale_flag(self):
		conv = create_conversation()
		seed = self.message(conv)
		msg = self.message(conv, 2, "assistant", streaming=1)
		self.turn(conv, seed, "done", msg)
		row = next(row for row in get_conversation(conv)["messages"] if row["name"] == msg)
		self.assertIsNone(row["activity_state"])

	def test_activity_annotation_does_not_claim_nonstreaming_history_is_running(self):
		conv = create_conversation()
		seed = self.message(conv)
		finished = self.message(conv, 2, "assistant", streaming=0)
		self.message(conv, 3, "assistant", streaming=1, recovering=1)
		rows = {row["name"]: row for row in get_conversation(conv)["messages"]}
		self.assertNotIn("activity_state", rows[seed])
		self.assertNotIn("activity_state", rows[finished])
