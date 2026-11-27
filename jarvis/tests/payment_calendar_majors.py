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

GATE_NO_V15_READER_PINS = "unsupported_major: no v15 reader pins (until S6)"
GATE_BANK_ONE_PASS_V15 = "the one-pass bank read needs the v15 reader pins (until S6)"
GATE_NO_LAZY_DOCUMENT = "Frappe 15 has no LazyDocument or LazyChildTable"
GATE_NO_COMPANY_RESTRICTION = "ERPNext 15 has no Company Restriction doctype or restrict_to_companies"
GATE_NO_TAX_WITHHOLDING_ENTRY = "ERPNext 15 has no Tax Withholding Entry doctype"
GATE_NO_PARTY_NUMBERS = "ERPNext 15 has no Customer Number At Supplier or Supplier Number At Customer doctype"
GATE_NO_MASKED_FIELDS = "Frappe 15 has no Meta.get_masked_fields"


def v16_only(gate: str):
	"""Skip the test or class on a Frappe major other than 16. `gate` is one of the GATE_* constants."""
	return unittest.skipUnless(MAJOR == "16", gate)


def v15_only(reason: str):
	"""Skip the test or class on a Frappe major other than 15."""
	return unittest.skipUnless(MAJOR == "15", reason)


def interface_error(message: str = "closed"):
	"""A driver interface error of the installed driver: `frappe.db.InterfaceError` where it exists.

	Frappe 15 has no such attribute on `frappe.db`; its MariaDB driver is pymysql.
	"""
	return getattr(frappe.db, "InterfaceError", pymysql.err.InterfaceError)(message)
