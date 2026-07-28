"""Keep admin's Jarvis Tenant.installed_apps (the fleet's skill/tool gating
signal) in step with the bench: app changes end with a migrate, so
after_migrate diffs the live list against the last-synced snapshot and rides
the existing admin sync on change.

The snapshot is stamped only after a send the admin persisted - stale costs
one redundant resync; premature would silence the gap forever. An empty
snapshot seeds the baseline WITHOUT a sync (no restart wave on deploy).
"""

from __future__ import annotations

import json

import frappe

SETTINGS = "Jarvis Settings"
FIELD = "installed_apps_synced"


def after_migrate() -> None:
	"""Best-effort; never blocks a migrate."""
	try:
		from jarvis import selfhost

		if selfhost.is_self_hosted():
			return
		if not _admin_configured():
			return
		current = _current_apps()
		synced = _synced_apps()
		if synced is None:
			record_synced_snapshot()
			return
		if current == synced:
			return
		pool = _pool_active()
		if pool is None:
			# Neither leg is safe to guess; stale snapshot retries next migrate.
			frappe.logger("jarvis.installed_apps").warning(
				"pool mode unreadable; deferring installed-apps resync"
			)
			return
		_enqueue_resync(synced, current, pool=pool)
	except Exception:
		frappe.log_error(
			title="installed-apps resync check failed",
			message=frappe.get_traceback(),
		)


def record_synced_snapshot() -> None:
	"""Stamp the live app list as synced. Never raises."""
	try:
		frappe.db.set_single_value(SETTINGS, FIELD, json.dumps(_current_apps()), update_modified=False)
	except Exception:
		pass


def _current_apps() -> list[str]:
	return sorted(frappe.get_installed_apps())


def _synced_apps() -> list[str] | None:
	"""Last-synced snapshot; None when never recorded or unreadable."""
	raw = frappe.db.get_single_value(SETTINGS, FIELD)
	if not raw:
		return None
	try:
		val = json.loads(raw)
	except ValueError:
		return None
	if not isinstance(val, list):
		return None
	return sorted(str(a) for a in val if a)


def _admin_configured() -> bool:
	"""jarvis_admin_url set (site config outranks the Settings field)."""
	try:
		if (frappe.conf.get("jarvis_admin_url") or "").strip():
			return True
		settings = frappe.get_cached_doc(SETTINGS)
		return bool((settings.get("jarvis_admin_url") or "").strip())
	except Exception:
		return False


def _pool_active() -> bool | None:
	"""Pool tenant? The resync MUST take the pool leg then - the single-model
	render knocks the container down to one credential. Derived from models[],
	not from the persisted proxy_active flag, which now means the narrower "a
	Bifrost/cliproxy sidecar is deployed" and is 0 for a BYO api-key pool that
	still has to resync through /llm-pool. None when unreadable; the caller
	defers rather than guesses."""
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	try:
		return compute_pool_mode(frappe.get_single(SETTINGS))
	except Exception:
		return None


def _enqueue_resync(synced: list[str], current: list[str], pool: bool) -> None:
	"""Enqueue the pool or single-model sync (same job ids as the real ops so
	triggers coalesce; workers re-read Settings at run time)."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		ADMIN_SYNC_RQ_TIMEOUT_S,
	)

	frappe.logger("jarvis.installed_apps").info(
		"installed_apps changed %s -> %s; enqueueing %s resync",
		synced,
		current,
		"pool" if pool else "creds-restart",
	)
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	common = {
		"queue": "long",
		"timeout": ADMIN_SYNC_RQ_TIMEOUT_S,
		"enqueue_after_commit": not run_inline,
		"now": run_inline,
		"deduplicate": True,
	}
	if pool:
		frappe.enqueue(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
			job_id="jarvis_settings_sync:pool",
			**common,
		)
	else:
		frappe.enqueue(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
			job_id="jarvis_settings_sync:restart",
			action="restart",
			**common,
		)
