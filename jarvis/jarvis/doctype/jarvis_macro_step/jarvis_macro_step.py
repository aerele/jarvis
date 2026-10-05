"""Jarvis Macro Step — child row of Jarvis Macro (one prompt in the sequence)."""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisMacroStep(NotRenamable, Document):
	pass
