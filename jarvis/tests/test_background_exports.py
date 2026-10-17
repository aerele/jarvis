"""Background-export boundary tests, including real files and MariaDB streams."""

import csv
import datetime
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._session import impersonate
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export import jobs, saved_queries, streaming
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
			result = query({"from": "ToDo", "limit": 2})
			self.assertIn("result_id", result)
			self.assertEqual(len(result["rows"]), 2)
			q.run.return_value = [{"name": "x" * 40_000}]
			result = query({"from": "ToDo", "limit": 2})
			guarded, _ = enforce_result_budget(result, "query")
			self.assertEqual(guarded["result_id"], result["result_id"])


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
			patch.object(jobs, "_conversation", return_value=self.conversation),
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
			patch.object(jobs, "_conversation", return_value=self.conversation),
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
			patch.object(jobs, "_conversation", return_value=self.conversation),
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
				patch.object(jobs, "_conversation", return_value=self.conversation),
			):
				first = jobs.enqueue(source, "csv")
			self.names.append(first["export_job"])
			with patch.object(jobs, "_conversation", return_value=other.name):
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
			with patch.object(jobs, "_conversation", return_value=other.name):
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
		with patch.object(frappe, "get_all", wraps=frappe.get_all) as get_all:
			states = jobs.get_jobs([first.name, second.name, "missing"])
		self.assertEqual(states[first.name]["status"], "ready")
		self.assertEqual(states[second.name]["status"], "ready")
		self.assertIsNone(states["missing"])
		self.assertEqual([c.args[0] for c in get_all.call_args_list], [jobs.DOCTYPE, "File"])


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
