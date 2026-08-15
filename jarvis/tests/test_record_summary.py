"""Tests for jarvis.chat._record_summary - field selection, permission-checked doc
reads, Frappe-correct formatting, and cast-compare no-op detection.

These back every confirmation card. A bug here is a card that misdescribes a write.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat._record_summary import _MAX_VAL, fmt


def _df(**kwargs):
	"""A stand-in DocField. format_value only reads attributes, so a _dict is enough."""
	return frappe._dict(kwargs)


class TestFmt(FrappeTestCase):
	def test_check_uses_cint_so_string_zero_is_no(self):
		# format_value has no Check branch (formatters.py:146) and returns 1/0. A
		# model sends "0" as a string; naive truthiness would render "Yes" and
		# fabricate a phantom change on a gated card.
		df = _df(fieldtype="Check", fieldname="disabled")
		self.assertEqual(fmt(0, df), "No")
		self.assertEqual(fmt("0", df), "No")
		self.assertEqual(fmt(1, df), "Yes")
		self.assertEqual(fmt("1", df), "Yes")

	def test_html_fieldtypes_bypass_format_value(self):
		# format_value turns newlines into <br> for Text; the cards escape every
		# value, so that would render as literal tags.
		df = _df(fieldtype="Small Text", fieldname="notes")
		self.assertEqual(fmt("a\nb", df), "a\nb")

	def test_percent_formats(self):
		df = _df(fieldtype="Percent", fieldname="pct")
		self.assertEqual(fmt(12.5, df), "12.5%")

	def test_list_renders_row_count(self):
		self.assertEqual(fmt([1, 2, 3]), "3 rows")
		self.assertEqual(fmt([1]), "1 row")

	def test_none_renders_empty(self):
		self.assertEqual(fmt(None), "")

	def test_truncates_at_limit(self):
		out = fmt("x" * 500)
		self.assertEqual(len(out), _MAX_VAL)
		self.assertTrue(out.endswith("…"))

	def test_limit_is_overridable_for_long_form_bodies(self):
		out = fmt("x" * 500, limit=8000)
		self.assertEqual(out, "x" * 500)

	def test_raising_field_falls_back_to_str(self):
		# format_value can raise (get_field_currency attribute access, meta.py:886).
		# One odd field must never blank a whole card. Assert the VALUE, not the type:
		# fmt always returns a str, so assertIsInstance would pass even with the
		# except branch deleted.
		df = _df(fieldtype="Currency", fieldname="amount", options="bad_link_field")
		self.assertEqual(fmt(5, df, doc=object()), "5")


from jarvis.chat._record_summary import same_value


class TestSameValue(FrappeTestCase):
	def test_typed_float_equals_string_request(self):
		# The phantom-diff case display comparison was introduced to fix: the DB
		# holds a typed 100.0, the model sends "100". The save changes nothing.
		df = _df(fieldtype="Currency", fieldname="rate")
		self.assertTrue(same_value(100.0, "100", df))

	def test_rounding_collision_is_NOT_a_no_op(self):
		# THE regression this rule exists for. fmt_money at precision 2 renders both
		# as "100.00"; a display compare would drop the row and the card would omit
		# a change the confirm writes.
		df = _df(fieldtype="Currency", fieldname="rate")
		self.assertFalse(same_value(100.005, "100.001", df))

	def test_float_precision_collision_is_NOT_a_no_op(self):
		df = _df(fieldtype="Float", fieldname="exchange_rate")
		self.assertFalse(same_value(83.1234, "83.1236", df))

	def test_check_string_zero_is_a_no_op(self):
		df = _df(fieldtype="Check", fieldname="disabled")
		self.assertTrue(same_value(0, "0", df))
		self.assertFalse(same_value(0, "1", df))

	def test_none_equals_empty_string(self):
		self.assertTrue(same_value(None, ""))

	def test_uncastable_value_counts_as_changed(self):
		# Never hide a row because we could not compare it. Garbage bounces pre-card
		# via _DRY_RUN_ON_PARK anyway (api.py:1033-1039); this is a safety net.
		df = _df(fieldtype="Currency", fieldname="rate")
		self.assertFalse(same_value(100.0, object(), df))

	def test_dates_compare_across_types(self):
		# A typed date from the DB vs the model's string. Comparing two identical
		# STRINGS here would pass even with Date missing from _CAST entirely.
		import datetime

		df = _df(fieldtype="Date", fieldname="due_date")
		self.assertTrue(same_value(datetime.date(2026, 7, 17), "2026-07-17", df))
		self.assertFalse(same_value(datetime.date(2026, 7, 17), "2026-07-18", df))

	def test_setting_a_date_to_today_on_an_empty_field_is_NOT_a_no_op(self):
		# getdate(None) returns TODAY (frappe/utils/data.py:125-126). A bare getdate
		# cast would compare today to today and silently drop "due today" - a routine
		# request - from a gated card.
		from frappe.utils import nowdate

		df = _df(fieldtype="Date", fieldname="due_date")
		self.assertFalse(same_value(None, nowdate(), df))
		self.assertFalse(same_value(nowdate(), None, df))

	def test_empty_dates_are_equal_to_each_other(self):
		df = _df(fieldtype="Date", fieldname="due_date")
		self.assertTrue(same_value(None, "", df))
		self.assertTrue(same_value(None, None, df))

	def test_empty_datetimes_do_not_produce_a_phantom_change(self):
		# get_datetime(None) returns now() (data.py:164-165); two calls microseconds
		# apart would render "(empty) -> (empty)" as a change.
		df = _df(fieldtype="Datetime", fieldname="starts_on")
		self.assertTrue(same_value(None, None, df))

	def test_invalid_datetime_strings_are_not_equal(self):
		# get_datetime returns None for garbage rather than raising, so two different
		# invalid strings would otherwise compare equal (None == None).
		df = _df(fieldtype="Datetime", fieldname="starts_on")
		self.assertFalse(same_value("not-a-date", "also-not-a-date", df))

	def test_no_df_falls_back_to_string_compare(self):
		self.assertTrue(same_value("abc", "abc"))
		self.assertFalse(same_value("abc", "abd"))


from jarvis.chat._record_summary import _MAX_FLOOR, pick_fields


class TestPickFields(FrappeTestCase):
	def test_floor_includes_title_and_list_view_fields(self):
		fields = pick_fields(frappe.get_meta("ToDo"))
		self.assertIn("description", fields)
		self.assertNotIn("name", fields)  # name is the row header, not a row

	def test_floor_is_capped_and_deduped(self):
		fields = pick_fields(frappe.get_meta("User"))
		self.assertLessEqual(len(fields), _MAX_FLOOR)
		self.assertEqual(len(fields), len(set(fields)))

	def test_floor_excludes_child_tables(self):
		meta = frappe.get_meta("User")
		for f in pick_fields(meta):
			self.assertNotIn(meta.get_field(f).fieldtype, ("Table", "Table MultiSelect"))

	def test_long_text_fieldtypes_sort_last(self):
		# data_fieldtypes includes Text Editor / Markdown Editor / Code, so a 10k
		# body field can be in_list_view and would otherwise take a floor slot from
		# the customer and the total.
		meta = frappe.get_meta("ToDo")
		fields = pick_fields(meta)
		types = [meta.get_field(f).fieldtype for f in fields]
		long_idx = [i for i, t in enumerate(types) if t in ("Text Editor", "Markdown Editor", "Code")]
		short_idx = [i for i, t in enumerate(types) if t not in ("Text Editor", "Markdown Editor", "Code")]
		if long_idx and short_idx:
			self.assertGreater(min(long_idx), max(short_idx))

	def test_no_meta_returns_empty(self):
		self.assertEqual(pick_fields(None), [])


from unittest.mock import patch

from jarvis.chat._record_summary import summary_rows


class TestSummaryRows(FrappeTestCase):
	def setUp(self):
		self.todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "card summary probe",
				"priority": "High",
			}
		).insert(ignore_permissions=True)

	def test_returns_title_and_rows_for_a_readable_record(self):
		out = summary_rows("ToDo", self.todo.name)
		self.assertIsNotNone(out)
		self.assertIn("rows", out)
		values = {r["value"] for r in out["rows"]}
		self.assertIn("High", values)

	def test_missing_record_returns_none(self):
		self.assertIsNone(summary_rows("ToDo", "does-not-exist-xyz"))

	def test_missing_record_leaves_no_message_in_the_log(self):
		# frappe.throw leaves an entry in message_log that would otherwise leak into
		# the turn. Pre-existing latent bug in _bulk_update_card's catch.
		frappe.clear_messages()
		summary_rows("ToDo", "does-not-exist-xyz")
		self.assertFalse(frappe.get_message_log())

	def test_unreadable_record_returns_none_and_no_title(self):
		# THE permission fix. get_doc checks nothing (document.py:141-145 -> :336-349
		# defaults check_permission to None) and apply_fieldlevel_read_permissions is
		# permlevel-only, so without has_permission the card would render the
		# customer name of a record the user cannot read.
		# FrappeTestCase runs as Administrator, whose permission checks early-return,
		# so assert the CALL, not the effect.
		with patch("frappe.model.document.Document.has_permission", return_value=False) as hp:
			out = summary_rows("ToDo", self.todo.name)
		hp.assert_called_once_with("read")
		self.assertIsNone(out)

	def test_permission_is_checked_before_any_field_is_read(self):
		with patch("frappe.model.document.Document.has_permission", return_value=False):
			with patch("frappe.model.document.Document.apply_fieldlevel_read_permissions") as afl:
				summary_rows("ToDo", self.todo.name)
		afl.assert_not_called()

	def test_submittable_doctype_gets_a_synthetic_docstatus_row(self):
		# docstatus is not a DocField, so pick_fields cannot select it and
		# summary_rows appends it. ToDo is not submittable, so flip the meta flag:
		# the branch reads meta.is_submittable and doc.docstatus, and this exercises
		# both. (Asserting only "no crash" on a non-submittable doctype would ship
		# this branch with zero coverage.)
		self.todo.db_set("docstatus", 1)
		meta = frappe.get_meta("ToDo")
		with patch.object(meta, "is_submittable", 1):
			out = summary_rows("ToDo", self.todo.name)
		row = next((r for r in out["rows"] if r["label"] == "Docstatus"), None)
		self.assertIsNotNone(row)
		self.assertEqual(row["value"], "Submitted")

	def test_docstatus_row_does_not_exceed_the_row_cap(self):
		# The synthetic row is appended AFTER the capped loop breaks, so without a
		# reserved slot a submittable doctype returns _MAX_ROWS + 1 rows and the
		# documented cap is a lie.
		from jarvis.chat._record_summary import _MAX_ROWS

		meta = frappe.get_meta("ToDo")
		with patch.object(meta, "is_submittable", 1):
			out = summary_rows("ToDo", self.todo.name)
		self.assertLessEqual(len(out["rows"]), _MAX_ROWS)
		self.assertEqual(out["rows"][-1]["label"], "Docstatus")

	def test_non_submittable_doctype_has_no_docstatus_row(self):
		out = summary_rows("ToDo", self.todo.name)
		self.assertNotIn("Docstatus", {r["label"] for r in out["rows"]})


from jarvis.chat._record_summary import _MAX_ROWS as _MR
from jarvis.chat._record_summary import values_rows


class TestValuesRows(FrappeTestCase):
	def test_renders_every_proposed_key_not_just_floor_fields(self):
		# Design decision 3: proposed content is shown WHOLE. A field outside the
		# meta floor is one the save will write - hiding it would let you approve a
		# value you never saw.
		meta = frappe.get_meta("ToDo")
		out = values_rows(meta, {"description": "x", "color": "#fff"})
		labels = {r["label"] for r in out["rows"]}
		self.assertEqual(len(out["rows"]), 2)
		self.assertTrue(any("olor" in la for la in labels))

	def test_preserves_caller_order(self):
		meta = frappe.get_meta("ToDo")
		out = values_rows(meta, {"priority": "High", "description": "x"})
		self.assertEqual(out["rows"][0]["value"], "High")

	def test_caps_and_reports_the_remainder(self):
		values = {f"f{i}": f"v{i}" for i in range(_MR + 5)}
		out = values_rows(None, values)
		self.assertEqual(len(out["rows"]), _MR)
		self.assertEqual(out["extra"], 5)

	def test_masks_secrets(self):
		out = values_rows(None, {"api_key": "sk-live-123"})
		self.assertEqual(out["rows"][0]["value"], "[hidden]")
		self.assertNotIn("sk-live-123", str(out))

	def test_no_meta_falls_back_to_fieldnames(self):
		out = values_rows(None, {"whatever": "v"})
		self.assertEqual(out["rows"][0]["label"], "whatever")
		self.assertEqual(out["extra"], 0)


from jarvis.chat._record_summary import table_rows


class TestTableRows(FrappeTestCase):
	def test_columns_union_includes_caller_set_keys_outside_list_view(self):
		# THE rule. Sales Invoice Item's list-view columns are item/qty/rate/amount;
		# a batch that also sets income_account on every row must not write it
		# invisibly. ``columns`` holds LABELS for rendering; ``fieldnames`` is the
		# machine-readable list, so assert against that.
		meta = frappe.get_meta("Sales Invoice")
		rows = [{"item_code": "ITEM-1", "qty": 2, "income_account": "Sales - X"}]
		out = table_rows(meta, "items", rows)
		self.assertIn("income_account", out["fieldnames"])

	def test_renders_cells_for_each_row(self):
		meta = frappe.get_meta("Sales Invoice")
		rows = [{"item_code": "ITEM-1", "qty": 2}, {"item_code": "ITEM-2", "qty": 3}]
		out = table_rows(meta, "items", rows)
		self.assertEqual(out["count"], 2)
		self.assertEqual(len(out["rows"]), 2)

	def test_caps_rows_and_reports_the_remainder(self):
		meta = frappe.get_meta("Sales Invoice")
		rows = [{"item_code": f"I-{i}"} for i in range(_MR + 3)]
		out = table_rows(meta, "items", rows)
		self.assertEqual(len(out["rows"]), _MR)
		self.assertEqual(out["extra"], 3)

	def test_a_key_that_is_not_a_child_field_is_counted_not_rendered(self):
		# The save drops it (fails valid_columns); rendering it as a written value
		# would violate the effective-values invariant.
		meta = frappe.get_meta("Sales Invoice")
		out = table_rows(meta, "items", [{"item_code": "I-1", "not_a_real_field": "x"}])
		self.assertNotIn("not_a_real_field", out["fieldnames"])
		self.assertEqual(out["unknown_columns"], 1)

	def test_unknown_key_is_counted_once_not_once_per_row(self):
		# An unknown key never enters `proposed`, so a plain counter re-increments on
		# every row: 3 rows -> unknown_columns=3 for ONE bad field.
		meta = frappe.get_meta("Sales Invoice")
		rows = [{"item_code": f"I-{i}", "not_a_real_field": "x"} for i in range(3)]
		out = table_rows(meta, "items", rows)
		self.assertEqual(out["unknown_columns"], 1)

	def test_columns_are_capped_at_MAX_COLS_not_MAX_ROWS(self):
		meta = frappe.get_meta("Sales Invoice")
		child = frappe.get_meta(meta.get_field("items").options)
		many = {
			d.fieldname: "v"
			for d in child.fields[:15]
			if d.fieldtype not in ("Section Break", "Column Break")
		}
		out = table_rows(meta, "items", [many])
		self.assertLessEqual(len(out["fieldnames"]), 8)
		self.assertGreater(out["extra_columns"], 0)

	def test_unknown_table_field_returns_none(self):
		self.assertIsNone(table_rows(frappe.get_meta("ToDo"), "nope", [{"a": 1}]))

	def test_empty_rows_returns_none(self):
		self.assertIsNone(table_rows(frappe.get_meta("Sales Invoice"), "items", []))
