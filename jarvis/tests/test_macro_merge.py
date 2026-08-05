"""Tests for the macro-merge surface (summarize a step sequence into one prompt).

The LLM summarize turn itself is exercised by the live smoke; these tests
cover the deterministic plumbing: enqueue + throwaway conversation, the
worker-side apply that lands the summary on the macro doc, and running the
merged prompt as a single turn (skills union, first non-empty overrides),
all owner-gated.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat.macros_api import summarize_macro, update_macro

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

	def test_pending_written_before_dispatch(self):
		"""Regression: dispatch can complete the turn inline before _enqueue_turn
		returns, so the advance hook's lookup for {merge_conversation, merge_status
		== "pending"} must never miss. Assert "pending" is already on the macro
		at the moment _enqueue_turn is invoked, not after."""
		m = _mk_macro(
			[
				{"label": "a", "prompt": "Sales analytics for last quarter"},
				{"label": "b", "prompt": "Find the highest outstanding customer"},
			]
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		seen = {}

		def _capture(conversation, prompt):
			seen["merge_status"] = frappe.db.get_value("Jarvis Macro", m.name, "merge_status")
			seen["merge_conversation"] = frappe.db.get_value("Jarvis Macro", m.name, "merge_conversation")
			return {"run_id": "r1", "message_id": "m1"}

		with patch("jarvis.chat.api._enqueue_turn", side_effect=_capture):
			r = summarize_macro(m.name)
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Jarvis Conversation", r["conversation"], force=True, ignore_permissions=True
			)
		)
		self.assertEqual(seen["merge_status"], "pending")
		self.assertEqual(seen["merge_conversation"], r["conversation"])

	def test_overloaded_dispatch_clears_pending_mark(self):
		"""When dispatch reports overloaded, the pending mark written up front
		must be rolled back — otherwise Run stays gated forever on a summary
		turn that never ran (get_macro_merge would poll pending forever)."""
		m = _mk_macro(
			[
				{"label": "a", "prompt": "Sales analytics for last quarter"},
				{"label": "b", "prompt": "Find the highest outstanding customer"},
			]
		)
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		overload = {"ok": False, "overloaded": True, "reason": "busy"}
		with patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			r = summarize_macro(m.name)
		self.assertFalse(r["ok"])
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_status"), "")
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_conversation"), "")


class TestUpdateMacroSummary(_MacroMergeBase):
	"""update_macro is the sole surviving surface that writes merged_prompt from
	the SPA (the poll/apply/clear/discard merge endpoints were removed as dead
	REST surface - jarvis#474 - once the UI moved to reading merge_status /
	merged_prompt straight off get_macro and editing the summary as a plain
	form field)."""

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
		update_macro(m.name, merged_prompt="Do p1, and from those results p2, then p3.")
		doc = frappe.get_doc("Jarvis Macro", m.name)
		self.assertEqual(len(doc.steps), 3)  # steps untouched
		self.assertEqual([s.prompt for s in doc.steps], ["p1", "p2", "p3"])
		self.assertIn("from those results", doc.merged_prompt)
		self.assertEqual(doc.merge_status, "ready")

	def test_clearing_merged_prompt_clears_merge_status(self):
		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		update_macro(m.name, merged_prompt="the summary")
		update_macro(m.name, merged_prompt="")
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merged_prompt") or "", "")
		self.assertEqual(frappe.db.get_value("Jarvis Macro", m.name, "merge_status") or "", "")

	def test_update_steps_clears_stale_summary(self):
		from jarvis.chat.macros_api import get_macro

		m = _mk_macro([{"prompt": "p1"}, {"prompt": "p2"}])
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Macro", m.name, force=True, ignore_permissions=True)
		)
		update_macro(m.name, merged_prompt="the summary")
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

	def test_a_fault_in_the_merge_path_lands_failed_not_pending(self):
		"""#632: a code fault used to skip the landing write entirely, leaving
		merge_status on "pending" forever. "pending" means "still merging", so nothing
		ever revisits it: the Run button never enables and a genuine bug is
		indistinguishable from work in progress. The concrete case was an ImportError
		from the lazy cross-file ``_MERGE_RE`` import, which no test on either side of
		that boundary noticed."""
		from jarvis.chat import macros

		m, conv = self._pending_macro_with_reply(MERGE_BLOCK)
		with patch.object(macros, "_merge_outcome", side_effect=ImportError("boom")):
			macros.advance_after_turn(conv, errored=False)

		doc = frappe.get_doc("Jarvis Macro", m.name)
		self.assertEqual(doc.merge_status, "failed", "a fault must not leave the macro pending")
		self.assertEqual(doc.merged_prompt or "", "")
		self.assertEqual(doc.merge_conversation or "", "", "the throwaway link must be cleared")

	def test_a_fault_in_the_merge_path_is_logged_not_silent(self):
		"""The other half: the fault is re-raised out of _apply_merge_after_turn so the
		caller records a real traceback. Before this it was swallowed with no log, no
		failed status and no user-visible error."""
		from jarvis.chat import macros

		m, conv = self._pending_macro_with_reply(MERGE_BLOCK)
		with (
			patch.object(macros, "_merge_outcome", side_effect=ImportError("boom")),
			patch.object(macros.frappe, "log_error") as logged,
		):
			macros.advance_after_turn(conv, errored=False)

		self.assertTrue(logged.called, "a code fault here must reach the Error Log")
		titles = " ".join(str(c.kwargs.get("title", "")) for c in logged.call_args_list)
		self.assertIn("merge-apply failed", titles)

	def test_a_fault_still_does_not_strand_the_turn(self):
		"""The contract advance_after_turn's docstring states: a macro bug must never
		raise into a normal turn. Re-raising from _apply_merge_after_turn must not
		change that, so the outer guard is what callers keep relying on."""
		from jarvis.chat import macros

		_, conv = self._pending_macro_with_reply(MERGE_BLOCK)
		with patch.object(macros, "_merge_outcome", side_effect=ImportError("boom")):
			macros.advance_after_turn(conv, errored=False)  # must not raise

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
		update_macro(m.name, merged_prompt="One prompt to rule them all.")
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


class TestMacroRunModeSnapshot(_MacroMergeBase):
	"""#470 — merged-vs-stepped is fixed when the run is DISPATCHED, never re-decided on
	a later dispatch.

	A run parked in ``waiting_capacity`` waits up to ``_MAX_CAPACITY_ATTEMPTS`` x the 5 min
	resume cron (~100 min), and macro edits take no per-run lock, so the macro the resume
	re-read could easily have changed shape underneath it. Driving the state machine with a
	stubbed ``api._enqueue_turn`` lets every dispatched prompt be asserted in order, which
	is what makes "did not re-run a step" and "did not complete early" observable."""

	MACRO = "Jarvis Macro"
	RUN = "Jarvis Macro Run"

	def _mk(self, n: int):
		m = _mk_macro([{"label": f"s{i}", "prompt": f"p{i}"} for i in range(1, n + 1)])
		self.addCleanup(lambda: frappe.delete_doc(self.MACRO, m.name, force=True, ignore_permissions=True))
		return m

	def _recorder(self):
		"""A stubbed dispatcher that records the prompts it actually sent, and can be
		flipped to report the accept gate as overloaded."""
		state = {"overloaded": False, "sent": []}

		def fake_enqueue(_conversation, prompt, **_kw):
			if state["overloaded"]:
				return {"ok": False, "overloaded": True, "reason": "busy"}
			state["sent"].append(prompt)
			return {"run_id": "r", "message_id": "m"}

		return state, fake_enqueue

	def _start(self, macro):
		from jarvis.chat import macros

		res = macros.run_macro(macro.name)
		run_name, conv = res["data"]["macro_run"], res["data"]["conversation"]
		self.addCleanup(
			lambda: (
				frappe.delete_doc(self.RUN, run_name, force=True, ignore_permissions=True),
				frappe.db.delete("Jarvis Chat Message", {"conversation": conv}),
				frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True),
			)
		)
		return run_name, conv

	def _row(self, run_name):
		return frappe.db.get_value(
			self.RUN, run_name, ["status", "current_step", "total_steps", "run_mode"], as_dict=True
		)

	def _land_summary(self, macro_name, text="MERGED"):
		"""What ``_apply_merge_after_turn`` does when a background summarize lands."""
		frappe.db.set_value(
			self.MACRO,
			macro_name,
			{"merged_prompt": text, "merge_status": "ready"},
			update_modified=False,
		)
		frappe.db.commit()

	# ---- the snapshot itself ---------------------------------------------------- #
	def test_mode_is_snapshotted_on_the_run_at_dispatch(self):
		state, fake = self._recorder()
		stepped = self._mk(3)
		merged = self._mk(3)
		update_macro(merged.name, merged_prompt="MERGED")
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			s_run, _ = self._start(stepped)
			m_run, _ = self._start(merged)
		self.assertEqual(self._row(s_run).run_mode, "stepped")
		self.assertEqual(self._row(m_run).run_mode, "merged")
		self.assertEqual(state["sent"], ["p1", "MERGED"])

	# ---- direction (a): a stepped run that gains a summary mid-park -------------- #
	def test_stepped_run_that_gains_a_summary_mid_park_stays_stepped(self):
		from jarvis.chat import macros

		m = self._mk(3)
		state, fake = self._recorder()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)  # step 1 dispatches
			state["overloaded"] = True
			macros.advance_after_turn(conv, errored=False)  # step 2 cannot be admitted
			self.assertEqual(self._row(run_name).status, "waiting_capacity")
			self.assertEqual(self._row(run_name).current_step, 1, "a parked step must not advance")

			# A background summarize completes while the run sits parked.
			self._land_summary(m.name)
			state["overloaded"] = False
			macros.resume_waiting_capacity_runs()
			macros.advance_after_turn(conv, errored=False)
			macros.advance_after_turn(conv, errored=False)

		# Re-deriving the mode here ran the 3-step summary into the SAME conversation and
		# reset current_step to 1, so steps 2 and 3 executed a second time afterwards.
		self.assertEqual(state["sent"], ["p1", "p2", "p3"])
		self.assertEqual(self._row(run_name).status, "completed")

	# ---- direction (b): a merged run whose summary is cleared mid-park ----------- #
	def test_merged_run_whose_summary_is_cleared_mid_park_does_not_complete_early(self):
		from jarvis.chat import macros

		m = self._mk(3)
		update_macro(m.name, merged_prompt="MERGED")
		state, fake = self._recorder()
		state["overloaded"] = True
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)
			self.assertEqual(self._row(run_name).status, "waiting_capacity")

			# The user edits the steps mid-park; update_macro clears the stale summary.
			update_macro(m.name, steps=[{"prompt": f"q{i}"} for i in range(1, 5)])
			self.assertFalse(frappe.db.get_value(self.MACRO, m.name, "merged_prompt"))

			state["overloaded"] = False
			macros.resume_waiting_capacity_runs()
			macros.advance_after_turn(conv, errored=False)

		# Re-deriving the mode ran q1 as a step and then finished the run "completed" off
		# total=min(total_steps=1, 4)=1, i.e. 1 of 4 steps with a success in the history.
		row = self._row(run_name)
		self.assertNotEqual(row.status, "completed", "1 of 4 steps ran and the run claimed success")
		self.assertEqual(row.status, "failed")
		self.assertEqual(state["sent"], [], "nothing may dispatch once the snapshot is unrunnable")
		self.assertIn("edited", (frappe.db.get_value(self.RUN, run_name, "error") or "").lower())

	def test_stepped_run_whose_steps_shrank_past_the_cursor_fails_honestly(self):
		from jarvis.chat import macros

		m = self._mk(3)
		state, fake = self._recorder()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)  # p1 dispatches, current_step = 1
			state["overloaded"] = True
			macros.advance_after_turn(conv, errored=False)  # parks at current_step = 1
			# The macro is cut to a single step, so index 1 no longer exists. Left alone
			# this is an IndexError re-raised once per cron cycle until the attempt cap.
			update_macro(m.name, steps=[{"prompt": "only"}])
			state["overloaded"] = False
			macros.resume_waiting_capacity_runs()

		self.assertEqual(state["sent"], ["p1"])
		self.assertEqual(self._row(run_name).status, "failed")
		self.assertEqual(frappe.db.get_value(self.RUN, run_name, "capacity_attempts"), 0)

	# ---- the unedited paths still work ------------------------------------------ #
	def test_normal_merged_run_still_works_end_to_end(self):
		from jarvis.chat import macros

		m = self._mk(3)
		update_macro(m.name, merged_prompt="MERGED")
		state, fake = self._recorder()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)
			macros.advance_after_turn(conv, errored=False)
		self.assertEqual(state["sent"], ["MERGED"])
		row = self._row(run_name)
		self.assertEqual((row.status, row.total_steps, row.current_step), ("completed", 1, 1))

	def test_normal_stepped_run_still_works_end_to_end(self):
		from jarvis.chat import macros

		m = self._mk(3)
		state, fake = self._recorder()
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)
			for _ in range(3):
				macros.advance_after_turn(conv, errored=False)
		self.assertEqual(state["sent"], ["p1", "p2", "p3"])
		row = self._row(run_name)
		self.assertEqual((row.status, row.total_steps, row.current_step), ("completed", 3, 3))

	def test_parked_and_unedited_run_resumes_in_its_own_mode(self):
		from jarvis.chat import macros

		m = self._mk(3)
		update_macro(m.name, merged_prompt="MERGED")
		state, fake = self._recorder()
		state["overloaded"] = True
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, conv = self._start(m)
			state["overloaded"] = False
			macros.resume_waiting_capacity_runs()
			macros.advance_after_turn(conv, errored=False)
		self.assertEqual(state["sent"], ["MERGED"])
		self.assertEqual(self._row(run_name).status, "completed")

	# ---- interaction with the #471 reaper ---------------------------------------- #
	def test_a_parked_run_stays_distinguishable_from_a_stranded_one(self):
		from jarvis.chat import macros

		m = self._mk(3)
		state, fake = self._recorder()
		state["overloaded"] = True
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			run_name, _conv = self._start(m)
		# Aged far past the stale-run cutoff: only the reaper's `running`-only status
		# allowlist can be saving it, and the snapshot does not disturb that.
		frappe.db.sql(
			"UPDATE `tabJarvis Macro Run` SET modified=%(t)s WHERE name=%(n)s",
			{"t": add_to_date(now_datetime(), seconds=-macros.STALE_RUN_AFTER_SECONDS * 4), "n": run_name},
		)
		frappe.db.commit()
		macros.reap_stale_macro_runs()
		self.assertEqual(self._row(run_name).status, "waiting_capacity", "a parked run was reaped")
		# And it is still resumable afterwards.
		state["overloaded"] = False
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			macros.resume_waiting_capacity_runs()
		self.assertEqual(state["sent"], ["p1"])
		self.assertEqual(self._row(run_name).status, "running")

	# ---- rows written before the column existed ---------------------------------- #
	def test_legacy_run_without_a_snapshot_falls_back_to_total_steps(self):
		from jarvis.chat import macros

		# A merged run that was already parked when the column shipped: run_mode is NULL,
		# and total_steps=1 against a multi-step macro is the only record of its shape.
		merged_macro = self._mk(3)
		update_macro(merged_macro.name, merged_prompt="MERGED")
		stepped_macro = self._mk(3)
		state, fake = self._recorder()
		state["overloaded"] = True
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			merged_run, _ = self._start(merged_macro)
			stepped_run, _ = self._start(stepped_macro)
		frappe.db.sql(
			"UPDATE `tabJarvis Macro Run` SET run_mode=NULL WHERE name IN (%(a)s, %(b)s)",
			{"a": merged_run, "b": stepped_run},
		)
		# A summary lands on the stepped macro too, which is exactly what used to flip it.
		self._land_summary(stepped_macro.name, "WRONG")
		frappe.db.commit()

		state["overloaded"] = False
		with patch("jarvis.chat.api._enqueue_turn", side_effect=fake):
			macros.resume_waiting_capacity_runs()
		self.assertIn("MERGED", state["sent"])
		self.assertIn("p1", state["sent"])
		self.assertNotIn("WRONG", state["sent"])
