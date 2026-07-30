"""Dev-only helpers for the customer bench.

Gated by the System Manager role. Exposed as the ``bench reset-onboarding``
command (``jarvis.commands``) to wipe local state so the operator can run the
onboarding wizard fresh without manual DB surgery.

Companion to ``jarvis_admin_v2.api.dev.purge_customer`` on the admin side -
the admin button wipes admin-side records; this clears the customer bench.

Sandbox mode (the former ``Jarvis Settings.sandbox_mode`` toggle that used
to gate this module) was removed as a dead feature: System Manager was
always the real security boundary (sandbox mode was documented as
self-attested UX, not hardening), so ``reset_onboarding`` now gates on
System Manager alone via ``frappe.only_for``.
"""

import frappe
from frappe.utils.password import remove_encrypted_password

SETTINGS = "Jarvis Settings"

# Password fieldtype stores the real value in __Auth, not the doctype row.
# db_set("") only blanks the row's masked placeholder; __Auth retains the
# prior secret, so get_password() keeps returning it. We explicitly drop
# the __Auth row for these fields.
_PASSWORD_FIELDS = {
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_password",
	"agent_token",
	"chat_device_private_key",
	"chat_device_token",
	"llm_api_key",
}


# Fields cleared by reset_onboarding(), split by the empty value each type takes.
# Grouped here so tests can iterate without re-listing field names.

# Data / Text / Password / Attach Image -> ""
_RESET_CLEAR_FIELDS = (
	# Admin connection. The customer email + password are the OAuth password-grant
	# pair admin_client prefers over the api-key fallback, so leaving them behind
	# lets a "reset" bench still authenticate as the previous customer.
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_email",
	"jarvis_admin_customer_password",
	# Agent / container
	"agent_url",
	"agent_token",
	# Chat device pairing
	"chat_device_id",
	"chat_device_public_key",
	"chat_device_private_key",
	"chat_device_token",
	# Sync trace + the per-push statuses that otherwise read as "already sent"
	"last_sync_status",
	"last_sync_warnings",
	"installed_apps_synced",
	"custom_skills_sync_status",
	"agent_skills_sync_status",
	"learned_skills_sync_status",
	"wiki_mirror_last_sync_status",
	# LLM credentials + connection state
	"llm_model",
	"llm_api_key",
	"llm_base_url",
	"llm_auth_mode",
	"llm_oauth_account_email",
	"preset",
	# Release notice belonging to the previous tenancy
	"release_notice_message",
	# Whitelabel branding - the SPA falls back to "Jarvis" when blank
	"agent_name",
	"brand_logo",
	"brand_favicon",
)

# Datetime -> None. "" is not a date: MariaDB stores 0000-00-00 or rejects it.
_RESET_NULL_FIELDS = (
	"last_sync_at",
	"agent_token_issued_at",
	"llm_oauth_connected_at",
	"llm_pool_synced_at",
	"llm_direct_synced_at",
	"custom_skills_synced_at",
	"agent_skills_synced_at",
	"learned_skills_synced_at",
	"wiki_mirror_last_synced_at",
)

# Check / Int -> 0
_RESET_ZERO_FIELDS = (
	"proxy_active",
	"proxy_recommended",
	"agent_catalog_dirty",
	"agent_catalog_version",
	"release_notice_active",
)


def reset_onboarding(wipe_data: bool = False) -> dict:
	"""Wipe local Jarvis Settings connection + LLM credentials so the
	customer bench can run the onboarding wizard from step 1 again.

	``wipe_data`` additionally deletes all workspace content (chats, skills,
	macros, triggers, learning artifacts, wiki, dashboards — the same
	``onboarding._WIPE_DOCTYPES`` set the self-serve reset offers) for a true
	factory reset. The ``bench reset-onboarding`` CLI passes it by default;
	programmatic callers must opt in.

	Preserved (these are settings, not onboarded session state):
	  - jarvis_admin_url        (so the bench remembers which admin to point at)
	  - enabled, token_budget_monthly
	  - sampling: llm_temperature, llm_max_output_tokens
	  - llm_provider (reset to the doctype default "Anthropic" so the form
	    stays valid - it's a Select field, can't be blank)

	Does NOT call the admin-side purge - use
	``jarvis_admin_v2.api.dev.purge_customer`` on admin for that. The two
	together give a clean two-step reset; this one alone is enough when the
	admin record was already removed or never created.
	"""
	frappe.only_for("System Manager")
	s = frappe.get_single(SETTINGS)

	# Tear down the container's OAuth auth-profile FIRST — before the field loop
	# below wipes jarvis_admin_api_key / agent_url / agent_token (after which the
	# bench can no longer reach the container). The real access/refresh tokens
	# live in the container's auth-profiles.json (the bench only holds metadata),
	# so without this the old codex tokens linger and openclaw keeps serving the
	# OLD chat even after a "reset". Best-effort + non-fatal: a dev reset must
	# still succeed when admin/fleet is down, the tenant was already purged, or
	# nothing was connected. Only attempted when a container is actually wired up.
	if (s.get("agent_url") or "").strip():
		try:
			from jarvis import admin_client

			admin_client.post_subscription_disconnect()
		except Exception:
			frappe.logger().info(
				"reset_onboarding: container subscription_disconnect skipped/failed (non-fatal)"
			)

	# db_set (not save) throughout so on_update never fires mid-reset.
	for field in _RESET_CLEAR_FIELDS:
		s.db_set(field, "")
		if field in _PASSWORD_FIELDS:
			remove_encrypted_password(SETTINGS, SETTINGS, field)
	for field in _RESET_NULL_FIELDS:
		s.db_set(field, None)
	for field in _RESET_ZERO_FIELDS:
		s.db_set(field, 0)

	# Clear the models[] pool via a direct child-row delete rather than
	# s.set("models", []) + save(), so Jarvis Settings.on_update
	# (validate_models / admin pool-sync) does NOT fire during the reset.
	frappe.db.delete(
		"Jarvis LLM Pool Model",
		{
			"parent": SETTINGS,
			"parenttype": SETTINGS,
			"parentfield": "models",
		},
	)

	# Select field - must hold a valid option; reset to default.
	s.db_set("llm_provider", "Anthropic")
	frappe.db.commit()
	wiped: list = []
	if wipe_data:
		from jarvis.onboarding import _WIPE_DOCTYPES, _wipe_workspace_content

		_wipe_workspace_content()
		frappe.db.commit()
		wiped = list(_WIPE_DOCTYPES)

	return {
		"ok": True,
		"data": {
			"cleared_fields": [
				*_RESET_CLEAR_FIELDS,
				*_RESET_NULL_FIELDS,
				*_RESET_ZERO_FIELDS,
				"models",
				"llm_provider",
			],
			"wiped_doctypes": wiped,
		},
	}
