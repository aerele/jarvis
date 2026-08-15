"""Regression tests for the permlevel-leak finding group (F1, F3, F7, F8, F9,
F10, F22 - see .superpowers/sdd/audit-findings.md).

All six mutating/read tools (get_doc, create_doc, update_doc, submit_doc,
cancel_doc, amend_doc) returned ``doc.as_dict()`` without first calling
``doc.apply_fieldlevel_read_permissions()``, so a permlevel-restricted field
on the doc travelled straight back to a caller who lacks that permlevel role
- even though ``frappe.has_permission(..., ptype=...)`` only gates the
document as a whole, not individual fields. preview_doc has the same bug
class structurally: ``_summarize()`` echoes header field values with no
permlevel filter.

Fixture: one custom submittable DocType (``JV Permlevel Test``) with a
permlevel-0 field and a permlevel-1 field (default value = SECRET_VALUE, so
every fresh/copied doc carries it automatically). Two System Users:

- ``USER_RESTRICTED`` holds a role with full permlevel-0 CRUD + submit/
  cancel/amend, but no permlevel-1 grant at all.
- ``USER_PRIVILEGED`` holds a role with the same permlevel-0 grants *plus*
  permlevel-1 read/write.

Each test proves the restricted user's tool call does NOT surface the
restricted field's real value (``apply_fieldlevel_read_permissions()``
nulls it out, though the schema-defined key still appears in ``as_dict()``),
while the privileged user's identical call does - so the fix strips the
leak without breaking legitimate access.
"""

from __future__ import annotations

import contextlib

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype
from frappe.tests.utils import FrappeTestCase

from jarvis.tools.amend_doc import amend_doc
from jarvis.tools.cancel_doc import cancel_doc
from jarvis.tools.create_doc import create_doc
from jarvis.tools.get_doc import get_doc
from jarvis.tools.preview_doc import preview_doc
from jarvis.tools.submit_doc import submit_doc
from jarvis.tools.update_doc import update_doc

DT_NAME = "JV Permlevel Test"
FIELD_PUBLIC = "public_field"
FIELD_RESTRICTED = "restricted_field"
FIELD_TOTAL = "grand_total"
ROLE_BASE = "JPL Permlevel Base Role"
ROLE_PRIV = "JPL Permlevel Priv Role"
USER_RESTRICTED = "jpl-permlevel-restricted@example.com"
USER_PRIVILEGED = "jpl-permlevel-privileged@example.com"
SECRET_VALUE = "top-secret-value"
SECRET_TOTAL = 918273.0


def _ensure_role(name: str) -> None:
	if not frappe.db.exists("Role", name):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def _ensure_user(email: str, roles: tuple) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if user.user_type != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
		user = frappe.get_doc("User", email)
	if "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")
	missing = [r for r in roles if r not in frappe.get_roles(email)]
	if missing:
		user.add_roles(*missing)


def _ensure_doctype() -> None:
	# Recreate on every run rather than skip-if-exists: this fixture's shape
	# is authoritative for this test module, and a stale schema from an
	# earlier iteration would silently invalidate the assertions below.
	if frappe.db.exists("DocType", DT_NAME):
		frappe.delete_doc("DocType", DT_NAME, force=True, ignore_permissions=True)
	new_doctype(
		name=DT_NAME,
		custom=1,
		is_submittable=1,
		fields=[
			{"label": "Public Field", "fieldname": FIELD_PUBLIC, "fieldtype": "Data", "permlevel": 0},
			{
				"label": "Restricted Field",
				"fieldname": FIELD_RESTRICTED,
				"fieldtype": "Data",
				"permlevel": 1,
				"default": SECRET_VALUE,
			},
			{
				"label": "Grand Total",
				"fieldname": FIELD_TOTAL,
				"fieldtype": "Currency",
				"permlevel": 1,
				"default": str(SECRET_TOTAL),
			},
		],
		permissions=[
			{
				"role": ROLE_BASE,
				"permlevel": 0,
				"read": 1,
				"write": 1,
				"create": 1,
				"submit": 1,
				"cancel": 1,
				"amend": 1,
			},
			{
				"role": ROLE_PRIV,
				"permlevel": 0,
				"read": 1,
				"write": 1,
				"create": 1,
				"submit": 1,
				"cancel": 1,
				"amend": 1,
			},
			{"role": ROLE_PRIV, "permlevel": 1, "read": 1, "write": 1},
		],
	).insert()


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _make_doc(*, docstatus: int = 0) -> str:
	"""Insert a JV Permlevel Test doc as Administrator (bypasses permlevel
	entirely, so the restricted field lands with its real SECRET_VALUE
	default) and drive it to the requested docstatus."""
	doc = frappe.get_doc({"doctype": DT_NAME, FIELD_PUBLIC: "visible"})
	doc.insert(ignore_permissions=True)
	if docstatus >= 1:
		doc.submit()
	if docstatus >= 2:
		doc.cancel()
	frappe.db.commit()
	return doc.name


class PermlevelLeakTestCase(FrappeTestCase):
	"""Shared fixture for the whole finding group."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_role(ROLE_BASE)
		_ensure_role(ROLE_PRIV)
		_ensure_doctype()
		_ensure_user(USER_RESTRICTED, roles=(ROLE_BASE,))
		_ensure_user(USER_PRIVILEGED, roles=(ROLE_PRIV,))
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(DT_NAME)
		frappe.db.commit()
		super().tearDown()


class TestGetDocPermlevelLeak(PermlevelLeakTestCase):
	"""F1: get_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		name = _make_doc()
		with _as(USER_RESTRICTED):
			result = get_doc(doctype=DT_NAME, name=name)
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result[FIELD_PUBLIC], "visible")

	def test_privileged_user_sees_restricted_field(self):
		name = _make_doc()
		with _as(USER_PRIVILEGED):
			result = get_doc(doctype=DT_NAME, name=name)
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestCreateDocPermlevelLeak(PermlevelLeakTestCase):
	"""F7: create_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		with _as(USER_RESTRICTED):
			result = create_doc(doctype=DT_NAME, values={FIELD_PUBLIC: "created"})
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result[FIELD_PUBLIC], "created")

	def test_privileged_user_sees_restricted_field(self):
		with _as(USER_PRIVILEGED):
			result = create_doc(doctype=DT_NAME, values={FIELD_PUBLIC: "created"})
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestUpdateDocPermlevelLeak(PermlevelLeakTestCase):
	"""F8: update_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		name = _make_doc()
		with _as(USER_RESTRICTED):
			result = update_doc(doctype=DT_NAME, name=name, changes={FIELD_PUBLIC: "updated"})
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result[FIELD_PUBLIC], "updated")

	def test_privileged_user_sees_restricted_field(self):
		name = _make_doc()
		with _as(USER_PRIVILEGED):
			result = update_doc(doctype=DT_NAME, name=name, changes={FIELD_PUBLIC: "updated"})
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestSubmitDocPermlevelLeak(PermlevelLeakTestCase):
	"""F9: submit_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		name = _make_doc(docstatus=0)
		with _as(USER_RESTRICTED):
			result = submit_doc(doctype=DT_NAME, name=name)
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result["docstatus"], 1)

	def test_privileged_user_sees_restricted_field(self):
		name = _make_doc(docstatus=0)
		with _as(USER_PRIVILEGED):
			result = submit_doc(doctype=DT_NAME, name=name)
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestCancelDocPermlevelLeak(PermlevelLeakTestCase):
	"""F3: cancel_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		name = _make_doc(docstatus=1)
		with _as(USER_RESTRICTED):
			result = cancel_doc(doctype=DT_NAME, name=name)
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result["docstatus"], 2)

	def test_privileged_user_sees_restricted_field(self):
		name = _make_doc(docstatus=1)
		with _as(USER_PRIVILEGED):
			result = cancel_doc(doctype=DT_NAME, name=name)
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestAmendDocPermlevelLeak(PermlevelLeakTestCase):
	"""F22: amend_doc."""

	def test_restricted_user_does_not_see_restricted_field(self):
		name = _make_doc(docstatus=2)
		with _as(USER_RESTRICTED):
			result = amend_doc(doctype=DT_NAME, name=name)
		# apply_fieldlevel_read_permissions() deletes the in-memory attribute,
		# but as_dict() still emits the key (schema-defined column) with a
		# None fallback rather than omitting it - assert the value is gone,
		# not the key.
		self.assertIsNone(result.get(FIELD_RESTRICTED))
		self.assertEqual(result["docstatus"], 0)

	def test_privileged_user_sees_restricted_field(self):
		name = _make_doc(docstatus=2)
		with _as(USER_PRIVILEGED):
			result = amend_doc(doctype=DT_NAME, name=name)
		self.assertEqual(result[FIELD_RESTRICTED], SECRET_VALUE)


class TestPreviewDocPermlevelLeak(PermlevelLeakTestCase):
	"""F10: preview_doc - structural (no as_dict; _summarize() echoes
	header field values). The restricted caller supplies a guessed value
	for the restricted field; Frappe's write-side permlevel reset silently
	overwrites it back to the REAL default inside the preview sandbox's
	doc.insert(), so an unfiltered echo hands back the real secret instead
	of the caller's own guess - or a validation error."""

	def test_restricted_user_does_not_see_restricted_field(self):
		with _as(USER_RESTRICTED):
			result = preview_doc(DT_NAME, {FIELD_PUBLIC: "visible", FIELD_RESTRICTED: "guessed-value"})
		self.assertTrue(result["valid"])
		self.assertNotIn(FIELD_RESTRICTED, result["resolved"])

	def test_privileged_user_sees_restricted_field(self):
		with _as(USER_PRIVILEGED):
			result = preview_doc(DT_NAME, {FIELD_PUBLIC: "visible", FIELD_RESTRICTED: "guessed-value"})
		self.assertTrue(result["valid"])
		self.assertEqual(result["resolved"][FIELD_RESTRICTED], "guessed-value")


class TestPreviewDocTotalsPermlevelLeak(PermlevelLeakTestCase):
	"""F10 completion: `_summarize()`'s `totals` dict comprehension (the
	`_TOTAL_FIELDS` net_total/total_taxes_and_charges/grand_total/
	rounded_total) did an unguarded `doc.get(f)` with no permlevel filter,
	unlike `resolved`/`server_filled`. A tenant who customizes one of those
	fields to permlevel>0 (fixture: `grand_total`, default SECRET_TOTAL) had
	its real server-computed value leak to a restricted caller via `totals`
	even though the same field is correctly withheld from `resolved`."""

	def test_restricted_user_does_not_see_restricted_total(self):
		with _as(USER_RESTRICTED):
			result = preview_doc(DT_NAME, {FIELD_PUBLIC: "visible"})
		self.assertTrue(result["valid"])
		self.assertNotIn(FIELD_TOTAL, result["totals"])

	def test_privileged_user_sees_restricted_total(self):
		with _as(USER_PRIVILEGED):
			result = preview_doc(DT_NAME, {FIELD_PUBLIC: "visible"})
		self.assertTrue(result["valid"])
		self.assertEqual(float(result["totals"][FIELD_TOTAL]), SECRET_TOTAL)
