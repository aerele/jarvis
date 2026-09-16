"""Tests for the predefined PDF templates feature.

Two layers, mirroring theme.py's own split:
  * pure unit tests (plain ``unittest``, no bench) - the template SET and the
    token-driven stylesheet, including the load-bearing "classic == today's
    output" snapshot;
  * bench tests (``FrappeTestCase``) - export_document's ``template`` arg, the
    whitelisted list/get/set-default API, and the per-document re-render.

The classic snapshot guards the whole refactor: ``component_css()`` with no
override must stay byte-identical to ``fixtures/classic_component_css.txt``
(captured from the pre-refactor engine). If that breaks, EXISTING PDFs changed -
the one thing the token refactor promised never to do.

render_pdf is mocked throughout the bench suite: wkhtmltopdf is absent in local
dev (see test_export_document_furniture), so a real render is a Frappe Cloud
smoke concern, not a unit-test one.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import pdf_templates as api
from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import JARVIS_USER_ROLE
from jarvis.tools._export import load_render_sidecar
from jarvis.tools._export.document import templates as tpl
from jarvis.tools._export.document import theme
from jarvis.tools.export_document import export_document

_MODULE = "jarvis.tools.export_document"
_MD = "# Report\n\nSome prose.\n\n- a\n- b\n"
_FIXTURE = Path(__file__).parent / "fixtures" / "classic_component_css.txt"
_SETTINGS = "Jarvis Settings"
_FIELD = "default_pdf_template"
_ALL = {"classic", "editorial", "minimal", "branded", "formal"}


def _valid_pdf() -> bytes:
	"""A real, parseable one-page PDF. render_pdf is mocked in the bench suite, and
	Frappe 16's File.check_content runs pypdf over every saved PDF (a JS-embed
	scan) - fake bytes fail that, so the tests that persist a real File hand it
	this instead."""
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=72, height=72)
	buf = BytesIO()
	writer.write(buf)
	return buf.getvalue()


# --------------------------------------------------------------------------- #
# Pure module tests - the template set + token stylesheet. FrappeTestCase (not
# bare unittest) so `bench run-tests` collects them; they use no site features.
# --------------------------------------------------------------------------- #


class TestTemplatesModule(FrappeTestCase):
	def test_keys_and_default(self):
		self.assertEqual(set(tpl.KEYS), _ALL)
		self.assertEqual(tpl.DEFAULT_TEMPLATE, "classic")

	def test_classic_css_is_empty_override(self):
		# The invariant's root: classic states no palette/font change, so it is the
		# engine's current look verbatim.
		self.assertEqual(tpl.TEMPLATES["classic"]["css"], {})

	def test_resolve_unknown_and_blank_fall_back_to_classic(self):
		for bad in ("nope", "", "   ", None):
			self.assertEqual(tpl.resolve(bad)["key"], "classic")

	def test_resolve_is_case_and_space_insensitive(self):
		self.assertEqual(tpl.resolve("  EDITORIAL ")["key"], "editorial")

	def test_is_valid(self):
		self.assertTrue(tpl.is_valid("minimal"))
		self.assertTrue(tpl.is_valid(" Formal "))
		self.assertFalse(tpl.is_valid("nope"))
		self.assertFalse(tpl.is_valid(None))

	def test_summaries_shape_and_spec(self):
		by_key = {d["key"]: d for d in tpl.summaries()}
		self.assertEqual(set(by_key), _ALL)
		fields = {"key", "label", "description", "accent", "body_font", "display_font", "masthead", "cover"}
		for d in by_key.values():
			self.assertEqual(set(d), fields)
		self.assertEqual(by_key["editorial"]["body_font"], "serif")
		self.assertEqual(by_key["editorial"]["masthead"], "center")
		self.assertEqual(by_key["minimal"]["display_font"], "sans")
		self.assertEqual(by_key["branded"]["accent"], "#2e7d6b")


class TestComponentCssTokens(FrappeTestCase):
	def test_classic_snapshot_unchanged(self):
		golden = _FIXTURE.read_text()
		self.assertEqual(
			theme.component_css(), golden, "classic component_css drifted from the golden snapshot"
		)

	def test_classic_token_set_matches_default(self):
		# Applying classic's (empty) override must equal passing no override at all.
		self.assertEqual(
			theme.component_css(css_tokens=tpl.resolve("classic")["css"]),
			theme.component_css(),
		)

	def test_each_template_primary_reaches_css(self):
		for key in tpl.KEYS:
			css = theme.component_css(css_tokens=tpl.resolve(key)["css"])
			primary = tpl.resolve(key)["css"].get("primary", "#1f4e79")
			self.assertIn(primary, css, f"{key} primary {primary} missing from its stylesheet")

	def test_editorial_uses_serif_and_maroon(self):
		css = theme.component_css(css_tokens=tpl.resolve("editorial")["css"])
		self.assertIn("#7a3b3b", css)
		self.assertIn(theme.FONT_STACKS["serif"], css)

	def test_unknown_token_is_ignored_not_crash(self):
		# Only real palette/font tokens are merged; a stray key is dropped, never
		# injected and never a KeyError.
		css = theme.component_css(css_tokens={"bogus": "x", "primary": "#123456"})
		self.assertIn("#123456", css)
		self.assertNotIn("bogus", css)

	def test_font_scale_is_additive_and_opt_in(self):
		# The scale is APPENDED after the untouched base sheet (so the golden classic
		# prefix is preserved verbatim), restates only the reading sizes, and is
		# absent when a template does not set it.
		golden = _FIXTURE.read_text()
		scaled = theme.component_css(css_tokens={"font_scale": 1.05})
		self.assertTrue(scaled.startswith(golden))
		self.assertIn("body { font-size: 11pt; }", scaled)
		self.assertIn("h1 { font-size: 23.1pt; }", scaled)
		self.assertIn(".cover .doc-title { font-size: 35.7pt; }", scaled)
		self.assertNotIn("template type scale", theme.component_css(css_tokens={"primary": "#123456"}))

	def test_font_scale_ignores_unity_and_out_of_range(self):
		golden = _FIXTURE.read_text()
		for value in (1, 1.0, 0.5, 3, "x", None):
			self.assertEqual(
				theme.component_css(css_tokens={"font_scale": value}), golden, f"font_scale={value!r}"
			)

	def test_predefined_scales_reach_their_stylesheets(self):
		self.assertIn(
			"template type scale x1.05", theme.component_css(css_tokens=tpl.resolve("editorial")["css"])
		)
		self.assertIn(
			"template type scale x0.95", theme.component_css(css_tokens=tpl.resolve("minimal")["css"])
		)
		for key in ("classic", "branded", "formal"):
			self.assertNotIn("template type scale", theme.component_css(css_tokens=tpl.resolve(key)["css"]))


# --------------------------------------------------------------------------- #
# export_document(template=...) - activation, palette wiring, arg override,
# unknown-key degrade. render_pdf mocked; its args carry the composed body+CSS.
# --------------------------------------------------------------------------- #


class TestExportDocumentTemplate(FrappeTestCase):
	def setUp(self):
		self.render = patch(f"{_MODULE}.render_pdf", return_value=b"%PDF-1.4 fake").start()
		# save + sidecar are exercised in TestRerenderDocument; stub them here so
		# these palette assertions inspect only what render_pdf received and never
		# persist a File (Frappe 16 runs pypdf over every saved PDF - fake bytes
		# fail that scan). A fresh dict per call avoids cross-test note bleed.
		patch(f"{_MODULE}.save_export_file", side_effect=lambda *a, **k: {"name": "F-TEST"}).start()
		patch(f"{_MODULE}.save_render_sidecar", return_value=None).start()
		self.addCleanup(patch.stopall)

	def _styled_body(self) -> str:
		"""The composed <style>+masthead+body handed to render_pdf (arg 0)."""
		self.assertTrue(self.render.called, "render_pdf was not called - took the plain path?")
		return self.render.call_args.args[0]

	def test_template_activates_rich_path(self):
		# A template alone (no other rich kwarg) must switch to the branded renderer.
		export_document(_MD, format="pdf", template="editorial")
		self.assertTrue(self.render.called)

	def test_editorial_palette_in_css(self):
		export_document(_MD, format="pdf", template="editorial")
		self.assertIn("#7a3b3b", self._styled_body())

	def test_classic_palette_and_no_maroon(self):
		export_document(_MD, format="pdf", template="classic")
		body = self._styled_body()
		self.assertIn("#1f4e79", body)
		self.assertNotIn("#7a3b3b", body)

	def test_branded_draws_accent_bar(self):
		export_document(_MD, format="pdf", title="Rpt", template="branded")
		self.assertIn("border-top:6pt solid #2e7d6b", self._styled_body())

	def test_editorial_centers_masthead_when_cover_off(self):
		export_document(_MD, format="pdf", title="Rpt", template="editorial", cover=False)
		self.assertIn('style="text-align:center"', self._styled_body())

	def test_template_default_watermark_applied(self):
		export_document(_MD, format="pdf", template="formal")
		self.assertEqual(self.render.call_args.kwargs["watermark"], "CONFIDENTIAL")

	def test_explicit_watermark_overrides_template(self):
		export_document(_MD, format="pdf", template="formal", watermark="DRAFT")
		self.assertEqual(self.render.call_args.kwargs["watermark"], "DRAFT")

	def test_unknown_template_degrades_to_classic_with_note(self):
		out = export_document(_MD, format="pdf", template="bogus")
		self.assertTrue(any("Unknown PDF template" in n for n in out["notes"]))
		self.assertIn("#1f4e79", self._styled_body())


# --------------------------------------------------------------------------- #
# Whitelisted API: list / get default / set default.
# --------------------------------------------------------------------------- #


class TestPdfTemplatesApi(FrappeTestCase):
	def setUp(self):
		self._snap = frappe.db.get_single_value(_SETTINGS, _FIELD)

	def tearDown(self):
		frappe.db.set_single_value(_SETTINGS, _FIELD, self._snap or "classic", update_modified=False)
		frappe.db.commit()
		frappe.set_user("Administrator")

	def test_list_returns_all_templates_and_a_default(self):
		out = api.list_pdf_templates()
		self.assertTrue(out["ok"])
		self.assertEqual({t["key"] for t in out["data"]["templates"]}, _ALL)
		self.assertIn(out["data"]["default"], _ALL)

	def test_set_then_get_round_trips(self):
		api.set_default_pdf_template("editorial")
		self.assertEqual(api.get_default_pdf_template()["data"]["default"], "editorial")
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), "editorial")

	def test_set_normalizes_case_and_space(self):
		api.set_default_pdf_template("  MINIMAL ")
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), "minimal")

	def test_set_invalid_key_raises_and_leaves_store(self):
		api.set_default_pdf_template("branded")
		with self.assertRaises(InvalidArgumentError):
			api.set_default_pdf_template("nope")
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), "branded")

	def test_guest_cannot_set_default(self):
		api.set_default_pdf_template("classic")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.set_default_pdf_template("editorial")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), "classic")


# --------------------------------------------------------------------------- #
# Per-document override: rerender_document via the render-inputs sidecar.
# --------------------------------------------------------------------------- #


def _grant_jarvis_user(email: str) -> str:
	"""A System User holding the Jarvis User role - passes require_jarvis_access
	but is NOT the owner of another user's export (rolled back by FrappeTestCase)."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "PdfTpl Tester",
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
	return email


class TestRerenderDocument(FrappeTestCase):
	def setUp(self):
		# These persist REAL Files (the sidecar round-trip is the whole point), so
		# render_pdf must yield a PDF that passes Frappe 16's pypdf JS-scan.
		self.render = patch(f"{_MODULE}.render_pdf", return_value=_valid_pdf()).start()
		self.addCleanup(patch.stopall)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _make_source(self, template: str = "editorial") -> str:
		return export_document(_MD, format="pdf", title="Src", template=template)["name"]

	def test_rerender_produces_new_doc_in_chosen_template(self):
		src = self._make_source("editorial")
		out = api.rerender_document(src, "minimal")
		self.assertTrue(out["ok"])
		self.assertNotEqual(out["data"]["name"], src)
		# The re-rendered doc carries its own sidecar recording the new template.
		self.assertEqual(load_render_sidecar(out["data"]["name"])["template"], "minimal")

	def test_rerender_unknown_template_raises(self):
		src = self._make_source()
		with self.assertRaises(InvalidArgumentError):
			api.rerender_document(src, "nope")

	def test_rerender_missing_source_raises(self):
		with self.assertRaises(InvalidArgumentError):
			api.rerender_document("does-not-exist", "classic")

	def test_rerender_foreign_file_refused(self):
		src = self._make_source()  # owned by Administrator
		frappe.set_user(_grant_jarvis_user("pdftpl_other@example.com"))
		with self.assertRaises(frappe.PermissionError):
			api.rerender_document(src, "minimal")
