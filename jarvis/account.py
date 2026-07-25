"""Customer-side wrappers for the /jarvis-account page.

Thin shims over admin_client (so the browser never holds admin api_key /
api_secret). Errors are normalized via the shared ``_surface`` helper from
onboarding so admin ValidationErrors arrive as clean ``frappe.throw`` toasts.

The page also reuses these existing onboarding endpoints directly under
their published names - no duplicates:

  - jarvis.onboarding.save_llm_creds  (LLM section save)
  - jarvis.onboarding.renew           (renew / reactivate / resume CTAs)
  - jarvis.onboarding.finish_payment  (post-Razorpay confirm)
"""

import frappe

from jarvis import admin_client, release_notice
from jarvis.onboarding import _surface
from jarvis.permissions import require_jarvis_admin

# R2-H4 chat-readiness gate, shared by boot, is_ready_for_chat and the send
# entitlement check. Only "Ready" is cached, so suspension/renewal is still seen
# promptly; 2 min keeps active-chat admin calls to ~1 per burst.
_CHAT_GATE_CACHE_KEY = "jarvis:chat_readiness_gate"
_CHAT_GATE_CACHE_TTL_S = 120


def _admin_chat_gate() -> dict:
	"""Last managed ready-gate: ask admin whether the customer's container is
	actually provisioned enough to serve chat. Fail-open and v1-tolerant.

	Called only AFTER the local signup + LLM-credential checks have passed, at
	the managed ready-exits of ``is_ready_for_chat`` — it is the final gate.

	Returns ``{"ready": True, "reason": None}`` UNLESS admin is reachable AND
	reports a ``chat_readiness`` != ``"Ready"``, in which case
	``{"ready": False, "reason": <code>, "detail": <admin's sentence>}``. The
	code is ``"subscription_suspended"`` for ``Suspended`` (renew) and
	``"container_provisioning"`` otherwise (wait) - kept distinct so a suspended
	customer isn't told to wait for a container that won't come back.

	- v1-tolerance: an ABSENT ``chat_readiness`` key (v1 admin, or a v2 that
	  doesn't surface it) means the control plane has no opinion → allow.
	- Resilience: ANY ``get_connection`` failure (unreachable / auth / timeout)
	  → allow. A control-plane hiccup must never bounce an already-provisioned
	  customer out of chat. We do NOT negative-cache, so a transient block or
	  error clears on the very next load rather than sticking for the TTL.
	"""
	cache = frappe.cache()
	cached = cache.get_value(_CHAT_GATE_CACHE_KEY)
	if cached:
		# The billing banner rides the cached verdict. Caching a bare flag would
		# hide an expiring/grace notice for the whole TTL on every ready load.
		# Tolerate the pre-upgrade shape (a bare 1) rather than re-asking admin.
		notice = cached.get("notice") if isinstance(cached, dict) else {}
		return {"ready": True, "reason": None, "billing_notice": notice or {}}
	try:
		conn = admin_client.get_connection(timeout_s=8) or {}
	except Exception:
		# Fail open on ANY admin error; deliberately no negative cache.
		return {"ready": True, "reason": None, "billing_notice": {}}
	# Refresh the locally-mirrored release notice on this gate's ~120s cadence so
	# an active user sees an activate/clear without waiting for the daily sync.
	release_notice.persist(conn.get("release_notice") or {})
	notice = conn.get("billing_notice") or {}
	if "chat_readiness" in conn and conn["chat_readiness"] != "Ready":
		suspended = conn["chat_readiness"] == "Suspended"
		return {
			"ready": False,
			"reason": "subscription_suspended" if suspended else "container_provisioning",
			# Carried even when blocked: "detail" is the ADMIN wording, and a
			# member who cannot renew needs a different call to action.
			"billing_notice": notice,
			# Admin owns the wording (jarvis_admin_v2.billing.entitlement) so the
			# two sides can't drift into different explanations. A v1/older admin
			# sends no reason; the SPA falls back to its own copy.
			"detail": conn.get("chat_readiness_reason") or "",
		}
	# Reachable + (Ready, or v1-absent) → allow and cache the positive verdict.
	cache.set_value(_CHAT_GATE_CACHE_KEY, {"notice": notice}, expires_in_sec=_CHAT_GATE_CACHE_TTL_S)
	return {"ready": True, "reason": None, "billing_notice": notice}


@frappe.whitelist()
def is_onboarded() -> dict:
	"""True iff Jarvis Settings holds an admin api_key. The wizard's
	completion-card branch and the account page's redirect guard share this.

	Pool-pending customers (paid but no tenant yet) still count as onboarded -
	they've completed signup; the agent_url just hasn't been wired up. The
	account page handles that state via tenant_status: pending.
	"""
	settings = frappe.get_single("Jarvis Settings")
	api_key = (
		settings.get_password(
			"jarvis_admin_api_key",
			raise_exception=False,
		)
		or ""
	).strip()
	return {"onboarded": bool(api_key)}


@frappe.whitelist()
def is_ready_for_chat() -> dict:
	"""Pre-flight check used by /jarvis-chat's page load to decide whether to
	render the chat surface or redirect the customer to /jarvis-onboarding.

	Stricter than ``is_onboarded`` - signup (admin api_key) AND a usable LLM
	credential for the active ``llm_auth_mode`` must be in place. A pool
	tenant mid-RE-save still counts as ready (the container keeps serving
	its previous config), but a pool whose FIRST apply never succeeded does
	not.

	Returns ``{ready: bool, reason: str | None}`` where ``reason`` is one of:

	- ``"signup"`` - jarvis_admin_api_key is empty (customer hasn't completed
	  the wizard's signup step).
	- ``"llm_credentials"`` - signup done, but LLM creds for the active
	  auth mode are missing. api_key mode needs llm_api_key + llm_provider +
	  llm_model; subscription / oauth modes need llm_oauth_connected_at
	  (the timestamp set when the oauth grant completes).
	- ``"llm_pool_provisioning"`` - a pool is configured (proxy_active) but
	  no sync has ever applied it to the container (first sync pending or
	  failed).
	- ``"container_provisioning"`` - all local checks passed, but admin reports
	  the container isn't chat-ready yet (chat_readiness != "Ready"). Set only by
	  the final ``_admin_chat_gate`` at the managed ready-exits; fail-open and
	  v1-tolerant (see ``_admin_chat_gate``).
	- ``None`` when ``ready`` is True.
	"""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		# Self-hosted: ready iff a validated openclaw connection is stored.
		# No admin signup / managed LLM creds involved.
		sh = frappe.get_single("Jarvis Settings")
		if (sh.agent_url or "").strip() and sh.selfhost_last_validated_at:
			return {"ready": True, "reason": None}
		return {"ready": False, "reason": "selfhost_connection"}

	settings = frappe.get_single("Jarvis Settings")

	admin_api_key = (
		settings.get_password(
			"jarvis_admin_api_key",
			raise_exception=False,
		)
		or ""
	).strip()
	if not admin_api_key:
		return {"ready": False, "reason": "signup"}

	# Pool mode: proxy_active is config INTENT, derived and committed at
	# save time BEFORE the async pool sync runs - it does not prove the
	# container ever received the pool. Gate on evidence of a successful
	# apply instead: llm_pool_synced_at, stamped by the pool-sync job on
	# every "ok" (tenants provisioned before the field existed are
	# backfilled by patch v1_10). A pool that has EVER applied stays ready
	# through a later re-save's transient pending/failed - the container
	# keeps serving its previous config. A fresh tenant whose FIRST sync
	# is still pending or failed is NOT ready: sending them to chat
	# guarantees failing turns while onboarding still shows "provisioning"
	# (JARVIS-2026-07-08 split-brain).
	#
	# Deliberately NOT a last_sync_status check: that field is shared with
	# the single-model sync, so a stale legacy "ok (reload via admin)" from
	# a queued creds job could falsely open the gate for a never-applied
	# pool.
	if getattr(settings, "proxy_active", 0):
		if getattr(settings, "llm_pool_synced_at", None):
			return _admin_chat_gate()
		return {"ready": False, "reason": "llm_pool_provisioning"}

	auth_mode = (getattr(settings, "llm_auth_mode", "") or "api_key").strip()

	if auth_mode == "api_key":
		llm_key = (
			settings.get_password(
				"llm_api_key",
				raise_exception=False,
			)
			or ""
		).strip()
		provider = (getattr(settings, "llm_provider", "") or "").strip()
		model = (getattr(settings, "llm_model", "") or "").strip()
		if not (llm_key and provider and model):
			return {"ready": False, "reason": "llm_credentials"}
		# Local key/provider/model presence is config INTENT (committed at save,
		# before the async admin apply runs) — it does NOT prove the container ever
		# received the creds. Gate on evidence of a CONFIRMED apply instead
		# (round-4 review R4-P0-6 / P1-10): llm_direct_synced_at is stamped only on
		# admin status="applied". A direct tenant that has EVER confirmed stays
		# ready through a later re-save's transient "applying" (the container keeps
		# serving its previous key); a FRESH tenant whose first apply is still
		# pending/failed is NOT ready — opening chat there guarantees failing turns
		# while onboarding still shows "applying". Legacy direct tenants are
		# backfilled by patch v2_00_backfill_llm_direct_synced_at.
		if not getattr(settings, "llm_direct_synced_at", None):
			return {"ready": False, "reason": "llm_provisioning"}
	elif auth_mode in ("subscription", "oauth"):
		# Both modes use the same local signal: llm_oauth_connected_at is
		# set (read-only) when the oauth grant completes and the admin
		# pushes the auth-profile blob to the container.
		if not getattr(settings, "llm_oauth_connected_at", None):
			return {"ready": False, "reason": "llm_credentials"}
	else:
		# Unknown auth_mode - treat as misconfigured; the wizard owns it.
		return {"ready": False, "reason": "llm_credentials"}

	return _admin_chat_gate()


@frappe.whitelist()
def get_llm_usage() -> dict:
	"""Real, curated Bifrost usage for the Monitor tab (System-Manager only,
	spec 7). DIRECT tenants (proxy_active=0, no Bifrost) short-circuit to the
	empty shape — no pointless admin round-trip."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	if not getattr(settings, "proxy_active", 0):
		return {
			"applicable": False,
			"period": None,
			"tokens_in": 0,
			"tokens_out": 0,
			"cost_usd": 0.0,
			"per_model": [],
			"used_vs_limit": {"used_usd": 0.0, "limit_usd": None},
		}
	data = _surface(admin_client.get_llm_usage) or {}
	data["applicable"] = True
	return data


@frappe.whitelist()
def get_llm_connection_status() -> dict:
	"""Connection card for the Monitor tab: auth profile present + OAuth expiry.
	Wrapper over admin_client.post_llm_auth_status, remapped to the customer
	contract field names. Never returns token material. System-Manager only.

	DIRECT tenants (proxy_active=0, no Bifrost/cliproxy sidecar) short-circuit
	before the admin round-trip, mirroring get_llm_usage above - there is no
	proxy auth profile to report. Before this, the raw admin payload's
	leftover fields (a stale/default default_model with auth_profile_present
	false) made the SPA's ConnectionPane render a misleading orange "Not
	connected" for a direct tenant whose chat verifiably works."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	if not getattr(settings, "proxy_active", 0):
		return {
			"proxy_active": False,
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": settings.get("llm_model") or "",
		}
	raw = _surface(admin_client.post_llm_auth_status) or {}
	data = raw.get("data", raw) or {}
	return {
		"proxy_active": True,
		"auth_present": bool(data.get("auth_profile_present")),
		"oauth_expires_at": data.get("openai_profile_expires_ms"),
		"profile_ids": data.get("profile_ids", []),
		"default_model": data.get("default_model", ""),
	}


@frappe.whitelist()
def get_account() -> dict:
	"""Plan + validity + upgrade-eligible plans for the account page.

	System-Manager only, like its siblings above. Until now the only gate was
	the UI: SettingsDialog hides the ACCOUNT & BILLING rail group from non-SM
	users, and the /jarvis-account desk page carries roles=["System Manager"].
	Neither stops a direct /api/method call, so any authenticated user could
	read the account's plan, subscription status and validity.
	"""
	require_jarvis_admin()
	return _surface(admin_client.get_account_summary)


@frappe.whitelist()
def preview_upgrade(target_plan: str) -> dict:
	"""Prorated amount for the upgrade modal's per-plan cards.

	Same gate as ``start_upgrade`` below: this is the read half of the same
	billing transaction, reachable only from the SM-only desk page, and it
	spends an admin round-trip per call. Whoever may not upgrade the plan has
	no business pricing the upgrade either.
	"""
	require_jarvis_admin()
	return _surface(admin_client.preview_upgrade, target_plan)


@frappe.whitelist()
def start_upgrade(target_plan: str) -> dict:
	"""Create the prorated Razorpay order; the page then opens Checkout.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review): initiates a billing transaction tied to the site's admin
	account; non-admin staff shouldn't be able to upgrade the plan.
	"""
	require_jarvis_admin()
	return _surface(admin_client.start_upgrade, target_plan)


@frappe.whitelist()
def cancel_plan_at_period_end() -> dict:
	"""Schedule a period-end cancellation of the site's BILLING plan.

	Named plan, not subscription: in this app "subscription" means the LLM
	provider subscription (disconnect_subscription / DirectSubscriptionCard),
	and confusing the two would be expensive.

	Gated on System Manager for the same reason as start_upgrade: it changes
	the billing state of the site's admin account. The gate runs BEFORE the
	admin round-trip so an unauthorized caller never spends a network call.
	"""
	require_jarvis_admin()
	out = _surface(admin_client.cancel_plan_at_period_end)
	_bust_chat_gate()
	return out


@frappe.whitelist()
def resume_plan() -> dict:
	"""Undo a scheduled cancellation (System Manager, as above)."""
	require_jarvis_admin()
	out = _surface(admin_client.resume_plan)
	_bust_chat_gate()
	return out


@frappe.whitelist()
def reauthorize_autopay() -> dict:
	"""Start re-arming auto-renewal; the page then opens a mandate Checkout.

	No chat-gate bust: this only creates a Razorpay object, it changes no
	entitlement. confirm_payment is what flips autorenew back on.
	"""
	require_jarvis_admin()
	return _surface(admin_client.reauthorize_autopay)


@frappe.whitelist()
def preview_downgrade(target_plan: str) -> dict:
	"""Describe a downgrade for the picker. SM-only, same gate as upgrade."""
	require_jarvis_admin()
	return _surface(admin_client.preview_downgrade, target_plan)


@frappe.whitelist()
def start_downgrade(target_plan: str) -> dict:
	"""Schedule a downgrade (next cycle). Monthly autopay returns a
	subscription id for a ₹0 mandate Checkout; Annual just schedules.

	Chat-gate bust: a downgrade never changes entitlement until the boundary,
	so no bust is needed - the container keeps serving the current plan."""
	require_jarvis_admin()
	return _surface(admin_client.start_downgrade, target_plan)


@frappe.whitelist()
def cancel_scheduled_downgrade() -> dict:
	"""Revoke a scheduled downgrade (SM-only)."""
	require_jarvis_admin()
	return _surface(admin_client.cancel_scheduled_downgrade)


def _bust_chat_gate() -> None:
	"""Drop the chat-readiness cache after a billing state change.

	Belt-and-braces: cancelling does not itself change readiness (entitlement
	runs to period end), but the pane re-reads immediately afterwards and a
	stale positive verdict would be confusing. Costs one Redis DEL."""
	try:
		frappe.cache().delete_value(_CHAT_GATE_CACHE_KEY)
	except Exception:
		pass
