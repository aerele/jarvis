"""One definition of what a reset clears on Jarvis Settings.

Five reset paths exist. Every settings field any of them clears comes from the
specs below — L1 and L2 clear none of their own (a container rebuild and a
workspace-content wipe touch nothing on this Single), which is why a spec is
named only from L3 down:

  1. ``dev.reset_onboarding`` (CLI ``bench reset-onboarding``) applies ``FULL``
  2-4. ``onboarding.request_workspace_reset`` offers a four-level ladder, each
       level optionally deeper:

       L1  (no flags)         rebuild the container only
       L2  wipe_data          + workspace content
       L3  revoke_llm         + LLM connections  (applies ``LLM``)
       L4  disconnect_after   + admin connection (applies ``CONNECTION |
                                OAUTH_MARKERS``, deferred until the rebuilt
                                container reports Ready)

  5. ``onboarding.disconnect_bench`` terminal action applies ``CONNECTION |
     OAUTH_MARKERS`` with no rebuild or poll

They kept separate field lists that drifted - the CLI missed the credential the
bench authenticates with (now ``CONNECTION``), the self-serve path blanked a
``reqd`` field (``llm_auth_mode``, now in ``LLM.defaults``). All now compose from
here, so the field lists cannot drift apart. The composition is load-bearing: L1
rebuilds but clears nothing; L2 adds workspace wipe; L3 adds ``LLM``; L4 and the
terminal action each add ``CONNECTION``.
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
		"last_sync_warnings",
		"installed_apps_synced",
		"custom_skills_sync_status",
		"agent_skills_sync_status",
		"learned_skills_sync_status",
		"wiki_mirror_last_sync_status",
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
	),
)

# The OAuth markers whose backing credential a DISCONNECT itself destroys, and
# nothing wider.
#
# Both paths that clear ``CONNECTION`` first have admin tear the container's OAuth
# auth-profile down (``api.tenant.prepare_bench_disconnect``), and an L4 rebuild
# would drop it regardless - OAuth creds never ride a rebuild. So after either,
# the container can no longer answer a turn on an OAuth grant, while these two
# fields on this Single still say it can.
#
# That is not cosmetic. ``account.is_ready_for_chat`` gates the whole LLM step on
# ``llm_oauth_connected_at`` for auth_mode oauth/subscription, and
# ``account._has_llm_config`` reads either field - so a bench that disconnected and
# then reconnected with the emailed code would SKIP LLM setup and land the customer
# in a chat whose container holds no credential. Plan edge case 21.
#
# Deliberately NOT the whole ``LLM`` spec: a disconnect is not a revoke. An api-key
# tenant's ``/secrets/llm.key`` and a pool's own keys survive the disconnect, so
# ``llm_api_key``, the models[] pool and ``llm_direct_synced_at`` /
# ``llm_pool_synced_at`` must stay - clearing them would make every disconnect a
# silent L3. ``llm_auth_mode`` stays too, and stays reqd: the workspace WAS set up
# for OAuth, and leaving it set is what routes readiness back through LLM setup
# rather than into the "unknown auth_mode" verdict.
#
# A strict subset of ``LLM`` (``llm_oauth_account_email`` is in its blank list,
# ``llm_oauth_connected_at`` in its null list), so ``FULL`` already covers it and
# the CLI cannot drift from the self-serve paths here.
OAUTH_MARKERS = ResetSpec(
	blank=("llm_oauth_account_email",),
	null=("llm_oauth_connected_at",),
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
