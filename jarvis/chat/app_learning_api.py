"""SPA-facing API for "Learn from custom apps" (the Settings card + runs list).

Whitelisted endpoints for scheduling, cancelling and monitoring
``Jarvis App Learning Run`` rows (the engine lives in
``jarvis.learning.app_analysis``). EVERY endpoint is manage-gated (System
Manager / Jarvis Admin; Administrator implicit) for M2 — this is an
org-config surface with real token cost and direct wiki/skill writes, so
plain jarvis users get a PermissionError even on the read paths. Mirrors the
``jarvis.chat.triggers_api`` shape ({ok, data} envelope, frozen list-page
envelope, whitelisted filters/sort).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from jarvis.chat.macros_api import _clamp_page, _lk, _load_filters
from jarvis.learning import app_analysis
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_user

RUN = "Jarvis App Learning Run"

_STATUSES = {
	"Queued",
	"Zipping",
	"Analyzing",
	"Ingesting",
	"Completed",
	"Failed",
	"Cancelled",
}
_CANCELLABLE = ("Queued", "Zipping", "Analyzing")

_RUN_FILTERS = {"app", "status"}
_RUN_SORTABLE = {
	"creation": "creation",
	"app": "app",
	"status": "status",
	"finished_at": "finished_at",
}

# Furthest out a run may be scheduled (spec: cap 30 days).
MAX_SCHEDULE_DAYS_OUT = 30

_ROW_FIELDS = (
	"name",
	"app",
	"status",
	"scheduled_at",
	"started_at",
	"finished_at",
	"conversation",
	"zip_size",
	"file_count",
	"batches_total",
	"batches_done",
	"pages_written",
	"skills_created",
	"skills_deferred",
	"error",
	"requested_by",
	"creation",
)

_DATETIME_ROW_KEYS = ("scheduled_at", "started_at", "finished_at", "creation")


def _require_manage() -> None:
	"""System Manager / Jarvis Admin (Administrator implicit) only — M2 keeps
	the whole surface manage-gated, reads included."""
	if not has_jarvis_admin_access():
		frappe.throw(
			_("You need the Jarvis Admin or System Manager role to manage app learning."),
			frappe.PermissionError,
		)


def _row_out(row: dict) -> dict:
	for key in _DATETIME_ROW_KEYS:
		if key in row:
			row[key] = str(row[key]) if row[key] else ""
	return row


# --------------------------------------------------------------------------- #
# apps + overview
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def list_custom_apps() -> dict:
	"""Installed non-core apps with a size probe and their latest run, for the
	scheduling UI."""
	_require_manage()
	return {"ok": True, "data": app_analysis.list_custom_apps_data()}


@frappe.whitelist()
@require_jarvis_user
def get_app_learning_overview() -> dict:
	"""State for the settings card: the active run (if any), the queued count
	and the schedulable apps list."""
	_require_manage()
	active = frappe.get_all(
		RUN,
		filters={"status": ["in", list(app_analysis.ACTIVE)]},
		fields=list(_ROW_FIELDS),
		order_by="creation asc",
		limit=1,
	)
	return {
		"ok": True,
		"data": {
			"active_run": _row_out(active[0]) if active else None,
			"queued": frappe.db.count(RUN, {"status": "Queued"}),
			"apps": app_analysis.list_custom_apps_data(),
		},
	}


# --------------------------------------------------------------------------- #
# schedule / cancel
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def schedule_app_learning(apps: str, when: str = "", consent: int = 0) -> dict:
	"""RETIRED (rollback-operable). The chat-batch custom-app learning pipeline has been
	REPLACED by the Custom App Learning *scribe* delegate agent (marketplace slug
	``custom-app-learning``), which reads app source and writes the wiki in the Jarvis
	container instead of feeding source through a chat transcript. This endpoint REFUSES
	while the pipeline is retired (the default) so no new chat-pipeline run can start; the
	engine and the ``Jarvis App Learning Run`` doctype stay present (rollback + historical
	rows) but dormant. Install/run the agent from the Agents page instead.

	CA3-5: the refusal is CONDITIONAL on ``app_analysis._legacy_retired()``. When an
	operator DELIBERATELY re-enables the pipeline for rollback (conf
	``jarvis_app_learning_reenable``), this endpoint executes the retained scheduling body
	below, so a rollback can actually queue a run — the self-gating ``app_analysis.tick``
	cron (re-registered in hooks, inert while retired) then drives it forward."""
	_require_manage()
	if app_analysis._legacy_retired():
		frappe.throw(
			_(
				"The chat-batch app-learning pipeline is retired. Install and run the "
				"Custom App Learning agent from the Agents page instead."
			)
		)
	# Retained legacy scheduling body — operable ONLY when the pipeline is re-enabled for
	# rollback (the refusal above is bypassed).
	if cint(consent) != 1:
		frappe.throw(_("Consent to the token cost and direct wiki/skill writes is required."))

	try:
		raw = frappe.parse_json(apps) if isinstance(apps, str) else apps
	except Exception:
		raw = None
	names: list[str] = []
	if isinstance(raw, list):
		for a in raw:
			if isinstance(a, str) and a.strip() and a.strip() not in names:
				names.append(a.strip())
	if not names:
		frappe.throw(_("Pick at least one app to learn from."))

	valid = set(app_analysis._installed_custom_apps())
	for app in names:
		if app not in valid:
			frappe.throw(_("App {0} is not available for learning.").format(app))

	now = now_datetime()
	when = (when or "").strip()
	if when:
		scheduled_at = get_datetime(when)
		if not scheduled_at:
			frappe.throw(_("Invalid schedule time."))
		if scheduled_at <= now:
			frappe.throw(_("Schedule time must be in the future."))
		if scheduled_at > add_to_date(now, days=MAX_SCHEDULE_DAYS_OUT):
			frappe.throw(_("Schedule time can be at most {0} days out.").format(MAX_SCHEDULE_DAYS_OUT))
	else:
		scheduled_at = now

	# Validate every app BEFORE inserting anything, so one dupe never leaves a
	# half-scheduled batch behind.
	for app in names:
		open_run = frappe.get_all(
			RUN,
			filters={"app": app, "status": ["in", list(app_analysis.NON_TERMINAL)]},
			fields=["name", "status"],
			limit=1,
		)
		if open_run:
			frappe.throw(_("App {0} already has a run in progress ({1}).").format(app, open_run[0].status))

	created: list[str] = []
	for app in names:
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"app": app,
				"status": "Queued",
				"scheduled_at": scheduled_at,
				"requested_by": frappe.session.user,
				"consent_at": now,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(doc.name)
	frappe.db.commit()

	if scheduled_at <= now:
		app_analysis._enqueue_tick()
	return {"ok": True, "data": {"runs": created, "scheduled_at": str(scheduled_at)}}


@frappe.whitelist()
@require_jarvis_user
def cancel_app_learning_run(name: str) -> dict:
	"""Cancel a Queued/Zipping/Analyzing run. An in-flight turn finishes, but
	the turn-end hook checks status before chaining, so nothing advances.
	Ingesting is past the point of no return (writes may have landed)."""
	_require_manage()
	doc = frappe.get_doc(RUN, name)
	if doc.status not in _CANCELLABLE:
		frappe.throw(
			_("Only a run that is Queued, Zipping or Analyzing can be cancelled (this one is {0}).").format(
				doc.status
			)
		)
	app_analysis.mark_cancelled(doc.name)
	return {"ok": True}


# --------------------------------------------------------------------------- #
# runs list (frozen envelope)
# --------------------------------------------------------------------------- #
def _order_by(sort_field: str, sort_dir: str) -> str:
	"""Whitelisted ORDER BY (unknown sort field throws — the triggers_api
	idiom)."""
	field = (sort_field or "").strip() or "creation"
	if field not in _RUN_SORTABLE:
		frappe.throw(_("Unknown sort field: {0}").format(field))
	d = "desc" if (sort_dir or "desc").lower() == "desc" else "asc"
	return f"`{_RUN_SORTABLE[field]}` {d}, `name` asc"


@frappe.whitelist()
@require_jarvis_user
def list_app_learning_runs_page(
	search: str = "",
	filters: str = "{}",
	sort_field: str = "creation",
	sort_dir: str = "desc",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""All learning runs (manage-gated, so no row scoping), server-side
	search/filter/sort/paginate. Search covers app + error; filters are
	whitelisted to {app, status}."""
	_require_manage()
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _RUN_FILTERS)

	conds = ["1=1"]
	params: dict = {"start": start, "page_length": pl}
	if search:
		params["q"] = f"%{_lk(search)}%"
		conds.append("(app LIKE %(q)s OR error LIKE %(q)s)")
	if "app" in f:
		params["app"] = str(f["app"])
		conds.append("app = %(app)s")
	if "status" in f:
		if f["status"] not in _STATUSES:
			frappe.throw(_("Invalid status filter."))
		params["status"] = f["status"]
		conds.append("status = %(status)s")

	where = " AND ".join(conds)
	order = _order_by(sort_field, sort_dir)

	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabJarvis App Learning Run` WHERE {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""SELECT {", ".join(_ROW_FIELDS)}
		FROM `tabJarvis App Learning Run`
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)
	for r in rows:
		_row_out(r)

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
