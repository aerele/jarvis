"""Durable append-only sink for the admin "User activity" report.

One metadata-only ``Jarvis Activity Event`` row per read, export, direct app
action, refused call or rejected session (plan:
``.claude/workflow/plans/2026-11-06-user-activity-report.md`` 4.2). The twin of
``jarvis.agent_audit`` and it keeps the same contract, which is the whole design:

- NEVER raises and NEVER commits. A failed audit insert must not fail, delay or
  roll back the user's call; and a commit here would break ``FrappeTestCase``'s
  class-level rollback isolation and could half-commit a tool's transaction.
- The insert runs in its OWN savepoint, so a failure rolls back only itself and
  leaves the caller's transaction (the tool's write) intact.
- Failures go to a FILE logger only, never ``frappe.log_error``: an Error Log insert
  is itself a DB write that would touch this same transaction, and its traceback
  carries frame locals (tool args, which this table must never hold). Even the file
  line carries the exception CLASS and raise site, not its message, because a
  driver or validation message can quote a column value.
- Metadata only. The doctype has no column for message text, tool arguments,
  results or error messages, so storing them is structurally impossible; this
  module additionally coerces and truncates every value it is handed.
"""

import time
import traceback

import frappe

DOCTYPE = "Jarvis Activity Event"

# Mirror the doctype's Select options (a test pins that they cannot drift). An
# unknown value is rejected here, not coerced: a mislabelled security event is
# worse than a missing one that the file log reports.
EVENTS = frozenset({"read", "export", "action", "denied_call", "auth_rejected", "foreign_session"})
OUTCOMES = frozenset({"ok", "denied", "failed"})
CHANNELS = frozenset({"desktop", "pwa", "chat", "agent_run", "macro", "trigger", "api"})

# Field lengths of the doctype (Data defaults to 140).
_DATA_LEN = 140
_CODE_LEN = 64

# Tools that only describe the schema or the catalogue: the model calls them
# constantly to learn what exists, they return no business records, and logging
# them would bury the real reads (plan E4). Explicit on purpose: a new tool is
# LOGGED as a read until someone decides it is introspection. Each name is
# pinned against the registry by test_tool_sets_name_real_tools.
INTROSPECTION_TOOLS = frozenset(
	{
		"get_schema",  # DocType field map
		"describe_customizations",  # site customisation index
		"get_report_filters",  # a report's filter definitions, no report data
		"list_connector_actions",  # connector action catalogue
		"list_app_modules",  # app-learning module catalogue
	}
)

# Write-classified tools that produce a downloadable file: they are in
# api._WRITE_TOOLS (so they already get an Agent Write row) but represent a DATA
# EXPORT to the admin, so the tool path also emits an ``export`` event for them.
EXPORT_TOOLS = frozenset({"export_excel", "export_query", "export_document", "report_pdf", "download_pdf"})

# Where a tool result may carry its record list: ``rows`` (get_list paged, query),
# ``result`` (run_report / query_report), ``docs`` (get_doc batch), ``results``
# (get_workflow_transitions); ``data`` / ``records`` are the generic envelopes.
_LIST_KEYS = ("data", "rows", "result", "records", "docs", "results")
# Count keys of an export result: ``total`` on export_query's immediate envelope
# (_finish_env), ``rows_written`` on its background job state (jobs.py).
# export_excel / export_document / report_pdf / download_pdf return no row count.
_EXPORT_COUNT_KEYS = ("total", "rows_written")
# Args that name the doctype/report a call targeted; strings only.
_REF_ARG_KEYS = ("doctype", "report_name", "ref_doctype")

_CLIENT_HEADER = "X-Jarvis-Client"
_CLIENT_VALUES = frozenset({"desktop", "pwa"})


def _text(value, limit=_DATA_LEN) -> str:
	if value is None:
		return ""
	try:
		return str(value)[:limit]
	except Exception:
		return ""


def _count(value) -> int:
	try:
		return max(int(value), 0)
	except (TypeError, ValueError, OverflowError):
		return 0


def row_count_of(tool, data) -> int:
	"""Rows a tool call returned or exported, 0 when not derivable.

	A bare list is its own length; a dict carries either a record list under one
	of ``data/rows/result/records`` or, for the export tools, a count key."""
	if isinstance(data, list):
		return len(data)
	if not isinstance(data, dict):
		return 0
	if tool in EXPORT_TOOLS:
		for key in _EXPORT_COUNT_KEYS:
			if key in data:
				return _count(data[key])
		return 0
	for key in _LIST_KEYS:
		if isinstance(data.get(key), list):
			return len(data[key])
	# get_doc on one record returns the document itself (no list key): one row read.
	# A batch (names=[...]) carries `docs`, counted above.
	if tool == "get_doc":
		return 1
	return 0


def _known_ref(value: str) -> str:
	"""The canonical name of the DocType or Report ``value`` names, else "".

	The argument is model-supplied free text (a failed call is logged with whatever
	string was passed), so it is stored only when it IS a schema object name: a customer
	name or phone number pasted into ``doctype`` must never land in the table (plan D1).
	Request-cached single-column reads; the DB's own spelling is returned because the
	collation is case-insensitive. A lookup error answers "" (nothing stored)."""
	if len(value) > _DATA_LEN:
		return ""
	try:
		for doctype in ("DocType", "Report"):
			found = frappe.db.get_value(doctype, value, "name", cache=True)
			if found:
				return _text(found)
	except Exception:
		pass
	return ""


def ref_doctype_of(tool, args) -> str:
	"""The doctype or report a call targeted, from the first STRING argument only (a
	list or dict value could be anything), kept only when it names an existing DocType
	or Report (see ``_known_ref``)."""
	if not isinstance(args, dict):
		return ""
	for key in _REF_ARG_KEYS:
		val = args.get(key)
		if isinstance(val, str) and val:
			return _known_ref(val)
	return ""


def _client_header() -> str:
	try:
		return (frappe.request.headers.get(_CLIENT_HEADER) or "").strip().lower()
	except (AttributeError, RuntimeError):
		return ""  # no request bound: tests, `bench execute`, background jobs


def resolve_channel() -> str:
	"""Which surface a call came from. A LABEL for the admin, never a gate.

	1. ``X-Jarvis-Client: desktop|pwa``, set by the same-origin shim in each SPA
	   (anything else, including a forged value, is ignored);
	2. a plugin session key on this request (``_agent_run_ctx``, set by
	   ``api._dispatch_from_session`` for the whole dispatch) is the agent's own
	   tool call: ``chat``;
	3. otherwise ``api``.

	Agent-run / macro / trigger have no cheap request-local marker (the delegate
	capability is a DB read per call, macro/skill markers are consume-once and
	cleared before the audit rows are written), so the call site that knows
	passes ``channel=`` explicitly instead."""
	client = _client_header()
	if client in _CLIENT_VALUES:
		return client
	from jarvis.tools import _agent_run_ctx

	if _agent_run_ctx.get_session_key():
		return "chat"
	return "api"


# De-duplication of the failure line: a broken table fails on EVERY tool call, and one
# line per call would fill the log (and bury the first, useful one). One identical line
# (same site, event kind, text) per process per window; it resets on a worker restart.
_FAILURE_WINDOW_S = 300
_FAILURE_MAX_KEYS = 256
_last_failure_logged: dict[str, float] = {}


def _reset_failure_log() -> None:
	"""Forget what was logged (tests; also lets a fixed fault be re-reported)."""
	_last_failure_logged.clear()


def _should_log_failure(key: str) -> bool:
	now = time.monotonic()
	last = _last_failure_logged.get(key)
	if last is not None and now - last < _FAILURE_WINDOW_S:
		return False
	if len(_last_failure_logged) >= _FAILURE_MAX_KEYS:
		for stale in [k for k, t in _last_failure_logged.items() if now - t >= _FAILURE_WINDOW_S]:
			_last_failure_logged.pop(stale, None)
		if len(_last_failure_logged) >= _FAILURE_MAX_KEYS:
			_last_failure_logged.clear()  # bounded: a flood of distinct keys cannot grow it
	_last_failure_logged[key] = now
	return True


def _fail(what: str, exc: BaseException | None = None, event=None) -> None:
	"""File-log one failure. Fixed text plus the site, the event kind (only a known one,
	so a caller's junk is never echoed) and, for an exception, its class and raise site:
	never the message, args or frame locals. Identical lines are logged once per five
	minutes (``_should_log_failure``)."""
	try:
		where = ""
		if exc is not None and exc.__traceback__ is not None:
			frame = traceback.extract_tb(exc.__traceback__)[-1]
			where = f" at {frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}"
		kind = f" ({type(exc).__name__}{where})" if exc is not None else ""
		site = _text(getattr(frappe.local, "site", "") or "", 80)
		known = event if isinstance(event, str) and event in EVENTS else "invalid"
		line = f"activity log [{site}] {known}: {what}{kind}"
		if _should_log_failure(line):
			frappe.logger("jarvis.activity_log").error(line)
	except Exception:
		pass  # the logger itself failing must not surface either


def record(
	event,
	*,
	actor,
	method="",
	label="",
	ref_doctype="",
	ref_name="",
	row_count=0,
	outcome="ok",
	channel=None,
	code="",
) -> str | None:
	"""Insert one ``Jarvis Activity Event``. Returns its name, or ``None`` when
	nothing was written (rejected input, missing table, insert failure).

	Safe to call from inside any request: never raises, never commits (see the
	module docstring)."""
	try:
		if event not in EVENTS:
			return _fail_none("unknown event rejected", event)
		if outcome not in OUTCOMES:
			return _fail_none("unknown outcome rejected", event)
		if channel is not None and channel not in CHANNELS:
			return _fail_none("unknown channel rejected", event)

		# Code deployed before `bench migrate` has run: not an error, nothing to log to.
		if not frappe.db.table_exists(DOCTYPE):
			return None

		from jarvis.tools import _private_output

		# Delegate metadata redaction (the Deposit run): same condition agent_audit
		# uses to blank ref_doctype/ref_name/bulk_count. Whatever names the target or
		# its size is dropped, along with the free-text label and the tool name.
		redacted = _private_output.metadata_redacted()
		if redacted:
			label = ref_doctype = ref_name = ""
			row_count = 0

		actor = _text(actor)
		doc = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"at": frappe.utils.now(),
				"actor": actor,
				# Snapshot, so the row stays attributable after the user is deleted
				# (events are kept to the 90-day expiry). Single-field, request-cached
				# read: lighter than get_cached_value, which unpickles the whole User.
				"actor_name": (
					_text(frappe.db.get_value("User", actor, "full_name", cache=True) or actor)
					if actor
					else ""
				),
				"event": event,
				"method": _text(_private_output.redact_delegate_tool(method)),
				"label": _text(label),
				"ref_doctype": _text(ref_doctype),
				"ref_name": _text(ref_name),
				"row_count": _count(row_count),
				"outcome": outcome,
				"channel": channel or resolve_channel(),
				"code": _text(code, _CODE_LEN),
			}
		)

		# Document.insert ends in notify_update(): a doc_update plus a site-wide list_update
		# naming the acting user, published per tool call for a table nobody watches live.
		# The flag is read as `flags.get("notify_update", True)` on v15 and v16 alike.
		doc.flags.notify_update = False

		sp = f"activity_log_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp)
		try:
			# Server-authored; no role holds create. ignore_links: `actor` may name a
			# user deleted since (an auth_rejected for a stale session).
			doc.insert(ignore_permissions=True, ignore_links=True)
		except Exception:
			frappe.db.rollback(save_point=sp)  # recover the outer txn; never poison the caller
			raise
		try:
			frappe.db.release_savepoint(sp)
		except Exception:
			pass
		return doc.name
	except Exception as e:
		try:
			if frappe.db.is_table_missing(e):
				return None  # table dropped under a stale table_exists cache
		except Exception:
			pass
		_fail("insert failed", e, event)
		return None


def _fail_none(what: str, event=None) -> None:
	_fail(what, event=event)
	return None
