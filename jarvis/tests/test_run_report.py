import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.core.doctype.prepared_report.prepared_report import (
	PreparedReport,
	process_filters_for_prepared_report,
)
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import _prepared_reports
from jarvis.tools.run_report import run_report

INLINE_REPORT = "Jarvis Test Inline Report"
PREP_REPORT = "Jarvis Test Prepared Report"

# after_insert on Prepared Report enqueues a real 25-min generate_report job; in
# tests we never want it to run (and it wouldn't inline anyway), so we no-op the
# enqueue at the module where it is bound.
_ENQUEUE = "frappe.core.doctype.prepared_report.prepared_report.enqueue"


def _ensure_report(name: str, prepared: int) -> None:
	"""Hermetic Query Report over ToDo. Idempotent; left in place across tests."""
	if frappe.db.exists("Report", name):
		return
	frappe.get_doc(
		{
			"doctype": "Report",
			"report_name": name,
			"ref_doctype": "ToDo",
			"report_type": "Query Report",
			"is_standard": "No",
			"query": "select name, description from `tabToDo` order by creation desc limit 5",
			"prepared_report": prepared,
			"disabled": 0,
		}
	).insert(ignore_permissions=True)


def _make_pr(status, *, owner="Administrator", filters=None, age_seconds=0):
	"""Create a Prepared Report row for PREP_REPORT in a given state and back-date
	its creation. Stores NO gz attachment - completed-copy reads are simulated by
	patching get_prepared_data (see _completed), because the real
	create_json_gz_file physical-file round-trip is flaky under run-parallel-tests."""
	with patch(_ENQUEUE):
		pr = frappe.get_doc(
			{
				"doctype": "Prepared Report",
				"report_name": PREP_REPORT,
				"filters": process_filters_for_prepared_report(filters or {}),
			}
		).insert(ignore_permissions=True)
	updates = {"status": status, "owner": owner}
	if status == "Completed":
		updates["report_end_time"] = now()
	frappe.db.set_value("Prepared Report", pr.name, updates, update_modified=False)
	if age_seconds:
		frappe.db.set_value(
			"Prepared Report",
			pr.name,
			"creation",
			add_to_date(now(), seconds=-age_seconds),
			update_modified=False,
		)
	return pr.name


@contextmanager
def _completed(result, *, columns=None, filters=None, owner="Administrator"):
	"""A Completed Prepared Report row whose stored output is `result`, made
	readable WITHOUT touching disk: get_prepared_data returns the decompressed
	json bytes the real method would (its physical gz round-trip is unreliable
	under run-parallel-tests). Yields the row name."""
	dn = _make_pr("Completed", owner=owner, filters=filters)
	payload = json.dumps({"result": result, "columns": columns or []}).encode("utf-8")
	with patch.object(PreparedReport, "get_prepared_data", return_value=payload):
		yield dn


class TestRunReportInline(FrappeTestCase):
	"""The non-prepared path must stay byte-identical - no envelope, no status."""

	def setUp(self):
		_ensure_report(INLINE_REPORT, prepared=0)
		# Commit the fixture so run()'s @read_only ("START TRANSACTION READ ONLY")
		# doesn't hit an ImplicitCommitError on the pending insert (same reason
		# test_dashboards_api commits its fixtures in setUp).
		frappe.db.commit()

	def test_runs_known_report(self):
		company = frappe.defaults.get_global_default("company")
		if not company:
			self.skipTest("test bench has no default company; happy-path covered in E2E task")
		result = run_report(
			report_name="Sales Register",
			filters={"from_date": "2020-01-01", "to_date": "2020-01-02", "company": company},
		)
		self.assertIn("columns", result)
		self.assertIn("result", result)

	def test_inline_report_returns_no_status_envelope(self):
		result = run_report(report_name=INLINE_REPORT)
		self.assertIn("columns", result)
		self.assertIn("result", result)
		# regression: inline path is untouched - Frappe's run() dict has status=None
		# and no prepared_report marker, unlike our prepared-report envelope.
		self.assertNotIn("prepared_report", result)
		self.assertIsNone(result.get("status"))

	def test_rejects_unknown_report(self):
		with self.assertRaises(InvalidArgumentError):
			run_report(report_name="Definitely Not A Report")

	def test_rejects_missing_report_name(self):
		with self.assertRaises(InvalidArgumentError):
			run_report(report_name="")

	def test_permission_check_blocks_unauthorized_user(self):
		if not frappe.db.exists("Report", "Sales Register"):
			self.skipTest("Sales Register needs erpnext; permission path covered in CI")
		user_email = "reportless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Reportless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				run_report(report_name="Sales Register")
		finally:
			frappe.set_user("Administrator")


class TestRunReportPrepared(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_ensure_report(PREP_REPORT, prepared=1)
		# A Prepared Report row does not roll back with the test transaction, so a
		# row from one test would leak into the next; start each test from a clean
		# slate (scoped to this fixture report, so it's parallel-safe).
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		frappe.db.commit()

	# ---- trigger + envelope shape -------------------------------------------
	def test_prepared_started_when_nothing_exists(self):
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with patch(_ENQUEUE):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "started")
		self.assertTrue(env["prepared_report"])  # keeps dashboards_api backstop intact
		self.assertIn("message", env)
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before + 1)

	# ---- worker/queue unavailable => RAISE (not a silent envelope) ----------
	def test_scheduler_paused_raises_without_triggering(self):
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with (
			patch.object(frappe, "in_test", False),
			patch.dict(frappe.conf, {"developer_mode": 0}),
			patch("jarvis.tools._prepared_reports.is_scheduler_inactive", return_value=True),
		):
			with self.assertRaises(InvalidArgumentError):
				run_report(report_name=PREP_REPORT)
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before)

	def test_queue_unreachable_raises_without_triggering(self):
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with (
			patch.object(frappe, "in_test", False),
			patch.dict(frappe.conf, {"developer_mode": 0}),
			patch("jarvis.tools._prepared_reports.is_scheduler_inactive", return_value=False),
			patch("jarvis.tools._prepared_reports._queue_reachable", return_value=False),
		):
			with self.assertRaises(InvalidArgumentError):
				run_report(report_name=PREP_REPORT)
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before)

	# ---- completed copy reuse -----------------------------------------------
	def test_ready_from_completed_copy(self):
		with _completed([{"name": "T1", "description": "hi"}], columns=[{"label": "Name"}]):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["result"], [{"name": "T1", "description": "hi"}])
		self.assertTrue(env["as_of"])

	def test_empty_completed_is_ready_with_zero_rows(self):
		with _completed([]):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["result"], [])  # 0 rows is a COMPLETE answer, not "no data"

	def test_null_result_completed_is_ready_with_zero_rows(self):
		# A report that stores result=None (columns but no rows) is a genuine 0-row
		# answer - it must be `ready`, not fall through and regenerate forever.
		with _completed(None, columns=[{"label": "Name"}]):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["result"], [])

	def test_completed_with_unreadable_output_is_not_ready(self):
		# Completed but its stored output won't read (get_prepared_data throws) ->
		# must NOT be "ready"; it falls through and (re)generates instead.
		_make_pr("Completed")  # no attachment -> real get_prepared_data throws
		with patch(_ENQUEUE):
			env = run_report(report_name=PREP_REPORT)
		self.assertNotEqual(env["status"], "ready")

	def test_row_cap_applied_and_note_reports_true_total(self):
		total = _prepared_reports._MAX_ROWS + 25
		big = [{"name": f"T{i}"} for i in range(total)]
		with _completed(big):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(len(env["result"]), _prepared_reports._MAX_ROWS)
		self.assertIn(str(total), env["row_note"])  # note reports the UNCAPPED total

	def test_canonical_filters_round_trip_reuses_completed_copy(self):
		# A completed copy stored for {"company":"Acme"} must be reused when the
		# agent passes the same logical filters plus a bookkeeping/empty key.
		with _completed([{"name": "X"}], filters={"company": "Acme"}):
			env = run_report(
				report_name=PREP_REPORT,
				filters={"company": "Acme", "prepared_report_name": "junk", "cost_center": ""},
			)
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["result"], [{"name": "X"}])

	# ---- in-flight + stall self-heal ----------------------------------------
	def test_generating_when_recent_started(self):
		_make_pr("Started", age_seconds=30)
		env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "generating")

	def test_generating_when_fresh_queued(self):
		_make_pr("Queued", age_seconds=5)
		env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "generating")

	def test_stalled_started_retriggers(self):
		# A Started row past its deadline is a stuck job - re-asking must start a
		# fresh run, not dead-end on a non-actionable "stalled".
		_make_pr("Started", age_seconds=_prepared_reports._DEFAULT_REPORT_TIMEOUT + 600)
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with patch(_ENQUEUE):
			env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "started")
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before + 1)

	def test_old_queued_stays_generating_no_retrigger(self):
		# A Queued row is enqueued and waiting - even an old one must NOT re-trigger
		# (that would pile duplicate long-queue jobs); it reports generating.
		_make_pr("Queued", age_seconds=3600)
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "generating")
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before)

	# ---- terminal failure is surfaced, not looped ---------------------------
	def test_errored_report_surfaced_as_failed_without_retrigger(self):
		_make_pr("Error")
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "failed")
		# must NOT re-fire a fresh doomed job
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before)

	def test_newer_inflight_wins_over_older_error(self):
		# An old Error must not shadow a newer in-flight run: the newest attempt
		# decides. Older failed row, then a fresh Started -> generating, not failed.
		_make_pr("Error", age_seconds=300)
		_make_pr("Started", age_seconds=10)
		env = run_report(report_name=PREP_REPORT)
		self.assertEqual(env["status"], "generating")

	# ---- filter guards ------------------------------------------------------
	def test_filter_permission_gate_denies_before_triggering(self):
		# The validate_filters_permissions gate is the only record-level Link-filter
		# check on the ignore_permissions trigger/read path. With non-empty filters,
		# a denial must translate to PermissionDeniedError AND create nothing.
		before = frappe.db.count("Prepared Report", {"report_name": PREP_REPORT})
		with patch(
			"jarvis.tools._prepared_reports.validate_filters_permissions",
			side_effect=frappe.ValidationError("You do not have permission to access Company: Acme."),
		):
			with self.assertRaises(PermissionDeniedError):
				run_report(report_name=PREP_REPORT, filters={"company": "Acme"})
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": PREP_REPORT}), before)

	def test_malformed_filters_raise_invalid_argument(self):
		with self.assertRaises(InvalidArgumentError):
			run_report(report_name=PREP_REPORT, filters=[1, 2, 3])
		with self.assertRaises(InvalidArgumentError):
			run_report(report_name=PREP_REPORT, filters="not json")

	# ---- C1: completed copy is readable WITHOUT the Prepared Report role -----
	def test_read_completed_needs_no_prepared_report_role(self):
		# _read_completed uses frappe.get_doc (no Prepared Report read role needed),
		# never run()-by-name (which throws for a roleless user).
		with _completed([{"name": "T1"}], owner="reportless2@example.com") as dn:
			if not frappe.db.exists("User", "reportless2@example.com"):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": "reportless2@example.com",
						"first_name": "Reportless2",
						"send_welcome_email": 0,
					}
				).insert(ignore_permissions=True)
			frappe.set_user("reportless2@example.com")
			try:
				env = _prepared_reports._read_completed(dn)
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(env["status"], "ready")
		self.assertEqual(env["result"], [{"name": "T1"}])

	# ---- permission gate: Has Role on the report ----------------------------
	def test_has_role_restricted_report_is_denied(self):
		gated = "Jarvis Test Gated Prepared Report"
		if not frappe.db.exists("Report", gated):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": gated,
					"ref_doctype": "ToDo",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tabToDo` limit 1",
					"prepared_report": 1,
					"disabled": 0,
					"roles": [{"role": "System Manager"}],
				}
			).insert(ignore_permissions=True)
		user_email = "gated-reportless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "GatedReportless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		before = frappe.db.count("Prepared Report", {"report_name": gated})
		try:
			with self.assertRaises(PermissionDeniedError):
				run_report(report_name=gated)
		finally:
			frappe.set_user("Administrator")
		# denied BEFORE any generation is triggered
		self.assertEqual(frappe.db.count("Prepared Report", {"report_name": gated}), before)
