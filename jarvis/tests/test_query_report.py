"""Prepared-report API against real private files and current user permissions.

Run on a dedicated test site. Each test removes its committed fixtures.
"""

import gzip
import json
import time
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


class TestPreparedReportIntegration(unittest.TestCase):
	def test_real_private_report_full_read_and_revoked_customer(self):
		if not getattr(frappe.local, "db", None):
			self.skipTest("Requires a dedicated connected Frappe test site")
		if not str(frappe.local.site).startswith(("test_", "test.", "jarvis-ci.")):
			self.skipTest("Only disposable test sites may create these fixtures")
		frappe.set_user("Administrator")
		created = []
		checks = []

		def save_doc(d):
			doc = frappe.get_doc(d).insert(ignore_permissions=True)
			created.append((doc.doctype, doc.name))
			return doc

		try:
			from frappe.utils.file_manager import save_file

			from jarvis.api import _run_tool
			from jarvis.tools._result_guard import enforce_result_budget
			from jarvis.tools.query_report import query_report

			tag = uuid.uuid4().hex[:10]
			report = save_doc(
				{
					"doctype": "Report",
					"report_name": "Jarvis Prepared Query " + tag,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tabSales Invoice` limit 1",
					"prepared_report": 1,
					"add_total_row": 1,
					"roles": [{"role": "Accounts User"}],
				}
			)
			email = "prepared-query-" + tag + "@example.test"
			save_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Prepared Query Test",
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
				}
			)
			group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or frappe.db.get_value(
				"Customer Group", {}, "name"
			)
			territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or frappe.db.get_value(
				"Territory", {}, "name"
			)
			customers = [
				save_doc(
					{
						"doctype": "Customer",
						"customer_name": "Prepared Query " + tag + " " + n,
						"customer_type": "Company",
						"customer_group": group,
						"territory": territory,
					}
				)
				for n in ["A", "B"]
			]
			company = frappe.db.get_value("Company", {}, "name")
			cols = [
				{"fieldname": "party_type", "label": "Party Type", "fieldtype": "Link", "options": "DocType"},
				{
					"fieldname": "party",
					"label": "Customer",
					"fieldtype": "Dynamic Link",
					"options": "party_type",
				},
				{"fieldname": "outstanding", "label": "Outstanding", "fieldtype": "Currency"},
				{"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency"},
			]

			def make_run(owner, rows, filters=None):
				with patch("frappe.core.doctype.prepared_report.prepared_report.enqueue"):
					doc = save_doc(
						{
							"doctype": "Prepared Report",
							"report_name": report.name,
							"owner": owner,
							"filters": json.dumps(
								filters or {"company": company, "report_date": "2026-10-29"}
							),
						}
					)
				frappe.db.set_value(
					"Prepared Report",
					doc.name,
					{"owner": owner, "status": "Completed", "report_end_time": frappe.utils.now()},
				)
				body = {
					"columns": cols,
					"result": rows + [["Total", "", f"{len(rows) / 10:.2f}", "INR"]],
					"skip_total_row": 0,
				}
				f = save_file(
					"prepared-" + tag + "-" + doc.name + ".json.gz",
					gzip.compress(json.dumps(body).encode()),
					"Prepared Report",
					doc.name,
					is_private=1,
				)
				created.append(("File", f.name))
				return doc

			large = make_run(
				"Administrator",
				[
					{
						"party_type": "Customer",
						"party": f"DEMO-{i:05}",
						"outstanding": "0.10",
						"currency": "INR",
					}
					for i in range(1, 6001)
				],
			)
			limited = make_run(
				email,
				[
					{"party_type": "Customer", "party": c.name, "outstanding": "0.10", "currency": "INR"}
					for c in customers
				],
			)
			frappe.db.commit()
			before = {dt: frappe.db.count(dt) for dt in ["Prepared Report", "File", "Notification Log"]}
			started = time.monotonic()
			initial = query_report(large.name)
			answer = _run_tool(
				"query_report",
				{
					"prepared_report": large.name,
					"version": initial["version"],
					"sql": "SELECT COUNT(*), decimal_sum(c3), max(_row) FROM report_rows",
				},
			)
			assert answer["ok"], answer
			assert answer["data"]["rows"] == [[6000, "600.00", 6000]], answer
			assert answer["data"]["source_rows"] == 6001
			guarded, event = enforce_result_budget(answer["data"], "query_report")
			assert event is None and guarded["coverage"]["complete"]
			tail = query_report(
				large.name, version=initial["version"], sql="SELECT c2 FROM report_rows WHERE _row = 6000"
			)
			assert tail["rows"] == [["DEMO-06000"]]
			checks.append(
				{
					"case": "6000 data rows through real File and API",
					"rows": answer["data"]["rows"],
					"source_rows": 6001,
					"seconds": round(time.monotonic() - started, 3),
				}
			)
			assert before == {dt: frappe.db.count(dt) for dt in before}, (
				"Read created report/file/notification rows"
			)
			from jarvis.api import _reads_skill_rows
			from jarvis.tools import _report_query_source

			with patch("jarvis.api._is_a_skill_doctype", side_effect=lambda dt: dt == "Sales Invoice"):
				assert _reads_skill_rows("query_report", {"prepared_report": large.name})
			with (
				patch.object(
					_report_query_source, "validate_filters_permissions", side_effect=frappe.PermissionError
				),
				patch.object(_report_query_source.compat, "file_bytes") as read,
			):
				with self.assertRaises(PermissionDeniedError):
					query_report(large.name)
				read.assert_not_called()
			with patch.object(
				_report_query_source, "_masked_fields", return_value=[frappe._dict(fieldname="outstanding")]
			):
				masked = query_report(large.name)
				assert masked["source_columns"][2]["masked"]
				assert masked["tables"][0]["sample"][0][4] == "XXXXXXXX"
				with self.assertRaises(InvalidArgumentError):
					query_report(
						large.name,
						version=masked["version"],
						sql="SELECT COUNT(*) FROM report_rows WHERE c3 > 100",
					)
				with self.assertRaisesRegex(InvalidArgumentError, "changed"):
					query_report(
						large.name, version=initial["version"], sql="SELECT COUNT(*) FROM report_rows"
					)
			with patch.object(_report_query_source.compat, "file_bytes") as read:
				frappe.db.set_value("Prepared Report", large.name, "status", "Queued")
				with self.assertRaisesRegex(InvalidArgumentError, "completed"):
					query_report(large.name)
				read.assert_not_called()
			frappe.db.set_value("Prepared Report", large.name, "status", "Completed")

			frappe.set_user(email)
			for identifier in [large.name, "missing-" + tag]:
				try:
					query_report(identifier)
				except PermissionDeniedError as exc:
					assert str(exc) == "No prepared report is available for this identifier."
				else:
					raise AssertionError("Other user source disclosed")
			fresh = query_report(limited.name)
			allowed = query_report(
				limited.name,
				version=fresh["version"],
				sql="SELECT COUNT(*), decimal_sum(c3) FROM report_rows",
			)
			assert allowed["rows"] == [[2, "0.20"]], allowed
			checks.append({"case": "ordinary Accounts User owns report", "rows": allowed["rows"]})
			frappe.set_user("Administrator")
			save_doc(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Customer",
					"for_value": customers[0].name,
					"apply_to_all_doctypes": 1,
				}
			)
			frappe.db.commit()
			frappe.clear_cache(user=email)
			frappe.set_user(email)
			try:
				query_report(limited.name)
			except PermissionDeniedError as exc:
				assert "Access to the stored report has changed" in str(exc), str(exc)
			else:
				raise AssertionError("Dynamic Link Customer restriction bypassed")
			checks.append(
				{"case": "Customer User Permission added after generation", "result": "refused before SQL"}
			)
			frappe.set_user("Administrator")
			try:
				query_report(large.name, version="a" * 64, sql="SELECT COUNT(*) FROM report_rows")
			except InvalidArgumentError as exc:
				assert "changed" in str(exc)
			else:
				raise AssertionError("Stale version accepted")
			checks.append({"case": "stale source version", "result": "rejected"})
			assert checks
			print(json.dumps(checks))
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			for dt, name in reversed(created):
				if frappe.db.exists(dt, name):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.commit()
			print("Removed scoped test-site fixtures.")
