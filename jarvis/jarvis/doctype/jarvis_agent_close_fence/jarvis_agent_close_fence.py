"""Internal primary-key rows for closed-agent confirmation fencing."""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisAgentCloseFence(NotRenamable, Document):
	pass
