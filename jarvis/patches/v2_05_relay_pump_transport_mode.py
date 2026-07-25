"""CDX-10 — backfill the DB-AUTHORITATIVE transport_mode on every existing
Jarvis Relay Pump control row.

The `transport_mode` / `mode_epoch` columns are added by the doctype schema sync
(this patch is post_model_sync, so they already exist). Derive each existing row's
initial transport_mode ONCE from the site's `jarvis_pump_enabled` config flag — the
same absent-vs-explicit-0 distinction every pump predicate shares (jarvis.chat.pump
._config_transport_mode) — so the ROW becomes the fenced dispatch decision from the
first turn after the upgrade. Only NULL/empty rows are touched (idempotent; a row a
cutover already stamped is never re-derived from the file). mode_epoch keeps its 0
default.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Jarvis Relay Pump"):
		return
	from jarvis.chat.pump import _config_transport_mode

	mode = _config_transport_mode()
	frappe.db.sql(
		"""UPDATE `tabJarvis Relay Pump`
		SET transport_mode=%(m)s
		WHERE transport_mode IS NULL OR transport_mode=''""",
		{"m": mode},
	)
	frappe.db.commit()
