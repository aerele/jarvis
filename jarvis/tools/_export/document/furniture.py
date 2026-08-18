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
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile

import frappe

from jarvis.exceptions import InvalidArgumentError

from .sanitizer import sanitize_letterhead, sanitize_rich

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
_MIN_RENDER_TIMEOUT_S = 1
_MAX_RENDER_TIMEOUT_S = 60
_WKHTMLTOPDF_BINARY = "wkhtmltopdf"

_IMG_TAG_RE = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""", re.IGNORECASE)

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


def render_pdf(
	body_html: str,
	*,
	page_size: str = "A4",
	orientation: str = "portrait",
	margins_mm: int = 15,
	header_html: str | None = None,
	footer_html: str | None = None,
	watermark: str | None = None,
	letterhead_header: str = "",
	letterhead_footer: str = "",
	page_numbers: bool = True,
	timeout: int = 25,
) -> bytes:
	"""Render ``body_html`` (a fragment the caller already sanitized) to PDF bytes
	via a direct, hard-timed wkhtmltopdf subprocess.

	Header/footer/watermark are agent-influenced and re-sanitized here.
	``letterhead_header``/``letterhead_footer`` are ALREADY resolved + safe (see
	``resolve_letterhead``) and folded in as-is. Temp files for the header/footer
	documents use content-independent ``frappe.generate_hash()`` names and are
	removed in a ``finally`` that runs on success, error, AND timeout.

	Raises ``InvalidArgumentError`` (clean, user-facing) when the binary is
	missing, an argument is out of range, the render times out, wkhtmltopdf exits
	non-zero, or the output is empty/not a PDF - never a raw ``TimeoutExpired`` /
	``OSError`` and never a partial/zero-byte PDF. Every infra-caused failure (a
	working binary misbehaving) is also written to the Error Log so a systemic
	regression - e.g. an FC image bump changing wkhtmltopdf's behaviour - is
	visible to operators rather than silently surfacing as a user-facing rejection
	forever.
	"""
	binary = _resolve_binary()
	orientation_flag = _normalize_orientation(orientation)
	page_size = _normalize_page_size(page_size)
	margins_mm = _normalize_margin(margins_mm)
	timeout = _normalize_timeout(timeout)
	_guard_furniture_len(header_html, footer_html, watermark)

	header_doc = _build_header(header_html, watermark, letterhead_header)
	footer_doc = _build_footer(footer_html, letterhead_footer)

	tmp_dir = tempfile.gettempdir()
	header_file = footer_file = None
	try:
		# Temp creation is INSIDE the try so a failure writing the second file
		# (ENOSPC / fd exhaustion) still runs the finally that removes the first.
		header_file = _write_temp(tmp_dir, header_doc) if header_doc else None
		footer_file = _write_temp(tmp_dir, footer_doc) if footer_doc else None

		args = _build_args(
			binary,
			page_size=page_size,
			orientation=orientation_flag,
			margins_mm=margins_mm,
			header_file=header_file,
			footer_file=footer_file,
			page_numbers=page_numbers,
		)
		document = _compose_document(body_html, "")
		try:
			# Security review (frappe-subprocess-exec): ``args`` is an argv list,
			# never a shell command. Its only variable CLI values are validated
			# above; header/footer paths are absolute names generated by _write_temp;
			# document content is carried on stdin, not interpolated into argv.
			result = subprocess.run(  # nosemgrep: frappe-subprocess-exec
				args,
				input=document.encode("utf-8"),
				capture_output=True,
				shell=False,
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
		for path in (header_file, footer_file):
			if path:
				_remove_quietly(path)


def _resolve_binary() -> str:
	"""Resolve only the fixed ``wkhtmltopdf`` executable and validate its path.

	The renderer name is tool-controlled, never supplied by the caller or read from
	pdfkit configuration. ``shutil.which`` may still return a PATH-derived value, so
	the result must be absolute, have the expected basename, and point to an
	executable regular file before it is allowed into argv.
	"""
	path = shutil.which(_WKHTMLTOPDF_BINARY)
	if path:
		try:
			return _validate_binary_path(path)
		except (OSError, TypeError, ValueError):
			_log_infra_failure("rich-pdf binary path rejected", "wkhtmltopdf resolved unsafely")
			raise InvalidArgumentError("wkhtmltopdf binary path is not a trusted executable") from None
	_log_infra_failure("rich-pdf binary not found", "wkhtmltopdf missing from PATH")
	raise InvalidArgumentError(
		"wkhtmltopdf binary not found — rich PDF rendering needs wkhtmltopdf "
		"(present on the Frappe Cloud bench; absent in local dev)"
	)


def _validate_binary_path(path: str) -> str:
	"""Return a canonical, executable path for the fixed renderer binary.

	This is the final trust boundary for argv[0]. In particular, a compromised or
	misconfigured PATH result such as ``/bin/sh`` must never become the child
	process merely because it exists and is executable.
	"""
	candidate = os.fsdecode(path)
	if not os.path.isabs(candidate) or os.path.basename(candidate) != _WKHTMLTOPDF_BINARY:
		raise ValueError("unexpected wkhtmltopdf path")
	resolved = os.path.realpath(candidate)
	# Validate the symlink target too, so a PATH entry named ``wkhtmltopdf`` cannot
	# simply point at a different executable such as /bin/sh.
	if os.path.basename(resolved) != _WKHTMLTOPDF_BINARY:
		raise ValueError("wkhtmltopdf symlink resolves to an unexpected executable")
	if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
		raise ValueError("wkhtmltopdf path is not an executable file")
	return resolved


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


def _normalize_timeout(timeout: int) -> int:
	"""Return a bounded integer subprocess timeout."""
	if isinstance(timeout, bool):
		raise InvalidArgumentError(f"timeout must be an integer, got {timeout!r}")
	try:
		value = int(timeout)
	except (TypeError, ValueError):
		raise InvalidArgumentError(f"timeout must be an integer, got {timeout!r}") from None
	if value < _MIN_RENDER_TIMEOUT_S or value > _MAX_RENDER_TIMEOUT_S:
		raise InvalidArgumentError(
			f"timeout must be between {_MIN_RENDER_TIMEOUT_S} and {_MAX_RENDER_TIMEOUT_S} seconds, got {value}"
		)
	return value


def _guard_furniture_len(header_html, footer_html, watermark) -> None:
	"""Bound the agent-influenced furniture inputs (the body is capped upstream)."""
	for label, value in (("header", header_html), ("footer", footer_html), ("watermark", watermark)):
		if value and len(value) > _MAX_FURNITURE_CHARS:
			raise InvalidArgumentError(f"{label} exceeds {_MAX_FURNITURE_CHARS} characters")


def _build_args(
	binary: str,
	*,
	page_size: str,
	orientation: str,
	margins_mm: int,
	header_file: str | None,
	footer_file: str | None,
	page_numbers: bool,
) -> list[str]:
	"""Build the exact wkhtmltopdf CLI arg list.

	The leading security/fidelity flags are UNCONDITIONAL - they are present on
	every render regardless of options (a test asserts each one; dropping any
	fails it). Body is read from stdin and the PDF written to stdout via ``- -``.

	Page numbers use the TEXT footer (``--footer-center``), NOT an HTML footer:
	wkhtmltopdf substitutes ``[page]``/``[topage]`` in the text header/footer
	options in its own C++ layer, which WORKS under ``--disable-javascript`` - the
	``--footer-html`` bracket substitution relies on JS wkhtmltopdf injects, so it
	renders a literal ``[page]`` when JS is off. A text footer and an HTML footer
	are mutually exclusive, so when there IS an HTML footer (a letterhead footer or
	an explicit ``footer``) it wins and auto page numbers are omitted.
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
	elif page_numbers:
		args += [
			"--footer-center",
			"Page [page] of [topage]",
			"--footer-font-size",
			"8",
			"--footer-spacing",
			"4",
		]
	args += ["-", "-"]  # stdin in, stdout out
	return args


def _build_header(header_html: str | None, watermark: str | None, letterhead_header: str) -> str:
	"""Compose the header document, or ``""`` when there is nothing to render.

	Order: resolved-and-safe letterhead header (as-is) → sanitized agent header →
	sanitized watermark wrapped in the tool-controlled ``.jv-watermark`` block.
	"""
	parts = []
	if letterhead_header:
		parts.append(f'<div class="jv-lh-header">{letterhead_header}</div>')
	if header_html:
		parts.append(f'<div class="jv-header">{sanitize_rich(header_html)}</div>')
	if watermark:
		parts.append(f'<div class="jv-watermark">{sanitize_rich(watermark)}</div>')
	return _compose_document("".join(parts), _FURNITURE_CSS) if parts else ""


def _build_footer(footer_html: str | None, letterhead_footer: str) -> str:
	"""Compose the HTML footer document, or ``""`` when there is none.

	Holds the resolved-and-safe letterhead footer and/or a sanitized explicit
	``footer``. Page numbers are NOT here - they go through the text footer in
	``_build_args`` (see there for why: ``[page]`` substitution needs JS in an HTML
	footer but not in a text footer)."""
	parts = []
	if letterhead_footer:
		parts.append(f'<div class="jv-lh-footer">{letterhead_footer}</div>')
	if footer_html:
		parts.append(f'<div class="jv-footer">{sanitize_rich(footer_html)}</div>')
	return _compose_document("".join(parts), _FURNITURE_CSS) if parts else ""


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
