from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisPhysicalCountItem(NotRenamable, Document):
	pass
