"""Mirror the operator's fleet-wide announcement locally and expose it to the SPA.

The control plane composes one announcement and ships it on the connection poll
(``conn["announcement"]``); the bench stores it in a read-only Jarvis Settings
mirror and renders it as a soft, dismissible banner. Unlike the release notice
this is informational only - there is no gate, no version self-clear, no
on-demand re-check: the mirror refreshes on the daily sync + the chat gate's
persist sites + boot, and the banner's re-show cadence lives per-device in the
browser (see frontend/src/announcementNudge.js).
"""

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from jarvis import compat

SETTINGS = "Jarvis Settings"
_FIELDS = (
	"announcement_active",
	"announcement_id",
	"announcement_title",
	"announcement_message",
	"announcement_severity",
	"announcement_link_url",
	"announcement_link_label",
	"announcement_interval_days",
	"announcement_expires_on",
)


def _norm(field: str, value) -> str:
	"""Comparable string form for the no-op-skip. The Datetime field is routed
	through get_datetime so a stored ``datetime`` and the wire's string form of
	the same instant compare equal - otherwise a set expiry would force a write
	on every persist (H3). Everything else compares by its plain string."""
	if field == "announcement_expires_on":
		if not value:
			return ""
		try:
			return str(get_datetime(value))
		except Exception:
			return str(value)
	return str(value if value is not None else "")


def persist(announcement: dict) -> None:
	"""Mirror the admin-sent announcement onto Jarvis Settings. Best-effort; an
	empty dict clears it. Skips the write when nothing changed - the banner
	re-shows on a per-device timer, and churning ``modified`` would collide with
	an operator editing the Settings form."""
	try:
		n = announcement or {}
		fresh = {
			"announcement_active": 1 if n.get("active") else 0,
			"announcement_id": n.get("id") or "",
			"announcement_title": n.get("title") or "",
			"announcement_message": n.get("message") or "",
			"announcement_severity": n.get("severity") or "",
			"announcement_link_url": n.get("link_url") or "",
			"announcement_link_label": n.get("link_label") or "",
			"announcement_interval_days": cint(n.get("interval_days")),
			# A Datetime column rejects "" (and would leave the channel dead), so an
			# absent/blank expiry is stored as NULL, never "" (H2).
			"announcement_expires_on": n.get("expires_on") or None,
		}
		current = compat.single_values(SETTINGS, _FIELDS)
		if all(_norm(k, current.get(k)) == _norm(k, v) for k, v in fresh.items()):
			return
		frappe.db.set_single_value(SETTINGS, fresh, update_modified=False)
	except Exception:
		pass


def boot_payload() -> dict:
	"""``announcement`` for context.boot. The SPA/PWA read ``active`` to decide
	whether to show the soft banner; ``id`` + ``interval_days`` key the per-device
	snooze. TOTAL - the expiry compare is wrapped so a bad ``expires_on`` can never
	500 the bare boot call (H1); a strict ``now < expires_on`` boundary matches the
	control plane resolver (H4). Fails toward showing, like the release notice."""
	row = frappe.get_cached_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
	active = cint(row.get("announcement_active"))
	expires_on = row.get("announcement_expires_on")
	if active and expires_on:
		try:
			if now_datetime() >= get_datetime(expires_on):
				active = 0
		except Exception:
			# A garbage/unparseable expiry must not raise (it would break every boot)
			# - keep the stored active, matching release's fail-toward-showing.
			pass
	return {
		"active": bool(active),
		"id": row.get("announcement_id") or "",
		"title": row.get("announcement_title") or "",
		"message": row.get("announcement_message") or "",
		"severity": row.get("announcement_severity") or "",
		"link_url": row.get("announcement_link_url") or "",
		"link_label": row.get("announcement_link_label") or "",
		"interval_days": cint(row.get("announcement_interval_days")),
		# Serialized so context.boot stays plain-JSON (a LocalProxy/datetime can trip
		# the |tojson boot emitter); the client never reads it - boot already gated
		# active by the expiry above.
		"expires_on": str(expires_on) if expires_on else "",
	}
