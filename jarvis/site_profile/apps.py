"""App-level custom/core classification for customization discovery.

Single source of truth shared by get_schema, collect.py and the [Context:]
clause. Every function fails toward "nothing custom".
"""

from __future__ import annotations

import re

import frappe

# The core stack the persona skill families teach; anything else installed is
# the customer's own. Extendable per site via core_apps_override.
_KNOWN_APPS = frozenset(
	{
		"frappe",
		"erpnext",
		"hrms",
		"india_compliance",
		"payments",
		"webshop",
		"insights",
		"wiki",
		"jarvis",
	}
)

_SETTINGS = "Jarvis Settings"


def known_apps() -> frozenset[str]:
	"""_KNOWN_APPS plus core_apps_override entries; the constant on failure."""
	try:
		settings = frappe.get_cached_doc(_SETTINGS)
		raw = (settings.get("core_apps_override") or "").strip()
	except Exception:
		return _KNOWN_APPS
	if not raw:
		return _KNOWN_APPS
	extra = {p.strip().lower() for p in re.split(r"[,\n]", raw) if p.strip()}
	return _KNOWN_APPS | frozenset(extra)


def _installed_apps() -> list[str]:
	"""Test seam - patching frappe.get_installed_apps globally breaks the
	framework's own hook resolution."""
	return frappe.get_installed_apps()


def custom_apps() -> list[str]:
	"""Installed apps outside the core stack; empty on failure."""
	try:
		installed = _installed_apps()
	except Exception:
		return []
	known = known_apps()
	return [a for a in installed if a and a.lower() not in known]


def custom_module_names() -> set[str]:
	"""Modules of custom apps (classifies app-shipped custom=0 doctypes)."""
	apps = custom_apps()
	if not apps:
		return set()
	try:
		return set(frappe.get_all("Module Def", filters={"app_name": ("in", apps)}, pluck="name"))
	except Exception:
		return set()


def known_module_names() -> set[str]:
	"""Modules of KNOWN apps: a Custom Field stamped with one is app-shipped
	fixture schema even when is_system_generated predates it. Empty on
	failure - fail toward over-reporting."""
	try:
		return set(
			frappe.get_all(
				"Module Def",
				filters={"app_name": ("in", sorted(known_apps()))},
				pluck="name",
			)
		)
	except Exception:
		return set()


def is_custom_doctype_module(module: str | None) -> bool:
	"""True iff ``module`` belongs to a custom app. Never raises."""
	if not module:
		return False
	try:
		return module in custom_module_names()
	except Exception:
		return False
