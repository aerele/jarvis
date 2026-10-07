from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisChatSendRequest(NotRenamable, Document):
	pass
