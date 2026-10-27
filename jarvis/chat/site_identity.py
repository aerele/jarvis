"""An instance id that lives outside the database.

A restored database copy keeps every row but not the files beside it, so the id sits
in ``site_config.json`` (32 lowercase hex). A copy that carries the config file too
is caught by the site pin instead; this id catches the DB-only restore.
"""

import re
import secrets

import frappe

_KEY = "jarvis_instance_id"
_LOCK = "jarvis:instance_id_lock"
_VALID = re.compile(r"[0-9a-f]{32}")


def _valid(value) -> bool:
	return isinstance(value, str) and bool(_VALID.fullmatch(value))


def instance_id() -> str:
	current = frappe.conf.get(_KEY)
	if _valid(current):
		return current
	with frappe.cache.lock(_LOCK, timeout=10):
		# frappe.conf can be stale in this process, so re-read the file under the lock.
		current = frappe.get_site_config().get(_KEY)
		if _valid(current):
			return current
		from frappe.installer import update_site_config

		current = secrets.token_hex(16)
		update_site_config(_KEY, current)
		frappe.conf[_KEY] = current
		return current
