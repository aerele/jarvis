"""The payment-gateway return landing pad (jarvis.www.jarvis_pay_return).

Guards a browser rule that is invisible in code review and only shows up with a
real gateway in a real browser.

Frappe sets its session cookie ``samesite="Lax"`` (frappe/auth.py). Lax withholds
the cookie on a CROSS-SITE POST but sends it on a top-level GET. Cashfree returns
from mandate authorisation by POSTing a form to the merchant's return_url, so
aiming that at ``/jarvis/onboarding`` meant the request arrived with no ``sid``:
Frappe saw a Guest, the SPA gate bounced to ``/app``, and a customer who had just
authorised a mandate landed on ``/login?redirect-to=%2Fapp`` with no sign their
payment had happened.

This page absorbs the POST as a Guest and answers 303, which the browser follows
as a same-site GET - and that GET does carry the cookie.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.www import jarvis_pay_return


class TestPayReturnLanding(FrappeTestCase):
	def setUp(self):
		frappe.local.flags.redirect_location = None

	def _redirect(self):
		with self.assertRaises(frappe.Redirect) as ctx:
			jarvis_pay_return.get_context({})
		return ctx.exception, frappe.local.flags.redirect_location

	def test_it_redirects_to_the_wizard(self):
		_exc, location = self._redirect()
		self.assertEqual(location, "/jarvis/onboarding")

	def test_the_status_is_303_not_301(self):
		"""303 is the only status DEFINED to turn a POST into a GET, and the
		conversion is the whole point - a GET is what carries the SameSite=Lax
		cookie. 301 would also work in practice but is cached as permanent, which
		on a payment return URL outlives any future change with no way to
		invalidate it from our side."""
		exc, _location = self._redirect()
		self.assertEqual(exc.http_status_code, 303)
		self.assertNotEqual(exc.http_status_code, 301)

	def test_it_needs_no_session(self):
		"""The entire reason it exists: the cross-site POST arrives WITHOUT the
		session cookie. If this page required a login it would fail exactly where
		it is needed."""
		original = frappe.session.user
		try:
			frappe.set_user("Guest")
			_exc, location = self._redirect()
			self.assertEqual(location, "/jarvis/onboarding")
		finally:
			frappe.set_user(original)

	def test_it_emits_no_cookies_so_the_real_session_survives(self):
		"""The subtle half, and the one that actually logged customers out.

		The cross-site POST arrives with no cookie, so Frappe resolves a Guest
		session and would answer ``Set-Cookie: sid=Guest`` - OVERWRITING the real
		session the browser still holds. The customer would then be genuinely
		logged out rather than merely unrecognised for one request, and the GET
		that follows would carry the Guest sid and land on login anyway.

		Suppressing every cookie on this response is what makes the redirect
		worth doing at all.
		"""

		class _FakeCookieManager:
			def __init__(self):
				self.cookies = {"sid": {"value": "Guest"}, "user_id": {"value": "Guest"}}
				self.to_delete = ["something"]

		cm = _FakeCookieManager()
		original = getattr(frappe.local, "cookie_manager", None)
		frappe.local.cookie_manager = cm
		try:
			self._redirect()
		finally:
			frappe.local.cookie_manager = original

		self.assertEqual(cm.cookies, {}, "a Set-Cookie here overwrites the customer's session")
		self.assertEqual(cm.to_delete, [], "queued deletions clear the cookie just as effectively")

	def test_it_survives_a_missing_cookie_manager(self):
		"""Never let cookie handling be the thing that breaks a payment return."""
		original = getattr(frappe.local, "cookie_manager", None)
		frappe.local.cookie_manager = None
		try:
			_exc, location = self._redirect()
		finally:
			frappe.local.cookie_manager = original
		self.assertEqual(location, "/jarvis/onboarding")

	def test_it_carries_no_gateway_state_into_the_wizard(self):
		"""The gateway's cf_status is unverified client input and confirmation is
		a server-side fetch regardless, so nothing from the POST body is trusted
		or forwarded - the target is a bare route."""
		_exc, location = self._redirect()
		self.assertNotIn("?", location)
		self.assertNotIn("cf_", location)
