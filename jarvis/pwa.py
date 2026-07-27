"""Serve the PWA's service worker from the site root, so it can control the app.

A service worker may only claim a scope at or below the path it is served from.
The PWA's bundle lives at /assets/jarvis/pwa/, so a worker shipped alongside it
could only ever control /assets/jarvis/pwa/ — never the app itself, which Frappe
serves at /jarvis-mobile. (This is the trap the Frappe HR PWA falls into: its
worker is registered from /assets/hrms/frontend/sw.js and so never controls
/hrms. It works there because that worker exists for Firebase push, not offline.)

The usual fix — drop sw.js at the web root — is closed off in Frappe: the
StaticPage renderer explicitly refuses to serve ``js`` out of an app's ``www``
folder (UNSUPPORTED_STATIC_PAGE_TYPES). So the app registers a custom page
renderer instead. Custom renderers are tried FIRST (PathResolver.resolve), and
they see the endpoint AFTER website_route_rules have been applied — which is why
the worker is exposed at the root-level ``/jarvis-mobile.sw.js`` rather than
``/jarvis-mobile/sw.js``: the latter would be swallowed by the app's own
``/jarvis-mobile/<path:app_path>`` catch-all and served the HTML shell.

Served from the root, the worker is free to claim any narrower scope, and
main.js registers it with an explicit ``{scope: "/jarvis-mobile"}`` — so it
controls exactly the PWA and never the Desk at /app.
"""

import os

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer

# Root-level, deliberately NOT under /jarvis-mobile/ (see module docstring).
SW_ROUTE = "jarvis-mobile.sw.js"

# Per-tenant PWA manifest, served root-level (same catch-all reasoning as the
# worker) at a URL OUTSIDE the service worker's precache list, so the customer's
# name/icon aren't pinned to the stale build-time manifest. main.js repoints the
# shell's <link rel="manifest"> here at runtime. id/scope/start_url stay constant
# (changing them re-installs / breaks the standalone app).
MANIFEST_ROUTE = "jarvis-mobile.webmanifest"

_DEFAULT_ICONS = [
	{
		"src": "/assets/jarvis/manifest/icon-192.png?v=2",
		"sizes": "192x192",
		"type": "image/png",
		"purpose": "any",
	},
	{
		"src": "/assets/jarvis/manifest/icon-512.png?v=2",
		"sizes": "512x512",
		"type": "image/png",
		"purpose": "any",
	},
	{
		"src": "/assets/jarvis/manifest/icon-192-maskable.png?v=2",
		"sizes": "192x192",
		"type": "image/png",
		"purpose": "maskable",
	},
	{
		"src": "/assets/jarvis/manifest/icon-512-maskable.png?v=2",
		"sizes": "512x512",
		"type": "image/png",
		"purpose": "maskable",
	},
]

# The only scope this worker is allowed to claim. Sent as a response header so
# the browser permits the narrower registration even though the script is served
# from the root.
SW_SCOPE = "/jarvis-mobile"


def _sw_path() -> str:
	"""Absolute path of the built worker (vite-plugin-pwa emits it into outDir)."""
	return os.path.join(frappe.get_app_path("jarvis"), "public", "pwa", "sw.js")


class ServiceWorkerRenderer(BaseRenderer):
	def can_render(self) -> bool:
		# BaseRenderer strips the leading slash off the path.
		return self.path == SW_ROUTE and os.path.isfile(_sw_path())

	def render(self):
		with open(_sw_path(), "rb") as f:
			data = f.read()

		# build_response guesses the content type from the path, so the ".js"
		# suffix earns the right mimetype; a worker served as text/html is
		# rejected outright.
		return self.build_response(
			data,
			headers={
				"Service-Worker-Allowed": SW_SCOPE,
				# The worker is the one file that must never be served stale: a
				# cached copy would pin users to an old precache manifest and the
				# bundle it names.
				"Cache-Control": "no-cache, no-store, must-revalidate",
			},
		)


class ManifestRenderer(BaseRenderer):
	"""Serve a per-tenant PWA manifest with the customer's assistant name +
	logo (whitelabel, Phase 4). id/scope/start_url stay the built-in values so
	the installed app's identity never changes; only name/short_name/icons vary."""

	def can_render(self) -> bool:
		return self.path == MANIFEST_ROUTE

	def render(self):
		name = (frappe.db.get_single_value("Jarvis Settings", "agent_name") or "").strip() or "Jarvis"
		logo = (frappe.db.get_single_value("Jarvis Settings", "brand_logo") or "").strip()
		# Custom logo first (browser prefers it), defaults kept for guaranteed
		# installability (sized + maskable icons).
		icons = ([{"src": logo, "sizes": "any", "purpose": "any"}] if logo else []) + _DEFAULT_ICONS
		manifest = {
			"id": "/jarvis-mobile",
			"scope": "/jarvis-mobile",
			"start_url": "/jarvis-mobile",
			"name": name,
			"short_name": name,
			"description": "Your AI teammate. Ask for anything across your ERP - in plain language.",
			"display": "standalone",
			"orientation": "portrait",
			"background_color": "#F4F4F5",
			"theme_color": "#F4F4F5",
			"lang": "en",
			"categories": ["business", "productivity"],
			"icons": icons,
		}
		# build_response json-serializes a dict; the header overrides the mimetype
		# to the manifest content type.
		return self.build_response(
			manifest,
			headers={
				"Content-Type": "application/manifest+json",
				"Cache-Control": "no-cache",
			},
		)
