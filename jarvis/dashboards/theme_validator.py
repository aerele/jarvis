"""Deterministic save-time validator for dashboard canvas HTML.

Pure + frappe-free (import-safe without a bench) so it can be unit-tested
standalone; the ``Jarvis Dashboard`` controller calls
``validate_dashboard_html`` on every save and throws a structured error when it
returns violations. NO auto-repair — a rejected save surfaces the reasons and
the builder's chat loop regenerates.

What it enforces (a standard theme):
  * off-palette color literals in CSS  (hex / rgb / hsl / named, outside the
    theme's own hexes + an approved white/black/transparent neutral set)
  * ``font-family`` (or ``font`` shorthand) naming a non-system face
  * ``@media (prefers-color-scheme)`` and the ``color-scheme`` property
    (light/dark is driven by the theme's ``data-theme`` ONLY)
  * ``!important`` on a color/font declaration
  * off-palette color in an inline ``style=`` attribute
Always enforced (a safety subset, even for the bespoke ``Custom`` theme):
  * ``@font-face``
  * external URLs (http/https/protocol-relative resource refs)

Scope + honest limits: color/font rules scan CSS only — ``<style>`` blocks and
inline ``style=`` attributes — NOT ``<script>`` bodies, so colors set in ECharts
JS config are governed by the skill's "use window.JARVIS_THEME.palette"
instruction, not this linter. Detection is regex over comment-stripped CSS
(``/* */`` in CSS, ``<!-- -->`` in HTML are removed first); it targets the
enumerable patterns above and is a standard/cleanliness gate layered under the
hard CSP+sandbox boundary (which is what actually blocks network egress), not an
exhaustive parser.
"""

from __future__ import annotations

import re
from typing import NamedTuple

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
RULE_PREFERS_SCHEME = "prefers-color-scheme"
RULE_COLOR_SCHEME = "color-scheme"
RULE_IMPORTANT = "important-color-font"
RULE_INLINE_COLOR = "inline-style-color"
RULE_FONT_FACE = "font-face"  # safety
RULE_EXTERNAL_URL = "external-url"  # safety

# Color-bearing CSS properties (shorthands included). Used for the named-color,
# inline-color and !important checks.
_COLOR_PROPS = frozenset({
	"color", "background", "background-color", "background-image",
	"border", "border-color", "border-top", "border-right", "border-bottom",
	"border-left", "border-top-color", "border-right-color",
	"border-bottom-color", "border-left-color", "outline", "outline-color",
	"box-shadow", "text-shadow", "fill", "stroke", "caret-color",
	"column-rule-color", "text-decoration-color", "accent-color",
})
_FONT_PROPS = frozenset({"font-family", "font"})

# Approved neutral hex values (always allowed regardless of theme).
_APPROVED_HEXES = frozenset({"#ffffff", "#000000"})
# Approved color keywords (values, not families).
_APPROVED_COLOR_WORDS = frozenset({
	"transparent", "currentcolor", "inherit", "initial", "unset", "revert",
	"white", "black", "none",
})
# Generic font-family keywords (always allowed as families).
_GENERIC_FAMILIES = frozenset({
	"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
	"ui-sans-serif", "ui-serif", "ui-monospace", "ui-rounded", "math", "emoji",
	"fangsong", "inherit", "initial", "unset", "revert",
})

# Full CSS named-color set (minus the approved neutrals above), used to flag a
# brand-y named color sitting in a color-bearing declaration value.
_CSS_NAMED_COLORS = frozenset({
	"aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige",
	"bisque", "blanchedalmond", "blue", "blueviolet", "brown", "burlywood",
	"cadetblue", "chartreuse", "chocolate", "coral", "cornflowerblue",
	"cornsilk", "crimson", "cyan", "darkblue", "darkcyan", "darkgoldenrod",
	"darkgray", "darkgrey", "darkgreen", "darkkhaki", "darkmagenta",
	"darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon",
	"darkseagreen", "darkslateblue", "darkslategray", "darkslategrey",
	"darkturquoise", "darkviolet", "deeppink", "deepskyblue", "dimgray",
	"dimgrey", "dodgerblue", "firebrick", "floralwhite", "forestgreen",
	"fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod", "gray", "grey",
	"green", "greenyellow", "honeydew", "hotpink", "indianred", "indigo",
	"ivory", "khaki", "lavender", "lavenderblush", "lawngreen", "lemonchiffon",
	"lightblue", "lightcoral", "lightcyan", "lightgoldenrodyellow", "lightgray",
	"lightgrey", "lightgreen", "lightpink", "lightsalmon", "lightseagreen",
	"lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue",
	"lightyellow", "lime", "limegreen", "linen", "magenta", "maroon",
	"mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple",
	"mediumseagreen", "mediumslateblue", "mediumspringgreen", "mediumturquoise",
	"mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin",
	"navajowhite", "navy", "oldlace", "olive", "olivedrab", "orange",
	"orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise",
	"palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum",
	"powderblue", "purple", "rebeccapurple", "red", "rosybrown", "royalblue",
	"saddlebrown", "salmon", "sandybrown", "seagreen", "seashell", "sienna",
	"silver", "skyblue", "slateblue", "slategray", "slategrey", "snow",
	"springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise",
	"violet", "wheat", "whitesmoke", "yellow", "yellowgreen",
})

# ── regexes ──────────────────────────────────────────────────────────────────
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>([\s\S]*?)</style>", re.I)
_INLINE_STYLE_RE = re.compile(r"""\bstyle\s*=\s*("([^"]*)"|'([^']*)')""", re.I)
_CSS_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
# One CSS declaration: `prop: value` up to ; } or {. Skips selectors (their
# "prop" is a tag/class name, never a color/font property we inspect).
_DECL_RE = re.compile(r"([-a-zA-Z]+)\s*:\s*([^;{}]*)")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_RGB_RE = re.compile(r"\brgba?\(\s*([^)]*)\)", re.I)
_HSL_RE = re.compile(r"\bhsla?\(\s*([^)]*)\)", re.I)
_FONT_FACE_RE = re.compile(r"@font-face\b", re.I)
_PREFERS_SCHEME_RE = re.compile(r"@media[^{]*prefers-color-scheme", re.I)
# `color-scheme:` as its own property — the negative lookbehind keeps it from
# matching the `color-scheme` tail of `prefers-color-scheme` (its own rule).
_COLOR_SCHEME_RE = re.compile(r"(?<![-\w])color-scheme\s*:", re.I)
_FONT_FACE_BLOCK_RE = re.compile(r"@font-face\s*\{[^{}]*\}", re.I)
# http/https (except W3C XML namespaces, which are identifiers, not fetches).
_HTTP_URL_RE = re.compile(r"""https?://(?!(?:www\.)?w3\.org/)[^\s"'()<>]+""", re.I)
# protocol-relative resource references: src/href="//…" or url(//…).
_PROTO_REL_RE = re.compile(r"""(?:\bsrc|\bhref|\burl\()\s*=?\s*["']?//[^\s"'()<>]+""", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────
def _strip_css_comments(css: str) -> str:
	return _CSS_COMMENT_RE.sub(" ", css or "")


def _style_css(html: str) -> str:
	"""All author <style> CSS, comment-stripped and joined."""
	return "\n".join(_strip_css_comments(m.group(1)) for m in _STYLE_BLOCK_RE.finditer(html))


def _inline_styles(html: str) -> list[str]:
	out = []
	for m in _INLINE_STYLE_RE.finditer(html):
		out.append(_strip_css_comments(m.group(2) if m.group(2) is not None else (m.group(3) or "")))
	return out


def _norm_hex(h: str) -> str:
	"""Normalize a #rgb / #rgba / #rrggbb / #rrggbbaa literal to #rrggbb
	(alpha dropped). Returns '' if not a clean 3/4/6/8-digit hex."""
	s = h[1:]
	if len(s) in (3, 4):
		s = "".join(c * 2 for c in s[:3])
	elif len(s) in (6, 8):
		s = s[:6]
	else:
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
		rgb = [max(0, min(255, int(round(float(x))))) for x in parts[:3]]
	except ValueError:
		return ""
	return "#" + "".join(f"{c:02x}" for c in rgb)


def _hsl_is_neutral(inner: str) -> bool | None:
	"""True if an hsl()/hsla() is achromatic black/white (s=0 & l∈{0,100}) —
	an approved neutral. False if chromatic / non-neutral. None if unparseable."""
	parts = [p.strip().rstrip("%") for p in re.split(r"[,\s/]+", inner.strip()) if p.strip()]
	if len(parts) < 3:
		return None
	try:
		s = float(parts[1])
		l = float(parts[2])
	except ValueError:
		return None
	return s == 0 and (l == 0 or l == 100)


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


def _iter_decls(css: str):
	"""Yield (prop_lower, value) declarations from a CSS string."""
	for m in _DECL_RE.finditer(css):
		yield m.group(1).lower(), m.group(2).strip()


def _family_tokens(value: str, shorthand: bool) -> list[str]:
	"""Candidate font-family names from a font-family value (or a `font`
	shorthand). For the shorthand, the leading size/line-height/weight tokens of
	the first comma-segment are dropped (family is the trailing word group)."""
	segs = [s.strip() for s in value.split(",") if s.strip()]
	if not segs:
		return []
	fams = list(segs)
	if shorthand:
		# `font: 700 14px/1.5 "Segoe UI", sans-serif` -> first seg's family is
		# after the last space run; keep the rest of the segments verbatim.
		first = segs[0]
		fams[0] = first.rsplit(" ", 1)[-1] if " " in first and '"' not in first and "'" not in first else first
	return [f.strip().strip("\"'").lower() for f in fams if f.strip()]


# ── individual checks ────────────────────────────────────────────────────────
def _check_font_face(css: str, out: list) -> None:
	if _FONT_FACE_RE.search(css):
		out.append(Violation(
			RULE_FONT_FACE, "<style>", "@font-face",
			"@font-face is not allowed — the canvas has no network and must use the system font stack.",
		))


def _check_external_urls(html: str, out: list) -> None:
	scan = _HTML_COMMENT_RE.sub(" ", html)
	seen: set = set()
	for m in _HTTP_URL_RE.finditer(scan):
		url = m.group(0)[:120]
		if url in seen:
			continue
		seen.add(url)
		out.append(Violation(
			RULE_EXTERNAL_URL, "html", url,
			f"External URL '{url}' is not allowed — inline all resources (data: URIs only).",
		))
	for m in _PROTO_REL_RE.finditer(scan):
		frag = m.group(0)[:120]
		if frag in seen:
			continue
		seen.add(frag)
		out.append(Violation(
			RULE_EXTERNAL_URL, "html", frag,
			f"Protocol-relative resource '{frag}' is not allowed — inline all resources.",
		))


def _check_prefers_color_scheme(css: str, out: list) -> None:
	if _PREFERS_SCHEME_RE.search(css):
		out.append(Violation(
			RULE_PREFERS_SCHEME, "<style>", "@media (prefers-color-scheme)",
			"@media (prefers-color-scheme) is not allowed — light/dark follows the theme's data-theme only.",
		))


def _check_color_scheme(css: str, out: list) -> None:
	if _COLOR_SCHEME_RE.search(css):
		out.append(Violation(
			RULE_COLOR_SCHEME, "<style>", "color-scheme",
			"The color-scheme property is not allowed — light/dark follows the theme's data-theme only.",
		))


def _check_css_colors(css: str, allowed_hexes: frozenset, out: list) -> None:
	# Pass A: hex + rgb/hsl literals anywhere in the CSS.
	for m in _HEX_RE.finditer(css):
		lit = m.group(0)
		if not _color_allowed(lit, allowed_hexes):
			out.append(_off_palette(lit))
	for m in _RGB_RE.finditer(css):
		lit = m.group(0)
		if not _color_allowed(lit, allowed_hexes):
			out.append(_off_palette(lit))
	for m in _HSL_RE.finditer(css):
		lit = m.group(0)
		if not _color_allowed(lit, allowed_hexes):
			out.append(_off_palette(lit))
	# Pass B: named colors, only inside a color-bearing declaration value.
	for prop, value in _iter_decls(css):
		if prop not in _COLOR_PROPS:
			continue
		for word in re.findall(r"[a-zA-Z]+", value):
			w = word.lower()
			if w in _CSS_NAMED_COLORS and w not in _APPROVED_COLOR_WORDS:
				out.append(_off_palette(word))


def _off_palette(token: str) -> Violation:
	return Violation(
		RULE_OFF_PALETTE, "<style>", token,
		f"Off-theme color '{token}' — use a var(--jd-*) token or a color from the selected theme.",
	)


def _check_font_family(css: str, allowed_families: frozenset, out: list) -> None:
	for prop, value in _iter_decls(css):
		if prop not in _FONT_PROPS:
			continue
		for fam in _family_tokens(value, shorthand=(prop == "font")):
			if not fam or fam.startswith("var(") or fam in _GENERIC_FAMILIES or fam in allowed_families:
				continue
			# A `font:` shorthand carries size/weight tokens we can't fully model;
			# only flag a token that looks like a real family name (letters).
			if prop == "font" and not re.search(r"[a-zA-Z]", fam):
				continue
			out.append(Violation(
				RULE_FONT_FAMILY, "<style>", fam,
				f"Font family '{fam}' is not allowed — use var(--jd-font)/var(--jd-font-display) "
				"or the system font stack.",
			))


def _check_important(css: str, out: list) -> None:
	for prop, value in _iter_decls(css):
		if "!important" not in value.lower():
			continue
		if prop in _COLOR_PROPS or prop in _FONT_PROPS:
			out.append(Violation(
				RULE_IMPORTANT, "<style>", f"{prop}: … !important",
				f"!important on '{prop}' is not allowed — it escapes the theme layer.",
			))


def _check_inline_styles(inline_styles: list, allowed_hexes: frozenset, out: list) -> None:
	for css in inline_styles:
		for prop, value in _iter_decls(css):
			if prop not in _COLOR_PROPS:
				continue
			bad = None
			for lit_m in list(_HEX_RE.finditer(value)) + list(_RGB_RE.finditer(value)) + list(_HSL_RE.finditer(value)):
				if not _color_allowed(lit_m.group(0), allowed_hexes):
					bad = lit_m.group(0)
					break
			if bad is None:
				for word in re.findall(r"[a-zA-Z]+", value):
					w = word.lower()
					if w in _CSS_NAMED_COLORS and w not in _APPROVED_COLOR_WORDS:
						bad = word
						break
			if bad is not None:
				out.append(Violation(
					RULE_INLINE_COLOR, "inline style=", bad,
					f"Off-theme color '{bad}' in an inline style= — it escapes the theme layer; "
					"use a var(--jd-*) token in a <style> rule.",
				))


# ── public entry point ───────────────────────────────────────────────────────
def validate_dashboard_html(html: str, theme: str) -> list[Violation]:
	"""Return the (possibly empty) list of ``Violation`` for a dashboard's HTML
	under ``theme`` (a DocType label or lowercase key). Pure + deterministic.

	For the bespoke ``Custom`` theme only the safety bans (@font-face, external
	URLs) apply — the design rules are relaxed. An unknown theme is validated as
	the default ('jarvis')."""
	html = html or ""
	key = theme_spec.theme_key(theme)
	css = _style_css(html)
	violations: list = []

	# Safety bans — always, including Custom.
	_check_font_face(css, violations)
	_check_external_urls(html, violations)
	if key == theme_spec.CUSTOM_KEY:
		return violations

	spec = theme_spec.load_theme(key) or theme_spec.load_theme(theme_spec.DEFAULT_KEY)
	allowed_hexes = frozenset(h.lower() for h in spec["validator"]["allowed_color_hexes"])
	allowed_families = frozenset(f.lower() for f in spec["validator"]["allowed_font_families"])

	# @font-face is already flagged (safety); drop its block so its internal
	# `font-family:<face-name>` and any data: src don't double-report as design
	# violations.
	css_design = _FONT_FACE_BLOCK_RE.sub(" ", css)
	_check_prefers_color_scheme(css_design, violations)
	_check_color_scheme(css_design, violations)
	_check_css_colors(css_design, allowed_hexes, violations)
	_check_font_family(css_design, allowed_families, violations)
	_check_important(css_design, violations)
	_check_inline_styles(_inline_styles(html), allowed_hexes, violations)
	return violations


def format_violations(violations: list) -> str:
	"""Render violations as the human message body for the save-time throw."""
	lines = [f"- [{v.rule}] {v.message}" for v in violations]
	return "\n".join(lines)
