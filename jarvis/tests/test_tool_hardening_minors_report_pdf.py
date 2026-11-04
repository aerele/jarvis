"""report_pdf requires the Export permission on the report's DocType."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools.report_pdf import report_pdf

REPORT = "JV RP Export Perm Test"


class TestReportPdfExportPermission(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Report", REPORT):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": REPORT,
					"ref_doctype": "User",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": 'SELECT name AS "User:Data:220" FROM tabUser LIMIT 3',
				}
			).insert(ignore_permissions=True)

	def test_refused_without_export_permission(self):
		with (
			patch("frappe.permissions.can_export", return_value=False),
			patch("jarvis.tools.report_pdf.frappe_run_report") as run,
		):
			with self.assertRaises(PermissionDeniedError) as cm:
				report_pdf(REPORT)
		self.assertIn("no export permission on User", str(cm.exception))
		run.assert_not_called()
