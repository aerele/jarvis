"""Shared events invalidate projected reads; raw owner payloads never fan out."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat.events import CHANNEL, publish_to_user
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT


class TestCollaborationRealtime(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		from jarvis.chat.collaboration_realtime import publish_notice

		original_enqueue = frappe.enqueue

		def run_notice(method, **kwargs):
			if method == "jarvis.chat.collaboration_realtime.publish_notice":
				return publish_notice(kwargs["conversation"])
			return original_enqueue(method, **kwargs)

		queued = patch("frappe.enqueue", side_effect=run_notice)
		queued.start()
		self.addCleanup(queued.stop)

	def activate(self):
		self.binding(uuid.uuid4().hex)
		frappe.db.commit()

	def notify(self):
		publish_to_user(
			OWNER,
			{
				"kind": "run:end",
				"conversation_id": self.conversation,
				"text": "RAW_SECRET_SENTINEL",
				"pending": [{"token": "TOKEN_SENTINEL"}],
				"session_key": "OWNER_SESSION_SENTINEL",
			},
		)

	def test_committed_event_reaches_current_members_only_and_contains_no_raw_payload(self):
		self.activate()
		with patch("frappe.publish_realtime") as send:
			frappe.db.set_value(c.CONVERSATION, self.conversation, "title", "Committed title")
			self.notify()
			send.assert_not_called()
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})
			for call in send.call_args_list:
				self.assertEqual(
					call.args,
					(CHANNEL, {"kind": "collaboration:changed", "conversation_id": self.conversation}),
				)
			self.assertNotIn("SENTINEL", str(send.call_args_list))

	def test_revoke_before_commit_prevents_recipient_notification(self):
		self.activate()
		with patch("frappe.publish_realtime") as send:
			frappe.db.delete(c.SHARE, {"name": self.share})
			self.notify()
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER})

	def test_rolled_back_reply_does_not_publish_or_commit_itself(self):
		self.activate()
		with patch("frappe.publish_realtime") as send:
			frappe.db.set_value(c.CONVERSATION, self.conversation, "title", "Uncommitted title")
			self.notify()
			send.assert_not_called()
			frappe.db.rollback()
			frappe.db.commit()
			send.assert_not_called()
		self.assertNotEqual(
			frappe.db.get_value(c.CONVERSATION, self.conversation, "title"), "Uncommitted title"
		)

	def test_disabled_recipient_and_sharing_off_get_no_common_content(self):
		self.activate()
		frappe.db.set_value("User", RECIPIENT, "enabled", 0)
		frappe.db.commit()
		with patch("frappe.publish_realtime") as send:
			self.notify()
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER})
		frappe.db.set_value("User", RECIPIENT, "enabled", 1)
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 0)
		frappe.db.commit()
		with patch("frappe.publish_realtime") as send:
			self.notify()
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER})

	def test_share_changed_stays_an_inert_revocation_notice(self):
		self.activate()
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with patch("frappe.publish_realtime") as send:
			publish_to_user(
				RECIPIENT,
				{"kind": "share:changed", "conversation_id": self.conversation, "token": "TOKEN_SENTINEL"},
			)
			frappe.db.commit()
			send.assert_called_once_with(
				CHANNEL, {"kind": "share:changed", "conversation_id": self.conversation}, user=RECIPIENT
			)

	def test_private_events_keep_the_original_single_user_contract(self):
		payload = {"kind": "assistant:delta", "conversation_id": self.conversation, "text": "Private reply"}
		with patch("frappe.publish_realtime") as send:
			publish_to_user(OWNER, payload)
			send.assert_called_once_with(CHANNEL, payload, user=OWNER)
		self.assertNotEqual(OWNER, OUTSIDER)

	def test_pump_does_not_store_or_publish_partial_shared_reply(self):
		from jarvis.chat import pump
		from jarvis.chat import turn_state as ts
		from jarvis.chat.collaboration_bindings import insert_message

		bound, turn = self.running_turn(self.binding(uuid.uuid4().hex))
		reply = insert_message(bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value(ts.TURN, turn.name, {"state": "streaming", "assistant_message": reply.name})
		frappe.db.commit()
		state = pump._RunState(
			run_id=turn.name,
			conversation=self.conversation,
			owner=RECIPIENT,
			assistant_message=reply.name,
			session_key=bound.session_key,
			version=int(turn.version),
			pending_seq=1,
			pending_text="api_key=PARTIAL_SECRET_SENTINEL",
			pending_delta="api_key=PARTIAL_SECRET_SENTINEL",
		)
		context = SimpleNamespace(epoch=0, relay_target_id="default")
		with patch("frappe.publish_realtime") as send:
			pump._flush_deltas(context, state)
			frappe.db.commit()
			self.assertEqual(frappe.db.get_value(ts.MSG, reply.name, "content"), "")
			self.assertNotIn("PARTIAL_SECRET_SENTINEL", str(send.call_args_list))

	def test_notification_leaves_no_locks_on_the_producer_connection(self):
		from jarvis.tests.race_harness import other_connection

		self.activate()
		with patch("frappe.publish_realtime"):
			self.notify()
			frappe.db.commit()
		with other_connection() as other:
			other.sql("SET SESSION innodb_lock_wait_timeout=1")
			other.sql(
				"UPDATE `tabJarvis Conversation` SET title=title WHERE name=%s",
				(self.conversation,),
			)
			other.commit()

	def test_already_committed_notice_survives_an_unrelated_rollback(self):
		self.activate()
		with patch("frappe.publish_realtime") as send:
			self.notify()
			frappe.db.rollback()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})
			self.assertNotIn("SENTINEL", str(send.call_args_list))

	def test_notice_does_not_wait_for_another_readers_conversation_lock(self):
		from jarvis.tests.race_harness import other_connection

		self.activate()
		with other_connection() as blocker, other_connection() as notice_db:
			blocker.sql("UPDATE `tabJarvis Conversation` SET title=title WHERE name=%s", (self.conversation,))
			notice_db.sql("SET SESSION innodb_lock_wait_timeout=1")
			with (
				patch("frappe.database.get_db", return_value=notice_db),
				patch.object(notice_db, "connect"),
				patch("frappe.publish_realtime") as send,
			):
				self.notify()
				frappe.db.commit()
				self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})

	def test_tool_result_publisher_cannot_bypass_common_projection(self):
		from jarvis.api import publish_realtime_tool_result

		self.activate()
		with patch("frappe.publish_realtime") as send:
			publish_realtime_tool_result(
				user=OWNER,
				conversation_id=self.conversation,
				tool_message_id=self.message,
				tool_name="get_doc",
				args={"api_key": "ARG_SENTINEL"},
				result={"ok": True, "private_url": "/private/files/RESULT_SENTINEL.pdf"},
				status="success",
			)
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})
			for call in send.call_args_list:
				self.assertEqual(
					call.args,
					(CHANNEL, {"kind": "collaboration:changed", "conversation_id": self.conversation}),
				)
			self.assertNotIn("SENTINEL", str(send.call_args_list))

	def test_shared_artifact_publish_never_contains_private_file_urls(self):
		from jarvis.api import _maybe_attach_artifact
		from jarvis.chat.collaboration_bindings import insert_message
		from jarvis.chat.collaboration_turn import turn_context

		bound, turn = self.running_turn(self.binding(uuid.uuid4().hex))
		reply = insert_message(bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value("Jarvis Chat Turn", turn.name, "assistant_message", reply.name)
		frappe.db.commit()
		with patch("frappe.publish_realtime") as send, turn_context(bound, turn.name):
			_maybe_attach_artifact(
				self.conversation,
				RECIPIENT,
				{
					"ok": True,
					"data": {"filename": "report.pdf", "file_url": "/private/files/FILE_SENTINEL.pdf"},
				},
			)
			frappe.db.commit()
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})
			self.assertNotIn("FILE_SENTINEL", str(send.call_args_list))
			self.assertIn("FILE_SENTINEL", frappe.db.get_value("Jarvis Chat Message", reply.name, "canvas"))

	def test_context_free_shared_artifact_cannot_attach_to_another_actors_reply(self):
		from jarvis.api import _maybe_attach_artifact
		from jarvis.chat.collaboration_bindings import insert_message

		bound, turn = self.running_turn(self.binding(uuid.uuid4().hex))
		reply = insert_message(bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value("Jarvis Chat Turn", turn.name, "assistant_message", reply.name)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			_maybe_attach_artifact(
				self.conversation,
				OWNER,
				{
					"ok": True,
					"data": {"filename": "report.pdf", "file_url": "/private/files/WRONG_ACTOR.pdf"},
				},
			)
		self.assertFalse(frappe.db.get_value("Jarvis Chat Message", reply.name, "canvas"))

	def test_pending_action_conversation_alias_cannot_publish_tokens_or_raw_preview(self):
		self.activate()
		with patch("frappe.publish_realtime") as send:
			publish_to_user(
				RECIPIENT,
				{
					"kind": "action:pending",
					"conversation": self.conversation,
					"token": "TOKEN_SENTINEL",
					"preview": {"api_key": "PREVIEW_SENTINEL"},
				},
			)
			frappe.db.commit()
			self.assertNotIn("SENTINEL", str(send.call_args_list))
			self.assertEqual({call.kwargs["user"] for call in send.call_args_list}, {OWNER, RECIPIENT})

	def test_notification_enqueues_only_an_inert_job_without_connecting_on_producer(self):
		self.activate()
		with (
			patch("frappe.database.get_db", side_effect=AssertionError("producer connected")),
			patch("frappe.enqueue") as enqueue,
		):
			self.notify()
			frappe.db.commit()
			enqueue.assert_called_once()
			self.assertEqual(enqueue.call_args.kwargs["conversation"], self.conversation)
			self.assertNotIn("SENTINEL", str(enqueue.call_args))

	def test_pending_writes_coalesce_notices_and_rollback_drops_only_their_own_job(self):
		self.activate()
		with patch("frappe.enqueue") as enqueue:
			frappe.db.set_value(c.CONVERSATION, self.conversation, "title", "One transaction")
			for _ in range(3):
				self.notify()
			enqueue.assert_not_called()
			frappe.db.rollback()
			frappe.db.commit()
			enqueue.assert_not_called()
			frappe.db.set_value(c.CONVERSATION, self.conversation, "title", "Next transaction")
			for _ in range(3):
				self.notify()
			frappe.db.commit()
			enqueue.assert_called_once()
