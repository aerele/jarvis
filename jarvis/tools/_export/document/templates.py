"""Predefined PDF templates for the rich-PDF document engine.

A template is a named preset with two parts:

  * ``css``       - a PARTIAL override of ``theme._CLASSIC_CSS_TOKENS`` (palette +
                    font roles). Anything omitted falls back to classic, so a
                    template only states what it changes. ``component_css`` does
                    the merge (see theme.py). One extra, non-token key is honoured:
                    ``font_scale`` (0.85..1.2), an opt-in type scale appended as an
                    override block AFTER the base sheet, so a template that omits
                    it (classic) renders the base sheet byte-for-byte.
  * ``page`` / ``placement`` - geometry + furniture choices consumed by
                    ``export_document`` and ``furniture.py`` (masthead alignment,
                    cover, logo, accent bar, watermark, footer, page numbers).

Pure module - no ``import frappe`` - so the set is unit-testable without a bench,
matching theme.py / sanitizer.py. This is v1's whole "template" model; the tokens
here are the same shape the dashboard themes already ship. Adding a template is a
new entry below; the classic entry is the engine's existing look verbatim (its
``css`` override is empty), so it must never diverge from the default.
"""

from __future__ import annotations

from .theme import FONT_STACKS

_SANS = FONT_STACKS["sans"]
_SERIF = FONT_STACKS["serif"]

# Default page geometry + furniture, per template. ``cover`` is auto|on|off;
# ``watermark`` is a default text ("" = none) an explicit export arg can override.
_DEFAULT_PLACEMENT = {
	"masthead_align": "left",
	"cover": "auto",
	"logo": True,
	"accent_bar": False,
	"watermark": "",
	"footer": True,
	"page_numbers": True,
}
_DEFAULT_PAGE = {"size": "A4", "orientation": "portrait", "margins_mm": 15}


def _tpl(key, label, description, *, css=None, page=None, placement=None):
	return {
		"key": key,
		"label": label,
		"description": description,
		"css": css or {},
		"page": {**_DEFAULT_PAGE, **(page or {})},
		"placement": {**_DEFAULT_PLACEMENT, **(placement or {})},
	}


TEMPLATES: dict[str, dict] = {
	"classic": _tpl(
		"classic",
		"Classic",
		"Navy serif masthead, clean tables, understated. The default.",
		# Empty css override == the engine's current look, verbatim.
		css={},
	),
	# Each preset below restates the WHOLE supporting palette (ink, muted, hairline,
	# tints, chart series), not just primary/dark, so the four looks read as
	# different documents rather than the classic sheet in a different accent.
	# ``font_scale`` is the opt-in type scale (theme._font_scale_css); classic never
	# sets it, which is what keeps its stylesheet byte-identical.
	"editorial": _tpl(
		"editorial",
		"Editorial",
		"Claret and parchment, all serif, generous type, opening on a full cover page. For reports and long reads.",
		css={
			"font_text": _SERIF,
			"font_display": _SERIF,
			"font_scale": 1.05,
			"primary": "#7a3b3b",
			"dark": "#3a1f1f",
			"ink": "#2b211b",
			"muted": "#7c6a5c",
			"line": "#e3d8cc",
			"tint": "#f5ede3",
			"zebra": "#faf5ee",
			"callout_bg": "#f8f0e6",
			"s1": "#7a3b3b",
			"s2": "#b8862b",
			"s3": "#4f6b52",
			"s4": "#5a4a6b",
		},
		placement={"masthead_align": "center", "cover": "on"},
	),
	"minimal": _tpl(
		"minimal",
		"Minimal",
		"Cool slate on white, all sans, compact type. No logo, no cover page, hairlines only.",
		css={
			"font_text": _SANS,
			"font_display": _SANS,
			"font_scale": 0.95,
			"primary": "#3f4b5a",
			"dark": "#1e242c",
			"ink": "#22272e",
			"muted": "#7a838f",
			"line": "#e4e7eb",
			"tint": "#f2f4f6",
			"zebra": "#f8f9fa",
			"callout_bg": "#f3f5f7",
			"s1": "#3f4b5a",
			"s2": "#7b8794",
			"s3": "#b0b8c2",
			"s4": "#1e242c",
		},
		placement={"logo": False, "cover": "off"},
	),
	"branded": _tpl(
		"branded",
		"Branded",
		"Brand green with a bold accent bar, mint table tints and a logo lockup. All sans and confident.",
		css={
			"font_text": _SANS,
			"font_display": _SANS,
			"primary": "#2e7d6b",
			"dark": "#0f3d34",
			"ink": "#1b2622",
			"muted": "#5f6f6a",
			"line": "#d3e0db",
			"tint": "#e4f0ec",
			"zebra": "#f2f8f6",
			"callout_bg": "#e8f3ef",
			"s1": "#2e7d6b",
			"s2": "#c9932a",
			"s3": "#3f6f9f",
			"s4": "#8a5a83",
		},
		placement={"accent_bar": True},
	),
	"formal": _tpl(
		"formal",
		"Formal",
		"Black serif on white, wider margins, heavier rules and a repeating CONFIDENTIAL watermark. For contracts and notices.",
		css={
			"font_text": _SERIF,
			"font_display": _SERIF,
			"primary": "#2c2c2c",
			"dark": "#141414",
			"ink": "#161616",
			"muted": "#585858",
			"line": "#b9b9b9",
			"tint": "#ebebeb",
			"zebra": "#f4f4f4",
			"callout_bg": "#efefef",
			"s1": "#141414",
			"s2": "#5a5a5a",
			"s3": "#9c9c9c",
			"s4": "#cfcfcf",
		},
		page={"margins_mm": 20},
		placement={"watermark": "CONFIDENTIAL"},
	),
}

DEFAULT_TEMPLATE = "classic"
KEYS: tuple[str, ...] = tuple(TEMPLATES.keys())


def _norm(key: str | None) -> str:
	return (key or "").strip().lower()


def is_valid(key: str | None) -> bool:
	"""True if ``key`` names a shipped template (case/space-insensitive)."""
	return _norm(key) in TEMPLATES


def resolve(key: str | None) -> dict:
	"""Return the template for ``key``, or the classic default for an unknown/blank
	key. Never raises - a bad key degrades to classic (the caller can note it)."""
	return TEMPLATES.get(_norm(key), TEMPLATES[DEFAULT_TEMPLATE])


def summaries() -> list[dict]:
	"""Compact list for the settings pane: key, label, description, and a small
	spec (accent, fonts, masthead, cover) - no rendering, no ``frappe``."""
	out = []
	for t in TEMPLATES.values():
		css = t["css"]
		out.append(
			{
				"key": t["key"],
				"label": t["label"],
				"description": t["description"],
				"accent": css.get("primary", "#1f4e79"),
				"body_font": "serif" if css.get("font_text") == _SERIF else "sans",
				"display_font": "sans" if css.get("font_display") == _SANS else "serif",
				"masthead": t["placement"]["masthead_align"],
				"cover": t["placement"]["cover"],
			}
		)
	return out
