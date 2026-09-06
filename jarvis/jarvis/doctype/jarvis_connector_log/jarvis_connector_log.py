"""Jarvis Connector Log - a queryable audit trail for every MCP connector call
(one row per call, NOT deduped/folded like ``Jarvis Client Error`` - each call
matters for a security review). Kept separate from the generic ``Error Log``
so connector issues (denied actions, SSRF blocks, circuit-breaker trips,
upstream failures) can be listed/filtered on their own; a row with
``status in (Failed, Denied, Blocked)`` IS the error view for this feature.

Rows are written only through ``log_call`` below, always with
``ignore_permissions=True`` (the broker writes under the impersonated caller,
who does not necessarily have create rights on this doctype - and never should:
only System Manager may read/write it, see the DocType permissions). This
controller is a bounds backstop, mirroring ``Jarvis Client Error``: it
truncates free-text fields so nothing oversized lands in the table regardless
of the caller. Never versioned (``track_changes: 0``) - high-churn telemetry,
not a business record.

``jarvis/connectors/broker.py`` (a parallel change, not this file) is expected
to be the only real caller of ``log_call``.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

DT = "Jarvis Connector Log"

MAX_CONNECTOR = 140
MAX_ACTION = 140
MAX_ERROR_CODE = 64
MAX_MESSAGE = 500
MAX_RUN_ID = 140
MAX_ARGS_SUMMARY = 500

_VALID_STATUSES = ("Success", "Failed", "Denied", "Blocked")


class JarvisConnectorLog(Document):
	def validate(self) -> None:
		self.connector = (self.connector or "").strip()[:MAX_CONNECTOR]
		self.action = (self.action or "").strip()[:MAX_ACTION]
		self.error_code = (self.error_code or "").strip()[:MAX_ERROR_CODE]
		self.message = (self.message or "").strip()[:MAX_MESSAGE]
		self.run_id = (self.run_id or "").strip()[:MAX_RUN_ID]
		self.args_summary = (self.args_summary or "").strip()[:MAX_ARGS_SUMMARY]
		if self.status not in _VALID_STATUSES:
			frappe.throw("A Jarvis Connector Log row needs a valid status.")
		if self.duration_ms is not None and int(self.duration_ms) < 0:
			self.duration_ms = 0
		if self.response_bytes is not None and int(self.response_bytes) < 0:
			self.response_bytes = 0

	@staticmethod
	def clear_old_logs(days=90):
		"""Satisfies frappe's ``LogType`` protocol so Log Settings picks this
		doctype up via ``default_log_clearing_doctypes`` in hooks.py (mirrors
		``Jarvis Trigger Activity``'s implementation - without this method the
		hook registration is silently dropped by ``remove_unsupported_doctypes``)."""
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType(DT)
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


def log_call(
	*,
	connector: str,
	action: str,
	status: str,
	user: str | None = None,
	error_code: str = "",
	message: str = "",
	duration_ms: int | None = None,
	run_id: str = "",
	args_summary: str = "",
	response_bytes: int | None = None,
) -> None:
	"""Insert one audit row. Best-effort: a logging failure must never break the
	tool call it is logging, so every exception is swallowed after being sent to
	the site logger. Does NOT commit - the caller (broker) decides the
	transaction boundary around the real tool-call work."""
	try:
		frappe.get_doc(
			{
				"doctype": DT,
				"connector": connector,
				"user": user or frappe.session.user,
				"action": action,
				"status": status,
				"error_code": error_code,
				"message": message,
				"duration_ms": duration_ms,
				"run_id": run_id,
				"args_summary": args_summary,
				"response_bytes": response_bytes,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.logger("jarvis.connectors").warning("jarvis connector log insert failed", exc_info=True)
