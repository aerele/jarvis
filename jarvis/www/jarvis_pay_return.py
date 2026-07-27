"""Landing pad for a payment gateway's redirect back into the wizard.

Exists because of one browser rule. Frappe sets its session cookie with
``samesite="Lax"`` (frappe/auth.py), and Lax withholds the cookie on a
CROSS-SITE POST while still sending it on a top-level GET. Cashfree returns
from mandate authorisation by POSTing a form to the merchant's return_url - so
pointing that straight at ``/jarvis/onboarding`` meant the request arrived with
no ``sid``. Frappe saw a Guest, the SPA's access gate bounced it to ``/app``, and
the customer who had just authorised a mandate landed on
``/login?redirect-to=%2Fapp`` with no sign their payment had happened.

This page absorbs that POST as a Guest (it needs no session, and reads nothing
it would have to trust) and answers with a redirect. The browser follows it as a
top-level GET to the same site, Lax lets the cookie through, and the wizard
renders for the logged-in customer exactly as it would have.

Deliberately NOT carrying the gateway's status through: it is unverified client
input, and confirmation is a server-side fetch either way. The wizard resumes by
asking admin what actually happened (reconcileMidFlightSignup -> finish_payment),
which is the only account of the payment we trust.
"""

import frappe

no_cache = 1

# Where the customer belongs afterwards: the wizard's own route. Its normal
# gates still apply from here - a Guest still goes to login, a user without
# access still goes to /jarvis-no-access.
_WIZARD_ROUTE = "/jarvis/onboarding"


def _keep_the_browsers_session_cookie():
	"""Emit NO Set-Cookie on this response.

	Without this the page makes the problem worse rather than better. The
	cross-site POST arrives with no cookie, so Frappe resolves a Guest session -
	and then answers with ``Set-Cookie: sid=Guest``, which OVERWRITES the real
	session cookie the browser still holds. The customer is not merely treated as
	logged out for one request; they are actually logged out, and the following
	GET carries the Guest sid too, so the redirect lands on the login page all
	the same.

	CookieManager collects pending cookies in a plain dict and writes them at
	response time, so clearing it here leaves the browser's existing ``sid``
	untouched. Safe on this page specifically because it only redirects: it has
	no session of its own to persist and nothing to remember.
	"""
	cm = getattr(frappe.local, "cookie_manager", None)
	if not cm:
		return
	cm.cookies.clear()
	# to_delete would emit expiry headers for the same cookies, which clears them
	# in the browser just as effectively.
	if getattr(cm, "to_delete", None):
		cm.to_delete.clear()


def get_context(context):
	_keep_the_browsers_session_cookie()
	# 303, explicitly, rather than Frappe's default 301. Two reasons, both
	# load-bearing:
	#
	#   - 303 is the one status DEFINED to turn a POST into a GET. Browsers do
	#     convert on 301/302 too, but by convention rather than by spec, and the
	#     conversion is the entire point here: only a GET carries the
	#     SameSite=Lax session cookie.
	#   - 301 means "permanently moved" and browsers cache it. Caching a
	#     permanent redirect on a payment return URL is a trap - it would outlive
	#     any future change to where this lands, with no way to invalidate it
	#     from our side.
	frappe.local.flags.redirect_location = _WIZARD_ROUTE
	raise frappe.Redirect(http_status_code=303)
