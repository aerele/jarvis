"""Only completed, deliberately shared records may seed an actor's fresh session."""

import json

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_helpers import CollaborationCase


class TestCollaborationContext(CollaborationCase):
	def turn(self, run, text, state="done"):
		from jarvis.chat.collaboration_bindings import insert_message, stamp

		binding = self.binding(run)
		seed = insert_message(binding, {"role": "user", "content": text, "run_id": run, "origin": "human"})
		turn = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": run,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": state,
				"seed_message": seed.name,
				"enqueued_at": frappe.utils.now_datetime(),
				"done_at": frappe.utils.now_datetime() if state == "done" else None,
			}
		)
		stamp(turn, binding)
		turn.insert(ignore_permissions=True)
		return binding

	def history(self, binding, **kwargs):
		from jarvis.chat.collaboration_context import history_block

		return history_block(binding, binding.logical_run_id, **kwargs)

	def test_future_queued_prompt_never_seeds_an_earlier_turn(self):
		first = self.turn("a" * 32, "completed request")
		current = self.turn("b" * 32, "CURRENT_REQUEST", state="queued")
		self.turn("c" * 32, "FUTURE_QUEUED_SECRET", state="queued")
		text = self.history(current)
		self.assertIn("completed request", text)
		self.assertNotIn("CURRENT_REQUEST", text)
		self.assertNotIn("FUTURE_QUEUED_SECRET", text)
		self.assertNotEqual(first.session_key, current.session_key)

	def test_completed_reply_after_current_seed_seq_is_preserved(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "prior request")
		current = self.turn("b" * 32, "new queued request", state="queued")
		insert_message(first, {"role": "assistant", "content": "PRIOR_COMPLETED_REPLY", "run_id": "a" * 32})
		self.assertIn("PRIOR_COMPLETED_REPLY", self.history(current))

	def test_settled_reply_remains_context_while_usage_enrichment_is_pending(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "prior request", state="finalizing")
		insert_message(first, {"role": "assistant", "content": "SETTLED_VISIBLE_REPLY"})
		current = self.turn("b" * 32, "next request", state="queued")
		self.assertIn("SETTLED_VISIBLE_REPLY", self.history(current))

	def test_idempotency_fingerprint_preserves_meaningful_attachment_order(self):
		from jarvis.chat.collaboration_context import request_fingerprint

		attachments = [{"file_url": "/private/files/first.png"}, {"file_url": "/private/files/second.png"}]
		self.assertNotEqual(
			request_fingerprint("Compare these images", attachments),
			request_fingerprint("Compare these images", list(reversed(attachments))),
		)

	def test_hidden_rows_and_tokens_do_not_enter_shared_context(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "prior request")
		insert_message(
			first, {"role": "assistant", "content": "HIDDEN_PRIVATE_STEP", "hidden": 1, "run_id": "a" * 32}
		)
		insert_message(
			first,
			{
				"role": "tool",
				"content": "ordinary result",
				"tool_args": json.dumps({"api_key": "KEY_SECRET_SENTINEL", "token_number": 17}),
				"pending_card": json.dumps({"token": "OTHER_ACTOR_TOKEN"}),
				"run_id": "a" * 32,
			},
		)
		current = self.turn("b" * 32, "current", state="queued")
		text = self.history(current)
		for secret in ("HIDDEN_PRIVATE_STEP", "KEY_SECRET_SENTINEL", "OTHER_ACTOR_TOKEN"):
			self.assertNotIn(secret, text)
		self.assertIn("token_number", text)

	def test_oversized_record_is_replaced_whole_and_marked_omitted(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "prior request")
		insert_message(
			first,
			{
				"role": "assistant",
				"content": "LONG_RECORD_START_" + "x" * 70000 + "_LONG_RECORD_END",
				"run_id": "a" * 32,
			},
		)
		current = self.turn("b" * 32, "current", state="queued")
		text = self.history(current)
		self.assertLessEqual(len(text.encode()), 65536)
		self.assertIn("older content omitted", text.lower())
		self.assertNotIn("LONG_RECORD_START_", text)
		self.assertNotIn("_LONG_RECORD_END", text)

	def test_existing_compact_tool_result_is_used_before_omitting_a_record(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "prior request")
		insert_message(
			first,
			{
				"role": "tool",
				"content": "report result",
				"tool_name": "run_report",
				"tool_result": json.dumps(
					{
						"ok": True,
						"data": {
							"row_note": "COMPACT_REPORT_SUMMARY",
							"rows": [
								{
									"doctype": "Sales Invoice",
									"name": "ACC-SINV-001",
									"description": "LARGE_RAW_RESULT" * 10000,
								}
							],
						},
					}
				),
			},
		)
		current = self.turn("b" * 32, "current", state="queued")
		text = self.history(current)
		self.assertIn("COMPACT_REPORT_SUMMARY", text)
		self.assertNotIn("LARGE_RAW_RESULT", text)

	def test_history_is_limited_to_whole_records_with_an_omission_notice(self):
		from jarvis.chat.collaboration_bindings import insert_message

		first = self.turn("a" * 32, "older request")
		for i in range(7):
			insert_message(
				first, {"role": "assistant", "content": f"COMPLETE_RECORD_{i}", "run_id": "a" * 32}
			)
		current = self.turn("b" * 32, "current", state="queued")
		text = self.history(current, max_records=3)
		self.assertIn("older content omitted", text.lower())
		self.assertNotIn("COMPLETE_RECORD_0", text)
		self.assertIn("COMPLETE_RECORD_6", text)

	def test_revoked_binding_cannot_obtain_context(self):
		current = self.turn("b" * 32, "current", state="queued")
		frappe.db.set_value(c.SHARE, current.share, {"access_mode": "View", "access_revision": 3})
		with self.assertRaises(frappe.PermissionError):
			self.history(current)

	def test_current_request_is_bounded_by_utf8_bytes_and_not_truncated(self):
		from jarvis.chat.collaboration_context import validate_request

		self.assertEqual(validate_request("é" * 8192), "é" * 8192)
		with self.assertRaises(frappe.ValidationError):
			validate_request("é" * 8193)

	def test_complete_prompt_including_attachments_refuses_smaller_model(self):
		from jarvis.chat.collaboration_context import require_prompt_budget

		with self.assertRaises(frappe.ValidationError):
			require_prompt_budget(
				"request" * 100, attachments=[{"data": "x" * 2000}], context_tokens=1024, runtime_reserve=256
			)
		require_prompt_budget("small request", attachments=[], context_tokens=8192, runtime_reserve=1024)

	def test_unknown_model_capacity_fails_closed(self):
		from jarvis.chat.collaboration_context import require_prompt_budget

		with self.assertRaises(frappe.ValidationError):
			require_prompt_budget("small request", attachments=[], context_tokens=0, runtime_reserve=0)
