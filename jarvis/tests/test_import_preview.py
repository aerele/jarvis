"""Tests for jarvis.tools._import_preview - the read-only, transient Data Import preview.

The load-bearing assumption (spec Phase-0): a preview computed on an UN-INSERTED
``Data Import`` doc persists nothing. We prove it here against an ephemeral custom
parent+child DocType created in ``setUpClass`` (Timesheet is ERPNext-owned and absent
on jarvis.test, so we cannot use it) - mirroring Frappe's own
``data_import/test_importer.py::create_doctype_if_not_exists``.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._import_preview import build_import_preview
from jarvis.tools.preview_import import preview_import

PARENT_DT = "Jarvis Import Test DT"
CHILD_DT = "Jarvis Import Test Log"


def _ensure_doctypes():
	# DocType creation is DDL and persists across runs; delete+recreate so fixture
	# changes always take effect.
	frappe.delete_doc_if_exists("DocType", PARENT_DT)
	frappe.delete_doc_if_exists("DocType", CHILD_DT)
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": CHILD_DT,
			"module": "Custom",
			"custom": 1,
			"istable": 1,
			"fields": [
				{"label": "Activity", "fieldname": "activity", "fieldtype": "Data", "reqd": 1},
				{"label": "Hours", "fieldname": "hours", "fieldtype": "Float"},
			],
		}
	).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": PARENT_DT,
			"module": "Custom",
			"custom": 1,
			"autoname": "field:title",
			"allow_import": 1,
			"fields": [
				{"label": "Title", "fieldname": "title", "fieldtype": "Data", "reqd": 1, "unique": 1},
				# reqd WITH a default -> Frappe auto-fills it -> the stricter guard must NOT block it.
				{
					"label": "Status",
					"fieldname": "status",
					"fieldtype": "Select",
					"options": "Open\nClosed",
					"reqd": 1,
					"default": "Open",
				},
				{"label": "Assignee", "fieldname": "assignee", "fieldtype": "Link", "options": "User"},
				{"label": "Secret Code", "fieldname": "secret_code", "fieldtype": "Password"},
				# reqd Table -> blocks only when NO child column is mapped to it.
				{"label": "Logs", "fieldname": "logs", "fieldtype": "Table", "options": CHILD_DT, "reqd": 1},
			],
			"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "import": 1}],
		}
	).insert(ignore_permissions=True)


def _make_csv(content: str, name: str) -> str:
	existing = frappe.db.exists("File", {"file_name": name})
	if existing:
		frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
	f = frappe.get_doc(doctype="File", content=content, file_name=name, is_private=1).save(
		ignore_permissions=True
	)
	return f.file_url


# A FLAT export: parent columns repeated on every row (the Timesheet-CSV shape).
_FLAT_CSV = (
	"Title,Status,Assignee,Activity (Logs),Hours (Logs)\n"
	"REC-1,Open,Administrator,Did a thing,2\n"
	"REC-2,Open,Administrator,Did another,3\n"
)
_NO_TITLE_CSV = "Status,Assignee\nOpen,Administrator\n"
_BAD_LINK_CSV = "Title,Assignee\nX-1,nonexistent-user-zzz\n"
_JUNK_CSV = "Foo,Bar\n1,2\n"
_SECRET_CSV = "Title,Secret Code\nX-1,hunter2\n"
# Omits Status (reqd but defaulted) - the guard must NOT block it. Has child cols so
# the reqd Logs table is satisfied.
_NO_STATUS_CSV = "Title,Activity (Logs),Hours (Logs)\nX-9,do,1\n"
# Parent-only: the reqd Logs table has no child columns mapped -> must block.
_NO_CHILD_CSV = "Title,Status\nX-8,Open\n"
# An unrecognised header remapped onto a child field via the override.
_EXTRA_CSV = "Title,Activity (Logs),Extra\nX-7,do,5\n"


class TestImportPreview(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_import_flat.csv")
		cls.no_title_url = _make_csv(_NO_TITLE_CSV, "jarvis_import_no_title.csv")
		cls.bad_link_url = _make_csv(_BAD_LINK_CSV, "jarvis_import_bad_link.csv")
		cls.junk_url = _make_csv(_JUNK_CSV, "jarvis_import_junk.csv")
		cls.secret_url = _make_csv(_SECRET_CSV, "jarvis_import_secret.csv")
		cls.no_status_url = _make_csv(_NO_STATUS_CSV, "jarvis_import_no_status.csv")
		cls.no_child_url = _make_csv(_NO_CHILD_CSV, "jarvis_import_no_child.csv")
		cls.extra_url = _make_csv(_EXTRA_CSV, "jarvis_import_extra.csv")
		# Header-only (0 data rows) exercises Frappe's "no data" throw -> clean error.
		cls.header_only_url = _make_csv("Title,Status\n", "jarvis_import_header_only.csv")

	# --- core / transient ---------------------------------------------------

	def test_transient_preview_returns_shapes_and_persists_nothing(self):
		before = frappe.db.count("Data Import")
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		self.assertTrue(out["importable"])
		self.assertEqual(out["total_rows"], 2)
		self.assertEqual(len(out["sample"]["rows"]), 2)
		self.assertEqual(frappe.db.count("Data Import"), before)

	def test_child_column_is_marked(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		by_header = {c["header"]: c for c in out["columns"]}
		self.assertFalse(by_header["Title"]["child"])
		self.assertTrue(by_header["Activity (Logs)"]["child"])
		self.assertEqual(by_header["Activity (Logs)"]["child_doctype"], CHILD_DT)

	def test_flat_file_fans_out_records_equal_rows(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		self.assertEqual(out["total_rows"], 2)
		self.assertEqual(out["total_records"], 2)

	def test_fan_out_advisory_emitted(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		types = {w["type"] for w in out["warnings"]["advisory"]}
		self.assertIn("fan_out", types)

	def test_can_run_true_for_admin(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		self.assertTrue(out["can_run"])

	# --- guards -------------------------------------------------------------

	def test_import_type_literal_validated(self):
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(doctype=PARENT_DT, file_url=self.flat_url, import_type="Insert")

	def test_missing_mandatory_parent_blocks(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.no_title_url)
		fields = {w.get("field") for w in out["warnings"]["blocking"] if w["type"] == "missing_mandatory"}
		self.assertIn("title", fields)

	def test_invalid_link_blocks(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.bad_link_url)
		types = {w["type"] for w in out["warnings"]["blocking"]}
		# A bad Link value is a Frappe-derived (non-info) warning -> classified invalid_value.
		self.assertIn("invalid_value", types)

	def test_near_zero_mapping_blocks(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.junk_url)
		types = {w["type"] for w in out["warnings"]["blocking"]}
		self.assertIn("no_mapping", types)

	def test_unmapped_columns_advisory(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.junk_url)
		types = {w["type"] for w in out["warnings"]["advisory"]}
		self.assertIn("unmapped_columns", types)

	def test_update_without_id_blocks(self):
		out = build_import_preview(
			doctype=PARENT_DT, file_url=self.no_title_url, import_type="Update Existing Records"
		)
		types = {w["type"] for w in out["warnings"]["blocking"]}
		self.assertIn("missing_id", types)

	def test_secret_column_masked(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.secret_url)
		headers = out["sample"]["columns"]
		idx = headers.index("Secret Code")
		self.assertEqual(out["sample"]["rows"][0]["cells"][idx], "[hidden]")

	def test_header_only_file_clean_error(self):
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(doctype=PARENT_DT, file_url=self.header_only_url)

	def test_row_bound_refuses(self):
		frappe.conf.jarvis_import_max_rows = 1
		try:
			with self.assertRaises(InvalidArgumentError):
				build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		finally:
			del frappe.conf["jarvis_import_max_rows"]

	def test_bad_filename_errors(self):
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(doctype=PARENT_DT, filename="no_such_file_zzz.csv")

	# --- stricter-mandatory nuances -----------------------------------------

	def test_defaulted_mandatory_not_blocked(self):
		# Status is reqd but has a default -> Frappe auto-fills it -> must NOT block.
		out = build_import_preview(doctype=PARENT_DT, file_url=self.no_status_url)
		fields = {w.get("field") for w in out["warnings"]["blocking"] if w["type"] == "missing_mandatory"}
		self.assertNotIn("status", fields)

	def test_mandatory_table_without_child_columns_blocks(self):
		out = build_import_preview(doctype=PARENT_DT, file_url=self.no_child_url)
		fields = {w.get("field") for w in out["warnings"]["blocking"] if w["type"] == "missing_mandatory"}
		self.assertIn("logs", fields)

	# --- mapping override ---------------------------------------------------

	def test_mapping_dont_import_drops_column(self):
		out = build_import_preview(
			doctype=PARENT_DT, file_url=self.flat_url, mapping={"Status": "Don't Import"}
		)
		by_header = {c["header"]: c for c in out["columns"]}
		self.assertFalse(by_header["Status"]["mapped"])

	def test_mapping_child_field_by_qualified_key(self):
		out = build_import_preview(
			doctype=PARENT_DT, file_url=self.extra_url, mapping={"Extra": "logs.hours"}
		)
		by_header = {c["header"]: c for c in out["columns"]}
		self.assertTrue(by_header["Extra"]["mapped"])
		self.assertTrue(by_header["Extra"]["child"])
		self.assertEqual(by_header["Extra"]["field"], "hours")

	def test_mapping_miskey_errors(self):
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(
				doctype=PARENT_DT, file_url=self.flat_url, mapping={"NoSuchHeader": "status"}
			)

	# --- preview_import tool wrapper ----------------------------------------

	def test_preview_import_tool_delegates_and_is_read_only(self):
		before = frappe.db.count("Data Import")
		out = preview_import(PARENT_DT, file_url=self.flat_url)
		self.assertTrue(out["importable"])
		self.assertEqual(out["total_rows"], 2)
		self.assertIn("warnings", out)
		self.assertEqual(frappe.db.count("Data Import"), before)

	# --- reviewer fixes -----------------------------------------------------

	def test_file_read_is_permission_gated(self):
		# A raw file_url must NOT disclose a file the caller cannot read (Frappe's importer
		# resolves a File by url with no permission check; we gate it as the caller).
		real = frappe.has_permission

		def fake(doctype=None, ptype="read", *a, **k):
			if doctype == "File" and ptype == "read":
				return False
			return real(doctype, ptype, *a, **k)

		with patch("frappe.has_permission", side_effect=fake):
			with self.assertRaises(InvalidArgumentError):
				build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)

	def test_mapping_to_nonexistent_field_errors(self):
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(
				doctype=PARENT_DT, file_url=self.flat_url, mapping={"Title": "no_such_field_zzz"}
			)

	def test_mapping_wrong_type_errors(self):
		# A truthy non-dict mapping must fail loud/clean, not 500 on .items().
		with self.assertRaises(InvalidArgumentError):
			build_import_preview(doctype=PARENT_DT, file_url=self.flat_url, mapping=["not", "a", "dict"])

	def test_submittable_flag_reflects_meta(self):
		# PARENT_DT is not submittable, so the card must not later claim "will submit".
		out = build_import_preview(doctype=PARENT_DT, file_url=self.flat_url)
		self.assertFalse(out["submittable"])
