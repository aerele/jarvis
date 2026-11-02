"""Database isolation fixture for approval backports."""

from contextlib import contextmanager

import frappe


@contextmanager
def repeatable_read():
	"""Arm a repeatable snapshot even on sites configured as READ COMMITTED."""
	levels = {
		"READ-UNCOMMITTED": "READ UNCOMMITTED",
		"READ-COMMITTED": "READ COMMITTED",
		"REPEATABLE-READ": "REPEATABLE READ",
		"SERIALIZABLE": "SERIALIZABLE",
	}
	previous = levels[frappe.db.sql("SELECT @@session.tx_isolation")[0][0]]
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	frappe.db.commit()
	try:
		yield
	finally:
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL " + previous)
		frappe.db.commit()
