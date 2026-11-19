"""Server reload of an expired report snapshot (plan B.9).

Run on a dedicated test site, ONE snapshot test module at a time: every test clears the
`jarvis:report_snapshot*` keys (snapshots, locks, rate counters) of the shared Redis.
Fixtures are Query Reports (Server Scripts are off on the test site). The clock of the
time budget is injected (`reload_module.clock`); `time.monotonic` is never patched.
"""

import gzip
import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import redis
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import (
	DelegateReportRefused,
	FefoScopeRefused,
	InvalidArgumentError,
	PermissionDeniedError,
	RetryLaterError,
	SnapshotReloadedError,
)
from jarvis.tests.test_report_snapshots import _banned_calls, snapshot_module_paths
from jarvis.tools import _report_snapshot_digest as digest
from jarvis.tools import _report_snapshot_events as events
from jarvis.tools import _report_snapshot_payload as payload
from jarvis.tools import _report_snapshot_reload as reload_module
from jarvis.tools import _report_snapshot_source as source
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools import _report_snapshots as snap
from jarvis.tools import query_report as query_module
from jarvis.tools import run_report as run_report_module
from jarvis.tools._report_query_data import MAX_ANSWER, decode_report, encode
from jarvis.tools._report_query_process import RESOURCE_ERROR
from jarvis.tools._result_guard import MAX_RESULT_CHARS, measure
from jarvis.tools._source_process import BUSY as WORKER_BUSY
from jarvis.tools.query_report import query_report

NOT_AVAILABLE = events.NOT_AVAILABLE
NO_ACCESS = events.NO_ACCESS
UNAVAILABLE = events.UNAVAILABLE
BUSY = reload_module.BUSY_TEXT
TRANSIENT = reload_module.TRANSIENT_TEXT
SQL = "SELECT COUNT(*) AS n FROM report_rows"

ROWS_QUERY = (
	"select concat('ROW-', lpad(seq, 4, '0')) as `Name:Data:300`, "
	"seq as `Amount:Currency:100` from seq_1_to_40"
)
SESSION_KEY = "agent:main:report-snapshot-reload"


def _todo_query(tag):
	return (
		"select name as `Name:Data:200`, description as `Description:Data:300` from `tabToDo` "
		f"where locate('reload-{tag}-', description) = 1 order by description"
	)


def _clear_keys():
	cache = frappe.cache()
	keys = cache.keys(cache.make_key("jarvis:report_snapshot*"))
	if keys:
		cache.delete(*keys)
	digest.forget_perm_digest()
	for memo in ("jarvis_snapshot_conversation",):
		if hasattr(frappe.local, memo):
			delattr(frappe.local, memo)


def _snapshot_keys():
	cache = frappe.cache()
	return sorted(k.decode() for k in cache.keys(cache.make_key("jarvis:report_snapshot*")))


def _row_keys():
	"""The content and meta keys left. The per-owner index (it may still list a deleted id until
	the id falls off it) and the lock and rate keys are not snapshots."""
	prefixes = ("jarvis:report_snapshot:", "jarvis:report_snapshot_meta:")
	return [k for k in _snapshot_keys() if k.split("|", 1)[1].startswith(prefixes)]


def _meta_key(snapshot_id):
	return frappe.cache().make_key(store.META_PREFIX + snapshot_id)


def _content_key(snapshot_id):
	return frappe.cache().make_key(store.CONTENT_PREFIX + snapshot_id)


def _has(key):
	return int(frappe.cache().ttl(key) != -2)


def _meta(snapshot_id):
	return json.loads(frappe.cache().get(_meta_key(snapshot_id)))


def _put_meta(snapshot_id, **changes):
	meta = _meta(snapshot_id)
	meta.update(changes)
	frappe.cache().set(_meta_key(snapshot_id), json.dumps(meta), xx=True, keepttl=True)


def _rate_key(owner="Administrator"):
	return frappe.cache().make_key(reload_module.RATE_PREFIX + owner)


def _lock_key(owner="Administrator"):
	return frappe.cache().make_key(reload_module.LOCK_PREFIX + owner)


@contextmanager
def _connection_fault(method, only=None):
	"""Fail the calls of one redis-py connection method (all, or those whose packed command
	satisfies `only`); the test asserts that it fired."""
	import redis.connection as connection

	fired = []
	real = getattr(connection.AbstractConnection, method)

	def faulty(self, *args, **kwargs):
		packed = args[0] if args else b""
		blob = b"".join(packed) if isinstance(packed, list) else packed
		if only is None or (isinstance(blob, bytes) and only(blob)):
			fired.append(method)
			raise redis.exceptions.ConnectionError("injected " + method)
		return real(self, *args, **kwargs)

	with patch.object(connection.AbstractConnection, method, faulty):
		yield fired


class ReloadCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.tag = uuid.uuid4().hex[:8]
		cls.rows_report = cls._report("Rows", ROWS_QUERY)
		cls.todo_report = cls._report("Todo", _todo_query(cls.tag))
		cls.foreign = "snap-reload-foreign-" + cls.tag + "@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": cls.foreign,
				"first_name": "Reload Foreign",
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)

	@classmethod
	def _report(cls, label, query):
		return (
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": f"Jarvis Reload {label} {cls.tag}",
					"ref_doctype": "ToDo",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": query,
					"disable_prepared_report_automation": 1,
					"roles": [{"role": "System Manager"}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ("like", f"reload-{cls.tag}-%")})
		frappe.delete_doc("User", cls.foreign, force=True, ignore_permissions=True)
		for name in (cls.rows_report, cls.todo_report):
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_clear_keys()
		self.assertFalse(snap.in_agent_reload_scope())
		self.now = [1000.0]
		frappe.local.message_log = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_messages = False
		_clear_keys()

	# --- helpers ------------------------------------------------------------------

	def seed(self, report=None, filters=None, **meta):
		report = report or self.rows_report
		data = run_report_module.run_report(report, filters or {})
		snapshot_id, reason = snap.create(data, report_name=report, filters=filters or {}, run_ms=1200)
		self.assertIsNotNone(snapshot_id, reason)
		if meta:
			_put_meta(snapshot_id, **meta)
		return snapshot_id

	def expire(self, snapshot_id):
		frappe.cache().delete(_content_key(snapshot_id))

	def call(self, snapshot_id, **kwargs):
		"""`query_report` on the agent path: the only place a reload may run."""
		with snap.agent_reload_scope():
			return query_report(snapshot_id, **kwargs)

	def refused(self, snapshot_id, exc_type=InvalidArgumentError, **kwargs):
		with self.assertRaises(exc_type) as caught:
			self.call(snapshot_id, **kwargs)
		return caught.exception

	def reload(self, snapshot_id, **kwargs):
		"""A call that triggers a reload. Shape B (owner decision 2026-11-19): it is answered
		with the `snapshot_reloaded` error, never with rows. Returns the error."""
		return self.refused(snapshot_id, SnapshotReloadedError, **kwargs)

	def generation(self, snapshot_id):
		return _meta(snapshot_id)["generation"]

	def add_todo(self, n=1):
		for i in range(n):
			frappe.get_doc(
				{"doctype": "ToDo", "description": f"reload-{self.tag}-{uuid.uuid4().hex[:6]}"}
			).insert(ignore_permissions=True)

	def drop_todos(self):
		frappe.db.delete("ToDo", {"description": ("like", f"reload-{self.tag}-%")})

	@contextmanager
	def run_hook(self, before=None, raises=None, advance=0):
		"""Wrap the report run of a reload: count its calls, run `before`, advance the fake
		clock, raise, or run the real report."""
		real = run_report_module.frappe_run_report
		calls = []

		def fake(**kwargs):
			calls.append(kwargs)
			if before:
				before()
			self.now[0] += advance
			if raises is not None:
				raise raises
			return real(**kwargs)

		with patch.object(run_report_module, "frappe_run_report", fake):
			yield calls

	@contextmanager
	def fake_clock(self):
		with patch.object(reload_module, "clock", lambda: self.now[0]):
			yield

	@contextmanager
	def events(self):
		with patch("jarvis.telemetry.record_snapshot_event") as record:
			yield record

	def reload_ends(self, record):
		return [c for c in record.call_args_list if c.args[0] == "reload_end"]

	def last_end(self, record):
		ends = self.reload_ends(record)
		self.assertTrue(ends, record.call_args_list)
		return ends[-1].args[1], ends[-1].kwargs

	def assert_nothing_ran(self, calls):
		self.assertEqual(calls, [])

	def capture_lines(self):
		logger = frappe.logger("jarvis.report_snapshot", max_size=1_000_000, file_count=30)
		lines = []

		class Capture(logging.Handler):
			def emit(handler, record):
				lines.append(json.loads(record.getMessage()))

		handler = Capture()
		logger.addHandler(handler)
		self.addCleanup(logger.removeHandler, handler)
		return lines


class TestReloadReply(ReloadCase):
	def test_two_creates_of_identical_data_give_an_identical_sha(self):
		first, second = self.seed(), self.seed()
		self.assertEqual(_meta(first)["sha256"], _meta(second)["sha256"])
		stored = gzip.decompress(frappe.cache().get(_content_key(first)))
		self.assertEqual(hashlib.sha256(stored).hexdigest(), _meta(first)["sha256"])

	def test_expired_content_unchanged_data_does_not_run_the_sql_and_keeps_the_version(self):
		snapshot_id = self.seed()
		first = self.call(snapshot_id)
		created_at = _meta(snapshot_id)["created_at"]
		self.expire(snapshot_id)
		with (
			self.run_hook() as calls,
			self.events() as record,
			patch.object(query_module, "analyze_report", wraps=query_module.analyze_report) as analyze,
		):
			error = self.reload(snapshot_id, sql=SQL, version=first["version"])
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0]["report_name"], self.rows_report)
		self.assertTrue(analyze.called)
		self.assertTrue(all(c.kwargs["sql"] is None for c in analyze.call_args_list))
		self.assertEqual(error.version, first["version"])
		message = str(error)
		self.assertTrue(message.startswith("The report snapshot expired and was reloaded at "))
		self.assertIn("Data unchanged.", message)
		self.assertIn("Your SQL did not run.", message)
		self.assertIn(f"Send it again with version {first['version']}.", message)
		self.assertTrue(
			message.endswith("Do not combine numbers from before the reload with numbers after it.")
		)
		self.assertLessEqual(len(message), 400)
		meta = _meta(snapshot_id)
		self.assertEqual((meta["generation"], meta["created_at"]), (1, created_at))
		self.assertIn(meta["reloaded_at"], message)
		self.assertIsNone(meta["reload_started_at"])
		# The call sent ONE `read` event (no `live` from the inspect), and S4 pairing still holds.
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		self.assertEqual([(c.args[1], c.kwargs["sql_executed"]) for c in reads], [("reloaded", False)])
		self.assertEqual(self.last_end(record)[0], "ok")
		fresh = self.call(snapshot_id, sql=SQL, version=first["version"])
		self.assertTrue(fresh["sql_executed"])
		self.assertEqual(fresh["generation"], 1)
		self.assertEqual(fresh["as_of"], meta["reloaded_at"])
		self.assertEqual(fresh["created_at"], created_at)

	def test_an_inspect_call_that_reloads_gets_the_error_without_the_sql_sentence(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		error = self.reload(snapshot_id)
		self.assertNotIn("Your SQL did not run.", str(error))
		self.assertIn("Send it again with version " + error.version, str(error))

	def test_changed_data_gives_a_new_version_and_the_old_one_names_the_reload(self):
		self.add_todo()
		snapshot_id = self.seed(self.todo_report)
		first = self.call(snapshot_id)
		self.expire(snapshot_id)
		self.add_todo()
		with self.run_hook() as calls:
			error = self.reload(snapshot_id, sql=SQL, version=first["version"])
		self.assertEqual(len(calls), 1)
		self.assertNotEqual(error.version, first["version"])
		self.assertIn("Data changed.", str(error))
		old = self.refused(snapshot_id, sql=SQL, version=first["version"])
		self.assertIn("reloaded at", str(old))
		fresh = self.call(snapshot_id, sql=SQL, version=error.version)
		self.assertTrue(fresh["sql_executed"])

	def test_a_masking_only_change_gives_a_reload_error_with_a_new_version_not_a_version_error(self):
		snapshot_id = self.seed()
		first = self.call(snapshot_id)
		self.expire(snapshot_id)
		with patch.object(source, "_column_policy", return_value=({"amount"}, set())):
			error = self.reload(snapshot_id, sql=SQL, version=first["version"])
		self.assertNotEqual(error.version, first["version"])
		self.assertNotIn("Inspect it again before querying", str(error))

	def test_a_changed_digest_with_live_content_deletes_it_and_reloads_under_the_new_access(self):
		snapshot_id = self.seed()
		first = self.call(snapshot_id)
		digest.forget_perm_digest()
		with (
			patch.object(digest, "get_user_permissions", return_value={"Company": [{"doc": "X"}]}),
			self.run_hook() as calls,
		):
			error = self.reload(snapshot_id, sql=SQL, version=first["version"])
		self.assertEqual(len(calls), 1)
		self.assertIn("Access changed.", str(error))
		self.assertNotEqual(error.version, first["version"])

	def test_the_reload_runs_with_the_stored_literal_report_date(self):
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no company on the test site")
		filters = {"company": company, "report_date": "2026-10-29"}
		columns = [{"fieldname": "party_name", "label": "Party", "fieldtype": "Data"}]
		data = {"columns": columns, "result": [{"party_name": "Acme"}], "skip_total_row": 0}
		snapshot_id, reason = snap.create(data, report_name="Accounts Receivable", filters=filters, run_ms=5)
		self.assertIsNotNone(snapshot_id, reason)
		self.expire(snapshot_id)
		seen = []

		def run(**kwargs):
			seen.append(kwargs["filters"])
			return data

		with (
			patch("jarvis.tools._report_scope.nowdate", return_value="2026-12-31"),
			patch.object(run_report_module, "frappe_run_report", run),
		):
			self.reload(snapshot_id)
		self.assertEqual(seen[0]["report_date"], "2026-10-29")
		self.assertEqual(seen[0]["company"], company)
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_an_empty_reload_result_is_stored(self):
		self.add_todo()
		snapshot_id = self.seed(self.todo_report)
		self.expire(snapshot_id)
		self.drop_todos()
		error = self.reload(snapshot_id)
		self.assertIn("Data changed.", str(error))
		self.assertEqual(decode_report(frappe.cache().get(_content_key(snapshot_id)))[1], [])

	def test_a_reloaded_id_moves_to_the_front_of_the_index(self):
		first, second = self.seed(), self.seed()
		cache = frappe.cache()
		index = store.INDEX_PREFIX + "Administrator"
		self.assertEqual([x.decode() for x in cache.lrange(index, 0, -1)], [second, first])
		self.expire(first)
		self.reload(first)
		self.assertEqual([x.decode() for x in cache.lrange(index, 0, -1)], [first, second])
		prefixed = cache.make_key(index)
		self.assertTrue(prefixed.decode().startswith(frappe.conf.get("db_name") + "|"))
		self.assertTrue(0 < cache.ttl(prefixed) <= 86400)

	def test_the_error_is_worded_in_one_function_with_the_wire_code(self):
		info = snap.ReloadInfo(2, "2026-11-18 10:00:00", True, False, "R")
		bare = query_module._reload_error(None, info, sql_given=True)
		self.assertIsNone(bare.version)
		self.assertIn("Inspect it again, then send your SQL.", str(bare))
		self.assertNotIn("version", str(bare).replace("Inspect", ""))
		full = query_module._reload_error({"version": "v" * 64}, info, sql_given=True)
		self.assertEqual(full.version, "v" * 64)
		self.assertIn("Send it again with version " + "v" * 64 + ".", str(full))
		self.assertEqual(api._error_code(full), "snapshot_reloaded")
		self.assertIsInstance(full, SnapshotReloadedError)

	def test_every_change_wording_and_the_length_limit(self):
		for perm, data, wording in (
			(False, False, "Data unchanged."),
			(False, True, "Data changed."),
			(True, False, "Access changed."),
			(True, True, "Access and data changed."),
		):
			with self.subTest(wording):
				info = snap.ReloadInfo(2, "2026-11-18 10:00:00.123456", data, perm, "R")
				message = str(query_module._reload_error({"version": "v" * 64}, info, sql_given=True))
				self.assertIn(wording, message)
				self.assertLessEqual(len(message), 400)


class TestReportSaveIsNotAnAccessChange(ReloadCase):
	"""`Report.modified` is not a digest input: Frappe saves the Report during a slow run
	(`enable_prepared_report`), which must not make the next read a "permission change"."""

	def assert_read_is_live(self, snapshot_id):
		first = self.call(snapshot_id)
		with self.run_hook() as calls:
			answer = self.call(snapshot_id, sql=SQL, version=first["version"])
		self.assertTrue(answer["sql_executed"])
		self.assertEqual(calls, [])  # no reload
		self.assertEqual(self.generation(snapshot_id), 0)
		self.assertTrue(_has(_content_key(snapshot_id)))  # nothing deleted

	def test_the_prepared_report_automation_between_create_and_read_is_a_live_read(self):
		snapshot_id = self.seed()
		before = frappe.db.get_value("Report", self.rows_report, "modified")
		# The body of frappe's `enable_prepared_report`, in this connection: it would
		# re-init and destroy the test process otherwise.
		self.addCleanup(frappe.db.set_value, "Report", self.rows_report, "prepared_report", 0)
		frappe.db.set_value("Report", self.rows_report, "prepared_report", 1)
		self.assertNotEqual(frappe.db.get_value("Report", self.rows_report, "modified"), before)
		digest.forget_perm_digest()
		self.assert_read_is_live(snapshot_id)

	def test_a_plain_report_save_between_create_and_read_is_a_live_read(self):
		snapshot_id = self.seed()
		before = frappe.db.get_value("Report", self.rows_report, "modified")
		frappe.get_doc("Report", self.rows_report).save(ignore_permissions=True)
		self.assertNotEqual(frappe.db.get_value("Report", self.rows_report, "modified"), before)
		digest.forget_perm_digest()
		self.assert_read_is_live(snapshot_id)


class TestRefusals(ReloadCase):
	def test_a_report_switched_to_prepared_is_refused_and_deleted(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		frappe.db.set_value("Report", self.rows_report, "prepared_report", 1)
		self.addCleanup(frappe.db.set_value, "Report", self.rows_report, "prepared_report", 0)
		before = frappe.db.count("Prepared Report")
		with (
			self.events() as record,
			patch("jarvis.tools._prepared_reports.handle_prepared", side_effect=AssertionError("job")) as job,
			patch("frappe.enqueue", side_effect=AssertionError("job")),
		):
			error = self.refused(snapshot_id)
		self.assertEqual(str(error), NOT_AVAILABLE)
		job.assert_not_called()
		self.assertEqual(frappe.db.count("Prepared Report"), before)
		self.assertEqual(_row_keys(), [])
		self.assertEqual(self.last_end(record)[0], "refused:prepared")

	def test_run_report_without_inline_only_is_unchanged(self):
		report = self.rows_report
		self.assertEqual(run_report_module.run_report(report, {}), run_report_module._run_report(report, {}))
		frappe.db.set_value("Report", report, "prepared_report", 1)
		self.addCleanup(frappe.db.set_value, "Report", report, "prepared_report", 0)
		with patch(
			"jarvis.tools._prepared_reports.handle_prepared", return_value={"prepared_report": True, "n": 1}
		) as prepared:
			public = run_report_module.run_report(report, {})
			private = run_report_module._run_report(report, {})
		self.assertEqual(public, private)
		self.assertEqual(prepared.call_count, 2)
		with patch(
			"jarvis.chat.agent_scope.assert_active_fefo_report_scope",
			side_effect=InvalidArgumentError("FEFO text"),
		):
			with self.assertRaises(InvalidArgumentError) as caught:
				run_report_module.run_report(report, {})
		self.assertEqual(str(caught.exception), "FEFO text")
		self.assertIsInstance(caught.exception, FefoScopeRefused)

	def test_a_slow_first_run_is_refused_without_a_run(self):
		snapshot_id = self.seed(run_ms=9000)
		self.expire(snapshot_id)
		with self.events() as record, self.run_hook() as calls:
			error = self.refused(snapshot_id)
		self.assertEqual(str(error), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "refused:slow")
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)
		self.assertEqual(frappe.cache().get(_rate_key()), None)

	def test_a_run_ms_refresh_makes_a_slow_reload_refuse_the_next_one(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.fake_clock(), self.run_hook(advance=9) as calls:
			self.reload(snapshot_id)
		self.assertEqual(len(calls), 1)
		self.assertEqual(_meta(snapshot_id)["run_ms"], 9000)
		self.expire(snapshot_id)
		with self.fake_clock(), self.run_hook() as calls, self.events() as record:
			self.refused(snapshot_id)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "refused:slow")

	def test_a_permission_failure_deletes_both_keys_with_the_access_text(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=frappe.PermissionError("no")), self.events() as record:
			error = self.refused(snapshot_id, PermissionDeniedError)
		self.assertEqual(str(error), NO_ACCESS)
		self.assertEqual(_row_keys(), [])
		self.assertEqual(self.last_end(record)[0], "refused:permission")
		self.assertEqual(frappe.local.message_log, [])

	def test_an_unknown_report_and_a_gone_filter_value_delete_both_keys(self):
		missing = self.seed()
		_put_meta(missing, report_name="No Such Report " + self.tag)
		self.expire(missing)
		self.assertEqual(str(self.refused(missing)), NOT_AVAILABLE)
		self.assertEqual(_row_keys(), [])
		gone = self.seed()
		self.expire(gone)
		with self.run_hook(raises=frappe.ValidationError("filter value gone")):
			self.assertEqual(str(self.refused(gone)), NOT_AVAILABLE)
		self.assertEqual(_row_keys(), [])
		self.assertEqual(frappe.local.message_log, [])

	def test_an_unclassified_error_deletes_the_keys_and_is_never_a_killed_attempt(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=RuntimeError("boom")), self.events() as record:
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assertEqual(self.last_end(record)[0], "failed:RuntimeError")
		self.assertEqual(_row_keys(), [])

	def test_fefo_and_delegate_refusals_keep_the_keys_and_clear_the_mark(self):
		cases = {
			"fefo": patch("jarvis.chat.agent_scope.active_fefo_scope", return_value={"company": "X"}),
			"fefo proof": patch(
				"jarvis.chat.agent_scope.active_fefo_scope",
				side_effect=InvalidArgumentError("FEFO visibility proof is stale or unavailable"),
			),
			"delegate": patch(
				"jarvis.tools._delegate_capability.snapshot_config_report_refusal",
				return_value="delegate text",
			),
		}
		for label, context in cases.items():
			with self.subTest(label):
				snapshot_id = self.seed()
				self.expire(snapshot_id)
				with (
					context,
					self.events() as record,
					patch.object(run_report_module, "frappe_run_report") as ran,
				):
					error = self.refused(snapshot_id)
				self.assertEqual(str(error), NOT_AVAILABLE)
				ran.assert_not_called()
				self.assertEqual(_has(_meta_key(snapshot_id)), 1)
				self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
				self.assertEqual(_meta(snapshot_id)["generation"], 0)
				self.assertEqual(
					self.last_end(record)[0], "refused:" + ("delegate" if label == "delegate" else "fefo")
				)
				# the next allowed call reloads: not a killed attempt
				self.reload(snapshot_id)
				self.assertEqual(self.generation(snapshot_id), 1)

	def test_the_private_skip_keeps_the_keys(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.object(payload._private_output, "metadata_redacted", return_value=True),
			self.events() as record,
		):
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
		self.assertEqual(self.last_end(record)[0], "refused:private")

	def test_typed_refusals_keep_message_and_wire_code_through_run_tool(self):
		args = {"report_name": self.rows_report}
		with patch(
			"jarvis.chat.agent_scope.assert_active_fefo_report_scope",
			side_effect=InvalidArgumentError("FEFO text"),
		):
			fefo = api._run_tool("run_report", args)
		self.assertFalse(fefo["ok"])
		self.assertEqual(
			(fefo["error"]["code"], fefo["error"]["message"]), ("InvalidArgumentError", "FEFO text")
		)
		with patch(
			"jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="delegate text"
		):
			delegate = api._run_tool("run_report", args)
		self.assertEqual(
			(delegate["error"]["code"], delegate["error"]["message"]),
			("PermissionDeniedError", "delegate text"),
		)
		self.assertIn(delegate["error"]["code"], api._ACTIVITY_DENIED_CODES)
		self.assertTrue(delegate["error"].get("hint"))
		with patch(
			"jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="delegate text"
		):
			with self.assertRaises(DelegateReportRefused) as caught:
				run_report_module.run_report(self.rows_report, {})
		self.assertIsInstance(caught.exception, PermissionDeniedError)
		self.assertEqual(api._error_code(caught.exception), "PermissionDeniedError")
		self.assertEqual(api._error_code(FefoScopeRefused("x")), "InvalidArgumentError")

	def test_mute_messages_is_restored_and_the_message_log_stays_empty(self):
		for before in (False, True):
			with self.subTest(before=before):
				snapshot_id = self.seed()
				self.expire(snapshot_id)
				frappe.flags.mute_messages = before

				def noisy():
					frappe.msgprint("a report message")

				with self.run_hook(before=noisy, raises=frappe.PermissionError("no")):
					self.refused(snapshot_id, PermissionDeniedError)
				self.assertEqual(frappe.flags.mute_messages, before)
				self.assertEqual(frappe.local.message_log, [])

	def test_a_foreign_user_gets_the_missing_text_with_no_lock_and_no_count(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		frappe.set_user(self.foreign)
		with (
			self.run_hook() as calls,
			patch.object(type(frappe.cache()), "lock", side_effect=AssertionError("no lock")),
		):
			error = self.refused(snapshot_id)
		self.assertEqual(str(error), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		keys = _snapshot_keys()
		self.assertFalse([k for k in keys if "reload" in k])

	def test_the_reload_itself_refuses_a_foreign_record_after_the_lock(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		meta = _meta(snapshot_id)
		frappe.set_user(self.foreign)
		with snap.agent_reload_scope(), self.run_hook() as calls, self.assertRaises(InvalidArgumentError):
			reload_module.reload_snapshot(snapshot_id, meta, perm_changed=False, miss="not_available")
		self.assert_nothing_ran(calls)
		self.assertIsNone(frappe.cache().get(_rate_key(self.foreign)))
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])

	def test_missing_meta_is_not_available_with_no_reload(self):
		snapshot_id = self.seed()
		frappe.cache().delete(_meta_key(snapshot_id), _content_key(snapshot_id))
		with self.run_hook() as calls:
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)

	def test_an_id_trimmed_during_its_reload_stays_not_available(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def trim():
			frappe.cache().delete(_meta_key(snapshot_id))

		with self.run_hook(before=trim):
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertEqual(_has(_meta_key(snapshot_id)), 0)

	def test_switches_off_give_not_available_with_no_run(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.dict(frappe.conf, {"jarvis_report_snapshot_reload": 0}),
			self.run_hook() as calls,
			self.events() as record,
		):
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "disabled")
		live = self.seed()
		with (
			patch.dict(frappe.conf, {"jarvis_report_snapshots": 0}),
			patch.object(store, "keys", side_effect=AssertionError("no Redis access")),
		):
			self.assertEqual(str(self.refused(live)), NOT_AVAILABLE)
		with patch.dict(frappe.conf, {"jarvis_report_snapshots": 0, "jarvis_report_snapshot_reload": 1}):
			self.assertFalse(snap.reload_enabled())


class TestTransient(ReloadCase):
	def test_database_timeouts_and_deadlocks_keep_the_record_and_can_retry(self):
		errors = {
			"timeout": frappe.db.OperationalError(
				1969, "Query execution was interrupted (max_statement_time exceeded)"
			),
			"deadlock": frappe.QueryDeadlockError("deadlock"),
		}
		for label, exc in errors.items():
			with self.subTest(label):
				_clear_keys()
				snapshot_id = self.seed()
				self.expire(snapshot_id)
				with self.run_hook(raises=exc), self.events() as record:
					error = self.refused(snapshot_id, RetryLaterError)
				self.assertEqual(str(error), TRANSIENT)
				meta = _meta(snapshot_id)
				self.assertEqual(meta["generation"], 0)
				self.assertIsNone(meta["reload_started_at"])
				self.assertEqual(int(frappe.cache().get(_rate_key())), 1)
				self.assertTrue(self.last_end(record)[0].startswith("failed:"))
				self.reload(snapshot_id)
				self.assertEqual(self.generation(snapshot_id), 1)

	def test_a_redis_fault_while_writing_keeps_the_record_retryable(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def content_write(blob):
			return b"\r\nSET\r\n" in blob and b"report_snapshot:rs_" in blob

		with _connection_fault("send_packed_command", only=content_write) as fired:
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertTrue(fired)
		self.assertEqual(str(error), TRANSIENT)
		meta = _meta(snapshot_id)
		self.assertEqual(meta["generation"], 0)
		self.assertIsNone(meta["reload_started_at"])
		self.assertEqual(int(frappe.cache().get(_rate_key())), 1)
		self.reload(snapshot_id)
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_a_connection_error_is_retryable_with_no_reload_and_no_run(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		for method in ("send_packed_command", "read_response"):
			with self.subTest(method):
				with _connection_fault(method) as fired, self.run_hook() as calls:
					error = self.refused(snapshot_id, RetryLaterError)
				self.assertTrue(fired)
				self.assertEqual(str(error), UNAVAILABLE)
				self.assert_nothing_ran(calls)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)

	def test_a_worker_busy_run_is_transient(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=InvalidArgumentError(WORKER_BUSY)):
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), TRANSIENT)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)


class TestLockAndCap(ReloadCase):
	def hold(self, owner="Administrator", ttl=None):
		lock = frappe.cache().lock(_lock_key(owner), timeout=ttl or reload_module.RELOAD_LOCK_TTL_S)
		self.assertTrue(lock.acquire(blocking=False))
		return lock

	def test_a_fresh_holder_gives_busy_with_no_run(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		lock = self.hold()
		self.addCleanup(lambda: lock.owned() and lock.release())
		with self.run_hook() as calls, self.events() as record:
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), BUSY)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "busy")
		self.assertIsNone(frappe.cache().get(_rate_key()))

	def test_a_holder_older_than_thirty_seconds_is_stale_not_busy(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		lock = self.hold(ttl=reload_module.RELOAD_LOCK_TTL_S - reload_module.STALE_AFTER_S - 10)
		self.addCleanup(lambda: lock.owned() and lock.release())
		with self.run_hook() as calls:
			error = self.refused(snapshot_id, InvalidArgumentError)
		self.assertEqual(str(error), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)

	def test_a_lock_that_vanished_meanwhile_is_busy(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with patch("redis.lock.Lock.acquire", return_value=False), self.run_hook() as calls:
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), BUSY)
		self.assert_nothing_ran(calls)

	def test_a_lock_without_a_ttl_is_treated_as_stale(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		frappe.cache().set(_lock_key(), "x")
		with self.run_hook() as calls:
			error = self.refused(snapshot_id, InvalidArgumentError)
		self.assertEqual(str(error), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)

	def test_only_the_changed_key_text_is_a_lost_fence(self):
		watch_error = redis.exceptions.WatchError
		self.assertEqual(store.lost_fence(watch_error("Watched variable changed.")), "stale")
		self.assertEqual(store.lost_fence(watch_error("Watched variable changed."), "watch"), "watch")
		for text in (
			"A ConnectionError occurred while watching one or more keys",  # redis-py 7.x
			"A ConnectionError occurred on while watching one or more keys",  # redis-py 4.5.5
			"anything else",
		):
			with self.subTest(text), self.assertRaises(redis.exceptions.ConnectionError):
				store.lost_fence(watch_error(text))

	def test_the_lock_is_released_only_when_it_was_acquired(self):
		real_release = redis.lock.Lock.release
		released = []

		def spy(lock):
			released.append(lock)
			return real_release(lock)

		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with patch.object(redis.lock.Lock, "release", spy):
			held = self.hold()
			with self.run_hook():
				self.refused(snapshot_id, RetryLaterError)  # busy
			held.release()
			released.clear()
			stale = self.hold(ttl=reload_module.RELOAD_LOCK_TTL_S - reload_module.STALE_AFTER_S - 10)
			with self.run_hook():
				self.refused(snapshot_id)  # stale
			real_release(stale)
			self.assertEqual(released, [])
			self.reload(snapshot_id)  # acquired: success
			self.assertEqual(len(released), 1)
			self.expire(snapshot_id)
			with self.run_hook(raises=RuntimeError("boom")):
				self.refused(snapshot_id)  # acquired: exception
			self.assertEqual(len(released), 2)

	def test_the_lock_is_released_after_success_and_after_an_exception(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		self.reload(snapshot_id)
		self.assertEqual(_has(_lock_key()), 0)
		self.expire(snapshot_id)
		with self.run_hook(raises=RuntimeError("boom")):
			self.refused(snapshot_id)
		self.assertEqual(_has(_lock_key()), 0)

	def test_a_holder_past_its_ttl_cannot_release_a_newer_holders_lock(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		newer = []

		def take_over():
			frappe.cache().delete(_lock_key())  # the first lock expired
			lock = frappe.cache().lock(_lock_key(), timeout=reload_module.RELOAD_LOCK_TTL_S)
			self.assertTrue(lock.acquire(blocking=False))
			newer.append(lock)

		with self.run_hook(before=take_over):
			self.reload(snapshot_id)
		self.assertEqual(_has(_lock_key()), 1)
		self.assertTrue(newer[0].owned())
		newer[0].release()

	def test_a_stale_generation_write_is_rejected_by_the_fence(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def winner():
			meta = _meta(snapshot_id)
			meta.update(generation=1, sha256="w" * 64, reload_started_at="winner-stamp")
			frappe.cache().set(_meta_key(snapshot_id), json.dumps(meta), xx=True, keepttl=True)

		with patch.object(store, "_between_check_and_exec", winner), self.events() as record:
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), TRANSIENT)
		meta = _meta(snapshot_id)
		self.assertEqual((meta["generation"], meta["sha256"]), (1, "w" * 64))
		self.assertEqual(meta["reload_started_at"], "winner-stamp")  # not cleared after the lost fence
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertEqual(self.last_end(record)[0], "failed:watch")

	def test_a_winner_that_committed_before_the_write_is_not_overwritten(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def winner():
			meta = _meta(snapshot_id)
			meta.update(generation=1, sha256="w" * 64, reload_started_at="winner-stamp")
			frappe.cache().set(_meta_key(snapshot_id), json.dumps(meta), xx=True, keepttl=True)

		with self.run_hook(before=winner):
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), TRANSIENT)
		meta = _meta(snapshot_id)
		self.assertEqual((meta["generation"], meta["sha256"]), (1, "w" * 64))
		self.assertEqual(meta["reload_started_at"], "winner-stamp")
		self.assertEqual(_has(_content_key(snapshot_id)), 0)

	def test_a_write_that_loses_the_watch_deletes_nothing(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def bump():
			frappe.cache().set(
				_meta_key(snapshot_id), frappe.cache().get(_meta_key(snapshot_id)), xx=True, keepttl=True
			)

		with patch.object(store, "_between_check_and_exec", bump):
			self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(_has(_meta_key(snapshot_id)), 1)

	def test_a_killed_attempt_refuses_once_and_deletes_the_keys(self):
		snapshot_id = self.seed(reload_started_at="2026-11-18 00:00:00 abcd")
		self.expire(snapshot_id)
		with self.run_hook() as calls, self.events() as record:
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "failed:killed")
		self.assertEqual(_row_keys(), [])

	def test_a_simulated_system_exit_leaves_the_mark_set(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=SystemExit(1)), self.assertRaises(SystemExit):
			self.call(snapshot_id)
		self.assertTrue(_meta(snapshot_id)["reload_started_at"])
		self.assertEqual(_has(_lock_key()), 0)
		with self.events() as record:
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assertEqual(self.last_end(record)[0], "failed:killed")

	def test_the_seventh_reload_inside_an_hour_is_capped(self):
		snapshot_id = self.seed()
		for n in range(1, 7):
			self.expire(snapshot_id)
			self.reload(snapshot_id)
			self.assertEqual(self.generation(snapshot_id), n)
		self.expire(snapshot_id)
		with self.run_hook() as calls, self.events() as record:
			self.assertEqual(str(self.refused(snapshot_id)), NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "capped")
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])

	def test_reloads_spread_over_five_hours_are_never_capped(self):
		snapshot_id = self.seed()
		window_start = None
		for minute in (0, 50, 100, 150, 200, 250, 300):
			if window_start is not None and minute - window_start >= 60:
				frappe.cache().delete(_rate_key())  # the fixed window expired in Redis
				window_start = None
			elif window_start is not None:
				frappe.cache().expire(_rate_key(), (60 - (minute - window_start)) * 60)
			window_start = minute if window_start is None else window_start
			self.expire(snapshot_id)
			self.reload(snapshot_id)
			self.assertEqual(self.generation(snapshot_id), [0, 50, 100, 150, 200, 250, 300].index(minute) + 1)

	def test_incr_never_extends_the_window(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		self.reload(snapshot_id)
		frappe.cache().expire(_rate_key(), 1000)
		self.expire(snapshot_id)
		self.reload(snapshot_id)
		self.assertEqual(int(frappe.cache().get(_rate_key())), 2)
		self.assertTrue(0 < frappe.cache().ttl(_rate_key()) <= 1000)

	def test_busy_not_agent_path_disabled_and_slow_do_not_count(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.assertRaises(InvalidArgumentError):
			query_report(snapshot_id)  # not the agent path
		with (
			patch.dict(frappe.conf, {"jarvis_report_snapshot_reload": 0}),
			self.assertRaises(InvalidArgumentError),
		):
			self.call(snapshot_id)
		self.assertIsNone(frappe.cache().get(_rate_key()))

	def test_a_context_refusal_then_an_allowed_call_reloads(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with patch("jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="x"):
			self.refused(snapshot_id)
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
		self.reload(snapshot_id)
		self.assertEqual(self.generation(snapshot_id), 1)


class TestTimeBudget(ReloadCase):
	def spy_deadline(self, order):
		real = query_module.Deadline
		seen = []

		def spy(seconds=22):
			seen.append(seconds)
			order.append("deadline")
			return real(seconds)

		return patch.object(query_module, "Deadline", spy), seen

	def test_a_reload_that_took_20_seconds_inspects_under_a_7_second_deadline(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		order = []
		spy, seen = self.spy_deadline(order)
		with self.fake_clock(), spy, self.run_hook(before=lambda: order.append("run"), advance=20):
			error = self.reload(snapshot_id)
		self.assertEqual(order, ["run", "deadline"])
		self.assertAlmostEqual(seen[0], 7.0)
		self.assertTrue(error.version)

	def test_a_fast_reload_inspects_under_the_query_deadline_not_the_whole_call_budget(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		order = []
		spy, seen = self.spy_deadline(order)
		with self.fake_clock(), spy, self.run_hook(advance=0.5):
			self.reload(snapshot_id)
		self.assertEqual(len(seen), 1)
		self.assertLessEqual(seen[0], 22)
		self.assertGreater(seen[0], 0)

	def test_a_reload_that_took_26_seconds_returns_the_note_without_an_inspect(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		order = []
		spy, seen = self.spy_deadline(order)
		with self.fake_clock(), spy, self.run_hook(advance=26):
			error = self.reload(snapshot_id)
		self.assertEqual(seen, [])
		self.assertIsNone(error.version)
		self.assertEqual(self.generation(snapshot_id), 1)
		self.assertIn("Inspect it again, then send your SQL.", str(error))

	def test_a_reload_that_took_35_seconds_still_stores_its_rows(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.fake_clock(), self.run_hook(advance=35) as calls:
			first = self.reload(snapshot_id)
		self.assertIsNone(first.version)
		meta = _meta(snapshot_id)
		self.assertGreater(meta["run_ms"], 8000)
		self.assertEqual(_has(_content_key(snapshot_id)), 1)
		with self.fake_clock(), self.run_hook() as again:
			second = self.call(snapshot_id)
		self.assert_nothing_ran(again)
		self.assertEqual(len(calls), 1)
		self.assertIn("version", second)
		self.assertEqual(second["generation"], 1)

	def test_an_inspect_that_hits_its_deadline_or_a_busy_worker_gives_the_note_only(self):
		for message in (RESOURCE_ERROR, WORKER_BUSY):
			with self.subTest(message[:20]):
				_clear_keys()
				snapshot_id = self.seed()
				self.expire(snapshot_id)
				with patch.object(query_module, "analyze_report", side_effect=InvalidArgumentError(message)):
					error = self.reload(snapshot_id)
				self.assertIsNone(error.version)
				self.assertEqual(self.generation(snapshot_id), 1)
				self.assertNotEqual(str(error), RESOURCE_ERROR)

	def test_any_other_inspect_failure_still_gives_the_note_and_one_reloaded_read_event(self):
		failures = (
			("lookalike", InvalidArgumentError("Report error: " + RESOURCE_ERROR)),
			("retry_later", RetryLaterError("try later")),
			("permission", PermissionDeniedError("no access")),
			("runtime", RuntimeError("boom")),
		)
		for label, failure in failures:
			with self.subTest(label):
				_clear_keys()
				snapshot_id = self.seed()
				self.expire(snapshot_id)
				with (
					patch.object(query_module, "analyze_report", side_effect=failure),
					self.events() as record,
				):
					error = self.reload(snapshot_id)
				self.assertIsNone(error.version)
				self.assertTrue(str(error).startswith("The report snapshot expired and was reloaded at "))
				self.assertEqual(self.generation(snapshot_id), 1)
				reads = [c for c in record.call_args_list if c.args[0] == "read"]
				self.assertEqual([c.args[1] for c in reads], ["reloaded"])
				self.assertEqual(self.last_end(record)[0], "ok")

	def test_a_failed_inspect_after_a_reload_logs_the_reason_and_the_class_only(self):
		secret = "SECRET-INSPECT-VALUE"
		_clear_keys()
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.object(query_module, "analyze_report", side_effect=TypeError(secret)),
			self.assertLogs(
				frappe.logger("jarvis.report_snapshot", max_size=1_000_000, file_count=30), level="INFO"
			) as logs,
		):
			error = self.reload(snapshot_id)
		self.assertIsNone(error.version)
		lines = [json.loads(record.getMessage()) for record in logs.records]
		failures = [line for line in lines if line.get("outcome") == "inspect_after_reload_failed"]
		self.assertEqual(len(failures), 1)
		self.assertEqual((failures[0]["event"], failures[0]["error"]), ("failure", "TypeError"))
		self.assertNotIn(secret, "\n".join(logs.output))

	def test_a_gate_refusal_of_the_inspect_sends_no_read_event_of_its_own(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.object(
				source.SnapshotSource,
				"confirm",
				side_effect=lambda self_: (_ for _ in ()).throw(self_._changed(delete=False)),
				autospec=True,
			),
			self.events() as record,
		):
			error = self.reload(snapshot_id)
		self.assertIsNone(error.version)
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		self.assertEqual([c.args[1] for c in reads], ["reloaded"])


class TestAgentPath(ReloadCase):
	def test_the_scope_is_set_inside_the_agent_call_and_reset_after_it(self):
		seen = []

		def record(*args, **kwargs):
			seen.append(snap.in_agent_reload_scope())
			return {"ok": True, "data": {"x": 1}}

		with (
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("jarvis.api._run_tool", side_effect=record),
		):
			api._dispatch_from_session("Administrator", SESSION_KEY, "get_doc", {})
		self.assertEqual(seen, [True])
		self.assertFalse(snap.in_agent_reload_scope())
		with (
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("jarvis.api._run_tool", side_effect=RuntimeError("boom")),
			self.assertRaises(RuntimeError),
		):
			api._dispatch_from_session("Administrator", SESSION_KEY, "get_doc", {})
		self.assertFalse(snap.in_agent_reload_scope())

	def test_the_scope_resets_with_its_token_when_nested(self):
		with snap.agent_reload_scope():
			with snap.agent_reload_scope():
				self.assertTrue(snap.in_agent_reload_scope())
			self.assertTrue(snap.in_agent_reload_scope())
		self.assertFalse(snap.in_agent_reload_scope())

	def test_the_agent_entry_reloads_end_to_end(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api._persist_and_publish_tool_call"),
		):
			result = api._dispatch_from_session(
				"Administrator", SESSION_KEY, "query_report", {"prepared_report": snapshot_id}
			)
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["error"]["code"], "snapshot_reloaded")
		self.assertTrue(result["error"]["message"].startswith("The report snapshot expired and was reloaded"))
		self.assertLessEqual(len(result["error"]["message"]), 400)
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_the_desk_call_tool_never_reloads(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook() as calls, self.events() as record:
			result = api._dispatch_current_user("query_report", {"prepared_report": snapshot_id})
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["message"], NOT_AVAILABLE)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "not_agent_path")

	def shared(self, tool, args, run_tool=None):
		from jarvis.chat import collaboration_dispatch

		real_get = frappe.db.get_value

		def get_value(doctype, *a, **k):
			if doctype in ("Jarvis Chat Message", "Jarvis Pending Action"):
				return None
			return real_get(doctype, *a, **k)

		permit = SimpleNamespace(
			turn=SimpleNamespace(binding=SimpleNamespace(conversation=None, name="B"), run_id="r")
		)

		@contextmanager
		def scope(*a, **k):
			yield permit

		patches = [
			patch.object(collaboration_dispatch, "callback_scope", scope),
			patch.object(collaboration_dispatch, "validate_targets"),
			patch.object(collaboration_dispatch, "receipt_id", return_value="x"),
			patch.object(collaboration_dispatch, "call_key", return_value="k"),
			patch.object(collaboration_dispatch, "claim", return_value=True),
			patch.object(collaboration_dispatch, "complete"),
			patch("jarvis.chat.agent_closure.assert_session_dispatch_allowed"),
			patch("jarvis.api.persist_tool_receipt"),
			patch.object(frappe.db, "get_value", get_value),
		]
		if run_tool:
			patches.append(patch("jarvis.api._run_tool", side_effect=run_tool))
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return collaboration_dispatch.dispatch(SESSION_KEY, tool, args, "call-1")

	def test_shared_dispatch_keeps_the_wire_codes_of_the_typed_refusals(self):
		cases = (
			(FefoScopeRefused("fefo text"), "InvalidArgumentError"),
			(DelegateReportRefused("delegate text"), "PermissionDeniedError"),
			(RetryLaterError("later"), "RetryLaterError"),
		)
		for exc, code in cases:
			with self.subTest(code=code):

				def raiser(*a, _exc=exc, **k):
					raise _exc

				result = self.shared("run_report", {"report_name": "R"}, run_tool=raiser)
				self.assertFalse(result["ok"])
				self.assertEqual(result["error"]["code"], code)
				self.assertEqual(result["error"]["message"], str(exc))

	def test_shared_dispatch_never_reloads(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook() as calls, self.events() as record:
			result = self.shared("query_report", {"prepared_report": snapshot_id})
		self.assertFalse(result["ok"], result)
		self.assert_nothing_ran(calls)
		self.assertEqual(self.last_end(record)[0], "not_agent_path")
		self.assertFalse(snap.in_agent_reload_scope())

	def test_shared_dispatch_with_an_oversized_run_report_makes_no_snapshot(self):
		rows = [{"name": f"R{i}", "blob": "x" * 90} for i in range(600)]
		big = {"ok": True, "data": {"columns": [{"fieldname": "name"}], "result": rows}}
		with patch.object(snap, "create", side_effect=AssertionError("no snapshot")) as create:
			result = self.shared("run_report", {"report_name": "R"}, run_tool=lambda *a, **k: big)
		create.assert_not_called()
		data = result["data"]
		self.assertTrue(data["note"].startswith("Result truncated to fit the context window"))
		self.assertNotIn("run", data)
		self.assertEqual(_snapshot_keys(), [])


class TestRegistryAndStatics(ReloadCase):
	def test_inline_only_is_not_a_model_argument(self):
		from jarvis.tools import registry

		self.assertNotIn("inline_only", registry._ACCEPTED_PARAMS["run_report"])
		frappe.db.set_value("Report", self.rows_report, "prepared_report", 1)
		self.addCleanup(frappe.db.set_value, "Report", self.rows_report, "prepared_report", 0)
		envelope = {"prepared_report": True, "status": "started"}
		with patch("jarvis.tools._prepared_reports.handle_prepared", return_value=envelope) as prepared:
			result = registry.dispatch("run_report", {"report_name": self.rows_report, "inline_only": True})
		prepared.assert_called_once()
		self.assertEqual(result["status"], "started")

	def test_every_snapshot_module_avoids_the_wrapper_methods_that_swallow_errors(self):
		for path in snapshot_module_paths():
			self.assertEqual(_banned_calls(path.read_text()), [], path.name)

	def test_the_reload_runs_the_inline_only_body_not_the_public_tool(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.object(run_report_module, "_run_report", wraps=run_report_module._run_report) as inner,
			patch.object(run_report_module, "run_report", side_effect=AssertionError("public tool")),
		):
			self.reload(snapshot_id)
		inner.assert_called_once()
		self.assertIs(inner.call_args.kwargs["inline_only"], True)
		self.assertEqual(inner.call_args.args[0], self.rows_report)


class TestTelemetry(ReloadCase):
	def test_start_and_end_events_carry_the_signal_fields_in_order(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		order = []
		with self.events() as record, self.run_hook(before=lambda: order.append("run")):
			record.side_effect = lambda event, *a, **k: order.append(event)
			self.reload(snapshot_id)
		self.assertEqual([x for x in order if x != "read"][:3], ["reload_start", "run", "reload_end"])
		calls = {c.args[0]: c for c in record.call_args_list if c.args[0].startswith("reload")}
		for name, outcome in (("reload_start", "started"), ("reload_end", "ok")):
			fields = calls[name].kwargs
			self.assertEqual(calls[name].args[1], outcome)
			self.assertEqual(fields["snapshot_id"], snapshot_id)
			self.assertIsInstance(fields["reload_ms"], int)
			self.assertIn("generation", fields)
		end = calls["reload_end"].kwargs
		self.assertEqual((end["generation"], end["data_changed"], end["perm_changed"]), (1, False, False))
		self.assertEqual(end["origin_created_at"], _meta(snapshot_id)["created_at"])
		self.assertEqual(calls["reload_start"].kwargs["generation"], 0)

	def test_a_reload_with_an_evicted_signal_reports_it_as_a_field_of_the_one_read_event(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.events() as record:
			self.reload(snapshot_id)
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		self.assertEqual([c.args[1] for c in reads], ["reloaded"])
		self.assertIs(reads[0].kwargs["evicted"], True)
		self.assertEqual(reads[0].kwargs["generation"], 1)

	def test_a_failed_reload_of_an_evicted_snapshot_carries_the_field_too(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.events() as record, patch.dict(frappe.conf, {"jarvis_report_snapshot_reload": 0}):
			self.refused(snapshot_id)
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		self.assertEqual([c.args[1] for c in reads], ["not_available"])
		self.assertIs(reads[0].kwargs["evicted"], True)

	def test_a_delete_by_jarvis_is_not_evicted_on_a_reload(self):
		snapshot_id = self.seed()
		digest.forget_perm_digest()
		with (
			patch.object(digest, "get_user_permissions", return_value={"Company": [{"doc": "X"}]}),
			self.events() as record,
		):
			self.reload(snapshot_id)
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		self.assertEqual([c.args[1] for c in reads], ["reloaded"])
		self.assertNotIn("evicted", reads[0].kwargs)

	def test_the_content_delete_stamp_is_under_the_generation_fence(self):
		snapshot_id = self.seed()
		meta = _meta(snapshot_id)
		_put_meta(snapshot_id, generation=5, content_deleted_at=None)
		store.delete_content(snapshot_id, meta)  # a caller that still holds generation 0
		self.assertIsNone(_meta(snapshot_id)["content_deleted_at"])
		store.delete_content(snapshot_id, {**meta, "generation": 5})
		self.assertTrue(_meta(snapshot_id)["content_deleted_at"])

	def test_a_reload_clears_the_content_deleted_stamp(self):
		snapshot_id = self.seed(content_deleted_at="2026-11-18 00:00:00")
		self.expire(snapshot_id)
		self.reload(snapshot_id)
		self.assertIsNone(_meta(snapshot_id)["content_deleted_at"])

	def test_the_conversation_lookup_is_memoised_on_frappe_local(self):
		from jarvis import telemetry
		from jarvis.tools import _agent_run_ctx

		conversation = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "reload memo"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", conversation.name, "session_key", SESSION_KEY)
		_agent_run_ctx.set_session_key(SESSION_KEY)
		self.addCleanup(_agent_run_ctx.clear_session_key)
		self.addCleanup(
			frappe.delete_doc, "Jarvis Conversation", conversation.name, force=True, ignore_permissions=True
		)
		with patch(
			"jarvis.tools._export.jobs.current_conversation", return_value=conversation.name
		) as lookup:
			self.assertEqual(telemetry._snapshot_conversation(), conversation.name)
			self.assertEqual(telemetry._snapshot_conversation(), conversation.name)
		self.assertEqual(lookup.call_count, 1)

	def test_a_reload_then_a_pinned_sql_read_make_a_matching_pair(self):
		from jarvis.tools import _agent_run_ctx

		conversation = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "reload pair"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", conversation.name, "session_key", SESSION_KEY)
		_agent_run_ctx.set_session_key(SESSION_KEY)
		self.addCleanup(_agent_run_ctx.clear_session_key)
		self.addCleanup(
			frappe.delete_doc, "Jarvis Conversation", conversation.name, force=True, ignore_permissions=True
		)
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		lines = self.capture_lines()
		with patch.object(frappe, "log_level", None):
			reply = self.reload(snapshot_id)
			self.call(snapshot_id, sql=SQL, version=reply.version)
		starts = [x for x in lines if x["event"] == "reload_start"]
		ends = [x for x in lines if x["event"] == "reload_end"]
		self.assertEqual((len(starts), len(ends)), (1, 1))
		self.assertEqual(ends[0]["outcome"], "ok")
		hashed = hashlib.sha256(snapshot_id.encode()).hexdigest()[:12]
		for line in (starts[0], ends[0]):
			self.assertEqual(line["snapshot_hash"], hashed)
			self.assertEqual(line["conversation"], conversation.name)
			self.assertIsInstance(line["reload_ms"], int)
		pinned = [x for x in lines if x["event"] == "read" and x["sql_executed"] is True]
		self.assertEqual(len(pinned), 1)
		key = lambda x: (x["conversation"], x["snapshot_hash"], x["generation"])  # noqa: E731
		self.assertEqual(key(pinned[0]), key(ends[0]))
		self.assertEqual(ends[0]["generation"], 1)
		self.assertTrue(lines.index(pinned[0]) > lines.index(ends[0]))
		reloaded = [x for x in lines if x["event"] == "read" and x["outcome"] == "reloaded"]
		self.assertEqual(len(reloaded), 1)
		self.assertTrue(reloaded[0]["evicted"] and key(reloaded[0]) == key(ends[0]))
		# the inspect inside the reload call is not a `live` read of its own
		self.assertEqual([x for x in lines if x["event"] == "read" and x["outcome"] == "live"], pinned)

	def test_the_reload_lines_reach_the_logger_at_info_without_a_log_level(self):
		name = "jarvis.report_snapshot-" + str(frappe.local.site)
		frappe.loggers.pop(name, None)
		logging.getLogger(name).setLevel(logging.NOTSET)
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		lines = self.capture_lines()
		with patch.object(frappe, "log_level", None):
			self.reload(snapshot_id)
		self.assertEqual(
			{x["event"] for x in lines} & {"reload_start", "reload_end"}, {"reload_start", "reload_end"}
		)

	def test_the_read_after_a_reload_names_the_report_for_the_skill_row_gate(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		self.reload(snapshot_id)  # shape B: the reload call itself reads no rows (an error is not judged)
		reply = self.call(snapshot_id)
		self.assertEqual(reply["report_name"], self.rows_report)
		with patch("jarvis.api._is_a_skill_doctype", return_value=True):
			self.assertTrue(api._reads_skill_rows("query_report", {"prepared_report": snapshot_id}, reply))
		with patch("jarvis.api._is_a_skill_doctype", return_value=False):
			self.assertFalse(api._reads_skill_rows("query_report", {"prepared_report": snapshot_id}, reply))

	def test_create_returns_the_stored_size_and_api_does_not_import_a_private_helper(self):
		created = snap.create(
			run_report_module.run_report(self.rows_report, {}),
			report_name=self.rows_report,
			filters={},
			run_ms=1,
		)
		self.assertEqual(created.size, len(frappe.cache().get(_content_key(created[0]))))
		self.assertEqual(created, (created[0], "ok"))
		skipped = snap.create({"result": "x"}, report_name=self.rows_report, filters={}, run_ms=1)
		self.assertEqual((skipped, skipped.size), ((None, "no_rows"), None))
		self.assertNotIn("_report_snapshots._keys", Path(api.__file__).read_text())


class TestStampRobustness(ReloadCase):
	"""A6: a stamp that was applied but whose reply was lost must not read as a killed attempt."""

	def test_a_lost_stamp_reply_is_taken_back_and_the_next_call_reloads(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		real, calls = store.update_meta, []

		def lose_the_reply(*args, **kwargs):
			calls.append(1)
			state = real(*args, **kwargs)
			if len(calls) == 1:  # the stamp: applied in Redis, reply lost on the way back
				raise redis.exceptions.ConnectionError("reply lost")
			return state

		with (
			patch.object(store, "update_meta", lose_the_reply),
			self.run_hook() as ran,
			self.events() as record,
		):
			error = self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(str(error), UNAVAILABLE)
		self.assert_nothing_ran(ran)
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
		self.assertEqual(self.last_end(record)[0], "failed:redis_error")
		self.reload(snapshot_id)  # not `failed:killed`
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_a_reader_write_between_watch_and_exec_does_not_leave_a_killed_stamp(self):
		"""A reader's `content_deleted_at` write loses our fence. The stamp is ours and no
		newer writer exists, so it is taken back: the next call reloads, it is not `killed`."""
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def reader_write():
			_put_meta(snapshot_id, content_deleted_at="2026-11-19 00:00:00")

		with patch.object(store, "_between_check_and_exec", reader_write):
			self.refused(snapshot_id, RetryLaterError)
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
		with self.events() as record:
			self.reload(snapshot_id)
		self.assertEqual(self.last_end(record)[0], "ok")
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_a_stamp_that_is_not_ours_is_never_cleared_after_a_lost_reply(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		real, calls = store.update_meta, []

		def lose_the_reply_and_let_another_attempt_stamp(*args, **kwargs):
			calls.append(1)
			if len(calls) > 1:
				return real(*args, **kwargs)
			real(*args, **kwargs)
			_put_meta(snapshot_id, reload_started_at="another attempt")
			raise redis.exceptions.ConnectionError("reply lost")

		with patch.object(store, "update_meta", lose_the_reply_and_let_another_attempt_stamp):
			self.refused(snapshot_id, RetryLaterError)
		self.assertEqual(len(calls), 2)  # the stamp, then the compare-and-set that found it was not ours
		self.assertEqual(_meta(snapshot_id)["reload_started_at"], "another attempt")

	def test_clearing_the_stamp_retries_once_after_a_lost_watch(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		real, seen = store.update_meta, []

		def flaky(*args, **kwargs):
			seen.append(1)
			if len(seen) == 2:  # the first clear
				return "watch"
			return real(*args, **kwargs)

		with (
			patch.object(store, "update_meta", flaky),
			patch("jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="x"),
		):
			self.refused(snapshot_id)
		self.assertEqual(len(seen), 3)
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])

	def test_a_clear_that_cannot_succeed_is_logged_not_silent(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		real, seen = store.update_meta, []

		def never_clears(*args, **kwargs):
			seen.append(1)
			return real(*args, **kwargs) if len(seen) == 1 else "stale"

		lines = self.capture_lines()
		with (
			patch.object(frappe, "log_level", None),
			patch.object(store, "update_meta", never_clears),
			patch("jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="x"),
		):
			self.refused(snapshot_id)
		failures = [x["outcome"] for x in lines if x["event"] == "failure"]
		self.assertEqual(failures, ["reload_clear_stale"])


class TestContentDeleteFence(ReloadCase):
	"""A5: Jarvis deletes content only inside the fence of the record it read."""

	def test_a_stale_view_never_deletes_a_newer_generations_content(self):
		snapshot_id = self.seed()
		stale = _meta(snapshot_id)
		_put_meta(snapshot_id, generation=1)  # a reload moved on
		store.delete_content(snapshot_id, stale)
		self.assertEqual(_has(_content_key(snapshot_id)), 1)
		self.assertIsNone(_meta(snapshot_id)["content_deleted_at"])
		store.delete_content(snapshot_id, _meta(snapshot_id))
		self.assertEqual(_has(_content_key(snapshot_id)), 0)
		self.assertTrue(_meta(snapshot_id)["content_deleted_at"])

	def test_a_writer_between_the_check_and_the_exec_keeps_its_content(self):
		snapshot_id = self.seed()
		stale = _meta(snapshot_id)

		fired = []

		def winner():  # one reload write lands between the check and the EXEC
			if not fired:
				fired.append(True)
				_put_meta(snapshot_id, generation=1)
				frappe.cache().set(_content_key(snapshot_id), b"NEWER", keepttl=True)

		with patch.object(store, "_before_meta_exec", winner):
			store.delete_content(snapshot_id, stale)
		self.assertTrue(fired)
		self.assertEqual(frappe.cache().get(_content_key(snapshot_id)), b"NEWER")
		self.assertIsNone(_meta(snapshot_id)["content_deleted_at"])

	def test_a_missing_record_drops_the_unreachable_rows(self):
		snapshot_id = self.seed()
		stale = _meta(snapshot_id)
		frappe.cache().delete(_meta_key(snapshot_id))
		store.delete_content(snapshot_id, stale)
		self.assertEqual(_has(_content_key(snapshot_id)), 0)


class TestMetaTtl(ReloadCase):
	"""B (A10): the 24 h record keeps its own expiry through every in-place update."""

	PINNED = 5000

	def pin(self, snapshot_id):
		frappe.cache().expire(_meta_key(snapshot_id), self.PINNED)

	def assert_kept(self, snapshot_id, label):
		ttl = frappe.cache().ttl(_meta_key(snapshot_id))
		self.assertTrue(self.PINNED - 60 <= ttl <= self.PINNED, f"{label}: ttl {ttl}")

	def test_update_meta_keeps_the_ttl(self):
		snapshot_id = self.seed()
		self.pin(snapshot_id)
		self.assertEqual(store.update_meta(snapshot_id, lambda m: {**m, "run_ms": 7}), "ok")
		self.assert_kept(snapshot_id, "update_meta")

	def test_the_content_delete_stamp_keeps_the_ttl(self):
		snapshot_id = self.seed()
		self.pin(snapshot_id)
		store.delete_content(snapshot_id, _meta(snapshot_id))
		self.assertTrue(_meta(snapshot_id)["content_deleted_at"])
		self.assert_kept(snapshot_id, "delete_content")

	def test_mark_and_clear_stamp_keep_the_ttl(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		self.pin(snapshot_id)
		with (
			patch("jarvis.tools._delegate_capability.snapshot_config_report_refusal", return_value="x"),
			self.events() as record,
		):
			self.refused(snapshot_id)  # marked, then refused by context and cleared
		self.assertEqual(self.last_end(record)[0], "refused:delegate")
		self.assertIsNone(_meta(snapshot_id)["reload_started_at"])
		self.assertEqual(int(frappe.cache().get(_rate_key())), 1)  # it did get as far as the mark
		self.assert_kept(snapshot_id, "mark + clear_stamp")

	def test_a_reload_resets_the_ttl_to_the_full_record_life(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		self.pin(snapshot_id)
		self.reload(snapshot_id)
		self.assertTrue(86400 - 60 <= frappe.cache().ttl(_meta_key(snapshot_id)) <= 86400)


class TestCustomReportFilters(ReloadCase):
	"""A8: the filters a Custom Report's saved defaults applied are the effective ones, and a
	reload pins them: a later edit of the saved defaults cannot move the scope."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.base = cls._report(
			"TagBase",
			"select name as `Name:Data:200`, description as `Description:Data:300` from `tabToDo` "
			"where locate(%(tag)s, description) = 1 order by description",
		)
		cls.custom = (
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": f"Jarvis Reload Custom {cls.tag}",
					"ref_doctype": "ToDo",
					"report_type": "Custom Report",
					"reference_report": cls.base,
					"is_standard": "No",
					"json": json.dumps({"filters": {"tag": cls.prefix("A")}, "columns": []}),
					"roles": [{"role": "System Manager"}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	@classmethod
	def prefix(cls, label):
		return f"reload-{cls.tag}-{label}-"

	def setUp(self):
		super().setUp()
		self.drop_todos()
		frappe.db.set_value(
			"Report", self.custom, "json", json.dumps({"filters": {"tag": self.prefix("A")}, "columns": []})
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in (cls.custom, cls.base):
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def todo(self, label, n):
		for i in range(n):
			frappe.get_doc(
				{"doctype": "ToDo", "description": f"{self.prefix(label)}{uuid.uuid4().hex[:6]}"}
			).insert(ignore_permissions=True)

	def test_saved_filters_that_change_cannot_move_a_reload(self):
		self.todo("A", 2)
		self.todo("B", 1)
		data = run_report_module.run_report(self.custom, {})
		self.assertEqual(data["custom_filters"], {"tag": self.prefix("A")})
		self.assertEqual(len(data["result"]), 2)
		filters = api._snapshot_filters({"report_name": self.custom, "filters": {"tag": "ignored"}}, data)
		self.assertEqual(filters, {"tag": self.prefix("A")})
		snapshot_id, reason = snap.create(data, report_name=self.custom, filters=filters, run_ms=5)
		self.assertIsNotNone(snapshot_id, reason)
		sha = _meta(snapshot_id)["sha256"]
		frappe.db.set_value(
			"Report", self.custom, "json", json.dumps({"filters": {"tag": self.prefix("B")}, "columns": []})
		)
		digest.forget_perm_digest()  # a new request
		self.expire(snapshot_id)
		with self.run_hook() as calls:
			error = self.reload(snapshot_id)
		self.assertEqual(calls[0]["filters"], {"tag": self.prefix("A")})
		self.assertIs(calls[0]["are_default_filters"], False)
		# The scope did not move: the rows are the two of A (B has one), byte for byte.
		self.assertEqual(_meta(snapshot_id)["sha256"], sha)
		self.assertIn("Data unchanged.", str(error))  # a report edit is not an access change
		self.assertEqual(self.generation(snapshot_id), 1)

	def test_without_the_pin_frappe_would_have_applied_the_new_defaults(self):
		self.todo("A", 2)
		self.todo("B", 1)
		frappe.db.set_value(
			"Report", self.custom, "json", json.dumps({"filters": {"tag": self.prefix("B")}, "columns": []})
		)
		moved = run_report_module.run_report(self.custom, {"tag": self.prefix("A")})  # a normal run
		self.assertEqual(len(moved["result"]), 1)
		pinned = run_report_module._run_report(self.custom, {"tag": self.prefix("A")}, inline_only=True)
		self.assertEqual(len(pinned["result"]), 2)
		self.assertNotIn("custom_filters", pinned)

	def test_a_run_that_applied_no_saved_filters_keeps_the_call_filters_and_the_scope(self):
		data = {"result": [], "columns": [], "report_scope": {"company": "C", "report_date": "2026-01-01"}}
		args = {"report_name": "Accounts Receivable", "filters": '{"customer": "X"}'}
		self.assertEqual(
			api._snapshot_filters(args, data), {"customer": "X", "company": "C", "report_date": "2026-01-01"}
		)

	def test_custom_filters_that_are_not_an_object_cannot_be_pinned(self):
		for odd in (["a"], "x=1"):
			with self.subTest(odd):
				self.assertIsNone(api._snapshot_filters({"report_name": "R"}, {"custom_filters": odd}))
		big = {
			"columns": [{"fieldname": "a", "label": "A", "fieldtype": "Data"}],
			"result": [{"a": "x" * 100} for _ in range(600)],
			"custom_filters": ["a"],
		}
		self.assertGreater(measure(big), MAX_RESULT_CHARS)
		data, measured, fields = api._snapshot_oversized_run_report(big, {"report_name": "R"}, 5)
		self.assertEqual(fields, {"snapshot": "skipped:filters"})
		self.assertNotIn("run", data)
		self.assertEqual(_snapshot_keys(), [])


class TestRealAccessChanges(ReloadCase):
	"""A10 (a), (d): real role, Report Role and User Permission changes, no patched
	permission functions, as a user who is not a System Manager."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.doctype = frappe.get_doc(
			{
				"doctype": "DocType",
				"name": "Jarvis Reload Gate " + cls.tag,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"fields": [{"fieldname": "title", "label": "Title", "fieldtype": "Data"}],
				"permissions": [
					{"role": "Accounts User", "read": 1, "report": 1, "permlevel": 0},
					{"role": "System Manager", "read": 1, "report": 1, "permlevel": 0},
				],
			}
		).insert(ignore_permissions=True)
		cls.reader = f"reload-reader-{cls.tag}@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": cls.reader,
				"first_name": "Reload Reader",
				"send_welcome_email": 0,
				"roles": [{"role": "Accounts User"}],
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
		cls.gated = cls._gated_report()

	@classmethod
	def _gated_report(cls):
		return (
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": f"Jarvis Reload Gated {cls.tag}",
					"ref_doctype": cls.doctype.name,
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select concat('ROW-', lpad(seq, 4, '0')) as `Name:Data:300` from seq_1_to_30",
					"disable_prepared_report_automation": 1,
					"roles": [{"role": "Accounts User"}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("User", cls.reader, force=True, ignore_permissions=True)
		frappe.delete_doc("Report", cls.gated, force=True, ignore_permissions=True)
		frappe.delete_doc("DocType", cls.doctype.name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		user = frappe.get_doc("User", self.reader)
		if "Accounts User" not in {r.role for r in user.roles}:
			user.add_roles("Accounts User")
		self.set_report_roles("Accounts User")
		frappe.clear_cache(user=self.reader)

	def set_report_roles(self, *roles):
		"""A report without roles is open to every user with report access: replace the role."""
		report = frappe.get_doc("Report", self.gated)
		report.set("roles", [{"role": role} for role in roles])
		report.save(ignore_permissions=True)

	def seed_as_reader(self):
		frappe.set_user(self.reader)
		data = run_report_module.run_report(self.gated, {})
		snapshot_id, reason = snap.create(data, report_name=self.gated, filters={}, run_ms=5)
		self.assertIsNotNone(snapshot_id, reason)
		return snapshot_id

	def test_the_reader_can_use_the_snapshot_before_any_change(self):
		snapshot_id = self.seed_as_reader()
		self.assertEqual(self.call(snapshot_id)["prepared_report"], snapshot_id)

	def test_a_report_role_removed_from_the_reader_refuses_the_reload_and_deletes_the_keys(self):
		snapshot_id = self.seed_as_reader()
		frappe.set_user("Administrator")
		self.set_report_roles("System Manager")  # the reader's role is no longer on the report
		digest.forget_perm_digest()
		self.expire(snapshot_id)
		frappe.set_user(self.reader)
		with self.events() as record:
			error = self.refused(snapshot_id, PermissionDeniedError)
		self.assertEqual(str(error), NO_ACCESS)
		self.assertEqual(self.last_end(record)[0], "refused:permission")
		self.assertEqual(_row_keys(), [])

	def test_a_role_removed_from_the_user_refuses_the_reload_and_deletes_the_keys(self):
		snapshot_id = self.seed_as_reader()
		frappe.set_user("Administrator")
		frappe.get_doc("User", self.reader).remove_roles("Accounts User")
		frappe.clear_cache(user=self.reader)
		frappe.set_user(self.reader)
		digest.forget_perm_digest()
		with self.events() as record:
			error = self.refused(snapshot_id, PermissionDeniedError)  # the content was still live
		self.assertEqual(str(error), NO_ACCESS)
		self.assertEqual(self.last_end(record)[0], "refused:permission")
		self.assertEqual(_row_keys(), [])

	def test_a_real_user_permission_added_then_removed_reloads_each_time(self):
		snapshot_id = self.seed_as_reader()
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("no company on the test site")
		frappe.set_user("Administrator")
		permission = frappe.get_doc(
			{"doctype": "User Permission", "user": self.reader, "allow": "Company", "for_value": company}
		).insert(ignore_permissions=True)
		frappe.set_user(self.reader)
		digest.forget_perm_digest()
		with self.run_hook() as calls:
			error = self.reload(snapshot_id)  # the content was live: the digest alone forced it
		self.assertEqual(len(calls), 1)
		self.assertIn("Access changed.", str(error))
		self.assertEqual(self.generation(snapshot_id), 1)
		self.assertEqual(self.call(snapshot_id)["generation"], 1)  # settled: reads the new rows
		frappe.set_user("Administrator")
		frappe.delete_doc("User Permission", permission.name, force=True, ignore_permissions=True)
		frappe.set_user(self.reader)
		digest.forget_perm_digest()
		with self.run_hook() as calls:
			error = self.reload(snapshot_id)
		self.assertEqual(len(calls), 1)
		self.assertIn("Access changed.", str(error))
		self.assertEqual(self.generation(snapshot_id), 2)


class TestFailureEvents(ReloadCase):
	"""A4: `reason` rides the read and reload_end events; non-transient failures are logged."""

	def ends_and_reads(self, record):
		ends = [c for c in record.call_args_list if c.args[0] == "reload_end"]
		reads = [c for c in record.call_args_list if c.args[0] == "read"]
		return ends[-1], reads[-1]

	def test_a_redis_fault_carries_its_exception_class_on_both_events(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def down(*args, **kwargs):
			raise redis.exceptions.ConnectionError("down")

		with patch.object(store, "update_meta", down), self.events() as record:
			self.refused(snapshot_id, RetryLaterError)
		end, read = self.ends_and_reads(record)
		self.assertEqual(end.args[1], "failed:redis_error")
		self.assertEqual(end.kwargs["reason"], "ConnectionError")
		self.assertEqual((read.args[1], read.kwargs["reason"]), ("redis_error", "ConnectionError"))

	def test_a_lost_fence_carries_the_watch_reason(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)

		def bump():
			_put_meta(snapshot_id, generation=5)

		with patch.object(store, "_between_check_and_exec", bump), self.events() as record:
			self.refused(snapshot_id, RetryLaterError)
		end, read = self.ends_and_reads(record)
		self.assertEqual((end.args[1], end.kwargs["reason"]), ("failed:watch", "WatchError"))
		self.assertEqual(read.kwargs["reason"], "WatchError")

	def test_a_transient_run_error_carries_its_class(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=frappe.QueryDeadlockError("deadlock")), self.events() as record:
			self.refused(snapshot_id, RetryLaterError)
		end, read = self.ends_and_reads(record)
		self.assertEqual(
			(end.args[1], end.kwargs["reason"]), ("failed:QueryDeadlockError", "QueryDeadlockError")
		)
		self.assertEqual((read.args[1], read.kwargs["reason"]), ("error", "QueryDeadlockError"))

	def test_a_write_error_carries_its_class(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with (
			patch.object(store, "_write_reload", side_effect=redis.exceptions.ConnectionError("write down")),
			self.events() as record,
		):
			self.refused(snapshot_id, RetryLaterError)
		end, _ = self.ends_and_reads(record)
		self.assertEqual((end.args[1], end.kwargs["reason"]), ("failed:error", "ConnectionError"))

	def test_an_unclassified_report_failure_is_logged_at_error_with_the_class_only(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		secret = "SECRET-FILTER-VALUE"
		lines = self.capture_lines()
		with patch.object(frappe, "log_level", None), self.run_hook(raises=RuntimeError(secret)):
			self.refused(snapshot_id)
		failures = [x for x in lines if x["event"] == "failure"]
		self.assertEqual([(x["outcome"], x["error"]) for x in failures], [("reload_failed", "RuntimeError")])
		self.assertNotIn(secret, json.dumps(lines))

	def test_a_context_refusal_and_a_transient_failure_are_not_error_logs(self):
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		lines = self.capture_lines()
		with self.run_hook(raises=frappe.QueryDeadlockError("deadlock")):
			self.refused(snapshot_id, RetryLaterError)
		self.assertEqual([x for x in lines if x["event"] == "failure"], [])


class TestSignalsOnRealEvents(ReloadCase):
	"""The rollout script reads the lines that a real reload writes: `reload_start` carries the
	generation before the reload, `reload_end` (ok) the generation after it."""

	def compute(self, lines):
		from jarvis.scripts import report_snapshot_signals as signals

		records, malformed = signals.parse_records([json.dumps(x) for x in lines])
		self.assertEqual(malformed, 0)
		return signals.compute(records)

	def healthy_cycle(self):
		"""Seed, reload under changed access (the live content is deleted, not evicted), then
		the read that follows the reload."""
		digest.forget_perm_digest()
		snapshot_id = self.seed()
		digest.forget_perm_digest()
		with patch.object(digest, "get_user_permissions", return_value={"Company": [{"doc": "X"}]}):
			error = self.reload(snapshot_id)
			self.assertEqual(self.generation(snapshot_id), 1)
			self.call(snapshot_id, sql=SQL, version=error.version)

	def test_healthy_reloads_leave_no_unmatched_start_and_no_abort(self):
		lines = self.capture_lines()
		for _ in range(25):
			_clear_keys()  # a fresh owner window: the reload cap is 6 an hour
			self.healthy_cycle()
		starts = [x for x in lines if x["event"] == "reload_start"]
		ends = [x for x in lines if x["event"] == "reload_end"]
		self.assertEqual((len(starts), len(ends)), (25, 25))
		self.assertEqual({x["generation"] for x in starts}, {0})
		self.assertEqual({x["generation"] for x in ends}, {1})
		result = self.compute(lines)
		self.assertEqual(result["unmatched_reload_starts"], 0)
		self.assertEqual(result["abort"], [])

	def test_a_killed_attempt_is_counted(self):
		class Killed(BaseException):
			pass

		lines = self.capture_lines()
		for _ in range(2):
			self.healthy_cycle()
		digest.forget_perm_digest()
		snapshot_id = self.seed()
		digest.forget_perm_digest()
		with (
			patch.object(digest, "get_user_permissions", return_value={"Company": [{"doc": "X"}]}),
			self.run_hook(raises=Killed()),
			self.assertRaises(Killed),
		):
			self.call(snapshot_id)
		self.assertEqual(len([x for x in lines if x["event"] == "reload_start"]), 3)
		self.assertEqual(self.compute(lines)["unmatched_reload_starts"], 1)

	def test_a_failed_reload_matches_at_the_unchanged_generation(self):
		lines = self.capture_lines()
		snapshot_id = self.seed()
		self.expire(snapshot_id)
		with self.run_hook(raises=frappe.QueryDeadlockError("deadlock")):
			self.refused(snapshot_id, RetryLaterError)
		ends = [x for x in lines if x["event"] == "reload_end"]
		self.assertEqual([(x["outcome"], x["generation"]) for x in ends], [("failed:QueryDeadlockError", 0)])
		self.assertEqual(self.compute(lines)["unmatched_reload_starts"], 0)
