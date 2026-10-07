"""Background (prepared) reports a chat started: that chat's report card and the
Approval Board's "Reports ready in your chats" list.

Read from the chat's own ``run_report`` rows, no record of its own: a row that
answered ``started`` / ``generating`` opens a card for the Prepared Report it names
(``run``); a later row that answered ``ready`` for that run, or for the same report
and filters, showed the results and closes it. The card's state is the Prepared
Report's: preparing, ready or failed.
"""

import json
import re

import frappe
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


@frappe.whitelist()
@require_jarvis_user
def chat_report_runs(conversation: str) -> dict:
	"""This chat's background reports not shown yet: ``{items: [{conversation, run,
	report_name, filters, status: preparing|ready|failed, ready_at}]}``. Owner-only."""
	me = frappe.session.user
	if frappe.db.get_value(CONV, conversation, "owner") != me:
		frappe.throw("Not permitted", frappe.PermissionError)
	rows = frappe.get_all(
		MSG,
		filters={"conversation": conversation, "role": "tool", "tool_name": _TOOL},
		fields=["conversation", "tool_args", "tool_result"],
		order_by="seq asc",
	)
	return {"items": _with_state(_open_runs(rows), me)}


def ready_reports(me: str) -> list[dict]:
	"""For the Approval Board: background reports ready in ``me``'s chats (asked in the
	last 14 days) and not shown there yet, newest first, at most 10."""
	rows = frappe.db.sql(
		"""SELECT m.conversation, m.tool_args, m.tool_result, c.title, c.origin_page
		FROM `tabJarvis Chat Message` m
		JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE c.owner = %(me)s AND c.status != 'Archived'
		AND m.role = 'tool' AND m.tool_name = %(tool)s AND m.creation >= %(since)s
		ORDER BY m.conversation, m.seq""",
		{"me": me, "tool": _TOOL, "since": add_days(now(), -_BOARD_DAYS)},
		as_dict=True,
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


def _open_runs(rows) -> list[dict]:
	"""Runs started (or found generating) and not shown yet, from run_report rows in
	seq order per chat."""
	open_runs, defaults = {}, {}
	for r in rows:
		data, args = _data(r.tool_result), _json(r.tool_args)
		run, status = data.get("run"), data.get("status")
		report = data.get("report_name") or args.get("report_name")
		if not run or not report:
			continue
		key = _key(report, args.get("filters"), defaults)
		if status in ("started", "generating"):
			open_runs.setdefault(
				(r.conversation, run),
				{
					"conversation": r.conversation,
					"run": run,
					"report_name": report,
					"filters": _filters_label(args.get("filters")),
					"key": key,
				},
			)
		elif status == "ready":
			for k, v in list(open_runs.items()):
				if k[0] == r.conversation and (k[1] == run or (key and v["key"] == key)):
					del open_runs[k]
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
			fields=["name", "status", "report_end_time"],
			ignore_permissions=True,  # owner-scoped; ERP users lack the Prepared Report role
		)
	}
	out = []
	for r in runs:
		p = reports.get(r["run"])
		if not p or p.status == "Cancelled":
			continue
		status = "ready" if p.status == "Completed" else "failed" if p.status in _FAILED else "preparing"
		item = {k: v for k, v in r.items() if k != "key"}
		item.update(status=status, ready_at=str(p.report_end_time or "") if status == "ready" else "")
		out.append(item)
	return out


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
