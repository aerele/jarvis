"""Intake worker: guards, limits, one-call read, retry reuse and slot handling."""

import io
import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis import _redis_lock
from jarvis.intake import capture, reader, spec, worker
from jarvis.triggers import engine, llm_action
from jarvis.triggers import llm_task_client as lt

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"
READING = {"description": "buy milk"}


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
		llm_action_key = llm_action._cap_key(rule.name)
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

	def test_daily_cap_skips(self):
		rule = self._rule(llm_daily_cap=2)
		llm_action._bump(llm_action._cap_key(rule.name))
		llm_action._bump(llm_action._cap_key(rule.name))
		row = self._row(rule)
		client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Skipped", "daily limit reached"))

	def test_site_cap_skips(self):
		rule = self._rule()
		row = self._row(rule)
		llm_action._bump(worker.site_cap_key())
		with mock.patch.object(worker, "_site_cap", return_value=1):
			client, _ = self._run(row)
		client.assert_not_called()
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Skipped", "daily limit reached"))

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

	def test_old_plugin_maps_to_update_copy(self):
		rule = self._rule()
		row = self._row(rule)
		client = mock.Mock(side_effect=lt.LLMTaskNotAvailableError("404"))
		with mock.patch("jarvis.intake.client.vision_task_complete", client), mock.patch("frappe.enqueue"):
			worker.run_job(row.name)
		got = self._get(row)
		self.assertEqual((got.status, got.summary), ("Failed", llm_action._NO_AGENT_UPDATE_MESSAGE))

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
		self.assertEqual(log.call_args.kwargs["title"], "jarvis.intake.worker")

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
