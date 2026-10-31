"""Current field and omitted-link permissions against a real disposable site."""

import gzip
import json
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


class TestStoredReportPermissions(unittest.TestCase):
	def test_field_denial_and_new_omitted_link_restriction(self):
		if not getattr(frappe.local, "db", None) or not str(frappe.local.site).startswith(
			("test_", "test.", "jarvis-ci.")
		):
			self.skipTest("Requires a connected disposable Frappe site")
		from frappe.utils.file_manager import save_file

		from jarvis.tools.query_report import query_report

		tag = uuid.uuid4().hex[:8]
		created = []
		original_user = frappe.session.user

		def save(values):
			doc = frappe.get_doc(values).insert(ignore_permissions=True)
			created.append((doc.doctype, doc.name))
			return doc

		try:
			frappe.set_user("Administrator")
			doctype = save(
				{
					"doctype": "DocType",
					"name": "Jarvis Report Gate " + tag,
					"module": "Custom",
					"custom": 1,
					"autoname": "hash",
					"fields": [
						{"fieldname": "public_value", "label": "Public", "fieldtype": "Data"},
						{"fieldname": "secret_value", "label": "Secret", "fieldtype": "Data", "permlevel": 1},
						{
							"fieldname": "territory",
							"label": "Territory",
							"fieldtype": "Link",
							"options": "Territory",
						},
					],
					"permissions": [
						{"role": "Accounts User", "read": 1, "report": 1, "permlevel": 0},
						{"role": "System Manager", "read": 1, "report": 1, "permlevel": 0},
						{"role": "System Manager", "read": 1, "permlevel": 1},
					],
				}
			)
			email = "report-gate-" + tag + "@example.test"
			save(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Report gate",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
					"user_type": "System User",
				}
			)
			report = save(
				{
					"doctype": "Report",
					"report_name": "Jarvis Permission Report " + tag,
					"ref_doctype": doctype.name,
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tab" + doctype.name + "`",
					"prepared_report": 1,
					"roles": [{"role": "Accounts User"}],
				}
			)
			with patch("frappe.core.doctype.prepared_report.prepared_report.enqueue"):
				run = save({"doctype": "Prepared Report", "report_name": report.name, "filters": "{}"})
			frappe.db.set_value("Prepared Report", run.name, {"owner": email, "status": "Completed"})
			body = {
				"columns": [
					{"fieldname": "public_value", "label": "Public", "fieldtype": "Data"},
					{"fieldname": "secret_value", "label": "Secret", "fieldtype": "Data"},
				],
				"result": [{"public_value": "visible", "secret_value": "PRIVATE-SALARY-" + tag}],
				"skip_total_row": 1,
			}
			file = save_file(
				"report-gate-" + tag + ".json.gz",
				gzip.compress(json.dumps(body).encode()),
				"Prepared Report",
				run.name,
				is_private=1,
			)
			created.append(("File", file.name))
			frappe.db.commit()
			frappe.set_user(email)
			initial = query_report(run.name)
			self.assertNotIn("PRIVATE-SALARY-" + tag, json.dumps(initial))
			self.assertEqual([c["fieldname"] for c in initial["source_columns"]], ["public_value"])
			self.assertEqual(
				query_report(run.name, version=initial["version"], sql="SELECT c1 FROM report_rows")["rows"],
				[["visible"]],
			)
			with self.assertRaises(InvalidArgumentError):
				query_report(run.name, version=initial["version"], sql="SELECT c2 FROM report_rows")
			frappe.set_user("Administrator")
			territory = frappe.db.get_value("Territory", {}, "name")
			if not territory:
				territory = save(
					{"doctype": "Territory", "territory_name": "Report gate " + tag, "is_group": 1}
				).name
			save(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Territory",
					"for_value": territory,
					"apply_to_all_doctypes": 1,
				}
			)
			frappe.db.commit()
			frappe.set_user(email)
			# A fresh inspection must not bless old rows whose required link is absent.
			with self.assertRaises(PermissionDeniedError):
				query_report(run.name)
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			for dt, name in reversed(created):
				if frappe.db.exists(dt, name):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.commit()
			frappe.set_user(original_user)
