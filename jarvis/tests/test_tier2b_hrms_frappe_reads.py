"""Validation + envelope tests for the Tier-2b HRMS + Frappe
computed-read tools.

Same mock-the-boundary pattern as Tier-2a: patch ``frappe.db.exists``
+ the underlying helper so the tests don't depend on HRMS leave
allocations, holiday lists, shift assignments, or DocType naming
patterns being seeded in the test bench.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools.get_employee_shift import get_employee_shift
from jarvis.tools.get_holidays_for_employee import get_holidays_for_employee
from jarvis.tools.get_leave_balance_on import get_leave_balance_on
from jarvis.tools.get_leave_details import get_leave_details
from jarvis.tools.get_leaves_for_period import get_leaves_for_period
from jarvis.tools.get_linked_docs import get_linked_docs
from jarvis.tools.get_naming_series_preview import get_naming_series_preview
from jarvis.tools.get_submitted_linked_docs import get_submitted_linked_docs


def _all_exist():
	return patch("frappe.db.exists", return_value=True)


def _no_exists():
	return patch("frappe.db.exists", return_value=False)


def _allow_perm():
	return patch("frappe.has_permission", return_value=True)


def _deny_perm():
	return patch("frappe.has_permission", return_value=False)


# ---------------------------------------------------------------------
# HRMS - leave / shift / holidays
# ---------------------------------------------------------------------


class TestGetLeaveBalanceOn(FrappeTestCase):
	def test_rejects_empty_employee(self):
		with self.assertRaises(InvalidArgumentError):
			get_leave_balance_on("", "Casual Leave")

	def test_rejects_empty_leave_type(self):
		with self.assertRaises(InvalidArgumentError):
			get_leave_balance_on("HR-EMP-001", "")

	def test_rejects_unknown_employee(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			get_leave_balance_on("Unknown", "Casual Leave")

	def test_rejects_when_no_employee_read_perm(self):
		with _all_exist(), _deny_perm(), self.assertRaises(PermissionDeniedError):
			get_leave_balance_on("HR-EMP-001", "Casual Leave")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.leave_application.leave_application.get_leave_balance_on",
				return_value=12.5,
			),
		):
			out = get_leave_balance_on("HR-EMP-001", "Casual Leave", "2026-06-18")
		self.assertEqual(out["balance"], 12.5)
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(out["leave_type"], "Casual Leave")
		self.assertEqual(out["date"], "2026-06-18")


class TestGetLeavesForPeriod(FrappeTestCase):
	def test_rejects_empty_args(self):
		with self.assertRaises(InvalidArgumentError):
			get_leaves_for_period("", "Casual Leave", "2026-01-01", "2026-01-31")
		with self.assertRaises(InvalidArgumentError):
			get_leaves_for_period("HR-EMP-001", "", "2026-01-01", "2026-01-31")
		with self.assertRaises(InvalidArgumentError):
			get_leaves_for_period("HR-EMP-001", "Casual Leave", "", "2026-01-31")
		with self.assertRaises(InvalidArgumentError):
			get_leaves_for_period("HR-EMP-001", "Casual Leave", "2026-01-01", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.leave_application.leave_application.get_leaves_for_period",
				return_value=3.5,
			),
		):
			out = get_leaves_for_period(
				"HR-EMP-001",
				"Casual Leave",
				"2026-01-01",
				"2026-01-31",
			)
		self.assertEqual(out["days"], 3.5)
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(out["from_date"], "2026-01-01")
		self.assertEqual(out["to_date"], "2026-01-31")


class TestGetLeaveDetails(FrappeTestCase):
	def test_rejects_empty_employee(self):
		with self.assertRaises(InvalidArgumentError):
			get_leave_details("")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.leave_application.leave_application.get_leave_details",
				return_value=[{"leave_type": "Casual", "balance": 12}],
			),
		):
			out = get_leave_details("HR-EMP-001", "2026-06-18")
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(out["date"], "2026-06-18")
		self.assertEqual(out["leave_details"], [{"leave_type": "Casual", "balance": 12}])


class TestGetHolidaysForEmployee(FrappeTestCase):
	def test_rejects_empty_args(self):
		with self.assertRaises(InvalidArgumentError):
			get_holidays_for_employee("", "2026-01-01", "2026-01-31")
		with self.assertRaises(InvalidArgumentError):
			get_holidays_for_employee("HR-EMP-001", "", "2026-01-31")
		with self.assertRaises(InvalidArgumentError):
			get_holidays_for_employee("HR-EMP-001", "2026-01-01", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.utils.get_holidays_for_employee",
				return_value=[{"holiday_date": "2026-01-26", "description": "Republic Day"}],
			) as g,
		):
			out = get_holidays_for_employee(
				"HR-EMP-001",
				"2026-01-01",
				"2026-01-31",
			)
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(len(out["holidays"]), 1)
		# raise_exception=False must be forwarded so the agent gets [] not 500.
		self.assertEqual(g.call_args.kwargs.get("raise_exception"), False)


class TestGetEmployeeShift(FrappeTestCase):
	def test_rejects_empty_employee(self):
		with self.assertRaises(InvalidArgumentError):
			get_employee_shift("")

	def test_calls_real_signature_with_for_timestamp(self):
		# autospec=True validates the call against the REAL hrms function's
		# signature (employee, for_timestamp, consider_default_shift,
		# next_shift_direction) - it has no `for_date` parameter, so this
		# raises TypeError pre-fix, catching the exact bug a loose
		# MagicMock(return_value=...) mock would silently swallow.
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.shift_assignment.shift_assignment.get_employee_shift",
				autospec=True,
				return_value={"name": "Day Shift", "start_time": "09:00"},
			) as ges,
		):
			out = get_employee_shift("HR-EMP-001", "2026-06-18")
		ges.assert_called_once_with(
			employee="HR-EMP-001",
			for_timestamp="2026-06-18",
			consider_default_shift=True,
		)
		self.assertEqual(out["shift"], {"name": "Day Shift", "start_time": "09:00"})
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(out["for_date"], "2026-06-18")

	def test_returns_envelope_when_shift_assigned(self):
		# The real helper returns a plain dict (frappe._dict), never a
		# Document - it has no .as_dict() method. Mock with a real dict so
		# a stray `.as_dict()` call on the result raises, not silently
		# resolves via frappe._dict's dict.get-based __getattr__.
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.shift_assignment.shift_assignment.get_employee_shift",
				return_value={"name": "Day Shift", "start_time": "09:00"},
			),
		):
			out = get_employee_shift("HR-EMP-001", "2026-06-18")
		self.assertEqual(out["shift"], {"name": "Day Shift", "start_time": "09:00"})
		self.assertEqual(out["employee"], "HR-EMP-001")
		self.assertEqual(out["for_date"], "2026-06-18")

	def test_returns_none_when_no_shift(self):
		# The real helper returns `{}` (never None) when nothing is found
		# (`return shift_details or {}`) - assert the tool collapses that
		# falsy empty dict to None rather than calling a nonexistent
		# .as_dict() on it.
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"hrms.hr.doctype.shift_assignment.shift_assignment.get_employee_shift",
				return_value={},
			),
		):
			out = get_employee_shift("HR-EMP-001", "2026-06-18")
		self.assertIsNone(out["shift"])


# ---------------------------------------------------------------------
# Frappe - linked docs + naming series preview
# ---------------------------------------------------------------------


class TestGetLinkedDocs(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			get_linked_docs("", "x")
		with self.assertRaises(InvalidArgumentError):
			get_linked_docs("User", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.linked_with.get",
				return_value={"Communication": [{"name": "COMM-001"}]},
			),
		):
			out = get_linked_docs("Contact", "Test-Contact")
		self.assertEqual(out["doctype"], "Contact")
		self.assertEqual(out["name"], "Test-Contact")
		self.assertEqual(out["linked"], {"Communication": [{"name": "COMM-001"}]})


class TestGetSubmittedLinkedDocs(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			get_submitted_linked_docs("", "x")

	def test_returns_envelope_when_helper_returns_list(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.linked_with.get_submitted_linked_docs",
				return_value=[
					{"doctype": "Sales Invoice", "name": "SINV-001", "docstatus": 1},
				],
			),
		):
			out = get_submitted_linked_docs("Sales Order", "SO-001")
		self.assertEqual(len(out["linked"]), 1)
		self.assertEqual(out["linked"][0]["doctype"], "Sales Invoice")

	def test_returns_envelope_when_helper_returns_dict(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.linked_with.get_submitted_linked_docs",
				return_value={"docs": [{"doctype": "Sales Invoice", "name": "SINV-001"}]},
			),
		):
			out = get_submitted_linked_docs("Sales Order", "SO-001")
		self.assertEqual(len(out["linked"]), 1)


class TestGetNamingSeriesPreview(FrappeTestCase):
	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_naming_series_preview("")

	def test_rejects_unknown_doctype(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			get_naming_series_preview("Not-A-DocType")

	def test_rejects_when_no_create_perm(self):
		with _all_exist(), _deny_perm(), self.assertRaises(PermissionDeniedError):
			get_naming_series_preview("Sales Invoice", "SINV-.YYYY.-")

	def test_returns_envelope_for_explicit_series(self):
		fake_ns = MagicMock()
		fake_ns.get_preview.return_value = ["SINV-2026-001", "SINV-2026-002", "SINV-2026-003"]
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"frappe.model.naming.NamingSeries",
				return_value=fake_ns,
			),
		):
			out = get_naming_series_preview("Sales Invoice", "SINV-.YYYY.-")
		self.assertEqual(out["doctype"], "Sales Invoice")
		self.assertEqual(out["series"], "SINV-.YYYY.-")
		self.assertEqual(len(out["preview"]), 3)

	def test_rejects_field_autoname(self):
		# autoname "field:customer_name" doesn't have a preview shape -
		# surface clearly rather than hand back something useless.
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"frappe.db.get_value",
				return_value="field:customer_name",
			),
		):
			with self.assertRaises(InvalidArgumentError):
				get_naming_series_preview("Customer")
