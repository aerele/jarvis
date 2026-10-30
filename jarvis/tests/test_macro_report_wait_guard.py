"""The agent changes nothing while its macro step waits for a report.

A step that got ``waiting: true`` was told to stop and wait; the server enforces it for
the agent's tool calls (``macro_report_wait.write_refusal``, in ``call_tool``'s agent
branch). A person's card confirm, a typed follow-up in the run's chat and every bench
with the feature off must not notice.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import macro_report_wait, pending_confirm
from jarvis.chat.actions_api import confirm_tool
from jarvis.tests.test_macro_report_wait import (
	_ACME,
	_MACHINE,
	CONV,
	OWNER,
	REPORT_B,
	RUN,
	TURN,
	ParkBase,
)
from jarvis.tests.test_run_report import PREP_REPORT

WAITING = "This step is waiting for a background report; Jarvis will continue it when the report is ready."
FAILED = "This step stopped because a report it needed could not be produced; nothing can be changed in it."
SESSION = "agent:test:report-wait-guard"

# What the agent might call to change something, gated and not: a card is parked for the
# first, the second group runs at once (files, dashboards, memory, write-backs).
GATED = ("send_email", "create_doc", "update_doc", "submit_doc", "delete_doc", "run_method", "add_comment")
UNGATED = ("export_excel", "download_pdf", "save_dashboard", "remember")
READS = ("get_list", "get_doc", "query", "run_report", "get_schema")


class GuardBase(ParkBase):
	"""A run at step 1 whose step turn is live (``self.step_turn``), in a chat the agent
	reaches by session key. ``self.waiting`` adds the answer a step is told to wait on."""

	def setUp(self):
		super().setUp()
		self.run, self.conv, self.step_turn = self._mk_gated()
		frappe.db.set_value(CONV, self.conv, "session_key", SESSION, update_modified=False)
		frappe.db.commit()
		self.pr = self._pr()

	def _waiting(self, **kw):
		self._tool(self.conv, "started", self.pr, waiting=True, **kw)

	def _call(self, tool, args=None):
		"""The agent's call through ``_dispatch_from_session``: ``(result, run_tool, receipt)``."""
		frappe.set_user(OWNER)
		ran = {"ok": True, "data": {}}
		with (
			patch("jarvis.api._get_header", return_value=""),
			patch("jarvis.api._run_tool", return_value=ran) as run_tool,
			patch("jarvis.api._persist_and_publish_tool_call") as receipt,
		):
			result = api._dispatch_from_session(OWNER, SESSION, tool, args or {})
		return result, run_tool, receipt

	def _assert_refused(self, tool, message=WAITING):
		result, run_tool, receipt = self._call(tool)
		self.assertEqual(result["ok"], False, tool)
		self.assertEqual(result["error"], {"code": "ReportWaitError", "message": message}, tool)
		run_tool.assert_not_called()  # no card is parked, nothing is sent or written
		self.assertEqual(receipt.call_args.kwargs["result"], result, tool)

	def _assert_allowed(self, tool):
		result, run_tool, _ = self._call(tool)
		self.assertTrue(result["ok"], (tool, result))
		run_tool.assert_called_once()

	def _live_turn_of_another_message(self, run_id="typed0000001", origin="human"):
		"""The step's turn is over; ``run_id`` is live on a new message of ``origin``."""
		frappe.db.set_value(TURN, self.step_turn, "state", "done", update_modified=False)
		frappe.db.commit()
		self._turn_as(self.conv, run_id, seed=self._msg(self.conv, "user", origin=origin, content="hi"))
		return run_id


class TestAfterAWaitingAnswer(GuardBase):
	def test_every_write_is_refused_with_the_reason(self):
		self._waiting()
		for tool in (*GATED, *UNGATED):
			with self.subTest(tool=tool):
				self._assert_refused(tool)

	def test_nothing_is_written_or_sent(self):
		self._waiting()
		todos = frappe.db.count("ToDo")
		queued = frappe.db.count("Email Queue")
		for tool, args in (
			("create_doc", {"doctype": "ToDo", "values": {"description": "guard-probe"}}),
			("send_email", {"recipients": "nobody@example.com", "subject": "s", "content": "c"}),
		):
			self._call(tool, args)
		self.assertEqual((frappe.db.count("ToDo"), frappe.db.count("Email Queue")), (todos, queued))
		self.assertFalse(pending_confirm.has_live_card(OWNER, self.conv))

	def test_reads_still_work(self):
		self._waiting()
		for tool in READS:
			with self.subTest(tool=tool):
				self._assert_allowed(tool)

	def test_the_refusal_is_a_tool_error_the_model_reads(self):
		self._waiting()
		result, _, _ = self._call("send_email")
		self.assertEqual(set(result), {"ok", "error"})

	def test_a_later_ready_read_of_the_same_report_lifts_it(self):
		self._waiting()
		self._tool(self.conv, "ready", self.pr)
		for tool in ("send_email", "create_doc"):
			with self.subTest(tool=tool):
				self._assert_allowed(tool)

	def test_a_ready_read_of_a_fresh_copy_of_the_same_report_and_filters_lifts_it(self):
		self._waiting()
		self._tool(self.conv, "ready", self._pr(PREP_REPORT, _ACME, "Completed"))
		self._assert_allowed("create_doc")

	def test_a_ready_read_of_another_report_does_not(self):
		self._waiting()
		self._tool(self.conv, "ready", self._pr(REPORT_B, _ACME, "Completed"), report=REPORT_B)
		self._assert_refused("create_doc")

	def test_a_ready_read_of_the_same_report_with_other_filters_does_not(self):
		self._waiting()
		other = self._pr(PREP_REPORT, {"company": "Globex"}, "Completed")
		self._tool(self.conv, "ready", other, filters={"company": "Globex"})
		self._assert_refused("create_doc")

	def test_one_report_still_waiting_keeps_it(self):
		self._waiting()
		second = self._pr(REPORT_B, _ACME)
		self._tool(self.conv, "started", second, report=REPORT_B, waiting=True)
		self._tool(self.conv, "ready", self.pr)
		self._assert_refused("create_doc")

	def test_a_started_answer_without_waiting_is_not_a_wait(self):
		# The switch off, an ordinary chat: today's envelope carries no ``waiting``.
		self._tool(self.conv, "started", self.pr)
		self._assert_allowed("create_doc")

	def test_a_read_that_failed_is_not_a_wait(self):
		self._tool(self.conv, "failed", self.pr, error="Out of memory.")
		self._assert_allowed("create_doc")

	def test_the_continuation_that_waits_again_is_guarded_too(self):
		# Step turn over; the continuation is live and its own answer says waiting.
		frappe.db.set_value(TURN, self.step_turn, "state", "done", update_modified=False)
		self._waiting()  # the original turn's row, outside the continuation's window
		cont = "cont00000001"
		frappe.db.set_value(RUN, self.run, "resume_turn", cont, update_modified=False)
		self._turn_as(self.conv, cont, seed=self._msg(self.conv, "user", origin="continuation", content="go"))
		self._assert_allowed("create_doc")  # nothing yet in ITS turn
		self._tool(self.conv, "started", self._pr(PREP_REPORT, {"company": "Globex"}), waiting=True)
		self._assert_refused("create_doc")


class TestOtherCallers(GuardBase):
	def test_a_persons_card_confirm_in_the_same_chat_still_runs(self):
		self._waiting()
		self._assert_refused("create_doc")  # the agent is held ...
		desc = "report-wait-guard-human-confirm"
		minted = {}
		real = pending_confirm.mint

		def spy(**kwargs):
			minted["token"] = real(**kwargs)
			return minted["token"]

		frappe.set_user(OWNER)
		# ... but a card is still parked by the bench's own gate and the person's Confirm runs it
		with patch("jarvis.chat.pending_confirm.mint", side_effect=spy):
			parked = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": desc}}, conversation=self.conv
			)
		self.assertEqual(parked["data"]["status"], "pending_confirmation", parked)
		with patch("jarvis.chat.api._dispatch_turn"):
			done = confirm_tool(minted["token"], conversation=self.conv)
		self.assertTrue(done["ok"], done)
		created = frappe.db.get_value("ToDo", {"description": desc}, "name")
		self.addCleanup(lambda: frappe.delete_doc("ToDo", created, force=True, ignore_permissions=True))
		self.assertTrue(created)

	def test_the_guard_is_not_in_run_tool(self):
		self._waiting()
		frappe.set_user(OWNER)
		with patch("jarvis.chat.macro_report_wait.write_refusal") as guard:
			api._run_tool("get_schema", {"doctype": "ToDo"}, conversation=self.conv)
		guard.assert_not_called()

	def test_a_typed_follow_up_turn_in_the_chat_is_not_guarded(self):
		self._waiting()
		self._live_turn_of_another_message()
		for tool in ("send_email", "create_doc"):
			with self.subTest(tool=tool):
				self._assert_allowed(tool)

	def test_a_turn_after_the_follow_up_started_is_judged_by_its_own_rows(self):
		self._waiting()
		self._live_turn_of_another_message()
		self._tool(self.conv, "started", self._pr(PREP_REPORT, {"company": "Globex"}), waiting=True)
		self._assert_refused("create_doc")  # its own answer, not the step's

	def test_a_run_that_is_not_running_does_not_guard(self):
		self._waiting()
		frappe.db.set_value(RUN, self.run, "status", "completed", update_modified=False)
		self._assert_allowed("create_doc")

	def test_a_chat_without_a_run_does_not_guard(self):
		frappe.db.delete(RUN, {"name": self.run})
		self._assert_allowed("create_doc")

	def test_no_live_turn_does_not_guard(self):
		self._waiting()
		frappe.db.set_value(TURN, self.step_turn, "state", "done", update_modified=False)
		self._assert_allowed("create_doc")

	def test_no_conversation_does_not_guard(self):
		self.assertIsNone(macro_report_wait.write_refusal(None, "send_email"))

	def test_the_refused_set_is_the_benches_own_write_list(self):
		self.assertTrue(api._GATED_WRITES <= api._WRITE_TOOLS)
		for tool in (*GATED, *UNGATED):
			self.assertIn(tool, api._WRITE_TOOLS)
		for tool in READS:
			self.assertNotIn(tool, api._WRITE_TOOLS)
			self.assertIsNone(macro_report_wait.write_refusal(self.conv, tool))


class TestAFailedWait(GuardBase):
	def setUp(self):
		super().setUp()
		self.cont = "explain00001"
		frappe.db.set_value(
			RUN, self.run, {"waiting_failed": 1, "resume_turn": self.cont}, update_modified=False
		)
		frappe.db.set_value(TURN, self.step_turn, "state", "done", update_modified=False)
		frappe.db.commit()

	def _explanation_live(self):
		self._turn_as(
			self.conv, self.cont, seed=self._msg(self.conv, "user", origin="continuation", content="x")
		)

	def test_the_explanation_turn_may_change_nothing(self):
		self._explanation_live()
		for tool in (*GATED, *UNGATED):
			with self.subTest(tool=tool):
				self._assert_refused(tool, FAILED)

	def test_it_does_not_need_a_waiting_answer(self):
		self._explanation_live()
		self._assert_refused("send_email", FAILED)

	def test_reads_stay_allowed(self):
		self._explanation_live()
		self._assert_allowed("get_list")

	def test_another_turn_of_the_chat_is_not_guarded(self):
		self._live_turn_of_another_message()
		for tool in ("send_email", "create_doc"):
			with self.subTest(tool=tool):
				self._assert_allowed(tool)

	def test_the_steps_original_turn_is_not_the_explanation(self):
		frappe.db.set_value(TURN, self.step_turn, "state", "streaming", update_modified=False)
		frappe.db.commit()
		self._assert_allowed("create_doc")


class TestWhenTheFeatureIsOffOrBroken(GuardBase):
	def test_the_kill_switch_off_does_not_guard(self):
		self._waiting()
		with patch.dict(frappe.conf, {"jarvis_macro_report_wait": 0}):
			self._assert_allowed("send_email")

	def test_a_bench_without_the_turn_machine_does_not_guard(self):
		self._waiting()
		with patch(_MACHINE, return_value=False):
			self._assert_allowed("send_email")

	def test_a_failed_wait_with_the_switch_off_does_not_guard(self):
		frappe.db.set_value(
			RUN, self.run, {"waiting_failed": 1, "resume_turn": self.step_turn}, update_modified=False
		)
		with patch.dict(frappe.conf, {"jarvis_macro_report_wait": 0}):
			self._assert_allowed("send_email")

	def test_a_lookup_error_allows_the_call_and_logs_once(self):
		self._waiting()
		frappe.cache().delete_value(f"jarvis_macro_wait_guard_log:{self.conv}")
		with (
			patch("jarvis.chat.held_sheets.live_turn", side_effect=RuntimeError("db gone")),
			patch("frappe.log_error") as log,
		):
			result, run_tool, _ = self._call("send_email")
		self.assertTrue(result["ok"])
		run_tool.assert_called_once()
		log.assert_called_once()
		self.assertEqual(log.call_args.kwargs["title"], "jarvis macro report wait guard failed")

	def test_a_failing_guard_logs_at_most_once_an_hour_per_chat(self):
		self._waiting()
		frappe.cache().delete_value(f"jarvis_macro_wait_guard_log:{self.conv}")
		with (
			patch("jarvis.chat.held_sheets.live_turn", side_effect=RuntimeError("db gone")),
			patch("frappe.log_error") as log,
		):
			for _ in range(3):
				result, _run_tool, _ = self._call("send_email")
				self.assertTrue(result["ok"])
		log.assert_called_once()
		self.addCleanup(lambda: frappe.cache().delete_value(f"jarvis_macro_wait_guard_log:{self.conv}"))

	def test_an_unreadable_rows_lookup_allows_the_call_and_logs_once(self):
		self._waiting()
		key = f"jarvis_macro_wait_log:jarvis macro report wait read failed:{self.conv}"
		frappe.cache().delete_value(key)
		self.addCleanup(lambda: frappe.cache().delete_value(key))
		with (
			patch.object(macro_report_wait, "_rows", side_effect=RuntimeError("db gone")),
			patch("frappe.log_error") as log,
		):
			result, run_tool, _ = self._call("send_email")
		self.assertTrue(result["ok"])
		run_tool.assert_called_once()
		log.assert_called_once()

	def test_a_read_costs_no_lookup(self):
		self._waiting()
		with patch.object(macro_report_wait, "_write_refusal") as lookup:
			self.assertIsNone(macro_report_wait.write_refusal(self.conv, "get_list"))
		lookup.assert_not_called()


class TestTheWords(GuardBase):
	def test_the_messages(self):
		self.assertEqual(macro_report_wait.GUARD_WAITING, WAITING)
		self.assertEqual(macro_report_wait.GUARD_FAILED, FAILED)
		self.assertNotIn("\u2014", WAITING + FAILED)
