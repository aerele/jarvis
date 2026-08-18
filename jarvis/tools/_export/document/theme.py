"""Branded component stylesheet for the rich-PDF document engine.

This is the visual layer the rich path (Task 3's ``furniture.render_pdf`` and
Task 6's ``export_document`` orchestrator) wraps around ``sanitize_rich``'s
output. It is a **pure module** - no ``import frappe`` anywhere in it - so the
stylesheet and its class-name contract are unit-testable without a bench, the
same purity guarantee ``sanitizer.py`` makes for the same reason.

WHY a function AND a constant: ``component_css()`` is the source of truth
(callable, so a later per-tenant token layer - accent color from ``Jarvis
Settings``, say - can build on top of it without breaking this signature);
``THEME_CSS`` is the same output cached at import time for callers that just
want the static string.

THE CONTRACT: every class name below is load-bearing. Task 4's number/RAG/
chart builders and Task 6's HTML assembly emit these EXACT class names onto
sanitized (agent) or tool-built (post-sanitize) markup, so renaming one here
silently breaks a downstream tool. New classes are free to add; the existing
names are not free to rename without updating every caller.

  * Typography: bare ``h1``..``h6`` + ``body`` (no class needed - the
    sanitizer already allows these tags with only a ``class`` attribute).
  * Tables: bare ``table``/``th``/``td`` (base) + ``.zebra`` (put on the
    ``<table>``; alternates via ``tr:nth-child``) + ``.grouped`` (put on the
    subtotal/group-total ``<tr>``) + ``.indent-1``..``.indent-5`` (tree-row
    label indent, any tag) + ``.neg`` (negative-value red; the tool supplies
    the parenthetical text, this only supplies the color) + ``.rag-red`` /
    ``.rag-amber`` / ``.rag-green`` (status cell or nested chip ``<span>``).
  * ``.num`` - ``text-align:right`` helper for numeric columns.
  * ``.callout`` - accent left-border + tinted background.
  * ``.kpi-tile`` - a value/label/delta card. The tile itself is a complete,
    reasonable-looking box with no children; nested ``.kpi-value`` /
    ``.kpi-label`` / ``.kpi-delta`` are optional convenience sub-classes for
    callers that want the full value+label+delta layout (``.kpi-delta``
    reuses the contract's own ``.neg`` to flip red - one negative-color
    token for the whole document, not two).
  * ``.cover`` - full-bleed title page, ``page-break-after`` so body content
    starts on its own page.
  * ``.section-divider`` - ``page-break-before`` section header rule.
  * ``.signature-block`` - sign-off area; nested ``.signature-line`` is an
    optional convenience sub-class for an individual name/date line.
  * ``.bar-chart`` (container) + ``.bar`` (a single bar). ``.bar`` styles
    ONLY color/height - the tool sets ``width`` per bar via a tool-controlled
    inline ``style`` spliced in AFTER ``sanitize_rich`` (agent content itself
    can never carry a ``style`` attribute - see ``sanitizer.py``). Nested
    ``.bar-row`` / ``.bar-label`` / ``.bar-track`` are optional convenience
    sub-classes for the label+track layout around a bar.

WHY no flexbox/grid and no CSS custom properties (``var()``): the render
engine is wkhtmltopdf's patched-Qt WebKit (a pre-2013 WebKit fork - see the
plan's "Engine = wkhtmltopdf ONLY" constraint), which does not reliably
support either. Every layout below uses block / inline-block / table flow
only, and every color is baked into the CSS text at build time through
``string.Template`` ``$``-substitution rather than emitted as ``var(--x)``.

WHY ``string.Template`` and not an f-string: the CSS body below is mostly
literal ``{`` / ``}`` (every rule block), which an f-string would force
escaping into ``{{``/``}}`` throughout - error-prone at this size.
``string.Template`` substitutes ``$name`` tokens instead, so the CSS reads as
plain CSS.

WHY one global ``-webkit-print-color-adjust:exact`` rule instead of repeating
it on every background-bearing selector: wkhtmltopdf drops print background
fills without it, and ``*`` is a strict superset of "anything with a
background fill" - elements with no fill simply ignore a harmless
color-adjust hint. One rule, easy to snapshot-test, nothing to keep in sync
by hand as components are added.
"""

from __future__ import annotations

from string import Template

# --- brand tokens (v1 default; a future per-tenant layer swaps these before
# calling component_css(), not the template itself) ------------------------

_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'

_PRIMARY = "#1f4e79"  # accents, rules, chart bars, thead fill
_DARK = "#132d47"  # headings, cover background
_MUTED = "#666666"  # captions, labels, footnotes
_INK = "#1a1a1a"  # body text
_LINE = "#d9d9d9"  # hairline borders
_TINT = "#eef2f7"  # zebra/callout/subtotal tinted background

_RED = "#b3261e"
_RED_BG = "#fbe9e7"
_AMBER = "#8a5a00"
_AMBER_BG = "#fff4e0"
_GREEN = "#1e7d32"
_GREEN_BG = "#e6f4ea"

# Tree-row indent step; five levels (.indent-1..indent-5) generated below.
_INDENT_STEP_PT = 14
_INDENT_LEVELS = range(1, 6)

_CSS_TEMPLATE = Template(
	"""
/* === Branded component stylesheet (Slice 1) - see theme.py docstring for
   the full class-name contract and the wkhtmltopdf layout constraints. === */

* {
	-webkit-print-color-adjust:exact;
	print-color-adjust:exact;
}

body {
	font-family: $font;
	font-size: 11pt;
	line-height: 1.5;
	color: $ink;
}

h1, h2, h3, h4, h5, h6 {
	font-weight: 700;
	line-height: 1.25;
	color: $dark;
	margin: 0 0 10pt;
}
h1 { font-size: 26pt; }
h2 { font-size: 20pt; }
h3 { font-size: 16pt; }
h4 { font-size: 13pt; }
h5 { font-size: 11.5pt; }
h6 {
	font-size: 10pt;
	text-transform: uppercase;
	letter-spacing: 0.05em;
	color: $muted;
}

/* --- tables --------------------------------------------------------- */

table {
	width: 100%;
	border-collapse: collapse;
	margin: 8pt 0 16pt;
}
th, td {
	padding: 6pt 8pt;
	border-bottom: 0.75pt solid $line;
	text-align: left;
	vertical-align: top;
}
thead th {
	background: $primary;
	color: #ffffff;
	font-weight: 600;
	border-bottom: none;
}

/* Right-align a numeric column/cell. */
.num {
	text-align: right;
}

/* Alternating body-row shading - opt a table in with class="zebra". */
table.zebra tbody tr:nth-child(even) td,
table.zebra tbody tr:nth-child(even) th {
	background: $tint;
}

/* A subtotal/group-total row - put class="grouped" on the <tr>. */
tr.grouped td,
tr.grouped th {
	font-weight: 700;
	background: $tint;
	border-top: 1.5pt solid $primary;
	border-bottom: 0.75pt solid $primary;
}

/* Tree-structured rows: indent the label cell by nesting depth. */
$indent_rules

/* A negative value - color only; the tool supplies "(1,234)"-style text. */
.neg {
	color: $red;
}

/* RAG status - safe directly on a <td>/<th>, or on a nested <span> chip. */
.rag-red, .rag-amber, .rag-green {
	padding: 2pt 8pt;
	border-radius: 3pt;
	font-weight: 600;
	font-size: 9.5pt;
	white-space: nowrap;
}
.rag-red { background: $red_bg; color: $red; }
.rag-amber { background: $amber_bg; color: $amber; }
.rag-green { background: $green_bg; color: $green; }

/* --- callout ---------------------------------------------------------- */

.callout {
	border-left: 4pt solid $primary;
	background: $tint;
	padding: 10pt 14pt;
	margin: 10pt 0;
}
.callout h1, .callout h2, .callout h3,
.callout h4, .callout h5, .callout h6 {
	margin-top: 0;
}

/* --- kpi tile ----------------------------------------------------------- */

.kpi-tile {
	display: inline-block;
	min-width: 120pt;
	padding: 12pt 16pt;
	margin: 0 8pt 8pt 0;
	border: 0.75pt solid $line;
	border-radius: 4pt;
	background: #ffffff;
	vertical-align: top;
}
.kpi-tile .kpi-value {
	display: block;
	font-size: 22pt;
	font-weight: 700;
	color: $dark;
}
.kpi-tile .kpi-label {
	display: block;
	font-size: 9pt;
	color: $muted;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	margin-top: 2pt;
}
.kpi-tile .kpi-delta {
	display: inline-block;
	font-size: 9.5pt;
	font-weight: 600;
	margin-top: 4pt;
	color: $green;
}
.kpi-tile .kpi-delta.neg {
	color: $red;
}

/* --- cover / section pages ----------------------------------------------- */

.cover {
	page-break-after: always;
	height: 100%;
	padding-top: 200pt;
	text-align: center;
	background: $dark;
	color: #ffffff;
}
.cover h1, .cover h2, .cover h3 {
	color: #ffffff;
}
.cover p {
	color: #f2f2f2;
}

.section-divider {
	page-break-before: always;
	border-top: 2pt solid $primary;
	padding-top: 14pt;
	margin: 0 0 12pt;
	color: $dark;
}

/* --- signature block -------------------------------------------------- */

.signature-block {
	margin-top: 40pt;
	padding-top: 10pt;
	border-top: 0.75pt solid $line;
}
.signature-block .signature-line {
	display: block;
	width: 220pt;
	margin-top: 28pt;
	padding-top: 4pt;
	border-top: 0.75pt solid $ink;
	font-size: 9.5pt;
	color: $muted;
}

/* --- CSS bar charts ------------------------------------------------------ */

.bar-chart {
	margin: 10pt 0 16pt;
}
/* One row = label | track (holds the bar) | value, kept on a single line so the
   text never overlaps the bar (nowrap; the three inline-blocks sum to <100%). */
.bar-chart .bar-row {
	margin-bottom: 6pt;
	white-space: nowrap;
}
.bar-chart .bar-label {
	display: inline-block;
	width: 26%;
	font-size: 9.5pt;
	color: $muted;
	vertical-align: middle;
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.bar-chart .bar-track {
	display: inline-block;
	width: 52%;
	height: 14pt;
	background: $tint;
	vertical-align: middle;
	overflow: hidden;
}
.bar-chart .bar-value {
	display: inline-block;
	width: 20%;
	font-size: 9.5pt;
	text-align: right;
	vertical-align: middle;
	white-space: nowrap;
}
/* Color/height only - the tool sets `width` per bar via inline style. The bar
   fills the track height and is empty (label/value live in their own columns). */
.bar {
	display: block;
	height: 14pt;
	background: $primary;
}
"""
)


def component_css() -> str:
	"""Return the branded component stylesheet (see module docstring for the
	full class-name contract).

	Pure string composition - no I/O, no ``frappe``. Deterministic: two calls
	return identical text, so it is safe to call once and cache (``THEME_CSS``
	does exactly that) or call per-render if a future per-tenant token layer
	needs to vary the substitution inputs.
	"""
	indent_rules = "\n".join(
		f".indent-{level} {{ padding-left: {level * _INDENT_STEP_PT}pt; }}" for level in _INDENT_LEVELS
	)
	return _CSS_TEMPLATE.substitute(
		font=_FONT,
		ink=_INK,
		dark=_DARK,
		muted=_MUTED,
		primary=_PRIMARY,
		line=_LINE,
		tint=_TINT,
		red=_RED,
		red_bg=_RED_BG,
		amber=_AMBER,
		amber_bg=_AMBER_BG,
		green=_GREEN,
		green_bg=_GREEN_BG,
		indent_rules=indent_rules,
	)


THEME_CSS: str = component_css()
