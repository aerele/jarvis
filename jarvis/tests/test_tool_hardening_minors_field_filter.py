"""create_doc / update_doc refuse amended_from and underscore-prefixed fields."""

from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc

NOTE_DT = "Note"
_REFUSED = ["amended_from", "_comments", "_assign", "_liked_by", "_user_tags"]


class TestFieldFilter(FrappeTestCase):
	def test_create_doc_refuses_each_field_naming_it(self):
		for field in _REFUSED:
			with self.assertRaises(InvalidArgumentError, msg=field) as cm:
				create_doc(doctype=NOTE_DT, values={field: "x", "title": "t"})
			self.assertIn(field, str(cm.exception))

	def test_update_doc_refuses_each_field_naming_it(self):
		for field in _REFUSED:
			with self.assertRaises(InvalidArgumentError, msg=field) as cm:
				update_doc(doctype=NOTE_DT, name="anything", changes={field: "x", "content": "y"})
			self.assertIn(field, str(cm.exception))
