"""Tests for the macro-merge surface (summarize a step sequence into one prompt).

The LLM summarize turn itself is exercised by the live smoke; these tests
cover the deterministic plumbing: enqueue + throwaway conversation, the poll
state machine parsing the jarvis-macro-merge block, and the apply that
collapses the steps (skills union, first non-empty overrides), all
owner-gated.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.macros_api import (
	apply_macro_merge,
	discard_macro_merge,
	get_macro_merge,
	summarize_macro,
)

MERGE_BLOCK = (
	"Here you go:\n\n```jarvis-macro-merge\n"
	'{"mergeable": true, "reason": "2 uses 1", '
	'"merged_prompt": "1) Analytics.\\n2) Using the results of (1), find the top debtor.", '
	'"dependencies": [{"step": 2, "uses": [1]}]}\n```'
)


def _mk_macro(steps):
	doc = frappe.get_doc(
		{
			"doctype": "Jarvis Macro",
			"macro_name": f"merge-test-{frappe.generate_hash(length=6)}",
			"enabled": 1,
			"steps": steps,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _mk_conv(assistant=None, streaming=0, error=""):
	conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "merge test"})
	conv.flags.ignore_permissions = True
	conv.insert()
	if assistant is not None:
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": conv.name,
				"seq": 2,
				"role": "assistant",
				"content": assistant,
				"streaming": streaming,
				"error": error,
			}
		).insert(ignore_permissions=True)
	return conv.name


class _MacroMergeBase(FrappeTestCase):
	"""Isolation base for the macro-merge tests.

	Several of these flows COMMIT mid-test — summarize/run dispatch through
	``api._enqueue_turn`` (which commits) and ``_pending_macro_with_reply`` /
	``advance_after_turn`` commit outright — so a macro created by ``_mk_macro`` is
	made durable and survives the per-test rollback. Counted against the per-owner
	cap (``MAX_MACROS_PER_OWNER`` = 25), leaked survivors from earlier tests/runs
	eventually make EVERY ``_mk_macro`` insert throw "You can have at most 25
	macros." Clear the committed test macros up front so the cap is never exhausted.
	Transport-independent: the leak is the commit, not the pump."""

	def setUp(self):
		super().setUp()
		for n in frappe.get_all(
			"Jarvis Macro", filters={"macro_name": ["like", "merge-test-%"]}, pluck="name"
		):
			frappe.delete_doc("Jarvis Macro", n, force=True, ignore_permissions=True)
		frappe.db.commit()


class TestSummarizeMacro(_MacroMergeBase):
	def test_enqueues_one_turn_with_steps_and_skill(self):
		m = _mk_macro(
			[
				{"label": "a", "prompt": "Sales analytics for last quarter"},
				{"label": "b", "prompt": "Find the highest outstanding customer"},
			]
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		with patch("jarvis.chat.api._enqueue_turn") as enq:
			r = summarize_macro(m.name)
		self.assertTrue(r["ok"])
		conv = r["conversation"]
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True)
		)
		# one turn, into the returned conversation, steps JSON + skill invocation in the prompt
		enq.assert_called_once()
		args, kwargs = enq.call_args
		self.assertEqual(args[0], conv)
		self.assertIn("Sales analytics for last quarter", args[1])
		self.assertIn("/macro-merge", args[1])
		# throwaway conversation is hidden from the sidebar
		self.assertEqual(frappe.db.get_value("Jarvis Conversation", conv, "status"), "Archived")
		# the macro is marked "summarizing" so Run is gated until the worker applies
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_status"), "pending")
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_conversation"), conv)

	def test_rejects_single_step_macro(self):
		m = _mk_macro([{"label": "only", "prompt": "one thing"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		with self.assertRaises(frappe.ValidationError):
			summarize_macro(m.name)


class TestGetMacroMerge(_MacroMergeBase):
	def _cleanup_conv(self, conv):
		self.addCleanup(
			lambda: (
				frappe.db.delete("Jarvis Chat Message", {"conversation": conv}),
				frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True),
			)
		)

	def test_pending_when_no_reply(self):
		conv = _mk_conv(assistant=None)
		self._cleanup_conv(conv)
		self.assertEqual(get_macro_merge(conv)["status"], "pending")

	def test_pending_while_streaming(self):
		conv = _mk_conv(assistant="partial", streaming=1)
		self._cleanup_conv(conv)
		self.assertEqual(get_macro_merge(conv)["status"], "pending")

	def test_error_from_turn_error(self):
		conv = _mk_conv(assistant="", error="quota exceeded")
		self._cleanup_conv(conv)
		r = get_macro_merge(conv)
		self.assertEqual(r["status"], "error")
		self.assertIn("quota", r["error"])

	def test_error_when_no_block(self):
		conv = _mk_conv(assistant="I could not do that.")
		self._cleanup_conv(conv)
		self.assertEqual(get_macro_merge(conv)["status"], "error")

	def test_ready_parses_block(self):
		conv = _mk_conv(assistant=MERGE_BLOCK)
		self._cleanup_conv(conv)
		r = get_macro_merge(conv)
		self.assertEqual(r["status"], "ready")
		self.assertTrue(r["merge"]["mergeable"])
		self.assertIn("Using the results of (1)", r["merge"]["merged_prompt"])
		self.assertEqual(r["merge"]["dependencies"], [{"step": 2, "uses": [1]}])

	def test_ownership_enforced(self):
		conv = _mk_conv(assistant=MERGE_BLOCK)
		self._cleanup_conv(conv)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_macro_merge(conv)
		finally:
			frappe.set_user("Administrator")


class TestApplyMacroMerge(_MacroMergeBase):
	def test_stores_summary_and_keeps_steps(self):
		# The sequence stays as the editable source; the summary rides alongside
		# and (see TestMergedRun) is what run_macro executes.
		m = _mk_macro(
			[
				{"label": "a", "prompt": "p1"},
				{"label": "b", "prompt": "p2"},
				{"label": "c", "prompt": "p3"},
			]
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		r = apply_macro_merge(m.name, "Do p1, and from those results p2, then p3.")
		self.assertEqual(r["step_count"], 3)  # steps untouched
		doc = frappe.get_doc("Jarvis Macro", m.name)
		self.assertEqual(len(doc.steps), 3)
		self.assertEqual([s.prompt for s in doc.steps], ["p1", "p2", "p3"])
		self.assertIn("from those results", doc.merged_prompt)

	def test_empty_prompt_refused(self):
		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		with self.assertRaises(frappe.ValidationError):
			apply_macro_merge(m.name, "   ")

	def test_clear_macro_merge(self):
		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		apply_macro_merge(m.name, "the summary")
		from jarvis.chat.macros_api import clear_macro_merge

		clear_macro_merge(m.name)
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merged_prompt") or "", "")

	def test_update_steps_clears_stale_summary(self):
		from jarvis.chat.macros_api import get_macro, update_macro

		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		apply_macro_merge(m.name, "the summary")
		# steps replaced without a merged_prompt in the same call → summary is stale → cleared
		update_macro(m.name, steps=frappe.as_json([{"prompt": "p1 changed"}, {"prompt": "p2"}]))
		self.assertEqual(get_macro(m.name)["merged_prompt"], "")
		# but sending merged_prompt alongside keeps/sets it
		update_macro(
			m.name, steps=frappe.as_json([{"prompt": "p1"}, {"prompt": "p2"}]), merged_prompt="edited summary"
		)
		self.assertEqual(get_macro(m.name)["merged_prompt"], "edited summary")


class TestMergeApplyHook(_MacroMergeBase):
	"""The worker-side apply: advance_after_turn lands the summary on the macro
	when the background summarize turn ends — no browser needed."""

	def _pending_macro_with_reply(self, reply_content, errored=False):
		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		conv = _mk_conv(assistant=reply_content)
		frappe.db.set_value(
			"Jarvis Macro",
			m.name,
			{
				"merge_status": "pending",
				"merge_conversation": conv,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return m, conv

	def test_ready_reply_applies_summary_and_cleans_up(self):
		from jarvis.chat import macros

		m, conv = self._pending_macro_with_reply(MERGE_BLOCK)
		macros.advance_after_turn(conv, errored=False)
		doc = frappe.get_doc("Jarvis Macro", m.name)
		self.assertEqual(doc.merge_status, "ready")
		self.assertIn("Using the results of (1)", doc.merged_prompt)
		self.assertEqual(doc.merge_conversation or "", "")
		self.assertFalse(frappe.db.exists("Jarvis Conversation", conv))  # throwaway cleaned

	def test_errored_turn_marks_failed_and_steps_still_run(self):
		from jarvis.chat import macros

		m, conv = self._pending_macro_with_reply("irrelevant")
		macros.advance_after_turn(conv, errored=True)
		doc = frappe.get_doc("Jarvis Macro", m.name)
		self.assertEqual(doc.merge_status, "failed")
		self.assertEqual(doc.merged_prompt or "", "")
		# failed ≠ blocked: the sequence runs (checked in TestMergedRun fallback)

	def test_unmergeable_reply_marks_failed(self):
		from jarvis.chat import macros

		block = (
			'ok\n\n```jarvis-macro-merge\n{"mergeable": false, "reason": "checkpoint", '
			'"merged_prompt": "", "dependencies": []}\n```'
		)
		m, conv = self._pending_macro_with_reply(block)
		macros.advance_after_turn(conv, errored=False)
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_status"), "failed")

	def test_run_blocked_while_pending(self):
		from jarvis.chat import macros

		m, conv = self._pending_macro_with_reply(MERGE_BLOCK)
		self.addCleanup(
			lambda: (
				frappe.db.delete("Jarvis Chat Message", {"conversation": conv}),
				frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True),
			)
		)
		with self.assertRaises(frappe.ValidationError):
			macros.run_macro(m.name)


class TestMergedRun(_MacroMergeBase):
	def test_run_macro_uses_summary_as_single_turn(self):
		from jarvis.chat import macros

		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}, {"prompt": "p3"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		apply_macro_merge(m.name, "One prompt to rule them all.")
		with patch("jarvis.chat.api._enqueue_turn") as enq:
			r = macros.run_macro(m.name)
		run_name = r["data"]["macro_run"]
		conv = r["data"]["conversation"]
		self.addCleanup(
			lambda: (
				frappe.delete_doc("Jarvis Macro Run", run_name, force=True, ignore_permissions=True),
				frappe.db.delete("Jarvis Chat Message", {"conversation": conv}),
				frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True),
			)
		)
		enq.assert_called_once()  # ONE turn, not three
		self.assertTrue(enq.call_args[0][1].startswith("One prompt to rule them all."))
		run = frappe.get_doc("Jarvis Macro Run", run_name)
		self.assertEqual(run.total_steps, 1)
		self.assertEqual(run.current_step, 1)

	def test_run_macro_without_summary_chains_steps(self):
		from jarvis.chat import macros

		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		with patch("jarvis.chat.api._enqueue_turn") as enq:
			r = macros.run_macro(m.name)
		run_name = r["data"]["macro_run"]
		conv = r["data"]["conversation"]
		self.addCleanup(
			lambda: (
				frappe.delete_doc("Jarvis Macro Run", run_name, force=True, ignore_permissions=True),
				frappe.db.delete("Jarvis Chat Message", {"conversation": conv}),
				frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True),
			)
		)
		enq.assert_called_once()  # step 1 enqueued; the chain advances per turn
		self.assertTrue(enq.call_args[0][1].startswith("p1"))
		self.assertEqual(frappe.get_doc("Jarvis Macro Run", run_name).total_steps, 2)


class TestDiscardMacroMerge(FrappeTestCase):
	def test_deletes_conversation_and_messages(self):
		conv = _mk_conv(assistant=MERGE_BLOCK)
		discard_macro_merge(conv)
		self.assertFalse(frappe.db.exists("Jarvis Conversation", conv))
		self.assertEqual(frappe.db.count("Jarvis Chat Message", {"conversation": conv}), 0)


class TestMacroCapacityDefer(_MacroMergeBase):
	"""CDX-19 (residual) — when a macro step cannot be admitted (the site's turn queue is full,
	`_enqueue_turn` returns overloaded), the run must NOT advance and must NOT wait forever for a
	turn-end that never comes. It parks in `waiting_capacity`; the resume cron re-attempts the
	SAME step, bounded by capacity_attempts, then fails honestly. `_enqueue_turn` is mocked here
	to drive the run state machine deterministically (its own seed/Turn disposition is covered in
	test_chat_admission)."""

	RUN = "Jarvis Macro Run"

	def _mk_two_step(self):
		m = _mk_macro([{"label": "a", "prompt": "first"}, {"label": "b", "prompt": "second"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		return m

	def _cleanup_run(self, run_name):
		self.addCleanup(lambda: frappe.delete_doc(self.RUN, run_name, force=True, ignore_permissions=True))

	def test_overloaded_step_parks_waiting_capacity_not_advanced(self):
		from jarvis.chat import macros

		m = self._mk_two_step()
		overload = {
			"ok": False,
			"overloaded": True,
			"reason": "The site is busy — please try again in a moment.",
		}
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			res = macros.run_macro(m.name)
		run_name = res["data"]["macro_run"]
		self._cleanup_run(run_name)
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "waiting_capacity")
		# The step is NOT advanced — current_step still points at step 0 for the resume to retry.
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "current_step"), 0)

	def test_resume_cron_heals_once_capacity_frees(self):
		from jarvis.chat import macros

		m = self._mk_two_step()
		overload = {"ok": False, "overloaded": True, "reason": "busy"}
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			res = macros.run_macro(m.name)
		run_name = res["data"]["macro_run"]
		self._cleanup_run(run_name)
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "waiting_capacity")
		# Capacity frees: the resume cron re-attempts the SAME step, which now dispatches.
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r1", "message_id": "m1"}) as enq:
			macros.resume_waiting_capacity_runs()
		enq.assert_called_once()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "running")
		self.assertEqual(
			frappe.db.get_value(self.RUN, run_name, "current_step"), 1, "step advanced on the heal"
		)
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "capacity_attempts"), 1)

	def test_resume_bounded_then_fails_honestly(self):
		from jarvis.chat import macros

		m = self._mk_two_step()
		overload = {"ok": False, "overloaded": True, "reason": "busy"}
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			res = macros.run_macro(m.name)
		run_name = res["data"]["macro_run"]
		self._cleanup_run(run_name)
		# Fast-forward to the attempt ceiling; the next resume exceeds it and fails the run.
		frappe.db.set_value(self.RUN, run_name, "capacity_attempts", macros._MAX_CAPACITY_ATTEMPTS)
		frappe.db.commit()
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			macros.resume_waiting_capacity_runs()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "failed")
		self.assertIn("busy", (frappe.db.get_value(self.RUN, run_name, "error") or "").lower())


class TestMacroStopResumeSerialization(_MacroMergeBase):
	"""CDX-22 — the capacity-resume critical section is serialized with stop (state-fenced flip +
	pre-enqueue re-check), so neither ordering lets a resume erase a stop or start work after it.
	CDX-23 — a resumed enqueue that RAISES is compensated so the run never strands ``running``."""

	RUN = "Jarvis Macro Run"

	def _mk_two_step(self):
		m = _mk_macro([{"label": "a", "prompt": "first"}, {"label": "b", "prompt": "second"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		return m

	def _parked_run(self):
		from jarvis.chat import macros

		overload = {"ok": False, "overloaded": True, "reason": "busy"}
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			res = macros.run_macro(self._mk_two_step().name)
		run_name = res["data"]["macro_run"]
		self.addCleanup(lambda: frappe.delete_doc(self.RUN, run_name, force=True, ignore_permissions=True))
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "waiting_capacity")
		return run_name

	# ---- CDX-22 ordering A: stop lands BEFORE the resume flip ------------------- #
	def test_stop_before_resume_write_aborts_no_enqueue(self):
		from jarvis.chat import macros

		run_name = self._parked_run()
		macros.stop_macro_run(run_name)  # writes stopped under the SAME per-run lock
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "stopped")
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}) as enq:
			macros.resume_waiting_capacity_runs()
		# The state-fenced flip (WHERE status='waiting_capacity') matches 0 rows on a stopped run.
		enq.assert_not_called()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "stopped")

	# ---- CDX-22 ordering B: stop lands AFTER the flip, before the enqueue ------- #
	def test_stop_after_resume_write_before_enqueue_aborts(self):
		from jarvis.chat import macros

		run_name = self._parked_run()

		def fake_status(rn):
			# Simulate a stop landing after the flip-to-running commit and before the pre-enqueue
			# eligibility re-check (e.g. the redis lock's TTL lapsed).
			frappe.db.set_value(self.RUN, rn, {"status": "stopped"}, update_modified=True)
			frappe.db.commit()
			return "stopped"

		with (
			patch.object(macros, "_run_status_now", side_effect=fake_status),
			patch("jarvis.chat.api._enqueue_turn") as enq,
		):
			macros.resume_waiting_capacity_runs()
		enq.assert_not_called()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "stopped")

	# ---- CDX-23: a resumed enqueue that RAISES is compensated ------------------- #
	def test_resume_enqueue_exception_restores_waiting_capacity_then_retries(self):
		from jarvis.chat import macros

		run_name = self._parked_run()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("boom")):
			macros.resume_waiting_capacity_runs()
		# Compensated back to waiting_capacity (NOT stranded 'running'); the attempt was counted.
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "waiting_capacity")
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "capacity_attempts"), 1)
		# Next cycle: capacity frees and the SAME step dispatches.
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}) as enq:
			macros.resume_waiting_capacity_runs()
		enq.assert_called_once()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "running")
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "capacity_attempts"), 2)

	def test_resume_enqueue_exception_at_cap_fails_honestly(self):
		from jarvis.chat import macros

		run_name = self._parked_run()
		# One shy of the cap so THIS resume's attempt (=_MAX) is the last allowed one.
		frappe.db.set_value(self.RUN, run_name, "capacity_attempts", macros._MAX_CAPACITY_ATTEMPTS - 1)
		frappe.db.commit()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("boom")):
			macros.resume_waiting_capacity_runs()
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "status"), "failed")
		self.assertIn("busy", (frappe.db.get_value(self.RUN, run_name, "error") or "").lower())
