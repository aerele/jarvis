"""Whitelisted API for the predefined PDF templates feature.

Three endpoints back the SPA Settings -> PDF Templates pane (list the templates,
get/set the workspace default), and one user-driven endpoint re-renders an
already-generated document into a different template (the per-document override).

The template SET itself is a pure module (``jarvis.tools._export.document.templates``)
so it is unit-testable without a bench; this module is the frappe-facing seam:
permission gates + Jarvis Settings persistence + the re-render. The workspace
default lives in the ``Jarvis Settings`` single (field ``default_pdf_template``),
written only through the admin setter here.

House response shape: ``{"ok": True, "data": ...}``. Methods are type-annotated
because hooks.py sets require_type_annotated_api_methods.
"""

from __future__ import annotations

import contextlib

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_access, require_jarvis_admin
from jarvis.tools._export import load_render_sidecar
from jarvis.tools._export.document import db_templates as pdf_templates
from jarvis.tools.export_document import export_document

SETTINGS = "Jarvis Settings"
_DEFAULT_FIELD = "default_pdf_template"


@frappe.whitelist()
def list_pdf_templates() -> dict:
	"""The shipped templates (key, label, description + a small spec) for the pane,
	plus the current workspace default. Any Jarvis user may read (the pane is
	read-only for non-admins)."""
	require_jarvis_access()
	# Admins manage templates here, so they must see DISABLED custom templates too
	# (to re-enable them); non-admins get the read-only enabled-only list.
	templates = pdf_templates.admin_summaries() if has_jarvis_admin_access() else pdf_templates.summaries()
	return {"ok": True, "data": {"templates": templates, "default": _current_default()}}


@frappe.whitelist()
def get_default_pdf_template() -> dict:
	"""The workspace default template key. Any Jarvis user may read."""
	require_jarvis_access()
	return {"ok": True, "data": {"default": _current_default()}}


@frappe.whitelist()
def set_default_pdf_template(key: str) -> dict:
	"""Set the workspace default template. Tenant-admin only; the server re-checks
	(the SPA gate is only UX). Writes the Single field directly (no full doc.save,
	so it never trips the LLM-sync machinery in on_update, which can restart the
	container - same reasoning as update_branding)."""
	require_jarvis_admin()
	if not pdf_templates.is_valid(key):
		raise InvalidArgumentError(
			f"Unknown template '{key}'. Choose one of: {', '.join(pdf_templates.keys())}."
		)
	resolved = pdf_templates.resolve(key)["key"]
	frappe.db.set_single_value(SETTINGS, _DEFAULT_FIELD, resolved, update_modified=False)
	frappe.db.commit()
	return {"ok": True, "data": {"default": resolved}}


@frappe.whitelist()
def rerender_document(source_name: str, template: str) -> dict:
	"""Re-render an already-generated document in a different predefined template.

	User-driven (no agent, no LLM): loads the render inputs stored beside the source
	File at generation and replays ``export_document`` with the new template. The
	result is a fresh private File owned by the caller. Requires Jarvis access and
	ownership of the source File (its owner-only ACL)."""
	require_jarvis_access()
	if not pdf_templates.is_valid(template):
		raise InvalidArgumentError(
			f"Unknown template '{template}'. Choose one of: {', '.join(pdf_templates.keys())}."
		)
	_require_readable_file(source_name)
	payload = load_render_sidecar(source_name)
	if not payload:
		raise InvalidArgumentError(
			"This document can no longer be reformatted (its render data is unavailable)."
		)
	payload["template"] = pdf_templates.resolve(template)["key"]
	env = export_document(**payload)
	return {"ok": True, "data": env}


def _current_default() -> str:
	"""The stored workspace default, or ``classic`` when unset/invalid. Best-effort
	read: the field may not exist yet on a not-yet-migrated bench."""
	value = None
	with contextlib.suppress(Exception):
		value = frappe.db.get_single_value(SETTINGS, _DEFAULT_FIELD)
	return pdf_templates.resolve(value)["key"]


def _require_readable_file(source_name: str) -> None:
	"""Fail unless the source File exists and the caller may read it. For an
	unattached private export that is owner-only, so this enforces ownership."""
	if not source_name or not frappe.db.exists("File", source_name):
		raise InvalidArgumentError("The source document was not found.")
	if not frappe.has_permission("File", "read", doc=source_name):
		raise frappe.PermissionError("You do not have access to this document.")


# --- Custom template admin CRUD (tenant-admin only; the SPA gate is only UX) ------

CUSTOM_DT = "Jarvis PDF Template"

# Fields the SPA may set on a custom template. Validation (slug, css values,
# built-in-key collision) is enforced by the doctype controller on save.
_TEMPLATE_FIELDS = (
	"label",
	"description",
	"accent_color",
	"dark_color",
	"body_font",
	"display_font",
	"masthead_align",
	"cover",
	"watermark",
	"page_size",
	"orientation",
	"margins_mm",
)

# Check fields with their default when the payload omits them (a new template is
# enabled and shows the logo by default; accent bar / letterhead footer are opt-in).
_CHECK_DEFAULTS = {"enabled": True, "show_logo": True, "accent_bar": False, "use_letterhead_footer": False}


@frappe.whitelist()
def save_pdf_template(payload: str) -> dict:
	"""Create or update a custom PDF template from the settings pane. Tenant-admin
	only. Validation (safe slug, hex/font css values, no built-in-key collision,
	one letter head per company) runs in the doctype controller."""
	require_jarvis_admin()
	data = frappe.parse_json(payload) or {}
	key = (data.get("template_key") or "").strip().lower()
	if not key:
		raise InvalidArgumentError("Template Key is required.")

	values = {f: data.get(f) for f in _TEMPLATE_FIELDS}
	for c, dflt in _CHECK_DEFAULTS.items():
		values[c] = 1 if data.get(c, dflt) else 0

	if frappe.db.exists(CUSTOM_DT, key):
		doc = frappe.get_doc(CUSTOM_DT, key)
		doc.update(values)
	else:
		doc = frappe.get_doc({"doctype": CUSTOM_DT, "template_key": key, **values})

	doc.set("company_letter_heads", [])
	if values["use_letterhead_footer"]:
		for row in data.get("company_letter_heads") or []:
			if row.get("company") and row.get("letter_head"):
				doc.append(
					"company_letter_heads",
					{"company": row["company"], "letter_head": row["letter_head"]},
				)
	doc.save(ignore_permissions=True)  # require_jarvis_admin above is the ACL
	frappe.db.commit()
	return {"ok": True, "data": {"key": doc.template_key}}


@frappe.whitelist()
def delete_pdf_template(key: str) -> dict:
	"""Delete a custom PDF template. Tenant-admin only. The controller's on_trash
	resets the workspace default to classic if this was it."""
	require_jarvis_admin()
	if not key or not frappe.db.exists(CUSTOM_DT, key):
		raise InvalidArgumentError("That template does not exist.")
	frappe.delete_doc(CUSTOM_DT, key, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "data": {"deleted": key}}


@frappe.whitelist()
def get_pdf_template(key: str) -> dict:
	"""The editable fields of one custom template (for the edit form). Admin only."""
	require_jarvis_admin()
	if not key or not frappe.db.exists(CUSTOM_DT, key):
		raise InvalidArgumentError("That template does not exist.")
	doc = frappe.get_doc(CUSTOM_DT, key)
	out = {"template_key": doc.template_key}
	for f in _TEMPLATE_FIELDS:
		out[f] = doc.get(f)
	for c in _CHECK_DEFAULTS:
		out[c] = bool(doc.get(c))
	out["company_letter_heads"] = [
		{"company": r.company, "letter_head": r.letter_head} for r in doc.company_letter_heads
	]
	return {"ok": True, "data": out}


@frappe.whitelist()
def preview_pdf_template(config: str) -> dict:
	"""HTML preview for a saved key or an unsaved draft, for the sandboxed-iframe
	preview. Admin only. A draft's css values are validated first (so an invalid
	value surfaces as an error rather than rendering)."""
	require_jarvis_admin()
	from jarvis.tools.export_document import preview_template_html

	cfg = frappe.parse_json(config) or {}
	company = cfg.get("company")
	if cfg.get("key"):
		tpl = pdf_templates.resolve(cfg["key"])
	else:
		tpl = _draft_to_tpl(cfg.get("draft") or {})
	return {"ok": True, "data": {"html": preview_template_html(tpl, company)}}


@frappe.whitelist()
def pdf_template_options() -> dict:
	"""Companies + non-disabled Letter Heads for the footer-mapping dropdowns, and the
	built-in font catalog for the body/title font pickers. Admin only."""
	require_jarvis_admin()
	from jarvis.tools._export.document import font_catalog

	return {
		"ok": True,
		"data": {
			"companies": frappe.get_all("Company", fields=["name"], order_by="name", pluck="name"),
			"letter_heads": frappe.get_all(
				"Letter Head", filters={"disabled": 0}, order_by="letter_head_name", pluck="name"
			),
			"fonts": font_catalog.font_options(),
		},
	}


def _draft_to_tpl(draft: dict) -> dict:
	"""Turn an UNSAVED draft (raw pane fields) into the resolver's template shape for
	a live preview, applying the same validation the controller enforces on save.
	Raises ``InvalidArgumentError`` for an invalid css value or an unknown font."""
	from jarvis.tools._export.document import db_templates, font_catalog
	from jarvis.tools._export.document.css_guard import CssValueError, validate_hex_color

	d = dict(draft)
	d.setdefault("template_key", "preview")
	try:
		d["accent_color"] = validate_hex_color(d.get("accent_color"), field="Accent Color")
		d["dark_color"] = validate_hex_color(d.get("dark_color"), field="Dark Color")
	except CssValueError as exc:
		raise InvalidArgumentError(str(exc))
	for f in ("body_font", "display_font"):
		key = (d.get(f) or "").strip() or font_catalog.DEFAULT_KEY
		if not font_catalog.is_valid(key):
			raise InvalidArgumentError(f"{f.replace('_', ' ').title()} must be a built-in font.")
		d[f] = key
	return db_templates._doc_to_template(d)
