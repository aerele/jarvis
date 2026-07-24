"""Jarvis Chat Turn DocType controller.

Server-owned durable turn record for Phase-0 admission control
(jarvis.chat.admission). Every mutation goes through a server path with
``ignore_permissions=True`` and version-CAS discipline; there is no
end-user create/write/delete. The controller is intentionally minimal -
all invariants are enforced by the admission module's CAS statements, not
by ORM validation (the hot path never uses ``save()``).
"""

import frappe
from frappe.model.document import Document


class JarvisChatTurn(Document):
	# Minimal controller. run_id is the autoname field (name == run_id), so a
	# duplicate send collides on primary-key insert - the idempotency guard.
	pass
