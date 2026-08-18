"""Jarvis Turn Usage DocType controller.

One append-only row per completed turn's usage attribution (role-labelled,
cache-aware usage-dashboard Part A, task U1). Written best-effort inside
``jarvis.chat.usage.record_turn_usage`` on the RECORDED and VALID_ZERO paths,
try/except-wrapped so a write failure here can never change that function's
returned outcome or raise into the turn (the module's NEVER-raises contract).
There is no end-user create/write/delete; every insert goes through a server
path with ``ignore_permissions=True``.
"""

from frappe.model.document import Document


class JarvisTurnUsage(Document):
	pass
