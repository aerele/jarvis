"""Branded component stylesheet for the rich-PDF document engine.

This is the visual layer the rich path (``furniture.render_pdf`` and the
``export_document`` orchestrator) wraps around ``sanitize_rich``'s output. It is
a **pure module** - no ``import frappe`` anywhere in it - so the stylesheet and
its class-name contract are unit-testable without a bench, the same purity
guarantee ``sanitizer.py`` makes for the same reason.

DESIGN GOAL: a general-purpose, content-agnostic, presentation-ready document
style. It must read well for ANY LLM-composed content - a letter, memo,
proposal, minutes, essay, checklist, one-pager, OR a long analysis with
charts/tables - not just a report. Quality comes from scale, weight, spacing,
measure and color, tuned for wkhtmltopdf; the report-specific components
(charts, KPI tiles, callouts) look sharp WHEN present and are simply absent
otherwise.

WHY three font stacks: a serif ``$font_display`` masthead over a neutral sans
``$font_text`` body is a classic editorial pairing; scoping the serif to the
title/cover caps availability risk. Actual font availability on the Linux
render container is only provable by the Frappe-Cloud render smoke - these are
graceful fallback stacks, and most of the typographic quality is in the scale
and rhythm, not the font file. ``$font_mono`` covers ``code``/``pre``.

WHY ``component_css(page_size, orientation, margins_mm)`` now takes geometry:
the full ``.cover`` fills the printable page height, which depends on the page
size and margins. The module stays pure - it only does arithmetic on the
numbers passed in (``_cover_height_pt``); it never touches ``frappe``. The
default call ``component_css()`` is unchanged (A4 / portrait / 15mm), so
``THEME_CSS`` and every existing caller keep working.

THE CONTRACT: every class name below is load-bearing. The number/RAG/chart
builders and the ``export_document`` / ``furniture`` HTML assembly emit these
EXACT class names, so renaming one here silently breaks a downstream caller.
New classes are free to add; existing names are not free to rename.

  * Typography: bare ``h1``..``h6`` + ``body`` + ``p``/``ul``/``ol``/``li``/
    ``blockquote``/``hr``/``a``/``code``/``pre``/``caption`` (the sanitizer
    allows these tags with only a ``class`` attribute).
  * Tables: bare ``table``/``th``/``td`` + ``.zebra`` + ``.grouped`` +
    ``.indent-1``..``.indent-5`` + ``.neg`` + ``.num`` (right-align, now on
    ``th`` too) + ``.rag-red`` / ``.rag-amber`` / ``.rag-green``.
  * ``.callout`` - a bordered card (distinct surface from zebra/subtotal).
  * ``.kpi-tile`` (+ ``.kpi-value`` / ``.kpi-label`` / ``.kpi-delta``).
  * ``.doc-title-block`` (+ ``.doc-title`` / ``.doc-subtitle`` / ``.doc-meta`` /
    ``.doc-brand`` / ``.brand-logo`` / ``.brand-name`` / ``.brand-addr``) - the
    tool-built masthead; ``.cover`` wraps it for a full title page.
  * ``.section-divider`` - a section rule (NO forced page break anymore).
  * ``.page-break`` - the explicit page-break utility (replaces the old
    section-divider auto-break).
  * ``.signature-block`` (+ ``.signature-line``).
  * ``.bar-chart`` (+ ``.bar-row`` / ``.bar-label`` / ``.bar-track`` /
    ``.bar-value`` / ``.bar`` / ``.bar-chart-title`` / ``.bar-chart-caption`` /
    ``.bar.series-1``..``.series-4``). ``.bar`` styles color/height ONLY - the
    tool sets ``width`` per bar via a tool-controlled inline style spliced in
    after ``sanitize_rich``.

WHY no flexbox/grid and no CSS custom properties (``var()``): the render engine
is wkhtmltopdf's patched-Qt WebKit (a pre-2013 WebKit fork) which does not
reliably support either. Every layout below uses block / inline-block / table
flow only (the full cover centers via ``display:table`` + ``table-cell`` +
``vertical-align:middle``), and every color/size is baked into the CSS text at
build time through ``string.Template`` ``$``-substitution rather than
``var(--x)``.

WHY one global ``-webkit-print-color-adjust:exact`` rule: wkhtmltopdf drops
print background fills without it; ``*`` is a strict superset of "anything with
a fill", and fill-less elements ignore the hint.
"""

from __future__ import annotations

from string import Template

# --- brand tokens (v1 default) --------------------------------------------

_FONT_TEXT = (
	'"Helvetica Neue", Helvetica, "Liberation Sans", Arial, "Segoe UI", Roboto, "DejaVu Sans", sans-serif'
)
_FONT_DISPLAY = 'Georgia, "Liberation Serif", "Noto Serif", "Times New Roman", serif'
_FONT_MONO = '"DejaVu Sans Mono", "Liberation Mono", Consolas, "Courier New", monospace'

_PRIMARY = "#1f4e79"  # accents, rules, chart bars, thead border
_DARK = "#132d47"  # headings, cover background
_MUTED = "#666666"  # captions, labels, footnotes
_INK = "#1a1a1a"  # body text
_LINE = "#d9d9d9"  # hairline borders
_TINT = "#eef2f7"  # thead / subtotal / chart-track tinted background
_ZEBRA = "#f5f7fa"  # zebra striping (lighter than tint, distinct from callout)
_CALLOUT_BG = "#eef3f9"  # callout card background (distinct from zebra/subtotal)

_RED = "#b3261e"
_RED_BG = "#fbe9e7"
_AMBER = "#8a5a00"
_AMBER_BG = "#fff4e0"
_GREEN = "#1e7d32"
_GREEN_BG = "#e6f4ea"

# Categorical chart palette for grouped/stacked series - distinct, readable on
# white: primary blue, teal, gold, plum.
_SERIES = ("#1f4e79", "#2e7d6b", "#a9791c", "#6b4a7a")

# Tree-row indent step; five levels (.indent-1..indent-5) generated below.
_INDENT_STEP_PT = 14
_INDENT_LEVELS = range(1, 6)

# Page geometry for the full-cover height computation. Portrait (width_pt,
# height_pt); 1pt = 1/72in, 1mm = 2.834645669pt.
_MM_TO_PT = 2.834645669
_PAGE_DIMENSIONS_PT = {
	"A5": (419.53, 595.28),
	"A4": (595.28, 841.89),
	"A3": (841.89, 1190.55),
	"LETTER": (612.0, 792.0),
	"LEGAL": (612.0, 1008.0),
	"TABLOID": (792.0, 1224.0),
}

# Extra top margin (mm) wkhtmltopdf reserves when a running header exists, so a
# logo / brand line never overlaps the body. Defined HERE (the pure module) so
# the ONE "+14mm when a header exists" rule is shared: furniture._build_args uses
# it for --margin-top, and cover_height_pt subtracts it so the full cover fills
# the ACTUAL printable area instead of overflowing onto page 2 (the branded-cover
# bug the review caught).
HEADER_RESERVE_MM = 14


def cover_height_pt(page_size: str, orientation: str, margins_mm: float, header: bool = False) -> float:
	"""Printable page height in pt = page_height - top_margin - bottom_margin,
	page-size + orientation aware. When ``header`` is True the top margin includes
	the ``HEADER_RESERVE_MM`` reservation (matching furniture._build_args), so a
	branded cover fills the page rather than spilling past ``page-break-after``.

	Unknown page size falls back to A4; ``page_size``/``orientation`` are coerced to
	str so a mistyped non-string arg degrades to the fallback instead of crashing;
	the result is floored at 100pt so a pathological margin can never make the cover
	collapse or go negative. Public (furniture.py reuses it to place the watermark)
	but still pure - arithmetic on the args only, no ``frappe``."""
	w, h = _PAGE_DIMENSIONS_PT.get(str(page_size or "A4").upper(), _PAGE_DIMENSIONS_PT["A4"])
	if str(orientation or "portrait").lower() == "landscape":
		w, h = h, w
	try:
		margin = max(0.0, float(margins_mm))
	except (TypeError, ValueError):
		margin = 15.0
	top = margin + (HEADER_RESERVE_MM if header else 0)
	usable = h - (top + margin) * _MM_TO_PT
	return round(max(usable, 100.0), 1)


_CSS_TEMPLATE = Template(
	"""
/* === Branded component stylesheet - see theme.py docstring for the full
   class-name contract and the wkhtmltopdf layout constraints. === */

* {
	-webkit-print-color-adjust:exact;
	print-color-adjust:exact;
}

body {
	font-family: $font_text;
	font-size: 10.5pt;
	line-height: 1.5;
	color: $ink;
}

/* --- headings: tighter, more refined scale WITH top margins for rhythm --- */

h1, h2, h3, h4, h5, h6 {
	font-weight: 700;
	color: $dark;
}
h1 { font-size: 22pt; line-height: 1.2;  margin: 0 0 8pt; }
h2 { font-size: 16pt; line-height: 1.25; margin: 20pt 0 7pt; }
h3 { font-size: 13pt; line-height: 1.3;  margin: 16pt 0 6pt; }
h4 { font-size: 11.5pt; line-height: 1.35; margin: 14pt 0 5pt; }
h5 { font-size: 11pt; line-height: 1.4; margin: 12pt 0 4pt; color: $dark; }
h6 {
	font-size: 9.5pt;
	line-height: 1.4;
	margin: 12pt 0 4pt;
	text-transform: uppercase;
	letter-spacing: 0.06em;
	color: $muted;
}
/* A doc/section opening on a heading gets no dead gap above it. */
h1:first-child, h2:first-child, h3:first-child,
h4:first-child, h5:first-child, h6:first-child { margin-top: 0; }

/* --- body prose (was untuned WebKit defaults) ------------------------- */

p { margin: 0 0 8pt; max-width: 34em; }
/* Cap lists to the same measure as prose so the right edge doesn't go ragged
   (a bullet list running full-width beside capped paragraphs reads unfinished). */
ul, ol { margin: 0 0 8pt; padding-left: 20pt; max-width: 34em; }
li { margin-bottom: 3pt; }
ul ul, ol ol, ul ol, ol ul { margin: 4pt 0 0; }
blockquote {
	margin: 10pt 0;
	padding: 2pt 0 2pt 14pt;
	border-left: 3pt solid $primary;
	color: #444444;
	max-width: 34em;
	page-break-inside: avoid;
}
hr { border: 0; border-top: 0.75pt solid $line; margin: 18pt 0; }
a { color: $primary; text-decoration: underline; }
strong, b { font-weight: 700; }
em, i { font-style: italic; }
code {
	font-family: $font_mono;
	font-size: 0.92em;
	background: $tint;
	padding: 1pt 3pt;
	border-radius: 2pt;
}
pre {
	font-family: $font_mono;
	font-size: 9pt;
	line-height: 1.4;
	background: $tint;
	border: 0.75pt solid $line;
	border-radius: 3pt;
	padding: 8pt 10pt;
	margin: 10pt 0;
	white-space: pre-wrap;
	page-break-inside: avoid;
}
pre code { background: transparent; padding: 0; }
caption { caption-side: top; text-align: left; font-size: 9pt; color: $muted; margin-bottom: 4pt; }

/* --- tables --------------------------------------------------------- */

table {
	width: 100%;
	border-collapse: collapse;
	margin: 8pt 0 16pt;
}
/* Repeat the header row across page breaks on long tables. */
thead { display: table-header-group; }
th, td {
	padding: 5pt 9pt;
	border-bottom: 0.75pt solid $line;
	text-align: left;
	vertical-align: top;
}
/* Lighter, universal header (a full navy fill on every 2-col table in a letter
   is too heavy); the primary rule under it carries the emphasis. */
thead th {
	background: $tint;
	color: $dark;
	font-weight: 700;
	border-bottom: 1.5pt solid $primary;
}

/* Right-align a numeric column/cell - now on the <th> too, so a numeric header
   lines up with its column instead of hanging left. */
.num, th.num, td.num, thead th.num {
	text-align: right;
}
/* Center a column/cell (md's |:---:| centered column promotes to this). */
.text-center, th.text-center, td.text-center, thead th.text-center {
	text-align: center;
}

/* Alternating body-row shading - opt a table in with class="zebra". Lighter
   than $tint so it stays distinct from the callout/subtotal surface. */
table.zebra tbody tr:nth-child(even) td,
table.zebra tbody tr:nth-child(even) th {
	background: $zebra;
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

/* --- callout: a bordered card, distinct from a shaded table row ------- */

.callout {
	border: 0.75pt solid $line;
	border-left: 3pt solid $primary;
	background: $callout_bg;
	padding: 10pt 14pt;
	margin: 12pt 0;
	page-break-inside: avoid;
}
.callout h1, .callout h2, .callout h3,
.callout h4, .callout h5, .callout h6 {
	margin-top: 0;
}
.callout p:last-child { margin-bottom: 0; }

/* --- kpi tile ----------------------------------------------------------- */

.kpi-tile {
	display: inline-block;
	min-width: 120pt;
	padding: 12pt 16pt;
	margin: 0 8pt 8pt 0;
	border: 0.75pt solid $line;
	border-top: 2pt solid $primary;
	border-radius: 4pt;
	background: #ffffff;
	vertical-align: top;
	page-break-inside: avoid;
}
.kpi-tile .kpi-value {
	display: block;
	font-size: 20pt;
	font-weight: 700;
	color: $dark;
}
.kpi-tile .kpi-label {
	display: block;
	font-size: 8.5pt;
	color: $muted;
	text-transform: uppercase;
	letter-spacing: 0.05em;
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

/* --- title block (default masthead) + full cover (opt-in) --------------- */

.doc-title-block {
	margin: 0 0 24pt;
	padding-bottom: 12pt;
	border-bottom: 1.5pt solid $primary;
}
.doc-brand { margin: 0 0 12pt; }
.doc-brand .brand-logo img { height: 34pt; width: auto; max-width: 100%; }
.doc-brand .brand-name { font-weight: 700; font-size: 12pt; color: $dark; }
.doc-brand .brand-addr { font-size: 9pt; color: $muted; line-height: 1.35; }
.doc-title {
	font-family: $font_display;
	font-size: 28pt;
	font-weight: 700;
	line-height: 1.15;
	color: $dark;
	margin: 0;
}
.doc-subtitle { font-size: 13pt; color: $muted; margin: 6pt 0 0; }
.doc-meta { font-size: 9.5pt; color: $muted; margin: 8pt 0 0; }

/* Full cover: a printable-page-height panel, centered via table-cell (no
   flexbox). Height is computed for the render's page size/orientation/margins,
   so it fills the page instead of collapsing (the old height:100% bug). True
   bleed-to-paper-edge is impossible under uniform non-zero margins; L/R padding
   fixes the old edge-bleed. */
.cover {
	display: table;
	width: 100%;
	height: ${cover_height}pt;
	page-break-after: always;
	background: $dark;
	-webkit-print-color-adjust: exact;
	print-color-adjust: exact;
}
.cover .cover-inner {
	display: table-cell;
	vertical-align: middle;
	text-align: center;
	padding: 0 40pt;
	color: #ffffff;
}
.cover .doc-title-block { border: 0; margin: 0; padding: 0; }
.cover .doc-title { font-family: $font_display; color: #ffffff; font-size: 34pt; }
.cover .doc-subtitle, .cover .doc-meta { color: #f2f2f2; }
.cover .doc-brand .brand-name { color: #ffffff; }
.cover .doc-brand .brand-addr { color: #f2f2f2; }

/* --- section rule + explicit page break -------------------------------- */

/* A section header rule - NO forced page break (use .page-break for that). */
.section-divider {
	border-top: 2pt solid $primary;
	padding-top: 14pt;
	margin: 22pt 0 12pt;
	color: $dark;
}
/* Force the next content onto a new page. */
.page-break { page-break-before: always; }

/* --- signature block -------------------------------------------------- */

.signature-block {
	margin-top: 40pt;
	padding-top: 10pt;
	border-top: 0.75pt solid $line;
	page-break-inside: avoid;
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
.bar-chart-title { font-size: 10pt; font-weight: 700; color: $dark; margin: 0 0 6pt; }
.bar-chart-caption { font-size: 9pt; color: $muted; margin: 4pt 0 0; }
/* One row = label | track (holds the bar) | value, kept on a single line so the
   text never overlaps the bar (nowrap; the three inline-blocks sum to 100%). */
.bar-chart .bar-row {
	margin-bottom: 6pt;
	white-space: nowrap;
	page-break-inside: avoid;
}
.bar-chart .bar-label {
	display: inline-block;
	width: 28%;
	font-size: 9.5pt;
	color: $muted;
	vertical-align: middle;
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.bar-chart .bar-track {
	display: inline-block;
	width: 50%;
	height: 14pt;
	background: $tint;
	vertical-align: middle;
	overflow: hidden;
	border-bottom: 0.75pt solid $line;
	border-right: 0.75pt solid $line;
}
.bar-chart .bar-value {
	display: inline-block;
	width: 22%;
	font-size: 9.5pt;
	text-align: right;
	vertical-align: middle;
	white-space: nowrap;
}
/* Color/height only - the tool sets `width` per bar via inline style. */
.bar {
	display: block;
	height: 13pt;
	background: $primary;
	border-bottom: 1pt solid $dark;
}
.bar.series-1 { background: $s1; }
.bar.series-2 { background: $s2; }
.bar.series-3 { background: $s3; }
.bar.series-4 { background: $s4; }
"""
)


def component_css(
	page_size: str = "A4", orientation: str = "portrait", margins_mm: float = 15, header: bool = False
) -> str:
	"""Return the branded component stylesheet (see module docstring for the full
	class-name contract).

	Pure string composition - no I/O, no ``frappe``. Deterministic for a given
	``(page_size, orientation, margins_mm, header)``. The geometry only affects the
	full ``.cover`` height (``cover_height_pt``); every other rule is fixed.
	``header`` must reflect whether the render will have a running header (brand /
	agent header / watermark), so the cover height accounts for the reserved top
	margin. The default call ``component_css()`` (A4 / portrait / 15mm / no header)
	is unchanged, so ``THEME_CSS`` and legacy no-arg callers keep working.
	"""
	indent_rules = "\n".join(
		f".indent-{level} {{ padding-left: {level * _INDENT_STEP_PT}pt; }}" for level in _INDENT_LEVELS
	)
	return _CSS_TEMPLATE.substitute(
		font_text=_FONT_TEXT,
		font_display=_FONT_DISPLAY,
		font_mono=_FONT_MONO,
		ink=_INK,
		dark=_DARK,
		muted=_MUTED,
		primary=_PRIMARY,
		line=_LINE,
		tint=_TINT,
		zebra=_ZEBRA,
		callout_bg=_CALLOUT_BG,
		red=_RED,
		red_bg=_RED_BG,
		amber=_AMBER,
		amber_bg=_AMBER_BG,
		green=_GREEN,
		green_bg=_GREEN_BG,
		s1=_SERIES[0],
		s2=_SERIES[1],
		s3=_SERIES[2],
		s4=_SERIES[3],
		indent_rules=indent_rules,
		cover_height=cover_height_pt(page_size, orientation, margins_mm, header=header),
	)


THEME_CSS: str = component_css()
