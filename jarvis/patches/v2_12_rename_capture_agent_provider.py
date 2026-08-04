"""Copy openclaw_provider into the renamed agent_provider column.

The pending-OAuth-capture provider key was renamed openclaw_provider ->
agent_provider (white-label). Frappe model-sync ADDS the new column blank and
leaves the old column in place (it never drops a removed field's column), so this
post_model_sync patch copies the values across.

Without it a capture minted before the migration and still un-consumed loses the
key ``_revoke_token`` uses to find the provider's revocation endpoint, so the
expiry sweeper would erase the ciphertext locally while leaving a LIVE refresh
token valid upstream. Captures live CAPTURE_TTL_MINUTES (30), so the exposure
window is small but real - and silent, because a missing key returns the terminal
"unsupported" state rather than raising.

Clobber-safe + idempotent: it only fills a row whose new column is still blank
from a non-blank old value, so a re-run can never overwrite a live value with a
stale one. The old openclaw_provider column is RETAINED this release as the
rollback net; a later contract patch drops it once the rename is proven.
"""

import frappe

DT = "Jarvis Pending OAuth Capture"


def execute():
	# The DocType is post-plan-05; a bench that predates it has no table to touch.
	if not frappe.db.table_exists(DT):
		return
	# Fresh install: the JSON only ever shipped agent_provider, so the old column
	# never existed and there is nothing to copy.
	if "openclaw_provider" not in frappe.db.get_table_columns(DT):
		return
	frappe.db.sql(
		"""
		UPDATE `tabJarvis Pending OAuth Capture`
		SET agent_provider = openclaw_provider
		WHERE COALESCE(agent_provider, '') = '' AND COALESCE(openclaw_provider, '') != ''
		"""
	)
