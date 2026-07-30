"""REV-3 OAuth endpoints. Bench drives the OAuth flow end-to-end;
customer's browser is the only "laptop-side" actor (no helper script).

Two whitelisted endpoints:
  - begin_paste_signin(provider, model) → {nonce, authorize_url}
  - complete_paste_signin(nonce, redirected_url) → {account_email, sync_status}

Plus disconnect() to reverse the connection.
"""

import base64
import hashlib
import json
import re
import secrets
import time

import frappe
import requests

from jarvis import admin_client, onboarding
from jarvis.exceptions import JarvisError
from jarvis.oauth.providers import (
	UnknownProviderError,
	accepts_bare_code,
	build_authorize_url,
	extract_account_id,
	get_provider,
	is_oauth_provider,
	provider_redirect_uri,
)
from jarvis.permissions import require_jarvis_admin


class TokenExchangeError(JarvisError):
	"""Provider's /oauth/token endpoint rejected the code or had an error.

	The ``code`` attribute is one of the opaque codes from
	``_TOKEN_EXCHANGE_OPAQUE_CODES`` below; the message is safe to surface
	to the customer. The full provider response detail is logged via
	``frappe.log_error`` server-side at raise time so ops can triage.
	"""

	def __init__(self, message: str, *, code: str = "token_exchange_failed"):
		super().__init__(message)
		self.code = code


# Provider error_description → opaque code mapping.
# Provider responses can leak implementation detail; in particular,
# ``invalid_client`` distinguishes "client_secret needed" from
# "client_secret wrong", an oracle on gemini-cli's confidential-client
# secret. We collapse the provider's distinction into opaque buckets.
# Sprint-1 Important #6 from the 2026-06-16 code review.
_TOKEN_EXCHANGE_OPAQUE_CODES = {
	"invalid_grant": (
		"code_invalid",
		"The authorization code was rejected. Start a fresh sign-in.",
	),
	"invalid_client": (
		"auth_failed",
		"The provider rejected this sign-in. If this keeps happening, contact support.",
	),
	"invalid_request": (
		"auth_failed",
		"The provider rejected this sign-in. If this keeps happening, contact support.",
	),
}


_CACHE_KEY = "jarvis.oauth.codex_signin"
_NONCE_TTL_SECS = 600
# Cap on how many cache fields the GC sweep visits per begin_paste_signin.
# A misbehaving caller looping begin without complete could otherwise fill
# the hash and make every subsequent begin pay an O(N) Redis trip. The cap
# bounds the sweep cost; truly stale entries get cleaned next time.
_GC_SWEEP_LIMIT = 256
_HTTP_TIMEOUT = 30
_REDIRECT_URI = "http://localhost:1455/auth/callback"
# Codex/gemini-cli's CLI-specific model IDs (not OpenAI/Google's standard
# API model names). Catalogue lives in jarvis/_subscription_models.py and
# is imported here so the chat-tier and oauth-tier validators agree on
# the same set of accepted models (previously this module declared sets
# and chat/api.py declared lists - 2026-06-16 punch-list drift item).
#
# Catalog sync constraints: these values must match agent 2026.6.4's
# bundled codex catalog (the version pinned by jarvis_admin.host_setup.
# DEFAULT_AGENT_IMAGE). The script at
# jarvis-fleet-agent/scripts/verify-agent-assumptions.sh asserts at
# image-bump time that the catalog still contains "gpt-5.5"; if it ever
# fails because the catalog drifted, update jarvis/_subscription_models.py
# + the JS mirrors atomically and re-run the script before bumping the
# image pin.
from jarvis._subscription_models import DEFAULT_MODEL as _DEFAULT_MODEL
from jarvis._subscription_models import SUBSCRIPTION_MODELS as _SUBSCRIPTION_MODELS


def _coerce_subscription_model(provider: str, model: str) -> str:
	"""Return ``model`` if valid for ``provider``'s subscription mode,
	else fall back to ``_DEFAULT_MODEL[provider]``. Empty string for an
	unknown provider (begin_paste_signin already rejects those upstream)."""
	valid = _SUBSCRIPTION_MODELS.get(provider, [])
	if model and model in valid:
		return model
	return _DEFAULT_MODEL.get(provider, "")


# _ok / _err live in jarvis/_responses.py - single source of truth for the
# customer-facing envelope shape, shared with jarvis/api.py and any future
# bench endpoint. Punch-list item from the 2026-06-16 review.
from jarvis._responses import err as _err
from jarvis._responses import ok as _ok


def _generate_pkce() -> tuple[str, str]:
	"""Return (verifier, challenge) per RFC 7636."""
	verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
	return verifier, challenge


def _gc_expired_nonces() -> None:
	"""Sweep ``_CACHE_KEY`` for entries past their ``expires_at_ts`` and drop
	them.

	The OAuth nonce cache is a Redis hash where each field is a per-flow
	nonce. ``frappe.cache.hset`` doesn't honour per-field TTLs (Redis HSET
	can't), so abandoned sign-ins (customer started the flow, closed the
	tab, never pasted the URL) leave their PKCE verifier + state hanging
	in the hash until the whole key gets wiped. Punch-list "stale PKCE
	verifiers + unconsumed nonces never GC'd" from the 2026-06-16 review.

	Called opportunistically from begin_paste_signin so the hash stays
	bounded without a separate scheduled job. Cost is one HGETALL per
	begin (~ms-cheap until N gets large; the sweep limit caps the
	worst case).
	"""
	try:
		entries = frappe.cache.hgetall(_CACHE_KEY) or {}
	except Exception:
		# Best-effort: a Redis hiccup mustn't block a fresh sign-in.
		return
	now_ts = int(time.time())
	for i, (field, value) in enumerate(entries.items()):
		if i >= _GC_SWEEP_LIMIT:
			break
		# hgetall on a freshly-wiped cache can return {} - any field shape
		# that doesn't carry expires_at_ts is something we didn't write
		# and shouldn't try to interpret.
		try:
			expires_at_ts = (value or {}).get("expires_at_ts")
		except AttributeError:
			continue
		if not expires_at_ts or expires_at_ts < now_ts:
			# Decode bytes-vs-str defensively - Frappe's redis hash returns
			# bytes on some configs and str on others.
			key = field.decode() if isinstance(field, bytes) else field
			frappe.cache.hdel(_CACHE_KEY, key)


def _begin_signin(provider: str, model: str, *, pool: bool) -> dict:
	"""Shared nonce/PKCE/state machinery behind ``begin_paste_signin`` (the
	DIRECT single-model flow) and ``begin_pool_account_signin`` (the POOL
	multi-account capture flow).

	Mints a nonce + PKCE pair, caches the verifier/state bound to
	``frappe.session.user``, and returns the authorize URL. ``pool`` tags
	the cached entry so ``complete_pool_account_signin`` can tell a POOL
	nonce apart from a DIRECT one. The caller is responsible for the
	System-Manager gate.
	"""
	try:
		p = get_provider(provider)
	except UnknownProviderError as e:
		return _err("unknown_provider", str(e))
	if p.get("grant_type") == "device_code":
		# Device-code providers (Kimi) have no authorize URL — build_authorize_url
		# would KeyError on p["authorize"]. They are captured via
		# begin_pool_account_signin -> _begin_device_signin, never the paste-back
		# path. Fail clean rather than 500 if this is reached (e.g. a DIRECT
		# re-authorize for a stored device-code provider).
		return _err(
			"device_flow_required",
			"This provider uses a device-code sign-in and can't be connected this way.",
		)

	# GC abandoned sign-ins BEFORE writing the new nonce. Otherwise a
	# user who loops begin without ever completing (e.g. wizard reload
	# while debugging) grows the cache hash unboundedly.
	_gc_expired_nonces()

	nonce = secrets.token_hex(24)
	verifier, challenge = _generate_pkce()
	state = secrets.token_urlsafe(16)
	# xAI's authorize endpoint requires a fresh nonce alongside state; other
	# providers ignore it (build_authorize_url only emits it when requires_nonce).
	oidc_nonce = secrets.token_urlsafe(16)
	# Per-provider redirect_uri (xAI pins cli-proxy-api's exact loopback
	# callback). Cached so _exchange_code re-sends the SAME value.
	redirect_uri = provider_redirect_uri(provider, _REDIRECT_URI)

	authorize_url = build_authorize_url(
		provider=provider,
		redirect_uri=redirect_uri,
		code_challenge=challenge,
		state=state,
		# Pool accounts feed cli-proxy-api, which needs the codex-scope
		# token audience; the direct flow keeps the connectors scope for
		# agent's own codex path. See providers.py pool_scope.
		pool=pool,
		oidc_nonce=oidc_nonce,
	)

	entry = {
		"provider": provider,
		"model": _coerce_subscription_model(provider, model),
		"status": "pending",
		"expires_at_ts": int(time.time()) + _NONCE_TTL_SECS,
		"verifier": verifier,
		"state": state,
		"redirect_uri": redirect_uri,
		"originator_user": frappe.session.user,
	}
	if pool:
		entry["pool"] = True
	frappe.cache.hset(_CACHE_KEY, nonce, entry)

	return _ok(
		{
			"nonce": nonce,
			"authorize_url": authorize_url,
			"expires_in": _NONCE_TTL_SECS,
		}
	)


@frappe.whitelist()
def begin_paste_signin(provider: str, model: str) -> dict:
	"""Mint a nonce + PKCE pair, return the authorize URL for the customer
	to open in their browser.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16 code
	review). The cached nonce is bound to ``frappe.session.user`` so
	another logged-in System Manager can't complete an in-flight sign-in
	that someone else started.
	"""
	require_jarvis_admin()
	return _begin_signin(provider, model, pool=False)


def _begin_device_signin(provider: str, model: str) -> dict:
	"""Device-code (RFC 8628) capture start for a device-flow provider (Kimi).

	Requests a device_code + user_code from the provider's
	device_authorization endpoint, caches the device_code bound to the user
	(tagged pool+device), and returns the user_code + verification_uri for the
	frontend to display. There is NO authorize URL / redirect / paste — the user
	approves out-of-band and the frontend polls ``poll_pool_account_signin``.
	"""
	p = get_provider(provider)
	_gc_expired_nonces()
	device_id = secrets.token_hex(16)
	headers = dict(p.get("device_headers") or {})
	headers["X-Msh-Device-Id"] = device_id
	try:
		resp = requests.post(
			p["device_authorization"],
			data={"client_id": p["client_id"]},
			headers=headers,
			timeout=_HTTP_TIMEOUT,
		)
	except requests.RequestException as e:
		frappe.log_error(
			title="oauth device authorization: network error", message=f"provider={provider!r} error={e!r}"
		)
		return _err("network_error", "Couldn't reach the sign-in provider. Try again in a minute.")
	if not resp.ok:
		frappe.log_error(
			title="oauth device authorization: provider rejected",
			message=f"provider={provider!r} status={resp.status_code} detail={resp.text!r}",
		)
		return _err("device_start_failed", "Couldn't start sign-in with the provider. Try again.")
	try:
		body = resp.json()
	except ValueError:
		return _err("device_start_failed", "The provider returned an unexpected response.")

	device_code = body.get("device_code")
	user_code = body.get("user_code")
	verification_uri = body.get("verification_uri") or body.get("verification_url")
	if not device_code or not user_code:
		return _err("device_start_failed", "The provider returned an incomplete device response.")
	interval = int(body.get("interval") or p.get("poll_interval_s") or 5)
	# Device flow's own expiry (typically <=15 min) but never past our nonce TTL.
	expires_in = min(int(body.get("expires_in") or _NONCE_TTL_SECS), _NONCE_TTL_SECS)

	nonce = secrets.token_hex(24)
	entry = {
		"provider": provider,
		"model": _coerce_subscription_model(provider, model),
		"status": "pending",
		"expires_at_ts": int(time.time()) + expires_in,
		"device_code": device_code,
		"device_id": device_id,
		"interval": interval,
		"pool": True,
		"device": True,
		"originator_user": frappe.session.user,
	}
	frappe.cache.hset(_CACHE_KEY, nonce, entry)
	return _ok(
		{
			"nonce": nonce,
			"device_flow": True,
			"user_code": user_code,
			"verification_uri": verification_uri,
			"verification_uri_complete": body.get("verification_uri_complete") or verification_uri,
			"interval": interval,
			"expires_in": expires_in,
		}
	)


@frappe.whitelist()
def begin_pool_account_signin(provider: str, model: str) -> dict:
	"""POOL variant of ``begin_paste_signin``: start capturing ONE more account
	into a pool "subscription" model.

	Routes on the provider's grant type: paste-back providers (OpenAI, Google,
	xAI) mint a nonce + PKCE pair via the shared ``_begin_signin`` machinery;
	device-code providers (Kimi) go through ``_begin_device_signin`` (no
	authorize URL — the frontend shows a code + link and polls
	``poll_pool_account_signin``). Either way the cached entry is tagged ``pool``
	and this endpoint captures a blob for the caller to stash, rather than
	writing creds to Jarvis Settings / pushing to the container.

	Gated on System Manager + per-user nonce binding.
	"""
	require_jarvis_admin()
	try:
		p = get_provider(provider)
	except UnknownProviderError as e:
		return _err("unknown_provider", str(e))
	if p.get("grant_type") == "device_code":
		return _begin_device_signin(provider, model)
	return _begin_signin(provider, model, pool=True)


from urllib.parse import parse_qs, urlparse

# A bare authorization code: no URL syntax ("://", leading "/" or "?"), no
# pair separator ("&"), no whitespace, printable ASCII only. The length floor
# rejects stray keystrokes; the ceiling keeps a pasted essay out of the token
# request.
#
# "=" is deliberately NOT disqualifying. A base64/JWT-shaped code can carry "="
# as padding, and excluding it would re-break the very case this exists to
# handle. Query pastes are ruled out by _OAUTH_QUERY_KEYS below instead, which
# keys off what the string actually *parses to* rather than one character.
_BARE_CODE_RE = re.compile(r"\A[\x21-\x7e]{8,512}\Z")

# Params that mark a paste as an OAuth query/callback rather than a bare code.
# If one of these parsed out but `code` did not, the customer pasted a real
# (but code-less) callback, so this is a missing_code, not a code to redeem.
_OAUTH_QUERY_KEYS = ("state", "error", "error_description", "error_uri", "session_state", "iss", "scope")


def _looks_like_bare_code(raw: str) -> bool:
	if "://" in raw or raw.startswith(("/", "?")) or "&" in raw:
		return False
	return bool(_BARE_CODE_RE.match(raw))


def _parse_redirected_url(raw: str, *, allow_bare_code: bool = False) -> dict:
	"""Defensively parse whatever the customer pasted back.

	Accepts:
	  - http://localhost:1455/auth/callback?code=A&state=B
	  - ?code=A&state=B
	  - code=A&state=B
	  - A                     (bare code; only when allow_bare_code)

	``allow_bare_code`` is set from the provider's ``code_only_paste`` flag —
	xAI shows a bare code instead of redirecting to a callback URL. A bare code
	has no ``state``, so ``bare`` is returned True to tell the caller the state
	compare has nothing to compare against (PKCE binds the code instead; see
	providers.py's ``code_only_paste`` comment).

	Returns: {"code": str|None, "state": str|None, "bare": bool}
	"""
	raw = (raw or "").strip()
	if not raw:
		return {"code": None, "state": None, "bare": False}

	if "://" in raw or raw.startswith("/"):
		query = urlparse(raw).query
	elif raw.startswith("?"):
		query = raw[1:]
	else:
		query = raw

	q = parse_qs(query)
	code = (q.get("code") or [None])[0]
	if code:
		return {"code": code, "state": (q.get("state") or [None])[0], "bare": False}

	# No `code=`. If the paste still parsed to recognisable OAuth params, it was
	# a genuine callback that simply lacks a code (e.g. "state=..." on its own,
	# or an ?error= bounce) - that is a missing_code, never a code to redeem.
	if any(k in q for k in _OAUTH_QUERY_KEYS):
		return {"code": None, "state": None, "bare": False}

	# Otherwise, for a code-only provider the paste may be the code itself. A
	# base64-padded code parses to junk like {"AbC123": ["="]}, which matches no
	# OAuth key, so it correctly falls through to here.
	if allow_bare_code and _looks_like_bare_code(raw):
		return {"code": raw, "state": None, "bare": True}
	return {"code": None, "state": None, "bare": False}


def _validate_signin_nonce(nonce: str):
	"""Validate a cached sign-in nonce shared by the DIRECT and POOL complete
	endpoints: existence, expiry (evicts on read), pending status, and the
	per-user binding.

	Returns ``(entry, None)`` on success or ``(None, err_envelope)`` on any
	failure. The error message for a user-binding mismatch is deliberately
	the same as "unknown_nonce" so live nonces aren't leaked.
	"""
	entry = frappe.cache.hget(_CACHE_KEY, nonce)
	if not entry:
		return None, _err("unknown_nonce", "nonce not recognized")
	if entry["expires_at_ts"] < int(time.time()):
		# Drop the expired field so the hash doesn't grow with dead
		# entries waiting on the periodic GC sweep. Companion to the
		# _gc_expired_nonces sweep on begin.
		frappe.cache.hdel(_CACHE_KEY, nonce)
		return None, _err("expired", "nonce has expired; generate a new sign-in URL")
	if entry["status"] != "pending":
		return None, _err("not_pending", f"nonce status is {entry['status']!r}")
	# Per-user binding: the user who began the sign-in must be the same
	# one completing it. Without this, a second System Manager on the same
	# site could complete a peer's pending OAuth with a redirect they
	# control. The error message is the same as "unknown_nonce" on purpose
	# (don't leak which nonces are live).
	if entry.get("originator_user") != frappe.session.user:
		return None, _err("unknown_nonce", "nonce not recognized")
	return entry, None


def _exchange_and_build_blob(entry: dict, redirected_url: str):
	"""Parse the pasted redirect, verify state (constant-time), exchange the
	code, and build the agent auth-profile blob.

	Shared by the DIRECT (``complete_paste_signin``) and POOL
	(``complete_pool_account_signin``) flows so both build an identically
	shaped blob. Returns ``(result, None)`` on success where ``result`` is
	``{"provider", "model", "email", "blob"}``, or ``(None, err_envelope)``
	on any validation / token-exchange failure. Does NOT push the blob,
	save creds, or touch Jarvis Settings — those side effects belong to the
	DIRECT caller only.
	"""
	provider = entry["provider"]
	bare_ok = accepts_bare_code(provider)
	parsed = _parse_redirected_url(redirected_url, allow_bare_code=bare_ok)
	if not parsed["code"]:
		return None, _err(
			"missing_code",
			"couldn't find an authorization code in what you pasted; paste the code "
			"itself or the full callback URL"
			if bare_ok
			else "no `code` parameter found in the pasted URL",
		)
	# Constant-time compare on the state parameter. The state value is the
	# OAuth CSRF nonce - if an attacker can observe how long the
	# comparison takes, plain ``!=`` short-circuits on the first
	# differing byte and leaks a prefix-recovery oracle. secrets.compare_digest
	# runs in constant time over the longer of the two inputs.
	# Punch-list "state comparison non-constant-time" from the 2026-06-16 review.
	#
	# Skipped only for a BARE code from a code_only_paste provider (xAI), which
	# has no state to echo back. The exchange below still sends this nonce's
	# `code_verifier`, so PKCE keeps the code bound to this very authorize
	# request. A pasted URL is still state-checked even for those providers:
	# if a `code=` param came through, a `state=` one should have too.
	if not parsed["bare"] and not secrets.compare_digest(parsed["state"] or "", entry["state"] or ""):
		return None, _err(
			"state_mismatch", "the `state` parameter doesn't match; regenerate the sign-in URL and try again"
		)

	# Re-coerce belt-and-suspenders: nonces live up to 10 min, so
	# _SUBSCRIPTION_MODELS could in principle be tightened mid-flight
	# (e.g. a codex model deprecated). begin_paste_signin already coerced
	# at cache time; doing it again here means the cached model can never
	# escape the codex-valid set even across config reloads.
	model = _coerce_subscription_model(provider, entry["model"])

	try:
		tokens = _exchange_code(
			provider=provider,
			code=parsed["code"],
			code_verifier=entry["verifier"],
			redirect_uri=entry.get("redirect_uri") or _REDIRECT_URI,
		)
	except TokenExchangeError as e:
		# Nonce NOT cleared; customer can paste again if they fix the URL.
		# `e.code` is one of the pre-mapped opaque codes from
		# _TOKEN_EXCHANGE_OPAQUE_CODES; the message is the user-safe text
		# from that same map, NOT the raw provider response. The provider
		# detail was logged via frappe.log_error inside _exchange_code.
		return None, _err(e.code, str(e))

	access_token = tokens.get("access_token")
	if not access_token:
		return None, _err("token_exchange_failed", "provider returned no access_token")

	email = tokens.get("email") or _fetch_account_email(provider, access_token, tokens.get("id_token") or "")

	p = get_provider(provider)
	now_ms = int(time.time() * 1000)
	expires_ms = now_ms + int(tokens.get("expires_in", 3600)) * 1000
	blob = {
		"type": "oauth",
		"provider": p["agent_provider"],
		"access": access_token,
		"refresh": tokens.get("refresh_token") or "",
		"expires": expires_ms,
		"email": email,
		"accountId": extract_account_id(provider, access_token),
		"clientId": p["client_id"],
		# Retain the id_token: the POOL path's CLIProxyAPI-codex reformat needs
		# it. The DIRECT auth-profile push must strip it first, though — the
		# fleet-agent schema rejects unknown keys (extra_forbidden); see
		# complete_paste_signin's direct_blob.
		"id_token": tokens.get("id_token") or "",
	}
	return {"provider": provider, "model": model, "email": email, "blob": blob}, None


@frappe.whitelist()
def complete_paste_signin(nonce: str, redirected_url: str) -> dict:
	"""Wizard calls this after the customer signs in and pastes the URL
	they copied from the browser's address bar.

	Gated on System Manager + per-user nonce binding: another System
	Manager can't accidentally (or maliciously) complete a sign-in that
	someone else started. Sprint-1 Important from the 2026-06-16 code
	review.
	"""
	require_jarvis_admin()
	entry, err = _validate_signin_nonce(nonce)
	if err:
		return err

	result, err = _exchange_and_build_blob(entry, redirected_url)
	if err:
		return err

	provider = result["provider"]
	model = result["model"]
	email = result["email"]
	blob = result["blob"]

	p = get_provider(provider)
	# The fleet-agent's PUT /auth-profile schema is STRICT (pydantic
	# extra_forbidden) and rejects an `id_token` field. id_token is only needed by
	# the POOL path's CLIProxyAPI-codex reformat, NOT the DIRECT auth-profiles.json
	# push — the blob builder keeps it for the pool flow, so strip it here or every
	# direct push 502s with `extra_forbidden ... blob.id_token`.
	direct_blob = {k: v for k, v in blob.items() if k != "id_token"}
	# Push the OAuth blob to the container's auth-profiles.json via admin/fleet.
	# Handle admin/session failures gracefully: a raw exception here surfaces as an
	# opaque "Internal Server Error" in the card. The common cause is an EXPIRED
	# bench session during the sign-in round-trip (admin_client then runs as guest
	# and can't read the admin creds → "no api_key/secret"), or admin/fleet being
	# unreachable while switching a proxy tenant to direct. Return a clean,
	# actionable message and bail BEFORE save_llm_creds, so the tenant's current
	# config is left untouched rather than half-migrated.
	try:
		admin_client.post_push_oauth_blob(p["agent_provider"], direct_blob)
	except admin_client.AdminAuthError as e:
		return _err(
			"admin_auth",
			f"Couldn't apply the sign-in — your session or admin credentials "
			f"aren't valid. Refresh the page (re-login if prompted) and try "
			f"again. ({e})",
		)
	except (
		admin_client.AdminUnreachableError,
		admin_client.AdminValidationError,
		admin_client.AdminRateLimitedError,
	) as e:
		return _err(
			"push_failed",
			f"Couldn't apply the sign-in to your agent right now — try again in a moment. ({e})",
		)
	# force=True is mandatory here. The OAuth blob lives in the container's
	# auth-profiles.json (out-of-band from Jarvis Settings), so on_update's
	# diff classifier sees no change and skips the re-render+restart when
	# a customer re-authorizes with the same provider+model. Without the
	# restart the container's agent keeps serving stale auth, surfacing
	# as the same ProviderAuthError the re-auth was meant to fix. Verified
	# live 2026-06-11.
	sync_result = onboarding.save_llm_creds(
		provider=provider,
		model=model,
		api_key="",
		base_url="",
		auth_mode="oauth",
		force=True,
	)

	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("llm_oauth_account_email", email, update_modified=False)
	settings.db_set("llm_oauth_connected_at", frappe.utils.now_datetime(), update_modified=False)

	frappe.cache.hdel(_CACHE_KEY, nonce)
	return _ok(
		{
			"account_email": email,
			"last_sync_status": (sync_result or {}).get("last_sync_status", ""),
		}
	)


@frappe.whitelist()
def complete_pool_account_signin(nonce: str, redirected_url: str) -> dict:
	"""Capture ONE more chat-subscription account for a pool "subscription"
	model, and RETURN the blob for the caller to stash in a pool
	subscription-account row.

	Same validation as ``complete_paste_signin`` (nonce+user binding,
	constant-time state compare, code exchange) and builds the same
	agent blob shape (WITH ``id_token``) — but this endpoint is
	capture-only: it does NOT call ``save_llm_creds``, does NOT push the
	blob to the container, and does NOT write to Jarvis Settings. The
	frontend collects N of these into a pool subscription and persists them
	later via ``save_llm_pool`` (which routes accounts[].oauth_blob through
	``pool_serialize.build_pool_payload`` into the oauth_blobs map).

	Gated on System Manager + per-user nonce binding; the nonce must have
	been minted by ``begin_pool_account_signin`` (carries ``pool=True``).
	Returns ``{account_ref, label, oauth_blob, account_email}`` where
	``account_ref`` is a freshly generated ``SUB_<hex>`` id that satisfies
	``^[A-Za-z0-9_-]{1,64}$`` and ``oauth_blob`` is the JSON-encoded blob.
	"""
	require_jarvis_admin()
	entry, err = _validate_signin_nonce(nonce)
	if err:
		return err
	# Only PASTE-BACK pool nonces may be completed here; a DIRECT paste-signin
	# nonce goes through complete_paste_signin, and a DEVICE-code pool nonce
	# (pool+device, no state/verifier) goes through poll_pool_account_signin —
	# routing one here would KeyError on entry["state"] in _exchange_and_build_blob.
	# Same opaque message as unknown_nonce so live nonces aren't leaked.
	if not entry.get("pool") or entry.get("device"):
		return _err("unknown_nonce", "nonce not recognized")

	result, err = _exchange_and_build_blob(entry, redirected_url)
	if err:
		return err

	email = result["email"]
	# account_ref is the stable per-account key the pool subscription row is
	# stored under (and later fed to build_pool_payload -> oauth_blobs). It
	# must match ^[A-Za-z0-9_-]{1,64}$; "SUB_" + 16 hex chars = 20 chars.
	account_ref = "SUB_" + secrets.token_hex(8)

	# Capture-only: clear the nonce (single-use, like complete_paste_signin)
	# and hand the blob back. No push, no save_llm_creds, no Settings write.
	frappe.cache.hdel(_CACHE_KEY, nonce)
	return _ok(
		{
			"account_ref": account_ref,
			"label": email,
			"oauth_blob": json.dumps(result["blob"]),
			"account_email": email,
		}
	)


@frappe.whitelist()
def poll_pool_account_signin(nonce: str) -> dict:
	"""Poll a device-code (Kimi) pool sign-in started by
	``begin_pool_account_signin``.

	Returns ``{status:"pending"}`` while the user hasn't approved yet, or the
	same capture-only ``{account_ref, label, oauth_blob, account_email}`` shape
	as ``complete_pool_account_signin`` on success. The frontend calls this on
	the device flow's ``interval`` until it stops returning "pending".

	Gated on System Manager + per-user nonce binding; the nonce must have been
	minted by ``begin_pool_account_signin`` for a device-flow provider.
	"""
	require_jarvis_admin()
	entry, err = _validate_signin_nonce(nonce)
	if err:
		return err
	if not (entry.get("pool") and entry.get("device")):
		# Same opaque message as unknown_nonce so live nonces aren't leaked.
		return _err("unknown_nonce", "nonce not recognized")

	p = get_provider(entry["provider"])
	headers = dict(p.get("device_headers") or {})
	headers["X-Msh-Device-Id"] = entry.get("device_id", "")
	try:
		resp = requests.post(
			p["token"],
			data={
				"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
				"device_code": entry["device_code"],
				"client_id": p["client_id"],
			},
			headers=headers,
			timeout=_HTTP_TIMEOUT,
		)
	except requests.RequestException as e:
		frappe.log_error(
			title="oauth device poll: network error", message=f"provider={entry['provider']!r} error={e!r}"
		)
		return _err("network_error", "Couldn't reach the sign-in provider. Try again in a minute.")

	body: dict = {}
	try:
		parsed = resp.json()
		if isinstance(parsed, dict):
			body = parsed
	except ValueError:
		pass

	if not resp.ok:
		err_code = body.get("error") if isinstance(body.get("error"), str) else ""
		# Not-yet-approved: keep the nonce alive and keep polling.
		if err_code in ("authorization_pending", "slow_down"):
			return _ok({"status": "pending"})
		# Terminal failure (expired_token / access_denied / ...): burn the nonce.
		frappe.cache.hdel(_CACHE_KEY, nonce)
		frappe.log_error(
			title="oauth device poll: provider rejected",
			message=(
				f"provider={entry['provider']!r} status={resp.status_code} "
				f"err={err_code!r} detail={resp.text!r}"
			),
		)
		msg = (
			"Sign-in expired; start again."
			if err_code == "expired_token"
			else "Sign-in was declined or failed. Start again."
		)
		return _err("device_" + (err_code or "failed"), msg)

	access_token = body.get("access_token")
	if not access_token:
		# 200 with no token yet — treat as still pending.
		return _ok({"status": "pending"})

	# Success. Build the device-flow agent blob; the fleet-agent's
	# oauth_blob_to_cliproxy_kimi transform consumes access/refresh/expires/
	# token_type/scope/device_id (Kimi is device-code: NO id_token, NO email).
	now_ms = int(time.time() * 1000)
	expires_ms = now_ms + int(body.get("expires_in", 3600)) * 1000
	blob = {
		"type": "oauth",
		"provider": p["agent_provider"],
		"access": access_token,
		"refresh": body.get("refresh_token") or "",
		"expires": expires_ms,
		"token_type": body.get("token_type") or "Bearer",
		"scope": body.get("scope") or "",
		"device_id": entry.get("device_id", ""),
	}
	account_ref = "SUB_" + secrets.token_hex(8)
	frappe.cache.hdel(_CACHE_KEY, nonce)
	# Kimi returns no account email, so synthesize a stable non-secret label.
	label = f"Kimi {account_ref[-4:]}"
	return _ok(
		{
			"status": "ok",
			"account_ref": account_ref,
			"label": label,
			"oauth_blob": json.dumps(blob),
			"account_email": "",
		}
	)


def _exchange_code(*, provider: str, code: str, code_verifier: str, redirect_uri: str = "") -> dict:
	"""POST to provider's token endpoint, return parsed JSON.

	On error, raises TokenExchangeError with an opaque code + user-safe
	message. The full provider response detail is logged server-side via
	frappe.log_error so operators can triage without leaking the detail
	(e.g. invalid_client vs invalid_grant) to the wire. Sprint-1
	Important #6 from the 2026-06-16 code review.
	"""
	p = get_provider(provider)
	try:
		data = {
			"grant_type": "authorization_code",
			"code": code,
			"code_verifier": code_verifier,
			"client_id": p["client_id"],
			"redirect_uri": redirect_uri or _REDIRECT_URI,
		}
		# Confidential clients (gemini-cli) require client_secret alongside
		# PKCE. Pure-PKCE clients (codex) leave it blank and we don't send it.
		if p.get("client_secret"):
			data["client_secret"] = p["client_secret"]
		resp = requests.post(
			p["token"],
			data=data,
			timeout=_HTTP_TIMEOUT,
		)
	except requests.RequestException as e:
		# Log the network detail; surface a fixed message + opaque code.
		frappe.log_error(
			title="oauth token exchange: network error",
			message=f"provider={provider!r} error={e!r}",
		)
		raise TokenExchangeError(
			"Couldn't reach the sign-in provider. Try again in a minute.",
			code="network_error",
		) from e

	if not resp.ok:
		# Parse the provider's response defensively. The full body is logged
		# server-side; only an opaque code + canned message goes back to
		# the wire so the response can't be used as an oracle (e.g.
		# distinguishing invalid_client from invalid_grant).
		raw_error = ""
		detail = resp.text
		try:
			body = resp.json()
			err = body.get("error")
			# RFC-6749 says `error` is a STRING code, but some providers return
			# it as an object. Only a string is a valid (hashable) opaque-code
			# lookup key; for an object, pull a nested code if one is present,
			# else fall through to the generic message. This guards the
			# `unhashable type: 'dict'` crash from using `error` as a dict key.
			if isinstance(err, str):
				raw_error = err
			elif isinstance(err, dict):
				nested = err.get("type") or err.get("code") or err.get("error")
				raw_error = nested if isinstance(nested, str) else ""
			detail = body.get("error_description") or err or resp.text
		except ValueError:
			pass
		frappe.log_error(
			title="oauth token exchange: provider rejected",
			message=(
				f"provider={provider!r} status={resp.status_code} raw_error={raw_error!r} detail={detail!r}"
			),
		)
		opaque_code, opaque_msg = _TOKEN_EXCHANGE_OPAQUE_CODES.get(
			raw_error,
			("token_exchange_failed", "Sign-in failed at the provider. Start a fresh sign-in."),
		)
		raise TokenExchangeError(opaque_msg, code=opaque_code)

	return resp.json()


def _fetch_account_email(provider: str, access_token: str, id_token: str) -> str:
	"""Best-effort email lookup via the provider's Bearer-authenticated
	userinfo endpoint (OpenAI + Gemini). The id_token JWT branch below is
	retained as a defensive fallback for any future provider configured
	with ``userinfo: None`` - no current provider takes that path."""
	p = get_provider(provider)
	if p["userinfo"]:
		try:
			resp = requests.get(
				p["userinfo"],
				headers={"Authorization": f"Bearer {access_token}"},
				timeout=_HTTP_TIMEOUT,
			)
			if resp.ok:
				return resp.json().get("email") or ""
		except requests.RequestException:
			pass
		return ""
	# Fallback for providers with userinfo=None - parse id_token JWT for email.
	# No current provider takes this path; retained defensively.
	if not id_token or id_token.count(".") < 2:
		return ""
	try:
		import json as _json

		_, payload, _ = id_token.split(".", 2)
		padding = "=" * (-len(payload) % 4)
		decoded = base64.urlsafe_b64decode(payload + padding)
		return _json.loads(decoded).get("email", "") or ""
	except Exception:
		# ValueError used to be listed explicitly but it's a subclass
		# of Exception - the union was redundant. Anything that goes
		# wrong parsing a JWT id_token (malformed base64, broken JSON,
		# missing dots) is non-fatal: id_token is a best-effort source
		# for the email hint, callers always have a fallback.
		return ""


@frappe.whitelist()
def disconnect() -> dict:
	"""Clear the container's OAuth profile, flip bench back to api_key.

	Gated on System Manager (Sprint-1 Important from the 2026-06-16
	code review): writes Jarvis Settings, ends an active subscription
	connection.
	"""
	require_jarvis_admin()
	try:
		admin_client.post_subscription_disconnect()
	except (
		admin_client.AdminUnreachableError,
		admin_client.AdminAuthError,
		admin_client.AdminValidationError,
	) as e:
		return _err("disconnect_failed", str(e))
	settings = frappe.get_single("Jarvis Settings")
	settings.db_set("llm_auth_mode", "api_key", update_modified=False)
	settings.db_set("last_sync_status", "disconnected", update_modified=False)
	settings.db_set("llm_oauth_account_email", "", update_modified=False)
	settings.db_set("llm_oauth_connected_at", None, update_modified=False)
	# DON'T wipe _CACHE_KEY here. The previous shape called
	# frappe.cache.delete_key(_CACHE_KEY), which nukes every pending
	# OAuth sign-in across the whole site - so a second System Manager
	# mid-paste-signin would see their nonce vanish under them when a
	# peer happened to click Disconnect. The cache holds only short-TTL
	# (10 min) per-user-bound nonces; _gc_expired_nonces sweeps them on
	# the next begin call. Punch-list "disconnect() wipes entire OAuth
	# signin cache hash" from the 2026-06-16 review.
	return _ok({})


# Auth modes that mean "a single chat subscription, served DIRECT via the
# container's auth-profiles.json (codex / gemini-cli runtime)" - NOT the pooled
# cliproxy path. "oauth" is the REV-1 canonical value; "subscription" is the
# legacy value some migrated tenants still carry.
_DIRECT_SUBSCRIPTION_MODES = {"oauth", "subscription"}


def _is_direct_subscription(
	auth_mode: str, has_models: bool, proxy_active: bool, provider_is_oauth: bool
) -> bool:
	"""True when the tenant is on the legacy DIRECT chat-subscription path.

	These tenants keep their LLM config in the flat ``llm_*`` / ``llm_oauth_*``
	fields (the ``v1_seed_llm_models`` migration deliberately skips oauth /
	subscription tenants) and are served by the container's
	``auth-profiles.json``, NOT the pooled cliproxy sidecar. ``get_llm_config``
	reads only the ``models[]`` child table, so the unified LlmPoolEditor can
	neither see nor re-authorize them. This predicate lets the SPA fall back to
	the DIRECT re-authorize (``begin_paste_signin`` / ``complete_paste_signin``)
	instead of silently offering only the pool editor.

	``has_models`` keys on the PRESENCE of ANY ``models[]`` row (enabled or
	not), matching ``get_llm_config``'s enabled-agnostic read — a pooled tenant
	mid-reconfiguration with all rows disabled must NOT be misclassified as
	direct (that would hide their real pool behind the direct card).

	``provider_is_oauth`` gates on ``llm_provider`` being an OAuth-capable
	provider: a tenant left in ``oauth`` mode with a non-OAuth provider (e.g.
	``Anthropic`` after ``reset_onboarding``) is NOT offered a re-authorize card
	that would only ever error ``unknown_provider``.

	Pure (no DB access) so the branch logic is unit-testable without a site.
	"""
	return (
		(auth_mode or "") in _DIRECT_SUBSCRIPTION_MODES
		and not proxy_active
		and not has_models
		and provider_is_oauth
	)


@frappe.whitelist()
def get_direct_subscription_status() -> dict:
	"""Surface the flat-field DIRECT chat-subscription connection to the SPA.

	The Account SPA's ``LlmPoolEditor`` is fed by ``onboarding.get_llm_config``,
	which reads ONLY the ``models[]`` child table. Existing direct
	chat-subscription (OAuth) tenants have an empty ``models[]`` and their
	connection lives in the flat ``llm_*`` / ``llm_oauth_*`` fields - invisible
	to that editor, so after the desk->SPA account migration they had no way to
	re-authorize. This read-only endpoint lets ``AccountView`` detect such a
	tenant and render a DIRECT re-authorize / disconnect card, WITHOUT migrating
	them onto the proxy path (a lone subscription row in ``models[]`` would force
	``compute_proxy_active`` true and re-architect them onto cliproxy with no
	credential blob).

	System-Manager only (matches the rest of the LLM-config surface). Never
	returns token material - only display metadata.
	"""
	require_jarvis_admin()
	settings = frappe.get_single("Jarvis Settings")
	auth_mode = (settings.get("llm_auth_mode") or "").strip()
	provider = settings.get("llm_provider") or ""
	proxy_active = bool(settings.get("proxy_active"))
	enabled_models = [m for m in (settings.get("models") or []) if m.enabled]
	has_models = bool(settings.get("models"))
	connected_at = settings.get("llm_oauth_connected_at")
	is_direct = _is_direct_subscription(
		auth_mode,
		has_models,
		proxy_active,
		is_oauth_provider(provider),
	)
	# A single chat subscription stored as a proxy pool-of-one. A single
	# subscription does NOT need cliproxy (the direct/codex auth-profiles.json
	# path serves it), so AccountView surfaces the DIRECT "Chat subscription"
	# option for these tenants and a re-authorize switches them off the pool.
	is_single_subscription_pool = (
		proxy_active
		and len(enabled_models) == 1
		and (enabled_models[0].credential_type or "api_key") == "subscription"
	)
	return {
		"is_direct_subscription": is_direct,
		"connected": bool(connected_at),
		"auth_mode": auth_mode,
		"provider": provider,
		"model": settings.get("llm_model") or "",
		"account_email": settings.get("llm_oauth_account_email") or "",
		"connected_at": str(connected_at) if connected_at else "",
		# Lets AccountView gate the container-OAuth "Connection" card to proxy
		# tenants: direct tenants are covered by DirectSubscriptionCard, and an
		# api_key tenant has no OAuth profile so the card would misleadingly read
		# "Not connected".
		"proxy_active": proxy_active,
		"is_single_subscription_pool": is_single_subscription_pool,
	}
