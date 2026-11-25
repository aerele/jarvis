"""AC-B9: every caller of `visible_names` reaches the batch with the switch on, and the loop with it off.

The callers are `sources._visible_rows` (the collect of the reader and of today's read) and, in `access`,
`_check_vouchers` and `_check_parties` through `validate_result_visibility` (publish) and
`verify_returned_rows` (read). The spies sit on internal helpers only: `batch_visible`, `_evaluate_batch` and
`permissions._visible_names_per_doc`. No pinned Frappe callable is patched.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import NonCallableMock, patch

import frappe

from jarvis.payment_calendar import access, batch_permission, read_log, sources
from jarvis.payment_calendar import permissions as calendar_permissions
from jarvis.tests import batch_permission_fixtures
from jarvis.tests.batch_permission_fixtures import (
	HOOK_PATH,
	HOOK_STATE,
	SI,
	BatchCase,
	hooks,
	session_as,
)
from jarvis.tools._preview_sandbox import preview_sandbox

CUSTOMER = "Customer"
ALLOW_ALL = HOOK_PATH + "deny_listed"


class Spies:
	def __init__(self, visible, evaluate, per_doc):
		self.visible, self.evaluate, self.per_doc = visible, evaluate, per_doc

	def batch_doctypes(self) -> set[str]:
		return {call.args[0] for call in self.visible.call_args_list}

	def batch_names(self, doctype: str) -> set[str]:
		(names,) = [set(call.args[1]) for call in self.visible.call_args_list if call.args[0] == doctype]
		return names


@contextmanager
def spied():
	with (
		patch.object(batch_permission, "batch_visible", wraps=batch_permission.batch_visible) as visible,
		patch.object(batch_permission, "_evaluate_batch", wraps=batch_permission._evaluate_batch) as evaluate,
		patch.object(
			calendar_permissions, "_visible_names_per_doc", wraps=calendar_permissions._visible_names_per_doc
		) as per_doc,
	):
		yield Spies(visible, evaluate, per_doc)


class CallerCase(BatchCase):
	"""A user with a Company User Permission is never a small input, so the batch path is the one that runs."""

	def setUp(self):
		super().setUp()
		self.addCleanup(HOOK_STATE.clear)
		f = self.f
		self.voucher_names = [f.si_plain, f.si_twe_a, f.si_wrong_parentfield]
		self.party_names = [f.fx.customer, f.cust_plain]
		self.rows = [
			{
				"voucher_type": SI,
				"voucher_no": name,
				"party_type": CUSTOMER,
				"party": f.fx.customer,
				"_permission": {},
			}
			for name in self.voucher_names
		]
		self.scope = {"company": f.a, "actor": f.u1}

	def run_caller(self, caller, *, switch: bool):
		"""`(spies)` for one call of `caller(self)` as the Company-permission user, with a hook on both doctypes."""
		with (
			hooks({SI: [ALLOW_ALL], CUSTOMER: [ALLOW_ALL]}),
			session_as(self.f.u1),
			spied() as spy,
			patch.dict(frappe.local.conf, {batch_permission.SWITCH: int(switch)}),
		):
			caller(self)
		return spy

	def assert_reaches_the_batch(self, caller, doctypes):
		spy = self.run_caller(caller, switch=True)
		self.assertEqual(spy.batch_doctypes(), set(doctypes))
		self.assertEqual({call.args[0] for call in spy.evaluate.call_args_list}, set(doctypes))
		spy.per_doc.assert_not_called()
		return spy

	def assert_stays_per_document(self, caller, doctypes):
		spy = self.run_caller(caller, switch=False)
		self.assertEqual(spy.batch_doctypes(), set(doctypes), "the entry point is the same")
		spy.evaluate.assert_not_called()
		self.assertEqual({call.args[0] for call in spy.per_doc.call_args_list}, set(doctypes))
		return spy


def visible_rows(case):
	engine = SimpleNamespace(columns=[], filters=frappe._dict(company=case.f.a))
	report = SimpleNamespace(ref_doctype=SI)
	rows = [frappe._dict(row) for row in case.rows]
	kept = sources._visible_rows(engine, report, rows)
	case.assertEqual([row.voucher_no for row in kept], case.voucher_names)


def check_vouchers(case):
	access._check_vouchers(case.f.a, case.rows)


def check_parties(case):
	access._check_parties({(CUSTOMER, name) for name in case.party_names})


def publish(case):
	result = {"sources": {"Receivable": {"permission_columns": [], "rows": case.rows, "excluded_rows": []}}}
	with patch.object(access, "require_report", return_value=SimpleNamespace(ref_doctype=SI)):
		access.validate_result_visibility(case.scope, result)


def read(case):
	with patch.object(access, "require_report", return_value=SimpleNamespace(ref_doctype=SI)):
		access.verify_returned_rows(
			case.scope, "Receivable", [], case.rows, [(CUSTOMER, name) for name in case.party_names]
		)


class TestEveryCallerUsesTheBatch(CallerCase):
	def test_collect_reaches_the_batch_for_vouchers_and_parties(self):
		spy = self.assert_reaches_the_batch(visible_rows, (SI, CUSTOMER))
		self.assertEqual(spy.batch_names(SI), set(self.voucher_names))
		self.assertEqual(spy.batch_names(CUSTOMER), {self.f.fx.customer})

	def test_the_voucher_check_reaches_the_batch(self):
		spy = self.assert_reaches_the_batch(check_vouchers, (SI,))
		self.assertEqual(spy.batch_names(SI), set(self.voucher_names))

	def test_the_party_check_reaches_the_batch(self):
		spy = self.assert_reaches_the_batch(check_parties, (CUSTOMER,))
		self.assertEqual(spy.batch_names(CUSTOMER), set(self.party_names))

	def test_publish_reaches_the_batch_for_vouchers_and_parties(self):
		spy = self.assert_reaches_the_batch(publish, (SI, CUSTOMER))
		self.assertEqual(spy.batch_names(SI), set(self.voucher_names))

	def test_a_read_reaches_the_batch_for_vouchers_and_parties(self):
		spy = self.assert_reaches_the_batch(read, (SI, CUSTOMER))
		self.assertEqual(spy.batch_names(CUSTOMER), set(self.party_names))


class TestEveryCallerStaysPerDocumentWithTheSwitchOff(CallerCase):
	def test_collect_runs_the_loop_only(self):
		self.assert_stays_per_document(visible_rows, (SI, CUSTOMER))

	def test_the_voucher_check_runs_the_loop_only(self):
		self.assert_stays_per_document(check_vouchers, (SI,))

	def test_the_party_check_runs_the_loop_only(self):
		self.assert_stays_per_document(check_parties, (CUSTOMER,))

	def test_publish_runs_the_loop_only(self):
		self.assert_stays_per_document(publish, (SI, CUSTOMER))

	def test_a_read_runs_the_loop_only(self):
		self.assert_stays_per_document(read, (SI, CUSTOMER))


class TestNoTestLeavesAnErrorLogRow(BatchCase):
	def test_an_event_outside_a_view_reaches_a_patched_writer_not_the_error_log(self):
		self.assertIsInstance(read_log.error_log_once, NonCallableMock)
		rows = frappe.db.count("Error Log", {"method": ("like", "Payment Calendar batch permission%")})
		batch_permission.queue_event(batch_permission.EVENT_PLAN, "permission plan runs per document: test")
		self.error_log_once.assert_called_once()
		self.assertEqual(
			frappe.db.count("Error Log", {"method": ("like", "Payment Calendar batch permission%")}), rows
		)


class TestAnEventAfterAPreviewSandbox(BatchCase):
	"""A preview sandbox leaves `commit` and `rollback` on `frappe.db`; that is not an open snapshot view."""

	def test_an_event_outside_a_view_is_written_at_once_and_no_rollback_is_sent(self):
		left = set(vars(frappe.db))
		self.addCleanup(lambda: [vars(frappe.db).pop(name, None) for name in set(vars(frappe.db)) - left])
		with preview_sandbox():
			pass
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			batch_permission.queue_event(
				batch_permission.EVENT_PLAN, "permission plan runs per document: test"
			)
			routing_statements = [c.args[0].strip().lower() for c in sql.call_args_list if c.args]
		self.error_log_once.assert_called_once()
		self.assertEqual([s for s in routing_statements if s.startswith("rollback")], [])


class TestFixtureBuildRetry(TestCase):
	def test_a_busy_site_ends_the_build_after_a_few_short_waits_and_the_error_is_raised(self):
		with (
			patch.object(
				batch_permission_fixtures, "BatchFixtures", side_effect=frappe.QueryDeadlockError
			) as build,
			patch.object(batch_permission_fixtures.time, "sleep") as sleep,
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "savepoint"),
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				batch_permission_fixtures.build_fixtures()
		self.assertEqual(build.call_count, 4)
		self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 15)
		self.assertEqual(sleep.call_count, 3, "no wait after the last attempt")
