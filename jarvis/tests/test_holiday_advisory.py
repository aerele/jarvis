from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools import _holiday_advisory as ha

HL = "JV Holiday Advisory Test"
NAMED_DATE = "2026-08-15"  # Independence Day (weekly_off = 0)
WOFF_DATE = "2026-08-16"  # a Sunday (weekly_off = 1)
PLAIN_DATE = "2026-08-17"  # ordinary working day


def _doc(doctype, **fields):
	return frappe._dict(doctype=doctype, **fields)


class TestHolidayAdvisory(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if frappe.db.exists("Holiday List", HL):
			frappe.delete_doc("Holiday List", HL, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": HL,
				"from_date": "2026-01-01",
				"to_date": "2026-12-31",
				"holidays": [
					{"holiday_date": NAMED_DATE, "description": "Independence Day", "weekly_off": 0},
					{"holiday_date": WOFF_DATE, "description": "Sunday", "weekly_off": 1},
				],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		# Every test resolves any employee to the fixture holiday list.
		self._p = patch.object(ha, "get_holiday_list_for_employee", return_value=HL)
		self._p.start()

	def tearDown(self):
		self._p.stop()

	def test_doctype_not_in_map_is_silent(self):
		self.assertEqual(ha.advisories_for_doc(_doc("Sales Invoice", posting_date=NAMED_DATE)), [])

	def test_named_holiday_single_date_warns(self):
		out = ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE))
		self.assertEqual(len(out), 1)
		self.assertIn("holiday", out[0])
		self.assertIn("Independence Day", out[0])
		self.assertIn(NAMED_DATE, out[0])

	def test_weekly_off_is_labelled_distinctly(self):
		out = ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=WOFF_DATE))
		self.assertEqual(len(out), 1)
		self.assertIn("weekly off", out[0])

	def test_ordinary_date_is_silent(self):
		self.assertEqual(
			ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=PLAIN_DATE)), []
		)

	def test_missing_employee_is_silent(self):
		self.assertEqual(ha.advisories_for_doc(_doc("Attendance", attendance_date=NAMED_DATE)), [])

	def test_range_names_each_holiday_day(self):
		out = ha.advisories_for_doc(
			_doc("Leave Application", employee="EMP-1", from_date=NAMED_DATE, to_date=WOFF_DATE)
		)
		# both the named holiday and the weekly-off fall in the span
		self.assertEqual(len(out), 2)
		self.assertTrue(any("Independence Day" in w for w in out))
		self.assertTrue(any("weekly off" in w for w in out))

	def test_datetime_field_is_normalised_to_date(self):
		out = ha.advisories_for_doc(_doc("Employee Checkin", employee="EMP-1", time=f"{NAMED_DATE} 09:30:00"))
		self.assertEqual(len(out), 1)

	def test_no_holiday_list_is_silent(self):
		with patch.object(ha, "get_holiday_list_for_employee", return_value=None):
			self.assertEqual(
				ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE)), []
			)

	def test_erpnext_absent_is_silent(self):
		with patch.object(ha, "get_holiday_list_for_employee", None):
			self.assertEqual(
				ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE)), []
			)

	def test_attach_adds_warnings_only_when_present(self):
		r = ha.attach({"name": "X"}, _doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE))
		self.assertIn("warnings", r)
		self.assertEqual(len(r["warnings"]), 1)
		r2 = ha.attach({"name": "Y"}, _doc("Attendance", employee="EMP-1", attendance_date=PLAIN_DATE))
		self.assertNotIn("warnings", r2)

	def test_internal_error_is_swallowed(self):
		with patch.object(ha, "get_holiday_list_for_employee", side_effect=RuntimeError("boom")):
			self.assertEqual(
				ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE)), []
			)

	def test_resolver_resolved_as_of_activity_date(self):
		"""F1: the holiday list is resolved AS OF the activity date (as_on), not
		today - so a backdated attendance uses the list in effect then, not now."""
		from frappe.utils import getdate

		with patch.object(ha, "get_holiday_list_for_employee", return_value=HL) as m:
			ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE))
		m.assert_called_once_with("EMP-1", raise_exception=False, as_on=getdate(NAMED_DATE))

	def test_attach_survives_non_list_warnings_field(self):
		"""F2: a curated doctype with a Custom Field named `warnings` puts a
		non-list value in as_dict(); attach must NOT raise (never break the write)."""
		doc = _doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE)
		r = ha.attach({"name": "X", "warnings": None}, doc)
		self.assertIsInstance(r["warnings"], list)
		self.assertEqual(len(r["warnings"]), 1)
		r2 = ha.attach({"name": "Y", "warnings": "a stringy custom field"}, doc)
		self.assertIsInstance(r2["warnings"], list)
		self.assertEqual(len(r2["warnings"]), 1)

	def test_swallow_preserves_pre_existing_message(self):
		"""F3: the never-raise swallow must drop only messages THIS call pushed,
		never a msgprint the successful write itself emitted."""
		frappe.local.message_log = [{"message": "the write's own message"}]
		before = len(frappe.local.message_log)
		with patch.object(ha, "get_holiday_list_for_employee", side_effect=RuntimeError("boom")):
			ha.advisories_for_doc(_doc("Attendance", employee="EMP-1", attendance_date=NAMED_DATE))
		self.assertEqual(len(frappe.local.message_log), before)
		self.assertTrue(any("the write's own message" in str(m) for m in frappe.local.message_log))
