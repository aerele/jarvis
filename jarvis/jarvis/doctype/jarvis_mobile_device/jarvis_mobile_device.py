"""Jarvis Mobile Device DocType controller (JF-016).

One row per paired phone. It holds the PUBLIC half of a device credential
(``token_id``) plus ``HMAC-SHA256(key=token_id, msg=secret)`` — keyed, not a
plain ``sha256(token_id + ":" + secret)``, because concatenation is ambiguous
across the separator. The plaintext secret is handed to the phone exactly once,
at pairing, and never stored, so a database dump yields no usable credential.

Rows are written only by ``jarvis.mobile.device_auth`` (mint / revoke); the
doctype perms are read-if_owner for Jarvis User and full for System Manager,
so a user can see their own device inventory in Desk but cannot re-enable a
revoked device by writing the row. Revoked rows are deleted after
``device_auth.REVOKED_RETENTION_DAYS`` by the daily
``prune_revoked_devices`` job.
"""

import frappe
from frappe.model.document import Document

MAX_LABEL_CHARS = 140
MAX_PLATFORM_CHARS = 40


class JarvisMobileDevice(Document):
	def validate(self) -> None:
		if not self.token_id or not self.secret_hash:
			frappe.throw("A Jarvis Mobile Device needs both a token id and a hashed secret.")
		# Client-supplied strings: bounded here so a hostile pairing payload
		# cannot store an oversized label (the columns are 140/40 wide).
		self.device_label = (self.device_label or "").strip()[:MAX_LABEL_CHARS] or "Mobile device"
		self.platform = (self.platform or "").strip()[:MAX_PLATFORM_CHARS].lower() or "unknown"
		if self.enabled:
			self.revoked_at = None
			self.revoked_by = None
			self.revoked_reason = None
