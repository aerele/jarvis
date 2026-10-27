"""One site per tenant: pin the agent to the site it was first connected from.

A restored copy of a customer's site carries the same agent URL and tokens, so
without a pin it could drive that customer's agent. The pin is the folder name
of the site (``frappe.local.site``), never a header, payload or hostname, and
the check needs no HTTP request so jobs and workers can run it too.

Flags are written with ``set_single_value``: ``Jarvis Settings.save()`` would
sync to the remote admin. Secrets are never wiped on a block.
"""

import frappe

SETTINGS = "Jarvis Settings"


def copy_message(paired_site: str) -> str:
	return (
		f"This site is a copy of {paired_site}. Jarvis is turned off here. "
		"Contact support to use Jarvis on this site."
	)


def _get(field: str) -> str:
	return (frappe.db.get_single_value(SETTINGS, field) or "").strip()


def status() -> dict:
	paired_site = _get("paired_site")
	current_site = (frappe.local.site or "").strip()
	blocked = bool(frappe.db.get_single_value(SETTINGS, "pairing_blocked")) or bool(paired_site and paired_site != current_site)
	return {
		"blocked": blocked,
		"paired_site": paired_site,
		"current_site": current_site,
		"reason": _get("pairing_blocked_reason"),
	}


def pin_if_unset() -> bool:
	"""Stamp this site as the paired site when no pin exists yet."""
	if _get("paired_site"):
		return False
	frappe.db.set_single_value(SETTINGS, "paired_site", frappe.local.site, update_modified=False)
	return True


def assert_not_copy(*, admin: bool = False) -> None:
	st = status()
	if not st["blocked"]:
		return
	if not st["reason"]:
		frappe.db.set_single_value(
			SETTINGS,
			{
				"pairing_blocked": 1,
				"pairing_blocked_reason": copy_message(st["paired_site"]),
			},
			update_modified=False,
		)
	from jarvis.exceptions import AdminSiteIsCopyError, SiteIsCopyError

	raise (AdminSiteIsCopyError if admin else SiteIsCopyError)(st["paired_site"])


def clear_pairing_block() -> dict:
	"""Support tool, not whitelisted:
	``bench --site <site> execute jarvis.chat.site_pin.clear_pairing_block``."""
	frappe.db.set_single_value(
		SETTINGS,
		{"pairing_blocked": 0, "pairing_blocked_reason": "", "paired_site": ""},
		update_modified=False,
	)
	return status()
