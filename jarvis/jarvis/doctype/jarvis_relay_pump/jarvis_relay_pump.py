"""Jarvis Relay Pump DocType controller.

Phase-0 minimal per-shard control row. The admission serializer
(jarvis.chat.admission) takes ``SELECT ... FOR UPDATE`` on this row to
serialize the credit read + admit decision across all senders and the
promoter for a given ``relay_target_id`` (OAR-1). Lease/epoch/pump fields
arrive additively in WP-1.
"""

import frappe
from frappe.model.document import Document


class JarvisRelayPump(Document):
	pass
