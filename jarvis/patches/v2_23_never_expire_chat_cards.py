"""Chat cards on ``Jarvis Pending Action`` never expire (PR-3b).

Keyed on pending-action existence (the display row's ``tool_call_id`` is its name):
- a pending row whose pending action is still Pending loses its ``expires_at``, so
  no client counts it down;
- a pending row with no pending action (a legacy Redis card) whose ``expires_at``
  has passed gets its ``expired`` chip: its token is long gone.
Every other row is left alone. Batched and idempotent.
"""

import frappe

_BATCH = 500


def _batched(select: str, update: str) -> int:
	done = 0
	while True:
		names = frappe.db.sql_list(select + " LIMIT %(n)s", {"n": _BATCH, "now": frappe.utils.now_datetime()})
		if not names:
			return done
		frappe.db.sql(update, {"names": tuple(names)})
		frappe.db.commit()
		done += len(names)
		if len(names) < _BATCH:
			return done


def execute():
	if not (
		frappe.db.table_exists("Jarvis Pending Action") and frappe.db.table_exists("Jarvis Chat Message")
	):
		return
	_batched(
		"""SELECT m.name FROM `tabJarvis Chat Message` m
		JOIN `tabJarvis Pending Action` pa ON pa.name = m.tool_call_id
		WHERE m.role = 'tool' AND m.tool_status = 'pending' AND m.expires_at IS NOT NULL
		  AND pa.status = 'Pending'""",
		"UPDATE `tabJarvis Chat Message` SET expires_at = NULL WHERE name IN %(names)s",
	)
	_batched(
		"""SELECT m.name FROM `tabJarvis Chat Message` m
		LEFT JOIN `tabJarvis Pending Action` pa ON pa.name = m.tool_call_id
		WHERE m.role = 'tool' AND m.tool_status = 'pending' AND m.expires_at < %(now)s
		  AND pa.name IS NULL""",
		"""UPDATE `tabJarvis Chat Message` SET tool_status = '', action_outcome = 'expired',
			pending_card = NULL, content = CONCAT(IFNULL(tool_name, ''), ' → expired')
		WHERE name IN %(names)s AND tool_status = 'pending'""",
	)
