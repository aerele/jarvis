"""Composite indexes on Jarvis Chat Turn for the Phase-0 admission query
shapes (jarvis.chat.admission). Frappe's doctype JSON only expresses
single-column indexes; the admission COUNT/scan queries want these
composites:

  (state, relay_target_id) - the shard inflight COUNT + queued scan.
  (conversation, state)    - the per-conversation single-flight lookup.
  (reserved, reservation_expires_at) - the crashed-enqueue reclaim sweep.

add_index is idempotent (no-ops when the index already exists), so this is
safe to re-run on an upgraded site.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Jarvis Chat Turn"):
		return
	frappe.db.add_index("Jarvis Chat Turn", ["state", "relay_target_id"], index_name="state_relay")
	frappe.db.add_index("Jarvis Chat Turn", ["conversation", "state"], index_name="conv_state")
	frappe.db.add_index(
		"Jarvis Chat Turn", ["reserved", "reservation_expires_at"], index_name="reserved_expiry"
	)
