"""Tests for the mobile PWA's service-worker plumbing.

The PWA is only a PWA if its service worker actually controls the app, and that
rests on one fragile arrangement (see ``jarvis/pwa.py``):

- **The worker is reachable at the site root.** Frappe's StaticPage renderer
  refuses to serve ``.js`` out of ``www/``, so a custom page renderer serves it
  at ``/jarvis-mobile.sw.js``. A worker may only claim a scope at or below the
  path it is served from, so this root-level path is what lets it claim
  ``/jarvis-mobile`` — a worker shipped under ``/assets/jarvis/pwa/`` could only
  ever control ``/assets/jarvis/pwa/``.
- **The app's catch-all must not swallow it.** ``website_route_rules`` sends
  ``/jarvis-mobile/<path:app_path>`` to the app shell. Had the worker been
  exposed at ``/jarvis-mobile/sw.js`` it would resolve to that rule and be
  served HTML — registration would die on a MIME error and the PWA would
  silently stop being installable. The renderer sees the endpoint AFTER route
  rules are applied, so the only defence is that the two paths cannot collide.
- **The scope is narrow.** The header must grant ``/jarvis-mobile`` and nothing
  above it; a worker scoped to ``/`` would control the Desk.

These are exactly the invariants a well-meaning refactor of the route rules
would break without any visible error, so they are pinned here.

NOTE ON THE BUILD: CI runs ``--skip-assets`` and never builds the frontends, and
``jarvis/public/pwa/`` is generated output that is not committed. So nothing here
may depend on a built bundle existing: the renderer tests point ``_sw_path`` at a
fixture instead, and the one test that reads the real artifact skips when it is
absent. The invariant that artifact would prove (absolute precache URLs) is
pinned against the vite config, which IS committed and is what a refactor would
actually touch.
"""

import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request
from frappe.website.path_resolver import resolve_path

from jarvis.pwa import SW_ROUTE, SW_SCOPE, ServiceWorkerRenderer, _sw_path
from jarvis.www.jarvis_mobile import get_context

# Stands in for the built worker. Content is irrelevant to the renderer — it
# serves bytes — so this only has to be recognisable in an assertion.
FIXTURE_SW = b"/* built worker */ self.skipWaiting()\n"


def _resolve(path: str) -> str:
	"""Resolve a path the way a real HTTP request would.

	Route rules are ONLY applied when a request exists — ``evaluate_dynamic_routes``
	returns None otherwise, and ``resolve_from_map`` then hands the path straight
	back. Resolving without a request would make every assertion below pass
	vacuously, which is precisely the bug this helper exists to prevent.
	"""
	set_request(method="GET", path=f"/{path}")
	return resolve_path(path)


def _app_root() -> str:
	"""Repo root of the app (the parent of the python package), where pwa/ lives."""
	return os.path.dirname(frappe.get_app_path("jarvis"))


class TestServiceWorkerRoute(FrappeTestCase):
	"""Route resolution — needs no build."""

	def test_sw_route_is_not_swallowed_by_the_app_catch_all(self):
		"""The worker path must survive route resolution untouched.

		If this fails, /jarvis-mobile.sw.js is being rewritten to the app shell,
		the browser gets HTML where it expects JavaScript, registration dies on a
		MIME error, and the app quietly stops being installable.
		"""
		self.assertEqual(_resolve(SW_ROUTE), SW_ROUTE)

	def test_app_routes_still_resolve_to_the_shell(self):
		"""The flip side: the bare route and deep links DO reach the shell, so a
		refresh on /jarvis-mobile/c/<id> renders the app instead of 404ing."""
		self.assertEqual(_resolve("jarvis-mobile"), "jarvis_mobile")
		self.assertEqual(_resolve("jarvis-mobile/c/abc123"), "jarvis_mobile")

	def test_desk_and_desktop_spa_are_untouched(self):
		"""The renderer runs on every website request and the app owns two
		catch-alls; neither may reach across into the Desk or the /jarvis SPA."""
		self.assertEqual(_resolve("jarvis/skills"), "jarvis")
		self.assertEqual(_resolve("app"), "app")


class TestServiceWorkerRenderer(FrappeTestCase):
	"""How the worker is served. Points _sw_path at a fixture so these run on an
	unbuilt checkout (i.e. in CI) and still exercise the real renderer."""

	def setUp(self):
		fd, self.sw_file = tempfile.mkstemp(suffix="-sw.js")
		with os.fdopen(fd, "wb") as f:
			f.write(FIXTURE_SW)
		self._patch = patch("jarvis.pwa._sw_path", return_value=self.sw_file)
		self._patch.start()
		self.addCleanup(self._patch.stop)
		self.addCleanup(os.unlink, self.sw_file)

	def test_renderer_claims_only_the_worker_path(self):
		"""can_render() runs on every website request; it must match one path."""
		self.assertTrue(ServiceWorkerRenderer(SW_ROUTE).can_render())
		for path in ("app", "jarvis", "jarvis-mobile", "jarvis-mobile/c/abc"):
			self.assertFalse(
				ServiceWorkerRenderer(path).can_render(),
				f"renderer must not claim {path!r}",
			)

	def test_worker_is_served_as_javascript_scoped_to_the_app(self):
		"""A worker served as text/html is rejected outright, and one allowed a
		scope above /jarvis-mobile could control the Desk."""
		response = ServiceWorkerRenderer(SW_ROUTE).render()

		self.assertEqual(response.status_code, 200)
		self.assertIn("javascript", response.headers["Content-Type"])
		self.assertEqual(response.headers["Service-Worker-Allowed"], SW_SCOPE)
		self.assertEqual(SW_SCOPE, "/jarvis-mobile")
		# Never stale: a cached worker pins users to an old precache manifest.
		self.assertIn("no-store", response.headers["Cache-Control"])
		self.assertEqual(response.get_data(), FIXTURE_SW)


class TestUnbuiltCheckout(FrappeTestCase):
	def test_missing_bundle_declines_instead_of_exploding(self):
		"""On a checkout where the PWA was never built (CI runs --skip-assets),
		the renderer must decline the route so it 404s — an eager can_render()
		would hand the request to a render() that raises FileNotFoundError, i.e.
		a 500 on a site that simply has no PWA built."""
		with patch("jarvis.pwa._sw_path", return_value="/nonexistent/sw.js"):
			self.assertFalse(ServiceWorkerRenderer(SW_ROUTE).can_render())


class TestPrecacheUrlsAreAbsolute(FrappeTestCase):
	"""Workbox resolves precache URLs against wherever the WORKER is served from —
	the site root here, not the bundle directory. Relative entries would resolve
	to /assets/index-*.js and 404, so the build must emit them absolute."""

	def test_vite_config_pins_the_precache_prefix(self):
		"""The committed source of truth, so this runs even unbuilt. Dropping
		modifyURLPrefix is the refactor that would silently break precaching."""
		config = frappe.read_file(os.path.join(_app_root(), "pwa", "vite.config.js"))
		self.assertIsNotNone(config, "pwa/vite.config.js is missing")
		self.assertIn("modifyURLPrefix", config)
		self.assertIn('"": "/assets/jarvis/pwa/"', config)

	def test_built_worker_precaches_absolute_urls(self):
		"""And when the bundle IS built, the emitted worker must actually carry
		absolute URLs. Skipped on an unbuilt checkout rather than failing."""
		if not os.path.isfile(_sw_path()):
			self.skipTest("PWA not built (CI runs --skip-assets); vite config is asserted above")

		sw = frappe.read_file(_sw_path())
		self.assertIn('"/assets/jarvis/pwa/offline.html"', sw)
		self.assertNotIn('"offline.html"', sw)


class TestGuestReachesTheAppsOwnLogin(FrappeTestCase):
	"""The shell must render for a Guest.

	Not cosmetic: the manifest scope is /jarvis-mobile, and a standalone PWA that
	navigates OUTSIDE its scope is handed off to the browser. Redirecting a guest
	to the Desk's /login therefore ejected the user from the installed app into a
	Chrome tab. Guests get the shell; the router shows the app's own Login route.
	"""

	def setUp(self):
		self._user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._user)

	def test_guest_gets_the_shell_not_a_redirect(self):
		frappe.set_user("Guest")
		context = frappe._dict()
		# A redirect out of scope is the bug; get_context must simply return.
		get_context(context)
		self.assertEqual(context.boot["frappe_user_id"], "Guest")

	def test_signed_in_user_without_access_goes_to_the_no_access_page(self):
		"""They already have a session — a login form would be nonsense. The
		branded /jarvis-no-access page is where a non-Jarvis user belongs: it
		explains what Jarvis is, offers the "ask your admin" CTA, and links
		back to the Desk (the pre-no-access-page behavior was a bare /app
		bounce)."""
		frappe.set_user("Administrator")
		with patch("jarvis.www.jarvis_mobile.has_jarvis_access", return_value=False):
			with self.assertRaises(frappe.Redirect):
				get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/jarvis-no-access")

	def test_user_with_access_gets_the_shell(self):
		frappe.set_user("Administrator")
		with patch("jarvis.www.jarvis_mobile.has_jarvis_access", return_value=True):
			context = frappe._dict()
			get_context(context)
		self.assertEqual(context.boot["frappe_user_id"], "Administrator")
		self.assertEqual(context.boot["default_route"], "/jarvis-mobile")
