"""Landed-cost checks for generic tool RPCs, separate from ordinary Desk saves."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from jarvis.exceptions import PermissionDeniedError

_TOOL_SCOPE = ContextVar("jarvis_lcv_tool_write", default=False)
LCV = "Landed Cost Voucher"
_MUTATORS = frozenset(
	{
		"frappe.client.insert",
		"frappe.client.insert_many",
		"frappe.client.save",
		"frappe.client.submit",
		"frappe.client.set_value",
		"frappe.desk.form.save.savedocs",
		"frappe.model.workflow.apply_workflow",
		"frappe.model.workflow.bulk_workflow_approval",
		"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
	}
)


@contextmanager
def tool_scope():
	token = _TOOL_SCOPE.set(True)
	try:
		yield
	finally:
		_TOOL_SCOPE.reset(token)


def _targets_lcv(value, depth=0):
	if depth > 4:
		return False
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			return False
	if isinstance(value, dict):
		if value.get("doctype") == LCV or value.get("dt") == LCV:
			return True
		return any(_targets_lcv(value.get(key), depth + 1) for key in ("doc", "docs", "documents", "doclist"))
	if isinstance(value, list):
		return any(_targets_lcv(item, depth + 1) for item in value)
	return False


def reject_generic_lcv_write(method, args):
	# Bulk workflow endpoints can enqueue work beyond the current request. Refuse
	# known LCV mutation routes before dispatch rather than lose the scope in a job.
	if method in _MUTATORS and _targets_lcv(args):
		raise PermissionDeniedError(
			"Use the landed-cost preview and the dedicated create, update or submit tools for this voucher."
		)


def guard_tool_document(doc, method=None):
	if not _TOOL_SCOPE.get() or doc.doctype != LCV:
		return
	from jarvis.tools._lcv_source_tax import gate_lcv

	gate_lcv(doc, lock=True)
