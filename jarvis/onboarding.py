"""Onboarding - store the admin token + container connection into Jarvis
Settings, and thin server wrappers the onboarding page calls (so the browser
never holds admin creds). admin_client returns already-unwrapped admin data."""

import json

import frappe

from jarvis import admin_client, release_notice
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)
from jarvis.permissions import grant_onboarding_admin, require_jarvis_admin


def _require_admin_url() -> None:
	"""Raise ValidationError if no admin URL is configured deliberately.

	start_signup must target a deliberately-chosen control plane. The admin
	URL resolves (admin_client._admin_url ->
	hooks.get_default_admin_url) in this order: (1) ``jarvis_admin_url`` in
	site_config / common_site_config (via frappe.conf), (2) Jarvis Settings.
	jarvis_admin_url per-customer override, (3) the hardcoded fallback for
	fresh installs. Silently falling through to (3) on a multi-site bench may
	land the wrong tenancy, so require (1) or (2) to be set - only (3)-alone is
	the fail-fast case. (1) wins when both are set (site config is the
	deployment's source of truth; a stale doctype value must not mask it).
	"""
	configured = (frappe.db.get_single_value("Jarvis Settings", "jarvis_admin_url") or "").strip() or (
		frappe.conf.get("jarvis_admin_url") or ""
	).strip()
	if not configured:
		raise frappe.ValidationError(
			"No Jarvis Admin URL configured. Set Jarvis Settings -> Jarvis "
			"Admin URL, or 'jarvis_admin_url' in site_config.json, before "
			"continuing onboarding."
		)


def _throw_admin_error(e) -> None:
	"""Map one already-raised admin_client exception to the clean frappe.throw
	the onboarding page renders. Extracted from _surface so a caller can inspect
	the raised error first, then fall back to the identical mapping for every
	other admin-side failure. Always raises."""
	if isinstance(e, AdminValidationError):
		frappe.throw(str(e))
	if isinstance(e, AdminAuthError):
		frappe.throw(f"admin authentication failed; check the bench's admin credentials. ({e})")
	if isinstance(e, AdminUnreachableError):
		frappe.throw(f"admin is unreachable right now; try again in a moment. ({e})")
	if isinstance(e, AdminRateLimitedError):
		retry = e.retry_after_seconds or 0
		retry_str = f"retry in {retry}s" if retry > 0 else "retry shortly"
		frappe.throw(f"admin rate-limited the request; {retry_str}.")
	raise e


def _surface(fn, *args, **kwargs):
	"""Run an admin_client call; re-raise every admin-side error as a clean
	frappe.ValidationError so the onboarding page renders a red toast with
	an operator-actionable message instead of a long traceback dump.

	Sprint-3 (2026-06-16 review): the docstring promised "no traceback for
	any admin failure" but only AdminValidationError was actually caught;
	AdminUnreachableError / AdminAuthError / AdminRateLimitedError fell
	through and surfaced as raw 500s. Now ALL four are caught:

	- AdminValidationError  -> "<message>"
	- AdminAuthError        -> "admin authentication failed; check your "
	                            "site's bench-admin credentials"
	- AdminUnreachableError -> "admin is unreachable; check network / "
	                            "service status and try again"
	- AdminRateLimitedError -> "rate-limited by admin; retry in Ns" where
	                            N is the AdminRateLimitedError.retry_after_seconds
	                            (admin's response hint).
	"""
	try:
		return fn(*args, **kwargs)
	except (AdminValidationError, AdminAuthError, AdminUnreachableError, AdminRateLimitedError) as e:
		_throw_admin_error(e)


def write_connection(data: dict) -> None:
	"""Persist native admin credentials + container connection into Jarvis
	Settings via db_set (no on_update creds-push retrigger during onboarding).

	The four Password fields (jarvis_admin_api_key/_secret,
	jarvis_admin_customer_password, agent_token) go through
	set_settings_password instead of a bare db_set: db_set writes exactly what
	it's given straight into tabSingles with no encryption (only
	Document.save()'s _save_passwords path encrypts a Password field), so a
	bare db_set of a real secret sat there in plaintext. set_settings_password
	encrypts into __Auth first, then db_sets only the mask - preserving the
	"no on_update retrigger" property this function exists for."""
	if not isinstance(data, dict):
		return
	from jarvis._password_utils import set_settings_password

	s = frappe.get_single("Jarvis Settings")
	if data.get("api_key"):
		set_settings_password(s, "jarvis_admin_api_key", data["api_key"])
	if data.get("api_secret"):
		set_settings_password(s, "jarvis_admin_api_secret", data["api_secret"])
	# OAuth password-grant credentials. ``customer`` is the admin-side login
	# (email, the grant username); ``customer_password`` is the durable secret
	# the bench exchanges for short-lived bearer tokens. The email arrives in
	# the signup response; the password arrives later (verified poll / flag-off
	# signup), so each is persisted independently when present.
	if data.get("customer"):
		s.db_set("jarvis_admin_customer_email", data["customer"])
	if data.get("customer_password"):
		set_settings_password(s, "jarvis_admin_customer_password", data["customer_password"])
	if data.get("agent_url"):
		s.db_set("agent_url", data["agent_url"])
	if data.get("agent_token"):
		set_settings_password(s, "agent_token", data["agent_token"])
	# NB: the release notice is deliberately NOT mirrored here. Several callers
	# pass a partial payload (a password, a customer email), and an absent notice
	# key means "cleared" - which would drop a live notice. It is persisted only
	# where a full get_connection payload is in hand: sync_connection and the
	# chat gate.


@frappe.whitelist()
def sync_connection() -> dict:
	"""Pull the container connection from admin and store it. Daily scheduled +
	the page's 'Sync connection' button. No-op until onboarded/assigned.

	Gated on System Manager: writes admin credentials and container connection
	into Jarvis Settings (jarvis_admin_api_key, agent_url, agent_token). The
	scheduler runs as Administrator which bypasses only_for. Sprint-1 Important
	from the 2026-06-16 code review.
	"""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	api_key = settings.get_password("jarvis_admin_api_key", raise_exception=False) or ""
	api_secret = settings.get_password("jarvis_admin_api_secret", raise_exception=False) or ""
	if not (api_key and api_secret):
		return {"synced": False, "reason": "not onboarded"}
	data = admin_client.get_connection()
	# Outside the agent_url branch: a payload without one must still be able to
	# raise or clear the release notice, and this is the only refresh an idle
	# bench gets.
	release_notice.persist(data.get("release_notice") or {})
	if data.get("agent_url"):
		write_connection(data)
		return {"synced": True, "tenant_status": data.get("tenant_status")}
	return {"synced": False, "tenant_status": data.get("tenant_status", "pending")}


@frappe.whitelist()
def list_plans() -> list:
	return admin_client.get_plans()


@frappe.whitelist()
def get_preset_catalog() -> list:
	"""Preset catalog for the desk onboarding step + the /ai SPA route.
	Thin wrapper over admin_client (fetch/cache/bundled fallback)."""
	return admin_client.get_preset_catalog()


@frappe.whitelist()
def save_llm_pool(models: str | list, preset: str | None = None, routing_mode: str = "failover") -> dict:
	"""Write the customer's multi-model LLM pool into Jarvis Settings.models[]
	(+ preset, routing_mode) and let the existing on_update pipeline validate
	(validate_models), derive proxy_active, mirror models[0] into legacy llm_*,
	and sync DIRECT (/llm-creds) vs PROXY (/llm-pool) via admin.

	``models`` MUST stay annotated: with Frappe's
	``require_type_annotated_api_methods`` enforced (declared in hooks.py),
	an un-annotated whitelisted param 500s the request before the body runs
	(JARVIS-2026-07-08 incident, fault a).

	System-Manager-gated. routing_mode is always 'failover' in v1. preset is an
	admin-catalog key or None; validated against the fetched catalog."""
	require_jarvis_admin()
	if isinstance(models, str):
		models = json.loads(models)
	if not isinstance(models, list) or not models:
		raise frappe.ValidationError("models must be a non-empty list")
	if routing_mode != "failover":
		raise frappe.ValidationError("routing_mode must be 'failover' in v1")

	preset = (preset or "").strip()
	if preset:
		keys = {e.get("key") for e in admin_client.get_preset_catalog()}
		if preset not in keys:
			raise frappe.ValidationError(f"unknown preset '{preset}'")

	from jarvis.jarvis.pool_serialize import (
		_get_password,
		_model_accounts,
		normalize_provider,
	)

	s = frappe.get_single("Jarvis Settings")

	# Preserve secrets on re-save. get_llm_config never returns api_key / oauth_blob,
	# so the reloaded editor posts a BLANK secret for anything the user didn't
	# re-enter this session. Snapshot the currently-stored secrets and merge them
	# back into any row/account left blank, so editing a pool (e.g. changing a
	# model id or reordering) does not silently wipe a previously-working
	# credential. Keyed by canonical provider (api keys are per-vendor) and by
	# account_ref (server-stable) respectively.
	prior_api_keys = {}
	prior_blobs = {}
	for pm in s.get("models") or []:
		if (pm.credential_type or "api_key") == "api_key":
			pk = _get_password(pm, "api_key")
			if pk:
				prior_api_keys[normalize_provider(pm.provider)] = pk
		else:
			for a in _model_accounts(pm):
				ref = (a.get("account_ref") if hasattr(a, "get") else "") or ""
				blob = (a.get("oauth_blob") if hasattr(a, "get") else "") or ""
				if ref and blob:
					prior_blobs[ref] = blob

	s.set("models", [])
	for i, m in enumerate(models):
		sub = m.get("subscription")
		cred_type = "subscription" if sub else "api_key"
		provider = normalize_provider(m.get("provider"))
		row = {
			"provider": provider,
			"model": (m.get("model") or "").strip(),
			"base_url": (m.get("base_url") or "").strip(),
			"tier": m.get("tier") or "strong",
			"order": m.get("order", i),
			"credential_type": cred_type,
			"enabled": 1,
		}
		if cred_type == "api_key":
			# Blank posted key + a stored key for this vendor → keep the stored one.
			row["api_key"] = (m.get("api_key") or "").strip() or prior_api_keys.get(provider, "")
		else:
			row["rotation"] = (sub or {}).get("rotation") or "sticky"
			# Subscription accounts are stored as a JSON string in the
			# `subscription_accounts` Password field ON the model row (a child of
			# the Jarvis Settings Single). Frappe's ORM does NOT persist/auto-load
			# grandchild tables, so the previous accounts[] grandchild Table never
			# saved. As a child-row Password field it is encrypted at rest via the
			# normal save() -> _save_passwords path (identical to `api_key`), so
			# oauth_blobs never sit in plaintext in the DB column.
			merged_accounts = []
			for a in (sub or {}).get("accounts") or []:
				a = dict(a)
				if not (a.get("oauth_blob") or "").strip():
					ref = a.get("account_ref") or ""
					if ref and prior_blobs.get(ref):
						a["oauth_blob"] = prior_blobs[ref]
				merged_accounts.append(a)
			row["subscription_accounts"] = json.dumps(merged_accounts)
		s.append("models", row)

	s.preset = preset
	s.routing_mode = routing_mode
	# A models[]-based config never uses the flat direct-OAuth fields (a pooled
	# chat subscription's creds live in models[].subscription_accounts, served by
	# cliproxy — not auth-profiles.json). Clear any stale direct chat-subscription
	# display state left over from a prior DIRECT connection so
	# get_direct_subscription_status / the account UI can't later misread it as a
	# live direct connection after the tenant migrated to a pool. auth_mode is
	# re-mirrored from models[0] by on_update.
	s.llm_oauth_account_email = ""
	s.llm_oauth_connected_at = None
	# save() -> on_update -> _on_update_unified_llm: validate_models (throws),
	# compute_proxy_active, mirror models[0], enqueue pool/creds sync.
	s.save(ignore_permissions=True)
	frappe.db.commit()

	row = (
		frappe.db.get_value(
			"Jarvis Settings", "Jarvis Settings", ["last_sync_at", "last_sync_status"], as_dict=True
		)
		or {}
	)
	return {
		"last_sync_at": str(row.get("last_sync_at") or ""),
		"last_sync_status": row.get("last_sync_status") or "",
		"proxy_active": bool(frappe.db.get_single_value("Jarvis Settings", "proxy_active")),
	}


@frappe.whitelist()
def start_signup(email: str, company: str, plan: str) -> dict:
	"""Guest signup → store the api_token → return the Razorpay handles for Checkout.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review): the customer signing up is configuring Jarvis for their entire
	site, which is a System Manager operation on Frappe by convention.
	Without this, a non-admin staff user could initiate a paid signup using
	a different email/company under the site's admin contract.

	Requires ``Jarvis Settings.jarvis_admin_url`` to be set first. Otherwise
	admin_client falls back to the DEFAULT_ADMIN_URL, which on a multi-site
	bench may be the wrong control plane. Fail fast with an actionable
	error instead of silently landing the wrong tenancy.

	Two response shapes depending on admin's
	``require_email_verification`` flag:
	  - flag OFF (legacy): admin returns a Razorpay order; wizard goes
	    straight to Checkout.
	  - flag ON: admin returns ``pending_verification: True`` and no
	    order; wizard shows a "check your email" screen and polls
	    ``check_signup_payment_state`` after the customer clicks the
	    magic link. Either shape persists api_key + api_secret on the
	    bench so the poll endpoint can authenticate.
	"""
	require_jarvis_admin()
	_require_admin_url()
	data = _surface(admin_client.signup, email, company, plan)
	# Persist whatever credentials the response carries. The guard also fires
	# on ``customer`` so the OAuth grant username is stored even if a future
	# admin response shape omits api_key/api_secret. write_connection skips
	# empty fields individually. customer_password is present only on the
	# flag-off path (verify-on defers it to the poll).
	if data.get("api_key") or data.get("api_secret") or data.get("customer"):
		write_connection(
			{
				"api_key": data.get("api_key", ""),
				"api_secret": data.get("api_secret", ""),
				"customer": data.get("customer", ""),
				"customer_password": data.get("customer_password", ""),
			}
		)
	# PART 4 REVISED, TASK 48: the onboarding user becomes a Jarvis Admin. Grant
	# here (after the admin signup call + connection write succeed) as the EARLY
	# durable stamp that survives the multi-session email-verify flow. Idempotent
	# with the finish_payment grant. On a fresh bench nobody holds Jarvis Admin,
	# so the require_jarvis_admin gate above still requires the first onboarder to
	# be the SM site owner — a plain Jarvis User is rejected before reaching here.
	grant_onboarding_admin()
	return data


@frappe.whitelist()
def get_account_defaults() -> dict:
	"""Prefill for the onboarding Account step so the customer doesn't retype what
	the site already knows: the caller's email + a default company. Company is the
	user/global default when set, else the site's sole Company; ``companies`` lists
	options for a client datalist when several exist. Silent no-op (blank / empty
	list) on sites without the Company doctype or read permission.

	Ports the desk auto-fetch (jarvis_onboarding.js, commit 1507495) to the server
	because the SPA has no ``frappe.defaults``. System-Manager only (the onboarding
	route is SM-gated).
	"""
	require_jarvis_admin()
	user = frappe.session.user
	email = (frappe.db.get_value("User", user, "email") or user) if user and user != "Guest" else ""

	company, companies = "", []
	try:
		company = (
			frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company") or ""
		)
		companies = [c.name for c in frappe.get_all("Company", fields=["name"], limit=20)]
		if not company and len(companies) == 1:
			company = companies[0]
	except Exception:
		# No Company doctype / no read permission — leave blank so the client keeps
		# its placeholder, exactly like the desk auto-fetch's silent no-op.
		company, companies = "", []
	return {"email": email, "company": company, "companies": companies}


@frappe.whitelist()
def check_signup_payment_state() -> dict:
	"""Wizard-poll endpoint for the email-verification window.

	Calls admin's ``get_signup_payment_state`` (authenticated via the
	api_key + api_secret persisted at start_signup time) and returns the
	response unchanged. The wizard JS branches on
	``pending_verification`` to decide whether to keep showing the
	"check your email" screen or to open Razorpay Checkout.

	Gated on System Manager for the same reason as start_signup: this is
	part of the same paid-signup flow on the customer's bench.
	"""
	require_jarvis_admin()
	_require_admin_url()
	data = _surface(admin_client.get_signup_payment_state)
	# On the verified poll (email confirmed) admin delivers the customer's
	# OAuth password once. Persist it so subsequent admin calls use bearer
	# auth. Absent on the not-yet-verified poll and on the flag-off path.
	if isinstance(data, dict) and data.get("customer_password"):
		write_connection({"customer_password": data["customer_password"]})
	return data


@frappe.whitelist()
def finish_payment(payload: dict | str) -> dict:
	"""Confirm Checkout success → store the returned container connection.

	Gated on System Manager: writes container connection (agent_url,
	agent_token) into Jarvis Settings.
	"""
	require_jarvis_admin()
	if isinstance(payload, str):
		payload = json.loads(payload)
	data = _surface(admin_client.confirm_payment, payload)
	write_connection(data)
	# PART 4 REVISED, TASK 48: the AUTHORITATIVE "onboarding AND paying" grant —
	# make the paying user a Jarvis Admin once payment confirms and the connection
	# is written. Idempotent with the start_signup grant.
	grant_onboarding_admin()
	return data


@frappe.whitelist()
def renew() -> dict:
	"""Existing customer initiates a renewal payment; returns the Razorpay handles
	for Checkout. The page then completes Checkout and calls finish_payment.

	Gated on System Manager: initiates a billing transaction tied to the
	site's admin account.
	"""
	require_jarvis_admin()
	return _surface(admin_client.renew)


@frappe.whitelist()
def save_llm_creds(
	provider: str,
	model: str,
	api_key: str = "",
	base_url: str = "",
	auth_mode: str = "api_key",
	force: bool = False,
) -> dict:
	"""Save LLM provider/model/auth mode + (api_key when applicable) and let
	on_update re-render openclaw.json. Returns the on_update outcome
	(last_sync_status) so the page can tell the customer whether their
	agent is fully ready.

	REV-1: ``auth_mode="oauth"`` lets the OAuth poll-success path save
	without requiring an api_key - credentials live in the container's
	auth-profiles.json (pushed via the separate push_oauth_blob path).

	``force`` (REV-3, 2026-06-12): when True, bypass on_update's diff
	gate (``_classify_llm_change`` returning None when no field changed)
	so the admin/fleet-agent push fires even on a no-op save. Required
	in the complete_paste_signin path because that flow:
	  - pushes the OAuth blob (which lives in auth-profiles.json, not
	    Jarvis Settings, so the bench's diff classifier doesn't see it)
	  - then needs fleet-agent to re-render openclaw.json AND restart
	    the container so openclaw picks up the new auth profile.
	Without ``force=True``, a customer re-authorizing with the same
	provider+model gets a stale openclaw.json + no restart, and openclaw
	keeps serving the previous (broken) state. Verified live 2026-06-11.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review): a non-admin staff user could otherwise flip ``llm_base_url``
	to an attacker-controlled URL and exfiltrate chat context through
	future LLM calls.
	"""
	require_jarvis_admin()
	if not provider or not model:
		raise frappe.ValidationError("provider and model are required")
	if auth_mode not in {"api_key", "oauth"}:
		raise frappe.ValidationError(f"unsupported auth_mode: {auth_mode}")
	if auth_mode == "api_key" and not api_key:
		raise frappe.ValidationError("api_key is required when auth_mode=api_key")
	s = frappe.get_single("Jarvis Settings")
	if auth_mode == "api_key":
		# API-key path: write models[0] so the table is the source of truth.
		# on_update's _on_update_unified_llm mirrors models[0] back to the
		# legacy fields (llm_provider / llm_model / llm_base_url / llm_auth_mode
		# / llm_api_key) so all downstream readers continue to work unchanged.
		#
		# We also set llm_api_key in-memory so that validate()'s
		# _validate_auth_mode_requirements passes before on_update runs (the
		# validator checks the in-memory value first, then falls back to DB).
		s.set("models", [])
		s.append(
			"models",
			{
				"provider": provider,
				"model": model,
				"base_url": (base_url or "").strip(),
				"credential_type": "api_key",
				"api_key": api_key,
				"tier": "strong",
				"order": 0,
				"enabled": 1,
			},
		)
		# Satisfy _validate_auth_mode_requirements (reads in-memory before DB).
		s.llm_api_key = api_key
	else:
		# TODO: represent direct-OAuth single-model in the models table (future)
		# For now, leave the direct-OAuth path on the legacy field write.
		# Clear any stale api_key models rows so on_update takes the legacy
		# classify/sync path rather than the unified table path (which would
		# mirror models[0].credential_type='api_key' back over the oauth mode).
		existing_enabled = [m for m in (s.get("models") or []) if m.enabled]
		if len(existing_enabled) > 1:
			frappe.throw(
				"A multi-model LLM pool is configured. Remove the extra models from your LLM settings "
				"before switching to single-model OAuth.",
				title="LLM Configuration",
			)
		s.set("models", [])
		# Also clear preset so that a stale preset doesn't leave a ghost pool
		# flag after switching to oauth (preset + 0 models → empty pool push).
		s.preset = ""
		s.llm_provider = provider
		s.llm_model = model
		s.llm_auth_mode = auth_mode
		s.llm_base_url = (base_url or "").strip()
	if force:
		# Read by on_update -> _classify_llm_change. Cleared after the
		# enqueue dispatches so a subsequent save() in the same request
		# (e.g. db_set for last_sync_status) doesn't double-fire.
		s.flags.force_admin_sync = True
	s.save(ignore_permissions=True)
	frappe.db.commit()
	# on_update writes last_sync_* via frappe.db.set_value so the
	# in-memory ``s`` doc is stale. Fetch JUST the two fields we
	# need rather than reloading the entire Singles doc (the previous
	# shape was ``frappe.get_single(...)`` then ``.get(...)`` on
	# every field - pointless re-fetch from the 2026-06-16 review).
	row = (
		frappe.db.get_value(
			"Jarvis Settings",
			"Jarvis Settings",
			["last_sync_at", "last_sync_status"],
			as_dict=True,
		)
		or {}
	)
	return {
		"last_sync_at": str(row.get("last_sync_at") or ""),
		"last_sync_status": row.get("last_sync_status") or "",
	}


@frappe.whitelist()
def get_llm_config() -> dict:
	"""Current effective LLM pool for the desk step + /ai SPA: models[] rows,
	preset, routing_mode, derived proxy_active. Reads models[] (NOT the legacy
	llm_* mirrors). Never returns api_key secrets — only a has_key boolean.
	System-Manager-only (spec 7)."""
	require_jarvis_admin()
	from jarvis.jarvis.pool_serialize import _model_accounts

	s = frappe.get_single("Jarvis Settings")
	models = []
	for m in s.get("models") or []:
		cred_type = m.credential_type or "api_key"
		entry = {
			"provider": m.provider or "",
			"model": m.model or "",
			"base_url": m.base_url or "",
			"tier": m.tier or "strong",
			"order": m.order or 0,
			"enabled": bool(m.enabled),
			"credential_type": cred_type,
		}
		if cred_type == "subscription":
			# Surface connected accounts so the UI can show them (has_key style).
			# NEVER send oauth_blob to the client — only the display metadata.
			accts = _model_accounts(m)
			entry["rotation"] = m.rotation or "sticky"
			entry["accounts"] = [
				{
					"upstream": (a.get("upstream") if hasattr(a, "get") else "") or "openai",
					"account_ref": (a.get("account_ref") if hasattr(a, "get") else "") or "",
					"label": (a.get("label") if hasattr(a, "get") else "") or "",
				}
				for a in accts
			]
			entry["has_key"] = bool(accts)
		else:
			entry["has_key"] = bool(m.get_password("api_key", raise_exception=False))
		models.append(entry)
	return {
		"models": models,
		"preset": s.get("preset") or "",
		"routing_mode": s.get("routing_mode") or "failover",
		"proxy_active": bool(s.get("proxy_active")),
	}


@frappe.whitelist()
def get_llm_sync_status() -> dict:
	"""Lightweight poller for the onboarding + account pages.

	``Jarvis Settings.on_update`` writes ``last_sync_status = 'pending: ...'``
	synchronously, then enqueues the heavy admin call. When the background
	job finishes, the status flips to ``ok (... via admin)`` or
	``failed: ...``. The UI polls this method every few seconds to observe
	that transition.

	Returns:
	    A dict with ``last_sync_at`` (ISO string or ""), ``last_sync_status``
	    (e.g. ``pending: provisioning container``, ``ok (restart via admin)``,
	    ``failed: admin unreachable: ...``), a convenience boolean
	    ``pending`` for client-side branching, ``subscription_status`` (one
	    of ``verified``/``unverified``/``unchecked``/``not_applicable``, or
	    ``""`` if the pool sync worker never wrote one - e.g. no pool sync
	    has run yet, or the fleet is on a pre-warnings contract), and
	    ``warnings`` - a list of ``{"code": str, "message": str}`` dicts
	    from the last pool apply (empty list when none), and
	    ``model_statuses`` - a list of ``{"provider", "model", "status"}``
	    per-model verdicts (``status`` one of ``verified``/``failed``/
	    ``unchecked``; api-key models only) the AI-models list keys each
	    api-key row's health off. Contract 1.12 adds an optional ``"detail"``
	    string per entry carrying the provider's raw error text (e.g. a z.ai
	    "insufficient balance" 1113 error) - absent on a fleet that predates
	    1.12, and passed through here verbatim with no whitelisting (this
	    function stores/returns whatever admin sent, so a new optional key
	    needs no server-side change to reach the client). A corrupt/empty
	    stored value for either list degrades to ``[]`` rather than ever
	    500ing this poller.
	"""
	s = frappe.get_single("Jarvis Settings")
	status = s.get("last_sync_status") or ""

	# Round-4 review R4-P0-6: an apply that admin returned as status="applying"
	# (busy lock / read-timeout / CAS refusal) left last_sync_status at the
	# pending-applying marker and did NOT stamp the durable synced marker. The
	# in-job convergence poll and the */5 reconcile_pending_llm_sync safety net
	# converge it eventually, but this poller runs every few seconds during
	# onboarding — so while (and ONLY while) we are pending-applying, consult
	# admin's read-only serving receipt (get_connection -> chat_readiness) and,
	# if it now reports Ready, flip the marker + status to "ok". Best-effort: any
	# admin error leaves us pending (the next poll retries). This is the "admit
	# chat from an admin serving receipt, not HTTP success" the review asks for,
	# done lazily from the poller the UI already runs.
	if status.startswith(_pending_applying_status()):
		status = _reconcile_pending_applying(s) or status

	def _json_list(raw):
		"""Stored JSON text -> list, degrading a corrupt/empty value to [] rather than
		ever 500ing this poller."""
		try:
			val = json.loads(raw or "[]")
			return val if isinstance(val, list) else []
		except (ValueError, TypeError):
			return []

	return {
		"last_sync_at": str(s.get("last_sync_at") or ""),
		"last_sync_status": status,
		"pending": status.startswith("pending:"),
		"subscription_status": s.get("last_subscription_status") or "",
		"warnings": _json_list(s.get("last_sync_warnings")),
		# Per-model verdicts from the last pool apply: [{provider, model, status}] where
		# status is verified | failed | unchecked (api-key models only; subscriptions use
		# subscription_status above). [] when no pool sync has run or the fleet predates
		# contract 1.11. The AI-models list keys each api-key row's health off this.
		"model_statuses": _json_list(s.get("last_model_statuses")),
	}


def _pending_applying_status() -> str:
	"""The jarvis_settings pending-applying marker, imported lazily so this
	module keeps zero import-time coupling to the (heavy) doctype module."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		_PENDING_APPLYING_STATUS,
	)

	return _PENDING_APPLYING_STATUS


def _reconcile_pending_applying(settings) -> str | None:
	"""Lazy customer-side reconcile for a pending-applying sync (round-4 R4-P0-6).

	Consult admin's read-only serving verdict for the active generation. When admin
	reports the container Ready, the apply has CONVERGED — stamp the durable synced
	marker (llm_pool_synced_at for a pool, llm_direct_synced_at for direct, via the
	shared _stamp_converged_ok) and flip the status to "ok" so is_ready_for_chat
	opens chat and the poller stops calling admin. Returns the new status string on
	a flip, else None (stay pending).

	Best-effort: any admin error / non-Ready verdict leaves the pending status
	untouched; the next poll retries. Never raises out of the poller."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		_admin_chat_readiness,
		_stamp_converged_ok,
	)

	state, _reason = _admin_chat_readiness()
	if state != "Ready":
		return None
	_stamp_converged_ok(settings, is_pool=bool(settings.get("proxy_active")))
	# _stamp_converged_ok's commit gate only fires in a worker/migrate context;
	# this runs in a web request, where a GET would otherwise roll the terminal
	# write back at request end.
	frappe.db.commit()
	return settings.get("last_sync_status")
