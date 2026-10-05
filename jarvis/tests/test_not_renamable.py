"""Records of Jarvis doctypes are never renamed or merged; the one doctype whose
definition allows renaming keeps it, and renaming a User still updates links to that user."""

from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.model.base_document import get_controller
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import NotRenamable
from jarvis.tests.test_part3_security import _as, _ensure_user

USER = "nr-user@example.com"

NOT_RENAMABLE = (
	"Jarvis User Settings",
	"Jarvis Chat Message",
	"Jarvis Connector",
	"Jarvis Agent Installation",
	"Jarvis Conversation",
	"Jarvis Macro",
	"Jarvis Macro Run",
	"Jarvis Agent Activity",
	"Jarvis Agent Finding",
	"Jarvis Agent Run",
	"Jarvis Agent Run Step",
	"Jarvis Approval Request",
	"Jarvis Custom Skill",
	"Jarvis Dashboard",
	"Jarvis Mobile Device",
	"Jarvis Personalise Question",
	"Jarvis Skill Promotion Request",
	"Jarvis Wiki Promotion Request",
	"Jarvis Voice Note",
	"Jarvis Wiki Page",
	"Jarvis Trigger",
	"Jarvis Agent Listing",
	"Jarvis Pending Action",
	"Jarvis User Memory",
	"Jarvis Chat Turn",
	"Jarvis Turn Effect",
	"Jarvis Turn Usage",
	"Jarvis Chat Session",
	"MCP OAuth Token",
	"MCP OAuth Client",
	"Jarvis Pending OAuth Capture",
	"Jarvis Agent Write",
	"Jarvis Agent Provenance Event",
	"Jarvis App Learning Run",
	"Jarvis Connector Log",
	"Jarvis Learned Pattern",
	"Jarvis Relay Pump",
	"Jarvis Agent Model Choice",
	"Jarvis Client Error",
	"Jarvis Import Announcement",
	"Jarvis Pattern Detector State",
	"Jarvis Pattern Run",
	"Jarvis Pattern Snapshot",
	"Jarvis Personalise Question Rule",
	"Jarvis Shared Skill Slug",
	"Jarvis Trigger Activity",
	"Jarvis Wiki Graph History",
)

NOT_RENAMABLE_CHILD = (
	"Jarvis Agent Allowed Role",
	"Jarvis Agent Allowed User",
	"Jarvis Connector Action",
	"Jarvis Custom Skill Allowed Role",
	"Jarvis Custom Skill Share",
	"Jarvis Dashboard Filter",
	"Jarvis Dashboard Source",
	"Jarvis Learned Pattern Role",
	"Jarvis LLM Pool Model",
	"Jarvis LLM Pool Subscription Account",
	"Jarvis Macro Step",
	"Jarvis Pending Action Waiter",
	"Jarvis User Model Usage",
)

# Doctypes whose definition allows renaming (System Manager, from Desk). Kept.
RENAMABLE = ("Jarvis PDF Template",)

# Child rows of a renamable doctype, left as the framework ships them.
RENAMABLE_CHILD = ("Jarvis PDF Template Company Letter Head",)

REFUSED = NOT_RENAMABLE + NOT_RENAMABLE_CHILD


def _jarvis_doctypes() -> set[str]:
	"""Every regular and child doctype the app ships, read from its definitions on disk."""
	root = Path(frappe.get_app_path("jarvis", "jarvis", "doctype"))
	names = set()
	for path in root.glob("*/*.json"):
		if path.stem != path.parent.name:
			continue
		definition = json.loads(path.read_text())
		if not definition.get("issingle"):
			names.add(definition["name"])
	return names


def _seed(doctype: str) -> str:
	"""A row as it sits in the table, without the doctype's own validation: the
	refusal must not depend on what a valid record of each doctype looks like."""
	meta = frappe.get_meta(doctype)
	name = f"nr-{frappe.generate_hash(length=10)}"
	doc = frappe.new_doc(doctype)
	doc.name = name
	for df in meta.fields:
		keyed = df.unique or meta.autoname == f"field:{df.fieldname}"
		if keyed and df.fieldtype in ("Data", "Link"):
			doc.set(df.fieldname, name)
	doc.db_insert()
	return name


def _new_name(doctype: str) -> str:
	"""A free name that also fits the field the doctype is named by."""
	meta = frappe.get_meta(doctype)
	field = meta.get_field((meta.autoname or "").removeprefix("field:"))
	if field and field.fieldtype == "Date":
		return str(frappe.utils.add_days("1900-01-01", int(frappe.generate_hash(length=4), 16)))
	return f"nr-moved-{frappe.generate_hash(length=8)}"


def _row(doctype: str, name: str) -> dict:
	return frappe.db.get_value(doctype, name, ["name", "owner", "modified"], as_dict=True)


class TestNotRenamable(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER, ["Jarvis User"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_every_jarvis_doctype_is_listed_once(self):
		# A new doctype or child table has to choose: not renamable, or renamable on purpose.
		listed = REFUSED + RENAMABLE + RENAMABLE_CHILD
		self.assertEqual(_jarvis_doctypes(), set(listed))
		self.assertEqual(len(listed), len(set(listed)))
		for doctype in REFUSED:
			with self.subTest(doctype=doctype):
				self.assertFalse(frappe.get_meta(doctype).allow_rename)
				self.assertTrue(issubclass(get_controller(doctype), NotRenamable))

	def test_rename_and_merge_are_refused(self):
		for doctype in REFUSED:
			with self.subTest(doctype=doctype):
				name, other = _seed(doctype), _seed(doctype)
				before = _row(doctype, name)
				new = f"nr-new-{frappe.generate_hash(length=8)}"
				with _as(USER):
					doc = frappe.get_doc(doctype, name)
					with self.assertRaises(frappe.PermissionError):
						doc.rename(new)
					with self.assertRaises(frappe.PermissionError):
						doc.rename(other, merge=True)
				self.assertEqual(_row(doctype, name), before, f"{doctype} was changed")
				self.assertTrue(frappe.db.exists(doctype, other))
				self.assertFalse(frappe.db.exists(doctype, new))

	def test_rename_is_not_exposed(self):
		from frappe.model.document import Document

		frappe.is_whitelisted(Document.rename)  # control: this check can pass
		for doctype in REFUSED:
			with self.subTest(doctype=doctype):
				method = frappe.get_doc(doctype, _seed(doctype)).rename
				with self.assertRaises(frappe.PermissionError):
					frappe.is_whitelisted(method.__func__)

	def test_a_server_side_rename_is_refused_too(self):
		for doctype in REFUSED:
			with self.subTest(doctype=doctype):
				name, other = _seed(doctype), _seed(doctype)
				before = _row(doctype, name)
				with self.assertRaises(frappe.PermissionError):
					frappe.rename_doc(doctype, name, f"nr-moved-{frappe.generate_hash(length=8)}", force=True)
				with self.assertRaises(frappe.PermissionError):
					frappe.rename_doc(doctype, name, other, merge=True, force=True)
				self.assertEqual(_row(doctype, name), before)
				self.assertTrue(frappe.db.exists(doctype, other))

	def test_a_server_side_rename_without_validation_is_rolled_back(self):
		# The refusal after the rename has run relies on the caller rolling back.
		from frappe.model.rename_doc import rename_doc

		for doctype in REFUSED:
			with self.subTest(doctype=doctype):
				name = _seed(doctype)
				before = _row(doctype, name)
				new = _new_name(doctype)
				frappe.db.savepoint("nr_rename")
				with self.assertRaises(frappe.PermissionError):
					rename_doc(doctype, name, new, force=True, validate=False, show_alert=False)
				frappe.db.rollback(save_point="nr_rename")
				self.assertEqual(_row(doctype, name), before)
				self.assertFalse(frappe.db.exists(doctype, new))

	def test_a_renamable_doctype_still_renames(self):
		old, new = (f"nr-tpl-{frappe.generate_hash(length=8)}" for _ in range(2))
		frappe.get_doc({"doctype": "Jarvis PDF Template", "template_key": old, "label": old}).insert()
		frappe.rename_doc("Jarvis PDF Template", old, new)
		self.assertTrue(frappe.db.exists("Jarvis PDF Template", new))
		self.assertFalse(frappe.db.exists("Jarvis PDF Template", old))

	def test_renaming_a_user_still_updates_links_to_that_user(self):
		old = _ensure_user(f"nr-{frappe.generate_hash(length=8)}@example.com", ["Jarvis User"])
		new = f"nr-{frappe.generate_hash(length=8)}@example.com"
		settings = _seed("Jarvis User Settings")
		frappe.db.set_value("Jarvis User Settings", settings, "user", old, update_modified=False)
		frappe.rename_doc("User", old, new, force=True)
		self.assertEqual(frappe.db.get_value("Jarvis User Settings", settings, "user"), new)
