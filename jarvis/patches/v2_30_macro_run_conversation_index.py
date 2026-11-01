"""Create the macro-run conversation index on upgraded sites."""

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


DOCTYPE = "Jarvis Macro Run"
FIELDNAME = "conversation"
INDEX_FIELDS = ["conversation"]
INDEX_NAME = "conversation_index"


def execute():
	"""Create the named lookup index after model sync.

	Controller hooks run only when model sync saves the DocType document.
	Existing sites can therefore have the field and controller code without the
	index. This post-model-sync patch supplies the missing delivery step. Schema
	sync removes a single-field index when its DocField has no ``search_index``.
	Keep the generated Property Setter so a later reload retains this index.
	"""
	frappe.db.add_index(DOCTYPE, INDEX_FIELDS, index_name=INDEX_NAME)
	make_property_setter(
		DOCTYPE,
		FIELDNAME,
		property="search_index",
		value="1",
		property_type="Check",
		for_doctype=False,
		validate_fields_for_doctype=False,
	)
