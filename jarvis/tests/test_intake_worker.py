"""Intake worker: guards, limits, one-call read, retry reuse and slot handling."""

import io
import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, cint, now_datetime

from jarvis import _redis_lock
from jarvis.intake import capture, reader, spec, worker
from jarvis.triggers import engine, llm_action
from jarvis.triggers import llm_task_client as lt

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"
READING = {"description": "buy milk"}
# setUp patches the module attribute; the capability test needs the real function.
_REAL_VISION_AVAILABLE = reader.vision_available


def _png() -> bytes:
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", (40, 40), (10, 200, 10)).save(buf, format="PNG")
	return buf.getvalue()


def _pdf(pages: int) -> bytes:
	from pypdf import PdfWriter

	writer = PdfWriter()
	for _ in range(pages):
		writer.add_blank_page(width=200, height=200)
	buf = io.BytesIO()
	writer.write(buf)
	return buf.getvalue()


class TestIntakeWorker(FrappeTestCase):
	def setUp(self):
		self._triggers: list[str] = []
		self._files: list[str] = []
		self._clear_state()
		# The worker commits, so rows from other runs outlive the rollback; the chaining and
		# pump tests look at every Queued row of the (throwaway) site.
		frappe.db.delete(ACTIVITY, {"action_type": "Intake"})
		# Unattended identity is a separate concern; the one test about it un-mocks this.
		p = mock.patch("jarvis.permissions.is_valid_unattended_owner", return_value=True)
		p.start()
		self.addCleanup(p.stop)
		# The test site has no vision provider; the capability gate has its own tests.
		v = mock.patch("jarvis.intake.reader.vision_available", return_value=True)
		v.start()
		self.addCleanup(v.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clear_state()
		for name in self._files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, ignore_permissions=True, force=True)
		for name in self._triggers:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		engine.clear_cache()
		capture.clear_intake_cache()
		# The worker commits, so this cleanup must be committed too or the rule leaks into
		# later tests (capture's "another rule already creates ..." check).
		frappe.db.commit()

	def _clear_state(self):
		cache = frappe.cache()
		for key in worker.slot_keys():
			cache.delete(cache.make_key(key))
		cache.delete(cache.make_key(worker.site_cap_key()))
		cache.delete(cache.make_key(f"{worker.site_cap_key()}:count"))
		cache.delete(cache.make_key(f"jarvis:intake:capbell:Administrator:{frappe.utils.nowdate()}"))
		cache.delete(cache.make_key("jarvis:intake:failalert"))
		for user in ("Administrator", "intake.uploader@example.com"):
			cache.delete(cache.make_key(worker.user_cap_key(user)))
			cache.delete(cache.make_key(f"{worker.user_cap_key(user)}:count"))

	def _rule(self, **over):
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps({"fields": ["description"]}),
			"enabled": 1,
		}
		doc.update(over)
		rule = frappe.get_doc(doc).insert(ignore_permissions=True)
		self._triggers.append(rule.name)
		llm_action_key = llm_action.cap_key(rule.name)
		cache = frappe.cache()
		cache.delete(cache.make_key(llm_action_key))
		cache.delete(cache.make_key(f"{llm_action_key}:count"))
		return rule

	def _file(self, name="sheet.png", content=None):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": name, "content": content or _png(), "is_private": 1}
		).insert(ignore_permissions=True)
		self._files.append(doc.name)
		return doc

	def _row(self, rule, fdoc=None, **over):
		fdoc = fdoc or self._file()
		values = {
			"doctype": ACTIVITY,
			"trigger": rule.name,
			"trigger_label": rule.trigger_name,
			"action_type": "Intake",
			"status": "Queued",
			"source_file": fdoc.name,
			"file_hash": fdoc.content_hash,
			"target_doctype": "ToDo",
			"event_user": "Administrator",
			"summary": "Queued",
		}
		values.update(over)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def _run(self, row, reply=None, finish=None):
		"""Run the job with the model and the draft stage faked; return (client, finish)."""
		client = mock.Mock(return_value=READING if reply is None else reply)
		fin = finish or mock.Mock()
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", client),
			mock.patch("jarvis.intake.draft.finish", fin),
			mock.patch("frappe.enqueue"),
		):
			worker.run_job(row.name)
		return client, fin

	def _get(self, row):
		return frappe.db.get_value(
			ACTIVITY, row.name, ["status", "summary", "detail", "attempts", "reading_json"], as_dict=True
		)

	def test_invalid_uploader_fails_before_read(self):
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.permissions.is_valid_unattended_owner", return_value=False):
			client, fin = self._run(row)
		client.assert_not_called()
		fin.assert_not_called()
		self.assertEqual(self._get(row).status, "Failed")

	def test_disabled_trigger_skips(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.db.set_value(TRIGGER, rule.name, "enabled", 0)
		client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Skipped", "rule turned off"))

	def test_rule_cap_defers(self):
		rule = self._rule(llm_daily_cap=2)
		llm_action.bump(llm_action.cap_key(rule.name))
		llm_action.bump(llm_action.cap_key(rule.name))
		row = self._row(rule)
		client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual(
			(got.status, got.summary),
			("Queued", "Waiting: daily limit reached for this rule (resets tomorrow)"),
		)

	def test_site_cap_defers(self):
		rule = self._rule()
		row = self._row(rule)
		llm_action.bump(worker.site_cap_key())
		with mock.patch.object(worker, "_site_cap", return_value=1):
			client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual(
			(got.status, got.summary),
			("Queued", "Waiting: daily limit reached for the site (resets tomorrow)"),
		)

	def test_ai_call_counts_once_against_both_caps(self):
		rule = self._rule()
		row = self._row(rule)
		client, fin = self._run(row)
		client.assert_called_once()
		self.assertEqual(llm_action.daily_count(rule.name), 1)
		self.assertEqual(frappe.cache().get_value(f"{worker.site_cap_key()}:count"), 1)
		fin.assert_called_once()
		self.assertEqual(fin.call_args.args[1], READING)

	def test_reading_saved_and_reused_on_retry(self):
		rule = self._rule()
		row = self._row(rule)
		client = mock.Mock(return_value=READING)
		fin = mock.Mock(side_effect=[RuntimeError("draft blew up"), None])
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", client),
			mock.patch("jarvis.intake.draft.finish", fin),
			mock.patch("frappe.enqueue"),
		):
			worker.run_job(row.name)
			first = self._get(row)
			self.assertEqual(first.status, "Failed")
			self.assertEqual(json.loads(first.reading_json), READING)
			self.assertIn("draft blew up", first.detail)
			frappe.db.set_value(ACTIVITY, row.name, "status", "Queued")
			worker.run_job(row.name)
		self.assertEqual(client.call_count, 1)
		self.assertEqual(fin.call_count, 2)
		self.assertEqual(self._get(row).attempts, 2)
		# The retry made no AI call, so it cost nothing against the caps.
		self.assertEqual(llm_action.daily_count(rule.name), 1)

	def test_pdf_page_cap_noted(self):
		rule = self._rule()
		fdoc = self._file("big.pdf", _pdf(14))
		client = mock.Mock(return_value=READING)
		with mock.patch("jarvis.intake.client.vision_task_complete", client):
			s = spec.load({"intake_target_doctype": "ToDo", "intake_spec": {"fields": ["description"]}})
			data, note = reader.read_file(fdoc.name, s)
		self.assertEqual(data, READING)
		self.assertEqual(note, "Read 10 of 14 pages")
		self.assertEqual(len(client.call_args.kwargs["images"]), 10)
		row = self._row(rule, fdoc)
		_client, fin = self._run(row)
		# The uploader sees the note: it is in the Running summary and in the saved reading
		# (so a draft-only retry keeps it); draft.finish pops it into the final summary.
		got = self._get(row)
		self.assertIn("Read 10 of 14 pages", got.summary)
		self.assertEqual(json.loads(got.reading_json)["_note"], "Read 10 of 14 pages")
		self.assertEqual(fin.call_args.args[1]["_note"], "Read 10 of 14 pages")

	def test_note_counts_pages_actually_read(self):
		fdoc = self._file("nine.pdf", _pdf(14))
		s = spec.load({"intake_target_doctype": "ToDo", "intake_spec": {"fields": ["description"]}})
		real = reader.vision.pdf_parts

		def nine(*a, **kw):
			parts, total = real(*a, **kw)
			return parts[:9], total

		with (
			mock.patch("jarvis.intake.reader.vision.pdf_parts", nine),
			mock.patch("jarvis.intake.client.vision_task_complete", mock.Mock(return_value=READING)),
		):
			_data, note = reader.read_file(fdoc.name, s)
		self.assertEqual(note, "Read 9 of 14 pages")

	def test_second_job_for_a_claimed_row_makes_no_ai_call(self):
		rule = self._rule()
		row = self._row(rule)
		stale = frappe.get_doc(ACTIVITY, row.name)  # what a racing job loaded: still Queued
		client, _ = self._run(row)
		self.assertEqual(client.call_count, 1)
		client2 = mock.Mock(return_value=READING)
		real_get_doc = frappe.get_doc
		racing = mock.Mock(
			side_effect=lambda *a, **kw: stale if a and a[0] == ACTIVITY else real_get_doc(*a, **kw)
		)
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", client2),
			mock.patch("jarvis.intake.draft.finish"),
			mock.patch("frappe.enqueue"),
			mock.patch("frappe.get_doc", racing),
		):
			worker.run_job(row.name)
		client2.assert_not_called()
		cache = frappe.cache()
		self.assertFalse(any(cache.exists(k) for k in worker.slot_keys()))

	def test_claim_stamps_modified_so_the_pump_does_not_see_a_stale_row(self):
		rule = self._rule()
		row = self._row(rule)
		old = add_to_date(now_datetime(), minutes=-30)
		frappe.db.set_value(ACTIVITY, row.name, "modified", old, update_modified=False)
		self.assertTrue(worker._claim_row(frappe.get_doc(ACTIVITY, row.name)))
		modified = frappe.db.get_value(ACTIVITY, row.name, "modified")
		self.assertGreater(modified, add_to_date(now_datetime(), minutes=-1))

	def test_job_runs_as_administrator_whoever_enqueued_it(self):
		"""A job inherits the enqueuer (an uploader, a retrying owner). The bookkeeping must
		not depend on who that was, and the caller's session comes back afterwards."""
		rule = self._rule()
		row = self._row(rule)
		seen = []
		fin = mock.Mock(side_effect=lambda *a: seen.append(frappe.session.user))
		frappe.set_user("Guest")
		try:
			self._run(row, finish=fin)
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(seen, ["Administrator"])

	def _stuck(self, rule, attempts):
		row = self._row(rule, status="Running", attempts=attempts)
		frappe.db.set_value(
			ACTIVITY, row.name, "modified", add_to_date(now_datetime(), minutes=-11), update_modified=False
		)
		return row

	def test_pump_requeues_stuck_running_row_under_three_attempts(self):
		rule = self._rule()
		row = self._stuck(rule, 1)
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Queued", "Retrying after an interruption"))
		self.assertIn(row.name, [c.kwargs["activity"] for c in enq.call_args_list])

	def test_one_bad_stuck_row_does_not_stop_the_rest(self):
		rule = self._rule()
		rows = [self._stuck(rule, 1), self._stuck(rule, 1)]
		calls = []

		def enqueue(name):
			calls.append(name)
			if len(calls) == 1:
				raise RuntimeError("queue down")

		with (
			mock.patch.object(worker, "_enqueue", side_effect=enqueue),
			mock.patch("frappe.log_error") as log,
		):
			worker._recover_stuck()
		self.assertEqual(sorted(calls), sorted(r.name for r in rows))
		log.assert_called_once()

	def test_pump_fails_stuck_running_row_after_three_attempts(self):
		rule = self._rule()
		row = self._stuck(rule, 3)
		fresh = self._row(rule, self._file("f.png"), status="Running", attempts=1)
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", "Stopped unexpectedly. Retry it."))
		self.assertNotIn(row.name, [c.kwargs["activity"] for c in enq.call_args_list])
		self.assertEqual(self._get(fresh).status, "Running")  # recent: left alone

	def test_removed_source_file_fails_cleanly_and_is_not_retryable(self):
		rule = self._rule()
		fdoc = self._file()
		row = self._row(rule, fdoc)
		frappe.delete_doc("File", fdoc.name, ignore_permissions=True, force=True)
		with mock.patch("frappe.log_error") as log:
			client, fin = self._run(row)
		client.assert_not_called()
		fin.assert_not_called()
		log.assert_not_called()  # an expected outcome, not an Error Log entry
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", "The file was removed"))
		self.assertEqual(capture.FILE_REMOVED_SUMMARY, "The file was removed")

	def _bell(self, row, like):
		# Rows from other runs are committed, so only this test's own (named by file) count.
		file_name = frappe.db.get_value("File", row.source_file, "file_name")
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": row.event_user, "subject": ["like", f"{file_name}: %{like}%"]},
			pluck="name",
		)

	def test_failed_and_skipped_rows_tell_the_uploader(self):
		rule = self._rule()
		failed = self._row(rule, self._file(f"{frappe.generate_hash(length=8)}.png"))
		err = mock.Mock(side_effect=lt.LLMTaskError("model said no"))
		with mock.patch("jarvis.intake.client.vision_task_complete", err), mock.patch("frappe.enqueue"):
			worker.run_job(failed.name)
		self.assertEqual(len(self._bell(failed, "Reading the file failed")), 1)
		# the bell carries the safe summary only, never the manager-only detail
		self.assertFalse(self._bell(failed, "model said no"))
		skipped = self._row(rule, self._file(f"{frappe.generate_hash(length=8)}.png"))
		frappe.db.set_value(TRIGGER, rule.name, "enabled", 0)
		self._run(skipped)
		self.assertEqual(len(self._bell(skipped, "rule turned off")), 1)

	def test_old_plugin_maps_to_update_copy(self):
		rule = self._rule()
		row = self._row(rule)
		client = mock.Mock(side_effect=lt.LLMTaskNotAvailableError("404"))
		with mock.patch("jarvis.intake.client.vision_task_complete", client), mock.patch("frappe.enqueue"):
			worker.run_job(row.name)
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", worker.NO_AGENT_UPDATE_SUMMARY))
		self.assertEqual(worker.NO_AGENT_UPDATE_SUMMARY, "Intake needs the latest agent update on this site.")
		self.assertNotIn("LLM", got.summary)

	def test_failure_stores_short_reason_and_logs_traceback(self):
		rule = self._rule()
		row = self._row(rule)
		fin = mock.Mock(side_effect=RuntimeError("/home/secret/path.py: SELECT * FROM tabUser " + "x" * 500))
		with mock.patch("frappe.log_error") as log:
			self._run(row, finish=fin)
		got = self._get(row)
		self.assertEqual(got.status, "Failed")
		self.assertNotIn("Traceback", got.detail)
		self.assertTrue(got.detail.startswith("RuntimeError"))
		self.assertLessEqual(len(got.detail), 260)
		self.assertTrue(log.called)
		self.assertIn("Traceback", log.call_args.kwargs["message"])
		self.assertEqual(log.call_args.kwargs["title"], "Jarvis intake: draft failed")

	def test_crash_stores_no_traceback(self):
		rule = self._rule()
		row = self._row(rule)
		with (
			mock.patch("jarvis.intake.worker._run_stages", side_effect=KeyError("boom")),
			mock.patch("frappe.log_error") as log,
			mock.patch("frappe.enqueue"),
		):
			worker.run_job(row.name)
		got = self._get(row)
		self.assertNotIn("Traceback", got.detail)
		self.assertIn("KeyError", got.detail)
		self.assertIn("Traceback", log.call_args.kwargs["message"])

	def test_other_agent_error_fails_with_safe_summary(self):
		rule = self._rule()
		row = self._row(rule)
		client = mock.Mock(side_effect=lt.LLMTaskError("secret ref ABC-123"))
		with mock.patch("jarvis.intake.client.vision_task_complete", client), mock.patch("frappe.enqueue"):
			worker.run_job(row.name)
		got = self._get(row)
		self.assertEqual(got.status, "Failed")
		self.assertNotIn("ABC-123", got.summary)
		self.assertIn("ABC-123", got.detail)
		self.assertFalse(got.reading_json)

	def test_no_image_reply_fails_retryably(self):
		rule = self._rule()
		row = self._row(rule)
		client, fin = self._run(row, reply={"error": "NO_IMAGE"})
		fin.assert_not_called()
		got = self._get(row)
		self.assertEqual(got.status, "Failed")
		self.assertEqual(got.summary, "This site's AI model can't read images for intake yet.")
		self.assertFalse(got.reading_json)
		# The model was called, so a NO_IMAGE answer counts (R23): junk uploads cannot dodge the caps.
		self.assertEqual(llm_action.daily_count(rule.name), 1)

	def test_prompt_has_fixed_rules_labels_and_no_file_text(self):
		rule = self._rule(intake_instructions="Dates are day first.")
		row = self._row(rule, self._file("receipt-secret-name.png"))
		client, _ = self._run(row)
		prompt = client.call_args.kwargs["prompt"]
		self.assertIn("Never guess or invent values", prompt)
		self.assertIn("Dates are day first.", prompt)
		self.assertIn("Description", prompt)
		self.assertIn("NO_IMAGE", prompt)
		self.assertNotIn("receipt-secret-name", prompt)
		self.assertLess(prompt.index("Never guess"), prompt.index("Dates are day first."))
		for img in client.call_args.kwargs["images"]:
			self.assertLessEqual(len(img["data_b64"]), 200_000 * 4 // 3 + 4)

	def test_no_free_slot_leaves_row_queued(self):
		rule = self._rule()
		row = self._row(rule)
		for key in worker.slot_keys():
			_redis_lock.claim(key, 60)
		client, fin = self._run(row)
		client.assert_not_called()
		fin.assert_not_called()
		got = self._get(row)
		self.assertEqual(got.status, "Queued")
		self.assertFalse(got.attempts)

	def test_slot_released_after_job_and_next_queued_row_enqueued(self):
		rule = self._rule()
		row = self._row(rule)
		later = self._row(rule, self._file("b.png"))
		client = mock.Mock(return_value=READING)
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", client),
			mock.patch("jarvis.intake.draft.finish"),
			mock.patch("frappe.enqueue") as enq,
		):
			worker.run_job(row.name)
		cache = frappe.cache()
		self.assertFalse(any(cache.exists(k) for k in worker.slot_keys()))
		kw = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.intake.worker.run_job")
		self.assertEqual((kw["activity"], kw["job_id"]), (later.name, f"intake:{later.name}"))
		self.assertTrue(kw["deduplicate"])

	def _chain(self, rows_after: int = 3, enqueued=()):
		"""Finish one job with ``rows_after`` more rows Queued; return the names chained."""
		rule = self._rule()
		first = self._row(rule)
		rest = [self._row(rule, self._file(f"c{i}.png")) for i in range(rows_after)]
		for i, r in enumerate(rest):
			frappe.db.set_value(ACTIVITY, r.name, "creation", add_to_date(now_datetime(), seconds=i + 1))
		skip = {rest[i].name for i in enqueued}
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", mock.Mock(return_value=READING)),
			mock.patch("jarvis.intake.draft.finish"),
			mock.patch("frappe.enqueue") as enq,
			mock.patch("jarvis.intake.worker.is_job_enqueued", side_effect=lambda jid: jid[7:] in skip),
		):
			worker.run_job(first.name)
		return [c.kwargs["activity"] for c in enq.call_args_list], [r.name for r in rest]

	def test_finished_job_starts_one_row_per_free_slot(self):
		chained, rest = self._chain()
		self.assertEqual(chained, rest[:2])  # two slots free, so two streams, oldest first

	def test_chain_skips_rows_that_are_already_in_the_queue(self):
		chained, rest = self._chain(enqueued=(0,))
		self.assertEqual(chained, rest[1:3])

	def test_pump_enqueues_old_queued_rows_only_when_a_slot_is_free(self):
		rule = self._rule()
		old = [self._row(rule, self._file(f"o{i}.png")) for i in range(3)]
		for i, r in enumerate(old):
			frappe.db.set_value(
				ACTIVITY,
				r.name,
				"creation",
				add_to_date(now_datetime(), minutes=-10 + i),
				update_modified=False,
			)
		fresh = self._row(rule, self._file("fresh.png"))
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		names = [c.kwargs["activity"] for c in enq.call_args_list]
		self.assertEqual(names, [old[0].name, old[1].name])
		self.assertNotIn(fresh.name, names)
		for key in worker.slot_keys():
			_redis_lock.claim(key, 60)
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		enq.assert_not_called()

	# ---- review fixes: who may run, which limit, what counts --------------------------

	def test_refused_target_fails_at_run_time_without_a_call(self):
		rule = self._rule()
		# A rule saved before the refused list grew, or edited around validation.
		frappe.db.set_value(TRIGGER, rule.name, "intake_target_doctype", "Role")
		row = self._row(rule)
		client, fin = self._run(row)
		client.assert_not_called()
		fin.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", "This rule's target is not allowed"))
		self.assertEqual(llm_action.daily_count(rule.name), 0)

	def test_uploader_without_create_permission_is_skipped_before_any_call(self):
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.intake.guards.frappe.has_permission", return_value=False):
			client, fin = self._run(row)
		client.assert_not_called()
		fin.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Skipped", "You can't create ToDo"))
		self.assertEqual(llm_action.daily_count(rule.name), 0)
		self.assertEqual(cint(frappe.cache().get_value(f"{worker.site_cap_key()}:count")), 0)

	def test_website_user_uploader_is_skipped_before_any_call(self):
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.intake.guards.is_desk_user", return_value=False):
			client, _ = self._run(row)
		client.assert_not_called()
		self.assertEqual(self._get(row).status, "Skipped")

	def test_per_user_cap_defers_and_names_the_limit(self):
		rule = self._rule()
		row = self._row(rule)
		llm_action.bump(worker.user_cap_key("Administrator"))
		with mock.patch.object(worker, "_user_cap", return_value=1):
			client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual(
			(got.status, got.summary),
			("Queued", "Waiting: daily limit reached for your uploads (resets tomorrow)"),
		)

	def test_zero_site_cap_pauses_intake_and_keeps_rows_queued(self):
		rule = self._rule()
		row = self._row(rule)
		before = frappe.db.count("Notification Log", {"subject": ["like", "Intake:%"]})
		with mock.patch.object(worker, "_site_cap", return_value=0):
			client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Queued", "Intake is paused on this site"))
		self.assertEqual(frappe.db.count("Notification Log", {"subject": ["like", "Intake:%"]}), before)

	def _three_waiting(self):
		rule = self._rule()
		rows = [self._row(rule, self._file(f"p{i}.png")) for i in range(3)]
		old = add_to_date(now_datetime(), minutes=-10)
		for i, r in enumerate(rows):
			frappe.db.set_value(
				ACTIVITY, r.name, "creation", add_to_date(old, seconds=i), update_modified=False
			)
		return rows

	def test_paused_site_starts_no_job_chain_and_resumes_when_unpaused(self):
		rows = self._three_waiting()
		with mock.patch.object(worker, "_site_cap", return_value=0):
			client, _ = self._run(rows[0])
			with mock.patch("frappe.enqueue") as enq:
				worker.run_job(rows[0].name)
				worker.run_job(rows[1].name)
				worker.pump()
				worker._enqueue_next("none")
			enq.assert_not_called()
		client.assert_not_called()
		for r in rows:
			self.assertEqual(self._get(r).status, "Queued")
		self.assertEqual(self._get(rows[0]).summary, worker.PAUSED_SUMMARY)
		# Un-paused: the pump starts the oldest rows again, and a finished job chains.
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		self.assertEqual([c.kwargs["activity"] for c in enq.call_args_list], [rows[0].name, rows[1].name])
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", mock.Mock(return_value=READING)),
			mock.patch("jarvis.intake.draft.finish"),
			mock.patch("frappe.enqueue") as enq,
			mock.patch("jarvis.intake.worker.is_job_enqueued", return_value=False),
		):
			worker.run_job(rows[0].name)
		self.assertEqual([c.kwargs["activity"] for c in enq.call_args_list], [rows[1].name, rows[2].name])

	def test_paused_site_does_not_work_a_row_with_a_saved_reading(self):
		rule = self._rule()
		row = self._row(rule, reading_json=json.dumps(READING))
		with mock.patch.object(worker, "_site_cap", return_value=0):
			_client, fin = self._run(row)
		fin.assert_not_called()
		self.assertEqual(self._get(row).status, "Queued")

	def test_zero_per_user_cap_means_no_per_user_limit(self):
		rule = self._rule()
		row = self._row(rule)
		for _i in range(3):
			llm_action.bump(worker.user_cap_key("Administrator"))
		with mock.patch.object(worker, "_user_cap", return_value=0):
			client, _ = self._run(row)
		client.assert_called_once()

	def test_unsaved_settings_read_as_defaults_and_zero_is_kept(self):
		with mock.patch("frappe.qb.from_") as q:
			q.return_value.select.return_value.where.return_value.run.return_value = []
			self.assertEqual(worker._setting("intake_daily_cap", 300), 300)
			q.return_value.select.return_value.where.return_value.run.return_value = [("0",)]
			self.assertEqual(worker._setting("intake_daily_cap", 300), 0)

	def test_deferrals_ring_one_bell_per_uploader_per_day(self):
		rule = self._rule(llm_daily_cap=1)
		llm_action.bump(llm_action.cap_key(rule.name))
		rows = [self._row(rule, self._file(f"w{i}.png")) for i in range(3)]
		before = frappe.db.count("Notification Log", {"subject": ["like", "Intake: daily limit%"]})
		for row in rows:
			self._run(row)
		after = frappe.db.count("Notification Log", {"subject": ["like", "Intake: daily limit%"]})
		self.assertEqual(after - before, 1)

	def test_pump_leaves_todays_waiting_rows_and_picks_them_up_tomorrow(self):
		rule = self._rule()
		waiting = self._row(rule, summary="Waiting: daily limit reached for the site (resets tomorrow)")
		fresh = self._row(rule, self._file("next.png"))
		old = add_to_date(now_datetime(), minutes=-10)
		for name in (waiting.name, fresh.name):
			frappe.db.set_value(ACTIVITY, name, "creation", old, update_modified=False)
		frappe.db.set_value(
			ACTIVITY, waiting.name, "creation", add_to_date(old, minutes=-1), update_modified=False
		)
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		self.assertEqual([c.kwargs["activity"] for c in enq.call_args_list], [fresh.name])
		# The day turns: the row was last touched yesterday.
		frappe.db.set_value(
			ACTIVITY, waiting.name, "modified", add_to_date(now_datetime(), days=-1), update_modified=False
		)
		with mock.patch("frappe.enqueue") as enq:
			worker.pump()
		self.assertIn(waiting.name, [c.kwargs["activity"] for c in enq.call_args_list])

	def test_usage_comes_from_the_rows_when_the_cache_counters_are_gone(self):
		rule = self._rule(llm_daily_cap=2)
		for i in range(2):
			self._row(rule, self._file(f"d{i}.png"), status="Success", read_on=frappe.utils.nowdate())
		# No Redis counter at all (a flush): the day's read_on stamps stand in for it.
		self.assertEqual(llm_action.daily_count(rule.name), 0)
		row = self._row(rule, self._file("late.png"))
		client, _ = self._run(row)
		client.assert_not_called()
		self.assertIn("Waiting:", self._get(row).summary)

	def test_a_flushed_counter_is_seeded_from_the_rows_when_it_is_recreated(self):
		rule = self._rule()
		today = frappe.utils.nowdate()
		for i in range(2):
			self._row(rule, self._file(f"s{i}.png"), status="Success", read_on=today)
		row = self._row(rule, self._file("s3.png"))
		# No Redis counters (a flush): the first call after it must not restart them at 1.
		worker._count_call(row)
		cache = frappe.cache()
		self.assertEqual(cint(cache.get_value(f"{worker.site_cap_key()}:count")), 3)
		self.assertEqual(cint(cache.get_value(f"{worker.user_cap_key('Administrator')}:count")), 3)
		self.assertEqual(llm_action.daily_count(rule.name), 3)
		worker._count_call(row)  # an existing counter just moves on by one
		self.assertEqual(cint(cache.get_value(f"{worker.site_cap_key()}:count")), 4)

	def test_a_database_error_loading_the_rule_leaves_the_row_queued(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.db.commit()  # the guard rolls back, as a real job does after a failed query
		for target in ("_load_rule", "_may_not_run"):
			with self.subTest(target=target):
				with (
					mock.patch.object(worker, target, side_effect=frappe.QueryTimeoutError("lock")),
					mock.patch("frappe.enqueue") as enq,
				):
					worker.run_job(row.name)
				enq.assert_not_called()
				self.assertEqual(self._get(row).status, "Queued")

	def test_a_real_read_stamps_read_on_and_counts_the_user(self):
		rule = self._rule()
		row = self._row(rule)
		self._run(row)
		self.assertEqual(str(frappe.db.get_value(ACTIVITY, row.name, "read_on")), frappe.utils.nowdate())
		self.assertEqual(cint(frappe.cache().get_value(f"{worker.user_cap_key('Administrator')}:count")), 1)

	def _failing(self, error):
		rule = self._rule()
		row = self._row(rule)
		client = mock.Mock(side_effect=error)
		with mock.patch("jarvis.intake.client.vision_task_complete", client), mock.patch("frappe.enqueue"):
			worker.run_job(row.name)
		return rule, row

	def test_error_after_the_request_reached_the_model_counts(self):
		rule, row = self._failing(lt.LLMTaskError("agent timed out"))
		self.assertEqual(llm_action.daily_count(rule.name), 1)
		self.assertEqual(cint(frappe.cache().get_value(f"{worker.site_cap_key()}:count")), 1)
		self.assertEqual(cint(frappe.cache().get_value(f"{worker.user_cap_key('Administrator')}:count")), 1)
		self.assertEqual(str(frappe.db.get_value(ACTIVITY, row.name, "read_on")), frappe.utils.nowdate())

	def test_error_before_the_request_left_the_site_is_not_counted(self):
		rule, row = self._failing(lt.not_sent(lt.LLMTaskError("llm-task request failed")))
		self.assertEqual(llm_action.daily_count(rule.name), 0)
		self.assertFalse(frappe.db.get_value(ACTIVITY, row.name, "read_on"))

	def test_old_plugin_404_is_not_counted(self):
		rule, row = self._failing(lt.LLMTaskNotAvailableError("404"))
		self.assertEqual(llm_action.daily_count(rule.name), 0)
		self.assertEqual(cint(frappe.cache().get_value(f"{worker.site_cap_key()}:count")), 0)

	def test_all_null_reading_fails_as_read_nothing(self):
		# The vision check already passed, so an all-null object means a blank or unclear
		# file: saying "the model can't read images" would send the admin the wrong way.
		rule = self._rule()
		row = self._row(rule)
		client, fin = self._run(row, reply={"description": None})
		fin.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", worker.READ_NOTHING_SUMMARY))
		self.assertFalse(got.reading_json)
		self.assertEqual(llm_action.daily_count(rule.name), 1)  # R23: the call was made

	def test_text_only_model_fails_before_the_call_with_intake_wording(self):
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.intake.reader.vision_available", return_value=False):
			client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual(
			(got.status, got.summary), ("Failed", "This site's AI model can't read images for intake yet.")
		)
		self.assertEqual(llm_action.daily_count(rule.name), 0)

	def test_vision_available_needs_a_multimodal_provider_and_the_toggle(self):
		for provider, toggle, expected in (
			("Anthropic", 1, True),
			("openai", None, True),
			("Anthropic", 0, False),
			("Ollama", 1, False),
			("", 1, False),
		):
			settings = frappe._dict(llm_provider=provider, vision_attachments_enabled=toggle)
			with (
				self.subTest(provider=provider, toggle=toggle),
				mock.patch("frappe.get_cached_doc", return_value=settings),
			):
				self.assertIs(_REAL_VISION_AVAILABLE(), expected)

	def test_error_before_the_claim_leaves_the_row_queued(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.db.commit()  # a real row is committed before its job runs; the job rolls back
		with mock.patch.object(worker, "_claim_slot", side_effect=ConnectionError("redis down")):
			client, fin = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.attempts), ("Queued", 0))

	def test_error_in_the_claim_leaves_the_row_queued_and_frees_the_slot(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.db.commit()  # a real row is committed before its job runs; the job rolls back
		with mock.patch.object(worker, "_claim_row", side_effect=RuntimeError("deadlock")):
			client, _ = self._run(row)
		client.assert_not_called()
		self.assertEqual(self._get(row).status, "Queued")
		cache = frappe.cache()
		self.assertFalse(any(cache.exists(k) for k in worker.slot_keys()))

	def test_older_in_flight_row_for_the_same_bytes_wins_after_the_claim(self):
		rule = self._rule()
		fdoc = self._file()
		older = self._row(rule, fdoc, status="Running")
		newer = self._row(rule, fdoc)
		bells = frappe.db.count("Notification Log", {"for_user": "Administrator"})
		client, _ = self._run(newer)
		client.assert_not_called()
		got = frappe.db.get_value(ACTIVITY, newer.name, ["status", "summary", "duplicate_of"], as_dict=True)
		self.assertEqual((got.status, got.duplicate_of), ("Skipped", older.name))
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": "Administrator"}), bells)

	def test_the_older_row_is_not_skipped_by_a_newer_in_flight_one(self):
		rule = self._rule()
		fdoc = self._file()
		older = self._row(rule, fdoc)
		self._row(rule, fdoc, status="Running")
		client, _ = self._run(older)
		client.assert_called_once()

	def test_worker_endings_write_back_to_the_source_record(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "scan holder"}).insert()
		self.addCleanup(frappe.delete_doc, "ToDo", todo.name, force=True, ignore_permissions=True)
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype="ToDo",
			intake_target_doctype="Note",
			intake_status_field="sender",
			intake_spec=json.dumps({"fields": ["title"]}),
		)
		row = self._row(rule, target_docname=todo.name, target_doctype="Note")
		self._run(row, reply={"error": "NO_IMAGE"})
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "sender"), "Failed")
		# A rule turned off ends Skipped, and the record says so.
		row2 = self._row(rule, self._file("z.png"), target_docname=todo.name, target_doctype="Note")
		frappe.db.set_value(TRIGGER, rule.name, "enabled", 0)
		self._run(row2)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "sender"), "Skipped")

	def test_a_duplicate_does_not_overwrite_the_records_status(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "scan holder"}).insert()
		self.addCleanup(frappe.delete_doc, "ToDo", todo.name, force=True, ignore_permissions=True)
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype="ToDo",
			intake_target_doctype="Note",
			intake_status_field="sender",
			intake_spec=json.dumps({"fields": ["title"]}),
		)
		fdoc = self._file()
		self._row(rule, fdoc, status="Running", target_docname=todo.name, target_doctype="Note")
		frappe.db.set_value("ToDo", todo.name, "sender", "Running")
		dup = self._row(rule, fdoc, target_docname=todo.name, target_doctype="Note")
		self._run(dup)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "sender"), "Running")

	def test_five_failures_in_an_hour_alert_the_managers_once(self):
		rule = self._rule()
		manager = "intake.manager@example.com"
		if not frappe.db.exists("User", manager):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": manager,
					"first_name": "Intake",
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}],
				}
			).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "User", manager, force=True, ignore_permissions=True)
		rows = [self._row(rule, self._file(f"f{i}.png")) for i in range(6)]
		before = frappe.db.count(
			"Notification Log", {"for_user": manager, "subject": ["like", "Document Intake: % files failed%"]}
		)
		err = mock.Mock(side_effect=lt.LLMTaskError("agent down"))
		with mock.patch("jarvis.intake.client.vision_task_complete", err), mock.patch("frappe.enqueue"):
			for row in rows:
				worker.run_job(row.name)
		alerts = frappe.get_all(
			"Notification Log",
			filters={"for_user": manager, "subject": ["like", "Document Intake: % files failed%"]},
			pluck="name",
		)
		self.assertEqual(len(alerts) - before, 1)

	def test_expected_read_failures_log_once_an_hour_under_a_named_class(self):
		rule = self._rule()
		rows = [self._row(rule, self._file(f"e{i}.png")) for i in range(3)]
		err = mock.Mock(side_effect=lt.LLMTaskError("agent down"))
		cache = frappe.cache()
		cache.delete(cache.make_key("jarvis:intake:errlog:Jarvis intake: read failed"))
		with (
			mock.patch("jarvis.intake.client.vision_task_complete", err),
			mock.patch("frappe.enqueue"),
			mock.patch("frappe.log_error") as log,
		):
			for row in rows:
				worker.run_job(row.name)
		titles = [c.kwargs.get("title") for c in log.call_args_list]
		self.assertEqual(titles.count("Jarvis intake: read failed"), 1)

	def test_recover_failure_logs_the_traceback_not_just_the_row_name(self):
		rule = self._rule()
		row = self._stuck(rule, 1)
		with (
			mock.patch.object(worker, "_enqueue", side_effect=RuntimeError("queue down")),
			mock.patch("frappe.log_error") as log,
		):
			worker._recover_stuck()
		message = log.call_args.kwargs["message"]
		self.assertIn(row.name, message)
		self.assertIn("RuntimeError", message)

	def test_pump_does_nothing_for_new_rows_when_no_intake_rule_is_enabled(self):
		with (
			mock.patch.object(capture, "intake_map", return_value={"folder": {}, "record": {}}),
			mock.patch.object(worker, "_waiting_rows") as waiting,
			mock.patch.object(worker, "_recover_stuck") as recover,
		):
			worker.pump()
		waiting.assert_not_called()
		recover.assert_called_once()  # a row stuck Running is still rescued
