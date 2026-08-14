import frappe

from jarvis.permissions import has_jarvis_access

no_cache = 1


def get_context(context):
	"""Landing page for a signed-in user who lacks Jarvis access — the redirect
	target of www/jarvis.py and www/jarvis_mobile.py's gates (and the desk's
	floating Jarvis button once it reads bootinfo.jarvis_has_access). Read-only:
	no frappe.db.commit()."""
	if frappe.session.user == "Guest":
		# Not reachable via the two www gates above (they keep Guests on the
		# /app -> login bounce), but a Guest can still land here directly.
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	if has_jarvis_access():
		# Self-heals: access may have been granted after the link that sent the
		# user here was generated (a stale bookmark, or an admin who granted
		# the role mid-session), so don't strand an already-authorized user.
		frappe.local.flags.redirect_location = "/jarvis"
		raise frappe.Redirect

	context.user_fullname = frappe.utils.get_fullname(frappe.session.user)

	# Whitelabel branding: same lookup as www/jarvis.py and www/jarvis_mobile.py,
	# so a white-label tenant doesn't leak the underlying "Jarvis" brand on this
	# gate page. Blank => the template falls back to "Jarvis".
	_brand = (
		frappe.get_cached_value(
			"Jarvis Settings",
			"Jarvis Settings",
			["agent_name"],
			as_dict=True,
		)
		or {}
	)
	context.agent_name = _brand.get("agent_name") or "Jarvis"
	return context
