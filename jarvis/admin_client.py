"""HTTPS client for the Jarvis admin (jarvis_admin) app.

Authenticated calls prefer a short-lived OAuth bearer token: the bench
exchanges the customer's password (Jarvis Settings.jarvis_admin_customer_password,
username = jarvis_admin_customer_email) for an access token at admin's native
Frappe OAuth endpoint using the shared public client id `jarvis-bench`, caches
it in Redis, and sends `Authorization: Bearer <access_token>`. Customers
onboarded before OAuth (no password stored) fall back to the legacy native
api_key:api_secret (`Authorization: token <api_key>:<api_secret>`) - admin
accepts both during the migration window.

Guest calls (signup, get_plans) skip the header entirely; their admin
endpoints are @frappe.whitelist(allow_guest=True).
"""

import re
import time

import frappe
import requests

from jarvis.exceptions import (
	AdminAuthError,
	AdminContractError,
	AdminRateLimitedError,
	AdminRejectedError,
	AdminUnreachableError,
	AdminValidationError,
)

# Outer bench->admin HTTP budget. It MUST sit strictly ABOVE the admin's own
# admin->agent leg (now 100s) so the bench never hangs up on an apply the admin
# is still driving and writes a spurious "failed" over creds that land seconds
# later (the onboarding livelock, audit F1/F2). 150s = the 100s admin->agent
# budget + headroom for the HTTPS round-trip and admin's handler/response
# serialization. Increase both together if the admin->agent leg grows.
DEFAULT_TIMEOUT_S = 150

# OAuth password-grant config. The bench exchanges the customer's password
# (Jarvis Settings.jarvis_admin_customer_password) for short-lived bearer
# tokens against admin's native Frappe OAuth token endpoint, using the shared
# public client id. Authenticated calls prefer the bearer; calls fall back to
# the legacy api_key:api_secret when no password is stored (pre-OAuth
# customers, dual-auth migration window).
_OAUTH_CLIENT_ID = "jarvis-bench"
_OAUTH_TOKEN_PATH = "/api/method/frappe.integrations.oauth2.get_token"
_OAUTH_SCOPE = "all openid"
# Site-scoped Redis cache for the access/refresh tokens (frappe.cache() is
# per-site). Admin credentials are a per-site singleton, so one key suffices.
_OAUTH_CACHE_KEY = "jarvis:admin_oauth_token"
# Re-mint this many seconds before the cached access token's stated expiry so
# a request can't race past the boundary.
_OAUTH_EXPIRY_SKEW_S = 60
# Upper bound the cache entry lives, so the refresh token outlives the
# (~15min) access token; on entry expiry we re-mint with the password grant.
_OAUTH_CACHE_TTL_S = 24 * 60 * 60

# The admin control-plane app namespace. SWITCH TO V2: v2 is now the default
# control plane. Every admin /api/method path is built under this namespace via
# _m(). Pin a bench back to v1 with `set-config jarvis_admin_app jarvis_admin`.
# Read fresh via frappe.conf each call so a config change is honored without a
# worker restart — same rationale as _admin_url()'s fresh frappe.conf read.
# (Deliberately does NOT cover _OAUTH_TOKEN_PATH, which is Frappe-native and
# un-namespaced.)
_DEFAULT_ADMIN_APP = "jarvis_admin_v2"


def _admin_app() -> str:
	return (frappe.conf.get("jarvis_admin_app") or "").strip() or _DEFAULT_ADMIN_APP


def _m(dotted: str) -> str:
	"""Build an admin /api/method path under the configured admin-app namespace.
	e.g. _m("api.tenant.renew") -> "/api/method/jarvis_admin_v2.api.tenant.renew"."""
	return f"/api/method/{_admin_app()}.{dotted}"


# Cap on the cross-boundary message length. Long messages (e.g. a Frappe
# 500 with a 10KB traceback that happens to embed a token mid-frame) get
# truncated at the admin_client edge so they can't blow up
# ``last_sync_status`` (a Data field) or burn Error Log rows. Anything
# longer than this lands in Error Log only.
_MAX_MESSAGE_CHARS = 500

# Patterns to redact before any admin response text is allowed to cross
# the boundary into an Admin*Error message (which then becomes the body of
# ``last_sync_status`` via jarvis_settings.py and the Error Log via
# frappe.log_error). Even though admin's whitelisted endpoints are not
# supposed to echo secrets, defense-in-depth: a future admin handler
# raising ``frappe.throw("body was %s" % body)`` would otherwise reflect
# the request's api_key / api_secret / refresh_token straight back into
# the bench's status field. Punch-list "secret values can leak to
# last_sync_status/Error Log via upstream passthrough" from the
# 2026-06-16 cross-repo review.
_SECRET_PATTERNS = (
	# token=VALUE / api_key=VALUE / api_secret=VALUE / Bearer VALUE /
	# Authorization: Bearer VALUE / etc. Captures the credential keyword
	# + the (=|:) + the secret. We replace the whole tail with [REDACTED]
	# so the keyword survives ("AuthenticationError: api_key=[REDACTED]
	# is invalid").
	re.compile(
		r"(?i)\b("
		r"api[_-]?key|api[_-]?secret|client[_-]?secret|"
		r"access[_-]?token|refresh[_-]?token|"
		r"authorization|bearer|password|secret"
		r")\s*[=:]\s*\S+"
	),
	# OpenAI / Anthropic-style key prefixes (sk-..., sk-ant-..., etc.)
	# without an explicit keyword. Conservative threshold (20+ chars)
	# so we don't false-positive on short literals like "sk-1".
	re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
	# RFC 7519 JWTs (id_token / access_token shapes).
	re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
)


def _scrub_secrets(text: str) -> str:
	"""Strip token-shaped substrings from text crossing the admin_client
	boundary. Truncate to ``_MAX_MESSAGE_CHARS`` so a 10KB Frappe traceback
	can't pollute ``last_sync_status``.

	Idempotent: scrubbing already-scrubbed text leaves [REDACTED] markers
	intact (the patterns don't match the literal "[REDACTED]").
	"""
	if not text:
		return text
	out = text
	for pat in _SECRET_PATTERNS:
		out = pat.sub(
			lambda m: (
				# Keyword + "=[REDACTED]" for the labeled-credential pattern;
				# bare "[REDACTED]" for the prefix / JWT patterns (whole match
				# IS the secret).
				f"{m.group(1)}=[REDACTED]" if m.lastindex else "[REDACTED]"
			),
			out,
		)
	if len(out) > _MAX_MESSAGE_CHARS:
		out = out[:_MAX_MESSAGE_CHARS] + "...[truncated]"
	return out


# DEFAULT_ADMIN_URL lives in hooks.py as a single source of truth for
# deployment-level constants; re-exported here so existing
# ``from jarvis.admin_client import DEFAULT_ADMIN_URL`` callers keep working.
# Override per-customer via ``Jarvis Settings.jarvis_admin_url``.
from jarvis.hooks import DEFAULT_ADMIN_URL


def _admin_url(settings) -> str:
	# A deliberately-set site/common config ``jarvis_admin_url`` is the
	# deployment's source of truth and WINS over the ``Jarvis Settings`` field.
	# A reinstall / re-provision can leave a stale dev value (e.g.
	# "http://127.0.0.1:8000") in the doctype field; letting that mask a
	# correctly-configured site config made the admin unreachable. Resolution
	# order: site/common config -> Jarvis Settings override -> hardcoded
	# fallback. Read FRESH via frappe.conf / get_default_admin_url() so a config
	# value added after worker start is honored without a restart (the
	# module-level DEFAULT_ADMIN_URL import binds once and would miss it).
	from jarvis.hooks import get_default_admin_url

	conf_url = (frappe.conf.get("jarvis_admin_url") or "").strip().rstrip("/")
	if conf_url:
		return conf_url
	return ((settings.jarvis_admin_url or "").rstrip("/")) or get_default_admin_url().rstrip("/")


# Payment gateways this bench build can launch in the wizard. Advertised to
# admin so it never returns a provider the SPA/desk checkout can't render
# (an older bench omits this, and admin falls back to razorpay).
SUPPORTED_PROVIDERS = ("razorpay", "cashfree")

# Plan-09 WS2 capability advert (frozen contract). Tells admin, in the
# signup-payment envelope, that this bench understands the admin-hosted pay page
# (``admin_pay_page_v1``) and which provider checkout SHAPES it can render. This
# is BEHAVIOR-NEUTRAL predeployment: today's admin ignores keys it does not
# declare as arguments (Frappe drops unknown form_dict entries — the same
# mechanism that lets ``supported_providers``/``jarvis_version`` ride an old
# admin), so nothing changes now. A later admin negotiates a token-only response
# for capable benches off this advert and hard-fails incapable ones
# pre-provider-object (plan-09 §R P0-4). The names here are the frozen contract —
# keep them in lockstep with the admin side; do NOT rename without a coordinated
# admin change. ``_client_capabilities()`` hands each caller a fresh dict so a
# body mutation can never corrupt the shared template.
PAY_PAGE_CAPABILITY = "admin_pay_page_v1"
PROVIDER_SHAPES = ("razorpay_order", "razorpay_mandate", "cashfree_order", "cashfree_mandate")


def _client_capabilities() -> dict:
	return {"pay_page": PAY_PAGE_CAPABILITY, "provider_shapes": list(PROVIDER_SHAPES)}


def _is_production_bench() -> bool:
	"""True on a bench booted in production mode (supervisor/systemd managed). Used
	only to decide whether an ``http://`` public origin is a misconfiguration worth
	warning about — the injection fix below is identical everywhere. Mirrors the
	admin-side ``origin._is_production_site`` (jarvis_admin_v2 WS5)."""
	return bool(
		frappe.conf.get("restart_supervisor_on_update") or frappe.conf.get("restart_systemd_on_update")
	)


def _public_origin() -> str:
	"""A public site origin for the ``frappe_site_url`` this bench hands admin.

	The bench half of the plan-09 P1-5 ``get_url`` sweep. A bare
	``frappe.utils.get_url()`` derives the host from the request ``Host`` header
	when ``host_name`` is unset (``get_url``'s own ``allow_header_override`` path —
	proven reachable on this very environment), so a guest spoofing ``Host:`` on a
	signup / reconnect / replacement-lookup POST could choose the
	``frappe_site_url`` admin records for this tenant and the base of the magic
	link admin then mails. The load-bearing fix is ``allow_header_override=False``:
	it kills that injection vector.

	Resolution order mirrors the admin-side ``admin_public_origin`` (jarvis_admin_v2
	WS5): a configured ``host_name`` (validated https) wins; otherwise the site
	fallback from ``get_url`` with header override OFF. An ``http://`` base on a
	production bench is a misconfiguration — but it is caught at DEPLOY time by the
	readiness gate, NOT by throwing here: throwing would break signup on any bench
	where ``host_name`` is unset (every dev bench), and the injection vector is
	already closed regardless of scheme."""
	from urllib.parse import urlsplit

	host_name = (frappe.conf.get("host_name") or frappe.conf.get("hostname") or "").strip()
	if host_name:
		if not host_name.startswith(("http://", "https://")):
			host_name = "https://" + host_name
		parts = urlsplit(host_name)
		if parts.scheme == "https" and parts.hostname and "." in parts.hostname:
			return f"https://{parts.hostname.lower()}"

	base = (frappe.utils.get_url(allow_header_override=False) or "").rstrip("/")
	if base.startswith("http://") and _is_production_bench():
		frappe.logger("jarvis.onboarding").warning(
			"frappe_site_url resolved to an http base on a production bench; "
			"set host_name to an https origin (deploy readiness gates this)"
		)
	return base


def signup(
	email: str,
	company_name: str,
	plan: str,
	coupon: str | None = None,
	provider: str | None = None,
	billing: dict | None = None,
) -> dict:
	"""Guest signup against admin. Returns admin's data dict, which carries a
	``payment_provider`` discriminator plus that gateway's checkout handles:
	Razorpay -> {api_key, api_secret, razorpay_key_id, amount_inr}; Cashfree ->
	{..., cashfree_app_id, cashfree_env, amount_inr}.

	The order handles depend on the plan, NOT on the gateway. Annual (and any
	non-autopay) plan is a one-shot order and comes back with
	``razorpay_order_id`` / ``cashfree_order_id`` + ``payment_session_id``. A
	paid MONTHLY plan is an autopay mandate instead and comes back with
	``razorpay_subscription_id`` / ``cashfree_subscription_id`` +
	``subscription_session_id``. Do not assume one-shot: an earlier version of
	this docstring did, and it was wrong for every monthly plan.

	When the admin's ``Jarvis Admin Settings.require_email_verification``
	flag is ON, the response is {..., pending_verification: True} with no order
	handles - deferred until the customer clicks the magic link and the bench
	polls ``get_signup_payment_state``.

	``provider`` (optional) requests a specific gateway; admin defaults to
	razorpay when omitted or unsupported.

	``billing`` (optional, Plan 01) is a typed snapshot admin normalizes and
	stores on the Customer in the signup transaction; the response echoes
	``billing_saved: true`` only when a NEW admin persisted it (an older admin
	drops the unknown kwarg and never echoes it).
	"""
	body = {
		"email": email,
		"company_name": company_name,
		"plan": plan,
		"frappe_site_url": _public_origin(),
		"supported_providers": list(SUPPORTED_PROVIDERS),
		"client_capabilities": _client_capabilities(),
	}
	if coupon:
		body["coupon"] = coupon
	if provider:
		body["provider"] = provider
	if billing:
		body["billing"] = billing
	return _post_guest(path=_m("billing.signup.signup"), body=body)


def resume_pending_signup(
	plan: str,
	provider: str | None = None,
	*,
	billing: dict | None = None,
	idempotency_key: str | None = None,
) -> dict:
	"""Authenticated failed-payment resume: re-issues checkout handles for the
	caller's own Pending Payment signup, optionally on a different plan/provider.
	Returns the same checkout-fields shape as signup's sync path (no credentials
	— the bench already holds them; that's how this call authenticates).

	``idempotency_key`` makes the retry safe to repeat: admin hashes it onto the
	subscription with the handles it mints, so a replay of the SAME key returns
	the intent that key already created instead of opening a second gateway
	object. A double-submitted wizard, a retried POST and a refreshed pay screen
	converge on one order. A key admin has not seen is a NEW intent, which is
	what a genuine second attempt after a decline needs - so the bench mints per
	attempt, not per lifetime (jarvis.onboarding_contract.next_idempotency_key).
	Omitted when None, exactly as an older bench sends nothing.

	Raises AdminContractError carrying admin's ``code`` on a coded conflict
	(PAYMENT_ALREADY_ACTIVE, SIGNUP_TERMINAL, ...); a plain AdminValidationError
	from an admin too old to send one."""
	body: dict = {"plan": plan, "client_capabilities": _client_capabilities()}
	if provider:
		body["provider"] = provider
	if billing:
		body["billing"] = billing
	if idempotency_key:
		body["idempotency_key"] = idempotency_key
	return _post(path=_m("billing.signup.resume_pending_signup"), body=body)


def update_pending_billing(billing: dict) -> dict:
	"""Authenticated billing-only edit: update the caller's owned Pending Payment
	Customer WITHOUT re-issuing a payment intent (the post-intent Review & Pay
	"Edit" path). Returns admin's data (``billing_saved`` + normalized ``billing``
	summary). Raises AdminValidationError on a rejected payload or a non-resumable
	status."""
	return _post(path=_m("billing.signup.update_pending_billing"), body={"billing": billing})


def reconnect_eligibility(email: str, company_name: str = "") -> dict:
	"""Guest: would a reconnect for this (email, company) find anything? Read-only
	precursor to request_account_reconnect, so the wizard can offer the reconnect
	path only when it works. Short timeout - it gates a hint, never a decision:
	the caller treats any failure as "don't offer"."""
	return _post_guest(
		path=_m("billing.reconnect.can_reconnect"),
		body={"email": email, "company_name": company_name},
		timeout_s=8,
	)


def site_replacement() -> dict:
	"""Guest: was THIS site's account reconnected somewhere else?

	The only question a site whose credentials were rotated away can still ask -
	it can no longer authenticate, so it cannot be told over any other call.
	Returns ``{replaced, at, moved_to}``; treat any failure as "not replaced"."""
	return _post_guest(
		path=_m("billing.reconnect.check_site_replaced"),
		body={"frappe_site_url": _public_origin()},
		timeout_s=8,
	)


def request_account_reconnect(email: str, company_name: str = "") -> dict:
	"""Guest: start a fresh-bench reconnect to an EXISTING paid account (wiped
	site recovery). Admin emails a CODE to the registered address and returns an
	opaque request_id to poll — the response is identical whether or not the
	email matches an account. Nothing is re-paid. ``company_name`` disambiguates
	when one email owns several company accounts (multi-company identity)."""
	return _post_guest(
		path=_m("billing.reconnect.request_account_reconnect"),
		body={"email": email, "company_name": company_name, "frappe_site_url": _public_origin()},
	)


def get_reconnect_state(request_id: str, code: str = "") -> dict:
	"""Guest poll for the reconnect. Statuses: ``pending``, ``awaiting_code``
	(the customer must type the code mailed to them, or one support issued),
	``ready`` (+ api_key, api_secret, customer, customer_password), ``expired``.
	The code is what releases the credentials - request_id alone never does."""
	return _post_guest(
		path=_m("billing.reconnect.get_reconnect_state"),
		body={"request_id": request_id, "code": code},
	)


def get_signup_payment_state() -> dict:
	"""Authenticated poll. Returns one of:
	    {pending_verification: True}
	      - customer hasn't clicked the magic link yet
	    {pending_verification: False, razorpay_order_id, razorpay_key_id,
	     amount_inr}
	      - verification done; wizard can advance to Razorpay Checkout
	    {pending_verification: False, subscription_status: <other>}
	      - signup already completed (verification + payment both done)

	Uses the authenticated _post path with the api_key + api_secret the
	bench stashed at signup time. Only meaningful between the verification-
	on signup() return and the customer's click of the magic link; the
	wizard polls this on a "I've verified my email" button click.
	"""
	return _post(
		path=_m("billing.signup.get_signup_payment_state"),
		body={},
	)


def check_signup_payment_status() -> dict:
	"""Authenticated PROVIDER-TRUTH check on this signup's payment.

	The authoritative half of the payment surface, and the difference from
	``get_signup_payment_state`` is where the answer comes from: the poll reads
	admin's own subscription row and never asks a gateway whether money moved,
	while this asks the GATEWAY and converges a verified payment through the
	same activation seam a callback or a webhook would have used. It is what a
	customer whose checkout redirect died and whose webhook was lost can click -
	their money is captured at the gateway and nothing else in the flow will
	ever notice.

	Takes no arguments by contract: admin resolves the customer from the
	authentication this call carries and every remote identifier off their own
	row, so there is nothing a caller can substitute to aim a check at somebody
	else's payment.

	Same envelope as the poll (``code`` + the ``can_*`` capability flags), plus
	two facts only a check can state: ``gateway_consulted`` (whether a provider
	was really reached in THIS call - false when it failed and false when the
	answer came from the short cache) and ``awaiting_manual_reconciliation``
	(present only when true: the gateway holds money we could not credit to this
	exact subscription and generation, an operator is placing it, and a Pay
	affordance must be suppressed - the code stays the ordinary pending one
	because a decline here would invite a SECOND payment).

	Never creates or replaces an intent. A decline or a dead handle is a REPORT;
	opening the replacement is ``resume_pending_signup``'s job, on an explicit
	customer action. Rate-limited per customer: a 429 carrying
	``PAYMENT_CHECK_RATE_LIMITED`` is "wait and ask again", NOT a decline."""
	return _post(
		path=_m("billing.signup.check_signup_payment_status"),
		body={},
	)


def get_plans() -> list:
	return _post_guest(path=_m("billing.signup.get_plans"), body={})


def get_payment_providers() -> dict:
	"""Which gateways the control plane will actually charge on right now, and
	which to preselect: ``{providers: [...], default: "..."}``.

	Guest-safe like get_plans - the wizard needs it before the customer has any
	credentials. Returns only enabled keys and the default, never gateway
	configuration.

	The caller intersects the result with SUPPORTED_PROVIDERS: admin may enable a
	gateway this bench build cannot render, and offering it would strand the
	customer at a checkout step that never opens."""
	return _post_guest(path=_m("billing.signup.get_payment_providers"), body={})


# Admin-owned preset catalog (spec 3.3). Guest-safe fetch (get_plans pattern),
# cached in per-site Redis, bundled fallback so onboarding never hard-fails.
# The path is built per-call via _m() so the admin-app namespace override is
# honored (a module-level constant binds at import and can't read config).
_PRESET_CATALOG_CACHE_KEY = "jarvis:preset_catalog"
_PRESET_CATALOG_TTL_S = 6 * 60 * 60


def get_preset_catalog() -> list:
	"""Fetch the enabled Aerele preset catalog from admin (guest-safe), cache it,
	and fall back to the last cached copy then the bundled default so onboarding
	never hard-fails (spec L7). Never raises."""
	from jarvis._preset_catalog import BUNDLED_PRESET_CATALOG

	cache = frappe.cache()
	cached = cache.get_value(_PRESET_CATALOG_CACHE_KEY)
	if cached:
		return cached
	try:
		catalog = _post_guest(path=_m("billing.catalog.get_preset_catalog"), body={})
	except Exception:
		# "Never raises": onboarding's preset step must degrade to the bundled
		# catalog on ANY failure, not just the Admin* family. A scheme-less
		# jarvis_admin_url, for instance, raises requests.MissingSchema (a
		# RequestException that _do_post does NOT convert to an Admin* error),
		# which would otherwise 500 the whitelisted onboarding endpoint. #200
		# review #9.
		frappe.log_error(title="get_preset_catalog fell back to bundled")
		return BUNDLED_PRESET_CATALOG
	if isinstance(catalog, dict):
		catalog = catalog.get("data") or catalog.get("catalog") or catalog.get("presets") or []
	if isinstance(catalog, list) and catalog:
		cache.set_value(_PRESET_CATALOG_CACHE_KEY, catalog, expires_in_sec=_PRESET_CATALOG_TTL_S)
		return catalog
	return BUNDLED_PRESET_CATALOG


# Admin-owned provider + model catalog. Same shape as the preset catalog above,
# with two MANDATORY differences because this one is read on the CHAT HOT PATH
# (chat/api.py:940 inside send_message, chat/api.py:1456 inside
# set_conversation_model), whereas presets are only read at onboarding time.
#
#   1. A SHORT timeout. _post_guest defaults to DEFAULT_TIMEOUT_S = 150. An admin
#      that hangs rather than refuses would block a chat send for 2.5 minutes.
#   2. NEGATIVE CACHING. Caching only successes means every single request
#      retries a dead admin. The failure marker makes an outage cost one slow
#      request per _MODEL_CATALOG_FAIL_TTL_S, not one per chat turn.
_MODEL_CATALOG_CACHE_KEY = "jarvis:model_catalog"
_MODEL_CATALOG_TTL_S = 6 * 60 * 60
_MODEL_CATALOG_FAIL_KEY = "jarvis:model_catalog:failed"
_MODEL_CATALOG_FAIL_TTL_S = 60
_MODEL_CATALOG_TIMEOUT_S = 5


def get_model_catalog() -> list:
	"""Fetch the provider + model catalog from admin (guest-safe), cache it, and
	fall back to the bundled default so the picker never hard-fails.

	NEVER raises, and never blocks a chat turn for more than
	_MODEL_CATALOG_TIMEOUT_S. Callers on the chat hot path rely on both.
	"""
	from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

	try:
		return _fetch_model_catalog()
	except Exception as e:
		# The whole body is guarded, not just the HTTP call: the Redis
		# get_value/set_value calls can raise too (a connection blip during a
		# cache read), and this runs inside send_message. An unguarded cache
		# error would turn a transient Redis hiccup into a 500 on every chat
		# send, which is precisely the failure this function exists to avoid.
		#
		# frappe.logger(), NOT frappe.log_error: log_error writes an Error Log
		# DOCUMENT, so it needs the DB (and the cache) to be healthy. This is the
		# last-resort guard, reached exactly when infrastructure is unhealthy, so
		# logging through it would raise from the handler and defeat the guard.
		frappe.logger().warning("get_model_catalog degraded to the bundled catalog: %s", e)
		return BUNDLED_MODEL_CATALOG


def _fetch_model_catalog() -> list:
	"""Cache-then-network body of get_model_catalog. May raise; the caller
	converts any failure into the bundled fallback."""
	from jarvis._model_catalog import BUNDLED_MODEL_CATALOG

	cache = frappe.cache()
	cached = cache.get_value(_MODEL_CATALOG_CACHE_KEY)
	if cached:
		return cached
	# A recent failure short-circuits the network entirely. Without this, a
	# hanging admin costs every chat send a full timeout.
	if cache.get_value(_MODEL_CATALOG_FAIL_KEY):
		return BUNDLED_MODEL_CATALOG
	try:
		catalog = _post_guest(
			path=_m("fleet.provider_catalog.get_provider_catalog"),
			body={},
			timeout_s=_MODEL_CATALOG_TIMEOUT_S,
		)
	except Exception:
		# Degrade on ANY failure, not just the Admin* family: a scheme-less
		# jarvis_admin_url raises requests.MissingSchema, which _do_post does not
		# convert. Same reasoning as get_preset_catalog.
		cache.set_value(_MODEL_CATALOG_FAIL_KEY, 1, expires_in_sec=_MODEL_CATALOG_FAIL_TTL_S)
		frappe.log_error(title="get_model_catalog: admin unreachable")
		return BUNDLED_MODEL_CATALOG
	if isinstance(catalog, dict):
		catalog = catalog.get("data") or []
	if isinstance(catalog, list) and catalog:
		cache.set_value(_MODEL_CATALOG_CACHE_KEY, catalog, expires_in_sec=_MODEL_CATALOG_TTL_S)
		return catalog
	# Admin answered but served nothing. Distinct from unreachable: log it
	# separately so "admin is down" and "admin says zero enabled providers" are
	# tellable apart in the logs. Still falls back, since an empty picker is
	# worse for the customer than a slightly stale one.
	cache.set_value(_MODEL_CATALOG_FAIL_KEY, 1, expires_in_sec=_MODEL_CATALOG_FAIL_TTL_S)
	frappe.log_error(title="get_model_catalog: admin returned an empty catalog")
	return BUNDLED_MODEL_CATALOG


# Admin-owned speech-to-text config (voice features). Authenticated tenant
# fetch, cached in per-site Redis so chat-UI loads / transcribe calls don't
# pay an admin round-trip each time. The path is built per-call via _m() so the
# admin-app namespace override is honored (a module-level constant binds at
# import and can't read config).
_STT_CONFIG_CACHE_KEY = "jarvis:stt_config"
# No bench-side bust on admin key rotation/disable: the success TTL is the
# propagation lag bound, so keep it short.
_STT_CONFIG_TTL_S = 300
_STT_CONFIG_MISS_TTL_S = 60
_STT_CONFIG_MISS = {"__stt_unavailable__": True}


def get_stt_config() -> dict | None:
	"""Fetch the tenant's speech-to-text config from admin
	(``{"enabled": bool, "api_key": str, "model": str}``), cache it, and
	return None on ANY failure — voice features must degrade to
	"not configured" rather than break callers (``get_chat_ui_settings``
	runs on every SPA load). Failures are negative-cached briefly so a
	slow/down admin can't make every SPA load pay a fresh round-trip.
	Never raises."""
	cache = frappe.cache()
	cached = cache.get_value(_STT_CONFIG_CACHE_KEY)
	if cached == _STT_CONFIG_MISS:
		return None
	if cached:
		return cached
	try:
		# Short timeout: this is best-effort config on a hot endpoint; a
		# slow admin must degrade to "not configured", not block the SPA.
		cfg = _post(path=_m("api.tenant.get_stt_config"), body={}, timeout_s=5)
	except Exception:
		cache.set_value(_STT_CONFIG_CACHE_KEY, _STT_CONFIG_MISS, expires_in_sec=_STT_CONFIG_MISS_TTL_S)
		return None
	if not isinstance(cfg, dict):
		cache.set_value(_STT_CONFIG_CACHE_KEY, _STT_CONFIG_MISS, expires_in_sec=_STT_CONFIG_MISS_TTL_S)
		return None
	out = {
		"enabled": bool(cfg.get("enabled")),
		"api_key": cfg.get("api_key") or "",
		"model": cfg.get("model") or "",
	}
	cache.set_value(_STT_CONFIG_CACHE_KEY, out, expires_in_sec=_STT_CONFIG_TTL_S)
	return out


def confirm_payment(payload: dict) -> dict:
	"""POST Razorpay Checkout result; returns {agent_url, agent_token, tenant_status}."""
	return _post(path=_m("api.tenant.confirm_payment"), body=payload)


def get_connection(*, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
	"""Fetch the assigned container connection (fallback / scheduled sync).

	``timeout_s`` is keyword-only and defaults to DEFAULT_TIMEOUT_S so existing
	callers are unaffected. The chat-readiness gate (jarvis.account) passes a
	short 8s budget so a slow admin can't stall the SPA/boot path.

	The response carries ``chat_readiness`` ("Provisioning" | "Configuring" |
	"Ready") + ``chat_readiness_reason`` - the convergence signal the bench polls
	after an "applying"/timeout apply outcome to learn the admin reconcile
	finished the apply (F2). Pass a short ``timeout_s`` for those hot status
	probes so a slow admin can't stretch a convergence loop past its job budget.

	Also reports this bench's jarvis version so the control plane can close out a
	release rollout; an older admin ignores the key.
	"""
	from jarvis import __version__

	return _post(
		path=_m("api.tenant.get_connection"),
		body={"jarvis_version": __version__},
		timeout_s=timeout_s,
	)


# --------------------------------------------------------------------------- #
# Support panel proxies (Plan 3 B2). The customer bench forwards requesting_user +
# scope to the control-plane support endpoints, which re-derive the customer from the
# API key. JSON calls ride _post; only download needs raw bytes (see _authenticated_raw).
# --------------------------------------------------------------------------- #

_SUPPORT_TIMEOUT_S = 30


def support_status(*, timeout_s: int = 8) -> dict:
	return _post(path=_m("support.api.support_status"), body={}, timeout_s=timeout_s)


def support_list_tickets(*, requesting_user: str, scope: str) -> dict:
	return _post(
		path=_m("support.api.list_tickets"),
		body={"requesting_user": requesting_user, "scope": scope},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_create_ticket(
	*, subject: str, body: str, requesting_user: str, scope: str, tenant_id=None
) -> dict:
	return _post(
		path=_m("support.api.create_ticket"),
		body={
			"subject": subject,
			"body": body,
			"requesting_user": requesting_user,
			"scope": scope,
			"tenant_id": tenant_id,
		},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_get_thread(*, ticket: str, requesting_user: str, scope: str) -> dict:
	return _post(
		path=_m("support.api.get_thread"),
		body={"ticket": ticket, "requesting_user": requesting_user, "scope": scope},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_reply(*, ticket: str, body: str, requesting_user: str, scope: str) -> dict:
	return _post(
		path=_m("support.api.reply"),
		body={"ticket": ticket, "body": body, "requesting_user": requesting_user, "scope": scope},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_close_ticket(*, ticket: str, requesting_user: str, scope: str) -> dict:
	return _post(
		path=_m("support.api.close_ticket"),
		body={"ticket": ticket, "requesting_user": requesting_user, "scope": scope},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_awaiting_count(*, requesting_user: str, scope: str) -> dict:
	return _post(
		path=_m("support.api.awaiting_count"),
		body={"requesting_user": requesting_user, "scope": scope},
		timeout_s=_SUPPORT_TIMEOUT_S,
	)


def support_upload(
	*,
	ticket: str,
	filename: str,
	content_b64: str,
	requesting_user: str,
	scope: str,
	comm: str | None = None,
) -> dict:
	# Bytes ride b64-in-JSON (the CP media.upload decodes) -> plain _post, no binary helper.
	# comm (optional): the reply Communication to attach the File to so it renders inline; the CP
	# re-checks it belongs to the ticket before honoring it.
	return _post(
		path=_m("support.media.upload"),
		body={
			"ticket": ticket,
			"filename": filename,
			"content_b64": content_b64,
			"requesting_user": requesting_user,
			"scope": scope,
			"comm": comm,
		},
		timeout_s=DEFAULT_TIMEOUT_S,
	)


def support_download(*, ticket: str, file_url: str, requesting_user: str, scope: str):
	"""Raw streamed fetch of a Helpdesk file via the CP proxy. Returns
	(content_bytes, content_type, content_disposition)."""
	resp = _authenticated_raw(
		_m("support.media.download"),
		{"ticket": ticket, "file_url": file_url, "requesting_user": requesting_user, "scope": scope},
		timeout_s=DEFAULT_TIMEOUT_S,
	)
	return (
		resp.content,
		resp.headers.get("Content-Type", "application/octet-stream"),
		resp.headers.get("Content-Disposition"),
	)


def _raise_for_admin_raw(resp):
	"""Status routing for a RAW Response, mirroring _do_post's envelope routing (R1-3): 2xx returns
	the Response; 401/403 -> AdminAuthError (drives the ladder); 429 -> AdminRateLimitedError; other
	4xx -> AdminValidationError; 5xx -> AdminUnreachableError. DRIFT-GUARD: keep in sync with
	_do_post's status branches - mirror any change there in both places.

	One branch deliberately has NO mirror: _do_post's AdminRejectedError needs admin's
	structured ``error.code``, and this path streams raw bytes (media download) with no
	envelope to read it from. A rejection here stays an AdminUnreachableError, which is
	right - no media caller waits on a reconcile."""
	if resp.status_code < 400:
		return resp
	if resp.status_code in (401, 403):
		raise AdminAuthError(f"admin returned {resp.status_code}", status_code=resp.status_code)
	if resp.status_code == 429:
		raise AdminRateLimitedError("rate_limited")
	if resp.status_code < 500:
		raise AdminValidationError(
			_scrub_secrets((resp.text or "")[:_MAX_MESSAGE_CHARS]) or f"admin returned {resp.status_code}"
		)
	raise AdminUnreachableError(f"admin returned {resp.status_code}")


def _authenticated_raw(path: str, body: dict, *, timeout_s: int):
	"""Auth ladder (bearer -> 401 re-mint -> 403-terminal -> legacy fallback) around a RAW POST,
	returning the requests.Response so the caller can read bytes (P3 — _do_post is JSON-only, so it
	can't be reused for streamed media). DRIFT-GUARD: this replicates _post's ladder; mirror any
	change to _post's auth ladder here too. Error status routing is in _raise_for_admin_raw."""
	settings = frappe.get_single("Jarvis Settings")
	admin_url = _admin_url(settings)
	url = admin_url + path

	def _send(headers):
		try:
			return requests.post(url, json=body, headers=headers, timeout=timeout_s, stream=True)
		except (requests.ConnectionError, requests.Timeout) as e:
			raise AdminUnreachableError("admin is unreachable; check network / service status") from e

	bearer = {"Content-Type": "application/json"}
	access_token = _admin_access_token(settings, admin_url)
	if access_token:
		try:
			return _raise_for_admin_raw(_send({**bearer, "Authorization": f"Bearer {access_token}"}))
		except AdminAuthError as e:
			if e.status_code == 403:
				raise  # authorization denial, not a stale token — terminal
			access_token = _admin_access_token(settings, admin_url, force_refresh=True)
			if access_token:
				try:
					return _raise_for_admin_raw(_send({**bearer, "Authorization": f"Bearer {access_token}"}))
				except AdminAuthError as retry_err:
					if retry_err.status_code == 403:
						raise
			frappe.cache().delete_value(_OAUTH_CACHE_KEY)

	api_key = (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip()
	api_secret = (settings.get_password("jarvis_admin_api_secret", raise_exception=False) or "").strip()
	if not api_key or not api_secret:
		raise AdminAuthError("not onboarded (no OAuth password and no api_key/secret)")
	return _raise_for_admin_raw(_send({**bearer, "Authorization": f"token {api_key}:{api_secret}"}))


def renew(provider: str | None = None) -> dict:
	"""Existing customer pays again to extend (manual one-shot). Returns admin's
	data dict with the ``payment_provider`` discriminator + that gateway's
	checkout handles. A sub renews on the gateway it was created with unless
	``provider`` overrides."""
	body: dict = {"supported_providers": list(SUPPORTED_PROVIDERS)}
	if provider:
		body["provider"] = provider
	return _post(path=_m("api.tenant.renew"), body=body)


def post_update_llm_creds(
	provider: str,
	model: str,
	base_url: str,
	api_key: str,
	auth_mode: str = "api_key",
) -> dict:
	"""POST customer's new LLM creds to admin's /tenant/update-llm-creds.

	``auth_mode`` defaults to ``"api_key"`` to keep existing call sites
	source-compatible. Subscription-mode callers pass ``"subscription"`` and
	pass the OAuth access token as ``api_key``.
	"""
	# Ship the site's installed apps so admin persists them and the fleet-agent
	# scopes the tenant's persona skill families (e.g. no hrms app -> no hrms-*).
	return _post(
		path=_m("api.tenant.update_llm_creds"),
		body={
			"provider": provider,
			"model": model,
			"base_url": base_url,
			"api_key": api_key,
			"auth_mode": auth_mode,
			"installed_apps": frappe.get_installed_apps(),
		},
	)


def post_rotate_llm_secret(secret: str) -> dict:
	"""POST a rotated LLM secret to admin's /tenant/rotate-llm-secret.

	Used by the bench-side OAuth refresh cron via _sync_via_admin("reload").
	Hot-rotates /secrets/llm.key on the container without restart.

	Raises:
		AdminRateLimitedError on HTTP 429.
		AdminAuthError, AdminUnreachableError, AdminValidationError as usual.
	"""
	return _post(
		path=_m("api.tenant.rotate_llm_secret"),
		body={"secret": secret},
	)


def post_rotate_agent_token(new_token: str) -> dict:
	"""POST a rotated plugin agent_token to admin's /tenant/rotate-agent-token.

	C2 PR-3C orchestrator. Called from rotate_agent_token (this module's
	whitelisted bench endpoint, gated to System Manager). The bench
	generates fresh randomness, calls here, and ONLY persists locally
	when this returns success - so a partial-failure mid-rotation leaves
	the on-disk token in lockstep with what the container knows.

	Default 180s timeout matches push_oauth_blob: admin chains to
	fleet-agent's PUT /rotate-agent-token, which does a ``compose up -d``
	(container recreate) + healthz poll. Admin's bound is healthz+30s
	(default 90s); 180s gives HTTPS round-trip + response headroom.

	Raises:
	    AdminAuthError, AdminUnreachableError, AdminValidationError
	    (shares the rotate-secret 20/h bucket).
	"""
	return _post(
		path=_m("api.tenant.rotate_agent_token"),
		body={"new_token": new_token},
		timeout_s=180,
	)


def post_push_oauth_blob(provider: str, blob: dict) -> dict:
	"""POST an openclaw OAuthCredential blob to admin → fleet-agent → container.

	Called after a successful device-code poll. The container's openclaw
	codex/gemini-cli provider reads the blob from auth-profiles.json and
	refreshes internally via pi-ai going forward.

	Timeout is bumped above the default 90s because the admin handler
	chains to fleet-agent's PUT /auth-profile, which now runs
	``openclaw doctor --fix --non-interactive`` (up to 60s, migrates the
	legacy JSON store to SQLite on openclaw 2026.6.5+) plus
	``docker compose restart`` + healthz poll. Admin's own bound is 150s;
	we give bench 180s to allow for the HTTPS round-trip and admin's
	response serialization on top of that. The earlier 90s default ran
	out at the doctor step, surfacing as the same
	"AdminUnreachableError: read timeout" we hit 2026-06-12.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
		(rate-limit shares rotate-secret's 20/h bucket).
	"""
	return _post(
		path=_m("api.tenant.push_oauth_blob"),
		body={"provider": provider, "blob": blob},
		timeout_s=180,
	)


def post_push_custom_skills(skills: list[dict]) -> dict:
	"""POST the customer's rendered custom skills to admin → fleet → container.

	``skills`` is the list built by ``jarvis.chat.custom_skills.build_push_payload``
	(each ``{slug, description, user_invocable, body}``). The fleet-agent does a
	FULL RECONCILE (writes the desired set, removes the rest) then restarts the
	container so openclaw re-scans ``workspace/skills``. An empty list is a valid
	"remove all custom skills" reconcile.

	Timeout matches ``post_push_oauth_blob``: the admin handler chains to the
	fleet-agent's ``PUT /custom-skills`` which re-renders the entrypoint, writes
	the files and runs ``docker compose restart`` + healthz poll.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
		(rate-limit shares the rotate-secret bucket).
	"""
	return _post(
		path=_m("api.tenant.push_custom_skills"),
		body={"skills": skills},
		timeout_s=180,
	)


def post_push_agent_skills(agent_skills: list[dict]) -> dict:
	"""POST the customer's ENABLED marketplace-agent bundles to admin -> fleet ->
	container, into the SEPARATE ``agent_skills`` reconcile namespace (adversarial
	S4: never let it evict the customer's own custom skills).

	``agent_skills`` is the list built by
	``jarvis.chat.agent_catalog.build_agent_push_payload`` (each
	``{slug, description, body}`` where ``slug`` is ``agent-<agent_slug>``). The
	fleet-agent does a FULL RECONCILE of the agent_skills dir (writes the desired
	set, removes the rest), unions ``agent-*`` into the skill allowlist, then
	restarts the container so openclaw re-scans ``workspace/skills``. An empty
	list is a valid "remove all agent skills" reconcile.

	NOTE: the admin endpoint ``jarvis_admin.api.tenant.push_agent_skills`` and the
	fleet ``PUT /v1/containers/{name}/agent-skills`` are the B5 half of this work
	(a sibling of the custom-skills chain). Until they ship this raises
	``AdminValidationError`` (unknown method), which ``apply_agents`` records as a
	terminal ``failed:`` status — the bench-side path is complete and structured
	identically to ``post_push_custom_skills``.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
		(rate-limit shares the rotate-secret bucket).
	"""
	return _post(
		path=_m("api.tenant.push_agent_skills"),
		body={"agent_skills": agent_skills},
		timeout_s=180,
	)


def post_push_learned_skills(learned_skills: list[dict]) -> dict:
	"""POST the customer's compiled learned skills to admin -> fleet -> container,
	into the SEPARATE ``learned_skills`` reconcile namespace (Behavioural Pattern
	Learning Phase 2; adversarial S4: never let compiled behaviour bundles evict
	the customer's own custom skills or the marketplace-agent bundles).

	``learned_skills`` is the list built by
	``jarvis.learning.compiler.build_learned_push_payload`` (each
	``{slug, description, body}`` where ``slug`` is ``learned-<domain>``, matching
	the agent- item shape). The fleet-agent does a FULL RECONCILE of the
	learned_skills dir (writes the desired set, removes the rest), unions
	``learned-*`` into the skill allowlist, then restarts the container so
	openclaw re-scans ``workspace/skills``. An empty list is a valid "remove all
	learned skills" reconcile.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
		(rate-limit shares the rotate-secret bucket).
	"""
	return _post(
		path=_m("api.tenant.push_learned_skills"),
		body={"learned_skills": learned_skills},
		timeout_s=180,
	)


def post_agent_run(run_id: str, agent_id: str, session_key: str, message: str, timeout_s: int = 600) -> dict:
	"""Dispatch ONE marketplace-agent delegate turn: bench → admin → fleet → the
	customer's container (Phase 2C run relay).

	The agent scheduler (``jarvis.chat.agent_scheduler._launch_audit``) mints the
	Jarvis Agent Run + the per-run ``session_key`` (Phase 1), then calls here.
	Admin resolves the customer's own running container and forwards to the fleet
	agent-run verb, which dispatches the turn DETACHED on the cron lane and returns
	the 202 run-state (``{run_id, status:"queued", ...}``) immediately. The run
	completes later on the fleet worker; poll ``get_agent_run_status`` for the
	result (Phase 3 ``record_agent_run`` will be the completion/writeback path).

	``session_key`` is passed VERBATIM end-to-end (the only user resolver — the
	fleet never touches the Chat Session row). Idempotent on ``run_id`` at the
	fleet boundary: a re-dispatch of a seen run returns the existing state.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError.
	"""
	return _post(
		path=_m("api.tenant.agent_run"),
		body={
			"run_id": run_id,
			"agent_id": agent_id,
			"session_key": session_key,
			"message": message,
			"timeout_s": timeout_s,
		},
	)


def get_agent_run_status(run_id: str) -> dict:
	"""Poll a delegate run's state via admin → fleet (Phase 2C run relay).

	Returns ``{status, result:{reply, canvas_ref, gateway_session_id, ...}, error}``.
	Used by the Phase-3 completion path to learn when a dispatched delegate run has
	finished and to pull its result. Raises AdminAuthError / AdminUnreachableError /
	AdminValidationError (an unknown run surfaces as a downstream error)."""
	return _post(
		path=_m("api.tenant.agent_run_status"),
		body={"run_id": run_id},
	)


def push_wiki_files(
	files: list[dict], delete: list | None = None, known_paths: list | None = None
) -> dict | None:
	"""POST one batch of rendered org-wiki mirror files to admin → fleet →
	container workspace ``wiki/`` (wiki v2 mirror; see jarvis.chat.wiki_mirror).

	``files``: ``[{path, content_b64}]`` with paths RELATIVE under the wiki
	dir (e.g. ``customers/customer--acme.md``, ``index.md``); the caller keeps
	each batch under the fleet-agent's 256KB body cap. ``delete``: relative
	paths to remove. ``known_paths``: full-sync reconcile — fleet prunes wiki
	files not in the list.

	NO restart (the workspace is a live RW bind mount), so admin gives this
	relay its OWN rate bucket — it never burns the rotate-secret 20/h bucket
	the skill pushes share.

	Returns the parsed response dict (``{ok, written, deleted, pruned}``) or
	None on ANY failure: the mirror is a derived, rebuildable copy and the
	sync must degrade to "retry next sync" — never raise into the wiki save
	paths or the sync worker (which also means: no negative cache; the next
	sync should probe again immediately).
	"""
	try:
		return _post(
			path=_m("api.tenant.post_push_wiki_files"),
			body={"files": files, "delete": delete, "known_paths": known_paths},
			timeout_s=60,
		)
	except Exception:
		frappe.log_error(
			title="admin_client: push_wiki_files failed",
			message=frappe.get_traceback(),
		)
		return None


def push_wiki_graph(payload: dict) -> dict | None:
	"""POST the computed User/Role/Org wiki-utilization graph to admin, which
	re-validates and upserts it into ``Jarvis Wiki Graph Snapshot`` (see
	jarvis.chat.wiki_graph). Own rate bucket admin-side; NOT a container op.

	Returns the parsed response dict or None on ANY failure — the graph is a
	derived, rebuildable analytics copy and the sync must degrade to "retry next
	sync", never raise into the wiki save paths or the sync worker.
	"""
	try:
		return _post(
			path=_m("api.tenant.post_push_wiki_graph"),
			body={"graph": payload},
			timeout_s=60,
		)
	except Exception:
		frappe.log_error(
			title="admin_client: push_wiki_graph failed",
			message=frappe.get_traceback(),
		)
		return None


def get_generated_media(since_ms: int = 0) -> list[dict]:
	"""Pull recent codex ``imagegen`` output for this customer's running tenant
	container (admin → fleet → container disk). Returns a list of
	``{filename, mime, size, mtime_ms, b64}`` (capped by the fleet agent).

	Best-effort: the caller swallows failures - a missing generated image must
	never fail a chat turn. Read-only on the container (no restart).
	"""
	# _post already unwraps the admin's ``data`` envelope, so the response here
	# is the ``{"media": [...]}`` dict itself (not ``{"data": {"media": ...}}``).
	resp = _post(
		path=_m("api.tenant.fetch_generated_media"),
		body={"since_ms": int(since_ms or 0)},
		timeout_s=60,
	)
	return (resp or {}).get("media") or []


def post_subscription_disconnect() -> dict:
	"""POST to admin to clear the customer's OAuth profile on the container.

	Idempotent - a tenant in api_key mode is a no-op success.

	Carries the same 180s as post_push_oauth_blob, and for the same reason: this
	lands on admin's DELETE /auth-profile, whose own agent bound is 150s
	(``agent_client.delete_auth_profile``) and which runs doctor + restart inside
	it. Riding the shared DEFAULT_TIMEOUT_S of 150 left ZERO headroom for the
	HTTPS round trip on top of that, so the bench could give up on a call admin
	was still serving -- the same shape as the disconnect defect below.
	"""
	return _post(
		path=_m("api.tenant.subscription_disconnect"),
		body={},
		timeout_s=180,
	)


#: The disconnect's own HTTP budget. Deliberately NOT the shared 150s.
#:
#: admin's interactive-apply ladder hands the agent ``provision_healthz_timeout_s``
#: to come back healthy and then waits ``+30s`` on top of that itself
#: (``agent_client._interactive_apply_timeouts``). At the shipped 180s healthz
#: budget admin is therefore entitled to spend 210s answering, while this call
#: gave up at DEFAULT_TIMEOUT_S = 150 -- SIXTY SECONDS before admin could reply.
#: So any disconnect that actually needed its healthz budget (i.e. exactly the
#: slow container the budget exists for) raised AdminUnreachableError on a call
#: that was still succeeding server-side.
#:
#: That false failure is not cosmetic. ``onboarding.disconnect_llm`` aborts
#: BEFORE ``_clear_llm_secrets`` when this raises, by design, so the customer was
#: left advertising a live model while admin and the host had already destroyed
#: the credentials -- the exact inversion of the ordering guarantee that abort
#: exists to provide. Observed end-to-end on a live pool tenant.
#:
#: INVARIANT: keep this ABOVE admin's ``provision_healthz_timeout_s`` + 30.
#:
#: AND BELOW the web worker's own ceiling, which this file cannot enforce. The
#: call is synchronous inside a whitelisted request, so gunicorn's ``-t``
#: (bench's ``http_timeout``, unset here and so defaulting to 120 in a
#: supervisor deployment) bounds the whole thing. If that ceiling is lower than
#: the budget admin needs, the worker is killed mid-call and the caller never
#: reaches ``_clear_llm_secrets`` -- the same split state this constant exists
#: to prevent, just triggered a layer up. Raising this without also raising
#: ``http_timeout`` therefore fixes dev and leaves that deployment exposed;
#: the durable fix is to stop depending on the response (converge from admin's
#: own state on a later pass) rather than to keep widening timeouts.
_DISCONNECT_TIMEOUT_S = 240


def post_disconnect_llm() -> dict:
	"""POST to admin to delete EVERY LLM credential from the customer's container:
	the pool spec and its sidecar keys, /secrets/llm.key, and any auth profile.

	Wider than post_subscription_disconnect above, which only drops the DIRECT
	chat-subscription auth profile and leaves an api-key pool serving. This is the
	whole connection coming down, so nothing is left that could answer a turn.

	Idempotent - a tenant with nothing configured is a no-op success, so a repeat
	call (or a retry after a read timeout) is safe.

	Runs on _DISCONNECT_TIMEOUT_S, NOT the shared DEFAULT_TIMEOUT_S: see that
	constant for why 150s was strictly below what admin is allowed to spend, and
	what the resulting false "admin is unreachable" did to the customer's row.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
	"""
	return _post(
		path=_m("api.tenant.disconnect_llm"),
		body={},
		timeout_s=_DISCONNECT_TIMEOUT_S,
	)


def unpair_chat_devices() -> dict:
	"""Drop the container's paired devices. Idempotent; a file op on the agent,
	so it needs none of the disconnect budget above."""
	return _post(path=_m("api.tenant.unpair_chat_devices"), body={}, timeout_s=60)


# --------------------------------------------------------------------------- #
# Workspace reset proxies. The customer bench forwards to the control plane,
# which re-derives the customer from the api_key. ``_post`` already unwraps the
# admin's {ok, data} envelope to the inner data dict (see _do_post), so these
# return it directly — do NOT ``.get("data")`` again (that double-unwraps to {}).
# --------------------------------------------------------------------------- #
def reset_workspace(reason: str = "") -> dict:
	"""Self-serve container rebuild (keeps customer + subscription + site data).
	Container-recreate class op — destroy + warm-pool claim happen inline."""
	return _post(path=_m("api.tenant_request.reset_workspace"), body={"reason": reason}, timeout_s=180)


def reset_workspace_state() -> dict:
	"""Latest workspace-reset request state (for the SPA poll)."""
	return _post(path=_m("api.tenant_request.get_request_state"), body={}, timeout_s=8)


def post_update_llm_pool(
	*,
	spec: dict,
	api_keys: dict,
	oauth_blobs: dict,
	idempotency_key: str | None = None,
	timeout_s: int | None = None,
) -> dict:
	"""POST a PoolSpec + separated secrets to admin → fleet-agent → openclaw.

	``spec``        : secret-free PoolSpec dict (name, routing_mode, models).
	``api_keys``    : mapping ref → plaintext key (e.g. {"POOL_KEY_0": "sk-..."}).
	``oauth_blobs`` : mapping account_ref → parsed OAuth blob dict.
	``idempotency_key`` (plan-05 D2): opaque per-Start-chatting-attempt key. Admin
	    dedupes a retry carrying the same key to the SAME durable apply operation
	    (no new desired version, refunds the rate token, does not re-drive the
	    push), so a double-click / lost-response resume converges on one operation.
	    Omitted (None) preserves the pre-plan05 behaviour for internal callers.
	``timeout_s`` (plan-05 D2, F2/F3): a SHORT bound for the synchronous
	    descriptor-obtain from ``sync_pool_now`` - well under the gunicorn budget.
	    A timeout here is not a lost apply: admin commits desired + operation before
	    the fleet push, so the operation exists and the caller resumes via the same
	    idempotency key. None keeps the long DEFAULT_TIMEOUT_S for the async worker.

	The admin endpoint merges the secrets with the spec before forwarding to
	fleet-agent. Implemented in T3 (jarvis_admin); this stub is the bench-side
	caller so the controller and tests can reference it before that lands.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
	"""
	# Rides the shared DEFAULT_TIMEOUT_S (150s) like post_update_llm_creds so
	# both interactive apply legs sit above the admin's 100s admin->agent budget
	# (ladder fix F1). The pool apply is the same class of restart-render op; a
	# read-timeout here is now absorbed as an "applying"/pending outcome and
	# reconciled via get_connection, not written as a terminal failure.
	#
	# installed_apps: the pool-safe leg of the migrate-time resync (mirrors
	# post_update_llm_creds). A new admin persists it before the fleet forward
	# and echoes installed_apps_persisted; an old admin ignores the extra field.
	body = {
		"spec": spec,
		"api_keys": api_keys,
		"oauth_blobs": oauth_blobs,
		"installed_apps": frappe.get_installed_apps(),
	}
	if idempotency_key:
		body["idempotency_key"] = idempotency_key
	kw = {"timeout_s": timeout_s} if timeout_s is not None else {}
	return _post(path=_m("api.tenant.update_llm_pool"), body=body, **kw)


def get_llm_apply_operation(operation_id: str, *, timeout_s: int = 8) -> dict:
	"""Read-only status of a durable LLM-apply operation (plan-05 D2).

	Wraps admin's ``api.tenant.get_llm_apply_operation``, which never mutates and
	never spends the 20/hour apply bucket, so the SPA may poll it freely. ``_post``
	unwraps the admin ``data`` envelope, so this returns the §8.4 status dict
	directly:

	    {operation_id, state, code, message, tenant (opaque), desired_version,
	     applied_version, tenant_authority_generation, chat_readiness,
	     chat_readiness_reason, retryable, retry_after_seconds}

	Short default timeout: this is a hot poll on a converging apply, so a slow
	admin must not stretch the SPA's follow loop past a beat. UnknownOperation
	surfaces as AdminValidationError (404); a transport error surfaces as
	AdminUnreachableError, which the client seam treats as "keep polling", not a
	verdict.

	Raises:
		AdminAuthError, AdminUnreachableError, AdminValidationError
	"""
	return _post(
		path=_m("api.tenant.get_llm_apply_operation"),
		body={"operation_id": operation_id},
		timeout_s=timeout_s,
	)


def post_llm_auth_status() -> dict:
	"""Ask admin (and via admin, fleet-agent) whether the customer's
	container actually holds a usable OAuth profile right now.

	Used by the wizard / account page to gate the "Connected" UI state
	on the runtime contract rather than on the bench having sent the
	push. The on-disk file can be present without the running gateway
	seeing it (that's the bug class fleet-agent Task 1.2's restart
	closed), and the bench's last_sync_status only reflects whether the
	admin call returned 2xx - neither tells you "openclaw resolved the
	profile."

	Returns:
	    ``_post`` already unwraps the admin's ``data`` envelope, so this is
	    the inner dict itself (not ``{"ok": ..., "data": ...}``):
	    {"auth_profile_present": bool,
	     "profile_ids": [...],
	     "default_model": str,
	     "openai_profile_expires_ms": int | None}
	    Never includes token material.

	    ``auth_profile_present`` is provider-aware: admin recomputes it from
	    the tenant's llm_provider rather than trusting the fleet-agent's
	    OpenAI-only flag.

	Raises AdminAuthError / AdminUnreachableError / AdminValidationError
	in the same shape as the other admin_client methods.
	"""
	return _post(
		path=_m("api.tenant.llm_auth_status"),
		body={},
	)


def get_llm_usage() -> dict:
	"""Curated real Bifrost usage for the customer's tenant (monitor tab).
	Chain: fleet-agent /llm-usage -> admin api.tenant.get_llm_usage -> here.
	Raises AdminAuthError / AdminUnreachableError / AdminValidationError."""
	return _post(path=_m("api.tenant.get_llm_usage"), body={})


def push_usage_rollup(rollup: dict) -> dict:
	"""Push the bench's month-to-date per-user + per-model usage rollup to admin
	(Architecture A, fleet usage spec §3). Idempotent snapshot; admin upserts on
	(tenant, user, month). Called best-effort from the usage_push daily cron.
	Raises AdminAuthError / AdminUnreachableError / AdminValidationError."""
	return _post(path=_m("api.tenant.ingest_usage_rollup"), body={"rollup": rollup})


def push_error_rollup(errors: list) -> dict:
	"""Push a batch of scrubbed tenant errors (UI + code-level) to admin for the
	per-tenant Errors feed. Called best-effort from the error_push */5 cron.
	Raises AdminAuthError / AdminUnreachableError / AdminValidationError."""
	return _post(path=_m("api.tenant.ingest_error_rollup"), body={"errors": errors})


def push_bench_heartbeat(heartbeat: dict) -> dict:
	"""Push the bench liveness vector (watchdog + oldest-turn ages) to admin's GAP 1
	dead-man's-switch. Called best-effort from the heartbeat */5 cron.
	Raises AdminAuthError / AdminUnreachableError / AdminValidationError."""
	return _post(path=_m("api.tenant.ingest_bench_heartbeat"), body={"heartbeat": heartbeat})


def pair_chat_device(public_key: str, device_id: str, *, request_timeout_s: int = 30) -> dict:
	"""POST customer's chat device pubkey to admin; admin asks the fleet-agent
	to write a PairedDevice record into the customer's openclaw container and
	returns the issued bearer device-token. Customer keeps the private key.

	Sprint-2 plumb-through (2026-06-16 review): ``request_timeout_s`` is
	the budget the bench asks admin to allow for its admin -> fleet-agent
	leg. Defaults to 30s (matches admin's prior hardcoded value). Admin
	clamps to [5, 90] on its side so an over-large value can't push the
	overall HTTPS round-trip past the bench's outer DEFAULT_TIMEOUT_S.

	The outer HTTPS round-trip timeout (bench -> admin) stays at
	DEFAULT_TIMEOUT_S; that's the absolute upper bound on this call.
	"""
	return _post(
		path=_m("api.tenant.pair_chat_device"),
		body={
			"public_key": public_key,
			"device_id": device_id,
			"request_timeout_s": request_timeout_s,
		},
	)


def get_account_summary() -> dict:
	"""Fetch the customer's plan + validity + upgrade-eligible plans. Used by
	the /jarvis/billing SPA page to render the plan cards and the settings
	dialog's Plan and billing summary."""
	return _post(
		path=_m("api.account.get_account_summary"),
		body={},
	)


def preview_upgrade(target_plan: str) -> dict:
	"""Get the prorated amount for upgrading to ``target_plan`` (no order
	created). Used by the upgrade plan picker so each plan card shows the
	live-computed amount before the customer commits."""
	return _post(
		path=_m("api.account.preview_upgrade"),
		body={"target_plan": target_plan},
	)


def start_upgrade(target_plan: str, provider: str | None = None) -> dict:
	"""Create a prorated order for the upgrade and return the provider
	discriminator + that gateway's checkout handles (+ target_plan). The order
	stashes the upgrade intent for confirm_payment to apply after Checkout. A
	sub upgrades on the gateway it was created with unless ``provider`` overrides."""
	body: dict = {"target_plan": target_plan, "supported_providers": list(SUPPORTED_PROVIDERS)}
	if provider:
		body["provider"] = provider
	return _post(path=_m("api.account.start_upgrade"), body=body)


def cancel_plan_at_period_end() -> dict:
	"""Schedule a period-end cancellation of the BILLING plan.

	Not the LLM provider subscription - that is post_subscription_disconnect.
	Service runs until current_period_end; resume_plan undoes it until then.
	Returns {cancel_at_period_end, cancelled_at, access_ends_on,
	days_remaining, has_mandate}."""
	return _post(path=_m("api.account.cancel_plan_at_period_end"), body={})


def resume_plan() -> dict:
	"""Undo a scheduled cancellation while the paid period still has time.
	Returns the same shape plus ``requires_reauthorization`` - true when the
	sub had an autopay mandate, which cancelling released and which cannot be
	re-armed without a fresh payment authorization."""
	return _post(path=_m("api.account.resume_plan"), body={})


def reauthorize_autopay() -> dict:
	"""Mint a replacement Razorpay mandate for a sub whose autopay is off.

	Returns the subscription id for a mandate-auth Checkout. Nothing is charged
	now - the first cycle fires at the current period end.
	"""
	return _post(path=_m("api.account.reauthorize_autopay"), body={})


def preview_downgrade(target_plan: str) -> dict:
	"""Describe a downgrade before starting it. Returns {target_plan,
	target_price_inr, effective_on, requires_checkout}. No Razorpay object."""
	return _post(path=_m("api.account.preview_downgrade"), body={"target_plan": target_plan})


def start_downgrade(target_plan: str) -> dict:
	"""Schedule a downgrade for the next cycle. Monthly autopay returns a
	razorpay_subscription_id for a (₹0) mandate-auth Checkout; Annual/manual
	returns {scheduled: 1} with no checkout."""
	return _post(path=_m("api.account.start_downgrade"), body={"target_plan": target_plan})


def cancel_scheduled_downgrade() -> dict:
	"""Revoke a scheduled (revocable) downgrade - stay on the current plan."""
	return _post(path=_m("api.account.cancel_scheduled_downgrade"), body={})


def _oauth_token_request(admin_url: str, grant: dict) -> dict | None:
	"""POST a form-encoded grant to admin's OAuth token endpoint. Returns the
	token dict ({access_token, refresh_token, expires_in, ...}) on success, or
	None on any failure (network, non-JSON, non-200, missing access_token) so
	the caller can fall back. Never raises; never logs token material.

	Form-encoded (not JSON): Frappe's get_token reads ``request.form``.
	"""
	payload = {**grant, "client_id": _OAUTH_CLIENT_ID, "scope": _OAUTH_SCOPE}
	url = admin_url + _OAUTH_TOKEN_PATH
	try:
		resp = requests.post(
			url,
			data=payload,
			headers={"Content-Type": "application/x-www-form-urlencoded"},
			timeout=DEFAULT_TIMEOUT_S,
		)
	except requests.RequestException as e:
		# Broad catch (not just ConnectionError/Timeout) so SSL errors, redirect
		# loops, etc. return None and the caller falls back rather than crashing.
		# A requests exception repr carries url/host, never the POST body.
		frappe.log_error(
			title="admin_client: oauth token network error",
			message=f"url={url!r} grant={grant.get('grant_type')!r} error={e!r}",
		)
		return None
	try:
		token = resp.json()
	except ValueError:
		frappe.log_error(
			title="admin_client: oauth token non-JSON response",
			message=f"grant={grant.get('grant_type')!r} status={resp.status_code}",
		)
		return None
	if resp.status_code != 200 or not isinstance(token, dict) or not token.get("access_token"):
		# invalid_grant / invalid_client / expired-or-revoked refresh, etc.
		# Log the error code only (never the credentials) for triage.
		err = token.get("error") if isinstance(token, dict) else None
		frappe.log_error(
			title="admin_client: oauth token request rejected",
			message=(f"grant={grant.get('grant_type')!r} status={resp.status_code} error={err!r}"),
		)
		return None
	return token


def clear_cached_token() -> None:
	"""Drop the cached bearer. Anything that rotates this bench's admin credentials
	MUST call it: the cached token was minted from the old ones, stays valid for its
	full TTL, and keeps authenticating as the PREVIOUS account - so a reconnected
	bench asks about a customer it no longer is and sits on "still being set up"."""
	frappe.cache().delete_value(_OAUTH_CACHE_KEY)


def _cache_oauth_token(token: dict) -> None:
	ttl = int(token.get("expires_in") or 0)
	if ttl <= 0:
		# No usable lifetime advertised. Don't cache an instantly-stale token:
		# the freshness check would always miss and re-mint on every call,
		# storming the token endpoint. The caller still uses this token once.
		return
	frappe.cache().set_value(
		_OAUTH_CACHE_KEY,
		{
			"access_token": token["access_token"],
			"refresh_token": token.get("refresh_token"),
			"access_expires_at": time.time() + ttl,
		},
		expires_in_sec=_OAUTH_CACHE_TTL_S,
	)


def _admin_access_token(settings, admin_url: str, *, force_refresh: bool = False) -> str | None:
	"""Return a valid OAuth bearer access token for admin, or None when the
	bench has no OAuth credentials stored (pre-OAuth customer -> caller falls
	back to api_key:api_secret).

	Serves a cached access token until shortly before expiry. On a miss, tries
	the refresh token (best-effort), then the password grant (the durable
	bootstrap). ``force_refresh`` bypasses the cache to re-mint after a 401.
	"""
	username = (settings.jarvis_admin_customer_email or "").strip()
	# Password field -> get_password decrypts the real value out of __Auth.
	password = (settings.get_password("jarvis_admin_customer_password", raise_exception=False) or "").strip()
	if not username or not password:
		return None

	cache = frappe.cache()
	cached = {} if force_refresh else (cache.get_value(_OAUTH_CACHE_KEY) or {})
	access = cached.get("access_token")
	if access and (cached.get("access_expires_at", 0) - _OAUTH_EXPIRY_SKEW_S) > time.time():
		return access

	token = None
	refresh = cached.get("refresh_token")
	if refresh:
		token = _oauth_token_request(
			admin_url,
			{
				"grant_type": "refresh_token",
				"refresh_token": refresh,
			},
		)
	if not token:
		token = _oauth_token_request(
			admin_url,
			{
				"grant_type": "password",
				"username": username,
				"password": password,
			},
		)
	if not token:
		return None
	_cache_oauth_token(token)
	return token["access_token"]


def _post(path: str, body: dict, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
	"""Authenticated POST. Prefers a short-lived OAuth bearer token (password
	grant, cached in Redis) and falls back to the legacy native
	api_key:api_secret for customers onboarded before OAuth. Raises
	AdminAuthError when neither credential set is available.

	Folds the Settings + admin-URL read here so public wrappers stay one-liners
	(one Settings load per call).

	DRIFT-GUARD: _authenticated_raw (media, raw/streamed responses) replicates this
	bearer->401-remint->403-terminal->legacy ladder; mirror any change here there too.
	"""
	settings = frappe.get_single("Jarvis Settings")
	admin_url = _admin_url(settings)

	# Preferred path: OAuth bearer. Retry once on a token rejection (revoked,
	# or raced past the ~15min cap) by re-minting. If the retry is still
	# rejected, drop the cached token so the next call re-mints cleanly
	# instead of replaying the poisoned one, then fall through to the legacy
	# credential below.
	access_token = _admin_access_token(settings, admin_url)
	if access_token:
		headers = {
			"Authorization": f"Bearer {access_token}",
			"Content-Type": "application/json",
		}
		try:
			return _do_post(admin_url + path, body, headers, timeout_s, admin_url)
		except AdminAuthError as token_err:
			# A 403 is an authorization denial, not a stale token. Re-minting
			# would yield a token for the same customer principal (which backs
			# both the bearer and the legacy api_key:api_secret), so the retry
			# and the legacy fallback would just replay into the same 403 while
			# storming the token endpoint and evicting the cache on every call.
			# Surface it as-is; only a 401 (revoked / over-cap token) re-mints.
			if token_err.status_code == 403:
				raise
			access_token = _admin_access_token(settings, admin_url, force_refresh=True)
			if access_token:
				headers["Authorization"] = f"Bearer {access_token}"
				try:
					return _do_post(admin_url + path, body, headers, timeout_s, admin_url)
				except AdminAuthError as retry_err:
					# Same rule on the retry: a 403 is terminal; a 401 falls
					# through to the legacy credential below.
					if retry_err.status_code == 403:
						raise
			# Both bearer attempts rejected (or re-mint failed) - clear the
			# cache and fall through to legacy.
			frappe.cache().delete_value(_OAUTH_CACHE_KEY)

	# Legacy native api_key:api_secret (pre-OAuth customers / OAuth fallback).
	# Both are Password fields - attribute access returns the masked "*****"
	# placeholder; get_password decrypts the real value out of __Auth.
	api_key = (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip()
	api_secret = (settings.get_password("jarvis_admin_api_secret", raise_exception=False) or "").strip()
	if not api_key or not api_secret:
		raise AdminAuthError("not onboarded (Jarvis Settings: no OAuth password and no api_key/secret)")
	headers = {
		"Authorization": f"token {api_key}:{api_secret}",
		"Content-Type": "application/json",
	}
	return _do_post(admin_url + path, body, headers, timeout_s, admin_url)


def _post_guest(path: str, body: dict, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
	"""Unauthenticated POST (signup, get_plans). No Authorization header.
	Fetches the admin URL override from Settings internally so callers
	don't have to."""
	settings = frappe.get_single("Jarvis Settings")
	admin_url = _admin_url(settings)
	headers = {"Content-Type": "application/json"}
	return _do_post(admin_url + path, body, headers, timeout_s, admin_url)


def _extract_frappe_message(payload: dict) -> str:
	"""Pull the user-facing message out of a Frappe exception envelope.

	Frappe encodes user-visible alerts under `_server_messages` (a JSON-encoded
	list of JSON-encoded dicts with a `message` key). When that's empty, fall
	back to the `exception` string and strip the leading `module.path.ClassName: `
	prefix so we don't leak Python internals to the operator.

	The return value is always scrubbed for token-shaped substrings before it
	crosses the admin_client boundary - see _scrub_secrets for the patterns.
	Punch-list "secret values can leak to last_sync_status/Error Log via
	upstream passthrough" from the 2026-06-16 cross-repo review.
	"""
	import json as _json

	raw = (payload.get("_server_messages") or "").strip()
	if raw:
		try:
			messages = _json.loads(raw)
			if messages:
				first = _json.loads(messages[0]) if isinstance(messages[0], str) else messages[0]
				msg = (first or {}).get("message") or ""
				if msg:
					return _scrub_secrets(msg)
		except (ValueError, TypeError):
			pass
	exc = (payload.get("exception") or "").strip()
	if ":" in exc:
		return _scrub_secrets(exc.split(":", 1)[1].strip())
	return _scrub_secrets(exc or payload.get("exc_type") or "unknown admin error")


def _envelope_error_message(envelope) -> str:
	"""Pull ``error.message`` out of an admin envelope and run it through
	_scrub_secrets. Single bottleneck for the err.get('message') paths -
	every Admin*Error message we construct from upstream-controlled text
	flows through here."""
	if not isinstance(envelope, dict):
		return ""
	err = envelope.get("error", {}) or {}
	return _scrub_secrets(err.get("message") or "")


def _contract_error(payload) -> dict:
	"""The onboarding contract's machine-readable ``error`` object, or ``{}``.

	ONE reader for the contract's TWO wire shapes. A whitelisted method's RETURN
	value and a RAISED exception do not serialize the same way, so the identical
	``error`` object sits at two different depths::

	    returned   {"message": {"ok": false, "error": {"code": ...}}}
	    thrown     {"exc_type": "DuplicateEntryError", "error": {"code": ...}}

	Unifying them admin-side is a coordinated release against benches already in
	the field, so the contract's answer is a reader that accepts both: top level
	first, then under ``message``. (See the admin repo's
	``tests/billing/fixtures/README.md``; the vendored copy of that corpus in
	``jarvis/tests/fixtures/admin_contract/`` is what pins this function.)

	FAIL CLOSED on anything else. An admin older than the contract emits no
	``error`` object at all - only Frappe's ``exc_type`` and a sentence - and the
	answer to that is the generic path plus ``exc_type``, NEVER a prose parse.
	Requiring ``code`` is what makes "unknown shape" and "no contract" the same
	branch, so a future shape cannot be half-read.

	The message is scrubbed here (the single bottleneck every upstream-controlled
	string crosses); the rest of the object is admin's, verbatim - contract rule
	3 keeps internal document names out of it, and a facade needs the extras
	(``retry_after_seconds``, ``subscription_status``, ``attempt_id``) intact."""
	if not isinstance(payload, dict):
		return {}
	err = payload.get("error")
	if not (isinstance(err, dict) and err.get("code")):
		# Fall THROUGH rather than give up: a top-level ``error`` that carries no
		# code does not mean there is no contract, only that this depth is not
		# where it is. A response can legitimately hold both (a framework-shaped
		# hint at the top, admin's coded envelope under ``message``), and the
		# earlier shape - which stopped at the first ``error`` object it saw -
		# would have read the codeless one and reported "no contract".
		inner = payload.get("message")
		err = inner.get("error") if isinstance(inner, dict) else None
	if not isinstance(err, dict) or not err.get("code"):
		return {}
	out = dict(err)
	out["message"] = _scrub_secrets(str(err.get("message") or ""))
	return out


def _rejection(message: str, *, payload, status: int, exc_type: str = "") -> AdminValidationError:
	"""Build the known-4xx rejection, preserving admin's contract when it sent
	one (returned for the caller to ``raise``).

	AdminContractError is a SUBCLASS of AdminValidationError, so this is a strict
	widening: every caller that only knows the parent - the whole non-onboarding
	surface - sees the same class and the same ``str(e)`` it saw before, and only
	a caller that asks for ``code`` notices the difference."""
	err = _contract_error(payload)
	if not err:
		return AdminValidationError(message, exc_type=exc_type or None)
	# The contract's own display copy wins over Frappe's generic extraction: on
	# the THROWN shape the latter degrades to the bare exception class name when
	# the body carries no _server_messages, and "DuplicateEntryError" is not a
	# sentence to put in front of a customer.
	return AdminContractError(
		err.get("message") or message or f"admin returned {status}",
		code=str(err.get("code") or ""),
		contract_code=str(err.get("contract_code") or ""),
		recovery=str(err.get("recovery") or ""),
		error=err,
		exc_type=exc_type or None,
		http_status=status,
	)


# Admin ``error.code`` values that name a PERMANENT rejection: the admin was
# reached, validated the request and refused it, so re-sending the SAME payload
# can never converge. jarvis_admin_v2's @fleet_endpoint answers a FleetError
# with HTTP 502 and ``error.code`` = the raising class's name, so a config
# refusal arrives on the wire wearing the same status as a gateway fault - which
# is how a rejected creds push came to be recorded as "pending: admin applying
# config" forever (jarvis #542).
#
# An ALLOWLIST, not a denylist: an unrecognised code keeps today's optimistic
# "admin may still be reconciling" handling, so a fleet error class we have not
# classified yet can only ever be too patient, never wrongly terminal.
_PERMANENT_REJECTION_CODES = frozenset(
	{
		# Bad provider slug, a provider with no resolvable base_url, an
		# unusable openclaw render - deterministic in the request itself.
		"FleetConfigError",
		# A pool apply the fleet-agent permanently rejected (its invalid_spec
		# envelope class). admin's own docstring: retrying the same desired
		# spec against a healthy fleet-agent can never converge.
		"PoolSpecRejected",
	}
)


def _permanent_rejection_code(envelope) -> str:
	"""admin's ``error.code`` when it names a permanent rejection, else "".

	Only ever consulted on a response the admin actually shaped (an ``ok:
	false`` envelope) - a proxy's HTML 502 has no envelope and never reaches
	here, so a genuine gateway fault cannot be mistaken for a refusal."""
	if not isinstance(envelope, dict):
		return ""
	err = envelope.get("error")
	if not isinstance(err, dict):
		return ""
	code = err.get("code") or ""
	return code if code in _PERMANENT_REJECTION_CODES else ""


def _do_post(url: str, body: dict, headers: dict, timeout_s: int, admin_url: str) -> dict:
	try:
		resp = requests.post(url, json=body, headers=headers, timeout=timeout_s)
	except (requests.ConnectionError, requests.Timeout) as e:
		# Log the raw network detail to Error Log for operator triage;
		# surface only the bench-friendly summary on the exception (the
		# UI renders this verbatim). Punch-list item from the 2026-06-16
		# review: error bodies were re-raised verbatim, leaking
		# internal exception strings (paths, urllib internals) into
		# the customer-facing toast.
		frappe.log_error(
			title="admin_client: network error",
			message=f"url={url!r} error={e!r}",
		)
		raise AdminUnreachableError("admin is unreachable; check network / service status") from e

	try:
		payload = resp.json()
	except ValueError:
		# Non-JSON response usually = Frappe 5xx HTML error page or an
		# upstream proxy 502/504. The body could include internal
		# paths/tracebacks; log it but don't surface to the bench UI.
		frappe.log_error(
			title="admin_client: non-JSON response",
			message=f"url={url!r} status={resp.status_code} body={resp.text[:1000]!r}",
		)
		raise AdminUnreachableError(f"admin returned non-JSON response (status {resp.status_code})")

	envelope = payload.get("message", payload) if isinstance(payload, dict) else payload

	# Pre-extract the clean message + exc_type if Frappe wrapped a raised
	# exception. The status-based branches below prefer this clean text
	# over the raw envelope when available.
	exc_type = payload.get("exc_type", "") if isinstance(payload, dict) else ""
	clean = (
		_extract_frappe_message(payload)
		if (isinstance(payload, dict) and (exc_type or payload.get("_server_messages")))
		else ""
	)

	def _envelope_message() -> str:
		# _envelope_error_message already scrubs; clean is already scrubbed
		# (it came from _extract_frappe_message). Falling back to "" is fine.
		return _envelope_error_message(envelope) or clean or ""

	# Status-based routing for the three unambiguous wire signals.
	# The 2026-06-16 review caught that the previous shape ran the
	# exc_type allowlist BEFORE the status check, so a 429 admin
	# response with exc_type="RateLimitedError" (not in the allowlist)
	# fell through to AdminUnreachableError - losing the rate-limit
	# category entirely. 401/403/429 always win.
	if resp.status_code in (401, 403):
		# Carry admin's structured ``error.code`` when the refusal envelope has one,
		# so callers branch on a stable token rather than the human sentence a
		# hardened control plane may omit (jarvis.account._is_never_paid_403).
		err_obj = (envelope or {}).get("error", {}) if isinstance(envelope, dict) else {}
		raise AdminAuthError(
			_envelope_message() or f"admin returned {resp.status_code}",
			status_code=resp.status_code,
			code=(err_obj.get("code") or "") if isinstance(err_obj, dict) else "",
		)
	if resp.status_code == 429:
		err = (envelope or {}).get("error", {}) if isinstance(envelope, dict) else {}
		# A CODED 429 (PAYMENT_CHECK_RATE_LIMITED) is "wait and ask again" and
		# must never be rendered as a decline; carry its code so a caller can
		# tell it from the stock per-IP limiter's bare status.
		contract = _contract_error(payload)
		raise AdminRateLimitedError(
			_envelope_error_message(envelope) or clean or "rate_limited",
			retry_after_seconds=int(err.get("retry_after_seconds") or 0),
			code=str(contract.get("code") or ""),
			recovery=str(contract.get("recovery") or ""),
			error=contract,
		)

	# Frappe-wrapped raised exception with no unambiguous status. Route
	# by exc_type allowlist; default to AdminUnreachableError when the
	# class isn't recognised.
	if exc_type:
		if exc_type in ("ValidationError", "DuplicateEntryError", "DoesNotExistError"):
			# The THROWN wire shape: admin's contract ``error`` object rides at
			# the top level next to exc_type. Both survive here - the code for a
			# reader that has one, exc_type for the pre-contract admin that
			# sends nothing else.
			raise _rejection(clean, payload=payload, status=resp.status_code, exc_type=exc_type)
		# AuthenticationError ~ a token/credential failure (retry-eligible, 401);
		# PermissionError ~ an authorization denial (terminal, 403). Tag the
		# status so _post re-mints on the former but surfaces the latter as-is.
		if exc_type == "AuthenticationError":
			raise AdminAuthError(clean, status_code=401)
		if exc_type == "PermissionError":
			raise AdminAuthError(clean, status_code=403)
		# Unknown exc_type. Log it (so we learn what other admin error
		# classes to add to the allowlist) but don't embed admin_url +
		# raw exception class in the user-facing message.
		frappe.log_error(
			title=f"admin_client: unrecognised exc_type={exc_type!r}",
			message=f"url={url!r} clean={clean!r}",
		)
		# A response carrying an exc_type means the admin was REACHED and raised
		# an exception, so route by status exactly like the enveloped path below -
		# NOT as "unreachable". A 4xx is a rejected request (e.g. Helpdesk's
		# InvalidEmailAddressError, a ValidationError subclass that isn't on the
		# allowlist above) and must surface its clean message; only a 5xx is a
		# genuine admin fault.
		if 400 <= resp.status_code < 500:
			raise _rejection(
				clean or f"admin returned {resp.status_code}",
				payload=payload,
				status=resp.status_code,
				exc_type=exc_type,
			)
		raise AdminUnreachableError(clean or f"admin returned an unrecognised error: {exc_type}")
	# Sprint-3 PR-8 (2026-06-16 review): a 4xx response with the
	# structured envelope ({"ok": false, "error": {...}}) is a
	# user-input / business-rule error, NOT an "admin is unreachable"
	# condition. The previous shape raised AdminUnreachableError for
	# both 4xx envelopes AND genuine 5xx / network failures, which
	# made _surface() in onboarding.py show "admin is unreachable;
	# try again" for things like "no subscription found" or
	# "downgrade not supported" - misleading and unhelpful.
	#
	# Route by HTTP status:
	#   4xx + envelope -> AdminValidationError (clean text to UI)
	#   5xx + envelope -> AdminUnreachableError (network / admin-down)
	#   200 with ok:false (rare; some endpoints inline failure) -> AdminUnreachableError
	#
	# jarvis #542 refines the last two: a 5xx (or an inlined ok:false) whose
	# ``error.code`` is on _PERMANENT_REJECTION_CODES is admin REFUSING the
	# request, not admin being unwell, so it raises the AdminRejectedError
	# subclass carrying that code + admin's own message. Callers that retry or
	# wait for a reconcile branch on it; everything else keeps catching the
	# base class exactly as before.
	if resp.status_code >= 400:
		msg = _envelope_error_message(envelope)
		if not msg:
			# No structured ``error.message`` -> log the raw body but
			# don't include it in the user-facing exception.
			frappe.log_error(
				title=f"admin_client: {resp.status_code} with no error.message",
				message=f"url={url!r} body={resp.text[:1000]!r}",
			)
			msg = f"admin returned {resp.status_code}"
		if 400 <= resp.status_code < 500:
			# The RETURNED wire shape: {"message": {"ok": false, "error": {...}}}
			# under a deliberate 4xx. Same error object as the thrown form, one
			# depth lower, and the same class comes out of here.
			raise _rejection(msg, payload=payload, status=resp.status_code)
		rejected = _permanent_rejection_code(envelope)
		if rejected:
			raise AdminRejectedError(
				f"admin returned a {resp.status_code} error: {msg}",
				code=rejected,
				detail=msg,
			)
		raise AdminUnreachableError(f"admin returned a {resp.status_code} error: {msg}")
	if isinstance(envelope, dict) and not envelope.get("ok", True):
		err = envelope.get("error", {}) or {}
		code = err.get("code") or "?"
		msg = _envelope_error_message(envelope)
		if not msg:
			frappe.log_error(
				title="admin_client: 200 with ok:false but no error.message",
				message=f"url={url!r} body={resp.text[:1000]!r}",
			)
			msg = "admin returned an error envelope with no message"
		# Keep code in the message (stable identifier admin_client
		# callers + ops can grep for). admin_url is intentionally
		# omitted - the bench knows where it's pointing.
		if _permanent_rejection_code(envelope):
			raise AdminRejectedError(f"{code}: {msg}", code=code, detail=msg)
		raise AdminUnreachableError(f"{code}: {msg}")
	return envelope.get("data", envelope) if isinstance(envelope, dict) else envelope
