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
_MUTATION_PTYPES = frozenset({"create", "write", "submit", "cancel", "amend", "delete"})
_MAX_PLANNER_WAREHOUSES = 10
_MAX_NATIVE_PLAN_QUERY_ROWS = 2_000


def _plan_evidence(approval) -> dict | None:
	"""Return only a structurally valid sealed count-plan evidence object.

	This module is also called from permission hooks.  It must therefore fail
	closed without importing the count-entry controller (which calls this module
	from its own access checks).  The entry controller repeats the stronger
	write-time validation before it records a fact.
	"""
	try:
		evidence = frappe.parse_json(approval.get("assessment_evidence") or "{}")
	except (TypeError, ValueError):
		return None
	if not isinstance(evidence, dict):
		return None
	finding = evidence.get("finding")
	proposal = finding.get("proposal") if isinstance(finding, dict) else None
	if (
		approval.get("source") != "Agent Review"
		or approval.get("agent") != CYCLE_COUNT_PLANNER
		or approval.get("result_class") != "derived_candidate"
		or approval.get("ref_doctype") != WAREHOUSE
		or not isinstance(approval.get("ref_name"), str)
		or not approval.get("ref_name")
		or not isinstance(finding, dict)
		or finding.get("ref_name") != approval.get("ref_name")
		or not isinstance(proposal, dict)
		or proposal.get("outcome") != "plan"
		or not isinstance(proposal.get("lines"), list)
		or not isinstance(evidence.get("scope"), dict)
		or not isinstance(evidence["scope"].get("company"), str)
		or not evidence["scope"]["company"]
		or not isinstance(evidence.get("reviewer"), str)
		or not evidence["reviewer"]
	):
		return None
	return evidence


def _has_native_plan_read(approval, evidence: dict, user: str) -> bool:
	"""Require current native Warehouse and Item read for a sealed plan.

	The plan embeds current book quantities and stock identifiers.  An approval
	share is deliberately not a substitute for the ordinary stock-record read
	permission.  Check every planned Item, rather than only the visible header,
	so a partial permission set never reveals a partial count plan.
	"""
	warehouse = approval.get("ref_name")
	if not frappe.has_permission(WAREHOUSE, "read", doc=warehouse, user=user):
		return False
	proposal = evidence["finding"]["proposal"]
	lines = proposal.get("lines")
	if not isinstance(lines, list) or not lines:
		return False
	seen = set()
	for line in lines:
		item_code = line.get("item_code") if isinstance(line, dict) else None
		if not isinstance(item_code, str) or not item_code or item_code in seen:
			return False
		seen.add(item_code)
		if not frappe.has_permission("Item", "read", doc=item_code, user=user):
			return False
	return True


def may_read_count_plan(approval, user: str | None = None, *, lock_share: bool = False) -> bool:
	"""Return whether ``user`` may read a full Cycle-Count plan.

	A plan boundary (conversation owner, sealed reviewer, or exact approval
	DocShare) grants *plan membership* only.  Native Warehouse and Item read is
	required as a second boundary for every detail, approval context, and stored
	physical-count fact.  Administrator remains the framework superuser path.
	"""
	user = user or frappe.session.user
	if not isinstance(user, str) or not user:
		return False
	if user == "Administrator":
		return True
	evidence = _plan_evidence(approval)
	if not evidence:
		return False
	if frappe.db.get_value(CONVERSATION, approval.get("conversation"), "owner") != user:
		if evidence["reviewer"] != user and not has_approval_read_share(
			approval.get("name"), user, lock=lock_share
		):
			return False
	return _has_native_plan_read(approval, evidence, user)


def require_count_plan_read(approval, user: str | None = None, *, lock_share: bool = False) -> dict:
	"""Raise a non-disclosing error unless the full count-plan read gate passes."""
	if not may_read_count_plan(approval, user, lock_share=lock_share):
		frappe.throw("You are not allowed to view this count plan.", frappe.PermissionError)
	return _plan_evidence(approval) or {}


def has_approval_read_share(approval_name: str, user: str | None = None, *, lock: bool = False) -> bool:
	"""Return a current read grant on this exact count-plan approval.

	This is narrower than generic Approval Request permission. A System Manager
	can read approvals broadly, but a count plan stays private unless the person
	owns its conversation, is its sealed reviewer, or holds this exact DocShare.
	Controlled mutations use ``lock=True`` after their writer locks so a grant or
	revoke cannot race their final authorization.
	"""
	user = user or frappe.session.user
	if not isinstance(approval_name, str) or not approval_name or not isinstance(user, str) or not user:
		return False
	return bool(
		frappe.db.get_value(
			"DocShare",
			{
				"share_doctype": APPROVAL,
				"share_name": approval_name,
				"user": user,
				"read": 1,
			},
			"name",
			for_update=lock,
		)
	)


def _may_view_plan_count(doc, user: str) -> bool:
	"""Apply the full plan and native-stock read gate to one count fact."""
	approval_name = doc.get("approval_request")
	if not approval_name:
		return False
	try:
		approval = frappe.get_doc(APPROVAL, approval_name)
	except frappe.DoesNotExistError:
		return False
	return may_read_count_plan(approval, user)


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


def visible_count_plan_approval_names(user: str | None = None) -> tuple[str, ...]:
	"""Return readable Cycle-Count approval names for list/query predicates.

	Frappe's list query hooks are SQL predicates and cannot call the native Item
	permission engine for every JSON-encoded planned line.  Resolve that small
	sealed plan set once, then return exact approval names to the caller's SQL
	predicate.  A very large set fails closed rather than turning an incomplete
	name list into a visibility grant.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return ()
	if not isinstance(user, str) or not user:
		return ()
	rows = frappe.db.sql(
		"""
		SELECT ar.name, ar.source, ar.agent, ar.result_class, ar.ref_doctype,
		       ar.ref_name, ar.conversation, ar.assessment_evidence
		FROM `tabJarvis Approval Request` ar
		LEFT JOIN `tabJarvis Conversation` c ON c.name = ar.conversation
		WHERE ar.agent = %(agent)s
		  AND ar.source = 'Agent Review'
		  AND (
			c.owner = %(user)s
			OR JSON_UNQUOTE(JSON_EXTRACT(ar.assessment_evidence, '$.reviewer')) = %(user)s
			OR EXISTS (
				SELECT 1 FROM `tabDocShare` ds
				WHERE ds.share_doctype = 'Jarvis Approval Request'
				  AND ds.share_name = ar.name AND ds.user = %(user)s AND ds.`read` = 1
			)
		  )
		ORDER BY ar.name
		LIMIT %(limit)s
		""",
		{"agent": CYCLE_COUNT_PLANNER, "user": user, "limit": _MAX_NATIVE_PLAN_QUERY_ROWS + 1},
		as_dict=True,
	)
	if len(rows) > _MAX_NATIVE_PLAN_QUERY_ROWS:
		frappe.logger("jarvis.cycle_count_permissions").warning(
			{
				"event": "native_plan_query_limit",
				"user": user,
				"limit": _MAX_NATIVE_PLAN_QUERY_ROWS,
			}
		)
		return ()
	return tuple(row.name for row in rows if may_read_count_plan(row, user))


def count_plan_approval_query_condition(user: str | None = None, *, table: str | None = None) -> str:
	"""Return a fail-closed SQL predicate for Approval Request list paths."""
	user = user or frappe.session.user
	if user == "Administrator":
		return "1=1"
	table = table or f"{SQL_QUOTE}tab{APPROVAL}{SQL_QUOTE}"
	names = visible_count_plan_approval_names(user)
	not_cycle = f"({table}.agent IS NULL OR {table}.agent <> {frappe.db.escape(CYCLE_COUNT_PLANNER)})"
	if not names:
		return not_cycle
	quoted = ", ".join(frappe.db.escape(name) for name in names)
	return f"({not_cycle} OR {table}.name IN ({quoted}))"


def visible_physical_count_names(user: str | None = None) -> tuple[str, ...]:
	"""Return count facts whose sealed plan and native sources are both readable."""
	user = user or frappe.session.user
	names = visible_count_plan_approval_names(user)
	if not names:
		return ()
	# Do not use ``frappe.get_all(COUNT)`` here. This helper is called by the
	# Physical Count query-condition hook itself, and a normal ORM list can
	# re-enter that hook before the predicate has been built.
	rows = frappe.db.sql(
		f"SELECT name FROM {SQL_QUOTE}tab{COUNT}{SQL_QUOTE} "
		"WHERE approval_request IN %(approvals)s ORDER BY name LIMIT %(limit)s",
		{"approvals": names, "limit": _MAX_NATIVE_PLAN_QUERY_ROWS + 1},
		pluck=True,
	)
	if len(rows) > _MAX_NATIVE_PLAN_QUERY_ROWS:
		frappe.logger("jarvis.cycle_count_permissions").warning(
			{
				"event": "native_count_query_limit",
				"user": user,
				"limit": _MAX_NATIVE_PLAN_QUERY_ROWS,
			}
		)
		return ()
	return tuple(rows)


def conversation_has_retained_count_plan(conversation: str) -> bool:
	"""Lock and detect Cycle-Count state that must keep its chat authority.

	An approved plan stores its ownership boundary through the conversation.  A
	clear-history delete would otherwise retain a fact while making the owner
	unable to list, read, or correct it.  Lock a running planner run first so a
	concurrent staging path cannot add a pending approval after this check and
	before the conversation is deleted.
	"""
	if not isinstance(conversation, str) or not conversation:
		return False
	live_runs = frappe.db.sql(
		"""
		SELECT name FROM `tabJarvis Agent Run`
		WHERE conversation = %(conversation)s AND agent = %(agent)s AND status = 'running'
		FOR UPDATE
		""",
		{"conversation": conversation, "agent": CYCLE_COUNT_PLANNER},
		pluck=True,
	)
	if live_runs:
		return True
	plans = frappe.db.sql(
		"""
		SELECT ar.name FROM `tabJarvis Approval Request` ar
		WHERE ar.conversation = %(conversation)s
		  AND ar.agent = %(agent)s
		  AND ar.source = 'Agent Review'
		  AND ar.status IN ('Pending', 'Approved')
		FOR UPDATE
		""",
		{"conversation": conversation, "agent": CYCLE_COUNT_PLANNER},
		pluck=True,
	)
	if plans:
		return True
	return bool(
		frappe.db.sql(
			"""
			SELECT pc.name FROM `tabJarvis Physical Count` pc
			INNER JOIN `tabJarvis Approval Request` ar ON ar.name = pc.approval_request
			WHERE ar.conversation = %(conversation)s AND ar.agent = %(agent)s
			LIMIT 1 FOR UPDATE
			""",
			{"conversation": conversation, "agent": CYCLE_COUNT_PLANNER},
			pluck=True,
	)
	)


def _stock_variance_query_condition(scope, count_table: str) -> str:
	"""The matching submitted count headers for one Warehouse/date scope."""
	company = frappe.db.escape(scope.company)
	start = frappe.db.escape(f"{scope.from_date.isoformat()} 00:00:00")
	stop = frappe.db.escape(f"{(scope.to_date + timedelta(days=1)).isoformat()} 00:00:00")
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


def _planner_query_condition(scope, count_table: str) -> str:
	"""The submitted historical headers for a planner's exact leaf warehouses."""
	company = frappe.db.escape(scope.company)
	history_from = frappe.db.escape(f"{scope.count_history_from.isoformat()} 00:00:00")
	warehouses = ", ".join(frappe.db.escape(name) for name in scope.warehouses)
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


def _physical_count_visibility_condition(user: str, count_table: str) -> str:
	"""Return the full private count predicate for one trusted SQL table."""
	conditions = []
	plan_names = visible_physical_count_names(user)
	if plan_names:
		quoted = ", ".join(frappe.db.escape(name) for name in plan_names)
		conditions.append(f"{count_table}.name IN ({quoted})")
	if scope := _stock_variance_delegate_scope(user):
		conditions.append(_stock_variance_query_condition(scope, count_table))
	if scope := _cycle_count_planner_delegate_scope(user):
		conditions.append(_planner_query_condition(scope, count_table))
	return "(" + " or ".join(conditions) + ")" if conditions else "1=0"


def physical_count_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""Limit lists to native-readable plans or an active delegate's scope.

	The human plan path resolves exact readable approval names before it builds a
	SQL predicate.  This keeps Item and Warehouse permissions in force for list,
	report, and generic REST queries; a bare approval share is not enough.  The
	delegate paths remain narrow server-bound source allowances.  Administrator
	is Frappe's unavoidable superuser path and is not used by product flows.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	count_table = f"{SQL_QUOTE}tab{COUNT}{SQL_QUOTE}"
	return _physical_count_visibility_condition(user, count_table)


def physical_count_item_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""Limit child-table lists to rows whose count header is visible.

	Frappe checks a child list's parent DocType permission without a parent row.
	The parent query condition is therefore not applied to a direct generic child
	list. Correlate each child row to its header and reuse the exact header rule.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	count_table = f"{SQL_QUOTE}tab{COUNT}{SQL_QUOTE}"
	item_table = f"{SQL_QUOTE}tabJarvis Physical Count Item{SQL_QUOTE}"
	return (
		f"exists (select 1 from {count_table} count_header "
		f"where count_header.{SQL_QUOTE}name{SQL_QUOTE} = {item_table}.{SQL_QUOTE}parent{SQL_QUOTE} "
		f"and {item_table}.{SQL_QUOTE}parenttype{SQL_QUOTE} = {frappe.db.escape(COUNT)} "
		f"and {item_table}.{SQL_QUOTE}parentfield{SQL_QUOTE} = {frappe.db.escape('items')} "
		f"and {_physical_count_visibility_condition(user, 'count_header')})"
	)


def has_physical_count_permission(doc, ptype: str = "read", user: str | None = None, debug=False) -> bool:
	"""Deny access unless the caller can view its plan or active delegate scope.

	All writes must start at the controlled count endpoints. This hook runs before
	Frappe's ``check_if_latest`` locks an existing draft or submitted fact, so it
	must reject a Desk, REST, or ORM write here rather than in a document hook.
	"""
	user = user or frappe.session.user
	if doc and ptype in _MUTATION_PTYPES:
		# Import lazily to avoid importing the entry controller while hooks load.
		# The marker is a module object identity, never a client-controlled flag.
		from jarvis.chat.cycle_count_entry import is_controlled_count_mutation

		if not is_controlled_count_mutation(doc):
			return False
	if user == "Administrator" or not doc:
		return True
	if ptype in _DYNAMIC_READ_PTYPES:
		return _may_view_count(doc, user)
	# The delegate allowance is read-only. The controller applies the tighter
	# recorder/reviewer rule for every mutation, and a source run must never
	# make a count writable, submittable, cancellable, or deletable.
	return _may_view_plan_count(doc, user)
