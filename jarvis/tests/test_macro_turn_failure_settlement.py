"""#2194 / #2195: a macro's summary turn or step turn that FAILS must settle the macro.

The existing macro tests put a Turn in its end state by hand (``_force`` /
``_settle_success``) and then call the hook or the five minute check. These play real
gateway frames through the real chain instead:

    macro (real summarize_macro / _dispatch_step) -> queued Turn -> pump.drain_slice
    -> RelayMux -> invoke_settlement -> finalize.run_finalize -> macro_advance effect
    -> macros.advance_after_turn

Only the gateway socket and the prepare job are doubled (``_DoubleGateway``,
``_prepare_stub``), as in ``test_pump_pipeline``. Nothing here calls ``_land_summary``
or ``_apply_step_end`` directly.

Frames played (``TRANSCRIPTS`` patched in per test, the shipped ones are not changed):

* ``empty-final``: a chat final with no assistant message (the runtime's shape for a
  turn that produced nothing).
* ``limit-chat-error``: a chat ``state=error`` whose message is the Claude CLI quota text.
* ``limit-lifecycle-final``: only an agent lifecycle error frame carrying that text,
  then the empty final the gateway sends after it.
* ``limit-lifecycle-cut``: a lifecycle error frame, then the stream is cut with NO chat
  event at all. The turn can only end through recovery.

Needs a Frappe site and database, so it is not runnable without one. Run it on a
scratch site that is not a tenant of anyone, see the commands in the PR notes.
"""

from __future__ import annotations

from collections import defaultdict
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import finalize, macros, pump
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.harness import transcripts
from jarvis.tests.test_macro_reconcile import CheckBase
from jarvis.tests.test_macro_summary_reconcile import MIN, SummaryBase

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"

# The text the Claude CLI prints when the plan's session is used up. Synthetic: no
# quota is touched, the double just plays this string.
LIMIT_TEXT = "You've hit your session limit · resets 7:20am (UTC)"

FAILURES = {
	"empty-final": {
		"name": "empty-final",
		"description": "A chat final with no message: the turn produced nothing.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": [],
		"terminal": {"kind": "failed_final", "stopReason": "stop"},
	},
	"limit-chat-error": {
		"name": "limit-chat-error",
		"description": "A chat state=error carrying the CLI quota text.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": [],
		"terminal": {"kind": "error", "state": "error", "errorMessage": LIMIT_TEXT},
	},
	"limit-lifecycle-final": {
		"name": "limit-lifecycle-final",
		"description": "Quota text on a lifecycle error frame only, then an empty final.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": [{"op": "lifecycle_error", "error": LIMIT_TEXT}],
		"terminal": {"kind": "failed_final", "stopReason": "error"},
	},
	"limit-lifecycle-cut": {
		"name": "limit-lifecycle-cut",
		"description": "Quota text on a lifecycle frame, then the stream is cut: no chat event.",
		"ack": {"status": "started"},
		"ack_behavior": "normal",
		"frames": [{"op": "lifecycle_error", "error": LIMIT_TEXT}],
		"inject": {"drop_after_frame": 0},
		"terminal": {"kind": "final", "text": None},
	},
}
# Every frame shape that ends in a chat terminal. The cut one has none.
TERMINAL_SHAPES = ("empty-final", "limit-chat-error", "limit-lifecycle-final")


def setUpModule():
	install_synthetic_runtime_profile()


class _PlayFrames:
	"""Drives ONE queued Turn through the real pump with a transcript from ``FAILURES``."""

	def _play(self, rid: str, conv: str, shape: str, *, until=("errored", "finalizing", "done")) -> str:
		double = self._double()
		with patch.dict(transcripts.TRANSCRIPTS, FAILURES):
			deps = self._deps(double=double, prepare=self._prepare_stub(conv, double, shape))
			ctx = self._make_ctx(deps)
			self._pubs.clear()
			self._pump_until(ctx, lambda: self._state(rid) in until)
		return self._state(rid)

	def _into_recovery(self, rid: str, conv: str, shape: str = "limit-lifecycle-cut") -> None:
		"""The cut stream, through the real edges: the pump leaves the turn ``streaming``
		(no terminal ever comes), its soft deadline passes, and the shard's real watchdog
		parks it ``recovering``."""
		self.assertEqual(self._play(rid, conv, shape, until=("streaming", "recovering")), "streaming")
		frappe.db.set_value(TURN, rid, "deadline_at", self._ago(5), update_modified=False)
		frappe.db.commit()
		self.assertEqual(self._wd()["parked"], 1)
		self.assertEqual(self._state(rid), "recovering")

	def _exhaust_budget(self, rid: str) -> dict:
		frappe.db.set_value(
			TURN, rid, "recovery_started_at", self._ago(pump.RECOVERY_BUDGET_S + 60), update_modified=False
		)
		frappe.db.commit()
		return self._wd()

	def _finalize(self, rid: str) -> None:
		"""The finalize job the pump would have enqueued, run for real."""
		out = finalize.run_finalize(rid, self._target)
		self.assertTrue(out["ok"], out)


class TestASummaryTurnThatFails(_PlayFrames, SummaryBase):
	"""#2194: the summary must leave "pending" whenever its turn ends, whatever it said."""

	def _run_shape(self, shape: str):
		name, conv, rid = self._mk_summary(age_s=0)
		return name, conv, rid, self._play(rid, conv, shape)

	def _cut(self):
		"""A summary whose stream was cut: parked ``recovering`` by the real watchdog."""
		name, conv, rid = self._mk_summary(age_s=0)
		self._into_recovery(rid, conv)
		return name, conv, rid

	def test_every_failed_terminal_settles_the_turn_and_owes_the_hook(self):
		for shape in TERMINAL_SHAPES:
			with self.subTest(shape=shape):
				name, conv, rid, state = self._run_shape(shape)
				self.assertEqual(state, "errored", "a failed or empty terminal settles errored")
				self.assertEqual(self._val(rid, "terminal_kind"), "relay:error")
				self.assertEqual(
					macros._hook_effect_status(rid),
					"pending",
					"settlement wrote the macro_advance row in the same commit as the end",
				)
				self.assertEqual(self._mark(name)[:2], ("pending", conv), "the hook has not run yet")

	def test_the_finalize_job_then_fails_the_summary_and_tells_the_form(self):
		for shape in TERMINAL_SHAPES:
			with self.subTest(shape=shape):
				self.calls.clear()
				name, conv, rid, _ = self._run_shape(shape)
				self.published.clear()
				self._finalize(rid)
				self.assertEqual(self._mark(name)[:2], ("failed", ""), "Run is no longer refused")
				self.assertEqual(self._mark(name)[2] or "", "", "no stale summary text")
				self.assertFalse(frappe.db.exists(CONV, conv), "the throwaway chat is removed")
				self.assertEqual([p["status"] for p in self.published], ["failed"])
				self.assertEqual(
					[c["kw"].get("errored") for c in self.calls], [True], "the hook ran once, errored"
				)

	def test_a_new_summary_wakes_the_pump_after_its_turn_is_committed(self):
		"""The dispatch edge of #2194's "no job on any queue": a summary turn that is queued
		is only ever started by a pump wake (or the watchdog). Nothing asserted the wake
		for a summary: ``CheckBase`` replaces ``ensure_pump`` with a no-op."""
		seen = []

		def wake(target, **kw):
			# The turn row exists when the wake comes.
			seen.append((target, frappe.db.get_value(TURN, {"relay_target_id": target}, "state")))
			return {"enqueued": False}

		with patch.object(pump, "ensure_pump", wake):
			name = self._mk_macro()
			conv = self._summarize(name)
		rid = frappe.db.get_value(TURN, {"conversation": conv}, "name")
		self.assertEqual(self._state(rid), "queued")
		self.assertEqual(seen, [(self._target, "queued")], "woken once, after the commit of the turn")

	def test_a_stream_cut_with_only_a_lifecycle_error_is_not_a_terminal(self):
		"""The turn parks for recovery; nothing has told the macro. Characterises the
		window the five minute check and the watchdog are there for."""
		name, conv, rid = self._cut()
		self.assertEqual(self._mark(name)[:2], ("pending", conv))
		self.assertEqual(self.calls, [])

	def test_a_recovery_that_runs_out_of_budget_fails_the_summary_through_finalize(self):
		"""``pump._settle_recover_errored`` used to end the turn errored with no
		``macro_advance`` row, so the finalize job had nothing to run and the summary
		waited for the five minute check (chat >= 20 minutes old, scheduler on)."""
		name, conv, rid = self._cut()
		self.assertEqual(self._exhaust_budget(rid)["errored"], 1, "the budget edge ended the turn")
		self.assertEqual(self._state(rid), "errored")
		self.assertEqual(macros._hook_effect_status(rid), "pending", "the end owes the macro hook")
		self.assertEqual(self._mark(name)[:2], ("pending", conv), "not landed before finalize runs")
		self._finalize(rid)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertFalse(frappe.db.exists(CONV, conv))
		self.assertEqual([p["status"] for p in self.published], ["failed"])

	def test_the_budget_edge_owes_the_hook_and_no_output_effects(self):
		name, conv, rid = self._cut()
		self._exhaust_budget(rid)
		self.assertEqual(set(self._effects(rid)), {"macro_advance"}, "the hook and nothing else")
		self._finalize(rid)
		self.assertIn("run:error", self._pub_kinds())
		self.assertNotIn("run:end", self._pub_kinds(), "no terminal_publish re-sends it as a success")

	def test_a_lost_finalize_enqueue_is_made_up_for_by_the_watchdog(self):
		"""The end commits its effect rows; the enqueue after the commit may be lost. The
		watchdog's effect-ledger scan sends the finalize again, and landing is once."""
		name, conv, rid = self._cut()
		self._exhaust_budget(rid)
		self.assertGreaterEqual(self._wd()["finalize_requeued"], 1)
		self._finalize(rid)
		# The landing deleted the throwaway chat and its Turn: a second delivery is a no-op.
		self.assertEqual(finalize.run_finalize(rid, self._target), {"ok": False, "reason": "no turn"})
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual([p["status"] for p in self.published], ["failed"])


class TestAStepTurnThatFails(_PlayFrames, CheckBase):
	"""#2195: the run must end ``failed`` with the reason in its chat."""

	def _first_step(self, **kw):
		run, conv, rid = self._mk_run(steps=3, **kw)
		return run, conv, rid

	def test_every_failed_terminal_ends_the_run_failed_and_says_why(self):
		for shape in TERMINAL_SHAPES:
			with self.subTest(shape=shape):
				run, conv, rid = self._first_step()
				self.calls.clear()
				self.assertEqual(self._play(rid, conv, shape), "errored")
				self._finalize(rid)
				row = self._run(run)
				self.assertEqual(row.status, "failed")
				self.assertIn("Step 1 failed", row.error or "")
				self.assertEqual([c["kw"].get("errored") for c in self.calls], [True])
				self.assertTrue(self._closing_lines(conv), "the run chat holds the closing line")

	def test_the_quota_text_reaches_the_chat_row_and_the_run_reason(self):
		for shape in ("limit-chat-error", "limit-lifecycle-final"):
			with self.subTest(shape=shape):
				run, conv, rid = self._first_step()
				self._play(rid, conv, shape)
				self._finalize(rid)
				err = frappe.db.get_value(MSG, self._val(rid, "assistant_message"), "error") or ""
				self.assertIn("session limit", err, "the chat row carries the provider's text")
				reason = self._run(run).error or ""
				self.assertIn("usage allowance", reason, "the run says it was a usage limit")
				self.assertIn("7:20am (UTC)", reason, "and when it comes back")

	def test_the_next_step_is_not_sent_after_a_failed_one(self):
		run, conv, rid = self._first_step(stop_on_error=1)
		self._play(rid, conv, "limit-chat-error")
		self._finalize(rid)
		self.assertEqual(len(self._seeds(conv)), 1, "step 2 never went out")

	def test_a_stream_cut_leaves_the_run_running_until_recovery_ends(self):
		run, conv, rid = self._first_step()
		self._into_recovery(rid, conv)
		self.assertEqual(self._run(run).status, "running")

	def test_a_recovery_that_runs_out_of_budget_ends_the_run_through_finalize(self):
		run, conv, rid = self._first_step()
		self._into_recovery(rid, conv)
		self.assertEqual(self._exhaust_budget(rid)["errored"], 1)
		self.assertEqual(macros._hook_effect_status(rid), "pending")
		self.assertEqual(self._run(run).status, "running", "not before finalize runs")
		self._finalize(rid)
		row = self._run(run)
		self.assertEqual(row.status, "failed")
		self.assertIn("Step 1 failed", row.error or "")

	def test_the_end_enqueues_finalize_once_committed(self):
		run, conv, rid = self._first_step()
		self._into_recovery(rid, conv)
		seen = []
		deps = pump.PumpDeps()
		deps.enqueue_finalize = lambda r, t: seen.append(
			(r, t, self._state(r), macros._hook_effect_status(r))
		)
		deps.enqueue_pump_job = lambda *a, **k: None
		frappe.db.set_value(
			TURN, rid, "recovery_started_at", self._ago(pump.RECOVERY_BUDGET_S + 60), update_modified=False
		)
		frappe.db.commit()
		pump._watchdog_shard(self._target, deps, defaultdict(int))
		self.assertIn((rid, self._target, "errored", "pending"), seen, "enqueued after the commit")


class TestAUsageLimitReason(FrappeTestCase):
	"""#2195: what the run row says for a provider's usage-limit text."""

	def test_a_limit_with_a_reset_time_keeps_the_time(self):
		cases = {
			"You've hit your session limit \u00b7 resets 7:20am (UTC)": "Resets 7:20am (UTC).",
			"You've hit your weekly limit \u00b7 resets Mon 9:00am": "Resets Mon 9:00am.",
			"5-hour limit reached \u2219 resets 3pm": "Resets 3pm.",
		}
		for text, note in cases.items():
			with self.subTest(text=text):
				reason = macros._plain_reason(text)
				self.assertTrue(
					reason.startswith("The model account\u2019s usage allowance was reached."), reason
				)
				self.assertTrue(reason.endswith(note), reason)

	def test_a_limit_without_a_reset_time_gets_the_headline_only(self):
		self.assertEqual(
			macros._plain_reason("Claude AI usage limit reached|1760000000"),
			"The model account\u2019s usage allowance was reached.",
		)

	def test_other_errors_are_as_before(self):
		self.assertEqual(macros._plain_reason("Daily limit of 5 emails reached"), "")
		self.assertIn("request limit", macros._plain_reason("rate limit exceeded, resets in 5s"))
		self.assertNotIn("Resets", macros._plain_reason("rate limit exceeded, resets in 5s"))
