"""Owner-permission-checked attachment reads for a named chat recipient."""

from __future__ import annotations

import json
import mimetypes
import re

import frappe
from frappe import _
from frappe.utils.response import build_response
from werkzeug.wrappers import Response

from jarvis.chat.share_projection import safe_value
from jarvis.tools._export.document.sanitizer import sanitize_letterhead

_LOCAL_FILE = re.compile(r"^/(private/)?files/([^/%\\?#\x00-\x1f]+)$")
_INLINE = frozenset({"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"})


def _canvas(raw) -> list:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except (ValueError, RecursionError):
			return []
	return raw if isinstance(raw, list) else []


def canvas_item(message: str, name: str) -> dict:
	for item in _canvas(frappe.db.get_value("Jarvis Chat Message", message, "canvas")):
		if isinstance(item, dict) and item.get("name") == name:
			return item
	_refuse()


def _refuse():
	frappe.throw(_("This attachment is unavailable in the shared chat."), frappe.PermissionError)


def read_file(conversation, url: str):
	match = _LOCAL_FILE.fullmatch(url) if isinstance(url, str) else None
	if not match or match[2] in (".", ".."):
		_refuse()
	# Membership is structural and exact. Text mentions and hidden rows do not
	# authorize a file; no filename, LIKE query or request-derived disk path.
	attached = any(
		isinstance(item, dict) and item.get("file_url") == url
		for raw in frappe.get_all(
			"Jarvis Chat Message", filters={"conversation": conversation.name, "hidden": 0}, pluck="canvas"
		)
		for item in _canvas(raw)
	)
	if not attached:
		_refuse()
	for name in frappe.get_all(
		"File",
		filters={"file_url": url, "is_folder": 0, "is_private": int(bool(match[1]))},
		pluck="name",
		order_by="name",
	):
		row = frappe.get_doc("File", name)
		# SQL collations can equate case/accent variants or trailing spaces.
		# They must never widen the exact canvas URL's authorization.
		if row.file_url != url:
			continue
		if frappe.has_permission("File", "read", doc=row, user=conversation.owner):
			return row
	_refuse()


def download(doc) -> Response:
	content = doc.get_content()
	mime = mimetypes.guess_type(doc.file_name or "")[0] or "application/octet-stream"
	frappe.local.response.update(
		{
			"type": "download",
			"filename": doc.file_name,
			"filecontent": content,
			"content_type": mime,
			"display_content_as": "inline" if mime in _INLINE else "attachment",
		}
	)
	# Frappe v15 has no local.response_headers; return the HTTP response directly.
	response = build_response("download")
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response


def static_html(content: bytes | str, conversation: str, *, dark: bool = False) -> str:
	if isinstance(content, bytes):
		content = content.decode("utf-8", errors="replace")
	# The renderer accepts presentation markup only: no JS, links, forms,
	# embedded documents, remote images, author CSS or navigation attributes.
	clean = sanitize_letterhead(safe_value(content, conversation))
	background, foreground = ("#171717", "#ededed") if dark else ("#ffffff", "#171717")
	csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
	return (
		'<!doctype html><html><head><meta charset="utf-8">'
		f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
		f"<style>body{{font-family:system-ui,sans-serif;background:{background};color:{foreground};padding:1rem}}"
		"table{border-collapse:collapse}td,th{padding:.4rem;border:1px solid #888}img{max-width:100%}</style>"
		f"</head><body>{clean}</body></html>"
	)
