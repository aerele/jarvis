"""Every participant, including the owner, reads the same projected transcript."""

import json
import uuid
from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.chat.collaboration_callbacks import callback_scope, record_gateway_tool
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user


class TestCollaborationRead(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING, self.bound.name, {"runtime_session_id": "physical", "runtime_policy_hash": "verified"}
		)
		self.reply = insert_message(
			self.bound, {"role": "assistant", "content": "Reply: api_key=PRIVATE_SECRET_SENTINEL"}
		)
		insert_message(self.bound, {"role": "user", "hidden": 1, "content": "HIDDEN_CONTINUATION_SENTINEL"})
		frappe.db.commit()

	def read(self, user=RECIPIENT):
		from jarvis.chat.collaboration_api import get_conversation

		with as_user(user):
			return get_conversation(self.conversation)

	def recent_terminal_turns(self):
		from jarvis.chat.collaboration_bindings import stamp

		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, "enqueued_at", frappe.utils.add_to_date(None, days=-1)
		)
		for _ in range(30):
			run = uuid.uuid4().hex
			row = frappe.get_doc(
				{
					"doctype": "Jarvis Chat Turn",
					"name": run,
					"run_id": run,
					"conversation": self.conversation,
					"relay_target_id": "default",
					"state": "cancelled",
					"seed_message": self.turn.seed_message,
					"enqueued_at": frappe.utils.now_datetime(),
				}
			)
			stamp(row, self.bound)
			row.db_insert()
		frappe.db.commit()

	def test_active_request_remains_visible_past_the_recent_terminal_window(self):
		self.recent_terminal_turns()
		turns = self.read()["turns"]
		self.assertIn(self.turn.name, [row["run_id"] for row in turns])
		self.assertLessEqual(len(turns), 52)

	def test_watched_completed_request_has_bounded_status_after_it_leaves_the_recent_window(self):
		from jarvis.chat.collaboration_api import get_conversation

		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
		self.recent_terminal_turns()
		with as_user(RECIPIENT):
			turns = get_conversation(self.conversation, watch_run=self.turn.name)["turns"]
		self.assertIn(self.turn.name, [row["run_id"] for row in turns])
		self.assertLessEqual(len(turns), 52)

	def pending(self):
		from jarvis.chat.pending_actions import park

		record_gateway_tool(self.turn.name, "qa-create", "jarvis__create_doc")
		preview = {"card": {"heading": "Create a Note", "summary": "A proposed Note"}}
		with callback_scope(self.bound.session_key, "create_doc", "qa-create", wait_s=0):
			return park(
				kind="chat",
				owner_user=RECIPIENT,
				exec_user=RECIPIENT,
				tool="create_doc",
				args={"doctype": "Note", "values": {"title": "Shared Note"}},
				conversation=self.conversation,
				run_id=self.turn.name,
				preview=preview,
				card=preview["card"],
				summary="Create a Note",
				display_row=lambda row: api._insert_pending_row(
					self.conversation, "create_doc", preview, row.name, None
				),
			)

	def test_owner_and_recipient_receive_projected_common_content_and_actor_labels(self):
		for user in (OWNER, RECIPIENT):
			with self.subTest(user=user):
				body = self.read(user)
				self.assertEqual(body["conversation"].get("owner"), OWNER)
				self.assertEqual(body["conversation"].get("owner_name"), frappe.utils.get_fullname(OWNER))
				text = json.dumps(body, default=str)
				self.assertNotIn("PRIVATE_SECRET_SENTINEL", text)
				self.assertNotIn("HIDDEN_CONTINUATION_SENTINEL", text)
				row = next(r for r in body["messages"] if r["name"] == self.reply.name)
				self.assertEqual(row["actor"]["id"], RECIPIENT)
				self.assertEqual(row["content"], "Reply: api_key=[redacted]")

	def test_legacy_shared_view_advertises_projected_collaboration_without_action_payloads(self):
		from jarvis.chat.sharing import get_shared_conversation

		with as_user(RECIPIENT):
			body = get_shared_conversation(self.conversation)
		self.assertTrue(body["conversation"].get("collaboration_available"))
		self.assertTrue(body["conversation"].get("collaboration_active"))
		self.assertNotIn("PRIVATE_SECRET_SENTINEL", json.dumps(body, default=str))
		self.assertFalse(any(row.get("control") for row in body["messages"]))
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}), as_user(RECIPIENT):
			body = get_shared_conversation(self.conversation)
		self.assertFalse(body["conversation"]["collaboration_available"])
		self.assertTrue(body["conversation"]["collaboration_active"])

	def test_only_initiator_receives_usable_pending_control(self):
		token = self.pending()
		owner = self.read(OWNER)
		self.assertNotIn(token, json.dumps(owner, default=str))
		self.assertTrue(any(r.get("awaiting_actor") for r in owner["messages"]))
		actor = self.read(RECIPIENT)
		controls = [r["control"] for r in actor["messages"] if r.get("control")]
		self.assertEqual([control["action"] for control in controls], [token])

	def test_downgraded_actor_keeps_reading_but_loses_all_action_controls(self):
		token = self.pending()
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "View", "access_revision": 3})
		frappe.db.commit()
		body = self.read()
		self.assertEqual(body["conversation"]["access_mode"], "View")
		self.assertFalse(body["conversation"]["can_chat"])
		self.assertNotIn(token, json.dumps(body, default=str))

	def test_outsider_and_revoked_user_cannot_read_common_history(self):
		with self.assertRaises(frappe.PermissionError):
			self.read(OUTSIDER)
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.read(RECIPIENT)

	def test_owner_read_survives_rollout_and_sharing_off_without_raw_fallback(self):
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 0)
		frappe.db.commit()
		with patch.dict(frappe.conf, {"chat_collaboration_enabled": 0}):
			body = self.read(OWNER)
		self.assertFalse(body["conversation"]["can_chat"])
		self.assertNotIn("PRIVATE_SECRET_SENTINEL", json.dumps(body, default=str))

	def test_turn_state_is_common_but_stop_authority_is_actor_or_owner_only(self):
		from jarvis.chat.sharing import set_shares

		with as_user(OWNER):
			set_shares(self.conversation, [RECIPIENT, OUTSIDER])
		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, "dispatch_payload", '{"secret":"DISPATCH_SENTINEL"}'
		)
		frappe.db.commit()
		for actor, can_stop in ((RECIPIENT, True), (OWNER, True), (OUTSIDER, False)):
			body = self.read(actor)
			row = next(row for row in body["turns"] if row["run_id"] == self.turn.name)
			self.assertEqual(row["actor"]["id"], RECIPIENT)
			self.assertEqual(row["state"], "dispatching")
			self.assertEqual(row["can_stop"], can_stop)
			self.assertNotIn("DISPATCH_SENTINEL", json.dumps(body, default=str))
			self.assertNotIn(self.bound.session_key, json.dumps(body, default=str))

	def test_downgrade_removes_stop_control_and_preserves_common_status(self):
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "View", "access_revision": 3})
		frappe.db.commit()
		row = next(row for row in self.read()["turns"] if row["run_id"] == self.turn.name)
		self.assertEqual(row["state"], "dispatching")
		self.assertFalse(row["can_stop"])

	def question(self):
		from jarvis.chat.collaboration_asks import materialize
		from jarvis.tests.test_chat_collaboration_asks import ASK

		frappe.db.set_value("Jarvis Chat Message", self.reply.name, "content", ASK)
		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, {"state": "finalizing", "assistant_message": self.reply.name}
		)
		frappe.db.commit()
		return materialize(self.turn.name)

	def test_only_initiator_gets_question_answer_control(self):
		name = self.question()
		self.assertTrue(name)
		for actor, usable in ((RECIPIENT, True), (OWNER, False)):
			body = self.read(actor)
			question = body["questions"][0]
			self.assertEqual(question["actor"]["id"], RECIPIENT)
			self.assertIn("warehouse", question["question"])
			self.assertEqual(question.get("control") == {"question": name}, usable)
			if not usable:
				self.assertNotIn(name, json.dumps(body, default=str))

	def test_regrant_does_not_restore_an_old_question_control(self):
		name = self.question()
		frappe.db.set_value(c.SHARE, self.share, {"access_mode": "Chat", "access_revision": 4})
		frappe.db.commit()
		body = self.read()
		self.assertNotIn(name, json.dumps(body, default=str))
		self.assertFalse(body["questions"][0].get("control"))

	def test_projected_read_does_not_lock_the_conversation_or_owner(self):
		from jarvis.tests.race_harness import other_connection

		self.read()
		with other_connection() as other:
			other.sql("SET SESSION innodb_lock_wait_timeout=1")
			other.sql("UPDATE `tabJarvis Conversation` SET title=title WHERE name=%s", self.conversation)
			other.sql("UPDATE `tabUser` SET full_name=full_name WHERE name=%s", OWNER)
			other.commit()

	def test_read_ignores_an_old_snapshot_after_revocation(self):
		from jarvis.tests._collaboration_committed import repeatable_read
		from jarvis.tests.race_harness import open_read_view, other_connection, snapshot_isolation_off

		with repeatable_read(), snapshot_isolation_off():
			open_read_view()
			with other_connection() as other:
				other.sql("DELETE FROM `tabJarvis Conversation Share` WHERE name=%s", self.share)
				other.commit()
			with self.assertRaises(frappe.PermissionError):
				self.read()

	def test_failed_connection_close_still_restores_the_callers_database(self):
		from jarvis.tests.race_harness import other_connection

		producer = frappe.local.db
		with other_connection() as reader:
			close = reader.close

			def fail_close():
				close()
				raise RuntimeError("simulated close failure")

			try:
				with (
					patch("frappe.database.get_db", return_value=reader),
					patch.object(reader, "connect"),
					patch.object(reader, "close", side_effect=fail_close),
				):
					with self.assertRaises(RuntimeError):
						self.read()
				self.assertIs(frappe.local.db, producer)
			finally:
				frappe.local.db = producer
