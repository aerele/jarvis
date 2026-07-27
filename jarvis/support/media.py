"""Bench-side media proxy (Plan 3 B4).

download streams a Helpdesk file from the control plane back to the browser (same-origin, so the
SPA can point <img>/<a> at it). upload reads the posted file, base64s it, and forwards to the CP
(which decodes, caps, allowlist-checks, and attaches it to the ticket). Role-gated like api.py.
"""

import base64

import frappe
from werkzeug.wrappers import Response

from jarvis import admin_client
from jarvis.onboarding import _surface
from jarvis.permissions import support_scope
from jarvis.support.api import _requesting_user

_MAX_BYTES = 25 * 1024 * 1024


def _scope() -> str:
	scope = support_scope()
	if scope is None:
		frappe.throw("You don't have Jarvis support access.", frappe.PermissionError)
	return scope


@frappe.whitelist()
def download(ticket: str, file_url: str):
	scope = _scope()
	content, content_type, disposition = _surface(
		admin_client.support_download,
		ticket=ticket,
		file_url=file_url,
		requesting_user=_requesting_user(),
		scope=scope,
	)
	out = Response(content, content_type=content_type)
	out.headers["Content-Disposition"] = disposition or "attachment"
	out.headers["X-Content-Type-Options"] = "nosniff"
	return out


@frappe.whitelist()
def upload(ticket: str) -> dict:
	scope = _scope()
	f = frappe.request.files.get("file") if frappe.request else None
	if not f:
		frappe.throw("no file provided")
	content = f.read()
	if not content:
		frappe.throw("This file is empty.")
	if len(content) > _MAX_BYTES:
		frappe.throw("file too large")
	content_b64 = base64.b64encode(content).decode("ascii")
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_upload,
			ticket=ticket,
			filename=f.filename,
			content_b64=content_b64,
			requesting_user=_requesting_user(),
			scope=scope,
		),
	}
