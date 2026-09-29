"""Jarvis Agent Model Choice DocType controller.

One row per marketplace agent: the tenant-wide model its delegate runs on.
Deliberately carries NO DocPerm rows, so generic REST/Desk can neither read
nor write it; every access goes through the gated APIs in
``jarvis.chat.agent_models`` (``ignore_permissions`` after their own gate),
and every write is a ``doc.save`` so track_changes records it.
"""

from frappe.model.document import Document


class JarvisAgentModelChoice(Document):
	pass
