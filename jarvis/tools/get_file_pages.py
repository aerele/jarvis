"""Render a server-side File's pages as images the model can SEE.

``read_file`` returns extracted text, which is empty for a scanned PDF - the
agent then had no way to read a private File mid-conversation and had to ask
the customer to re-attach it in chat (chat attachments arrive as vision).
This tool closes that gap: it rasterizes the File's pages (pypdfium2, same
caps as the chat vision path) and returns them base64-encoded; the agent
plugin lifts ``pages[]`` into image blocks on the tool result, so the model
reads the pages exactly like chat-attached ones.

Permission contract: same as ``read_file`` - the calling user must have read
permission on the File.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

_DEFAULT_PAGES = 4
_MAX_PAGES_PER_CALL = 6
_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}


def get_file_pages(
	file_url: str | None = None,
	filename: str | None = None,
	first_page: int = 1,
	max_pages: int = _DEFAULT_PAGES,
) -> dict:
	"""Return a File's pages as images (for scanned PDFs and image files).

	Identify the file by ``file_url`` (preferred) or ``filename`` (most recent
	readable match). ``first_page`` (1-based) + ``max_pages`` (<= 6 per call)
	window a long document - call again with a higher ``first_page`` for the
	rest. Use when ``read_file`` reports no extractable text; for text PDFs
	and spreadsheets prefer ``read_file`` (cheaper than pixels).
	"""
	from jarvis.chat.vision import image_part, pdf_parts
	from jarvis.tools.read_file import _resolve_file

	fdoc = _resolve_file(file_url, filename)
	if not frappe.has_permission("File", "read", doc=fdoc.name):
		# Generic on purpose - see read_file._resolve_file / F12 in
		# audit-findings.md: do not echo the resolved file's real name back
		# to a caller who isn't permitted to read it.
		raise PermissionDeniedError("no matching file found")

	first_page = max(1, int(first_page or 1))
	max_pages = max(1, min(int(max_pages or _DEFAULT_PAGES), _MAX_PAGES_PER_CALL))
	content = fdoc.get_content()
	if isinstance(content, str):
		content = content.encode()

	ext = (fdoc.file_name or "").rsplit(".", 1)[-1].lower()
	if ext == "pdf":
		parts, total = pdf_parts(content, fdoc.file_name, first_page=first_page, max_pages=max_pages)
		if not parts:
			raise InvalidArgumentError(
				f"no renderable pages (document has {total} pages; first_page={first_page})"
			)
		pages = [{"page": p["page"], "mime": p["mime"], "data_b64": p["data_b64"]} for p in parts]
		last = pages[-1]["page"]
		return {
			"kind": "file_pages",
			"file_name": fdoc.file_name,
			"page_count": total,
			"pages": pages,
			"has_more": last < total,
			"note": "pages are attached to this tool result as images"
			+ (f"; call again with first_page={last + 1} for the rest" if last < total else ""),
		}
	if ext in _IMAGE_EXT:
		part = image_part(content, fdoc.file_name)
		if not part:
			raise InvalidArgumentError(f"could not decode image {fdoc.file_name!r}")
		return {
			"kind": "file_pages",
			"file_name": fdoc.file_name,
			"page_count": 1,
			"pages": [{"page": 1, "mime": part["mime"], "data_b64": part["data_b64"]}],
			"has_more": False,
			"note": "image is attached to this tool result",
		}
	raise InvalidArgumentError(
		f"{fdoc.file_name!r} is not a PDF or image; use read_file for text/spreadsheet content"
	)
