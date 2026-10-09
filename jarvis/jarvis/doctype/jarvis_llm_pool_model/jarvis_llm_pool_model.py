from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisLLMPoolModel(NotRenamable, Document):
	pass
