# Migration: seed Jarvis Settings.models from legacy llm_* fields
# Idempotent: only runs if models table is empty AND llm_model is set
#
# FT1 fix (2026-06-26): only seeds for api_key tenants with a non-blank key.
# oauth/subscription/blank-key tenants are left with an empty models table
# (they stay on the legacy direct path) and the migration must NOT raise or
# make admin network calls.
#
# Implementation note: we insert the child row directly via frappe.get_doc /
# doc.insert() rather than calling settings.save(), so Jarvis Settings'
# on_update / _on_update_unified_llm is never triggered. This avoids:
#   - validate_models() throwing for any credential type
#   - _enqueue_pool_sync / _on_update_single_model_legacy making network calls
#   - bench migrate aborting for ANY tenant configuration

import frappe


def execute():
	frappe.set_user("Administrator")
	settings = frappe.get_single("Jarvis Settings")

	# Idempotent guard: already migrated
	if settings.get("models"):
		return

	# Only migrate api_key mode (empty string is the old default for api_key)
	auth_mode = settings.get("llm_auth_mode") or ""
	if auth_mode not in ("api_key", ""):
		# oauth / subscription tenants → leave models empty, legacy path continues
		return

	# Require a non-blank model name to have anything to migrate
	if not settings.get("llm_model"):
		return

	# Require a non-blank api_key for api_key mode tenants
	api_key = settings.get_password("llm_api_key", raise_exception=False) or ""
	if not api_key:
		# Blank-key api_key tenants → leave models empty (validate_models would
		# reject "api_key is blank on an enabled model" anyway)
		return

	# Insert the child row directly so Jarvis Settings.on_update is NOT triggered.
	# This keeps bench migrate free of network/admin calls and prevents any
	# validate_models() throw on partially-configured legacy data.
	row = frappe.get_doc(
		{
			"doctype": "Jarvis LLM Pool Model",
			"parent": "Jarvis Settings",
			"parenttype": "Jarvis Settings",
			"parentfield": "models",
			"provider": settings.get("llm_provider") or "",
			"model": settings.get("llm_model"),
			"base_url": settings.get("llm_base_url") or "",
			"credential_type": "api_key",
			"tier": "strong",
			"order": 0,
			"enabled": 1,
			"api_key": api_key,
		}
	)
	row.insert(ignore_permissions=True)
	frappe.db.commit()
