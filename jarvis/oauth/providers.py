"""Per-provider OAuth metadata + authorize URL builder.

Used by jarvis.oauth.api to drive the paste-back OAuth flow against
the provider's own /oauth/authorize endpoint, with the codex-CLI-specific
parameter set that auth.openai.com requires for codex's client_id.
"""

import base64
import json
from urllib.parse import urlencode

from jarvis.exceptions import JarvisError
from jarvis.hooks import OAUTH_CLIENT_IDS, get_oauth_client_secret


class UnknownProviderError(JarvisError):
	"""Provider label not in PROVIDER_OAUTH_MAP."""


_PROVIDER_OAUTH_MAP: dict[str, dict] = {
	"OpenAI": {
		"authorize": "https://auth.openai.com/oauth/authorize",
		"token": "https://auth.openai.com/oauth/token",
		"userinfo": "https://api.openai.com/v1/userinfo",
		"scope": "openid profile email offline_access api.connectors.read api.connectors.invoke",
		# POOL sign-ins (CLIProxyAPI subscription accounts) MUST use the
		# codex-CLI scope set - no connectors scopes. The connectors scope
		# yields an access_token with aud=https://api.openai.com/v1 (fine for
		# agent's codex app-server, which is why the DIRECT flow keeps it),
		# but cli-proxy-api's codex backend needs aud=chatgpt.com/backend-api
		# and rejects the connectors-audience token: the account loads, then
		# /v1/models returns [] and every call 502s "unknown provider".
		# Live-verified 2026-07-03; this scope set is exactly what
		# cli-proxy-api's own --codex-login requests.
		"pool_scope": "openid email profile offline_access",
		# agent_provider keys the auth-profiles.json entry that agent
		# looks up at request time. After fix/oauth-model-provider-key on
		# fleet-agent + fix/chat-worker-mapped-model-provider on this app,
		# agent queries by the MODEL-provider key ("openai"), not the
		# OAuth flow id ("openai-codex"). The OAuth login flow itself uses
		# the metadata above (authorize URL, scopes, codex-cli params); only
		# the storage/lookup identity is the mapped name.
		"agent_provider": "openai",
		# codex-cli-specific authorize params - auth.openai.com returns a
		# generic "unknown_error" page mid-flow without these.
		"extra_authorize_params": {
			"id_token_add_organizations": "true",
			"codex_cli_simplified_flow": "true",
			"originator": "codex_cli_rs",
		},
	},
	# Google Gemini chat SUBSCRIPTION removed 2026-08-19: Google discontinued
	# consumer login-with-Google for Gemini (2026-06-18), so the gemini-cli OAuth
	# flow is dead (UNSUPPORTED_CLIENT). Gemini stays available via API key.
	# xAI Grok — SAME authorization_code + PKCE paste-back flow as codex. The
	# only deltas: a distinct loopback redirect_uri (xAI's server validates it
	# as an exact string, so mirror cli-proxy-api's default) and a fresh per-
	# request `nonce` param on the authorize URL (requires_nonce). Public/PKCE
	# client, so client_secret stays empty. Pooled accounts feed cli-proxy-api's
	# `xai` channel; there is no distinct pool_scope (one scope constant).
	"xAI Grok": {
		"authorize": "https://auth.x.ai/oauth2/authorize",
		"token": "https://auth.x.ai/oauth2/token",
		"userinfo": "https://auth.x.ai/oauth2/userinfo",
		"scope": "openid profile email offline_access grok-cli:access api:access",
		"agent_provider": "xai",
		"redirect_uri": "http://127.0.0.1:56121/callback",
		"requires_nonce": True,
		# xAI's approval screen hands the customer a BARE authorization code to
		# copy rather than bouncing them to a callback URL they can lift from the
		# address bar. cli-proxy-api hits the same wall: its xai prompt reads
		# "Paste the xAI callback Token", where the claude channel's reads "Paste
		# the Claude callback URL". A bare code carries no `state`, so
		# _exchange_and_build_blob skips the state compare for this provider.
		# That is safe HERE AND ONLY HERE: the token exchange always sends this
		# nonce's PKCE `code_verifier` (S256), so a code minted for anyone else's
		# authorize request cannot be redeemed against it - PKCE already supplies
		# the request binding that state would. Do NOT set this on a provider
		# that returns a real callback URL.
		"code_only_paste": True,
		"extra_authorize_params": {"plan": "generic", "referrer": "cli-proxy-api"},
	},
	# Kimi (Moonshot) — DEVICE-CODE flow (RFC 8628), NOT paste-back: there is no
	# authorize URL / redirect / code to paste. begin_pool_account_signin routes
	# grant_type=="device_code" providers to _begin_device_signin; the frontend
	# shows user_code + verification_uri and polls poll_pool_account_signin.
	# Public device client (no secret, no PKCE). Custom X-Msh-* headers required.
	"Kimi (Moonshot)": {
		"grant_type": "device_code",
		"device_authorization": "https://auth.kimi.com/api/oauth/device_authorization",
		"token": "https://auth.kimi.com/api/oauth/token",
		"agent_provider": "kimi",
		"device_headers": {"X-Msh-Platform": "cli-proxy-api", "X-Msh-Version": "1.0.0"},
		"poll_interval_s": 5,
	},
}


def is_oauth_provider(label: str) -> bool:
	"""True when ``label`` is a PASTE-BACK (authorization_code) OAuth provider —
	exactly the providers ``begin_paste_signin`` can mint an authorize URL for.

	Device-code providers (Kimi) are deliberately EXCLUDED: they are in
	``_PROVIDER_OAUTH_MAP`` but have no authorize URL, so ``build_authorize_url``
	would KeyError on ``p["authorize"]``. Callers use this to gate the DIRECT
	"Re-authorize" affordance for a stored ``llm_provider`` (e.g. skip it for a
	non-OAuth default like ``Anthropic`` left behind by ``reset_onboarding``, and
	for a device-code provider that can only be captured via the pool flow).
	"""
	entry = _PROVIDER_OAUTH_MAP.get(label)
	return entry is not None and entry.get("grant_type") != "device_code"


def accepts_bare_code(label: str) -> bool:
	"""True when ``label``'s approval screen hands back a BARE authorization code
	instead of a callback URL, so the paste box must take the code on its own.

	Only xAI sets ``code_only_paste``. See the comment on that key for why
	skipping the ``state`` compare is sound for a bare code (PKCE binds it) and
	why no URL-returning provider may opt in.
	"""
	return bool(_PROVIDER_OAUTH_MAP.get(label, {}).get("code_only_paste"))


def agent_provider_for(label: str) -> str:
	"""The ``agent_provider`` (cliproxy upstream id — ``openai`` / ``xai`` /
	``kimi``) for a subscription provider label, or "" when the label is not a
	known OAuth/subscription provider.

	Metadata only: unlike ``get_provider`` it never resolves the client id/secret,
	so it is cheap enough for the chat hot path (``_catalog_models_for_pool`` maps
	a catalog entry's ``subscription_label`` to the upstream a pool row carries).
	"""
	return (_PROVIDER_OAUTH_MAP.get(label) or {}).get("agent_provider", "")


def get_provider(label: str) -> dict:
	"""Look up provider metadata, including the lazy-resolved client_id +
	client_secret. ``client_secret`` is an empty string for PKCE-only
	providers (OpenAI codex); confidential-client providers read it from
	their ``JARVIS_*_OAUTH_CLIENT_SECRET`` env override."""
	if label not in _PROVIDER_OAUTH_MAP:
		raise UnknownProviderError(f"OAuth not supported for provider {label!r}")
	entry = dict(_PROVIDER_OAUTH_MAP[label])
	entry["client_id"] = OAUTH_CLIENT_IDS[label]
	entry["client_secret"] = get_oauth_client_secret(label)
	return entry


def extract_account_id(provider: str, access_token: str) -> str:
	"""Pull agent's `accountId` claim out of the access JWT.

	agent's codex auth resolver gates on this field: profiles without
	an accountId are treated as unusable and chat fails with "No API key
	found for provider openai". See openclaw/docs/concepts/oauth.md step
	6 of the codex OAuth exchange.

	OpenAI codex: ``payload["https://api.openai.com/auth"]["chatgpt_account_id"]``.

	Best-effort: returns ``""`` on any parse / claim-lookup failure,
	mirroring ``_fetch_account_email``'s never-raise contract.
	"""
	if provider != "OpenAI":
		return ""
	if not access_token or access_token.count(".") < 2:
		return ""
	try:
		_, payload_b64, _ = access_token.split(".", 2)
		padded = payload_b64 + "=" * (-len(payload_b64) % 4)
		claims = json.loads(base64.urlsafe_b64decode(padded))
	except (ValueError, TypeError):
		return ""
	if not isinstance(claims, dict):
		return ""
	namespace = claims.get("https://api.openai.com/auth")
	if not isinstance(namespace, dict):
		return ""
	value = namespace.get("chatgpt_account_id")
	return value if isinstance(value, str) else ""


def build_authorize_url(
	*,
	provider: str,
	redirect_uri: str,
	code_challenge: str,
	state: str,
	pool: bool = False,
	oidc_nonce: str = "",
) -> str:
	"""Construct the /oauth/authorize URL with all required parameters.

	``pool=True`` selects the provider's ``pool_scope`` when it defines one
	(subscription-pool accounts consumed by cli-proxy-api need a different
	token audience than the direct agent flow - see the OpenAI entry).

	``oidc_nonce`` is added to the authorize params only for providers that set
	``requires_nonce`` (xAI's authorize endpoint 400s without a fresh nonce, the
	same way ``state`` is minted per request). Ignored for providers that don't.
	``redirect_uri`` is the effective per-provider redirect the caller resolved.
	"""
	p = get_provider(provider)
	params = {
		"response_type": "code",
		"client_id": p["client_id"],
		"redirect_uri": redirect_uri,
		"scope": (p.get("pool_scope") or p["scope"]) if pool else p["scope"],
		"code_challenge": code_challenge,
		"code_challenge_method": "S256",
		"state": state,
	}
	if p.get("requires_nonce") and oidc_nonce:
		params["nonce"] = oidc_nonce
	params.update(p["extra_authorize_params"])
	return f"{p['authorize']}?{urlencode(params)}"


def provider_redirect_uri(provider: str, default: str) -> str:
	"""The effective OAuth redirect_uri for ``provider``: its own value when it
	pins one (xAI mirrors cli-proxy-api's exact loopback callback), else the
	shared default. build_authorize_url and the token exchange MUST use the same
	value, so this is resolved once in _begin_signin and cached on the nonce."""
	return _PROVIDER_OAUTH_MAP.get(provider, {}).get("redirect_uri") or default
