"""Macro runs and prepared reports: the gate (``jarvis.chat.macro_report_wait``).

``run_report`` serves a prepared report's newest copy of any age. Inside a macro run
the step must read a copy made at/after the run started, so it asks this module
whether the calling turn IS that run's step (``macro_context``). Everything else (a
typed follow-up in the run's chat, another user, the kill switch, a bench without the
turn machine) must get today's answer, byte for byte.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import macro_report_wait, macros, macros_api, report_runs
from jarvis.tests import test_macro_run_outcome as outcome
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

	def test_a_failed_read_in_a_continuation_does_not_end_the_run_directly(self):
		run, conv, _ = self._mk_run(steps=2, at_step=1)
		frappe.db.set_value(RUN, run, "resume_turn", "resume000001", update_modified=False)
		bad = self._pr(PREP_REPORT, _ACME, "Error")
		step_turn = self._end_step(run, conv, [dict(status="failed", run=bad, error="Nope.")])
		self._advance(conv, step_turn)
		self.assertNotEqual(self._values(run).error, f"Step 1 stopped: {PREP_REPORT} failed: Nope.")

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
