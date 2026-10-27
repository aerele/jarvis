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
		f"This site is a copy of {paired_site or 'another site'}. Jarvis is turned off here. "
		"Contact support to use Jarvis on this site."
	)


_PIN_FIELDS = ("paired_site", "pairing_blocked", "pairing_blocked_reason")
_read_failure_logged = False


def _read_pin() -> dict:
	"""The three pin values in ONE query on ``tabSingles``. Deliberately not
	``get_single_value``: that validates the field against the DocType meta and
	raises on a site where the fields are not migrated yet."""
	singles = frappe.qb.DocType("Singles")
	rows = (
		frappe.qb.from_(singles)
		.select(singles.field, singles.value)
		.where(singles.doctype == SETTINGS)
		.where(singles.field.isin(_PIN_FIELDS))
		.run()
	)
	return {field: value for field, value in rows}


def _log_read_failure() -> None:
	"""Once per process: a broken pin read must be visible without flooding."""
	global _read_failure_logged
	if _read_failure_logged:
		return
	_read_failure_logged = True
	try:
		frappe.log_error(title="site_pin: status read failed")
	except Exception:
		pass


def _current_site() -> str:
	"""Empty when there is no site context (a worker thread, a bare
	``frappe.local``). Never raises."""
	try:
		return (getattr(frappe.local, "site", None) or "").strip()
	except Exception:
		return ""


def status() -> dict:
	"""Fails OPEN: any error reading the pin reports not blocked, so an
	infrastructure fault can never lock the real site out."""
	current_site = _current_site()
	if not current_site:
		return {"blocked": False, "paired_site": "", "current_site": "", "reason": ""}
	try:
		raw = _read_pin()
		paired_site = (raw.get("paired_site") or "").strip()
		flag = str(raw.get("pairing_blocked") or "0").strip().lower() in ("1", "true")
		reason = (raw.get("pairing_blocked_reason") or "").strip()
	except Exception:
		_log_read_failure()
		return {"blocked": False, "paired_site": "", "current_site": current_site, "reason": ""}
	return {
		"blocked": flag or bool(paired_site and paired_site != current_site),
		"paired_site": paired_site,
		"current_site": current_site,
		"reason": reason,
	}


def pin_if_unset() -> bool:
	"""Stamp this site as the paired site when no pin exists yet. A failed read
	pins nothing."""
	current_site = _current_site()
	if not current_site:
		return False
	try:
		if (_read_pin().get("paired_site") or "").strip():
			return False
	except Exception:
		_log_read_failure()
		return False
	frappe.db.set_single_value(SETTINGS, "paired_site", current_site, update_modified=False)
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
	``bench --site <site> execute jarvis.chat.site_pin.clear_pairing_block``.
	Returns the new status plus what it cleared."""
	before = status()
	frappe.db.set_single_value(
		SETTINGS,
		{"pairing_blocked": 0, "pairing_blocked_reason": "", "paired_site": ""},
		update_modified=False,
	)
	return {
		**status(),
		"cleared_paired_site": before["paired_site"],
		"cleared_reason": before["reason"],
	}
