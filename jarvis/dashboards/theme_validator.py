"""Deterministic save-time validator for dashboard canvas HTML.

Pure + frappe-free (imports nothing from frappe, so it is unit-testable
standalone); the ``Jarvis Dashboard`` controller calls ``validate_dashboard_html``
on every save and throws a structured error when it returns violations. NO
auto-repair — a rejected save surfaces the reasons and the builder's chat loop
regenerates.

PARSE-BASED, not regex-over-raw-text. The browser tokenizes + decodes CSS and
HTML before it applies them, so a raw-spelling regex is trivially bypassed
(``\\6b``-escapes, ``&#58;`` entities, ``var()`` indirection, unbalanced braces).
This validator therefore parses the same way the browser does:

  * CSS is tokenized with **tinycss2** — which DECODES escapes (``o\\6b lch`` →
    ``oklch``, ``@f\\6f nt-face`` → ``@font-face``), SEPARATES the selector prelude
    from the declaration VALUES (so an id selector ``#abc`` is never read as a hex
    color), exposes ``!important`` structurally, and reports parse errors so a
    stray/misplaced ``}`` (which would break out of the ``@layer author {}``
    wrapper) is detectable.
  * HTML is parsed with **html5lib** (into an lxml tree) exactly like a browser —
    it DECODES entities (``https&#58;//…`` → ``https://…``), reads EVERY ``style=``
    attribute including UNQUOTED ones, reads an unclosed ``<style>`` to
    end-of-document (raw-text parsing), and surfaces url-bearing attribute values.

What it enforces (a standard theme) — the COLOR + FONT + light/dark contract:
  * off-palette color VALUES in CSS, in ANY syntax. The policy is an allow-list
    inversion: a color-valued token that is not (a) an allow-listed theme hex,
    (b) a ``var(--jd-*)`` token, or (c) an approved white/black/transparent
    neutral is off-palette — whether written as hex, ``rgb()``/``hsl()``, a modern
    color function (``oklch/oklab/lab/lch/hwb/color()/color-mix()/device-cmyk()``),
    a named CSS color, or a CSS SYSTEM color (``CanvasText``/``Highlight``/…). A
    ``var()`` in a color value is allowed ONLY when it references a ``--jd-*`` token
    that EXISTS in the selected theme AND (if it carries a fallback after the first
    comma) that fallback itself validates — so ``var(--jd-nope,#f00)`` and
    ``var(--jd-ink, red)`` are both off-palette (the browser would render the bad
    fallback), while ``var(--jd-ink)`` passes.
  * ``font-family`` (or ``font`` shorthand) naming a non-system face, INCLUDING
    ``font-family:var(--x)`` where ``--x`` is not an existing theme font token, or
    a ``var(--jd-font, …)`` whose fallback names a non-system face.
  * author redefinition of a ``--jd-*`` theme token (only the theme layer owns
    them).
  * ``@media (prefers-color-scheme)`` and the ``color-scheme`` property
    (light/dark is driven by the theme's ``data-theme`` ONLY).
  * ``!important`` ANYWHERE in author CSS (it escapes the theme layer; the theme
    owns its layer, the author never needs it).
  * structurally-malformed / brace-unbalanced author CSS (a stray/misplaced ``}``
    that would break out of the injected ``@layer author {}`` wrapper).
  * every rule above is applied to inline ``style=`` declarations too, not just
    ``<style>`` blocks.
Always enforced (a safety subset, even for the bespoke ``Custom`` theme):
  * ``@font-face`` (incl. escaped ``@f\\6f nt-face``).
  * external URLs (http/https + protocol-relative ``//``) in any attribute value
    (entity-decoded), CSS ``url()`` token, or ``@import`` — after browser URL
    canonicalization (ASCII TAB/LF/CR stripped, special-scheme backslashes folded)
    so a control-char-split ``https:/<TAB>/evil`` still resolves + is caught. Only
    the exact XML namespace URIs (``http://www.w3.org/2000/svg`` / ``…/1999/xlink``)
    are exempt.

Honest scope + limits: this validator enforces COLOR, font-family, no-``!important``,
no-``prefers-color-scheme``, well-formed CSS, and the safety bans. It does NOT
deterministically lint STRUCTURE (spacing / type scale / layout) — structural
adherence comes from the winning named component classes (emitted in ``@layer
theme``) plus the generation prompt, not a hard spacing rule. Color/font rules
scan CSS only — ``<style>`` blocks and inline ``style=`` attributes — NOT
``<script>`` bodies, so colors set in ECharts JS config are governed by the
skill's "use window.JARVIS_THEME.palette" instruction, not this linter. It is a
standard/cleanliness gate layered under the hard CSP+sandbox boundary (which is
what actually blocks network egress), not an exhaustive engine.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import html5lib
import tinycss2
from tinycss2 import ast

from jarvis.dashboards import theme_spec

# Current validator/schema version stamped onto docs that pass. Unstamped docs
# (0/None) are legacy and grandfathered (never re-validated on read).
THEME_SCHEMA_VERSION = 1


class Violation(NamedTuple):
	"""One machine-readable rejection. ``rule`` is a stable code; ``location``
	is where it was found; ``offending`` is the exact token; ``message`` is the
	human line surfaced in the save error."""

	rule: str
	location: str
	offending: str
	message: str

	def as_dict(self) -> dict:
		return {
			"rule": self.rule,
			"location": self.location,
			"offending": self.offending,
			"message": self.message,
		}


# Rule codes ------------------------------------------------------------------
RULE_OFF_PALETTE = "off-palette-color"
RULE_FONT_FAMILY = "font-family-override"
RULE_TOKEN_REDEFINE = "token-redefinition"
RULE_PREFERS_SCHEME = "prefers-color-scheme"
RULE_COLOR_SCHEME = "color-scheme"
RULE_IMPORTANT = "important-declaration"
RULE_INLINE_COLOR = "inline-style-color"
RULE_MALFORMED = "malformed-css"  # a stray } that would escape @layer author
RULE_FONT_FACE = "font-face"  # safety
RULE_EXTERNAL_URL = "external-url"  # safety

# Color-bearing CSS properties (shorthands included). A named/functional color in
# one of these declaration VALUES is inspected; a hex in any of them is caught too.
_COLOR_PROPS = frozenset(
	{
		"color",
		"background",
		"background-color",
		"background-image",
		"border",
		"border-color",
		"border-top",
		"border-right",
		"border-bottom",
		"border-left",
		"border-top-color",
		"border-right-color",
		"border-bottom-color",
		"border-left-color",
		"outline",
		"outline-color",
		"box-shadow",
		"text-shadow",
		"fill",
		"stroke",
		"caret-color",
		"column-rule",
		"column-rule-color",
		"text-decoration",
		"text-decoration-color",
		"accent-color",
		"-webkit-text-fill-color",
		"-webkit-text-stroke-color",
		"-webkit-text-stroke",
		"-webkit-tap-highlight-color",
		"border-inline-color",
		"border-block-color",
		"border-inline-start-color",
		"border-inline-end-color",
		"border-block-start-color",
		"border-block-end-color",
		"scrollbar-color",
		"text-emphasis-color",
		"text-emphasis",
		"stop-color",
		"flood-color",
		"lighting-color",
	}
)
_FONT_PROPS = frozenset({"font-family", "font"})

# Approved neutral hex values (always allowed regardless of theme).
_APPROVED_HEXES = frozenset({"#ffffff", "#000000"})
# Approved color keywords (values, not families).
_APPROVED_COLOR_WORDS = frozenset(
	{
		"transparent",
		"currentcolor",
		"inherit",
		"initial",
		"unset",
		"revert",
		"white",
		"black",
		"none",
	}
)
# Generic font-family keywords (always allowed as families).
_GENERIC_FAMILIES = frozenset(
	{
		"serif",
		"sans-serif",
		"monospace",
		"cursive",
		"fantasy",
		"system-ui",
		"ui-sans-serif",
		"ui-serif",
		"ui-monospace",
		"ui-rounded",
		"math",
		"emoji",
		"fangsong",
		"inherit",
		"initial",
		"unset",
		"revert",
	}
)
# Modern color functions — never allow-listable, so any occurrence is off-palette.
_MODERN_COLOR_FNS = frozenset(
	{
		"oklch",
		"oklab",
		"lab",
		"lch",
		"hwb",
		"color-mix",
		"device-cmyk",
		"color",
	}
)
# Exact XML namespace URIs that are identifiers, never fetched — the ONLY http(s)
# values exempt from the external-URL safety ban (narrowed from "the w3.org host").
_ALLOWED_XML_NS = frozenset(
	{
		"http://www.w3.org/2000/svg",
		"http://www.w3.org/1999/xlink",
	}
)

# Full CSS named-color set (minus the approved neutrals above), used to flag a
# brand-y named color sitting in a color-bearing declaration value.
_CSS_NAMED_COLORS = frozenset(
	{
		"aliceblue",
		"antiquewhite",
		"aqua",
		"aquamarine",
		"azure",
		"beige",
		"bisque",
		"blanchedalmond",
		"blue",
		"blueviolet",
		"brown",
		"burlywood",
		"cadetblue",
		"chartreuse",
		"chocolate",
		"coral",
		"cornflowerblue",
		"cornsilk",
		"crimson",
		"cyan",
		"darkblue",
		"darkcyan",
		"darkgoldenrod",
		"darkgray",
		"darkgrey",
		"darkgreen",
		"darkkhaki",
		"darkmagenta",
		"darkolivegreen",
		"darkorange",
		"darkorchid",
		"darkred",
		"darksalmon",
		"darkseagreen",
		"darkslateblue",
		"darkslategray",
		"darkslategrey",
		"darkturquoise",
		"darkviolet",
		"deeppink",
		"deepskyblue",
		"dimgray",
		"dimgrey",
		"dodgerblue",
		"firebrick",
		"floralwhite",
		"forestgreen",
		"fuchsia",
		"gainsboro",
		"ghostwhite",
		"gold",
		"goldenrod",
		"gray",
		"grey",
		"green",
		"greenyellow",
		"honeydew",
		"hotpink",
		"indianred",
		"indigo",
		"ivory",
		"khaki",
		"lavender",
		"lavenderblush",
		"lawngreen",
		"lemonchiffon",
		"lightblue",
		"lightcoral",
		"lightcyan",
		"lightgoldenrodyellow",
		"lightgray",
		"lightgrey",
		"lightgreen",
		"lightpink",
		"lightsalmon",
		"lightseagreen",
		"lightskyblue",
		"lightslategray",
		"lightslategrey",
		"lightsteelblue",
		"lightyellow",
		"lime",
		"limegreen",
		"linen",
		"magenta",
		"maroon",
		"mediumaquamarine",
		"mediumblue",
		"mediumorchid",
		"mediumpurple",
		"mediumseagreen",
		"mediumslateblue",
		"mediumspringgreen",
		"mediumturquoise",
		"mediumvioletred",
		"midnightblue",
		"mintcream",
		"mistyrose",
		"moccasin",
		"navajowhite",
		"navy",
		"oldlace",
		"olive",
		"olivedrab",
		"orange",
		"orangered",
		"orchid",
		"palegoldenrod",
		"palegreen",
		"paleturquoise",
		"palevioletred",
		"papayawhip",
		"peachpuff",
		"peru",
		"pink",
		"plum",
		"powderblue",
		"purple",
		"rebeccapurple",
		"red",
		"rosybrown",
		"royalblue",
		"saddlebrown",
		"salmon",
		"sandybrown",
		"seagreen",
		"seashell",
		"sienna",
		"silver",
		"skyblue",
		"slateblue",
		"slategray",
		"slategrey",
		"snow",
		"springgreen",
		"steelblue",
		"tan",
		"teal",
		"thistle",
		"tomato",
		"turquoise",
		"violet",
		"wheat",
		"whitesmoke",
		"yellow",
		"yellowgreen",
	}
)

# CSS SYSTEM colors (current CSS Color 4 + the deprecated CSS2 set). These are
# NOT in the conventional named-color list yet ARE valid <color> keywords that
# render an OS/theme-derived color, so they must be rejected in a color context
# exactly like a named color (DR2-3).
_CSS_SYSTEM_COLORS = frozenset(
	{
		# current (CSS Color Module Level 4)
		"accentcolor",
		"accentcolortext",
		"activetext",
		"buttonborder",
		"buttonface",
		"buttontext",
		"canvas",
		"canvastext",
		"field",
		"fieldtext",
		"graytext",
		"highlight",
		"highlighttext",
		"linktext",
		"mark",
		"marktext",
		"selecteditem",
		"selecteditemtext",
		"visitedtext",
		# deprecated (CSS2 system colors)
		"activeborder",
		"activecaption",
		"appworkspace",
		"background",
		"buttonhighlight",
		"buttonshadow",
		"captiontext",
		"inactiveborder",
		"inactivecaption",
		"inactivecaptiontext",
		"infobackground",
		"infotext",
		"menu",
		"menutext",
		"scrollbar",
		"threeddarkshadow",
		"threedface",
		"threedhighlight",
		"threedlightshadow",
		"threedshadow",
		"window",
		"windowframe",
		"windowtext",
	}
)

# The two render tokens that carry a FONT stack (the only --jd-* a font-family
# var() may reference); token existence in the theme is checked separately.
_FONT_TOKEN_NAMES = frozenset({"--jd-font", "--jd-font-display"})

# At-rules whose body is a list of RULES (recurse into them for nested checks).
_RULE_BODY_AT = frozenset(
	{
		"media",
		"supports",
		"layer",
		"container",
		"document",
		"-moz-document",
		"scope",
		"keyframes",
		"-webkit-keyframes",
		"-moz-keyframes",
		"-o-keyframes",
	}
)

# Only used to parse an isolated rgb()/hsl() literal's numeric args.
_RGB_RE = re.compile(r"\brgba?\(\s*([^)]*)\)", re.I)
_HSL_RE = re.compile(r"\bhsla?\(\s*([^)]*)\)", re.I)
# An external/protocol-relative URL anywhere in a (decoded) attribute value.
_URLISH_RE = re.compile(r"""(?:https?:)?//[^\s"'()<>]+""", re.I)
# Browser URL preprocessing (WHATWG): ASCII TAB / LF / CR are stripped from the
# whole URL before parsing, so `https:/<TAB>/evil` resolves to `https://evil`.
_URL_CONTROL_STRIP = {0x09: None, 0x0A: None, 0x0D: None}
# http(s) is a "special scheme": the URL parser treats `\` as `/`. Normalize
# only the separator run right after the scheme so `https:\\evil` / `https:/\evil`
# surface, while an arbitrary `\\` elsewhere is never mistaken for a URL (DR2-4).
_SCHEME_SLASH_RE = re.compile(r"(https?:)([\\/]+)", re.I)


# ── color literal helpers (reused by both CSS and inline passes) ──────────────
def _norm_hex(h: str) -> str:
	"""Normalize a #rgb / #rgba / #rrggbb / #rrggbbaa literal to #rrggbb (alpha
	dropped). Returns '' if not a clean 3/4/6/8-digit hex."""
	s = h[1:]
	if len(s) in (3, 4):
		s = "".join(c * 2 for c in s[:3])
	elif len(s) in (6, 8):
		s = s[:6]
	else:
		return ""
	if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
		return ""
	return "#" + s.lower()


def _rgb_to_hex(inner: str) -> str:
	"""rgb()/rgba() numeric args -> #rrggbb (alpha ignored). '' if unparseable
	(percentages, calc, var, etc.) — an unparseable functional color is treated
	as off-palette by the caller."""
	parts = [p.strip() for p in re.split(r"[,\s/]+", inner.strip()) if p.strip()]
	if len(parts) < 3:
		return ""
	try:
		rgb = [max(0, min(255, round(float(x)))) for x in parts[:3]]
	except ValueError:
		return ""
	return "#" + "".join(f"{c:02x}" for c in rgb)


def _hsl_is_neutral(inner: str) -> bool | None:
	"""True if an hsl()/hsla() is achromatic black/white (s=0 & l∈{0,100}) — an
	approved neutral. False if chromatic. None if unparseable."""
	parts = [p.strip().rstrip("%") for p in re.split(r"[,\s/]+", inner.strip()) if p.strip()]
	if len(parts) < 3:
		return None
	try:
		s = float(parts[1])
		lightness = float(parts[2])
	except ValueError:
		return None
	return s == 0 and (lightness == 0 or lightness == 100)


def _color_allowed(literal: str, allowed_hexes: frozenset) -> bool:
	"""Is a single hex/rgb/hsl literal within the theme allow-list (+ neutrals)?"""
	lit = literal.strip().lower()
	if lit.startswith("#"):
		h = _norm_hex(lit)
		return bool(h) and (h in allowed_hexes or h in _APPROVED_HEXES)
	m = _RGB_RE.fullmatch(lit)
	if m:
		h = _rgb_to_hex(m.group(1))
		return bool(h) and (h in allowed_hexes or h in _APPROVED_HEXES)
	m = _HSL_RE.fullmatch(lit)
	if m:
		return _hsl_is_neutral(m.group(1)) is True
	return False


def _canonicalize_url_text(text: str) -> str:
	"""Apply the browser's URL preprocessing to a decoded string so obfuscated
	external URLs surface before the external-URL test (DR2-4): strip ASCII
	TAB/LF/CR (the WHATWG parser removes them anywhere in a URL) and fold a
	special-scheme backslash run right after ``http(s):`` to ``/``."""
	s = (text or "").translate(_URL_CONTROL_STRIP)
	return _SCHEME_SLASH_RE.sub(lambda m: m.group(1) + m.group(2).replace("\\", "/"), s)


def _is_external_url(url: str) -> bool:
	"""A decoded URL string that the browser would FETCH off-origin. data:/blob:/
	relative/fragment are fine; the exact SVG/xlink namespaces are exempt. The
	value is canonicalized (control chars stripped, special-scheme backslashes
	folded) so the exemption + test run on what the browser would actually load."""
	s = _canonicalize_url_text(url).strip().lower()
	if not s or s in _ALLOWED_XML_NS:
		return False
	return s.startswith("http://") or s.startswith("https://") or s.startswith("//")


def _external_urls_in_text(text: str) -> list[str]:
	"""External/protocol-relative URLs in a decoded attribute value (skips the
	exact XML namespace URIs). The text is canonicalized first so a URL split by
	an entity-decoded control char (``https:/&#x09;/evil`` → ``https://evil``) or
	a special-scheme backslash still matches."""
	out = []
	for m in _URLISH_RE.finditer(_canonicalize_url_text(text)):
		u = m.group(0)
		if u.lower() in _ALLOWED_XML_NS:
			continue
		out.append(u)
	return out


# ── HTML parsing (html5lib → lxml) ────────────────────────────────────────────
def _parse_html(html: str):
	return html5lib.parse(html or "", treebuilder="lxml", namespaceHTMLElements=False)


def _local_name(tag) -> str:
	if not isinstance(tag, str):
		return ""
	return tag.rsplit("}", 1)[-1].lower()


def _style_blocks(tree) -> list[str]:
	"""Every <style> element's raw CSS text (an unclosed <style> already carries
	its raw-text-to-EOF content, matching the browser)."""
	return [(el.text or "") for el in tree.iter() if _local_name(el.tag) == "style"]


def _inline_styles(tree) -> list[str]:
	"""Every element's decoded ``style=`` attribute value (incl. unquoted)."""
	out = []
	for el in tree.iter():
		if not isinstance(el.tag, str):
			continue
		st = el.get("style")
		if st:
			out.append(st)
	return out


def _attr_values(tree):
	for el in tree.iter():
		if not isinstance(el.tag, str):
			continue
		for v in el.attrib.values():
			if v:
				yield v


# ── CSS parsing (tinycss2) ────────────────────────────────────────────────────
def _decls(content):
	return tinycss2.parse_declaration_list(content or [], skip_comments=True, skip_whitespace=True)


def _rules(content):
	return tinycss2.parse_rule_list(content or [], skip_comments=True, skip_whitespace=True)


def _flatten_block(content):
	"""Yield ('decl', Declaration) / ('atrule', AtRule) from a declaration-block
	token list, recursing into nested rule bodies. Native CSS nesting puts a
	nested rule's declarations inside a ``CurlyBracketsBlock`` token of the parent
	block, so its selector prelude (which may itself contain an id ``#hash``) is
	never read as a value — only the nested block's declarations are."""
	for d in _decls(content):
		if isinstance(d, ast.Declaration):
			yield ("decl", d)
		elif isinstance(d, ast.AtRule):
			yield from _flatten([d])
	for tok in content or []:
		if type(tok).__name__ == "CurlyBracketsBlock":
			yield from _flatten_block(tok.content)


def _flatten(rules):
	"""Depth-first walk yielding ('decl', Declaration) / ('atrule', AtRule) for
	the whole (possibly nested) rule tree. @font-face is terminal (its body is the
	face definition, flagged as a safety violation, never recursed)."""
	for rule in rules:
		if isinstance(rule, ast.QualifiedRule):
			yield from _flatten_block(rule.content)
		elif isinstance(rule, ast.AtRule):
			yield ("atrule", rule)
			kw = (rule.lower_at_keyword or "").lower()
			if kw == "font-face" or rule.content is None:
				continue
			if kw in _RULE_BODY_AT:
				yield from _flatten(_rules(rule.content))
			else:
				yield from _flatten_block(rule.content)


def _has_structural_error(css: str) -> bool:
	"""True if the CSS has a stray/misplaced close bracket — a top-level ``}``
	(or ``)`` / ``]``) with no opener. Such a ``}`` would terminate the injected
	``@layer author {}`` wrapper early and let the following rule render UNLAYERED,
	outranking the theme (F#4). An unclosed ``{`` is NOT flagged: it stays
	contained inside the wrapper, so it cannot escape."""
	for tok in tinycss2.parse_component_value_list(css or ""):
		if isinstance(tok, ast.ParseError) and tok.kind in ("}", ")", "]"):
			return True
	return False


def _first_custom_prop(tokens) -> str | None:
	for t in tokens:
		if type(t).__name__ == "IdentToken" and (t.value or "").startswith("--"):
			return t.value
	return None


def _var_fallback_tokens(arguments) -> list:
	"""The token list after the FIRST top-level comma in a ``var()``'s arguments —
	its fallback expression (``[]`` when there is no fallback). The browser renders
	this when the referenced custom property is unset, so it must be validated too
	(DR2-1)."""
	for idx, t in enumerate(arguments or []):
		if type(t).__name__ == "LiteralToken" and t.value == ",":
			return list(arguments[idx + 1 :])
	return []


def _url_strings(tokens) -> list[str]:
	"""Every URL a declaration value would fetch: url() tokens and url("…")
	function-string args, recursively (e.g. inside a gradient / image-set)."""
	out = []
	for tok in tokens:
		tn = type(tok).__name__
		if tn == "URLToken":
			out.append(tok.value or "")
		elif tn == "FunctionBlock":
			if (tok.lower_name or "").lower() == "url":
				out += [a.value for a in tok.arguments if type(a).__name__ == "StringToken"]
			else:
				out += _url_strings(tok.arguments)
	return out


def _import_urls(prelude) -> list[str]:
	"""The URL of an @import (string form ``@import "…"`` or ``@import url(…)``)."""
	out = []
	for tok in prelude or []:
		tn = type(tok).__name__
		if tn == "StringToken":
			out.append(tok.value or "")
		elif tn == "URLToken":
			out.append(tok.value or "")
		elif tn == "FunctionBlock" and (tok.lower_name or "").lower() == "url":
			out += [a.value for a in tok.arguments if type(a).__name__ == "StringToken"]
	return out


def _has_prefers_scheme(prelude) -> bool:
	"""True if a media-query prelude references ``prefers-color-scheme`` (decoded,
	so an escaped ``prefers-c\\6f lor-scheme`` is caught too)."""

	def idents(tokens):
		for t in tokens or []:
			tn = type(t).__name__
			if tn == "IdentToken":
				yield (t.value or "").lower()
			elif tn in ("ParenthesesBlock", "SquareBracketsBlock", "CurlyBracketsBlock"):
				yield from idents(t.content)
			elif tn == "FunctionBlock":
				yield from idents(t.arguments)

	return "prefers-color-scheme" in set(idents(prelude))


# ── violation factories ───────────────────────────────────────────────────────
def _color_violation(token: str, rule: str, location: str) -> Violation:
	if rule == RULE_INLINE_COLOR:
		msg = (
			f"Off-theme color '{token}' in an inline style= — it escapes the theme "
			"layer; use a var(--jd-*) token in a <style> rule."
		)
	else:
		msg = f"Off-theme color '{token}' — use a var(--jd-*) token or a color from the selected theme."
	return Violation(rule, location, token, msg)


def _external_violation(url: str) -> Violation:
	u = (url or "")[:120]
	return Violation(
		RULE_EXTERNAL_URL,
		"html",
		u,
		f"External URL '{u}' is not allowed — inline all resources (data: URIs only).",
	)


# ── declaration + at-rule checks ──────────────────────────────────────────────
def _scan_color_tokens(tokens, allowed_hexes, allowed_tokens, out, *, color_rule, location):
	"""Flag any color VALUE that is not an allowed theme hex / existing --jd-*
	token / approved neutral. Recurses into non-color functions (gradients,
	image-set) and into a var() fallback expression."""
	for tok in tokens:
		tn = type(tok).__name__
		if tn == "HashToken":
			lit = "#" + (tok.value or "")
			if not _color_allowed(lit, allowed_hexes):
				out.append(_color_violation(lit, color_rule, location))
		elif tn == "FunctionBlock":
			fname = (tok.lower_name or "").lower()
			if fname in _MODERN_COLOR_FNS:
				out.append(_color_violation(tinycss2.serialize([tok]).strip(), color_rule, location))
			elif fname in ("rgb", "rgba", "hsl", "hsla"):
				lit = tinycss2.serialize([tok]).strip()
				if not _color_allowed(lit, allowed_hexes):
					out.append(_color_violation(lit, color_rule, location))
			elif fname == "var":
				# The referenced token must EXIST in the selected theme (a mere
				# `--jd-` prefix on a non-existent name would render its fallback);
				# and a fallback, if present, is validated recursively (DR2-1).
				ref = _first_custom_prop(tok.arguments)
				if not (ref and ref.lower() in allowed_tokens):
					out.append(_color_violation(tinycss2.serialize([tok]).strip(), color_rule, location))
				else:
					fb = _var_fallback_tokens(tok.arguments)
					if fb:
						_scan_color_tokens(
							fb, allowed_hexes, allowed_tokens, out, color_rule=color_rule, location=location
						)
			else:
				_scan_color_tokens(
					tok.arguments,
					allowed_hexes,
					allowed_tokens,
					out,
					color_rule=color_rule,
					location=location,
				)
		elif tn == "IdentToken":
			# Reject any <color> keyword that is not approved: a conventional named
			# color OR a CSS system color (DR2-3). Non-color idents (`solid`,
			# `center`, …) in a color-bearing shorthand are not <color> keywords and
			# pass through untouched.
			w = (tok.value or "").lower()
			if w not in _APPROVED_COLOR_WORDS and (w in _CSS_NAMED_COLORS or w in _CSS_SYSTEM_COLORS):
				out.append(_color_violation(tok.value, color_rule, location))


def _family_candidates(tokens, shorthand):
	"""(kind, display, var_ref, var_fn) per comma-separated font-family segment.
	``kind`` is 'var' (a var() indirection — ``var_fn`` is the FunctionBlock, so
	its fallback can be validated) or 'name' (a literal family; ``var_fn`` None)."""
	segments = [[]]
	for t in tokens:
		if type(t).__name__ == "LiteralToken" and t.value == ",":
			segments.append([])
		else:
			segments[-1].append(t)
	out = []
	for idx, seg in enumerate(segments):
		var_fn = next(
			(t for t in seg if type(t).__name__ == "FunctionBlock" and (t.lower_name or "").lower() == "var"),
			None,
		)
		if var_fn is not None:
			out.append(
				("var", tinycss2.serialize([var_fn]).strip(), _first_custom_prop(var_fn.arguments), var_fn)
			)
			continue
		strings = [t for t in seg if type(t).__name__ == "StringToken"]
		if strings:
			out.append(("name", strings[-1].value.strip().lower(), None, None))
			continue
		idents = [t for t in seg if type(t).__name__ == "IdentToken"]
		if not idents:
			continue
		if shorthand and idx == 0:
			# `font:` shorthand — the family is the trailing ident run after the
			# last size / line-height / numeric token of the first segment.
			last_num = -1
			for i, t in enumerate(seg):
				tn = type(t).__name__
				if tn in ("NumberToken", "DimensionToken", "PercentageToken") or (
					tn == "LiteralToken" and t.value == "/"
				):
					last_num = i
			tail = [t.value for t in seg[last_num + 1 :] if type(t).__name__ == "IdentToken"]
			name = " ".join(tail)
		else:
			name = " ".join(t.value for t in idents)
		out.append(("name", name.strip().lower(), None, None))
	return out


def _check_font(tokens, shorthand, allowed_families, allowed_tokens, out, *, location):
	for kind, display, ref, var_fn in _family_candidates(tokens, shorthand):
		if kind == "var":
			# Must reference an EXISTING font token (--jd-font / --jd-font-display);
			# a fallback, if present, is validated as a font family too (DR2-1).
			low = (ref or "").lower()
			if low in _FONT_TOKEN_NAMES and low in allowed_tokens:
				fb = _var_fallback_tokens(var_fn.arguments) if var_fn is not None else []
				if fb:
					_check_font(fb, False, allowed_families, allowed_tokens, out, location=location)
				continue
			out.append(
				Violation(
					RULE_FONT_FAMILY,
					location,
					display,
					f"Font family '{display}' is not allowed — reference var(--jd-font)/"
					"var(--jd-font-display) or the system font stack, not another custom property.",
				)
			)
			continue
		fam = display
		if not fam or not any(c.isalpha() for c in fam):
			continue
		if fam in _GENERIC_FAMILIES or fam in allowed_families:
			continue
		out.append(
			Violation(
				RULE_FONT_FAMILY,
				location,
				fam,
				f"Font family '{fam}' is not allowed — use var(--jd-font)/var(--jd-font-display) "
				"or the system font stack.",
			)
		)


def _check_declaration(d, ctx, out, *, color_rule, location):
	name = d.name or ""
	lname = (d.lower_name or name).lower()

	# External URLs in a declaration value — always (incl. Custom).
	for u in _url_strings(d.value):
		if _is_external_url(u):
			out.append(_external_violation(u))

	if not ctx["design"]:
		return

	if lname.startswith("--jd-"):
		out.append(
			Violation(
				RULE_TOKEN_REDEFINE,
				location,
				name,
				f"'{name}' is a theme token — do not redefine it; the selected theme owns "
				"the --jd-* values. Reference it with var(--jd-*), never reassign it.",
			)
		)
	if d.important:
		out.append(
			Violation(
				RULE_IMPORTANT,
				location,
				f"{name}: … !important",
				f"!important on '{name}' is not allowed — it escapes the theme layer; "
				"remove it and rely on the theme's own styling.",
			)
		)
	if lname == "color-scheme":
		out.append(
			Violation(
				RULE_COLOR_SCHEME,
				location,
				"color-scheme",
				"The color-scheme property is not allowed — light/dark follows the theme's data-theme only.",
			)
		)
	if lname in _COLOR_PROPS:
		_scan_color_tokens(
			d.value,
			ctx["allowed_hexes"],
			ctx["allowed_tokens"],
			out,
			color_rule=color_rule,
			location=location,
		)
	if lname in _FONT_PROPS:
		_check_font(
			d.value, lname == "font", ctx["allowed_families"], ctx["allowed_tokens"], out, location=location
		)


def _check_at_rule(rule, ctx, out):
	kw = (rule.lower_at_keyword or "").lower()
	if kw == "font-face":
		out.append(
			Violation(
				RULE_FONT_FACE,
				"<style>",
				"@font-face",
				"@font-face is not allowed — the canvas has no network and must use the system font stack.",
			)
		)
		return
	if kw == "import":
		for u in _import_urls(rule.prelude):
			if _is_external_url(u):
				out.append(_external_violation(u))
	if ctx["design"] and kw == "media" and _has_prefers_scheme(rule.prelude):
		out.append(
			Violation(
				RULE_PREFERS_SCHEME,
				"<style>",
				"@media (prefers-color-scheme)",
				"@media (prefers-color-scheme) is not allowed — light/dark follows the theme's data-theme only.",
			)
		)


def _check_style_block(css, ctx, out):
	# A stray/misplaced } would break out of the @layer author {} wrapper (F#4);
	# reject it (standard themes only — Custom author CSS is never layer-wrapped).
	if ctx["design"] and _has_structural_error(css):
		out.append(
			Violation(
				RULE_MALFORMED,
				"<style>",
				"unbalanced-braces",
				"The CSS has unbalanced or misplaced braces — fix the syntax so it can "
				"be enforced under the theme.",
			)
		)
	rules = tinycss2.parse_stylesheet(css or "", skip_comments=True, skip_whitespace=True)
	for kind, node in _flatten(rules):
		if kind == "atrule":
			_check_at_rule(node, ctx, out)
		else:
			_check_declaration(node, ctx, out, color_rule=RULE_OFF_PALETTE, location="<style>")


def _check_inline_style(css, ctx, out):
	for d in _decls(css):
		if isinstance(d, ast.Declaration):
			_check_declaration(d, ctx, out, color_rule=RULE_INLINE_COLOR, location="inline style=")


# ── public entry point ───────────────────────────────────────────────────────
def validate_dashboard_html(html: str, theme: str) -> list[Violation]:
	"""Return the (possibly empty) list of ``Violation`` for a dashboard's HTML
	under ``theme`` (a DocType label or lowercase key). Pure + deterministic.

	For the bespoke ``Custom`` theme only the safety bans (@font-face, external
	URLs) apply — the design rules are relaxed. An unknown theme is validated as
	the default ('jarvis')."""
	html = html or ""
	key = theme_spec.theme_key(theme)
	design = key != theme_spec.CUSTOM_KEY

	try:
		tree = _parse_html(html)
	except Exception:
		# html5lib parses any string like a browser, so this is effectively
		# unreachable; fail closed (reject) rather than silently pass an
		# un-scannable document.
		return [
			Violation(
				RULE_MALFORMED,
				"html",
				"unparseable",
				"The dashboard HTML could not be parsed — fix the markup and try again.",
			)
		]

	violations: list = []

	# Safety: external URLs in any (entity-decoded) attribute value — always.
	for value in _attr_values(tree):
		for u in _external_urls_in_text(value):
			violations.append(_external_violation(u))

	allowed_hexes: frozenset = frozenset()
	allowed_families: frozenset = frozenset()
	allowed_tokens: frozenset = frozenset()
	if design:
		spec = theme_spec.load_theme(key) or theme_spec.load_theme(theme_spec.DEFAULT_KEY)
		allowed_hexes = frozenset(h.lower() for h in spec["validator"]["allowed_color_hexes"])
		allowed_families = frozenset(f.lower() for f in spec["validator"]["allowed_font_families"])
		# The EXACT --jd-* render-token names this theme emits (DR2-1): a var()
		# color/font ref must name one that exists, not merely start with --jd-.
		allowed_tokens = frozenset("--jd-" + str(name).lower() for name in (spec.get("render_tokens") or {}))
	ctx = {
		"allowed_hexes": allowed_hexes,
		"allowed_families": allowed_families,
		"allowed_tokens": allowed_tokens,
		"design": design,
	}

	for css in _style_blocks(tree):
		_check_style_block(css, ctx, violations)
	for css in _inline_styles(tree):
		_check_inline_style(css, ctx, violations)

	return _dedupe(violations)


def _dedupe(violations: list) -> list:
	"""Drop identical (rule, offending) repeats so a value flagged by two passes
	surfaces once. Order preserved."""
	seen: set = set()
	out: list = []
	for v in violations:
		key = (v.rule, v.offending)
		if key in seen:
			continue
		seen.add(key)
		out.append(v)
	return out


def format_violations(violations: list) -> str:
	"""Render violations as the human message body for the save-time throw — the
	human ``.message`` only (the machine-readable ``rule`` code stays in the
	structured ``Violation`` for the feed-to-model path, never in the modal)."""
	return "\n".join(f"- {v.message}" for v in violations)
