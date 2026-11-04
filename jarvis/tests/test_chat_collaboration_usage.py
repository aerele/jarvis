"""Charge the immutable sender once; never bill a different run/session's counters."""

import uuid
from unittest.mock import Mock

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import usage
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT

_FIELDS = [
	"total_tokens",
	"month_input_tokens",
	"month_output_tokens",
	"month_tokens",
	"usage_month",
	"period_tokens",
	"period_key",
	"last_usage_at",
	"modified",
]


class TestCollaborationUsage(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING,
			self.bound.name,
			{"runtime_session_id": "physical-qa", "runtime_policy_hash": "verified"},
		)
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{
				"state": "finalizing",
				"gateway_run_id": "gateway-qa",
				"usage_recorded": 0,
				"terminal_kind": "relay:final",
			},
		)
		self.old_settings = frappe.db.get_value(
			"Jarvis User Settings", {"user": RECIPIENT}, ["name", *_FIELDS], as_dict=True
		)
		self.settings = usage.get_or_create_user_settings(RECIPIENT)
		self.old_models = frappe.get_all("Jarvis User Model Usage", {"parent": self.settings.name}, ["*"])
		self.actor_total = (
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens") or 0
		)
		self.owner_total = frappe.db.get_value("Jarvis User Settings", {"user": OWNER}, "total_tokens")
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.delete("Jarvis Turn Usage", {"run_id": self.turn.name})
		frappe.db.delete("Jarvis User Model Usage", {"parent": self.settings.name})
		for row in self.old_models:
			frappe.get_doc({"doctype": "Jarvis User Model Usage", **row}).db_insert()
		if self.old_settings:
			frappe.db.set_value(
				"Jarvis User Settings",
				self.settings.name,
				{key: self.old_settings[key] for key in _FIELDS},
				update_modified=False,
			)
		else:
			frappe.db.delete("Jarvis User Settings", {"name": self.settings.name})
		frappe.db.commit()
		super().tearDown()

	def row(self, **overrides):
		return {
			"key": self.bound.session_key,
			"sessionId": "physical-qa",
			"lastRunId": "gateway-qa",
			"totalTokensFresh": True,
			"hasActiveRun": False,
			"inputTokens": 15,
			"outputTokens": 5,
			"totalTokens": 100,
			"contextTokens": 8192,
			"modelProvider": "provider",
			"model": "actor-model",
			**overrides,
		}

	def record(self, row=None):
		return usage.record_turn_usage(
			self.bound.session_key, self.row() if row is None else row, run_id=self.turn.name
		)

	def test_actual_usage_charges_recipient_once_and_keeps_owner_unchanged_after_revoke(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		self.assertEqual(self.record(), usage.USAGE_RECORDED)
		self.assertEqual(self.record(), usage.USAGE_VALID_ZERO)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"),
			self.actor_total + 20,
		)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", {"user": OWNER}, "total_tokens"), self.owner_total
		)
		rows = frappe.get_all(
			"Jarvis Turn Usage",
			{"run_id": self.turn.name},
			["user", "session_key", "tokens_in", "tokens_out"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].user, rows[0].session_key, rows[0].tokens_in, rows[0].tokens_out),
			(RECIPIENT, self.bound.session_key, 15, 5),
		)

	def test_different_physical_session_or_later_child_counters_are_never_billed(self):
		for values in (
			{"sessionId": "replaced-session"},
			{"lastRunId": "different-child"},
			{"key": "owner-global-session"},
		):
			with self.subTest(values=values):
				self.assertEqual(self.record(self.row(**values)), usage.USAGE_RETRY)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"), self.actor_total
		)
		self.assertFalse(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "usage_recorded"))

	def test_tool_usage_count_uses_exact_actor_child_even_for_late_receipts(self):
		reply = insert_message(self.bound, {"role": "assistant", "content": "completed"})
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "assistant_message", reply.name)
		insert_message(self.bound, {"role": "tool", "tool_name": "get_doc", "tool_result": "{}"})
		frappe.db.commit()
		self.assertEqual(self.record(), usage.USAGE_RECORDED)
		self.assertEqual(
			frappe.db.get_value("Jarvis Turn Usage", {"run_id": self.turn.name}, "tool_calls"), 1
		)

	def test_tenant_tool_rollup_attributes_shared_tool_to_actor_instead_of_conversation_owner(self):
		from datetime import datetime

		from jarvis.chat.usage_push import _tool_message_aggregates

		start, end = datetime(2000, 1, 1), datetime(2100, 1, 1)
		_, before, _ = _tool_message_aggregates(start, end)
		insert_message(self.bound, {"role": "tool", "tool_name": "get_doc", "tool_result": "{}"})
		frappe.db.commit()
		_, after, _ = _tool_message_aggregates(start, end)
		self.assertEqual(after.get(RECIPIENT, 0) - before.get(RECIPIENT, 0), 1)
		self.assertEqual(after.get(OWNER, 0) - before.get(OWNER, 0), 0)

	def test_capture_uses_exact_fresh_child_and_never_records_it_twice(self):
		from jarvis.chat.collaboration_usage import capture

		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "terminal_kind", "relay:final")
		frappe.db.commit()
		session = Mock()
		session.list_sessions.return_value = [self.row(key="OWNER_PRIVATE_SESSION"), self.row()]
		self.assertEqual(capture(self.bound, self.turn.name, session=session), usage.USAGE_RECORDED)
		self.assertEqual(capture(self.bound, self.turn.name, session=session), usage.USAGE_VALID_ZERO)
		self.assertEqual(session.list_sessions.call_count, 1)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"),
			self.actor_total + 20,
		)

	def terminal_usage(self, state):
		from jarvis.chat.collaboration_usage import capture

		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, {"state": state, "terminal_kind": "relay:error"}
		)
		frappe.db.commit()
		session = Mock()
		session.list_sessions.return_value = [self.row()]
		self.assertEqual(capture(self.bound, self.turn.name, session=session), usage.USAGE_RECORDED)
		self.assertEqual(capture(self.bound, self.turn.name, session=session), usage.USAGE_VALID_ZERO)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"),
			self.actor_total + 20,
		)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", {"user": OWNER}, "total_tokens"), self.owner_total
		)

	def test_actual_cancelled_request_still_charges_its_actor_once(self):
		self.terminal_usage("cancelled")

	def test_actual_errored_request_still_charges_its_actor_once(self):
		self.terminal_usage("errored")

	def test_release_between_claim_and_force_done_keeps_usage_retryable(self):
		from unittest.mock import patch

		from jarvis.chat import turn_state as ts

		ts.insert_required_effects(self.turn.name, ["usage"])
		outcome, token = ts.claim_effect(self.turn.name, "usage")
		self.assertEqual(outcome, "attempt")
		frappe.db.commit()
		original = ts._run_cas

		def release_after_losing_claim(sql, params):
			if "attempts=attempts+1" in sql:
				# B loses to A's live claim; A then releases its first attempt
				# before B's separate force-done mutation gets to the row.
				self.assertEqual(original(sql, params), 0)
				self.assertTrue(ts.release_effect(self.turn.name, "usage", token))
				frappe.db.commit()
				return 0
			return original(sql, params)

		with patch.object(ts, "_run_cas", side_effect=release_after_losing_claim):
			self.assertEqual(ts.claim_effect(self.turn.name, "usage"), ("busy", None))
		row = frappe.db.get_value(
			ts.EFFECT, {"turn": self.turn.name, "effect_name": "usage"}, ["status", "attempts"]
		)
		self.assertEqual(row, ("pending", 1))
		self.assertEqual(ts.claim_effect(self.turn.name, "usage")[0], "attempt")

	def test_exhausted_usage_effect_records_unavailable_and_releases_cleanup(self):
		from contextlib import contextmanager
		from unittest.mock import patch

		from jarvis.chat import finalize, settlement
		from jarvis.chat.collaboration_lifecycle import retire_conversation
		from jarvis.chat.collaboration_usage import require_previous_usage
		from jarvis.tests.race_harness import as_job

		reply = insert_message(self.bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{
				"state": "terminal_observed",
				"terminal_kind": "relay:error",
				"cancel_requested": 1,
				"pump_epoch": 12,
				"assistant_message": reply.name,
			},
		)
		frappe.db.commit()
		session = Mock()
		session.list_sessions.return_value = [
			self.row(totalTokensFresh=False, inputTokens=None, outputTokens=None)
		]

		@contextmanager
		def checkout(*args, **kwargs):
			yield session

		with (
			patch("jarvis.chat.agent_session_pool.checkout", checkout),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
			patch("jarvis.chat.collaboration_usage.time.sleep"),
		):
			settlement.invoke_settlement(
				self.turn.name,
				relay_target_id="default",
				epoch=12,
				version=frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "version"),
				terminal_kind="relay:error",
				terminal_payload={"state": "aborted"},
				assistant_message=reply.name,
				owner=RECIPIENT,
				conversation=self.conversation,
				deps=Mock(),
			)
			with as_job():
				for _ in range(4):
					finalize.run_finalize(self.turn.name)
		row = frappe.get_doc("Jarvis Chat Turn", self.turn.name)
		self.assertFalse(row.usage_recorded)
		self.assertTrue(row.get("collaboration_usage_unavailable"))
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"), self.actor_total
		)
		self.assertFalse(frappe.db.exists("Jarvis Turn Usage", {"run_id": self.turn.name}))
		# Crash after the effect's force-done commit but before the explicit
		# disposition: both consumers must repair it without another finalize job.
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "collaboration_usage_unavailable", 0)
		frappe.db.commit()
		with patch("jarvis.chat.collaboration_usage.time.sleep"):
			require_previous_usage(self.bound, "next-child", session)
		self.assertTrue(
			frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "collaboration_usage_unavailable")
		)
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "collaboration_usage_unavailable", 0)
		frappe.db.commit()
		self.assertTrue(retire_conversation(self.conversation))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Retired")

	def test_aborted_settlement_and_finalizer_record_actual_usage_without_unsettling_stop(self):
		from contextlib import contextmanager
		from unittest.mock import patch

		from jarvis.chat import finalize, settlement
		from jarvis.tests.race_harness import as_job

		reply = insert_message(self.bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value(
			"Jarvis Chat Turn",
			self.turn.name,
			{
				"state": "terminal_observed",
				"terminal_kind": "relay:error",
				"cancel_requested": 1,
				"pump_epoch": 12,
				"assistant_message": reply.name,
			},
		)
		frappe.db.commit()
		session = Mock()
		session.list_sessions.return_value = [self.row()]

		@contextmanager
		def checkout(*args, **kwargs):
			yield session

		with (
			patch("jarvis.chat.agent_session_pool.checkout", checkout),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			settlement.invoke_settlement(
				self.turn.name,
				relay_target_id="default",
				epoch=12,
				version=frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "version"),
				terminal_kind="relay:error",
				terminal_payload={"state": "aborted", "text": "Completed portion"},
				assistant_message=reply.name,
				owner=RECIPIENT,
				conversation=self.conversation,
				deps=Mock(),
			)
			with as_job():
				finalize.run_finalize(self.turn.name)
				finalize.run_finalize(self.turn.name)
		self.assertEqual(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "state"), "cancelled")
		self.assertTrue(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "usage_recorded"))
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", self.settings.name, "total_tokens"),
			self.actor_total + 20,
		)
		self.assertEqual(frappe.db.count("Jarvis Turn Usage", {"run_id": self.turn.name}), 1)
