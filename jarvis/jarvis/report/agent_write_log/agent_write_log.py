"""Script Report over the append-only agent-write audit trail.

Gives a Jarvis Admin one-click Desk export (Excel / CSV / PDF), print and chart
of what the agent wrote to ERP data — the bulk-export twin of the in-app Agent
Audit pane. Metadata only: the fetched columns are the SAME shared safe field
list the whitelisted read uses, so the report can never surface conversation
content (the doctype has no such columns anyway)."""

import frappe
from frappe import _
from frappe.utils import getdate

from jarvis.agent_audit import AGENT_WRITE_FIELDS, OUTCOMES
from jarvis.permissions import require_jarvis_admin


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
		query["actor"] = actor  # Desk Link(User) filter → exact id

	outcome = filters.get("outcome")
	if outcome in OUTCOMES:
		query["outcome"] = outcome

	# Date range on the indexed `at` column; coerce via getdate (defense-in-depth
	# parity with the whitelisted read — the query builder also parameterizes).
	frm = getdate(filters["from_date"]) if filters.get("from_date") else None
	to = getdate(filters["to_date"]) if filters.get("to_date") else None
	if frm and to:
		query["at"] = ["between", [frm, to]]
	elif frm:
		query["at"] = [">=", frm]
	elif to:
		query["at"] = ["<=", to]

	data = frappe.get_all(
		"Jarvis Agent Write",
		filters=query,
		fields=list(AGENT_WRITE_FIELDS),
		order_by="at desc, name desc",
	)
	return _columns(), data
