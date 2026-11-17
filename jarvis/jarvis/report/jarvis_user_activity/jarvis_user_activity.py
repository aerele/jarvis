"""Script Report over the unified user-activity stream (Jarvis Activity Event plus
Jarvis Agent Write), the Desk twin of the in-app "User activity" page.

Gives a Jarvis Admin one-click Desk export (Excel / CSV / PDF), print and chart of
what people did through Jarvis: reads, exports and writes made by the agent on their
behalf, and direct actions in the app. Metadata only: every row comes from
``jarvis.activity_report``, whose field allowlists keep conversation content out, so
the report can never surface it.

Two views, chosen by the ``view`` filter: "Events" (the newest rows: 500 on screen,
50,000 for an export, the truncation stated) and "Summary" (one row per person with
usage counts and red flags). The read model lives in ``jarvis.activity_report``; this
file only shapes it into report columns."""

import frappe
from frappe import _
from frappe.utils import getdate

from jarvis import activity_report
from jarvis.permissions import require_jarvis_admin

VIEWS = ("Events", "Summary")

# The Events view on screen. A Desk report renders every row into a datatable, so 50,000
# would freeze the browser tab; the full ceiling is for an export only.
SCREEN_LIMIT = 500

# How a Desk export reaches ``execute`` (the same on Frappe 15 and 16, verified in
# ``frappe.desk.query_report``): ``export_query`` is the whitelisted method (its dotted
# name is ``form_dict.cmd``, set by the RPC and the ``/api/method`` routes alike) and sets
# NO flag or form_dict key for the report; with "export in background" the work runs in the
# RQ job ``run_export_query_job``, where ``form_dict`` is empty and ``frappe.local.job``
# names the method instead.
_EXPORT_CMD = "frappe.desk.query_report.export_query"
_EXPORT_JOB = "frappe.desk.query_report.run_export_query_job"


def _is_export():
	"""True while Desk's query-report export (foreground or background job) is calling
	``execute``."""
	if (getattr(frappe.local, "form_dict", None) or {}).get("cmd") == _EXPORT_CMD:
		return True
	job = getattr(frappe.local, "job", None)
	return bool(job) and job.get("method") == _EXPORT_JOB


def _events_columns():
	return [
		{"fieldname": "at", "label": _("When"), "fieldtype": "Datetime", "width": 160},
		{"fieldname": "actor", "label": _("Person"), "fieldtype": "Link", "options": "User", "width": 180},
		{"fieldname": "actor_name", "label": _("Person Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "activity", "label": _("Activity"), "fieldtype": "Data", "width": 110},
		{"fieldname": "label", "label": _("What"), "fieldtype": "Data", "width": 200},
		{"fieldname": "method", "label": _("Method"), "fieldtype": "Data", "width": 160},
		{"fieldname": "ref_doctype", "label": _("Reference Doctype"), "fieldtype": "Data", "width": 160},
		{"fieldname": "ref_name", "label": _("Reference Name"), "fieldtype": "Data", "width": 160},
		{"fieldname": "rows", "label": _("Records"), "fieldtype": "Int", "width": 80},
		{"fieldname": "outcome", "label": _("Outcome"), "fieldtype": "Data", "width": 90},
		{"fieldname": "channel", "label": _("Channel"), "fieldtype": "Data", "width": 100},
		{"fieldname": "code", "label": _("Code"), "fieldtype": "Data", "width": 120},
	]


def _summary_columns():
	def num(fieldname, label, width=90):
		return {"fieldname": fieldname, "label": label, "fieldtype": "Int", "width": width}

	return [
		{"fieldname": "user", "label": _("Person"), "fieldtype": "Link", "options": "User", "width": 180},
		{"fieldname": "name", "label": _("Person Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "flags", "label": _("Flags"), "fieldtype": "Data", "width": 360},
		num("turns", _("Turns")),
		num("tokens", _("Tokens"), 110),
		num("reads", _("Reads")),
		num("rows_read", _("Records Read"), 100),
		num("exports", _("Exports")),
		num("rows_exported", _("Records Exported"), 120),
		num("writes", _("Writes")),
		num("erp_deletes", _("ERP Deletes"), 100),
		num("refused_denied", _("Refused or Denied"), 130),
		num("failed", _("Failed")),
		num("actions", _("Actions")),
	]


def _day(value):
	return str(getdate(value)) if value else ""


def _scope_line(first_event_at):
	"""What this report covers, and since when. Desk is out of scope on purpose, and
	the two sources started recording on different days (the app-action hook shipped
	after the tool-path one), so each states its own start. One whole template per case:
	a translator needs the full sentence, not fragments to join."""
	agent = _day((first_event_at or {}).get("agent"))
	app = _day((first_event_at or {}).get("app"))
	if agent and app:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"Agent activity recorded from {0}; app actions from {1}."
		).format(agent, app)
	if agent:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"Agent activity recorded from {0}. App actions are not recorded yet."
		).format(agent)
	if app:
		return _(
			"Covers actions taken in Jarvis only (not Desk). "
			"No agent activity recorded yet; app actions from {0}."
		).format(app)
	return _(
		"Covers actions taken in Jarvis only (not Desk). "
		"No agent activity recorded yet. App actions are not recorded yet."
	)


def _kept_note():
	return _("Data is kept for {0} days.").format(activity_report.HORIZON_DAYS)


def _stale_note(n):
	if n == 1:
		return _("Totals for 1 day are still being prepared.")
	return _("Totals for {0} days are still being prepared.").format(n)


def _partial_note(n):
	"""Days the summary left out because the daily totals were too far behind to compute
	them on request (``activity_report.LIVE_DAY_CAP``)."""
	if n == 1:
		return _("Totals for the oldest day in this range are not included yet.")
	return _("Totals for the {0} oldest days in this range are not included yet.").format(n)


def _health_notes(health):
	"""Plain-English warnings from the logging-health block, in a fixed order."""
	notes = []
	if health.get("warn"):
		notes.append(
			_(
				"Warning: agents ran tools in the last 24 hours but no activity was recorded, "
				"so logging may be broken."
			)
		)
	if health.get("scheduler_disabled"):
		notes.append(_("Background jobs are paused on this site, so daily totals are not being updated."))
	lag = health.get("rollup_lag_days") or 0
	if lag > 1:
		notes.append(
			_(
				"Daily totals are {0} days behind. Ask your system administrator to check the scheduler."
			).format(lag)
		)
	return notes


def _before_horizon(from_date):
	"""events() does not clamp; say so when the requested start predates what is kept."""
	return bool(from_date) and getdate(from_date) < activity_report.horizon_day()


def _events(filters):
	exporting = _is_export()
	if not frappe.db.table_exists(activity_report.ACTIVITY_EVENT):
		return _events_columns(), [], _preparing_note()  # code deployed, migrate not run yet
	limit = activity_report.EXPORT_MAX_LIMIT if exporting else SCREEN_LIMIT
	res = activity_report.events(filters, limit=limit, for_export=True)
	# Events view only needs the two start dates; the full summary would aggregate
	# every person in range for nothing.
	message = _scope_line(activity_report.first_event_at())
	if _before_horizon(filters.get("from_date")):
		message += " " + _kept_note()
	if res.get("truncated"):
		if exporting:
			message += " " + _(
				"Showing the newest {0} events; narrow the date range to see the rest."
			).format(f"{limit:,}")
		else:
			message += " " + _(
				"Showing the newest {0} events; narrow the filters or export to see more."
			).format(f"{limit:,}")
	return _events_columns(), res["rows"], message


def _preparing_note():
	return _("Activity totals are still being prepared. Please try again in a few minutes.")


def _summary(filters):
	res = activity_report.summary(filters)
	if res.get("preparing"):
		return _summary_columns(), [], _preparing_note()
	data = []
	for p in res["people"]:
		row = {k: v for k, v in p.items() if k != "flags"}
		row["flags"] = "; ".join(f["label"] for f in p.get("flags") or [])
		data.append(row)
	message = _scope_line(res.get("first_event_at"))
	if res.get("clamped"):
		message += " " + _kept_note()
	stale = res.get("stale_days") or 0
	if stale > 0:
		message += " " + _stale_note(stale)
	if res.get("partial") and res.get("excluded_days"):
		message += " " + _partial_note(res["excluded_days"])
	for note in _health_notes(res.get("logging_health") or {}):
		message += " " + note
	return _summary_columns(), data, message


def _parse_filters(filters):
	"""The filters as a dict. Desk sends a dict or a JSON object; anything else (a list, a
	number, text that is not JSON) is a clear refusal, not a 500 from ``dict()``. Nothing
	at all (``None``, ``""``, ``[]``) means no filters, as it always did."""
	if not filters:
		return {}
	try:
		parsed = frappe.parse_json(filters) if isinstance(filters, str) else filters
	except ValueError:
		parsed = None
	if not isinstance(parsed, dict):
		frappe.throw(_("The report filters must be an object of filter names and values."))
	return dict(parsed)


def execute(filters=None):
	# The Report doctype's roles already gate WHO can open this; re-check because
	# execute() is reachable programmatically, and match the in-app page's readers.
	require_jarvis_admin()
	filters = _parse_filters(filters)
	view = filters.get("view") or "Events"
	if view not in VIEWS:
		frappe.throw(_("Unknown view {0}").format(frappe.bold(view)))
	if view == "Summary":
		return _summary(filters)
	return _events(filters)
