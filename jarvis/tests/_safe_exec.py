"""A restoring drop-in for frappe's ``enable_safe_exec`` test helper.

Frappe's ``frappe.tests.classes.context_managers.enable_safe_exec`` sets
``server_script_enabled`` to 1 in the bench-wide ``common_site_config.json`` on
entry and unconditionally writes 0 on exit. On a developer bench that also hosts
real sites, every test run therefore switches server scripts OFF for every site
on the bench (Script triggers, Server Scripts) until someone re-enables them.

This version enables safe exec the same way but, on exit, restores whatever was
in the file before: the previous value if the key existed, or no key at all if
it did not. Use it everywhere a Jarvis test needs real server-script execution.
"""

import json
import os
from contextlib import contextmanager

import frappe
from frappe.installer import update_site_config
from frappe.utils.safe_exec import SAFE_EXEC_CONFIG_KEY

_MISSING = object()


def _common_conf_path() -> str:
	return os.path.join(frappe.local.sites_path, "common_site_config.json")


def _read_raw(path: str):
	"""The key's literal value in the file (no env overrides), or _MISSING."""
	try:
		with open(path) as f:
			return json.load(f).get(SAFE_EXEC_CONFIG_KEY, _MISSING)
	except (OSError, ValueError):
		return _MISSING


@contextmanager
def enable_safe_exec():
	"""Temporarily enable server scripts; restore the previous setting on exit."""
	conf = _common_conf_path()
	previous = _read_raw(conf)
	update_site_config(SAFE_EXEC_CONFIG_KEY, 1, validate=False, site_config_path=conf)
	try:
		yield
	finally:
		# "None" is update_site_config's sentinel for deleting the key.
		restore = "None" if previous is _MISSING else previous
		update_site_config(SAFE_EXEC_CONFIG_KEY, restore, validate=False, site_config_path=conf)
