"""Tests for the dashboard/chart creation tools (need DB; run as the fixture
test context). Inserts are rolled back by FrappeTestCase."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.create_dashboard import create_dashboard
from jarvis.tools.create_dashboard_chart import create_dashboard_chart


def _h(prefix):
	return f"{prefix} {frappe.generate_hash(length=6)}"


class TestCreateDashboardChart(FrappeTestCase):
	def test_count_time_series(self):
		res = create_dashboard_chart(
			chart_name=_h("JT Count"),
			document_type="ToDo",
			chart_type="Count",
			based_on="creation",
			time_interval="Monthly",
			timespan="Last Year",
		)
		self.assertTrue(res["name"])
		self.assertEqual(res["chart_type"], "Count")
		self.assertIn("dashboard-chart", res["url"])
		self.assertEqual(frappe.db.get_value("Dashboard Chart", res["name"], "chart_type"), "Count")

	def test_group_by(self):
		res = create_dashboard_chart(
			chart_name=_h("JT GroupBy"),
			document_type="ToDo",
			chart_type="Group By",
			group_by_based_on="status",
			group_by_type="Count",
		)
		self.assertTrue(res["name"])

	def test_invalid_chart_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(chart_name="x", document_type="ToDo", chart_type="Bogus")

	def test_count_needs_based_on(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(chart_name="x", document_type="ToDo", chart_type="Count")

	def test_sum_needs_value_field(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name="x",
				document_type="ToDo",
				chart_type="Sum",
				based_on="creation",
			)

	def test_group_by_child_table_needs_parent_document_type(self):
		# Contact/Contact Email is a core Frappe parent/child pair - istable,
		# no ERPNext fixture needed. Without parent_document_type Frappe's own
		# Dashboard Chart.validate() would reject the insert; the tool should
		# catch this itself and name the valid parent in the message.
		with self.assertRaises(InvalidArgumentError) as ctx:
			create_dashboard_chart(
				chart_name=_h("JT Child NoParent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
			)
		self.assertIn("Contact", str(ctx.exception))

	def test_group_by_child_table_with_parent_document_type(self):
		res = create_dashboard_chart(
			chart_name=_h("JT Child Parent"),
			document_type="Contact Email",
			chart_type="Group By",
			group_by_based_on="email_id",
			parent_document_type="Contact",
		)
		self.assertTrue(res["name"])
		self.assertEqual(
			frappe.db.get_value("Dashboard Chart", res["name"], "parent_document_type"),
			"Contact",
		)

	def test_wrong_parent_document_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Wrong Parent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
				parent_document_type="ToDo",
			)

	def test_junk_parent_document_type_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Junk Parent"),
				document_type="Contact Email",
				chart_type="Group By",
				group_by_based_on="email_id",
				parent_document_type="No Such Doctype 123",
			)

	def test_parent_document_type_rejected_for_non_child_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard_chart(
				chart_name=_h("JT Parent NonChild"),
				document_type="ToDo",
				chart_type="Count",
				based_on="creation",
				parent_document_type="ToDo",
			)

	def test_duplicate_chart_name_returns_clean_error(self):
		# Dashboard Chart autonames on chart_name -> a repeat raises Frappe's
		# DuplicateEntryError (a NameError). _run_tool must translate it to the
		# {ok: False, error} envelope, not let it escape as an HTTP 500.
		from jarvis.api import _run_tool

		name = _h("JT Dup")
		args = {"chart_name": name, "document_type": "ToDo", "chart_type": "Count", "based_on": "creation"}
		create_dashboard_chart(**args)
		res = _run_tool("create_dashboard_chart", dict(args))
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "InvalidArgumentError")


class TestCreateDashboard(FrappeTestCase):
	def test_dashboard_links_a_chart(self):
		ch = create_dashboard_chart(
			chart_name=_h("JT Dash Chart"),
			document_type="ToDo",
			chart_type="Count",
			based_on="creation",
		)
		dash = create_dashboard(dashboard_name=_h("JT Dash"), charts=[ch["name"]])
		self.assertTrue(dash["name"])
		self.assertIn("dashboard", dash["url"])
		linked = frappe.get_all("Dashboard Chart Link", filters={"parent": dash["name"]}, pluck="chart")
		self.assertIn(ch["name"], linked)

	def test_empty_charts_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_dashboard(dashboard_name="x", charts=[])
