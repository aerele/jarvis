"""Tests for the bundled font catalog behind custom PDF templates.

A catalog font (a curated, bundled SIL OFL font) must render in BOTH the settings-
pane HTML preview (Chrome, via base64 ``@font-face``) and the real PDF (wkhtmltopdf,
via fontconfig by family name — a base64 ``@font-face`` blanks its page). These two
mechanisms are the crux; the tests assert each path uses the right one.

  * ``font_catalog`` (PURE) — keys/validity, the font stack, ``font_options``,
    ``font_face_css`` (preview @font-face) and ``staged_fontconfig`` (PDF) + cleanup.
  * The merged resolver — a template's font stack carries the bundled family + fallback
    and records the bundled keys; a system default records nothing.
  * The ``Jarvis PDF Template`` controller — accepts catalog keys, rejects unknowns.
  * ``export_document`` — preview HTML has ``@font-face``; the PDF path stages a
    fontconfig file and passes it to ``render_pdf`` (and NEVER emits ``@font-face``).
  * The whitelisted options endpoint carries the catalog; the admin gate holds.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import pdf_templates as api
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.document import db_templates, font_catalog
from jarvis.tools._export.document.theme import FONT_STACKS
from jarvis.tools.export_document import export_document, preview_template_html

_MODULE = "jarvis.tools.export_document"
_TPL = db_templates.DT
_BUNDLED = "lora"  # a bundled catalog key (serif)


def _delete_pdf_template(key: str) -> None:
	if frappe.db.exists(_TPL, key):
		frappe.delete_doc(_TPL, key, force=True, ignore_permissions=True)
		frappe.db.commit()


class TestFontCatalog(FrappeTestCase):
	def test_defaults_and_bundled_validity(self):
		self.assertTrue(font_catalog.is_valid("sans"))
		self.assertTrue(font_catalog.is_valid("serif"))
		self.assertTrue(font_catalog.is_valid(_BUNDLED))
		self.assertFalse(font_catalog.is_valid("nope"))
		self.assertFalse(font_catalog.is_valid(None))

	def test_is_bundled(self):
		self.assertTrue(font_catalog.is_bundled(_BUNDLED))
		self.assertFalse(font_catalog.is_bundled("sans"))
		self.assertFalse(font_catalog.is_bundled("nope"))

	def test_stack_for(self):
		# a bundled font: family first, then the category fallback stack
		self.assertTrue(font_catalog.stack_for("lora").startswith('"Lora", '))
		self.assertIn("serif", font_catalog.stack_for("lora"))
		self.assertTrue(font_catalog.stack_for("inter").startswith('"Inter", '))
		# a default: the plain system stack (no bundled family prepended)
		self.assertEqual(font_catalog.stack_for("sans"), FONT_STACKS["sans"])
		self.assertEqual(font_catalog.stack_for("serif"), FONT_STACKS["serif"])
		# a bundled font's fallback is its category's system stack
		self.assertTrue(font_catalog.stack_for("lora").endswith(FONT_STACKS["serif"]))
		# unknown → None (caller falls back to classic)
		self.assertIsNone(font_catalog.stack_for("nope"))

	def test_family_for(self):
		self.assertEqual(font_catalog.family_for("lora"), "Lora")
		self.assertEqual(font_catalog.family_for("sans"), "")
		self.assertEqual(font_catalog.family_for("nope"), "")

	def test_font_options_shape(self):
		opts = font_catalog.font_options()
		keys = {o["key"] for o in opts}
		self.assertIn("sans", keys)
		self.assertIn("lora", keys)
		for o in opts:
			self.assertEqual(set(o), {"key", "label", "category"})

	def test_bundled_files_present(self):
		# every bundled key ships a regular + bold face
		for o in font_catalog.font_options():
			if font_catalog.is_bundled(o["key"]):
				faces = font_catalog._faces(o["key"])
				weights = {w for _f, w, _p in faces}
				self.assertEqual(weights, {"regular", "bold"}, o["key"])
				for _f, _w, p in faces:
					self.assertTrue(os.path.isfile(p))

	def test_font_face_css_regular_and_bold(self):
		css, notes = font_catalog.font_face_css([_BUNDLED])
		self.assertEqual(css.count("@font-face"), 2)  # regular + bold
		self.assertIn('font-family:"Lora"', css)
		self.assertIn("font-weight:400", css)
		self.assertIn("font-weight:700", css)
		self.assertIn("base64,", css)
		self.assertEqual(notes, [])

	def test_font_face_css_skips_defaults(self):
		css, notes = font_catalog.font_face_css(["sans", "serif"])
		self.assertEqual(css, "")
		self.assertEqual(notes, [])

	def test_font_face_css_missing_bold_notes(self):
		# A bundled family present but missing its bold face: the regular face still
		# renders, and the missing weight is surfaced as a note (not silently faux-bold).
		reg = font_catalog._faces(_BUNDLED)[:1]  # regular only
		with patch.object(font_catalog, "_faces", return_value=reg):
			css, notes = font_catalog.font_face_css([_BUNDLED])
		self.assertEqual(css.count("@font-face"), 1)
		self.assertTrue(any("bold" in n for n in notes), notes)

	def test_staged_fontconfig_copy_error_degrades(self):
		# A face that vanishes after the existence check must be skipped with a note,
		# never abort the export (edge case 14).
		with patch.object(font_catalog.shutil, "copyfile", side_effect=OSError("boom")):
			with font_catalog.staged_fontconfig([_BUNDLED]) as (conf, notes):
				self.assertIsNone(conf)  # nothing staged → fall back, don't crash
		self.assertTrue(notes)

	def test_staged_fontconfig_creates_and_cleans_up(self):
		with font_catalog.staged_fontconfig([_BUNDLED]) as (conf, notes):
			self.assertIsNotNone(conf)
			self.assertTrue(os.path.exists(conf))
			with open(conf, encoding="utf-8") as fh:
				body = fh.read()
			self.assertIn("<fontconfig>", body)
			self.assertIn("/etc/fonts/fonts.conf", body)  # inherits system config
			staged_dir = os.path.join(os.path.dirname(conf), "fonts")
			self.assertEqual(len(os.listdir(staged_dir)), 2)  # regular + bold copied
			saved = conf
		self.assertFalse(os.path.exists(saved))  # temp tree removed on exit

	def test_staged_fontconfig_none_for_defaults_only(self):
		with font_catalog.staged_fontconfig(["sans", "serif"]) as (conf, notes):
			self.assertIsNone(conf)
		with font_catalog.staged_fontconfig([]) as (conf, notes):
			self.assertIsNone(conf)


class TestResolverFonts(FrappeTestCase):
	def _tpl(self, **fields):
		d = {"template_key": "fc-res", "body_font": "sans", "display_font": "sans", **fields}
		return db_templates._doc_to_template(d)

	def test_bundled_font_stack_and_keys(self):
		t = self._tpl(body_font="lora", display_font="inter")
		self.assertTrue(t["css"]["font_text"].startswith('"Lora", '))
		self.assertTrue(t["css"]["font_display"].startswith('"Inter", '))
		self.assertEqual(set(t["font_keys"]), {"lora", "inter"})
		self.assertEqual(t["body_font"], "lora")
		self.assertEqual(t["display_font"], "inter")

	def test_same_bundled_font_deduped(self):
		t = self._tpl(body_font="lora", display_font="lora")
		self.assertEqual(t["font_keys"], ["lora"])

	def test_default_records_no_keys(self):
		t = self._tpl(body_font="serif", display_font="sans")
		self.assertEqual(t["font_keys"], [])
		self.assertIn("serif", t["css"]["font_text"])

	def test_unknown_key_leaves_token_unset(self):
		t = self._tpl(body_font="bogus")
		self.assertNotIn("font_text", t["css"])  # falls back to classic
		self.assertEqual(t["font_keys"], [])


class TestControllerFonts(FrappeTestCase):
	KEY = "fc-ctl"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)
		self.addCleanup(lambda: _delete_pdf_template(self.KEY))

	def test_accepts_catalog_keys(self):
		doc = frappe.get_doc(
			{
				"doctype": _TPL,
				"template_key": self.KEY,
				"label": "X",
				"body_font": "lora",
				"display_font": "inter",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.body_font, "lora")
		self.assertEqual(doc.display_font, "inter")

	def test_rejects_unknown_font(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": _TPL, "template_key": self.KEY, "label": "X", "body_font": "comic-sans"}
			).insert(ignore_permissions=True)


class TestExportCatalogFont(FrappeTestCase):
	KEY = "fc-export"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)
		self.addCleanup(lambda: _delete_pdf_template(self.KEY))
		frappe.get_doc(
			{
				"doctype": _TPL,
				"template_key": self.KEY,
				"label": "FC Export",
				"enabled": 1,
				"body_font": "lora",
				"display_font": "inter",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		db_templates.clear_cache()

	def test_preview_html_has_font_face(self):
		html = preview_template_html(db_templates.resolve(self.KEY))
		self.assertIn("@font-face", html)
		self.assertIn("Lora", html)
		self.assertIn("Inter", html)

	def test_html_export_has_font_face(self):
		with patch(f"{_MODULE}.save_export_file", side_effect=self._fake_save):
			export_document("# Hi\n\nbody text", format="html", template=self.KEY)
		self.assertIn("@font-face", self._saved_payload.decode())

	def test_pdf_path_stages_fontconfig_no_font_face(self):
		captured = {}

		def _fake_render(body_html, **kwargs):
			captured["fc"] = kwargs.get("font_config_file")
			captured["existed"] = bool(kwargs.get("font_config_file")) and os.path.exists(
				kwargs["font_config_file"]
			)
			captured["has_font_face"] = "@font-face" in body_html
			return b"%PDF-1.4 fake"

		with (
			patch(f"{_MODULE}.render_pdf", side_effect=_fake_render),
			patch(f"{_MODULE}.save_export_file", side_effect=self._fake_save),
			patch(f"{_MODULE}.save_render_sidecar"),
		):
			export_document("# Hi\n\nbody text", format="pdf", template=self.KEY)
		self.assertIsNotNone(captured["fc"])
		self.assertTrue(captured["existed"])  # the staged conf existed during the render
		self.assertFalse(captured["has_font_face"])  # PDF CSS uses the family stack only

	_saved_payload = b""

	def _fake_save(self, filename, payload, title, mime):
		self._saved_payload = payload
		return {
			"file_url": "/private/files/x",
			"filename": filename,
			"title": title,
			"mime_type": mime,
			"size_bytes": len(payload),
			"name": "FAKE",
		}


class TestOptionsAndApi(FrappeTestCase):
	KEY = "fc-api"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)
		frappe.set_user("Administrator")
		self.addCleanup(lambda: frappe.set_user("Administrator"))
		self.addCleanup(lambda: _delete_pdf_template(self.KEY))

	def test_options_carries_font_catalog(self):
		out = api.pdf_template_options()
		keys = {f["key"] for f in out["data"]["fonts"]}
		self.assertIn("sans", keys)
		self.assertIn("lora", keys)

	def test_save_get_round_trips_font_keys(self):
		api.save_pdf_template(
			json.dumps({"template_key": self.KEY, "label": "X", "body_font": "lora", "display_font": "inter"})
		)
		got = api.get_pdf_template(self.KEY)
		self.assertEqual(got["data"]["body_font"], "lora")
		self.assertEqual(got["data"]["display_font"], "inter")

	def test_preview_draft_rejects_unknown_font(self):
		with self.assertRaises(InvalidArgumentError):
			api.preview_pdf_template(json.dumps({"draft": {"body_font": "comic-sans"}}))

	def test_no_brand_font_endpoints(self):
		# the upload-era API is gone
		self.assertFalse(hasattr(api, "save_brand_font"))
		self.assertFalse(hasattr(api, "list_brand_fonts"))
		self.assertFalse(hasattr(api, "delete_brand_font"))

	def test_non_admin_denied_on_options(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.pdf_template_options()
