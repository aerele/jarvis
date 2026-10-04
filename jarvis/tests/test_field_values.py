"""R2-3 field type check before a create / update writes (``jarvis.tools._field_values``).

It accepts what Frappe accepts and rejects only what Frappe would reject or silently
corrupt, naming the field. Real saves through the tool path; the class rolls back."""

from __future__ import annotations

import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import InvalidFieldValueError
from jarvis.tools._field_values import _check, check_values
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc

ITEM = "_J2A Type Check Item"


def _envelope(tool: str, args: dict) -> dict:
	return api._dispatch_and_wrap(tool, args, True)


class _Defaults:
	"""Set site defaults for one test and put them back."""

	def set_default(self, key: str, value: str) -> None:
		before = frappe.db.get_default(key)
		frappe.db.set_default(key, value)
		self.addCleanup(frappe.db.set_default, key, before)


class TestAcceptsWhatFrappeAccepts(_Defaults, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")

	def _item(self, **values) -> dict:
		frappe.db.delete("Item", {"item_code": ITEM})
		return create_doc(
			"Item",
			{"item_code": ITEM, "item_group": "Services", "stock_uom": "Nos", "is_stock_item": 0, **values},
		)

	def test_numbers_and_check(self):
		self.set_default("number_format", "#,###.##")
		doc = self._item(disabled=True, shelf_life_in_days="12.0", weight_per_unit="1,234.5")
		stored = frappe.db.get_value(
			"Item", doc["name"], ["disabled", "shelf_life_in_days", "weight_per_unit"]
		)
		self.assertEqual(stored, (1, 12, 1234.5))

	def test_dates_are_passed_on_in_iso_form(self):
		doc = self._item(end_of_life="3 Oct 2026")
		self.assertEqual(str(frappe.db.get_value("Item", doc["name"], "end_of_life")), "2026-10-03")

	def test_day_first_site_reads_day_first_but_iso_stays_iso(self):
		self.set_default("date_format", "dd-mm-yyyy")
		self.assertEqual(check_values("ToDo", {"date": "03-10-2026"})["date"], "2026-10-03")
		self.assertEqual(check_values("ToDo", {"date": "2026-10-03"})["date"], "2026-10-03")
		self.assertEqual(check_values("ToDo", {"date": "2026-10-03 10:00:00"})["date"], "2026-10-03")
		self.set_default("date_format", "mm-dd-yyyy")
		self.assertEqual(check_values("ToDo", {"date": "03-10-2026"})["date"], "2026-03-10")

	def test_year_first_is_never_read_day_first(self):
		# On either site format a value that starts with the year is year-month-day.
		cases = {
			"2026-10-3": "2026-10-03",
			"2026/10/03": "2026-10-03",
			"2026.10.03": "2026-10-03",
			"2026-11-5": "2026-11-05",
		}
		for date_format in ("dd-mm-yyyy", "mm-dd-yyyy"):
			self.set_default("date_format", date_format)
			for given, want in cases.items():
				with self.subTest(date_format=date_format, given=given):
					self.assertEqual(check_values("ToDo", {"date": given})["date"], want)
			with self.subTest(date_format=date_format, given="datetime"):
				out = check_values("Event", {"starts_on": "2026/10/03 10:00"})
				self.assertEqual(out["starts_on"], "2026-10-03 10:00:00")
			for typo in ("2026-13-01", "2026-02-30", "2026/10/03 25:00"):
				with (
					self.subTest(date_format=date_format, typo=typo),
					self.assertRaises(InvalidFieldValueError) as cm,
				):
					check_values("ToDo", {"date": typo})
				self.assertIn("Due Date (date)", str(cm.exception))

	def test_a_partial_date_is_refused_not_filled_from_today(self):
		# dateutil fills a missing day / month / year from today; MariaDB refused all of
		# these before the type check, so they must not turn into an invented date.
		dates = ("2026-10", "Oct 2026", "Sept 2026", "2026", "10", "9:30", "3 Oct", "October")
		datetimes = ("2026-10", "10:00", "Oct 2026 10:00", "3 Oct 10:00")
		for date_format in ("dd-mm-yyyy", "mm-dd-yyyy"):
			self.set_default("date_format", date_format)
			for given in dates:
				with self.subTest(date_format=date_format, date=given):
					with self.assertRaises(InvalidFieldValueError) as cm:
						check_values("ToDo", {"date": given})
					self.assertIn("Due Date (date)", str(cm.exception))
			for given in datetimes:
				with self.subTest(date_format=date_format, datetime=given):
					with self.assertRaises(InvalidFieldValueError):
						check_values("Event", {"starts_on": given})
			# Complete dates in words still read.
			self.assertEqual(check_values("ToDo", {"date": "3 Oct 2026"})["date"], "2026-10-03")
			out = check_values("Event", {"starts_on": "Oct 3 2026 10:00"})
			self.assertEqual(out["starts_on"], "2026-10-03 10:00:00")

	def test_a_partial_date_through_the_real_tools(self):
		for date_format in ("dd-mm-yyyy", "mm-dd-yyyy"):
			self.set_default("date_format", date_format)
			with self.subTest(date_format=date_format, via="create"):
				with self.assertRaises(InvalidFieldValueError):
					create_doc("ToDo", {"description": "j2a partial", "date": "Oct 2026"})
			r = create_doc("ToDo", {"description": "j2a partial", "date": "2026-10-03"})
			with self.subTest(date_format=date_format, via="update"):
				with self.assertRaises(InvalidFieldValueError):
					update_doc("ToDo", r["name"], {"date": "2026-10"})
			self.assertEqual(str(frappe.db.get_value("ToDo", r["name"], "date")), "2026-10-03")

	def test_a_year_first_date_with_a_zone_takes_the_site_local_day(self):
		from zoneinfo import ZoneInfo

		from frappe.utils import get_system_timezone

		zone = ZoneInfo(get_system_timezone())
		want = datetime.datetime(2026, 10, 3, 23, 30, tzinfo=datetime.UTC).astimezone(zone)
		self.assertEqual(check_values("ToDo", {"date": "2026-10-03T23:30:00Z"})["date"], str(want.date()))

	def test_year_first_through_the_real_tools(self):
		self.set_default("date_format", "dd-mm-yyyy")
		r = create_doc("ToDo", {"description": "j2a year first", "date": "2026-10-3"})
		self.assertEqual(str(frappe.db.get_value("ToDo", r["name"], "date")), "2026-10-03")
		update_doc("ToDo", r["name"], {"date": "2026-11-5"})
		self.assertEqual(str(frappe.db.get_value("ToDo", r["name"], "date")), "2026-11-05")
		with self.assertRaises(InvalidFieldValueError):
			update_doc("ToDo", r["name"], {"date": "2026-13-01"})
		self.assertEqual(str(frappe.db.get_value("ToDo", r["name"], "date")), "2026-11-05")

	def test_a_datetime_with_a_time_zone_is_site_local_without_the_offset(self):
		from zoneinfo import ZoneInfo

		from frappe.utils import get_system_timezone

		want = (
			datetime.datetime(2026, 10, 3, 10, 0, tzinfo=datetime.UTC)
			.astimezone(ZoneInfo(get_system_timezone()))
			.replace(tzinfo=None)
		)
		for given in ("2026-10-03T10:00:00Z", "2026-10-03 15:30:00+05:30"):
			with self.subTest(given=given):
				self.assertEqual(check_values("Event", {"starts_on": given})["starts_on"], str(want))
		r = create_doc(
			"Event", {"subject": "j2a tz", "event_type": "Private", "starts_on": "2026-10-03T10:00:00Z"}
		)
		self.assertEqual(frappe.db.get_value("Event", r["name"], "starts_on"), want)

	def test_time_must_read_back_as_written(self):
		df = frappe._dict(fieldtype="Time", fieldname="t", label="Start")
		self.assertEqual(_check(df, "10:30", ""), "10:30:00")
		self.assertEqual(_check(df, "9:05:07", ""), "09:05:07")
		self.assertEqual(_check(df, "2:15 PM", ""), "14:15:00")
		self.assertEqual(_check(df, "12:00 am", ""), "00:00:00")
		self.assertEqual(_check(df, "9 PM", ""), "21:00:00")
		self.assertEqual(_check(df, "9pm", ""), "21:00:00")
		self.assertEqual(_check(df, "9:5", ""), "09:05:00")
		for bad in ("9.30", "24:00:00", "10:60", "13:00 PM", "noon", "9"):
			with self.subTest(bad=bad), self.assertRaises(InvalidFieldValueError):
				_check(df, bad, "")

	def test_check_reads_values_the_way_frappe_means_them(self):
		df = frappe._dict(fieldtype="Check", fieldname="c", label="Flag")
		for given, want in (
			(True, 1),
			(False, 0),
			("true", 1),
			("Yes", 1),
			("TRUE", 1),
			("false", 0),
			("No", 0),
			("1", 1),
			("0", 0),
			(2, 1),
			(-1, 1),
			(0, 0),
			("0.0", 0),
			(0.5, 0),
			("0.5", 0),
			(1.5, 1),
		):
			with self.subTest(given=given):
				self.assertEqual(_check(df, given, ""), want)
		with self.assertRaises(InvalidFieldValueError):
			_check(df, "abc", "")

	def test_a_falsy_select_is_skipped_like_frappe(self):
		df = frappe._dict(fieldtype="Select", fieldname="s", label="Kind", options="Low\nHigh")
		for given in (0, False):
			with self.subTest(given=given):
				self.assertEqual(_check(df, given, ""), given)

	def test_datetime_and_time(self):
		out = check_values("Event", {"starts_on": "3 Oct 2026 10:30"})
		self.assertEqual(out["starts_on"], "2026-10-03 10:30:00")
		self.assertEqual(_check(frappe._dict(fieldtype="Time", fieldname="t"), "10:30", ""), "10:30:00")

	def test_select_with_a_trailing_space_and_untranslated_options(self):
		r = create_doc("ToDo", {"description": "j2a select", "priority": "High "})
		self.assertEqual(frappe.db.get_value("ToDo", r["name"], "priority"), "High")
		lang = frappe.local.lang
		frappe.local.lang = "de"
		self.addCleanup(setattr, frappe.local, "lang", lang)
		# Frappe compares the stored (untranslated) options, so does the check.
		self.assertEqual(check_values("ToDo", {"priority": "Medium"})["priority"], "Medium")
		with self.assertRaises(InvalidFieldValueError):
			check_values("ToDo", {"priority": frappe._("Medium") + "-x"})

	def test_nothing_to_rewrite_returns_the_same_objects(self):
		# The File Box classifier reads back the dicts the insert mutates: no copies.
		rows = [{"item_code": "X", "qty": 2}]
		values = {"customer": "C", "delivery_date": "2026-10-03", "items": rows}
		out = check_values("Sales Order", values)
		self.assertIs(out, values)
		self.assertIs(out["items"], rows)
		rewritten = check_values("Sales Order", {**values, "delivery_date": "3 Oct 2026"})
		self.assertEqual(rewritten["delivery_date"], "2026-10-03")
		self.assertIs(rewritten["items"], rows)

	def test_empty_values_unknown_fields_and_frappes_own_skips_pass(self):
		values = {"date": "", "priority": None, "not_a_field": "abc", "status": "Bogus"}
		self.assertEqual(check_values("ToDo", values), values)
		self.assertEqual(check_values("ToDo", "not a dict"), "not a dict")


class TestRejectsWhatFrappeWouldCorrupt(_Defaults, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")

	def test_baseline_frappe_saves_garbage_as_zero(self):
		# What the check prevents: Frappe's own insert stores cint("abc") = 0.
		frappe.db.delete("Item", {"item_code": ITEM})
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_group": "Services",
				"stock_uom": "Nos",
				"shelf_life_in_days": "abc",
			}
		).insert()
		self.assertEqual(frappe.db.get_value("Item", doc.name, "shelf_life_in_days"), 0)

	def test_qty_abc_in_an_item_row_names_the_field_and_saves_nothing(self):
		company = frappe.db.get_value("Company", {}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		item = frappe.db.get_value("Item", {"is_stock_item": 0, "disabled": 0, "has_variants": 0}, "name")
		before = frappe.db.count("Sales Order")
		r = _envelope(
			"create_doc",
			{
				"doctype": "Sales Order",
				"values": {
					"company": company,
					"customer": customer,
					"delivery_date": frappe.utils.add_days(frappe.utils.today(), 3),
					"items": [
						{"item_code": item, "qty": 1, "rate": 5},
						{"item_code": item, "qty": "abc", "rate": 5},
					],
				},
			},
		)
		self.assertEqual(
			(r["ok"], r["error"]["code"], r["error"]["kind"]), (False, "InvalidArgumentError", "fixable")
		)
		self.assertIn("Items row 2: Quantity (qty) must be a number", r["error"]["message"])
		self.assertIn("'abc'", r["error"]["message"])
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_update_rejects_instead_of_storing_zero(self):
		frappe.db.delete("Item", {"item_code": ITEM})
		create_doc(
			"Item", {"item_code": ITEM, "item_group": "Services", "stock_uom": "Nos", "shelf_life_in_days": 7}
		)
		with self.assertRaises(InvalidFieldValueError) as cm:
			update_doc("Item", ITEM, {"shelf_life_in_days": "abc"})
		self.assertIn("shelf_life_in_days", str(cm.exception))
		self.assertEqual(frappe.db.get_value("Item", ITEM, "shelf_life_in_days"), 7)

	def test_table(self):
		self.set_default("number_format", "#,###.##")
		rows = [
			("Int", "abc"),
			("Int", "1,2"),  # not thousands grouping; cint reads 0
			("Int", "12.5"),
			("Int", 12.5),
			("Int", "nan"),
			("Int", [1]),
			("Check", "abc"),  # cint("abc") is 0
			("Float", "abc"),
			("Currency", "inf"),
			("Percent", {"v": 1}),
			("Date", "garbage"),
			("Date", "2026-02-30"),
			("Date", 20261003),
			("Datetime", "31/31/2026"),
			("Time", "25:99"),
			("Select", "Urgent"),
		]
		for fieldtype, value in rows:
			df = frappe._dict(fieldtype=fieldtype, fieldname="f", label="Field", options="Low\nHigh")
			with (
				self.subTest(fieldtype=fieldtype, value=value),
				self.assertRaises(InvalidFieldValueError) as cm,
			):
				_check(df, value, "")
			self.assertIn("Field (f)", str(cm.exception))

	def test_a_comma_decimal_site_rejects_an_ambiguous_number(self):
		self.set_default("number_format", "#.###,##")
		df = frappe._dict(fieldtype="Float", fieldname="rate", label="Rate")
		for bad in ("1.234,5", "1,5"):
			with self.subTest(bad=bad), self.assertRaises(InvalidFieldValueError):
				_check(df, bad, "")
		for good in ("1234.5", "1,234.5", "1.234"):
			with self.subTest(good=good):
				self.assertEqual(_check(df, good, ""), good)

	def test_a_grouped_whole_number_on_a_dot_decimal_site(self):
		self.set_default("number_format", "#,##,###.##")
		df = frappe._dict(fieldtype="Int", fieldname="n", label="Count")
		self.assertEqual(_check(df, "1,234", ""), "1234")
		self.assertEqual(_check(df, "1,00,000", ""), "100000")
		for bad in ("1,2", "1,234.5", ",123"):
			with self.subTest(bad=bad), self.assertRaises(InvalidFieldValueError):
				_check(df, bad, "")
		self.set_default("number_format", "#.###,##")
		with self.assertRaises(InvalidFieldValueError):
			_check(df, "1,234", "")  # on a comma-decimal site the comma is not grouping

	def test_reading_the_number_format_raises_no_deprecation_warning(self):
		import warnings

		from jarvis.tools._field_values import _decimal_separator

		with warnings.catch_warnings():
			warnings.simplefilter("error")
			self.set_default("number_format", "#.###,##")
			self.assertEqual(_decimal_separator(), ",")
			self.set_default("number_format", "#,###.##")
			self.assertEqual(_decimal_separator(), ".")

	def test_the_check_leaves_no_message_behind(self):
		before = len(frappe.local.message_log or [])
		with self.assertRaises(InvalidFieldValueError):
			check_values("ToDo", {"date": "not a date"})
		self.assertEqual(len(frappe.local.message_log or []), before)

	def test_the_draft_panel_shares_it(self):
		from jarvis.chat.actions_api import apply_action

		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "j2a type check"}).insert(
			ignore_permissions=True
		)
		r = apply_action(
			frappe.as_json(
				{
					"verb": "create",
					"doctype": "ToDo",
					"conversation": conv.name,
					"values": {"description": "j2a", "date": "garbage"},
				}
			)
		)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertIn("Due Date (date)", r["error"]["message"])
