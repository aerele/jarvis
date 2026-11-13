"""Document Intake: capture listener (extended by a later task).

For now this module only owns the registry cache key, so the Jarvis Trigger
controller can bust it when an Intake trigger is saved or deleted.
"""

import frappe

INTAKE_MAP_CACHE_KEY = "jarvis:intake_map"


def _delete_cache_key() -> None:
	frappe.cache().delete_value(INTAKE_MAP_CACHE_KEY)


def clear_intake_cache() -> None:
	"""Bust the intake registry now AND again on commit, so a concurrent request
	cannot re-cache the map from the still-old committed rows (same reasoning as
	``jarvis.triggers.engine.clear_cache``)."""
	_delete_cache_key()
	try:
		frappe.db.after_commit.add(_delete_cache_key)
	except Exception:
		pass
