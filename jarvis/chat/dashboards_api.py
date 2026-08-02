"""SPA-facing API for the Jarvis Dashboards page.

Whitelisted endpoints for the ``/jarvis`` Dashboards UI: caps probe, paginated
dashboard list (frozen ``{rows, total, has_more, start, page_length}`` shape
inside the ``{ok, data}`` envelope), scope-visibility-filtered CRUD, and the
view-time data runner (``run_dashboard_source`` — always executes the SAVED
spec as the SESSION user; there is deliberately no spec parameter, so a viewer
can never smuggle an ad-hoc query through a shared dashboard). Mirrors the
shape of ``jarvis.chat.triggers_api`` (whose list-page helpers it reuses via
``jarvis.chat.macros_api``).

Visibility: the list uses raw SQL, which bypasses the ORM permission hooks, so
``dashboard_permissions.visible_scope_condition()`` is spliced into the WHERE
for non-admins (the wiki list does the same). Per-doc reads go through
``doc.check_permission`` (the has_permission hook).

Error contract of ``run_dashboard_source`` (always HTTP 200):
``{ok: True, data: {source_name, tool, rows[, columns], truncated, took_ms}}``
or ``{ok: False, error: {code, message}}`` with code one of
``PermissionError`` / ``InvalidArgumentError`` / ``InternalError`` (internal
errors are logged server-side and never leak tracebacks).
"""

from __future__ import annotations

import re
import time

import frappe
from frappe import _

from jarvis.chat import dashboard_permissions, list_filters
from jarvis.chat.macros_api import _clamp_page, _lk, _load_filters
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_user

DASHBOARD = "Jarvis Dashboard"

_ALLOWED_TOOLS = {"query", "run_report", "get_list"}
_MAX_SOURCES = 12
_MAX_SPEC_CHARS = 32_000
_MAX_HTML_CHARS = 1_000_000
# Post-fetch hard cap on rows returned to the dashboard renderer. Sits above
# the tools' own limits (query/get_list cap at limit<=1000; run_report has no
# limit) so it only ever bites report results — sliced, never errored.
DASHBOARD_MAX_ROWS = 2000

# The declared-sources block inside the dashboard HTML document:
# <script type="application/json" id="jarvis-sources">{"sources": [...]}</script>
_SOURCES_BLOCK_RE = re.compile(
	r'<script[^>]*\bid=["\']jarvis-sources["\'][^>]*>(.*?)</script>',
	re.I | re.S,
)

# Fields a create/update payload may set. Everything else (owner, target_user
# — server-derived for User scope, dashboard_type — always derived, timestamps)
# is server-owned; unknown keys throw.
_ALLOWED_PAYLOAD_FIELDS = {
	"name",
	"dashboard_title",
	"description",
	"html",
	"scope",
	"target_role",
	"sources",
	"source_conversation",
	"theme",
}

_SCOPES = {"Org", "Role", "User"}
_TYPES = {"Static", "Connected"}

_DASHBOARD_FILTERS = {"scope", "dashboard_type", "owner"}
_DASHBOARD_SORTABLE = {
	"modified": "modified",
	"dashboard_title": "dashboard_title",
	"dashboard_type": "dashboard_type",
	"scope": "scope",
	"owner": "owner",
}

# Per-tool spec key allow-lists (unknown keys throw at save time).
_GET_LIST_SPEC_KEYS = {"doctype", "fields", "filters", "order_by", "limit", "parent_doctype"}
_RUN_REPORT_SPEC_KEYS = {"report_name", "filters"}

# A get_list order_by: one or more `fieldname [asc|desc]` tokens, comma-joined.
# fieldname allows an optional `child_table.field` / backtick-free dotted path.
_ORDER_BY_RE = re.compile(
	r"^\s*[a-zA-Z_][\w.]*(?:\s+(?:asc|desc))?"
	r"(?:\s*,\s*[a-zA-Z_][\w.]*(?:\s+(?:asc|desc))?)*\s*$",
	re.IGNORECASE,
)

# Per-request wall-clock ceiling (seconds) for the dashboard data runner, so a
# heavy report/query can't pin a web worker. MariaDB max_statement_time.
_SOURCE_STMT_TIMEOUT_S = 20


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _order_by(sort_field: str, sort_dir: str, sortable: dict, default_field: str) -> str:
	"""Whitelisted ORDER BY (unknown sort field throws — same strictness as the
	triggers list)."""
	field = (sort_field or "").strip() or default_field
	if field not in sortable:
		frappe.throw(_("Unknown sort field: {0}").format(field))
	d = "desc" if (sort_dir or "desc").lower() == "desc" else "asc"
	return f"`{sortable[field]}` {d}, `name` asc"


def _clear_perm_message_noise() -> None:
	"""Drop any messages ``frappe.has_permission`` pushed onto the message log
	on a denied path ("User X does not have doctype access …"), so they never
	leak back to the client in ``_server_messages``. Best-effort."""
	try:
		frappe.local.message_log = []
	except Exception:
		pass


def _error_envelope(code: str, message: str) -> dict:
	return {"ok": False, "error": {"code": code, "message": message}}


def _set_stmt_timeout(seconds: int) -> None:
	"""Best-effort per-request statement timeout so a heavy dashboard source
	can't pin a web worker. MariaDB's max_statement_time is a session variable
	in seconds; a no-op on other backends or if the SET fails."""
	try:
		if frappe.db.db_type == "mariadb":
			frappe.db.sql("SET SESSION max_statement_time = %s", (seconds,))
	except Exception:
		pass


def _dashboard_detail(doc) -> dict:
	"""Full dashboard detail for the editor/viewer. ``can_edit`` tells the SPA
	whether to offer the edit surfaces to this session user."""
	return {
		"name": doc.name,
		"dashboard_title": doc.dashboard_title,
		"description": doc.description or "",
		"dashboard_type": doc.dashboard_type,
		"theme": doc.theme or "Jarvis",
		"scope": doc.scope,
		"target_role": doc.target_role or "",
		"target_user": doc.target_user or "",
		"html": doc.html or "",
		"sources": [
			{"source_name": s.source_name, "tool": s.tool, "spec": s.spec or ""} for s in (doc.sources or [])
		],
		"source_conversation": doc.source_conversation or "",
		"owner": doc.owner,
		"modified": str(doc.modified),
		"can_edit": dashboard_permissions.can_edit_dashboard(doc),
	}


# --------------------------------------------------------------------------- #
# caps probe
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def get_dashboards_caps() -> dict:
	"""Single gating probe for the Dashboards page: which scopes the caller
	may create in, which roles they may target, and the size caps."""
	stt_enabled = False
	try:
		from jarvis.chat import voice

		stt_enabled = bool(voice.stt_config())
	except Exception:
		stt_enabled = False
	return {
		"ok": True,
		"data": {
			"creatable_scopes": dashboard_permissions.creatable_scopes(),
			"manageable_roles": dashboard_permissions.manageable_roles(),
			"max_sources": _MAX_SOURCES,
			"max_html_chars": _MAX_HTML_CHARS,
			"max_rows": DASHBOARD_MAX_ROWS,
			"stt_enabled": stt_enabled,
		},
	}


# --------------------------------------------------------------------------- #
# list / detail
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
@list_filters.filter_errors_to_envelope
def list_dashboards_page(
	search: str = "",
	filters: str = "{}",
	filters_v2: str | list | None = None,
	sort_field: str = "modified",
	sort_dir: str = "desc",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Scope-visible dashboards, server-side search / filter / sort / paginate.
	Raw SQL bypasses the ORM permission hooks, so the visibility fragment is
	spliced into the WHERE for non-admins. Frozen envelope in ``data``.

	``filters_v2`` (plan 08 §6.2) is ADDITIVE: the canonical clause list,
	validated and compiled against this caller's schema by
	``jarvis.chat.list_filters``. Legacy ``filters`` keeps working unchanged for
	the compatibility window. The visibility fragment below is server-authored
	and stays on the query object's server side, so a user clause is ANDed
	INSIDE it and can never widen it (C08-5).

	Scope invariant (MIGRATION-CHECKLIST §1): every role that reaches this
	endpoint holds a permlevel-0 ``read`` on Jarvis Dashboard with no
	``if_owner``, so the ORM read scope is the whole table and this SQL scope —
	owner OR Org OR the caller's Role OR a User-scoped row addressed to them — is
	strictly narrower. The catalog is therefore derived from the right authority.
	"""
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _DASHBOARD_FILTERS)

	q = list_filters.new_query("saved_dashboards")
	if search:
		q.server_condition(
			"(dashboard_title LIKE %(q)s OR description LIKE %(q)s)", q=f"%{_lk(search)}%"
		)
	if "scope" in f:
		if f["scope"] not in _SCOPES:
			frappe.throw(_("Invalid scope filter."))
		q.server_condition("scope = %(scope)s", scope=f["scope"])
	if "dashboard_type" in f:
		if f["dashboard_type"] not in _TYPES:
			frappe.throw(_("Invalid dashboard_type filter."))
		q.server_condition("dashboard_type = %(dashboard_type)s", dashboard_type=f["dashboard_type"])
	if "owner" in f:
		q.server_condition("owner = %(owner)s", owner=str(f["owner"]))
	if not has_jarvis_admin_access():
		# Values inside are frappe.db.escape'd; spliced (not parameterized)
		# because the role list is variable-length — same as the wiki list. It
		# carries no placeholders, so it needs no bound params of its own.
		q.server_condition(dashboard_permissions.visible_scope_condition())

	q.apply(filters_v2)
	where = q.where()
	params = q.params({"start": start, "page_length": pl})
	order = _order_by(sort_field, sort_dir, _DASHBOARD_SORTABLE, "modified")

	total = list_filters.bounded_sql(f"SELECT COUNT(*) FROM `tabJarvis Dashboard` WHERE {where}", params)[0][0]
	rows = list_filters.bounded_sql(
		f"""SELECT name, dashboard_title, description, dashboard_type, scope,
		target_role, target_user, owner, modified
		FROM `tabJarvis Dashboard`
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)
	for r in rows:
		r["modified"] = str(r["modified"])

	return {
		"ok": True,
		"data": {
			"rows": rows,
			"total": total,
			"has_more": start + len(rows) < total,
			"start": start,
			"page_length": pl,
		},
	}


@frappe.whitelist()
@require_jarvis_user
def get_dashboard(name: str) -> dict:
	"""Full detail of one dashboard (read is scope-gated by the has_permission
	hook via check_permission)."""
	doc = frappe.get_doc(DASHBOARD, name)
	doc.check_permission("read")
	return {"ok": True, "data": _dashboard_detail(doc)}


@frappe.whitelist()
@require_jarvis_user
def dashboard_for_conversation(conversation: str) -> dict:
	"""The saved dashboard this conversation built, if there is one.

	Main chat offers "Open in Dashboards" on a builder conversation's html
	artifact: a saved dashboard opens in place (``?edit=``), an unsaved canvas
	is promoted onto the builder from the transcript instead. This is the
	lookup behind that fork, so ``{}`` is an ordinary answer, not an error.

	Permission: the CALLER must own the conversation (conversations are
	strictly private - the same gate ``chat.api.get_conversation`` applies),
	and the dashboard row goes through ``frappe.get_list`` - NOT get_all,
	which sets ignore_permissions and would skip the
	permission_query_conditions hook that applies the scope-visibility matrix.
	A dashboard that has since moved out of the caller's reach therefore reads
	as "none" rather than leaking its title.

	``owner`` is in the filters as well: ``source_conversation`` is a
	client-settable payload field, so an Org-scope dashboard carrying someone
	else's conversation id would otherwise be visible on THEIR "Open in
	Dashboards". The caller provably owns the conversation, so nobody else can
	legitimately have built from it.

	``creation`` is returned (and ordered on, not ``modified``): the caller
	compares it against the clicked message's own creation, so merely EDITING
	an old dashboard cannot re-hijack a click on a newer unsaved build.
	"""
	from jarvis.chat.api import _get_owned_conversation

	_get_owned_conversation(conversation)  # non-owner: PermissionError
	rows = frappe.get_list(
		DASHBOARD,
		filters={"source_conversation": conversation, "owner": frappe.session.user},
		fields=["name", "dashboard_title", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {"ok": True, "data": {}}
	return {
		"ok": True,
		"data": {
			"name": rows[0].name,
			"dashboard_title": rows[0].dashboard_title or "",
			"creation": str(rows[0].creation or ""),
		},
	}


# --------------------------------------------------------------------------- #
# save / delete
# --------------------------------------------------------------------------- #
def _parse_payload(payload: str) -> dict:
	try:
		raw = frappe.parse_json(payload)
	except Exception:
		raw = None
	if not isinstance(raw, dict):
		frappe.throw(_("Payload must be a JSON object."))
	out: dict = {}
	for k, v in raw.items():
		if k not in _ALLOWED_PAYLOAD_FIELDS:
			frappe.throw(_("Unknown field: {0}").format(k))
		out[k] = v
	return out


def _parse_sources_block(html: str) -> list:
	"""Extract the declared sources from the ``jarvis-sources`` JSON block
	inside the dashboard HTML. No block -> no sources (a Static dashboard).
	A present-but-broken block throws — silently ignoring it would save a
	Connected-looking document with no data wiring."""
	m = _SOURCES_BLOCK_RE.search(html or "")
	if not m:
		return []
	try:
		parsed = frappe.parse_json(m.group(1).strip())
	except Exception:
		parsed = None
	if not isinstance(parsed, dict) or not isinstance(parsed.get("sources"), list):
		frappe.throw(_('The jarvis-sources block must be JSON of the shape {"sources": [...]}.'))
	return parsed["sources"]


def _normalize_source_rows(sources) -> list[dict]:
	"""Coerce an explicit-payload or html-block sources list into child-row
	dicts. ``spec`` may arrive as a JSON object (the natural authoring shape in
	the html block) or a pre-serialized string; the child field stores a
	string. Shape errors throw; content validation happens in the controller
	via ``_validate_source_row``.

	LLM-authored blocks drift toward the tool-call surface, so the common
	dialects are folded into the canonical shape rather than rejected:
	``id``/``name`` for ``source_name``, a ``jarvis__`` tool prefix, and
	``args``/``args.spec`` for ``spec`` (the runtime bridge normalizes the
	same way, so stored rows and rendered widgets always agree)."""
	if not isinstance(sources, list):
		frappe.throw(_("sources must be a list."))
	rows: list[dict] = []
	for i, s in enumerate(sources):
		if not isinstance(s, dict):
			frappe.throw(_("sources[{0}] must be an object.").format(i))
		name = s.get("source_name") or s.get("id") or s.get("name")
		tool = s.get("tool")
		if isinstance(tool, str) and tool.startswith("jarvis__"):
			tool = tool[len("jarvis__") :]
		spec = s.get("spec")
		if spec is None:
			args = s.get("args")
			if isinstance(args, dict):
				spec = args.get("spec") if isinstance(args.get("spec"), dict) else args
		# double-wrap drift ({"spec": {"spec": {...}}}): unwrap lone spec keys
		while isinstance(spec, dict) and len(spec) == 1 and isinstance(spec.get("spec"), dict):
			spec = spec["spec"]
		if isinstance(spec, (dict, list)):
			spec = frappe.as_json(spec)
		rows.append(
			{
				"source_name": name,
				"tool": tool,
				"spec": spec if isinstance(spec, str) else "",
			}
		)
	return rows


@frappe.whitelist()
@require_jarvis_user
def save_dashboard(payload: str) -> dict:
	"""Create or update a dashboard from a whitelisted-fields JSON payload.
	``name`` absent -> insert; present -> owner/admin-gated update. An explicit
	``sources`` list wins; otherwise the sources are (re)parsed from the html's
	``jarvis-sources`` block whenever html is provided — the html document is
	the source of truth, so stale child rows never outlive it. Scope gates,
	caps, per-source validation and the dashboard_type derivation run in the
	DocType controller."""
	fields = _parse_payload(payload)
	name = str(fields.pop("name", None) or "").strip()
	sources = fields.pop("sources", None)
	if sources is None and "html" in fields:
		sources = _parse_sources_block(fields.get("html") or "")

	if name:
		doc = frappe.get_doc(DASHBOARD, name)
		if not dashboard_permissions.can_edit_dashboard(doc):
			frappe.throw(
				_("Only the dashboard's owner or a Jarvis Admin can edit it."),
				frappe.PermissionError,
			)
		doc.update(fields)
	else:
		if not (fields.get("dashboard_title") or "").strip():
			frappe.throw(_("dashboard_title is required."))
		if "html" not in fields:
			frappe.throw(_("html is required."))
		doc = frappe.get_doc({"doctype": DASHBOARD, **fields})

	if sources is not None:
		doc.set("sources", [])
		for row in _normalize_source_rows(sources):
			doc.append("sources", row)

	if doc.is_new():
		doc.insert()
	else:
		doc.save()
	frappe.db.commit()
	return {"ok": True, "data": _dashboard_detail(doc)}


@frappe.whitelist()
@require_jarvis_user
def delete_dashboard(name: str) -> dict:
	"""Delete one dashboard (owner or admin tier)."""
	doc = frappe.get_doc(DASHBOARD, name)
	if not dashboard_permissions.can_edit_dashboard(doc):
		frappe.throw(
			_("Only the dashboard's owner or a Jarvis Admin can delete it."),
			frappe.PermissionError,
		)
	frappe.delete_doc(DASHBOARD, name)
	frappe.db.commit()
	return {"ok": True, "data": {"deleted": name}}


# --------------------------------------------------------------------------- #
# per-source spec validation (shared with the DocType controller)
# --------------------------------------------------------------------------- #
def _validate_source_row(row: dict) -> None:
	"""Validate one source row's tool + spec (source_name charset/uniqueness
	is the controller's job). Everything throws frappe.ValidationError-shaped
	errors so Desk saves and the SPA get the same clean 417s."""
	label = row.get("source_name") or "?"
	tool = (row.get("tool") or "").strip()
	if tool not in _ALLOWED_TOOLS:
		frappe.throw(
			_("Source '{0}': unknown tool '{1}' (allowed: query, run_report, get_list).").format(label, tool)
		)
	raw = row.get("spec")
	if isinstance(raw, str) and len(raw) > _MAX_SPEC_CHARS:
		frappe.throw(_("Source '{0}': spec must be at most {1} characters.").format(label, _MAX_SPEC_CHARS))
	try:
		spec = frappe.parse_json(raw)
	except Exception:
		spec = None
	if not isinstance(spec, dict):
		frappe.throw(_("Source '{0}': spec must be a JSON object.").format(label))

	if tool == "query":
		_validate_query_spec(label, spec)
	elif tool == "get_list":
		_validate_get_list_spec(label, spec)
	else:
		_validate_run_report_spec(label, spec)


def _validate_query_spec(label: str, spec: dict) -> None:
	"""Reuse the query tool's own validators (shape + doctype existence) so a
	saved source can't be shaped in a way the runner would reject anyway. The
	tool raises InvalidArgumentError (not a frappe.ValidationError) — translate
	to frappe.throw for the save path."""
	from jarvis.tools.query import _collect_doctypes, _validate_doctype, _validate_spec_shape

	try:
		_validate_spec_shape(spec)
		for dt in _collect_doctypes(spec):
			_validate_doctype(dt)
	except InvalidArgumentError as e:
		frappe.throw(_("Source '{0}': {1}").format(label, str(e)))
	if "limit" in spec:
		limit = spec["limit"]
		if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 1000):
			frappe.throw(_("Source '{0}': limit must be an integer between 1 and 1000.").format(label))


def _validate_get_list_spec(label: str, spec: dict) -> None:
	unknown = set(spec) - _GET_LIST_SPEC_KEYS
	if unknown:
		frappe.throw(
			_("Source '{0}': unknown get_list spec key(s): {1}").format(label, ", ".join(sorted(unknown)))
		)
	doctype = spec.get("doctype")
	if not isinstance(doctype, str) or not doctype.strip():
		frappe.throw(_("Source '{0}': doctype is required.").format(label))
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Source '{0}': unknown DocType: {1}").format(label, doctype))
	if "fields" in spec and not (
		isinstance(spec["fields"], list) and all(isinstance(x, str) for x in spec["fields"])
	):
		frappe.throw(_("Source '{0}': fields must be a list of strings.").format(label))
	if "filters" in spec and not isinstance(spec["filters"], (dict, list)):
		frappe.throw(_("Source '{0}': filters must be an object or a list.").format(label))
	if "limit" in spec:
		limit = spec["limit"]
		if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 1000):
			frappe.throw(_("Source '{0}': limit must be an integer between 1 and 1000.").format(label))
	# order_by is handed straight to frappe.get_list; validate it ourselves
	# (whitelisted `fieldname [asc|desc]` tokens) rather than trusting the ORM
	# to sanitize an order-by expression.
	if "order_by" in spec:
		ob = spec["order_by"]
		if not isinstance(ob, str) or not _ORDER_BY_RE.match(ob.strip()):
			frappe.throw(
				_(
					"Source '{0}': order_by must be 'fieldname' or "
					"'fieldname asc|desc' (optionally comma-separated)."
				).format(label)
			)


def _validate_run_report_spec(label: str, spec: dict) -> None:
	unknown = set(spec) - _RUN_REPORT_SPEC_KEYS
	if unknown:
		frappe.throw(
			_("Source '{0}': unknown run_report spec key(s): {1}").format(label, ", ".join(sorted(unknown)))
		)
	report_name = spec.get("report_name")
	if not isinstance(report_name, str) or not report_name.strip():
		frappe.throw(_("Source '{0}': report_name is required.").format(label))
	if not frappe.db.exists("Report", report_name):
		frappe.throw(_("Source '{0}': unknown Report: {1}").format(label, report_name))
	if frappe.db.get_value("Report", report_name, "prepared_report"):
		frappe.throw(
			_(
				"Source '{0}': '{1}' runs as a background Prepared Report and cannot "
				"serve a dashboard live. Pick a report that runs inline."
			).format(label, report_name)
		)
	if "filters" in spec and not isinstance(spec["filters"], dict):
		frappe.throw(_("Source '{0}': filters must be an object.").format(label))


# --------------------------------------------------------------------------- #
# view-time data runner
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def run_dashboard_source(dashboard: str, source_name: str) -> dict:
	"""Execute one SAVED data source of a dashboard as the SESSION user.

	Deliberately takes NO spec parameter — the saved child row is the only
	thing that ever runs, so a shared dashboard can never carry a viewer-
	supplied query. All record/field-level permissions are the tools' own
	(query weaves Engine permission conditions; get_list is the ORM;
	run_report is Frappe's report permission model) — a viewer sees exactly
	the rows THEY are allowed to see. Errors come back as HTTP-200 envelopes
	so the dashboard renderer can show a per-tile message."""
	doc = frappe.get_doc(DASHBOARD, dashboard)
	try:
		doc.check_permission("read")
	except frappe.PermissionError:
		_clear_perm_message_noise()
		return _error_envelope("PermissionError", _("You do not have access to this dashboard."))

	row = next((s for s in (doc.sources or []) if s.source_name == source_name), None)
	if row is None:
		return _error_envelope(
			"InvalidArgumentError",
			_("Unknown source '{0}' on this dashboard.").format(source_name),
		)
	try:
		spec = frappe.parse_json(row.spec)
	except Exception:
		spec = None
	if not isinstance(spec, dict):
		return _error_envelope("InvalidArgumentError", _("The saved spec for this source is not valid JSON."))

	tool = row.tool
	columns = None
	t0 = time.monotonic()
	# Bound the runner's wall-clock so a heavy report/query can't pin a web
	# worker (MariaDB only; best-effort). Scoped to this whitelisted request.
	_set_stmt_timeout(_SOURCE_STMT_TIMEOUT_S)
	try:
		if tool == "query":
			from jarvis.tools.query import query as _query

			# rows ONLY — the tool's "sql" debug key must never reach a viewer.
			rows = list(_query(spec, confirm_large=True).get("rows") or [])
		elif tool == "get_list":
			from jarvis.tools.get_list import get_list as _get_list

			kwargs = {k: v for k, v in spec.items() if k in _GET_LIST_SPEC_KEYS}
			kwargs.setdefault("doctype", "")
			rows = _get_list(confirm_large=True, **kwargs)
		elif tool == "run_report":
			from jarvis.tools.run_report import run_report as _run_report

			res = _run_report(spec.get("report_name") or "", spec.get("filters") or {})
			if (
				not isinstance(res, dict)
				or res.get("prepared_report")
				or not isinstance(res.get("result"), list)
			):
				return _error_envelope(
					"InvalidArgumentError",
					_(
						"This report did not return rows inline (it may run as a "
						"background Prepared Report), so it cannot serve a dashboard."
					),
				)
			columns = res.get("columns") or []
			# run_report has no inherent row cap (query/get_list are ≤1000 by the
			# tools); cap here so a huge report never materializes the full result
			# set into the browser payload. The post-loop slice still applies.
			rows = res["result"][: DASHBOARD_MAX_ROWS + 1]
		else:
			return _error_envelope("InvalidArgumentError", _("Unsupported tool: {0}").format(tool))
	except (PermissionDeniedError, frappe.PermissionError) as e:
		# has_permission pushes "no access" lines into the message log on the
		# denied path; clear them so they never ride back in _server_messages.
		_clear_perm_message_noise()
		return _error_envelope("PermissionError", str(e) or "permission denied")
	except InvalidArgumentError as e:
		return _error_envelope("InvalidArgumentError", str(e))
	except Exception:
		frappe.log_error(
			title="Jarvis: dashboard source run failed",
			message=frappe.get_traceback(),
		)
		return _error_envelope("InternalError", _("The data source could not be run. The error was logged."))

	truncated = False
	if len(rows) > DASHBOARD_MAX_ROWS:
		rows = rows[:DASHBOARD_MAX_ROWS]
		truncated = True
	data = {
		"source_name": source_name,
		"tool": tool,
		"rows": rows,
		"truncated": truncated,
		"took_ms": int((time.monotonic() - t0) * 1000),
	}
	if columns is not None:
		data["columns"] = columns
	return {"ok": True, "data": data}
