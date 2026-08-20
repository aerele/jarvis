"""Tests for export_document: composed content → downloadable PDF / HTML / PNG.

save_file is mocked so we inspect the exact bytes the tool produced (real
render engines still run — get_pdf / md_to_html / pypdfium2).

Two test tiers live here:

  * ``TestExportDocument*`` (``FrappeTestCase``) — the ORIGINAL plain-path suite.
    These need a site (they run in the bench gate / ``bench run-tests``).
  * ``TestRich*`` / ``TestPlainPathBackCompat`` (plain ``unittest.TestCase``) —
    the Slice-1 rich-path orchestration suite. Everything site-dependent
    (``render_pdf`` shells out to wkhtmltopdf; ``save_export_file``/``save_file``/
    ``get_pdf``/telemetry touch the site) is mocked, so these run under bare
    pytest from the worktree AND under the bench gate. They are the load-bearing
    proof of the dual-path branch / splice / guard / order / telemetry logic.

Running site-free with BARE pytest: deselect the ``FrappeTestCase`` classes, e.g.
``-k "not (TestExportDocumentGuards or TestExportDocumentFormats or
TestExportDocumentSanitization)"``. Otherwise ``FrappeTestCase.setUpClass`` calls
``frappe.init("test_site")`` (setting ``frappe.local.site`` before failing with
no real site), which then poisons the site-free guards in the other classes. The
real bench gate (``bench run-tests``) has a real ``test_site`` and needs no
deselect.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

# Frappe's file logger writes to a cwd-relative ``../logs`` (frappe/utils/logger.py),
# and importing ``frappe.utils.pdf`` opens a cssutils handler there at import time. A
# real bench already has that directory; bare pytest run from a worktree does not, so
# the site-free unit tests below (which mock frappe.utils.pdf.get_pdf) would hit a
# FileNotFoundError. Create it ONLY when running site-free (no bench init) so the
# real ``bench run-tests`` gate — where a site is initialized — is untouched.
if not getattr(frappe.local, "site", None):
	os.makedirs(os.path.join(os.pardir, "logs"), exist_ok=True)

from jarvis.exceptions import InvalidArgumentError, NoDataError
from jarvis.tools._export.document.sanitizer import sanitize_rich as _real_sanitize_rich
from jarvis.tools.export_document import export_document

_MD = "# Report\n\n| Metric | Value |\n|---|---|\n| Revenue | 1000 |\n\n- point one\n"


def _saved(mock):
	"""(filename, bytes) export_document handed save_file."""
	return mock.call_args.args[0], mock.call_args.args[1]


def _pdf_backend_ok() -> bool:
	"""Frappe's HTML→PDF backend (wkhtmltopdf) may be absent in a minimal env;
	the pdf/png tests skip rather than fail when it can't render."""
	try:
		from frappe.utils.pdf import get_pdf

		return bool(get_pdf("<p>x</p>"))
	except Exception:
		return False


def _png_backend_ok() -> bool:
	if not _pdf_backend_ok():
		return False
	try:
		import PIL
		import pypdfium2

		return True
	except Exception:
		return False


class TestExportDocumentGuards(FrappeTestCase):
	def test_rejects_empty_content(self):
		with self.assertRaises(NoDataError):
			export_document("")
		with self.assertRaises(NoDataError):
			export_document("   \n  ")

	def test_rejects_unknown_format(self):
		with self.assertRaises(InvalidArgumentError):
			export_document(_MD, format="docx")


class TestExportDocumentFormats(FrappeTestCase):
	def _mock(self):
		m = patch("frappe.utils.file_manager.save_file").start()
		self.addCleanup(patch.stopall)
		m.return_value = frappe._dict(
			file_url="/private/files/doc.out", file_name="doc.out", file_size=1, name="F-DOC"
		)
		return m

	def test_pdf(self):
		if not _pdf_backend_ok():
			self.skipTest("no HTML→PDF backend (wkhtmltopdf) in this environment")
		m = self._mock()
		out = export_document(_MD, format="pdf")
		fname, payload = _saved(m)
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertTrue(fname.endswith(".pdf"))
		self.assertEqual(payload[:4], b"%PDF")

	def test_html_is_standalone_and_renders_markdown(self):
		m = self._mock()
		out = export_document(_MD, format="html")
		fname, payload = _saved(m)
		text = payload.decode("utf-8")
		self.assertEqual(out["mime_type"], "text/html")
		self.assertTrue(fname.endswith(".html"))
		self.assertIn("<!doctype html>", text.lower())
		self.assertIn("<title>Document</title>", text)  # plain path default (no title)
		self.assertIn("<table", text)  # markdown table rendered
		self.assertIn("Revenue", text)

	def test_png(self):
		if not _png_backend_ok():
			self.skipTest("no PDF→PNG rendering stack (wkhtmltopdf / pypdfium2 / PIL)")
		m = self._mock()
		out = export_document(_MD, format="png")
		fname, payload = _saved(m)
		self.assertEqual(out["mime_type"], "image/png")
		self.assertTrue(fname.endswith(".png"))
		self.assertEqual(payload[:4], b"\x89PNG")

	def test_raw_html_benign_tags_survive_sanitization(self):
		"""content_is_html=True skips markdown conversion, not sanitization —
		benign tags with no dangerous attributes pass through unchanged."""
		m = self._mock()
		export_document("<h1>Raw</h1><p>kept verbatim</p>", format="html", content_is_html=True)
		_, payload = _saved(m)
		text = payload.decode("utf-8")
		self.assertIn("<h1>Raw</h1>", text)  # not markdown-escaped
		self.assertIn("kept verbatim", text)

	def test_default_format_is_pdf(self):
		if not _pdf_backend_ok():
			self.skipTest("no HTML→PDF backend (wkhtmltopdf) in this environment")
		m = self._mock()
		out = export_document(_MD)
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertEqual(_saved(m)[1][:4], b"%PDF")


class TestExportDocumentSanitization(FrappeTestCase):
	"""content is agent-composed (effectively LLM-controlled) text. Two threats,
	both must be closed on BOTH the Markdown branch and the content_is_html=True
	raw-HTML branch: XSS in the standalone HTML, and SSRF-via-render — wkhtmltopdf
	fetches any <img>/<link>/SVG <image> it finds, server-side, at render time."""

	def _mock(self):
		m = patch("frappe.utils.file_manager.save_file").start()
		self.addCleanup(patch.stopall)
		m.return_value = frappe._dict(
			file_url="/private/files/doc.out", file_name="doc.out", file_size=1, name="F-DOC"
		)
		return m

	def _rendered_html(self, content: str, *, content_is_html: bool) -> str:
		m = self._mock()
		export_document(content, format="html", content_is_html=content_is_html)
		return _saved(m)[1].decode("utf-8")

	def test_script_tag_stripped_markdown_branch(self):
		text = self._rendered_html("hello <script>alert(1)</script> world", content_is_html=False)
		self.assertNotIn("<script", text)
		self.assertNotIn("alert(1)", text.replace("&#39;", "'"))  # not present as live JS either way

	def test_script_tag_stripped_raw_html_branch(self):
		text = self._rendered_html("<p>hi</p><script>alert(1)</script>", content_is_html=True)
		self.assertNotIn("<script", text)

	def test_img_tag_stripped_both_branches(self):
		"""The SSRF vector this whole change exists to close: an <img src> makes
		wkhtmltopdf issue a server-side fetch at render time (cloud metadata
		endpoints, internal network probing, file:// local reads)."""
		md_text = self._rendered_html("![x](http://169.254.169.254/latest/meta-data/)", content_is_html=False)
		self.assertNotIn("<img", md_text)
		html_text = self._rendered_html('<img src="file:///etc/passwd">', content_is_html=True)
		self.assertNotIn("<img", html_text)

	def test_svg_image_href_stripped(self):
		text = self._rendered_html(
			'<svg><image href="http://attacker.example/beacon"/></svg>', content_is_html=True
		)
		self.assertNotIn("<svg", text)
		self.assertNotIn("<image", text)

	def test_link_and_meta_stripped(self):
		text = self._rendered_html(
			'<link rel="stylesheet" href="http://attacker.example/x.css">'
			'<meta http-equiv="refresh" content="0;url=http://attacker.example">',
			content_is_html=True,
		)
		self.assertNotIn("<link", text)
		self.assertNotIn("http-equiv", text)

	def test_onerror_attribute_and_javascript_href_stripped(self):
		text = self._rendered_html(
			'<a href="javascript:alert(1)" onclick="alert(2)">click</a>', content_is_html=True
		)
		self.assertNotIn("javascript:", text)
		self.assertNotIn("onclick", text)

	def test_benign_markdown_formatting_still_renders(self):
		text = self._rendered_html("# Title\n\n**bold** and *italic*\n\n- one\n- two", content_is_html=False)
		self.assertIn("<h1", text)  # md_to_html's header-ids extra adds an id attribute
		self.assertIn("Title", text)
		self.assertIn("<strong>bold</strong>", text)
		self.assertIn("<li>one</li>", text)

	def test_content_length_cap(self):
		with self.assertRaises(InvalidArgumentError):
			export_document("x" * 200_001, format="html")
		# exactly at the cap is accepted
		m = self._mock()
		export_document("x" * 200_000, format="html")
		self.assertTrue(m.called)


# ---------------------------------------------------------------------------
# Slice-1 RICH PATH orchestration (site-free unit tests)
#
# export_document imports frappe (md_to_html / save_file / telemetry) and the
# rich render shells out to wkhtmltopdf. We mock the site-dependent seams at the
# module namespace so these prove the *orchestration* — the dual-path branch,
# the post-sanitize splice, the guards, the sanitize→render order, and the
# telemetry outcome — without needing a bench.
# ---------------------------------------------------------------------------

_MODULE = "jarvis.tools.export_document"
_RICH_MD = "# Report\n\nSome prose.\n"


class _RichBase(unittest.TestCase):
	"""Common mocks: render_pdf (no wkhtmltopdf locally), the two savers, and
	telemetry (touches frappe.local). sanitize_rich / component_css / css_bar stay
	REAL — they are pure modules, so the splice/order assertions exercise the true
	code path."""

	def setUp(self):
		self.render = patch(f"{_MODULE}.render_pdf", return_value=b"%PDF-1.4 fake").start()
		# resolve_brand touches frappe.db/has_permission (needs a site); it is
		# unit-tested directly in test_export_document_furniture. Here it is stubbed
		# to the empty "no brand, no note" default; brand-specific tests override.
		self.resolve_brand = patch(
			f"{_MODULE}.resolve_brand",
			return_value={"logo_html": "", "company": None, "address_lines": [], "note": None},
		).start()
		self.save_rich = patch(
			f"{_MODULE}.save_export_file",
			return_value={
				"file_url": "/private/files/document.pdf",
				"filename": "document.pdf",
				"title": "Document",
				"mime_type": "application/pdf",
				"size_bytes": 12,
				"name": "F-RICH",
			},
		).start()
		# Plain path uses its own save_file + get_pdf; keep them site-free too.
		self.save_plain = patch("frappe.utils.file_manager.save_file").start()
		self.save_plain.return_value = frappe._dict(
			file_url="/private/files/doc.out", file_name="doc.out", file_size=1, name="F-PLAIN"
		)
		self.get_pdf = patch("frappe.utils.pdf.get_pdf", return_value=b"%PDF-plain").start()
		self.telemetry = patch(f"{_MODULE}.telemetry.record_export_event").start()
		self.addCleanup(patch.stopall)

	def _rich_doc_body(self) -> str:
		"""The composed doc_body export_document handed render_pdf."""
		self.assertTrue(self.render.called, "render_pdf was not called (took the plain path?)")
		return self.render.call_args.args[0]


class TestDualPathBranching(_RichBase):
	"""Branch on TRUTHY (non-empty) rich kwargs. Empty ≠ present."""

	def test_no_rich_kwargs_takes_plain_path(self):
		# The load-bearing regression: a legacy call renders through the plain
		# get_pdf path and NEVER touches the rich renderer.
		export_document(_RICH_MD, format="pdf")  # content-only: title now activates rich
		self.assertTrue(self.get_pdf.called)
		self.assertFalse(self.render.called)
		self.assertTrue(self.save_plain.called)
		self.assertFalse(self.save_rich.called)

	def test_falsy_rich_kwargs_stay_plain(self):
		# empty string / empty list / False / None must NOT flip to rich.
		for kw in (
			{"header": ""},
			{"footer": ""},
			{"watermark": ""},
			{"charts": []},
			{"theme": False},
			{"letterhead": None},
			{"letterhead": ""},
			{"letterhead": False},
		):
			with self.subTest(kw=kw):
				self.render.reset_mock()
				self.get_pdf.reset_mock()
				export_document(_RICH_MD, format="pdf", **kw)
				self.assertFalse(self.render.called, f"{kw} wrongly flipped to the rich path")
				self.assertTrue(self.get_pdf.called)

	def test_truthy_rich_kwargs_take_rich_path(self):
		for kw in (
			{"theme": True},
			{"header": "Confidential"},
			{"footer": "page footer"},
			{"watermark": "DRAFT"},
			{"letterhead": "Standard"},
			{"letterhead": True},
			{"charts": [{"rows": [{"label": "A", "value": "1", "pct": 50}]}]},
		):
			with self.subTest(kw=kw):
				self.render.reset_mock()
				self.get_pdf.reset_mock()
				# charts case needs its token so the splice has a home.
				content = "{{chart:0}}\n\nbody" if "charts" in kw else _RICH_MD
				export_document(content, format="pdf", **kw)
				self.assertTrue(self.render.called, f"{kw} did not flip to the rich path")
				self.assertFalse(self.get_pdf.called)


class TestRichPipeline(_RichBase):
	def test_sanitize_runs_on_full_body_before_render(self):
		"""Structural order spy: sanitize_rich is called on the body, and its
		stripping is visible in the exact doc_body handed to render_pdf, which runs
		strictly AFTER sanitize."""
		order = []

		def spy_sanitize(html):
			order.append("sanitize")
			return _real_sanitize_rich(html)

		def spy_render(doc_body, **kw):
			order.append("render")
			spy_render.doc_body = doc_body
			return b"%PDF-1.4 fake"

		with (
			patch(f"{_MODULE}.sanitize_rich", side_effect=spy_sanitize),
			patch(f"{_MODULE}.render_pdf", side_effect=spy_render),
		):
			export_document(
				"<p>keep me</p><script>alert(1)</script>",
				format="pdf",
				content_is_html=True,
				theme=True,
			)
		self.assertEqual(order, ["sanitize", "render"])
		self.assertIn("keep me", spy_render.doc_body)
		self.assertNotIn("<script", spy_render.doc_body)  # sanitize ran before render

	def test_chart_token_spliced_post_sanitize_into_render_body(self):
		"""{{chart:0}} is replaced by css_bar() output — tool-generated HTML added
		AFTER sanitize — and that output reaches the doc_body render_pdf sees."""
		with patch(f"{_MODULE}.css_bar", return_value="<div>SENTINEL_BAR</div>") as bar:
			export_document(
				"<p>{{chart:0}}</p>",
				format="pdf",
				content_is_html=True,
				charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
			)
		self.assertTrue(bar.called)
		body = self._rich_doc_body()
		self.assertIn("SENTINEL_BAR", body)
		self.assertNotIn("{{chart:0}}", body)
		# THEME_CSS is tool-added after sanitize.
		self.assertIn("<style>", body)
		self.assertIn("bar-chart", body)  # component_css contract class present

	def test_post_sanitize_empty_raises_no_data(self):
		# content that sanitizes to nothing must never yield a blank PDF.
		with self.assertRaises(NoDataError):
			export_document("<script>only unsafe</script>", content_is_html=True, theme=True)
		self.assertFalse(self.render.called)

	def test_too_many_charts_raises_invalid_argument(self):
		import jarvis.tools.export_document as ed

		specs = [{"rows": []} for _ in range(ed._MAX_CHARTS + 1)]
		with self.assertRaises(InvalidArgumentError):
			export_document(_RICH_MD, format="pdf", charts=specs)
		self.assertFalse(self.render.called)

	def test_max_charts_boundary_accepted(self):
		import jarvis.tools.export_document as ed

		specs = [{"rows": []} for _ in range(ed._MAX_CHARTS)]
		export_document(_RICH_MD, format="pdf", charts=specs)  # at the cap: fine
		self.assertTrue(self.render.called)

	def test_spec_without_token_adds_note(self):
		out = export_document(
			"<p>no placeholder here</p>",
			format="pdf",
			content_is_html=True,
			charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
		)
		self.assertTrue(self.render.called)  # no crash
		self.assertTrue(any("chart 0" in n for n in out["notes"]), out["notes"])

	def test_token_without_spec_adds_note(self):
		out = export_document(
			"<p>Report body</p><p>{{chart:5}}</p>",  # real body + an orphan token
			format="pdf",
			content_is_html=True,
			header="Confidential",  # trigger rich without providing chart 5
		)
		self.assertTrue(any("chart:5" in n for n in out["notes"]), out["notes"])
		# the orphan token is not left as literal junk in the document.
		self.assertNotIn("{{chart:5}}", self._rich_doc_body())

	def test_blank_after_orphan_token_raises_no_data(self):
		# content that is ONLY an orphan chart token strips to <p></p> — a childless
		# skeleton — which must be caught as blank, not shipped as an empty PDF.
		with self.assertRaises(NoDataError):
			export_document("{{chart:0}}", content_is_html=False, theme=True)
		self.assertFalse(self.render.called)

	def test_tag_skeleton_blank_raises_no_data(self):
		with self.assertRaises(NoDataError):
			export_document("<div><span></span></div>", content_is_html=True, theme=True)
		self.assertFalse(self.render.called)

	def test_malformed_chart_rows_degrade_to_note_not_crash(self):
		# A malformed rows shape (the review's top cross-lane finding) degrades the
		# chart to a note; the export still succeeds, and telemetry still fires.
		out = export_document(
			"<p>Body</p><p>{{chart:0}}</p>",
			format="pdf",
			content_is_html=True,
			charts=[{"rows": [[1, 2]]}],  # 2-tuple rows → css_bar would raise
		)
		self.assertTrue(self.render.called)
		self.assertTrue(any("chart 0" in n for n in out["notes"]), out["notes"])
		self.assertNotIn("{{chart:0}}", self._rich_doc_body())

	def test_total_chart_rows_cap(self):
		import jarvis.tools.export_document as ed

		big = [{"rows": [{"label": "x", "value": "1", "pct": 1}] * (ed._MAX_TOTAL_CHART_ROWS + 1)}]
		with self.assertRaises(InvalidArgumentError):
			export_document(_RICH_MD, format="pdf", charts=big)
		self.assertFalse(self.render.called)

	def test_token_in_code_block_left_literal(self):
		# A {{chart:0}} shown inside <code> is documentation, not a placeholder —
		# it must NOT be spliced (tree-aware, text-node-only splice).
		export_document(
			"<p>Docs:</p><pre><code>{{chart:0}}</code></pre>",
			format="pdf",
			content_is_html=True,
			charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
		)
		body = self._rich_doc_body()
		self.assertIn("{{chart:0}}", body)  # left literal inside code
		self.assertNotIn("bar-chart", body.split("<code>")[1].split("</code>")[0])

	def test_token_in_attribute_not_corrupted(self):
		# A token that survives inside an href must not corrupt the tag (the splice
		# never touches attribute values).
		with patch(f"{_MODULE}.css_bar", return_value="<div>BAR</div>"):
			export_document(
				'<a href="https://x/{{chart:0}}">link</a>',
				format="pdf",
				content_is_html=True,
				charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
			)
		body = self._rich_doc_body()
		self.assertNotIn('<div>BAR</div>"', body)  # not spliced INTO the href value
		self.assertIn("link", body)

	def test_chart_value_with_token_no_double_substitution(self):
		# chart 0's rendered output contains the literal text "{{chart:1}}"; chart 1
		# must NOT be spliced into chart 0's already-rendered markup.
		def fake_bar(rows, **kw):
			label = rows[0]["label"] if rows else ""
			return f"<div>BAR::{label}</div>"

		with patch(f"{_MODULE}.css_bar", side_effect=fake_bar):
			export_document(
				"<p>{{chart:0}}</p><p>{{chart:1}}</p>",
				format="pdf",
				content_is_html=True,
				charts=[{"rows": [{"label": "{{chart:1}}", "value": "x", "pct": 1}]}, {"rows": []}],
			)
		body = self._rich_doc_body()
		# chart 0 rendered once with its literal label; chart 1 rendered once.
		self.assertEqual(body.count("BAR::"), 2)

	def test_unexpected_error_logged_and_reraised_clean(self):
		# A bug on the rich path (here: sanitize_rich blows up) must not escape as a
		# raw 500 — it is caught at the boundary, re-raised clean, and telemetried.
		with patch(f"{_MODULE}.sanitize_rich", side_effect=RuntimeError("boom")):
			with self.assertRaises(InvalidArgumentError):
				export_document(_RICH_MD, format="pdf", theme=True)
		self.assertEqual(self.telemetry.call_args.kwargs["outcome"], "rejected")

	def test_rich_render_passes_furniture_and_caps_to_render_pdf(self):
		import jarvis.tools.export_document as ed

		self.resolve_brand.return_value = {
			"logo_html": '<img src="data:image/png;base64,AAA">',
			"company": "Acme",
			"address_lines": [],
			"note": None,
		}
		export_document(
			_RICH_MD,
			format="pdf",
			header="H",
			footer="F",
			watermark="W",
			letterhead="Standard",
			page_size="Letter",
			orientation="landscape",
			margins_mm=20,
			page_numbers=False,
		)
		_, kwargs = self.render.call_args
		self.assertEqual(kwargs["header_html"], "H")
		# footer is now a plain TEXT zone, not an HTML footer.
		self.assertEqual(kwargs["footer_text"], "F")
		self.assertEqual(kwargs["watermark"], "W")
		# branding rides the running HEADER (built from the resolved brand); the raw
		# name is never forwarded, and there is no letterhead_* kwarg anymore.
		self.assertIn("jv-brand-header", kwargs["brand_header"])
		self.assertIn("data:image/png;base64,AAA", kwargs["brand_header"])
		self.assertNotIn("letterhead", kwargs)
		self.assertNotIn("footer_html", kwargs)
		self.assertEqual(kwargs["page_size"], "Letter")
		self.assertEqual(kwargs["orientation"], "Landscape")  # normalized up front
		self.assertEqual(kwargs["margins_mm"], 20)
		self.assertFalse(kwargs["page_numbers"])
		# timeout is the render budget minus pre-render work already spent.
		self.assertLessEqual(kwargs["timeout"], ed._RENDER_TIMEOUT_S)
		self.assertGreaterEqual(kwargs["timeout"], 5)

	def test_letterhead_name_resolved_verbatim(self):
		export_document(_RICH_MD, format="pdf", letterhead="Acme")
		self.assertEqual(self.resolve_brand.call_args.args[0], "Acme")

	def test_letterhead_bool_resolves_to_default(self):
		# letterhead=True triggers rich but is NOT a name -> resolve with None so
		# the site's DEFAULT Letter Head / Company is used.
		export_document(_RICH_MD, format="pdf", letterhead=True)
		self.assertIsNone(self.resolve_brand.call_args.args[0])

	def test_charts_only_rich_render_still_resolves_default_brand(self):
		# A branded render with no letterhead intent still resolves the default brand.
		export_document(
			"body {{chart:0}}",
			format="pdf",
			charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
		)
		self.assertTrue(self.resolve_brand.called)
		self.assertIsNone(self.resolve_brand.call_args.args[0])

	def test_letterhead_not_found_note_surfaced(self):
		self.resolve_brand.return_value = {
			"logo_html": "",
			"company": None,
			"address_lines": [],
			"note": "letterhead 'Ghost' not found — rendered without it",
		}
		out = export_document(_RICH_MD, format="pdf", letterhead="Ghost")
		self.assertTrue(any("Ghost" in n for n in out["notes"]), out["notes"])

	def test_notes_field_present_even_when_clean(self):
		out = export_document(_RICH_MD, format="pdf", theme=True)
		self.assertEqual(out["notes"], [])


class TestMastheadAndCover(_RichBase):
	"""The tool-built masthead, the scalable cover, and numeric-column alignment."""

	def _render_body(self, *, content, **kw):
		export_document(content, format="pdf", **kw)
		return self._rich_doc_body()

	def test_title_becomes_tool_built_masthead_not_literal_markdown(self):
		# The title is a param the tool escapes into a div — the old bug (agent
		# wrapping markdown in <div class="cover">, so `#`/`**` rendered literally)
		# cannot happen.
		body = self._render_body(
			content="<p>real body</p>", content_is_html=True, theme=True, title="# ERP <x>"
		)
		self.assertIn('<div class="doc-title"># ERP &lt;x&gt;</div>', body)
		self.assertIn("doc-title-block", body)
		self.assertIn("real body", body)

	def test_subtitle_and_meta_plumb_through(self):
		body = self._render_body(
			content="<p>b</p>", content_is_html=True, theme=True, title="T", subtitle="Sub", meta="INR"
		)
		self.assertIn('<div class="doc-subtitle">Sub</div>', body)
		self.assertIn('<div class="doc-meta">INR</div>', body)

	def test_cover_true_forces_full_cover(self):
		body = self._render_body(content="<p>b</p>", content_is_html=True, theme=True, title="T", cover=True)
		self.assertIn('<div class="cover">', body)

	def test_cover_false_forces_title_block(self):
		body = self._render_body(content="<p>b</p>", content_is_html=True, theme=True, title="T", cover=False)
		self.assertIn("doc-title-block", body)
		self.assertNotIn('<div class="cover">', body)

	def test_cover_auto_off_for_short_doc(self):
		body = self._render_body(content="<p>short</p>", content_is_html=True, theme=True, title="T")
		self.assertNotIn('<div class="cover">', body)

	def test_cover_auto_on_for_long_structured_doc(self):
		long_body = "<h2>Section</h2><p>" + ("word " * 1500) + "</p>"  # >6000 chars + a heading
		body = self._render_body(content=long_body, content_is_html=True, theme=True, title="T")
		self.assertIn('<div class="cover">', body)

	def test_markdown_right_aligned_column_promoted_to_num(self):
		md = "| Item | Amount |\n|:-----|-------:|\n| A | 100 |\n"
		body = self._render_body(content=md, theme=True)
		self.assertIn('class="num"', body)  # numeric column promoted
		self.assertNotIn('style="text-align', body)  # the inline style was stripped by sanitize

	def test_component_css_built_with_page_geometry(self):
		with patch(f"{_MODULE}.component_css", return_value="/* css */") as cc:
			export_document(
				_RICH_MD, format="pdf", theme=True, page_size="Letter", orientation="landscape", margins_mm=20
			)
		# geometry is normalized up front (orientation canonicalized to "Landscape").
		self.assertEqual(cc.call_args.args, ("Letter", "Landscape", 20))
		# no brand/header/watermark in this render → header=False (cover not reserved).
		self.assertFalse(cc.call_args.kwargs["header"])


class TestFormatMatrix(_RichBase):
	def test_html_returns_composed_doc_body(self):
		out = export_document("<p>hello</p>", format="html", content_is_html=True, theme=True, title="Q3")
		self.assertFalse(self.render.called)  # html needs no wkhtmltopdf
		fname, payload = self.save_rich.call_args.args[0], self.save_rich.call_args.args[1]
		self.assertTrue(fname.endswith(".html"))
		text = payload.decode("utf-8")
		self.assertIn("hello", text)
		self.assertIn("<style>", text)  # component_css composed in
		# rich HTML must be a WELL-FORMED standalone document (doctype + charset +
		# title), not a bare fragment — else non-ASCII renders as mojibake.
		self.assertIn("<!doctype html>", text.lower())
		self.assertIn("charset", text.lower())
		self.assertIn("<title>Q3</title>", text)
		self.assertEqual(out["notes"], [])

	def test_html_with_furniture_kwarg_adds_ignored_note(self):
		out = export_document("<p>hi</p>", format="html", content_is_html=True, header="Confidential")
		self.assertFalse(self.render.called)
		self.assertTrue(
			any("only to PDF" in n or "furniture" in n.lower() for n in out["notes"]), out["notes"]
		)

	def test_png_renders_pdf_then_rasterizes(self):
		with patch(f"{_MODULE}._pdf_to_png", return_value=b"\x89PNG-fake") as topng:
			export_document(_RICH_MD, format="png", theme=True)
		self.assertTrue(self.render.called)
		topng.assert_called_once_with(b"%PDF-1.4 fake")
		self.assertEqual(self.save_rich.call_args.args[1], b"\x89PNG-fake")


class TestTelemetry(_RichBase):
	def _last_outcome(self):
		self.assertTrue(self.telemetry.called)
		return self.telemetry.call_args.kwargs["outcome"]

	def test_clean_rich_render_is_ok(self):
		export_document(_RICH_MD, format="html", theme=True)
		self.assertEqual(self._last_outcome(), "ok")

	def test_degraded_when_notes_emitted(self):
		export_document("<p>hi</p>", format="html", content_is_html=True, header="Confidential")
		self.assertEqual(self._last_outcome(), "degraded")

	def test_plain_path_emits_ok(self):
		export_document(_RICH_MD, format="html")
		self.assertEqual(self._last_outcome(), "ok")

	def test_no_data_guard_emits_no_data_outcome(self):
		with self.assertRaises(NoDataError):
			export_document("")
		self.assertEqual(self._last_outcome(), "no_data")

	def test_bad_format_emits_rejected_outcome(self):
		with self.assertRaises(InvalidArgumentError):
			export_document(_RICH_MD, format="docx")
		self.assertEqual(self._last_outcome(), "rejected")

	def test_telemetry_carries_rich_flag(self):
		# The rich path and the byte-preserved plain path must be distinguishable in
		# the telemetry stream (they would otherwise collapse to one tuple).
		export_document(_RICH_MD, format="html", theme=True)
		self.assertIs(self.telemetry.call_args.kwargs["rich"], True)
		self.telemetry.reset_mock()
		export_document(_RICH_MD, format="html")
		self.assertIs(self.telemetry.call_args.kwargs["rich"], False)


class TestPlainPathBackCompatUnit(_RichBase):
	"""Site-free mirror of the FrappeTestCase plain-path assertions, so the
	'plain path unchanged' contract is proven under bare pytest too."""

	def _plain_html(self, content, *, content_is_html=False):
		# content-only (no title): title now activates the rich path.
		export_document(content, format="html", content_is_html=content_is_html)
		return self.save_plain.call_args.args[1].decode("utf-8")

	def test_plain_html_is_standalone_and_renders_markdown(self):
		text = self._plain_html(_MD)
		self.assertFalse(self.render.called)
		self.assertIn("<!doctype html>", text.lower())
		self.assertIn("<title>Document</title>", text)  # plain path default (no title)
		self.assertIn("<table", text)
		self.assertIn("Revenue", text)

	def test_plain_script_and_img_stripped(self):
		md = self._plain_html("![x](http://169.254.169.254/latest/meta-data/)")
		self.assertNotIn("<img", md)
		html_text = self._plain_html("<p>hi</p><script>alert(1)</script>", content_is_html=True)
		self.assertNotIn("<script", html_text)

	def test_plain_pdf_uses_get_pdf_not_rich(self):
		out = export_document(_MD, format="pdf")  # content-only: title now activates rich
		self.assertTrue(self.get_pdf.called)
		self.assertFalse(self.render.called)
		self.assertEqual(self.save_plain.call_args.args[1], b"%PDF-plain")
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertNotIn("notes", out)  # plain return shape unchanged (no notes key)


class TestBlankAndNoteEdges(_RichBase):
	def test_entity_whitespace_only_raises_no_data(self):
		# A doc whose only content is entity-whitespace (&nbsp;) renders blank —
		# _has_visible_content unescapes entities, so this is caught, not shipped.
		with self.assertRaises(NoDataError):
			export_document("<p>&nbsp;&nbsp;</p>", content_is_html=True, theme=True)
		self.assertFalse(self.render.called)

	def test_chart_only_empty_labels_still_renders(self):
		# A chart-only doc whose rows carry no label/value still has visible bars —
		# it must NOT be rejected as blank (bar-chart presence counts as content).
		out = export_document(
			"{{chart:0}}",
			format="pdf",
			charts=[{"rows": [{"pct": 50}, {"pct": 80}]}],
		)
		self.assertTrue(self.render.called)
		self.assertEqual(out["notes"], [])
		self.assertIn("bar-chart", self._rich_doc_body())

	def test_token_in_code_with_spec_no_false_not_found_note(self):
		# A chart referenced ONLY inside a <code> example (left literal) must not be
		# mis-reported as "placeholder not found" — it IS in the content.
		out = export_document(
			"<p>Body</p><pre><code>{{chart:0}}</code></pre>",
			format="pdf",
			content_is_html=True,
			charts=[{"rows": [{"label": "A", "value": "1", "pct": 50}]}],
		)
		self.assertEqual(out["notes"], [], out["notes"])
		self.assertIn("{{chart:0}}", self._rich_doc_body())  # left literal


class TestTelemetryDirect(unittest.TestCase):
	"""Direct coverage of record_export_event's new rich/detail fields (the
	call-site tests only prove export_document PASSES them; this proves the
	function actually lands them in the emitted line)."""

	def test_emits_rich_and_detail(self):
		captured: dict = {}
		with (
			patch("jarvis.telemetry._emit", side_effect=lambda e: captured.update(e)),
			patch("jarvis.telemetry.frappe.utils.now", return_value="2026-01-01 00:00:00"),
		):
			from jarvis import telemetry

			telemetry.record_export_event(
				tool="export_document",
				fmt="pdf",
				rows=0,
				outcome="rejected",
				rich=True,
				detail="ValueError: boom",
			)
		self.assertEqual(captured.get("kind"), "export")
		self.assertIs(captured.get("rich"), True)
		self.assertIn("boom", captured.get("detail", ""))

	def test_omits_rich_and_detail_when_absent(self):
		captured: dict = {}
		with (
			patch("jarvis.telemetry._emit", side_effect=lambda e: captured.update(e)),
			patch("jarvis.telemetry.frappe.utils.now", return_value="x"),
		):
			from jarvis import telemetry

			telemetry.record_export_event(tool="t", fmt="pdf", rows=0)
		self.assertNotIn("rich", captured)
		self.assertNotIn("detail", captured)


class TestReviewFixes(_RichBase):
	"""Locks the /review-loop fix wave for the composed rich path."""

	def test_title_subtitle_meta_cover_activate_rich(self):
		# Critical: a titled export must reach the rich masthead path (title-only on
		# the plain path rendered NO visible title).
		for kw in ({"title": "Q3 Report"}, {"subtitle": "sub"}, {"meta": "INR"}, {"cover": True}):
			with self.subTest(kw=kw):
				self.render.reset_mock()
				self.get_pdf.reset_mock()
				export_document(_RICH_MD, format="pdf", **kw)
				self.assertTrue(self.render.called, f"{kw} did not activate the rich path")
				self.assertFalse(self.get_pdf.called)

	def test_cover_false_alone_stays_plain(self):
		export_document(_RICH_MD, format="pdf", cover=False)  # falsy → not an activator
		self.assertTrue(self.get_pdf.called)
		self.assertFalse(self.render.called)

	def test_chart_title_caption_forwarded_to_css_bar(self):
		with patch(f"{_MODULE}.css_bar", return_value="<div>BAR</div>") as bar:
			export_document(
				"<p>{{chart:0}}</p>",
				format="pdf",
				content_is_html=True,
				charts=[{"rows": [{"label": "A", "value": "1", "pct": 5}], "title": "Rev", "caption": "INR"}],
			)
		self.assertEqual(bar.call_args.kwargs.get("title"), "Rev")
		self.assertEqual(bar.call_args.kwargs.get("caption"), "INR")

	def test_cover_height_reserves_header_when_brand_present(self):
		# Critical: a running header (brand) → component_css gets header=True so the
		# cover height accounts for the reserved top margin (no page-2 overflow).
		self.resolve_brand.return_value = {
			"logo_html": "",
			"company": "Acme",
			"address_lines": [],
			"note": None,
		}
		with patch(f"{_MODULE}.component_css", return_value="/* css */") as cc:
			export_document(_RICH_MD, format="pdf", theme=True)
		self.assertTrue(cc.call_args.kwargs["header"])

	def test_brand_and_page_numbers_coexist(self):
		# The plan's named regression: a brand header AND page numbers both render.
		self.resolve_brand.return_value = {
			"logo_html": "",
			"company": "Acme",
			"address_lines": [],
			"note": None,
		}
		export_document(_RICH_MD, format="pdf", theme=True)  # page_numbers default True
		kwargs = self.render.call_args.kwargs
		self.assertIn("jv-brand-header", kwargs["brand_header"])
		self.assertTrue(kwargs["page_numbers"])

	def test_masthead_length_guard(self):
		with self.assertRaises(InvalidArgumentError):
			export_document("<p>body</p>", content_is_html=True, theme=True, title="x" * 3000)
		self.assertFalse(self.render.called)

	def test_html_path_validates_geometry_like_pdf(self):
		# The HTML path now rejects a bad page_size the same as the PDF path.
		with self.assertRaises(InvalidArgumentError):
			export_document("<p>b</p>", format="html", content_is_html=True, theme=True, page_size="A2")


class TestDecideCover(unittest.TestCase):
	"""_decide_cover's AND semantics + page-break token detection (pure)."""

	def _long(self):
		return "<p>" + ("word " * 1500) + "</p>"  # >6000 visible chars

	def test_and_semantics(self):
		import jarvis.tools.export_document as ed

		self.assertFalse(ed._decide_cover(None, self._long()))  # long, UNstructured → no
		self.assertFalse(ed._decide_cover(None, "<h2>s</h2><p>short</p>"))  # structured, SHORT → no
		self.assertTrue(ed._decide_cover(None, "<h2>s</h2>" + self._long()))  # both → yes

	def test_page_break_token_multi_class(self):
		import jarvis.tools.export_document as ed

		# page-break combined with another class still counts as structure.
		body = '<div class="section-divider page-break"></div>' + self._long()
		self.assertTrue(ed._decide_cover(None, body))

	def test_explicit_override(self):
		import jarvis.tools.export_document as ed

		self.assertTrue(ed._decide_cover(True, ""))
		self.assertFalse(ed._decide_cover(False, "<h1>x</h1>" + ("y" * 7000)))


class TestPromoteTableAlignment(unittest.TestCase):
	"""_promote_table_alignment right→num, center→text-center, merge/negative (pure)."""

	def test_right_to_num(self):
		import jarvis.tools.export_document as ed

		self.assertIn('class="num"', ed._promote_table_alignment('<td style="text-align:right;">9</td>'))

	def test_center_to_text_center(self):
		import jarvis.tools.export_document as ed

		self.assertIn(
			'class="text-center"', ed._promote_table_alignment('<th style="text-align:center;">H</th>')
		)

	def test_left_and_no_style_untouched(self):
		import jarvis.tools.export_document as ed

		out = ed._promote_table_alignment('<td style="text-align:left;">L</td><td>plain</td>')
		self.assertNotIn("num", out)
		self.assertNotIn("text-center", out)

	def test_existing_class_merged_not_duplicated(self):
		import jarvis.tools.export_document as ed

		out = ed._promote_table_alignment('<td class="foo" style="text-align:right;">9</td>')
		self.assertIn('class="foo num"', out)
		self.assertEqual(out.count("class="), 1)

	def test_multi_property_style_still_matches(self):
		import jarvis.tools.export_document as ed

		out = ed._promote_table_alignment('<td style="color:red;text-align:right;">9</td>')
		self.assertIn('class="num"', out)
