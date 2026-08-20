"""Page furniture + the rich-PDF render call (the 2nd, sanitized render path).

``render_pdf`` turns an *already-sanitized* body-HTML fragment into PDF bytes on
wkhtmltopdf, wrapping it with header/footer/watermark/letterhead furniture and a
real, hard render timeout.

WHY the binary is called DIRECTLY via ``subprocess.run`` (deviation from the
plan's "pdfkit.from_string + patched FrappePDFKit"): ``pdfkit.from_string`` shells
out with ``subprocess.communicate()`` and NO timeout, so a runaway render cannot
be aborted - it pins the RQ worker well past the plugin's 30s ``call_tool`` abort
and leaves an orphaned File. ``subprocess.run([...], timeout=...)`` gives a REAL
hard bound: on ``TimeoutExpired`` the child process is killed and no partial
output is returned. A bonus: because we build the exact wkhtmltopdf arg list here,
pdfkit's ``<meta name="pdfkit-...">`` option-injection vector does not exist at
all, so the ``FrappePDFKit`` monkeypatch is unnecessary (and the sanitizer already
strips ``<meta>`` regardless).

WHY header/footer/watermark are sanitized here too: they are as agent-influenced
as the body, and wkhtmltopdf will FETCH any ``<img src>``/``<link>`` it finds in a
``--header-html``/``--footer-html`` document (a *separate* render), so each runs
through ``sanitize_rich`` before it is written to a temp file.

WHY the letterhead is resolved OUTSIDE this module (``resolve_letterhead``, called
from ``export_document._render_rich`` where the ``notes`` channel lives): a
letterhead is folded in as trusted operator config, but "trusted" must be
ENFORCED, not assumed - so resolution (a) defaults to the site's default Letter
Head when the caller named none, (b) checks the impersonated user's READ
permission on the Letter Head, and (c) neutralises its images: an ``<img>`` with a
remote ``src`` is DROPPED (wkhtmltopdf would fetch it server-side - the SSRF the
whole rich path exists to prevent), and a same-site ``/files/`` logo is inlined as
a permission-checked base64 ``data:`` URI (which also makes it actually render -
the body is fed via stdin with no base URL, so a relative ``src`` would not
resolve otherwise). ``render_pdf`` then receives the resolved, safe HTML.

WHY the watermark lives in the HEADER template, not the body: wkhtmltopdf only
paints a body ``position:fixed`` element on page 1, whereas the header document is
composited onto EVERY page, so a rotated, absolutely-positioned block placed in
the header repeats across all pages.

Testability: wkhtmltopdf is absent in local dev (brew removed it) - the real
render is the Frappe Cloud smoke gate, not a unit test. The unit suite mocks
``subprocess.run`` and asserts the arg list, the sanitized temp-file contents, and
the temp-file lifecycle. See ``jarvis/tests/test_export_document_furniture.py``.
"""

from __future__ import annotations

import base64
import contextlib
import html
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile

import frappe
import pdfkit

from jarvis.exceptions import InvalidArgumentError

from .sanitizer import sanitize_letterhead, sanitize_rich
from .theme import cover_height_pt

_SANS = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'

# Header/footer/watermark are agent-influenced; cap them so a runaway model can't
# hand the pre-render sanitize an unbounded fragment (the body itself is capped in
# export_document; these are the other agent-controlled inputs).
_MAX_FURNITURE_CHARS = 20_000

# A letterhead logo is small operator config; refuse to inline anything larger so
# a base64 blob can't bloat the header document.
_MAX_LOGO_BYTES = 2_000_000

# Page sizes we forward to wkhtmltopdf (canonical spelling). An unknown size would
# otherwise reach the CLI and fail the whole render with an opaque exit code.
_PAGE_SIZES = {
	"a3": "A3",
	"a4": "A4",
	"a5": "A5",
	"letter": "Letter",
	"legal": "Legal",
	"tabloid": "Tabloid",
}

_MAX_MARGIN_MM = 100

_IMG_TAG_RE = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _furniture_css(watermark_top_pt: float = 340) -> str:
	"""Self-contained CSS for the running-header document - a SEPARATE wkhtmltopdf
	render, so it carries no body/theme styling and must style itself.

	Sizes any brand/letterhead logo down to the header strip (a Letter Head logo
	is authored for Frappe's own print layout, not this band), gives left/center/
	right helpers, and places the ``.jv-watermark`` for the render's page geometry
	(``watermark_top_pt`` is computed from page size/orientation so it stays
	centered on Letter/A3/landscape, not the old portrait-A4-only ``340pt``). The
	watermark rotate/position is tool-controlled here because agent content can
	never carry a ``style`` attribute (it is class-only after ``sanitize_rich``).
	"""
	return f"""
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
	font-family: {_SANS};
	font-size: 9pt;
	color: #666666;
	-webkit-print-color-adjust: exact;
	print-color-adjust: exact;
}}
.jv-brand-header, .jv-header {{ width: 100%; }}
.jv-brand-name {{ font-weight: 700; color: #132d47; }}
.jv-center {{ text-align: center; }}
.jv-right {{ text-align: right; }}
.jv-brand-header img, .jv-lh-header img {{ height: 30pt; width: auto; max-width: 100%; }}
.jv-watermark {{
	position: absolute;
	top: {watermark_top_pt:.0f}pt;
	left: 0;
	right: 0;
	text-align: center;
	transform: rotate(-45deg);
	font-size: 72pt;
	font-weight: 700;
	color: rgba(0, 0, 0, 0.08);
	z-index: -1;
}}
"""


def _esc(value: object) -> str:
	"""Escape a text field for embedding; ``None`` renders empty, not 'None'."""
	return "" if value is None else html.escape(str(value))


def _text_only(value: str, limit: int = 200) -> str:
	"""Reduce a footer string to plain, single-line text for a wkhtmltopdf text
	footer zone (``--footer-left``): strip tags, unescape entities, collapse
	whitespace, and truncate. Passed to the subprocess as an argv item (no shell),
	so there is no injection surface; wkhtmltopdf's own ``[page]``-style tokens in
	the text are harmless."""
	text = _TAG_RE.sub("", value or "")
	text = " ".join(html.unescape(text).split())
	return text[:limit]


def render_pdf(
	body_html: str,
	*,
	page_size: str = "A4",
	orientation: str = "portrait",
	margins_mm: int = 15,
	header_html: str | None = None,
	footer_text: str | None = None,
	watermark: str | None = None,
	brand_header: str = "",
	page_numbers: bool = True,
	timeout: int = 25,
) -> bytes:
	"""Render ``body_html`` (a fragment the caller already sanitized) to PDF bytes
	via a direct, hard-timed wkhtmltopdf subprocess.

	Branding lives in the running HTML header: ``brand_header`` is ALREADY
	tool-built + safe (see ``build_brand_header`` / ``resolve_brand``); an agent
	``header_html``/``watermark`` is agent-influenced and re-sanitized here. Page
	numbers and an optional plain-text ``footer_text`` go through wkhtmltopdf TEXT
	footer zones (``--footer-right``/``--footer-left``) - NOT ``--footer-html`` -
	because ``[page]``/``[topage]`` substitution runs in wkhtmltopdf's C++ layer
	(JS-free) and a TEXT footer coexists with a header on the opposite edge, so a
	branded document keeps its "Page X of Y" instead of the old mutually-exclusive
	``--footer-html`` silently dropping it. The header temp file uses a
	content-independent ``frappe.generate_hash()`` name, removed in a ``finally``
	that runs on success, error, AND timeout.

	Raises ``InvalidArgumentError`` (clean, user-facing) when the binary is
	missing, an argument is out of range, the render times out, wkhtmltopdf exits
	non-zero, or the output is empty/not a PDF - never a raw ``TimeoutExpired`` /
	``OSError`` and never a partial/zero-byte PDF. Every infra-caused failure is
	also written to the Error Log so a systemic regression is visible to operators.
	"""
	binary = _resolve_binary()
	orientation_flag = _normalize_orientation(orientation)
	page_size = _normalize_page_size(page_size)
	margins_mm = _normalize_margin(margins_mm)
	_guard_furniture_len(header_html, footer_text, watermark)

	# Center the watermark vertically for the ACTUAL page geometry (not a fixed
	# portrait-A4 constant); ~36pt lifts the rotated block so its middle sits on
	# the page center.
	watermark_top = cover_height_pt(page_size, orientation_flag, margins_mm) / 2 - 36
	header_doc = _build_header(header_html, watermark, brand_header, watermark_top)

	tmp_dir = tempfile.gettempdir()
	header_file = None
	try:
		header_file = _write_temp(tmp_dir, header_doc) if header_doc else None

		args = _build_args(
			binary,
			page_size=page_size,
			orientation=orientation_flag,
			margins_mm=margins_mm,
			header_file=header_file,
			footer_text=footer_text,
			page_numbers=page_numbers,
			has_header=bool(header_doc),
		)
		document = _compose_document(body_html, "")
		try:
			result = subprocess.run(
				args,
				input=document.encode("utf-8"),
				capture_output=True,
				timeout=timeout,
			)
		except subprocess.TimeoutExpired as exc:
			# Hard bound hit: the child was killed, no usable output. Surface a
			# clean, actionable error rather than the raw TimeoutExpired.
			_log_infra_failure("rich-pdf render timed out", f"exceeded {timeout}s")
			raise InvalidArgumentError(f"PDF render exceeded {timeout}s — reduce content/charts") from exc
		except OSError as exc:
			# The resolved binary failed to EXECUTE (not-executable, bad format,
			# removed between resolve and run, fork failure under memory pressure).
			_log_infra_failure("rich-pdf binary failed to execute", str(exc))
			raise InvalidArgumentError("PDF render could not start — the renderer failed to execute") from exc

		if result.returncode != 0:
			# stderr can carry the /tmp/jv-pdf-<hash>.html temp path — log it for
			# operators, but keep it OUT of the user-facing message.
			tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-500:]
			_log_infra_failure(f"rich-pdf render exit {result.returncode}", tail)
			raise InvalidArgumentError(f"PDF render failed (renderer exited {result.returncode})")
		if not result.stdout:
			# A zero-byte result is a failed render, never a valid empty PDF.
			tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-500:]
			_log_infra_failure("rich-pdf render produced no output", tail)
			raise InvalidArgumentError("PDF render produced no output")
		if not result.stdout.startswith(b"%PDF-"):
			# Non-empty but not a PDF: a corrupt/error render slipping past exit 0.
			_log_infra_failure(
				"rich-pdf render output is not a PDF", result.stdout[:80].decode("latin-1", "replace")
			)
			raise InvalidArgumentError("PDF render produced invalid output (not a PDF)")
		return result.stdout
	finally:
		if header_file:
			_remove_quietly(header_file)


def _resolve_binary() -> str:
	"""Return the wkhtmltopdf executable path, or raise a clean error.

	Tries pdfkit's own resolver first (it decodes ``$PDFKIT_...``/PATH lookups and
	raises ``OSError`` when nothing is found), then ``shutil.which``. A missing
	binary is EXPECTED in local dev (the real render runs on the FC bench), so the
	failure is an ``InvalidArgumentError``, not a crash - but it is also logged, so
	a binary that goes missing on the FC bench (where it should be present) is
	visible to operators rather than silently rejecting every render.
	"""
	with contextlib.suppress(OSError):
		raw = pdfkit.configuration().wkhtmltopdf
		path = raw.decode() if isinstance(raw, bytes) else str(raw)
		if path:
			return path
	path = shutil.which("wkhtmltopdf")
	if path:
		return path
	_log_infra_failure("rich-pdf binary not found", "wkhtmltopdf missing from PATH and pdfkit config")
	raise InvalidArgumentError(
		"wkhtmltopdf binary not found — rich PDF rendering needs wkhtmltopdf "
		"(present on the Frappe Cloud bench; absent in local dev)"
	)


def _normalize_orientation(orientation: str) -> str:
	"""Validate orientation and return wkhtmltopdf's expected capitalized token
	(``Portrait``/``Landscape``)."""
	value = str(orientation).strip().lower()
	if value not in ("portrait", "landscape"):
		raise InvalidArgumentError(f"orientation must be 'portrait' or 'landscape', got {orientation!r}")
	return value.capitalize()


def _normalize_page_size(page_size: str) -> str:
	"""Validate ``page_size`` against the allowlist and return the canonical
	spelling wkhtmltopdf expects. Rejects an unknown value with a clean error
	(consistent with ``orientation``) instead of letting the CLI fail opaquely."""
	value = str(page_size).strip().lower()
	if value not in _PAGE_SIZES:
		raise InvalidArgumentError(
			f"page_size must be one of {sorted(v for v in _PAGE_SIZES.values())}, got {page_size!r}"
		)
	return _PAGE_SIZES[value]


def _normalize_margin(margins_mm: int) -> int:
	"""Validate the margin is a non-negative int within a sane bound. A negative
	value would produce a ``-50mm`` token wkhtmltopdf's arg parser can mis-read as
	a flag; an absurd value would leave no printable area."""
	try:
		value = int(margins_mm)
	except (TypeError, ValueError):
		raise InvalidArgumentError(f"margins_mm must be an integer, got {margins_mm!r}") from None
	if value < 0 or value > _MAX_MARGIN_MM:
		raise InvalidArgumentError(f"margins_mm must be between 0 and {_MAX_MARGIN_MM}, got {value}")
	return value


def _guard_furniture_len(header, footer, watermark) -> None:
	"""Bound the agent-influenced furniture inputs (the body is capped upstream)."""
	for label, value in (("header", header), ("footer", footer), ("watermark", watermark)):
		if value and len(value) > _MAX_FURNITURE_CHARS:
			raise InvalidArgumentError(f"{label} exceeds {_MAX_FURNITURE_CHARS} characters")


def _build_args(
	binary: str,
	*,
	page_size: str,
	orientation: str,
	margins_mm: int,
	header_file: str | None,
	footer_text: str | None,
	page_numbers: bool,
	has_header: bool,
) -> list[str]:
	"""Build the exact wkhtmltopdf CLI arg list.

	The leading security/fidelity flags are UNCONDITIONAL - present on every render
	regardless of options (a test asserts each one; dropping any fails it). Body is
	read from stdin and the PDF written to stdout via ``- -``.

	Header: when a header document exists it is ``--header-html`` with a
	``--header-spacing`` gap, and the TOP margin is expanded by 14mm so a logo /
	brand line sits in reserved space and never overlaps the body (wkhtmltopdf does
	NOT auto-grow the top margin for a header).

	Footer: TEXT zones only, never ``--footer-html``. ``[page]``/``[topage]``
	substitution runs in wkhtmltopdf's C++ layer, which WORKS under
	``--disable-javascript`` (an HTML footer's bracket substitution needs the JS
	wkhtmltopdf injects, so it would print a literal ``[page]`` with JS off). Text
	zones on different edges coexist, so page numbers (``--footer-right``) and an
	optional plain-text footer (``--footer-left``) BOTH render - fixing the old
	mutual-exclusivity where any HTML footer silently dropped "Page X of Y".
	"""
	margin_top = margins_mm + 14 if has_header else margins_mm
	args = [
		binary,
		"--disable-local-file-access",
		"--disable-javascript",
		"--background",
		"--images",
		"--print-media-type",
		"--disable-smart-shrinking",
		"--quiet",
		"--encoding",
		"utf-8",
		"--page-size",
		page_size,
		"--orientation",
		orientation,
		"--margin-top",
		f"{margin_top}mm",
		"--margin-bottom",
		f"{margins_mm}mm",
		"--margin-left",
		f"{margins_mm}mm",
		"--margin-right",
		f"{margins_mm}mm",
	]
	if header_file:
		args += ["--header-html", header_file, "--header-spacing", "4"]
	footer_args: list[str] = []
	if footer_text:
		clean = _text_only(footer_text)
		if clean:
			footer_args += ["--footer-left", clean]
	if page_numbers:
		footer_args += ["--footer-right", "Page [page] of [topage]"]
	if footer_args:
		args += footer_args + ["--footer-font-size", "8", "--footer-spacing", "4"]
	args += ["-", "-"]  # stdin in, stdout out
	return args


def _build_header(
	header_html: str | None, watermark: str | None, brand_header: str, watermark_top_pt: float
) -> str:
	"""Compose the running-header document, or ``""`` when there is nothing to
	render.

	Order: the tool-built-and-safe ``brand_header`` (logo, else company name) →
	sanitized agent ``header_html`` → sanitized watermark wrapped in the
	tool-controlled ``.jv-watermark`` block. The CSS is built for the render's page
	geometry so the watermark stays centered.
	"""
	parts = []
	if brand_header:
		parts.append(brand_header)
	if header_html:
		parts.append(f'<div class="jv-header">{sanitize_rich(header_html)}</div>')
	if watermark:
		parts.append(f'<div class="jv-watermark">{sanitize_rich(watermark)}</div>')
	return _compose_document("".join(parts), _furniture_css(watermark_top_pt)) if parts else ""


# --- brand resolution + tool-built title block / running header ------------


def resolve_brand(letterhead: str | None) -> dict:
	"""Resolve the tenant's brand for the composed document, fail-safe.

	Returns ``{"logo_html", "company", "address_lines", "note"}``:

	  * ``logo_html`` - the default (or named) Letter Head's logo, already
	    permission-checked, image-neutralised (remote dropped) and base64-inlined
	    by ``resolve_letterhead`` (whose doc-bound Jinja TEXT is deliberately
	    dropped - it can't render on a doc-less report). Empty when there is no
	    usable logo.
	  * ``company`` / ``address_lines`` - the default Company's name + primary
	    address (the "no logo -> company name + address" fallback the user asked
	    for; also the running-header identity). Empty on a frappe-only bench with
	    no Company, or when unreadable.
	  * ``note`` - a degrade message when a NAMED letterhead couldn't be applied.

	Never raises: any failure yields an empty brand and a title-only render.
	"""
	logo_html, note = "", None
	try:
		logo_html, _footer, note = resolve_letterhead(letterhead)
	except Exception:
		logo_html, note = "", None
	company, address_lines = _resolve_company_identity()
	return {
		"logo_html": logo_html or "",
		"company": company,
		"address_lines": address_lines,
		"note": note,
	}


def _resolve_company_identity() -> tuple[str | None, list[str]]:
	"""Default Company name + primary-address display lines, mirroring the proven
	permission-checked chain in ``jarvis.onboarding`` (frappe-only-safe, no ERPNext
	dependency). Fail-safe: any problem -> ``(None, [])`` and a title-only render."""
	try:
		# Company is an ERPNext doctype; a frappe-only bench has none.
		if not frappe.db.exists("DocType", "Company"):
			return None, []
		company = frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")
		if not company:
			names = [c.name for c in frappe.get_all("Company", fields=["name"], limit=2)]
			if len(names) == 1:
				company = names[0]
		if not company or not frappe.has_permission("Company", "read", doc=company):
			return None, []
		company_name = frappe.db.get_value("Company", company, "company_name") or company
		return company_name, _resolve_company_address_lines(company)
	except Exception:
		_log_infra_failure("rich-pdf company identity resolution failed", "")
		return None, []


def _resolve_company_address_lines(company: str) -> list[str]:
	"""The company's PRIMARY billing Address as display lines (raw text; the
	builder escapes). Mirrors ``onboarding._resolve_company_billing_address``:
	frappe-only Dynamic Link query, filter EXPLICITLY on ``is_primary_address=1``
	(never a shipping/warehouse address dressed up as the header), own read-perm
	check. Nothing flagged primary -> no address."""
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
		pluck="parent",
	)
	if not linked:
		return []
	primary = frappe.get_all(
		"Address",
		filters={"name": ["in", linked], "is_primary_address": 1, "disabled": 0},
		pluck="name",
		order_by="modified desc",
		limit=1,
	)
	if not primary or not frappe.has_permission("Address", "read", doc=primary[0]):
		return []
	a = frappe.db.get_value(
		"Address",
		primary[0],
		["address_line1", "address_line2", "city", "state", "pincode", "country"],
		as_dict=True,
	)
	if not a:
		return []
	line1 = ", ".join(p for p in (a.get("address_line1"), a.get("address_line2")) if p)
	line2 = ", ".join(p for p in (a.get("city"), a.get("state"), a.get("pincode")) if p)
	return [x for x in (line1, line2, a.get("country")) if x]


def build_title_block(
	title: str | None,
	subtitle: str | None = None,
	meta: str | None = None,
	brand: dict | None = None,
	full_cover: bool = False,
) -> str:
	"""Build the tool-owned masthead HTML (class-only, every text field escaped),
	prepended to the body AFTER sanitize. Because the tool builds it from typed
	params, raw Markdown (``#``/``**``) can never leak into the title the way it
	did when the agent hand-wrapped Markdown in ``<div class="cover">``.

	``full_cover`` wraps the block in a full-page ``.cover`` (a printable-height,
	vertically-centered title page); otherwise it is the default ``.doc-title-block``
	masthead at the top of page 1. Returns ``""`` when there is nothing to show
	(no title and no brand), so a bare-prose document simply has no masthead.
	"""
	inner = _title_block_inner(title, subtitle, meta, brand or {})
	if not inner:
		return ""
	block = f'<div class="doc-title-block">{inner}</div>'
	if full_cover:
		return f'<div class="cover"><div class="cover-inner">{block}</div></div>'
	return block


def build_brand_header(brand: dict | None) -> str:
	"""Compact running-header HTML: the logo when there is one, else the company
	name (address is too much for a running band). ``""`` when there is no brand."""
	brand = brand or {}
	logo = brand.get("logo_html") or ""
	if logo:
		return f'<div class="jv-brand-header">{logo}</div>'
	company = brand.get("company")
	if company:
		return f'<div class="jv-brand-header jv-brand-name">{_esc(company)}</div>'
	return ""


def _title_block_inner(title, subtitle, meta, brand: dict) -> str:
	parts = []
	brand_html = _brand_block_html(brand)
	if brand_html:
		parts.append(f'<div class="doc-brand">{brand_html}</div>')
	if title:
		parts.append(f'<div class="doc-title">{_esc(title)}</div>')
	if subtitle:
		parts.append(f'<div class="doc-subtitle">{_esc(subtitle)}</div>')
	if meta:
		parts.append(f'<div class="doc-meta">{_esc(meta)}</div>')
	return "".join(parts)


def _brand_block_html(brand: dict) -> str:
	"""Logo (already safe/inlined) if present, else company name (bold) + address
	lines (escaped) - the branding precedence the user specified."""
	logo = brand.get("logo_html") or ""
	if logo:
		return f'<div class="brand-logo">{logo}</div>'
	company = brand.get("company")
	if not company:
		return ""
	parts = [f'<div class="brand-name">{_esc(company)}</div>']
	for line in brand.get("address_lines") or []:
		parts.append(f'<div class="brand-addr">{_esc(line)}</div>')
	return "".join(parts)


def resolve_letterhead(letterhead: str | None) -> tuple[str, str, str | None]:
	"""Resolve a letterhead to safe ``(header_html, footer_html, note)``.

	``letterhead`` is a ``Letter Head`` name, or ``None`` to use the site's default
	Letter Head (``is_default=1``). Enforces the "trusted config" assumption the
	rich path rests on:

	  * READ-permission checked as the impersonated user (an agent may only use a
	    Letter Head that user can read).
	  * Images neutralised via ``_inline_letterhead_images`` - remote ``<img>`` is
	    DROPPED (no server-side fetch / SSRF), same-site ``/files/`` logos are
	    inlined as permission-checked base64 ``data:`` URIs.

	``note`` is ``None`` on success (including "no default configured", which is a
	normal unbranded render), or a degrade message when a NAMED letterhead can't be
	found / read - so the caller can surface it rather than silently dropping the
	branding the user asked for. Never raises: any failure yields ``("", "", note)``.
	"""
	named = letterhead if isinstance(letterhead, str) and letterhead.strip() else None
	try:
		name = named or frappe.db.get_value("Letter Head", {"is_default": 1}, "name")
		if not name:
			# No name given and no default configured → plain, unbranded render.
			return "", "", None
		if not frappe.has_permission("Letter Head", "read", doc=name):
			return "", "", (f"letterhead {named!r} not found — rendered without it" if named else None)
		lh = frappe.db.get_value("Letter Head", name, ["content", "footer"], as_dict=True)
		if not lh:
			return "", "", (f"letterhead {named!r} not found — rendered without it" if named else None)
		# A Letter Head's content/footer is a document-BOUND Jinja template (company
		# address/contact keyed off the printed doc). A composed report has no such
		# doc, so rendering it produces "None"/partial junk, and folding it in raw
		# leaks {% %}/{{ }}. So keep ONLY the brand logo(s) and drop the text: extract
		# the <img> tags, inline same-site logos to permission-checked base64, and run
		# the airtight letterhead gate (nh3, data-only images). A tenant that wants
		# text branding on a report can pass header=/footer= explicitly.
		header = sanitize_letterhead(_inline_letterhead_images(_letterhead_logos(lh.get("content") or "")))
		footer = sanitize_letterhead(_inline_letterhead_images(_letterhead_logos(lh.get("footer") or "")))
		return header, footer, None
	except Exception:
		# Letterhead is a nicety, never a hard failure of the export.
		_log_infra_failure("rich-pdf letterhead resolution failed", f"letterhead={named!r}")
		return "", "", (f"letterhead {named!r} could not be applied" if named else None)


def _letterhead_logos(raw: str) -> str:
	"""Return only the ``<img>`` logo tags from a Letter Head's HTML, dropping its
	document-bound Jinja text block (which cannot render cleanly on a doc-less
	composed report). Bounds the input first: a letterhead larger than the
	furniture cap is broken/abuse and this runs BEFORE render_pdf's timeout."""
	if not raw or "<img" not in raw.lower():
		return ""
	if len(raw) > _MAX_FURNITURE_CHARS:
		_log_infra_failure("rich-pdf letterhead too large", f"{len(raw)} chars")
		return ""
	return "".join(_IMG_TAG_RE.findall(raw))


def _inline_letterhead_images(html: str) -> str:
	"""Rewrite each ``<img src>`` in trusted letterhead HTML: keep ``data:`` URIs,
	inline a same-site ``/files/`` logo as a permission-checked base64 ``data:``
	URI, and DROP a remote ``<img>`` outright (wkhtmltopdf would fetch it)."""
	if not html or "<img" not in html.lower():
		return html

	def _repl(match: re.Match) -> str:
		tag = match.group(0)
		src = _SRC_RE.search(tag)
		if not src:
			return tag
		new_src = _resolve_letterhead_img_src(src.group("url"))
		if new_src is None:
			return ""  # remote / unresolvable → drop the whole <img>, no fetch
		return tag[: src.start("url")] + new_src + tag[src.end("url") :]

	return _IMG_TAG_RE.sub(_repl, html)


def _resolve_letterhead_img_src(url: str) -> str | None:
	"""Return a safe ``src`` for a letterhead image, or ``None`` to drop it.

	``data:`` URIs pass through (already inline, no fetch). Remote URLs
	(``http(s)://`` / protocol-relative) are dropped. A same-site path is resolved
	to its ``File``, read-permission checked, and returned as a base64 ``data:``
	URI so wkhtmltopdf never issues a request."""
	value = (url or "").strip()
	if not value:
		return None
	if value.startswith("data:"):
		return value
	if value.lower().startswith(("http://", "https://", "//")):
		return None
	path = value.split("?", 1)[0].split("#", 1)[0]
	candidates = [path] if path.startswith("/") else ["/" + path, path]
	file_name = None
	for candidate in candidates:
		file_name = frappe.db.get_value("File", {"file_url": candidate}, "name")
		if file_name:
			break
	if not file_name or not frappe.has_permission("File", "read", doc=file_name):
		return None
	# Reject an oversized logo BEFORE reading it into memory (a user-readable
	# multi-hundred-MB File referenced in a letterhead must not be slurped first).
	size = frappe.db.get_value("File", file_name, "file_size")
	if size and int(size) > _MAX_LOGO_BYTES:
		return None
	mime = mimetypes.guess_type(path)[0] or ""
	# RASTER images only. SVG is deliberately refused: an inlined data:image/svg+xml
	# is an OPAQUE blob nh3 does not look inside, so an SVG carrying <image href>/
	# <script> would ride into the render and rely on QtWebKit's (untested-here)
	# SVG-static mode for containment. A raster logo has no such surface. Company
	# logos are almost always PNG/JPG; an SVG degrades to no logo, not a risk.
	if not mime.startswith("image/") or mime == "image/svg+xml":
		return None
	try:
		content = frappe.get_doc("File", file_name).get_content()
		if isinstance(content, str):
			content = content.encode("utf-8")
	except Exception:
		return None
	if not content or len(content) > _MAX_LOGO_BYTES:
		return None
	# Trust the bytes, not the filename extension: a non-image (or an SVG) renamed
	# *.png must not be inlined as data:image/*. (Harmless downstream anyway - the
	# data-only letterhead gate never fetches - but this keeps the data: label
	# honest and keeps active SVG markup out of the render entirely.)
	if not _looks_like_image(content):
		return None
	return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _looks_like_image(content: bytes) -> bool:
	"""Cheap magic-byte sniff for the common RASTER logo formats (SVG is refused -
	see _resolve_letterhead_img_src)."""
	head = content[:16]
	return (
		head.startswith(b"\x89PNG\r\n\x1a\n")  # png
		or head.startswith(b"\xff\xd8\xff")  # jpeg
		or head.startswith(b"GIF87a")
		or head.startswith(b"GIF89a")
		or (head[:4] == b"RIFF" and content[8:12] == b"WEBP")  # webp
		or head.startswith(b"BM")  # bmp
	)


def _compose_document(inner: str, css: str) -> str:
	"""Wrap a fragment in a minimal, self-contained HTML document with a declared
	charset - header/footer are separate renders and the body fragment needs a
	well-formed page around it."""
	return (
		'<!doctype html><html><head><meta charset="utf-8">'
		f"<style>{css}</style></head><body>{inner}</body></html>"
	)


def _write_temp(tmp_dir: str, content: str) -> str:
	"""Write ``content`` to a uniquely named temp file (name from
	``frappe.generate_hash()``, never derived from content) and return its path. If
	the write fails mid-stream (ENOSPC), remove the partial file before re-raising -
	its random name was never returned, so nothing else could ever clean it up."""
	path = os.path.join(tmp_dir, f"jv-pdf-{frappe.generate_hash()}.html")
	try:
		with open(path, "wb") as fh:
			fh.write(content.encode("utf-8"))
	except Exception:
		_remove_quietly(path)
		raise
	return path


def _remove_quietly(path: str) -> None:
	"""Best-effort temp-file removal. Suppress ANY OSError (a missing file, or an
	unusual EPERM/read-only tmpdir) - cleanup must never abort the finally loop
	(leaking the other temp) or replace the real in-flight error with a raw
	OSError."""
	with contextlib.suppress(OSError):
		os.remove(path)


def _log_infra_failure(title: str, message: str) -> None:
	"""Write an infra-caused render failure to the Error Log, best-effort. Logging
	must NEVER replace the real error the caller is about to raise, so every failure
	here (including a missing site in a unit test) is swallowed."""
	with contextlib.suppress(Exception):
		frappe.log_error(title=f"jarvis.rich_pdf: {title}"[:140], message=message or title)
