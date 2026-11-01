"""Internal primary-key lock row for the Reorder safety migration."""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisReorderSafetyFence(NotRenamable, Document):
	pass
