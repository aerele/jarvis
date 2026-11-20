"""The manual acceptance functions of the built-in reader: site guards, output hygiene, tiny smoke runs.

The real runs (playground, full drift, guard size) are done by hand. Here each function is proven on
`test_jarvis` at a tiny size, and each refuses a wrong site before it reads anything.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.tests import payment_calendar_reader_acceptance as acceptance
from jarvis.tests.payment_calendar_acceptance_support import (
	read_only_session,
	result_diff,
	stale_headers,
)
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	require_calendar_test_site,
	sync_outstanding,
)

SIDES = ("Receivable", "Payable")
CALLS = {
	"playground": {"company": "X"},
	"timing": {"company": "X"},
	"drift": {"seed": 1, "ops": 1},
	"timing_guard_size": {"vouchers": 1, "ledger_rows": 1},
}
SITES = {
	"playground": "test_jarvis",
	"timing": "test_jarvis",
	"drift": "jarvis_playground.localhost",
	"timing_guard_size": "jarvis_playground.localhost",
}


# Code that forks (`run_isolated`) runs in a fresh interpreter. Forking inside the test runner process makes
# that process segfault at exit (its many loaded extensions do not survive a fork at finalization), which
# `bench execute`, the real way to run the acceptance functions, does not.
HARNESS = (
	"import json, sys, frappe\n"
	"frappe.init(sys.argv[1], sites_path=sys.argv[2])\n"
	"frappe.connect()\n"
	"from jarvis.tests.test_payment_calendar_reader_acceptance import PROBES\n"
	"print('RESULT:' + json.dumps(PROBES[sys.argv[3]](**json.loads(sys.argv[4])), default=str))\n"
)
FAKE_ARGS = ("X", "Payable", "2025-06-16", "2025-01-01", "2025-12-31")


def in_fresh_process(probe: str, **kwargs):
	done = subprocess.run(
		[sys.executable, "-c", HARNESS, frappe.local.site, os.path.abspath(frappe.local.sites_path), probe]
		+ [json.dumps(kwargs)],
		capture_output=True,
		text=True,
		timeout=600,
	)
	if done.returncode != 0:
		raise AssertionError(f"probe {probe} failed ({done.returncode}):\n{done.stderr[-3000:]}")
	line = next(line for line in reversed(done.stdout.splitlines()) if line.startswith("RESULT:"))
	return json.loads(line[len("RESULT:") :])


def probe_timed_runs(company, today):
	with read_only_session():
		return acceptance._timed_runs(company, "Payable", today, 3, view=True)


def probe_isolated_read_keeps_the_connection(company, today):
	before = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
	for view in (True, False):
		if not view:
			frappe.db.savepoint("iso_probe")
		data = acceptance._timed_runs(company, "Payable", today, 1, view=view)
		assert data["runs"][0]["outcome"] == "served", data
		assert frappe.db.sql("SELECT CONNECTION_ID()")[0][0] == before
		if not view:
			# The savepoint of the parent survived the child's reads.
			frappe.db.rollback(save_point="iso_probe")
	frappe.db.sql("SELECT 1")
	return {"ok": True}


def probe_read_with_memory(kind):
	from jarvis.payment_calendar import builtin_reader

	def light(*args, stats=None, **kwargs):
		return {"rows": []}

	def heavy(*args, stats=None, **kwargs):
		block = b"x" * (200 << 20)
		return {"rows": [len(block)]}

	spent = b"x" * (300 << 20)  # raises the parent's lifetime peak, then goes
	del spent
	held = b"x" * (200 << 20)  # still resident at the fork
	with patch.object(builtin_reader, "read_obligations", heavy if kind == "heavy" else light):
		record = acceptance._timed_read(FAKE_ARGS, False)
	record["held_mb"] = len(held) >> 20
	return record


def probe_guard_size(out):
	return acceptance.timing_guard_size(out, vouchers=40, ledger_rows=400)


def probe_child_failure():
	"""A child that raises: the parent gets a RuntimeError and the traceback is on the child's stderr."""
	from jarvis.tests.payment_calendar_acceptance_support import run_isolated

	def fails():
		raise ValueError("child-boom")

	with tempfile.TemporaryFile(mode="w+") as captured:
		saved = os.dup(2)
		os.dup2(captured.fileno(), 2)
		try:
			try:
				run_isolated(fails, reopen=False)
				error = None
			except RuntimeError as exc:
				error = str(exc)
		finally:
			os.dup2(saved, 2)
			os.close(saved)
		captured.seek(0)
		return {"error": error, "stderr": captured.read()}


def probe_parent_interrupt_kills_the_child():
	"""An exception in the parent while it waits: the child is killed and reaped (no zombie, no orphan)."""
	from jarvis.tests import payment_calendar_acceptance_support as support

	pids = []
	real_fork = os.fork

	def recording_fork():
		pid = real_fork()
		if pid:
			pids.append(pid)
		return pid

	def sleeper():
		time.sleep(60)
		return {}

	with (
		patch.object(support.os, "fork", recording_fork),
		# The parent's first `fdopen` is the read of the child's pipe; the child never gets there.
		patch.object(support.os, "fdopen", side_effect=KeyboardInterrupt),
	):
		try:
			support.run_isolated(sleeper, reopen=False)
		except KeyboardInterrupt:
			pass
	try:
		os.kill(pids[0], 0)
		alive = True
	except ProcessLookupError:
		alive = False
	return {"alive": alive}


PROBES = {
	"child_failure": probe_child_failure,
	"interrupt": probe_parent_interrupt_kills_the_child,
	"timed_runs": probe_timed_runs,
	"connection": probe_isolated_read_keeps_the_connection,
	"memory": probe_read_with_memory,
	"guard_size": probe_guard_size,
}


def row(**changes):
	base = {
		"id": "a",
		"voucher_type": "Sales Invoice",
		"voucher_no": "SI-1",
		"party": "Acme",
		"account": "Debtors",
		"term": "",
		"due_date": "2025-06-30",
		"remaining": "10",
	}
	return {**base, **changes}


def result(*rows, **changes):
	return {"rows": list(rows), "excluded_rows": [], "notes": {}, "canonical_total": "10", **changes}


class TestAcceptanceSiteGuards(TestCase):
	def setUp(self):
		require_calendar_test_site()
		self.out = Path(tempfile.mkdtemp()) / "out.json"

	def test_each_function_refuses_the_wrong_site_before_any_read(self):
		queries = []
		real = frappe.db.sql

		def spy(query, *args, **kwargs):
			queries.append(str(query))
			return real(query, *args, **kwargs)

		for name, kwargs in CALLS.items():
			with self.subTest(name):
				with (
					patch.object(frappe.local, "site", SITES[name]),
					patch.object(frappe.db, "sql", spy),
					self.assertRaises(RuntimeError) as raised,
				):
					getattr(acceptance, name)(out=str(self.out), **kwargs)
				self.assertIn("only", str(raised.exception))
		self.assertEqual(queries, [])
		self.assertFalse(self.out.exists())

	def test_only_the_calendar_test_sites_are_accepted_whatever_allow_tests_says(self):
		from jarvis.tests.payment_calendar_acceptance_support import PLAYGROUND_SITE, require_site

		def accepted(site, ci):
			env = {"CI": "true"} if ci else {}
			with (
				patch.object(frappe.local, "site", site),
				patch.dict(frappe.conf, {"allow_tests": 1}),
				patch.dict("os.environ", env),
			):
				if not ci:
					os.environ.pop("CI", None)
				try:
					require_site("test_jarvis")
				except RuntimeError:
					return False
				return True

		self.assertTrue(accepted("test_jarvis", False))
		self.assertTrue(accepted("jarvis-ci.test", True))
		self.assertFalse(accepted("jarvis-ci.test", False), "the CI site only under CI")
		self.assertFalse(accepted("other.example.com", True), "allow_tests alone is not a test site")
		self.assertFalse(accepted(PLAYGROUND_SITE, True))

	def test_the_output_must_be_outside_the_repository_and_its_folder_must_exist(self):
		from jarvis.tests.payment_calendar_acceptance_support import REPO, prepare_out

		for bad in (
			REPO / "jarvis" / "out.json",
			Path(tempfile.gettempdir()) / "no-such-folder" / "out.json",
		):
			with self.subTest(str(bad)), self.assertRaises(RuntimeError):
				prepare_out(str(bad))
		self.assertEqual(prepare_out(str(self.out)), self.out.resolve())

	def test_the_output_must_not_be_in_any_git_work_tree_or_public_directory(self):
		from jarvis.tests.payment_calendar_acceptance_support import prepare_out

		root = Path(tempfile.mkdtemp())
		(root / "clone" / ".git").mkdir(parents=True)
		(root / "linked").mkdir()
		(root / "linked" / ".git").write_text("gitdir: elsewhere\n")  # a linked worktree has a file
		(root / "site" / "public" / "files").mkdir(parents=True)
		for name in ("clone", "linked", "site/public/files"):
			with self.subTest(name), self.assertRaises(RuntimeError):
				prepare_out(str(root / name / "out.json"))

	def test_the_output_file_is_owner_only_also_when_it_existed(self):
		from jarvis.tests.payment_calendar_acceptance_support import write_out

		self.out.write_text("old")
		self.out.chmod(0o644)
		write_out(self.out, {"a": 1})
		self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
		self.assertEqual(json.loads(self.out.read_text()), {"a": 1})

	def test_digests_use_a_domain_key_not_the_site_key(self):
		import hashlib
		import hmac

		from jarvis.tests.payment_calendar_acceptance_support import digest

		with patch.dict(frappe.conf, {"encryption_key": "site-key"}):
			domain_key = hmac.new(b"site-key", b"pcal-acceptance", hashlib.sha256).digest()
			expected = hmac.new(domain_key, b"Acme", hashlib.sha256).hexdigest()[:16]
			plain = hmac.new(b"site-key", b"Acme", hashlib.sha256).hexdigest()[:16]
			self.assertEqual(digest("Acme"), expected)
			self.assertNotEqual(digest("Acme"), plain)


class TestResultDiff(TestCase):
	def test_the_content_diff_leaves_ids_to_the_comparison_that_counts_them(self):
		found = result_diff(result(row(id="a")), result(row(id="b")))
		self.assertEqual(found["diffs"], 0)

	def test_an_id_difference_is_counted_as_a_difference(self):
		from jarvis.tests.payment_calendar_acceptance_support import compare_reads

		same = compare_reads((result(row(id="a")), None), (result(row(id="a")), None))
		other = compare_reads((result(row(id="a")), None), (result(row(id="b")), None))
		self.assertEqual((same["ids"], same["diffs"]), ("equal", 0))
		self.assertEqual((other["ids"], other["diffs"]), ("differ", 1))

	def test_a_difference_names_fields_and_digests_never_values(self):
		found = result_diff(
			result(row(remaining="11"), row(voucher_no="SI-2")),
			result(row(), row(voucher_no="SI-3")),
		)
		self.assertEqual(found["diffs"], 3)
		details = found["details"]["rows"]
		self.assertEqual(details["changed"][0]["fields"], ["remaining"])
		self.assertEqual((len(details["missing"]), len(details["extra"])), (1, 1))
		text = json.dumps(found)
		for name in ("SI-1", "SI-2", "SI-3", "Acme"):
			self.assertNotIn(name, text)

	def test_a_top_level_difference_counts_by_field_name(self):
		found = result_diff(result(row(), notes={"a": 1}), result(row(), notes={"a": 2}))
		self.assertEqual(found["details"]["top_level_fields"], ["notes"])
		self.assertEqual(found["diffs"], 1)


class TestAcceptanceRuns(TestCase):
	"""Committed fixtures with cleanup: `consistent_read` rolls back first, so a savepoint would not survive."""

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		self.out = Path(tempfile.mkdtemp()) / "out.json"
		self.fx = None

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for doctype, name in reversed(self.fx.created if self.fx else []):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		frappe.clear_cache()

	def committed_company(self):
		self.fx = CalendarFixtures()
		for side in SIDES:
			self.fx.invoice((40, 60), side=side, paid=30, credit=5)
			self.fx.invoice((100,), side=side, paid=100)
		sync_outstanding(self.fx.company)
		frappe.db.commit()
		return self.fx.company

	def test_playground_comparison_runs_in_a_read_only_session(self):
		company = self.committed_company()
		with read_only_session():
			records = {side: acceptance._compare_side(company, side, self.fx.today) for side in SIDES}
			with self.assertRaises(Exception):
				frappe.db.sql("UPDATE tabSeries SET current = current WHERE name = '_none_'")
		for side, record in records.items():
			with self.subTest(side):
				self.assertEqual((record["outcome"], record["diffs"]), ("compared", 0))
				self.assertIsNone(record["fallback"])
				self.assertTrue(record["control_passed"])
				self.assertIsNone(record["contract"])
				self.assertEqual(record["settled_rows"]["reader"], record["settled_rows"]["today"])
				self.assertEqual(record["headers"]["stale"], 0)
		# The session is writable again afterwards.
		frappe.db.sql("UPDATE tabSeries SET current = current WHERE name = '_none_'")

	def run_playground_here(self, **kwargs):
		"""`playground` on this test site: only the site name check is pointed at it."""
		with patch.object(acceptance, "PLAYGROUND_SITE", frappe.local.site):
			return acceptance.playground(self.fx.company, str(self.out), anchor=self.fx.today, **kwargs)

	def test_skip_eligibility_refuses_a_session_that_is_not_administrator(self):
		company = self.committed_company()
		frappe.set_user("Guest")
		for name, call in (
			("playground", lambda: acceptance.playground(company, str(self.out), skip_eligibility=True)),
			("timing", lambda: acceptance.timing(company, str(self.out), skip_eligibility=True)),
		):
			with (
				self.subTest(name),
				patch.object(acceptance, "PLAYGROUND_SITE", frappe.local.site),
				self.assertRaises(RuntimeError) as raised,
			):
				call()
			self.assertIn("Administrator", str(raised.exception))
		self.assertFalse(self.out.exists())

	def test_skip_eligibility_reads_where_an_unaudited_hook_makes_the_gate_refuse(self):
		self.committed_company()
		real = frappe.get_hooks

		def hooks(hook=None, *args, **kwargs):
			if hook == "has_permission":
				return {"Sales Invoice": ["some_app.access.has_permission"]}
			return real(hook, *args, **kwargs)

		with patch.object(frappe, "get_hooks", hooks):
			refused = self.run_playground_here()
			summary = self.run_playground_here(skip_eligibility=True)
		self.assertEqual(refused["fallbacks"], ["ineligible", "ineligible"])
		self.assertFalse(refused["eligibility_skipped"])
		self.assertEqual((summary["fallbacks"], summary["diffs"]), ([], 0))
		self.assertTrue(summary["eligibility_skipped"])
		self.assertTrue(summary["control_passed"])
		self.assertTrue(json.loads(self.out.read_text())["summary"]["eligibility_skipped"])

	def test_stale_headers_counts_a_header_that_differs_from_the_ledger(self):
		company = self.committed_company()
		invoice = frappe.get_all("Sales Invoice", filters={"company": company, "docstatus": 1}, pluck="name")[
			0
		]
		frappe.db.set_value("Sales Invoice", invoice, "outstanding_amount", 1234, update_modified=False)
		self.assertEqual(stale_headers(company, "Receivable")["stale"], 1)
		self.assertEqual(stale_headers(company, "Payable")["stale"], 0)

	def test_timing_runs_cold_then_warm_and_names_the_per_read_timers(self):
		company = self.committed_company()
		data = in_fresh_process("timed_runs", company=company, today=self.fx.today)
		self.assertEqual([run["kind"] for run in data["runs"]], ["cold", "warm", "warm"])
		for run in data["runs"]:
			self.assertEqual(run["outcome"], "served")
			self.assertTrue(
				{"control", "due_window", "multi", "eligibility", "build"} <= set(run["timings_ms"])
			)
			self.assertTrue(run["control"]["passed"])
			self.assertGreater(run["peak_rss_mb"], 0)
			self.assertGreaterEqual(run["read_growth_mb"], 0)
		self.assertEqual(data["max_seconds"], max(run["seconds"] for run in data["runs"]))

	def test_a_read_peak_excludes_memory_the_parent_holds_or_held(self):
		record = in_fresh_process("memory", kind="light")
		self.assertGreaterEqual(record["baseline_rss_mb"], record["held_mb"])
		self.assertGreaterEqual(record["peak_rss_mb"], record["held_mb"])
		self.assertLess(record["read_growth_mb"], 100)

	def test_a_read_peak_includes_what_the_read_itself_allocates(self):
		record = in_fresh_process("memory", kind="heavy")
		self.assertGreaterEqual(record["read_growth_mb"], 150)

	def test_a_failing_child_prints_its_traceback_to_stderr(self):
		record = in_fresh_process("child_failure")
		self.assertIn("wait status", record["error"])
		self.assertIn("ValueError: child-boom", record["stderr"])

	def test_an_interrupted_parent_kills_and_reaps_the_child(self):
		self.assertFalse(in_fresh_process("interrupt")["alive"])

	def test_an_isolated_read_leaves_the_parents_connection_and_transaction_alone(self):
		company = self.committed_company()
		self.assertTrue(in_fresh_process("connection", company=company, today=self.fx.today)["ok"])

	def test_drift_on_a_tiny_seed_is_isolated_and_writes_no_names(self):
		before = frappe.db.count("Payment Ledger Entry")
		summary = acceptance.drift(2, 12, str(self.out))
		self.assertEqual((summary["diffs"], summary["fallbacks"], summary["commits"]), (0, 0, 0))
		self.assertTrue(summary["isolation_equal"])
		self.assertTrue(summary["control_passed"])
		self.assertGreaterEqual(summary["drain_sides"], 2)
		for drain in json.loads(self.out.read_text())["drains"]:
			self.assertNotIn("timings_ms", drain)
			self.assertNotIn("reader_seconds", drain)
		self.assertEqual(frappe.db.count("Payment Ledger Entry"), before)
		saved = json.loads(self.out.read_text())
		self.assertEqual(saved["summary"], json.loads(json.dumps(summary)))
		self.assertNotIn("_J2A", self.out.read_text())

	def assert_back_to_back(self, summary, saved):
		self.assertIsInstance(summary["no_regression"], bool)
		self.assertIsInstance(summary["no_regression_median"], bool)
		for side in SIDES:
			figures = summary["per_side"][side]
			self.assertEqual(figures["order"], ["reader_first", "today_first", "reader_first"])
			for key in ("reader_seconds", "today_seconds", "reader_growth_mb", "today_growth_mb"):
				self.assertEqual(len(figures[key]), 3)
			self.assertEqual(figures["reader_max_seconds"], max(figures["reader_seconds"]))
			self.assertEqual(figures["today_max_seconds"], max(figures["today_seconds"]))
			self.assertEqual(figures["reader_median_seconds"], sorted(figures["reader_seconds"])[1])
			self.assertEqual(figures["today_median_seconds"], sorted(figures["today_seconds"])[1])
			self.assertTrue(all(growth >= 0 for growth in figures["today_growth_mb"]))
			self.assertEqual(figures["today_outcomes"], ["served"] * 3)
		runs = saved["sides"]["Payable"]["runs"]
		self.assertEqual(runs[0]["today"]["rows"], runs[0]["rows"])
		self.assertEqual(
			summary["no_regression"],
			all(f["reader_max_seconds"] <= f["today_max_seconds"] for f in summary["per_side"].values()),
		)

	def test_guard_size_fixture_on_a_tiny_size_is_served_and_cleaned_up(self):
		before = frappe.db.count("Payment Ledger Entry")
		summary = in_fresh_process("guard_size", out=str(self.out))
		self.assertTrue(summary["served"], summary)
		self.assertTrue(summary["control_passed"])
		self.assertTrue(summary["cleaned"])
		self.assertEqual(frappe.db.count("Payment Ledger Entry"), before)
		saved = json.loads(self.out.read_text())
		self.assert_back_to_back(summary, saved)
		fixture = saved["fixture"]
		self.assertEqual(fixture["invoices"], 80)
		self.assertGreater(fixture["unreferenced"], 0)
		self.assertGreaterEqual(fixture["ledger_rows"], 800)
		self.assertNotIn("_JPC_", self.out.read_text())
		self.assertIn("after_fixture", saved["memory_mb"])
