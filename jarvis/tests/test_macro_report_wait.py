"""Macro runs and prepared reports: the gate (``jarvis.chat.macro_report_wait``).

``run_report`` serves a prepared report's newest copy of any age. Inside a macro run
the step must read a copy made at/after the run started, so it asks this module
whether the calling turn IS that run's step (``macro_context``). Everything else (a
typed follow-up in the run's chat, another user, the kill switch, a bench without the
turn machine) must get today's answer, byte for byte.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import macro_report_wait, macros
from jarvis.tests import test_macro_run_outcome as outcome
from jarvis.tests.test_run_report import PREP_REPORT, _completed, _ensure_report
from jarvis.tools import _agent_run_ctx
from jarvis.tools.run_report import run_report

CONV = "Jarvis Conversation"
RUN = "Jarvis Macro Run"
TURN = "Jarvis Chat Turn"
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
