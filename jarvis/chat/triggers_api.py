"""SPA-facing API for the Jarvis Triggers page.

Whitelisted endpoints for the ``/jarvis`` Triggers UI: caps probe, paginated
trigger list (frozen ``{rows, total, has_more, start, page_length}`` shape
inside the ``{ok, data}`` envelope), manage-gated CRUD (System Manager /
Jarvis Admin — plain jarvis users are read-only), a read-only condition dry
tester, and the visibility-filtered activity feed. Mirrors the shape of
``jarvis.chat.macros_api`` (whose list-page helpers it reuses).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, getdate, now_datetime
from frappe.utils.safe_exec import is_safe_exec_enabled

from jarvis.chat import list_filters
from jarvis.chat.macros_api import _bool01, _clamp_page, _lk, _load_filters
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_user
from jarvis.triggers.engine import (
	EVENT_LABELS,
	LLM_EVENTS,
	SUPPORTED_EVENTS,
	clear_cache,
	eval_context,
)

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"

# Fields a create/update payload may set. Everything else (owner, the managed
# server_script link, timestamps) is server-owned; unknown keys throw.
_ALLOWED_PAYLOAD_FIELDS = {
	"trigger_name",
	"enabled",
	"target_doctype",
	"doc_event",
	"condition",
	"action_type",
	"script_body",
	"llm_instruction",
	"llm_daily_cap",
	"description",
	"source_conversation",
}

_ACTION_TYPES = {"Script", "LLM"}
_ACTIVITY_STATUSES = {"Success", "Failed", "Blocked", "Skipped"}

_TRIGGER_FILTERS = {"enabled", "action_type", "target_doctype", "doc_event"}
_TRIGGER_SORTABLE = {
	"modified": "modified",
	"trigger_name": "trigger_name",
	"target_doctype": "target_doctype",
	"doc_event": "doc_event",
	"action_type": "action_type",
	"enabled": "enabled",
}

_ACTIVITY_FILTERS = {
	"trigger",
	"status",
	"action_type",
	"target_doctype",
	"doc_event",
	"from_date",
	"to_date",
}
_ACTIVITY_SORTABLE = {
	"creation": "creation",
	"status": "status",
	"target_doctype": "target_doctype",
}

# Non-admin activity visibility scan bounds (see list_activity_page): fetch in
# chunks of 5 pages worth, stop once the page is filled or this many raw rows
# were permission-checked.
_ACTIVITY_SCAN_CAP = 400

_ACTIVITY_FIELDS_SQL = """name, `trigger` AS `trigger`, trigger_label,
	target_doctype, target_docname, doc_event, action_type, status, summary,
	duration_ms, event_user, creation"""


# --------------------------------------------------------------------------- #
# shared guards / helpers
# --------------------------------------------------------------------------- #
def _can_manage(user: str | None = None) -> bool:
	"""System Manager / Jarvis Admin (Administrator implicit) may manage."""
	return has_jarvis_admin_access(user)


def _require_manage() -> None:
	if not _can_manage():
		frappe.throw(
			_("You need the Jarvis Admin or System Manager role to manage triggers."),
			frappe.PermissionError,
		)


def _order_by(sort_field: str, sort_dir: str, sortable: dict, default_field: str) -> str:
	"""Whitelisted ORDER BY (unknown sort field throws — stricter than the
	macros list, which silently falls back)."""
	field = (sort_field or "").strip() or default_field
	if field not in sortable:
		frappe.throw(_("Unknown sort field: {0}").format(field))
	d = "desc" if (sort_dir or "desc").lower() == "desc" else "asc"
	return f"`{sortable[field]}` {d}, `name` asc"


def _publish_changed() -> None:
	"""Best-effort realtime nudge to the actor's own open tabs."""
	try:
		from jarvis.chat.events import publish_to_user

		publish_to_user(frappe.session.user, {"kind": "trigger:changed"})
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# caps probe
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def get_triggers_caps() -> dict:
	"""Single gating probe for the Triggers page: manage tier, whether Script
	actions are even possible on this bench, STT (voice composer reuse), and
	the event pickers."""
	stt_enabled = False
	try:
		from jarvis.chat import voice

		stt_enabled = bool(voice.stt_config())
	except Exception:
		stt_enabled = False
	return {
		"ok": True,
		"data": {
			"can_manage": _can_manage(),
			"scripts_enabled": bool(is_safe_exec_enabled()),
			"stt_enabled": stt_enabled,
			"events": [{"value": e, "label": EVENT_LABELS[e]} for e in SUPPORTED_EVENTS],
			"llm_events": [e for e in SUPPORTED_EVENTS if e in LLM_EVENTS],
		},
	}


# --------------------------------------------------------------------------- #
# trigger list / detail
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
@list_filters.filter_errors_to_envelope
def list_triggers_page(
	search: str = "",
	filters: str = "{}",
	filters_v2: str | list | None = None,
	sort_field: str = "modified",
	sort_dir: str = "desc",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Org-wide triggers (read is not row-scoped), server-side search / filter
	/ sort / paginate, plus per-row activity stats (one grouped query for the
	page's ids). Frozen envelope in ``data``.

	``filters_v2`` (plan 08 §6.2) is ADDITIVE and validated against this
	caller's schema; legacy ``filters`` keeps working for the compatibility
	window.

	Scope invariant (MIGRATION-CHECKLIST §1): reads are org-wide here — every
	role that reaches this endpoint holds a permlevel-0 ``read`` on Jarvis
	Trigger with no ``if_owner``, and this query adds no row predicate, so the
	SQL scope EQUALS the ORM read scope.

	Withholding (D1-b): ``condition`` / ``script_body`` / ``llm_instruction``
	are the trigger's LOGIC and are redacted from non-managers by
	``_trigger_detail``. This list never selects them either — but omission from
	a SELECT is not withholding from a FILTER, and a ``like`` filter over them
	would rebuild the redacted logic one character at a time. The registry
	declares all three in ``excluded_fields``, so they are absent from the
	catalog and rejected by the compiler.
	"""
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _TRIGGER_FILTERS)

	q = list_filters.new_query("triggers")
	if search:
		q.server_condition(
			"(trigger_name LIKE %(q)s OR target_doctype LIKE %(q)s)", q=f"%{_lk(search)}%"
		)
	if "enabled" in f:
		q.server_condition("enabled = %(enabled)s", enabled=_bool01(f["enabled"]))
	if "action_type" in f:
		if f["action_type"] not in _ACTION_TYPES:
			frappe.throw(_("Invalid action_type filter."))
		q.server_condition("action_type = %(action_type)s", action_type=f["action_type"])
	if "target_doctype" in f:
		q.server_condition("target_doctype = %(target_doctype)s", target_doctype=str(f["target_doctype"]))
	if "doc_event" in f:
		if f["doc_event"] not in SUPPORTED_EVENTS:
			frappe.throw(_("Invalid doc_event filter."))
		q.server_condition("doc_event = %(doc_event)s", doc_event=f["doc_event"])

	q.apply(filters_v2)
	where = q.where()
	params = q.params({"start": start, "page_length": pl})
	order = _order_by(sort_field, sort_dir, _TRIGGER_SORTABLE, "modified")

	total = list_filters.bounded_sql(f"SELECT COUNT(*) FROM `tabJarvis Trigger` WHERE {where}", params)[0][0]
	rows = list_filters.bounded_sql(
		f"""SELECT name, trigger_name, enabled, target_doctype, doc_event,
		action_type, description, modified, owner
		FROM `tabJarvis Trigger`
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	names = [r.name for r in rows]
	stats: dict = {}
	if names:
		since = add_to_date(now_datetime(), hours=-24)
		for x in frappe.db.sql(
			"""SELECT `trigger`, MAX(creation) AS last_activity_at,
			SUM(CASE WHEN creation >= %(since)s THEN 1 ELSE 0 END) AS activity_24h
			FROM `tabJarvis Trigger Activity`
			WHERE `trigger` IN %(names)s GROUP BY `trigger`""",
			{"names": tuple(names), "since": since},
			as_dict=True,
		):
			stats[x["trigger"]] = x
	for r in rows:
		s = stats.get(r.name)
		r["last_activity_at"] = str(s["last_activity_at"]) if s and s["last_activity_at"] else ""
		r["activity_24h"] = cint(s["activity_24h"]) if s else 0
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


def _trigger_detail(doc, *, can_manage: bool = True) -> dict:
	"""Full trigger detail. ``condition``/``script_body``/``llm_instruction``
	are the trigger's LOGIC and can embed internal business rules (or a value an
	admin surfaces via a script); core keeps Server Script bodies manager-only,
	so for non-managers (plain Jarvis Users, who get org-wide READ) these three
	fields are redacted to a marker. Managers see everything."""
	detail = {
		"name": doc.name,
		"trigger_name": doc.trigger_name,
		"enabled": cint(doc.enabled),
		"target_doctype": doc.target_doctype,
		"doc_event": doc.doc_event,
		"action_type": doc.action_type,
		"server_script": doc.server_script or "",
		"llm_daily_cap": cint(doc.llm_daily_cap),
		"description": doc.description or "",
		"source_conversation": doc.source_conversation or "",
		"owner": doc.owner,
		"modified": str(doc.modified),
		"can_manage": bool(can_manage),
	}
	if can_manage:
		detail["condition"] = doc.condition or ""
		detail["script_body"] = doc.script_body or ""
		detail["llm_instruction"] = doc.llm_instruction or ""
	else:
		redacted = None  # the SPA renders "—" / a locked note for non-managers
		detail["condition"] = redacted
		detail["script_body"] = redacted
		detail["llm_instruction"] = redacted
	return detail


@frappe.whitelist()
@require_jarvis_user
def get_trigger(name: str) -> dict:
	"""Full detail of one trigger (read is org-wide for jarvis users; the
	logic fields are redacted for non-managers — see _trigger_detail)."""
	doc = frappe.get_doc(TRIGGER, name)
	doc.check_permission("read")
	return {"ok": True, "data": _trigger_detail(doc, can_manage=_can_manage())}


# --------------------------------------------------------------------------- #
# manage (System Manager / Jarvis Admin)
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


@frappe.whitelist()
@require_jarvis_user
def create_trigger(payload: str) -> dict:
	"""Create a trigger from a whitelisted-fields JSON payload. Validation
	(denylist, event/action compatibility, condition, script compile, caps)
	runs in the doctype validate(); the controller materializes the managed
	Server Script and busts the engine cache."""
	_require_manage()
	fields = _parse_payload(payload)
	doc = frappe.get_doc({"doctype": TRIGGER, **fields})
	doc.insert()
	frappe.db.commit()
	return {"ok": True, "data": _trigger_detail(doc)}


@frappe.whitelist()
@require_jarvis_user
def update_trigger(name: str, payload: str) -> dict:
	"""Update whitelisted fields of a trigger (manage-gated)."""
	_require_manage()
	fields = _parse_payload(payload)
	doc = frappe.get_doc(TRIGGER, name)
	doc.update(fields)
	doc.save()
	frappe.db.commit()
	return {"ok": True, "data": _trigger_detail(doc)}


@frappe.whitelist()
@require_jarvis_user
def set_trigger_enabled(name: str, enabled: int) -> dict:
	"""Quick enable/disable toggle (manage-gated): raw db_set — no revalidation
	of an unchanged trigger — plus the engine-cache bust the controller would
	have done."""
	_require_manage()
	value = _bool01(enabled)
	doc = frappe.get_doc(TRIGGER, name)
	doc.db_set("enabled", value)
	clear_cache()
	_publish_changed()
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "enabled": value}}


@frappe.whitelist()
@require_jarvis_user
def delete_trigger(name: str) -> dict:
	"""Delete one trigger (manage-gated). on_trash removes the managed Server
	Script and busts the cache; activity rows survive (Data snapshot, not a
	Link)."""
	_require_manage()
	frappe.delete_doc(TRIGGER, name)
	frappe.db.commit()
	return {"ok": True, "data": {"deleted": 1}}


@frappe.whitelist()
@require_jarvis_user
def delete_triggers_bulk(names: str) -> dict:
	"""Bulk delete (manage-gated, cap 50). Per-row try/except so one bad row
	never aborts the batch; internal errors are logged, never leaked."""
	_require_manage()
	raw = frappe.parse_json(names) if isinstance(names, str) else (names or [])
	items = [str(n) for n in raw if n] if isinstance(raw, list) else []
	if len(items) > 50:
		frappe.throw(_("At most 50 triggers can be deleted at once."))
	deleted = 0
	skipped: list[dict] = []
	for n in items:
		try:
			# frappe.delete_doc defaults ignore_missing=True (silent no-op), so
			# a missing row must be detected explicitly to be reported.
			if not frappe.db.exists(TRIGGER, n):
				skipped.append({"name": n, "reason": "not found"})
				continue
			frappe.delete_doc(TRIGGER, n)
			deleted += 1
		except frappe.DoesNotExistError:
			skipped.append({"name": n, "reason": "not found"})
		except Exception:
			frappe.log_error(title="Jarvis: bulk trigger delete failed", message=frappe.get_traceback())
			skipped.append({"name": n, "reason": "error"})
	frappe.db.commit()
	return {"ok": True, "data": {"deleted": deleted, "skipped": skipped}}


# --------------------------------------------------------------------------- #
# condition dry test
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def test_trigger_condition(target_doctype: str, condition: str = "", docname: str = "") -> dict:
	"""READ-ONLY condition tester. Without ``docname``: compile/eval against an
	empty doc (webhook-style validation). With ``docname`` (caller needs read
	on that doc): also report whether the trigger WOULD fire. A bad expression
	is a payload (``{valid: False, error}``), never a 500."""
	target_doctype = (target_doctype or "").strip()
	if not target_doctype or not frappe.db.exists("DocType", target_doctype):
		frappe.throw(_("Unknown DocType: {0}").format(target_doctype))
	condition = (condition or "").strip()
	docname = (docname or "").strip()

	if docname:
		if not frappe.has_permission(target_doctype, "read", doc=docname):
			frappe.throw(
				_("You do not have read access to {0} {1}.").format(target_doctype, docname),
				frappe.PermissionError,
			)
		doc = frappe.get_doc(target_doctype, docname)
	else:
		doc = frappe.new_doc(target_doctype)

	if not condition:
		data: dict = {"valid": True}
		if docname:
			data["would_fire"] = True  # no condition = always fires
		return {"ok": True, "data": data}

	try:
		result = frappe.safe_eval(condition, eval_locals=eval_context(doc))
	except TypeError as e:
		# On a BLANK doc (no docname) a TypeError is almost always a numeric
		# field that is None on an empty doc (doc.grand_total > 100000) — the
		# condition is fine and will save; a real fired doc has the value. Mirror
		# the controller's blank-doc tolerance. On a REAL doc it is a genuine
		# evaluation error, so report it.
		if not docname:
			return {
				"ok": True,
				"data": {
					"valid": True,
					"note": (
						"Looks good. (Can't fully test on an empty document because a "
						"field is blank; it will evaluate against real documents.)"
					),
				},
			}
		return {"ok": True, "data": {"valid": False, "error": _friendly_condition_error(e, target_doctype)}}
	except Exception as e:
		return {"ok": True, "data": {"valid": False, "error": _friendly_condition_error(e, target_doctype)}}
	data = {"valid": True}
	if docname:
		data["would_fire"] = bool(result)
	return {"ok": True, "data": data}


def _friendly_condition_error(e: Exception, target_doctype: str) -> str:
	"""Rewrite raw Python exceptions into a one-line, business-readable hint
	(the raw str carries CPython artifacts like ``<unknown>`` a user can't act
	on)."""
	if isinstance(e, SyntaxError):
		return _("The condition has a syntax error — check quotes, brackets and operators.")
	if isinstance(e, NameError):
		return _(
			"Only 'doc' and 'utils' can be used in a condition "
			'(e.g. doc.status == "Open", utils.nowdate()). {0}'
		).format(str(e))
	if isinstance(e, AttributeError):
		return _("That field does not exist on {0}: {1}").format(target_doctype, str(e))
	return _("The condition could not be evaluated: {0}").format(str(e))


# --------------------------------------------------------------------------- #
# activity feed
# --------------------------------------------------------------------------- #
def _date_bound(value, end: bool) -> str:
	"""Validate a from/to date filter and widen it to the day boundary."""
	s = str(value or "").strip()
	try:
		d = getdate(s)
	except Exception:
		d = None
	if not d:
		frappe.throw(_("Invalid date filter: {0}").format(s))
	return f"{d} 23:59:59.999999" if end else f"{d} 00:00:00"


def _activity_where(search: str, f: dict) -> tuple[str, dict]:
	conds = ["1=1"]
	params: dict = {}
	if search:
		params["q"] = f"%{_lk(search)}%"
		conds.append("(trigger_label LIKE %(q)s OR target_docname LIKE %(q)s OR summary LIKE %(q)s)")
	if "trigger" in f:
		params["trigger"] = str(f["trigger"])
		conds.append("`trigger` = %(trigger)s")
	if "status" in f:
		if f["status"] not in _ACTIVITY_STATUSES:
			frappe.throw(_("Invalid status filter."))
		params["status"] = f["status"]
		conds.append("status = %(status)s")
	if "action_type" in f:
		if f["action_type"] not in _ACTION_TYPES:
			frappe.throw(_("Invalid action_type filter."))
		params["action_type"] = f["action_type"]
		conds.append("action_type = %(action_type)s")
	if "target_doctype" in f:
		params["target_doctype"] = str(f["target_doctype"])
		conds.append("target_doctype = %(target_doctype)s")
	if "doc_event" in f:
		if f["doc_event"] not in SUPPORTED_EVENTS:
			frappe.throw(_("Invalid doc_event filter."))
		params["doc_event"] = f["doc_event"]
		conds.append("doc_event = %(doc_event)s")
	if "from_date" in f:
		params["from_date"] = _date_bound(f["from_date"], end=False)
		conds.append("creation >= %(from_date)s")
	if "to_date" in f:
		params["to_date"] = _date_bound(f["to_date"], end=True)
		conds.append("creation <= %(to_date)s")
	return " AND ".join(conds), params


def _can_read_target(row) -> bool:
	"""Visibility axis for non-admins: read access on the row's target doc.
	Any error (deleted doc, broken meta) hides the row."""
	dt = row.get("target_doctype")
	dn = row.get("target_docname")
	if not dt:
		return False
	try:
		if dn:
			return bool(frappe.has_permission(dt, "read", doc=dn))
		return bool(frappe.has_permission(dt, "read"))
	except Exception:
		return False


def _clear_perm_message_noise() -> None:
	"""Drop any messages ``frappe.has_permission`` pushed onto the message log
	during the visibility scan (one "User X does not have doctype access …" per
	denied doctype/row), so they never leak back to the client in
	``_server_messages``. Best-effort."""
	try:
		frappe.local.message_log = []
	except Exception:
		pass


def _readable_target_doctypes(where: str, params: dict) -> list:
	"""The distinct target doctypes in the filtered activity that the caller
	has ANY doctype-level read on. Lets the non-admin scan exclude, in SQL,
	whole doctypes the user can't read at all (the common case — a trigger on a
	doctype they have no role for) instead of paying a per-row permission check
	for each of those rows. target_doctype is indexed, and the distinct set is
	tiny, so this is cheap."""
	dts = frappe.db.sql(
		f"SELECT DISTINCT target_doctype FROM `tabJarvis Trigger Activity` WHERE {where}",
		params,
	)
	out = []
	for (dt,) in dts:
		if not dt:
			continue
		try:
			if frappe.has_permission(dt, "read"):
				out.append(dt)
		except Exception:
			continue
	return out


@frappe.whitelist()
@require_jarvis_user
def list_activity_page(
	search: str = "",
	filters: str = "{}",
	sort_field: str = "creation",
	sort_dir: str = "desc",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Activity feed. System Manager / Jarvis Admin see everything (exact
	total). Everyone else sees a row iff they can READ its target doc —
	enforced by scanning raw rows in chunks (up to 5 pages worth per fetch,
	hard cap ``_ACTIVITY_SCAN_CAP`` permission checks) — so ``total`` is the
	filtered count found so far and ``approximate`` is True (the UI shows it
	as such)."""
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _ACTIVITY_FILTERS)
	where, params = _activity_where(search, f)
	order = _order_by(sort_field, sort_dir, _ACTIVITY_SORTABLE, "creation")

	if _can_manage():
		total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabJarvis Trigger Activity` WHERE {where}", params)[0][
			0
		]
		rows = frappe.db.sql(
			f"""SELECT {_ACTIVITY_FIELDS_SQL} FROM `tabJarvis Trigger Activity`
			WHERE {where} ORDER BY {order}
			LIMIT %(page_length)s OFFSET %(start)s""",
			{**params, "page_length": pl, "start": start},
			as_dict=True,
		)
		for r in rows:
			r["creation"] = str(r["creation"])
		return {
			"ok": True,
			"data": {
				"rows": rows,
				"total": total,
				"has_more": start + len(rows) < total,
				"start": start,
				"page_length": pl,
				"approximate": False,
			},
		}

	# Non-admin: first exclude — in SQL — every target doctype the user has no
	# read on at all (the bulk of a burst is usually one such doctype), so the
	# per-row record-level scan only runs over plausibly-visible rows. If NOTHING
	# is readable, return an empty page immediately (no scan, no ~1s spin).
	readable_dts = _readable_target_doctypes(where, params)
	if not readable_dts:
		_clear_perm_message_noise()  # drop the has_permission "no access" noise
		return {
			"ok": True,
			"data": {
				"rows": [],
				"total": 0,
				"has_more": False,
				"start": start,
				"page_length": pl,
				"approximate": True,
			},
		}
	dt_ph = ", ".join([f"%(rdt{i})s" for i in range(len(readable_dts))])
	scan_where = f"({where}) AND target_doctype IN ({dt_ph})"
	scan_params = {**params, **{f"rdt{i}": dt for i, dt in enumerate(readable_dts)}}

	# `needed` includes one overflow row so a full page can still report
	# has_more without another scan.
	needed = start + pl + 1
	chunk = pl * 5
	visible: list = []
	scanned = 0
	offset = 0
	exhausted = False
	while True:
		batch = frappe.db.sql(
			f"""SELECT {_ACTIVITY_FIELDS_SQL} FROM `tabJarvis Trigger Activity`
			WHERE {scan_where} ORDER BY {order}
			LIMIT %(chunk)s OFFSET %(offset)s""",
			{**scan_params, "chunk": chunk, "offset": offset},
			as_dict=True,
		)
		if not batch:
			exhausted = True
			break
		for r in batch:
			scanned += 1
			if _can_read_target(r):
				visible.append(r)
			if len(visible) >= needed or scanned >= _ACTIVITY_SCAN_CAP:
				break
		if len(visible) >= needed or scanned >= _ACTIVITY_SCAN_CAP:
			break
		if len(batch) < chunk:
			exhausted = True
			break
		offset += chunk

	# has_permission pushes "no access" lines into the message log on every
	# denied row; clear them so they never ride back in _server_messages.
	_clear_perm_message_noise()
	hit_cap = scanned >= _ACTIVITY_SCAN_CAP and not exhausted
	rows = visible[start : start + pl]
	for r in rows:
		r["creation"] = str(r["creation"])
	return {
		"ok": True,
		"data": {
			"rows": rows,
			"total": len(visible),
			"has_more": (len(visible) > start + pl) or hit_cap,
			"start": start,
			"page_length": pl,
			"approximate": True,
		},
	}


@frappe.whitelist()
@require_jarvis_user
def activity_stats() -> dict:
	"""Admin summary tiles: per-status counts over the last 24h + total rows.
	Non-admins get ``{}`` (the UI hides the tiles)."""
	if not _can_manage():
		return {"ok": True, "data": {}}
	since = add_to_date(now_datetime(), hours=-24)
	rows = frappe.db.sql(
		"""SELECT status, COUNT(*) AS n FROM `tabJarvis Trigger Activity`
		WHERE creation >= %(since)s GROUP BY status""",
		{"since": since},
		as_dict=True,
	)
	by = {r.status: cint(r.n) for r in rows}
	return {
		"ok": True,
		"data": {
			"last_24h": {s: by.get(s, 0) for s in ("Success", "Failed", "Blocked", "Skipped")},
			"total_rows": frappe.db.count(ACTIVITY),
		},
	}
