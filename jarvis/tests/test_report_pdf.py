"""Tests for the report_pdf tool (plan: 2026-08-10-report-to-pdf-tool).

report_pdf runs a saved Report, renders columns+rows to a PDF, stores it as a
private File, and returns the download_pdf envelope. The HTML-to-PDF byte step
(frappe.utils.pdf.get_pdf, which shells out to wkhtmltopdf) is stubbed in setUp
so the suite is hermetic: CI runners have no wkhtmltopdf binary, and producing
the actual PDF bytes is Frappe's job, not this tool's. Every render test still
exercises report_pdf's full logic (report run, HTML build, File save, and the
returned envelope). One test per edge case from the plan.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.report_pdf import (
	_cell,
	_normalise_columns,
	_render_html,
	_safe_filename,
	report_pdf,
)

_ENVELOPE_KEYS = {"file_url", "filename", "title", "mime_type", "size_bytes", "name"}


def _pdf_bytes(file_name: str) -> bytes:
	raw = frappe.get_doc("File", file_name).get_content()
	return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("latin-1", "ignore")


def _stub_pdf_bytes() -> bytes:
	"""A real, minimal one-page PDF for stubbing get_pdf when wkhtmltopdf is absent
	(CI runners have no binary). It has to genuinely parse: Frappe's File.save runs
	pdf_contains_js -> pypdf.PdfReader over the content, so a "%PDF-" string literal
	is rejected with "startxref not found". Built with pypdf, a Frappe dependency
	present wherever Frappe runs, including CI."""
	from io import BytesIO

	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=200, height=200)
	buf = BytesIO()
	writer.write(buf)
	return buf.getvalue()


class TestReportPdf(FrappeTestCase):
	"""Query Report fixtures over `tabUser` (readable by a System User)."""

	DATA = "JV RP Test Data"
	EMPTY = "JV RP Test Empty"
	TOTAL = "JV RP Test Total"
	BUILDER = "JV RP Test Builder"
	WIDE = "JV RP Test Wide"
	MISSFILTER = "JV RP Test MissFilter"
	PREPARED = "JV RP Test Prepared"

	def setUp(self):
		self._report(
			self.DATA,
			query='SELECT name AS "User:Data:220", enabled AS "Enabled:Int:80" '
			"FROM tabUser ORDER BY creation LIMIT 5",
		)
		self._report(
			self.EMPTY,
			query='SELECT name AS "User:Data:220" FROM tabUser WHERE name = "zzz-nobody-zzz"',
		)
		self._report(
			self.TOTAL,
			add_total_row=1,
			query='SELECT name AS "User:Data:220", 1 AS "Count:Int:80" '
			"FROM tabUser ORDER BY creation LIMIT 3",
		)
		self._report(self.BUILDER, report_type="Report Builder")
		# 7 columns > the landscape threshold, so this exercises the auto-landscape path.
		self._report(
			self.WIDE,
			query='SELECT name AS "A:Data:100", name AS "B:Data:100", name AS "C:Data:100", '
			'name AS "D:Data:100", name AS "E:Data:100", name AS "F:Data:100", '
			'name AS "G:Data:100" FROM tabUser ORDER BY creation LIMIT 3',
		)
		# References a %(since)s filter never supplied -> the report run fails.
		self._report(
			self.MISSFILTER,
			query='SELECT name AS "User:Data:220" FROM tabUser WHERE creation > %(since)s LIMIT 5',
		)
		# prepared_report=1 would return a queued async stub; the tool passes
		# ignore_prepared_report=True to force live data.
		self._report(
			self.PREPARED,
			prepared_report=1,
			query='SELECT name AS "User:Data:220" FROM tabUser ORDER BY creation LIMIT 3',
		)
		# CI runners have no wkhtmltopdf, so stub the HTML-to-PDF byte step with a
		# real, minimal PDF (see _stub_pdf_bytes: it must actually parse, as File.save
		# validates PDF content). report_pdf imports get_pdf from frappe.utils.pdf at
		# call time, so patch it there - test_empty_pdf_bytes_raises re-patches the
		# same target locally and that inner patch wins for the empty-bytes case.
		_pdf = patch("frappe.utils.pdf.get_pdf", return_value=_stub_pdf_bytes())
		_pdf.start()
		self.addCleanup(_pdf.stop)

	def _report(self, name, report_type="Query Report", add_total_row=0, query=None, prepared_report=0):
		if frappe.db.exists("Report", name):
			return
		doc = {
			"doctype": "Report",
			"report_name": name,
			"ref_doctype": "User",
			"report_type": report_type,
			"is_standard": "No",
			"add_total_row": add_total_row,
			"prepared_report": prepared_report,
		}
		if query:
			doc["query"] = query
		frappe.get_doc(doc).insert(ignore_permissions=True)

	# ── happy path ────────────────────────────────────────────────────────────
	def test_happy_path_produces_a_private_pdf(self):
		out = report_pdf(self.DATA)
		self.assertEqual(set(out), _ENVELOPE_KEYS)
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertEqual(out["title"], self.DATA)
		self.assertGreater(out["size_bytes"], 0)
		file_doc = frappe.get_doc("File", out["name"])
		self.assertEqual(file_doc.is_private, 1)
		# The real proof: the stored bytes are a PDF (list-of-lists rows rendered).
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	def test_custom_title_overrides_the_report_name(self):
		out = report_pdf(self.DATA, title="Q3 Users")
		self.assertEqual(out["title"], "Q3 Users")

	def test_pdf_file_is_owner_only_not_attached_to_report(self):
		# C5 (export plan-check): the PDF is rendered under the caller's row-/field-
		# filtered permissions, so it must be an UNATTACHED (owner-only) File - never
		# attached to the shared Report doc, which would let anyone who can read the
		# Report download a row-filtered artifact. Mutation-proof: reverting to
		# dt="Report" fails this.
		out = report_pdf(self.DATA)
		attached = frappe.db.get_value(
			"File", out["name"], ["attached_to_doctype", "attached_to_name"], as_dict=True
		)
		self.assertIsNone(attached.attached_to_doctype)
		self.assertIsNone(attached.attached_to_name)

	# ── zero rows ─────────────────────────────────────────────────────────────
	def test_zero_rows_still_produces_a_valid_pdf(self):
		"""An empty report is a PDF with a 'No data' note, never an error or a
		blank/zero-byte file."""
		out = report_pdf(self.EMPTY)
		self.assertGreater(out["size_bytes"], 0)
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	# ── total row ─────────────────────────────────────────────────────────────
	def test_add_total_row_report_renders(self):
		"""query_report.run appends the total as the last row; the PDF must still
		generate (we style it, never recompute it)."""
		out = report_pdf(self.TOTAL)
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	# ── orientation ───────────────────────────────────────────────────────────
	def test_explicit_landscape_succeeds(self):
		out = report_pdf(self.DATA, orientation="landscape")
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	def test_invalid_orientation_raises(self):
		with self.assertRaises(InvalidArgumentError):
			report_pdf(self.DATA, orientation="sideways")

	# ── bad input ─────────────────────────────────────────────────────────────
	def test_unknown_report_raises(self):
		with self.assertRaises(InvalidArgumentError):
			report_pdf("No Such Report ZZZ")

	def test_empty_report_name_raises(self):
		with self.assertRaises(InvalidArgumentError):
			report_pdf("")

	# ── report builder refused ────────────────────────────────────────────────
	def test_report_builder_is_refused_with_guidance(self):
		with self.assertRaises(InvalidArgumentError) as cm:
			report_pdf(self.BUILDER)
		self.assertIn("list view", str(cm.exception))

	# ── markup in the title must not crash the File save (reviewer M3) ─────────
	def test_markup_title_does_not_crash(self):
		"""A title carrying markup (e.g. '<b>') derives a filename Frappe's File
		sanitiser would otherwise reject with 'File name cannot have /'. It must
		produce a valid PDF, not a 500."""
		out = report_pdf(self.DATA, title="Flow <b>Test</b> / Q3")
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")
		# The stored filename is sanitised to safe characters.
		self.assertNotIn("/", out["filename"])
		self.assertNotIn("<", out["filename"])

	def test_safe_filename_strips_markup_slashes_and_never_empties(self):
		self.assertEqual(_safe_filename("Flow <b>x</b>/y"), "Flow-b-x-b-y")
		self.assertEqual(_safe_filename("////"), "report")
		self.assertEqual(_safe_filename(""), "report")
		self.assertLessEqual(len(_safe_filename("z" * 500)), 80)

	# ── get_pdf yields no bytes -> clean error (reviewer M1, edge case 10) ─────
	def test_empty_pdf_bytes_raises(self):
		with patch("frappe.utils.pdf.get_pdf", return_value=b""):
			with self.assertRaises(InvalidArgumentError) as cm:
				report_pdf(self.DATA)
		self.assertIn("no content", str(cm.exception))

	# ── a report that cannot run (missing filter) -> clean error (reviewer M2) ─
	def test_missing_filter_raises_invalid_argument(self):
		"""A report referencing a %(since)s filter with no value raises deep in
		Frappe (TypeError: format requires a mapping); the tool must surface it as
		InvalidArgumentError, not a 500."""
		with self.assertRaises(InvalidArgumentError):
			report_pdf(self.MISSFILTER)

	# ── wide report auto-selects landscape and still renders (reviewer m3) ─────
	def test_wide_report_renders(self):
		out = report_pdf(self.WIDE)
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	# ── zero-row render carries the explicit 'No data' marker (reviewer m2) ────
	def test_render_html_zero_rows_shows_no_data_marker(self):
		html = _render_html(
			"T", {}, [{"fieldname": "a", "label": "A", "fieldtype": "Data", "options": ""}], [], False
		)
		self.assertIn("No data for these filters", html)

	# ── prepared/async report is forced to live data (reviewer m1) ────────────
	def test_prepared_report_renders_live(self):
		out = report_pdf(self.PREPARED)
		self.assertEqual(_pdf_bytes(out["name"])[:5], b"%PDF-")

	# ── row-shape normalisation (covers the list-of-DICTS path a Script Report
	#    produces, without needing a Script Report module fixture) ──────────────
	def test_cell_reads_both_dict_and_list_rows(self):
		cols = _normalise_columns([{"fieldname": "a", "label": "A", "fieldtype": "Data"}, "B:Int:80"])
		self.assertEqual(cols[0]["fieldname"], "a")
		self.assertEqual(cols[1]["label"], "B")
		# dict row keyed by fieldname
		self.assertEqual(_cell({"a": "x", "b": 3}, cols[0], 0), "x")
		# list row read positionally
		self.assertEqual(_cell(["x", 3], cols[1], 1), 3)
		# short list row does not IndexError
		self.assertIsNone(_cell(["only-one"], cols[1], 1))


class TestReportPdfPermission(FrappeTestCase):
	"""The security test: a user who cannot run the report gets
	PermissionDeniedError and NO File is produced. Uses a restricted, non-admin
	System User over an ERPNext doctype it cannot read (Administrator bypasses
	permissions and would hide the bug)."""

	USER = "jarvis-report-pdf-perm@example.com"
	REPORT = "JV RP Perm Probe"

	def setUp(self):
		if not frappe.db.exists("DocType", "Sales Invoice"):
			self.skipTest("erpnext not installed on this site")
		if not frappe.db.exists("User", self.USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.USER,
					"first_name": "report-pdf-perm",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Report", self.REPORT):
			frappe.get_doc(
				{
					"doctype": "Report",
					"report_name": self.REPORT,
					"ref_doctype": "Sales Invoice",
					"report_type": "Query Report",
					"is_standard": "No",
					"query": 'SELECT name AS "Invoice:Data:220" FROM `tabSales Invoice` LIMIT 5',
				}
			).insert(ignore_permissions=True)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_restricted_user_is_denied_and_no_file_is_created(self):
		frappe.set_user(self.USER)
		with self.assertRaises(PermissionDeniedError):
			report_pdf(self.REPORT)
		# The denial happens before save_file. Assert AS ADMINISTRATOR - the
		# restricted user cannot list Files, so an empty result under that user
		# would prove nothing.
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Report", "attached_to_name": self.REPORT},
			),
			[],
		)
