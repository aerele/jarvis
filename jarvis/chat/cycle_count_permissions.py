"""Row-level access control for submitted Cycle-Count observations.

Jarvis Physical Count has Stock User DocPerms so a person with the normal stock
role can record an observation. The observation remains private to its approved
plan in Desk and REST. The Stock Variance and Cycle-Count Planner delegates get
separate, short-lived source-read allowances only while their server-bound agent
runs are running. They never give a run-as user general Desk access to count
facts.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe

COUNT = "Jarvis Physical Count"
APPROVAL = "Jarvis Approval Request"
CONVERSATION = "Jarvis Conversation"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
SESSION = "Jarvis Chat Session"
WAREHOUSE = "Warehouse"
STOCK_VARIANCE = "stock-variance-auditor"
CYCLE_COUNT_PLANNER = "cycle-count-planner-operator"
SQL_QUOTE = "`"

_DYNAMIC_READ_PTYPES = frozenset({"read", "select"})
_MAX_PLANNER_WAREHOUSES = 10


def _reviewer(approval) -> str | None:
	"""Return the sealed reviewer without treating malformed evidence as access."""
	try:
		evidence = frappe.parse_json(approval.get("assessment_evidence") or "{}")
	except (TypeError, ValueError):
		return None
	return evidence.get("reviewer") if isinstance(evidence, dict) else None


def _may_view_plan_count(doc, user: str) -> bool:
	"""Mirror the Cycle-Count plan visibility rule for one submitted fact."""
	approval_name = doc.get("approval_request")
	if not approval_name:
		return False
	try:
		approval = frappe.get_doc(APPROVAL, approval_name)
	except frappe.DoesNotExistError:
		return False
	if frappe.db.get_value(CONVERSATION, approval.conversation, "owner") == user:
		return True
	if _reviewer(approval) == user:
		return True
	return False


def _iso_date(value) -> date | None:
	"""Parse one required ISO day without falling back to a default."""
	if not isinstance(value, str):
		return None
	try:
		return date.fromisoformat(value)
	except ValueError:
		return None


def _scope_from_run_stamp(scope_json, user: str):
	"""Return one explicit date-bounded Warehouse subtree from a Run scope.

	The function deliberately does not inspect an installation config. A scope
	must already be stamped on the running server-side Run, and it must name a
	company, inclusive ISO date range, and Warehouse record scope. Missing,
	malformed, or cross-company data has no source-read effect.
	"""
	try:
		scope = frappe.parse_json(scope_json or "{}")
	except (TypeError, ValueError):
		return None
	if not isinstance(scope, dict):
		return None
	company = str(scope.get("company") or "").strip()
	from_date, to_date = _iso_date(scope.get("from_date")), _iso_date(scope.get("to_date"))
	record_scope = scope.get("record_scope")
	if not company or not from_date or not to_date or from_date > to_date:
		return None
	if not isinstance(record_scope, dict) or record_scope.get("doctype") != WAREHOUSE:
		return None
	warehouse_name = str(record_scope.get("name") or "").strip()
	if not warehouse_name:
		return None
	warehouse = frappe.db.get_value(
		WAREHOUSE,
		warehouse_name,
		["name", "company", "lft", "rgt"],
		as_dict=True,
	)
	if not warehouse or warehouse.company != company:
		return None
	try:
		lft, rgt = int(warehouse.lft), int(warehouse.rgt)
	except (TypeError, ValueError):
		return None
	if lft > rgt:
		return None
	# Scope resolution checks the selected record under the run-as identity.
	# Recheck it here so a later access change revokes this ephemeral allowance.
	if not frappe.has_permission(WAREHOUSE, "read", doc=warehouse.name, user=user):
		return None
	return frappe._dict(
		company=company,
		warehouse=warehouse.name,
		lft=lft,
		rgt=rgt,
		from_date=from_date,
		to_date=to_date,
	)


def _active_delegate_run(agent: str, user: str):
	"""Return an active, server-bound delegate run for the current tool dispatch.

	The agent-run context key is set only by api._dispatch_from_session after the
	server resolved the plugin bearer to a chat-session row. It is absent for
	Desk, REST, direct Python, and every ordinary employee request. All row data
	below is re-read on each permission decision, so a terminal run, deleted
	session, disabled installation, or remapped run-as user immediately loses the
	allowance.
	"""
	from jarvis.tools._agent_run_ctx import get_session_key

	# A direct check for another user must not borrow this delegate allowance.
	if user != frappe.session.user:
		return None
	session_key = get_session_key()
	if not session_key:
		return None
	run = frappe.db.get_value(
		RUN,
		{"session_key": session_key, "status": "running", "agent": agent},
		["name", "installation", "scope_json"],
		as_dict=True,
	)
	if not run or not run.installation:
		return None
	installation = frappe.db.get_value(
		INSTALLATION,
		run.installation,
		["agent", "run_as_user", "enabled"],
		as_dict=True,
	)
	if (
		not installation
		or installation.agent != agent
		or installation.run_as_user != user
		or not frappe.utils.cint(installation.enabled)
	):
		return None
	# A Run row alone is not a bearer credential. The matching server-minted
	# session must still bind this key to the impersonated run-as user.
	if frappe.db.get_value(SESSION, {"session_key": session_key}, "user") != user:
		return None
	return run


def _stock_variance_delegate_scope(user: str):
	"""Return the active Stock Variance source scope for this tool dispatch."""
	run = _active_delegate_run(STOCK_VARIANCE, user)
	return _scope_from_run_stamp(run.scope_json, user) if run else None


def _planner_scope_from_run_stamp(scope_json, user: str):
	"""Return the planner's exact historical leaf-warehouse source scope.

	Like the Stock Variance path, this reads only the immutable-at-launch Run
	scope. A planner needs submitted counts from its configured leaf warehouses
	back to its stamped count-history date; it does not gain any general physical
	count access.
	"""
	try:
		scope = frappe.parse_json(scope_json or "{}")
	except (TypeError, ValueError):
		return None
	if not isinstance(scope, dict):
		return None
	company = str(scope.get("company") or "").strip()
	history_from = _iso_date(scope.get("count_history_from"))
	warehouses = scope.get("warehouses")
	if (
		not company
		or not history_from
		or not isinstance(warehouses, list)
		or not 1 <= len(warehouses) <= _MAX_PLANNER_WAREHOUSES
	):
		return None
	names = [str(name).strip() if isinstance(name, str) else "" for name in warehouses]
	if not all(names) or len(set(names)) != len(names):
		return None
	rows = {
		row.name: row
		for row in frappe.get_all(
			WAREHOUSE,
			filters={"name": ["in", names]},
			fields=["name", "company", "is_group", "disabled"],
		)
	}
	for name in names:
		warehouse = rows.get(name)
		if (
			not warehouse
			or warehouse.company != company
			or frappe.utils.cint(warehouse.is_group)
			or frappe.utils.cint(warehouse.disabled)
			or not frappe.has_permission(WAREHOUSE, "read", doc=name, user=user)
		):
			return None
	return frappe._dict(company=company, warehouses=tuple(names), count_history_from=history_from)


def _cycle_count_planner_delegate_scope(user: str):
	"""Return the active Cycle-Count Planner source scope for this dispatch."""
	run = _active_delegate_run(CYCLE_COUNT_PLANNER, user)
	return _planner_scope_from_run_stamp(run.scope_json, user) if run else None


def _within_stock_variance_scope(doc, scope) -> bool:
	"""Whether a submitted count is inside an active delegate's scope."""
	if frappe.utils.cint(doc.get("docstatus")) != 1 or doc.get("company") != scope.company:
		return False
	try:
		counted_at = frappe.utils.get_datetime(doc.get("counted_at"))
		counted_day = counted_at.date()
	except (TypeError, ValueError, AttributeError):
		return False
	if not scope.from_date <= counted_day <= scope.to_date:
		return False
	warehouse = frappe.db.get_value(
		WAREHOUSE,
		doc.get("warehouse"),
		["company", "lft", "rgt"],
		as_dict=True,
	)
	if not warehouse or warehouse.company != scope.company:
		return False
	try:
		return scope.lft <= int(warehouse.lft) and int(warehouse.rgt) <= scope.rgt
	except (TypeError, ValueError):
		return False


def _within_planner_scope(doc, scope) -> bool:
	"""Whether a submitted count is within the planner's history input."""
	if (
		frappe.utils.cint(doc.get("docstatus")) != 1
		or doc.get("company") != scope.company
		or doc.get("warehouse") not in scope.warehouses
	):
		return False
	try:
		return frappe.utils.get_datetime(doc.get("counted_at")).date() >= scope.count_history_from
	except (TypeError, ValueError, AttributeError):
		return False


def _may_view_count(doc, user: str) -> bool:
	"""Allow the plan boundary, or the relevant active delegate source scope."""
	if _may_view_plan_count(doc, user):
		return True
	if scope := _stock_variance_delegate_scope(user):
		if _within_stock_variance_scope(doc, scope):
			return True
	if scope := _cycle_count_planner_delegate_scope(user):
		return _within_planner_scope(doc, scope)
	return False


def _plan_query_condition(user: str) -> str:
	"""The plan-owner and sealed-reviewer list predicate."""
	escaped = frappe.db.escape(user)
	count_table = f"{SQL_QUOTE}tab{COUNT}{SQL_QUOTE}"
	approval_table = f"{SQL_QUOTE}tab{APPROVAL}{SQL_QUOTE}"
	conversation_table = f"{SQL_QUOTE}tab{CONVERSATION}{SQL_QUOTE}"
	approval_field = f"{SQL_QUOTE}approval_request{SQL_QUOTE}"
	return (
		f"exists (select 1 from {approval_table} approval "
		f"left join {conversation_table} conversation "
		"on conversation.name = approval.conversation "
		f"where approval.name = {count_table}.{approval_field} and ("
		f"conversation.owner = {escaped} "
		f"or JSON_UNQUOTE(JSON_EXTRACT(approval.assessment_evidence, '$.reviewer')) = {escaped}"
		"))"
	)


def _stock_variance_query_condition(scope) -> str:
	"""The matching submitted count headers for one Warehouse/date scope."""
	company = frappe.db.escape(scope.company)
	start = frappe.db.escape(f"{scope.from_date.isoformat()} 00:00:00")
	stop = frappe.db.escape(f"{(scope.to_date + timedelta(days=1)).isoformat()} 00:00:00")
	count_table = f"{SQL_QUOTE}tab{COUNT}{SQL_QUOTE}"
	warehouse_table = f"{SQL_QUOTE}tabWarehouse{SQL_QUOTE}"
	docstatus_field = f"{SQL_QUOTE}docstatus{SQL_QUOTE}"
	company_field = f"{SQL_QUOTE}company{SQL_QUOTE}"
	counted_at_field = f"{SQL_QUOTE}counted_at{SQL_QUOTE}"
	warehouse_field = f"{SQL_QUOTE}warehouse{SQL_QUOTE}"
	lft_field = f"{SQL_QUOTE}lft{SQL_QUOTE}"
	rgt_field = f"{SQL_QUOTE}rgt{SQL_QUOTE}"
	return (
		f"({count_table}.{docstatus_field} = 1 "
		f"and {count_table}.{company_field} = {company} "
		f"and {count_table}.{counted_at_field} >= {start} "
		f"and {count_table}.{counted_at_field} < {stop} "
		f"and exists (select 1 from {warehouse_table} scoped_warehouse "
		f"where scoped_warehouse.name = {count_table}.{warehouse_field} "
		f"and scoped_warehouse.company = {company} "
		f"and scoped_warehouse.{lft_field} >= {scope.lft} "
		f"and scoped_warehouse.{rgt_field} <= {scope.rgt}))"
	)


def _planner_query_condition(scope) -> str:
	"""The submitted historical headers for a planner's exact leaf warehouses."""
	company = frappe.db.escape(scope.company)
	history_from = frappe.db.escape(f"{scope.count_history_from.isoformat()} 00:00:00")
	warehouses = ", ".join(frappe.db.escape(name) for name in scope.warehouses)
	count_table = f"{SQL_QUOTE}tab{COUNT}{SQL_QUOTE}"
	docstatus_field = f"{SQL_QUOTE}docstatus{SQL_QUOTE}"
	company_field = f"{SQL_QUOTE}company{SQL_QUOTE}"
	counted_at_field = f"{SQL_QUOTE}counted_at{SQL_QUOTE}"
	warehouse_field = f"{SQL_QUOTE}warehouse{SQL_QUOTE}"
	return (
		f"({count_table}.{docstatus_field} = 1 "
		f"and {count_table}.{company_field} = {company} "
		f"and {count_table}.{warehouse_field} in ({warehouses}) "
		f"and {count_table}.{counted_at_field} >= {history_from})"
	)


def physical_count_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""Limit lists to a private plan or an active delegate's stamped scope.

	The reviewer is sealed in the approval evidence. JSON_EXTRACT returns NULL
	for malformed evidence on the supported MariaDB version, which fails closed.
	No System Manager bypass is present; Administrator is Frappe's unavoidable
	superuser bypass and is not used by normal product paths.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	conditions = [_plan_query_condition(user)]
	if scope := _stock_variance_delegate_scope(user):
		conditions.append(_stock_variance_query_condition(scope))
	if scope := _cycle_count_planner_delegate_scope(user):
		conditions.append(_planner_query_condition(scope))
	return "(" + " or ".join(conditions) + ")"


def has_physical_count_permission(doc, ptype: str = "read", user: str | None = None, debug=False) -> bool:
	"""Deny access unless the caller can view its plan or active delegate scope.

	Frappe calls this hook only with a document. A new document has no approved
	plan yet, so create is left to the existing role and controller checks.
	"""
	user = user or frappe.session.user
	if user == "Administrator" or ptype == "create" or not doc:
		return True
	if ptype in _DYNAMIC_READ_PTYPES:
		return _may_view_count(doc, user)
	# The delegate allowance is read-only. The controller applies the tighter
	# recorder/reviewer rule for every mutation, and a source run must never
	# make a count writable, submittable, cancellable, or deletable.
	return _may_view_plan_count(doc, user)
