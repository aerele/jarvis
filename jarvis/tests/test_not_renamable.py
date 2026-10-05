"""Records of Jarvis doctypes are not renamable.

A rename rewrites every link to a record in place, without the permission hook or
the ``validate`` of the records that hold those links. Most Jarvis doctypes hold
per-user data, and the app never renames a record of any of them, so each one
refuses a rename or a merge (``jarvis.permissions.NotRenamable``). The one doctype
whose definition allows renaming keeps that, and renaming a User still carries
the user's Jarvis rows with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.base_document import get_controller
from frappe.tests.utils import FrappeTestCase

from jarvis.permissions import NotRenamable
from jarvis.tests.test_part3_security import _as, _ensure_user

OWNER = "nr-owner@example.com"
OTHER = "nr-other@example.com"

# Worst first: per-user rows whose links a rename or merge would carry onto
# another user's record.
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

# Doctypes whose definition allows renaming (System Manager, from Desk). Kept.
RENAMABLE = ("Jarvis PDF Template",)

# Read as their own owner, so the request route reaches the rename itself.
OWNER_READABLE = (
	"Jarvis User Settings",
	"Jarvis Chat Message",
	"Jarvis Connector",
	"Jarvis Agent Installation",
)


def _jarvis_doctypes() -> set[str]:
	"""Every regular doctype the app ships, read from its definitions on disk."""
	root = Path(frappe.get_app_path("jarvis", "jarvis", "doctype"))
	names = set()
	for path in root.glob("*/*.json"):
		if path.stem != path.parent.name:
			continue
		definition = json.loads(path.read_text())
		if not (definition.get("istable") or definition.get("issingle")):
			names.add(definition["name"])
	return names


def _seed(doctype: str, owner: str, **values) -> str:
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
	doc.update(values)
	doc.db_insert()
	frappe.db.set_value(doctype, name, "owner", owner, update_modified=False)
	return name


def _readable_by(doctype: str, user: str, conversation: str) -> dict:
	"""What makes a seeded row readable by its owner beyond ``owner`` itself."""
	if doctype == "Jarvis User Settings":
		return {"user": user}
	if doctype == "Jarvis Chat Message":
		return {"conversation": conversation}
	return {}


class TestNotRenamable(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(OWNER, ["Jarvis User"])
		_ensure_user(OTHER, ["Jarvis User"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_every_jarvis_doctype_is_listed_once(self):
		# A new doctype has to choose: not renamable, or renamable on purpose.
		self.assertEqual(_jarvis_doctypes(), set(NOT_RENAMABLE) | set(RENAMABLE))
		self.assertFalse(set(NOT_RENAMABLE) & set(RENAMABLE))
		self.assertEqual(len(NOT_RENAMABLE), len(set(NOT_RENAMABLE)))
		for doctype in NOT_RENAMABLE:
			with self.subTest(doctype=doctype):
				self.assertFalse(frappe.get_meta(doctype).allow_rename)
				self.assertTrue(issubclass(get_controller(doctype), NotRenamable))

	def test_rename_and_merge_are_refused_to_the_owner_and_to_another_user(self):
		from frappe.model.document import Document

		frappe.is_whitelisted(Document.rename)  # control: this check can pass
		for doctype in NOT_RENAMABLE:
			with self.subTest(doctype=doctype):
				own = _seed(doctype, OWNER)
				theirs = _seed(doctype, OTHER)
				new = f"nr-new-{frappe.generate_hash(length=8)}"
				for user in (OWNER, OTHER):
					with _as(user):
						doc = frappe.get_doc(doctype, own)
						# The request route accepts only whitelisted methods.
						with self.assertRaises(frappe.PermissionError):
							frappe.is_whitelisted(doc.rename.__func__)
						with self.assertRaises(frappe.PermissionError):
							doc.rename(new, force=True, validate_rename=False)
						with self.assertRaises(frappe.PermissionError):
							doc.rename(theirs, merge=True, force=True, validate_rename=False)
				self.assertTrue(frappe.db.exists(doctype, own), f"{doctype} was renamed away")
				self.assertTrue(frappe.db.exists(doctype, theirs))
				self.assertFalse(frappe.db.exists(doctype, new))
				self.assertEqual(frappe.db.get_value(doctype, own, "owner"), OWNER)

	def test_the_request_route_refuses_the_owner(self):
		from frappe import handler

		frappe.db.delete("Jarvis User Settings", {"user": ("in", (OWNER, OTHER))})
		own_conv = _seed("Jarvis Conversation", OWNER)
		other_conv = _seed("Jarvis Conversation", OTHER)
		for doctype in OWNER_READABLE:
			own = _seed(doctype, OWNER, **_readable_by(doctype, OWNER, own_conv))
			theirs = _seed(doctype, OTHER, **_readable_by(doctype, OTHER, other_conv))
			for args in (
				{"name": f"nr-new-{frappe.generate_hash(length=8)}", "force": 1, "validate_rename": 0},
				{"name": theirs, "merge": 1, "force": 1, "validate_rename": 0},
			):
				with self.subTest(doctype=doctype, merge=bool(args.get("merge"))), _as(OWNER):
					self.assertTrue(frappe.get_doc(doctype, own).has_permission("read"), "premise: readable")
					# Only the request's HTTP verb is taken out; the route is otherwise real.
					with (
						patch.object(handler, "is_valid_http_method"),
						self.assertRaises(frappe.PermissionError),
					):
						handler.run_doc_method("rename", dt=doctype, dn=own, args=frappe.as_json(args))
					self.assertTrue(frappe.db.exists(doctype, own))
					self.assertTrue(frappe.db.exists(doctype, theirs))

	def test_a_server_side_rename_is_refused_too(self):
		for doctype in NOT_RENAMABLE:
			with self.subTest(doctype=doctype):
				own = _seed(doctype, OWNER)
				theirs = _seed(doctype, OTHER)
				with self.assertRaises(frappe.PermissionError):
					frappe.rename_doc(doctype, own, f"nr-moved-{frappe.generate_hash(length=8)}", force=True)
				with self.assertRaises(frappe.PermissionError):
					frappe.rename_doc(doctype, own, theirs, merge=True, force=True)
				self.assertTrue(frappe.db.exists(doctype, own))
				self.assertTrue(frappe.db.exists(doctype, theirs))

	def test_a_renamable_doctype_still_renames(self):
		old, new = (f"nr-tpl-{frappe.generate_hash(length=8)}" for _ in range(2))
		frappe.get_doc({"doctype": "Jarvis PDF Template", "template_key": old, "label": old}).insert()
		frappe.rename_doc("Jarvis PDF Template", old, new)
		self.assertTrue(frappe.db.exists("Jarvis PDF Template", new))
		self.assertFalse(frappe.db.exists("Jarvis PDF Template", old))

	def test_renaming_a_user_carries_their_rows(self):
		# Renaming the User rewrites the links to it; the Jarvis rows keep their names.
		old = _ensure_user(f"nr-{frappe.generate_hash(length=8)}@example.com", ["Jarvis User"])
		new = f"nr-{frappe.generate_hash(length=8)}@example.com"
		settings = _seed("Jarvis User Settings", old, user=old)
		frappe.rename_doc("User", old, new, force=True)
		self.assertEqual(frappe.db.get_value("Jarvis User Settings", settings, "user"), new)
