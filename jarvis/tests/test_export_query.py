from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, NoDataError
from jarvis.tools._export import resolvers
from jarvis.tools.export_query import export_query


class TestExportQuery(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ["like", "eq-%"]})
		for i in range(4):
			frappe.get_doc({"doctype": "ToDo", "description": f"eq-{i}"}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_xlsx_export_returns_envelope_with_total(self):
		res = export_query(
			"ToDo",
			filters={"description": ["like", "eq-%"]},
			fields=["name", "description"],
			format="xlsx",
			title="Todos",
		)
		self.assertTrue(res["file_url"].endswith(".xlsx"))
		self.assertEqual(res["total"], 4)
		self.assertTrue(frappe.db.exists("File", res["name"]))

	def test_csv_export(self):
		res = export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"], format="csv")
		self.assertTrue(res["file_url"].endswith(".csv"))

	def test_bad_format_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			export_query("ToDo", format="parquet")

	def test_fails_closed_above_ceiling(self):
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 2):
			with self.assertRaises(InvalidArgumentError):
				export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"])

	def test_emits_telemetry_ok(self):
		with mock.patch("jarvis.telemetry.record_export_event") as m:
			export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"])
		m.assert_called_once()
		self.assertEqual(m.call_args.kwargs.get("mode"), "sync")
		self.assertEqual(m.call_args.kwargs.get("outcome"), "ok")

	def test_empty_result_raises_nodata(self):
		# A zero-match export must refuse (like export_excel), not hand back a
		# normal-looking but empty file (I-7).
		with self.assertRaises(NoDataError):
			export_query("ToDo", filters={"description": ["like", "__no-match-zzz__%"]}, fields=["name"])

	def test_non_string_format_is_clean_error(self):
		# A non-string format must be a clean InvalidArgumentError, not an opaque
		# AttributeError/500 from .lower() (M-4).
		with self.assertRaises(InvalidArgumentError):
			export_query("ToDo", filters={"description": ["like", "eq-%"]}, format=["csv"])

	def test_fail_closed_emits_outcome(self):
		# The fail-closed ceiling path must still emit a telemetry outcome, so
		# refused exports are visible to operators (I-5).
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 2):
			with mock.patch("jarvis.telemetry.record_export_event") as m:
				with self.assertRaises(InvalidArgumentError):
					export_query("ToDo", filters={"description": ["like", "eq-%"]}, fields=["name"])
		m.assert_called_once()
		self.assertEqual(m.call_args.kwargs.get("outcome"), "rejected")

	def test_cells_truncated_surfaced_in_envelope(self):
		# An over-long cell trips the xlsx 32767 cap; the disclosure must reach the
		# returned envelope, not just the model meta (I-11).
		frappe.get_doc({"doctype": "ToDo", "description": "trunc " + "x" * 40_000}).insert()
		res = export_query(
			"ToDo", filters={"description": ["like", "trunc %"]}, fields=["description"], format="xlsx"
		)
		self.assertTrue(res.get("cells_truncated"))
		self.assertIn("truncated", res.get("note", "").lower())

	def test_child_table_export_via_parent_doctype(self):
		# A child (Table) doctype is exportable when parent_doctype is passed - the
		# resolver's parent hint is now actually reachable through export_query (I-6).
		res = export_query("Has Role", parent_doctype="User", fields=["role"], format="csv")
		self.assertTrue(res["file_url"].endswith(".csv"))
