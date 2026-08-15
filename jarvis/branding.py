"""Whitelisted API for per-tenant whitelabel branding (assistant name, logo,
favicon). Tenant-admin only; the SPA Settings -> Branding pane reads/writes
here. Values are non-secret and also ride the www/jarvis.py boot payload to
every user.

House response shape: ``{"ok": True, "data": ...}``. Methods are type-annotated
because hooks.py sets require_type_annotated_api_methods.
"""

from __future__ import annotations

import frappe

from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import validate_branding_inputs
from jarvis.permissions import require_jarvis_admin

SETTINGS = "Jarvis Settings"


def _branding_payload(agent_name: str, logo: str, favicon: str) -> dict:
	return {
		"agent_name": agent_name or "",
		"brand_logo_url": logo or "",
		"brand_favicon_url": favicon or "",
	}


@frappe.whitelist()
def get_branding() -> dict:
	"""Current whitelabel identity for the Branding pane. Tenant-admin only."""
	require_jarvis_admin()
	doc = frappe.get_cached_doc(SETTINGS)
	return {
		"ok": True,
		"data": _branding_payload(doc.agent_name, doc.brand_logo, doc.brand_favicon),
	}


@frappe.whitelist()
def update_branding(agent_name: str = "", logo_url: str = "", favicon_url: str = "") -> dict:
	"""Set the whitelabel identity. Writes the Single fields directly (no full
	doc.save, so it never trips the LLM-sync machinery in on_update, which can
	restart the container). Tenant-admin only; server re-checks (the SPA gate is
	UX). ``logo_url``/``favicon_url`` are public file_urls the SPA already
	uploaded via upload_file."""
	require_jarvis_admin()
	name, logo, favicon = validate_branding_inputs(agent_name, logo_url, favicon_url)
	for field, value in (("agent_name", name), ("brand_logo", logo), ("brand_favicon", favicon)):
		frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
	frappe.db.commit()
	return {"ok": True, "data": _branding_payload(name, logo, favicon)}
