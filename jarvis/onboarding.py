"""Onboarding - store the admin token + container connection into Jarvis
Settings, and thin server wrappers the onboarding page calls (so the browser
never holds admin creds). admin_client returns already-unwrapped admin data."""

import json

import frappe
from frappe.utils import cint

from jarvis import admin_client, onboarding_contract, release_notice
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)
from jarvis.hooks import get_default_admin_url
from jarvis.permissions import grant_onboarding_admin, require_jarvis_admin

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
	# Capture the container this workspace pointed at BEFORE this write, so a
	# reconnect that repoints it can be told apart from a daily sync that rewrites
	# the same URL (which must not disturb an established claim).
	_old_agent_url = (s.get("agent_url") or "").strip()
	# Credentials just changed (fresh signup, or a reconnect rotating onto another
	# account): a bearer minted from the old ones would outlive them, and, the part
	# that used to be missing, the accepted tenant-authority (generation, handle)
	# must not outlive them either. The generation is a PER-CUSTOMER namespace
	# (admin-v2 fleet/_tenant_lookup.py: "the generation is an ASSIGNMENT generation,
	# per customer"), so a stored value left over from a PREVIOUS principal is not a
	# stale version of the new principal's count, it is a count from an unrelated
	# series - every fresh customer's first claim is generation 1, so a leftover
	# stored 1 (or higher) collides with the new customer's own first claim at the
	# SAME number but a DIFFERENT container handle. guard() cannot tell that apart
	# from the real invariant breach it exists to catch, so it HOLDS the stale
	# connection and re-polls forever: every retry carries the same new-customer
	# generation 1, which can never numerically exceed the stale leftover (jarvis
	# #693, reproduced end to end on a dev bench re-signed-up after an admin-side
	# customer purge without a matching bench-side reset). check_account_reconnect
	# and _disconnect_agent_transport already clear for exactly this reason on their
	# own paths; plain signup (and anywhere else a fresh principal's credentials
	# land here) needs the same guard, cleared BEFORE the connection block below so
	# a payload carrying both new credentials AND a connection in one call is
	# covered too.
	#
	# Deliberately narrower than the ``principal_change`` used below for the
	# cached-bearer/chat-ready resets: api_key/api_secret/customer are the strong
	# signal that this IS a different admin login, but a lone ``customer_password``
	# is not. admin re-serves that field on every poll while its 60-minute cache
	# entry lives (billing/signup.py ``_serve_signup_password``, "kept until TTL
	# rather than deleted on read"), including a poll a fully-connected bench could
	# still make within that window - and a lone customer_password never arrives
	# for a GENUINELY new principal either: every real new-principal call also
	# carries api_key/customer, which already trips this clear. Widening the
	# trigger to customer_password would drop the P0-5 protection for one window on
	# an otherwise healthy, unrelated connection for no gain.
	new_admin_login = any(data.get(k) for k in ("api_key", "api_secret", "customer"))
	if new_admin_login:
		from jarvis import tenant_authority

		tenant_authority.clear(s)
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
	# Credentials just changed (fresh signup, or a reconnect rotating onto another
	# account): a bearer minted from the old ones would outlive them. Wider than
	# ``new_admin_login`` above on purpose - a standalone customer_password (the
	# verified-poll delivery, no api_key/customer alongside it) still means the
	# bearer cache key's underlying password rotated, even though it is not by
	# itself proof of a DIFFERENT admin login.
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


def _adopt_orphan_capture(account_ref: str, consumed_capture_ids: list) -> str:
	"""Blob of this user's live capture for ``account_ref``, or "" if there isn't one.

	The client normally cites the capture by its own id. This is the fallback for
	when it cannot: ``LlmPoolEditor.load()`` blanks ``capture_id`` on every stored
	account and ``rehydratePendingCaptures`` only re-attaches an orphan one in
	singleMode, so a settings-pane reload after a failed save leaves a valid,
	unconsumed capture that no retry can ever cite.

	Scoped to the session user, mirroring ``pending_capture.list_active`` - an
	``account_ref`` from the request body must never reach another user's row.
	The claim itself goes through ``consume_capture``, so the owner gate, expiry,
	F10 anchor fence and once-only row lock all still apply; every ``CaptureError``
	returns "" and the caller refuses exactly as it did before.
	"""
	from jarvis.oauth import pending_capture

	cap_id = pending_capture.find_live_capture_id(account_ref)
	if not cap_id:
		return ""
	try:
		blob = pending_capture.consume_capture(cap_id)
	except pending_capture.CaptureAlreadyConsumed:
		# A concurrent save (a double-clicked Save, or a retry racing its own
		# in-flight request) claimed this capture between our lookup and our
		# consume. Its ciphertext is erased, so THIS save genuinely cannot
		# complete - but the account did get connected, by the winner. Falling
		# through to "no OAuth credential stored — reconnect this account" would
		# tell the customer to sign in again and mint a SECOND live provider
		# token for an account that is already linked, which is the hazard this
		# whole module exists to avoid. Say what actually happened instead.
		frappe.throw(
			"This account was just connected by another save. Reload the page - "
			"it is already linked, so there is no need to sign in again.",
			frappe.ValidationError,
		)
	except pending_capture.CaptureError:
		return ""
	consumed_capture_ids.append(cap_id)
	# Non-secret breadcrumb: an implicit adoption is worth being able to find later.
	frappe.logger("jarvis.oauth").info(f"adopted orphan capture for account_ref {account_ref}")
	return blob


@frappe.whitelist()
def save_llm_pool(
	models: str | list,
	preset: str | None = None,
	routing_mode: str = "failover",
	idempotency_key: str | None = None,
	force_probe: str | int | bool = False,
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

	``force_probe`` (jarvis_admin_v2#297): threaded straight into the synchronous
	``sync_pool_now`` call so the subscription Test button can ask admin to run a
	real probe even against a byte-identical config, instead of admin's
	byte-identical no-op path answering from the last verdict on record. Defaults
	False so every ordinary save, including "Start chatting", is unchanged: the
	admin request body gains the ``force_probe`` key ONLY when this is true, so a
	false-default call is byte-identical to the request this endpoint sent before
	the flag existed. It is not part of admin's idempotency fingerprint, so a
	repeat Test must mint a fresh ``idempotency_key`` on every press, never reuse
	one - a reused key resolves through admin's idempotent-reuse path regardless
	of this flag.

	All params MUST stay annotated: with Frappe's
	``require_type_annotated_api_methods`` enforced (declared in hooks.py),
	an un-annotated whitelisted param 500s the request before the body runs
	(JARVIS-2026-07-08 incident, fault a).

	System-Manager-gated. routing_mode is always 'failover' in v1. preset is an
	admin-catalog key or None; validated against the fetched catalog."""
	require_jarvis_admin()
	# Same coercion convention as jarvis.chat.api.set_star / set_auto_apply: a
	# whitelisted call arrives over HTTP as a string most of the time, so an
	# annotated `bool` param is trusted only after an explicit allowlist read,
	# never truthy-cast directly.
	force_probe = str(force_probe) in ("1", "true", "True", "on", "yes")
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
		_model_accounts,
		normalize_provider,
		stored_api_keys_by_provider,
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
	#
	# The api-key half is stored_api_keys_by_provider() rather than a loop local
	# to this function because jarvis.llm_key_probe's Test button now resolves a
	# stored key through the SAME helper (#679). Test has to probe the credential
	# this merge would ship, or a green Test would be attesting to a key Save
	# never sends.
	prior_api_keys = stored_api_keys_by_provider(s.get("models"))
	prior_blobs = {}
	for pm in s.get("models") or []:
		if (pm.credential_type or "api_key") != "api_key":
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
					elif ref:
						# Nothing stored either, so this account is heading straight for
						# validate_models' "no OAuth credential stored" refusal. Before
						# that, look for a live capture of THIS account the client failed
						# to cite: the editor blanks capture_id on every load and only
						# re-attaches an orphan one in singleMode, so a settings-pane
						# reload after a failed save drops it and no retry can ever
						# succeed (the capture sits valid and unconsumed server-side).
						#
						# Safe because the lookup is owner-scoped exactly like
						# list_active, and account_ref is no more privileged than
						# capture_id - both are server-minted and handed to the same
						# client in the same sign-in response, so this can only adopt a
						# capture the caller could already have cited explicitly.
						# consume_capture still applies the owner gate, the expiry, the
						# F10 anchor fence and the once-only row lock; any CaptureError
						# falls through to the same refusal as before.
						#
						# Fallback ONLY: a stored blob still wins above, so no
						# currently-succeeding save changes behaviour.
						#
						# Refusing is not the safer choice here - it forces a re-sign-in,
						# which mints a SECOND live provider token while the first stays
						# unrevoked (openai/xai/kimi have no entry in _REVOKE_ENDPOINTS),
						# the duplicate-live-token hazard this module exists to avoid.
						a["oauth_blob"] = _adopt_orphan_capture(ref, consumed_capture_ids)
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
	# Readiness-poll budget the SPA's legacy fallback should use for THIS leg, in
	# seconds. None means "use the SPA default" (75s), correct for a single-restart
	# api_key/oauth direct apply. The subscription direct leg (jarvis#715 step 3)
	# does TWO admin round-trips back to back in one sync job - _push_direct_
	# subscription_blob's doctor+restart THEN post_update_llm_creds's render+restart
	# (jarvis_settings.py:1154-1168) - so its confirmed-apply routinely outlasts 75s
	# and the wizard falsely showed "not connected, retry" mid-provision. 300s
	# matches the pool path's 5-minute operation deadline and spans one */5
	# reconcile_pending_llm_sync tick, so a stuck apply can self-heal inside the
	# poll rather than after it. Keyed on llm_auth_mode, the same field
	# _sync_via_admin branches on to cause the second restart - not re-derived.
	readiness_budget_s = None
	if compute_pool_mode(s):
		# The durable apply operation lives on the POOL path (admin creates it in
		# update_llm_pool). Push synchronously and hand its descriptor back so the
		# SPA follows ONE operation across save -> apply -> readiness.
		outcome = sync_pool_now(idempotency_key=idempotency_key or None, force_probe=force_probe)
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
		if (s.llm_auth_mode or "") == "subscription":
			# Dual-restart leg: give the fallback poll room to see the second
			# restart's confirmed apply (see readiness_budget_s comment above).
			readiness_budget_s = 300

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
		# Legacy readiness-poll budget (seconds) for the mode:"legacy" fallback; null
		# = SPA default. Set only for the dual-restart subscription direct leg.
		"readiness_budget_s": readiness_budget_s,
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

	That abort is NOT the whole story, and it cannot be. The admin call happens
	synchronously inside a whitelisted request, so the chain is bounded by
	gunicorn's ``-t`` on top of every client budget below it. A worker killed at
	that ceiling AFTER admin already committed its blanked row leaves the planes
	split the other way: admin (and the container) disconnected, this bench still
	holding live keys and still advertising a model. Widening timeouts only moves
	which layer does the killing.

	So the abort is the FAST path, not the only one. The durable half is
	``reconcile_pending_llm_sync``, which converges from admin's own state on a
	later pass and finishes the local clear through ``apply_local_disconnect``
	below. Nothing here records an intent for it to read - admin's state is the
	authority, and asking it is what makes a killed worker survivable.

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

	apply_local_disconnect(settings)
	return {"disconnected": True, "last_sync_status": "disconnected"}


def apply_local_disconnect(settings) -> None:
	"""The LOCAL half of a disconnect: destroy every stored credential, blank the
	mirrored connection fields, drop the cached readiness verdict.

	Split out of ``disconnect_llm`` because the scheduled reconcile needs the
	IDENTICAL terminal state when it finds admin already disconnected and this
	bench still holding secrets. Sharing one implementation is the point: a second,
	hand-rolled clear would drift from ``_DISCONNECTED_LLM_FIELDS`` and leave a
	half-cleared row that looks converged to everything downstream.

	Deliberately does NOT call admin. Its only two callers have already established
	that admin is done: one just got a success back, the other just read admin's
	state saying so. Re-driving the deletion from here would spend the customer's
	shared 20/hour rotate-ops bucket on a container admin's own reconcile is
	already converging.

	Idempotent: on an already-cleared tenant it deletes nothing and rewrites the
	same values.
	"""
	_clear_llm_secrets(settings)
	for field, value in _DISCONNECTED_LLM_FIELDS.items():
		settings.db_set(field, value, update_modified=False)
	frappe.db.commit()
	# There is no connection left for a cached "Ready" to be about.
	from jarvis.account import _bust_chat_gate

	_bust_chat_gate()


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
	email: str,
	company: str,
	plan: str,
	provider: str | None = None,
	billing: dict | None = None,
	partner_code: str | None = None,
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

	``partner_code`` (optional) is forwarded verbatim to admin, which resolves
	it to a Partner and attributes the customer. This bench does no validation
	of the code itself — an unknown code is admin's rejection to raise, and it
	surfaces through the normal ``_ADMIN_ERRORS`` → ``_throw_admin_error`` path
	below like any other signup error.

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
		data = admin_client.signup(
			email, company, plan, provider=provider, billing=billing, partner_code=partner_code
		)
	except _ADMIN_ERRORS as e:
		resumed = _try_resume_pending_signup(e, email, plan, provider, billing, partner_code)
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
	err,
	email: str,
	plan: str,
	provider: str | None,
	billing: dict | None = None,
	partner_code: str | None = None,
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
			partner_code=partner_code,
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
	Same System-Manager gating as the rest of onboarding.

	``company_account_exists`` is the separate, weaker answer to a separate
	question (admin-v2 #166): somebody ELSE at this company already has an account.
	It is not a reconnect offer and must not be rendered as one - this caller
	cannot recover a colleague's account, and admin will not tell them whose it is.
	False on an older admin that does not send the key, which is the safe default.
	"""
	require_jarvis_admin()
	blank = {"eligible": False, "needs_company": False, "company_account_exists": False}
	try:
		_require_admin_url()
		d = admin_client.reconnect_eligibility(email, company) or {}
	except Exception:
		return blank
	return {
		"eligible": bool(d.get("eligible")),
		"needs_company": bool(d.get("needs_company")),
		"company_account_exists": bool(d.get("company_account_exists")),
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
	"""Redeem the reconnect code the customer received BY EMAIL (the customer-
	started request path). Only a correct code releases anything: admin rotates and
	delivers the credentials, which _land_reconnect persists — see it for the
	connected-vs-resume_payment outcomes."""
	require_jarvis_admin()
	data = _surface(admin_client.get_reconnect_state, request_id, code) or {}
	return _land_reconnect(data)


@frappe.whitelist()
def redeem_reconnect_code(code: str, email: str = "") -> dict:
	"""Redeem an OPERATOR-ISSUED reconnect code on this fresh bench — the
	request-less counterpart to check_account_reconnect. The customer got the code
	from support (out of band) and enters it PLUS their registered email; admin
	verifies both, rotates, and returns the SAME ready bundle, which lands
	identically (_land_reconnect). No prior customer-started request exists, so
	there is no request_id. Same require_jarvis_admin gate: the customer is admin on
	their own new bench. A wrong/expired/unmatched code comes back ``invalid``."""
	require_jarvis_admin()
	data = _surface(admin_client.redeem_reconnect_code, code, email) or {}
	return _land_reconnect(data)


def _land_reconnect(data: dict) -> dict:
	"""Persist a reconnect ready-bundle and grant the onboarding admin role, exactly
	like a fresh signup would. Shared by the email/poll path
	(check_account_reconnect) and the operator-code path (redeem_reconnect_code):
	both receive the SAME ready bundle from admin, so landing MUST be identical or
	the two paths would diverge in what a reconnected site ends up holding.

	Two ready outcomes (admin-v2 #162): ``connected`` — a PAID account whose
	container is already running (ride sync_connection; only the LLM step re-does on
	this fresh site); ``resume_payment`` — an UNFINISHED checkout with no container,
	so the wizard goes back to Pay. Anything not ``ready`` (pending / awaiting_code /
	expired / invalid) is surfaced verbatim for the caller to render."""
	data = data or {}
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
	if (data.get("subscription_status") or "").strip() == "Pending Payment":
		return {"status": "resume_payment"}
	return {"status": "connected"}


# RFC 2606 / 6761 reserved names. Nothing here can receive mail, so none of it is
# a billing address: Frappe ships Administrator as admin@example.com and Guest as
# guest@example.com, and admin_v2 mints synthetic customer logins at @jarvis.invalid.
#
# Each entry carries a leading dot, and the candidate domain gets one too, so a
# single suffix test covers both the name itself and any subdomain of it -- with
# no way to match a longer name that merely ends in the same letters
# (".examplex.com" does not end with ".example.com").
_UNDELIVERABLE_SUFFIXES = (
	".example.com",
	".example.net",
	".example.org",
	".localhost",
	".test",
	".example",
	".invalid",
)


def _is_undeliverable(email: str) -> bool:
	"""Whether ``email``'s domain is reserved, so mail to it can never arrive."""
	# strip("."): "example.com." is the root-anchored form of the same name, and
	# would otherwise slip through -- ".example.com." does not end with
	# ".example.com".
	domain = email.rpartition("@")[2].strip().strip(".").lower()
	return bool(domain) and f".{domain}".endswith(_UNDELIVERABLE_SUFFIXES)


def _installed_apps() -> set[str]:
	"""The site's installed apps (test seam, patch HERE rather than
	``frappe.get_installed_apps`` directly, same convention as
	``jarvis.chat.agent_installability.installed_apps`` and
	``jarvis.site_profile.apps._installed_apps``). Fails toward the empty set so
	a lookup hiccup falls back to the free text Company field instead of
	silently claiming ERPNext is installed."""
	try:
		return set(frappe.get_installed_apps())
	except Exception:
		frappe.log_error(title="jarvis onboarding: get_installed_apps failed")
		return set()


@frappe.whitelist()
def get_account_defaults() -> dict:
	"""Prefill for the onboarding Account step so the customer doesn't retype what
	the site already knows: the caller's email + a default company. Company is the
	user/global default when set, else the site's sole Company; ``companies`` lists
	options for a client datalist when several exist. Silent no-op (blank / empty
	list) on sites without the Company doctype or read permission.

	``erpnext_installed`` tells the client whether Company should be a constrained
	picker (real Company records exist to pick from) or stay free text (no ERPNext
	on the site, so there is nothing to pick from).

	A reserved-domain email is dropped rather than sent. On a fresh site the caller
	is Administrator, i.e. admin@example.com, and the step it fills says receipts go
	to that address — so prefilling it puts an undeliverable address in the field as
	a real, submittable value. Blank lets the field's own placeholder show.

	Ports the desk auto-fetch (jarvis_onboarding.js, commit 1507495) to the server
	because the SPA has no ``frappe.defaults``. System-Manager only (the onboarding
	route is SM-gated).
	"""
	require_jarvis_admin()
	user = frappe.session.user
	email = (frappe.db.get_value("User", user, "email") or user) if user and user != "Guest" else ""
	if _is_undeliverable(email):
		email = ""

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
	return {
		"email": email,
		"company": company,
		"companies": companies,
		# Lets the client decide whether Company is a constrained picker (ERPNext
		# present, real Company records exist) or a free text field (no ERPNext,
		# or ERPNext installed with zero Company rows so onboarding never dead
		# ends on an empty picker).
		"erpnext_installed": "erpnext" in _installed_apps(),
	}


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
def request_workspace_reset(reason: str = "", wipe_data: bool = False, revoke_llm: bool = False) -> dict:
	"""Self-serve workspace reset: the control plane rebuilds the container NOW
	(subscription kept), then this site disconnects its agent transport and polls
	``workspace_reset_state`` back to Ready.

	``wipe_data`` also deletes workspace content (chats, skills, macros,
	triggers, learning artifacts, wiki, dashboards). ``revoke_llm`` also clears
	every LLM connection (pool models, subscription accounts, keys, synced
	markers) so the customer sets up their AI model fresh after the reset.
	Both run only AFTER the control plane accepted the reset.

	Gated on System Manager, like the rest of onboarding."""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	# Tear down the container-side OAuth auth-profile while the old container is
	# still reachable (same ordering as dev.reset_onboarding). Best-effort — a
	# dead container is often the very reason for the reset.
	if settings.get("agent_url"):
		try:
			admin_client.post_subscription_disconnect()
		except Exception:
			pass
	out = _surface(admin_client.reset_workspace, reason)
	if cint(wipe_data):
		_wipe_workspace_content()
	if cint(revoke_llm):
		_revoke_llm_connections(settings)
	_disconnect_agent_transport(settings, reconnect_llm=bool(cint(revoke_llm)))
	return out


@frappe.whitelist()
def reset_onboarding(wipe_data: bool = True) -> dict:
	"""Customer-facing "Reset onboarding" (Jarvis Settings button): clear this
	bench's connection + LLM credentials and — by default, matching the
	``bench reset-onboarding`` CLI — delete all workspace content, so the setup
	wizard runs from step 1.

	Thin whitelisted wrapper over :func:`jarvis.dev.reset_onboarding`, which does
	the teardown (container OAuth auth-profile + chat-device unpair, both
	best-effort; subscription/billing untouched). Distinct from
	:func:`request_workspace_reset`, which rebuilds the container via admin.

	System Manager only — the same gate dev.reset_onboarding enforces, stated here
	so the endpoint 403s before importing rather than deep inside the teardown."""
	frappe.only_for("System Manager")
	from jarvis import dev

	return dev.reset_onboarding(wipe_data=bool(cint(wipe_data)))


def _wipe_workspace_content() -> None:
	for dt in _WIPE_DOCTYPES:
		frappe.db.delete(dt)


def _revoke_llm_connections(settings) -> None:
	"""Clear the LLM subset — direct creds, OAuth state, the models[] pool and the
	synced markers — so is_ready_for_chat routes to the LLM setup step. Admin creds
	untouched. Shares its field list with the reset-onboarding CLI."""
	from jarvis import settings_reset

	settings_reset.apply(settings, settings_reset.LLM)


def _disconnect_agent_transport(settings, reconnect_llm: bool = False) -> None:
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
	settings.db_set(
		"last_sync_status", _RESETTING_RECONNECT_LLM_STATUS if reconnect_llm else _RESETTING_STATUS
	)
	_bust_chat_gate()
	frappe.db.commit()


@frappe.whitelist()
def workspace_reset_state() -> dict:
	"""Poll endpoint for the reset card: admin request state + serving readiness.
	When admin reports the new container Ready, persists the fresh connection and
	clears the resetting marker — chat works again with no manual steps."""
	require_jarvis_admin()
	return _workspace_reset_poll()


def _workspace_reset_poll() -> dict:
	settings = frappe.get_single("Jarvis Settings")
	req: dict = {}
	try:
		req = admin_client.reset_workspace_state() or {}
	except Exception:
		pass  # audit-row state is advisory; readiness below is the real signal

	def _resetting() -> bool:
		return (settings.get("last_sync_status") or "").startswith(_RESETTING_STATUS)

	def _reconnect_llm() -> bool:
		return (settings.get("last_sync_status") or "").startswith(_RESETTING_RECONNECT_LLM_STATUS)

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
	if ready and _resetting():
		from jarvis.account import _bust_chat_gate

		write_connection(data)
		settings.db_set("last_sync_status", "ok (workspace reset)")
		_bust_chat_gate()
		frappe.db.commit()
	elif _resetting() and data.get("agent_url") and not (settings.get("agent_url") or ""):
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
	}


def reconcile_pending_workspace_reset() -> None:
	"""*/5 backstop: converge a reset whose tab was closed mid-poll."""
	s = frappe.get_single("Jarvis Settings")
	if not (s.get("last_sync_status") or "").startswith(_RESETTING_STATUS):
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
	    the container so agent picks up the new auth profile.
	Without ``force=True``, a customer re-authorizing with the same
	provider+model gets a stale openclaw.json + no restart, and agent
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

	return _sync_status_payload(s, status)


def _sync_status_payload(s, status: str) -> dict:
	"""Project Jarvis Settings into the poller's response shape. PURE: it reads and
	formats, and unlike ``get_llm_sync_status`` it never probes admin and never
	writes.

	Split out so a caller that has already decided what the workspace's state is can
	report it without the lazy reconcile running underneath and committing a
	different answer (#713 review). ``resync_llm``'s not-configured branch is the
	case that needs it: it has just established there is nothing to re-drive, so a
	status projection that could stamp "ok (converged ...)" on the way out would
	contradict the very answer it is returning."""

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


#: Minimum gap between two Resync PUSHES. Sized to outlast a normal apply so the
#: second click of an impatient pair is throttled rather than doubling the work,
#: and to stay well inside the customer's shared 20/hour rotate-ops budget even if
#: someone clicks steadily for an hour.
_RESYNC_COOLDOWN_S = 180
_RESYNC_COOLDOWN_KEY = "jarvis:resync_llm_cooldown"


@frappe.whitelist()
def resync_llm() -> dict:
	"""Re-drive this workspace's saved AI configuration through admin. The customer
	half of #713: a way forward from a sync that failed.

	It exists because a failed sync used to be a dead end with no lever anywhere.
	Nothing retries on its own once the workspace stops looking outstanding:
	``on_update`` syncs only what CHANGED, so re-saving the same config is a no-op;
	``reconcile_pending_llm_sync`` acts on a pending-applying marker or on a first
	apply that never proved itself, and a workspace that HAS a proven apply (the
	shape in #713: llm_direct_synced_at already set from an earlier good sync)
	matches no branch it has. The status said "Last sync failed" and nothing in the
	product was ever going to try again.

	PROBES BEFORE IT PUSHES. Most of what leaves a workspace on a failed status is
	bookkeeping that lost a race, not a container that missed its config, so the
	first thing this does is ask admin whether the container is already serving what
	was saved (chat_readiness == Ready, the same receipt every other convergence
	path trusts). If it is, this stamps the success markers and returns: no restart,
	no re-render, no interruption to a workspace that was working the whole time.
	The push is only for a workspace admin says is NOT converged.

	Gated on ``require_jarvis_admin`` (it drives a container mutation, like every
	other write on this module).

	THROTTLED, because "click it again" is what a customer does to a status that has
	not moved. Frappe's ``deduplicate=True`` only collapses a second click while the
	first job is still queued or running; once a worker exits without converging -
	routine, since the in-job poll gives up after ``_POOL_CONVERGE_DEADLINE_S`` - the
	job id frees and the next click drives a WHOLE new push: another container
	re-render and another unit of the customer's shared 20/hour rotate-ops budget,
	until admin starts refusing with a rate-limit the SPA renders as a failure. So a
	push is allowed at most once per ``_RESYNC_COOLDOWN_S``; inside that window the
	call reports ``throttled`` with the live status and queues nothing. The Ready
	probe is NOT throttled - it neither pushes nor restarts, and answering "your
	config is already live" instantly is the best outcome a Resync click can have.

	Returns ``get_llm_sync_status()``'s shape, so a caller can render the response
	directly with no second round trip, plus ``outcome``:

	  * ``converged``      - admin already serves this config; markers stamped.
	  * ``queued``         - a re-push was enqueued; ``leg`` is "pool" or "direct".
	  * ``throttled``      - a re-push went out moments ago; nothing was queued.
	  * ``not_configured`` - nothing saved to re-drive; nothing was queued.
	"""
	from jarvis.account import _has_llm_config
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		_admin_chat_readiness,
		_stamp_converged_ok,
		request_resync,
	)
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	# Both halves matter: a credential with no control-plane tenancy has nowhere to
	# be pushed, and a tenancy with no credential has nothing to push. Projected
	# WITHOUT get_llm_sync_status's lazy reconcile, which could otherwise stamp an
	# "ok (converged ...)" on its way out and contradict this very answer.
	if not _has_llm_config(settings) or not _has_admin_credentials(settings):
		status = settings.get("last_sync_status") or ""
		return {**_sync_status_payload(settings, status), "outcome": "not_configured", "leg": ""}

	state, _reason = _admin_chat_readiness()
	if state == "Ready":
		# READY MEANS NEVER PUSH, whether or not our own stamp lands. Making the push
		# conditional on the stamp succeeding would restart a healthy container in
		# precisely the situation this endpoint exists to handle gently: five writers
		# race to record this same Ready, so losing that race is ordinary, and it
		# means SOMEONE recorded it. The status below carries whatever landed.
		if _stamp_converged_ok(settings, is_pool=compute_pool_mode(settings)):
			# The stamp's own commit gate only fires in a worker; this is a request.
			frappe.db.commit()
		return {**get_llm_sync_status(), "outcome": "converged", "leg": ""}

	cache = frappe.cache()
	if cache.get_value(_RESYNC_COOLDOWN_KEY):
		return {**get_llm_sync_status(), "outcome": "throttled", "leg": ""}

	# Armed only once a push is actually queued. Arming it first would let a call
	# that queued NOTHING still lock the customer out for three minutes - the same
	# "click it again and nothing happens" dead end this endpoint exists to remove.
	# Fails open when redis is down (RedisWrapper swallows connection errors): that
	# loses the throttle, never the retry, and admin's own rate limiter is the
	# backstop that actually protects the rotate-ops budget.
	leg = request_resync(settings)
	cache.set_value(_RESYNC_COOLDOWN_KEY, 1, expires_in_sec=_RESYNC_COOLDOWN_S)
	return {**get_llm_sync_status(), "outcome": "queued", "leg": leg}


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

	Best-effort: any admin error / non-Ready verdict / lost write-conflict race
	leaves the pending status untouched; the next poll retries. Never raises out of
	the poller.

	This poll is one of the FOUR writers that race on the converged-ok markers
	(#713), and the one that runs most often - every few seconds while a workspace
	is pending-applying, from the SPA. It is also the fastest way back: the moment a
	sync worker records the pending marker, this poller is what converges it,
	seconds later, without waiting for the */5 reconcile."""
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
	if not _stamp_converged_ok(settings, is_pool=compute_pool_mode(settings)):
		# Whoever won the race wrote the same outcome, so there is nothing to
		# correct and nothing to tell the customer: stay pending and let the next
		# poll read the winner's value.
		return None
	# _stamp_converged_ok's commit gate only fires in a worker/migrate context;
	# this runs in a web request, where a GET would otherwise roll the terminal
	# write back at request end.
	frappe.db.commit()
	return settings.get("last_sync_status")
