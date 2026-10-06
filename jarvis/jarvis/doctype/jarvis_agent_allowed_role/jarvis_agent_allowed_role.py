"""Jarvis Agent Allowed Role — child rows of ``Jarvis Agent Listing.allowed_roles``.

One Role per row. An EMPTY table means the listing is unrestricted; a non-empty
table means only users holding at least one of these roles (or System Manager)
may install / run the agent. This is bench-admin state set via
``agents_api.set_agent_roles`` — the catalog sync never writes it.
"""

from frappe.model.document import Document

from jarvis.permissions import NotRenamable


class JarvisAgentAllowedRole(NotRenamable, Document):
	pass
