"""Jarvis Client Error - local, per-bench buffer of UI errors before they are
forwarded to the admin control plane.

Rows are written only by ``jarvis.api_errors`` (browser reports) which scrubs
message/stack of PII / ERP values before insert. This controller is a bounds
backstop: it truncates the free-text fields and normalizes the small ones so
nothing oversized or malformed lands in the table regardless of the caller.
Never versioned (``track_changes: 0``) - these rows are high-churn telemetry,
not business records.
"""

import frappe
from frappe.model.document import Document

MAX_MESSAGE = 500
MAX_STACK = 2000
MAX_SURFACE = 40
MAX_ROUTE = 200


class JarvisClientError(Document):
	def validate(self) -> None:
		self.message = (self.message or "").strip()[:MAX_MESSAGE]
		self.stack = (self.stack or "").strip()[:MAX_STACK]
		self.surface = ((self.surface or "").strip()[:MAX_SURFACE]) or "unknown"
		self.route = (self.route or "").strip()[:MAX_ROUTE]
		if not self.fingerprint:
			frappe.throw("A Jarvis Client Error needs a fingerprint.")
		if not self.count or int(self.count) < 1:
			self.count = 1
		now = frappe.utils.now_datetime()
		if not self.first_seen:
			self.first_seen = now
		if not self.last_seen:
			self.last_seen = now
