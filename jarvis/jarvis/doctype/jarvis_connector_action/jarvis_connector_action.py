from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisConnectorAction(NotRenamable, Document):
	pass
