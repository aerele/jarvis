"""Shared completion has a closed actor-safe effect set and a durable ledger."""

import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import finalize, worker
from jarvis.chat import turn_state as ts
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._share_helpers import RECIPIENT
from jarvis.tests.race_harness import as_job
from jarvis.tests.test_chat_collaboration_asks import ASK
from jarvis.tests.test_chat_collaboration_rq import RQCase


def setUpModule():
	install_synthetic_runtime_profile()


class TestCollaborationFinalize(RQCase):
	def drive(self):
		super().drive()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "finalizing")

	def finish(self):
		with as_job():
			return finalize.run_finalize(self.turn.name)

	def test_settlement_owes_only_shared_safe_effects(self):
		self.drive()
		self.assertEqual(
			set(frappe.get_all(ts.EFFECT, {"turn": self.turn.name}, pluck="effect_name")),
			{"terminal_publish", "enrich_cards", "chat_asks", "usage", "telemetry_flush"},
		)

	def test_real_effect_ledger_completes_without_owner_or_global_hooks(self):
		self.drive()
		with (
			patch("jarvis.chat.turn_handler.persist_rich_outputs") as rich,
			patch("jarvis.chat.chat_asks.materialize_from_turn") as legacy_asks,
			patch("jarvis.chat.macros.advance_after_turn") as macro,
			patch("jarvis.chat.wiki.maybe_nudge") as wiki,
		):
			result = self.finish()
			self.assertTrue(result["ok"])
			self.assertTrue(result["done"])
			for hook in (rich, legacy_asks, macro, wiki):
				hook.assert_not_called()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "done")
		self.assertTrue(frappe.db.get_value(ts.TURN, self.turn.name, "usage_recorded"))
		self.assertTrue(ts.all_required_effects_done(self.turn.name))
		self.assertTrue(self.finish()["already_done"])
		self.assertEqual(len(self.gateway.sends), 1)

	def test_finalized_question_is_bound_to_its_actual_initiator(self):
		self.gateway.reply = ASK
		self.drive()
		self.assertTrue(self.finish()["done"])
		rows = frappe.get_all(
			"Jarvis Approval Request",
			{"conversation": self.conversation},
			["actor", "owner", "collaboration_binding", "collaboration_run_id", "status"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].actor, rows[0].owner), (RECIPIENT, RECIPIENT))
		self.assertEqual(
			(rows[0].collaboration_binding, rows[0].collaboration_run_id, rows[0].status),
			(self.bound.name, self.turn.name, "Pending"),
		)

	def test_revocation_blocks_new_enrichment_but_keeps_actual_usage_settlement(self):
		self.gateway.reply = ASK
		self.drive()
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with patch("jarvis.chat.cards_enrich.enrich_text") as enrich:
			self.assertTrue(self.finish()["done"])
			enrich.assert_not_called()
		self.assertEqual(frappe.db.count("Jarvis Approval Request", {"conversation": self.conversation}), 0)
		self.assertTrue(frappe.db.get_value(ts.TURN, self.turn.name, "usage_recorded"))

	def test_failed_actor_effect_retries_without_logging_raw_content(self):
		self.drive()
		with (
			patch("jarvis.chat.collaboration_asks.materialize", side_effect=RuntimeError("SECRET_SENTINEL")),
			patch("frappe.log_error") as log,
		):
			result = self.finish()
			self.assertTrue(result["ok"])
			self.assertFalse(result["done"])
			self.assertTrue(log.called)
			self.assertNotIn("SECRET_SENTINEL", str(log.call_args_list))
		self.assertEqual(
			frappe.db.get_value(ts.EFFECT, {"turn": self.turn.name, "effect_name": "chat_asks"}, "status"),
			"pending",
		)
		self.assertTrue(self.finish()["done"])

	def child(self):
		from jarvis.chat.collaboration_continuations import enqueue

		previous = getattr(frappe.local, "jarvis_shared_continuation_ids", None)
		frappe.local.jarvis_shared_continuation_ids = ["completion-test:" + uuid.uuid4().hex]
		try:
			with (
				patch("jarvis.chat.admission.relay_target_id", return_value=self.target),
				patch.dict(frappe.conf, {"jarvis_phase0_admission_enabled": 1}),
			):
				return enqueue(self.bound, "Continue the same actor's request.", claim=lambda: True)
		finally:
			frappe.local.jarvis_shared_continuation_ids = previous

	def test_same_binding_child_captures_prior_usage_before_replacing_gateway_counters(self):
		self.drive()
		self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "usage_recorded"))
		child = self.child()
		self.assertTrue(child["ok"])
		with as_job():
			worker.run_agent_turn(self.conversation, child["message_id"], child["run_id"])
		self.assertTrue(frappe.db.get_value(ts.TURN, self.turn.name, "usage_recorded"))
		self.assertEqual(len(self.gateway.sends), 2)
		self.assertEqual(frappe.db.get_value(ts.TURN, child["run_id"], "state"), "finalizing")

	def test_child_refuses_before_model_when_prior_usage_belongs_to_another_run(self):
		self.drive()
		child = self.child()
		self.gateway.last_run = "WRONG_GATEWAY_RUN"
		with as_job():
			worker.run_agent_turn(self.conversation, child["message_id"], child["run_id"])
		self.assertEqual(len(self.gateway.sends), 1)
		self.assertFalse(frappe.db.get_value(ts.TURN, self.turn.name, "usage_recorded"))
		self.assertEqual(frappe.db.get_value(ts.TURN, child["run_id"], "state"), "cancelled")
