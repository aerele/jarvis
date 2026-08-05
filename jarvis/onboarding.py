"""Onboarding - store the admin token + container connection into Jarvis
Settings, and thin server wrappers the onboarding page calls (so the browser
never holds admin creds). admin_client returns already-unwrapped admin data."""

import json
from typing import NamedTuple

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint

from jarvis import admin_client, onboarding_contract, release_notice
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminRejectedError,
	AdminUnreachableError,
	AdminValidationError,
)
from jarvis.hooks import get_default_admin_url
from jarvis.permissions import grant_onboarding_admin, require_jarvis_access, require_jarvis_admin

# Every admin-side failure admin_client raises. One tuple so the onboarding
# facade's catch sites cannot drift apart from _surface's.
_ADMIN_ERRORS = (AdminValidationError, AdminAuthError, AdminUnreachableError, AdminRateLimitedError)


def _require_admin_url() -> None:
	"""Block onboarding only if no admin URL resolves at all.

	get_default_admin_url() returns ``jarvis_admin_url`` from site_config (via
	frappe.conf) or the bench-wide ``_DEFAULT_ADMIN_URL_FALLBACK``, so a
	deployment relying on that default is allowed - this raises only when even
	the default is empty. (admin_client._admin_url additionally honours the
	Jarvis Settings per-customer override at call time.)
	"""
	if not (get_default_admin_url() or "").strip():
		raise frappe.ValidationError(
			"No Jarvis Admin URL configured. Set Jarvis Settings -> Jarvis "
			"Admin URL, or 'jarvis_admin_url' in site_config.json, before "
			"continuing onboarding."
		)


def _throw_admin_error(e) -> None:
	"""Map one already-raised admin_client exception to the clean frappe.throw
	the onboarding page renders. Extracted from _surface so a caller can inspect
	the raised error first, then fall back to the identical mapping for every
	other admin-side failure. Always raises.

	The machine-readable half of admin's rejection is parked on the response
	first, so a THROWN failure still delivers the contract: the page keeps
	rendering the same sentence it always did, and a caller that wants to branch
	gets ``error.code`` next to Frappe's ``exc_type`` instead of a sentence to
	parse. Stamped here rather than at one call site because every onboarding
	surface throws through this function."""
	error, _status = onboarding_contract.error_object(e)
	onboarding_contract.stamp_error(error)
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
	except _ADMIN_ERRORS as e:
		_throw_admin_error(e)


def write_connection(data: dict) -> bool:
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
		return False
	from jarvis._password_utils import set_settings_password

	# Whether the CONNECTION block (agent_url + agent_token) was actually
	# persisted. False when the payload carried no agent_url, and — the case that
	# matters — when tenant_authority.guard HELD the write. Callers that go on to
	# act as if the connection had landed must gate on this; see the L4 poll, which
	# used to record a reset complete and then destroy the credentials on the
	# strength of a write that never happened.
	connection_written = False
	s = frappe.get_single("Jarvis Settings")
	# Capture the container this workspace pointed at BEFORE this write, so a
	# reconnect that repoints it can be told apart from a daily sync that rewrites
	# the same URL (which must not disturb an established claim).
	_old_agent_url = (s.get("agent_url") or "").strip()
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
	# Connection block, guarded by the monotonic tenant-authority generation
	# (review plan 04 P0-5). A slow poll carrying an OLDER authority generation
	# than the one already accepted must NOT overwrite the connection, or a
	# customer whose container just moved/repaired is silently regressed onto the
	# old one. guard() advances the stored (generation, handle) only on ACCEPT, so
	# the secrets and the authority receipt move together. Credential fields above
	# are unguarded: they carry no generation and are never part of the race.
	if data.get("agent_url"):
		from jarvis import tenant_authority

		write_conn = True
		try:
			outcome = tenant_authority.guard(s, data)
		except tenant_authority.AuthorityInvariantError:
			# Same generation, different serving container: never resolve by
			# guessing. HOLD the current connection and let the next poll retry
			# ("hold + re-poll"), never downgrade onto the other container. Recorded
			# once (deduped) so a divergence that does not clear stays visible.
			write_conn = False
			tenant_authority.log_invariant_once(
				data.get(tenant_authority.GEN_FIELD),
				s.get(tenant_authority.HANDLE_FIELD),
				data.get(tenant_authority.HANDLE_FIELD),
			)
		else:
			if outcome == tenant_authority.REJECT:
				write_conn = False
				tenant_authority.log_stale_once(
					s.get(tenant_authority.GEN_FIELD), data.get(tenant_authority.GEN_FIELD)
				)
		if write_conn:
			s.db_set("agent_url", data["agent_url"])
			if data.get("agent_token"):
				set_settings_password(s, "agent_token", data["agent_token"])
		connection_written = write_conn
	# Credentials just changed (fresh signup, or a reconnect rotating onto another
	# account): a bearer minted from the old ones would outlive them.
	principal_change = any(data.get(k) for k in ("api_key", "api_secret", "customer", "customer_password"))
	if principal_change:
		admin_client.clear_cached_token()
	# End the established chat-Ready claim when this write repoints the workspace at
	# a DIFFERENT admin principal or a DIFFERENT container (review P0-06): the
	# workspace admin confirmed Ready is no longer the one we are about to serve, so
	# the marker must not carry a fail-open verdict across the boundary. A daily
	# sync that rewrites the SAME agent_url with no new principal is left alone -
	# clearing there would eject every established customer once a day. The
	# authority anchor is the mechanical backstop; this is the explicit intent.
	container_change = bool(data.get("agent_url")) and data["agent_url"].strip() != _old_agent_url
	if principal_change or container_change:
		s.db_set("chat_was_ready_at", None)
		s.db_set("chat_ready_authority", "")
	# This is the write that can point the workspace at a DIFFERENT container or a
	# different admin principal, so any cached readiness verdict describes a
	# connection that is no longer the current one.
	from jarvis.account import _bust_chat_gate

	_bust_chat_gate()
	# NB: the release notice is deliberately NOT mirrored here. Several callers
	# pass a partial payload (a password, a customer email), and an absent notice
	# key means "cleared" - which would drop a live notice. It is persisted only
	# where a full get_connection payload is in hand: sync_connection and the
	# chat gate.
	return connection_written


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
def list_payment_providers() -> dict:
	"""Gateways the wizard may offer, already narrowed to what THIS bench can
	render: ``{providers: [...], default: "..."}``.

	Two filters, and both matter. Admin drops gateways the operator disabled;
	this intersects with ``SUPPORTED_PROVIDERS`` so a gateway enabled on a newer
	control plane than this bench build is not offered here and then dead-ended
	at a checkout that cannot open.

	Fails OPEN to razorpay: the chooser is a convenience, and a control-plane
	blip must not leave a customer unable to pay at all. Razorpay is the gateway
	that supports every flow, so it is the safe floor.
	"""
	ui_v2 = _payment_ui_v2_enabled()
	try:
		data = admin_client.get_payment_providers() or {}
	except Exception:
		return {"providers": ["razorpay"], "default": "razorpay", "payment_ui_v2": ui_v2}

	providers = [p for p in (data.get("providers") or []) if p in admin_client.SUPPORTED_PROVIDERS]
	if not providers:
		return {"providers": ["razorpay"], "default": "razorpay", "payment_ui_v2": ui_v2}
	default = (data.get("default") or "").strip().lower()
	return {
		"providers": providers,
		"default": default if default in providers else providers[0],
		"payment_ui_v2": ui_v2,
	}


def _payment_ui_v2_enabled() -> bool:
	"""The plan-09 07-c payment-UI rollout flag. Site-level boolean, DEFAULT ON.

	The admin-hosted checkout is the ONLY payment path after cutover (no fallback,
	owner decision 4), so this flag does NOT toggle old-vs-new behaviour — the old
	tenant-origin SDK path no longer exists. It gates ROLLOUT MESSAGING only: when
	explicitly disabled (``jarvis_payment_ui_v2 = 0`` in site config) the pay step
	shows a maintenance-style honest hold instead of taking the customer to
	checkout, so an operator can pause new checkouts on a bench without shipping
	code. Unset / None = ON."""
	value = frappe.conf.get("jarvis_payment_ui_v2")
	if value is None:
		return True
	return bool(value)


@frappe.whitelist()
def get_preset_catalog() -> list:
	"""Preset catalog for the desk onboarding step + the /ai SPA route.
	Thin wrapper over admin_client (fetch/cache/bundled fallback)."""
	return admin_client.get_preset_catalog()


def _subscription_upstream(sub: dict) -> str:
	"""The one upstream a posted subscription block's accounts agree on, or "".

	Two rows are only folded together when this matches, so folding can never
	manufacture the mixed-upstream row ``validate_models`` rejects.
	"""
	seen = {(a.get("upstream") or "").strip() for a in (sub.get("accounts") or []) if isinstance(a, dict)}
	seen.discard("")
	return seen.pop() if len(seen) == 1 else ""


def _merge_subscription_accounts(target: list, incoming: list) -> None:
	"""Append ``incoming`` accounts onto ``target``, skipping account_refs already
	there.

	A ref that arrives twice contributes only its ``oauth_blob``, and only when the
	copy already held carries none: a client that posts the same account in two
	rows (one of them reloaded, so blank) must end up with the credential, not with
	the ``duplicate_account_ref`` rejection ``validate_models`` would otherwise
	raise.
	"""
	by_ref = {}
	for a in target:
		ref = a.get("account_ref") if isinstance(a, dict) else None
		if ref:
			by_ref[ref] = a
	for a in incoming:
		ref = a.get("account_ref") if isinstance(a, dict) else None
		held = by_ref.get(ref) if ref else None
		if held is None:
			copy = dict(a) if isinstance(a, dict) else a
			target.append(copy)
			if ref:
				by_ref[ref] = copy
		elif not (held.get("oauth_blob") or "").strip():
			held["oauth_blob"] = a.get("oauth_blob") or ""


def _coalesce_subscription_models(models: list) -> list:
	"""Fold posted subscription rows that name the SAME model into ONE row holding
	all of their accounts.

	Every subscription model in a pool renders through ONE shared Bifrost provider
	entry ("cliproxy-subs"), so two rows naming the same model render duplicate
	routing targets and ``llm_proxy.validate()`` rejects the WHOLE spec with
	``duplicate_subscription_model``. The pool editor's "+ Add a model" flow seeds
	its new row on the chosen provider's default model id, so a customer adding a
	SECOND account of a provider they already use posted exactly that pair - and
	only learned it was refused after completing a full OAuth sign-in (#575).

	Pooling several accounts of one provider is the whole point of the subscription
	tier, so the honest reading of that payload is "another account for this model",
	never "a second model". Fold rather than reject: a rejection would ALSO lock any
	tenant that already stored such a pair out of every Jarvis Settings save, since
	``on_update`` re-validates on every write, and it would still cost the customer
	the sign-in they just completed.

	API-key rows pass through untouched - ``llm_proxy.validate`` scopes their
	duplicate check to (provider, model) pairs and they carry no accounts to merge.
	"""
	out: list = []
	folded: dict[tuple[str, str], dict] = {}
	for m in models:
		sub = m.get("subscription") if isinstance(m, dict) else None
		if not isinstance(sub, dict):
			out.append(m)
			continue
		key = ((m.get("model") or "").strip(), _subscription_upstream(sub))
		target = folded.get(key)
		if target is None:
			row = dict(m)
			row["subscription"] = dict(sub)
			row["subscription"]["accounts"] = [
				dict(a) if isinstance(a, dict) else a for a in (sub.get("accounts") or [])
			]
			folded[key] = row
			out.append(row)
			continue
		_merge_subscription_accounts(target["subscription"]["accounts"], sub.get("accounts") or [])
	return out


@frappe.whitelist()
def save_llm_pool(
	models: str | list,
	preset: str | None = None,
	routing_mode: str = "failover",
	idempotency_key: str | None = None,
) -> dict:
	"""Write the customer's multi-model LLM pool into Jarvis Settings.models[]
	(+ preset, routing_mode) and let the existing on_update pipeline validate
	(validate_models), derive pool_mode/proxy_active, mirror models[0] into legacy llm_*,
	and sync DIRECT (/llm-creds) vs PROXY (/llm-pool) via admin.

	Freshly-connected chat-subscription accounts arrive as an opaque
	``capture_id`` (never the raw OAuth blob - the minted token stayed
	server-side; review P0-04); this ADOPTS each capture exactly once into the
	saved config.

	For a POOL config the /llm-pool push is SYNCHRONOUS (plan-05 D2, Fable ruling
	/ review P0-02): the durable apply-operation descriptor admin creates in the
	same transaction as its desired-state write comes back under
	``apply_operation`` so the SPA follows ONE operation across
	save -> apply -> readiness. ``idempotency_key`` (opaque, per Start-chatting
	attempt) makes a double-click / lost-response resume converge on that same
	operation with no new desired version. A single-model config keeps the
	async creds path and returns ``apply_operation: null`` with ``mode: "legacy"``.

	All params MUST stay annotated: with Frappe's
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
	# Several accounts of one provider belong on ONE model row (see #575). Fold
	# before anything is read or written so the merge covers every client of this
	# endpoint, not just the SPA that happens to have the fix.
	models = _coalesce_subscription_models(models)
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
	from jarvis.oauth import pending_capture

	# capture_ids adopted this save, so consumed_by_operation can be recorded once
	# admin hands the durable descriptor back (audit only).
	consumed_capture_ids: list[str] = []

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
				# A freshly-connected SPA account carries an opaque capture_id (the token
				# stayed server-side; review P0-04). ADOPT the durable capture exactly
				# once: consume_capture atomically claims it and returns the decrypted
				# blob, which lands here and is persisted ENCRYPTED into
				# subscription_accounts by the save below - all in this one request
				# transaction, so the blob either moved into the saved config AND the
				# capture was burned, or neither did.
				cap_id = (a.pop("capture_id", "") or "").strip()
				ref = a.get("account_ref") or ""
				if cap_id:
					try:
						a["oauth_blob"] = pending_capture.consume_capture(cap_id)
						consumed_capture_ids.append(cap_id)
					except pending_capture.CaptureAlreadyConsumed:
						# Resume (SPA re-called save with the same idempotency_key and
						# the same payload): the first attempt already moved this blob
						# into the stored config, so fall back to it rather than
						# hard-failing - the credential was adopted, not lost.
						if ref and prior_blobs.get(ref):
							a["oauth_blob"] = prior_blobs[ref]
				elif not (a.get("oauth_blob") or "").strip():
					# No fresh capture and no re-entered blob: keep the stored one for a
					# re-saved account.
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
	# compute_pool_mode/compute_proxy_active, mirror models[0]. For a POOL config we
	# SUPPRESS the async enqueue and push synchronously below so we can return the
	# durable apply-operation descriptor (plan-05 D2, Fable ruling / review P0-02);
	# a single-model config keeps the existing async creds enqueue.
	idempotency_key = (idempotency_key or "").strip()
	s.flags.suppress_pool_enqueue = True
	s.save(ignore_permissions=True)
	frappe.db.commit()
	# The pool this workspace runs on just changed, so the readiness verdict admin
	# gave about the PREVIOUS one is finished. account._admin_chat_gate keys its
	# cache by config revision and this save moves it, so the old entry is already
	# unreachable; the explicit drop covers the revision returning to a previous
	# value inside the TTL (see _bust_chat_gate).
	from jarvis.account import _bust_chat_gate

	_bust_chat_gate()

	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import sync_pool_now
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	apply_operation = None
	resumable = False
	retry_after_seconds = 0
	if compute_pool_mode(s):
		# The durable apply operation lives on the POOL path (admin creates it in
		# update_llm_pool). Push synchronously and hand its descriptor back so the
		# SPA follows ONE operation across save -> apply -> readiness.
		outcome = sync_pool_now(idempotency_key=idempotency_key or None)
		apply_operation = outcome.get("apply_operation")
		resumable = bool(outcome.get("resumable"))
		retry_after_seconds = int(outcome.get("retry_after_seconds") or 0)
		# An OLD admin (no plan-05 apply-operation) that succeeded WITHOUT a descriptor
		# is a capability degrade, not a failure: report mode:"legacy" so the SPA falls
		# back to the bounded fail-closed readiness poll instead of a support dead-end
		# on a genuinely successful apply (F1).
		mode = "legacy" if (apply_operation is None and outcome.get("legacy_capability")) else "operation"
		if apply_operation and consumed_capture_ids:
			# Audit only (best-effort): tie the adopted captures to the operation.
			pending_capture.mark_consumed_by_operation(
				consumed_capture_ids, apply_operation.get("operation_id")
			)
	else:
		# Single-model (creds) path: on_update enqueued the async creds sync, and
		# admin's creds endpoint mints no apply operation, so there is no descriptor
		# to follow - the SPA falls back to the readiness poll for this config.
		mode = "legacy"

	row = (
		frappe.db.get_value(
			"Jarvis Settings", "Jarvis Settings", ["last_sync_at", "last_sync_status"], as_dict=True
		)
		or {}
	)
	return {
		# Plan-05 D2 (review §8.4): the durable operation descriptor the SPA follows,
		# or null on the legacy single-model path / an unallocated failure.
		"apply_operation": apply_operation,
		"idempotency_key": idempotency_key,
		"resumable": resumable,
		"retry_after_seconds": retry_after_seconds,
		"mode": mode,
		# Legacy fields kept for the settings status strip and older callers.
		"last_sync_at": str(row.get("last_sync_at") or ""),
		"last_sync_status": row.get("last_sync_status") or "",
		"proxy_active": bool(frappe.db.get_single_value("Jarvis Settings", "proxy_active")),
	}


# Every Jarvis Settings field that names or dates the LLM connection, and the
# value that means "there is no connection". The api_key Password fields are NOT
# here - clearing a Password needs its __Auth row dropped too, so they go through
# _clear_llm_secrets below.
#
# llm_pool_synced_at / llm_direct_synced_at look like history rather than
# credentials, but they are cleared deliberately: is_ready_for_chat treats a
# stamped marker as "this tenant has applied at least once, so keep chat open
# through a transient pending". Leaving them set would let the NEXT connection
# open chat before its first apply is confirmed - exactly the split-brain the
# markers exist to prevent. A disconnect ends that history.
_DISCONNECTED_LLM_FIELDS = {
	"llm_provider": "",
	"llm_model": "",
	"llm_base_url": "",
	"llm_auth_mode": "api_key",
	"llm_oauth_account_email": "",
	"llm_oauth_connected_at": None,
	"preset": "",
	"proxy_active": 0,
	"proxy_recommended": 0,
	"llm_pool_synced_at": None,
	"llm_direct_synced_at": None,
	# Same marker jarvis.oauth.api.disconnect writes. humaniseSyncStatus does not
	# recognise it, which is correct here: the editor's status strip hides itself
	# rather than reporting on an apply that no longer has a subject.
	"last_sync_status": "disconnected",
	"last_subscription_status": "",
	"last_sync_warnings": "[]",
	"last_model_statuses": "[]",
}

_POOL_MODEL_DOCTYPE = "Jarvis LLM Pool Model"


@frappe.whitelist()
def disconnect_llm() -> dict:
	"""Tear the customer's whole LLM connection down: delete every credential this
	bench stores AND ask admin to delete them from the container.

	Deliberately NOT expressed as "save an empty pool". save_llm_pool rejects an
	empty list, and so does the fleet agent, because an empty pool reaching the
	apply path is almost always a bug rather than an intention. A separate method
	keeps that guard intact, cannot be reached by accident from an edit, and gives
	the destructive action its own auditable name in the logs.

	Admin goes FIRST and its failure aborts the whole thing. The other order would
	leave a bench that reads "disconnected" in front of a container still holding
	live keys and still answering turns - the customer would believe their
	credentials were deleted when they were not, which is the one outcome this
	feature cannot have.

	Idempotent: a tenant with nothing configured clears nothing and still succeeds
	(admin's endpoint is idempotent for the same reason).
	"""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	# Only call admin when there is an admin tenancy to call. An un-onboarded
	# bench has no credentials for it, so admin_client would raise
	# AdminAuthError("not onboarded") on what is otherwise a perfectly valid
	# local wipe.
	if _has_admin_credentials(settings):
		_surface(admin_client.post_disconnect_llm)

	_clear_llm_secrets(settings)
	for field, value in _DISCONNECTED_LLM_FIELDS.items():
		settings.db_set(field, value, update_modified=False)
	frappe.db.commit()
	# There is no connection left for a cached "Ready" to be about.
	from jarvis.account import _bust_chat_gate

	_bust_chat_gate()
	return {"disconnected": True, "last_sync_status": "disconnected"}


def _has_admin_credentials(settings) -> bool:
	"""True when this bench holds credentials for the control plane. Mirrors the
	un-onboarded short-circuit in reconcile_pending_llm_sync: either the api-key
	pair or the OAuth password is enough to authenticate a call."""
	api_key = (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip()
	customer_pw = (
		settings.get_password("jarvis_admin_customer_password", raise_exception=False) or ""
	).strip()
	return bool(api_key or customer_pw)


def _clear_llm_secrets(settings) -> None:
	"""Delete every stored LLM credential: the models[] child rows (whose api_key
	and subscription_accounts are Password fields) and the legacy flat llm_api_key.

	Password fields keep the real value in __Auth, keyed by (doctype, row name) -
	the doctype column only ever holds a mask. Frappe cleans __Auth in
	frappe.delete_doc, which does not apply here: child rows of a Single are
	removed by Document.update_child_table with a bare SQL DELETE that never looks
	at __Auth. So clearing models[] the ordinary way leaves the encrypted keys
	behind, readable by anything that knows a deleted row's name. Drop them by hand,
	before the rows themselves.

	The sweep is by DOCTYPE, not by row name: Jarvis LLM Pool Model is a child of
	the Jarvis Settings Single and of nothing else, so every one of its __Auth rows
	belongs to the connection being torn down - including any orphaned by an
	earlier ordinary pool save. "Removed from everywhere" has to mean those too.
	"""
	from jarvis._password_utils import clear_settings_password

	frappe.db.delete("__Auth", {"doctype": _POOL_MODEL_DOCTYPE})
	frappe.db.delete(_POOL_MODEL_DOCTYPE, {"parenttype": "Jarvis Settings", "parent": "Jarvis Settings"})
	settings.set("models", [])
	clear_settings_password(settings, "llm_api_key")


@frappe.whitelist()
def start_signup(
	email: str, company: str, plan: str, provider: str | None = None, billing: dict | None = None
) -> dict:
	"""Guest signup → store the api_token → return the Razorpay handles for Checkout.

	Gated on ``require_jarvis_admin`` (Sprint-1 Important from the 2026-06-16
	code review): the customer signing up is configuring Jarvis for their entire
	site. Note what that gate actually is — ``JARVIS_ADMIN_ROLES``, i.e. System
	Manager OR the ``Jarvis Admin`` role, plus Administrator — NOT System Manager
	alone, which this docstring claimed for a year. It matters here because
	``grant_onboarding_admin`` below hands out ``Jarvis Admin``, so every user
	who completes an onboarding can reach this endpoint afterwards. Without the
	gate, any staff user could initiate a paid signup using a different
	email/company under the site's admin contract.

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
	# Money the gateway is holding that an operator has not been able to place
	# stops the WHOLE endpoint, not just the resume half below. The context is
	# bench-level, so a raised flag means THIS bench's money is parked — and
	# letting a fresh signup through while it is would be strictly worse than
	# letting a retry through: a second account, on top of a payment nobody has
	# managed to credit to the first. A status check is the documented way out
	# and clears the flag on any answer.
	if onboarding_contract.awaiting_reconciliation():
		_refuse_while_money_is_parked()  # always raises
	# The identity the customer TYPED, before admin is asked anything. Two
	# reasons it is written first: a response lost in transit still leaves the
	# bench knowing whose signup this was (plan 03's "bench response lost after
	# credentials saved" race), and the resumed wizard has a real address to
	# render instead of prefilling the site admin's. Server truth from admin
	# overwrites it below.
	#
	# Identity ONLY. The plan and the provider are deliberately NOT written here:
	# they are what a later initiate charges on, and a REQUESTED plan is not a
	# confirmed one. Writing them pre-call meant a plan admin rejected (disabled,
	# zero-priced, a gateway the operator switched off) became this site's sticky
	# default, and the next Pay click charged on it. They land in absorb(), from
	# a response admin actually agreed to.
	onboarding_contract.update(
		email=(email or "").strip(),
		company=(company or "").strip(),
		contract_version=onboarding_contract.CONTRACT_VERSION,
	)
	try:
		# NOT through _surface: that converts the admin error into a bare
		# frappe.throw, and the duplicate check below needs the class and the
		# contract code that conversion discards. Anything non-resumable then
		# goes through the identical mapping _surface would have applied.
		data = admin_client.signup(email, company, plan, provider=provider, billing=billing)
	except _ADMIN_ERRORS as e:
		resumed = _try_resume_pending_signup(e, email, plan, provider, billing)
		if resumed is None:
			_throw_admin_error(e)  # always raises
		data = resumed
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
	# Server truth for the identity and the money, over the prefill written
	# above: admin's envelope carries the account's real email and company, the
	# plan label, and what is actually due today.
	onboarding_contract.absorb(data)
	# Credentials do not go back to the browser. This response is the one that
	# CARRIES them — api_key, api_secret and (on the flag-off path) the OAuth
	# login password — and write_connection above has already stored every one.
	# Both paths return through here, so the resumed retry is covered with the
	# fresh signup. The wizard reads ``pending_verification`` and the checkout
	# handles and nothing else (OnboardingView runStartPay → launchCheckout);
	# no key stripped here is read anywhere in frontend/src.
	#
	# augment_pay_page is behaviour-neutral unless admin returned a pay-page token
	# (plan-09 WS7): on a token answer it attaches the bench's OWN attested pay
	# origin so the wizard top-level-navigates to the admin-hosted checkout instead
	# of opening a gateway SDK on this origin.
	return onboarding_contract.augment_pay_page(onboarding_contract.strip_credentials(data))


def _try_resume_pending_signup(
	err, email: str, plan: str, provider: str | None, billing: dict | None = None
) -> dict | None:
	"""Failed-payment retry: when guest signup is rejected as a duplicate and
	this bench holds admin credentials, resume the pending signup through admin's
	authenticated ``resume_pending_signup`` — same plan or a newly chosen one —
	instead of dead-ending the wizard on the duplicate error.

	Two things decide it, and NEITHER is a message:

	**Is this a duplicate?** ``error.code == ACCOUNT_ALREADY_EXISTS``, or
	Frappe's ``exc_type == "DuplicateEntryError"`` from an admin older than the
	contract. The substring test this replaces ("already registered or pending")
	stopped matching when admin reworded the sentence on 2026-07-26, and the
	resume was unreachable for every declined-card customer from that day on.

	**Is it ours?** Possession of the credentials the first signup minted. That
	IS the ownership proof: they authenticate the resume, and admin resolves the
	customer from the authentication — no caller-supplied identifier chooses the
	record. The email comparison that used to stand here is DELETED, not
	repaired: it tested the typed address against ``jarvis_admin_customer_email``,
	which holds admin's synthetic OAuth login ``cust-<hash>@jarvis.invalid``
	(signup.py ``_synthetic_login``; the bench stores ``data["customer"]``
	verbatim in write_connection). A real address and that login are never equal,
	so the gate could only ever return None — the second, independent kill of
	this dead end, and the one that survived fixing the wording.

	Returns admin's checkout payload, or None when the fallback doesn't apply (no
	stored creds, or a non-duplicate error) so the caller surfaces the original
	error. A resume that itself fails (e.g. a genuinely different account's
	duplicate) also returns None: the original duplicate error is the honest
	message for that case."""
	if not onboarding_contract.is_duplicate_signup(err):
		return None
	settings = frappe.get_single("Jarvis Settings")
	if not _has_admin_credentials(settings):
		return None
	try:
		context = onboarding_contract.load()
		return admin_client.resume_pending_signup(
			plan,
			provider=provider,
			billing=billing,
			idempotency_key=_reserve_idempotency_key(context=context),
		)
	except Exception:
		# Deliberate fallthrough to the original duplicate error (a rejected
		# resume usually means a REAL duplicate), but leave a trace so a broken
		# resume path is debuggable rather than invisible.
		frappe.log_error(
			title="signup resume fallback failed (showing original duplicate error)",
			message=frappe.get_traceback(),
		)
		return None


def _reserve_idempotency_key(*, supplied: str | None = None, context: dict | None = None) -> str:
	"""Pick the key for the initiation about to be made and persist it BEFORE
	the call.

	The order is the whole point. A key written only after a successful response
	is no protection against the case the key exists for — the response that
	never arrives — because the retry would then mint a second key and admin
	would open a second gateway object beside the one the first call already
	created."""
	key = onboarding_contract.next_idempotency_key(supplied=supplied, context=context)
	onboarding_contract.update(idempotency_key=key)
	return key


def _absorb_signup_state(data: dict, *, from_check: bool = False) -> dict:
	"""Fold an authenticated state/check envelope into local state.

	Admin delivers the customer's OAuth password on whichever poll first runs
	after the email is confirmed (kept until TTL rather than deleted on read, so
	a dropped response is recoverable) — both the passive poll and the
	provider-truth check can carry it, and whichever one the wizard happens to
	call must persist it or the bench never gets bearer auth. It is persisted
	HERE, from the original envelope, and stripped from the copy that goes back
	to the browser.

	``from_check`` marks a provider-truth answer, which is additionally
	authoritative about the reconciliation flag — see
	``onboarding_contract.absorb_check``."""
	if not isinstance(data, dict):
		return onboarding_contract.load()
	if data.get("customer_password"):
		write_connection({"customer_password": data["customer_password"]})
	if from_check:
		return onboarding_contract.absorb_check(data)
	return onboarding_contract.absorb(data)


def _refuse_while_money_is_parked() -> None:
	"""Refuse an onboarding action while a payment is awaiting manual
	reconciliation. Always raises.

	Used by the surfaces that THROW (start_signup) rather than return an
	envelope; ``initiate_signup_payment`` expresses the same refusal as an
	``ok: false`` body. Both carry the same code, the same recovery hint and the
	same 409 — the status coming off the exception class, because a throw's
	status is a property of its type and nothing else."""
	error = {
		"code": onboarding_contract.BENCH_AWAITING_RECONCILIATION,
		"message": "we're still confirming a payment on this signup; no new payment is needed",
		"recovery": onboarding_contract.RECOVERY_CHECK_STATUS,
	}
	onboarding_contract.stamp_error(error)
	frappe.throw(error["message"], onboarding_contract.SignupConflictError)


def _no_signup_here() -> dict:
	"""The day-one answer: this site has never started a signup.

	A bench with no admin credentials cannot authenticate anything, so asking the
	control plane is a guaranteed 401 - which the facade would then dress up as
	"admin authentication failed; contact support" and show to somebody whose only
	mistake was opening the page before signing up. Answered locally, with no
	network call and copy that describes the actual situation."""
	return onboarding_contract.failure(
		{
			"code": onboarding_contract.BENCH_NO_SIGNUP_CONTEXT,
			"message": "no signup has been started on this site yet",
			"recovery": onboarding_contract.RECOVERY_RETRY,
		},
		409,
	)


@frappe.whitelist()
def get_onboarding_state() -> dict:
	"""Where this site's signup actually stands, from admin, plus what this
	bench knows locally.

	The PASSIVE half of the payment surface: admin reads its own subscription
	row and never asks a gateway whether money moved, so this is what a wizard
	polls. ``check_signup_payment_status`` below is the half that asks the
	provider.

	Returns ``{ok, contract_version, data, context}``. ``data`` is admin's
	envelope — its ``code``, its ``can_initiate_payment`` / ``can_check_status`` /
	``can_reconnect`` capability flags, its billing disclosure — passed through
	unfiltered except for credential-shaped keys, so an additive admin release
	needs no bench change to reach the page. Gate a Pay button on
	``can_initiate_payment`` and a reconnect offer on ``can_reconnect``; never
	re-derive either from a status string, and never from a message.

	A failure answers with the same envelope under a DELIBERATE 4xx/5xx and
	``ok: false`` — never a success-shaped body under a success status. A site
	that has not signed up yet is answered locally, without a doomed call.

	Gated on Jarvis Admin (``JARVIS_ADMIN_ROLES`` — System Manager OR the Jarvis
	Admin role, plus Administrator), like the rest of onboarding."""
	require_jarvis_admin()
	_require_admin_url()
	if not _has_admin_credentials(frappe.get_single("Jarvis Settings")):
		return _no_signup_here()
	try:
		data = admin_client.get_signup_payment_state()
	except _ADMIN_ERRORS as e:
		error, status = onboarding_contract.error_object(e)
		return onboarding_contract.failure(error, status)
	context = _absorb_signup_state(data)
	return onboarding_contract.success(data, context=context)


@frappe.whitelist()
def initiate_signup_payment(
	plan: str | None = None,
	provider: str | None = None,
	idempotency_key: str | None = None,
) -> dict:
	"""Open (or re-open) checkout for THIS site's own pending signup.

	The authenticated retry, and the reason the failed-payment dead end had a way
	out at all: the bench holds the credentials admin minted at signup, so it
	proves ownership by authenticating rather than by naming an email. No
	caller-supplied identifier chooses the record.

	``plan``/``provider`` default to the ones the signup was started with (from
	the local context); passing them switches plan or gateway on the retry,
	exactly as admin's resume allows.

	``idempotency_key`` is honoured verbatim when given, and REFUSED locally when
	it cannot work — an over-long key is answered here, with nothing persisted,
	because the stored key is what the next attempt reuses and storing one admin
	rejects is a brick the customer cannot clear. When none is given the bench
	supplies its own: the stored key is reused while the intent it bought is still
	payable — so a double-clicked Pay button, a retried POST and a refreshed page
	converge on ONE gateway object — and a fresh key is minted once the last known
	code says the recovery is a new intent. It is persisted before the call,
	because the case it exists for is the response that never arrives.

	Refused locally, before any network call, while the last provider-truth check
	says money is awaiting manual reconciliation: the gateway is holding a payment
	an operator has not been able to place, and a second intent would take a
	second one for it.

	Returns the same ``{ok, contract_version, data, context}`` envelope as
	``get_onboarding_state``. A coded conflict (already paid, terminal,
	verification still pending) comes back as ``ok: false`` with admin's code
	under its deliberate 4xx — ``PAYMENT_ALREADY_ACTIVE`` in particular means
	continue setup and MUST NOT be retried into a second charge.

	Gated on Jarvis Admin (``JARVIS_ADMIN_ROLES``), like the rest of onboarding."""
	require_jarvis_admin()
	_require_admin_url()
	if not _has_admin_credentials(frappe.get_single("Jarvis Settings")):
		# The one endpoint that could still reach admin unauthenticated: a bench
		# whose credentials were cleared (reset, reconnect onto another account)
		# but whose signup context survived would sail past the plan check below
		# on a remembered plan and earn the guaranteed 401 the day-one guard
		# exists to kill.
		return _no_signup_here()
	context = onboarding_contract.load()
	key_error = onboarding_contract.supplied_key_error(idempotency_key)
	if key_error:
		return onboarding_contract.failure(key_error, 400, context=context)
	if onboarding_contract.awaiting_reconciliation(context):
		# The customer has already paid something the gateway is holding, and the
		# code that came with it is the ordinary PENDING one - deliberately, so a
		# wizard does not invite a second payment - which means nothing in the code
		# alone stops this call. The flag does. It clears when a later check says
		# the operator placed the money.
		return onboarding_contract.failure(
			{
				"code": onboarding_contract.BENCH_AWAITING_RECONCILIATION,
				"message": "we're still confirming a payment on this signup; no new payment is needed",
				"recovery": onboarding_contract.RECOVERY_CHECK_STATUS,
			},
			409,
			context=context,
		)
	plan = (plan or context.get("plan") or "").strip()
	if not plan:
		# Nothing local to resume with and nothing named. A coded refusal rather
		# than a guess: initiating on the wrong plan is a wrong charge.
		return onboarding_contract.failure(
			{
				"code": onboarding_contract.BENCH_NO_SIGNUP_CONTEXT,
				"message": "no signup in progress on this site; start one before paying",
				"recovery": onboarding_contract.RECOVERY_RETRY,
			},
			409,
			context=context,
		)
	provider = (provider or context.get("payment_provider") or "").strip().lower()
	try:
		data = admin_client.resume_pending_signup(
			plan,
			provider=provider or None,
			idempotency_key=_reserve_idempotency_key(supplied=idempotency_key, context=context),
		)
	except _ADMIN_ERRORS as e:
		error, status = onboarding_contract.error_object(e)
		# Only a PAYMENT-STATE code updates what the page renders and what the next
		# key does. A rate-limit backoff and a transport failure describe this CALL,
		# not the money: absorbing "you are asking too often" as the payment's state
		# would mint a fresh intent on the next click.
		return onboarding_contract.failure(
			error, status, context=onboarding_contract.absorb_payment_outcome(e)
		)
	context = _absorb_signup_state(data)
	return onboarding_contract.success(data, context=context)


@frappe.whitelist()
def check_signup_payment_status() -> dict:
	"""Ask the PROVIDER what happened to this signup's payment, and converge.

	What a customer whose checkout redirect died and whose webhook was lost can
	click. Everything else in the flow learns that money moved from something the
	browser brings back or something the gateway pushes; this endpoint goes and
	asks, and a verified payment is activated through the same seam a callback or
	a webhook would have used — so the callback that turns up late is a no-op
	rather than a second activation.

	Never creates or replaces an intent: a decline or a dead handle is a REPORT,
	and opening the replacement is ``initiate_signup_payment``'s job on an
	explicit customer action.

	Two fields only this surface adds, and both change what may be rendered:
	``gateway_consulted`` (false when the call failed AND when the answer came
	from the short cache — so "checked just now" can never be shown as "and the
	gateway confirmed it") and ``awaiting_manual_reconciliation`` (the gateway
	holds money we could not credit to this exact attempt and an operator is
	placing it — the code stays the ordinary pending one, because a decline here
	would invite a SECOND payment, so it is THIS FLAG that suppresses a Pay
	affordance).

	Rate-limited per customer: ``PAYMENT_CHECK_RATE_LIMITED`` means wait
	``retry_after_seconds`` and ask again. It asserts nothing about the payment
	and must never be rendered as a decline.

	This is also the ONLY surface that may lower the reconciliation flag, because
	admin sends it only when it is true — so an ordinary poll can raise it and
	nothing could ever clear it. Running a check is how a customer refused a retry
	gets out of that state once the operator has placed the money.

	Gated on Jarvis Admin (``JARVIS_ADMIN_ROLES``), like the rest of onboarding."""
	require_jarvis_admin()
	_require_admin_url()
	if not _has_admin_credentials(frappe.get_single("Jarvis Settings")):
		return _no_signup_here()
	try:
		data = admin_client.check_signup_payment_status()
	except _ADMIN_ERRORS as e:
		error, status = onboarding_contract.error_object(e)
		return onboarding_contract.failure(error, status)
	context = _absorb_signup_state(data, from_check=True)
	return onboarding_contract.success(data, context=context)


@frappe.whitelist()
def update_billing(billing: dict) -> dict:
	"""Authenticated billing-only edit facade (Plan 01, post-intent Review & Pay
	"Edit"): forwards to admin's ``update_pending_billing`` on the owned pending
	Customer. Does NOT create or replace a payment intent and never calls guest
	signup. Gated on Jarvis Admin like the rest of onboarding; admin re-checks
	ownership + Pending Payment status. Returns admin's data
	(``billing_saved`` + normalized ``billing`` summary) un-flattened."""
	require_jarvis_admin()
	_require_admin_url()
	return _surface(admin_client.update_pending_billing, billing)


@frappe.whitelist()
def reconnect_available(email: str, company: str = "") -> dict:
	"""Would a reconnect for this (email, company) find an account to reconnect?

	Gates the wizard's reconnect offer so it appears only when it would work. Fails
	CLOSED: any admin-side error answers "not available" rather than raising, because
	this only decides whether to show a hint - a wizard that breaks because the
	control plane blipped would be a worse trade than a hint that stays hidden.
	Same System-Manager gating as the rest of onboarding."""
	require_jarvis_admin()
	try:
		_require_admin_url()
		d = admin_client.reconnect_eligibility(email, company) or {}
	except Exception:
		return {"eligible": False, "needs_company": False}
	return {
		"eligible": bool(d.get("eligible")),
		"needs_company": bool(d.get("needs_company")),
	}


@frappe.whitelist()
def start_account_reconnect(email: str, company: str = "") -> dict:
	"""Fresh-bench recovery: ask admin to email a reconnect CODE to the
	REGISTERED address of an existing paid account (wiped-site scenario — the
	duplicate-email guard blocks re-signup, and nothing should be re-paid).
	``company`` disambiguates when the email owns several company accounts.
	Returns {request, message}; the customer then types the code, which
	``check_account_reconnect`` redeems. Same System-Manager gating as the rest
	of onboarding."""
	require_jarvis_admin()
	_require_admin_url()
	return _surface(admin_client.request_account_reconnect, email, company)


@frappe.whitelist()
def check_account_reconnect(request_id: str, code: str = "") -> dict:
	"""Redeem the reconnect code the customer received by email (or from
	support). Only a correct code releases anything: admin then rotates and
	delivers the credentials — persist them and
	grant the onboarding admin role, exactly like a fresh signup would. The
	wizard then rides the normal sync_connection path to the customer's
	EXISTING container; only the LLM step needs re-doing on this fresh site."""
	require_jarvis_admin()
	data = _surface(admin_client.get_reconnect_state, request_id, code) or {}
	if data.get("status") != "ready":
		return {"status": data.get("status") or "expired"}
	# A reconnect deliberately re-points this bench at an existing account's
	# container, whose authority generation is unrelated to whatever this site
	# last held. Forget the accepted (generation, handle) BEFORE riding
	# sync_connection, or a stored generation higher than the reconnected
	# account's would reject its connection as "older" and strand the site
	# (review plan 04 P0-5).
	from jarvis import tenant_authority

	tenant_authority.clear(frappe.get_single("Jarvis Settings"))
	write_connection(
		{
			"api_key": data.get("api_key", ""),
			"api_secret": data.get("api_secret", ""),
			"customer": data.get("customer", ""),
			"customer_password": data.get("customer_password", ""),
		}
	)
	grant_onboarding_admin()
	return {"status": "connected"}


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


def _company_defaults_error(code: str, message: str, http_status: int) -> dict:
	"""Coded error envelope for get_company_onboarding_defaults, mirroring admin
	signup.py's {"ok": False, "error": {"code": ...}} shape so the SPA keys on a
	stable code, never a prose message. ``message`` never carries billing PII."""
	frappe.local.response.http_status_code = http_status
	return {"ok": False, "error": {"code": code, "message": message}}


def _resolve_company_contact(company: str) -> dict | None:
	"""Primary Contact for the Company + a phone number.

	``frappe.contacts...get_default_contact`` is raw SQL that checks NO permission
	(C01-3), so we re-check Contact read permission ourselves and leak nothing if
	it is not readable. Phone falls back mobile_no -> phone -> child ``phone_nos``
	rows (C01-4: the denormalized fields are blank unless a child row carries the
	primary flag)."""
	from frappe.contacts.doctype.contact.contact import get_default_contact

	try:
		contact_name = get_default_contact("Company", company)
	except Exception:
		contact_name = None
	if not contact_name or not frappe.has_permission("Contact", "read", doc=contact_name):
		return None
	c = frappe.db.get_value(
		"Contact",
		contact_name,
		["name", "first_name", "last_name", "company_name", "mobile_no", "phone"],
		as_dict=True,
	)
	if not c:
		return None
	phone = (c.mobile_no or "").strip() or (c.phone or "").strip() or _contact_child_phone(contact_name)
	display = " ".join(p for p in (c.first_name, c.last_name) if p).strip() or (c.company_name or "")
	out = {"name": c.name}
	if display:
		out["display_name"] = display
	if phone:
		out["phone"] = phone
	return out


def _contact_child_phone(contact_name: str) -> str:
	"""Deterministic phone from a Contact's child ``phone_nos`` rows (C01-4):
	primary mobile, then primary phone, then the first non-empty row by idx."""
	rows = frappe.get_all(
		"Contact Phone",
		filters={"parent": contact_name, "parenttype": "Contact"},
		fields=["phone", "is_primary_phone", "is_primary_mobile_no"],
		order_by="idx asc",
	)
	for want in ("is_primary_mobile_no", "is_primary_phone"):
		for r in rows:
			if r.get(want) and (r.phone or "").strip():
				return r.phone.strip()
	for r in rows:
		if (r.phone or "").strip():
			return r.phone.strip()
	return ""


def _resolve_company_billing_address(company: str) -> dict | None:
	"""Primary billing Address for the Company, as an allowlisted presentation dict.

	ERPNext is OPTIONAL (C01-1): this deliberately does NOT call
	``erpnext...get_default_company_address`` \u2014 that helper both requires ERPNext
	(jarvis is a frappe-only app) AND does not select a primary address (its
	``max()`` over unset ``is_primary_address`` flags returns an arbitrary
	SQL-order row \u2014 C01-2). Instead we run the frappe-only Dynamic Link query that
	works on every bench and filter EXPLICITLY on ``is_primary_address = 1``,
	excluding disabled rows. Nothing flagged primary -> return NOTHING, never a
	warehouse/shipping address dressed up as billing. Own read-permission check
	(C01-3). GSTIN is read only when the field exists in Address metadata (India
	Compliance not installed on frappe-only benches)."""
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
		pluck="parent",
	)
	if not linked:
		return None
	primary = frappe.get_all(
		"Address",
		filters={"name": ["in", linked], "is_primary_address": 1, "disabled": 0},
		pluck="name",
		order_by="modified desc",
		limit=1,
	)
	if not primary or not frappe.has_permission("Address", "read", doc=primary[0]):
		return None
	has_gstin = frappe.get_meta("Address").has_field("gstin")
	fields = ["name", "address_line1", "address_line2", "city", "state", "pincode", "country"]
	if has_gstin:
		fields.append("gstin")
	a = frappe.db.get_value("Address", primary[0], fields, as_dict=True)
	if not a:
		return None
	out = {"name": a.name}
	for k in ("address_line1", "address_line2", "city", "state", "pincode", "country"):
		if a.get(k):
			out[k] = a.get(k)
	if has_gstin and a.get("gstin"):
		out["gstin"] = a.get("gstin")
	return out


@frappe.whitelist()
def get_company_onboarding_defaults(company: str) -> dict:
	"""ERP-derived billing defaults for ONE selected Company (Plan 01): its primary
	Contact's phone and its primary billing Address (+ optional India Compliance
	GSTIN). Behind the same onboarding-admin gate as the rest of onboarding.

	Returns an allowlisted PRESENTATION dict \u2014 never whole Contact/Address
	documents, never unrelated fields:

	    {"ok": True, "data": {"company": "...",
	        "contact": {"name","display_name","phone"},
	        "billing_address": {"name","address_line1","address_line2","city",
	                            "state","pincode","country","gstin"}}}

	``contact`` / ``billing_address`` are omitted when nothing resolves (so a site
	without linked Contact/Address still onboards). Failures carry a stable code:
	COMPANY_DEFAULTS_NOT_FOUND (blank/unknown company) or COMPANY_DEFAULTS_FORBIDDEN
	(company not readable). Neither the Frappe contact helper nor the Dynamic Link
	query checks permission, so every read gate here is ours (C01-3)."""
	require_jarvis_admin()
	company = (company or "").strip()
	if not company:
		return _company_defaults_error("COMPANY_DEFAULTS_NOT_FOUND", "company is required", 400)
	# Company is an ERPNext doctype and jarvis runs on frappe-only benches too
	# (C01-1). Guard the doctype's existence first so frappe.db.exists("Company", …)
	# can't 500 on a missing table — a frappe-only site simply has no company to
	# resolve.
	if not frappe.db.exists("DocType", "Company") or not frappe.db.exists("Company", company):
		return _company_defaults_error("COMPANY_DEFAULTS_NOT_FOUND", "unknown company", 404)
	if not frappe.has_permission("Company", "read", doc=company):
		return _company_defaults_error(
			"COMPANY_DEFAULTS_FORBIDDEN", "not permitted to read this company", 403
		)

	data: dict = {"company": company}
	contact = _resolve_company_contact(company)
	if contact:
		data["contact"] = contact
	address = _resolve_company_billing_address(company)
	if address:
		data["billing_address"] = address
	return {"ok": True, "data": data}


@frappe.whitelist()
def check_signup_payment_state() -> dict:
	"""Wizard-poll endpoint for the email-verification window.

	Calls admin's ``get_signup_payment_state`` (authenticated via the
	api_key + api_secret persisted at start_signup time) and returns the
	response unchanged. The wizard JS branches on
	``pending_verification`` to decide whether to keep showing the
	"check your email" screen or to open Razorpay Checkout.

	Gated on ``require_jarvis_admin`` (``JARVIS_ADMIN_ROLES``: System Manager or
	the Jarvis Admin role, plus Administrator) for the same reason as
	start_signup: this is part of the same paid-signup flow on the customer's
	bench.
	"""
	require_jarvis_admin()
	_require_admin_url()
	data = _surface(admin_client.get_signup_payment_state)
	# On the verified poll (email confirmed) admin delivers the customer's
	# OAuth password once. Persist it so subsequent admin calls use bearer
	# auth. Absent on the not-yet-verified poll and on the flag-off path.
	# Shared with get_onboarding_state so the two surfaces cannot disagree about
	# what a poll persists; the flat return shape this endpoint's caller expects
	# is unchanged.
	_absorb_signup_state(data)
	# ...and the password does NOT go on to the browser. It was persisted above
	# and the page has no use for it (verifyPollAction never reads it); returning
	# admin's dict verbatim put a plaintext login secret in an HTTP response body
	# on every verified poll.
	return onboarding_contract.strip_credentials(data)


@frappe.whitelist()
def finish_payment(payload: dict | str) -> dict:
	"""Confirm Checkout success → store the returned container connection.

	Gated on ``require_jarvis_admin`` (``JARVIS_ADMIN_ROLES``: System Manager or
	the Jarvis Admin role, plus Administrator): writes container connection
	(agent_url, agent_token) into Jarvis Settings.

	A FAILED confirm is where the wizard learns a payment was refused, and that
	verdict has to reach the local context or the next Pay click reuses the
	idempotency key that bought the refused intent — and admin hands back the
	dead order. Only a payment-state code is absorbed; a transport failure here
	says nothing about the money.
	"""
	require_jarvis_admin()
	if isinstance(payload, str):
		payload = json.loads(payload)
	try:
		data = admin_client.confirm_payment(payload)
	except _ADMIN_ERRORS as e:
		onboarding_contract.absorb_payment_outcome(e)
		_throw_admin_error(e)  # always raises
	write_connection(data)
	# PART 4 REVISED, TASK 48: the AUTHORITATIVE "onboarding AND paying" grant —
	# make the paying user a Jarvis Admin once payment confirms and the connection
	# is written. Idempotent with the start_signup grant.
	grant_onboarding_admin()
	onboarding_contract.absorb(data)
	# ``agent_token`` does not go back to the browser either, and the reason this
	# is not merely tidiness: admin's confirm_payment has a REPLAY branch that
	# re-serves the connection payload for a payment id it has already recorded
	# (api/tenant.py, the Cashfree arm keyed on a caller-supplied order id with no
	# signature). That makes this endpoint a repeatable token read rather than a
	# one-shot checkout completion — and its gate is require_jarvis_admin, which
	# grant_onboarding_admin hands to every user who finishes an onboarding.
	# The bench still consumes the token internally: write_connection above stored
	# it. The SPA parks this dict in ``state.successData`` and reads exactly two
	# fields from it, ``agent_url`` and ``tenant_status`` (proceedAfterPay), both
	# of which survive.
	return onboarding_contract.strip_credentials(data)


@frappe.whitelist()
def renew(provider: str | None = None) -> dict:
	"""Existing customer initiates a renewal payment; returns a pay-page token
	the billing page top-level-navigates to (plan-09 WS8). The admin-hosted
	checkout completes the payment; the webhook/return activates the plan.

	Gated on System Manager: initiates a billing transaction tied to the
	site's admin account.
	"""
	require_jarvis_admin()
	# plan-09 WS8: attest the token against the bench's OWN pay origin so
	# BillingPage can navigate (behaviour-neutral on a non-token answer).
	return onboarding_contract.augment_pay_page(_surface(admin_client.renew, provider=provider))


_RESETTING_STATUS = "pending: resetting workspace"
# Variant marker when the customer also revoked their LLM connections: the poll
# then completes on "container reachable" (readiness can never reach Ready with
# no LLM configured) and the wizard gate owns the rest.
_RESETTING_RECONNECT_LLM_STATUS = _RESETTING_STATUS + " (reconnect llm)"
# L4 ("reset and start fresh"): the customer asked for the admin connection to go
# too. It cannot go now — the poll below still has to authenticate to admin to
# watch the rebuild — so the intent rides the marker and the clear happens once
# the poll has seen the new container Ready. Suffix, not a third variant, so a
# reset that is BOTH llm-revoking and disconnecting still satisfies the
# ``startswith`` in _reconnect_llm.
_DISCONNECT_SUFFIX = " (disconnect)"
# Durable "no admin connection" marker: _clear_admin_connection writes this
# AFTER settings_reset.CONNECTION blanks last_sync_status (CONNECTION.blank
# includes it), so a reload can tell a bench that lost its admin credentials
# apart from one that simply never finished onboarding (last_sync_status stays
# "" for that case). Deliberately does NOT start with _RESETTING_STATUS, so
# _reset_in_flight() and reconcile_pending_workspace_reset() both correctly
# treat a disconnected bench as having no reset to converge.
#
# WRITE-ONLY BY DESIGN, and round-4 MINOR 2 is right that this is unusual enough
# to state outright rather than leave as an oddity.
#
# Nothing reads it back to ANSWER "is this bench disconnected" — ``_admin_connection_absent``
# does that, from the two credential fields — because the literal "disconnected"
# already means something ELSE on this same field: ``jarvis.oauth.api.disconnect``
# and ``onboarding.disconnect_llm`` both write it for a chat-subscription / LLM
# disconnect that leaves admin credentials fully intact (account.py's
# ``_has_llm_config`` docstring documents the same collision). Testing equality
# against it would tell a customer who merely unplugged their AI model that they
# need the emailed-code reconnect.
#
# It is kept because ``settings_reset.CONNECTION`` blanks ``last_sync_status``, and
# an empty field is indistinguishable from "never onboarded" for an operator
# reading the row — which is precisely the question they ask when a customer says
# chat stopped. Diagnostic value, not control flow. Do not build a predicate on it.
_DISCONNECTED_STATUS = "disconnected"


# A reset has TWO in-flight states, and conflating them is what let a transient
# failure destroy a customer's credentials.
#
# PRE-FLIGHT (this suffix): claimed, but the rebuild has NOT been requested yet —
# or was attempted and we do not know whether it started. The old container is
# still up and still Ready.
# CONVERGING (no suffix): admin accepted the request, so a rebuild really is
# happening and the poll's job is to watch for it.
#
# Why they must differ. The claim is written BEFORE the long calls (Amendment 2
# BLOCKER 2), so between claim and rebuild the marker says "resetting" while the
# OLD container still answers Ready. ``_workspace_reset_poll`` converges on
# ``ready and _resetting()``. With ONE state, a ``*/5``
# ``reconcile_pending_workspace_reset`` landing in that window declares the reset
# complete against a container that was never replaced — and for an L4 tears it
# down and clears the credentials.
#
# That window is not just the happy-path gap. When ``reset_workspace`` fails with
# a timeout or a 5xx the claim is deliberately KEPT (see _release_reset_claim: a
# bench timeout is the ordinary shape of a rebuild that IS running), the raise
# unwinds ``with redis_lock(...)`` and its ``finally`` RELEASES the lock. So a
# single transient network fault used to leave a convergeable marker, a free
# lock, and an un-rebuilt container — and reconcile would then converge on it five
# minutes later. No lock scope or TTL closes that; the lock is not even held when
# it happens. Only positive evidence that admin accepted the request does, which
# is what promotion is.
_PREFLIGHT_SUFFIX = " (requested)"

# The disconnect's own claim. Same role the pre-flight reset marker plays, for the
# same reason: ``disconnect_bench``'s critical section spans a teardown that can
# run to 240s, so holding the lock across it would freeze every convergence for
# the whole TTL when the worker is SIGKILLed at ``http_timeout``. The lock now
# covers only check-and-claim, and THIS carries in-flight-ness afterwards.
#
# Deliberately does NOT start with _RESETTING_STATUS: a disconnect is not a reset,
# nothing polls it, and ``reconcile_pending_workspace_reset`` must not try to
# converge one. It is matched by ``_workspace_op_in_flight`` instead.
_DISCONNECTING_STATUS = "pending: disconnecting bench"


def _reset_marker(
	reconnect_llm: bool = False, disconnect_after: bool = False, preflight: bool = False
) -> str:
	"""The ``last_sync_status`` marker for a reset at this depth.

	One definition, because the marker is not just display state — it is where a
	reset's DEPTH lives between the request and the poll that completes it. Two
	call sites deriving it separately is how an L4 gets silently downgraded.

	``preflight`` appends ``_PREFLIGHT_SUFFIX``: claimed, rebuild not yet known to
	have started. Guards treat it as in flight; the poll refuses to converge on it.
	"""
	marker = _RESETTING_RECONNECT_LLM_STATUS if reconnect_llm else _RESETTING_STATUS
	if disconnect_after:
		marker += _DISCONNECT_SUFFIX
	return marker + _PREFLIGHT_SUFFIX if preflight else marker


def _strip_preflight(status: str) -> str:
	"""The converging marker a pre-flight claim will be promoted to.

	Depth comparisons must go through this. A same-depth resubmission during the
	pre-flight window is idempotent, not "a reset at a different depth" — comparing
	the raw strings would refuse it with a message that is simply untrue."""
	status = status or ""
	return status[: -len(_PREFLIGHT_SUFFIX)] if status.endswith(_PREFLIGHT_SUFFIX) else status


def _is_preflight(settings) -> bool:
	"""Claimed, but not yet known to be rebuilding. See ``_PREFLIGHT_SUFFIX``."""
	return (settings.get("last_sync_status") or "").endswith(_PREFLIGHT_SUFFIX)


def _reset_in_flight(settings) -> str:
	"""The marker of a reset this bench has CLAIMED, or "" if none.

	The GUARD predicate — ``disconnect_bench``'s refusal, the depth check, and the
	poll's ``resetting`` display field. It matches pre-flight AND converging,
	because from the point of view of "may another workspace operation start", a
	claimed reset counts whether or not the rebuild has been confirmed.

	Deliberately NOT the same predicate the poll converges on
	(``_workspace_reset_poll_locked``'s ``_resetting()``), which excludes
	pre-flight. Guarding and converging are different questions and answering them
	with one predicate is what made a transient failure destructive — see
	``_PREFLIGHT_SUFFIX``.

	The bench, not the browser, is the authority on this. An in-flight reset was
	previously guarded only by a disabled button in the SPA, which two open Settings
	tabs defeat: the tab that did not start the reset never learns it is running.
	That matters because the marker lives in ``settings_reset.CONNECTION`` — a
	concurrent disconnect blanks it, and ``reconcile_pending_workspace_reset`` then
	has nothing left to converge, stranding the rebuild permanently."""
	status = settings.get("last_sync_status") or ""
	return status if status.startswith(_RESETTING_STATUS) else ""


def _workspace_op_in_flight(settings) -> str:
	"""Any claimed workspace operation — a reset at either state, OR a disconnect.

	The reset and the disconnect are mutually exclusive and each must refuse while
	the other is claimed, so both guards ask THIS rather than only about their own
	kind. ``_reset_in_flight`` on its own would let a reset start on top of a
	disconnect that is mid-teardown, and then clear ``CONNECTION`` under it."""
	status = settings.get("last_sync_status") or ""
	return _reset_in_flight(settings) or (status if status == _DISCONNECTING_STATUS else "")


# The critical section both entry points serialise on: read the marker, decide,
# and CLAIM it, as one indivisible step.
#
# A guard that reads state it has not yet claimed is not a guard.
# ``request_workspace_reset`` used to read the marker first and write it LAST,
# inside ``_disconnect_agent_transport``, after ``_recovery_outlook`` (8s),
# ``post_subscription_disconnect`` (180s budget), ``admin_client.reset_workspace``
# (180s, and it STARTS the rebuild) and the content wipe. For that whole
# multi-minute window ``disconnect_bench``'s ``_reset_in_flight`` guard saw no
# marker and let the disconnect through — blanking ``last_sync_status`` (it is in
# ``settings_reset.CONNECTION``) and leaving ``reconcile_pending_workspace_reset``
# nothing to converge, with the rebuilt container stranded and no bench able to
# reach it.
#
# One lock name, not one per action: all three ARE mutually exclusive.
# Serialising a reset only against other resets would leave exactly the
# reset-vs-disconnect race this exists to close.
#
# THREE holders, and the poll is not an afterthought. Claiming the marker early
# creates a state the old ordering never had: marker set, rebuild NOT yet
# requested, OLD container still up and Ready. ``_workspace_reset_poll``'s
# convergence test is ``ready and _resetting()``, and admin still reports the old
# container as Ready — so a ``*/5`` ``reconcile_pending_workspace_reset`` landing
# in that window would declare the reset complete against the container that was
# never replaced, and for an L4 go on to clear the credentials. The poll takes
# this lock too, non-blocking: while a request holds it there is by definition
# nothing to converge.
_RESET_LOCK = "workspace-reset"
# TTL: the backstop for a worker that dies holding it.
#
# 60s, because the guarded section is now only re-read -> guards -> claim-commit.
# It was 600s when the lock was held across the whole multi-minute request, and
# that was the wrong shape: redis_lock's release is a ``finally``, which a
# SIGKILLed worker never runs, and under a ~120s gunicorn ``http_timeout`` a
# SIGKILL mid-reset is the ORDINARY end of a slow reset rather than an
# exceptional one. Every convergence — including the ``*/5`` reconcile — then
# froze for the full ten minutes, and an L4's credential clear with it.
#
# What the lock stopped protecting when it shrank is carried by the marker
# instead, which is what a durable claim is for: see _PREFLIGHT_SUFFIX.
_RESET_LOCK_TTL_S = 60
# Wait: a contender is a second Settings tab clicking a moment later, not a
# convoy. Long enough that an ordinary overlap queues rather than erroring,
# short enough that a genuinely stuck holder surfaces as a message the customer
# can act on instead of a hung request.
_RESET_LOCK_WAIT_S = 5.0


def _reread_inside_lock():
	"""Re-read Jarvis Settings so a guard sees what ANOTHER worker committed.

	``settings.reload()`` alone does NOT do this, and three call sites used to say
	it did. Frappe holds ONE MariaDB transaction per request — pymysql's
	``autocommit`` default is False and ``get_connection_settings`` never
	overrides it, while ``Database.commit()`` immediately re-opens a transaction
	with ``START TRANSACTION``. Frappe also never overrides the isolation level
	for MariaDB, and this deployment reports ``REPEATABLE-READ``. Under InnoDB
	that means every consistent nonlocking read in a transaction returns the
	snapshot pinned by the FIRST read in it. ``Document.reload()`` is
	``load_from_db()`` -> ``get_singles_dict(..., for_update=False)``, a plain
	consistent read, so it re-issues the identical SELECT and gets the identical
	rows. Measured, not reasoned: a second connection's committed INSERT is
	invisible to a re-read and visible immediately after ``frappe.db.commit()``.

	So the snapshot has to END first. ``commit()`` rather than a ``for_update``
	locking read: the alternative would hold InnoDB row locks on ``tabSingles``
	for the whole guarded section, which here spans multi-minute admin calls and
	would block every unrelated writer to Jarvis Settings.

	Safe to commit at these three call sites specifically: each runs at the TOP of
	its locked section, before the function has written anything, so there is no
	half-finished work to make durable. Do not move a call to this below a write.

	Returns a freshly-read doc; callers must use the returned object.
	"""
	frappe.db.commit()
	return frappe.get_single("Jarvis Settings")


def _claim_reset(settings, marker: str) -> None:
	"""Write the resetting marker and commit it, so every other request sees it.

	Committed immediately and deliberately: the whole point is that a concurrent
	``disconnect_bench`` in another worker reads it, and an uncommitted write in
	this transaction is invisible to that worker.

	Stamps ``workspace_reset_claimed_at`` alongside. A claim with no timestamp
	cannot be resolved later: ``reconcile_pending_workspace_reset`` needs it both
	to tell admin's fresh request row from last week's (the customer who runs an L1
	at 10:00 and an L4 at 10:05 defeats any "is there a recent row" heuristic) and
	to know when an unresolvable claim has waited long enough to expire."""
	settings.db_set("last_sync_status", marker)
	settings.db_set("workspace_reset_claimed_at", frappe.utils.now_datetime())
	frappe.db.commit()


def _promote_reset_claim(settings, marker: str) -> None:
	"""Turn a PRE-FLIGHT claim into a converging one. Admin accepted the request.

	The single place a marker becomes something ``_workspace_reset_poll`` may
	converge on, and it is reached only after ``admin_client.reset_workspace``
	RETURNED — i.e. on positive evidence that a rebuild is actually happening.
	Everything else leaves the claim pre-flight for reconcile to resolve.

	``workspace_reset_claimed_at`` is kept, not cleared: the reset is still in
	flight, and a converging marker can still be orphaned by a crash before the
	poll finishes."""
	settings.db_set("last_sync_status", marker)
	frappe.db.commit()


def _release_reset_claim(settings, prior: str) -> None:
	"""Undo a claim whose reset PROVABLY never started.

	Called only when admin was reached and REFUSED — a 4xx/401/403/429 — so nothing
	was torn down and no rebuild is running, and restoring the previous status is
	honest. Without it, a refused request would leave the bench displaying
	"resetting" forever, with ``reconcile_pending_workspace_reset`` polling a reset
	that does not exist and ``disconnect_bench`` refused by a guard protecting
	nothing.

	NOT called on a timeout or a 5xx, and that distinction is load-bearing rather
	than cautious. Admin's ``reset_workspace`` runs the destroy + reprovision
	synchronously inside the HTTP request and commits its request row before
	starting, while this bench gives up at ``timeout_s=180``. A bench-side timeout
	is therefore the ORDINARY shape of a slow rebuild that is running, not evidence
	of one that is not. Releasing there blanks the marker mid-rebuild and strands
	the container — the same outcome the claim exists to prevent."""
	settings.db_set("last_sync_status", prior)
	settings.db_set("workspace_reset_claimed_at", None)
	frappe.db.commit()


# Customer content wiped by the "also delete my data" reset option. Raw table
# deletes (single-tenant DB, no hooks) — the dev.reset_onboarding precedent.
# Deliberately NOT wiped: Jarvis Settings, user settings/usage, agent listings/
# installations, relay pump, File rows.
_WIPE_DOCTYPES = (
	# chats
	"Jarvis Chat Message",
	"Jarvis Chat Turn",
	"Jarvis Turn Effect",
	"Jarvis Chat Session",
	"Jarvis Conversation",
	"Jarvis Voice Note",
	# skills
	"Jarvis Custom Skill Allowed Role",
	"Jarvis Custom Skill Share",
	"Jarvis Custom Skill",
	"Jarvis Skill Promotion Request",
	"Jarvis Shared Skill Slug",
	# macros + triggers + approvals
	"Jarvis Macro Step",
	"Jarvis Macro Run",
	"Jarvis Macro",
	"Jarvis Trigger Activity",
	"Jarvis Trigger",
	"Jarvis Approval Request",
	# learning artifacts
	"Jarvis Learned Pattern Role",
	"Jarvis Learned Pattern",
	"Jarvis Pattern Run",
	"Jarvis Pattern Snapshot",
	"Jarvis Pattern Detector State",
	"Jarvis Personalise Question Rule",
	"Jarvis Personalise Question",
	"Jarvis App Learning Run",
	# wiki + dashboards
	"Jarvis Wiki Page",
	"Jarvis Wiki Graph History",
	"Jarvis Wiki Promotion Request",
	"Jarvis Dashboard Source",
	"Jarvis Dashboard",
)


@frappe.whitelist()
def request_workspace_reset(
	reason: str = "",
	wipe_data: bool = False,
	revoke_llm: bool = False,
	disconnect_after: bool = False,
) -> dict:
	"""Self-serve workspace reset: the control plane rebuilds the container NOW
	(subscription kept), then this site disconnects its agent transport and polls
	``workspace_reset_state`` back to Ready.

	Four depths, each adding to the one above:

	  L1  (no flags)         rebuild the container
	  L2  wipe_data          + delete workspace content (chats, skills, macros,
	                           triggers, learning artifacts, wiki, dashboards)
	  L3  revoke_llm         + clear every LLM connection (pool models,
	                           subscription accounts, keys, synced markers) so the
	                           customer sets their AI model up fresh
	  L4  disconnect_after   + clear the admin connection itself — full
	                           ``bench reset-onboarding`` parity

	L2/L3 run only AFTER the control plane accepted the reset. L4 is different: it
	CANNOT run here, because the poll that watches the rebuild authenticates with
	the very credentials it clears. Doing it now would leave the reset initiated
	on the control plane and unobservable from this bench. The intent is recorded
	on the marker instead and ``_workspace_reset_poll`` performs the clear once
	the new container reports Ready — see ``_DISCONNECT_SUFFIX``.

	L4 leaves this bench with no credentials; the way back is the emailed-code
	reconnect. So it is refused up front when that recovery would not work, before
	anything is rebuilt or cleared — ``_recovery_outlook``.

	Gated on System Manager, like the rest of onboarding."""
	from jarvis._redis_lock import redis_lock

	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	disconnect_after = bool(cint(disconnect_after))
	reconnect_llm = bool(cint(revoke_llm))
	wipe_data = bool(cint(wipe_data))
	# The ladder is CUMULATIVE — each level adds to the one above — and until now
	# only the SPA's radio group enforced that. The server took three independent
	# booleans, so ``disconnect_after=1, revoke_llm=0, wipe_data=0`` was accepted
	# and produced a depth with no name: the marker is
	# ``"pending: resetting workspace (disconnect)"``, for which ``_reconnect_llm()``
	# is false, so the poll requires full chat-Ready before it will converge — while
	# the customer has asked for the connection to be destroyed at the end. Nothing
	# in the ladder describes that combination, and no test covers it.
	#
	# Enforced here rather than fixed in the poll, because the poll is not what is
	# wrong: the request is. Round-4 MINOR 4.
	if disconnect_after and not (wipe_data and reconnect_llm):
		frappe.throw(
			"Reset depth is cumulative: disconnecting this bench (L4) includes wiping "
			"workspace content and revoking LLM connections. Send those too, or choose a "
			"shallower reset.",
			frappe.ValidationError,
		)
	if reconnect_llm and not wipe_data:
		frappe.throw(
			"Reset depth is cumulative: revoking LLM connections (L3) includes wiping "
			"workspace content. Send wipe_data too, or choose a shallower reset.",
			frappe.ValidationError,
		)
	marker = _reset_marker(reconnect_llm, disconnect_after)
	prior_status = settings.get("last_sync_status") or ""

	# The lock is held for the WHOLE request, not just the check-and-claim. Two
	# separate windows need covering and one lock covers both:
	#
	#   check -> claim   a concurrent disconnect must not slip between reading the
	#                    marker and writing it (Amendment 2 BLOCKER 2).
	#   claim -> rebuild the marker is now set while the OLD container is still up
	#                    and Ready, so a concurrent poll would "converge" the reset
	#                    against a container that was never replaced. See _RESET_LOCK.
	#
	# The marker claim is still what makes the guard DURABLE — the lock dies with
	# the worker, the marker does not, and a crash between here and
	# ``admin_client.reset_workspace`` returning would otherwise leave admin
	# rebuilding a container this bench has no record of resetting.
	with redis_lock(
		_RESET_LOCK, timeout_s=_RESET_LOCK_TTL_S, blocking_timeout_s=_RESET_LOCK_WAIT_S
	) as acquired:
		if not acquired:
			frappe.throw(
				"Another workspace operation is already running on this site. Wait for it to "
				"finish, then try again.",
				frappe.ValidationError,
			)
		settings = _reread_inside_lock()
		prior_status = settings.get("last_sync_status") or ""
		# A reset already running at a DIFFERENT depth must not be silently
		# re-labelled. Admin is idempotent (named_lock + return the in-flight
		# request), so a repeat submission does not start a second rebuild — but
		# this bench would still overwrite the marker, and the marker is where the
		# depth lives. Re-submitting the SAME depth stays idempotent; changing it is
		# refused rather than applied to a rebuild the customer did not choose it for.
		in_flight = _workspace_op_in_flight(settings)
		if in_flight == _DISCONNECTING_STATUS:
			# A disconnect is mid-teardown. Its lock is already released (it only
			# covers check-and-claim), so this marker is the only thing standing
			# between a reset and a bench that is about to lose its credentials.
			frappe.throw(
				"This bench is being disconnected. Wait for that to finish before resetting.",
				frappe.ValidationError,
			)
		# Compared with the pre-flight suffix STRIPPED. A same-depth resubmission
		# while the first attempt is still pre-flight is idempotent, not "a reset at
		# a different depth" — comparing raw strings would refuse it with a message
		# that is simply untrue.
		if in_flight and _strip_preflight(in_flight) != marker:
			frappe.throw(
				"A workspace reset is already running at a different depth. Wait for it to finish, "
				"then start the deeper one.",
				frappe.ValidationError,
			)
		if disconnect_after:
			# Before the rebuild, not after: a customer who cannot reconnect must not
			# reach a state where the only thing standing between them and a lost
			# workspace is a code that will never be issued.
			outlook = _recovery_outlook()
			if not outlook.recoverable:
				frappe.throw(outlook.reason, frappe.ValidationError)
		# CLAIM, before any long call, as PRE-FLIGHT. Committed, so another worker's
		# ``disconnect_bench`` reads it even though this transaction is still open.
		# Pre-flight, so no poll can converge on it until admin has actually
		# accepted the request — see _PREFLIGHT_SUFFIX.
		_claim_reset(settings, _reset_marker(reconnect_llm, disconnect_after, preflight=True))

	# The lock ends HERE, not after the long calls. It only ever had to make
	# check-and-claim atomic; the marker carries in-flight-ness from now on, and it
	# is durable where the lock is not (redis_lock's release is a `finally`, which
	# a SIGKILLed worker never runs — and under a ~120s gunicorn http_timeout that
	# is the ORDINARY end of a slow reset, not an exceptional one). Holding it
	# across the calls below froze every convergence for the whole TTL.
	# Tear down the container-side OAuth auth-profile while the old container is
	# still reachable (same ordering as dev.reset_onboarding). Best-effort — a
	# dead container is often the very reason for the reset, and unlike the L4
	# disconnect, nothing irreversible follows here: the container is being
	# replaced wholesale and the bench keeps its credentials either way.
	if settings.get("agent_url"):
		try:
			admin_client.post_subscription_disconnect()
		except Exception:
			pass
	# NOT via _surface: it maps every admin error onto a bare
	# frappe.ValidationError, and the CLASS is precisely what decides whether the
	# claim may be given back. Mapped by hand below through the same
	# _throw_admin_error, so the customer-facing sentence is unchanged.
	try:
		out = admin_client.reset_workspace(reason)
	except (
		AdminRejectedError,
		AdminValidationError,
		AdminAuthError,
		AdminRateLimitedError,
	) as exc:
		# Admin was REACHED, validated the request and declined it (4xx / 401 / 403 /
		# 429, or a 5xx carrying a permanent rejection code). Nothing was started, so
		# the claim must not linger.
		#
		# AdminRejectedError is listed FIRST and explicitly: it subclasses
		# AdminUnreachableError, so without it a permanent refusal fell through to the
		# generic handler and blocked the customer for 15 minutes on a request admin
		# had provably declined.
		_release_reset_claim(settings, prior_status)
		_throw_admin_error(exc)
	except Exception as exc:
		# Everything else — a timeout, a 5xx, a network fault — is NOT evidence that
		# nothing is running, so the claim STAYS, and stays PRE-FLIGHT.
		#
		# Admin runs the destroy + reprovision SYNCHRONOUSLY inside the HTTP request,
		# committing its "Applying" row before it starts, while this bench gives up at
		# timeout_s=180. So a bench-side timeout is the ordinary shape of a rebuild
		# that IS running, and releasing here would blank the marker mid-rebuild and
		# strand the container.
		#
		# But it is equally NOT evidence that one IS running. Leaving a CONVERGING
		# marker here was the defect: the raise unwinds the lock (redis_lock's
		# `finally` DOES run on an exception), and five minutes later reconcile would
		# converge against a container that was never rebuilt — clearing the
		# credentials on an L4. Pre-flight is the honest state, and
		# reconcile_pending_workspace_reset resolves it by asking admin whether the
		# request actually exists.
		frappe.logger().info(
			"workspace reset: admin call failed without proving the rebuild did or did not "
			f"start; leaving the claim PRE-FLIGHT for reconcile to resolve "
			f"({type(exc).__name__}: {exc})"
		)
		if isinstance(exc, _ADMIN_ERRORS):
			_throw_admin_error(exc)
		raise
	# PROMOTE. Admin accepted the request, so a rebuild really is happening and the
	# poll's job is now to watch for it. This is the positive evidence, and it is the
	# only thing that turns a claim into something a poll may converge on.
	_promote_reset_claim(settings, marker)
	if wipe_data:
		_wipe_workspace_content()
	if reconnect_llm:
		_revoke_llm_connections(settings)
	_disconnect_agent_transport(
		settings,
		reconnect_llm=reconnect_llm,
		disconnect_after=disconnect_after,
	)
	return out


def _wipe_workspace_content() -> None:
	for dt in _WIPE_DOCTYPES:
		frappe.db.delete(dt)


def _revoke_llm_connections(settings) -> None:
	"""Clear the LLM subset — direct creds, OAuth state, the models[] pool and the
	synced markers — so is_ready_for_chat routes to the LLM setup step. Admin creds
	untouched. Shares its field list with the reset-onboarding CLI."""
	from jarvis import settings_reset

	settings_reset.apply(settings, settings_reset.LLM)


def _disconnect_agent_transport(
	settings, reconnect_llm: bool = False, disconnect_after: bool = False
) -> None:
	"""Clear the agent transport so chat gates on admin's chat_readiness while
	the container is rebuilt. Keeps admin creds and (unless the customer revoked
	them) the LLM config + ``*_synced_at`` markers — the control plane carries
	those onto the new container; clearing them would eject the customer into
	the setup wizard."""
	from jarvis import tenant_authority
	from jarvis._password_utils import clear_settings_password
	from jarvis.account import _bust_chat_gate
	from jarvis.chat.device import clear_credentials

	settings.db_set("agent_url", "")
	clear_settings_password(settings, "agent_token")
	# The rebuilt container is a new authority generation; forget the accepted
	# (generation, handle) so the poll that re-attaches it is not rejected as
	# "older" than the pre-reset generation (review plan 04 P0-5).
	tenant_authority.clear(settings)
	clear_credentials()
	# End the established chat-Ready claim BEFORE the fail-open policy can see it
	# (review P0-06). This is the self-serve rebuild path: the container is being
	# torn down and replaced, so the workspace admin confirmed Ready no longer
	# exists. Clearing agent_url above already moves the authority anchor, but the
	# marker is cleared explicitly too so the intent is not left to a side effect.
	settings.db_set("chat_was_ready_at", None)
	settings.db_set("chat_ready_authority", "")
	settings.db_set("last_sync_status", _reset_marker(reconnect_llm, disconnect_after))
	_bust_chat_gate()
	frappe.db.commit()


class _RecoveryOutlook(NamedTuple):
	"""Whether this bench could get back after its admin connection is cleared.

	``needs_company`` is not a refusal — see ``_recovery_outlook``."""

	recoverable: bool
	needs_company: bool
	reason: str


def _recovery_outlook() -> _RecoveryOutlook:
	"""Would the emailed-code reconnect return this bench to its account?

	Asks admin's ``billing.reconnect.can_reconnect_me`` (via
	``admin_client.reconnect_eligibility_me``) rather than re-deriving eligibility
	here, so the rules — subscription in Active/Suspended, and a live tenant — stay
	on the side that owns them and are free to change without this bench knowing.

	It is NOT ``_resolve_customer``, and an earlier version of this docstring said
	it was. ``can_reconnect_me`` applies the same predicates (``_ELIGIBLE_STATUSES``,
	``_has_live_tenant``) to the SESSION's customer, and shares
	``_eligible_accounts`` with ``_resolve_customer`` for the ambiguity half — but
	it is a separate function answering "could the caller reconnect", not
	"which account does this code reconnect". Those are different questions and
	only the shared helper keeps their answers aligned; there is no single call
	that makes drift impossible by construction.

	Fails CLOSED, and for a different reason than ``reconnect_available`` above:
	that one hides a hint when admin blips, this one refuses an IRREVERSIBLE
	clear. Refusing costs the customer a retry; clearing on a wrong "yes" costs
	them their workspace with no self-serve way back.

	``needs_company`` is reconnectable, not refused. It means the customer's contact
	address owns several accounts reconnect would also accept, so they must name one
	when they redeem the code. Admin computes it; treating it as "not recoverable"
	would block a customer who can in fact recover.

	Sends NO identity, deliberately. The obvious-looking
	``jarvis_admin_customer_email`` holds admin's synthetic OAuth login
	(``cust-<hash>@jarvis.invalid`` from ``signup._synthetic_login``), never a
	contact address. An earlier version of this function passed it to the guest
	``can_reconnect``, which resolves on the real address — so the precheck matched
	nothing, refused every genuine bench, and rendered a value the Jarvis Settings
	field documents as never-to-be-shown into a customer toast. Admin derives the
	customer from this bench's credentials instead, which cannot be got wrong here.

	Takes no arguments. It used to accept ``settings`` and never read it — every
	fact it needs is derived by admin from this bench's credentials, which is the
	entire point of the redesign above.
	"""
	try:
		_require_admin_url()
		data = admin_client.reconnect_eligibility_me() or {}
	except Exception as exc:
		# Deliberate degrade: an unverified recovery path is treated as no recovery
		# path. Same shape as reconnect_available's catch, opposite purpose — that
		# one hides a hint, this one refuses an irreversible clear.
		#
		# Logged WITH the exception, to Error Log rather than the info log. This is
		# the branch that refuses an irreversible action, and a fixed sentence with
		# no detail left an operator unable to tell a 403 (this account genuinely
		# cannot reconnect) from a DNS failure (nothing is known at all) — two very
		# different things to tell a customer who cannot disconnect.
		frappe.log_error(
			title="disconnect precheck: could not confirm reconnect eligibility; refusing",
			message=f"{type(exc).__name__}: {exc}\n\n{frappe.get_traceback()}",
		)
		return _RecoveryOutlook(
			False,
			False,
			"Jarvis Admin could not be reached to confirm this site can be reconnected afterwards. "
			"Nothing was changed — try again shortly.",
		)
	if data.get("recoverable"):
		return _RecoveryOutlook(True, bool(data.get("needs_company")), "")
	# Admin owns the refusal wording: it knows the account state, and it is the side
	# that can phrase one without naming an address this bench holds no legitimate
	# copy of. Fall back only if it sent none.
	return _RecoveryOutlook(
		False,
		False,
		data.get("reason")
		or "This account cannot be reconnected afterwards, so disconnecting now would leave no "
		"self-serve way back. Contact support.",
	)


class BenchTeardownRefused(frappe.ValidationError):
	"""admin was REACHED and the container teardown did not complete.

	Distinct from ``AdminUnreachableError`` on purpose, and the distinction is
	the whole of T14: unreachable is the ONLY condition that authorizes clearing
	the credentials without a confirmed teardown. Everything else — a refusal, a
	rate limit, an auth failure, a device unpair that was attempted and failed —
	means a retry can still succeed, so the clear must abort instead.

	A ``frappe.ValidationError`` subclass so ``disconnect_bench`` surfaces it the
	same way it surfaces its other two refusals, while ``_workspace_reset_poll``
	can still catch this class specifically without swallowing unrelated errors.
	"""


def _teardown_container_via_admin() -> dict:
	"""Have admin tear the container down, and decide whether this bench may
	proceed to destroy its credentials.

	Replaces the pair of calls this used to make (``post_subscription_disconnect``
	+ ``unpair_chat_devices``). Both gate on admin's ``current_customer``, which
	403s a Suspended account — a cohort deliberately admitted to the disconnect —
	so for exactly that cohort both were refused, and both refusals were caught by
	a bare ``except`` and logged in the same sentence as a dead container. The
	container kept its OAuth auth-profile and every paired chat-device token, and
	this bench then destroyed the only credentials that could ever revoke them.

	Returns admin's report when the clear may proceed. Raises
	``BenchTeardownRefused`` when it may not.

	The decision, and why each way:

	* ``AdminRejectedError`` — admin was reached and permanently refused. ABORT.
	  Checked BEFORE AdminUnreachableError because it is a SUBCLASS of it
	  (``jarvis.exceptions``); ordering these the other way silently turns every
	  permanent rejection into a proceed.
	* ``AdminUnreachableError`` — network fault, timeout, or a 5xx with no
	  recognised code. PROCEED. This is the only escape hatch, and it has to
	  exist: without it a bench whose control plane is gone can never leave its
	  tenancy, which is worse than the risk it accepts.
	* ``AdminValidationError`` (4xx: ResetLocked, MoveInFlight, UnknownProvider),
	  ``AdminAuthError``, ``AdminRateLimitedError`` — admin answered and said no.
	  ABORT; the customer retries.
	* ``devices_unpaired`` false — the unpair was ATTEMPTED and failed. ABORT.
	  This is the leg that must not be skipped: a surviving auth profile costs
	  the customer their own LLM credential, a surviving device token gives a
	  third party chat access to the workspace, and after the clear nothing can
	  revoke it. NOTE this gate applies only when admin ANSWERED — the
	  ``AdminUnreachableError`` branch above returns before reaching it, and must,
	  because nothing is known there and the escape hatch has to exist. So the
	  device leg is guaranteed against a reachable admin, not unconditionally.
	* ``profile_cleared`` false with devices unpaired — PROCEED, logged. This is
	  the ordinary dead-container disconnect: unpair is a file operation that
	  works on a stopped container, the auth-profile clear runs doctor + restart
	  and does not. Refusing here would block the case the disconnect exists for
	  (plan edge case 6), and the cost is bounded to the customer's own credential.
	* anything else — ABORT. Fail-safe: an unclassified failure is not evidence
	  that the teardown happened, and the action on the other side is irreversible.
	"""
	try:
		report = admin_client.prepare_bench_disconnect() or {}
	except AdminRejectedError as exc:
		raise BenchTeardownRefused(
			f"Jarvis Admin refused to release this bench ({exc}). Nothing was changed."
		) from exc
	except AdminUnreachableError as exc:
		frappe.logger().info(f"disconnect: admin unreachable during container teardown; proceeding ({exc})")
		return {"profile_cleared": False, "devices_unpaired": False, "removed": 0, "detail": str(exc)}
	except (AdminValidationError, AdminAuthError, AdminRateLimitedError) as exc:
		raise BenchTeardownRefused(
			f"Jarvis Admin could not release this bench yet: {exc} Nothing was changed — try again shortly."
		) from exc
	except Exception as exc:
		frappe.log_error(
			title="disconnect: unclassified failure preparing the container teardown",
			message=frappe.get_traceback(),
		)
		raise BenchTeardownRefused(
			f"Could not confirm this workspace's container was released ({type(exc).__name__}). "
			"Nothing was changed — try again shortly."
		) from exc

	if not report.get("devices_unpaired"):
		raise BenchTeardownRefused(
			"This workspace's paired chat devices could not be unpaired, so they would keep "
			"access after this bench loses its credentials. Nothing was changed — try again "
			f"shortly. ({report.get('detail') or 'no detail'})"
		)
	if not report.get("profile_cleared"):
		# Proceeding deliberately: see the docstring. Logged as an error, not an
		# info, because it leaves the customer's own LLM credential live on a
		# container no bench can reach again.
		frappe.log_error(
			title="disconnect: container auth-profile survived the teardown",
			message=f"devices unpaired ({report.get('removed')}); "
			f"auth profile NOT cleared: {report.get('detail')}",
		)
	return report


def _clear_admin_connection(settings, *, needs_company: bool = False) -> list:
	"""Tear this bench off its tenancy: the ``CONNECTION`` field set, plus the
	container-side state that does not live in Jarvis Settings.

	Ordering is load-bearing and matches ``dev.reset_onboarding``: after the field
	loop this bench can no longer authenticate to admin, so everything needing
	admin runs first.

	The teardown is NO LONGER best-effort. It was, and that was the defect: a
	refusal and a dead container were caught by the same bare ``except`` and
	logged in the same sentence, so the one case where the teardown was possible
	and got skipped was indistinguishable from the one where it was impossible.
	``_teardown_container_via_admin`` makes that distinction and raises
	``BenchTeardownRefused`` when the clear must not proceed — callers handle it.

	``settings_reset.apply`` covers more than it looks: the chat-device quartet
	(the same four fields ``chat.device.clear_credentials`` drops, via the same
	``clear_settings_password`` so the ``__Auth`` rows go too), the authority
	generation + handle (``tenant_authority.clear``'s pair), and the cached
	bearer, which it drops itself because ``CONNECTION`` carries passwords. The
	chat-readiness verdict cache is NOT a settings field, so it is busted here —
	without it a stale positive verdict outlives the connection it described.

	``needs_company`` is admin's answer from the caller's OWN eligibility check, and
	both callers have just made one. It is persisted here because this is the last
	moment it is knowable: after the sweep there are no credentials left to ask
	with. See ``bench_connection_state``.

	Returns the cleared field list for the caller's report.

	Raises:
		BenchTeardownRefused: nothing was cleared.
	"""
	from jarvis.account import _bust_chat_gate

	# UNCONDITIONAL, and that is the fix. This used to be gated on the bench's own
	# ``agent_url`` being non-empty — a leftover from the design where the BENCH
	# called the container-affecting endpoints itself. It is now admin that tears
	# the container down: it resolves the tenant with _any_tenant_for(customer) and
	# already answers profile_cleared/devices_unpaired true when there is nothing
	# to tear down. This bench's agent_url has no bearing on whether a container
	# exists or must be torn down, so the guard tested the wrong fact — and when it
	# was false it skipped the teardown with no raise, no log and no report, then
	# destroyed the credentials anyway, leaving every paired chat-device token live
	# on a workspace nothing could ever revoke them from.
	#
	# Reachable, not theoretical: on the L4 poll, write_connection declines to write
	# agent_url whenever tenant_authority.guard holds (REJECT, or an
	# AuthorityInvariantError from an equal generation with a different serving
	# container — what a reprovision onto a warm-pool container can produce). The
	# poll then re-read a Single still holding the "" committed at request time.
	_teardown_container_via_admin()

	from jarvis import settings_reset

	# CONNECTION plus the OAuth markers, because the teardown above just destroyed
	# the credential those markers describe. Left set, a bench that reconnects with
	# the emailed code SKIPS the LLM step and lands in a chat whose container holds
	# no auth profile - see settings_reset.OAUTH_MARKERS, plan edge case 21.
	spec = settings_reset.CONNECTION | settings_reset.OAUTH_MARKERS
	settings_reset.apply(settings, spec)
	# CONNECTION.blank clears last_sync_status to "" as part of the field sweep
	# above; write the durable disconnected marker AFTER that clear so it
	# survives it instead of leaving the field blank - see _DISCONNECTED_STATUS.
	settings.db_set("last_sync_status", _DISCONNECTED_STATUS)
	# The operation this claim was for is over. Not in any ResetSpec (it is
	# bookkeeping, not tenancy state), so it has to be cleared explicitly or a
	# resolved claim keeps a timestamp that reads as a live one.
	settings.db_set("workspace_reset_claimed_at", None)
	# T22: persist the ONE fact about recovery that cannot be recomputed once the
	# credentials are gone. Written here, after the sweep, for the same reason
	# _DISCONNECTED_STATUS is: CONNECTION.blank would otherwise wipe it. Both
	# callers have just asked admin, so both can supply it.
	settings.db_set("reconnect_needs_company", 1 if needs_company else 0)
	_bust_chat_gate()
	frappe.db.commit()
	return settings_reset.cleared_fields(spec)


def _admin_connection_absent(settings) -> bool:
	"""True when this bench holds neither signal a connected admin tenancy
	always has: the customer identity and the container address. Both are in
	``settings_reset.CONNECTION`` and both are blanked together by
	``_clear_admin_connection`` - checking BOTH (not either alone) is what keeps
	this from firing mid-reset, when ``_disconnect_agent_transport`` blanks
	``agent_url`` alone for EVERY depth (L1-L4) while ``jarvis_admin_customer_email``
	stays in place until a reset that actually reaches the CONNECTION clear.

	One definition, shared by ``disconnect_bench``'s idempotency check and
	``bench_connection_state``, so the two cannot disagree about what "no admin
	connection" means - same reasoning as ``_reset_marker``."""
	return (
		not (settings.get("jarvis_admin_customer_email") or "").strip()
		and not (settings.get("agent_url") or "").strip()
	)


@frappe.whitelist()
@rate_limit(limit=5, seconds=3600, ip_based=True)
def disconnect_bench() -> dict:
	"""Terminal, no-rebuild counterpart to L4: tear down the container-side OAuth
	auth-profile + chat devices while still reachable, then clear the admin
	connection. No rebuild, no poll, no resetting marker - this bench is LEAVING
	its tenancy, not resetting itself, so it must never be described as one.

	Idempotency comes before the recovery precheck, deliberately: a bench with no
	``jarvis_admin_customer_email`` and no ``agent_url`` (both in
	``settings_reset.CONNECTION``) is already disconnected, and letting that fall
	through to ``_recovery_outlook`` would throw "no registered customer email" -
	turning a harmless repeat click into an error that hides the real state
	instead of confirming it.

	Otherwise the same reconnect-eligibility precheck L4 uses runs first, and
	nothing is torn down or cleared unless it passes: a customer who could not
	get back in would be permanently locked out. ``needs_company`` is carried
	through rather than refused - it means the emailed-code reconnect works but
	needs the customer's company name to disambiguate, which the caller can
	surface as a heads-up.

	The container teardown then has its own veto: ``_teardown_container_via_admin``
	aborts the clear unless the paired chat devices were provably unpaired, or
	admin was genuinely unreachable. A dead CONTAINER still disconnects (the
	unpair is a file operation that survives it); a dead ADMIN still disconnects
	(otherwise a lost control plane traps this bench forever); an admin that
	answers and refuses does not.

	Rate-limited on top of ``require_jarvis_admin``: unlike L1-L4, this action
	never calls admin's ``reset_workspace``, so it never benefits from that
	endpoint's own 5/hr/IP budget."""
	from jarvis._redis_lock import redis_lock

	require_jarvis_admin()
	# The lock covers CHECK-AND-CLAIM only, exactly as request_workspace_reset's
	# does, and for the same reason: the teardown below can run to 240s
	# (admin_client.prepare_bench_disconnect's budget), and redis_lock's release is
	# a ``finally`` that a SIGKILLed worker never runs. Holding it across the
	# teardown froze every convergence — including the */5 reconcile — for the whole
	# TTL whenever gunicorn's http_timeout killed the request, which under a ~120s
	# ceiling is the ordinary end of a slow teardown, not an exceptional one.
	#
	# Taken BEFORE Jarvis Settings is read, not after. A doc fetched outside the
	# lock is a stale read: a reset could claim its marker and release the lock in
	# that gap, and this call would then see no marker, proceed, and clear
	# CONNECTION out from under a rebuild that is already running.
	with redis_lock(
		_RESET_LOCK, timeout_s=_RESET_LOCK_TTL_S, blocking_timeout_s=_RESET_LOCK_WAIT_S
	) as acquired:
		if not acquired:
			frappe.throw(
				"A workspace operation is already running on this site. Wait for it to finish, "
				"then disconnect.",
				frappe.ValidationError,
			)
		settings = _reread_inside_lock()
		if _admin_connection_absent(settings):
			return {
				"disconnected": True,
				"already_disconnected": True,
				"cleared": [],
				"needs_company": False,
			}
		# Refuse while ANY workspace operation is claimed, server-side. A reset in
		# flight has already blanked ``agent_url`` while the email survives, so the
		# idempotency test above does not fire and the clear would otherwise proceed
		# — blanking the marker with it (it is in CONNECTION) and leaving
		# reconcile_pending_workspace_reset nothing to converge, with the rebuilt
		# container stranded. The SPA disables the button, but a second Settings tab
		# never learns the reset started, so the browser cannot enforce this.
		#
		# _workspace_op_in_flight, not _reset_in_flight: a second disconnect landing
		# mid-teardown must be refused too, now that the lock no longer spans it.
		in_flight = _workspace_op_in_flight(settings)
		if in_flight == _DISCONNECTING_STATUS:
			frappe.throw(
				"This bench is already being disconnected. Wait for that to finish.",
				frappe.ValidationError,
			)
		if in_flight:
			frappe.throw(
				"A workspace reset is in progress. Wait for it to finish before disconnecting this bench.",
				frappe.ValidationError,
			)
		prior_status = settings.get("last_sync_status") or ""
		outlook = _recovery_outlook()
		if not outlook.recoverable:
			frappe.throw(outlook.reason, frappe.ValidationError)
		# CLAIM before the long calls, same as the reset. Nothing destructive has
		# happened yet, so an orphaned claim is always safe to expire and retry.
		_claim_reset(settings, _DISCONNECTING_STATUS)

	# Lock released. The claim carries in-flight-ness from here.
	try:
		cleared = _clear_admin_connection(settings, needs_company=outlook.needs_company)
	except BenchTeardownRefused as exc:
		# Nothing was torn down or cleared, so give the claim back rather than
		# leaving a marker that blocks the customer's next attempt.
		_release_reset_claim(settings, prior_status)
		# Re-thrown rather than raised straight through, so the reason reaches the
		# customer: the SPA renders ``e.messages[0]``, which comes from
		# ``_server_messages``, which only ``frappe.throw``'s msgprint populates. A
		# bare raise of the same class would degrade this to the generic "Could not
		# disconnect this bench." — the third refusal in this function to be
		# silently unexplained would be a poor place to end up.
		frappe.throw(str(exc), BenchTeardownRefused)
	except Exception:
		# Any other failure may have died PART WAY through the field sweep.
		# ROLL BACK FIRST. settings_reset.apply is a sequence of db_sets with no
		# commit until the end, so a partial clear is uncommitted and would be
		# discarded — but _release_reset_claim commits, which would make that
		# half-applied state durable. That is the property "no intermediate commit,
		# so a partial failure rolls back" depends on, and releasing without a
		# rollback silently broke it.
		frappe.db.rollback()
		# Then release: the bench is still connected and the claim is pointless.
		# reconcile would otherwise expire it on a 15-minute timer for a request
		# that already knows it failed.
		_release_reset_claim(settings, prior_status)
		raise
	return {
		"disconnected": True,
		"already_disconnected": False,
		"cleared": cleared,
		"needs_company": outlook.needs_company,
	}


@frappe.whitelist()
def bench_connection_state() -> dict:
	"""Durable answer to "is this bench disconnected from its admin tenancy",
	read from Jarvis Settings alone - no admin round-trip.

	Exists because both ``disconnect_bench()``'s response and the L4 poll's
	``disconnected: true`` (``_workspace_reset_poll``) are ONE-SHOT: only the
	caller that receives them ever sees that answer. Every later page load - a
	reload, a second tab, a tab that was closed mid-poll and converged later by
	``reconcile_pending_workspace_reset`` - has nothing to go on and falls
	through to the generic "isn't connected yet" onboarding poster, which never
	mentions the emailed-code reconnect: the ONLY way back once credentials are
	gone. This endpoint gives every later load the same durable answer.

	Making no admin call is not an optimisation here, it is required: a
	genuinely disconnected bench holds no admin credentials, so any
	authenticated admin call would simply fail.

	``disconnected`` reuses ``_admin_connection_absent`` - the exact predicate
	``disconnect_bench``'s own idempotency check uses - rather than testing
	``last_sync_status == _DISCONNECTED_STATUS``. See that constant's comment:
	the literal "disconnected" is already written to this same field by
	``jarvis.oauth.api.disconnect`` and ``onboarding.disconnect_llm`` for a
	chat-subscription / LLM disconnect that leaves admin credentials fully
	intact, so equality against it cannot tell that case apart from this one.

	``needs_company`` is admin's answer (``billing.reconnect``'s ambiguity
	branch, surfaced via ``reconnect_eligibility_me``) and cannot be recomputed
	here: once admin credentials are gone there is nothing left to ask. So it is
	PERSISTED at the moment of the clear by ``_clear_admin_connection``, onto
	``reconnect_needs_company`` — a field of its own, in no ``ResetSpec``,
	precisely because repurposing one ``settings_reset.CONNECTION`` already
	clears for an unrelated tenancy concern (release notice, catalog version,
	authority generation) would corrupt whichever feature owns it next.

	It used to be hardcoded ``False``, with this docstring deferring to "the
	one-shot ``disconnect_bench()`` / poll response". That deferral was wrong:
	``_workspace_reset_poll``'s return carried no ``needs_company`` either, and
	``GeneralPane.pollReset`` resolves the value by calling THIS endpoint — so
	after an L4 it was always false, and a customer whose registered address owns
	several eligible accounts was sent to a reconnect that could not complete
	with what they had been told.

	Gated on ``require_jarvis_access()``, NOT ``require_jarvis_admin()`` like its
	neighbours, and that difference is deliberate (round-4 MINOR 6).

	``OnboardingGate`` is the screen a disconnected bench actually lands on, for
	EVERY user — and it has copy for a non-admin teammate telling them to ask their
	workspace admin. Behind the admin gate that call 403s for exactly the person the
	copy is written for, so the ``catch`` left ``disconnected`` false and the
	generic first-time-setup poster rendered instead. The branch could never fire
	in production, and ``OnboardingGate.spec.js``'s test for it passed only because
	it mocks the API. Plan edge case 20 was unmet for non-admins.

	Safe to widen: this returns two booleans derived from local settings and makes
	no admin call. It carries no credential, no address and no tenancy identifier —
	``disconnected`` is already obvious to any user (chat has stopped working), and
	``needs_company`` only says a recovery step will ask for a name the user's own
	organisation owns. Anything that ACTS on this state is gated separately:
	``disconnect_bench`` and ``request_workspace_reset`` both keep
	``require_jarvis_admin``.
	"""
	require_jarvis_access()
	settings = frappe.get_single("Jarvis Settings")
	return {
		"disconnected": _admin_connection_absent(settings),
		"needs_company": bool(settings.get("reconnect_needs_company")),
	}


@frappe.whitelist()
def workspace_reset_state() -> dict:
	"""Poll endpoint for the reset card: admin request state + serving readiness.
	When admin reports the new container Ready, persists the fresh connection and
	clears the resetting marker — chat works again with no manual steps."""
	require_jarvis_admin()
	return _workspace_reset_poll()


def _workspace_reset_poll() -> dict:
	"""Read the reset's state and, when the rebuild is done, finish it.

	Takes ``_RESET_LOCK`` NON-BLOCKING and converges only if it gets it. While
	``request_workspace_reset`` holds it there is by definition nothing to
	converge — the marker is claimed but the rebuild has not been requested yet,
	and admin still reports the OLD container as Ready. Converging there would
	declare the reset complete against the container that was never replaced, and
	for an L4 go on to clear the credentials.

	Non-blocking rather than waiting, because both callers are pollers: the SPA
	re-asks every few seconds and ``reconcile_pending_workspace_reset`` every 5
	minutes. Reporting "still resetting" for one tick is free; queueing a request
	worker behind a multi-minute holder is not.
	"""
	from jarvis._redis_lock import redis_lock

	with redis_lock(_RESET_LOCK, timeout_s=_RESET_LOCK_TTL_S) as acquired:
		return _workspace_reset_poll_locked(may_converge=acquired)


def _workspace_reset_poll_locked(*, may_converge: bool) -> dict:
	# reconcile_pending_workspace_reset reads the Single itself before calling
	# here, so this request's read snapshot is already pinned and predates the
	# lock. _reread_inside_lock ends it; a plain reload() cannot.
	settings = _reread_inside_lock()
	req: dict = {}
	try:
		req = admin_client.reset_workspace_state() or {}
	except Exception:
		pass  # audit-row state is advisory; readiness below is the real signal

	def _resetting() -> bool:
		"""DISPLAY: is a reset claimed? Pre-flight counts — the customer's card
		should say "resetting" from the moment they click, not from the moment
		admin confirms."""
		return bool(_reset_in_flight(settings))

	def _may_converge_marker() -> bool:
		"""CONVERGENCE: is a rebuild known to be happening? Pre-flight does NOT
		count. Converging on a claim admin never accepted means declaring the reset
		complete against the container that was never replaced — and for an L4,
		tearing it down and clearing the credentials. See ``_PREFLIGHT_SUFFIX``."""
		return bool(_reset_in_flight(settings)) and not _is_preflight(settings)

	def _reconnect_llm() -> bool:
		return (settings.get("last_sync_status") or "").startswith(_RESETTING_RECONNECT_LLM_STATUS)

	def _disconnect_after() -> bool:
		# Stripped, like the depth check in request_workspace_reset. On a pre-flight
		# L4 marker the raw endswith() is False, because " (requested)" is the last
		# suffix - correct only by accident today, since this is called past the
		# pre-flight gate.
		return _strip_preflight(settings.get("last_sync_status") or "").endswith(_DISCONNECT_SUFFIX)

	try:
		data = admin_client.get_connection(timeout_s=8) or {}
	except Exception:
		return {
			"ready": False,
			"resetting": _resetting(),
			"status": req.get("status") or "",
			"message": req.get("message") or "",
		}
	# With the LLM revoked, readiness can never reach Ready (nothing configured);
	# "container reachable" completes the reset and the wizard gate owns the rest.
	ready = bool(data.get("agent_url")) and (data.get("chat_readiness") == "Ready" or _reconnect_llm())
	disconnected = False
	# T24 / round-4 MAJOR 2. Non-empty when the customer explicitly chose "reset and
	# disconnect", was shown the irreversibility warning, confirmed it — and then got
	# a SHALLOWER reset than they asked for. Degrading rather than clearing is the
	# right safety call; degrading rather than TELLING is not, and an Error Log entry
	# is not telling. The SPA renders this instead of the plain "Workspace is back"
	# toast, so the one poll that can answer actually does.
	disconnect_blocked = ""
	# Round-4 MINOR 7. ``ready`` used to be reported even when this pass could not
	# converge, and ``GeneralPane.pollReset`` ACTS on it: stopPoll() plus a hard
	# reload announcing "Workspace is back". So during a long L4 clear (up to 240s
	# in the teardown) a concurrent poll reloaded the page mid-clear, and the
	# terminal ``disconnected: true`` response was never seen by that tab.
	#
	# "Ready" is a claim about the customer's WORKSPACE being back, not about what
	# admin's container currently reports. While a reset is claimed and this pass
	# has not finished it, that claim is not yet true. Narrowed rather than
	# suppressed: a bench with no reset in flight is unaffected, so this cannot make
	# the card flicker on the ordinary path.
	#
	# PRE-FLIGHT is the second half of this condition, and gating on
	# ``not may_converge`` ALONE was wrong — round 5. ``may_converge`` is just "did
	# this pass get the lock", which is almost always true, so the narrowing hardly
	# ever fired. Meanwhile ``ready`` for an L3/L4 is
	# ``agent_url and (Ready or _reconnect_llm())``, and ``_reconnect_llm()`` matches
	# straight through the pre-flight suffix — so during pre-flight ``ready``
	# collapsed to "the OLD container is up", which is unconditionally true. With
	# T33 now starting the poll after a timed-out initiate, the very first tick
	# would have told the customer "Workspace is back — reloading" for a reset that
	# never started.
	if ready and _reset_in_flight(settings) and (not may_converge or _is_preflight(settings)):
		ready = False
	if may_converge and ready and _may_converge_marker():
		from jarvis.account import _bust_chat_gate

		disconnect_after = _disconnect_after()
		# GATED on the connection actually landing. write_connection returns False
		# when tenant_authority.guard HELD the write (a stale generation, or an equal
		# generation naming a different serving container). The guard's whole design
		# is "hold and re-poll" — so retiring the marker here would remove the very
		# retry it is waiting for, leaving a bench with agent_url still blank, no
		# marker, dead chat and nothing to converge it. For L4 it was worse: the same
		# branch went on to destroy the credentials.
		if not write_connection(data):
			frappe.logger().info(
				"workspace reset: connection write held by the authority guard; leaving the "
				"marker set so the next poll can converge"
			)
			return {
				"ready": ready,
				"resetting": _resetting(),
				"status": req.get("status") or "",
				"message": req.get("message") or "",
				"tenant_status": data.get("tenant_status") or "",
				"disconnected": False,
			}
		# The reset itself is done. For an L4 the marker does NOT go back to a
		# resting value here: it becomes the DISCONNECT claim, because the clear
		# below runs for up to ~264s (prepare_bench_disconnect's 240s budget plus
		# the eligibility re-check) and this poll's _RESET_LOCK expires at
		# _RESET_LOCK_TTL_S = 60.
		#
		# Retiring the marker first was a real hole: from t=60s there was neither
		# lock NOR marker, so a second tab's Reset passed every guard, called admin
		# and started a genuine rebuild — while the clear still in flight here
		# blanked CONNECTION underneath it. That is exactly the stranding the guards
		# exist to prevent, reached through the one long section that got the short
		# lock without a durable claim. T29 gave disconnect_bench this same shape;
		# the poll needed it too.
		_bust_chat_gate()
		# What the marker rests at once the disconnect half is over, one way or the
		# other. The RESET succeeded regardless of what the clear does next.
		reset_done_status = "ok (workspace reset)"
		if not disconnect_after:
			settings.db_set("last_sync_status", reset_done_status)
			frappe.db.commit()
		else:
			_claim_reset(settings, _DISCONNECTING_STATUS)
		if disconnect_after:
			# _clear_admin_connection overwrites the claim above with
			# _DISCONNECTED_STATUS on success, which is what stops
			# reconcile_pending_workspace_reset from picking this site up again —
			# there is no reset still pending. On either failure path below the
			# claim is given back to reset_done_status, so an L4-degraded-to-L3
			# leaves no marker behind; and if this worker dies mid-clear,
			# _expire_orphaned_disconnect_claim gives it back instead.
			#
			# write_connection() above took its OWN uncached frappe.get_single()
			# doc, so this ``settings`` object never observed that write. Re-read
			# it, now that the write is committed, before handing it to the clear.
			#
			# The round-1 fix here was to make _clear_admin_connection's
			# ``if agent_url:`` guard see a fresh value. That guard is GONE — it was
			# testing the wrong fact and skipping the teardown outright — so this
			# re-read no longer decides whether the container is torn down. It is
			# kept because settings_reset.apply is about to db_set this doc, and it
			# should not be a stale one.
			#
			# A NEW NAME, not a rebind of ``settings`` (round-4 MINOR 3). The
			# _resetting() / _reconnect_llm() / _disconnect_after() closures above
			# all read ``settings``, so rebinding it silently changed what they
			# returned from this line onward. That was correct only by accident —
			# ``disconnect_after`` is captured before it — and the next person to
			# call one of those closures below would have got a different answer for
			# no visible reason.
			fresh = frappe.get_single("Jarvis Settings")
			# T21 / round-4 MAJOR 8: RE-VALIDATE, minutes after the request-time
			# precheck and on the far side of a container rebuild.
			#
			# request_workspace_reset refused up front if the customer could not
			# reconnect, which satisfies plan edge case 2 AT REQUEST TIME. But for L4
			# the clear happens here, minutes later. A subscription that moved to
			# Cancelled during the rebuild — not Suspended; Cancelled is outside
			# billing.reconnect._ELIGIBLE_STATUSES — would have its credentials
			# destroyed with no emailed-code reconnect able to restore them. That is
			# the permanent lockout the plan calls "mandatory, not advisory" to
			# prevent, and one precheck "before clearing anything" cannot cover a gap
			# it sits minutes before.
			outlook = _recovery_outlook()
			if not outlook.recoverable:
				disconnect_blocked = outlook.reason
				# Give the disconnect claim back: the reset is done and nothing is
				# going to clear anything, so leaving the claim would block the
				# customer's next reset for the full expiry window.
				_release_reset_claim(settings, reset_done_status)
				frappe.log_error(
					title="workspace reset: L4 downgraded to L3, reconnect no longer available",
					message=f"the rebuild completed; the clear was abandoned: {outlook.reason}",
				)
			else:
				try:
					_clear_admin_connection(fresh, needs_company=outlook.needs_company)
					disconnected = True
				except BenchTeardownRefused as exc:
					# The RESET is complete; only the disconnect half could not be
					# done. Degrade to the L3 outcome — rebuilt container, bench still
					# connected — rather than 500ing a poll for a reset that succeeded,
					# and rather than clearing the credentials anyway.
					#
					# Raised by _teardown_container_via_admin, which runs BEFORE
					# settings_reset.apply, so nothing was cleared and the claim can be
					# given back safely.
					# Not str(exc) verbatim: those messages end "Nothing was changed",
					# which is true of the disconnect half and false of the reset half.
					disconnect_blocked = f"{exc} The workspace reset itself completed."
					_release_reset_claim(settings, reset_done_status)
					frappe.log_error(
						title="workspace reset: L4 completed the rebuild but could not disconnect",
						message=frappe.get_traceback(),
					)
				except Exception:
					# Anything else may have failed PART WAY through the field sweep.
					# Roll back first: settings_reset.apply is a sequence of db_sets
					# with no commit until the end, so an uncommitted partial clear
					# must not be made durable by the claim release that follows.
					frappe.db.rollback()
					disconnect_blocked = (
						"This bench could not be disconnected. The workspace reset itself "
						"completed. Nothing was changed — try again shortly."
					)
					_release_reset_claim(settings, reset_done_status)
					frappe.log_error(
						title="workspace reset: L4 clear failed unexpectedly",
						message=frappe.get_traceback(),
					)
	elif (
		may_converge
		and _may_converge_marker()
		and data.get("agent_url")
		and not (settings.get("agent_url") or "")
	):
		# New container reachable but not Ready yet: reconnect the transport and,
		# for a pool tenant, re-push the stored spec + subscription blobs — OAuth
		# creds never ride a rebuild, so without this a subscription pool stays
		# "blocked" forever. Hands convergence to the standard pending-applying
		# machinery (marker replaced; runs once — agent_url is set after this).
		write_connection(data)
		if settings.get("proxy_active"):
			settings._enqueue_pool_sync()
		frappe.db.commit()
	return {
		"ready": ready,
		"resetting": _resetting(),
		"status": req.get("status") or "",
		"message": req.get("message") or "",
		"tenant_status": data.get("tenant_status") or "",
		# Terminal for L4: this bench just lost its credentials, so this is the
		# last poll that can answer. Every later one falls into the unreachable
		# branch above. The UI needs the distinction — "you are disconnected, here
		# is how to come back" is not the same screen as "admin is down".
		"disconnected": disconnected,
		# The L4 the customer asked for became an L3. Reset done, bench still
		# connected, and this says why. One-shot by nature: the tab that started
		# the reset is polling at this exact moment, and a customer converged by
		# the */5 backstop instead sees a completed reset and a connected bench —
		# true, just unexplained. Persisting it durably is a second schema change
		# for a strictly rarer case; named in Amendment 3 OQ1 as a decision, not
		# an oversight.
		"disconnect_blocked": disconnect_blocked,
	}


# How long a PRE-FLIGHT claim may stay unresolved before it is given up on.
#
# Must exceed the worst case for a request that is genuinely still on its way to
# admin: _recovery_outlook (8s) + post_subscription_disconnect (180s) +
# reset_workspace (180s) + lock wait, i.e. ~6.5 min. 15 gives comfortable margin
# while still bounding how long a customer can be stuck behind a claim whose
# worker was SIGKILLed. Beyond it, a request worker would have to have outlived
# gunicorn's http_timeout many times over — which is the same premise that makes
# the SIGKILL ordinary in the first place.
_PREFLIGHT_EXPIRY_MINUTES = 15

# How long a PROMOTED (converging) claim may go unconverged before the bench gives
# up on it. Promotion proved a rebuild STARTED; it never promised one would finish,
# and a request admin abandoned — or a container that never comes back — would
# otherwise leave the marker outliving everything able to clear it.
#
# Deliberately generous: the poll converges the instant the container reports
# Ready, so this only fires when it never does. A warm-pool claim is seconds and a
# cold provision minutes, so 60 is far past any real rebuild while still bounded.
_RECONCILE_GIVE_UP_MINUTES = 60

# Slack on the AGE comparison in _resolve_preflight_claim. Admin's row is created
# moments after this bench stamps its claim, so a genuine row's age is slightly
# SMALLER; the slack covers the round trip and any clock rate difference without
# admitting a row from an earlier reset. Deliberately not a wall-clock tolerance —
# the comparison is between two durations, each measured on its own host's clock,
# so a timezone difference cannot enter it at all.
_CLAIM_CLOCK_SLACK_SECONDS = 120


def _resolve_preflight_claim(settings) -> None:
	"""Decide what a PRE-FLIGHT claim actually was, and leave no claim unresolved.

	A pre-flight marker means "this bench claimed a reset and does not know whether
	admin accepted it" — the state left by a timeout, a 5xx, or a worker killed
	between the claim and the promote. Nothing converges on it, deliberately, so
	without this resolver it would sit forever: exactly the *"no path may leave
	``last_sync_status`` on a resetting marker nothing can clear"* invariant the
	plan is built on, violated from the other side.

	Three outcomes:

	* Admin has a request row for THIS claim (``requested_at`` at or after the
	  claim, within clock slack) -> PROMOTE. The rebuild is real; the poll takes
	  over from here. This is the case where the bench's HTTP call died but admin
	  went on rebuilding regardless, which is the common one.
	* No such row, and the claim is younger than ``_PREFLIGHT_EXPIRY_MINUTES`` ->
	  leave it. The request may still be in flight; deciding now would be guessing.
	* No such row past the deadline -> give up honestly. Restore a status the
	  customer can act on, and clear the stamp. Nothing was torn down (the claim is
	  written before any destructive step), so this is safe to retry.

	Admin unreachable is NOT a verdict: leave the claim and try again next tick.
	Treating "cannot ask" as "no row" would expire a live rebuild's claim.
	"""
	claimed_at = settings.get("workspace_reset_claimed_at")
	try:
		req = admin_client.reset_workspace_state() or {}
	except Exception as exc:
		frappe.logger().info(f"reset claim: cannot reach admin to resolve a pre-flight claim ({exc})")
		return

	status = (req.get("status") or "").strip()
	# AGES, not wall clocks. Admin stamps ``requested_at`` in the ADMIN site's
	# timezone and this bench stamps its claim in its OWN, both naive local
	# datetimes — so comparing them directly makes a timezone difference a CONSTANT
	# BIAS of hours rather than skew of milliseconds. Bench ahead of admin would
	# expire every live rebuild's claim; bench behind admin would promote any row of
	# any age, permanently, which for an L4 is a credential clear against a
	# container that was never rebuilt. Both sites happen to be Asia/Kolkata today,
	# which is precisely why nothing caught it.
	#
	# ``requested_age_seconds`` is measured by admin on admin's clock; the claim's
	# age is measured here on ours. Two durations compare correctly across any pair
	# of timezones and any clock offset.
	admin_age = req.get("requested_age_seconds")
	claim_age = None
	if claimed_at:
		claim_age = frappe.utils.time_diff_in_seconds(
			frappe.utils.now_datetime(), frappe.utils.get_datetime(claimed_at)
		)
	# STATUS matters as much as the age. Promoting means "a rebuild is happening, go
	# converge it" — and a row that already reached a terminal FAILURE proves the
	# opposite. Promoting on one hands the poll a reset that can never complete and,
	# for an L4, a marker that eventually authorises clearing the credentials.
	_LIVE = ("Applying", "Pending Capacity", "Applied")
	if admin_age is not None and claim_age is not None and status in _LIVE:
		# The row is THIS claim's iff it is no OLDER than the claim, within slack:
		# admin's row is created moments AFTER the bench claims, so a genuine one has
		# a SMALLER age. A stale row from an earlier reset has a much larger one.
		if float(admin_age) <= float(claim_age) + _CLAIM_CLOCK_SLACK_SECONDS:
			promoted = _strip_preflight(settings.get("last_sync_status") or "")
			frappe.logger().info(
				f"reset claim: admin has a {status} request {admin_age}s old against a claim "
				f"{claim_age}s old; promoting"
			)
			_promote_reset_claim(settings, promoted)
			return
		frappe.logger().info(
			f"reset claim: admin's latest request is {admin_age}s old but this claim is only "
			f"{claim_age}s old — that row predates this claim, so it is not evidence for it"
		)
	elif status:
		frappe.logger().info(
			f"reset claim: admin's latest request is {status!r}; not evidence of a live rebuild"
		)

	if claimed_at:
		deadline = frappe.utils.add_to_date(
			frappe.utils.get_datetime(claimed_at), minutes=_PREFLIGHT_EXPIRY_MINUTES
		)
		if frappe.utils.now_datetime() < deadline:
			return

	# Past the deadline with no matching request: the reset never started. Say so
	# in a way the customer can act on rather than leaving a marker that means
	# "resetting" forever.
	frappe.log_error(
		title="workspace reset: claim expired without admin ever accepting the request",
		message=f"claimed_at={claimed_at!r} admin_request={req!r}",
	)
	_release_reset_claim(settings, "workspace reset did not start — try again")


def _expire_orphaned_disconnect_claim(settings) -> None:
	"""Give back a disconnect claim whose worker died before it finished.

	``disconnect_bench`` releases its lock after claiming, so the claim is all that
	blocks a concurrent reset — and if the worker is SIGKILLed mid-teardown nothing
	else will ever clear it. Without this the customer is locked out of BOTH
	actions permanently, which is the plan's clear-the-marker invariant violated by
	the disconnect path instead of the reset one.

	Safe to expire and retry: the claim is written before anything destructive, and
	both legs are idempotent — admin's teardown answers vacuous-true when there is
	nothing left to tear down, and a completed clear would have overwritten this
	marker with ``_DISCONNECTED_STATUS`` already. Seeing the claim still here means
	the clear did NOT complete, so the bench still holds its credentials.

	Waits the same ``_PREFLIGHT_EXPIRY_MINUTES`` as a reset claim, which is far
	longer than the 240s teardown budget — expiring a claim whose worker is merely
	slow would let a reset start on top of a live teardown."""
	claimed_at = settings.get("workspace_reset_claimed_at")
	if claimed_at:
		deadline = frappe.utils.add_to_date(
			frappe.utils.get_datetime(claimed_at), minutes=_PREFLIGHT_EXPIRY_MINUTES
		)
		if frappe.utils.now_datetime() < deadline:
			return
	frappe.log_error(
		title="bench disconnect: claim expired without completing",
		message=f"claimed_at={claimed_at!r}; the clear did not complete, credentials survive",
	)
	_release_reset_claim(settings, "disconnect did not complete — try again")


def reconcile_pending_workspace_reset() -> None:
	"""*/5 backstop: converge a reset whose tab was closed mid-poll, and resolve a
	claim whose request worker died before it could promote or release."""
	s = frappe.get_single("Jarvis Settings")
	# A disconnect claim is not a reset and nothing converges it, but it does block
	# both entry points, so an orphan has to be given back here or nowhere.
	if (s.get("last_sync_status") or "") == _DISCONNECTING_STATUS:
		_expire_orphaned_disconnect_claim(s)
		return
	if not (s.get("last_sync_status") or "").startswith(_RESETTING_STATUS):
		return
	# A pre-flight claim is not convergeable — polling it would do nothing forever.
	# Resolve it first; if that promotes, the poll below runs on the promoted
	# marker in the same tick rather than waiting another five minutes.
	if _is_preflight(s):
		# Under the lock: every OTHER writer of this marker takes it, and this one
		# runs from the */5 cron where a request worker may be promoting or releasing
		# the same field concurrently. Non-blocking — if a request holds it, that
		# request is itself resolving the claim and this tick has nothing to add.
		from jarvis._redis_lock import redis_lock

		with redis_lock(_RESET_LOCK, timeout_s=_RESET_LOCK_TTL_S) as acquired:
			if not acquired:
				return
			s = _reread_inside_lock()
			if not _is_preflight(s):
				return
			_resolve_preflight_claim(s)
		s = frappe.get_single("Jarvis Settings")
		if not _reset_in_flight(s) or _is_preflight(s):
			return
	else:
		# A CONVERGING claim needs a deadline too (round-5 MAJOR 2). Promotion
		# proved a rebuild had started; it did not promise one would finish. A
		# request that admin abandoned, or a rebuild whose container never comes
		# back, would otherwise poll forever — the marker outliving everything that
		# could clear it, which is the plan's own invariant broken from the far end.
		#
		# Generous, because the poll converges the moment the container reports
		# Ready and this only fires when it never does: _RECONCILE_GIVE_UP_MINUTES
		# is far past any real rebuild.
		claimed_at = s.get("workspace_reset_claimed_at")
		if claimed_at:
			deadline = frappe.utils.add_to_date(
				frappe.utils.get_datetime(claimed_at), minutes=_RECONCILE_GIVE_UP_MINUTES
			)
			if frappe.utils.now_datetime() >= deadline:
				frappe.log_error(
					title="workspace reset: rebuild never converged; giving up on the marker",
					message=f"claimed_at={claimed_at!r} status={s.get('last_sync_status')!r}",
				)
				_release_reset_claim(s, "workspace reset did not finish — check your workspace, or try again")
				return
	_workspace_reset_poll()


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
	# Same reason as save_llm_pool's: the cached readiness verdict was about the
	# credential this save replaced.
	from jarvis.account import _bust_chat_gate

	_bust_chat_gate()
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
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	state, _reason = _admin_chat_readiness()
	if state != "Ready":
		return None
	# The marker follows the SYNC LEG, so this is pool mode - a BYO api-key pool
	# has no sidecar (proxy_active=0) but still stamps llm_pool_synced_at.
	_stamp_converged_ok(settings, is_pool=compute_pool_mode(settings))
	# _stamp_converged_ok's commit gate only fires in a worker/migrate context;
	# this runs in a web request, where a GET would otherwise roll the terminal
	# write back at request end.
	frappe.db.commit()
	return settings.get("last_sync_status")
