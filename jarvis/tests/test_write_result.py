"""create_doc / update_doc return a compact result, or the ``fields`` asked for."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc


class TestWriteResult(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)

	def _todo(self) -> str:
		return frappe.get_doc({"doctype": "ToDo", "description": "jwr seed"}).insert().name

	def _compact(self, doctype: str, name: str) -> set:
		"""The keys a compact result carries for this saved record."""
		meta = frappe.get_meta(doctype)
		keys = {"doctype", "name", "docstatus"}
		for key in ("status", meta.title_field):
			if key and meta.has_field(key) and frappe.db.get_value(doctype, name, key) is not None:
				keys.add(key)
		return keys

	def test_create_default_is_compact(self):
		res = create_doc("ToDo", {"description": "jwr create", "priority": "High"})
		self.assertEqual(set(res), self._compact("ToDo", res["name"]))
		self.assertIn("status", res)
		self.assertEqual((res["doctype"], res["docstatus"]), ("ToDo", 0))
		self.assertNotIn("priority", res)  # a sent value that is not the title

	def test_title_field_is_returned_under_its_own_name(self):
		res = create_doc("Note", {"title": "jwr title", "content": "body"})
		self.assertEqual(res, {"doctype": "Note", "name": res["name"], "docstatus": 0, "title": "jwr title"})

	def test_update_default_is_compact(self):
		name = self._todo()
		res = update_doc("ToDo", name, {"priority": "High"})
		self.assertEqual(set(res), self._compact("ToDo", name))
		self.assertEqual(res["name"], name)
		self.assertNotIn("priority", res)

	def test_empty_fields_is_the_compact_default(self):
		name = self._todo()
		res = update_doc("ToDo", name, {"priority": "High"}, fields=[])
		self.assertEqual(set(res), self._compact("ToDo", name))
		res = create_doc("ToDo", {"description": "jwr empty"}, fields=[])
		self.assertEqual(set(res), self._compact("ToDo", res["name"]))

	def test_create_fields_returns_identity_and_those_fields(self):
		res = create_doc(
			"ToDo", {"description": "jwr f", "priority": "High"}, fields=["description", "priority"]
		)
		self.assertEqual(
			res,
			{
				"doctype": "ToDo",
				"name": res["name"],
				"docstatus": 0,
				"description": "jwr f",
				"priority": "High",
			},
		)

	def test_update_fields_returns_identity_and_those_fields(self):
		name = self._todo()
		res = update_doc("ToDo", name, {"priority": "Low"}, fields=["priority"])
		self.assertEqual(res, {"doctype": "ToDo", "name": name, "docstatus": 0, "priority": "Low"})

	def test_unknown_field_is_refused_before_the_write(self):
		before = frappe.db.count("ToDo")
		with self.assertRaises(InvalidArgumentError):
			create_doc("ToDo", {"description": "jwr unknown"}, fields=["no_such_field"])
		self.assertEqual(frappe.db.count("ToDo"), before)
		name = self._todo()
		with self.assertRaises(InvalidArgumentError):
			update_doc("ToDo", name, {"priority": "High"}, fields=["no_such_field"])
		self.assertEqual(frappe.db.get_value("ToDo", name, "priority"), "Medium")

	def test_star_is_not_special(self):
		with self.assertRaises(InvalidArgumentError):
			create_doc("ToDo", {"description": "jwr star"}, fields=["*"])

	def test_fields_with_a_batch_is_refused(self):
		name = self._todo()
		with self.assertRaises(InvalidArgumentError):
			create_doc(docs=[{"doctype": "ToDo", "values": {"description": "jwr b"}}], fields=["name"])
		with self.assertRaises(InvalidArgumentError):
			update_doc("ToDo", updates=[{"name": name, "changes": {"priority": "High"}}], fields=["name"])
