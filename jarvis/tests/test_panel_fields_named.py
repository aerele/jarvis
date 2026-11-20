"""#2171: a value a controller requires in code is marked on any DocType.

Frappe's ``reqd`` lists only static mandatory fields. An app can require a field in its
controller instead (india_compliance raises ``MandatoryError("HSN/SAC Code is required")``
from Item.validate when GST validation is on), and then the refusal carried no field
marks. An empty field the failure's message names, by label or fieldname, is now
marked too. ToDo stands in for any DocType: none of its fields but ``description`` is
``reqd``, so every mark below comes from the message.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import draft_card, panel_fields


def _refusal(message, values, exc=frappe.MandatoryError):
	return draft_card.refusal(exc(message), "create_doc", {"doctype": "ToDo", "values": values})


class TestControllerRequiredFieldsAreMarked(FrappeTestCase):
	def test_an_empty_field_the_message_names_is_marked(self):
		marks = _refusal("Sender is required for this ToDo.", {"description": "x"})
		self.assertEqual(marks, [{"fieldname": "sender", "label": "Sender", "where": "main"}])

	def test_a_field_with_a_value_is_never_marked(self):
		# a new ToDo's Due Date defaults to today, so naming it marks nothing
		self.assertIsNone(_refusal("Due Date is required.", {"description": "x"}))
		self.assertIsNone(_refusal("Sender is required.", {"description": "x", "sender": "a@example.com"}))

	def test_the_name_must_be_a_whole_word(self):
		doc = frappe.new_doc("ToDo")
		named = {m["fieldname"] for m in panel_fields._named_in_message(doc, "Please update the role first.")}
		# "role" is a whole word; "date" is only inside "update"
		self.assertEqual(named, {"role"})

	def test_a_fieldname_in_the_message_counts(self):
		marks = _refusal("allocated_to is required", {"description": "x"})
		self.assertEqual([m["fieldname"] for m in marks], ["allocated_to"])

	def test_markup_in_the_message_is_ignored(self):
		marks = _refusal("<b>Sender</b> is required", {"description": "x"})
		self.assertEqual([m["fieldname"] for m in marks], ["sender"])

	def test_any_other_failure_still_marks_nothing(self):
		self.assertIsNone(_refusal("Sender is required", {"description": "x"}, exc=frappe.ValidationError))

	def test_a_field_frappe_already_lists_is_marked_once(self):
		marks = panel_fields.missing_marks("create", "ToDo", "", {}, "Value missing for ToDo: Description")
		self.assertEqual([m["fieldname"] for m in marks], ["description"])

	def test_without_a_message_only_frappes_own_list_counts(self):
		self.assertEqual(panel_fields.missing_marks("create", "ToDo", "", {"description": "x"}), [])
