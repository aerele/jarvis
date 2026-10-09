"""Test helper: one frappe.db.set_value fails with a write conflict (1020)."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe


@contextmanager
def set_value_conflict(doctype: str, field: str):
	real = frappe.db.set_value

	def set_value(dt, name, fieldname=None, *args, **kwargs):
		if dt == doctype and fieldname == field:
			raise frappe.QueryDeadlockError("1020 record has changed")
		return real(dt, name, fieldname, *args, **kwargs)

	with patch.object(frappe.db, "set_value", side_effect=set_value):
		yield
