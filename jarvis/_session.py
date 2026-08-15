"""Session-safe user impersonation for HTTP request paths.

``frappe.set_user(x)`` is only safe in background/worker context. Inside a
cookie-session HTTP request it GUTS the live session object:

    local.session.user = username
    local.session.sid  = username   # sid becomes the username string
    local.session.data = _dict()     # csrf_token, session_expiry, inner user - all wiped

``frappe.local.session`` IS ``frappe.local.session_obj.data`` (same object), so
after such a switch the session object carries a bogus sid and an empty data
dict. At the end of the request Frappe schedules ``session_obj.update()``
(frappe/app.py sync_database -> after_response), which does
``frappe.cache.hset("session", self.sid, self.data)`` keyed on ``self.sid`` -
the ``__slots__`` attribute that STILL holds the browser's REAL sid. That
overwrites the real session's Redis cache entry with the gutted dict. Because
session resume reads cache before DB, the next request resolves the real sid to
``user = None`` -> demoted to Guest -> the user is silently logged out.

A plain ``frappe.set_user(orig)`` in a ``finally`` does NOT undo this: it
restores only the user NAME, leaving ``sid`` and ``data`` wiped.

``impersonate`` is the safe seam: it snapshots and restores ``sid`` and
``data`` (which ``frappe.set_user`` REPLACES rather than mutates, so the saved
reference stays intact), and is a true no-op when there is nothing to switch to
or the target is already the current user.
"""

from contextlib import contextmanager

import frappe

# The pre-impersonation (authenticated) session user for this request/job, parked
# on frappe.local so it dies with the request. Set by ``impersonate`` only.
_AUTH_USER_ATTR = "jarvis_authenticated_user"


def authenticated_user() -> str:
	"""The user this request/job AUTHENTICATED as - the session user BEFORE any
	:func:`impersonate` switch (the OUTERMOST one, when switches nest).

	Provenance must never be read from ambient session state after an identity
	switch: inside ``impersonate`` ``frappe.session.user`` is the impersonated
	identity, so stamping it records the wrong human. Callers that record WHO
	acted read it from here instead.

	Falls back to the live session user when nothing is impersonating, so a plain
	request path (or a background job) answers the same question correctly. Only
	:func:`impersonate` maintains the marker - a bare ``frappe.set_user`` switch
	is invisible to it, which is the other reason HTTP paths must use this seam.
	"""
	return getattr(frappe.local, _AUTH_USER_ATTR, None) or frappe.session.user


@contextmanager
def impersonate(user: str | None):
	"""Run the enclosed block AS ``user``, then restore the caller's session.

	No-op (yields without touching the session) when ``user`` is falsy or
	already the current session user - so callers can drop their own
	``if switch_to:`` guards and route unconditionally through here.

	Otherwise snapshots ``frappe.session.user``/``sid``/``data``, switches with
	``frappe.set_user`` (which rebuilds perms + caches for the target), and in a
	``finally`` restores the user via ``frappe.set_user`` then puts the original
	``sid`` and ``data`` back - keeping the browser's real cookie session intact
	so end-of-request ``Session.update`` re-persists the RIGHT session.
	"""
	if not user or user == frappe.session.user:
		yield
		return

	orig_user = frappe.session.user
	orig_sid = frappe.session.sid
	# frappe.set_user REPLACES local.session.data with a fresh dict rather than
	# mutating it, so this reference to the original inner dict stays valid.
	orig_data = frappe.session.data
	# Remember who the request was authenticated as, for authenticated_user(). The
	# OUTERMOST switch wins (nested impersonation must not re-anchor the identity on
	# an already-impersonated user), and the previous marker - not None - is what the
	# finally restores, so unwinding a nested switch leaves the outer one intact.
	outer_auth = getattr(frappe.local, _AUTH_USER_ATTR, None)
	# set_user is inside the try so a mid-switch failure still restores the
	# caller's session in the finally (never leave it gutted / half-switched).
	try:
		setattr(frappe.local, _AUTH_USER_ATTR, outer_auth or orig_user)
		frappe.set_user(user)
		yield
	finally:
		setattr(frappe.local, _AUTH_USER_ATTR, outer_auth)
		frappe.set_user(orig_user)
		frappe.local.session.sid = orig_sid
		frappe.local.session.data = orig_data
