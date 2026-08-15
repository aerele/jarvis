import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools._export.envelope import save_export_file
from jarvis.tools._export.model import ExportModel
from jarvis.tools._export.safety import escape_formula


class TestExportModel(FrappeTestCase):
	def test_model_holds_columns_rows_total(self):
		m = ExportModel(columns=["a", "b"], rows=[[1, 2]], total=1, meta={})
		self.assertEqual(m.columns, ["a", "b"])
		self.assertEqual(m.rows, [[1, 2]])
		self.assertEqual(m.total, 1)
		self.assertEqual(m.meta, {})

	def test_meta_defaults_to_empty_dict(self):
		m = ExportModel(columns=["a"], rows=[], total=0)
		self.assertEqual(m.meta, {})


class TestExportEnvelope(FrappeTestCase):
	def test_returns_download_card_envelope(self):
		env = save_export_file("x.csv", b"a,b\n1,2\n", title="My Todos", mime_type="text/csv")
		self.assertTrue(env["file_url"].endswith(".csv"))
		self.assertEqual(env["mime_type"], "text/csv")
		self.assertEqual(env["title"], "My Todos")
		self.assertGreater(env["size_bytes"], 0)
		self.assertTrue(frappe.db.exists("File", env["name"]))

	def test_file_is_private(self):
		env = save_export_file("x.csv", b"data\n", title="t", mime_type="text/csv")
		self.assertEqual(frappe.db.get_value("File", env["name"], "is_private"), 1)

	def test_default_is_owner_only_unattached(self):
		# No dt/dn -> unattached File, whose has_permission falls through to owner-only.
		env = save_export_file("x.csv", b"data\n", title="t", mime_type="text/csv")
		attached_to = frappe.db.get_value(
			"File", env["name"], ["attached_to_doctype", "attached_to_name"], as_dict=True
		)
		self.assertIsNone(attached_to.attached_to_doctype)
		self.assertIsNone(attached_to.attached_to_name)

	def test_can_attach_when_dt_dn_given(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "attach-target"}).insert()
		env = save_export_file(
			"x.csv", b"a,b\n1,2\n", title="t", mime_type="text/csv", dt="ToDo", dn=todo.name
		)
		self.assertEqual(frappe.db.get_value("File", env["name"], "attached_to_doctype"), "ToDo")

	def test_filename_extension_preserved_from_arg(self):
		env = save_export_file("ignored-base.xlsx", b"PK\x03\x04", title="Report Q1", mime_type="x")
		self.assertTrue(env["filename"].endswith(".xlsx"))

	def test_unsafe_title_is_sanitized(self):
		# A title carrying markup / slashes must not raise inside save_file
		# ("File name cannot have /") nor leak the raw characters.
		env = save_export_file("x.csv", b"data\n", title="Sales </b> 2026/Q1", mime_type="text/csv")
		self.assertNotIn("/", env["filename"])
		self.assertNotIn("<", env["filename"])
		self.assertTrue(env["filename"].endswith(".csv"))

	def test_blank_title_falls_back(self):
		env = save_export_file("x.csv", b"data\n", title="", mime_type="text/csv")
		self.assertTrue(env["filename"].endswith(".csv"))
		self.assertTrue(len(env["filename"]) > len(".csv"))

	def test_file_save_validation_error_becomes_clean_error(self):
		# save_file raises ValidationError (e.g. MaxFileSizeReachedError) for a File
		# constraint; an over-limit export must surface a clean InvalidArgumentError,
		# not an opaque 500 / Error Log. (Reviewer I1.)
		from unittest import mock

		from frappe.exceptions import ValidationError

		from jarvis.exceptions import InvalidArgumentError

		with mock.patch(
			"frappe.utils.file_manager.save_file", side_effect=ValidationError("File size exceeded")
		):
			with self.assertRaises(InvalidArgumentError):
				save_export_file("x.xlsx", b"PK\x03\x04", title="Big", mime_type="x")


class TestFormulaEscape(FrappeTestCase):
	def test_dangerous_leading_chars_are_prefixed(self):
		for bad in ("=1+1", "+1", "-1", "@x", "\t=cmd", "\r=cmd"):
			self.assertTrue(escape_formula(bad).startswith("'"), bad)

	def test_safe_values_untouched(self):
		self.assertEqual(escape_formula("Acme Ltd"), "Acme Ltd")
		self.assertEqual(escape_formula("123"), "123")
		self.assertEqual(escape_formula(""), "")

	def test_non_strings_pass_through(self):
		self.assertEqual(escape_formula(42), 42)
		self.assertEqual(escape_formula(0), 0)
		self.assertIsNone(escape_formula(None))
