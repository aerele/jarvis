"""A macro run keeps the steps it started with.

From the 2026-10-01 macros review: every step of a run was read from the live macro
when it was sent, so an edit while a run was in flight changed that run part-way: an
edited prompt, a step added, removed or moved, "Stop on error" turned over, a new
model override or a new summary all applied from the next STEP. A run parked for
capacity whose macro had shrunk was ended outright ("the macro was edited while this
run was waiting"). Now ``run_macro`` writes a copy of what the run runs on the run
row (``steps_snapshot``), in the transaction that inserts the row, the engine reads
that copy while the run is live, and every write that ends a run clears it. A run
started before the copy existed has none and reads the macro, as before.

The runs here are started by the real ``run_macro`` and their steps sent through
the real dispatch (``test_macro_dispatch_identity``'s shard of its own).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.client import set_value

from jarvis.chat import api as chat_api
from jarvis.chat import macros, macros_admin_api, macros_api
from jarvis.tests import test_macro_dispatch_identity as identity
from jarvis.tests.test_macro_dispatch_identity import step_turn_id

MACRO = identity.MACRO
TURN = identity.TURN
RUN = identity.RUN
OWNER = identity.OWNER
FIELD = "steps_snapshot"
_FULL = {"ok": False, "overloaded": True, "reason": "busy"}


class SnapshotBase(identity._Starting, identity.IdentityBase):
	def setUp(self):
		super().setUp()
		self.assertTrue(
			frappe.db.has_column(RUN, FIELD), "premise: the column exists (it needs `bench migrate`)"
		)

	# --- fixtures -------------------------------------------------------------- #

	def _start(self, *, steps=3, tag="snap", stop_on_error=1, step_fields=None, merged=""):
		"""A macro and a run of it started by ``run_macro`` (its first step sent).
		``step_fields``: extra fields per step, by index. ``merged``: a summary in
		place, so the run is a summarized one."""
		macro = self._mk_macro(steps=steps, tag=tag, stop_on_error=stop_on_error)
		if step_fields or merged:
			frappe.set_user(OWNER)
			doc = frappe.get_doc(MACRO, macro)
			for index, fields in (step_fields or {}).items():
				doc.steps[index].update(fields)
			doc.save(ignore_permissions=True)
			if merged:
				frappe.db.set_value(
					MACRO, macro, {"merged_prompt": merged, "merge_status": "ready"}, update_modified=False
				)
			frappe.db.commit()
		with self._starting():
			out = macros.run_macro(macro)
		frappe.set_user("Administrator")
		return out["data"]["macro_run"], out["data"]["conversation"], macro

	def _start_parked(self, **kw):
		"""As ``_start``, but the site was full: the run is parked before its first step."""
		with patch.object(macros, "_send_step", return_value=_FULL):
			run, conv, macro = self._start(**kw)
		self.assertEqual(self._run(run).status, "waiting_capacity", "premise: the run parked")
		return run, conv, macro

	def _edit(self, macro, change):
		"""The owner saves the macro with ``change(doc)`` applied."""
		frappe.set_user(OWNER)
		doc = frappe.get_doc(MACRO, macro)
		change(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user("Administrator")

	def _end_step(self, conv, run, index, *, state="done"):
		"""Step ``index`` (0-based) ends and its end reaches the engine."""
		self._finish(conv, step_turn_id(run, index), state=state)
		frappe.set_user("Administrator")

	def _resume(self):
		with self._starting():  # a run parked before its first step has no gateway session yet
			macros.resume_waiting_capacity_runs()
		frappe.set_user("Administrator")

	def _snapshot(self, run):
		return frappe.db.get_value(RUN, run, FIELD)

	def _skill(self):
		skill = frappe.get_doc(
			{
				"doctype": "Jarvis Custom Skill",
				"skill_name": f"snap-{frappe.generate_hash(length=6)}",
				"description": "a skill to tag",
				"instructions": "Do the thing.",
			}
		)
		skill.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Custom Skill", skill.name, force=True, ignore_permissions=True)
		)
		return skill

	def _forget_the_snapshot(self, run):
		"""The run as one started before this change left it: no snapshot."""
		frappe.db.set_value(RUN, run, FIELD, None, update_modified=False)
		frappe.db.commit()

	@contextmanager
	def _sent(self):
		"""Every ``_enqueue_turn`` call's keywords, the real dispatch still running."""
		with patch.object(chat_api, "_enqueue_turn", wraps=chat_api._enqueue_turn) as spy:
			yield spy

	def _assert_ran(self, run, conv, prompts, status="completed"):
		self.assertEqual(self._prompts(conv), prompts)
		row = self._run(run)
		self.assertEqual((row.status, row.error or ""), (status, ""))


# --------------------------------------------------------------------------- #
# What the snapshot holds
# --------------------------------------------------------------------------- #
class TestTheSnapshot(SnapshotBase):
	def test_it_is_written_with_the_run_row_and_holds_what_the_dispatch_reads(self):
		run, _, _ = self._start(
			steps=2,
			stop_on_error=0,
			step_fields={
				0: {"label": "First", "model_override": "m-1", "thinking_override": "high"},
				1: {"skills": json.dumps(["skill-a"])},
			},
		)
		snapshot = json.loads(self._snapshot(run))
		# An unset field is stored as the row holds it ('' or None, by Frappe version).
		for step in snapshot["steps"]:
			step.update({k: v or None for k, v in step.items()})
		self.assertEqual(
			snapshot,
			{
				"stop_on_error": 0,
				"merged_prompt": "",
				"steps": [
					{
						"label": "First",
						"prompt": "step 1",
						"skills": None,
						"model_override": "m-1",
						"thinking_override": "high",
					},
					{
						"label": None,
						"prompt": "step 2",
						"skills": json.dumps(["skill-a"]),
						"model_override": None,
						"thinking_override": None,
					},
				],
			},
		)

	def test_a_summarized_run_keeps_the_summary_it_sends(self):
		run, _, _ = self._start(steps=3, merged="  do all three  ")
		snapshot = json.loads(self._snapshot(run))
		self.assertEqual(snapshot["merged_prompt"], "do all three")
		self.assertEqual(len(snapshot["steps"]), 3)
		self.assertEqual(frappe.db.get_value(RUN, run, "run_mode"), "merged")


# --------------------------------------------------------------------------- #
# An edit while the run is in flight changes nothing in it
# --------------------------------------------------------------------------- #
class TestAnEditMidRunChangesNothing(SnapshotBase):
	def test_an_edited_prompt(self):
		run, conv, macro = self._start(steps=3)
		self._edit(macro, lambda d: setattr(d.steps[1], "prompt", "edited"))
		for index in range(3):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "step 2", "step 3"])

	def test_an_added_step(self):
		run, conv, macro = self._start(steps=2)
		self._edit(macro, lambda d: d.append("steps", {"prompt": "added"}))
		for index in range(2):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "step 2"])

	def test_a_removed_step(self):
		run, conv, macro = self._start(steps=3)
		self._edit(macro, lambda d: d.set("steps", d.steps[:1]))
		for index in range(3):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "step 2", "step 3"])

	def test_reordered_steps(self):
		run, conv, macro = self._start(steps=3)

		def reverse(d):
			prompts = [s.prompt for s in d.steps]
			for step, prompt in zip(d.steps, reversed(prompts), strict=True):
				step.prompt = prompt

		self._edit(macro, reverse)
		for index in range(3):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "step 2", "step 3"])

	def test_stop_on_error_turned_off(self):
		run, conv, macro = self._start(steps=3, stop_on_error=1)
		self._edit(macro, lambda d: setattr(d, "stop_on_error", 0))
		self._end_step(conv, run, 0, state="errored")
		self.assertEqual(self._prompts(conv), ["step 1"], "a step was sent after a failure")
		self.assertEqual(self._run(run).status, "failed")

	def test_stop_on_error_turned_on(self):
		run, conv, macro = self._start(steps=2, stop_on_error=0)
		self._edit(macro, lambda d: setattr(d, "stop_on_error", 1))
		self._end_step(conv, run, 0, state="errored")
		self.assertEqual(self._prompts(conv), ["step 1", "step 2"], "the run stopped at the failure")
		self.assertEqual(self._run(run).status, "running")

	def test_a_changed_model_and_thinking_override(self):
		run, conv, macro = self._start(
			steps=2, step_fields={1: {"model_override": "m-old", "thinking_override": "low"}}
		)

		def change(d):
			d.steps[1].model_override = "m-new"
			d.steps[1].thinking_override = "high"

		self._edit(macro, change)
		with self._sent() as sent:
			self._end_step(conv, run, 0)
		self.assertEqual(sent.call_count, 1)
		kw = sent.call_args.kwargs
		self.assertEqual((kw["model_override"], kw["thinking_override"]), ("m-old", "low"))

	def test_a_new_summary_does_not_change_a_summarized_run(self):
		# The summary landed again (Re-summarize) while the run was parked for
		# capacity: the run sends the summary it started with.
		run, conv, macro = self._start_parked(steps=3, merged="do all three")
		frappe.db.set_value(MACRO, macro, "merged_prompt", "do it differently", update_modified=False)
		frappe.db.commit()
		self._resume()
		self.assertEqual(self._prompts(conv), ["do all three"])
		self._end_step(conv, run, 0)
		self._assert_ran(run, conv, ["do all three"])

	def test_a_summarized_run_keeps_its_steps_skills(self):
		# The summary carries every step's skills. A skill taken off a step while the
		# run was parked is still the run's.
		skill = self._skill()
		run, conv, macro = self._start_parked(
			steps=2, merged="do both", step_fields={1: {"skills": json.dumps([skill.name])}}
		)
		self._edit(macro, lambda d: setattr(d.steps[1], "skills", None))
		self._resume()
		self.assertEqual(self._prompts(conv), [f"do both\n\nApply these skills: /{skill.skill_name}"])

	def test_a_progress_event_names_the_step_the_run_sends(self):
		run, conv, macro = self._start(steps=2, step_fields={1: {"label": "Old label"}})
		self._edit(macro, lambda d: setattr(d.steps[1], "label", "New label"))
		with patch.object(macros, "publish_to_user") as publish:
			self._end_step(conv, run, 0)
		labels = [
			c.args[1].get("label") for c in publish.call_args_list if c.args[1]["kind"] == "macro:progress"
		]
		self.assertEqual(labels, ["Old label"])

	def test_the_five_minute_check_carries_a_run_on_with_its_snapshot(self):
		# Step 2's end never reached the hook; the check finds it minutes later, after
		# the macro was cut to one step and its prompts rewritten.
		from jarvis.chat import macro_reconcile

		run, conv, macro = self._start(steps=3)
		self._end_step(conv, run, 0)
		step_two = step_turn_id(run, 1)
		self._finish(conv, step_two, deliver=False)
		ago = frappe.utils.add_to_date(None, minutes=-5)
		frappe.db.set_value(TURN, step_two, "done_at", ago, update_modified=False)
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified = %s WHERE name = %s", (ago, run))
		frappe.db.commit()

		def change(d):
			d.set("steps", d.steps[:1])
			d.steps[0].prompt = "edited"

		self._edit(macro, change)
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified = %s WHERE name = %s", (ago, run))
		frappe.db.commit()
		summary = dict.fromkeys(("acted", "busy", "raised", "conflicts"), 0)
		with patch.object(macro_reconcile, "_send_gate_refusal", return_value=None):
			macro_reconcile._RunCheck(run, summary).run()
		frappe.set_user("Administrator")
		self.assertEqual(summary["acted"], 1)
		self.assertEqual(self._prompts(conv), ["step 1", "step 2", "step 3"])
		self.assertEqual((self._run(run).status, self._run(run).current_step), ("running", 3))


# --------------------------------------------------------------------------- #
# A parked run resumes with its snapshot
# --------------------------------------------------------------------------- #
class TestAResumeAfterAnEdit(SnapshotBase):
	def _park_at_step_two(self, **kw):
		run, conv, macro = self._start(**kw)
		with patch.object(macros, "_send_step", return_value=_FULL):
			self._end_step(conv, run, 0)
		self.assertEqual(self._run(run).status, "waiting_capacity", "premise: the run parked")
		return run, conv, macro

	def test_a_run_parked_past_the_end_of_its_shrunk_macro_carries_on(self):
		# It used to be ended: "the macro was edited while this run was waiting".
		run, conv, macro = self._park_at_step_two(steps=3)
		self._edit(macro, lambda d: d.set("steps", d.steps[:1]))
		self._resume()
		self.assertEqual(self._prompts(conv), ["step 1", "step 2"])
		self.assertEqual(self._run(run).status, "running")
		for index in (1, 2):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "step 2", "step 3"])

	def test_a_summarized_run_whose_summary_was_cleared_carries_on(self):
		# A step edit clears the summary; the run sends the one it started with.
		run, conv, macro = self._start_parked(steps=3, merged="do all three")
		frappe.db.set_value(MACRO, macro, {"merged_prompt": "", "merge_status": ""}, update_modified=False)
		frappe.db.commit()
		self._resume()
		self.assertEqual(self._prompts(conv), ["do all three"])
		self.assertEqual(self._run(run).status, "running")


# --------------------------------------------------------------------------- #
# A parked run whose every step is already out
# --------------------------------------------------------------------------- #
class TestAParkedRunWithEveryStepOut(SnapshotBase):
	"""A second dispatcher parked the run while the first sent its last step (a run
	lock that lapsed), so the cursor is at the end. The resume has no step to send."""

	def _park_with_every_step_out(self, *, snapshot=True, last_step_ended=True):
		run, conv, macro = self._start(steps=2)
		self._end_step(conv, run, 0)  # step 2 sent: the cursor is at the end
		self.assertEqual(int(self._run(run).current_step), 2, "premise: every step is out")
		frappe.db.set_value(RUN, run, "status", "waiting_capacity", update_modified=False)
		frappe.db.commit()
		if last_step_ended:
			self._end_step(conv, run, 1)  # a parked run takes no step end
			self.assertEqual(self._run(run).status, "waiting_capacity", "premise: the end was not taken")
		if not snapshot:
			self._forget_the_snapshot(run)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": f"jarvis macro capacity-resume enqueue raised: {run}"}
		)
		with self._sent() as sent:
			self._resume()
		self.assertEqual(sent.call_count, 0, "a step was sent past the end")
		return run, conv

	def _capacity_resume_errors(self, run):
		return frappe.get_all(
			"Error Log", filters={"method": f"jarvis macro capacity-resume enqueue raised: {run}"}
		)

	def test_the_last_steps_end_ends_it(self):
		# It was re-parked every cycle (an IndexError) and failed ~100 minutes later as
		# "the site stayed busy", every step having run.
		run, conv = self._park_with_every_step_out()
		self._assert_ran(run, conv, ["step 1", "step 2"])
		self.assertEqual(self._capacity_resume_errors(run), [])

	def test_a_failed_last_step_fails_it(self):
		run, conv, _ = self._start(steps=2, stop_on_error=1)
		self._end_step(conv, run, 0)
		frappe.db.set_value(RUN, run, "status", "waiting_capacity", update_modified=False)
		frappe.db.commit()
		self._end_step(conv, run, 1, state="errored")
		self._resume()
		self.assertEqual(self._run(run).status, "failed")
		self.assertEqual(self._capacity_resume_errors(run), [])

	def test_a_last_step_still_running_ends_it_when_it_ends(self):
		run, conv = self._park_with_every_step_out(last_step_ended=False)
		self.assertEqual(self._run(run).status, "running", "the resume took the run back")
		self._end_step(conv, run, 1)
		self._assert_ran(run, conv, ["step 1", "step 2"])

	def test_without_a_snapshot_it_ends_as_before(self):
		# Read off the macro, a cursor at the end is what a macro shrunk past the
		# cursor looks like: ended at once, as it always was.
		run, _ = self._park_with_every_step_out(snapshot=False)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", macros._MACRO_CHANGED_ERROR))
		self.assertEqual(self._capacity_resume_errors(run), [])


# --------------------------------------------------------------------------- #
# A run started before the snapshot existed runs as it always did
# --------------------------------------------------------------------------- #
class TestARunWithoutASnapshot(SnapshotBase):
	def test_it_reads_the_macro_as_it_is_now(self):
		run, conv, macro = self._start(steps=3)
		self._forget_the_snapshot(run)
		self._edit(macro, lambda d: setattr(d.steps[1], "prompt", "edited"))
		for index in range(3):
			self._end_step(conv, run, index)
		self._assert_ran(run, conv, ["step 1", "edited", "step 3"])

	def test_its_stop_on_error_is_the_macros(self):
		run, conv, macro = self._start(steps=2, stop_on_error=1)
		self._forget_the_snapshot(run)
		self._edit(macro, lambda d: setattr(d, "stop_on_error", 0))
		self._end_step(conv, run, 0, state="errored")
		self.assertEqual(self._prompts(conv), ["step 1", "step 2"])

	def test_parked_past_the_end_of_its_shrunk_macro_it_is_still_ended(self):
		run, conv, macro = self._start(steps=3)
		with patch.object(macros, "_send_step", return_value=_FULL):
			self._end_step(conv, run, 0)
		self._forget_the_snapshot(run)
		self._edit(macro, lambda d: d.set("steps", d.steps[:1]))
		with self._sent() as sent:
			self._resume()
		sent.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("failed", macros._MACRO_CHANGED_ERROR))

	def _unreadable(self, run):
		frappe.db.set_value(RUN, run, FIELD, "{not json", update_modified=False)
		frappe.db.commit()
		self.addCleanup(frappe.cache().delete_value, f"jarvis_macro_snapshot_unreadable:{run}")
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			frappe.db.delete, "Error Log", {"method": f"jarvis.chat.macros.snapshot_unreadable: {run}"}
		)

	@contextmanager
	def _reports(self):
		"""The jobs ``_run_content`` asks for, recorded instead of queued."""
		asked, real = [], frappe.enqueue

		def enqueue(*args, **kw):
			method = args[0] if args else kw.get("method")
			if method == "jarvis.chat.macros._log_unreadable_snapshot":
				asked.append(kw)
				return None
			return real(*args, **kw)

		with patch.object(frappe, "enqueue", enqueue):
			yield asked

	def test_an_unreadable_snapshot_is_the_macro_and_is_reported_once(self):
		run, conv, macro = self._start(steps=3)
		self._unreadable(run)
		self._edit(macro, lambda d: setattr(d.steps[1], "prompt", "edited"))
		with self._reports() as asked:
			self._end_step(conv, run, 0)
			self._end_step(conv, run, 1)
		self.assertEqual(self._prompts(conv), ["step 1", "edited", "step 3"])
		self.assertEqual(asked, [{"queue": "short", "run_name": run}], "one report for the run")

		# The job, as a worker runs it: as the owner who queued it. Twice: the cache can
		# lose its marker.
		for _ in range(2):
			frappe.set_user(OWNER)
			macros._log_unreadable_snapshot(run_name=run)
		frappe.set_user("Administrator")
		logs = frappe.get_all(
			"Error Log",
			filters={"method": f"jarvis.chat.macros.snapshot_unreadable: {run}"},
			fields=["error", "owner"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].owner, "Administrator")
		self.assertNotIn("step 1", logs[0].error)  # the run, never its text

	def test_reporting_it_writes_nothing_in_the_sends_transaction(self):
		# ``_step_prompt`` is read right before ``_send_step``, as the owner: a write
		# there would keep a lost race from being replayed (``txn.replay_is_safe``).
		from jarvis.chat import txn

		run, _, macro = self._start(steps=2)
		self._unreadable(run)
		run_doc, macro_doc = frappe.get_doc(RUN, run), frappe.get_doc(MACRO, macro)
		frappe.db.commit()
		frappe.set_user(OWNER)
		with self._reports() as asked:
			prompt, _ = macros._step_prompt(run_doc, macro_doc, 1)
		safe = txn.replay_is_safe()
		frappe.set_user("Administrator")
		self.assertEqual((prompt, len(asked)), ("step 2", 1))
		self.assertTrue(safe, "the report wrote in the send's transaction")


# --------------------------------------------------------------------------- #
# Every write that ends a run clears it
# --------------------------------------------------------------------------- #
class TestEveryEndClearsTheSnapshot(SnapshotBase):
	def _assert_ended_and_cleared(self, run, status):
		self.assertEqual(self._run(run).status, status)
		self.assertIsNone(self._snapshot(run))

	def test_the_run_ends_through_the_hook(self):  # _finish
		run, conv, _ = self._start(steps=1)
		self.assertTrue(self._snapshot(run), "premise: the run has a snapshot")
		self._end_step(conv, run, 0)
		self._assert_ended_and_cleared(run, "completed")

	def test_the_stale_run_sweep(self):  # _cas_run_status
		run, _, _ = self._start(steps=2)
		self.assertTrue(self._snapshot(run), "premise: the run has a snapshot")
		frappe.db.sql(f"UPDATE `tab{RUN}` SET modified = modified - INTERVAL 1 DAY WHERE name = %s", run)
		frappe.db.commit()
		with patch.object(macros, "_stale_run_candidates", return_value=[run]):
			self.assertEqual(macros.reap_stale_macro_runs(), 1)
		self._assert_ended_and_cleared(run, "failed")

	def test_a_first_step_that_could_not_be_sent(self):  # _cas_run_status, from run_macro
		macro = self._mk_macro(steps=2, tag="nostart")
		with (
			self._starting(),
			patch.object(macros, "_send_step", side_effect=RuntimeError("gateway down")),
			patch("frappe.log_error"),
			self.assertRaises(frappe.ValidationError),
		):
			macros.run_macro(macro)
		frappe.set_user("Administrator")
		run = frappe.db.get_value(RUN, {"macro": macro}, "name")
		self._assert_ended_and_cleared(run, "failed")

	def test_a_run_whose_chat_is_gone(self):  # _fail_run_in_place
		run, _, _ = self._start_parked(steps=2)
		self.assertTrue(self._snapshot(run), "premise: the run has a snapshot")
		frappe.db.set_value(RUN, run, "conversation", None, update_modified=False)
		frappe.db.commit()
		self._resume()
		self._assert_ended_and_cleared(run, "failed")
		self.assertEqual(frappe.db.get_value(RUN, run, "error"), macros._CONVERSATION_GONE_ERROR)

	def test_the_owners_stop(self):  # _stop_run -> _cas_run_status
		run, _, _ = self._start(steps=2)
		self.assertTrue(self._snapshot(run), "premise: the run has a snapshot")
		frappe.set_user(OWNER)
		macros.stop_macro_run(run)
		frappe.set_user("Administrator")
		self._assert_ended_and_cleared(run, "stopped")

	def test_a_transition_between_two_live_states_keeps_it(self):
		run, _, _ = self._start_parked(steps=2)
		before = self._snapshot(run)
		self.assertTrue(macros._cas_run_status(run, "waiting_capacity", "running"))
		frappe.db.commit()
		self.assertEqual(self._snapshot(run), before)


class _MetaWithoutTheField:
	"""The run doctype's meta as a site whose migrate has not added the field reads it."""

	def __init__(self, meta):
		self._meta = meta

	def __getattr__(self, name):
		return getattr(self._meta, name)

	def has_field(self, fieldname):
		return fieldname != FIELD and self._meta.has_field(fieldname)


@contextmanager
def _before_the_migrate():
	real = frappe.get_meta

	def get_meta(doctype, *args, **kw):
		meta = real(doctype, *args, **kw)
		return _MetaWithoutTheField(meta) if doctype == RUN else meta

	with patch.object(frappe, "get_meta", get_meta):
		yield


# --------------------------------------------------------------------------- #
# Whether the field is there is the meta's say, not the table's
# --------------------------------------------------------------------------- #
class TestTheFieldGuard(SnapshotBase):
	def test_the_meta_decides_not_the_table(self):
		# On Frappe 16 the table lookup can see another site's database; on Frappe 15
		# it can be cached stale. Either way it may say yes before this site migrated.
		with patch.object(frappe.db, "has_column", return_value=True), _before_the_migrate():
			self.assertEqual(macros._snapshot_cleared(), {})
		with patch.object(frappe.db, "has_column", return_value=False):
			self.assertEqual(macros._snapshot_cleared(), {FIELD: None})

	def test_before_the_migrate_a_run_ends_without_naming_the_field(self):
		run, conv, _ = self._start(steps=1)
		before = self._snapshot(run)
		with _before_the_migrate():
			self._end_step(conv, run, 0)
		self.assertEqual(self._run(run).status, "completed")
		self.assertEqual(self._snapshot(run), before, "the end wrote the field")


# --------------------------------------------------------------------------- #
# An ended run that kept its snapshot loses it later
# --------------------------------------------------------------------------- #
class TestAnEndedRunThatKeptItsSnapshot(SnapshotBase):
	def _ended_by_older_code(self, run):
		"""Ended by a worker still on code from before the snapshot: its write names
		status alone. ``modified`` is set back so a change to it shows."""
		frappe.db.sql(
			f"""UPDATE `tab{RUN}` SET status = 'completed', modified = modified - INTERVAL 1 HOUR
			    WHERE name = %s""",
			run,
		)
		frappe.db.commit()
		self.assertTrue(self._snapshot(run), "premise: the ended run kept its snapshot")
		return frappe.db.get_value(RUN, run, "modified")

	def test_the_hourly_job_clears_it(self):
		ended, _, _ = self._start(steps=2, tag="ended")
		modified = self._ended_by_older_code(ended)
		live, _, _ = self._start(steps=2, tag="live")
		with patch.object(macros, "_stale_run_candidates", return_value=[]):
			macros.reap_stale_macro_runs()
		self.assertIsNone(self._snapshot(ended))
		self.assertEqual(
			frappe.db.get_value(RUN, ended, "modified"), modified, "nothing about the run changed"
		)
		self.assertTrue(self._snapshot(live), "a live run lost its snapshot")

	def test_the_hourly_job_leaves_it_before_the_migrate(self):
		ended, _, _ = self._start(steps=2)
		self._ended_by_older_code(ended)
		with patch.object(macros, "_stale_run_candidates", return_value=[]), _before_the_migrate():
			macros.reap_stale_macro_runs()
		self.assertTrue(self._snapshot(ended))

	def test_a_macro_delete_clears_it_on_a_row_kept_for_the_budget(self):
		run, conv, macro = self._start(steps=2)
		self._end_step(conv, run, 0)
		frappe.db.set_value(RUN, run, "trigger", "scheduled", update_modified=False)
		self._ended_by_older_code(run)
		macros.drop_runs_of_deleted_macro(macro)
		frappe.db.commit()
		row = frappe.db.get_value(RUN, run, ["macro", FIELD], as_dict=True)
		self.assertIsNotNone(row, "premise: the row is kept for the budget")
		self.assertEqual((row.macro, row[FIELD]), (None, None))


# --------------------------------------------------------------------------- #
# Never shown, never written by a user
# --------------------------------------------------------------------------- #
class TestNeverShownNeverWritten(SnapshotBase):
	MARK = "snapshot-marker-7f3a"

	def _keys(self, value):
		if isinstance(value, dict):
			for key, inner in value.items():
				yield key
				yield from self._keys(inner)
		elif isinstance(value, list | tuple):
			for inner in value:
				yield from self._keys(inner)

	def test_no_list_endpoint_returns_it(self):
		run, _, macro = self._start(steps=2, step_fields={1: {"prompt": self.MARK}})
		self.assertIn(self.MARK, self._snapshot(run), "premise: the snapshot holds the marked prompt")
		# The macro's own steps would show the mark: take it out of the macro.
		self._edit(macro, lambda d: setattr(d.steps[1], "prompt", "plain"))
		frappe.set_user(OWNER)
		owner_views = {
			"list_macro_runs": macros_api.list_macro_runs(),
			"get_macro_run": macros_api.get_macro_run(run),
			"macro_run_stats": macros_api.macro_run_stats(),
			"list_macros": macros_api.list_macros(),
			"list_macros_page": macros_api.list_macros_page(),
			"get_macro": macros_api.get_macro(macro),
		}
		frappe.set_user("Administrator")
		admin_views = {
			"admin_list_macros": macros_admin_api.admin_list_macros(),
			"admin_get_macro": macros_admin_api.admin_get_macro(macro),
		}
		self.assertIn(
			run, json.dumps(owner_views["list_macro_runs"], default=str), "premise: the run is listed"
		)
		self.assertIn(
			run, json.dumps(admin_views["admin_get_macro"], default=str), "premise: the run is listed"
		)
		for name, view in {**owner_views, **admin_views}.items():
			self.assertNotIn(FIELD, set(self._keys(view)), name)
			self.assertNotIn(self.MARK, json.dumps(view, default=str), name)

	def test_the_owner_cannot_write_it(self):
		run, _, _ = self._start(steps=2)
		before = self._snapshot(run)
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.PermissionError):
			set_value(RUN, run, FIELD, "{}")
		doc = frappe.get_doc(RUN, run)
		doc.set(FIELD, "{}")
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		self.assertEqual(self._snapshot(run), before)

	def test_it_is_hidden_and_read_only_on_the_form(self):
		field = frappe.get_meta(RUN).get_field(FIELD)
		self.assertEqual(
			(field.fieldtype, field.hidden, field.read_only, field.in_list_view), ("Long Text", 1, 1, 0)
		)
