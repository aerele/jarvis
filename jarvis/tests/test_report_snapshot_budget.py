"""Task 2b: the agent dispatch path snapshots a cut inline run_report.

Through the real agent entry point (`api._dispatch_from_session`) and a deterministic
Script Report: a result over the model-facing budget is stored as a report snapshot, the
model gets a cut preview that names the `rs_` id, and every other result is as before.
Redis keys under `jarvis:report_snapshot*` are cleared around every test.
"""

import json
import time
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api, telemetry
from jarvis.tools import _private_output
from jarvis.tools import _report_snapshot_digest as digest
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools import _report_snapshots as snap
from jarvis.tools._result_guard import MAX_RESULT_CHARS, measure

SESSION_KEY = "agent:main:report-snapshot-budget"

# Query Reports over the sequence engine: deterministic, no server script needed.
ROWS_QUERY = (
	"select concat('ROW-', lpad(seq, 4, '0'), repeat('x', 90)) as `Name:Data:300`, "
	"seq as `Amount:Currency:100` from seq_1_to_400"
)
SMALL_QUERY = "select concat('ROW-', lpad(seq, 4, '0')) as `Name:Data:300` from seq_1_to_5"


def _clear_keys():
	cache = frappe.cache()
	keys = cache.keys(cache.make_key("jarvis:report_snapshot*"))
	if keys:
		cache.delete(*keys)
	digest.forget_perm_digest()


def _snapshot_keys():
	cache = frappe.cache()
	return sorted(k.decode() for k in cache.keys(cache.make_key("jarvis:report_snapshot*")))


class _Clock:
	"""`jarvis.api.time` with a controllable `monotonic()`; every other attribute is the real
	`time`. `time.monotonic` itself is never patched (`Deadline` reads it)."""

	def __init__(self):
		self.now = 1000.0

	def monotonic(self):
		return self.now

	def __getattr__(self, name):
		return getattr(time, name)


def _big_run():
	rows = [{"name": f"R{i}", "blob": "x" * 90} for i in range(600)]
	return {"ok": True, "data": {"columns": [{"fieldname": "name"}], "result": rows}}


class SnapshotBudgetCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.tag = uuid.uuid4().hex[:8]
		cls.rows_report = cls._report("Rows", ROWS_QUERY)
		cls.small_report = cls._report("Small", SMALL_QUERY)

	@classmethod
	def _report(cls, label, query):
		return (
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": f"Jarvis Budget {label} {cls.tag}",
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
		for name in (cls.rows_report, cls.small_report):
			frappe.delete_doc("Report", name, force=True, ignore_permissions=True)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_clear_keys()

	def tearDown(self):
		frappe.set_user("Administrator")
		_clear_keys()

	def dispatch(self, tool, args, run_tool=None, **kwargs):
		"""The real agent entry point. Returns `(result, persisted_receipts)`; the receipt
		write is captured, not stored."""
		with ExitStack() as stack:
			stack.enter_context(patch("jarvis.api._get_header", return_value=None))
			persist = stack.enter_context(patch("jarvis.api._persist_and_publish_tool_call"))
			if run_tool is not None:
				stack.enter_context(patch("jarvis.api._run_tool", side_effect=run_tool))
				# A faked run has no stored report to gate; the gate has its own tests.
				stack.enter_context(
					patch("jarvis.tools._report_query_source.gate_report", return_value=(None, []))
				)
			result = api._dispatch_from_session("Administrator", SESSION_KEY, tool, args, **kwargs)
		return result, [call.kwargs["result"] for call in persist.call_args_list]


class TestCutRunReportIsSnapshotted(SnapshotBudgetCase):
	def meta(self, run):
		return json.loads(frappe.cache().get(frappe.cache().make_key(store.META_PREFIX + run)))

	def test_a_cut_run_report_carries_an_rs_run_and_the_snapshot_note(self):
		result, receipts = self.dispatch("run_report", {"report_name": self.rows_report, "filters": {}})
		self.assertTrue(result["ok"], result)
		data = result["data"]
		self.assertRegex(data["run"], r"^rs_[A-Za-z0-9_-]{32}$")
		self.assertIs(data["preview_complete"], False)
		self.assertTrue(data["_truncated"])
		self.assertEqual(data["total"], 400)
		self.assertLess(data["shown"], 400)
		self.assertTrue(data["note"].startswith(f"Partial preview: {data['shown']} of 400 rows."))
		self.assertIn(f"prepared_report={data['run']}", data["note"])
		self.assertNotIn("report_snapshot", data)
		self.assertNotIn("_jarvis_snapshot", data)
		self.assertLessEqual(measure(data), MAX_RESULT_CHARS)
		self.assertEqual(receipts[-1]["data"]["run"], data["run"])

	def test_the_stored_snapshot_is_the_whole_result_for_the_same_owner_and_report(self):
		result, _ = self.dispatch("run_report", {"report_name": self.rows_report, "filters": '{"a": 1}'})
		run = result["data"]["run"]
		meta = self.meta(run)
		self.assertEqual(meta["owner"], "Administrator")
		self.assertEqual(meta["report_name"], self.rows_report)
		self.assertEqual(meta["filters"], {"a": 1})
		from jarvis.tools.query_report import query_report

		inspected = query_report(run)
		self.assertEqual(inspected["report_name"], self.rows_report)
		self.assertEqual(inspected["sql_executed"], False)

	def test_the_non_row_overflow_case_is_snapshotted_with_zero_rows_shown(self):
		wide = {
			"columns": [{"fieldname": "name", "label": "Name", "fieldtype": "Data"}],
			"result": [{"name": f"ROW-{i}"} for i in range(1, 4)],
			"skip_total_row": 0,
			"chart": {"data": "C" * 40_000},
		}
		result, _ = self.dispatch(
			"run_report",
			{"report_name": self.small_report},
			run_tool=lambda *a, **k: {"ok": True, "data": wide},
		)
		data = result["data"]
		self.assertTrue(result["ok"], result)
		self.assertRegex(data["run"], r"^rs_")
		self.assertEqual((data["shown"], data["total"]), (0, 3))
		self.assertIs(data["preview_complete"], False)
		self.assertTrue(data["note"].startswith("Partial preview: 0 of 3 rows."))
		self.assertNotIn("_jarvis_snapshot", data)
		self.assertEqual(
			snap.perm_digest("Administrator", self.small_report), self.meta(data["run"])["perm_digest"]
		)

	def test_the_budget_event_records_the_snapshot_and_its_stored_size(self):
		with patch("jarvis.api.telemetry.record_budget_event") as record:
			result, _ = self.dispatch("run_report", {"report_name": self.rows_report})
		record.assert_called_once()
		kwargs = record.call_args.kwargs
		self.assertEqual((kwargs["tool"], kwargs["outcome"]), ("run_report", "truncated"))
		self.assertEqual(kwargs["snapshot"], "created")
		self.assertGreater(kwargs["snapshot_bytes"], 0)
		stored = frappe.cache().get(frappe.cache().make_key(store.CONTENT_PREFIX + result["data"]["run"]))
		self.assertEqual(kwargs["snapshot_bytes"], len(stored))

	def test_a_small_run_report_is_untouched(self):
		with patch("jarvis.api.telemetry.record_budget_event") as record:
			result, _ = self.dispatch("run_report", {"report_name": self.small_report})
		self.assertEqual(len(result["data"]["result"]), 5)
		self.assertNotIn("run", result["data"])
		record.assert_not_called()
		self.assertEqual(_snapshot_keys(), [])


class TestWhenNoSnapshotIsMade(SnapshotBudgetCase):
	def big_report(self, **extra):
		rows = [{"name": f"R{i}", "blob": "x" * 90} for i in range(600)]
		return {"ok": True, "data": {"columns": [{"fieldname": "name"}], "result": rows, **extra}}

	def test_a_skipped_snapshot_gives_the_generic_note_and_no_run(self):
		with (
			patch("jarvis.api._report_snapshots.create", return_value=store.Created(None, "caps")),
			patch("jarvis.api.telemetry.record_budget_event") as record,
		):
			result, _ = self.dispatch(
				"run_report", {"report_name": "R"}, run_tool=lambda *a, **k: self.big_report()
			)
		data = result["data"]
		self.assertTrue(data["note"].startswith("Result truncated to fit the context window"))
		self.assertNotIn("run", data)
		self.assertNotIn("preview_complete", data)
		self.assertEqual(record.call_args.kwargs["snapshot"], "skipped:caps")
		self.assertIsNone(record.call_args.kwargs.get("snapshot_bytes"))

	def test_a_failed_create_names_the_exception_class_in_the_budget_event(self):
		with (
			patch(
				"jarvis.api._report_snapshots.create",
				return_value=store.Created(None, "error", error="ConnectionError"),
			),
			patch("jarvis.api.telemetry.record_budget_event") as record,
		):
			result, _ = self.dispatch("run_report", {"report_name": self.rows_report})
		self.assertNotIn("run", result["data"])
		self.assertEqual(record.call_args.kwargs["snapshot"], "skipped:error")
		self.assertEqual(record.call_args.kwargs["snapshot_error"], "ConnectionError")

	def test_a_create_that_raises_never_fails_the_call(self):
		with (
			patch("jarvis.api._report_snapshots.create", side_effect=RuntimeError("boom")),
			patch("jarvis.api.telemetry.record_budget_event") as record,
		):
			result, _ = self.dispatch(
				"run_report", {"report_name": "R"}, run_tool=lambda *a, **k: self.big_report()
			)
		self.assertTrue(result["ok"])
		self.assertTrue(result["data"]["note"].startswith("Result truncated"))
		self.assertEqual(record.call_args.kwargs["snapshot"], "failed")

	def test_a_report_whose_stored_filters_fail_the_read_gate_gets_no_snapshot(self):
		"""An AR/AP report without its company or date: every read of its snapshot would be
		refused, so none is stored. The gate runs once, on the effective filters."""
		from jarvis.tools import _report_query_source as query_source

		real_gate = query_source.gate_report
		with (
			patch.dict(query_source.PARTY_REPORTS, {self.rows_report: ("Customer", "customer_name")}),
			patch.object(query_source, "gate_report", wraps=real_gate) as gate,
			patch("jarvis.api.telemetry.record_budget_event") as record,
		):
			result, _ = self.dispatch("run_report", {"report_name": self.rows_report, "filters": {}})
		self.assertTrue(result["ok"], result)
		data = result["data"]
		self.assertNotIn("run", data)
		self.assertTrue(data["note"].startswith("Result truncated to fit the context window"))
		self.assertEqual(record.call_args.kwargs["snapshot"], "skipped:gate")
		self.assertEqual(gate.call_count, 1)
		self.assertEqual(gate.call_args.args, (self.rows_report, {}, "Administrator"))
		self.assertEqual(_snapshot_keys(), [])

	def test_a_report_that_passes_the_read_gate_is_still_snapshotted(self):
		from jarvis.tools import _report_query_source as query_source

		with patch.object(query_source, "gate_report", wraps=query_source.gate_report) as gate:
			result, _ = self.dispatch("run_report", {"report_name": self.rows_report, "filters": {}})
		self.assertRegex(result["data"]["run"], r"^rs_")
		self.assertEqual(gate.call_count, 1)

	def test_a_measure_failure_makes_no_snapshot(self):
		with (
			patch("jarvis.api.measure", return_value=None),
			patch("jarvis.api._report_snapshots.create") as create,
		):
			result, _ = self.dispatch(
				"run_report", {"report_name": "R"}, run_tool=lambda *a, **k: self.big_report()
			)
		create.assert_not_called()
		self.assertNotIn("run", result["data"])

	def test_a_prepared_report_result_is_not_snapshotted(self):
		prepared = self.big_report(prepared_report=True, status="ready", run="PREP-0001")
		with patch("jarvis.api._report_snapshots.create") as create:
			result, _ = self.dispatch("run_report", {"report_name": "R"}, run_tool=lambda *a, **k: prepared)
		create.assert_not_called()
		self.assertEqual(result["data"]["run"], "PREP-0001")
		self.assertTrue(result["data"]["note"].startswith("Partial prepared-report preview"))

	def test_another_tool_is_not_snapshotted(self):
		rows = [{"name": f"R{i}", "blob": "x" * 90} for i in range(600)]
		with patch("jarvis.api._report_snapshots.create") as create:
			result, _ = self.dispatch(
				"query",
				{"from": "ToDo"},
				run_tool=lambda *a, **k: {"ok": True, "data": {"sql": "s", "rows": rows}},
			)
		create.assert_not_called()
		self.assertTrue(result["data"]["_truncated"])
		self.assertNotIn("run", result["data"])

	def test_a_private_output_result_is_not_budgeted_or_snapshotted(self):
		with (
			patch("jarvis.api._report_snapshots.create") as create,
			patch.object(_private_output, "allowed", return_value=True),
			patch.object(_private_output, "validate_args"),
			patch.object(_private_output, "prepare", side_effect=lambda result, tool: (result, result)),
		):
			result, _ = self.dispatch(
				"run_report",
				{"report_name": "R"},
				run_tool=lambda *a, **k: self.big_report(),
				result_mode=_private_output.MODE,
			)
		create.assert_not_called()
		self.assertEqual(len(result["data"]["result"]), 600)
		self.assertNotIn("_truncated", result["data"])
		self.assertNotIn("run", result["data"])

	def test_a_failed_run_report_is_not_snapshotted(self):
		error = {"ok": False, "error": {"code": "X", "message": "y"}}
		with patch("jarvis.api._report_snapshots.create") as create:
			result, _ = self.dispatch("run_report", {"report_name": "R"}, run_tool=lambda *a, **k: error)
		create.assert_not_called()
		self.assertEqual(result, error)

	def test_the_switch_off_leaves_the_call_as_it_was_before(self):
		with patch.dict(frappe.conf, {"jarvis_report_snapshots": 0}):
			result, _ = self.dispatch("run_report", {"report_name": self.rows_report})
		data = result["data"]
		self.assertNotIn("run", data)
		self.assertTrue(data["note"].startswith("Result truncated to fit the context window"))
		self.assertEqual(_snapshot_keys(), [])


class TestWhatIsPassedToCreate(SnapshotBudgetCase):
	def run_with_create(self, args, data_extra=None, run_tool=None):
		rows = [{"name": f"R{i}", "blob": "x" * 90} for i in range(600)]

		clock = _Clock()

		def fake_run(*a, **k):
			clock.now += 0.25
			return {
				"ok": True,
				"data": {"columns": [{"fieldname": "name"}], "result": rows, **(data_extra or {})},
			}

		with (
			patch("jarvis.api.time", clock),
			patch(
				"jarvis.api._report_snapshots.create",
				return_value=store.Created("rs_" + "a" * 32, "ok", 1234),
			) as create,
		):
			result, _ = self.dispatch("run_report", args, run_tool=run_tool or fake_run)
		return create, result

	def test_filters_are_normalised_and_scope_company_and_date_win(self):
		scope = {"company": "Acme", "report_date": "2026-01-02", "version": 1}
		create, _ = self.run_with_create(
			{"report_name": "R", "filters": '{"customer": "C1", "report_date": "2020-01-01"}'},
			{"report_scope": scope},
		)
		kwargs = create.call_args.kwargs
		self.assertEqual(
			kwargs["filters"], {"customer": "C1", "company": "Acme", "report_date": "2026-01-02"}
		)
		self.assertEqual(kwargs["report_name"], "R")

	def test_blank_filters_give_an_empty_dict_when_there_is_no_scope(self):
		create, _ = self.run_with_create({"report_name": "R"})
		self.assertEqual(create.call_args.kwargs["filters"], {})

	def test_run_ms_is_the_wall_time_of_the_call_in_milliseconds(self):
		create, _ = self.run_with_create({"report_name": "R"})
		run_ms = create.call_args.kwargs["run_ms"]
		self.assertIsInstance(run_ms, int)
		self.assertEqual(run_ms, 250)  # the injected clock moved 0.25 s inside the call

	def test_the_digest_passed_is_the_one_taken_before_the_run(self):
		"""Taken fresh before the run, so a stale per-request memo is not stamped on the rows."""
		pre_run = []

		def fake_run(*a, **k):
			pre_run.append(digest._compute_digest("Administrator", self.rows_report))
			return _big_run()

		setattr(frappe.local, digest._DIGEST_MEMO, {("Administrator", self.rows_report): "stale"})
		create, _ = self.run_with_create({"report_name": self.rows_report}, run_tool=fake_run)
		self.assertEqual(create.call_args.kwargs["perm_digest"], pre_run[0])

	def test_a_role_removed_during_the_run_is_not_stamped_on_the_rows(self):
		def fake_run(*a, **k):
			patcher = patch.object(frappe, "get_roles", return_value=["System Manager", "Another Role"])
			patcher.start()
			self.addCleanup(patcher.stop)
			return _big_run()

		before = digest._compute_digest("Administrator", self.rows_report)
		result, _ = self.dispatch("run_report", {"report_name": self.rows_report}, run_tool=fake_run)
		stored = json.loads(
			frappe.cache().get(frappe.cache().make_key(store.META_PREFIX + result["data"]["run"]))
		)
		self.assertEqual(stored["perm_digest"], before)
		self.assertNotEqual(before, digest._compute_digest("Administrator", self.rows_report))

	def test_no_digest_is_taken_for_another_tool(self):
		with patch("jarvis.api._report_snapshots.perm_digest") as taken:
			self.dispatch("list_docs", {"doctype": "ToDo"}, run_tool=lambda *a, **k: {"ok": True, "data": {}})
		taken.assert_not_called()

	def test_the_result_is_measured_once_for_the_snapshot_gate_and_the_guard(self):
		with patch("jarvis.api.measure", wraps=measure) as spy:
			self.run_with_create({"report_name": "R"})
		self.assertEqual(spy.call_count, 1)

	def run_taking(self, run_s, create_s=0.0):
		"""A run that takes `run_s` and a create that takes `create_s` on the injected clock."""
		clock = _Clock()

		def fake_run(*a, **k):
			clock.now += run_s
			return _big_run()

		def fake_create(*a, **k):
			clock.now += create_s
			return store.Created("rs_" + "a" * 32, "ok", 1234)

		with (
			patch("jarvis.api.time", clock),
			patch("jarvis.api._report_snapshots.create", side_effect=fake_create) as create,
			patch("jarvis.api.telemetry.record_budget_event") as record,
		):
			result, _ = self.dispatch("run_report", {"report_name": "R"}, run_tool=fake_run)
		return create, record.call_args.kwargs, result

	def test_a_run_that_took_too_long_is_not_snapshotted(self):
		create, event, result = self.run_taking(api.CREATE_SKIP_AFTER_MS / 1000)
		create.assert_not_called()
		self.assertEqual(event["snapshot"], "skipped:slow")
		self.assertNotIn("snapshot_create_ms", event)
		self.assertNotIn("run", result["data"])

	def test_a_run_just_under_the_limit_is_snapshotted(self):
		create, event, _ = self.run_taking(api.CREATE_SKIP_AFTER_MS / 1000 - 0.001)
		create.assert_called_once()
		self.assertEqual(event["snapshot"], "created")

	def test_the_create_time_is_recorded_when_a_create_ran(self):
		_, event, _ = self.run_taking(0.25, create_s=0.5)
		self.assertEqual(event["snapshot_create_ms"], 500)

	def test_the_id_and_flag_reach_the_guard_not_the_model(self):
		_, result = self.run_with_create({"report_name": "R"})
		self.assertEqual(result["data"]["run"], "rs_" + "a" * 32)
		self.assertNotIn("_jarvis_snapshot", result["data"])


class TestBudgetEventFields(FrappeTestCase):
	def emitted(self, **kwargs):
		with patch.object(telemetry, "_emit") as emit:
			telemetry.record_budget_event("run_report", "truncated", 50_000, 10, 400, **kwargs)
		return emit.call_args.args[0]

	def test_the_snapshot_fields_are_additive(self):
		line = self.emitted(snapshot="created", snapshot_bytes=1234)
		self.assertEqual((line["snapshot"], line["snapshot_bytes"]), ("created", 1234))
		self.assertEqual(line["outcome"], "truncated")
		self.assertEqual(line["kind"], "result_budget")

	def test_the_create_time_is_additive(self):
		self.assertEqual(self.emitted(snapshot="created", snapshot_create_ms=42)["snapshot_create_ms"], 42)

	def test_without_them_the_line_is_as_before(self):
		line = self.emitted()
		self.assertNotIn("snapshot", line)
		self.assertNotIn("snapshot_bytes", line)
		self.assertNotIn("snapshot_create_ms", line)

	def test_a_skip_reason_is_kept(self):
		self.assertEqual(self.emitted(snapshot="skipped:tree")["snapshot"], "skipped:tree")


class TestSkillRowGate(SnapshotBudgetCase):
	"""B.6: a snapshot read is judged on the report its own receipt names."""

	RUN = "rs_" + "a" * 32

	def reads(self, data, ref_is_skill=True):
		with patch("jarvis.api._is_a_skill_doctype", side_effect=lambda dt: ref_is_skill and dt == "ToDo"):
			return api._reads_skill_rows("query_report", {"prepared_report": self.RUN}, data)

	def test_a_snapshot_of_a_skill_doctype_report_is_a_skill_read(self):
		self.assertTrue(self.reads({"report_name": self.small_report}))

	def test_a_snapshot_of_another_report_is_not(self):
		self.assertFalse(self.reads({"report_name": self.small_report}, ref_is_skill=False))

	def test_a_receipt_without_a_report_name_is_judged_as_a_skill_read(self):
		for data in (None, {}, {"report_name": ""}, {"report_name": None}, {"report_name": 5}, "x"):
			with self.subTest(data=data):
				self.assertTrue(self.reads(data, ref_is_skill=False))

	def test_the_data_is_optional_and_the_old_two_argument_call_still_works(self):
		self.assertTrue(api._reads_skill_rows("query_report", {"prepared_report": self.RUN}))
		self.assertFalse(api._reads_skill_rows("get_list", {"doctype": "ToDo"}))

	def test_a_prepared_report_id_is_still_judged_by_its_own_row(self):
		with patch("jarvis.api._is_a_skill_doctype", return_value=True):
			self.assertFalse(
				api._reads_skill_rows(
					"query_report", {"prepared_report": "PR-none"}, {"report_name": self.small_report}
				)
			)

	def dispatch_judged(self, data, ref_is_skill):
		from jarvis.chat import macros

		with (
			patch("jarvis.api._dispatch_and_wrap", return_value={"ok": True, "data": data}),
			patch("jarvis.api._is_a_skill_doctype", side_effect=lambda dt: ref_is_skill and dt == "ToDo"),
			patch.object(macros, "check_skill_rows_read", return_value="note") as judge,
		):
			_, note = api._dispatch_judging_skills(
				"query_report", {"prepared_report": self.RUN}, False, "conv-1"
			)
		return judge, note

	def test_the_dispatch_judges_a_skill_report_snapshot_in_a_chat(self):
		judge, note = self.dispatch_judged({"report_name": self.small_report}, True)
		judge.assert_called_once()
		self.assertEqual(note, "note")

	def test_the_dispatch_does_not_judge_another_report_snapshot(self):
		judge, note = self.dispatch_judged({"report_name": self.small_report}, False)
		judge.assert_not_called()
		self.assertEqual(note, "")

	def test_the_dispatch_judges_a_snapshot_whose_receipt_has_no_report(self):
		judge, _ = self.dispatch_judged({"rows": []}, False)
		judge.assert_called_once()


class TestRunAsIsNotTheConversationOwner(SnapshotBudgetCase):
	"""Plan section 6: an agent run whose run-as user is not the conversation owner stores the
	snapshot under the run-as user, who (and only who) can read it."""

	def test_the_run_as_user_owns_and_reads_the_snapshot_not_the_conversation_owner(self):
		runner = f"snap-run-as-{self.tag}@example.test"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": runner,
				"first_name": "Run As",
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "User", runner, force=True, ignore_permissions=True)
		conversation = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "run-as owner"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", conversation.name, "session_key", SESSION_KEY)
		self.addCleanup(
			frappe.delete_doc, "Jarvis Conversation", conversation.name, force=True, ignore_permissions=True
		)
		self.assertEqual(
			frappe.db.get_value("Jarvis Conversation", conversation.name, "owner"), "Administrator"
		)
		with ExitStack() as stack:
			stack.enter_context(patch("jarvis.api._get_header", return_value=None))
			stack.enter_context(patch("jarvis.api._persist_and_publish_tool_call"))
			cut = api._dispatch_from_session(
				runner, SESSION_KEY, "run_report", {"report_name": self.rows_report}
			)
			run = cut["data"]["run"]
			meta = json.loads(frappe.cache().get(frappe.cache().make_key(store.META_PREFIX + run)))
			self.assertEqual(meta["owner"], runner)
			read = api._dispatch_from_session(runner, SESSION_KEY, "query_report", {"prepared_report": run})
			self.assertTrue(read["ok"], read)
			self.assertEqual(read["data"]["prepared_report"], run)
			foreign = api._dispatch_from_session(
				"Administrator", SESSION_KEY, "query_report", {"prepared_report": run}
			)
		self.assertFalse(foreign["ok"])
		self.assertIn("expired or is not available", foreign["error"]["message"])
