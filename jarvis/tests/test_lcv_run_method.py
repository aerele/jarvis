"""Generic RPCs must not bypass linked landed-cost checks."""

import json
import unittest
from unittest.mock import patch

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools.run_method import run_method

MODULE = "jarvis.tools.run_method"


class TestLCVRpcTargets(unittest.TestCase):
	def test_generic_mutators_refuse_lcv_targets_before_dispatch(self):
		requests = [
			("frappe.client.insert", {"doc": {"doctype": "Landed Cost Voucher"}}),
			("frappe.client.save", {"doc": json.dumps({"doctype": "Landed Cost Voucher"})}),
			("frappe.client.submit", {"doc": {"doctype": "Landed Cost Voucher", "name": "LCV"}}),
			("frappe.client.insert_many", {"docs": [{"doctype": "Landed Cost Voucher"}]}),
			(
				"frappe.client.set_value",
				{"doctype": "Landed Cost Voucher", "name": "LCV", "fieldname": "vendor_invoices"},
			),
			(
				"frappe.desk.form.save.savedocs",
				{"doc": json.dumps({"doctype": "Landed Cost Voucher"}), "action": "Submit"},
			),
			("frappe.model.workflow.apply_workflow", {"doc": {"doctype": "Landed Cost Voucher"}}),
			(
				"frappe.model.workflow.bulk_workflow_approval",
				{"doctype": "Landed Cost Voucher", "docnames": json.dumps(["LCV"]), "action": "Approve"},
			),
			(
				"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
				{"doctype": "Landed Cost Voucher", "docnames": ["LCV"], "action": "submit"},
			),
		]
		for method, args in requests:
			with (
				self.subTest(method=method),
				patch(MODULE + ".resolve_method", side_effect=lambda path: path),
				patch(MODULE + "._enforce_blocklist"),
				patch(MODULE + ".get_server_script_map", return_value={}),
				patch(MODULE + "._run_whitelisted", return_value={}) as dispatch,
			):
				with self.assertRaises(PermissionDeniedError):
					run_method(method, args)
				dispatch.assert_not_called()

	def test_ordinary_doc_with_lcv_text_still_dispatches(self):
		with (
			patch(MODULE + ".resolve_method", side_effect=lambda path: path),
			patch(MODULE + "._enforce_blocklist"),
			patch(MODULE + ".get_server_script_map", return_value={}),
			patch(MODULE + "._run_whitelisted", return_value={"ok": True}) as dispatch,
		):
			self.assertEqual(
				run_method(
					"frappe.client.insert", {"doc": {"doctype": "ToDo", "description": "Landed Cost Voucher"}}
				),
				{"ok": True},
			)
			dispatch.assert_called_once()
