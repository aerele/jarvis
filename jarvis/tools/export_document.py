"""Render composed content (a report/summary the agent assembled, not a single
DocType record) into a downloadable File — PDF, standalone HTML, or a PNG image.

This is the write-side twin of ``export_excel`` for prose/tabular *documents*.
``download_pdf`` only prints one existing record through a Print Format; a
report the agent composed from many queries has no record to print, so without
this the agent hand-builds the file with ``exec``/``browser`` and it never
reaches the user. Here we render the content with Frappe's own engines
(``md_to_html`` + ``get_pdf``) and save a private File the chat renders as a
download card.

Security: ``content`` is agent-composed, which means it is effectively
LLM-controlled text — it must never reach the HTML unsanitized. Two distinct
threats, both closed at one choke point:

  * XSS in the standalone HTML output — ``<script>``, inline event handlers,
    ``javascript:`` hrefs.
  * SSRF-via-render — ``get_pdf`` runs wkhtmltopdf, which FETCHES any
    ``<img src>`` / ``<link href>`` / SVG ``<image href>`` it finds at render
    time, server-side. An agent-composed ``<img src="http://169.254.169.254/…">``
    (cloud metadata) or ``file:///etc/passwd`` would make the PDF renderer
    issue that request. This tool does not support embedded
    images/charts (deliberately out of scope), so every fetch-capable tag is
    stripped outright rather than merely attribute-filtered — no feature is
    lost by removing a tag nothing here is meant to use.

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
becomes HTML, never whether the result gets sanitized.
"""

import frappe
from frappe.utils.html_utils import sanitize_html

from jarvis.exceptions import InvalidArgumentError, NoDataError

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


# Minimal, self-contained stylesheet so tables/headings read cleanly in every
# format (the HTML file opens standalone; the PDF/PNG render from the same CSS).
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


def export_document(
	content: str,
	format: str = "pdf",
	title: str | None = None,
	content_is_html: bool = False,
) -> dict:
	"""Render ``content`` and return ``{file_url, filename, title, mime_type,
	size_bytes, name}`` for a private downloadable File.

	``content`` is the composed document — Markdown by default (tables,
	headings, lists all render), or raw HTML when ``content_is_html`` is set.
	``format`` is ``"pdf"`` (default), ``"html"``, or ``"png"`` (a single image
	of the rendered pages, stacked). ``title`` names the file + document.

	Both content modes are sanitized identically before rendering (see the
	module docstring): ``content_is_html`` changes how ``content`` becomes
	HTML, never whether the result is sanitized. Images/embedded charts are
	not supported by design, not omission — do not ask for a workaround via
	``content_is_html``.
	"""
	if not isinstance(content, str) or not content.strip():
		raise NoDataError("No content to export.")
	if len(content) > _MAX_CONTENT_CHARS:
		raise InvalidArgumentError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
	fmt = (format or "pdf").lower()
	if fmt not in _FORMATS:
		raise InvalidArgumentError(f"format must be one of {sorted(_FORMATS)}")

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
