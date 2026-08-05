"""Dev-only helpers for the customer bench.

Gated by the System Manager role. Exposed as the ``bench reset-onboarding``
command (``jarvis.commands``) to wipe local state so the operator can run the
onboarding wizard fresh without manual DB surgery.

This is one of THREE callers that compose from ``jarvis.settings_reset`` -
``dev.reset_onboarding`` (FULL), ``onboarding._revoke_llm_connections`` (LLM) and
``onboarding._clear_admin_connection`` (CONNECTION | OAUTH_MARKERS). That module's
docstring enumerates FIVE reset PATHS, which is a different count and both are
right: the self-serve ladder is four user-facing depths served by two call sites,
because L1 and L2 clear no settings field at all. Do not "correct" either number
to match the other.

Here, ``reset_onboarding`` applies ``settings_reset.FULL`` (the entire
CONNECTION + LLM spec). The self-serve ladder in
``onboarding.request_workspace_reset`` offers granular depths; this CLI command is
the operator's blunt tool.

Companion to ``jarvis_admin_v2.api.dev.purge_customer`` on the admin side -
the admin button wipes admin-side records; this clears the customer bench.

Sandbox mode (the former ``Jarvis Settings.sandbox_mode`` toggle that used
to gate this module) was removed as a dead feature: System Manager was
always the real security boundary (sandbox mode was documented as
self-attested UX, not hardening), so ``reset_onboarding`` now gates on
System Manager alone via ``frappe.only_for``.
"""

import frappe

from jarvis import settings_reset
from jarvis.settings_reset import FULL as _FULL

SETTINGS = "Jarvis Settings"

# The field groups live in jarvis.settings_reset so the CLI and the self-serve
# workspace reset cannot drift apart. Re-exported here under the old names
# because tests iterate them rather than re-listing field names.
_RESET_CLEAR_FIELDS = _FULL.blank + _FULL.passwords
_PASSWORD_FIELDS = set(_FULL.passwords)
_RESET_NULL_FIELDS = _FULL.null
_RESET_ZERO_FIELDS = _FULL.zero
_RESET_DEFAULT_FIELDS = _FULL.defaults
_RESET_LITERAL_DEFAULTS = dict(_FULL.literals)


def reset_onboarding(wipe_data: bool = False) -> dict:
	"""Wipe local Jarvis Settings connection + LLM credentials (apply ``FULL``)
	so the customer bench can run the onboarding wizard from step 1 again.

	The CLI command ``bench reset-onboarding`` is the operator's blunt tool;
	``onboarding.request_workspace_reset`` offers customers a four-level ladder
	(rebuild only → + wipe data → + revoke LLM → + disconnect) so they can
	choose their reset depth. This function applies the deepest level (equivalent
	to the ladder's L4 + no rebuild polling).

	``wipe_data`` additionally deletes all workspace content (chats, skills,
	macros, triggers, learning artifacts, wiki, dashboards — the same
	``onboarding._WIPE_DOCTYPES`` set the ladder offers in its L2) for a true
	factory reset. The ``bench reset-onboarding`` CLI passes it by default;
	programmatic callers must opt in.

	Preserved (these are settings, not onboarded session state):
	  - jarvis_admin_url        (so the bench remembers which admin to point at)
	  - enabled, token_budget_monthly
	  - sampling: llm_temperature, llm_max_output_tokens
	  - llm_provider, llm_auth_mode (reset to a default, not blanked: auth mode is
	    `reqd`, so a blank one makes the next full .save() of the Single fail)

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
		from jarvis import admin_client

		try:
			admin_client.post_subscription_disconnect()
		except Exception:
			frappe.logger().info(
				"reset_onboarding: container subscription_disconnect skipped/failed (non-fatal)"
			)
		# The field loop below clears this bench's device credentials, but the
		# PAIRING lives in the container - so any surviving copy of the token
		# would keep chat access to a "reset" workspace. Same ordering reason as
		# the disconnect above: after the wipe the bench cannot reach admin.
		try:
			admin_client.unpair_chat_devices()
		except Exception:
			frappe.logger().info("reset_onboarding: container unpair skipped/failed (non-fatal)")

	settings_reset.apply(s, _FULL)
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
			"cleared_fields": settings_reset.cleared_fields(_FULL),
			"wiped_doctypes": wiped,
		},
	}
