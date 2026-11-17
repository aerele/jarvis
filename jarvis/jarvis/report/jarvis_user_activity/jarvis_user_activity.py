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

import sys

import frappe
from frappe import _

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


# An Auto Email Report (a scheduled mail, or its Send Now / Download) calls the report
# through ``AutoEmailReport.get_report_content`` -> ``Report.get_data``, which passes its
# "no. of rows" only to Report Builder reports: a script report's ``execute`` gets the
# filters alone, and no ``cmd`` or job (verified in Frappe 15.122 and 16.53,
# ``email/doctype/auto_email_report/auto_email_report.py``). The only trace of the caller
# is the call stack, so look for that one method in that one file.
_AUTO_EMAIL_FILE = "email/doctype/auto_email_report/auto_email_report.py"
_AUTO_EMAIL_METHOD = "get_report_content"


def _from_auto_email_report():
	frame = sys._getframe(1)
	while frame is not None:
		code = frame.f_code
		if code.co_name == _AUTO_EMAIL_METHOD and code.co_filename.replace("\\", "/").endswith(
			_AUTO_EMAIL_FILE
		):
			return True
		frame = frame.f_back
	return False


def _is_export():
	"""True while an export (Desk's query-report export, foreground or background job, or an
	Auto Email Report) is calling ``execute``: the full row ceiling applies, not the screen's."""
	if (getattr(frappe.local, "form_dict", None) or {}).get("cmd") == _EXPORT_CMD:
		return True
	job = getattr(frappe.local, "job", None)
	if job and job.get("method") == _EXPORT_JOB:
		return True
	return _from_auto_email_report()


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


# The message builders live in ``jarvis.activity_report`` (shared with the in-app page's
# API, so the two always say the same thing), e.g. ``activity_report.scope_line``.


def _events(filters):
	exporting = _is_export()
	if not frappe.db.table_exists(activity_report.ACTIVITY_EVENT):
		# code deployed, migrate not run yet
		return _events_columns(), [], activity_report.preparing_note()
	limit = activity_report.EXPORT_MAX_LIMIT if exporting else SCREEN_LIMIT
	res = activity_report.events(filters, limit=limit, for_export=True)
	# Events view only needs the two start dates; the full summary would aggregate
	# every person in range for nothing.
	notes = activity_report.events_notes(activity_report.first_event_at(), filters.get("from_date"))
	if res.get("truncated"):
		if exporting:
			notes.append(
				_("Showing the newest {0} events; narrow the date range to see the rest.").format(
					f"{limit:,}"
				)
			)
		else:
			notes.append(
				_("Showing the newest {0} events; narrow the filters or export to see more.").format(
					f"{limit:,}"
				)
			)
	return _events_columns(), res["rows"], " ".join(notes)


def _summary(filters):
	res = activity_report.summary(filters)
	if res.get("preparing"):
		return _summary_columns(), [], activity_report.preparing_note()
	data = []
	for p in res["people"]:
		row = {k: v for k, v in p.items() if k != "flags"}
		row["flags"] = "; ".join(f["label"] for f in p.get("flags") or [])
		data.append(row)
	return _summary_columns(), data, " ".join(activity_report.summary_notes(res))


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
