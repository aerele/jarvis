"""Narrow feature-data purge; accounting rows and company floors are untouched.

Two groups of hooks live here. The user and permission hooks purge one actor's snapshots and preferences.
The company hooks purge the per-company cache rows when a Company is deleted or renamed, when a
Transaction Deletion Record starts, runs or completes, and when the app is uninstalled. See `on_company_trash`.
"""

import frappe

from jarvis.payment_calendar import cache_state, cache_store, guard, read_log, store

# The Transaction Deletion Record statuses that purge: it is about to delete, is deleting, has deleted.
PURGE_STATUSES = ("Queued", "Running", "Completed")


def _purge_cache(actor):
	try:
		store.purge_actor(actor)
	except Exception:
		frappe.logger().warning("Payment Calendar lifecycle: purge_unavailable")


def _purge(actor, clear_preferences=False):
	try:
		store.purge_actor(actor)
		# A job admitted before this DB transaction commits must also disappear.
		frappe.db.after_commit.add(lambda: _purge_cache(actor))
		if clear_preferences and frappe.db.has_column("Jarvis User Settings", "payment_calendar_preferences"):
			frappe.db.set_value(
				"Jarvis User Settings",
				{"user": actor},
				"payment_calendar_preferences",
				"",
				update_modified=False,
			)
	except Exception:
		# User lifecycle must complete even if cache is unavailable. Every delivery
		# checks current access; remaining feature data also has a bounded TTL.
		frappe.logger().warning("Payment Calendar lifecycle: purge_unavailable")


def on_user_update(doc, method=None):
	old = doc.get_doc_before_save()

	def role_names(user):
		return {row.role for row in user.get("roles", [])}

	if (
		not old
		or not doc.enabled
		or any(doc.has_value_changed(field) for field in ("enabled", "user_type", "role_profile_name"))
		or role_names(old) != role_names(doc)
	):
		_purge(doc.name, clear_preferences=not doc.enabled)


def on_user_trash(doc, method=None):
	_purge(doc.name, clear_preferences=True)


def on_permission_change(doc, method=None):
	for actor in {doc.user, getattr(doc.get_doc_before_save(), "user", None)} - {None}:
		_purge(actor)


def _purge_site_cache():
	try:
		store.purge_site()
	except Exception:
		frappe.logger().warning("Payment Calendar lifecycle: purge_unavailable")


def reset_workspace():
	if frappe.db.has_column("Jarvis User Settings", "payment_calendar_preferences"):
		frappe.db.set_value(
			"Jarvis User Settings", {}, "payment_calendar_preferences", "", update_modified=False
		)
	_purge_site_cache()
	frappe.db.after_commit.add(_purge_site_cache)


# ------------------------------------------------------------------------------ cache purge
# Rules for every hook below. With the cache off (`effective_mode`, never the raw mode) a doc-event hook does no
# work. The purge runs in the CALLER's transaction (transaction control is off in a doc event), so a database
# error propagates and the Company delete or Transaction Deletion Record step fails as one unit. A job deadline
# propagates too. Any other error is logged by class (no name, no text) and the user's action goes on.


def on_company_trash(doc, method=None, *args, **kwargs):
	"""Company `on_trash`: purge the company's cache. Runs after ERPNext's own `on_trash` work."""
	_purge_company_cache(doc.name)


def on_company_rename(doc, method=None, old=None, new=None, merge=False, **kwargs):
	"""Company `after_rename`: purge the OLD name (the cache rows carry it as plain text)."""
	_purge_company_cache(old)


def on_deletion_record(doc, method=None, *args, **kwargs):
	"""Transaction Deletion Record `on_submit` and `on_change`: purge the company it deletes from.

	`on_submit` always purges. `on_change` purges only when the status is one of `PURGE_STATUSES`
	(a draft save, `Failed` and `Cancelled` change nothing).
	"""
	if method == "on_change" and doc.get("status") not in PURGE_STATUSES:
		return
	_purge_company_cache(doc.get("company"))


def before_uninstall():
	"""Delete this site's `jarvis:pcal:cache:*` keys and tell the `pcal_cache_purge` hooks about each company.

	The bench-wide slot keys (`make_key(shared=True)`) carry no site prefix, so they are not touched. The tables
	are dropped by Frappe with the app's DocTypes. The mode does not matter here. A statement error that keeps
	the transaction (say a table that is already gone) is logged, never allowed to block the uninstall.
	"""
	_guarded(lambda: frappe.cache.delete_keys(cache_state.CACHE_KEY_PREFIX), database_errors_propagate=False)
	_guarded(_notify_all_companies, database_errors_propagate=False)


def _purge_company_cache(company: str | None) -> None:
	if not company:
		return
	_guarded(lambda: _purge_when_active(company))


def _purge_when_active(company: str) -> None:
	if cache_state.cache_settings(lists_when_off=False).active:
		cache_store.purge_company(company)


def _notify_all_companies() -> None:
	# nosemgrep: frappe-sql-format-injection -- constant table name; no value
	for (company,) in frappe.db.sql(f"SELECT DISTINCT company FROM `tab{cache_store.HEAD}`"):
		cache_store.notify_purged(read_log.company_digest_or(company, read_log.SITE_DIGEST))


def _guarded(step, *, database_errors_propagate: bool = True) -> None:
	"""Run a hook step so that only a non-database error is logged and the user's action goes on.

	A job deadline and a database error that may have rolled the transaction back always propagate. Any other
	database error propagates too, unless `database_errors_propagate` is False. Other errors are logged by class.
	"""
	guard.guarded(step, "cache_hook", all_database_errors=database_errors_propagate)
