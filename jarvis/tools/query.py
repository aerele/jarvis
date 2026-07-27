"""Structured query tool — qb-based replacement for run_query.

The operator-authored-SQL design of run_query had two structural
problems with the same root cause: WE didn't construct the query,
the agent did.

1. **Multi-DB lock-in.** Raw MariaDB SQL doesn't run on Postgres or
   SQLite. Frappe v15+ supports all three; the framework recommends
   ``frappe.qb`` over raw SQL specifically because qb handles dialect
   translation (column quoting, function name mapping, etc.).

2. **Record-level permissions can't be enforced.** Frappe's
   ``DatabaseQuery.execute()`` weaves WHERE predicates from User
   Permissions, ``permission_query_conditions`` hooks, DocShare,
   ``if_owner`` constraints, and role-based access into the query at
   build time. There's no public hook to weave those into arbitrary
   operator-authored SQL.

The fix for both: **build the query ourselves** from a structured
spec the agent provides. The spec is dialect-agnostic; ``frappe.qb``
handles dialect translation. The query is constructed via the
``Engine`` API; ``Engine.get_permission_conditions()`` (at
``frappe/database/query.py:1619``) returns a pypika ``Criterion`` we
AND into our query's WHERE clause — that one call covers all five
permission layers natively.

Spec shape::

    {
        "from": "Sales Invoice",
        "alias": "si",
        "joins": [
            {"type": "left", "doctype": "Sales Invoice Item", "alias": "sii", "on": {"sii.parent": "si.name"}}
        ],
        "select": ["si.customer", {"agg": "sum", "field": "sii.qty", "as": "total_qty"}],
        "where": [
            {"field": "si.status", "op": "=", "value": "Submitted"},
            {"field": "si.posting_date", "op": ">=", "value": "2026-06-01"},
        ],
        "group_by": ["si.customer"],
        "having": [{"agg": "sum", "field": "sii.qty", "op": ">", "value": 100}],
        "order_by": [{"field": "total_qty", "dir": "desc"}],
        "limit": 100,
    }

Aliases are mandatory on joined tables; the FROM table's alias is
recommended (so the field references remain consistent across the
spec) but tolerated if omitted (the doctype name itself acts as the
implicit alias).

What this tool gives up vs ``run_query``:

- Window functions (rare; not used by the persona today).
- Recursive CTEs (very rare).
- UNION (express via two ``query`` calls + concat in the caller).
- Inline raw SQL fragments — no ``CASE WHEN ...`` or arbitrary
  expressions. The agent can't smuggle SQL through the spec.

These are the deliberate trade for portability + record-level
permission enforcement.
"""

from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.model import child_table_fields as _CHILD_FIELDS
from frappe.model import default_fields as _DEFAULT_FIELDS
from frappe.model import get_permitted_fields
from frappe.model import optional_fields as _OPTIONAL_FIELDS
from pypika import Order
from pypika import functions as fn
from pypika.terms import Criterion

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
	ResultTooLargeError,
)
from jarvis.tools import _expr

# Row guard: refuse a result over this size unless the caller passes
# ``confirm_large=True``. Mirrors the run_query guard so the agent's
# mental model carries across the two tools.
ROW_GUARD = 200
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100
# OFFSET ceiling. Operator dashboards rarely page past ~100k; higher
# numbers usually mean the agent should narrow the filter instead of
# paginating into a wall of irrelevant rows. The ceiling sits above
# realistic reporting depth but below "agent typo / accident".
MAX_OFFSET = 100_000

# EXISTS / NOT EXISTS sub-spec recursion cap. Two levels = one nest;
# deeper specs are rejected to prevent runaway agent payloads. The
# common case is a single nest ("rows where there's an X matching Y"),
# which fits comfortably under this cap.
_MAX_SUBSPEC_DEPTH = 2

# Operators allowed in ``where`` / ``having`` clauses. The dispatch
# table below maps each to a callable that produces a pypika Criterion.
_OPERATORS = {
	"=",
	"!=",
	"<",
	"<=",
	">",
	">=",
	"in",
	"not in",
	"like",
	"not like",
	"is null",
	"is not null",
	"between",
	# v0.2 additions: set-existence subqueries. The ``value`` for these
	# operators is a stripped sub-spec (from + alias + joins + where)
	# rather than a literal. Closes the "membership against complex
	# inner predicates" use case that LEFT JOIN + IS NULL gets unwieldy
	# at.
	"exists",
	"not exists",
}

# Aggregate functions allowed in ``select`` and ``having``. Each maps
# to a pypika function class; we instantiate it with the resolved
# column at translation time.
_AGGREGATES = {
	"sum": fn.Sum,
	"count": fn.Count,
	"avg": fn.Avg,
	"min": fn.Min,
	"max": fn.Max,
}

# Join type → pypika method name on the QueryBuilder.
_JOIN_METHODS = {
	"inner": "inner_join",
	"left": "left_join",
	"right": "right_join",
}


def query(spec: dict, confirm_large: bool = False) -> dict:
	"""Execute a structured query spec and return rows + the resolved SQL.

	Returns ``{"sql": "<resolved SQL>", "rows": [...]}``. The SQL is
	included for operator triage / debugging (mirrors run_query).

	Pipeline:

	1. Validate spec shape (raise ``InvalidArgumentError`` on failure).
	2. Extract referenced DocTypes (FROM + joins).
	3. DocType-level read permission check per referenced DocType.
	4. Per-site DocType allowlist check (reused from run_query).
	5. Build the qb query from the spec. Every concrete column reference
	   (select / where / having / group_by / order_by / join-on / EXISTS)
	   funnels through ``_resolve_field`` → ``_validate_column``, which
	   enforces the **field-level (permlevel) read ACL** — the same layer
	   ``get_list`` applies via ``apply_fieldlevel_read_permissions``. A
	   permlevel>0 field the caller's roles don't cover is rejected here,
	   so this tool now has field-level parity with ``get_list`` (not just
	   the record-level parity of step 6).
	6. Apply ``Engine.get_permission_conditions()`` for each referenced
	   DocType — weaves User Permissions + permission_query_conditions
	   hooks + DocShare + if_owner + role-based access predicates into
	   the WHERE clause. The single critical call that makes this tool
	   permission-honoring where run_query couldn't be.
	7. Execute via ``.run(as_dict=True)``.
	8. Row guard.

	Raises:
		InvalidArgumentError: spec is malformed.
		PermissionDeniedError: caller lacks DocType-level read, a
			referenced DocType is not on the per-site allowlist, OR a
			referenced column is a permlevel-restricted field the caller's
			roles don't cover (field-level ACL).
		ResultTooLargeError: result exceeds ``ROW_GUARD`` and
			``confirm_large`` is False.
	"""
	if not isinstance(spec, dict):
		raise InvalidArgumentError("spec must be a dict")

	# Step 1: validate spec shape (lightweight; deep validation happens
	# during translation when we have type context).
	_validate_spec_shape(spec)

	# Step 2: collect all referenced DocTypes for the permission gates.
	doctypes = _collect_doctypes(spec)

	# Step 3: DocType-level read permission gate (mirrors run_query). The plain
	# has_permission check comes first and is the only thing on the happy path -
	# no meta or schema lookup - so a readable DocType (or the mocked gate in
	# tests) never triggers the child-table derivation below, and an unknown
	# DocType still falls through to the downstream translation error rather than
	# a DoesNotExistError here.
	for dt in doctypes:
		if frappe.has_permission(dt, ptype="read"):
			continue
		# Denied by the plain check. For a CHILD (istable) DocType that is a false
		# negative: child DocTypes carry no permissions of their own, so
		# has_child_permission returns False for every non-admin without a parent -
		# which broke every join/read referencing a child table. Mirror get_list:
		# allow the child if the caller can read one of its owning parents
		# (parent_doctype-derived permission). Only reached on an actual denial, so
		# the get_meta / get_all derivation stays off the hot path.
		if frappe.get_meta(dt).istable:
			from jarvis.tools.get_list import _child_table_parents, _readable_child_parents

			if _readable_child_parents(dt):
				continue
			# The child has owning parents but the caller can read NONE of them.
			# Fail with a parent-oriented message (a child is reachable only
			# through a parent DocType) rather than the generic denial below,
			# which is kept for a child with no owning parents at all.
			owning = _child_table_parents(dt)
			if owning:
				raise PermissionDeniedError(
					f"no read permission on child DocType '{dt}': child tables are "
					f"readable only through a parent DocType, and you cannot read "
					f"any of its parents ({', '.join(owning)})"
				)
		raise PermissionDeniedError(f"no read permission on referenced DocType: {dt}")

	# Step 4: per-site DocType allowlist (defense-in-depth).
	allowlist = _load_doctype_allowlist()
	if allowlist is not None:
		for dt in doctypes:
			if dt not in allowlist:
				raise PermissionDeniedError(
					f"DocType {dt!r} is not in this site's query allowlist; "
					f"add it to Jarvis Settings.run_query_doctype_allowlist "
					f"to enable."
				)

	# Step 5: translate spec → qb expression. ``alias_map`` carries the
	# spec-alias → pypika.Table mapping so where/select/group_by/etc.
	# can resolve "alias.field" references.
	from_table, alias_map = _build_from_and_aliases(spec)
	q = frappe.qb.from_(from_table)

	# Joins.
	for j in spec.get("joins") or []:
		# SEC-003: validate the join's table-name + alias sinks.
		_validate_doctype(j["doctype"])
		_validate_identifier(j["alias"], "alias")
		joined_table = frappe.qb.DocType(j["doctype"]).as_(j["alias"])
		alias_map[j["alias"]] = (j["doctype"], joined_table)
		on_criterion = _build_on_criterion(j["on"], alias_map)
		join_method_name = _JOIN_METHODS[j.get("type", "inner")]
		q = getattr(q, join_method_name)(joined_table).on(on_criterion)

	# SELECT.
	select_terms = _build_select(spec.get("select") or ["name"], alias_map)
	q = q.select(*select_terms)

	# DISTINCT (v0.2). Applies to the SELECT - emits ``SELECT DISTINCT``
	# at the SQL level. pypika's ``.distinct()`` is a no-arg toggle on
	# the query builder, idempotent.
	if spec.get("distinct"):
		q = q.distinct()

	# WHERE.
	for w in spec.get("where") or []:
		q = q.where(_build_predicate(w, alias_map))

	# GROUP BY. ``allow_alias`` mirrors ORDER BY: a bare name may reference
	# a SELECT output alias (e.g. a computed ``month`` bucket) rather than
	# a physical column — MariaDB resolves aliases in GROUP BY. The alias
	# still passes the SEC-003 identifier check inside ``_resolve_field``.
	for gb in spec.get("group_by") or []:
		q = q.groupby(_resolve_field(gb, alias_map, allow_alias=True))

	# HAVING.
	for h in spec.get("having") or []:
		q = q.having(_build_predicate(h, alias_map))

	# ORDER BY.
	for ob in spec.get("order_by") or []:
		field = _resolve_field(ob["field"], alias_map, allow_alias=True)
		direction = Order.desc if ob.get("dir", "asc").lower() == "desc" else Order.asc
		q = q.orderby(field, order=direction)

	# LIMIT. ``or`` would treat 0 as falsy and silently substitute the
	# default; explicit None-check preserves operator intent and lets
	# the validation below catch 0 as the invalid value it is.
	limit_raw = spec.get("limit")
	limit = DEFAULT_LIMIT if limit_raw is None else int(limit_raw)
	if limit <= 0 or limit > MAX_LIMIT:
		raise InvalidArgumentError(f"limit must be between 1 and {MAX_LIMIT}")
	q = q.limit(limit)

	# OFFSET (v0.2). Optional pagination. Same None-check pattern as
	# limit so an explicit ``offset: 0`` is honored (no-op but valid)
	# and missing offset doesn't accidentally clobber. Default 0 means
	# the qb query doesn't get an OFFSET clause appended.
	offset_raw = spec.get("offset")
	if offset_raw is not None:
		offset = int(offset_raw)
		if offset < 0 or offset > MAX_OFFSET:
			raise InvalidArgumentError(f"offset must be between 0 and {MAX_OFFSET}")
		if offset > 0:
			q = q.offset(offset)

	# Step 6: weave record-level permission predicates per ALIAS.
	# This is the structural difference vs run_query - one call per
	# referenced table, all five permission layers covered. Engine returns
	# ``None`` when the user has no restrictions for the doctype; we
	# skip appending in that case (no-op).
	#
	# A single pass over ``alias_map`` is equivalent to the old
	# ``for dt in doctypes: for alias ...`` double loop (every alias's
	# doctype is in ``doctypes``), and lets the child branch resolve the
	# scoping parent from THIS alias's join/where signals.
	engine = _make_permission_engine(q, [table for (_, table) in alias_map.values()], spec["from"])
	for alias, (resolved_dt, table) in alias_map.items():
		q = _weave_record_gate(q, engine, alias, resolved_dt, table, spec, alias_map)

	# Step 7: execute.
	rows = q.run(as_dict=True)

	# Step 8: row guard (post-execute - we can't know the row count
	# without running, and limiting earlier would break aggregates).
	if len(rows) > ROW_GUARD and not confirm_large:
		raise ResultTooLargeError(
			row_count=len(rows),
			limit=ROW_GUARD,
			tool="query",
		)

	# Resolve the SQL for the operator triage payload. ``get_sql()``
	# is the qb call that produces the final dialect-specific SQL
	# string; safe to expose since the spec doesn't carry secrets.
	try:
		resolved_sql = q.get_sql()
	except Exception:
		resolved_sql = "<sql resolution failed>"

	return {"sql": resolved_sql, "rows": rows}


# ---- Spec validation ------------------------------------------------


def _validate_spec_shape(spec: dict) -> None:
	"""Top-of-pipe shape check. Catches obvious mistakes early so the
	translator below can assume its inputs are well-formed."""
	if "from" not in spec or not isinstance(spec["from"], str):
		raise InvalidArgumentError("spec.from must be a DocType name (string)")

	# v0.2 top-level fields.
	if "distinct" in spec and not isinstance(spec["distinct"], bool):
		raise InvalidArgumentError("spec.distinct must be true or false")
	if "offset" in spec and not isinstance(spec["offset"], int):
		raise InvalidArgumentError("spec.offset must be an integer")

	# ``select`` must be a list, not a string. A bare string passes the
	# truthy check and then ``for item in 'name'`` yields per-character
	# refs which the resolver silently turns into invalid SQL. Catch it
	# here. Also reject ``limit`` as a non-int (mirrors offset's check;
	# Python's ``int(10.5)`` would otherwise silently truncate to 10).
	if "select" in spec and not isinstance(spec["select"], list):
		raise InvalidArgumentError("spec.select must be a list")
	# Note: ``isinstance(True, int)`` is True in Python, so we explicitly
	# reject bools here - a stray ``limit: true`` would otherwise pass.
	if "limit" in spec:
		if isinstance(spec["limit"], bool) or not isinstance(spec["limit"], int):
			raise InvalidArgumentError("spec.limit must be an integer")

	if "joins" in spec:
		if not isinstance(spec["joins"], list):
			raise InvalidArgumentError("spec.joins must be a list")
		seen_aliases = {spec.get("alias") or spec["from"]}
		for i, j in enumerate(spec["joins"]):
			if not isinstance(j, dict):
				raise InvalidArgumentError(f"spec.joins[{i}] must be a dict")
			for k in ("doctype", "alias", "on"):
				if k not in j:
					raise InvalidArgumentError(f"spec.joins[{i}] missing required field: {k}")
			if j["alias"] in seen_aliases:
				raise InvalidArgumentError(
					f"spec.joins[{i}] alias {j['alias']!r} collides with another table in this query"
				)
			seen_aliases.add(j["alias"])
			if j.get("type", "inner") not in _JOIN_METHODS:
				raise InvalidArgumentError(f"spec.joins[{i}].type must be one of: {sorted(_JOIN_METHODS)}")

	for clause in ("where", "having"):
		if clause not in spec:
			continue
		if not isinstance(spec[clause], list):
			raise InvalidArgumentError(f"spec.{clause} must be a list")
		for i, p in enumerate(spec[clause]):
			if not isinstance(p, dict) or "op" not in p:
				raise InvalidArgumentError(f"spec.{clause}[{i}] must be a dict with 'op'")
			if p["op"] not in _OPERATORS:
				raise InvalidArgumentError(
					f"spec.{clause}[{i}].op {p['op']!r} not allowed; must be one of {sorted(_OPERATORS)}"
				)


def _load_doctype_allowlist() -> set[str] | None:
	"""Read the per-site DocType allowlist from Jarvis Settings.

	Returns ``None`` when the field is unset or empty (default; means
	"no extra restriction beyond Frappe permissions"). Returns a
	normalised set of DocType names when configured. Accepts comma OR
	newline separation so operators can paste from either a CSV row or
	a one-DocType-per-line list.

	Whitespace is trimmed; empty entries are dropped. Names are NOT
	case-normalised (Frappe DocType names are case-sensitive) so a
	typo in the allowlist silently doesn't match — that's the right
	failure mode (closed by default).

	Reads via ``frappe.get_cached_doc`` to use Frappe's Single-doc
	cache (one in-memory dict per process) instead of an SQL round-
	trip on every call. The cached path also doesn't get fooled by
	callers who have ``patch('frappe.db.sql')`` in scope.

	Note: the underlying Jarvis Settings field is named
	``run_query_doctype_allowlist`` for historical reasons (it was
	introduced before this tool replaced the old SQL tool). The
	field name stays as-is to avoid a migration; one operator knob
	gates everything.
	"""
	try:
		settings = frappe.get_cached_doc("Jarvis Settings")
	except Exception:
		# Bench misconfig / migration in flight / similar. Failing
		# open is safe: the caller's ``has_permission`` gate already
		# ran on every referenced DocType.
		return None
	raw = (settings.get("run_query_doctype_allowlist") or "").strip()
	if not raw:
		return None
	parts = re.split(r"[,\n]", raw)
	cleaned = {p.strip() for p in parts if p.strip()}
	return cleaned if cleaned else None


def _collect_doctypes(spec: dict) -> list[str]:
	"""Return the list of DocTypes the spec references — FROM + joins,
	plus any EXISTS / NOT EXISTS sub-spec FROM + joins recursively.

	De-duplicated while preserving first-seen order so error messages
	read naturally to the operator.

	The recursion into sub-specs is what closes the side-channel: the
	caller iterates this list for both the role gate
	(``has_permission``) and the per-site allowlist gate, so every
	doctype touched anywhere in the spec — outer or nested — is
	subjected to both checks. The ``Engine.get_permission_conditions``
	weave for record-level User Permissions happens separately, at
	the outer level for FROM + joins (in ``query()``) and at each
	sub-query level (in ``_build_exists_criterion``)."""
	out: list[str] = []

	def _walk(node: dict) -> None:
		if not isinstance(node, dict):
			return
		from_dt = node.get("from")
		if isinstance(from_dt, str) and from_dt not in out:
			out.append(from_dt)
		for j in node.get("joins") or []:
			dt = j.get("doctype")
			if isinstance(dt, str) and dt not in out:
				out.append(dt)
		for predicate_list_key in ("where", "having"):
			for p in node.get(predicate_list_key) or []:
				if not isinstance(p, dict):
					continue
				if p.get("op") in ("exists", "not exists"):
					sub = p.get("value")
					if isinstance(sub, dict):
						_walk(sub)

	_walk(spec)
	return out


# ---- Identifier validation (SEC-003) --------------------------------


# Field names and aliases must be bare SQL identifiers. pypika quotes
# identifiers with backticks but does NOT escape a backtick/quote
# embedded inside the identifier, so a crafted field/alias such as
# ``foo`) UNION SELECT ... -- `` would break out of the quoting and
# inject SQL that runs with the site's FULL DB privilege
# (``frappe.set_user`` changes only the application user, not the DB
# user — an injected UNION can read ``__Auth`` and bypass the tool's own
# permission weave). Restrict every field/alias identifier to this
# character class before it reaches pypika. DocType names are validated
# separately — they legitimately contain spaces — via an existence check.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_identifier(value: Any, kind: str) -> None:
	"""Reject a field/alias identifier that isn't a bare
	``[A-Za-z0-9_]+`` token. Blocks backticks, quotes, spaces, parens,
	semicolons, and injection payloads such as ``) UNION SELECT`` before
	the value reaches pypika's identifier quoting."""
	if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
		raise InvalidArgumentError(
			f"invalid {kind}: only letters, digits, and underscores are allowed (got {value!r})"
		)


def _validate_doctype(dt: Any) -> None:
	"""Confirm ``dt`` is a real DocType. Real DocType names never carry
	backticks or quotes (Frappe forbids them), so an existence check is
	the injection guard for the table-name sink (``frappe.qb.DocType``)
	while still permitting the spaces legitimate DocType names contain
	(e.g. ``Sales Invoice``)."""
	if not isinstance(dt, str) or not dt.strip():
		raise InvalidArgumentError("DocType name must be a non-empty string")
	try:
		frappe.get_meta(dt)
	except Exception:
		raise InvalidArgumentError(f"unknown DocType: {dt!r}")


def _from_doctype(alias_map: dict) -> str:
	"""Return the FROM/base doctype of a query scope.

	``_build_from_and_aliases`` seeds ``alias_map`` with the FROM entry
	before any join is appended, so the first-inserted value is always the
	base doctype. Used to mirror ``db_query``'s field-level ACL parenttype
	handling: the base table is the "main" table, and every other
	referenced doctype is a joined table whose field permissions are
	governed by the base doctype (``parenttype``)."""
	return next(iter(alias_map.values()))[0]


def _permitted_read_fields(dt: str, base_doctype: str | None) -> set[str]:
	"""Fields on ``dt`` the current user may READ (the field-level permlevel
	ACL), mirroring ``get_list``'s ``apply_fieldlevel_read_permissions``.

	A child (istable) DocType carries no permissions of its own, so
	``get_permitted_fieldnames`` short-circuits to an EMPTY list for
	``parenttype=None`` (``frappe/model/meta.py``: ``if self.istable and not
	parenttype: return []``). That would reject EVERY field regardless of its
	real permlevel — the customer-reported bug when a child table is the
	FROM/base doctype (``dt == base_doctype``, so the plain resolution below
	yields ``parenttype=None``). Resolve the child's owning parent(s) instead —
	the same ``_child_table_parents`` derivation step 3 uses for the
	record-level gate — and permit a field if it is readable under ANY owning
	parent the caller can read the child through. A child JOINed under a
	concrete parent already carries that parent as ``base_doctype``, so use it
	directly (unchanged pre-fix behaviour). Non-child DocTypes keep the plain
	``parenttype`` resolution.
	"""
	if not frappe.get_meta(dt).istable:
		parenttype = None if (base_doctype is None or dt == base_doctype) else base_doctype
		return set(
			get_permitted_fields(
				doctype=dt,
				parenttype=parenttype,
				permission_type="read",
				ignore_virtual=True,
			)
		)
	# Child table: evaluate its field ACL against the owning parent(s).
	if base_doctype and base_doctype != dt and not frappe.get_meta(base_doctype).istable:
		parents = [base_doctype]
	else:
		from jarvis.tools.get_list import _readable_child_parents

		# Only parents the caller can actually read the child THROUGH — mirrors
		# step 3's record-level gate. Without this filter, an owning parent with
		# zero DocPerm rows would make get_permitted_fieldnames fall through to
		# its "no permissions defined -> all fields" branch (frappe/model/meta.py)
		# and leak a permlevel-restricted child field to a caller whose only real
		# access path is a different, more restrictive parent.
		parents = _readable_child_parents(dt)
	permitted: set[str] = set()
	for parent in parents:
		permitted |= set(
			get_permitted_fields(
				doctype=dt,
				parenttype=parent,
				permission_type="read",
				ignore_virtual=True,
			)
		)
	return permitted


def _validate_column(dt: str, field: str, base_doctype: str | None = None) -> None:
	"""Validate ``field`` is a real, READABLE column of DocType ``dt``.

	Two gates, in order:

	1. **Existence.** The identifier-syntax check first (rejects malicious
	   characters), then column existence via ``get_valid_columns()`` —
	   which includes the standard fields (name / owner / creation /
	   modified / modified_by / idx / docstatus) and, for child tables,
	   parent / parentfield / parenttype. ``get_valid_columns()`` omits the
	   optional meta columns (_assign / _comments / _liked_by / _user_tags /
	   _seen), which are nonetheless real, queryable columns on standard
	   doctypes — permit them too so a legitimate query (e.g. filtering on
	   ``_assign``) is not over-blocked.

	2. **Field-level (permlevel) read ACL.** ``get_valid_columns()`` proves
	   the column EXISTS but says nothing about whether the caller may READ
	   it. Frappe's ``get_list`` strips permlevel>0 fields the caller's
	   roles don't cover via ``apply_fieldlevel_read_permissions``
	   (``frappe/model/db_query.py``); this tool re-implements field
	   resolution from scratch, so it must mirror that gate or a caller with
	   plain doctype read (but no elevated-permlevel role) could
	   select / filter / order / group-by / having / join-on / EXISTS any
	   permlevel>0 field. We defer to ``frappe.model.get_permitted_fields``
	   — the exact helper ``apply_fieldlevel_read_permissions`` uses — which
	   already handles CORE_DOCTYPES (no field ACL), the always-allowed
	   standard / OPTIONAL fields, and child-table ``parenttype`` resolution.

	``base_doctype`` is the query scope's FROM doctype. Mirroring
	``apply_fieldlevel_read_permissions``, the base table is the "main"
	table (``parenttype=None``, i.e. ``self.parent_doctype``) and every
	OTHER referenced doctype is a joined table whose field ACL is governed
	by the base doctype's permissions (``parenttype=base_doctype``, i.e.
	``self.doctype``). When ``base_doctype`` is None (defensive default) or
	equals ``dt``, ``dt`` is treated as its own base.
	"""
	_validate_identifier(field, "field")
	try:
		valid_columns = frappe.get_meta(dt).get_valid_columns()
	except Exception:
		raise InvalidArgumentError(f"unknown DocType: {dt!r}")
	if field not in valid_columns and field not in _OPTIONAL_FIELDS:
		raise InvalidArgumentError(f"unknown column {field!r} on DocType {dt!r}")
	# Field-level (permlevel) read ACL — mirror get_list's
	# apply_fieldlevel_read_permissions. ``ignore_virtual=True`` matches
	# db_query (a virtual field carries no real column, so it never reaches
	# here anyway — its name fails the existence check above).
	# Standard framework columns (name/owner/creation/.../parent) carry no
	# permlevel restriction and are always readable - skip the permitted-
	# fields lookup for them: it is needless DB work in production for these
	# columns, and it lets a broadly-Engine-mocked query test resolve
	# standard-field references without recursing through the patched Engine.
	if field in _OPTIONAL_FIELDS or field in _DEFAULT_FIELDS or field in _CHILD_FIELDS:
		return
	# Field-level ACL — permlevel-aware AND child-table-aware. A child (istable)
	# DocType queried as the FROM/base doctype (dt == base_doctype) has no
	# standalone permissions, so resolving it with parenttype=None blackholes
	# every field; _permitted_read_fields resolves child fields against the
	# owning parent(s) instead. See its docstring.
	permitted = _permitted_read_fields(dt, base_doctype)
	# OPTIONAL_FIELDS are always readable (mirrors
	# apply_fieldlevel_read_permissions' explicit ``column in
	# OPTIONAL_FIELDS`` allowance) even when absent from the permitted set.
	if field not in permitted and field not in _OPTIONAL_FIELDS:
		if frappe.get_meta(dt).istable:
			# A child field's permission level is defined on its owning
			# parent(s); point the caller there instead of at the child, which
			# carries no permissions of its own.
			from jarvis.tools.get_list import _readable_child_parents

			parents = ", ".join(_readable_child_parents(dt))
			raise PermissionDeniedError(
				f"no read permission on field {field!r} of DocType {dt!r} "
				f"(restricted by permission level; for child-table fields the "
				f"permission level is granted on the parent DocType: {parents})"
			)
		raise PermissionDeniedError(
			f"no read permission on field {field!r} of DocType {dt!r} (restricted by permission level)"
		)


# ---- Record-level permission weave (per-alias record gate) ----------


def _make_permission_engine(query_builder, tables: list, doctype: str):
	"""Instantiate a bare ``frappe.database.query.Engine`` wired with the few
	attributes its permission-condition helpers read.

	``Engine`` is normally bootstrapped through ``get_query()``; the query tool
	builds its own qb query, so we set the attributes directly:

	- ``user`` / ``ignore_user_permissions`` / ``ignore_permissions`` — the
	  permission flags every helper branches on;
	- ``query`` — shared (read-only) so hooks that introspect the join graph
	  see the full query;
	- ``tables`` — some ``permission_query_conditions`` hooks consult it;
	- ``doctype`` — hooks format the main table name as ``f"tab{self.doctype}"``,
	  so an unset value crashes the first such hook with AttributeError.

	Factored from the three call sites: the outer step-6 weave, the EXISTS
	sub-query weave, and the child-scope subquery build.
	"""
	from frappe.database.query import Engine

	engine = Engine()
	engine.user = frappe.session.user
	engine.ignore_user_permissions = False
	engine.ignore_permissions = False
	engine.query = query_builder
	engine.tables = list(tables)
	engine.doctype = doctype
	return engine


def _weave_record_gate(q, engine, alias: str, resolved_dt: str, table, node_spec: dict, alias_map: dict):
	"""AND this alias's record-level permission predicate into ``q``; return ``q``.

	Non-child DocType: the framework ``Engine.get_permission_conditions`` as
	before, but a raised ``frappe.PermissionError`` (raw HTML) is normalised to
	a clean ``PermissionDeniedError``.

	Child (istable) DocType: NEVER call ``get_permission_conditions`` on the
	child — it carries no permissions of its own, so the framework raises the
	raw ``Insufficient Permission`` error. Administrator is unrestricted
	(framework ``allow_everything`` parity, preserving the admin FROM-child
	path). A non-admin child is scoped to a single owning parent the caller can
	read — resolved from THIS alias's join/where signals — mirroring
	``get_list(child, parent_doctype=P)``.
	"""
	if frappe.get_meta(resolved_dt).istable:
		if frappe.session.user == "Administrator":
			return q
		parent_dt = _child_scoping_parent(resolved_dt, alias, node_spec, alias_map)
		cond = _child_record_scope(table, resolved_dt, parent_dt)
		if cond is not None:
			q = q.where(cond)
		return q
	try:
		cond = engine.get_permission_conditions(resolved_dt, table)
	except frappe.PermissionError:
		raise PermissionDeniedError(f"no read permission on DocType {resolved_dt!r}")
	if cond is not None:
		q = q.where(cond)
	return q


def _child_scoping_parent(child_dt: str, child_alias: str, node_spec: dict, alias_map: dict) -> str:
	"""Resolve the ONE parent DocType a child alias's rows are scoped to, or
	raise a clean ``PermissionDeniedError``.

	Structural resolution only (no SQL parsing — reads the same dicts the
	builder consumes). ``node_spec`` is the outer ``spec`` at the outer weave
	site, the ``sub_spec`` at the EXISTS site.

	1. Explicit signals (join ``parent``/``name`` link or ``parenttype``
	   literal) → exactly 1 distinct owning parent: use it IF the caller can
	   read the child through it, else DENY; >1 (conflicting) → DENY ambiguous.
	2. No signals → the parents the caller can read the child through: 0 → DENY
	   (defensive; step 3 already gates this), 1 → use it, >1 → DENY ambiguous.
	"""
	from jarvis.tools.get_list import _child_table_parents, _readable_child_parents

	owning_parents = _child_table_parents(child_dt)
	owning = set(owning_parents)
	signals = _collect_scoping_signals(child_dt, child_alias, node_spec, alias_map, owning)
	if signals:
		if len(signals) > 1:
			raise PermissionDeniedError(_ambiguous_scope_message(child_dt, child_alias, sorted(signals)))
		(pinned,) = tuple(signals)
		if frappe.has_permission(child_dt, ptype="read", parent_doctype=pinned):
			return pinned
		raise PermissionDeniedError(
			f"no read permission on child DocType '{child_dt}' through parent "
			f"'{pinned}'; you can read it through: {', '.join(_readable_child_parents(child_dt))}"
		)
	readable = _readable_child_parents(child_dt)
	if not readable:
		# Defensive: step 3's DocType gate already denies a child with no
		# readable parents. Kept parent-oriented in case a path reaches here.
		raise PermissionDeniedError(
			f"no read permission on child DocType '{child_dt}': child tables are "
			f"readable only through a parent DocType, and you cannot read any of "
			f"its parents ({', '.join(owning_parents)})"
		)
	if len(readable) > 1:
		raise PermissionDeniedError(_ambiguous_scope_message(child_dt, child_alias, readable))
	return readable[0]


def _ambiguous_scope_message(child_dt: str, child_alias: str, parents: list) -> str:
	"""Denial when a child alias could be scoped to more than one readable
	parent (no disambiguating signal, or conflicting signals)."""
	return (
		f"cannot scope record-level permissions for child DocType '{child_dt}': "
		f"you can read it through multiple parent DocTypes ({', '.join(parents)}); "
		f"join {child_alias}.parent to <parent_alias>.name of ONE of these "
		f"parents, or filter {child_alias}.parenttype = '<Parent>', so rows can "
		f"be scoped to a single parent"
	)


def _literal_owning_parent(ref, alias_map: dict, owning: set) -> str | None:
	"""``ref`` is a ``parenttype`` comparand. Mirror ``_build_on_criterion``'s
	literal detection: a string is a field reference only when it is
	``alias.field`` with a KNOWN alias — otherwise it is a literal. Return the
	literal when it names an owning parent."""
	if not isinstance(ref, str):
		return None
	is_field_ref = "." in ref and ref.split(".", 1)[0] in alias_map
	if is_field_ref:
		return None
	return ref if ref in owning else None


def _collect_scoping_signals(
	child_dt: str, child_alias: str, node_spec: dict, alias_map: dict, owning: set
) -> set:
	"""Explicit scoping signals for THIS child alias, read structurally from
	``node_spec``'s join ON dicts and TOP-LEVEL where equalities only:

	- ``<child_alias>.parent == <other_alias>.name`` where ``<other_alias>``'s
	  doctype is an owning parent (both orientations; in a sub-spec also the
	  correlated ``{"$field": "<outer_alias>.name"}`` form);
	- ``<child_alias>.parenttype == <string literal>`` naming an owning parent.

	Returns the set of distinct owning-parent doctypes signalled.
	"""
	parent_ref = f"{child_alias}.parent"
	parenttype_ref = f"{child_alias}.parenttype"
	signals: set = set()

	def _owning_of_name_ref(ref) -> str | None:
		# "<alias>.name" whose alias resolves to an owning-parent doctype.
		if isinstance(ref, str) and ref.count(".") == 1:
			other_alias, col = ref.split(".", 1)
			if col == "name" and other_alias in alias_map and alias_map[other_alias][0] in owning:
				return alias_map[other_alias][0]
		return None

	# JOIN ON equalities (dict of lhs -> rhs).
	for j in node_spec.get("joins") or []:
		on = j.get("on")
		if not isinstance(on, dict):
			continue
		for lhs, rhs in on.items():
			# parent <-> name link (either orientation).
			link = None
			if lhs == parent_ref:
				link = _owning_of_name_ref(rhs)
			elif rhs == parent_ref:
				link = _owning_of_name_ref(lhs)
			if link:
				signals.add(link)
			# parenttype = literal (either orientation).
			pt = None
			if lhs == parenttype_ref:
				pt = _literal_owning_parent(rhs, alias_map, owning)
			elif rhs == parenttype_ref:
				pt = _literal_owning_parent(lhs, alias_map, owning)
			if pt:
				signals.add(pt)

	# TOP-LEVEL where equalities.
	for w in node_spec.get("where") or []:
		if not isinstance(w, dict) or w.get("op") != "=":
			continue
		field = w.get("field")
		value = w.get("value")
		if field == parenttype_ref and isinstance(value, str) and value in owning:
			signals.add(value)
		elif field == parent_ref and isinstance(value, dict) and "$field" in value:
			# Correlated sub-spec form: c.parent = {"$field": "outer.name"}.
			link = _owning_of_name_ref(value["$field"])
			if link:
				signals.add(link)
	return signals


def _child_record_scope(child_table, child_dt: str, parent_dt: str):
	"""Record-level scope predicate for a child alias, pinned to ``parent_dt``.

	For a Single parent the parent SUBQUERY is skipped (a Single has no
	per-record rows and no real ``tab<Single>`` table), but the ``parenttype``
	conjunct is STILL applied — dropping it (H1) would let a user who can read a
	Single that shares this child table with a transactional parent read every
	OTHER parent's child rows too. Shape::

	    child.name IS NULL
	    OR ( child.parenttype = 'Parent'
	         AND child.parent IN (SELECT name FROM `tabParent` WHERE <cond>) )

	``<cond>`` is the PARENT's own record-level permission condition, evaluated
	on an UNALIASED parent table inside the subquery so raw-SQL
	``permission_query_conditions`` hooks that reference ``\\`tabParent\\``
	resolve. The ``isin`` conjunct is emitted only when the parent is restricted
	(``cond is not None``). The ``name IS NULL`` guard preserves a LEFT-joined
	child's null-extended row (NULL name, carries no data, cannot leak) under a
	single predicate shape for every join type.
	"""
	scoped = child_table.parenttype == parent_dt
	if not frappe.get_meta(parent_dt).issingle:
		parent_table = frappe.qb.DocType(parent_dt)
		sub_q = frappe.qb.from_(parent_table).select(parent_table.name)
		engine = _make_permission_engine(sub_q, [parent_table], parent_dt)
		cond = engine.get_permission_conditions(parent_dt, parent_table)
		if cond is not None:
			sub_q = sub_q.where(cond)
			scoped = scoped & child_table.parent.isin(sub_q)
	return child_table.name.isnull() | scoped


# ---- Translation: spec → qb expressions -----------------------------


def _build_from_and_aliases(spec: dict) -> tuple[Any, dict]:
	"""Build the FROM table and seed the alias_map with the FROM entry.
	The alias_map maps spec-alias → (doctype, pypika.Table) so later
	stages can resolve ``"alias.field"`` references."""
	from_dt = spec["from"]
	# SEC-003: validate the table-name sink and any explicit alias before
	# they reach pypika. The doctype-name fallback (below) is guarded by
	# the existence check; an explicitly-supplied alias flows into
	# ``.as_()`` and must be a bare identifier.
	_validate_doctype(from_dt)
	alias = spec.get("alias") or from_dt
	if spec.get("alias"):
		_validate_identifier(spec["alias"], "alias")
	# ``frappe.qb.DocType("Name")`` returns a pypika Table; ``.as_(alias)``
	# applies the alias. When the spec doesn't supply an alias we still
	# call ``.as_()`` so the doctype name + alias_map stay symmetric.
	table = frappe.qb.DocType(from_dt).as_(alias)
	alias_map = {alias: (from_dt, table)}
	return table, alias_map


def _resolve_field(field_ref: str, alias_map: dict, allow_alias: bool = False):
	"""Resolve a ``"alias.field"`` reference to a pypika Field.

	When ``allow_alias=True``, a bare name (no dot) is permitted and
	returned as a pypika ``Field`` without table qualification — this
	is the ORDER BY case where the operator may reference a SELECT
	alias like ``total_qty``."""
	# Reject empty / non-string / dot-only refs. Without this, callers
	# producing accidental empty strings (e.g. GROUP BY with an empty
	# entry, ORDER BY with an unset field) would silently produce
	# ``table[""]`` which generates invalid SQL with an empty column
	# reference.
	if not isinstance(field_ref, str) or not field_ref.strip():
		raise InvalidArgumentError(f"field reference must be a non-empty string, got {field_ref!r}")
	if "." not in field_ref:
		if allow_alias:
			# ORDER BY / GROUP BY may reference a SELECT output alias
			# (e.g. total_qty) rather than a physical column, so we can
			# only enforce SEC-003 identifier syntax here — column
			# existence isn't knowable for an output alias.
			_validate_identifier(field_ref, "field")
			from pypika import Field

			return Field(field_ref)
		# A bare reference like "name" is ambiguous in a join; require
		# the alias prefix for clarity.
		if len(alias_map) == 1:
			# Single-doctype query — the alias is unambiguous; resolve
			# against the only table.
			((dt, table),) = list(alias_map.values())
			# SEC-003: validate the column identifier + existence, and the
			# field-level (permlevel) read ACL. Single-table query, so ``dt``
			# is its own base doctype (parenttype resolves to None).
			_validate_column(dt, field_ref, _from_doctype(alias_map))
			return table[field_ref]
		raise InvalidArgumentError(
			f"field reference {field_ref!r} is ambiguous in a multi-table "
			f"query; prefix with the alias (e.g. 'si.name')"
		)
	alias, field = field_ref.split(".", 1)
	# Both halves of an alias.field reference must be non-empty.
	# ``"si."`` would otherwise produce ``table[""]`` and ``".name"``
	# would look up an empty alias - both generate invalid SQL.
	if not alias or not field:
		raise InvalidArgumentError(
			f"field reference {field_ref!r} must be of the form 'alias.field' with both halves non-empty"
		)
	if alias not in alias_map:
		raise InvalidArgumentError(
			f"field reference {field_ref!r} uses unknown alias {alias!r}; known aliases: {sorted(alias_map)}"
		)
	dt, table = alias_map[alias]
	# SEC-003: validate the column identifier + existence against the
	# resolved DocType before it reaches pypika's ``table[field]`` sink,
	# plus the field-level (permlevel) read ACL. ``dt`` may be a joined /
	# child table, so its field permissions are governed by the FROM/base
	# doctype (parenttype) — mirrors apply_fieldlevel_read_permissions.
	_validate_column(dt, field, _from_doctype(alias_map))
	return table[field]


def _build_on_criterion(on_spec: dict, alias_map: dict) -> Criterion:
	"""Build the JOIN ON criterion from a dict of column-equalities.

	The ``on`` shape is ``{"sii.parent": "si.name"}`` — left key is a
	field reference, right value is either another field reference (for
	column-equality, the common case) or a literal value (for static
	conditions). We auto-detect: if the value resolves as a known
	alias.field, treat it as column-equality; otherwise as a literal.
	"""
	# Empty ``on`` dict produces no equality terms; ``terms[0]`` below
	# would IndexError. Surface a clear error instead.
	if not isinstance(on_spec, dict) or not on_spec:
		raise InvalidArgumentError(
			"join.on must be a non-empty dict mapping lhs to rhs field "
			"references (e.g. {'sii.parent': 'si.name'})"
		)
	terms = []
	for lhs_ref, rhs_ref in on_spec.items():
		lhs = _resolve_field(lhs_ref, alias_map)
		# Treat as column-equality if rhs looks like alias.field with a
		# known alias. Otherwise it's a literal value.
		if isinstance(rhs_ref, str) and "." in rhs_ref and rhs_ref.split(".", 1)[0] in alias_map:
			rhs = _resolve_field(rhs_ref, alias_map)
		else:
			rhs = rhs_ref
		terms.append(lhs == rhs)
	# Multiple ON conditions get AND-combined.
	out: Criterion = terms[0]
	for t in terms[1:]:
		out = out & t
	return out


def _build_select(select_spec: list, alias_map: dict) -> list:
	"""Translate the select list. Entries can be:
	- bare string ``"si.customer"`` → resolved field
	- dict ``{"agg": "sum", "field": "sii.qty", "as": "total_qty"}`` →
	  aggregate function wrapping the field, with ``.as_()`` aliased.
	- dict with ``"distinct": True`` (v0.2) → ``COUNT(DISTINCT field)``
	  etc. Applies to all aggregates uniformly; qb rejects semantically
	  invalid combos (e.g. ``MIN(DISTINCT x)``) at SQL-build time.
	- dict ``{"expr": <tree>, "as": "alias"}`` (v0.3) → expression DSL
	  translated via ``_expr.build_expr``. The expression may itself be
	  a function call, field reference, or literal. Aliasing is
	  mandatory for expression entries (otherwise the column name is
	  whatever pypika emits from the expression, which is brittle)."""
	out = []
	for item in select_spec:
		if isinstance(item, str):
			out.append(_resolve_field(item, alias_map))
		elif isinstance(item, dict):
			# SEC-003: the ``as`` output alias flows into pypika's
			# ``.as_()``; validate it before it is used below.
			if "as" in item:
				_validate_identifier(item["as"], "alias")
			if "expr" in item and "agg" not in item:
				# Plain expression projection: {"expr": ..., "as": ...}
				if "as" not in item:
					raise InvalidArgumentError("select expression entries must carry an 'as' alias")
				_expr.validate_expr(item["expr"])
				built = _expr.build_expr(
					item["expr"],
					lambda ref: _resolve_field(ref, alias_map),
					lambda pr: _build_predicate(pr, alias_map),
				)
				out.append(built.as_(item["as"]))
			else:
				expr = _build_aggregate(item, alias_map)
				if "as" in item:
					expr = expr.as_(item["as"])
				out.append(expr)
		else:
			raise InvalidArgumentError(f"select entry must be a string or dict; got {type(item).__name__}")
	return out


def _build_aggregate(spec: dict, alias_map: dict):
	"""Build a single aggregate expression from a spec entry. Shared
	between ``_build_select`` and ``_build_predicate`` so the DISTINCT
	modifier (v0.2) lands in one place.

	pypika's aggregate classes accept a ``Field`` (or string for the
	``COUNT(*)`` special case). For DISTINCT support, the canonical
	pypika idiom is ``Count(field).distinct()`` - the aggregate gets
	wrapped, then ``.distinct()`` toggles the inner column to a
	DISTINCT projection. Same shape works across the aggregate family.
	"""
	agg_name = spec.get("agg")
	if agg_name not in _AGGREGATES:
		raise InvalidArgumentError(
			f"aggregate {agg_name!r} not allowed; must be one of {sorted(_AGGREGATES)}"
		)
	field_ref = spec.get("field")
	# v0.3: aggregates can wrap an expression instead of a bare field.
	# ``{"agg": "sum", "expr": <tree>, ...}`` produces ``SUM(<expr>)``.
	# Mutually exclusive with ``field``.
	agg_expr = spec.get("expr")
	if agg_expr is not None and field_ref is not None:
		raise InvalidArgumentError(f"aggregate {agg_name!r} cannot have both 'field' and 'expr'")
	if agg_name == "count" and field_ref == "*":
		# COUNT(*) and COUNT(DISTINCT *) - the latter is pointless but
		# pypika accepts it; we don't gate semantics, only shapes.
		expr = fn.Count("*")
	elif field_ref:
		expr = _AGGREGATES[agg_name](_resolve_field(field_ref, alias_map))
	elif agg_expr is not None:
		_expr.validate_expr(agg_expr)
		inner = _expr.build_expr(
			agg_expr,
			lambda ref: _resolve_field(ref, alias_map),
			lambda pr: _build_predicate(pr, alias_map),
		)
		expr = _AGGREGATES[agg_name](inner)
	else:
		raise InvalidArgumentError(f"aggregate {agg_name!r} missing 'field' or 'expr' (use '*' for COUNT(*))")
	if spec.get("distinct"):
		# Toggle DISTINCT on the inner column. pypika's
		# AggregateFunction.distinct() is the canonical entry point.
		expr = expr.distinct()
	return expr


def _build_predicate(p: dict, alias_map: dict, depth: int = 1) -> Criterion:
	"""Translate a WHERE/HAVING predicate dict to a pypika Criterion.

	Predicate shapes:

	- Plain field predicate::

	    {"field": "si.status", "op": "=", "value": "Submitted"}

	- Aggregate predicate (HAVING)::

	    {"agg": "sum", "field": "sii.qty", "op": ">", "value": 100}

	- Set-existence (v0.2)::

	    {"op": "not exists", "value": <stripped sub-spec>}

	The aggregate variant wraps the field in the agg function; we
	support both shapes in WHERE and HAVING uniformly so the agent
	doesn't have to remember which clause permits which.

	``depth`` tracks subquery nesting for the EXISTS recursion cap
	(default 1 = outer query; each EXISTS nesting increments).
	"""
	op = p["op"]

	# v0.2: EXISTS / NOT EXISTS take a sub-spec as ``value`` rather
	# than a literal. Handle these BEFORE the lhs/rhs path since
	# they don't have a ``field`` or ``agg`` lhs.
	if op in ("exists", "not exists"):
		sub_spec = p.get("value")
		if not isinstance(sub_spec, dict):
			raise InvalidArgumentError(f"{op!r} requires a sub-spec dict as 'value'")
		return _build_exists_criterion(
			sub_spec,
			alias_map,
			depth,
			negate=(op == "not exists"),
		)

	# Resolve the left side: bare field, aggregate, or v0.3 expression.
	if "agg" in p:
		lhs = _build_aggregate(p, alias_map)
	elif "expr" in p:
		# v0.3: WHERE / HAVING can predicate against an expression.
		_expr.validate_expr(p["expr"])
		lhs = _expr.build_expr(
			p["expr"],
			lambda ref: _resolve_field(ref, alias_map),
			lambda pr: _build_predicate(pr, alias_map),
		)
	else:
		field_ref = p.get("field")
		if not field_ref:
			raise InvalidArgumentError("predicate missing 'field', 'agg', or 'expr'")
		lhs = _resolve_field(field_ref, alias_map)

	# Apply the operator. Each branch produces a Criterion.
	if op == "=":
		return lhs == p["value"]
	if op == "!=":
		return lhs != p["value"]
	if op == "<":
		return lhs < p["value"]
	if op == "<=":
		return lhs <= p["value"]
	if op == ">":
		return lhs > p["value"]
	if op == ">=":
		return lhs >= p["value"]
	if op == "in":
		values = p.get("value")
		if not isinstance(values, list):
			raise InvalidArgumentError(f"'in' operator requires a list value; got {type(values).__name__}")
		return lhs.isin(values)
	if op == "not in":
		values = p.get("value")
		if not isinstance(values, list):
			raise InvalidArgumentError("'not in' operator requires a list value")
		return lhs.notin(values)
	if op == "like":
		return lhs.like(p["value"])
	if op == "not like":
		return lhs.not_like(p["value"])
	if op == "is null":
		return lhs.isnull()
	if op == "is not null":
		return lhs.isnotnull()
	if op == "between":
		values = p.get("value")
		if not isinstance(values, list) or len(values) != 2:
			raise InvalidArgumentError("'between' operator requires a 2-element list value")
		return lhs[slice(*values)]
	# Unreachable - _validate_spec_shape already restricts op to _OPERATORS.
	raise InvalidArgumentError(f"unsupported operator: {op}")


# ---- v0.2: EXISTS / NOT EXISTS sub-specs ----------------------------


# Fields the stripped sub-spec is NOT allowed to carry. Subqueries
# inside EXISTS don't need aggregates / ordering / paging - the engine
# just checks if any row matches, so SELECT / GROUP BY / HAVING /
# ORDER BY / LIMIT / OFFSET / DISTINCT are all noise. Reject up front
# so the agent gets a clear error rather than building a spec the
# qb side silently ignores.
_SUBSPEC_DISALLOWED_FIELDS = (
	"select",
	"group_by",
	"having",
	"order_by",
	"limit",
	"offset",
	"distinct",
)


def _build_exists_criterion(sub_spec: dict, outer_alias_map: dict, depth: int, *, negate: bool) -> Criterion:
	"""Build an EXISTS or NOT EXISTS criterion from a stripped sub-spec.

	The sub-spec carries only ``from``, ``alias``, ``joins``, ``where``.
	The outer ``alias_map`` is needed so the sub-spec's WHERE can
	reference outer-query columns via the ``{"$field": "alias.col"}``
	correlated-reference marker.

	Depth-counted to cap recursion at ``_MAX_SUBSPEC_DEPTH``. The outer
	query is depth=1; the first nested EXISTS is depth=2; depth=3
	raises - one level of nesting is the realistic ceiling and deeper
	specs are usually agent confusion or runaway payloads.

	Returns a pypika Criterion suitable for ``.where(...)``. NOT EXISTS
	is built via pypika's ``~`` negation on the EXISTS criterion (a
	standard pypika Term operator), avoiding a separate code path.
	"""
	if depth + 1 > _MAX_SUBSPEC_DEPTH:
		raise InvalidArgumentError(
			f"EXISTS / NOT EXISTS sub-spec nesting exceeds the {_MAX_SUBSPEC_DEPTH}-"
			f"level cap. Restructure the query to flatten the membership "
			f"check (e.g. a LEFT JOIN at the outer level)."
		)

	# Validate shape: only the four allowed fields are present.
	if not isinstance(sub_spec, dict):
		raise InvalidArgumentError("EXISTS sub-spec must be a dict")
	if "from" not in sub_spec or not isinstance(sub_spec["from"], str):
		raise InvalidArgumentError("EXISTS sub-spec.from must be a DocType name (string)")
	for forbidden in _SUBSPEC_DISALLOWED_FIELDS:
		if forbidden in sub_spec:
			raise InvalidArgumentError(
				f"EXISTS sub-spec must not include {forbidden!r}; "
				f"subqueries only carry from + alias + joins + where"
			)

	# Build the inner alias_map. The OUTER aliases stay reachable for
	# correlated references via the ``$field`` marker; they're folded
	# into the inner map under their original keys. If the sub-spec
	# accidentally re-uses an outer alias, that's a real collision -
	# refuse with a clean error.
	sub_from_table, sub_alias_map = _build_from_and_aliases(sub_spec)
	for outer_alias in outer_alias_map:
		if outer_alias in sub_alias_map:
			raise InvalidArgumentError(
				f"EXISTS sub-spec alias {outer_alias!r} collides with "
				f"the outer query's alias of the same name"
			)
		# Outer aliases are visible-but-not-redefinable inside the
		# subquery. We add them so $field markers resolve, but we also
		# track that they are outer-scope so the subquery doesn't
		# accidentally pull them into its FROM (handled because we
		# only consult these for resolving $field markers, not for
		# the qb.from_() chain).
		sub_alias_map[outer_alias] = outer_alias_map[outer_alias]

	# Sub-spec joins land in the inner alias_map as usual.
	sub_q = frappe.qb.from_(sub_from_table)
	for j in sub_spec.get("joins") or []:
		if j.get("type", "inner") not in _JOIN_METHODS:
			raise InvalidArgumentError(
				f"EXISTS sub-spec.joins[*].type must be one of: {sorted(_JOIN_METHODS)}"
			)
		for k in ("doctype", "alias", "on"):
			if k not in j:
				raise InvalidArgumentError(f"EXISTS sub-spec.joins[*] missing required field: {k}")
		if j["alias"] in sub_alias_map:
			raise InvalidArgumentError(f"EXISTS sub-spec alias {j['alias']!r} collides")
		# SEC-003: validate the sub-spec join's table-name + alias sinks.
		_validate_doctype(j["doctype"])
		_validate_identifier(j["alias"], "alias")
		joined_table = frappe.qb.DocType(j["doctype"]).as_(j["alias"])
		sub_alias_map[j["alias"]] = (j["doctype"], joined_table)
		on_criterion = _build_on_criterion(j["on"], sub_alias_map)
		join_method_name = _JOIN_METHODS[j.get("type", "inner")]
		sub_q = getattr(sub_q, join_method_name)(joined_table).on(on_criterion)

	# Sub-spec WHERE. Predicates may carry ``$field`` markers for
	# correlated references; resolve those before handing to the
	# regular predicate builder. Skip correlated-ref resolution for
	# nested EXISTS / NOT EXISTS predicates - their ``value`` is a
	# deeper sub-spec that gets its own recursive call through
	# ``_build_exists_criterion``, and any ``$field`` markers inside
	# that deeper level resolve against the deeper alias_map, not
	# this one.
	for w in sub_spec.get("where") or []:
		if w.get("op") in ("exists", "not exists"):
			resolved = w
		else:
			resolved = _resolve_correlated_refs(w, sub_alias_map)
		sub_q = sub_q.where(_build_predicate(resolved, sub_alias_map, depth=depth + 1))

	# Record-level permission weave for the sub-query. Without this
	# the EXISTS / NOT EXISTS form becomes a side-channel: a caller
	# with role-read on the sub-spec's DocType but a User Permission
	# restricting which records they can see would otherwise leak
	# existence over the full table. Mirror the outer query's
	# pipeline — instantiate an Engine, share the sub-query, and AND
	# each sub-spec doctype's get_permission_conditions() into the
	# sub-query WHERE. Outer aliases that we folded into
	# sub_alias_map for $field resolution are skipped — they were
	# already perm-gated at the outer level (and weaving them again
	# here would double-filter).
	# Only the sub-spec's own tables, not the outer-scoped aliases.
	sub_local_aliases = {
		a: (dt, table) for a, (dt, table) in sub_alias_map.items() if a not in outer_alias_map
	}
	sub_engine = _make_permission_engine(
		sub_q, [table for (_, table) in sub_local_aliases.values()], sub_spec["from"]
	)
	# Apply the record gate to every alias's table object. Earlier code
	# de-duplicated by doctype on the assumption that the returned criterion
	# was scoped to a canonical table; that's wrong — the predicate is built
	# against the specific table object it's handed, so a self-join under two
	# aliases needs the gate applied twice (once per alias) or the second
	# alias bypasses User Permissions / DocShare entirely. The child branch is
	# resolved from the SUB-spec's own signals (``sub_spec``), correlated
	# ``$field`` markers resolving through ``sub_alias_map`` (which carries the
	# folded-in outer aliases). THIS SITE MUST match the outer weave, or an
	# EXISTS over a child table becomes the same raw-HTML / leak side-channel.
	for alias, (resolved_dt, table) in sub_local_aliases.items():
		sub_q = _weave_record_gate(sub_q, sub_engine, alias, resolved_dt, table, sub_spec, sub_alias_map)

	# The SELECT projection of an EXISTS subquery is semantically
	# irrelevant; we select the literal 1 (cheapest non-empty
	# projection). Matches the SQL convention ``EXISTS (SELECT 1
	# FROM ...)`` so resolved SQL reads naturally.
	sub_q = sub_q.select(1)

	# pypika's ``QueryBuilder`` exposes the EXISTS-ness via being
	# usable as a Criterion when wrapped. The canonical idiom across
	# pypika versions is ``ExistsCriterion(sub_q)``; some versions
	# also have ``sub_q.exists()`` as a shorthand. Use the explicit
	# wrapper for stability across versions Frappe pins.
	try:
		# Newer pypika (>=0.49) exposes ExistsCriterion via terms.
		from pypika.terms import ExistsCriterion

		crit = ExistsCriterion(sub_q)
	except ImportError:
		# Fallback: some versions expose ``.exists()`` on QueryBuilder.
		# If neither path works we raise with a clear message rather
		# than silently mis-building.
		if hasattr(sub_q, "exists"):
			crit = sub_q.exists()
		else:
			raise InvalidArgumentError(
				"This Frappe version's pypika does not expose EXISTS "
				"subquery support; restructure the spec to a LEFT JOIN "
				"+ IS NULL form."
			)

	# NOT EXISTS via pypika's ``~`` term-negation.
	if negate:
		crit = crit.negate() if hasattr(crit, "negate") else (~crit)
	return crit


def _resolve_correlated_refs(predicate: dict, alias_map: dict) -> dict:
	"""Walk a predicate dict and convert any ``{"$field": "alias.col"}``
	values into resolved pypika ``Field`` references against the
	(combined inner+outer) alias_map.

	The agent writes correlated subqueries like::

	    {"field": "t.employee", "op": "=", "value": {"$field": "e.name"}}

	without the marker, pypika would treat ``"$field": "e.name"`` as a
	literal string and the EXISTS would compare ``t.employee`` to the
	literal text ``"e.name"`` instead of the outer table's column. The
	marker disambiguates: any time the value is a dict with the single
	key ``$field``, treat as a column reference.

	Returns a shallow-copy of the predicate with the value resolved.
	Idempotent on predicates that don't carry the marker.
	"""
	value = predicate.get("value")
	if isinstance(value, dict) and "$field" in value:
		resolved = _resolve_field(value["$field"], alias_map, allow_alias=False)
		return {**predicate, "value": resolved}
	# Lists may contain $field markers too (e.g. ``in [{$field: ...}]``,
	# though uncommon). Walk and resolve element-by-element.
	if isinstance(value, list):
		new_value = []
		for v in value:
			if isinstance(v, dict) and "$field" in v:
				new_value.append(_resolve_field(v["$field"], alias_map, allow_alias=False))
			else:
				# Reject unresolved $field markers buried inside nested
				# structures - those would otherwise reach pypika as raw
				# dicts and either fail opaquely or stringify into broken
				# SQL. The supported shapes are a top-level marker or a
				# direct list element; anything deeper is malformed.
				_assert_no_unresolved_field_marker(v)
				new_value.append(v)
		return {**predicate, "value": new_value}
	# Top-level value is neither a marker dict nor a list, but may still
	# carry a buried marker (e.g. a dict literal the agent constructed
	# by accident). Reject those too.
	_assert_no_unresolved_field_marker(value)
	return predicate


def _assert_no_unresolved_field_marker(node) -> None:
	"""Walk ``node`` recursively and raise if any nested dict still
	carries the ``$field`` marker. Resolution only handles top-level
	marker dicts and direct list elements; anywhere else means the
	agent built a malformed value and pypika would receive a raw dict.
	"""
	if isinstance(node, dict):
		if "$field" in node:
			raise InvalidArgumentError(
				"{'$field': ...} markers are only supported as a "
				"predicate's top-level value or as a direct list element "
				"in an 'in'/'not in' value; nested $field markers are "
				"not resolved"
			)
		for v in node.values():
			_assert_no_unresolved_field_marker(v)
	elif isinstance(node, list):
		for v in node:
			_assert_no_unresolved_field_marker(v)
