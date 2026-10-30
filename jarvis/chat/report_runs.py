"""Background (prepared) reports a chat started: that chat's report card and the
Approval Board's "Reports ready in your chats" list.

Read from the chat's own ``run_report`` rows, no record of its own: a row that
answered ``started`` / ``generating`` opens a card for the Prepared Report it names
(``run``). Any later row for that run, or for the same report and filters, replaces
it: ``ready`` showed the results, ``failed`` told the user, a new run opens its own
card. The card's state is the Prepared Report's: preparing, ready or failed.
"""

import json
import re

import frappe
from frappe.query_builder.functions import Function
from frappe.utils import add_days, formatdate, now

from jarvis.exceptions import InvalidArgumentError
from jarvis.permissions import require_jarvis_user
from jarvis.tools import _prepared_reports

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"
_TOOL = "run_report"
_BOARD_DAYS = 14
_BOARD_MAX = 10
_LABEL_MAX = 120
_FAILED = ("Error", "Failed")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json_functions_available() -> bool:
	"""MariaDB reads the card fields in SQL; elsewhere they are parsed in Python."""
	return frappe.db.db_type == "mariadb"


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def chat_report_runs(conversation: str) -> dict:
	"""This chat's background reports not shown yet: ``{items: [{conversation, run,
	report_name, filters, status: preparing|ready|failed, ready_at}]}``. Owner-only."""
	me = frappe.session.user
	if frappe.db.get_value(CONV, conversation, "owner") != me:
		frappe.throw("Not permitted", frappe.PermissionError)
	m = frappe.qb.DocType(MSG)
	return {"items": _with_state(_open_runs(_run_rows(m.conversation == conversation)), me)}


def ready_reports(me: str) -> list[dict]:
	"""For the Approval Board: background reports ready in ``me``'s chats (asked in the
	last 14 days) and not shown there yet, newest first, at most 10."""
	m, c = frappe.qb.DocType(MSG), frappe.qb.DocType(CONV)
	rows = _run_rows(
		(c.owner == me) & (c.status != "Archived") & (m.creation >= add_days(now(), -_BOARD_DAYS))
	)
	chats = {r.conversation: r for r in rows}
	ready = [i for i in _with_state(_open_runs(rows), me) if i["status"] == "ready"]
	ready.sort(key=lambda i: i["ready_at"], reverse=True)
	return [
		{
			**i,
			"title": chats[i["conversation"]].title or "",
			"origin_page": chats[i["conversation"]].origin_page or "",
		}
		for i in ready[:_BOARD_MAX]
	]


class _JsonValue(Function):
	def __init__(self, field, path):
		super().__init__("JSON_VALUE", field, path)


def _run_rows(where) -> list:
	"""run_report rows (seq order per chat) with only what a card needs: its status,
	run and report. On MariaDB these are read in SQL, so a finished report's rows
	(up to 2000 per answer) never leave the database."""
	m, c = frappe.qb.DocType(MSG), frappe.qb.DocType(CONV)
	query = (
		frappe.qb.from_(m)
		.join(c)
		.on(c.name == m.conversation)
		.select(m.conversation, m.tool_args, m.tool_name, c.title, c.origin_page)
		.where((m.role == "tool") & m.tool_name.isin((_TOOL, "query_report")) & where)
		.orderby(m.conversation)
		.orderby(m.seq)
	)
	mariadb = _json_functions_available()
	if mariadb:
		query = query.select(
			*(
				_JsonValue(m.tool_result, f"$.data.{k}").as_(k.replace(".", "_"))
				for k in (
					"status",
					"run",
					"report_name",
					"read_status",
					"prepared_report",
					"coverage.complete",
				)
			),
		)
	else:
		query = query.select(m.tool_result)
	rows = query.run(as_dict=True)
	if not mariadb:
		for r in rows:
			data = _data(r.pop("tool_result"))
			r.update(
				{k: data.get(k) for k in ("status", "run", "report_name", "read_status", "prepared_report")}
			)
			r.coverage_complete = (data.get("coverage") or {}).get("complete")
	return rows


def _open_runs(rows) -> list[dict]:
	"""Runs started (or found generating) and not superseded, from run_report rows in
	seq order per chat. The newest row for a run, or for its report and filters, wins."""
	open_runs, defaults = {}, {}
	for r in rows:
		args = _json(r.tool_args)
		if r.get("tool_name") == "query_report":
			# An inspection is not an answer. Close only this exact run after a
			# successful query loaded the complete permitted source.
			if args.get("sql") and r.get("coverage_complete") in (True, "true", "1", 1):
				open_runs.pop((r.conversation, r.get("prepared_report")), None)
			continue
		run, report = r.run, r.report_name or args.get("report_name")
		if not run or not report:
			continue
		key = _key(report, args.get("filters"), defaults)
		for k, v in list(open_runs.items()):
			if k[0] == r.conversation and (k[1] == run or (key and v["key"] == key)):
				del open_runs[k]
		if r.status in ("started", "generating") or (
			r.status == "ready" and r.get("read_status") == "not_read"
		):
			open_runs[(r.conversation, run)] = {
				"conversation": r.conversation,
				"run": run,
				"report_name": report,
				"filters": _filters_label(args.get("filters")),
				"key": key,
			}
	return list(open_runs.values())


def _key(report: str, filters, defaults: dict) -> str | None:
	"""Report + filters by meaning, as run_report matches them (None when unreadable)."""
	if report not in defaults:
		defaults[report] = _prepared_reports._report_defaults(report)
	try:
		canonical = _prepared_reports._canonical_filters(filters)
	except InvalidArgumentError:
		return None
	return report + _prepared_reports._match_key({**defaults[report], **canonical})


def _with_state(runs: list[dict], owner: str) -> list[dict]:
	"""Each run with its Prepared Report's state. A run stopped, deleted or not
	``owner``'s own is dropped."""
	if not runs:
		return []
	reports = {
		p.name: p
		for p in frappe.get_all(
			"Prepared Report",
			filters={"name": ["in", list({r["run"] for r in runs})], "owner": owner},
			fields=["name", "status", "report_end_time", "modified"],
			ignore_permissions=True,  # owner-scoped; ERP users lack the Prepared Report role
		)
	}
	errors = _failure_errors([name for name, p in reports.items() if p.status in _FAILED])
	out = []
	for r in runs:
		p = reports.get(r["run"])
		if not p or p.status == "Cancelled":
			continue
		status = "ready" if p.status == "Completed" else "failed" if p.status in _FAILED else "preparing"
		item = {k: v for k, v in r.items() if k != "key"}
		ready_at = str(p.report_end_time or p.modified) if status == "ready" else ""
		item.update(status=status, ready_at=ready_at)
		if status == "failed":
			item["error"] = errors.get(r["run"], "")
		out.append(item)
	return out


def _failure_errors(names: list[str]) -> dict:
	"""Each failed run's reason as its owner may read it (``failure_reason``), never
	the stored traceback. Read only for failed runs: the traceback can be large."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Prepared Report",
		filters={"name": ["in", names]},
		fields=["name", "error_message"],
		ignore_permissions=True,  # the names were already owner-scoped by the caller
	)
	return {row.name: _prepared_reports.failure_reason(row.error_message)[1] for row in rows}


def _filters_label(filters) -> str:
	"""The filter values in one line ("Acme · 01-09-2026"), unset ones left out."""
	if isinstance(filters, str):
		filters = _json(filters)
	if not isinstance(filters, dict):
		return ""
	parts = []
	for value in filters.values():
		if _prepared_reports._unset(value):
			continue
		if isinstance(value, list | tuple):
			parts.append(", ".join(str(v) for v in value))
		elif isinstance(value, str) and _DATE.match(value):
			parts.append(formatdate(value))
		else:
			parts.append(str(value))
	label = " · ".join(parts)
	return label if len(label) <= _LABEL_MAX else label[: _LABEL_MAX - 1] + "…"


def _json(raw) -> dict:
	try:
		value = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _data(raw) -> dict:
	data = _json(raw).get("data")
	return data if isinstance(data, dict) else {}
