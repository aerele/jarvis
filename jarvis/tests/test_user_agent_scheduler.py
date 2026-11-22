"""The user agent schedule sweep and Run now."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import user_agent_scheduler, user_agents_api
from jarvis.tests._user_agent_helpers import DOCTYPE, OWNER, UserAgentCase, _as

CONV = "Jarvis Conversation"


class TestUserAgentSweep(UserAgentCase):
	def setUp(self):
		super().setUp()
		self.enqueue = self._start(
			patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r1", "message_id": "m1"})
		)
		self._start(patch.object(frappe.db, "commit"))
		self._start(patch.object(frappe.db, "rollback"))
		self._start(patch("jarvis.chat.user_agent_scheduler.entitlement_code", return_value=None))

	def scheduled(self, *, due=True, **values):
		row = self.make_agent(
			schedule_enabled=1, schedule_frequency="daily", schedule_prompt="Send me the digest", **values
		)
		when = add_to_date(now_datetime(), minutes=-5 if due else 60)
		frappe.db.set_value(DOCTYPE, row.name, "next_run_at", when)
		return frappe.db.get_value(DOCTYPE, row.name, "*", as_dict=True)

	def state(self, name):
		return frappe.db.get_value(
			DOCTYPE,
			name,
			[
				"next_run_at",
				"last_run_status",
				"last_run_error",
				"last_run_message",
				"last_run_conversation",
				"schedule_enabled",
			],
			as_dict=True,
		)

	def test_a_due_agent_runs_as_its_owner_in_a_new_bound_chat(self):
		row = self.scheduled()
		seen = {}

		def enqueue(conversation, prompt, **kw):
			seen.update(user=frappe.session.user, conversation=conversation, prompt=prompt, **kw)
			return {"run_id": "r1", "message_id": "m1"}

		self.enqueue.side_effect = enqueue
		user_agent_scheduler.run_due_user_agents()
		self.assertEqual(seen["user"], OWNER)
		self.assertEqual(seen["prompt"], "Send me the digest")
		self.assertIs(seen["interactive"], False)
		self.assertEqual(seen["origin"], "macro")
		conv = frappe.db.get_value(
			CONV, seen["conversation"], ["owner", "user_agent", "session_key"], as_dict=True
		)
		self.assertEqual((conv.owner, conv.user_agent), (OWNER, row.agent_id))
		self.assertTrue(conv.session_key.startswith(f"agent:{row.agent_id}:dashboard:"))
		state = self.state(row.name)
		self.assertEqual(state.last_run_status, "Started")
		self.assertEqual(state.last_run_conversation, seen["conversation"])
		self.assertGreater(state.next_run_at, now_datetime())

	def test_a_bound_chat_with_no_macro_run_is_inert_to_the_macro_hook(self):
		from jarvis.chat import macros

		row = self.scheduled()
		user_agent_scheduler.run_due_user_agents()
		conversation = self.state(row.name).last_run_conversation
		self.assertFalse(frappe.db.exists("Jarvis Macro Run", {"conversation": conversation}))
		with patch.object(frappe, "log_error") as log_error, patch("frappe.enqueue") as enqueue:
			macros.advance_after_turn(conversation, errored=False)
		log_error.assert_not_called()
		enqueue.assert_not_called()

	def test_an_agent_not_due_is_left_alone(self):
		row = self.scheduled(due=False)
		before = self.state(row.name).next_run_at
		user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		self.assertEqual(self.state(row.name).next_run_at, before)

	def test_the_flag_off_runs_nothing_and_touches_nothing(self):
		row = self.scheduled()
		before = self.state(row.name).next_run_at
		self.flag(False)
		user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		self.assertEqual(self.state(row.name).next_run_at, before)

	def test_a_disabled_owner_fails_closed_and_the_schedule_is_switched_off(self):
		row = self.scheduled()
		frappe.db.set_value("User", OWNER, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "User", OWNER, "enabled", 1)
		user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		state = self.state(row.name)
		self.assertEqual((state.last_run_status, state.last_run_error), ("Failed", "owner_not_allowed"))
		self.assertEqual(state.schedule_enabled, 0)
		self.assertIsNone(state.next_run_at)

	def test_administrator_and_guest_owned_rows_fail_closed(self):
		for owner in ("Administrator", "Guest"):
			with self.subTest(owner=owner):
				row = self.scheduled(agent_name=f"Forged {owner}")
				frappe.db.set_value(DOCTYPE, row.name, "owner", owner)
				user_agent_scheduler.run_due_user_agents()
				self.enqueue.assert_not_called()
				self.assertEqual(self.state(row.name).last_run_error, "owner_not_allowed")

	def test_a_disabled_agent_moves_on_without_running(self):
		row = self.scheduled()
		frappe.db.set_value(DOCTYPE, row.name, "enabled", 0)
		user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		state = self.state(row.name)
		self.assertFalse(state.last_run_status)
		self.assertGreater(state.next_run_at, now_datetime())

	def test_a_blocked_run_stores_a_typed_code_and_a_sentence_and_tells_the_owner(self):
		row = self.scheduled()
		with patch("jarvis.chat.user_agent_scheduler.entitlement_code", return_value="usage_limit"):
			user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		state = self.state(row.name)
		self.assertEqual((state.last_run_status, state.last_run_error), ("Failed", "usage_limit"))
		self.assertIn("monthly usage limit", state.last_run_message)
		self.assertNotIn("Error Log", state.last_run_message)
		self.assertTrue(
			frappe.db.exists("Notification Log", {"for_user": OWNER, "email_content": state.last_run_message})
		)

	def test_a_dispatch_failure_is_recorded_and_the_sweep_goes_on(self):
		first = self.scheduled(agent_name="First")
		second = self.scheduled(agent_name="Second")
		calls = []

		def enqueue(conversation, prompt, **kw):
			calls.append(conversation)
			if len(calls) == 1:
				raise RuntimeError("gateway down")
			return {"run_id": "r2", "message_id": "m2"}

		self.enqueue.side_effect = enqueue
		with patch.object(frappe, "log_error"):
			user_agent_scheduler.run_due_user_agents()
		errors = {self.state(n).last_run_error or "" for n in (first.name, second.name)}
		self.assertEqual(errors, {"dispatch_failed", ""})

	def test_a_full_queue_is_recorded(self):
		row = self.scheduled()
		self.enqueue.return_value = {"ok": False, "overloaded": True}
		user_agent_scheduler.run_due_user_agents()
		self.assertEqual(self.state(row.name).last_run_error, "overloaded")

	def test_a_never_synced_agent_does_not_run(self):
		row = self.scheduled(synced=False)
		user_agent_scheduler.run_due_user_agents()
		self.enqueue.assert_not_called()
		self.assertEqual(self.state(row.name).last_run_error, "not_synced")

	def test_a_slot_another_sweep_took_is_not_run_twice(self):
		row = self.scheduled()
		stale = frappe._dict(row)
		frappe.db.set_value(DOCTYPE, row.name, "next_run_at", add_to_date(now_datetime(), minutes=-1))
		self.assertFalse(user_agent_scheduler._claim(stale, now_datetime()))

	def test_run_now_starts_an_attended_run_without_moving_the_schedule(self):
		row = self.scheduled(due=False)
		before = self.state(row.name).next_run_at
		with _as(OWNER):
			out = user_agents_api.run_user_agent_now(row.name, prompt="Run it now")
		self.assertEqual(self.enqueue.call_args.args[1], "Run it now")
		self.assertIs(self.enqueue.call_args.kwargs["interactive"], True)
		state = self.state(row.name)
		self.assertEqual(state.last_run_conversation, out["conversation"])
		self.assertEqual(state.next_run_at, before)
