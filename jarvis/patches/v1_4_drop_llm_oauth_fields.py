"""Drop legacy bench-side OAuth fields (now owned by openclaw inside the container).

REV-1 of the openclaw subscription work moves OAuth credential state into
the container's auth-profiles.json - openclaw refreshes tokens internally
via pi-ai. The bench no longer keeps refresh_token / access_token / expiry
on Jarvis Settings.

Safe because no production tenant ever completed a subscription connect
under the prior bench-driven flow (no real OAuth client IDs ever landed).
"""

import frappe
from frappe.utils.password import remove_encrypted_password

_FIELDS = [
	"llm_oauth_refresh_token",
	"llm_oauth_access_token",
	"llm_oauth_access_token_expires_at",
	"llm_oauth_account_email",
	"llm_oauth_connected_at",
	"llm_oauth_last_refresh_at",
]


def execute():
	for field in _FIELDS:
		# Clear any __Auth rows for the password fields (refresh_token, access_token).
		try:
			remove_encrypted_password("Jarvis Settings", "Jarvis Settings", field)
		except Exception:
			pass
		# Drop the column from the table.
		try:
			frappe.db.sql(f"ALTER TABLE `tabJarvis Settings` DROP COLUMN `{field}`")
		except Exception:
			pass  # column may already be gone
	frappe.db.commit()
