"""List coverage boundaries and the real permission-filtered query."""

import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError, ResultTooLargeError
from jarvis.tools.get_list import get_list


class TestListCoverage(unittest.TestCase):
	def read(self, rows, **args):
		with (
			patch.object(frappe, "get_meta", return_value=SimpleNamespace(istable=False)),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_list", return_value=rows) as query,
		):
			result = get_list("ToDo", list_mode="list-page-v1", **args)
			return result, query.call_args.kwargs

	def test_boundaries_and_permission_query_sentinel(self):
		for count in [0, 19, 20, 21]:
			result, query = self.read([{"name": str(i)} for i in range(count)])
			self.assertEqual(len(result["rows"]), min(count, 20))
			self.assertEqual(result["coverage"]["has_more"], count > 20)
			self.assertEqual(result["coverage"]["complete"], count <= 20)
			self.assertEqual(result["coverage"]["next_start"], 20 if count > 20 else None)
			self.assertEqual(query["limit"], 21)
			self.assertEqual(query["limit_start"], 0)
			self.assertNotIn("ignore_permissions", query)

	def test_sentinel_does_not_trigger_row_guard(self):
		result, _ = self.read([{}] * 201, limit=200)
		self.assertEqual(len(result["rows"]), 200)
		with self.assertRaises(ResultTooLargeError):
			self.read([{}] * 202, limit=201)
		self.assertEqual(len(self.read([{}] * 202, limit=201, confirm_large=True)[0]["rows"]), 201)

	def test_final_continuation_never_claims_all_previous_pages(self):
		r, q = self.read([{}] * 15, start=40, order_by="modified desc")
		self.assertFalse(r["coverage"]["complete"])
		self.assertFalse(r["coverage"]["has_more"])
		self.assertEqual(q["order_by"], "modified desc, name asc")
		r, _ = self.read([{}] * 21, start=100000)
		self.assertTrue(r["coverage"]["has_more"])
		self.assertIsNone(r["coverage"]["next_start"])

	def test_invalid_pagination_does_not_query(self):
		for args in [
			{"start": -1},
			{"start": True},
			{"start": 1.5},
			{"start": 100001},
			{"limit": False},
			{"limit": 1.5},
			{"fields": ["sum(amount)"]},
			{"fields": ["distinct name"]},
			{"order_by": "rand()"},
			{"order_by": 12},
			{"fields": "name"},
		]:
			with (
				self.subTest(args=args),
				patch.object(frappe, "get_list") as query,
				self.assertRaises(InvalidArgumentError),
			):
				get_list("ToDo", list_mode="list-page-v1", **args)
			query.assert_not_called()

	def test_legacy_return_stays_a_list(self):
		with (
			patch.object(frappe, "get_meta", return_value=SimpleNamespace(istable=False)),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_list", return_value=[{"name": "a"}]) as query,
		):
			self.assertEqual(get_list("ToDo"), [{"name": "a"}])
			self.assertNotIn("limit_start", query.call_args.kwargs)
			self.assertEqual(query.call_args.kwargs["limit"], 20)
		with self.assertRaises(InvalidArgumentError):
			get_list("ToDo", start=20)

	def test_denied_call_never_queries(self):
		with (
			patch.object(frappe, "get_meta", return_value=SimpleNamespace(istable=False)),
			patch.object(frappe, "has_permission", return_value=False),
			patch.object(frappe, "get_list") as query,
		):
			with self.assertRaises(PermissionDeniedError):
				get_list("ToDo", list_mode="list-page-v1")
			query.assert_not_called()

	def test_nested_skill_judging_and_alias_removal(self):
		from jarvis.api import _skill_rows_read, _without_column

		data = {"list_contract": "list-page-v1", "rows": [{"title": "Visible", "__jv_judge_name": "skill-a"}]}
		self.assertEqual(
			_skill_rows_read({"doctype": "Jarvis Custom Skill"}, data, "__jv_judge_name"), ["skill-a"]
		)
		r = _without_column({"ok": True, "data": data}, "__jv_judge_name")
		self.assertEqual(r["data"]["rows"], [{"title": "Visible"}])

	def test_backend_canonical_specimens(self):
		cases = json.loads((Path(__file__).parent / "fixtures/list-page-v1.json").read_text())
		for case in cases:
			actual, _ = self.read(case["source"], **case["request"])
			actual.pop("note")
			self.assertEqual(actual, case["response"])

	def test_result_budget_updates_coverage_and_restarts_at_first_omitted_row(self):
		from jarvis.tools._result_guard import MAX_RESULT_CHARS, enforce_result_budget

		for count, width in [(20, 3000), (1, MAX_RESULT_CHARS * 2)]:
			r, _ = self.read([{"name": str(i), "description": "x" * width} for i in range(count)])
			out, event = enforce_result_budget(r, tool="get_list")
			self.assertEqual(event["kind"], "truncated")
			self.assertLessEqual(len(json.dumps(out, separators=(",", ":"))), MAX_RESULT_CHARS)
			self.assertFalse(out["coverage"]["complete"])
			self.assertTrue(out["coverage"]["has_more"])
			self.assertEqual(out["coverage"]["returned"], len(out["rows"]))
			self.assertEqual(out["coverage"]["next_start"], len(out["rows"]) if out["rows"] else None)
			self.assertTrue(r["coverage"]["complete"])  # no mutation of original metadata


class TestListCoverageDatabase(unittest.TestCase):
	def test_visible_pages_do_not_leak_hidden_matches(self):
		if not frappe.local.site.endswith(".test"):
			self.skipTest("dedicated .test site required")
		previous = frappe.session.user
		prefix = "f05-" + uuid.uuid4().hex
		user = prefix + "@example.com"
		frappe.set_user("Administrator")
		try:
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "Coverage", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			for index in range(56):
				frappe.get_doc(
					{
						"doctype": "ToDo",
						"description": prefix,
						"allocated_to": user if index < 55 else "Administrator",
					}
				).insert(ignore_permissions=True)
			frappe.set_user(user)
			args = {
				"doctype": "ToDo",
				"fields": ["name"],
				"filters": {"description": prefix},
				"list_mode": "list-page-v1",
			}
			rows = []
			start = 0
			while True:
				r = get_list(**args, start=start)
				rows.extend(row["name"] for row in r["rows"])
				if not r["coverage"]["has_more"]:
					break
				start = r["coverage"]["next_start"]
			self.assertEqual(len(rows), 55)
			self.assertEqual(len(set(rows)), 55)
			self.assertEqual(len(r["rows"]), 15)
			r = get_list(**args, limit=55)
			self.assertTrue(r["coverage"]["complete"])
			self.assertFalse(r["coverage"]["has_more"])
			from jarvis.api import _run_tool

			response = _run_tool("get_list", args)
			self.assertTrue(response["ok"], response)
			self.assertEqual(response["data"]["coverage"]["next_start"], 20)
		finally:
			frappe.set_user(previous)
			frappe.db.rollback()
