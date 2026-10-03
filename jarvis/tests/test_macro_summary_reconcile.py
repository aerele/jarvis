"""The five minute check settles a macro's summary that sits "summarizing" because its
turn's end never landed it (``macro_reconcile._SummaryCheck``), and the landing itself
is a compare-and-set on the mark naming the chat that was read
(``macros._settle_summary_mark``).

From the 2026-10-01 macros review: the summarize turn is a turn like a step's, and its
hook is lost the same ways (no finalize for a turn that ends before it is sent, a
finalize that dies in the hook until the ledger gives up, a request that dies before
its dispatch). The mark then stayed "pending" for good, with Run refused behind it. And
a landing that wrote by macro name failed or overwrote a Re-summarize started
meanwhile.

Each summary here is started by the real ``summarize_macro`` on a shard of the test's
own in ``pump`` mode; its turn sits ``queued`` until the test moves it through the real
edge (the watchdog's age-out, the finalize ledger).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import admission, finalize, macro_reconcile, macros, macros_api, settlement
from jarvis.chat import turn_state as ts
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.race_harness import snapshot_isolation_on
from jarvis.tests.test_macro_reconcile import CheckBase, _Killed
from jarvis.tests.test_pump import TEST_USER

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
MACRO = "Jarvis Macro"
LOG = "Error Log"
SIGNAL = "jarvis.chat.macros.reconciled: "

# Text that must never reach an Error Log row: the steps' prompts and the reply.
PROMPT_MARK = "quarterly-receivables-prompt"
REPLY_MARK = "merged-receivables-answer"
MERGE_REPLY = (
	"Here it is.\n```jarvis-macro-merge\n"
	f'{{"mergeable": true, "reason": "", "merged_prompt": "{REPLY_MARK}"}}\n```'
)
MIN = 60
# The real candidate query, before ``SummaryBase`` narrows it to a test's own macros.
REAL_CANDIDATES = macro_reconcile._stuck_summary_candidates
REAL_COUNT = getattr(macro_reconcile, "_stuck_summary_count", None)


def setUpModule():
	install_synthetic_runtime_profile()


class SummaryBase(CheckBase):
	def setUp(self):
		super().setUp()
		self._since = frappe.utils.now()
		self._macros: list[str] = []
		self._conversations: list[str] = []
		self.published: list[dict] = []

		def candidates(**kw):
			# The site is shared: only this test's macros, through the real query.
			return [row for row in REAL_CANDIDATES(**kw) if row.name in self._macros]

		def count():
			return len(candidates(limit=10**6))

		def publish(user, payload):
			if payload.get("kind") == "macro:merged":
				self.published.append(payload)

		for p in (
			patch.object(macro_reconcile, "_stuck_summary_candidates", candidates),
			patch.object(macro_reconcile, "_stuck_summary_count", count),
			patch.object(macros, "publish_to_user", publish),
			# The control plane and the gateway handshake, at their boundary.
			patch.object(macros, "entitlement_block", return_value=None),
			patch(
				"jarvis.chat.api._ensure_session_key",
				side_effect=lambda *a, **k: f"sk-summary-{frappe.generate_hash(length=10)}",
			),
		):
			p.start()
			self.addCleanup(p.stop)
		self.addCleanup(self._drop_summary_logs)

	def tearDown(self):
		for name in self._macros:
			frappe.cache().delete_value(macro_reconcile._summary_key("skip", name))
		for conv in self._conversations:
			frappe.cache().delete_value(macro_reconcile._summary_key("signalled", conv))
		super().tearDown()

	def _drop_summary_logs(self):
		for name in self._macros:
			frappe.db.delete(LOG, {"method": ["like", f"%: {name}"]})
		frappe.db.delete(LOG, {"method": ["like", "jarvis macro merge-apply failed%"]})
		frappe.db.commit()

	# --- fixtures --------------------------------------------------------------- #

	def _mk_macro(self) -> str:
		frappe.set_user(TEST_USER)
		macro = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"summary-reconcile-{frappe.generate_hash(length=6)}",
				"steps": [{"prompt": f"{PROMPT_MARK} {i}"} for i in (1, 2)],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self._macros.append(macro.name)
		self._made.append((MACRO, macro.name))
		return macro.name

	def _summarize(self, name: str, *, force: int = 0) -> str:
		"""The real ``summarize_macro``, as the owner. Returns its chat."""
		frappe.set_user(TEST_USER)
		out = macros_api.summarize_macro(name, force=force)
		self.assertTrue(out["ok"], out)
		self._track(out["conversation"])
		return out["conversation"]

	def _track(self, conv: str):
		self._conversations.append(conv)
		self._made.append((CONV, conv))

	def _mk_summary(self, *, age_s=21 * MIN):
		"""A macro "summarizing", its turn ``queued``, its chat ``age_s`` old."""
		name = self._mk_macro()
		conv = self._summarize(name)
		rid = frappe.db.get_value(TURN, {"conversation": conv}, "name")
		self.assertEqual(self._state(rid), "queued")
		self.assertEqual(self._mark(name)[:2], ("pending", conv))
		self._age_chat(conv, age_s)
		return name, conv, rid

	def _mk_summary_with_no_turn(self, *, age_s=21 * MIN):
		"""The request that started the summary died after its message was committed and
		before the accept gate wrote the turn: the mark and the message, no turn."""
		name = self._mk_macro()
		frappe.set_user(TEST_USER)

		def dies(**kw):
			raise _Killed()

		with patch.object(admission, "accept_or_queue", dies), self.assertRaises(_Killed):
			macros_api.summarize_macro(name)
		frappe.db.rollback()
		conv = frappe.db.get_value(MACRO, name, "merge_conversation")
		self._track(conv)
		self.assertEqual(self._mark(name)[0], "pending")
		self.assertTrue(frappe.db.exists(MSG, {"conversation": conv, "role": "user"}))
		self.assertFalse(frappe.db.exists(TURN, {"conversation": conv}))
		self._age_chat(conv, age_s)
		return name, conv

	def _age_chat(self, conv: str, seconds: int):
		frappe.db.sql(f"UPDATE `tab{CONV}` SET creation=%s WHERE name=%s", (self._ago(seconds), conv))
		frappe.db.commit()

	def _mark(self, name: str) -> tuple:
		frappe.db.commit()
		return tuple(
			frappe.db.sql(
				f"SELECT merge_status, merge_conversation, merged_prompt FROM `tab{MACRO}` WHERE name=%s",
				name,
			)[0]
		)

	def _macro_row(self, name: str) -> dict:
		frappe.db.commit()
		return frappe.db.sql(f"SELECT * FROM `tab{MACRO}` WHERE name=%s", name, as_dict=True)[0]

	def _dead_turn(self, rid: str):
		"""The summarize turn waited too long in the queue: cancelled by the watchdog,
		which runs no finalize, so no hook. Ended five minutes ago."""
		self._age_out(rid)
		self.assertIsNone(macros._hook_effect_status(rid), "premise: no hook is owed")
		self._ended(rid)

	def _landing_lost(self, conv: str, rid: str):
		"""The reply finished; the finalize job died in the hook until the ledger gave up
		on it. Ended five minutes ago."""
		self._settle_success(conv, rid, MERGE_REPLY)
		self._force_done(rid)
		self.assertEqual(self._state(rid), "done")
		self._ended(rid)

	def _ended_with_reply(self, conv: str, rid: str, state: str, **reply):
		"""The turn ended ``state`` five minutes ago with this reply, and no finalize ledger:
		an end no hook comes for (the run check's own fixtures do the same)."""
		amsg = self._placeholder(conv, content=reply.pop("content", MERGE_REPLY), streaming=0, **reply)
		self._force(rid, state=state, version=5, assistant_message=amsg, done_at=frappe.utils.now())
		self.assertIsNone(macros._hook_effect_status(rid), "premise: no hook is owed")
		self._ended(rid)

	def _all_logs(self) -> list:
		"""Every Error Log row written since the test began (the site is shared)."""
		frappe.db.commit()
		return frappe.get_all(
			LOG,
			filters={"creation": [">=", self._since], "method": ["not like", "Jarvis: role-profile%"]},
			fields=["method", "error"],
			order_by="creation",
		)

	def _summary_signals(self) -> list:
		frappe.db.commit()
		return frappe.get_all(
			LOG,
			filters={"creation": [">=", self._since], "method": ["like", f"{SIGNAL}summary%"]},
			fields=["method", "error"],
		)


class TestAStuckSummaryIsSettled(SummaryBase):
	def test_a_turn_that_ended_with_no_hook_fails_the_summary(self):
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)
		out = self._tick()
		self.assertEqual(out["summaries_settled"], 1)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertFalse(frappe.db.exists(CONV, conv), "the winner removes the throwaway chat")
		self.assertEqual([p["status"] for p in self.published], ["failed"])
		self.assertEqual(self._summary_signals(), [], "an end with no hook is by design: no Error Log")

	def test_a_summary_that_worked_and_lost_its_landing_lands_once(self):
		name, conv, rid = self._mk_summary()
		self._landing_lost(conv, rid)
		self.assertEqual(self._mark(name)[0], "pending", "premise: the hook never landed it")
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertFalse(frappe.db.exists(CONV, conv))
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual([p["status"] for p in self.published], ["ready"])
		signals = self._summary_signals()
		self.assertEqual([s.method for s in signals], [f"{SIGNAL}{macro_reconcile.SHAPE_SUMMARY_FORCE_DONE}"])
		self.assertIn(name, signals[0].error)

	def test_a_hook_that_was_delivered_and_landed_nothing_is_made_up_for_without_a_log(self):
		name, conv, rid = self._mk_summary()
		self._settle_success(conv, rid, MERGE_REPLY)
		with patch.object(macros, "advance_after_turn", lambda *a, **k: None):
			finalize.run_finalize(rid, self._target)
		self.assertEqual(macros._hook_effect_status(rid), "done")
		self._ended(rid)
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertEqual(self._summary_signals(), [], "the hook WAS delivered: not a lost hook")

	def test_a_summary_with_no_turn_and_no_reply_fails(self):
		name, conv = self._mk_summary_with_no_turn()
		self.assertFalse(admission.reply_in_progress(conv), "premise")
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertFalse(frappe.db.exists(CONV, conv))
		self.assertEqual(
			[s.method for s in self._summary_signals()], [f"{SIGNAL}{macro_reconcile.SHAPE_SUMMARY_NO_TURN}"]
		)

	def test_a_summary_with_no_turn_and_a_reply_in_progress_waits(self):
		name, conv = self._mk_summary_with_no_turn()
		self._placeholder(conv, streaming=1)
		self.assertTrue(admission.reply_in_progress(conv), "premise")
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual(self._mark(name)[:2], ("pending", conv))
		# Given up on at an hour, and the chat of a live reply is not deleted.
		self._age_chat(conv, 61 * MIN)
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertTrue(frappe.db.exists(CONV, conv))

	def test_a_summary_whose_chat_is_gone_fails_at_once(self):
		name, conv, rid = self._mk_summary(age_s=0)
		frappe.db.delete(TURN, {"conversation": conv})
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.db.delete(CONV, {"name": conv})
		frappe.db.commit()
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual([p["status"] for p in self.published], ["failed"])
		self.assertEqual(self._all_logs(), [], "a chat deleted mid-summary is an ordinary state")

	def test_a_summary_under_twenty_minutes_old_is_left(self):
		name, conv, rid = self._mk_summary(age_s=19 * MIN)
		self._dead_turn(rid)
		before = self._macro_row(name)
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual(self._macro_row(name), before)
		self._age_chat(conv, 21 * MIN)
		self.assertEqual(self._tick()["summaries_settled"], 1)

	def test_the_clock_is_the_chats_creation_not_the_turns_end(self):
		name, conv, rid = self._mk_summary(age_s=19 * MIN)
		self._dead_turn(rid)
		self._ended(rid, 30 * MIN)
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual(self._mark(name)[0], "pending")


class TestATurnThatNeverEnds(SummaryBase):
	def test_is_failed_at_sixty_minutes_and_not_before(self):
		name, conv, rid = self._mk_summary()
		self._force(rid, state="ready")
		for age in (21, 59):
			self._age_chat(conv, age * MIN)
			self.assertEqual(self._tick()["summaries_settled"], 0, f"at {age} minutes")
			self.assertEqual(self._mark(name)[:2], ("pending", conv))
		self._age_chat(conv, 61 * MIN)
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual([p["status"] for p in self.published], ["failed"])
		# Its turn is live: deleting the chat would delete the Turn row under the pump.
		self.assertTrue(frappe.db.exists(CONV, conv))
		self.assertEqual(self._state(rid), "ready")
		self.assertEqual(self._summary_signals(), [], "not a lost hook")

	def test_its_late_end_lands_nothing(self):
		name, conv, rid = self._mk_summary(age_s=61 * MIN)
		self._force(rid, state="ready")
		self._tick()
		self._real_advance(conv, errored=False, run_id=rid)
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual(len(self.published), 1)
		self.assertTrue(frappe.db.exists(CONV, conv), "its turn is still live")


class TestOneSettlement(SummaryBase):
	def test_the_hook_then_the_check(self):
		name, conv, rid = self._mk_summary()
		self._settle_success(conv, rid, MERGE_REPLY)
		finalize.run_finalize(rid, self._target)  # the real hook lands it
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertEqual(len(self.published), 1)

	def test_the_check_then_the_hook(self):
		name, conv, rid = self._mk_summary()
		self._landing_lost(conv, rid)
		self._tick()
		self._real_advance(conv, errored=True, run_id=rid)
		self._real_advance(conv, errored=False, run_id=rid)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertEqual(len(self.published), 1)

	def test_a_re_summarize_started_meanwhile_survives_the_check(self):
		"""The check read the mark on chat A; a forced Re-summarize moves it to chat B
		before the check lands A's end. A's end must not fail B or clear its link."""
		name, conv_a, rid = self._mk_summary()
		self._dead_turn(rid)
		started = {}
		real_over = macro_reconcile.step_is_over

		def over(turn_id, **kw):
			def re_summarize():
				out = macros_api.summarize_macro(name, force=1)
				started["conv"] = out["conversation"]

			if turn_id == rid and not started:
				self._elsewhere(re_summarize)
			return real_over(turn_id, **kw)

		with patch.object(macro_reconcile, "step_is_over", over), snapshot_isolation_on():
			out = self._tick()
		self.assertEqual((out["summaries_settled"], out["conflicts"]), (0, 0))
		conv_b = started["conv"]
		self._track(conv_b)
		self.assertEqual(self._mark(name)[:2], ("pending", conv_b))
		self.assertTrue(frappe.db.exists(CONV, conv_b))
		self.assertTrue(frappe.db.exists(TURN, {"conversation": conv_b}))
		self.assertEqual(self.published, [])

	def test_a_save_of_the_macro_meanwhile_does_not_stop_the_check(self):
		"""Another writer changed the macro row (not its summary) after the check's
		snapshot: the compare-and-set is replayed on the present row and wins, rather
		than losing to a write conflict for an hour of ticks."""
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)
		real_over = macro_reconcile.step_is_over
		saved = []

		def over(turn_id, **kw):
			def save():
				frappe.db.set_value(MACRO, name, "description", "edited meanwhile", update_modified=True)

			if turn_id == rid and not saved:
				saved.append(1)
				self._elsewhere(save)
			return real_over(turn_id, **kw)

		with patch.object(macro_reconcile, "step_is_over", over), snapshot_isolation_on():
			out = self._tick()
		self.assertEqual((out["summaries_settled"], out["conflicts"]), (1, 0))
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual(frappe.db.get_value(MACRO, name, "description"), "edited meanwhile")

	def test_a_stale_hook_does_not_fail_a_re_summarize(self):
		"""The hook found the macro waiting on chat A; a forced Re-summarize moves the
		mark to chat B while the hook reads A's reply. A's landing loses."""
		name, conv_a, rid = self._mk_summary()
		started = {}
		real_outcome = macros._merge_outcome

		def outcome(conversation_id, *, errored):
			started["conv"] = self._summarize(name, force=1)
			frappe.set_user("Administrator")
			return real_outcome(conversation_id, errored=errored)

		with patch.object(macros, "_merge_outcome", outcome):
			macros._apply_merge_after_turn(conv_a, errored=True)
		self.assertEqual(self._mark(name)[:2], ("pending", started["conv"]))
		self.assertTrue(frappe.db.exists(CONV, started["conv"]))
		# A's turn is still queued: neither the losing hook nor Re-summarize removes a
		# chat with a live turn.
		self.assertTrue(frappe.db.exists(CONV, conv_a))
		self.assertEqual(self.published, [])

	def test_a_stale_owner_refusal_does_not_fail_a_re_summarize(self):
		"""The same for the hook's other write: a chat of another owner, whose refusal
		ran after the mark had moved on."""
		name, conv_a, rid = self._mk_summary()
		conv_b = self._summarize(name, force=1)
		frappe.db.set_value(CONV, conv_a, "owner", "Administrator", update_modified=False)
		frappe.db.commit()
		self.assertTrue(macros._drop_summary_link_unless_one_owner(name, conv_a))
		self.assertEqual(self._mark(name)[:2], ("pending", conv_b))
		self.assertEqual(self.published, [])


class TestTheGateAndTheTick(SummaryBase):
	def test_the_off_switch(self):
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)
		frappe.local.conf[macro_reconcile.OFF_SWITCH] = 1
		before = self._macro_row(name)
		self.assertEqual(self._tick()["summaries"], 0)
		self.assertEqual(self._macro_row(name), before)
		frappe.local.conf.pop(macro_reconcile.OFF_SWITCH)
		self.assertEqual(self._tick()["summaries_settled"], 1)

	def test_nothing_is_done_outside_pump_mode(self):
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)
		before = self._macro_row(name)
		for mode in ("legacy", "draining", "phase0", "kill switch"):
			with self.subTest(mode=mode):
				self._set_mode(mode)
				self.assertEqual(self._tick()["summaries_settled"], 0)
				self.assertEqual(self._macro_row(name), before)
		self._set_mode("pump")
		self.assertEqual(self._tick()["summaries_settled"], 1)

	def test_a_no_op_tick_writes_nothing(self):
		name, conv, rid = self._mk_summary()  # its turn is queued: not over
		macro_before = self._macro_row(name)
		conv_before = frappe.db.sql(f"SELECT * FROM `tab{CONV}` WHERE name=%s", conv, as_dict=True)
		logs_before = self._all_logs()
		out = self._tick()
		self.assertEqual((out["summaries"], out["summaries_settled"]), (1, 0))
		self.assertEqual(self._macro_row(name), macro_before)
		self.assertEqual(
			frappe.db.sql(f"SELECT * FROM `tab{CONV}` WHERE name=%s", conv, as_dict=True), conv_before
		)
		self.assertEqual(self._all_logs(), logs_before)
		self.assertEqual(self.published, [])

	def test_a_macro_that_raises_is_logged_once_and_the_next_one_is_still_settled(self):
		first, _conv1, rid1 = self._mk_summary(age_s=25 * MIN)
		second, _conv2, rid2 = self._mk_summary()
		self._dead_turn(rid1)
		self._dead_turn(rid2)
		real_land = macros._land_summary

		def land(macro_name, conversation_id, **kw):
			if macro_name == first:
				raise RuntimeError("a fault in the landing")
			return real_land(macro_name, conversation_id, **kw)

		with patch.object(macros, "_land_summary", land):
			out = self._tick()
			self.assertEqual((out["raised"], out["summaries_settled"]), (1, 1))
			self.assertEqual(self._mark(second)[0], "failed")
			self._watchdog_clears_the_cache()
			self.assertEqual(self._tick()["raised"], 0, "skipped for an hour after a raise")
		failures = frappe.get_all(
			LOG, filters={"method": f"jarvis.chat.macros.summary_reconcile_failed: {first}"}, pluck="name"
		)
		self.assertEqual(len(failures), 1)

	def test_no_error_log_row_carries_a_prompt_or_a_reply(self):
		name, conv, rid = self._mk_summary()
		self._landing_lost(conv, rid)
		self._mk_summary_with_no_turn()
		self._tick()
		logs = self._all_logs()
		self.assertEqual(
			sorted(s.method for s in self._summary_signals()),
			sorted(
				f"{SIGNAL}{shape}"
				for shape in (macro_reconcile.SHAPE_SUMMARY_FORCE_DONE, macro_reconcile.SHAPE_SUMMARY_NO_TURN)
			),
		)
		for row in logs:
			for mark in (PROMPT_MARK, REPLY_MARK, "jarvis-macro-merge"):
				self.assertNotIn(mark, row.method or "")
				self.assertNotIn(mark, row.error or "")
		# Once per summary: a later tick writes no second row.
		self._tick()
		self.assertEqual(len(self._summary_signals()), 2)

	def test_a_check_settled_summary_reads_as_a_hook_settled_one(self):
		"""What the macro form and list read (``get_macro``), and the event an open tab
		gets, are the same whoever landed the summary."""
		by_check, conv1, rid1 = self._mk_summary()
		self._landing_lost(conv1, rid1)
		self._tick()
		by_hook, conv2, rid2 = self._mk_summary()
		self._settle_success(conv2, rid2, MERGE_REPLY)
		finalize.run_finalize(rid2, self._target)
		frappe.set_user(TEST_USER)
		seen = [macros_api.get_macro(n) for n in (by_check, by_hook)]
		keys = ("merge_status", "merged_prompt")
		self.assertEqual(*[{k: s[k] for k in keys} for s in seen])
		self.assertEqual(
			[{k: v for k, v in p.items() if k not in ("macro", "macro_name")} for p in self.published],
			[{"kind": "macro:merged", "status": "ready"}] * 2,
		)


class TestWhatTheTurnAndTheReplyDecide(SummaryBase):
	def test_a_chat_with_no_turn_and_a_finished_reply_lands_it(self):
		"""Sent where no Turn row is written, the reply finished and its hook was lost: it
		lands as the hook would have landed it."""
		name, conv = self._mk_summary_with_no_turn()
		self._placeholder(conv, content=MERGE_REPLY, streaming=0)
		self.assertFalse(admission.reply_in_progress(conv), "premise")
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))
		self.assertEqual([p["status"] for p in self.published], ["ready"])

	def test_a_reply_turn_recovery_finished_lands(self):
		"""The turn was recorded ``errored``; turn recovery then finished its reply
		(``was_recovered``, no error, text): the answer arrived."""
		name, conv, rid = self._mk_summary()
		self._ended_with_reply(conv, rid, "errored", was_recovered=1)
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(self._mark(name), ("ready", "", REPLY_MARK))

	def test_a_turn_that_failed_or_was_stopped_fails_whatever_its_reply_says(self):
		for state in ("errored", "cancelled"):
			with self.subTest(state=state):
				name, conv, rid = self._mk_summary()
				self._ended_with_reply(conv, rid, state)
				self.assertEqual(self._tick()["summaries_settled"], 1)
				self.assertEqual(self._mark(name), ("failed", "", ""))

	def test_the_newest_turn_of_the_chat_decides(self):
		"""An older turn of the chat ended; a newer one (a re-dispatch) has not: the summary
		waits for the newer one."""
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)
		frappe.db.sql(f"UPDATE `tab{TURN}` SET creation=%s WHERE name=%s", (self._ago(30 * MIN), rid))
		frappe.db.commit()
		seed = frappe.db.get_value(TURN, rid, "seed_message")
		self._mk_turn(conv, f"summary-again-{frappe.generate_hash(length=6)}", seed, "queued")
		self.assertEqual(self._tick()["summaries_settled"], 0)
		self.assertEqual(self._mark(name)[:2], ("pending", conv))


class TestTheCandidatesAndTheBudget(SummaryBase):
	def _cut_short_logs(self):
		return self._logs(macro_reconcile.TICK_CUT_SHORT)

	def test_a_gone_chat_comes_first_then_the_oldest(self):
		newest, _c1, _r1 = self._mk_summary(age_s=30 * MIN)
		middle, _c2, _r2 = self._mk_summary(age_s=40 * MIN)
		oldest, _c3, _r3 = self._mk_summary(age_s=50 * MIN)
		gone, conv_gone, _r4 = self._mk_summary(age_s=0)
		frappe.db.delete(TURN, {"conversation": conv_gone})
		frappe.db.delete(MSG, {"conversation": conv_gone})
		frappe.db.delete(CONV, {"name": conv_gone})
		frappe.db.commit()
		self.assertEqual(
			[row.name for row in macro_reconcile._stuck_summary_candidates()], [gone, oldest, middle, newest]
		)

	def test_at_most_the_cap_is_looked_at_and_the_rest_is_counted(self):
		names = []
		for age in (23, 22, 21):
			name, _conv, rid = self._mk_summary(age_s=age * MIN)
			self._dead_turn(rid)
			names.append(name)
		self.assertGreaterEqual(REAL_COUNT(), 3, "the real count")
		with patch.object(macro_reconcile, "MAX_SUMMARIES_PER_TICK", 2):
			self.assertLessEqual(len(REAL_CANDIDATES()), 2, "the query itself is capped")
			out = self._tick()
			self.assertEqual((out["summaries_settled"], out["summaries_left"]), (2, 1))
			self.assertEqual([self._mark(n)[0] for n in names], ["failed", "failed", "pending"])
			cut = self._cut_short_logs()
			self.assertEqual(len(cut), 1)
			self.assertIn("1 stuck summaries were not looked at", cut[0].error)
			self.assertNotIn("candidate runs", cut[0].error)
			self.assertEqual(self._tick()["summaries_settled"], 1)

	def test_the_time_budget_stops_the_summaries_too(self):
		first, _c1, rid1 = self._mk_summary(age_s=25 * MIN)
		second, _c2, rid2 = self._mk_summary()
		self._dead_turn(rid1)
		self._dead_turn(rid2)
		real = macro_reconcile._SummaryCheck.run
		looked = []

		def run_check(check):
			looked.append(check.name)
			return real(check)

		def clock():
			# The budget is used up by the first summary, however often the clock is read.
			return macro_reconcile.TICK_BUDGET_S + 1.0 if looked else 0.0

		with (
			patch.object(macro_reconcile._SummaryCheck, "run", run_check),
			patch.object(macro_reconcile, "_clock", clock),
		):
			out = self._tick()
		self.assertEqual(looked, [first])
		self.assertEqual(
			(out["summaries_settled"], out["out_of_time"], out["summaries_left"], out["left"]), (1, 1, 1, 0)
		)
		self.assertEqual(self._mark(second)[0], "pending")
		cut = self._cut_short_logs()
		self.assertEqual(len(cut), 1)
		self.assertIn("time budget", cut[0].error)
		self.assertIn("1 stuck summaries were not looked at", cut[0].error)

	def test_summaries_a_run_half_out_of_time_left_are_counted(self):
		name, _conv, rid = self._mk_summary()
		self._dead_turn(rid)
		run, _run_conv, _run_turn = self._mk_run()
		self._idle(run)
		reads = iter([0.0])
		with patch.object(macro_reconcile, "_clock", lambda: next(reads, 1000.0)):
			out = self._tick()
		self.assertEqual(
			(out["out_of_time"], out["left"], out["summaries"], out["summaries_left"]), (1, 1, 0, 1)
		)
		self.assertEqual(self._mark(name)[0], "pending")
		cut = self._cut_short_logs()
		self.assertEqual(len(cut), 1)
		self.assertIn("1 candidate runs were not looked at", cut[0].error)
		self.assertIn("1 stuck summaries were not looked at", cut[0].error)


class TestGivingUp(SummaryBase):
	def _hook_owed_on_an_ended_turn(self, rid):
		"""The turn ended ``errored`` and its finalize job died once in the hook: the
		ledger still owes the hook, so the turn is not over, and nothing of the chat is
		live."""
		self._force(rid, state="errored", version=5, error="The model refused.", done_at=frappe.utils.now())
		ts.insert_required_effects(rid, settlement.TERMINAL_EFFECTS)
		frappe.db.commit()
		self._die_in_the_hook(rid)
		self.assertEqual(macros._hook_effect_status(rid), "running", "premise: a hook is owed")

	def test_the_give_up_removes_an_idle_chat_of_the_owner_only(self):
		mine, conv_mine, rid_mine = self._mk_summary(age_s=61 * MIN)
		theirs, conv_theirs, rid_theirs = self._mk_summary(age_s=61 * MIN)
		for rid in (rid_mine, rid_theirs):
			self._hook_owed_on_an_ended_turn(rid)
		frappe.db.set_value(CONV, conv_theirs, "owner", "Administrator", update_modified=False)
		frappe.db.commit()
		self.assertEqual(self._tick()["summaries_settled"], 2)
		self.assertEqual([self._mark(n)[:2] for n in (mine, theirs)], [("failed", "")] * 2)
		self.assertFalse(frappe.db.exists(CONV, conv_mine))
		self.assertTrue(frappe.db.exists(CONV, conv_theirs), "never another user's chat")

	def test_a_look_that_raises_on_every_tick_is_failed_at_sixty_minutes(self):
		name, conv, rid = self._mk_summary()

		def raises(conversation):
			raise RuntimeError("a fault in the turn read")

		with patch.object(macro_reconcile, "_summary_turn", raises):
			out = self._tick()
			self.assertEqual((out["raised"], out["summaries_settled"]), (1, 0))
			self.assertEqual(self._mark(name)[:2], ("pending", conv))
			# An hour later: the skip has run out, and the chat is an hour old.
			frappe.cache().delete_value(macro_reconcile._summary_key("skip", name))
			self._age_chat(conv, 61 * MIN)
			out = self._tick()
		self.assertEqual((out["raised"], out["summaries_settled"]), (1, 1))
		self.assertEqual(self._mark(name)[:2], ("failed", ""))
		self.assertEqual([p["status"] for p in self.published], ["failed"])
		self.assertTrue(frappe.db.exists(CONV, conv), "its turn is live: the chat is kept")


class TestTheTickLeavesATrace(SummaryBase):
	def test_a_lock_wait_is_tried_again_on_the_next_tick_and_logged_at_most_hourly(self):
		name, conv, rid = self._mk_summary()
		self._dead_turn(rid)

		def waits(*a, **k):
			raise frappe.QueryTimeoutError("Lock wait timeout exceeded")

		with patch.object(macros, "_land_summary", waits):
			for _ in range(2):
				out = self._tick()
				self.assertEqual((out["conflicts"], out["raised"], out["summaries_settled"]), (1, 0, 0))
		rows = self._logs(macro_reconcile.LOCK_WAIT)
		self.assertEqual(len(rows), 1)
		self.assertIn(name, rows[0].error)
		for mark in (PROMPT_MARK, REPLY_MARK, "Traceback"):
			self.assertNotIn(mark, rows[0].error)
		self.assertEqual(self._tick()["summaries_settled"], 1, "not skipped for an hour")

	def test_a_cached_macro_reads_the_landed_summary(self):
		name, conv, rid = self._mk_summary()
		self._landing_lost(conv, rid)
		self.assertEqual(frappe.get_cached_doc(MACRO, name).merge_status, "pending")
		self._tick()
		self.assertEqual(frappe.get_cached_doc(MACRO, name).merge_status, "ready")

	def test_each_summary_of_a_macro_has_its_own_signal(self):
		name, conv, rid = self._mk_summary()
		self._landing_lost(conv, rid)
		self._tick()
		conv2 = self._summarize(name)
		rid2 = frappe.db.get_value(TURN, {"conversation": conv2}, "name")
		self._age_chat(conv2, 21 * MIN)
		self._landing_lost(conv2, rid2)
		self.assertEqual(self._tick()["summaries_settled"], 1)
		self.assertEqual(len(self._summary_signals()), 2)
