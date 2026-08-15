"""Copy every Jarvis User Preference row's greeting state onto the matching
Jarvis User Settings row (one canonical per-user DocType). Idempotent and
guarded by table existence, so it no-ops on a fresh CI DB where the old table
was never created. Runs before v1_17 drops the old DocType."""

import frappe

from jarvis.chat.usage import get_or_create_user_settings


def execute():
	if not frappe.db.table_exists("Jarvis User Preference"):
		return
	rows = frappe.db.sql(
		"SELECT user, business_greeting_state, business_greeting_chat_count, "
		"business_greeting_hidden_at_count FROM `tabJarvis User Preference`",
		as_dict=True,
	)
	for r in rows:
		user = r.get("user")
		if not user or not frappe.db.exists("User", user):
			continue
		# Ensure the canonical row exists (explicit owner=user so if_owner holds).
		get_or_create_user_settings(user)
		frappe.db.set_value(
			"Jarvis User Settings",
			{"user": user},
			{
				"business_greeting_state": r.get("business_greeting_state") or "",
				"business_greeting_chat_count": r.get("business_greeting_chat_count") or 0,
				"business_greeting_hidden_at_count": r.get("business_greeting_hidden_at_count") or 0,
			},
			update_modified=False,
		)
	frappe.db.commit()
