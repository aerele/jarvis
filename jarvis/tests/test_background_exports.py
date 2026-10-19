"""Background-export boundary tests, including real files and MariaDB streams."""

import csv
import datetime
import inspect
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase, expectedFailure
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._session import impersonate
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import jobs, masking, resolvers, saved_queries, streaming
from jarvis.tools._export.database import MariaDBStream, Watchdog, connection_settings
from jarvis.tools._export.resolvers import _Column


def prepared(limit=None, rules=None):
	return streaming.Prepared(
		"",
		{},
		[_Column("value", None, None)],
		["Value"],
		{},
		["Data"],
		rules or [],
		"yyyy-mm-dd",
		"hh:mm:ss",
		limit,
	)


class TestStreamingWriter(TestCase):
	def test_csv_200000_rows_and_no_database_calls(self):
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(frappe.db, "sql", side_effect=AssertionError("STREAM queried DB")),
		):
			path = Path(directory) / "export.part"
			result = streaming.write(
				prepared(),
				({"value": i} for i in range(200_000)),
				path,
				"csv",
				200_000,
				50 * 1024 * 1024,
				lambda: None,
				lambda n: None,
			)
			self.assertEqual(result["rows_written"], 200_000)
			with path.open() as source:
				self.assertEqual(sum(1 for _ in csv.reader(source)), 200_001)

	def test_exact_cap_and_over_cap(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "export.part"
			with self.assertRaisesRegex(streaming.ExportLimit, "More than 2"):
				streaming.write(
					prepared(), iter([{"value": 1}] * 3), path, "csv", 2, 1000, lambda: None, lambda n: None
				)

	def test_explicit_top_n_and_formula_escape(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "export.part"
			result = streaming.write(
				prepared(limit=1),
				iter([{"value": "=HYPERLINK(1)"}, {"value": 2}]),
				path,
				"csv",
				2,
				1000,
				lambda: None,
				lambda n: None,
			)
			self.assertEqual(result["limited_by_spec"], 1)
			self.assertIn("'=HYPERLINK", path.read_text())

	def test_file_size_refuses(self):
		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaises(streaming.ExportLimit):
				streaming.write(
					prepared(),
					iter([{"value": "a" * 500}]),
					Path(directory) / "export.part",
					"csv",
					2,
					100,
					lambda: None,
					lambda n: None,
				)

	def test_xlsx_types_sanitization_and_highlights(self):
		from openpyxl import load_workbook

		from jarvis._xlsx_highlights import ExcelHighlight

		p = prepared(rules=[ExcelHighlight(0, "eq", "ok", "success", "cell")])
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(frappe.db, "sql", side_effect=AssertionError("STREAM queried DB")),
		):
			path = Path(directory) / "export.part"
			result = streaming.write(
				p,
				iter([{"value": "<p>=1+1</p>"}, {"value": "ok"}]),
				path,
				"xlsx",
				200_000,
				50 * 1024 * 1024,
				lambda: None,
				lambda n: None,
			)
			with path.open("rb") as source:
				workbook = load_workbook(source)
				self.assertTrue(workbook.active["A2"].value.startswith("'"))
				self.assertEqual(workbook.active["A2"].value[1:].strip(), "=1+1")
				self.assertEqual(workbook.active["A2"].data_type, "s")
				self.assertEqual(len(workbook.active.conditional_formatting), 1)
				workbook.close()
			self.assertEqual(result["rows_written"], 2)


class TestSavedQueries(FrappeTestCase):
	def test_owner_expiry_and_no_rows(self):
		frappe.set_user("Administrator")
		spec = {"from": "ToDo", "limit": 2, "offset": 3}
		key = saved_queries.save(spec)
		stored = frappe.cache().get_value("jarvis:query:" + key)
		self.assertEqual(stored["spec"], {"from": "ToDo", "select": ["name"]})
		self.assertNotIn("rows", stored)
		self.assertEqual(spec["limit"], 2)
		with (
			impersonate("Guest"),
			self.assertRaisesRegex(InvalidArgumentError, "not found"),
		):
			saved_queries.resolve(key)
		frappe.cache().delete_value("jarvis:query:" + key)
		with self.assertRaisesRegex(InvalidArgumentError, "expired"):
			saved_queries.resolve(key)

	def test_partial_only_ids_at_row_and_character_limits(self):
		from jarvis.tools._result_guard import enforce_result_budget
		from jarvis.tools.query import query

		with patch("jarvis.tools.query.compile_spec") as compile_query:
			q = compile_query.return_value
			q.limit.return_value = q
			q.get_sql.return_value = "SELECT"
			q.run.return_value = [{"name": "one"}]
			self.assertNotIn("result_id", query({"from": "ToDo", "limit": 2}))
			q.run.return_value = [{"name": "one"}] * 3
			with patch("jarvis.tools.query.DEFAULT_LIMIT", 2):
				result = query({"from": "ToDo"})
			self.assertIn("result_id", result)
			self.assertEqual(len(result["rows"]), 2)
			q.run.return_value = [{"name": "x" * 40_000}]
			result = query({"from": "ToDo", "limit": 2})
			guarded, _ = enforce_result_budget(result, "query")
			self.assertEqual(guarded["result_id"], result["result_id"])
			self.assertEqual(saved_queries.resolve(result["result_id"])["limit"], 2)

	def test_explicit_top_n_is_complete_even_when_more_rows_match(self):
		from jarvis.tools.query import query

		with patch("jarvis.tools.query.compile_spec") as compile_query:
			q = compile_query.return_value
			q.limit.return_value = q
			q.get_sql.return_value = "SELECT"
			q.run.return_value = [{"name": str(i)} for i in range(11)]
			result = query({"from": "ToDo", "limit": 10})
		self.assertEqual(len(result["rows"]), 10)
		self.assertTrue(result["more_rows"])
		self.assertNotIn("partial", result)
		self.assertNotIn("result_id", result)

	def test_result_id_survives_site_cache_clear_with_its_ttl(self):
		frappe.set_user("Administrator")
		key = saved_queries.save({"from": "ToDo"})
		try:
			frappe.clear_cache()
			self.assertEqual(saved_queries.resolve(key)["from"], "ToDo")
			ttl = frappe.cache().ttl(frappe.cache().make_key("jarvis:query:" + key))
			self.assertGreater(ttl, 0)
			self.assertLessEqual(ttl, saved_queries.TTL)
		finally:
			frappe.cache().delete_value("jarvis:query:" + key)


class TestBackgroundExportJobs(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.conversation = (
			frappe.get_doc({"doctype": "Jarvis Conversation", "title": "export integration test"})
			.insert(ignore_permissions=True)
			.name
		)
		self.names = []
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.names:
			if frappe.db.exists(jobs.DOCTYPE, name):
				job = frappe.get_doc(jobs.DOCTYPE, name)
				jobs._delete_file(job)
				frappe.db.delete(jobs.DOCTYPE, {"name": name})
		frappe.db.delete(
			"Notification Log", {"document_type": jobs.DOCTYPE, "document_name": ["in", self.names]}
		)
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conversation})
		frappe.db.delete("Jarvis Conversation", {"name": self.conversation})
		frappe.db.commit()
		super().tearDown()

	def job(self, **values):
		job = frappe.get_doc(
			{
				"doctype": jobs.DOCTYPE,
				"conversation": self.conversation,
				"source": json.dumps({"spec": {"from": "DocType", "select": ["name"], "limit": 2}}),
				"title": "Export test",
				"format": "csv",
				"status": "running",
				"attempt_token": "abc123",
				"heartbeat": jobs.now_datetime(),
				**values,
			}
		)
		job.flags.jarvis_server_write = True
		job.insert(ignore_permissions=True)
		self.names.append(job.name)
		frappe.db.commit()
		return job

	def test_real_stream_and_publish(self):
		job = self.job()
		with patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "ready", job.error)
		self.assertEqual(job.rows_written, 2)
		self.assertEqual(job.limited_by_spec, 2)
		self.assertTrue(jobs._file_exists(job))
		self.assertTrue(frappe.get_doc("File", job.file).is_private)
		jobs.announce(job.name)
		self.assertEqual(
			frappe.db.count("Jarvis Chat Message", {"tool_call_id": f"export:{job.name}:ready"}), 1
		)
		self.assertEqual(frappe.db.count("Notification Log", {"document_name": job.name}), 1)

	def test_no_limit_exports_publish_from_saved_id_and_filters(self):
		from openpyxl import load_workbook

		from jarvis.tools._export import resolvers
		from jarvis.tools.export_query import export_query

		# Real handoff, worker, publication, bytes and state; only RQ scheduling is stubbed.
		key = saved_queries.save({"from": "DocType", "select": ["name"], "limit": 1})
		try:
			for fmt, source in (
				("xlsx", {"result_id": key}),
				("csv", {"doctype": "DocType", "fields": ["name"]}),
			):
				with (
					self.subTest(format=fmt),
					patch.object(resolvers, "_HARD_ROW_CEILING", 2),
					patch.object(jobs, "current_conversation", return_value=self.conversation),
					patch.object(jobs, "dispatch"),
				):
					state = export_query(**source, format=fmt)
					name = state["export_job"]
					self.names.append(name)
					frappe.db.set_value(jobs.DOCTYPE, name, {"status": "running", "attempt_token": "abc123"})
					frappe.db.commit()
					jobs.run(name, "abc123")
					job = frappe.get_doc(jobs.DOCTYPE, name)
					self.assertEqual(job.status, "ready", job.error)
					self.assertEqual(job.limited_by_spec, 0)
					self.assertGreater(job.rows_written, 2)
					file = frappe.get_doc("File", job.file)
					self.assertRegex(file.file_name, r"jarvis-export-Export-[0-9a-f]{6}\." + fmt)
					if fmt == "csv":
						with open(file.get_full_path()) as handle:
							self.assertEqual(sum(1 for _ in csv.reader(handle)), job.rows_written + 1)
					else:
						book = load_workbook(file.get_full_path(), read_only=True)
						try:
							self.assertEqual(sum(1 for _ in book.active.rows), job.rows_written + 1)
						finally:
							book.close()
		finally:
			frappe.cache().delete_value("jarvis:query:" + key)

	def test_cancel_before_worker_start_has_cancelled_chat_and_bell(self):
		for status in ("queued", "paused"):
			with self.subTest(status=status):
				job = self.job(status=status)
				with patch.object(jobs, "dispatch"):
					result = jobs.cancel(job.name)
				self.assertEqual(result["status"], "cancelled")
				self.assertEqual(result["error"], "Export cancelled.")
				self.assertEqual(
					frappe.db.get_value(
						"Jarvis Chat Message", {"tool_call_id": f"export:{job.name}:cancelled"}, "content"
					),
					"Export cancelled.",
				)
				self.assertEqual(
					frappe.db.get_value("Notification Log", {"document_name": job.name}, "subject"),
					"Export cancelled.",
				)

	def test_dispatch_unknown_or_stale_registry_but_not_confirmed_absence(self):
		for count, stale, expected in ((None, False, 1), (0, True, 1), (0, False, 0)):
			with self.subTest(count=count, stale=stale):
				job = self.job(status="queued")
				with (
					patch("jarvis.chat.pump._probe_worker_count", return_value=count),
					patch("jarvis.chat.pump._registry_is_stale", return_value=stale),
					patch("frappe.enqueue") as enqueue,
				):
					jobs.dispatch()
				self.assertEqual(enqueue.call_count, expected)
				self.assertEqual(job.reload().status, "running" if expected else "queued")
				frappe.db.delete(jobs.DOCTYPE, {"name": job.name})
				frappe.db.commit()

	def test_failed_cancellation_commit_rolls_back_state(self):
		job = self.job(status="queued")
		commit = frappe.db.commit
		calls = 0

		def fail_cancellation():
			nonlocal calls
			calls += 1
			if calls == 2:
				raise RuntimeError("commit failed")
			return commit()

		with (
			patch.object(frappe.db, "commit", side_effect=fail_cancellation),
			self.assertRaisesRegex(RuntimeError, "commit failed"),
		):
			jobs.cancel(job.name)
		self.assertEqual(job.reload().status, "queued")
		self.assertFalse(job.cancel_requested)

	def test_failed_notification_rolls_back_message_and_card(self):
		job = self.job()
		with (
			patch.object(jobs, "_commit", side_effect=[None, RuntimeError("commit failed")]),
			self.assertRaisesRegex(RuntimeError, "commit failed"),
		):
			jobs.announce(job.name, initial=True)
		self.assertFalse(frappe.db.exists("Jarvis Chat Message", {"tool_call_id": f"export:{job.name}:card"}))
		self.assertFalse(job.reload().card_message)

	def test_stale_attempt_cannot_change_state(self):
		job = self.job()
		jobs._finish(job.name, "stale", "failed", "wrong worker")
		self.assertEqual(job.reload().status, "running")
		jobs._finish(job.name, "abc123", "paused", "site update")
		self.assertEqual(job.reload().retries, 1)
		frappe.db.set_value(jobs.DOCTYPE, job.name, "status", "running")
		frappe.db.commit()
		jobs._finish(job.name, "abc123", "paused", "site update again")
		self.assertEqual(job.reload().status, "failed")

	def test_cancelled_attempt_cannot_publish(self):
		job = self.job(cancel_requested=1)
		with patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		self.assertEqual(job.reload().status, "cancelled")
		self.assertFalse(job.file)

	def test_owner_checks(self):
		job = self.job()
		with impersonate("Guest"):
			with self.assertRaises(frappe.PermissionError):
				jobs.get_jobs([job.name])
			with self.assertRaises(frappe.PermissionError):
				jobs.cancel(job.name)

	def test_jarvis_role_reads_only_owned_jobs(self):
		users = []
		job = self.job()
		try:
			for label in ("owner", "peer"):
				user = frappe.get_doc(
					{
						"doctype": "User",
						"email": f"export-{label}-{frappe.generate_hash(length=8)}@example.com",
						"first_name": label,
						"send_welcome_email": 0,
						"user_type": "System User",
						"roles": [{"role": "Jarvis User"}],
					}
				).insert(ignore_permissions=True)
				users.append(user.name)
			frappe.db.set_value(jobs.DOCTYPE, job.name, "owner", users[0])
			frappe.db.commit()
			job.reload()
			with impersonate(users[0]):
				self.assertTrue(frappe.has_permission(jobs.DOCTYPE, "read", doc=job))
				self.assertEqual(
					frappe.get_list(jobs.DOCTYPE, filters={"name": job.name}, pluck="name"), [job.name]
				)
			with impersonate(users[1]):
				self.assertFalse(frappe.has_permission(jobs.DOCTYPE, "read", doc=job))
				self.assertEqual(frappe.get_list(jobs.DOCTYPE, filters={"name": job.name}, pluck="name"), [])
				self.assertEqual(jobs.get_jobs([job.name]), {job.name: None})
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete(jobs.DOCTYPE, {"name": job.name})
			for user in users:
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_filter_prepare_runs_real_stream(self):
		p = streaming.prepare({"doctype": "DocType", "fields": ["name"], "filters": {"name": "ToDo"}}, 100)
		db = MariaDBStream(connection_settings()).open()
		try:
			with patch.object(frappe.db, "sql", side_effect=AssertionError("STREAM queried DB")):
				self.assertEqual(list(db.stream_rows(p.sql, p.params)), [{"name": "ToDo"}])
		finally:
			db.close()

	def test_watchdog_sees_cancellation_and_lock_waiters(self):
		job = self.job(cancel_requested=1)
		db = MariaDBStream(connection_settings()).open()
		watchdog = Watchdog(db, job.name, "abc123", frappe.local.site, job.owner, job.conversation)
		try:
			with self.assertRaisesRegex(streaming.ExportLimit, "cancelled"):
				watchdog._tick()
			frappe.db.set_value(jobs.DOCTYPE, job.name, "cancel_requested", 0)
			frappe.db.commit()
			with patch.object(db, "detect_lock_waiters", return_value=True):
				with self.assertRaisesRegex(streaming.ExportLimit, "site update"):
					watchdog._tick()
		finally:
			db.close()

	def test_cancel_kills_only_our_running_query(self):
		job = self.job(started_at=jobs.now_datetime())
		db = MariaDBStream(connection_settings()).open()
		watchdog = Watchdog(db, job.name, "abc123", frappe.local.site, job.owner, job.conversation)
		errors = []

		def slow_query():
			try:
				list(db.stream_rows("SELECT SLEEP(30)", {}))
			except Exception as error:
				errors.append(error)

		thread = threading.Thread(target=slow_query)
		try:
			thread.start()
			watchdog.start()
			frappe.db.set_value(jobs.DOCTYPE, job.name, "cancel_requested", 1)
			frappe.db.commit()
			thread.join(timeout=8)
			self.assertFalse(thread.is_alive(), "Cancellation did not kill the read")
			self.assertTrue(errors)
			self.assertEqual(watchdog.reason.status, "cancelled")
			self.assertEqual(frappe.db.sql("SELECT 1"), ((1,),))
		finally:
			watchdog.stop()
			db.close()
			thread.join(timeout=2)

	def test_over_cap_match_ends_too_large_not_truncated(self):
		# The query asks for one row past the cap; that extra row is how a match
		# larger than the cap is told apart from one that exactly fits.
		job = self.job(source=json.dumps({"spec": {"from": "DocType", "select": ["name"]}}))
		with (
			patch.object(jobs, "limits", return_value=(2, 50 * 1024 * 1024)),
			patch.object(jobs, "dispatch"),
		):
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "too large", job.error)
		self.assertFalse(job.file)
		self.assertFalse(
			frappe.db.exists("File", {"attached_to_doctype": jobs.DOCTYPE, "attached_to_name": job.name})
		)

	def test_time_cap_kills_our_running_query(self):
		job = self.job(started_at=jobs.now_datetime())
		db = MariaDBStream(connection_settings()).open()
		# No cancel request: only the ten-minute deadline (shortened to one second) may act.
		watchdog = Watchdog(db, job.name, "abc123", frappe.local.site, job.owner, job.conversation, seconds=1)
		errors = []

		def slow_query():
			try:
				list(db.stream_rows("SELECT SLEEP(30)", {}))
			except Exception as error:
				errors.append(error)

		thread = threading.Thread(target=slow_query)
		try:
			thread.start()
			watchdog.start()
			thread.join(timeout=8)
			self.assertFalse(thread.is_alive(), "The time cap did not kill the read")
			self.assertTrue(errors)
			self.assertEqual(watchdog.reason.status, "too slow")
			self.assertEqual(frappe.db.sql("SELECT 1"), ((1,),))
		finally:
			watchdog.stop()
			db.close()
			thread.join(timeout=2)

	def test_stream_session_sets_statement_time_limit(self):
		db = MariaDBStream(connection_settings(), seconds=7).open()
		try:
			with db.connection.cursor() as cursor:
				cursor.execute("SELECT @@SESSION.max_statement_time, @@SESSION.net_write_timeout")
				statement_time, write_timeout = cursor.fetchone()
		finally:
			db.close()
		self.assertEqual((float(statement_time), float(write_timeout)), (7.0, 7.0))

	def test_run_relies_on_the_ten_minute_defaults(self):
		# run() builds both objects without a `seconds` argument.
		for cls in (Watchdog, MariaDBStream):
			self.assertEqual(inspect.signature(cls).parameters["seconds"].default, 600, cls.__name__)

	def test_pause_schedules_retry_five_minutes_out(self):
		job = self.job()
		jobs._finish(job.name, "abc123", "paused", "x")
		job.reload()
		self.assertEqual(job.status, "paused")
		self.assertEqual(job.retries, 1)
		self.assertFalse(job.finished_at)
		due = jobs.now_datetime() + datetime.timedelta(minutes=5)
		self.assertLess(abs((frappe.utils.get_datetime(job.next_attempt) - due).total_seconds()), 15)
		# Re-armed and paused again: the one retry is spent.
		frappe.db.set_value(jobs.DOCTYPE, job.name, {"status": "running", "attempt_token": "abc123"})
		frappe.db.commit()
		jobs._finish(job.name, "abc123", "paused", "y")
		job.reload()
		self.assertEqual(job.status, "failed")
		self.assertIn("interrupted again", job.error)

	def test_dispatch_skips_a_retry_that_is_not_yet_due(self):
		# dispatch() takes the oldest eligible job site-wide, so assert on this job's row and calls.
		job = self.job(
			status="paused",
			attempt_token="",
			next_attempt=jobs.now_datetime() + datetime.timedelta(minutes=5),
		)

		def started():
			return [c for c in enqueue.call_args_list if c.kwargs.get("name") == job.name]

		with (
			patch("jarvis.chat.pump._probe_worker_count", return_value=3),
			patch.object(frappe, "enqueue") as enqueue,
		):
			jobs.dispatch()
			self.assertEqual(job.reload().status, "paused")
			self.assertEqual(started(), [])
			frappe.db.set_value(
				jobs.DOCTYPE,
				job.name,
				"next_attempt",
				jobs.now_datetime() - datetime.timedelta(seconds=1),
			)
			frappe.db.commit()
			jobs.dispatch()
			job.reload()
			self.assertEqual(job.status, "running")
			calls = started()
			self.assertEqual(len(calls), 1)
			self.assertEqual(calls[0].kwargs["job_id"], "export-" + job.attempt_token)

	def test_reaper_expires_file_and_preserves_card(self):
		job = self.job()
		with patch.object(jobs, "dispatch"):
			jobs.announce(job.name, initial=True)
			jobs.run(job.name, "abc123")
			job.reload()
			file_name = job.file
			frappe.db.set_value(
				jobs.DOCTYPE,
				job.name,
				"expires_at",
				frappe.utils.add_to_date(jobs.now_datetime(), days=-1),
			)
			frappe.db.commit()
			jobs.reap()
		self.assertEqual(job.reload().status, "expired")
		self.assertFalse(frappe.db.exists("File", file_name))
		self.assertTrue(frappe.db.exists("Jarvis Chat Message", job.card_message))
		stored = json.loads(frappe.db.get_value("Jarvis Chat Message", job.card_message, "tool_result"))
		self.assertEqual(stored["data"]["rows_written"], 2)

	def test_reaper_retries_worker_death_only_once(self):
		old = frappe.utils.add_to_date(jobs.now_datetime(), minutes=-2)
		job = self.job(heartbeat=old, started_at=old)
		with patch.object(jobs, "dispatch"):
			jobs.reap()
			self.assertEqual(job.reload().status, "queued")
			self.assertEqual(job.retries, 1)
			frappe.db.set_value(
				jobs.DOCTYPE, job.name, {"status": "running", "heartbeat": old, "attempt_token": "second"}
			)
			frappe.db.commit()
			jobs.reap()
		self.assertEqual(job.reload().status, "failed")

	def test_same_query_joins_job_and_highlights_do_not_collide(self):
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		with (
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			first = jobs.enqueue(source, "csv", "First")
			self.names.append(first["export_job"])
			second = jobs.enqueue(source, "csv", "Second")
		self.assertEqual(first["export_job"], second["export_job"])
		self.assertNotEqual(
			jobs._key(source, "xlsx", None),
			jobs._key(source, "xlsx", [{"column": "name", "operator": "eq", "value": "A"}]),
		)

	def test_conversation_deletion_revokes_attempt(self):
		job = self.job()
		jobs.on_conversation_trash(frappe.get_doc("Jarvis Conversation", self.conversation))
		frappe.db.commit()
		self.assertFalse(jobs._attempt(job.name, "abc123"))

	def test_reaper_removes_registered_file_after_job_disappears(self):
		job = self.job()
		with patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
			job.reload()
			file_name = job.file
			path = Path(frappe.get_doc("File", file_name).get_full_path())
			frappe.db.delete(jobs.DOCTYPE, {"name": job.name})
			frappe.db.commit()
			jobs.reap()
		self.assertFalse(frappe.db.exists("File", file_name))
		self.assertFalse(path.exists())

	def test_site_capacity_and_one_job_per_owner(self):
		from unittest.mock import MagicMock

		from frappe.utils.background_jobs import generate_qname

		first = self.job(status="queued")
		second = self.job(status="queued")
		other = self.job(status="queued", owner="Guest")
		# Document.insert assigns the current session owner even with ignore_permissions.
		frappe.db.set_value(jobs.DOCTYPE, other.name, "owner", "Guest")
		frappe.db.commit()
		worker = MagicMock()
		worker.queue_names.return_value = [generate_qname("long")]
		with (
			patch("frappe.utils.background_jobs.get_workers", return_value=[worker] * 3),
			patch("frappe.enqueue") as enqueue,
		):
			jobs.dispatch()
		self.assertEqual(enqueue.call_count, 2)
		self.assertEqual(first.reload().status, "running")
		self.assertEqual(second.reload().status, "queued")
		self.assertEqual(other.reload().status, "running")

	def test_ready_reuse_rechecks_permission_scope(self):
		from dataclasses import replace

		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		job = self.job(fingerprint=jobs._key(source, "csv", None))
		with patch.object(jobs, "dispatch"):
			jobs.run(job.name, "abc123")
		self.assertEqual(jobs.existing(source, "csv")["export_job"], job.name)
		changed = replace(streaming.prepare(source, 200_000), sql="Changed row-permission scope")
		with patch.object(jobs, "prepare", return_value=changed):
			self.assertIsNone(jobs.existing(source, "csv"))

	def test_export_query_handoff_and_saved_id(self):
		from jarvis.tools._export import resolvers
		from jarvis.tools.export_query import export_query

		key = saved_queries.save({"from": "DocType", "select": ["name"], "limit": 1})
		with (
			patch.object(resolvers, "_HARD_ROW_CEILING", 2),
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			state = export_query(result_id=key, format="csv")
		self.names.append(state["export_job"])
		job = frappe.get_doc(jobs.DOCTYPE, state["export_job"])
		self.assertEqual(job.status, "queued")
		self.assertNotIn("limit", json.loads(job.source)["spec"])
		with self.assertRaises(InvalidArgumentError):
			export_query(doctype="DocType", result_id=key)

	def test_upload_limit_hands_off_to_generated_file_budget(self):
		from jarvis.tools.export_query import export_query

		with (
			patch("frappe.utils.file_manager.get_max_file_size", return_value=1),
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			state = export_query(spec={"from": "DocType", "select": ["name"], "limit": 2}, format="csv")
		self.names.append(state["export_job"])
		self.assertEqual(state["status"], "queued")

	def test_literal_percent_filters_use_real_parameters(self):
		values = ["50% complete", "100%% literal", "%(value)s", "quote ' and slash \\"]
		docs = [frappe.get_doc({"doctype": "ToDo", "description": value}).insert() for value in values]
		frappe.db.commit()
		try:
			for doc, value in zip(docs, values, strict=True):
				for filter_value in (value, ["in", [value]]):
					with self.subTest(value=filter_value):
						p = streaming.prepare(
							{
								"doctype": "ToDo",
								"fields": ["name"],
								"filters": {
									"description": filter_value,
								},
							},
							100,
						)
						db = MariaDBStream(connection_settings()).open()
						try:
							self.assertEqual(list(db.stream_rows(p.sql, p.params)), [{"name": doc.name}])
						finally:
							db.close()
			filters = {"description": ["like", "50%complete"]}
			p = streaming.prepare({"doctype": "ToDo", "fields": ["name"], "filters": filters}, 100)
			db = MariaDBStream(connection_settings()).open()
			try:
				self.assertEqual(
					list(db.stream_rows(p.sql, p.params)), frappe.get_list("ToDo", filters=filters)
				)
			finally:
				db.close()
		finally:
			for doc in docs:
				frappe.delete_doc("ToDo", doc.name, force=True)
			frappe.db.commit()

	def test_heartbeat_uses_utc_even_when_database_and_site_clocks_differ(self):
		job = self.job(started_at=jobs.now_datetime())
		db = MariaDBStream(connection_settings()).open()
		try:
			with db.control.cursor() as cursor:
				cursor.execute("SET SESSION time_zone='+09:00'")
			with patch("frappe.utils.get_system_timezone", return_value="America/New_York"):
				Watchdog(db, job.name, "abc123", frappe.local.site, job.owner, job.conversation)._tick()
				frappe.db.commit()
				job.reload()
				self.assertLess(abs((jobs.now_datetime() - job.heartbeat).total_seconds()), 5)
				self.assertTrue(jobs.public(job)["started_at"].endswith("+00:00"))
				with patch.object(jobs, "dispatch"):
					jobs.reap()
				self.assertEqual(job.reload().status, "running")
				self.assertEqual(job.retries, 0)
		finally:
			db.close()

	def test_expiry_selection_does_not_use_database_clock(self):
		job = self.job(
			status="ready",
			expires_at=frappe.utils.add_to_date(jobs.now_datetime(), seconds=-1),
			announced_status="ready",
		)
		jobs.announce(job.name, initial=True)
		previous = frappe.db.sql("SELECT @@session.time_zone")[0][0]
		try:
			frappe.db.sql("SET SESSION time_zone='-12:00'")
			with patch.object(jobs, "dispatch"):
				jobs.reap()
			self.assertEqual(job.reload().status, "expired")
		finally:
			frappe.db.sql("SET SESSION time_zone=%s", (previous,))

	def test_cross_chat_join_delivery_and_deletion(self):
		from jarvis.tools.export_query import export_query

		other = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "joined export"}).insert()
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		try:
			with (
				patch.object(jobs, "dispatch"),
				patch.object(jobs, "current_conversation", return_value=self.conversation),
			):
				first = jobs.enqueue(source, "csv")
			self.names.append(first["export_job"])
			with patch.object(jobs, "current_conversation", return_value=other.name):
				second = export_query(spec=source["spec"], format="csv")
			self.assertEqual(first["export_job"], second["export_job"])
			name = first["export_job"]
			self.assertEqual(len(jobs._cards(name)), 2)
			frappe.db.set_value(jobs.DOCTYPE, name, {"status": "running", "attempt_token": "abc123"})
			frappe.db.commit()
			# Keep the worker's original snapshot, then delete the original chat.
			original = frappe.get_doc(jobs.DOCTYPE, name)
			frappe.delete_doc("Jarvis Conversation", self.conversation, force=True, ignore_permissions=True)
			frappe.db.commit()
			self.assertEqual(frappe.db.get_value(jobs.DOCTYPE, name, "conversation"), other.name)
			with tempfile.TemporaryDirectory() as directory:
				path = Path(directory) / "export.part"
				path.write_text("Name\nToDo\n")
				jobs._publish_file(original, "abc123", path, {"rows_written": 1}, "permission")
			jobs.announce(name)
			jobs.announce(name)
			self.assertEqual(
				frappe.db.count(
					"Jarvis Chat Message",
					{
						"conversation": other.name,
						"tool_call_id": f"export:{name}:ready",
					},
				),
				1,
			)
			self.assertEqual(frappe.db.count("Notification Log", {"document_name": name}), 1)
			file_name = frappe.db.get_value(jobs.DOCTYPE, name, "file")
			self.assertTrue(file_name)
			frappe.delete_doc("Jarvis Conversation", other.name, force=True, ignore_permissions=True)
			frappe.db.commit()
			self.assertFalse(frappe.db.exists(jobs.DOCTYPE, name))
			self.assertFalse(frappe.db.exists("File", file_name))
		finally:
			frappe.db.delete("Jarvis Chat Message", {"conversation": other.name})
			frappe.db.delete("Jarvis Conversation", {"name": other.name})
			frappe.db.commit()

	def test_ready_join_delivers_once_to_each_chat(self):
		other = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "ready export"}).insert()
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		job = self.job(fingerprint=jobs._key(source, "csv", None))
		try:
			with patch.object(jobs, "dispatch"):
				jobs.announce(job.name, initial=True)
				jobs.run(job.name, "abc123")
			# Join after fanout selected its recipients, then interrupt the join
			# before completion. The old fanout must not clear this pending delivery.
			announce_one = jobs._announce_one

			def subscribe_during_delivery(*args, **kwargs):
				announce_one(*args, **kwargs)
				announce_one(job.name, other.name, initial=True, recovery=False)

			with patch.object(jobs, "_announce_one", side_effect=subscribe_during_delivery):
				jobs.announce(job.name)
			self.assertEqual(job.reload().announced_status, "")
			with patch.object(jobs, "dispatch"):
				jobs.reap()
			with patch.object(jobs, "current_conversation", return_value=other.name):
				for _ in range(2):
					self.assertEqual(jobs.existing(source, "csv", join=True)["export_job"], job.name)
			for conversation in (self.conversation, other.name):
				for suffix in ("card", "ready"):
					self.assertEqual(
						frappe.db.count(
							"Jarvis Chat Message",
							{
								"conversation": conversation,
								"tool_call_id": f"export:{job.name}:{suffix}",
							},
						),
						1,
					)
			self.assertEqual(frappe.db.count("Notification Log", {"document_name": job.name}), 1)
		finally:
			frappe.db.delete("Jarvis Chat Message", {"conversation": other.name})
			frappe.db.delete("Jarvis Conversation", {"name": other.name})
			frappe.db.commit()

	def test_dispatch_finds_eligible_owner_beyond_200_waiting_jobs(self):
		from unittest.mock import MagicMock

		from frappe.utils.background_jobs import generate_qname

		self.job(status="running")
		template = self.job(status="queued")
		# Insert cheap copies of the validated fixture to exercise the real SQL LIMIT.
		for _ in range(200):
			name = frappe.generate_hash(length=10)
			frappe.db.sql(
				"INSERT INTO `tabJarvis Export Job` (name,owner,creation,status,conversation) "
				"SELECT %s,owner,creation,status,conversation FROM `tabJarvis Export Job` WHERE name=%s",
				(name, template.name),
			)
			self.names.append(name)
		eligible = self.job(status="queued")
		frappe.db.set_value(jobs.DOCTYPE, eligible.name, "owner", "Guest")
		frappe.db.commit()
		worker = MagicMock()
		worker.queue_names.return_value = [generate_qname("long")]
		with (
			patch("frappe.utils.background_jobs.get_workers", return_value=[worker] * 3),
			patch("frappe.enqueue") as enqueue,
		):
			jobs.dispatch()
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(eligible.reload().status, "running")
		self.assertEqual(template.reload().status, "queued")

	def test_state_fetch_batches_jobs_and_file_records(self):
		first, second = self.job(), self.job()
		with patch.object(jobs, "dispatch"):
			jobs.run(first.name, "abc123")
			jobs.run(second.name, "abc123")
		with patch.object(frappe, "get_list", wraps=frappe.get_list) as get_list:
			states = jobs.get_jobs([first.name, second.name, "missing"])
		self.assertEqual(states[first.name]["status"], "ready")
		self.assertEqual(states[second.name]["status"], "ready")
		self.assertIsNone(states["missing"])
		self.assertEqual([c.args[0] for c in get_list.call_args_list], [jobs.DOCTYPE, "File"])

	def test_kill_switch_refuses_enqueue_and_holds_the_queue(self):
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		with patch.dict(frappe.conf, {"jarvis_background_export_disabled": 1}):
			with self.assertRaisesRegex(jobs.BackgroundUnavailable, "turned off"):
				jobs.enqueue(source, "csv")
			# A queued job waits: dispatch() reserves nothing while the switch is on.
			queued = self.job(status="queued", attempt_token="")
			with (
				patch("jarvis.chat.pump._probe_worker_count", return_value=3),
				patch.object(frappe, "enqueue") as enqueue,
			):
				jobs.dispatch()
			self.assertEqual(queued.reload().status, "queued")
			self.assertEqual([c for c in enqueue.call_args_list if c.kwargs.get("name") == queued.name], [])
			# An attempt reserved just before the flip goes back to the queue, unspent.
			reserved = self.job()
			with patch.object(jobs, "dispatch"):
				jobs.run(reserved.name, "abc123")
			reserved.reload()
			self.assertEqual(reserved.status, "queued")
			self.assertEqual(reserved.attempt_token or "", "")
			self.assertEqual((reserved.retries or 0, reserved.attempts or 0), (0, 0))
		# Without the switch the same queued job is reserved.
		with (
			patch("jarvis.chat.pump._probe_worker_count", return_value=3),
			patch.object(frappe, "enqueue") as enqueue,
		):
			jobs.dispatch()
		self.assertEqual(len([c for c in enqueue.call_args_list if c.kwargs.get("name") == queued.name]), 1)
		self.assertEqual(queued.reload().status, "running")

	def test_kill_switch_string_zero_is_off(self):
		# bench set-config without -p stores "0" as a string; it must not disable exports.
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		with (
			patch.dict(frappe.conf, {"jarvis_background_export_disabled": "0"}),
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			state = jobs.enqueue(source, "csv")
		self.names.append(state["export_job"])
		self.assertEqual(state["status"], "queued")

	def test_xlsx_needs_xlsxwriter_but_csv_does_not(self):
		source = {"spec": {"from": "DocType", "select": ["name"], "limit": 2}}
		with (
			patch.object(jobs, "_xlsxwriter_available", return_value=False),
			patch.object(jobs, "current_conversation", return_value=self.conversation),
			patch.object(jobs, "dispatch"),
		):
			with self.assertRaisesRegex(jobs.BackgroundUnavailable, "export it as CSV"):
				jobs.enqueue(source, "xlsx")
			state = jobs.enqueue(source, "csv")
			self.names.append(state["export_job"])
			self.assertEqual(state["status"], "queued")
			# A job queued before the package vanished fails with the same text, not a retry loop.
			job = self.job(format="xlsx")
			jobs.run(job.name, "abc123")
		job.reload()
		self.assertEqual(job.status, "failed")
		self.assertIn("export it as CSV", job.error)

	def test_refused_handoff_keeps_the_narrowing_advice(self):
		from jarvis.tools.export_query import export_query

		real_has_permission = frappe.has_permission

		def no_job_access(doctype=None, *args, **kwargs):
			if doctype == jobs.DOCTYPE:
				return False
			return real_has_permission(doctype, *args, **kwargs)

		busy = InvalidArgumentError("Exports are busy; please try again shortly.")
		unsupported = InvalidArgumentError("x does not support background streaming")
		# (label, a patch that makes the background route refuse, the reason the agent must see)
		blockers = [
			(
				"no job access",
				lambda: patch.object(frappe, "has_permission", side_effect=no_job_access),
				"role cannot",
			),
			(
				"no chat",
				lambda: patch.object(jobs, "current_conversation", return_value=None),
				"Start it from a Jarvis chat",
			),
			(
				"kill switch",
				lambda: patch.dict(frappe.conf, {"jarvis_background_export_disabled": 1}),
				"turned off",
			),
			("busy", lambda: patch.object(jobs, "_lock", side_effect=busy), "Exports are busy"),
			(
				"prepare",
				lambda: patch.object(jobs, "prepare", side_effect=unsupported),
				"background streaming",
			),
		]
		spec = {"from": "DocType", "select": ["name"]}
		for label, blocker, reason in blockers:
			with (
				self.subTest(blocker=label),
				patch.object(resolvers, "_HARD_ROW_CEILING", 1),
				patch.object(jobs, "current_conversation", return_value=self.conversation),
				patch.object(jobs, "dispatch"),
				blocker(),
				self.assertRaises(InvalidArgumentError) as ctx,
			):
				export_query(spec=spec, format="csv")
			self.assertNotIsInstance(ctx.exception, frappe.PermissionError)
			self.assertIn("Narrow", str(ctx.exception))
			self.assertIn(reason, str(ctx.exception))
		# The upload-size route carries its own advice too.
		with (
			patch("frappe.utils.file_manager.get_max_file_size", return_value=1),
			patch.object(jobs, "current_conversation", return_value=None),
			self.assertRaises(InvalidArgumentError) as ctx,
		):
			export_query(spec={"from": "DocType", "select": ["name"], "limit": 2}, format="csv")
		self.assertIn("Narrow", str(ctx.exception))
		self.assertIn("Start it from a Jarvis chat", str(ctx.exception))


class TestBackgroundFilterPermissions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.tests.test_query import TestQueryPermlevelFieldACL

		cls.fixture = TestQueryPermlevelFieldACL
		cls.fixture.setUpClass()

	def test_child_rows_obey_parent_record_scope_and_parenttype(self):
		f = self.fixture
		for parent, role, user, expected in (
			(f.SCOPE_PARENT_A, f.ROLE_SCOPE_A, f.USER_SCOPE_A, [10, 20, 30]),
			(f.SCOPE_SINGLE, f.ROLE_SCOPE_SINGLE, f.USER_SCOPE_SINGLE, [f.SINGLE_VAL]),
		):
			frappe.set_user("Administrator")
			perm = frappe.db.get_value(
				"DocPerm", {"parent": parent, "role": role}, ["name", "export"], as_dict=True
			)
			try:
				frappe.db.set_value("DocPerm", perm.name, "export", 1)
				frappe.db.commit()
				frappe.clear_cache(doctype=parent)
				frappe.set_user(user)
				p = streaming.prepare(
					{"doctype": f.SCOPE_CHILD_DT, "parent_doctype": parent, "fields": ["scope_val"]}, 200_000
				)
				db = MariaDBStream(connection_settings()).open()
				try:
					self.assertEqual(
						sorted(r["scope_val"] for r in db.stream_rows(p.sql, p.params)), expected
					)
				finally:
					db.close()
			finally:
				frappe.set_user("Administrator")
				frappe.db.set_value("DocPerm", perm.name, "export", perm.export)
				frappe.db.commit()
				frappe.clear_cache(doctype=parent)


class TestPreparedSqlFetchesOnePastTheCap(FrappeTestCase):
	"""A match larger than the cap is told apart from one that exactly fits by one extra row."""

	CAP = 2

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def assert_fetches_cap_plus_one(self, sql):
		# Frappe 15's legacy compiler writes lowercase "limit 3"; Frappe 16's builder "LIMIT 3".
		self.assertRegex(sql, re.compile(rf"\blimit\s+{self.CAP + 1}\b", re.I))

	def test_spec_sql_fetches_one_row_past_the_cap(self):
		p = streaming.prepare({"spec": {"from": "DocType", "select": ["name"]}}, self.CAP)
		self.assert_fetches_cap_plus_one(p.sql)

	def test_filter_sql_fetches_one_row_past_the_cap(self):
		p = streaming.prepare({"doctype": "DocType", "fields": ["name"]}, self.CAP)
		self.assert_fetches_cap_plus_one(p.sql)


class TestExportGateParity(FrappeTestCase):
	"""The immediate and the background export must refuse the same input in the same words.

	Each path is asserted on its own, against the text the immediate path gives today, so
	deleting a check from one path cannot hide behind the other (a deleted DocType check makes
	both fail later and alike, which an equality test would accept). The cases where the
	background path words it differently today are expected failures; the refactor that gives
	both paths one gate removes the decorator, and an unexpected success fails the run."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def immediate(self, **source):
		return resolvers.from_query(**source)

	def background(self, **source):
		return streaming.prepare(source, 10)

	def refused(self, call, error, text):
		with self.assertRaisesRegex(error, text):
			call()

	# Unknown DocType: the background path fails later, in get_meta.
	def test_unknown_doctype_immediate(self):
		self.refused(
			lambda: self.immediate(doctype="JV No Such", fields=["name"]),
			InvalidArgumentError,
			"unknown DocType: JV No Such",
		)

	@expectedFailure
	def test_unknown_doctype_background(self):
		self.refused(
			lambda: self.background(doctype="JV No Such", fields=["name"]),
			InvalidArgumentError,
			"unknown DocType: JV No Such",
		)

	# fields=["*"]: the background path words it for its own plain-field rule.
	def test_star_fields_immediate(self):
		self.refused(
			lambda: self.immediate(doctype="ToDo", fields=["*"]),
			InvalidArgumentError,
			"name the columns you want",
		)

	@expectedFailure
	def test_star_fields_background(self):
		self.refused(
			lambda: self.background(doctype="ToDo", fields=["*"]),
			InvalidArgumentError,
			"name the columns you want",
		)

	# A child table without its parent: the background path has a shorter text with no hint.
	def test_child_table_without_parent_immediate(self):
		self.refused(
			lambda: self.immediate(doctype="DocField", fields=["fieldname"]),
			InvalidArgumentError,
			"is a child table; pass parent_doctype",
		)

	@expectedFailure
	def test_child_table_without_parent_background(self):
		self.refused(
			lambda: self.background(doctype="DocField", fields=["fieldname"]),
			InvalidArgumentError,
			"is a child table; pass parent_doctype",
		)

	# The cases below already agree; they stay plain tests through the refactor.
	def test_no_export_permission_on_both_paths(self):
		with patch("frappe.permissions.can_export", return_value=False):
			for call in (
				lambda: self.immediate(doctype="ToDo", fields=["name"]),
				lambda: self.background(doctype="ToDo", fields=["name"]),
			):
				self.refused(call, PermissionDeniedError, "no export permission on ToDo")

	def test_no_read_permission_on_both_paths(self):
		with patch.object(frappe, "has_permission", return_value=False):
			for call in (
				lambda: self.immediate(doctype="ToDo", fields=["name"]),
				lambda: self.background(doctype="ToDo", fields=["name"]),
			):
				self.refused(call, PermissionDeniedError, "no read permission on ToDo")

	def test_spec_without_export_permission_on_both_paths(self):
		spec = {"from": "ToDo", "select": ["name"]}
		with patch("frappe.permissions.can_export", return_value=False):
			self.refused(
				lambda: resolvers.from_spec(spec), PermissionDeniedError, "no export permission on ToDo"
			)
			self.refused(
				lambda: streaming.prepare({"spec": spec}, 10),
				PermissionDeniedError,
				"no export permission on ToDo",
			)

	def test_computed_column_over_a_masked_field_on_both_paths(self):
		# Patched, so it runs on Frappe 15 (no masking there) as well as 16.
		spec = {
			"from": "ToDo",
			"alias": "m",
			"select": [{"expr": {"fn": "upper", "args": [{"field": "m.description"}]}, "as": "shout"}],
		}
		masked = {"m": {"description": object()}}
		with patch.object(masking, "masked_fields", return_value=masked):
			self.refused(
				lambda: resolvers.from_spec(spec), InvalidArgumentError, r"computes over m\.description"
			)
			self.refused(
				lambda: streaming.prepare({"spec": spec}, 10),
				InvalidArgumentError,
				r"computes over m\.description",
			)
