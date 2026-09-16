"""Tests for the Custom PDF Templates feature: DB-backed templates that extend the
5 predefined presets tested in ``test_pdf_templates.py``.

Mirrors that file's style and mocking conventions (``FrappeTestCase``, ``render_pdf``
mocked where the PDF path is exercised, the ``_MODULE`` string-patch pattern). Six
areas, one class each:

  * ``db_templates`` - the merged predefined+custom resolver (``resolve/is_valid/
    keys/summaries``), including the "predefined always wins a collision" defense.
  * ``css_guard`` - the pure hex-colour/font-role validators guarding every
    user-authored style value before it reaches a stylesheet.
  * The ``Jarvis PDF Template`` doctype controller's ``validate()`` - slug/collision/
    css/footer-mapping rejections.
  * ``furniture.resolve_company_letterhead_footer`` - the per-company pinned Letter
    Head footer resolution (mapped/unmapped/disabled/missing), image-neutralised.
  * ``export_document``'s HTML path - the ``jv-lh-footer`` band, on/off.
  * The whitelisted CRUD API in ``jarvis.pdf_templates`` - save/get/list/set-default/
    delete/preview/options, and the admin-only gate.

No real File is ever persisted for a PDF here (``save_export_file`` is mocked where
touched), so none of this needs wkhtmltopdf or Frappe 16's pypdf content scan.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import pdf_templates as api
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.document import css_guard, db_templates
from jarvis.tools._export.document import templates as _predef
from jarvis.tools._export.document.furniture import resolve_company_letterhead_footer
from jarvis.tools.export_document import export_document

_MODULE = "jarvis.tools.export_document"
_DT = db_templates.DT
_SETTINGS = "Jarvis Settings"
_FIELD = "default_pdf_template"

_FIXTURE_COMPANY = "_JPT PDF Template Co"
_FIXTURE_COMPANY_ABBR = "JPTPDF"


# --------------------------------------------------------------------------- #
# Shared fixture helpers. Every fixture is idempotent (deletes a same-named
# leftover from a crashed earlier run before creating) and every mutation the
# real API/controller would COMMIT is committed here too, so cleanup must be
# explicit (FrappeTestCase's per-test rollback does not undo a commit).
# --------------------------------------------------------------------------- #


def _delete_pdf_template(key: str) -> None:
	if frappe.db.exists(_DT, key):
		frappe.delete_doc(_DT, key, force=True, ignore_permissions=True)
		frappe.db.commit()


def _delete_letter_head(name: str) -> None:
	if frappe.db.exists("Letter Head", name):
		frappe.delete_doc("Letter Head", name, force=True, ignore_permissions=True)
		frappe.db.commit()


def _make_custom_template(
	key: str, *, company_letter_heads: list[dict] | None = None, **fields
) -> "frappe.model.document.Document":
	_delete_pdf_template(key)
	fields.setdefault("label", key.replace("-", " ").title())
	payload = {"doctype": _DT, "template_key": key, **fields}
	if company_letter_heads is not None:
		payload["company_letter_heads"] = company_letter_heads
	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _make_letter_head(name: str, *, footer: str = "", disabled: int = 0) -> str:
	_delete_letter_head(name)
	frappe.get_doc(
		{
			"doctype": "Letter Head",
			"letter_head_name": name,
			"source": "HTML",
			"footer_source": "HTML",
			"footer": footer,
			"disabled": disabled,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return name


def _resolve_test_company() -> str:
	"""Prefer an existing Company on the site (cheap, no chart-of-accounts risk);
	create one, idempotently and without a chart of accounts, only if none exists
	at all. Mirrors ``test_company_scope.py``'s ``_ensure_company``."""
	existing = frappe.get_all("Company", limit=1, pluck="name")
	if existing:
		return existing[0]
	if not frappe.db.exists("Company", _FIXTURE_COMPANY):
		frappe.local.flags.ignore_chart_of_accounts = True
		try:
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": _FIXTURE_COMPANY,
					"abbr": _FIXTURE_COMPANY_ABBR,
					"default_currency": "INR",
					"country": "India",
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.local.flags.ignore_chart_of_accounts = False
	return _FIXTURE_COMPANY


# --------------------------------------------------------------------------- #
# 1. db_templates - the merged predefined+custom resolver.
# --------------------------------------------------------------------------- #


class TestDbTemplatesResolver(FrappeTestCase):
	KEY = "jpt-resolver-custom"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)

	def tearDown(self):
		_delete_pdf_template(self.KEY)
		_delete_pdf_template("classic")  # only present if the collision test below ran

	def test_resolver_includes_created_custom_template(self):
		_make_custom_template(self.KEY, label="Acme Custom", accent_color="#123456", body_font="serif")

		self.assertTrue(db_templates.is_valid(self.KEY))
		self.assertIn(self.KEY, db_templates.keys())

		by_key = {d["key"]: d for d in db_templates.summaries()}
		self.assertIn(self.KEY, by_key)
		self.assertTrue(by_key[self.KEY]["custom"])
		self.assertEqual(by_key[self.KEY]["accent"], "#123456")
		self.assertFalse(by_key["classic"]["custom"])

		resolved = db_templates.resolve(self.KEY)
		self.assertEqual(resolved["css"]["primary"], "#123456")
		self.assertTrue(resolved["custom"])

	def test_predefined_template_resolves_unchanged(self):
		# No custom template involved at all - the merge must not alter a shipped
		# preset's own resolve() output one bit.
		self.assertEqual(db_templates.resolve("editorial"), _predef.resolve("editorial"))
		self.assertNotIn("custom", db_templates.resolve("editorial"))

	def test_colliding_custom_key_ignored_predefined_wins(self):
		# The doctype controller refuses this at save time (see
		# TestJarvisPdfTemplateController.test_rejects_builtin_key_collision), so
		# bypass it here (ignore_validate) to prove the RESOLVER also has its own
		# defense-in-depth against a colliding row, exercised for real rather than
		# via a mock.
		doc = frappe.get_doc({"doctype": _DT, "template_key": "classic", "label": "Evil Classic"})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		db_templates.clear_cache()

		self.assertNotIn("classic", db_templates._db_templates())
		self.assertEqual(db_templates.resolve("classic")["label"], "Classic")
		self.assertEqual(db_templates.keys().count("classic"), 1)


# --------------------------------------------------------------------------- #
# 2. css_guard - pure hex-colour / font-role validators.
# --------------------------------------------------------------------------- #


class TestCssGuard(FrappeTestCase):
	def test_valid_hex_colors_and_blank_pass_through(self):
		self.assertEqual(css_guard.validate_hex_color("#2e7d6b", field="Accent Color"), "#2e7d6b")
		self.assertEqual(css_guard.validate_hex_color("#fff", field="Accent Color"), "#fff")
		self.assertEqual(css_guard.validate_hex_color("", field="Accent Color"), "")
		self.assertEqual(css_guard.validate_hex_color(None, field="Accent Color"), "")

	def test_invalid_hex_colors_raise(self):
		for bad in ("#fff;} body{background:url(http://x)}", "red", "expression(1)", "#12"):
			with self.assertRaises(css_guard.CssValueError):
				css_guard.validate_hex_color(bad, field="Accent Color")

	def test_valid_font_roles_and_blank_pass_through(self):
		self.assertEqual(css_guard.validate_font_role("sans", field="Body Font"), "sans")
		self.assertEqual(css_guard.validate_font_role("serif", field="Body Font"), "serif")
		self.assertEqual(css_guard.validate_font_role("", field="Body Font"), "")

	def test_invalid_font_role_raises(self):
		with self.assertRaises(css_guard.CssValueError):
			css_guard.validate_font_role("comic", field="Body Font")


# --------------------------------------------------------------------------- #
# 3. Jarvis PDF Template controller - validate() rejections.
# --------------------------------------------------------------------------- #


class TestJarvisPdfTemplateController(FrappeTestCase):
	def test_rejects_non_slug_key(self):
		doc = frappe.get_doc({"doctype": _DT, "template_key": "Bad Key", "label": "Bad"})
		with self.assertRaisesRegex(frappe.ValidationError, "lowercase slug"):
			doc.insert(ignore_permissions=True)

	def test_rejects_builtin_key_collision(self):
		doc = frappe.get_doc({"doctype": _DT, "template_key": "classic", "label": "Evil"})
		with self.assertRaisesRegex(frappe.ValidationError, "built-in template key"):
			doc.insert(ignore_permissions=True)

	def test_rejects_bad_accent_color(self):
		doc = frappe.get_doc(
			{"doctype": _DT, "template_key": "jpt-ctrl-bad-color", "label": "Bad", "accent_color": "red"}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "hex colour"):
			doc.insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(_DT, "jpt-ctrl-bad-color"))

	def test_rejects_duplicate_company_mapping_row(self):
		company = _resolve_test_company()
		lh = _make_letter_head("_JPT Ctrl Dup LH")
		self.addCleanup(lambda: _delete_letter_head(lh))

		doc = frappe.get_doc(
			{
				"doctype": _DT,
				"template_key": "jpt-ctrl-dup-company",
				"label": "Dup",
				"use_letterhead_footer": 1,
				"company_letter_heads": [
					{"company": company, "letter_head": lh},
					{"company": company, "letter_head": lh},
				],
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "mapped more than once"):
			doc.insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(_DT, "jpt-ctrl-dup-company"))


# --------------------------------------------------------------------------- #
# 4. furniture.resolve_company_letterhead_footer - mapped / unmapped / disabled /
# missing. company_map is a plain dict and the Company lookup inside is best-
# effort (falls back to the raw footer text on failure), so none of this needs a
# real Company doctype record - only the Letter Head rows are real.
# --------------------------------------------------------------------------- #


class TestResolveCompanyLetterheadFooter(FrappeTestCase):
	FAKE_COMPANY = "_JPT Fake Co (does not exist)"

	def setUp(self):
		self.lh_ok = _make_letter_head(
			"_JPT Footer LH", footer='<p>JPT FOOTER TEXT</p><img src="http://evil.example/x.png">'
		)
		self.lh_disabled = _make_letter_head("_JPT Footer LH Disabled", footer="<p>Disabled</p>", disabled=1)
		self.addCleanup(lambda: _delete_letter_head(self.lh_ok))
		self.addCleanup(lambda: _delete_letter_head(self.lh_disabled))

	def test_mapped_company_renders_footer_and_drops_remote_img(self):
		footer_html, note = resolve_company_letterhead_footer(
			{self.FAKE_COMPANY: self.lh_ok}, self.FAKE_COMPANY
		)
		self.assertIn("JPT FOOTER TEXT", footer_html)
		self.assertNotIn("<img", footer_html)
		self.assertNotIn("evil.example", footer_html)
		self.assertIsNone(note)

	def test_unmapped_company_returns_empty_with_note(self):
		footer_html, note = resolve_company_letterhead_footer(
			{"Some Other Co": self.lh_ok}, self.FAKE_COMPANY
		)
		self.assertEqual(footer_html, "")
		self.assertTrue(note)

	def test_disabled_letter_head_returns_empty_with_note(self):
		footer_html, note = resolve_company_letterhead_footer(
			{self.FAKE_COMPANY: self.lh_disabled}, self.FAKE_COMPANY
		)
		self.assertEqual(footer_html, "")
		self.assertTrue(note)

	def test_missing_letter_head_returns_empty_with_note(self):
		footer_html, note = resolve_company_letterhead_footer(
			{self.FAKE_COMPANY: "_JPT Does Not Exist LH"}, self.FAKE_COMPANY
		)
		self.assertEqual(footer_html, "")
		self.assertTrue(note)


# --------------------------------------------------------------------------- #
# 5. export_document(format="html") - the jv-lh-footer band, on vs. off. No
# wkhtmltopdf on the HTML path, so only save_export_file is mocked (to inspect
# the composed body directly instead of reading a persisted File back).
# --------------------------------------------------------------------------- #


class TestExportDocumentLetterheadFooter(FrappeTestCase):
	KEY_ON = "jpt-export-lh-on"
	KEY_OFF = "jpt-export-lh-off"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)

		self.captured: dict = {}

		def _capture(filename, content, title, mime_type, dt=None, dn=None):
			self.captured["html"] = content
			return {
				"file_url": "/files/jpt-export-test.html",
				"filename": filename,
				"title": title,
				"mime_type": mime_type,
				"size_bytes": len(content),
				"name": "F-TEST",
			}

		patch(f"{_MODULE}.save_export_file", side_effect=_capture).start()
		self.addCleanup(patch.stopall)

		self.company = _resolve_test_company()
		self.lh = _make_letter_head("_JPT Export LH", footer="<p>JPT EXPORT FOOTER</p>")
		self.addCleanup(lambda: _delete_letter_head(self.lh))
		self.addCleanup(lambda: _delete_pdf_template(self.KEY_ON))
		self.addCleanup(lambda: _delete_pdf_template(self.KEY_OFF))

	def _html(self) -> str:
		return self.captured["html"].decode()

	def test_footer_band_rendered_when_letterhead_footer_enabled(self):
		_make_custom_template(
			self.KEY_ON,
			label="LH On",
			use_letterhead_footer=1,
			company_letter_heads=[{"company": self.company, "letter_head": self.lh}],
		)
		export_document("Report body.", format="html", template=self.KEY_ON, company=self.company)
		html = self._html()
		self.assertIn("jv-lh-footer", html)
		self.assertIn("JPT EXPORT FOOTER", html)

	def test_footer_band_absent_when_letterhead_footer_disabled(self):
		_make_custom_template(self.KEY_OFF, label="LH Off", use_letterhead_footer=0)
		export_document("Report body.", format="html", template=self.KEY_OFF, company=self.company)
		html = self._html()
		self.assertNotIn("jv-lh-footer", html)


# --------------------------------------------------------------------------- #
# 6. Whitelisted API round trip: save / get / list / set-default / delete /
# preview / options, plus the admin-only gate.
# --------------------------------------------------------------------------- #


class TestPdfTemplatesApiRoundTrip(FrappeTestCase):
	KEY = "jpt-roundtrip"

	def setUp(self):
		db_templates.clear_cache()
		self.addCleanup(db_templates.clear_cache)

		frappe.set_user("Administrator")
		self.addCleanup(lambda: frappe.set_user("Administrator"))

		self._default_snap = frappe.db.get_single_value(_SETTINGS, _FIELD)
		self.addCleanup(self._restore_default)
		self.addCleanup(lambda: _delete_pdf_template(self.KEY))

		self.company = _resolve_test_company()
		self.lh = _make_letter_head("_JPT API LH", footer="<p>JPT API FOOTER</p>")
		self.addCleanup(lambda: _delete_letter_head(self.lh))

	def _restore_default(self):
		frappe.db.set_single_value(_SETTINGS, _FIELD, self._default_snap or "classic", update_modified=False)
		frappe.db.commit()

	def _save(self, **overrides) -> dict:
		payload = {"template_key": self.KEY, "label": "Round Trip"}
		payload.update(overrides)
		return api.save_pdf_template(json.dumps(payload))

	def test_save_create_then_update_round_trips(self):
		out = self._save(description="v1", accent_color="#2e7d6b")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["key"], self.KEY)

		got = api.get_pdf_template(self.KEY)
		self.assertEqual(got["data"]["description"], "v1")
		self.assertEqual(got["data"]["accent_color"], "#2e7d6b")

		# Update the SAME key - not a second template.
		self._save(description="v2", accent_color="#123456")
		got2 = api.get_pdf_template(self.KEY)
		self.assertEqual(got2["data"]["description"], "v2")
		self.assertEqual(got2["data"]["accent_color"], "#123456")
		self.assertEqual(frappe.db.count(_DT, {"template_key": self.KEY}), 1)

	def test_list_includes_created_template(self):
		self._save()
		out = api.list_pdf_templates()
		self.assertIn(self.KEY, {t["key"] for t in out["data"]["templates"]})

	def test_set_default_to_custom_key_persists(self):
		self._save()
		api.set_default_pdf_template(self.KEY)
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), self.KEY)

	def test_delete_removes_template_and_resets_default_when_it_was_default(self):
		self._save()
		api.set_default_pdf_template(self.KEY)
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), self.KEY)

		api.delete_pdf_template(self.KEY)
		self.assertFalse(frappe.db.exists(_DT, self.KEY))
		self.assertEqual(frappe.db.get_single_value(_SETTINGS, _FIELD), "classic")

	def test_preview_saved_key_with_letterhead_footer(self):
		self._save(
			use_letterhead_footer=1,
			company_letter_heads=[{"company": self.company, "letter_head": self.lh}],
		)
		out = api.preview_pdf_template(json.dumps({"key": self.KEY, "company": self.company}))
		self.assertIn("jv-lh-footer", out["data"]["html"])
		self.assertIn("JPT API FOOTER", out["data"]["html"])

	def test_preview_invalid_draft_raises(self):
		with self.assertRaises(InvalidArgumentError):
			api.preview_pdf_template(json.dumps({"draft": {"accent_color": "red;}evil"}}))

	def test_pdf_template_options_returns_companies_and_letter_heads(self):
		disabled_lh = _make_letter_head("_JPT API LH Disabled", disabled=1)
		self.addCleanup(lambda: _delete_letter_head(disabled_lh))

		out = api.pdf_template_options()
		self.assertIn(self.company, out["data"]["companies"])
		self.assertIn(self.lh, out["data"]["letter_heads"])
		self.assertNotIn(disabled_lh, out["data"]["letter_heads"])

	def test_non_admin_denied_on_save_delete_preview(self):
		self._save()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.save_pdf_template(json.dumps({"template_key": "jpt-guest-x", "label": "x"}))
		with self.assertRaises(frappe.PermissionError):
			api.delete_pdf_template(self.KEY)
		with self.assertRaises(frappe.PermissionError):
			api.preview_pdf_template(json.dumps({"key": "classic"}))
