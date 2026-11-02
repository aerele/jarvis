"""Narrow feature-data purge; accounting rows and company floors are untouched."""

import frappe

from jarvis.payment_calendar import store


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
