"""Frappe-major gates for the Payment Calendar tests.

The Payment Calendar code ships on version-15 with the reader switched off (nothing serves a read there until
the v15 reader pins exist). A test that needs a Frappe 16 object, or a served reader read, is skipped on 15
through `v16_only` with one of the fixed gate constants below, so every skip names its reason in one place.
`v15_only` is for the tests that pin what a version-15 site does today.
"""

import unittest

import frappe
import pymysql

# The installed Frappe major (the ERPNext major is the same on every supported pair).
MAJOR = frappe.__version__.split(".")[0]
# True on Frappe 15: the only branch that is ever permissive. Any other major runs the test or fails loudly.
IS_V15 = MAJOR == "15"

GATE_NO_V15_READER_PINS = "unsupported_major: no v15 reader pins (until S6)"
GATE_BANK_ONE_PASS_V15 = "the one-pass bank read needs the v15 reader pins (until S6)"
GATE_NO_V15_PERMISSION_PINS = "no v15 batch permission pins: _PERMISSION_PINS has only a 16 entry"
GATE_NO_LAZY_DOCUMENT = "Frappe 15 has no LazyDocument or LazyChildTable"
GATE_NO_MASKED_FIELDS = "Frappe 15 has no Meta.get_masked_fields"
GATE_NO_DOCUMENT_TABLE_FIELDNAMES = "Frappe 15 Document has no _table_fieldnames cached property"
GATE_NO_META_CURRENCY_PRECISION = "Frappe 15 has no frappe.model.meta.get_currency_precision"


def v16_only(gate: str):
	"""Skip the test or class on Frappe 15. `gate` is one of the GATE_* constants. Any other major runs it."""
	return unittest.skipIf(IS_V15, gate)


def v15_only(reason: str):
	"""Skip the test or class on a Frappe major other than 15."""
	return unittest.skipUnless(IS_V15, reason)


def interface_error(message: str = "closed"):
	"""A driver interface error of the installed driver: `frappe.db.InterfaceError` where it exists.

	Frappe 15 has no such attribute on `frappe.db`; its MariaDB driver is pymysql. Only there does the pymysql class
	stand in: on any other major a missing attribute is a failure, not a fallback.
	"""
	if IS_V15:
		return getattr(frappe.db, "InterfaceError", pymysql.err.InterfaceError)(message)
	return frappe.db.InterfaceError(message)
