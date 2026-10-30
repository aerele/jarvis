"""Macro runs and prepared reports: the gate (``jarvis.chat.macro_report_wait``).

``run_report`` serves a prepared report's newest copy of any age. Inside a macro run
the step must read a copy made at/after the run started, so it asks this module
whether the calling turn IS that run's step (``macro_context``). Everything else (a
typed follow-up in the run's chat, another user, the kill switch, a bench without the
turn machine) must get today's answer, byte for byte.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import admission, macro_reconcile, macro_report_wait, macros, macros_api, report_runs
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests.race_harness import other_connection
from jarvis.tests.test_macro_dispatch_identity import IdentityBase
from jarvis.tests.test_run_report import PREP_REPORT, _completed, _ensure_report
from jarvis.tools import _agent_run_ctx
from jarvis.tools.run_report import run_report

CONV = "Jarvis Conversation"
RUN = "Jarvis Macro Run"
TURN = "Jarvis Chat Turn"
MSG = "Jarvis Chat Message"
OWNER = outcome.OWNER
_ENQUEUE = "frappe.core.doctype.prepared_report.prepared_report.enqueue"
_MACHINE = "jarvis.chat.admission.turn_machine_enabled"


class TestEnabled(outcome.MacroRunOutcomeBase):
	def test_on_by_default_on_the_turn_machine(self):
		with patch(_MACHINE, return_value=True), patch.dict(frappe.conf, clear=False):
			frappe.conf.pop("jarvis_macro_report_wait", None)
			self.assertTrue(macro_report_wait.enabled())

	def test_an_explicit_off_disables_it(self):
		for off in (0, False, "0", "false", "off"):
			with (
				self.subTest(off=off),
				patch(_MACHINE, return_value=True),
				patch.dict(frappe.conf, {"jarvis_macro_report_wait": off}),
			):
				self.assertFalse(macro_report_wait.enabled())

	def test_an_explicit_on_enables_it(self):
		for on in (1, True, "1", "true"):
			with (
				self.subTest(on=on),
				patch(_MACHINE, return_value=True),
				patch.dict(frappe.conf, {"jarvis_macro_report_wait": on}),
			):
				self.assertTrue(macro_report_wait.enabled())

	def test_off_without_the_turn_machine(self):
		with patch(_MACHINE, return_value=False), patch.dict(frappe.conf, {"jarvis_macro_report_wait": 1}):
			self.assertFalse(macro_report_wait.enabled())


class GateBase(outcome.MacroRunOutcomeBase):
	"""A two-step run that has sent step 1, its step turn live under the fixed step id."""

	def setUp(self):
		super().setUp()
		patcher = patch(_MACHINE, return_value=True)
		patcher.start()
		self.addCleanup(patcher.stop)
		conf = patch.dict(frappe.conf, {"jarvis_macro_report_wait": 1})
		conf.start()
		self.addCleanup(conf.stop)

	def _turn_as(self, conv, run_id, *, state="streaming", seed=None):
		"""A Turn named ``run_id`` (the turn machine's primary key) on ``conv``; a new
		macro user message seeds it unless ``seed`` names one."""
		seed = seed or self._msg(conv, "user", origin="macro", content="step")
		doc = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conv,
				"relay_target_id": "t",
				"turn_class": "interactive",
				"state": state,
				"seed_message": seed,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return seed

	def _mk_gated(self, *, state="streaming", **run_fields):
		"""A run at step 1 with its step turn live; returns ``(run, conv, step_turn)``."""
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn = macros._step_turn_id(run, 0)
		self._turn_as(conv, step_turn, state=state)
		frappe.db.set_value(
			RUN,
			run,
			{"started_at": add_to_date(now_datetime(), seconds=-600), **run_fields},
			update_modified=False,
		)
		frappe.db.commit()
		frappe.set_user(OWNER)
		return run, conv, step_turn


class TestMacroContext(GateBase):
	def test_the_steps_own_turn_gets_the_context(self):
		run, conv, _ = self._mk_gated()
		ctx = macro_report_wait.macro_context(conv)
		self.assertEqual(ctx.name, run)
		self.assertEqual(ctx.current_step, 1)
		self.assertFalse(ctx.resume_turn)
		self.assertEqual(ctx.started_at, frappe.db.get_value(RUN, run, "started_at"))

	def test_the_current_step_is_the_one_last_sent(self):
		# the cursor counts steps SENT: at 2 the step's turn is step 2's, not step 1's
		run, conv, step1 = self._mk_gated()
		frappe.db.set_value(TURN, step1, "state", "done")
		frappe.db.set_value(RUN, run, "current_step", 2, update_modified=False)
		self.assertIsNone(macro_report_wait.macro_context(conv))  # nothing live
		self._turn_as(conv, macros._step_turn_id(run, 1))
		self.assertEqual(macro_report_wait.macro_context(conv).current_step, 2)

	def test_the_steps_own_turn_is_still_accepted_once_a_resume_turn_is_set(self):
		# With a resume turn the Retry rule is off; the original step turn is judged by its id.
		run, conv, step_turn = self._mk_gated(resume_turn="resume000001")
		self.assertEqual(macro_report_wait.macro_context(conv).name, run)

	def test_a_typed_follow_up_turn_is_not_the_step(self):
		run, conv, step_turn = self._mk_gated(state="done")
		self._turn_as(conv, "typed0000001", seed=self._msg(conv, "user", origin="human", content="hi"))
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_the_resume_turn_gets_the_context(self):
		run, conv, step_turn = self._mk_gated(state="done", resume_turn="resume000001")
		self._turn_as(conv, "resume000001")
		self.assertEqual(macro_report_wait.macro_context(conv).resume_turn, "resume000001")

	def test_the_owners_retry_on_the_current_seed_is_accepted_while_no_resume_turn(self):
		run, conv, step_turn = self._mk_gated(state="errored")
		seed = frappe.db.get_value(TURN, step_turn, "seed_message")
		self._turn_as(conv, "retry0000001", seed=seed)
		self.assertEqual(macro_report_wait.macro_context(conv).name, run)

	def test_a_retry_is_not_accepted_once_a_resume_turn_is_set(self):
		run, conv, step_turn = self._mk_gated(state="errored", resume_turn="resume000001")
		seed = frappe.db.get_value(TURN, step_turn, "seed_message")
		self._turn_as(conv, "retry0000001", seed=seed)
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_retry_on_an_older_steps_seed_is_not_accepted(self):
		run, conv, step_turn = self._mk_gated(state="done")
		old_seed = frappe.db.get_value(TURN, step_turn, "seed_message")
		newer = self._msg(conv, "user", origin="macro", content="step 2")  # the newest step message
		self._turn_as(conv, "newer0000001", state="done", seed=newer)
		self._turn_as(conv, "retry0000001", seed=old_seed)
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_failed_wait_gets_no_context(self):
		run, conv, _ = self._mk_gated(waiting_failed=1)
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_another_user_gets_no_context(self):
		run, conv, _ = self._mk_gated()
		frappe.set_user("Administrator")
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_run_that_is_not_running_gets_no_context(self):
		for status in ("queued", "waiting_capacity", "completed", "failed", "stopped"):
			run, conv, _ = self._mk_gated()
			frappe.db.set_value(RUN, run, "status", status, update_modified=False)
			with self.subTest(status=status):
				self.assertIsNone(macro_report_wait.macro_context(conv))
			self._purge()

	def test_no_run_in_the_conversation(self):
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": "plain"})
		conv.flags.ignore_permissions = True
		conv.insert()
		self.assertIsNone(macro_report_wait.macro_context(conv.name))
		self.assertIsNone(macro_report_wait.macro_context(None))
		self.assertIsNone(macro_report_wait.macro_context(""))

	def test_no_live_turn(self):
		run, conv, _ = self._mk_gated(state="done")
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_run_not_yet_started_or_with_nothing_sent_gets_no_context(self):
		run, conv, _ = self._mk_gated(started_at=None)
		self.assertIsNone(macro_report_wait.macro_context(conv))
		frappe.db.set_value(
			RUN, run, {"started_at": now_datetime(), "current_step": 0}, update_modified=False
		)
		self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_the_kill_switch_gives_none(self):
		run, conv, _ = self._mk_gated()
		with patch.dict(frappe.conf, {"jarvis_macro_report_wait": 0}):
			self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_bench_without_the_turn_machine_gives_none(self):
		run, conv, _ = self._mk_gated()
		with patch(_MACHINE, return_value=False):
			self.assertIsNone(macro_report_wait.macro_context(conv))

	def test_a_lookup_error_fails_closed(self):
		run, conv, _ = self._mk_gated()
		with patch("jarvis.chat.held_sheets.live_turn", side_effect=RuntimeError("db gone")):
			self.assertIsNone(macro_report_wait.macro_context(conv))


class TestConversationForSession(GateBase):
	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		super().tearDown()

	def test_resolves_the_conversation_by_session_key(self):
		run, conv, _ = self._mk_gated()
		frappe.db.set_value(CONV, conv, "session_key", "sk-report-wait", update_modified=False)
		_agent_run_ctx.set_session_key("sk-report-wait")
		self.assertEqual(macro_report_wait.conversation_for_session(), conv)

	def test_none_without_a_session_key_or_a_match(self):
		_agent_run_ctx.clear_session_key()
		self.assertIsNone(macro_report_wait.conversation_for_session())
		_agent_run_ctx.set_session_key("sk-no-such-session")
		self.assertIsNone(macro_report_wait.conversation_for_session())


class TestRunReportInsideARun(GateBase):
	"""``run_report`` end to end: a copy older than the run is not served inside the
	step's own turn, and every other caller gets the old copy as before."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		_ensure_report(PREP_REPORT, prepared=1)
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		frappe.db.commit()
		self.addCleanup(_agent_run_ctx.clear_session_key)

	def _in_session(self, conv):
		frappe.db.set_value(CONV, conv, "session_key", "sk-report-wait", update_modified=False)
		frappe.db.commit()
		_agent_run_ctx.set_session_key("sk-report-wait")

	def _run_report(self, copy_age_seconds=3600, user=OWNER):
		"""``run_report`` as ``user`` with an OLD completed copy of the report on file."""
		with (
			_completed([{"name": "OLD"}], owner=user) as old,
			patch(_ENQUEUE),
			patch("jarvis.tools._prepared_reports.gate_report"),
			patch("jarvis.tools._prepared_reports.gate_filters"),
		):
			frappe.db.set_value(
				"Prepared Report", old, "creation", add_to_date(now_datetime(), seconds=-copy_age_seconds)
			)
			frappe.set_user(user)
			return run_report(report_name=PREP_REPORT), old

	def test_inside_the_step_an_old_copy_is_not_served(self):
		run, conv, _ = self._mk_gated()
		self._in_session(conv)
		env, old = self._run_report()
		self.assertEqual(env["status"], "started")
		self.assertIs(env["waiting"], True)
		self.assertNotEqual(env["run"], old)

	def test_inside_the_step_a_copy_made_after_the_run_started_is_served(self):
		run, conv, _ = self._mk_gated()
		self._in_session(conv)
		env, old = self._run_report(copy_age_seconds=60)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["run"], old)

	def _assert_as_today(self, conv):
		self._in_session(conv)
		env, old = self._run_report()
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["run"], old)
		self.assertNotIn("waiting", env)

	def test_outside_a_session_it_is_as_today(self):
		run, conv, _ = self._mk_gated()
		_agent_run_ctx.clear_session_key()
		env, old = self._run_report()
		self.assertEqual((env["status"], env["run"]), ("ready", old))

	def test_the_kill_switch_is_as_today(self):
		run, conv, _ = self._mk_gated()
		with patch.dict(frappe.conf, {"jarvis_macro_report_wait": 0}):
			self._assert_as_today(conv)

	def test_a_bench_without_the_turn_machine_is_as_today(self):
		run, conv, _ = self._mk_gated()
		with patch(_MACHINE, return_value=False):
			self._assert_as_today(conv)

	def test_a_conversation_without_a_run_is_as_today(self):
		run, conv, _ = self._mk_gated()
		frappe.db.set_value(RUN, run, "status", "completed", update_modified=False)
		self._assert_as_today(conv)

	def test_another_user_is_as_today(self):
		run, conv, _ = self._mk_gated()
		self._in_session(conv)
		env, old = self._run_report(user="Administrator")
		self.assertEqual((env["status"], env["run"]), ("ready", old))
		self.assertNotIn("waiting", env)

	def test_a_typed_follow_up_turn_is_as_today(self):
		run, conv, step_turn = self._mk_gated()
		frappe.db.set_value(TURN, step_turn, "state", "done")
		self._turn_as(conv, "typed0000001", seed=self._msg(conv, "user", origin="human", content="hi"))
		self._assert_as_today(conv)

	def test_a_failed_wait_is_as_today(self):
		run, conv, _ = self._mk_gated(waiting_failed=1)
		self._assert_as_today(conv)

	def test_a_lookup_error_resolving_the_session_is_as_today(self):
		# The whole resolution fails closed: a database error while finding the
		# conversation must not reach run_report.
		run, conv, _ = self._mk_gated()
		self._in_session(conv)
		real = frappe.db.get_value

		def broken(doctype, *args, **kwargs):
			if doctype == CONV:
				raise RuntimeError("db gone")
			return real(doctype, *args, **kwargs)

		with patch("frappe.db.get_value", side_effect=broken):
			self.assertIsNone(macro_report_wait.current_context())
			env, old = self._run_report()
		self.assertEqual((env["status"], env["run"]), ("ready", old))
		self.assertNotIn("waiting", env)

	def test_the_inline_path_is_untouched(self):
		_ensure_report("Jarvis Test Inline Report", prepared=0)
		run, conv, _ = self._mk_gated()
		self._in_session(conv)
		with patch("jarvis.tools._prepared_reports.handle_prepared") as handle:
			frappe.set_user(OWNER)
			run_report(report_name="Jarvis Test Inline Report")
		handle.assert_not_called()


REPORT_B = "Jarvis Test Prepared Report B"
_ACME = {"company": "Acme"}
_GLOBEX = {"company": "Globex"}


class ParkBase(GateBase):
	"""A two-step run at step 1 whose step turn has ended, with the ``run_report`` rows
	it left in the chat. ``_end_step`` builds that turn; ``_advance`` is the turn-end hook."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		for name in (PREP_REPORT, REPORT_B):
			_ensure_report(name, prepared=1)
		self._clear_prepared()
		self.addCleanup(self._clear_prepared)
		# A real wake would queue a job some worker on the site might run mid-test.
		self._wake_patch = patch("jarvis.chat.macro_report_wait.wake")
		self.wake = self._wake_patch.start()
		self.addCleanup(self._wake_patch.stop)

	def _clear_prepared(self):
		frappe.db.delete("Prepared Report", {"report_name": ["in", [PREP_REPORT, REPORT_B]]})
		frappe.db.commit()

	def _pr(self, report=PREP_REPORT, filters=None, status="Started"):
		"""A Prepared Report of the run's owner, with ``filters`` as the STORED filters."""
		with patch(_ENQUEUE):
			pr = frappe.get_doc(
				{"doctype": "Prepared Report", "report_name": report, "filters": json.dumps(filters or {})}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Prepared Report", pr.name, {"status": status, "owner": OWNER}, update_modified=False
		)
		return pr.name

	def _tool(self, conv, status, run, *, report=PREP_REPORT, filters=None, waiting=False, error="", **kw):
		"""One ``run_report`` row as the chat stores it: the model's args, the envelope."""
		frappe.set_user(OWNER)
		seq = self._seq[conv] = self._seq.get(conv, 0) + 1
		data = {"prepared_report": True, "status": status, "report_name": report, "run": run}
		if waiting:
			data["waiting"] = True
		if error:
			data.update(error=error, message=f"I couldn't run that report: {error}")
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": seq,
				"role": "tool",
				"tool_name": kw.get("tool_name", "run_report"),
				"tool_status": "completed",
				"tool_args": json.dumps({"report_name": report, "filters": filters or _ACME}),
				"tool_result": json.dumps({"ok": True, "data": data}),
			}
		)
		doc.flags.ignore_permissions = True
		doc.flags.jarvis_server_write = True
		doc.insert()

	def _end_step(self, run, conv, rows, *, state="done", error="", seed=None, turn_id=None):
		"""Step 1's turn, ended: the macro message (or ``seed``, for a retry of it), the
		Turn (inserted FIRST, as on the turn machine, where it exists before its tool
		rows), the ``rows`` (kwargs of ``_tool``) and the reply. Returns the Turn's id,
		the step's fixed one unless ``turn_id`` names another."""
		seed = seed or self._msg(conv, "user", origin="macro", content="step 1")
		step_turn = turn_id or macros._step_turn_id(run, 0)
		turn = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": step_turn,
				"conversation": conv,
				"relay_target_id": "t",
				"turn_class": "interactive",
				"state": "streaming",
				"seed_message": seed,
			}
		)
		turn.flags.ignore_permissions = True
		turn.insert()
		for row in rows:
			self._tool(conv, **row)
		assistant = self._msg(conv, "assistant", content="It is generating.")
		frappe.db.set_value(
			TURN,
			step_turn,
			{"state": state, "error": error, "assistant_message": assistant},
			update_modified=False,
		)
		frappe.db.commit()
		self.seed = seed
		return step_turn

	def _advance(self, conv, step_turn, *, errored=False):
		"""The turn-end hook, with the next step's dispatch recorded. Returns the mock."""
		with self._dispatching(conv) as enqueue:
			macros.advance_after_turn(conv, errored=errored, run_id=step_turn)
		return enqueue

	def _values(self, run):
		return frappe.db.get_value(
			RUN,
			run,
			["status", "error", "current_step", "waiting_on", "waiting_since", "waited"],
			as_dict=True,
		)


class TestParkingAtStepEnd(ParkBase):
	def test_two_reports_in_one_step_park_the_run_once_awaiting_both(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		a = self._pr(PREP_REPORT, {"company": "Acme", "year": 2026})
		b = self._pr(REPORT_B, _GLOBEX)
		step_turn = self._end_step(
			run,
			conv,
			[
				# The model's args differ from what Frappe stored: the stored ones are kept.
				dict(status="started", run=a, filters={"company": "acme"}, waiting=True),
				dict(status="started", run=b, report=REPORT_B, filters=_GLOBEX, waiting=True),
			],
		)
		enqueue = self._advance(conv, step_turn)
		got = self._values(run)
		self.assertEqual((got.status, got.current_step, got.waited), ("waiting_report", 1, 1))
		self.assertTrue(got.waiting_since)
		self.assertEqual(
			json.loads(got.waiting_on),
			[
				{
					"prepared_report": a,
					"report_name": PREP_REPORT,
					"filters": {"company": "Acme", "year": 2026},
				},
				{"prepared_report": b, "report_name": REPORT_B, "filters": _GLOBEX},
			],
		)
		enqueue.assert_not_called()  # no next step, and the run did not end

	def test_the_park_is_announced(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		a, b = self._pr(PREP_REPORT, _ACME), self._pr(REPORT_B, _GLOBEX)
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=a, waiting=True),
				dict(status="generating", run=b, report=REPORT_B, filters=_GLOBEX, waiting=True),
			],
		)
		with patch("jarvis.chat.macro_report_wait.publish_to_user") as publish:
			self._advance(conv, step_turn)
		(owner, payload), _ = publish.call_args
		self.assertEqual(owner, OWNER)
		self.assertEqual(payload["kind"], "macro:progress")
		self.assertEqual(payload["status"], "waiting_report")
		self.assertEqual(payload["macro_run"], run)
		self.assertEqual(payload["label"], f"Waiting for report: {PREP_REPORT}, {REPORT_B}")

	def test_waiting_then_ready_for_the_same_report_and_filters_does_not_park(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		first, second = self._pr(PREP_REPORT, _ACME), self._pr(PREP_REPORT, _ACME, "Completed")
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=first, waiting=True),
				dict(status="ready", run=second),  # a later answer for the same report+filters
			],
		)
		enqueue = self._advance(conv, step_turn)
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self._values(run).current_step, 2)
		self.assertNotEqual(self._values(run).status, "waiting_report")

	def test_waiting_then_ready_for_the_same_run_does_not_park(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME, "Completed")
		step_turn = self._end_step(
			run, conv, [dict(status="started", run=pr, waiting=True), dict(status="ready", run=pr)]
		)
		self.assertEqual(self._advance(conv, step_turn).call_count, 1)

	def test_a_wait_for_another_report_still_parks(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		a, b = self._pr(PREP_REPORT, _ACME, "Completed"), self._pr(REPORT_B, _GLOBEX)
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=a, waiting=True),
				dict(status="started", run=b, report=REPORT_B, filters=_GLOBEX, waiting=True),
				dict(status="ready", run=a),
			],
		)
		self._advance(conv, step_turn)
		got = self._values(run)
		self.assertEqual(got.status, "waiting_report")
		self.assertEqual([w["prepared_report"] for w in json.loads(got.waiting_on)], [b])

	def test_started_without_waiting_does_not_park(self):
		# What the tool answers with the kill switch off: today's behaviour, a step that
		# got `started` is judged done.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr)])
		enqueue = self._advance(conv, step_turn)
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self._values(run).current_step, 2)

	def test_the_kill_switch_and_a_bench_without_the_turn_machine_never_park(self):
		for label, patcher in (
			("switch", patch.dict(frappe.conf, {"jarvis_macro_report_wait": 0})),
			("machine", patch(_MACHINE, return_value=False)),
		):
			run, conv, _ = self._mk_run(steps=2, at_step=1)
			pr = self._pr(PREP_REPORT, _ACME)
			step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
			with self.subTest(label), patcher:
				self.assertEqual(self._advance(conv, step_turn).call_count, 1)
				self.assertEqual(self._values(run).current_step, 2)
			self._purge()

	def test_failed_then_ready_for_the_same_report_is_a_normal_verdict(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		bad, good = self._pr(PREP_REPORT, _ACME, "Error"), self._pr(PREP_REPORT, _ACME, "Completed")
		step_turn = self._end_step(
			run, conv, [dict(status="failed", run=bad, error="Boom."), dict(status="ready", run=good)]
		)
		self.assertEqual(self._advance(conv, step_turn).call_count, 1)
		self.assertEqual(self._values(run).current_step, 2)

	def test_a_failed_read_in_the_first_turn_ends_the_run_with_the_reason(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, armed=True)
		bad = self._pr(PREP_REPORT, _ACME, "Error")
		step_turn = self._end_step(
			run, conv, [dict(status="failed", run=bad, error="Report timed out.", filters=_ACME)]
		)
		enqueue = self._advance(conv, step_turn)
		got = self._values(run)
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, f"Step 1 stopped: {PREP_REPORT} failed: Report timed out.")
		enqueue.assert_not_called()
		self.assertFalse(frappe.db.get_value(CONV, conv, "skip_confirmation"))

	def test_a_failed_read_wins_over_a_wait_for_another_report(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		bad, b = self._pr(PREP_REPORT, _ACME, "Error"), self._pr(REPORT_B, _GLOBEX)
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="failed", run=bad, error="Nope."),
				dict(status="started", run=b, report=REPORT_B, filters=_GLOBEX, waiting=True),
			],
		)
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "failed")

	def test_a_failed_read_with_no_run_is_judged_by_report_and_filters(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn = self._end_step(run, conv, [dict(status="failed", run="", error="Blocked.")])
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).error, f"Step 1 stopped: {PREP_REPORT} failed: Blocked.")

	def test_the_reason_falls_back_to_the_message_and_is_one_bounded_line(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		bad = self._pr(PREP_REPORT, _ACME, "Error")
		step_turn = self._end_step(run, conv, [dict(status="failed", run=bad, error="x" * 400 + "\nsecond")])
		self._advance(conv, step_turn)
		reason = self._values(run).error.split("failed: ", 1)[1]
		self.assertLessEqual(len(reason), 120)
		self.assertNotIn("\n", reason)

	def test_an_errored_turn_with_an_open_wait_takes_the_error_path(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, stop_on_error=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(
			run,
			conv,
			[dict(status="started", run=pr, waiting=True)],
			state="errored",
			error="The model provider failed.",
		)
		enqueue = self._advance(conv, step_turn, errored=True)
		got = self._values(run)
		self.assertEqual(got.status, "failed")
		self.assertTrue(got.error.startswith("Step 1 failed:"))
		self.assertFalse(got.waiting_on)
		enqueue.assert_not_called()

	def test_a_sessions_yield_row_after_the_waiting_row_still_parks(self):
		# Measured with a real model: it stops, calls the runtime's sessions_yield, and the
		# turn still ends normally. Nothing may depend on the turn's last row.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=pr, waiting=True),
				dict(status="x", run="", waiting=False, tool_name="sessions_yield"),
			],
		)
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")

	def test_rows_of_other_turns_are_ignored(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		old, typed = self._pr(PREP_REPORT, _ACME), self._pr(REPORT_B, _GLOBEX)
		# A turn before the step's, with a wait of its own...
		self._msg(conv, "user", origin="human", content="earlier")
		self._tool(conv, "started", old, waiting=True)
		step_turn = self._end_step(run, conv, [])
		# ...and the owner's follow-up after it.
		self._msg(conv, "user", origin="human", content="and another")
		self._tool(conv, "started", typed, report=REPORT_B, filters=_GLOBEX, waiting=True)
		self.assertEqual(self._advance(conv, step_turn).call_count, 1)
		self.assertEqual(self._values(run).current_step, 2)

	def test_the_derivation_reads_the_turns_own_rows_only(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		turn = frappe.db.get_value(TURN, step_turn, ["name", "seed_message"], as_dict=True)
		waits, failed = macro_report_wait.step_waits(conv, turn)
		self.assertEqual([w["prepared_report"] for w in waits], [pr])
		self.assertEqual(failed, [])
		self.assertEqual(macro_report_wait.step_waits(conv, None), ([], []))

	def test_the_python_reading_gives_the_same_answer_as_the_sql_one(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		a, b = self._pr(PREP_REPORT, _ACME), self._pr(REPORT_B, _GLOBEX, "Error")
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=a, waiting=True),
				dict(status="failed", run=b, report=REPORT_B, filters=_GLOBEX, error="Nope."),
			],
		)
		turn = frappe.db.get_value(TURN, step_turn, ["name", "seed_message"], as_dict=True)
		expected = macro_report_wait.step_waits(conv, turn)
		with patch.object(report_runs, "_json_functions_available", return_value=False):
			self.assertEqual(macro_report_wait.step_waits(conv, turn), expected)
		self.assertEqual([w["prepared_report"] for w in expected[0]], [a])
		self.assertEqual([f["report_name"] for f in expected[1]], [REPORT_B])

	def test_a_wait_whose_prepared_report_is_gone_is_kept_with_the_models_filters(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		step_turn = self._end_step(run, conv, [dict(status="started", run="PR-GONE", waiting=True)])
		self._advance(conv, step_turn)
		got = self._values(run)
		self.assertEqual(got.status, "waiting_report")
		self.assertEqual(
			json.loads(got.waiting_on),
			[{"prepared_report": "PR-GONE", "report_name": PREP_REPORT, "filters": _ACME}],
		)

	def test_a_report_of_another_user_is_not_trusted_for_its_filters(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, {"company": "Secret"})
		frappe.db.set_value("Prepared Report", pr, "owner", "Administrator", update_modified=False)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		self._advance(conv, step_turn)
		[wait] = json.loads(self._values(run).waiting_on)
		self.assertEqual(wait["filters"], _ACME)

	def test_a_failed_wait_never_parks_again(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.db.set_value(RUN, run, "waiting_failed", 1, update_modified=False)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		self._advance(conv, step_turn)
		self.assertNotEqual(self._values(run).status, "waiting_report")

	def test_a_failed_read_in_a_continuation_ends_the_run_with_the_reason(self):
		# A resumed step is ended by its continuation alone; a failed read in it must not
		# fall through to "success" (T4).
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.db.set_value(RUN, run, "resume_turn", "resume000001", update_modified=False)
		bad = self._pr(PREP_REPORT, _ACME, "Error")
		step_turn = self._end_step(
			run, conv, [dict(status="failed", run=bad, error="Nope.")], turn_id="resume000001"
		)
		self._advance(conv, step_turn)
		got = self._values(run)
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, f"Step 1 stopped: {PREP_REPORT} failed: Nope.")

	def test_a_derivation_error_falls_back_to_todays_behaviour(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		with patch.object(report_runs, "_key", side_effect=RuntimeError("db gone")):
			self.assertEqual(self._advance(conv, step_turn).call_count, 1)
		self.assertEqual(self._values(run).current_step, 2)

	def test_a_run_stopped_meanwhile_is_not_parked(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.db.set_value(RUN, run, "status", "stopped", update_modified=False)
		doc = frappe.get_doc(RUN, run)
		macro_doc = frappe.get_doc("Jarvis Macro", doc.macro)
		self.assertFalse(macro_report_wait.park(doc, macro_doc, [{"prepared_report": "x"}]))
		self.assertEqual(self._values(run).status, "stopped")

	def test_a_row_without_a_status_does_not_supersede_an_open_wait(self):
		# A tool error (ok: false) carries no envelope. It says nothing about the report.
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(
			run,
			conv,
			[dict(status="started", run=pr, waiting=True), dict(status="", run="", filters=_ACME)],
		)
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")

	def test_a_row_without_a_status_does_not_hide_a_failed_read(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		bad = self._pr(PREP_REPORT, _ACME, "Error")
		step_turn = self._end_step(
			run, conv, [dict(status="failed", run=bad, error="Nope."), dict(status="", run="")]
		)
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "failed")

	def test_a_retried_step_is_judged_by_the_retrys_rows_only(self):
		# The failed first attempt's rows (a failed read, a wait for another report) are
		# in the seq range of the retry, which is a second Turn on the same seed.
		for label, retry_rows in (("clean", []), ("ready", "ready")):
			run, conv, _ = self._mk_run(steps=2, at_step=1)
			bad = self._pr(PREP_REPORT, _ACME, "Error")
			other = self._pr(REPORT_B, _GLOBEX)
			good = self._pr(PREP_REPORT, _GLOBEX, "Completed")
			first = self._end_step(
				run,
				conv,
				[
					dict(status="failed", run=bad, error="Nope."),
					dict(status="started", run=other, report=REPORT_B, filters=_GLOBEX, waiting=True),
				],
				state="errored",
				error="The model provider failed.",
			)
			rows = [dict(status="ready", run=good, filters=_GLOBEX)] if retry_rows == "ready" else []
			retry = self._end_step(run, conv, rows, seed=self.seed, turn_id="retry0000001")
			self.assertNotEqual(first, retry)
			with self.subTest(label):
				enqueue = self._advance(conv, retry)
				self.assertEqual(enqueue.call_count, 1)
				self.assertEqual(self._values(run).current_step, 2)
				self.assertNotIn(self._values(run).status, ("waiting_report", "failed"))
			self._purge()

	def test_one_unreadable_row_does_not_blank_the_rest(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		bad_row, good = self._pr(PREP_REPORT, _ACME), self._pr(REPORT_B, _GLOBEX)
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=bad_row, waiting=True),
				dict(status="started", run=good, report=REPORT_B, filters=_GLOBEX, waiting=True),
			],
		)
		real = report_runs._key

		def key(report, filters, defaults):
			if report == PREP_REPORT:
				raise RuntimeError("bad row")
			return real(report, filters, defaults)

		with (
			patch.object(report_runs, "_key", side_effect=key),
			patch("frappe.log_error") as log,
		):
			self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")
		self.assertEqual([w["prepared_report"] for w in json.loads(self._values(run).waiting_on)], [good])
		self.assertEqual(log.call_count, 1)  # once, not per row

	def test_a_step_the_user_stopped_is_not_parked_even_without_stop_on_error(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1, stop_on_error=0)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(
			run, conv, [dict(status="started", run=pr, waiting=True)], state="cancelled"
		)
		self._advance(conv, step_turn, errored=True)
		self.assertNotEqual(self._values(run).status, "waiting_report")
		self.assertFalse(self._values(run).waiting_on)

	def test_a_turn_end_that_is_not_the_steps_does_not_park(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		self._end_step(run, conv, [], state="streaming")  # the step, still going
		pr = self._pr(PREP_REPORT, _ACME)
		typed_seed = self._msg(conv, "user", origin="human", content="and this")
		typed = "typed0000001"
		turn = frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": typed,
				"conversation": conv,
				"relay_target_id": "t",
				"turn_class": "interactive",
				"state": "streaming",
				"seed_message": typed_seed,
			}
		)
		turn.flags.ignore_permissions = True
		turn.insert()
		self._tool(conv, "started", pr, waiting=True)
		frappe.db.set_value(TURN, typed, "state", "done")
		frappe.db.commit()
		self._advance(conv, typed)
		self.assertEqual(self._values(run).status, "running")
		self.assertFalse(self._values(run).waiting_on)

	def test_a_summarized_run_parks_as_one_step(self):
		run, conv, _ = self._mk_run(steps=3, at_step=1)
		frappe.db.set_value(RUN, run, {"run_mode": "merged", "total_steps": 1})
		frappe.db.commit()
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		with patch("jarvis.chat.macro_report_wait.publish_to_user") as publish:
			enqueue = self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")
		enqueue.assert_not_called()
		payload = publish.call_args.args[1]
		self.assertEqual((payload["step"], payload["total"]), (1, 1))


class TestAParkedRunIsLive(ParkBase):
	def _parked(self, **kw):
		run, conv, macro = self._mk_run(steps=2, at_step=1, **kw)
		pr = self._pr(PREP_REPORT, _ACME)
		step_turn = self._end_step(run, conv, [dict(status="started", run=pr, waiting=True)])
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")
		return run, conv, macro

	def test_stopping_a_waiting_run_stops_it_and_disarms_the_chat(self):
		run, conv, _ = self._parked(armed=True)
		frappe.set_user(OWNER)
		self.assertEqual(macros.stop_macro_run(run), {"ok": True})
		self.assertEqual(self._values(run).status, "stopped")
		self.assertFalse(frappe.db.get_value(CONV, conv, "skip_confirmation"))

	def test_the_macro_counts_the_waiting_run_as_live(self):
		run, conv, macro = self._parked()
		self.assertEqual(macros.live_runs_of(macro), [run])

	def test_a_hold_stops_a_waiting_run(self):
		run, conv, macro = self._parked(armed=True)
		self.assertEqual(macros.stop_runs_of_macro(macro, reason="Put on hold.", by="Administrator"), 1)
		self.assertEqual(self._values(run).status, "stopped")
		self.assertFalse(frappe.db.get_value(CONV, conv, "skip_confirmation"))

	def test_status_lists_agree_with_the_doctype(self):
		options = set(frappe.get_meta(RUN).get_field("status").options.split("\n"))
		self.assertIn("waiting_report", options)
		self.assertEqual(macros_api._RUN_STATUSES, options)
		self.assertTrue(set(macros._LIVE_RUN_STATUSES) <= options)
		self.assertIn("waiting_report", macros._LIVE_RUN_STATUSES)


# --------------------------------------------------------------------------- #
# waking a parked run
# --------------------------------------------------------------------------- #
_ENQUEUE_TURN = "jarvis.chat.api._enqueue_turn"
_OVERLOADED = "jarvis.chat.admission.shard_overloaded"
_BAD_ERROR = (
	"Traceback (most recent call last):\n"
	'  File "report.py", line 3, in execute\n'
	"frappe.exceptions.ValidationError: Set the `Company` filter\nbefore running this"
)


class ResumeBase(ParkBase):
	"""A two-step run parked on one prepared report (``self.pr``, still Started)."""

	REPORTS = ((PREP_REPORT, _ACME),)

	def setUp(self):
		super().setUp()
		self.run, self.conv, self.macro = self._parked(self.REPORTS)

	def _parked(self, reports=((PREP_REPORT, _ACME),), **kw):
		run, conv, macro = self._mk_run(steps=2, at_step=1, **kw)
		self.prs = [self._pr(report, filters) for report, filters in reports]
		self.pr = self.prs[0]
		step_turn = self._end_step(
			run,
			conv,
			[
				dict(status="started", run=pr, report=report, filters=filters, waiting=True)
				for pr, (report, filters) in zip(self.prs, reports, strict=True)
			],
		)
		self._advance(conv, step_turn)
		self.assertEqual(self._values(run).status, "waiting_report")
		frappe.set_user("Administrator")
		return run, conv, macro

	def _set_pr(self, name=None, **values):
		frappe.db.set_value("Prepared Report", name or self.pr, values, update_modified=False)
		frappe.db.commit()

	def _ready(self, name=None):
		self._set_pr(name, status="Completed", report_end_time="2026-10-28 09:30:00")

	def _row(self, run=None):
		return frappe.db.get_value(
			RUN,
			run or self.run,
			[
				"status",
				"error",
				"resume_turn",
				"resume_count",
				"step_resumes",
				"resume_compensations",
				"waiting_failed",
				"step_continuations",
				"waiting_on",
			],
			as_dict=True,
		)

	def _sending(self, result=None, raises=None):
		"""Stand-in for ``api._enqueue_turn``. Notes the run's row as it stood AT the call
		(``seen``), and, like the real gate, leaves the Turn named by ``run_id`` unless
		``result`` or ``raises`` says it did not get that far."""
		seen = []

		def enqueue(conversation, prompt, **kw):
			seen.append(self._row())
			if raises:
				raise raises
			if result is not None:
				return result
			self._turn_as(conversation, kw["run_id"], state="queued")
			return {"run_id": kw["run_id"], "message_id": None}

		patcher = patch(_ENQUEUE_TURN, side_effect=enqueue)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		mock.seen = seen
		return mock

	def _resume(self, run=None):
		frappe.set_user("Administrator")
		macro_report_wait.resume_report_waits(run)
		frappe.set_user("Administrator")


class TestAReadyReportContinuesTheStep(ResumeBase):
	def test_a_completed_report_sends_exactly_one_continuation_hook_and_cron_alike(self):
		self._ready()
		enqueue = self._sending()
		self._resume(self.run)  # the Prepared Report hook's wake
		self._resume()  # the cron, straight after
		self.assertEqual(enqueue.call_count, 1)
		args, kw = enqueue.call_args
		self.assertEqual(args[0], self.conv)
		self.assertEqual(
			{k: v for k, v in kw.items() if k != "run_id"},
			{"origin": "continuation", "hidden": True, "interactive": False},
		)
		got = self._row()
		self.assertEqual(kw["run_id"], got.resume_turn)
		self.assertEqual(kw["run_id"], hashlib.sha1(f"{self.run}:0:report:1".encode()).hexdigest()[:12])
		self.assertEqual(got.status, "running")

	def test_the_turn_id_is_in_the_row_before_the_enqueue_and_the_counters_are_not(self):
		self._ready()
		enqueue = self._sending()
		self._resume()
		(seen,) = enqueue.seen
		self.assertEqual(seen.status, "running")
		self.assertEqual(seen.resume_turn, self._row().resume_turn)
		self.assertTrue(seen.resume_turn)
		self.assertEqual((seen.resume_count, seen.step_resumes), (0, 0))
		after = self._row()
		self.assertEqual((after.resume_count, after.step_resumes, after.resume_compensations), (1, 1, 0))
		self.assertEqual(json.loads(after.step_continuations), {"0": after.resume_turn})

	def test_the_continuation_is_recorded_beside_the_steps_before_it(self):
		frappe.db.set_value(RUN, self.run, "step_continuations", json.dumps({"3": "older"}))
		frappe.db.commit()
		self._ready()
		self._sending()
		self._resume()
		self.assertEqual(
			json.loads(self._row().step_continuations), {"3": "older", "0": self._row().resume_turn}
		)

	def test_the_prompt_names_the_report_and_carries_the_stored_filters_verbatim(self):
		self._ready()
		enqueue = self._sending()
		self._resume()
		prompt = enqueue.call_args.args[1]
		self.assertEqual(
			prompt,
			f"{PREP_REPORT} (generated 2026-10-28 09:30:00) is ready. Call run_report with exactly "
			'these filters and finish this step: `{"company": "Acme"}`',
		)

	def test_a_summarized_run_finishes_the_remaining_task(self):
		frappe.db.set_value(RUN, self.run, {"run_mode": "merged", "total_steps": 1})
		frappe.db.commit()
		self._ready()
		enqueue = self._sending()
		self._resume()
		self.assertIn("and finish the remaining task: `", enqueue.call_args.args[1])
		self.assertNotIn("finish this step", enqueue.call_args.args[1])
		self.assertEqual(self._row().status, "running")

	def test_two_reports_are_one_line_each_and_a_backtick_in_a_filter_cannot_close_the_span(self):
		run, conv, _ = self._parked(
			reports=((PREP_REPORT, {"company": "A`B"}), (REPORT_B, _GLOBEX)), tag="two"
		)
		self.run, self.conv = run, conv
		for pr in self.prs:
			self._ready(pr)
		enqueue = self._sending()
		self._resume()
		lines = enqueue.call_args.args[1].split("\n")
		self.assertEqual(len(lines), 2)
		self.assertTrue(lines[0].startswith(f"{PREP_REPORT} "))
		self.assertTrue(lines[1].startswith(f"{REPORT_B} "))
		for line in lines:
			self.assertEqual(line.count("`"), 2, line)
		self.assertEqual(json.loads(lines[0].split("`")[1]), {"company": "A`B"})

	def test_a_report_still_generating_keeps_the_run_parked(self):
		for status in ("Queued", "Started"):
			with self.subTest(status=status):
				self._set_pr(status=status)
				enqueue = self._sending()
				self._resume()
				enqueue.assert_not_called()
				self.assertEqual(self._row().status, "waiting_report")

	def test_it_waits_for_every_report_of_the_step(self):
		run, conv, _ = self._parked(reports=((PREP_REPORT, _ACME), (REPORT_B, _GLOBEX)), tag="two")
		self.run, self.conv = run, conv
		self._ready(self.prs[0])
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row().status, "waiting_report")
		self._ready(self.prs[1])
		self._resume()
		self.assertEqual(enqueue.call_count, 1)

	def test_a_report_of_another_user_is_not_the_awaited_one(self):
		self._ready()
		self._set_pr(owner="Administrator")
		enqueue = self._sending()
		self._resume()
		self.assertIn("was removed", enqueue.call_args.args[1])

	def test_a_run_that_is_not_waiting_is_left_alone(self):
		self._ready()
		frappe.db.set_value(RUN, self.run, "status", "stopped")
		frappe.db.commit()
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()

	def test_a_run_stopped_between_the_list_and_the_lock_is_left_stopped(self):
		self._ready()
		enqueue = self._sending()

		@contextlib.contextmanager
		def stopped_while_waiting_for_the_lock(*a, **k):
			# A Stop from another worker, committed after this one listed the run and
			# before it got the lock.
			with other_connection() as other:
				other.sql(f"UPDATE `tab{RUN}` SET status='stopped' WHERE name=%s", self.run)
				other.commit()
			yield True

		with (
			patch("jarvis._redis_lock.redis_lock", stopped_while_waiting_for_the_lock),
			patch.object(macro_report_wait, "_state_of", wraps=macro_report_wait._state_of) as looked,
		):
			self._resume()  # the cron: lists first, so its transaction holds a read view
		enqueue.assert_not_called()
		looked.assert_not_called()  # it never acted on the stale `waiting_report`
		frappe.db.commit()
		self.assertEqual(self._row().status, "stopped")

	def test_a_run_whose_lock_is_held_is_skipped(self):
		self._ready()
		enqueue = self._sending()

		@contextlib.contextmanager
		def busy(*a, **k):
			yield False

		with patch("jarvis._redis_lock.redis_lock", busy):
			self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row().status, "waiting_report")


class TestAResumeThatCannotGoYet(ResumeBase):
	def test_the_turn_machine_off_or_draining_keeps_the_run_parked_untouched(self):
		self._ready()
		before = self._row()
		enqueue = self._sending()
		seen = []
		with patch(_MACHINE, side_effect=lambda **kw: seen.append(kw) or False):
			self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row(), before)
		self.assertEqual(seen, [{"from_db": True}])  # the fenced, DB-authoritative read

	def test_a_full_queue_keeps_the_run_parked_untouched(self):
		self._ready()
		before = self._row()
		enqueue = self._sending()
		with patch(_OVERLOADED, return_value=True) as full:
			self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row(), before)
		self.assertEqual(full.call_args.args, (self.conv,))
		self.assertTrue(full.call_args.kwargs["background"])  # the line a continuation joins

	def test_a_queue_that_fills_after_the_check_parks_the_run_again_without_counting(self):
		self._ready()
		enqueue = self._sending(result={"ok": False, "overloaded": True, "reason": "busy"})
		self._resume()
		got = self._row()
		self.assertEqual(got.status, "waiting_report")
		self.assertEqual((got.resume_count, got.step_resumes, got.resume_compensations), (0, 0, 0))
		self.assertFalse(got.waiting_failed)
		# The id was chosen once: the next attempt sends the very same turn.
		first = enqueue.call_args.kwargs["run_id"]
		self.assertEqual(got.resume_turn, first)
		self.assertEqual(json.loads(got.waiting_on)[0]["prepared_report"], self.pr)  # the wait is intact
		enqueue = self._sending()
		self._resume()
		self.assertEqual(enqueue.call_args.kwargs["run_id"], first)
		self.assertEqual(self._row().status, "running")


class TestADeliveryThatFailsOrRepeats(ResumeBase):
	def test_an_enqueue_that_raises_is_compensated_and_the_retry_reuses_the_id(self):
		self._ready()
		enqueue = self._sending(raises=RuntimeError("redis down"))
		with patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual(got.status, "waiting_report")
		self.assertEqual((got.resume_compensations, got.resume_count, got.step_resumes), (1, 0, 0))
		first = enqueue.call_args.kwargs["run_id"]
		self.assertEqual(got.resume_turn, first)
		enqueue = self._sending()
		self._resume()
		self.assertEqual(enqueue.call_args.kwargs["run_id"], first)
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.step_resumes), ("running", 1, 1))
		self.assertEqual(got.resume_compensations, 1)  # counted apart, never reset

	def test_a_refusal_that_is_not_overload_is_compensated_too(self):
		self._ready()
		self._sending(result={"ok": False, "reason": "no"})
		with patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual((got.status, got.resume_compensations, got.resume_count), ("waiting_report", 1, 0))

	def test_a_raise_after_the_turn_exists_leaves_the_run_running_and_counts_the_resume(self):
		self._ready()

		def enqueue(conversation, prompt, **kw):
			self._turn_as(conversation, kw["run_id"], state="queued")
			raise RuntimeError("died after the turn was written")

		with patch(_ENQUEUE_TURN, side_effect=enqueue), patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.step_resumes), ("running", 1, 1))
		self.assertEqual(got.resume_compensations, 0)

	def test_a_duplicate_means_it_went_out_so_the_run_stays_running_and_counts(self):
		self._ready()
		# A duplicate means the turn is there: this chat's, under the id the CAS names.
		self._turn_as(self.conv, hashlib.sha1(f"{self.run}:0:report:1".encode()).hexdigest()[:12])
		self._sending(result={"run_id": "x", "message_id": None, "duplicate": True})
		self._resume()
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.step_resumes), ("running", 1, 1))
		self.assertEqual(json.loads(got.step_continuations), {"0": got.resume_turn})

	def _taken_elsewhere(self):
		"""The id the first continuation gets, already the name of a Turn in another chat."""
		_, other_conv, _ = self._mk_run(steps=2, at_step=1, tag="elsewhere")
		taken = hashlib.sha1(f"{self.run}:0:report:1".encode()).hexdigest()[:12]
		self._turn_as(other_conv, taken, state="done")
		return taken

	def test_a_duplicate_that_is_another_chats_turn_did_not_go_out(self):
		self._taken_elsewhere()
		self._ready()
		self._sending(result={"run_id": "x", "message_id": None, "duplicate": True})
		with patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.step_resumes), ("waiting_report", 0, 0))
		self.assertEqual(got.resume_compensations, 1)
		self.assertFalse(got.step_continuations)

	def test_a_raise_with_the_id_taken_by_another_chat_is_compensated_too(self):
		self._taken_elsewhere()
		self._ready()
		self._sending(raises=RuntimeError("died"))
		with patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.resume_compensations), ("waiting_report", 0, 1))

	def test_the_fourth_compensation_ends_the_run_failed(self):
		self._ready()
		self._sending(raises=RuntimeError("redis down"))
		with patch("frappe.log_error"):
			for _ in range(3):
				self._resume()
				self.assertEqual(self._row().status, "waiting_report")
			self._resume()
		got = self._row()
		self.assertEqual(got.status, "failed")
		self.assertIn("could not be continued", got.error)

	def test_the_error_logs_have_fixed_titles_and_come_once_an_hour_per_run(self):
		frappe.db.delete("Error Log", {"method": ["like", "Jarvis: macro report%"]})
		self._ready()
		self._sending(raises=RuntimeError("redis down"))
		for _ in range(4):
			self._resume()
		titles = frappe.get_all(
			"Error Log", filters={"method": ["like", "Jarvis: macro report%"]}, pluck="method"
		)
		self.assertEqual(
			sorted(titles),
			[
				"Jarvis: macro report resume compensated",
				"Jarvis: macro report resume not delivered",
			],
		)
		frappe.db.delete("Error Log", {"method": ["like", "Jarvis: macro report%"]})
		frappe.db.commit()


class TestAReportThatFailed(ResumeBase):
	def _assert_explained(self, enqueue, needle, *, state="running"):
		self.assertEqual(enqueue.call_count, 1)
		prompt = enqueue.call_args.args[1]
		self.assertTrue(
			prompt.startswith(f"{PREP_REPORT} could not be produced. The system's reason, quoted as data")
		)
		self.assertIn(needle, prompt)
		self.assertTrue(prompt.endswith("Tell the user this step stopped and why. Do not continue the task."))
		got = self._row()
		self.assertEqual(got.status, state)
		self.assertEqual(got.waiting_failed, 1)
		self.assertEqual(got.resume_turn, enqueue.call_args.kwargs["run_id"])
		self.assertEqual(got.resume_count, 1)

	def test_error_failed_and_cancelled_each_send_the_explanation(self):
		statuses = ["Error", "Failed"]
		if "Cancelled" in (frappe.get_meta("Prepared Report").get_field("status").options or ""):
			statuses.append("Cancelled")
		for status in statuses:
			with self.subTest(status=status):
				run, conv, _ = self._parked(tag=status.lower())
				self.run, self.conv = run, conv
				self._set_pr(status=status, error_message="Report timed out.")
				enqueue = self._sending()
				self._resume()
				self._assert_explained(enqueue, "It took too long to generate and was stopped.")
				self.assertNotIn("is ready", enqueue.call_args.args[1])

	def test_a_status_with_no_reason_still_says_what_happened(self):
		self._set_pr(status="Cancelled" if False else "Failed", error_message="")
		enqueue = self._sending()
		self._resume()
		self._assert_explained(enqueue, "the report failed")

	def test_a_deleted_report_is_a_removed_report(self):
		frappe.db.delete("Prepared Report", {"name": self.pr})
		frappe.db.commit()
		enqueue = self._sending()
		self._resume()
		self._assert_explained(enqueue, "the report was removed")

	def test_a_failure_beats_a_report_still_generating(self):
		run, conv, _ = self._parked(reports=((PREP_REPORT, _ACME), (REPORT_B, _GLOBEX)), tag="two")
		self.run, self.conv = run, conv
		self._set_pr(self.prs[1], status="Error", error_message="Report timed out.")
		enqueue = self._sending()
		self._resume()
		self.assertIn(f"{REPORT_B} could not be produced", enqueue.call_args.args[1])

	def test_the_reason_is_one_line_with_no_backtick_to_close_the_span(self):
		self._set_pr(status="Error", error_message=_BAD_ERROR)
		enqueue = self._sending()
		self._resume()
		prompt = enqueue.call_args.args[1]
		self.assertIn("`Set the 'Company' filter before running this", prompt)
		self.assertEqual(prompt.count("`"), 2)
		self.assertNotIn("\n", prompt)

	def test_the_detail_is_capped(self):
		out = macro_report_wait._failed_prompt({"report": "R", "detail": "x`y\n" * 400})
		span = out.split("`")[1]
		self.assertEqual(out.count("`"), 2)
		self.assertLessEqual(len(span), 300)
		self.assertNotIn("\n", out)

	def test_the_chat_is_disarmed_before_the_explanation_goes(self):
		run, conv, _ = self._parked(armed=True, tag="armed")
		self.run, self.conv = run, conv
		self.assertTrue(frappe.db.get_value(CONV, conv, "skip_confirmation"))
		self._set_pr(status="Error", error_message="Report timed out.")
		armed_at_send = []

		def enqueue(conversation, prompt, **kw):
			armed_at_send.append(frappe.db.get_value(CONV, conversation, "skip_confirmation"))
			self._turn_as(conversation, kw["run_id"], state="queued")
			return {"run_id": kw["run_id"], "message_id": None}

		with patch(_ENQUEUE_TURN, side_effect=enqueue):
			self._resume()
		self.assertEqual(armed_at_send, [0])
		self.assertEqual(self._row().waiting_failed, 1)

	def test_a_card_that_outlived_the_sweep_ends_the_run_with_no_model_turn(self):
		run, conv, _ = self._parked(armed=True, tag="card")
		self.run, self.conv = run, conv
		self._set_pr(status="Error", error_message="Report timed out.")
		enqueue = self._sending()
		with (
			patch("jarvis.chat.pending_confirm.has_live_card", return_value=True),
			patch("frappe.log_error"),
		):
			self._resume()
		enqueue.assert_not_called()
		got = self._row()
		self.assertEqual(got.status, "failed")
		self.assertEqual(
			got.error, f"Step 1 stopped: {PREP_REPORT} failed: It took too long to generate and was stopped."
		)
		self.assertEqual(got.waiting_failed, 0)
		self.assertTrue(frappe.db.get_value(CONV, conv, "skip_confirmation"))  # fail closed

	def test_a_failed_explanation_that_did_not_go_is_put_back_not_failed_open(self):
		self._set_pr(status="Error", error_message="Report timed out.")
		self._sending(raises=RuntimeError("redis down"))
		with patch("frappe.log_error"):
			self._resume()
		got = self._row()
		self.assertEqual((got.status, got.waiting_failed, got.resume_compensations), ("waiting_report", 0, 1))


class TestTheWaitsDeadline(ResumeBase):
	def _wait_since(self, minutes):
		self._set_wait(add_to_date(now_datetime(), minutes=-minutes))

	def _set_wait(self, when):
		frappe.db.set_value(RUN, self.run, "waiting_since", when, update_modified=False)
		frappe.db.commit()

	def test_a_report_inside_its_deadline_is_waited_for(self):
		# Frappe's 25 minutes + the stall margin + the hour of queue grace.
		self._wait_since(89)
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row().status, "waiting_report")

	def test_a_report_past_its_deadline_failed_to_finish_in_time(self):
		self._wait_since(91)
		enqueue = self._sending()
		self._resume()
		self.assertIn("the report did not finish in time", enqueue.call_args.args[1])
		self.assertEqual(self._row().waiting_failed, 1)

	def _report_timeout(self, seconds):
		old = frappe.db.get_value("Report", PREP_REPORT, "timeout")
		frappe.db.set_value("Report", PREP_REPORT, "timeout", seconds, update_modified=False)
		frappe.db.commit()
		self.addCleanup(
			lambda: (
				frappe.db.set_value("Report", PREP_REPORT, "timeout", old, update_modified=False),
				frappe.db.commit(),
			)
		)

	def test_the_deadline_follows_the_reports_own_timeout(self):
		self._report_timeout(3 * 3600)  # + 5 minutes of stall margin + the hour of queue grace
		self._wait_since(244)
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		self._wait_since(246)
		self._resume()
		self.assertIn("did not finish in time", enqueue.call_args.args[1])

	def test_nothing_waits_past_six_hours(self):
		self._report_timeout(10 * 3600)
		self._wait_since(6 * 60 + 1)
		enqueue = self._sending()
		self._resume()
		self.assertIn("did not finish in time", enqueue.call_args.args[1])

	def test_an_expired_wait_leaves_one_error_log(self):
		frappe.db.delete("Error Log", {"method": macro_report_wait.LOG_EXPIRED})
		self._wait_since(120)
		self._sending(raises=RuntimeError("redis down"))
		with patch("frappe.log_error", wraps=frappe.log_error) as log:
			self._resume()
			self._resume()
		expired = [c for c in log.call_args_list if c.kwargs.get("title") == macro_report_wait.LOG_EXPIRED]
		self.assertEqual(len(expired), 1)
		self.assertIn(self.run, expired[0].kwargs["message"])
		frappe.db.delete("Error Log", {"method": ["like", "Jarvis: macro report%"]})
		frappe.db.commit()


class TestTheCaps(ResumeBase):
	def test_a_step_that_keeps_asking_to_wait_ends_the_run(self):
		frappe.db.set_value(RUN, self.run, "step_resumes", 3)
		frappe.db.commit()
		self._ready()
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		got = self._row()
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, "Step 1 kept waiting on reports, so the run was stopped.")

	def test_a_run_that_keeps_waiting_ends_too(self):
		frappe.db.set_value(RUN, self.run, "resume_count", 5)
		frappe.db.commit()
		self._ready()
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row().status, "failed")

	def test_one_below_the_caps_still_resumes(self):
		frappe.db.set_value(RUN, self.run, {"step_resumes": 2, "resume_count": 4})
		frappe.db.commit()
		self._ready()
		enqueue = self._sending()
		self._resume()
		self.assertEqual(enqueue.call_count, 1)
		got = self._row()
		self.assertEqual((got.step_resumes, got.resume_count), (3, 5))

	def test_at_the_cap_a_failed_report_is_the_reason_given(self):
		frappe.db.set_value(RUN, self.run, "step_resumes", 3)
		frappe.db.commit()
		self._set_pr(status="Error", error_message="Report timed out.")
		enqueue = self._sending()
		self._resume()
		enqueue.assert_not_called()
		self.assertIn("failed: It took too long", self._row().error)


class TestTheSharedGuards(ResumeBase):
	def test_a_hold_ends_a_waiting_run_without_a_turn(self):
		self._ready()
		enqueue = self._sending()
		with patch("jarvis.chat.macros.is_held", return_value=True):
			self._resume()
		enqueue.assert_not_called()
		self.assertEqual(self._row().status, "stopped")

	def test_an_owner_who_may_no_longer_run_unattended_work_ends_the_run(self):
		self._ready()
		enqueue = self._sending()
		with patch("jarvis.permissions.is_valid_unattended_owner", return_value=False):
			self._resume()
		enqueue.assert_not_called()
		got = self._row()
		self.assertEqual((got.status, got.error), ("failed", macros._OWNER_INELIGIBLE_ERROR))

	def test_a_run_whose_macro_steps_vanished_is_incompatible_only_if_the_sent_step_did(self):
		# A run with no steps snapshot reads the live macro: the step it parked on is the
		# last, and that must not read as "the list shrank past the cursor".
		frappe.db.set_value(RUN, self.run, {"steps_snapshot": "", "current_step": 2})
		frappe.db.commit()
		self._ready()
		self._sending()
		self._resume()
		self.assertEqual(self._row().status, "running")


# --------------------------------------------------------------------------- #
# the wake
# --------------------------------------------------------------------------- #
class TestThePreparedReportHook(ResumeBase):
	def _doc(self, name=None, status="Completed", owner=OWNER):
		return frappe._dict(name=name or self.pr, status=status, owner=owner)

	def _hook(self, doc):
		with patch("frappe.enqueue") as enqueue:
			macro_report_wait.on_prepared_report_update(doc)
		return enqueue

	def test_a_finished_awaited_report_wakes_its_run_after_commit(self):
		for status in ("Completed", "Error", "Failed", "Cancelled"):
			with self.subTest(status=status):
				enqueue = self._hook(self._doc(status=status))
				enqueue.assert_called_once()
				self.assertEqual(
					enqueue.call_args.args, ("jarvis.chat.macro_report_wait.resume_report_waits",)
				)
				kw = enqueue.call_args.kwargs
				self.assertEqual(kw["run"], self.run)
				self.assertEqual(kw["queue"], "short")
				self.assertEqual(kw["job_id"], f"macro-report-wake-{self.run}")
				self.assertTrue(kw["deduplicate"])
				self.assertTrue(kw["enqueue_after_commit"])

	def test_a_report_still_running_wakes_nobody(self):
		for status in ("Queued", "Started"):
			self._hook(self._doc(status=status)).assert_not_called()

	def test_a_report_no_run_awaits_wakes_nobody(self):
		self._hook(self._doc(name="PR-unrelated")).assert_not_called()

	def test_another_users_report_of_the_same_name_wakes_nobody(self):
		self._hook(self._doc(owner="Administrator")).assert_not_called()

	def test_a_name_that_is_only_inside_another_name_wakes_nobody(self):
		self._hook(self._doc(name=self.pr[:-1])).assert_not_called()

	def test_a_run_that_is_not_waiting_is_not_woken(self):
		frappe.db.set_value(RUN, self.run, "status", "stopped")
		frappe.db.commit()
		self._hook(self._doc()).assert_not_called()

	def test_it_never_raises(self):
		with patch("frappe.get_all", side_effect=RuntimeError("db gone")), patch("frappe.log_error") as log:
			macro_report_wait.on_prepared_report_update(self._doc())
		self.assertEqual(log.call_args.kwargs["title"], "jarvis macro report wake failed")
		with patch("frappe.enqueue", side_effect=RuntimeError("redis")), patch("frappe.log_error"):
			macro_report_wait.on_prepared_report_update(self._doc())

	def test_the_hooks_file_registers_it_beside_the_export_handler_and_the_cron(self):
		handlers = frappe.get_hooks("doc_events")["Prepared Report"]["on_update"]
		self.assertIn("jarvis.chat.macro_report_wait.on_prepared_report_update", handlers)
		self.assertIn("jarvis.tools._export.jobs.on_prepared_report_update", handlers)
		self.assertIn(
			"jarvis.chat.macro_report_wait.resume_report_waits",
			frappe.get_hooks("scheduler_events")["cron"]["*/5 * * * *"],
		)

	def test_a_park_asks_for_a_wake_so_a_report_that_finished_mid_turn_resumes_at_once(self):
		self.wake.assert_called_with(self.run)

	def test_wake_queues_one_pending_job_per_run_and_never_raises(self):
		self._wake_patch.stop()
		with patch("frappe.enqueue") as enqueue:
			macro_report_wait.wake(self.run)
		kw = enqueue.call_args.kwargs
		self.assertEqual((kw["run"], kw["queue"], kw["deduplicate"]), (self.run, "short", True))
		self.assertEqual(kw["job_id"], f"macro-report-wake-{self.run}")
		with patch("frappe.enqueue", side_effect=RuntimeError("redis")), patch("frappe.log_error") as log:
			macro_report_wait.wake(self.run)
		self.assertEqual(log.call_args.kwargs["title"], "jarvis macro report wake failed")


class TestTheCronNeverRaises(ResumeBase):
	def test_one_runs_fault_does_not_stop_the_others(self):
		run2, conv2, _ = self._parked(tag="second")
		self._ready()
		self.pr2 = self.pr
		calls = []

		def one(name, wait=0.0):
			calls.append(name)
			if len(calls) == 1:
				raise RuntimeError("boom")

		with (
			patch.object(macro_report_wait, "_resume_one", side_effect=one),
			patch("frappe.log_error") as log,
		):
			self._resume()
		self.assertEqual(len(calls), 2)
		self.assertIn("jarvis macro report resume failed", log.call_args.kwargs["title"])

	def test_a_run_that_keeps_failing_is_logged_once_an_hour(self):
		title = f"jarvis macro report resume failed: {self.run}"
		frappe.db.delete("Error Log", {"method": title})
		with patch.object(macro_report_wait, "_resume_one", side_effect=RuntimeError("boom")):
			for _ in range(3):
				self._resume(self.run)
		self.assertEqual(frappe.db.count("Error Log", {"method": title}), 1)
		frappe.db.delete("Error Log", {"method": title})
		frappe.db.commit()

	def test_an_unreadable_list_is_logged_not_raised(self):
		with patch("frappe.get_all", side_effect=RuntimeError("db gone")), patch("frappe.log_error"):
			macro_report_wait.resume_report_waits()


class RealResumeBase(IdentityBase):
	"""A run parked on a Completed report, on the pump shard of ``IdentityBase``: the
	resume goes through the real ``_enqueue_turn`` and accept gate, not a stand-in."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		_ensure_report(PREP_REPORT, prepared=1)
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		self.addCleanup(
			lambda: (frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT}), frappe.db.commit())
		)
		self.run, self.conv, self.macro = self._mk_run(steps=2, at_step=1)
		with patch(_ENQUEUE):
			pr = frappe.get_doc(
				{"doctype": "Prepared Report", "report_name": PREP_REPORT, "filters": json.dumps(_ACME)}
			).insert(ignore_permissions=True)
		self.pr = pr.name
		frappe.db.set_value(
			"Prepared Report", pr.name, {"status": "Completed", "owner": OWNER}, update_modified=False
		)
		frappe.db.set_value(
			RUN,
			self.run,
			{
				"status": "waiting_report",
				"waiting_since": now_datetime(),
				"waited": 1,
				"waiting_on": json.dumps(
					[{"prepared_report": self.pr, "report_name": PREP_REPORT, "filters": _ACME}]
				),
			},
			update_modified=False,
		)
		frappe.db.commit()

	def _row(self):
		return frappe.db.get_value(
			RUN, self.run, ["status", "resume_turn", "resume_count", "step_resumes"], as_dict=True
		)


class TestTheRealEnqueueOnTheTurnMachine(RealResumeBase):
	"""The continuation through the real ``_enqueue_turn`` and accept gate, not a
	stand-in: the turn named in the CAS is the Turn that exists, with a hidden
	continuation message, and a second delivery meets it."""

	def test_the_continuation_is_the_turn_the_cas_named(self):
		macro_report_wait.resume_report_waits(self.run)
		got = self._row()
		self.assertEqual((got.status, got.resume_count, got.step_resumes), ("running", 1, 1))
		turn = frappe.db.get_value(
			TURN, got.resume_turn, ["conversation", "state", "seed_message", "turn_class"], as_dict=True
		)
		self.assertEqual(turn.conversation, self.conv)
		self.assertEqual(turn.turn_class, "background")
		seed = frappe.db.get_value(
			MSG, turn.seed_message, ["role", "origin", "content", "hidden"], as_dict=True
		)
		self.assertEqual((seed.role, seed.origin), ("user", "continuation"))
		self.assertTrue(seed.hidden)
		self.assertIn(f"{PREP_REPORT} (generated ", seed.content)
		self.assertIn(
			' is ready. Call run_report with exactly these filters and finish this step: `{"company": "Acme"}`',
			seed.content,
		)
		# A second look (the cron after the hook) finds a running run and sends nothing.
		macro_report_wait.resume_report_waits()
		self.assertEqual(self._names(self.conv), [got.resume_turn])

	def test_a_turn_that_already_exists_is_a_duplicate_that_went_out(self):
		macro_report_wait.resume_report_waits(self.run)
		first = self._row()
		# The run is parked again with the id unspent-looking: counters back, same id.
		frappe.db.set_value(
			RUN,
			self.run,
			{"status": "waiting_report", "resume_count": 0, "step_resumes": 0},
			update_modified=False,
		)
		frappe.db.commit()
		macro_report_wait.resume_report_waits(self.run)
		got = self._row()
		self.assertEqual((got.status, got.resume_turn, got.resume_count), ("running", first.resume_turn, 1))
		self.assertEqual(self._names(self.conv), [first.resume_turn])  # no second turn, no second message

	def test_a_full_queue_keeps_the_run_parked_on_the_real_gate(self):
		with patch.object(admission, "MAX_BACKGROUND_QUEUE_DEPTH", 0):
			macro_report_wait.resume_report_waits(self.run)
		got = self._row()
		self.assertEqual((got.status, got.resume_count), ("waiting_report", 0))
		self.assertEqual(self._names(self.conv), [])
		macro_report_wait.resume_report_waits(self.run)
		self.assertEqual(self._row().status, "running")


# --------------------------------------------------------------------------- #
# recognising and judging the continuation (T4)
# --------------------------------------------------------------------------- #
class ContBase(ResumeBase):
	"""A two-step run whose step 1 parked on a report, and whose report is now resolved
	(Completed, or failed when ``FAILS``) and the run RESUMED: the continuation is queued
	under ``self.cont`` (with its own seed message, origin ``continuation``, as the real
	gate writes it), the step's original turn is ``self.step_turn`` and has ended."""

	FAILS = False
	MERGED = False

	def setUp(self):
		super().setUp()
		self.step_turn = macros._step_turn_id(self.run, 0)
		self.orig = frappe.db.get_value(
			TURN, self.step_turn, ["seed_message", "assistant_message"], as_dict=True
		)
		if self.MERGED:
			frappe.db.set_value(RUN, self.run, {"run_mode": "merged", "total_steps": 1})
			frappe.db.commit()
		if self.FAILS:
			self._set_pr(status="Error", error_message="Report timed out.")
		else:
			for pr in self.prs:
				self._ready(pr)
		self._continuation_enqueue = self._sending_continuation()
		self._resume(self.run)
		self.cont = self._row().resume_turn
		self.assertEqual(self._row().status, "running")
		self.wake.reset_mock()

	def _sending_continuation(self):
		"""``api._enqueue_turn`` as the real gate leaves it: a queued Turn under the given
		id with a hidden ``continuation`` seed message."""

		def enqueue(conversation, prompt, **kw):
			seed = self._msg(conversation, "user", origin="continuation", content=prompt)
			self._turn_as(conversation, kw["run_id"], state="queued", seed=seed)
			return {"run_id": kw["run_id"], "message_id": seed}

		patcher = patch(_ENQUEUE_TURN, side_effect=enqueue)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _end_continuation(self, rows=(), *, state="done", error="", reply="Done.", ended_s_ago=0):
		"""The continuation ends: its ``run_report`` rows, its reply, its final state."""
		for row in rows:
			self._tool(self.conv, **row)
		reply_id = self._msg(self.conv, "assistant", content=reply)
		frappe.db.set_value(
			TURN,
			self.cont,
			{
				"state": state,
				"error": error,
				"assistant_message": reply_id,
				"done_at": add_to_date(now_datetime(), seconds=-ended_s_ago),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return reply_id

	def _ready_row(self, pr=None, report=PREP_REPORT, filters=None):
		return dict(status="ready", run=pr or self.pr, report=report, filters=filters or _ACME)

	def _hook(self, **handle):
		"""The turn-end hook for the named handle (default: the continuation's id), with
		the next step's dispatch recorded. Returns the dispatch mock."""
		with self._dispatching(self.conv) as enqueue:
			macros.advance_after_turn(
				self.conv, errored=handle.pop("errored", False), **(handle or {"run_id": self.cont})
			)
		frappe.set_user("Administrator")
		return enqueue

	def _doc(self):
		return frappe.get_doc(RUN, self.run)

	def _full(self):
		return frappe.db.get_value(
			RUN,
			self.run,
			[
				"status",
				"error",
				"current_step",
				"resume_turn",
				"resume_count",
				"step_resumes",
				"step_continuations",
				"waiting_on",
				"waiting_failed",
			],
			as_dict=True,
		)


class TestTheOriginalTurnsEndAfterAResume(ContBase):
	"""Once ``resume_turn`` is set it is the ONLY turn that ends the step. The original
	turn's rows still say ``waiting: true``, so taken for the step's end again it would
	park the run on a report that was read."""

	def _assert_ignored(self, **handle):
		before = self._full()
		enqueue = self._hook(**handle)
		self.assertEqual(self._full(), before)
		enqueue.assert_not_called()
		self.wake.assert_not_called()

	def test_a_redelivery_by_the_turns_id_changes_nothing(self):
		self._assert_ignored(run_id=self.step_turn)

	def test_a_redelivery_by_the_reply_alone_changes_nothing(self):
		# Turn recovery names only the assistant row.
		self._assert_ignored(assistant_message=self.orig.assistant_message)

	def test_a_redelivery_by_the_steps_message_alone_changes_nothing(self):
		# The legacy worker names only the message.
		self._assert_ignored(seed_message=self.orig.seed_message)

	def test_a_finalize_retry_with_the_id_and_the_message_changes_nothing(self):
		self._assert_ignored(run_id=self.step_turn, seed_message=self.orig.seed_message)
		self._assert_ignored(run_id=self.step_turn, errored=True)

	def test_a_hook_that_names_no_turn_is_not_the_end_of_a_resumed_step(self):
		self._assert_ignored(seed_message=None)

	def test_any_other_turn_of_a_resumed_step_counts_as_superseded(self):
		self.assertTrue(
			macros._superseded_by_a_retry(self.step_turn, self.orig.seed_message, run=self._doc())
		)
		self.assertFalse(macros._superseded_by_a_retry(self.cont, self.orig.seed_message, run=self._doc()))

	def test_the_continuations_own_end_by_the_reply_alone_is_accepted(self):
		reply = self._end_continuation([self._ready_row()])
		enqueue = self._hook(assistant_message=reply)
		self.assertEqual(self._full().current_step, 2)
		enqueue.assert_called_once()

	def test_the_step_turn_helper_is_the_continuation_once_one_is_set(self):
		self.assertEqual(macros._step_turn(self._doc()), self.cont)
		frappe.db.set_value(RUN, self.run, "resume_turn", "", update_modified=False)
		self.assertEqual(macros._step_turn(self._doc()), self.step_turn)


class TestTheContinuationEnds(ContBase):
	def test_a_ready_read_moves_the_run_on_and_clears_the_resume(self):
		self._end_continuation([self._ready_row()])
		enqueue = self._hook()
		enqueue.assert_called_once()
		got = self._full()
		self.assertEqual((got.status, got.current_step), ("running", 2))
		self.assertEqual((got.resume_turn or "", got.step_resumes), ("", 0))
		# kept: the outcome of the step is judged by it, and the counters of the run
		self.assertEqual(json.loads(got.step_continuations), {"0": self.cont})
		self.assertEqual(got.resume_count, 1)

	def test_the_next_steps_end_is_judged_by_its_own_turn_again(self):
		self._end_continuation([self._ready_row()])
		self._hook()
		self.assertEqual(macros._step_turn(self._doc()), macros._step_turn_id(self.run, 1))
		# and the continuation's own end, redelivered, no longer moves anything
		before = self._full()
		self._hook(run_id=self.cont)
		self.assertEqual(self._full(), before)

	def test_a_continuation_that_never_read_the_report_fails_the_run(self):
		self._end_continuation([])
		enqueue = self._hook()
		enqueue.assert_not_called()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(
			got.error, f"Step 1 stopped: it did not read the report {PREP_REPORT} after it was ready."
		)

	def test_a_ready_read_of_another_report_is_not_the_awaited_one(self):
		other = self._pr(REPORT_B, _ACME, "Completed")
		self._end_continuation([self._ready_row(other, REPORT_B)])
		self._hook()
		self.assertEqual(self._full().status, "failed")
		self.assertIn("did not read the report", self._full().error)

	def test_a_ready_read_with_other_filters_is_not_the_awaited_one(self):
		# A model that drifted from the filters it was given read something else.
		other = self._pr(PREP_REPORT, {"company": "Initech"}, "Completed")
		self._end_continuation([self._ready_row(other, filters={"company": "Initech"})])
		self._hook()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(
			got.error, f"Step 1 stopped: it did not read the report {PREP_REPORT} after it was ready."
		)

	def test_a_ready_read_of_the_awaited_prepared_report_passes_whatever_filters_it_was_asked_with(self):
		self._end_continuation([self._ready_row(self.pr, filters={"company": "Acme", "extra": ""})])
		self._hook()
		self.assertEqual(self._full().current_step, 2)

	def test_a_fresh_copy_of_the_same_report_counts_as_read(self):
		again = self._pr(PREP_REPORT, _ACME, "Completed")
		self._end_continuation([self._ready_row(again)])
		self._hook()
		self.assertEqual(self._full().current_step, 2)

	def test_a_failed_read_ends_the_run_with_the_reason(self):
		self._end_continuation([dict(status="failed", run=self.pr, error="Out of memory.")])
		enqueue = self._hook()
		enqueue.assert_not_called()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, f"Step 1 stopped: {PREP_REPORT} failed: Out of memory.")

	def test_a_failed_read_wins_over_a_ready_read_of_another_report(self):
		other = self._pr(REPORT_B, _ACME, "Completed")
		self._end_continuation(
			[
				self._ready_row(),
				dict(status="failed", run=other, report=REPORT_B, error="Nope."),
			]
		)
		self._hook()
		self.assertEqual(self._full().error, f"Step 1 stopped: {REPORT_B} failed: Nope.")

	def test_an_unreadable_continuation_is_not_taken_for_one_that_read_nothing(self):
		self._end_continuation([])
		with (
			patch.object(macro_report_wait, "_rows", side_effect=RuntimeError("db")),
			patch("frappe.log_error"),
		):
			self._hook()
		self.assertEqual(self._full().current_step, 2)  # as before the feature: it goes on

	def test_an_errored_continuation_takes_the_error_path_not_the_unread_one(self):
		self._end_continuation([], state="errored", error="boom")
		self._hook(errored=True)
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertTrue(got.error.startswith("Step 1 failed:"), got.error)

	def test_a_continuation_that_waits_again_parks_the_run_and_the_resume_path_is_reusable(self):
		more = self._pr(PREP_REPORT, {"company": "Globex"})
		self._end_continuation(
			[
				dict(
					status="started",
					run=more,
					report=PREP_REPORT,
					filters={"company": "Globex"},
					waiting=True,
				)
			]
		)
		enqueue = self._hook()
		enqueue.assert_not_called()
		got = self._full()
		self.assertEqual(got.status, "waiting_report")
		self.assertEqual([w["prepared_report"] for w in json.loads(got.waiting_on)], [more])
		self.wake.assert_called_once()
		# the report finishes: a second continuation, the next id of the run
		self._set_pr(more, status="Completed", report_end_time="2026-10-28 10:30:00")
		self._sending_continuation()
		self._resume(self.run)
		again = self._full()
		self.assertEqual(again.status, "running")
		self.assertEqual(again.resume_turn, hashlib.sha1(f"{self.run}:0:report:2".encode()).hexdigest()[:12])
		self.assertNotEqual(again.resume_turn, self.cont)
		self.assertEqual((again.resume_count, again.step_resumes), (2, 2))
		self.assertEqual(json.loads(again.step_continuations), {"0": again.resume_turn})
		# the first continuation's end, redelivered, is not the step's any more
		before = self._full()
		self._hook(run_id=self.cont)
		self.assertEqual(self._full(), before)
		# and the second one's ends the step
		self.cont = again.resume_turn
		self._end_continuation([self._ready_row(more, filters={"company": "Globex"})])
		self._hook()
		self.assertEqual(self._full().current_step, 2)
		self.assertEqual(self._full().step_resumes, 0)


class TestAFailedReportsExplanationEnds(ContBase):
	FAILS = True

	def _reason(self):
		return f"Step 1 stopped: {PREP_REPORT} failed: It took too long to generate and was stopped."

	def test_the_run_is_waiting_failed_with_its_explanation_turn_out(self):
		self.assertEqual(self._full().waiting_failed, 1)

	def test_a_clean_explanation_still_ends_the_run_failed_with_the_reports_reason(self):
		self._end_continuation([])
		enqueue = self._hook()
		enqueue.assert_not_called()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, self._reason())

	def test_a_card_the_explanation_left_does_not_change_it(self):
		self._park_card(self.conv)
		self._end_continuation([])
		self._hook()
		got = self._full()
		self.assertEqual((got.status, got.error), ("failed", self._reason()))

	def test_an_errored_explanation_gives_the_reports_reason_not_its_own(self):
		self._end_continuation([], state="errored", error="boom")
		self._hook(errored=True)
		self.assertEqual(self._full().error, self._reason())

	def test_an_explanation_that_read_the_report_is_still_a_failed_run(self):
		self._end_continuation([self._ready_row()])
		self._hook()
		self.assertEqual((self._full().status, self._full().error), ("failed", self._reason()))

	def test_an_explanation_that_asks_to_wait_never_parks(self):
		more = self._pr(PREP_REPORT, _ACME)
		self._end_continuation([dict(status="started", run=more, waiting=True)])
		self._hook()
		self.assertEqual((self._full().status, self._full().error), ("failed", self._reason()))
		self.wake.assert_not_called()

	def test_the_failure_is_recorded_with_the_flip(self):
		(wait,) = json.loads(self._full().waiting_on)
		self.assertEqual(wait["failure"]["report"], PREP_REPORT)
		self.assertEqual(wait["failure"]["detail"], "It took too long to generate and was stopped.")

	def test_the_reason_is_the_one_recorded_when_the_explanation_was_sent(self):
		# The report is deleted (or changed) in the minutes the explanation takes: the
		# owner is told what the model was told, not what the report has become.
		frappe.delete_doc("Prepared Report", self.pr, force=True, ignore_permissions=True)
		self._end_continuation([])
		self._hook()
		self.assertEqual(self._full().error, self._reason())

	def test_a_report_that_changed_meanwhile_does_not_change_the_reason(self):
		self._set_pr(error_message="Something else.")
		self._end_continuation([])
		self._hook()
		self.assertEqual(self._full().error, self._reason())

	def test_a_run_without_the_record_falls_back_to_reading_the_report(self):
		waits = json.loads(self._full().waiting_on)
		for w in waits:
			w.pop("failure", None)
		frappe.db.set_value(RUN, self.run, "waiting_on", json.dumps(waits), update_modified=False)
		frappe.db.commit()
		self._end_continuation([])
		self._hook()
		self.assertEqual(self._full().error, self._reason())

	def test_the_original_turn_does_not_end_it(self):
		before = self._full()
		self._hook(run_id=self.step_turn)
		self.assertEqual(self._full(), before)


class TestAResumedStepIsJudgedByItsContinuation(ContBase):
	def _finish_step_two(self, *, state="done"):
		"""The step the hook dispatched ends (the newest turn of the chat)."""
		newest = frappe.get_all(TURN, filters={"conversation": self.conv}, order_by="creation desc", limit=1)[
			0
		].name
		frappe.db.set_value(TURN, newest, "state", state, update_modified=False)
		frappe.db.commit()
		with self._dispatching(self.conv):
			macros.advance_after_turn(self.conv, errored=state != "done", run_id=newest)

	def _stop_on_error_off(self):
		macro = frappe.db.get_value(RUN, self.run, "macro")
		frappe.db.set_value("Jarvis Macro", macro, "stop_on_error", 0)
		frappe.db.commit()

	def test_a_continuation_that_errored_is_the_steps_failure_in_the_final_outcome(self):
		self._stop_on_error_off()
		self._end_continuation([], state="errored", error="boom")
		enqueue = self._hook(errored=True)  # stop on error is off: step 2 goes out
		# the macro's own choice decides: no "did not read the report" on top of the error
		enqueue.assert_called_once()
		self.assertEqual((self._full().status, self._full().current_step), ("running", 2))
		self._finish_step_two()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertTrue(got.error.startswith("Finished, but 1 of 2 steps failed. Step 1:"), got.error)

	def test_a_continuation_that_left_a_draft_is_reported_as_a_draft_the_next_step_closed(self):
		self._end_continuation([self._ready_row()], reply=f"Here is the draft.\n{outcome._DRAFT}")
		self._hook()
		self._finish_step_two()
		got = self._full()
		self.assertEqual(got.status, "completed")
		self.assertTrue(got.error.startswith("Step 1 drafted a record that was not created"), got.error)

	def test_the_original_turns_clean_end_does_not_hide_the_continuations_failure(self):
		# The original turn said "It is generating." and ended done. Judged by it, the
		# run would be a clean success.
		self.assertEqual(frappe.db.get_value(TURN, self.step_turn, "state"), "done")
		self._stop_on_error_off()
		self._end_continuation([], state="errored", error="boom")
		self._hook(errored=True)
		self._finish_step_two()
		self.assertEqual(self._full().status, "failed")

	def test_a_clean_continuation_is_a_clean_run(self):
		self._end_continuation([self._ready_row()])
		self._hook()
		self._finish_step_two()
		self.assertEqual((self._full().status, self._full().error or ""), ("completed", ""))

	def test_the_outcome_reads_the_continuation_while_it_is_the_resume_turn_and_after(self):
		self.assertEqual(macros._step_turns(self.conv, self._doc())[0].name, self.cont)
		# the original is what a caller with no run still gets
		self.assertEqual(macros._step_turns(self.conv)[0].name, self.step_turn)
		# recorded only: the same after the resume_turn is cleared
		frappe.db.set_value(RUN, self.run, "resume_turn", "", update_modified=False)
		self.assertEqual(macros._step_turns(self.conv, self._doc())[0].name, self.cont)


class TestTwoCompaniesOfOneReport(ContBase):
	"""Two waits of one report with different filters are two reads."""

	REPORTS = ((PREP_REPORT, _ACME), (PREP_REPORT, _GLOBEX))

	def test_one_read_does_not_satisfy_both(self):
		self._end_continuation([self._ready_row(self.prs[0], filters=_ACME)])
		self._hook()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(
			got.error, f"Step 1 stopped: it did not read the report {PREP_REPORT} after it was ready."
		)

	def test_the_other_company_alone_does_not_either(self):
		self._end_continuation([self._ready_row(self.prs[1], filters=_GLOBEX)])
		self._hook()
		self.assertEqual(self._full().status, "failed")

	def test_both_reads_advance_the_run(self):
		self._end_continuation(
			[self._ready_row(self.prs[0], filters=_ACME), self._ready_row(self.prs[1], filters=_GLOBEX)]
		)
		self._hook()
		self.assertEqual(self._full().current_step, 2)

	def test_a_fresh_copy_for_the_second_company_counts_for_that_company_only(self):
		again = self._pr(PREP_REPORT, _GLOBEX, "Completed")
		self._end_continuation(
			[self._ready_row(self.prs[0], filters=_ACME), self._ready_row(again, filters=_GLOBEX)]
		)
		self._hook()
		self.assertEqual(self._full().current_step, 2)


class TestCapacityResumeOfAResumedLastStep(ContBase):
	"""A run parked for capacity whose last step was resumed: that step ends with its
	continuation, not with the step's fixed id (which the redelivery rule now ignores)."""

	def _park_for_capacity(self, **values):
		self._end_continuation([self._ready_row()])
		if self.MERGED:  # a merged run resumes only while its summary is there
			frappe.db.set_value("Jarvis Macro", self.macro, "merged_prompt", "Do the steps.")
		frappe.db.set_value(
			RUN, self.run, {"status": "waiting_capacity", "total_steps": 1, **values}, update_modified=False
		)
		frappe.db.commit()
		frappe.set_user("Administrator")
		with self._dispatching(self.conv) as enqueue:
			macros.resume_waiting_capacity_runs()
		frappe.set_user("Administrator")
		return enqueue

	def test_the_continuation_ends_the_last_step(self):
		enqueue = self._park_for_capacity()
		enqueue.assert_not_called()
		self.assertEqual(self._full().status, "completed", self._full().error)

	def test_the_step_end_is_delivered_by_the_continuations_turn(self):
		frappe.db.set_value(TURN, self.cont, "state", "streaming", update_modified=False)
		self.assertFalse(macros._step_end_delivered(self._doc(), 0))  # the original ended; this has not
		frappe.db.set_value(TURN, self.cont, "state", "done", update_modified=False)
		self.assertTrue(macros._step_end_delivered(self._doc(), 0))

	def test_the_continuation_is_the_turn_applied_never_the_steps_fixed_id(self):
		with patch.object(macros, "_apply_step_end", wraps=macros._apply_step_end) as apply:
			self._park_for_capacity()
		self.assertEqual([c.kwargs["run_id"] for c in apply.call_args_list], [self.cont])


class TestCapacityResumeOfAResumedSummarizedRun(TestCapacityResumeOfAResumedLastStep):
	MERGED = True


class TestAResumedSummarizedRun(ContBase):
	MERGED = True

	def test_the_continuations_end_ends_the_run(self):
		self._end_continuation([self._ready_row()])
		enqueue = self._hook()
		enqueue.assert_not_called()
		self.assertEqual((self._full().status, self._full().error or ""), ("completed", ""))

	def test_the_original_turn_does_not_end_it(self):
		before = self._full()
		self._hook(run_id=self.step_turn)
		self.assertEqual(self._full(), before)

	def test_a_continuation_that_never_read_the_report_fails_it_by_name(self):
		self._end_continuation([])
		self._hook()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertTrue(got.error.startswith("The summarized run stopped: it did not read"), got.error)

	def test_a_continuation_that_errored_is_the_runs_failure(self):
		self._end_continuation([], state="errored", error="boom")
		self._hook(errored=True)
		self.assertEqual(self._full().status, "failed")


class TestTheFiveMinuteCheckOnAResumedStep(ContBase):
	"""``macro_reconcile`` judges a resumed step by its continuation alone."""

	def setUp(self):
		super().setUp()
		for target in (
			patch.object(macro_reconcile, "_shard_is_on_the_pump", return_value=True),
			patch.object(macro_reconcile, "_may_run_unattended", return_value=True),
			patch.object(macro_reconcile, "_send_gate_refusal", return_value=None),
		):
			target.start()
			self.addCleanup(target.stop)
		real = macro_reconcile._candidates
		patcher = patch.object(
			macro_reconcile,
			"_candidates",
			lambda: [row for row in real() if row.name == self.run],
		)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.applied = []
		real_apply = macros._apply_step_end

		def spy(*args, **kw):
			self.applied.append(kw.get("run_id"))
			return real_apply(*args, **kw)

		patcher = patch.object(macros, "_apply_step_end", side_effect=spy)
		patcher.start()
		self.addCleanup(patcher.stop)
		# the step's original turn ended long ago and is judged by nobody
		self._age_turn(self.step_turn, 45 * 60)

	def _age_turn(self, turn, seconds):
		frappe.db.set_value(
			TURN, turn, "done_at", add_to_date(now_datetime(), seconds=-seconds), update_modified=False
		)
		frappe.db.commit()

	def _idle(self, seconds=15 * 60):
		frappe.db.sql(
			f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s",
			(add_to_date(now_datetime(), seconds=-seconds), self.run),
		)
		frappe.db.commit()

	def _tick(self):
		frappe.set_user("Administrator")
		with self._dispatching(self.conv) as enqueue:
			summary = macro_reconcile.reconcile_running_runs()
		frappe.set_user("Administrator")
		return summary, enqueue

	def test_a_queued_continuation_keeps_the_run_running_however_old_the_original_turn_is(self):
		self._idle()
		summary, enqueue = self._tick()
		self.assertEqual(summary["candidates"], 1)
		self.assertEqual(self.applied, [])
		enqueue.assert_not_called()
		self.assertEqual(self._full().status, "running")
		self.assertEqual(self._full().resume_turn, self.cont)

	def test_a_running_continuation_keeps_the_run_running_too(self):
		frappe.db.set_value(TURN, self.cont, "state", "streaming", update_modified=False)
		self._idle()
		self._tick()
		self.assertEqual(self.applied, [])
		self.assertEqual(self._full().status, "running")

	def test_a_continuation_whose_end_never_reached_the_hook_is_applied_by_its_id(self):
		self._end_continuation([self._ready_row()], ended_s_ago=5 * 60)
		self._idle()
		_summary, enqueue = self._tick()
		# the engine's own body, for the continuation, never the step's fixed id
		self.assertEqual(self.applied, [self.cont])
		self.assertNotIn(self.step_turn, self.applied)
		self.assertNotIn(macros._step_turn_id(self.run, 0), self.applied)
		enqueue.assert_called_once()  # step 2
		got = self._full()
		self.assertEqual((got.status, got.current_step, got.resume_turn or ""), ("running", 2, ""))

	def test_the_age_limit_is_measured_from_the_continuations_end(self):
		# ended 30 minutes ago: past CONTINUE_WITHIN_S, so the run ends instead of sending
		# step 2 (measured from the ORIGINAL turn, 45 minutes ago, it would be the same;
		# the next test pins the other direction)
		self._end_continuation([self._ready_row()], ended_s_ago=30 * 60)
		self._idle()
		_summary, enqueue = self._tick()
		enqueue.assert_not_called()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertTrue(got.error.startswith("The run was interrupted"), got.error)

	def test_an_old_original_turn_does_not_make_a_fresh_continuation_too_late(self):
		self._end_continuation([self._ready_row()], ended_s_ago=5 * 60)
		self._age_turn(self.step_turn, 3 * 60 * 60)
		self._idle()
		_summary, enqueue = self._tick()
		enqueue.assert_called_once()
		self.assertEqual(self._full().current_step, 2)

	def test_a_continuation_that_never_read_its_report_ends_failed_through_the_check(self):
		self._end_continuation([], ended_s_ago=5 * 60)
		self._idle()
		self._tick()
		self.assertEqual(self._full().status, "failed")
		self.assertIn("did not read the report", self._full().error)

	def test_a_stored_continuation_with_no_turn_after_ten_minutes_parks_the_run_again(self):
		frappe.db.delete(TURN, {"name": self.cont})
		before = self._full()
		self._idle(15 * 60)
		summary, _ = self._tick()
		self.assertEqual(summary["acted"], 1)
		self.assertEqual(self.applied, [])
		got = self._full()
		self.assertEqual(got.status, "waiting_report")
		# the id and the counters stay: the retry reuses the id and counts what goes out
		self.assertEqual(
			(got.resume_turn, got.resume_count, got.step_resumes),
			(before.resume_turn, before.resume_count, before.step_resumes),
		)
		self.assertEqual(json.loads(got.waiting_on), json.loads(before.waiting_on))
		self.wake.assert_called_once_with(self.run)
		# the next wake sends it again, under the same id
		enqueue = self._sending_continuation()
		self._resume(self.run)
		self.assertEqual(enqueue.call_args.kwargs["run_id"], self.cont)
		self.assertEqual(self._full().status, "running")

	def test_a_continuation_that_never_goes_out_is_given_up_on_after_three_tries(self):
		# The process dies between the flip and the enqueue, again and again: the flip
		# commits, no Turn is written, nothing counts it as sent (``_went_out``).
		frappe.db.delete(TURN, {"name": self.cont})

		def never_sent(conversation, prompt, **kw):
			return {"run_id": kw["run_id"], "message_id": None}

		with patch(_ENQUEUE_TURN, side_effect=never_sent), patch.object(macro_report_wait, "_went_out"):
			for tries in (1, 2, 3):
				self._idle(15 * 60)
				self._tick()
				got = self._full()
				self.assertEqual(got.status, "waiting_report")
				self.assertEqual(self._row().resume_compensations, tries)
				self._resume(self.run)
				self.assertEqual(self._full().status, "running")  # flipped, nothing sent
			self._idle(15 * 60)
			self._tick()
		got = self._full()
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, "Step 1 could not be continued after its report was ready.")

	def test_a_stored_continuation_with_no_turn_is_left_alone_before_ten_minutes(self):
		frappe.db.delete(TURN, {"name": self.cont})
		self._idle(5 * 60)
		summary, _ = self._tick()
		self.assertEqual(summary["acted"], 0)
		self.assertEqual(self._full().status, "running")

	def test_a_continuation_turn_of_another_chat_is_not_ours(self):
		frappe.db.delete(TURN, {"name": self.cont})
		_, other_conv, _ = self._mk_run(steps=2, at_step=1, tag="elsewhere")
		self._turn_as(other_conv, self.cont, state="done")
		self._idle(15 * 60)
		self._tick()
		self.assertEqual(self._full().status, "waiting_report")

	def test_the_stuck_runs_report_names_a_continuation_that_never_went_out(self):
		frappe.db.delete(TURN, {"name": self.cont})
		self._idle(30 * 60)
		rows = [r for r in macro_reconcile.stuck_runs() if r["run"] == self.run]
		self.assertEqual([r["why"] for r in rows], ["continuation_not_sent"])


class TestStopAndHoldCancelAQueuedContinuation(RealResumeBase):
	def _resumed(self):
		macro_report_wait.resume_report_waits(self.run)
		turn = self._row().resume_turn
		self.assertEqual(frappe.db.get_value(TURN, turn, "state"), "queued")
		return turn

	def test_stop_cancels_the_queued_continuation(self):
		turn = self._resumed()
		self.assertTrue(macros._stop_run(self.run, reason="Stopped.", by="Administrator"))
		self.assertEqual(frappe.db.get_value(TURN, turn, "state"), "cancelled")
		self.assertEqual(frappe.db.get_value(RUN, self.run, "status"), "stopped")

	def test_a_hold_or_delete_cancels_it_too(self):
		turn = self._resumed()
		self.assertEqual(macros.stop_runs_of_macro(self.macro, reason="Put on hold.", by="Administrator"), 1)
		self.assertEqual(frappe.db.get_value(TURN, turn, "state"), "cancelled")

	def test_a_continuation_already_handed_to_the_agent_is_left_to_finish(self):
		turn = self._resumed()
		frappe.db.set_value(TURN, turn, "state", "streaming", update_modified=False)
		frappe.db.commit()
		macros._stop_run(self.run, reason="Stopped.", by="Administrator")
		self.assertEqual(frappe.db.get_value(TURN, turn, "state"), "streaming")

	def test_stopping_a_parked_run_cancels_nothing(self):
		self.assertTrue(macros._stop_run(self.run, reason="Stopped.", by="Administrator"))
		self.assertEqual(frappe.db.get_value(RUN, self.run, "status"), "stopped")


class TestTheStaleSweepBeltForAParkedRun(ResumeBase):
	def _age(self, seconds):
		frappe.db.sql(
			f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s",
			(add_to_date(now_datetime(), seconds=-seconds), self.run),
		)
		frappe.db.commit()

	def _sweep(self):
		frappe.set_user("Administrator")
		with patch.object(macros, "_stale_run_candidates", return_value=[self.run]):
			return macros.reap_stale_macro_runs()

	def test_a_run_parked_past_the_longest_wait_and_an_hour_is_a_candidate(self):
		self._age(macro_report_wait.STALE_WAIT_AFTER_S + 600)
		cutoff = add_to_date(now_datetime(), seconds=-macros.STALE_RUN_AFTER_SECONDS)
		self.assertIn(self.run, macros._stale_run_candidates(cutoff))

	def test_a_run_inside_that_window_is_not(self):
		# older than the sweep's own three hours, younger than seven
		self._age(macros.STALE_RUN_AFTER_SECONDS + 3600)
		cutoff = add_to_date(now_datetime(), seconds=-macros.STALE_RUN_AFTER_SECONDS)
		self.assertNotIn(self.run, macros._stale_run_candidates(cutoff))

	def test_the_window_is_the_waits_cap_plus_an_hour(self):
		self.assertEqual(macro_report_wait.STALE_WAIT_AFTER_S, 7 * 3600)

	def test_the_sweep_ends_it_failed_through_a_compare_and_set_expecting_waiting_report(self):
		self._age(macro_report_wait.STALE_WAIT_AFTER_S + 600)
		with patch.object(macros, "_cas_run_status", wraps=macros._cas_run_status) as cas:
			self.assertEqual(self._sweep(), 1)
		self.assertEqual(cas.call_args.args[:3], (self.run, "waiting_report", "failed"))
		got = frappe.db.get_value(RUN, self.run, ["status", "error"], as_dict=True)
		self.assertEqual(got.status, "failed")
		self.assertEqual(got.error, macros._STALE_RUN_ERROR)

	def test_a_parked_run_inside_the_window_is_left_alone(self):
		self._age(macros.STALE_RUN_AFTER_SECONDS * 2)
		self.assertEqual(self._sweep(), 0)
		self.assertEqual(frappe.db.get_value(RUN, self.run, "status"), "waiting_report")

	def test_a_run_that_resumed_after_the_scan_is_left_alone(self):
		# listed while parked and old; by the re-read under the lock it is running again
		self._age(macro_report_wait.STALE_WAIT_AFTER_S + 600)
		frappe.db.set_value(RUN, self.run, "status", "running")
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s", (now_datetime(), self.run))
		frappe.db.commit()
		self.assertEqual(self._sweep(), 0)
		self.assertEqual(frappe.db.get_value(RUN, self.run, "status"), "running")

	def test_a_run_parked_again_after_the_scan_is_left_alone(self):
		self._age(macro_report_wait.STALE_WAIT_AFTER_S + 600)
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified=%s WHERE name=%s", (now_datetime(), self.run))
		frappe.db.commit()
		self.assertEqual(self._sweep(), 0)
		self.assertEqual(frappe.db.get_value(RUN, self.run, "status"), "waiting_report")
