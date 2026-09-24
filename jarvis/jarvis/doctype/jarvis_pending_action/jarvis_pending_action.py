"""Jarvis Pending Action: one durable, sealed executable card (a chat Confirm card
or a held File Box write).

Server-written only. There are no DocPerm rows and the permission hooks below deny
everything, so no REST/Desk/tool path can read or write a row. The one insert is
``pending_actions.park`` (flagged ``jarvis_server_write``); every later change is a
raw conditional UPDATE in ``jarvis.chat.pending_actions``. The call itself is sealed
here, on insert, once the name exists (A3)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import strip_html

from jarvis.chat.pending_actions import _seal

SUMMARY_MAX = 280


class JarvisPendingAction(Document):
	def validate(self):
		refuse_orm_write(self)
		# Plain text before the seal (Desk renders it as HTML); the card hash is over this.
		self.summary = plain_text(self.summary)[:SUMMARY_MAX]
		# Canonical JSON text, stored verbatim (the fields skip the XSS filter), so the
		# card hash sealed below is exactly what a later unseal recomputes.
		for field in ("card", "preview", "needs_input", "needs_fix", "sheet_outcome"):
			self.set(field, _seal.json_text(self.get(field)))
		if self.flags.pa_args is None:
			frappe.throw(_("A pending action needs its sealed call."))
		self.sealed_call = _seal.seal_call(self, self.flags.pa_args, self.flags.pa_targets)

	def on_trash(self):
		refuse_orm_write(self, deleting=True)


def plain_text(text) -> str:
	"""Tags stripped; a ``<`` left over could only open an unterminated tag."""
	return strip_html(str(text or "")).replace("<", "")


def refuse_orm_write(doc, *, deleting: bool = False) -> None:
	"""Only a flagged server insert (or flagged delete) passes; saving an existing
	row is always refused. Administrator included; REST cannot set flags."""
	if doc.flags.jarvis_server_write and (deleting or doc.is_new()):
		return
	frappe.throw(_("Pending actions are managed by Jarvis."), frappe.PermissionError)


def has_permission(doc=None, ptype=None, user=None, debug=False) -> bool:
	return False


def get_permission_query_conditions(user=None, doctype=None) -> str:
	return "1=0"


def on_doctype_update():
	"""Composite indexes for the executor, reconciler, purge and board reads."""
	for fields in (
		["conversation", "status"],
		["owner_user", "status"],
		["status", "executing_at"],
		["settled", "status"],
		["kind", "status"],
	):
		frappe.db.add_index("Jarvis Pending Action", fields)
