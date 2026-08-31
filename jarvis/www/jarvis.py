import frappe

from jarvis import release_notice
from jarvis.permissions import (
	has_jarvis_access,
	has_jarvis_admin_access,
	support_scope,
)

no_cache = 1

_SUPPORT_AVAILABLE_CACHE_KEY = "jarvis:support_state"
_SUPPORT_AVAILABLE_TTL_S = 300
# R1-7: negative-cache a transient CP blip for a shorter window so support reappears promptly.
_SUPPORT_UNAVAILABLE_TTL_S = 60

# Support states the SPA understands. Only ``unconfigured`` is an ACTIONABLE gap (an
# operator can still set support up), so that is the one the UI shows as a disabled,
# explained entry; every other unavailable state hides support exactly as before.
SUPPORT_OK = "ok"
SUPPORT_UNCONFIGURED = "unconfigured"
SUPPORT_OFF = "off"
SUPPORT_ERROR = "error"


def _support_state() -> str:
	"""Fleet-wide support state, Redis-cached (P8) so boot never blocks on a CP round-trip.
	Uses support_status (relaxed auth) — NOT get_connection, which 403s suspended customers (P7).

	Returns one of ``ok`` / ``unconfigured`` / ``off`` / ``error``. The CP has always known
	which of those it means; the bench used to collapse them into one boolean, so a
	transient CP blip was indistinguishable from "support was never set up" and both just
	hid the button. ``error`` is kept distinct precisely so a self-healing blip does NOT
	render as "support is not set up" — it stays hidden and retries in a minute.

	A CP too old to send ``reason`` degrades safely: unavailable-without-a-reason is
	treated as ``off`` (hide), which is exactly today's behaviour."""
	cache = frappe.cache()
	cached = cache.get_value(_SUPPORT_AVAILABLE_CACHE_KEY, expires=True)
	if cached:
		return cached
	try:
		from jarvis import admin_client

		res = admin_client.support_status(timeout_s=8) or {}
		if res.get("available"):
			state = SUPPORT_OK
		else:
			reason = (res.get("reason") or "").strip()
			# Only a reason we KNOW is trusted; anything else (incl. an old CP that sends
			# none) falls back to the pre-existing hide-it behaviour.
			state = reason if reason in (SUPPORT_UNCONFIGURED, SUPPORT_OFF) else SUPPORT_OFF
	except Exception:
		state = SUPPORT_ERROR
	# R1-7 unchanged: EVERY unavailable state keeps the short TTL, so support appears
	# within a minute of an operator configuring it or flipping the switch on.
	ttl = _SUPPORT_AVAILABLE_TTL_S if state == SUPPORT_OK else _SUPPORT_UNAVAILABLE_TTL_S
	cache.set_value(_SUPPORT_AVAILABLE_CACHE_KEY, state, expires_in_sec=ttl)
	return state


def _support_available() -> bool:
	"""Back-compat boolean: support is usable. Kept as its own name because other
	surfaces (and the PWA) read the boot flag by that meaning."""
	return _support_state() == SUPPORT_OK


def get_context(context):
	# Server-side gate: the SPA route is authoritative, so a user without Jarvis
	# access never gets the app shell. A Guest is sent to the Desk (/app), which
	# in turn redirects to login — preserving the guest→login bounce. A signed-in
	# but unauthorized user is sent to the branded /jarvis-no-access page instead.
	# Follows Frappe's www redirect idiom.
	if not has_jarvis_access():
		frappe.local.flags.redirect_location = (
			"/app" if frappe.session.user == "Guest" else "/jarvis-no-access"
		)
		raise frappe.Redirect

	# frappe-ui's jinjaBootData plugin emits `window["<key>"] = {{ boot[key]|tojson }}`
	# for each key in `boot`, so the SPA gets window.csrf_token (frappe-ui reads it
	# for the X-Frappe-CSRF-Token header) and window.site_name (socket.js). Auth is
	# the session cookie — no custom token flow. Every value must be a plain JSON
	# type (a LocalProxy like frappe.local.lang breaks |tojson).
	context.boot = {
		"csrf_token": frappe.sessions.get_csrf_token(),
		"site_name": str(frappe.local.site),
		"default_route": "/jarvis",
		"is_system_manager": "System Manager" in frappe.get_roles(),
		# UI gate for the tenant-admin usage pane (design section 2). True for
		# System Managers too. Client gate is UX-only — every admin API
		# re-checks require_jarvis_admin server-side.
		"is_jarvis_admin": has_jarvis_admin_access(),
		# Native Frappe per-user theme (Light/Dark/Automatic); the SPA maps it to
		# light/dark/system so theme roams across devices without a Jarvis field.
		"jarvis_desk_theme": frappe.db.get_value("User", frappe.session.user, "desk_theme") or "Automatic",
		# NOTE: no `has_jarvis_access` boot flag — the guard above already
		# redirected anyone without access, so it would always be True here.
		# Site timezone for the SPA's dayjs config — shipping it in boot lets
		# AppShell configure systemTimezone synchronously instead of gating the
		# first routed render on a settings request.
		"time_zone": frappe.utils.get_system_timezone(),
	}

	# Whitelabel branding (Phase 2): tenant-admin-set identity, shipped to every
	# user in boot so the SPA renders the custom name/logo/favicon with no round
	# trip. Blank => the frontend falls back to the Jarvis defaults.
	_brand = (
		frappe.get_cached_value(
			"Jarvis Settings",
			"Jarvis Settings",
			["agent_name", "brand_logo", "brand_favicon"],
			as_dict=True,
		)
		or {}
	)
	context.boot["agent_name"] = _brand.get("agent_name") or ""
	context.boot["brand_logo_url"] = _brand.get("brand_logo") or ""
	context.boot["brand_favicon_url"] = _brand.get("brand_favicon") or ""

	# Release notice (operator-authored): the SPA shows a full-page interstitial
	# when this bench is behind the latest jarvis version. Up-to-date => no gate.
	context.boot["release_notice"] = release_notice.boot_payload()

	# Support panel gating (Plan 3 B5). Support access rides the base Jarvis roles
	# (support_scope: Jarvis User => own, Jarvis Admin => all), so there's nothing to grant —
	# just expose the per-user access flag and the fleet kill switch.
	context.boot["has_support_access"] = support_scope() is not None
	# Both flags, deliberately: `support_available` keeps its exact boolean meaning for
	# every existing reader, and `support_state` lets the SPA tell an actionable
	# "unconfigured" (show the entry disabled + explained) from "off"/"error" (hide).
	state = _support_state()
	context.boot["support_state"] = state
	context.boot["support_available"] = state == SUPPORT_OK

	# Onboarding guards: the localhost/non-public-host hard-block and the
	# background-worker health warning, both surfaced up front so the SPA wizard
	# and readiness banner don't need an extra round trip to learn them. Each
	# fails to the non-blocking default on any probe error - never falsely gate
	# or warn the customer off a transient blip.
	from jarvis import admin_client
	from jarvis.chat import pump

	try:
		context.boot["jarvis_public_host_ok"] = admin_client._onboarding_host_ok()
	except Exception:
		context.boot["jarvis_public_host_ok"] = True  # never falsely block the wizard
	try:
		context.boot["jarvis_worker_warning"] = bool(pump.chat_worker_status().get("degraded"))
	except Exception:
		context.boot["jarvis_worker_warning"] = False

	frappe.db.commit()
	return context
