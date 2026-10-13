"""export_query with a ``query`` spec: records a plain filter cannot pick.

A customer asked for "suppliers with no transactions in the last year" (2,136 rows)
as Excel. Only ``query`` can say NOT EXISTS, its result is cut to fit the chat, and
export_query took get_list filters only, so the agent tried to rebuild the file in
100-row batches through chat and stopped after one. ``spec`` exports the same query
server-side, complete, under the same permission gates.
"""

from __future__ import annotations

import io
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from openpyxl import load_workbook

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import resolvers
from jarvis.tools.export_query import export_query

PFX = "xqs-"
TOTAL = 230  # past query's 200-row guard and the ~100-row chat cut
WITH_COMMENT = 30


def _no_comment_spec(**extra) -> dict:
	"""ToDos of this suite that nobody commented on: the NOT EXISTS shape."""
	return {
		"from": "ToDo",
		"alias": "t",
		"select": ["t.name", "t.description", "t.status"],
		"where": [
			{"field": "t.description", "op": "like", "value": f"{PFX}%"},
			{
				"op": "not exists",
				"value": {
					"from": "Comment",
					"alias": "c",
					"where": [
						{"field": "c.reference_doctype", "op": "=", "value": "ToDo"},
						{"field": "c.reference_name", "op": "=", "value": {"$field": "t.name"}},
					],
				},
			},
		],
		"order_by": [{"field": "t.description", "dir": "asc"}],
		**extra,
	}


def _sheet(env: dict) -> list[tuple]:
	content = frappe.get_doc("File", env["name"]).get_content()
	wb = load_workbook(io.BytesIO(content if isinstance(content, bytes) else content.encode()))
	return list(wb.worksheets[0].iter_rows(values_only=True))


class TestExportQuerySpec(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.db.delete("ToDo", {"description": ["like", f"{PFX}%"]})
		for i in range(TOTAL):
			todo = frappe.get_doc({"doctype": "ToDo", "description": f"{PFX}{i:04d}"}).insert()
			if i < WITH_COMMENT:
				todo.add_comment("Comment", text="followed up")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		names = frappe.get_all("ToDo", filters={"description": ["like", f"{PFX}%"]}, pluck="name")
		frappe.db.delete("Comment", {"reference_doctype": "ToDo", "reference_name": ["in", names or [""]]})
		frappe.db.delete("ToDo", {"description": ["like", f"{PFX}%"]})
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def test_a_not_exists_query_exports_every_row(self):
		env = export_query(spec=_no_comment_spec(), title="Todos nobody commented on")
		self.assertEqual(env["total"], TOTAL - WITH_COMMENT)
		rows = _sheet(env)
		self.assertEqual(rows[0], ("ID", "Description", "Status"))
		self.assertEqual(len(rows) - 1, TOTAL - WITH_COMMENT)
		self.assertEqual(rows[1][1], f"{PFX}{WITH_COMMENT:04d}")
		self.assertEqual(env["title"], "Todos nobody commented on")

	def test_csv_and_aggregates_with_named_columns(self):
		spec = {
			"from": "ToDo",
			"alias": "t",
			"select": ["t.status", {"agg": "count", "field": "t.name", "as": "todo_count"}],
			"where": [{"field": "t.description", "op": "like", "value": f"{PFX}%"}],
			"group_by": ["t.status"],
		}
		env = export_query(spec=spec, format="csv")
		content = frappe.get_doc("File", env["name"]).get_content()
		text = content.decode() if isinstance(content, bytes) else content
		self.assertTrue(text.startswith("Status,Todo Count"), text[:40])
		self.assertIn(f"Open,{TOTAL}", text)

	def test_a_small_deliberate_limit_still_applies(self):
		env = export_query(spec=_no_comment_spec(limit=50))
		self.assertEqual(env["total"], 50)

	def test_fails_closed_above_the_ceiling(self):
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 10):
			with self.assertRaisesRegex(InvalidArgumentError, "too many to export in full"):
				export_query(spec=_no_comment_spec())

	def test_shapes_an_export_refuses(self):
		with self.assertRaisesRegex(InvalidArgumentError, "drop offset"):
			export_query(spec=_no_comment_spec(offset=10))
		with self.assertRaisesRegex(InvalidArgumentError, "'as' name"):
			export_query(spec={"from": "ToDo", "select": [{"agg": "count", "field": "name"}]})
		dup = {
			"from": "ToDo",
			"alias": "t",
			"joins": [{"doctype": "User", "alias": "u", "on": {"u.name": "t.allocated_to"}, "type": "left"}],
			"select": ["t.name", "u.name"],
		}
		with self.assertRaisesRegex(InvalidArgumentError, "both named 'name'"):
			export_query(spec=dup)
		with self.assertRaisesRegex(InvalidArgumentError, "one of the two"):
			export_query("ToDo", spec=_no_comment_spec())
		with self.assertRaisesRegex(InvalidArgumentError, "one of the two"):
			export_query()
		with self.assertRaisesRegex(InvalidArgumentError, "in the spec itself"):
			export_query(spec=_no_comment_spec(), filters={"status": "Open"})

	def test_highlights_may_name_the_field_or_its_label(self):
		for column in ("status", "Status"):
			env = export_query(
				spec=_no_comment_spec(),
				highlights=[{"column": column, "operator": "eq", "value": "Open", "style": "warning"}],
			)
			self.assertEqual(env["highlight_count"], 1)

	def test_the_export_permission_is_required(self):
		user = "xqs-reader@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "xqs", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user(user)
		try:
			# ToDo is readable by everyone (role All), exportable only by System Manager.
			with self.assertRaisesRegex(PermissionDeniedError, "no export permission on ToDo"):
				export_query(spec={"from": "ToDo", "select": ["name", "description"]})
			# A table the subquery reads is held to read permission, as in query.
			with self.assertRaisesRegex(PermissionDeniedError, "referenced DocType: Comment"):
				export_query(spec=_no_comment_spec())
		finally:
			frappe.set_user("Administrator")

	def test_the_doctype_form_now_has_label_headers(self):
		env = export_query("ToDo", filters={"description": ["like", f"{PFX}%"]}, fields=["name", "status"])
		self.assertEqual(_sheet(env)[0], ("ID", "Status"))
