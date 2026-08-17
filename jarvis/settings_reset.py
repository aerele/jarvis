"""One definition of what a reset clears on Jarvis Settings.

Two paths reset this site: the ``bench reset-onboarding`` CLI clears everything,
the self-serve workspace reset clears only the LLM subset. They kept separate
field lists that drifted - the CLI missed the credential the bench authenticates
with, the self-serve path blanked a ``reqd`` field. Both now compose from here.
"""

from typing import NamedTuple

import frappe

from jarvis._password_utils import clear_settings_password

SETTINGS = "Jarvis Settings"


class ResetSpec(NamedTuple):
	"""Fields to clear, grouped by the empty value each type takes."""

	blank: tuple = ()  # Data / Text -> ""
	passwords: tuple = ()  # -> "" AND the __Auth row dropped
	null: tuple = ()  # Datetime -> None; "" is not a date
	zero: tuple = ()  # Check / Int -> 0
	defaults: tuple = ()  # -> the doctype default; a reqd field must hold one
	literals: tuple = ()  # (field, value) where the doctype has no default
	clear_pool: bool = False  # the models[] child rows

	def __or__(self, other):
		return ResetSpec(
			self.blank + other.blank,
			self.passwords + other.passwords,
			self.null + other.null,
			self.zero + other.zero,
			self.defaults + other.defaults,
			self.literals + other.literals,
			self.clear_pool or other.clear_pool,
		)


# Subscription blobs live encrypted on the models[] rows, so the pool goes too.
LLM = ResetSpec(
	blank=("llm_model", "llm_base_url", "llm_oauth_account_email", "preset"),
	passwords=("llm_api_key",),
	null=("llm_oauth_connected_at", "llm_pool_synced_at", "llm_direct_synced_at"),
	zero=("proxy_active", "proxy_recommended"),
	# llm_auth_mode is reqd: blanking it leaves the Single unsaveable.
	defaults=("llm_auth_mode",),
	literals=(("llm_provider", "Anthropic"),),
	clear_pool=True,
)

CONNECTION = ResetSpec(
	blank=(
		# The customer email + password are the OAuth password-grant pair
		# admin_client._token() prefers over the api-key fallback, so leaving them
		# lets a reset bench still authenticate as the previous customer.
		"jarvis_admin_customer_email",
		"agent_url",
		# The opaque handle of the last accepted authority-fenced connection
		# (review plan 04 P0-5). A reset re-points this bench at a fresh tenancy,
		# so the previous handle must not linger and trip the identity check.
		"tenant_authority_handle",
		"chat_device_id",
		"chat_device_public_key",
		# Per-push statuses that otherwise read as "already sent" on a fresh site
		"last_sync_status",
		# The jarvis#841 dedup stamp names a config the PREVIOUS tenancy's
		# container held; left set, it could absorb the fresh tenancy's first
		# identical save inside its window.
		"llm_last_apply_fingerprint",
		"last_sync_warnings",
		"installed_apps_synced",
		"custom_skills_sync_status",
		"agent_skills_sync_status",
		"learned_skills_sync_status",
		"wiki_mirror_last_sync_status",
		# The role-based profile snapshot names agent slugs already deployed
		# to the PREVIOUS tenancy's container; a reset must not let a fresh
		# tenancy's first sync read it as "unchanged, no push needed".
		"role_profiles_pushed",
		# Same reasoning as role_profiles_pushed above: the cached admin config
		# and its version identity belong to the PREVIOUS tenancy's admin pull.
		# Left set, a fresh tenancy's first sync_role_profile_config would read
		# an unrelated version as "unchanged" and skip its first re-push.
		"role_profiles_config",
		"role_profiles_config_version",
		# Release notice + whitelabel branding of the previous tenancy. The SPA
		# falls back to "Jarvis" when agent_name is blank.
		"release_notice_message",
		"agent_name",
		"brand_logo",
		"brand_favicon",
	),
	passwords=(
		"jarvis_admin_api_key",
		"jarvis_admin_api_secret",
		"jarvis_admin_customer_password",
		"agent_token",
		"chat_device_private_key",
		"chat_device_token",
	),
	null=(
		"last_sync_at",
		# "this workspace has been chat-Ready" is a claim about the TENANCY that
		# earned it, and a reset ends that tenancy. Left set, the readiness gate
		# would keep failing OPEN through the new tenancy's provisioning
		# (account._has_been_chat_ready) - i.e. the reset site, whose container is
		# the one thing that definitely is not serving yet, would be the one told
		# its chat is fine. It re-earns the marker on its first real Ready. The
		# authority the claim was bound to goes with it.
		"chat_was_ready_at",
		"chat_ready_authority",
		"agent_token_issued_at",
		"custom_skills_synced_at",
		"agent_skills_synced_at",
		"learned_skills_synced_at",
		"role_profiles_pushed_at",
		"role_profiles_config_synced_at",
		"wiki_mirror_last_synced_at",
	),
	zero=(
		"agent_catalog_dirty",
		"agent_catalog_version",
		"release_notice_active",
		# Forget the accepted authority generation so the fresh tenancy's first
		# connection is accepted on its own terms, not rejected as "older" than
		# the previous tenancy's generation (review plan 04 P0-5).
		"tenant_authority_generation",
		# enable_role_profiles is now an admin-owned mirror (spec B4), not
		# operator intent - it must not carry the PREVIOUS tenancy's admin
		# decision into a fresh one with no config pulled yet.
		"enable_role_profiles",
	),
)

FULL = CONNECTION | LLM


def apply(settings, spec: ResetSpec) -> None:
	"""Clear ``spec`` on Jarvis Settings. db_set only - never save() - so
	on_update (creds push / pool sync) cannot fire mid-reset."""
	for field in spec.blank:
		settings.db_set(field, "")
	for field in spec.passwords:
		clear_settings_password(settings, field)
	for field in spec.null:
		settings.db_set(field, None)
	for field in spec.zero:
		settings.db_set(field, 0)
	meta = frappe.get_meta(SETTINGS)
	for field in spec.defaults:
		settings.db_set(field, meta.get_field(field).default)
	for field, value in spec.literals:
		settings.db_set(field, value)
	if spec.passwords:
		# A reset that leaves a cached bearer behind is not a reset: the token
		# outlives the credentials it was minted from.
		from jarvis.admin_client import clear_cached_token

		clear_cached_token()
	if spec.clear_pool:
		frappe.db.delete(
			"Jarvis LLM Pool Model",
			{"parent": SETTINGS, "parenttype": SETTINGS, "parentfield": "models"},
		)


def cleared_fields(spec: ResetSpec) -> list:
	"""What ``apply`` touched, for the caller's report."""
	return [
		*spec.blank,
		*spec.passwords,
		*spec.null,
		*spec.zero,
		*spec.defaults,
		*(field for field, _ in spec.literals),
		*(["models"] if spec.clear_pool else []),
	]
