"""Internal primary-key serializer for agent catalog Apply state."""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisAgentCatalogLock(NotRenamable, Document):
	pass
