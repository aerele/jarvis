"""Jarvis Agent Allowed User — child rows of ``Jarvis Agent Listing.allowed_users``.

One named User per row, the per-person half of the deny-by-default access model
(jarvis#1062): access is granted to ROLES (``allowed_roles``) and/or to NAMED
USERS (this table), and a listing with NEITHER is reachable by admins only.

Bench-admin state set via ``agents_api.set_agent_access`` and by the one-time
grandfather patch; the catalog sync never writes it.
"""

from frappe.model.document import Document


class JarvisAgentAllowedUser(Document):
	pass
