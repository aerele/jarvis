"""Page furniture + the rich-PDF render call (the 2nd, sanitized render path).

``render_pdf`` turns a *already-sanitized* body-HTML fragment into PDF bytes on
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
through ``sanitize_rich`` before it is written to a temp file. The letterhead
header/footer is operator-authored trusted config (a ``Letter Head`` doc) and is
folded in WITHOUT sanitizing - sanitizing would strip its logo, which is the whole
point of a letterhead.

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

import contextlib
import os
import shutil
import subprocess
import tempfile

import frappe
import pdfkit

from jarvis.exceptions import InvalidArgumentError

from .sanitizer import sanitize_rich

_SANS = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'

# Self-contained CSS for the header/footer documents - they are SEPARATE
# wkhtmltopdf renders, so they carry no body/theme styling and must style
# themselves. ``.jv-watermark`` is the tool-controlled rotate/position trick
# (agent content can never carry a ``style`` attribute - it is class-only after
# ``sanitize_rich`` - so the rotation must come from here, not the payload).
_FURNITURE_CSS = f"""
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
	font-family: {_SANS};
	font-size: 9pt;
	color: #666666;
	-webkit-print-color-adjust: exact;
	print-color-adjust: exact;
}}
.jv-pageno {{ text-align: center; }}
.jv-lh-header, .jv-lh-footer {{ width: 100%; }}
.jv-watermark {{
	position: absolute;
	top: 340pt;
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

# wkhtmltopdf substitutes the bracket variables ``[page]``/``[topage]`` inside
# header/footer HTML natively (text replacement, NOT JavaScript) - so page
# numbering survives ``--disable-javascript``.
_PAGE_NUMBER_HTML = '<div class="jv-pageno">Page [page] of [topage]</div>'


def render_pdf(
	body_html: str,
	*,
	page_size: str = "A4",
	orientation: str = "portrait",
	margins_mm: int = 15,
	header_html: str | None = None,
	footer_html: str | None = None,
	watermark: str | None = None,
	letterhead: str | None = None,
	page_numbers: bool = True,
	timeout: int = 25,
) -> bytes:
	"""Render ``body_html`` (a fragment the caller already sanitized) to PDF bytes
	via a direct, hard-timed wkhtmltopdf subprocess.

	Header/footer/watermark are agent-influenced and re-sanitized here; letterhead
	is trusted operator config, folded in as-is. Temp files for the header/footer
	documents use content-independent ``frappe.generate_hash()`` names and are
	removed in a ``finally`` that runs on success, error, AND timeout.

	Raises ``InvalidArgumentError`` (clean, user-facing) when the binary is
	missing, the render times out, wkhtmltopdf exits non-zero, or the output is
	empty - never a raw ``TimeoutExpired`` and never a partial/zero-byte PDF.
	"""
	binary = _resolve_binary()
	orientation_flag = _normalize_orientation(orientation)

	lh_header, lh_footer = _letterhead_parts(letterhead) if letterhead else ("", "")
	header_doc = _build_header(header_html, watermark, lh_header)
	footer_doc = _build_footer(footer_html, page_numbers, lh_footer)

	tmp_dir = tempfile.gettempdir()
	header_file = _write_temp(tmp_dir, header_doc) if header_doc else None
	footer_file = _write_temp(tmp_dir, footer_doc) if footer_doc else None

	try:
		args = _build_args(
			binary,
			page_size=page_size,
			orientation=orientation_flag,
			margins_mm=margins_mm,
			header_file=header_file,
			footer_file=footer_file,
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
			raise InvalidArgumentError(f"PDF render exceeded {timeout}s — reduce content/charts") from exc

		if result.returncode != 0:
			tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-500:]
			raise InvalidArgumentError(f"PDF render failed (wkhtmltopdf exit {result.returncode}): {tail}")
		if not result.stdout:
			# A zero-byte result is a failed render, never a valid empty PDF.
			raise InvalidArgumentError("PDF render produced no output")
		return result.stdout
	finally:
		for path in (header_file, footer_file):
			if path:
				_remove_quietly(path)


def _resolve_binary() -> str:
	"""Return the wkhtmltopdf executable path, or raise a clean error.

	Tries pdfkit's own resolver first (it decodes ``$PDFKIT_...``/PATH lookups and
	raises ``OSError`` when nothing is found), then ``shutil.which``. A missing
	binary is EXPECTED in local dev (the real render runs on the FC bench), so the
	failure is an ``InvalidArgumentError``, not a crash.
	"""
	with contextlib.suppress(OSError):
		raw = pdfkit.configuration().wkhtmltopdf
		path = raw.decode() if isinstance(raw, bytes) else str(raw)
		if path:
			return path
	path = shutil.which("wkhtmltopdf")
	if path:
		return path
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


def _build_args(
	binary: str,
	*,
	page_size: str,
	orientation: str,
	margins_mm: int,
	header_file: str | None,
	footer_file: str | None,
) -> list[str]:
	"""Build the exact wkhtmltopdf CLI arg list.

	The leading security/fidelity flags are UNCONDITIONAL - they are present on
	every render regardless of options (a test asserts each one; dropping any
	fails it). Body is read from stdin and the PDF written to stdout via ``- -``.
	"""
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
		f"{margins_mm}mm",
		"--margin-bottom",
		f"{margins_mm}mm",
		"--margin-left",
		f"{margins_mm}mm",
		"--margin-right",
		f"{margins_mm}mm",
	]
	if header_file:
		args += ["--header-html", header_file]
	if footer_file:
		args += ["--footer-html", footer_file]
	args += ["-", "-"]  # stdin in, stdout out
	return args


def _build_header(header_html: str | None, watermark: str | None, letterhead_header: str) -> str:
	"""Compose the header document, or ``""`` when there is nothing to render.

	Order: trusted letterhead header (as-is) → sanitized agent header → sanitized
	watermark wrapped in the tool-controlled ``.jv-watermark`` rotate block.
	"""
	parts = []
	if letterhead_header:
		parts.append(f'<div class="jv-lh-header">{letterhead_header}</div>')
	if header_html:
		parts.append(f'<div class="jv-header">{sanitize_rich(header_html)}</div>')
	if watermark:
		parts.append(f'<div class="jv-watermark">{sanitize_rich(watermark)}</div>')
	return _compose_document("".join(parts), _FURNITURE_CSS) if parts else ""


def _build_footer(footer_html: str | None, page_numbers: bool, letterhead_footer: str) -> str:
	"""Compose the footer document, or ``""`` when there is nothing to render.

	An explicit ``footer_html`` wins over auto page numbers (they are mutually
	exclusive); the trusted letterhead footer is always folded in when present.
	"""
	parts = []
	if letterhead_footer:
		parts.append(f'<div class="jv-lh-footer">{letterhead_footer}</div>')
	if footer_html:
		parts.append(f'<div class="jv-footer">{sanitize_rich(footer_html)}</div>')
	elif page_numbers:
		parts.append(_PAGE_NUMBER_HTML)
	return _compose_document("".join(parts), _FURNITURE_CSS) if parts else ""


def _letterhead_parts(letterhead: str) -> tuple[str, str]:
	"""Return ``(header_html, footer_html)`` for a named ``Letter Head``.

	Lazy-imports ``printview`` so this module stays importable without a bench;
	needs a site, so its direct test is gated. Unit tests stub THIS function to
	exercise the fold logic site-free.
	"""
	from frappe.www.printview import get_letter_head

	parts = get_letter_head(None, no_letterhead=False, letterhead=letterhead) or {}
	return parts.get("content") or "", parts.get("footer") or ""


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
	``frappe.generate_hash()``, never derived from content) and return its path."""
	path = os.path.join(tmp_dir, f"jv-pdf-{frappe.generate_hash()}.html")
	with open(path, "wb") as fh:
		fh.write(content.encode("utf-8"))
	return path


def _remove_quietly(path: str) -> None:
	"""Best-effort temp-file removal - a missing file is fine (already gone)."""
	with contextlib.suppress(FileNotFoundError):
		os.remove(path)
