import frappe

from jarvis import release_notice
from jarvis.permissions import (
	grant_default_support,
	has_jarvis_access,
	has_jarvis_admin_access,
	support_scope,
)

no_cache = 1

_SUPPORT_AVAILABLE_CACHE_KEY = "jarvis:support_available"
_SUPPORT_AVAILABLE_TTL_S = 300
# R1-7: negative-cache a transient CP blip for a shorter window so support reappears promptly.
_SUPPORT_UNAVAILABLE_TTL_S = 60


def _support_available() -> bool:
	"""Fleet-wide support kill switch, Redis-cached (P8) so boot never blocks on a CP round-trip.
	Uses support_status (relaxed auth) — NOT get_connection, which 403s suspended customers (P7).
	Any failure ⇒ False (button hidden, never an error)."""
	cache = frappe.cache()
	cached = cache.get_value(_SUPPORT_AVAILABLE_CACHE_KEY)
	if cached is not None:
		return cached == "1"
	try:
		from jarvis import admin_client

		available = bool(admin_client.support_status(timeout_s=8).get("available"))
	except Exception:
		available = False
	ttl = _SUPPORT_AVAILABLE_TTL_S if available else _SUPPORT_UNAVAILABLE_TTL_S
	cache.set_value(_SUPPORT_AVAILABLE_CACHE_KEY, "1" if available else "0", expires_in_sec=ttl)
	return available


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

	# Support panel gating (Plan 3 B5). Lazy-grant the default support role to this chat user so
	# support isn't dark (P2 — grant_default_support clears the role cache so support_scope sees
	# it in this same request), then expose both the per-user access flag and the fleet kill switch.
	grant_default_support()
	context.boot["has_support_access"] = support_scope() is not None
	context.boot["support_available"] = _support_available()

	frappe.db.commit()
	return context
