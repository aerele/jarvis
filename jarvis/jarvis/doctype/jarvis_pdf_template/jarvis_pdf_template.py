# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.tools._export.document import db_templates, font_catalog
from jarvis.tools._export.document import templates as _predef
from jarvis.tools._export.document.css_guard import CssValueError, validate_hex_color

# Lowercase slug: letters/digits in hyphen-separated groups. Keeps template_key a
# safe url/css/select token and predictable as the doctype name (autoname=field).
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_SETTINGS = "Jarvis Settings"
_DEFAULT_FIELD = "default_pdf_template"


class JarvisPDFTemplate(Document):
	def validate(self) -> None:
		self._validate_key()
		self._validate_style()
		self._validate_page()
		self._validate_letterheads()

	def _validate_page(self) -> None:
		if self.margins_mm in (None, ""):
			self.margins_mm = 15
		elif not (8 <= int(self.margins_mm) <= 40):
			frappe.throw(_("Margins must be between 8 and 40 mm."))

	def _validate_key(self) -> None:
		key = (self.template_key or "").strip().lower()
		if not key:
			frappe.throw(_("Template Key is required."))
		if not _SLUG.match(key):
			frappe.throw(
				_("Template Key must be a lowercase slug (letters, digits, hyphens), e.g. acme-invoice.")
			)
		# A custom template must never shadow a built-in preset (the merged resolver
		# also lets predefined win, this stops the collision at the source).
		if key in _predef.TEMPLATES:
			frappe.throw(_("'{0}' is a built-in template key; choose a different key.").format(key))
		self.template_key = key

	def _validate_style(self) -> None:
		# Attacker-controlled colours flow raw into the stylesheet; reject anything
		# that is not a bare hex colour (see css_guard). Fonts are font-catalog keys —
		# validate membership so only a known bundled/default font can be referenced.
		try:
			self.accent_color = validate_hex_color(self.accent_color, field=_("Accent Color"))
			self.dark_color = validate_hex_color(self.dark_color, field=_("Dark Color"))
		except CssValueError as exc:
			frappe.throw(str(exc))
		self.body_font = self._validate_font(self.body_font, _("Body Font"))
		self.display_font = self._validate_font(self.display_font, _("Display Font"))

	@staticmethod
	def _validate_font(value: str | None, field: str) -> str:
		key = (value or "").strip() or font_catalog.DEFAULT_KEY
		if not font_catalog.is_valid(key):
			frappe.throw(_("{0} must be one of the built-in fonts.").format(field))
		return key

	def _validate_letterheads(self) -> None:
		if not self.use_letterhead_footer:
			return
		seen: set[str] = set()
		for row in self.company_letter_heads or []:
			if not row.company or not row.letter_head:
				frappe.throw(_("Each footer mapping needs both a Company and a Letter Head."))
			if row.company in seen:
				frappe.throw(_("Company {0} is mapped more than once.").format(row.company))
			seen.add(row.company)

	def on_update(self) -> None:
		db_templates.clear_cache()

	def on_trash(self) -> None:
		db_templates.clear_cache()
		# If this template was the workspace default, fall back to classic so the
		# default never dangles at a now-deleted key.
		default = None
		try:
			default = frappe.db.get_single_value(_SETTINGS, _DEFAULT_FIELD)
		except Exception:
			default = None
		if default and default == self.template_key:
			frappe.db.set_single_value(
				_SETTINGS, _DEFAULT_FIELD, _predef.DEFAULT_TEMPLATE, update_modified=False
			)
