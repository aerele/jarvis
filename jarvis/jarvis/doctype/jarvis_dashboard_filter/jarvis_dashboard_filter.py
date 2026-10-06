from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisDashboardFilter(NotRenamable, Document):
	pass
