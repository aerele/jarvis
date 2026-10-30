"""The Desk boot key for the holiday touch.

Frappe caches the ``boot_session`` blob per user but calls ``extend_bootinfo`` after reading
that cache, on every Desk load. The answer depends on the date and on switches an admin can
flip, so it must ride this hook and not ``boot_session`` (a cached copy would go stale).

Gated on ``bootinfo.jarvis_has_access``, which ``boot_session`` already set from the same
cache: a user without Jarvis access costs no query here. An exception in an
``extend_bootinfo`` hook fails the whole Desk load, so this one never raises.
"""

from __future__ import annotations

import frappe

KEY = "jarvis_holiday_touch"


def extend_bootinfo(bootinfo) -> None:
	"""Put today's answer for the session user on ``bootinfo``; leave it alone on any fault."""
	try:
		if not bootinfo.get("jarvis_has_access") or frappe.session.user == "Guest":
			return
		from jarvis.holiday_touch import resolve

		bootinfo[KEY] = resolve.safe_answer(frappe.session.user)
	except Exception:
		_report()


def _report() -> None:
	try:
		from jarvis.holiday_touch.report import report

		report("boot", "Holiday touch: Desk boot key failed")
	except Exception:
		pass  # even the logger is broken: Desk must still load
