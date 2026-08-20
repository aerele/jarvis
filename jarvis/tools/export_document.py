"""Render composed content (a report/summary the agent assembled, not a single
DocType record) into a downloadable File — PDF, standalone HTML, or a PNG image.

This is the write-side twin of ``export_excel`` for prose/tabular *documents*.
``download_pdf`` only prints one existing record through a Print Format; a
report the agent composed from many queries has no record to print, so without
this the agent hand-builds the file with ``exec``/``browser`` and it never
reaches the user.

Two rendering paths share this one entry point:

  * The PLAIN path (the original, in-prod behaviour) — a runaway-guarded
    Markdown/HTML → ``get_pdf`` render with a minimal built-in stylesheet. It is
    taken whenever NO rich kwarg is truthy, and its behaviour is byte-preserved
    (same sanitizer, same shell, same ``save_file``) so every legacy call is
    unaffected. See ``_render_plain`` and the sanitizer note below.
  * The RICH path (Slice 1) — a branded, class-styled document rendered through
    the hard-timed direct-wkhtmltopdf pipeline (``_export/document``). It is
    opt-in: passing any truthy rich kwarg (``theme``/``letterhead``/``header``/
    ``footer``/``watermark``/``charts``) switches to it. See ``_render_rich``.

Security (PLAIN path): ``content`` is agent-composed, i.e. effectively
LLM-controlled text — it must never reach the HTML unsanitized. Two distinct
threats, both closed at one choke point:

  * XSS in the standalone HTML output — ``<script>``, inline event handlers,
    ``javascript:`` hrefs.
  * SSRF-via-render — ``get_pdf`` runs wkhtmltopdf, which FETCHES any
    ``<img src>`` / ``<link href>`` / SVG ``<image href>`` it finds at render
    time, server-side. An agent-composed ``<img src="http://169.254.169.254/…">``
    (cloud metadata) or ``file:///etc/passwd`` would make the PDF renderer
    issue that request. The plain path supports no embedded images, so every
    fetch-capable tag is stripped outright rather than merely attribute-filtered.

Two layers, mirroring the pattern ``frappe.utils.html_utils`` already uses for
``<script>``/``<style>`` (``clean_script_and_style``, a BeautifulSoup decompose
pass ahead of ``bleach.clean``):

  1. ``_strip_unsafe_tags`` — a BeautifulSoup decompose pass that REMOVES the
     fetch-capable tags outright (``img``/``svg``/``image``/``link``/``meta``/
     ``style``), plus ``<script>``. Bleach's own default behaviour for a tag
     outside its allowlist is to *escape* it into visible garbled text, not
     remove it — safe (nothing executes or fetches) but ugly in a document
     meant to read as a clean report, and it would leave a stripped
     ``<script>``'s payload sitting as visible inert text. Decomposing first
     gives a clean removal instead.
  2. ``frappe.utils.sanitize_html`` (bleach-based) on what remains — blocks
     event-handler attributes and non-``{cid,http,https,mailto}`` protocols
     as part of its base allowlist (belt-and-suspenders once ``<script>`` and
     every fetch-capable tag are already gone).

Applied identically to BOTH the Markdown-rendered branch and the
``content_is_html=True`` raw-HTML branch — the flag changes how ``content``
becomes HTML, never whether the result gets sanitized. The rich path uses its
own, tighter ``sanitize_rich`` (a direct ``nh3.clean``); see that module.
"""

import contextlib
import html as _html
import re
import time
from collections.abc import Mapping

import frappe
from frappe.utils.html_utils import sanitize_html

from jarvis import telemetry
from jarvis.exceptions import InvalidArgumentError, NoDataError
from jarvis.tools._export import save_export_file
from jarvis.tools._export.document.furniture import (
	build_brand_header,
	build_title_block,
	normalize_geometry,
	render_pdf,
	resolve_brand,
)
from jarvis.tools._export.document.graphics import css_bar
from jarvis.tools._export.document.sanitizer import sanitize_rich
from jarvis.tools._export.document.theme import component_css

_FORMATS = {
	"pdf": ("pdf", "application/pdf"),
	"html": ("html", "text/html"),
	"png": ("png", "image/png"),
}

# Every tag capable of triggering an out-of-band fetch when wkhtmltopdf renders
# the page, plus <script> — decomposed here (not left to sanitize_html's own
# allowlist filtering) so its content disappears cleanly instead of surviving
# as inert escaped text sitting visibly in the rendered document. A duplicate
# <html>/<head>/<body> in the caller's content is not a separate risk to name
# here: the HTML5 tree-construction algorithm (which html5lib implements)
# already folds a second html/head/body into the single real one rather than
# nesting it — verified empirically before relying on it.
_UNSAFE_TAGS = {"img", "image", "svg", "link", "meta", "style", "script"}

# A runaway model must not be able to hand wkhtmltopdf an unbounded document.
_MAX_CONTENT_CHARS = 200_000

# A document with dozens of charts is either a bug or an attempt to blow the
# render past its wall-clock budget; cap it loudly rather than let the render
# time out. CSS bars are cheap, so this ceiling is generous.
_MAX_CHARTS = 20

# Chart COUNT is capped above, but a single chart with millions of rows would
# still inflate the spliced body without limit (each row is a <div>), defeating
# _MAX_CONTENT_CHARS. Cap total rows across all charts too.
_MAX_TOTAL_CHART_ROWS = 2_000

# Hard wall-clock bound handed to the rich renderer's subprocess watchdog. Kept
# under the plugin's 30s call_tool abort so the bench kills a runaway render
# before the client gives up (a number, not "<30s").
_RENDER_TIMEOUT_S = 25

# {{chart:N}} placeholder token the agent drops into ``content`` where a chart
# should appear; the tool splices its own generated HTML in at that spot AFTER
# sanitize (so the tool-controlled markup is trusted). This is the reusable
# splice mechanism Slice 2 extends for images/QR/logos. Whitespace after the
# colon is tolerated (a common LLM formatting variance); the index is capped at
# 3 digits so a pathological ``{{chart:<190k digits>}}`` can never reach int().
_CHART_TOKEN_RE = re.compile(r"\{\{chart:\s*(\d{1,3})\s*\}\}")


def export_document(
	content: str,
	format: str = "pdf",
	title: str | None = None,
	content_is_html: bool = False,
	*,
	theme: bool = False,
	letterhead: str | bool | None = None,
	header: str | None = None,
	footer: str | None = None,
	watermark: str | None = None,
	page_size: str = "A4",
	orientation: str = "portrait",
	margins_mm: int = 15,
	page_numbers: bool = True,
	charts: list | None = None,
	subtitle: str | None = None,
	meta: str | None = None,
	cover: bool | None = None,
) -> dict:
	"""Render ``content`` and return ``{file_url, filename, title, mime_type,
	size_bytes, name}`` for a private downloadable File (the rich path also adds
	``notes``).

	``content`` is the composed document — Markdown by default (tables, headings,
	lists all render), or raw HTML when ``content_is_html`` is set. ``format`` is
	``"pdf"`` (default), ``"html"``, or ``"png"`` (a single image of the rendered
	pages, stacked). ``title`` names the file + document.

	DUAL PATH. With no truthy rich kwarg this is the original plain render —
	sanitized (see the module docstring), image-free by design. Passing any truthy
	rich kwarg switches to the branded rich path:

	  * ``theme`` — opt into the branded rich path. (The branded component
	    stylesheet is applied to EVERY rich render regardless; ``theme`` is one of
	    the activators, not an independent on/off switch for the CSS.)
	  * ``letterhead`` — a ``Letter Head`` name (str) to brand with, or ``True`` to
	    opt into the rich path without naming one. Branding is AUTOMATIC on the rich
	    path via ``resolve_brand``: the default (or named) Letter Head logo, else the
	    default Company's name + primary address. Logo images are read-permission-
	    checked and base64-inlined; remote images are dropped (never fetched).
	  * ``title`` / ``subtitle`` / ``meta`` — the tool builds a clean title masthead
	    from these (the agent never hand-writes a title, so raw Markdown can't leak).
	    ``cover`` forces (``True``) / suppresses (``False``) a full cover page; ``None``
	    auto-picks (a full cover only for a long, structured document).
	  * ``header`` / ``watermark`` — page furniture (PDF only). ``footer`` is a short
	    TEXT line at the bottom-left; page numbers always render bottom-right, so a
	    footer and page numbers coexist (branding rides the running header).
	  * ``page_size`` / ``orientation`` / ``margins_mm`` / ``page_numbers`` — page
	    geometry (consumed by the rich path; they do NOT by themselves trigger it).
	  * ``charts`` — a list of CSS-bar specs (``{"rows": [...]}``); reference each
	    with a ``{{chart:N}}`` token in ``content`` and it is spliced in after
	    sanitize. A token with no spec (or a spec with no token) is reported in
	    ``notes`` rather than crashing.

	Falsy rich kwargs (``header=""`` / ``charts=[]`` / ``theme=False`` /
	``letterhead=None``) do NOT flip to the rich path — empty is not present.

	SLICE-1 SCOPE (this release): rich means branding via **letterhead + CSS
	components + CSS bar charts** only. No embedded images — no matplotlib/image
	charts, no QR, no base64 logos (those are Slice 2). ``fmt_amount``/``fmt_pct``
	from ``_export.document.graphics`` return small HTML fragments meant for DIRECT
	embedding into ``content`` (e.g. a table cell); do NOT pass their output
	through ``kpi_tile``/``css_bar``, whose text escaping would double-escape the
	fragment's span.

	OUT OF SCOPE — HARD WALLS (fail loud / degrade, never silently pretend): tagged
	PDF / PDF-UA accessibility, PDF encryption + digital signatures, prepress CMYK
	/ bleed, full right-to-left text shaping, row-level keep-together, widow/orphan
	control, automatic hyphenation, and interactive AcroForm / embedded JavaScript.
	Do not promise any of these to the user.
	"""
	fmt = (format or "pdf").lower()
	rich = _rich_requested(theme, letterhead, header, footer, watermark, charts, title, subtitle, meta, cover)
	try:
		_guard_content(content)
		if fmt not in _FORMATS:
			raise InvalidArgumentError(f"format must be one of {sorted(_FORMATS)}")
		if rich:
			env = _render_rich(
				content,
				fmt,
				title,
				content_is_html,
				letterhead=letterhead,
				header=header,
				footer=footer,
				watermark=watermark,
				page_size=page_size,
				orientation=orientation,
				margins_mm=margins_mm,
				page_numbers=page_numbers,
				charts=charts,
				subtitle=subtitle,
				meta=meta,
				cover=cover,
			)
			outcome = "degraded" if env["notes"] else "ok"
		else:
			env = _render_plain(content, fmt, title, content_is_html)
			outcome = "ok"
	except (NoDataError, InvalidArgumentError) as exc:
		# Emit on the fail-closed exits too (export_document previously emitted no
		# telemetry at all — this closes that gap on both success and failure).
		telemetry.record_export_event(
			tool="export_document",
			fmt=fmt,
			rows=0,
			mode="sync",
			outcome=_outcome_for(exc),
			rich=rich,
			detail=_short_err(exc),
		)
		raise
	except Exception as exc:
		# Any OTHER exception (an unforeseen input, a bug) must not escape as a raw
		# 500 with no telemetry — that was the observability blind spot the review
		# found. Log it, record it, and re-raise a clean error. Logging is
		# best-effort: it must never REPLACE the real error (nor break a no-site
		# unit test), so it is suppressed.
		with contextlib.suppress(Exception):
			# Plain traceback only — NOT with_context, which dumps local variable
			# VALUES (the agent's document content, an inlined base64 logo) into the
			# operator-readable Error Log.
			frappe.log_error(
				title="jarvis.export_document: unexpected render failure",
				message=frappe.get_traceback(),
			)
		telemetry.record_export_event(
			tool="export_document",
			fmt=fmt,
			rows=0,
			mode="sync",
			outcome="rejected",
			rich=rich,
			detail=_short_err(exc),
		)
		raise InvalidArgumentError(
			"The document could not be generated due to an unexpected error. "
			"Simplify the content or charts and try again."
		) from exc
	telemetry.record_export_event(
		tool="export_document", fmt=fmt, rows=0, mode="sync", outcome=outcome, rich=rich
	)
	return env


def _guard_content(content: str) -> None:
	"""Shared front-gate for both paths: non-empty string within the char cap."""
	if not isinstance(content, str) or not content.strip():
		raise NoDataError("No content to export.")
	if len(content) > _MAX_CONTENT_CHARS:
		raise InvalidArgumentError(f"content exceeds {_MAX_CONTENT_CHARS} characters")


# A masthead line is a short heading, not a document; bound it so a runaway model
# can't hand build_title_block a megabyte "title" (parity with the furniture caps).
_MAX_MASTHEAD_CHARS = 2_000


def _guard_masthead(title, subtitle, meta) -> None:
	"""Bound the tool-built masthead fields (title/subtitle/meta)."""
	for label, value in (("title", title), ("subtitle", subtitle), ("meta", meta)):
		if isinstance(value, str) and len(value) > _MAX_MASTHEAD_CHARS:
			raise InvalidArgumentError(f"{label} exceeds {_MAX_MASTHEAD_CHARS} characters")


def _rich_requested(
	theme, letterhead, header, footer, watermark, charts, title, subtitle, meta, cover
) -> bool:
	"""Branch on TRUTHY (non-empty) rich kwargs. Geometry kwargs (page_size /
	orientation / margins_mm / page_numbers) have non-falsy defaults and are NOT
	activators — only these intent kwargs flip the path, and only when truthy, so
	``header=""`` / ``charts=[]`` / ``theme=False`` / ``letterhead=None`` /
	``cover=False`` stay plain.

	``title`` / ``subtitle`` / ``meta`` / ``cover`` ALSO activate: the tool builds a
	masthead from the title, and the persona no longer hand-writes the title into
	Markdown, so a titled export MUST render branded (title-only on the plain path
	would show no title at all). A bare content-only call (no title, no rich kwarg)
	stays on the byte-preserved plain path."""
	return bool(
		theme or letterhead or header or footer or watermark or charts or title or subtitle or meta or cover
	)


def _render_rich(
	content: str,
	fmt: str,
	title: str | None,
	content_is_html: bool,
	*,
	letterhead,
	header,
	footer,
	watermark,
	page_size,
	orientation,
	margins_mm,
	page_numbers,
	charts,
	subtitle,
	meta,
	cover,
) -> dict:
	"""The branded rich pipeline: md→HTML → promote-alignment → sanitize →
	post-sanitize chart splice → emptiness guard → resolve brand → tool-built
	masthead → theme-wrap → render/compose → save. Returns the download envelope
	plus a ``notes`` list carrying every degrade."""
	start = time.monotonic()
	charts = charts or []
	if len(charts) > _MAX_CHARTS:
		raise InvalidArgumentError(f"charts exceeds the {_MAX_CHARTS}-chart limit")
	_guard_chart_rows(charts)
	notes: list[str] = []

	# Validate page geometry up front so BOTH the PDF and HTML paths reject a bad
	# page_size/orientation/margin identically (previously only render_pdf, on the
	# PDF path, validated it — the HTML path silently degraded to a fallback).
	page_size, orientation, margins_mm = normalize_geometry(page_size, orientation, margins_mm)
	# Bound the masthead text (parity with the header/footer/watermark furniture caps).
	_guard_masthead(title, subtitle, meta)

	# 1. markdown → HTML BEFORE sanitize (else ![](…)/autolinks regenerate <img>/
	#    links post-strip); raw HTML is passed straight to the sanitizer. Promote
	#    md's aligned columns (|--:| right, |:-:| center) to class="num"/"text-center"
	#    while the inline style is still present — sanitize strips the style but keeps
	#    the class, so the alignment (and its header) survive.
	body = content if content_is_html else _promote_table_alignment(frappe.utils.md_to_html(content))
	# 2. sanitize the full agent body.
	body = sanitize_rich(body)
	# 3. splice tool-generated chart HTML into {{chart:N}} tokens (AFTER sanitize —
	#    this markup is tool-controlled, so it is trusted).
	body = _splice_charts(body, charts, notes)
	# 4. emptiness guard on the BODY: never emit a blank PDF. The tool-built
	#    masthead (below) is furniture, not content — a title alone is not a
	#    document — so this checks the body's visible text, not the masthead.
	if not _has_visible_content(body):
		raise NoDataError("No content to export after sanitization.")
	# 5. resolve the brand once (default Letter Head logo → else company name +
	#    address), for the masthead and the running header. Fail-safe.
	brand = resolve_brand(letterhead if isinstance(letterhead, str) else None)
	if brand.get("note"):
		notes.append(brand["note"])
	brand_header = build_brand_header(brand)
	# 6. tool-built masthead prepended to the body (trusted, class-only, escaped —
	#    raw markdown can never leak into the title the way it did when the agent
	#    hand-wrapped markdown in <div class="cover">). Full cover only for a
	#    long/formal document; a scalable title block otherwise.
	full_cover = _decide_cover(cover, body)
	title_block = build_title_block(title, subtitle, meta, brand, full_cover)
	# 7. theme CSS for the render's geometry. `header` reflects whether a running
	#    header exists (brand / agent header / watermark) so the full cover's height
	#    accounts for the reserved top margin and fills the page instead of spilling
	#    onto page 2 (the branded-cover overflow the review caught). The reservation
	#    is a wkhtmltopdf (PDF) margin concern; the HTML output paints no running
	#    header, so its cover uses the unreserved height.
	has_header = bool(brand_header or header or watermark)
	css = component_css(page_size, orientation, margins_mm, header=has_header and fmt != "html")

	if fmt == "html":
		# No wkhtmltopdf on the HTML path, so page furniture can't be applied —
		# surface that rather than silently dropping it (the brand/title masthead
		# DOES render here, so it is not in this note). page_numbers is excluded:
		# its default True would fire on every HTML render.
		if header or footer or watermark:
			notes.append("furniture (header/footer/watermark) applies only to PDF output")
		doc_title = frappe.utils.escape_html(title) if title else "Document"
		payload = (
			f"<!doctype html><html><head><meta charset='utf-8'>"
			f"<title>{doc_title}</title><style>{css}</style></head>"
			f"<body>{title_block}{body}</body></html>"
		).encode()
	else:
		styled_body = f"<style>{css}</style>{title_block}{body}"
		pdf_bytes = render_pdf(
			styled_body,
			page_size=page_size,
			orientation=orientation,
			margins_mm=margins_mm,
			header_html=header,
			footer_text=footer,
			watermark=watermark,
			brand_header=brand_header,
			page_numbers=page_numbers,
			# The 25s bound covers the whole render; subtract the pre-render work
			# (md→HTML, sanitize, splice) already spent so the total stays under
			# the plugin's 30s call_tool abort.
			timeout=max(5, _RENDER_TIMEOUT_S - int(time.monotonic() - start)),
		)
		payload = pdf_bytes if fmt == "pdf" else _pdf_to_png(pdf_bytes)

	if not payload:
		raise InvalidArgumentError(f"{fmt} rendering produced no content.")
	ext, mime = _FORMATS[fmt]
	env = save_export_file(f"document.{ext}", payload, title or "Document", mime)
	env["notes"] = notes
	return env


def _guard_chart_rows(charts: list) -> None:
	"""Bound total rows across all charts (chart COUNT is capped separately). A
	non-sized ``rows`` is skipped here and rejected per-chart later in the splice."""
	total = 0
	for spec in charts:
		rows = spec.get("rows", []) if isinstance(spec, Mapping) else []
		try:
			total += len(rows)
		except TypeError:
			continue
	if total > _MAX_TOTAL_CHART_ROWS:
		raise InvalidArgumentError(
			f"charts contain {total} rows, exceeding the {_MAX_TOTAL_CHART_ROWS}-row limit"
		)


def _has_visible_content(html_fragment: str) -> bool:
	"""True if the fragment would render something the user can see, for the
	emptiness guard — so a childless-tag skeleton (``<p></p>``) reads as empty but
	a real document (or a rendered chart with no text labels) does not.

	Cheap heuristic (not a sanitizer): strip tags, UNESCAPE entities (so a
	``&nbsp;``-only doc collapses to whitespace and is caught), and check for
	non-whitespace text — OR the presence of a spliced chart, whose colored bars
	render even when every row label/value is blank."""
	body = html_fragment or ""
	if 'class="bar-chart"' in body:
		return True
	text = _html.unescape(re.sub(r"<[^>]+>", "", body))
	return bool(text.strip())


# md_to_html emits inline `text-align:right|center` on a `|--:|`/`|:-:|` column's
# cells; the sanitizer strips inline style, so promote it to a surviving class.
_CELL_TAG_RE = re.compile(r"<(td|th)\b([^>]*)>", re.IGNORECASE)
_CELL_STYLE_RE = re.compile(r'\bstyle\s*=\s*"([^"]*)"', re.IGNORECASE)
_CELL_CLASS_RE = re.compile(r'\bclass\s*=\s*"([^"]*)"', re.IGNORECASE)
_ALIGN_RIGHT_RE = re.compile(r"text-align:\s*right", re.IGNORECASE)
_ALIGN_CENTER_RE = re.compile(r"text-align:\s*center", re.IGNORECASE)


def _promote_table_alignment(html_body: str) -> str:
	"""Rewrite an aligned md table cell's inline style to a surviving class:
	``text-align:right`` → ``class="num"``, ``text-align:center`` → ``class="text-center"``.

	Markdown's ``|--:|``/``|:-:|`` columns render as ``<td style="text-align:…">``;
	``sanitize_rich`` strips inline ``style`` (classes-only), which would drop the
	alignment (left-aligned columns are already the default, so they need nothing).
	Promoting it here — BEFORE sanitize — keeps the alignment AND its header aligned
	(theme's ``.num``/``.text-center`` cover ``<th>`` too). Only a cell that already
	carries the aligned style is touched; the sanitizer removes the style afterwards.
	A pre-existing ``class`` (double-quoted, as md emits) is merged, not duplicated.
	"""

	def _repl(match: "re.Match[str]") -> str:
		tag, attrs = match.group(1), match.group(2)
		style = _CELL_STYLE_RE.search(attrs)
		if not style:
			return match.group(0)
		if _ALIGN_RIGHT_RE.search(style.group(1)):
			promote = "num"
		elif _ALIGN_CENTER_RE.search(style.group(1)):
			promote = "text-center"
		else:
			return match.group(0)
		cls = _CELL_CLASS_RE.search(attrs)
		if cls:
			attrs = attrs[: cls.start(1)] + (cls.group(1) + " " + promote).strip() + attrs[cls.end(1) :]
		else:
			attrs = attrs + f' class="{promote}"'
		return f"<{tag}{attrs}>"

	return _CELL_TAG_RE.sub(_repl, html_body or "")


# A full cover page suits a long, formal document but is absurd on a one-page
# letter, so it is opt-in with a conservative auto: only when the body is BOTH
# long AND structured (a heading, or an explicit page break). ``cover`` overrides.
_COVER_MIN_CHARS = 6000
_STRUCTURE_RE = re.compile(r"<h[12]\b", re.IGNORECASE)
# Match `page-break` as a class TOKEN, so `class="section-divider page-break"`
# (page-break combined with any other class) counts, not just a sole class.
_PAGE_BREAK_RE = re.compile(r'class="[^"]*\bpage-break\b[^"]*"', re.IGNORECASE)


def _decide_cover(cover: bool | None, body: str) -> bool:
	"""Whether to render a full cover page. ``cover`` True/False forces it; ``None``
	is the auto heuristic (long AND structured), leaning to the clean title block."""
	if cover is not None:
		return bool(cover)
	text = _html.unescape(re.sub(r"<[^>]+>", "", body or ""))
	long_enough = len(text.strip()) >= _COVER_MIN_CHARS
	structured = bool(_STRUCTURE_RE.search(body or "")) or bool(_PAGE_BREAK_RE.search(body or ""))
	return long_enough and structured


def _short_err(exc: Exception) -> str:
	"""A short, log-safe one-liner for a failure's telemetry ``detail`` field."""
	return f"{type(exc).__name__}: {exc}"[:200]


def _splice_charts(body: str, charts: list, notes: list[str]) -> str:
	"""Replace each ``{{chart:N}}`` token with ``css_bar`` output for ``charts[N]``.

	Runs AFTER ``sanitize_rich`` so the tool-generated bar markup (which carries a
	single clamped ``width:N%`` inline style) is trusted and not stripped.

	The splice is TREE-AWARE, not a blind string replace: it walks TEXT NODES only
	(via a bounded ``html.parser`` parse of the already-sanitized, capped body), so
	a token that happens to sit inside an attribute value (e.g. an ``href``) or
	inside a ``<pre>``/``<code>`` block (documenting the syntax) is left alone
	rather than corrupting the tag or replacing a literal example. Each chart's
	markup is rendered once and inserted by index, so a token appearing inside a
	chart's own text can never trigger a second substitution.

	Degrades, never crashes: a malformed ``rows`` value, a spec whose token is
	absent, and a token with no matching spec are each reported in ``notes``. Orphan
	tokens are removed so no literal ``{{chart:N}}`` junk survives."""
	if not charts and _CHART_TOKEN_RE.search(body) is None:
		return body

	# Indices whose token appears ANYWHERE in the body (incl. a <code> example or
	# an attribute), so a chart referenced only in a literal example is not
	# mis-reported as "placeholder not found".
	referenced = {int(m.group(1)) for m in _CHART_TOKEN_RE.finditer(body)}

	# Render each spec ONCE (or degrade to a note), keyed by index.
	rendered: dict[int, str] = {}
	for i, spec in enumerate(charts):
		is_map = isinstance(spec, Mapping)
		rows = spec.get("rows", []) if is_map else []
		try:
			# Forward the spec's optional title/caption (the plugin schema advertises
			# them); css_bar escapes them and omits both when absent.
			rendered[i] = css_bar(
				rows,
				title=spec.get("title") if is_map else None,
				caption=spec.get("caption") if is_map else None,
			)
		except Exception:
			notes.append(f"chart {i} was provided but its rows were malformed and could not be rendered")
			rendered[i] = ""  # its token is removed rather than left as junk

	from bs4 import BeautifulSoup

	soup = BeautifulSoup(body, "html.parser")
	seen: set[int] = set()
	for node in list(soup.find_all(string=_CHART_TOKEN_RE)):
		if node.find_parent(["pre", "code"]):
			continue  # a token shown as a literal code example stays literal
		text = str(node)
		pieces: list[str] = []
		last = 0
		for m in _CHART_TOKEN_RE.finditer(text):
			idx = int(m.group(1))
			seen.add(idx)
			pieces.append(_html.escape(text[last : m.start()]))
			pieces.append(rendered.get(idx, ""))  # unknown index → drop token
			last = m.end()
		pieces.append(_html.escape(text[last:]))
		fragment = BeautifulSoup("".join(pieces), "html.parser")
		for child in list(fragment.contents):
			node.insert_before(child)
		node.extract()

	for i in rendered:
		if i not in referenced:
			notes.append(
				f"chart {i} was provided but its {{{{chart:{i}}}}} placeholder was not found in the content"
			)
	for idx in sorted(seen):
		if idx not in rendered:
			notes.append(f"placeholder {{{{chart:{idx}}}}} has no matching chart spec")
	return str(soup)


def _outcome_for(exc: Exception) -> str:
	"""Map a fail-closed exception to its telemetry outcome (NoDataError first, as
	it subclasses InvalidArgumentError)."""
	if isinstance(exc, NoDataError):
		return "no_data"
	return "rejected"


# Minimal, self-contained stylesheet so tables/headings read cleanly in every
# format (the HTML file opens standalone; the PDF/PNG render from the same CSS).
# PLAIN PATH ONLY — the rich path uses the branded component stylesheet instead.
_CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:12px;color:#1a1a1a;line-height:1.5;margin:28px;}
h1,h2,h3,h4{color:#111;margin:1.1em 0 .4em;line-height:1.25;}
h1{font-size:20px;} h2{font-size:16px;} h3{font-size:14px;}
table{border-collapse:collapse;width:100%;margin:.7em 0;}
th,td{border:1px solid #ccc;padding:5px 8px;text-align:left;font-size:11px;
  vertical-align:top;}
th{background:#f4f4f5;font-weight:600;}
code{background:#f4f4f5;padding:0 3px;border-radius:3px;font-size:.92em;}
ul,ol{margin:.4em 0 .4em 1.2em;} p{margin:.5em 0;}
"""


def _strip_unsafe_tags(html: str) -> str:
	"""Remove (not merely escape) every tag in ``_UNSAFE_TAGS``.

	Same technique ``frappe.utils.html_utils.clean_script_and_style`` already
	uses for ``<script>``/``<style>``: a BeautifulSoup decompose pass. Doing
	this ahead of ``sanitize_html`` gives a clean removal — bleach's own
	default for a disallowed tag is to escape it into visible text, which is
	safe but leaves garbled tag source sitting in the rendered document.

	``BeautifulSoup(html, "html5lib")`` always parses into a full document
	(wrapping bare content in its own ``<html><head></head><body>…</body></html>``,
	confirmed empirically), so this returns ``soup.body``'s inner HTML, not the
	whole parsed tree — ``html`` here is a fragment about to be spliced into
	this module's own page shell, not a full page in its own right.
	"""
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(html, "html5lib")
	for tag in soup(list(_UNSAFE_TAGS)):
		tag.decompose()
	return frappe.as_unicode(soup.body.decode_contents()) if soup.body else frappe.as_unicode(soup)


def _render_plain(content: str, fmt: str, title: str | None, content_is_html: bool) -> dict:
	"""The original plain render — byte-preserved so every legacy call is
	unchanged: same two-layer sanitizer, same self-contained shell, same
	``get_pdf``/``_pdf_to_png`` engines, same ``save_file`` (its own filename
	sanitizer + return shape, without a ``notes`` key)."""
	body = content if content_is_html else frappe.utils.md_to_html(content)
	body = _strip_unsafe_tags(body)
	body = sanitize_html(body)
	doc_title = frappe.utils.escape_html(title) if title else "Document"
	html = (
		f"<!doctype html><html><head><meta charset='utf-8'>"
		f"<title>{doc_title}</title><style>{_CSS}</style></head>"
		f"<body>{body}</body></html>"
	)

	if fmt == "html":
		payload = html.encode("utf-8")
	elif fmt == "pdf":
		from frappe.utils.pdf import get_pdf

		payload = get_pdf(html)
	else:  # png
		from frappe.utils.pdf import get_pdf

		payload = _pdf_to_png(get_pdf(html))

	if not payload:
		raise InvalidArgumentError(f"{fmt} rendering produced no content.")

	from frappe.utils.file_manager import save_file

	ext = _FORMATS[fmt][0]
	safe = (title or "document").replace(" ", "-").replace("/", "-")[:60] or "document"
	fdoc = save_file(f"{safe}.{ext}", payload, None, None, is_private=1)
	return {
		"file_url": fdoc.file_url,
		"filename": fdoc.file_name,
		"title": title or "Document",
		"mime_type": _FORMATS[fmt][1],
		"size_bytes": int(fdoc.file_size or len(payload)),
		"name": fdoc.name,
	}


def _pdf_to_png(pdf_bytes: bytes) -> bytes:
	"""Rasterize each PDF page (pypdfium2, the same engine get_file_pages uses)
	and stack them into one tall PNG so a multi-page report is a single image."""
	import io

	import pypdfium2 as pdfium
	from PIL import Image

	pdf = pdfium.PdfDocument(pdf_bytes)
	try:
		pages = [pdf[i].render(scale=2).to_pil().convert("RGB") for i in range(len(pdf))]
	finally:
		pdf.close()
	if not pages:
		return b""
	width = max(p.width for p in pages)
	canvas = Image.new("RGB", (width, sum(p.height for p in pages)), "white")
	y = 0
	for p in pages:
		canvas.paste(p, (0, y))
		y += p.height
	out = io.BytesIO()
	canvas.save(out, format="PNG")
	return out.getvalue()
