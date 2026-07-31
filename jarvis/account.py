"""Customer-side wrappers for the SPA billing page (/jarvis/billing).

Written originally for the /app/jarvis-account Desk page, which has been
retired. The endpoints were never Desk-specific and moved to the SPA unchanged;
only Razorpay Checkout had to be reimplemented there (frontend/src/lib/
useRazorpay.js).

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
from jarvis.jarvis.pool_serialize import compute_pool_mode, pool_primary_model
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
		# A site whose account was reconnected elsewhere fails auth here forever, so
		# failing open sends it into a chat that cannot work. Ask the one question it
		# can still ask before shrugging.
		moved = _site_replacement()
		if moved.get("replaced"):
			return {"ready": False, "reason": "site_replaced", "replaced_notice": moved, "billing_notice": {}}
		# Fail open on ANY other admin error; deliberately no negative cache.
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


_REPLACED_CACHE_KEY = "jarvis:site_replacement"
_REPLACED_CACHE_TTL_S = 300


def _site_replacement() -> dict:
	"""Cached {replaced, at, moved_to}. Cached both ways: a replaced site would
	otherwise ask on every gate miss, and an unreplaced one on every admin blip."""
	cache = frappe.cache()
	hit = cache.get_value(_REPLACED_CACHE_KEY)
	if isinstance(hit, dict):
		return hit
	try:
		out = admin_client.site_replacement() or {}
	except Exception:
		out = {}
	verdict = {
		"replaced": bool(out.get("replaced")),
		"at": out.get("at") or "",
		"moved_to": out.get("moved_to") or "",
	}
	cache.set_value(_REPLACED_CACHE_KEY, verdict, expires_in_sec=_REPLACED_CACHE_TTL_S)
	return verdict


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
	  (the timestamp set when the oauth grant completes). Soft: banner, not
	  the wizard gate — the workspace was established and stays reachable.
	- ``"llm_setup"`` - creds missing AND this workspace never completed
	  onboarding: no LLM config ever confirmed and the subscription never
	  went Active (e.g. a failed-payment signup). Hard: routes back to the
	  wizard — chat cannot work and there is no history to protect.
	- ``"llm_pool_provisioning"`` - a pool is configured (pool mode) but
	  no sync has ever applied it to the container (first sync pending or
	  failed).
	- ``"container_provisioning"`` - all local checks passed, but admin reports
	  the container isn't chat-ready yet (chat_readiness != "Ready"). Set only by
	  the final ``_admin_chat_gate`` at the managed ready-exits; fail-open and
	  v1-tolerant (see ``_admin_chat_gate``).
	- ``None`` when ``ready`` is True.
	"""
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

	# Pool mode: being a pool is config INTENT, derived and committed at
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
	#
	# Keyed on pool MODE, not the narrower proxy_active: a BYO api-key pool has
	# no sidecar but still syncs through /llm-pool and stamps llm_pool_synced_at,
	# so gating it on the direct marker would strand it forever.
	if compute_pool_mode(settings):
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
			return _llm_missing_verdict(settings)
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
			return _llm_missing_verdict(settings)
	else:
		# Unknown auth_mode - treat as misconfigured; the wizard owns it.
		return {"ready": False, "reason": "llm_credentials"}

	return _admin_chat_gate()


def _llm_missing_verdict(settings) -> dict:
	"""LLM creds absent for the active mode: wizard or banner?

	An ESTABLISHED workspace gets the soft ``llm_credentials`` banner — any LLM
	config ever confirmed (a synced/connected marker survives creds expiry), or
	an Active subscription (the workspace-reset "disconnect AI model
	connections" option clears every marker; its owner reconnects via
	Settings -> AI models, and the wizard would dead-end them at signup's
	duplicate guard). Never lock such a workspace away from chat + history over
	a recoverable credential problem.

	A workspace that has NEVER had a working LLM and whose subscription never
	went Active is still MID-ONBOARDING (e.g. a failed-payment signup, refresh):
	``llm_setup`` hard-gates back to the wizard — chat cannot work there, and
	the half-created signup resumes via start_signup's authenticated fallback.
	Subscription state comes from admin; fail OPEN to the soft banner when it
	is unknown/unreachable."""
	never_synced = not (
		getattr(settings, "llm_direct_synced_at", None)
		or getattr(settings, "llm_pool_synced_at", None)
		or getattr(settings, "llm_oauth_connected_at", None)
	)
	if not never_synced:
		return {"ready": False, "reason": "llm_credentials"}
	try:
		from jarvis import admin_client

		sub_status = (admin_client.get_connection(timeout_s=8) or {}).get("subscription_status") or ""
	except Exception:
		sub_status = ""
	# Hard-gate ONLY the never-paid shapes. Active is established (the revoke
	# case); Suspended/Cancelled stay SOFT too — the renew/suspension banner
	# path owns them, and the wizard would dead-end them at the dedup guard.
	if sub_status in ("none", "Pending Payment", "Pending Verification"):
		return {"ready": False, "reason": "llm_setup"}
	return {"ready": False, "reason": "llm_credentials"}


@frappe.whitelist()
def get_llm_usage() -> dict:
	"""Real, curated Bifrost usage for the Monitor tab (System-Manager only,
	spec 7). Tenants with no Bifrost (proxy_active=0) short-circuit to the empty
	shape — no pointless admin round-trip. That now includes a BYO api-key POOL,
	which the fleet renders agent-direct with no sidecar at all."""
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
	"""Connection card for Settings, General: how this workspace's LLM config is
	SHAPED (``pool_mode`` / ``proxy_active`` / ``model_count`` / ``routing_mode``)
	and whether it is actually SERVING (``health``). Never returns token material.
	System-Manager only.

	``health`` is decided here, from what the bench already knows, and it is the
	only field the status badge may render. Admin's ``auth_profile_present`` is
	still passed through as ``auth_present`` because support wants the raw claim,
	but it is NOT a verdict about the workspace. A live 4-model pool whose two
	chat subscriptions cliproxy had loaded and was answering turns with still came
	back ``auth_profile_present: false, profile_ids: []``, and trusting that
	boolean painted a red "Not connected" over a workspace whose chat
	demonstrably worked (#561). Why admin sees no profiles is a control-plane bug
	of its own; the point here is that the answer never described a pool in the
	first place, so no value of it should decide this badge.

	That is the same misleading state that was already fixed once for DIRECT
	tenants, by short-circuiting them before the admin round-trip: the raw admin
	payload's leftover fields (a stale/default default_model with
	auth_profile_present false) made the SPA render "Not connected" for a direct
	tenant whose chat verifiably worked. The proxy branch kept trusting the
	boolean, so the identical state came back for pools.

	The honest local signal is the one ``is_ready_for_chat`` already gates chat
	on: the durable marker the fleet stamps when it CONFIRMS an apply. Tying the
	badge to it means the badge and the chat gate cannot disagree - a workspace
	chat let the customer into is green because the same evidence opened both, and
	a workspace whose config never reached its container is red for the same
	reason chat refuses it. See ``_llm_health``.

	Tenants with no Bifrost/cliproxy sidecar (proxy_active=0, which includes a BYO
	api-key pool) still short-circuit before the admin round-trip, mirroring
	get_llm_usage above - there is no proxy auth profile to report.

	``disconnected`` is a state of its own, and it has to be computed FIRST. The
	DIRECT short-circuit below returns before any admin round-trip, so a tenant
	whose connection was torn down (jarvis.onboarding.disconnect_llm) would
	otherwise fall into it and report a healthy-looking config while chat is
	dead. It is derived here rather than added to ``chat_readiness``: that field
	is a shared admin/bench contract with exactly four values, and the bench
	already knows its own config is empty without asking anybody."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	pool_mode = compute_pool_mode(settings)
	# Shape, not health. The SPA needs these to name the topology honestly: a
	# single Model/Provider/Auth-mode triple describes one credential, and a
	# failover pool is not one credential. pool_mode and proxy_active answer
	# DIFFERENT questions and must not be blurred - see compute_proxy_active.
	shape = {
		"pool_mode": pool_mode,
		"model_count": len([m for m in (settings.get("models") or []) if m.enabled]),
		"routing_mode": settings.get("routing_mode") or "",
		"sync_status": settings.get("last_sync_status") or "",
	}
	if not _has_llm_config(settings):
		return {
			**shape,
			"proxy_active": False,
			"disconnected": True,
			"health": "down",
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": "",
		}
	if not getattr(settings, "proxy_active", 0):
		return {
			**shape,
			"proxy_active": False,
			"disconnected": False,
			"health": _llm_health(settings, pool_mode),
			"auth_present": False,
			"oauth_expires_at": None,
			"profile_ids": [],
			"default_model": settings.get("llm_model") or "",
		}
	raw = _surface(admin_client.post_llm_auth_status) or {}
	data = raw.get("data", raw) or {}
	return {
		**shape,
		"proxy_active": True,
		"disconnected": False,
		"health": _llm_health(settings, pool_mode),
		"auth_present": bool(data.get("auth_profile_present")),
		"oauth_expires_at": data.get("openai_profile_expires_ms"),
		"profile_ids": data.get("profile_ids", []),
		# Admin answers this with the Bifrost virtual endpoint
		# ("openai_compat/jarvis-pool"), which is not a model the customer picked
		# and not one they would recognise. The bench knows which member the
		# container actually runs first, so prefer that and keep admin's value as
		# the fallback for a shape pool_primary_model cannot read.
		"default_model": pool_primary_model(settings) or data.get("default_model", ""),
	}


def _llm_health(settings, pool_mode: bool) -> str:
	"""``ok`` / ``applying`` / ``attention`` / ``down`` for a workspace that HAS a
	credential (``_has_llm_config`` has already said so).

	Every input is local. Nothing here asks admin, because the one thing admin was
	asked - "does a cliproxy auth profile exist" - turned out not to describe a
	pool at all (see get_llm_connection_status).

	  applying  - a save is in flight. The container is still on its previous
	              config, so neither "fine" nor "broken" is true yet.
	  down      - the container has NEVER confirmed this workspace's config, so it
	              is not serving it. This is the same evidence is_ready_for_chat
	              gates chat on, which is what keeps the badge and the gate from
	              contradicting each other.
	  attention - the container IS serving, but the last apply failed or the
	              fleet's own probe reports the chat subscription rejecting
	              requests. Both are workspace-level verdicts. Per-MODEL verdicts
	              deliberately stay out: AI models shows them per row, and one dead
	              member of a healthy failover chain is what failover is for.
	  ok        - serving, and the last apply came back clean.

	The status prefixes match @/lib/syncStatus's, which is the SPA's one
	translator for the same field, so the badge and the sync line cannot disagree
	about which of the three an audit string means.
	"""
	status = (settings.get("last_sync_status") or "").strip().lower()
	if status.startswith("pending"):
		return "applying"
	if not _llm_apply_confirmed(settings, pool_mode):
		return "down"
	if status.startswith("failed"):
		return "attention"
	# The fleet's own pool-wide subscription probe. Only an explicit rejection
	# counts: "unchecked" (and a no-op apply, which runs no probe at all) means
	# nobody looked, which is not evidence of a problem.
	return "attention" if (settings.get("last_subscription_status") or "") == "unverified" else "ok"


def _llm_apply_confirmed(settings, pool_mode: bool) -> bool:
	"""Has the fleet CONFIRMED an apply of the leg this workspace syncs through?

	Mirrors ``is_ready_for_chat``'s three legs exactly, and must keep mirroring
	them - the whole value of this signal is that chat and the connection badge
	read the same evidence. Pool marker for a pool (including a BYO api-key pool,
	which has no sidecar but is still pushed through /llm-pool and still stamps
	llm_pool_synced_at), the OAuth connect stamp for a direct subscription/oauth
	tenant, the direct apply marker otherwise.

	Legacy workspaces on both legs are backfilled by patch (v1_10 for the pool,
	v2_00_backfill_llm_direct_synced_at for direct), so an established tenant does
	not read as never-applied.
	"""
	if pool_mode:
		return bool(settings.get("llm_pool_synced_at"))
	if (settings.get("llm_auth_mode") or "api_key").strip() in ("subscription", "oauth"):
		return bool(settings.get("llm_oauth_connected_at"))
	return bool(settings.get("llm_direct_synced_at"))


def _has_llm_config(settings) -> bool:
	"""True when this workspace still has a usable AI CREDENTIAL.

	It asks whether a credential exists, not whether a provider NAME is written
	down, and that distinction is the whole point. llm_provider / llm_model are
	labels that on_update mirrors from models[0]; they are not proof of anything
	on their own, and a partial disconnect can leave them behind.

	The concrete case: jarvis.oauth.api.disconnect (Disconnect chat subscription)
	deliberately clears only the OAuth side, writes last_sync_status
	"disconnected", and LEAVES llm_provider / llm_model in place. Testing the
	labels therefore reported such a workspace as connected while its own sync
	status said "disconnected" - two sources of truth disagreeing, and the
	customer's Connection badge showing a healthy state over a workspace that
	cannot answer a turn. Observed on a real tenant: models[] empty,
	last_sync_status "disconnected", llm_provider still "OpenAI".

	What counts as a credential:
	  * models[] - the pool holds its own keys/accounts. Checked even when every
	    row is DISABLED, because a paused pool still HAS credentials and must not
	    read as disconnected.
	  * proxy_active - DERIVED from the config at save time
	    (compute_proxy_active) and reset to 0 when the config goes away, so a set
	    flag is itself proof a pool exists whatever the mirrors say.
	  * a direct tenant's stored api key, or its live OAuth connection.
	"""
	if settings.get("models") or getattr(settings, "proxy_active", 0):
		return True
	# Direct (non-pool) tenant. The credential lives in the flat fields, so read
	# the credential itself rather than the label beside it.
	if (settings.get("llm_auth_mode") or "") == "oauth":
		return bool(settings.get("llm_oauth_connected_at") or settings.get("llm_oauth_account_email"))
	try:
		return bool(settings.get_password("llm_api_key", raise_exception=False))
	except Exception:
		# Never let a password-store read break the status endpoint: an
		# unreadable secret is not evidence of a connection.
		return False


@frappe.whitelist()
def get_account() -> dict:
	"""Plan + validity + upgrade-eligible plans for the account page.

	System-Manager only, like its siblings above. Until now the only gate was
	the UI: SettingsDialog hides the ACCOUNT & BILLING rail group from non-SM
	users, and the (now retired) jarvis-account desk page carried
	roles=["System Manager"]. Neither stops a direct /api/method call, so any
	authenticated user could read the account's plan, status and validity. The
	SPA /jarvis/billing route deliberately adds no client-side guard of its own
	and leans on this one.
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
def start_upgrade(target_plan: str, provider: str | None = None) -> dict:
	"""Create the prorated order on the sub's gateway (or the ``provider``
	override); the page then opens that gateway's Checkout.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review): initiates a billing transaction tied to the site's admin
	account; non-admin staff shouldn't be able to upgrade the plan.
	"""
	require_jarvis_admin()
	return _surface(admin_client.start_upgrade, target_plan, provider=provider)


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
