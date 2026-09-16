"""Merged PDF-template resolver: the 5 predefined presets PLUS DB-stored custom
templates (``Jarvis PDF Template``), exposed through the SAME surface the API
(``jarvis.pdf_templates``) and engine (``export_document._resolve_pdf_template``)
already consume: ``resolve / is_valid / summaries / keys``.

Why a separate module: ``templates.py`` is a PURE module (no ``import frappe``) so
the shipped set stays unit-testable without a bench, and its ``classic`` entry must
never diverge from the engine default. Custom templates live in the DB, so merging
them needs frappe — that merge belongs HERE, not in the pure module. Predefined
keys always win a collision (the doctype also rejects collisions at save time), so
a custom template can never shadow or mutate a shipped preset.

Hot-path safety: ``resolve``/``is_valid`` check the predefined set FIRST and only
touch the DB for a non-predefined key, so an ordinary export (a predefined key)
issues no query. The DB read is a per-request memo (custom templates change rarely
and are few).
"""

from __future__ import annotations

import frappe

from . import font_catalog
from . import templates as _predef
from .css_guard import CssValueError, validate_hex_color

DT = "Jarvis PDF Template"


def _safe_color(value) -> str:
	"""A validated hex colour, or "" for anything not a bare hex (dropped, never
	raised — the render path must survive a bad stored row)."""
	try:
		return validate_hex_color(value, field="color")
	except CssValueError:
		return ""


def _apply_font(css: dict, token: str, key, refs: list) -> str:
	"""Set ``css[token]`` (font stack) from a font-catalog ``key`` and return that
	key. A bundled font's family is prepended before its category fallback (so the
	preview @font-face and the PDF's fontconfig both resolve it, with a graceful
	fallback), and the key is recorded in ``refs`` for staging/embedding. A system
	default (``sans``/``serif``) sets the plain stack and records nothing. An unknown
	key leaves the token UNSET (so it falls back to classic, keeping partial templates
	partial) and returns ""."""
	stack = font_catalog.stack_for(key)
	if stack is None:
		return ""
	css[token] = stack
	if font_catalog.is_bundled(key):
		refs.append(key)
	return key


def _doc_to_template(d: dict) -> dict:
	"""Convert a ``Jarvis PDF Template`` row (as_dict) into the pure-module shape
	``{key,label,description,css,page,placement}`` plus custom-only footer metadata
	(``use_letterhead_footer``, ``company_letter_heads`` map). css is a PARTIAL
	override (only fields the admin set), matching the predefined presets.

	Values are trusted here only because the doctype controller + the css validator
	(``jarvis.tools._export.document.css_guard``) reject unsafe values on save; this
	function does not re-sanitize (that is the render-merge's job)."""
	css: dict = {}
	# Defense in depth: the controller validates on save, but a row created another
	# way (data import, fixture, direct frappe.db.set_value) would otherwise put a raw
	# value straight into a <style> block. Re-validate here at the render-merge; an
	# invalid colour is DROPPED (falls back to classic) rather than raising — a bad
	# stored row must never break every export, and must never reach the stylesheet.
	primary = _safe_color(d.get("accent_color"))
	if primary:
		css["primary"] = primary
	dark = _safe_color(d.get("dark_color"))
	if dark:
		css["dark"] = dark
	font_keys: list[str] = []
	body_font = _apply_font(css, "font_text", d.get("body_font"), font_keys)
	display_font = _apply_font(css, "font_display", d.get("display_font"), font_keys)

	placement = {
		"masthead_align": d.get("masthead_align") or "left",
		"cover": d.get("cover") or "auto",
		"logo": bool(d.get("show_logo", 1)),
		"accent_bar": bool(d.get("accent_bar", 0)),
		"watermark": d.get("watermark") or "",
		"footer": True,
		"page_numbers": True,
	}
	rows = d.get("company_letter_heads") or []
	return {
		"key": _predef._norm(d["template_key"]),
		"label": d.get("label") or d["template_key"],
		"description": d.get("description") or "",
		"css": css,
		"page": {
			"size": d.get("page_size") or "A4",
			"orientation": d.get("orientation") or "portrait",
			"margins_mm": int(d.get("margins_mm") or 15),
		},
		"placement": placement,
		"custom": True,
		# Catalog keys chosen for body/display; the bundled ones (deduped) drive the
		# preview @font-face + the PDF fontconfig staging (body + display may share a font).
		"font_keys": list(dict.fromkeys(font_keys)),
		"body_font": body_font or "sans",
		"display_font": display_font or "sans",
		"use_letterhead_footer": bool(d.get("use_letterhead_footer", 0)),
		"company_letter_heads": {
			r.get("company"): r.get("letter_head") for r in rows if r.get("company") and r.get("letter_head")
		},
	}


def _db_templates() -> dict[str, dict]:
	"""All ENABLED custom templates, keyed by normalized template_key. Memoized per
	request. Degrades to ``{}`` before the doctype exists (pre-migrate) so the engine
	and API keep working with predefined-only."""
	cache = getattr(frappe.local, "_jarvis_db_pdf_templates", None)
	if cache is not None:
		return cache
	out: dict[str, dict] = {}
	try:
		names = frappe.get_all(DT, filters={"enabled": 1}, pluck="name")
		for n in names:
			d = frappe.get_doc(DT, n).as_dict()
			key = _predef._norm(d.get("template_key") or "")
			# Predefined keys always win; a colliding custom row is ignored (the
			# controller forbids the collision at save, this is defense-in-depth).
			if key and key not in _predef.TEMPLATES:
				out[key] = _doc_to_template(d)
	except Exception:
		out = {}
	frappe.local._jarvis_db_pdf_templates = out
	return out


def clear_cache() -> None:
	"""Drop the per-request memo (call from the doctype's on_update/on_trash)."""
	if hasattr(frappe.local, "_jarvis_db_pdf_templates"):
		del frappe.local._jarvis_db_pdf_templates


def is_valid(key: str | None) -> bool:
	"""True if ``key`` names a predefined OR an enabled custom template."""
	if _predef.is_valid(key):
		return True
	return _predef._norm(key) in _db_templates()


def resolve(key: str | None) -> dict:
	"""Return the template for ``key`` (predefined or custom), or classic for an
	unknown/blank/disabled key. Never raises. Predefined checked first (no DB)."""
	k = _predef._norm(key)
	if k in _predef.TEMPLATES:
		return _predef.resolve(k)
	return _db_templates().get(k) or _predef.resolve(None)


def keys() -> tuple[str, ...]:
	"""Predefined keys followed by enabled custom keys."""
	return tuple(_predef.KEYS) + tuple(_db_templates().keys())


def _custom_summary(t: dict) -> dict:
	css = t["css"]
	# body_font / display_font are font-catalog keys (same shape the predefined
	# summaries emit); the SPA maps a key to its label + specimen.
	return {
		"key": t["key"],
		"label": t["label"],
		"description": t["description"],
		"accent": css.get("primary", "#1f4e79"),
		"body_font": t.get("body_font", "sans"),
		"display_font": t.get("display_font", "sans"),
		"masthead": t["placement"]["masthead_align"],
		"cover": t["placement"]["cover"],
		"custom": True,
	}


def summaries() -> list[dict]:
	"""Predefined summaries (``custom: False``) followed by the ENABLED custom
	templates, for the resolver-facing / non-admin read-only list."""
	out = [{**s, "custom": False, "enabled": True} for s in _predef.summaries()]
	out.extend({**_custom_summary(t), "enabled": True} for t in _db_templates().values())
	return out


def _all_db_templates() -> dict[str, dict]:
	"""ALL custom templates INCLUDING disabled, keyed by normalized key, each tagged
	with its real ``enabled`` flag. For the admin editor only (an admin must be able
	to find + re-enable a disabled template) — NOT the render hot path, so no memo."""
	out: dict[str, dict] = {}
	try:
		for name in frappe.get_all(DT, pluck="name"):
			d = frappe.get_doc(DT, name).as_dict()
			key = _predef._norm(d.get("template_key") or "")
			if key and key not in _predef.TEMPLATES:
				t = _doc_to_template(d)
				t["enabled"] = bool(d.get("enabled"))
				out[key] = t
	except Exception:
		out = {}
	return out


def admin_summaries() -> list[dict]:
	"""Predefined + ALL custom templates (including disabled, each tagged
	``enabled``), for the admin management pane so a disabled template stays
	visible + re-enablable."""
	out = [{**s, "custom": False, "enabled": True} for s in _predef.summaries()]
	for t in _all_db_templates().values():
		out.append({**_custom_summary(t), "enabled": t.get("enabled", True)})
	return out
