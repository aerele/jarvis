"""Script Report over the append-only agent-write audit trail.

Gives a Jarvis Admin one-click Desk export (Excel / CSV / PDF), print and chart
of what the agent wrote to ERP data — the bulk-export twin of the in-app Agent
Audit pane. Metadata only: the column list is the same explicit safe set the
whitelisted read uses, so the report can never surface conversation content
(the doctype has no such columns anyway)."""

import frappe
from frappe import _

from jarvis.permissions import require_jarvis_admin

_SAFE_FIELDS = (
	"at",
	"actor",
	"actor_name",
	"tool",
	"outcome",
	"provenance",
	"provenance_name",
	"ref_doctype",
	"ref_name",
	"bulk_count",
	"model",
)
_OUTCOMES = frozenset({"applied", "failed", "discarded"})


def _columns():
	return [
		{"fieldname": "at", "label": _("When"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "actor", "label": _("Actor"), "fieldtype": "Link", "options": "User", "width": 180},
		{"fieldname": "actor_name", "label": _("Actor Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "tool", "label": _("Tool"), "fieldtype": "Data", "width": 130},
		{"fieldname": "outcome", "label": _("Outcome"), "fieldtype": "Data", "width": 90},
		{"fieldname": "provenance", "label": _("Provenance"), "fieldtype": "Data", "width": 100},
		{"fieldname": "provenance_name", "label": _("Provenance Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "ref_doctype", "label": _("Reference Doctype"), "fieldtype": "Data", "width": 160},
		{"fieldname": "ref_name", "label": _("Reference Name"), "fieldtype": "Data", "width": 160},
		{"fieldname": "bulk_count", "label": _("Bulk Count"), "fieldtype": "Int", "width": 90},
		{"fieldname": "model", "label": _("Model"), "fieldtype": "Data", "width": 150},
	]


def execute(filters=None):
	# The Report doctype's roles already gate WHO can open this; re-check because
	# execute() is reachable programmatically, and match the pane's exact readers.
	require_jarvis_admin()
	filters = filters or {}
	query: dict = {}

	actor = filters.get("actor")
	actor = str(actor).strip() if actor else ""
	if actor:
		query["actor"] = actor

	outcome = filters.get("outcome")
	if outcome in _OUTCOMES:
		query["outcome"] = outcome

	# Optional date range on the indexed `at` column (from_date / to_date, the
	# Frappe filter convention). Half-open ranges are honoured.
	from_date = filters.get("from_date")
	to_date = filters.get("to_date")
	if from_date and to_date:
		query["at"] = ["between", [from_date, to_date]]
	elif from_date:
		query["at"] = [">=", from_date]
	elif to_date:
		query["at"] = ["<=", to_date]

	data = frappe.get_all(
		"Jarvis Agent Write",
		filters=query,
		fields=list(_SAFE_FIELDS),
		order_by="at desc",
	)
	return _columns(), data
