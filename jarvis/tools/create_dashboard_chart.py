"""Create a Frappe Dashboard Chart from a doctype's data.

Turns a "show me X over time / grouped by Y" request into a real, saved
Dashboard Chart the customer can pin to a dashboard - instead of dumping numbers
into chat. Supports the common shapes:

  - Count / Sum / Average over time   (a date field on the doctype)
  - Group By                          (count/sum/average per category)

Runs under the calling user: ``doc.insert()`` enforces create permission on
Dashboard Chart, and we check read permission on the charted doctype.
"""

import frappe
from frappe.desk.doctype.dashboard_chart.dashboard_chart import get_parent_doctypes

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

_CHART_TYPES = {"Count", "Sum", "Average", "Group By"}
_RENDER_TYPES = {"Line", "Bar", "Percentage", "Pie", "Donut", "Heatmap"}
_TIMESPANS = {"Last Year", "Last Quarter", "Last Month", "Last Week", "Select Date Range"}
_INTERVALS = {"Yearly", "Quarterly", "Monthly", "Weekly", "Daily"}
_GROUP_TYPES = {"Count", "Sum", "Average"}


def create_dashboard_chart(
	chart_name: str,
	document_type: str,
	chart_type: str = "Count",
	type: str = "Bar",
	based_on: str | None = None,
	value_based_on: str | None = None,
	group_by_based_on: str | None = None,
	group_by_type: str = "Count",
	aggregate_function_based_on: str | None = None,
	number_of_groups: int = 0,
	timespan: str = "Last Year",
	time_interval: str = "Monthly",
	filters: dict | list | None = None,
	is_public: int = 0,
	parent_document_type: str | None = None,
) -> dict:
	"""Create a Dashboard Chart; return {name, chart_name, chart_type, url}.

	``chart_type``: Count | Sum | Average (time series - need ``based_on`` date
	field; Sum/Average also need ``value_based_on`` numeric field) or ``Group
	By`` (need ``group_by_based_on``; Sum/Average ``group_by_type`` also needs
	``aggregate_function_based_on``). ``type`` is the render style. ``filters``
	is an optional Frappe filter dict/list scoping the charted records.
	``parent_document_type`` is required when ``document_type`` is a child
	table (e.g. charting "Sales Order Item" grouped by "item_code" needs
	``parent_document_type="Sales Order"``) - Frappe reads a child table's
	rows through its owning parent, and rejects the chart without it.
	"""
	if not chart_name:
		raise InvalidArgumentError("chart_name is required")
	if not document_type:
		raise InvalidArgumentError("document_type is required")
	if chart_type not in _CHART_TYPES:
		raise InvalidArgumentError(f"chart_type must be one of {sorted(_CHART_TYPES)}")
	if type not in _RENDER_TYPES:
		raise InvalidArgumentError(f"type must be one of {sorted(_RENDER_TYPES)}")
	# Branch on istable directly - Dashboard Chart.validate() itself gates the
	# "needs a parent" requirement on istable, and get_parent_doctypes() (a
	# live-Table-field scan) can diverge from it both ways: an istable
	# doctype whose owning field was since deleted/renamed still needs a
	# parent even though the scan now returns []; a doctype only reachable
	# via a Table MultiSelect shows up in the scan but is NOT istable and
	# does not need one. get_parent_doctypes() is used below only to name/
	# validate candidate parents, never to decide "is this a child table".
	if frappe.get_meta(document_type).istable:
		# Child (istable) doctypes carry no permissions of their own - Frappe
		# derives read access from an owning PARENT via
		# has_permission(child, ptype="read", parent_doctype=P) (the same call
		# get_list.py's _readable_child_parents / query.py's step-3 child-table
		# gate use). The plain has_permission(document_type, "read") above is
		# never reached for a child doctype: with no parent_doctype it returns
		# False for every non-admin regardless of real access, which would make
		# every child-table chart Administrator-only.
		valid_parents = get_parent_doctypes(document_type)
		if not parent_document_type:
			# Name only parents the caller can actually read the child
			# through - naming an unreadable one would disclose a schema
			# relationship to a DocType they have no access to.
			readable = [
				p
				for p in valid_parents
				if frappe.has_permission(document_type, ptype="read", parent_doctype=p)
			]
			if not readable:
				raise PermissionDeniedError(
					f"no read permission on {document_type!r} through any parent DocType"
				)
			raise InvalidArgumentError(
				f"{document_type!r} is a child table; pass parent_document_type "
				f"(one of: {', '.join(readable)})"
			)
		if not frappe.db.exists("DocType", parent_document_type):
			raise InvalidArgumentError(f"no such DocType {parent_document_type!r}")
		if parent_document_type not in valid_parents:
			# Deliberately do not list valid_parents here: the caller supplied
			# this value themselves, but the full candidate list may include a
			# parent they cannot read.
			raise InvalidArgumentError(
				f"parent_document_type {parent_document_type!r} is not a parent of {document_type!r}"
			)
		if not frappe.has_permission(document_type, ptype="read", parent_doctype=parent_document_type):
			raise PermissionDeniedError(
				f"no read permission on {document_type!r} through parent {parent_document_type!r}"
			)
	else:
		if parent_document_type:
			raise InvalidArgumentError(f"{document_type!r} is not a child table; omit parent_document_type")
		if not frappe.has_permission(document_type, "read"):
			raise PermissionDeniedError(f"no read permission on {document_type!r}")

	doc = frappe.new_doc("Dashboard Chart")
	doc.chart_name = chart_name
	doc.chart_type = chart_type
	doc.document_type = document_type
	doc.type = type
	doc.is_public = 1 if is_public else 0
	doc.filters_json = frappe.as_json(filters if filters is not None else [])
	if parent_document_type:
		doc.parent_document_type = parent_document_type

	if chart_type in ("Count", "Sum", "Average"):
		if not based_on:
			raise InvalidArgumentError(f"{chart_type} charts need a date field in 'based_on'")
		if timespan not in _TIMESPANS:
			raise InvalidArgumentError(f"timespan must be one of {sorted(_TIMESPANS)}")
		if time_interval not in _INTERVALS:
			raise InvalidArgumentError(f"time_interval must be one of {sorted(_INTERVALS)}")
		doc.based_on = based_on
		doc.timeseries = 1
		doc.timespan = timespan
		doc.time_interval = time_interval
		if chart_type in ("Sum", "Average"):
			if not value_based_on:
				raise InvalidArgumentError(f"{chart_type} charts need a numeric field in 'value_based_on'")
			doc.value_based_on = value_based_on
	else:  # Group By
		if not group_by_based_on:
			raise InvalidArgumentError("Group By charts need a field in 'group_by_based_on'")
		if group_by_type not in _GROUP_TYPES:
			raise InvalidArgumentError(f"group_by_type must be one of {sorted(_GROUP_TYPES)}")
		doc.group_by_based_on = group_by_based_on
		doc.group_by_type = group_by_type
		if group_by_type in ("Sum", "Average"):
			if not aggregate_function_based_on:
				raise InvalidArgumentError(
					f"group_by_type {group_by_type} needs a numeric field in 'aggregate_function_based_on'"
				)
			doc.aggregate_function_based_on = aggregate_function_based_on
		if number_of_groups:
			doc.number_of_groups = int(number_of_groups)

	doc.insert()
	return {
		"name": doc.name,
		"chart_name": doc.chart_name,
		"chart_type": doc.chart_type,
		"url": f"/app/dashboard-chart/{doc.name}",
	}
