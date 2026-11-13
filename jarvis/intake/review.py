"""Document Intake: the reviewer step for strict mode.

When a mandatory value is missing and the rule does not allow incomplete drafts,
``raise_request`` puts ONE Jarvis Approval Request (source "Intake") on the Approval
Board. It carries the missing list (with field metadata for the form) and the values
already read, so approving needs no AI call and no re-read of the file.

``decide`` is the whole Intake branch of the board's decide endpoint: the reviewer's
answers are cleaned exactly like model values, checked against the mandatory list, and
only then handed to ``draft.create`` (as the uploader). A request that cannot be
completed stays Pending and tells the reviewer what is still needed.
"""

import copy
from pathlib import PurePosixPath

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarvis._session import impersonate
from jarvis.intake import draft
from jarvis.intake import spec as intake_spec

AR = "Jarvis Approval Request"
ACTIVITY = "Jarvis Trigger Activity"
TRIGGER = "Jarvis Trigger"
SOURCE = "Intake"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DECISION_CAP = 500
_NO_REVIEWER = ("Administrator", "Guest")


# --------------------------------------------------------------------------- #
# Raise
# --------------------------------------------------------------------------- #
def _key(field: str, row, table: str | None) -> str:
	"""The answer key: ``field`` for a header field, ``table.row.field`` for a cell."""
	return field if row is None else f"{table}.{row}.{field}"


def _describe(s, item: dict) -> dict:
	"""One missing entry plus what the review form needs to ask for it."""
	table = (s.table or {}).get("fieldname")
	meta = frappe.get_meta(s.target)
	if item["row"] is None:
		df = meta.get_field(item["field"])
		label = draft._label(meta, item["field"])
	else:
		child_meta = frappe.get_meta(intake_spec._child_doctype(s))
		df = child_meta.get_field(item["field"])
		label = draft._label(child_meta, item["field"])
	# A mandatory table with no rows read cannot be answered with one value.
	answerable = df is not None and df.fieldtype not in intake_spec._NON_VALUE_TYPES
	return {
		**item,
		"key": _key(item["field"], item["row"], table),
		"label": label,
		"fieldtype": df.fieldtype if df else "Data",
		"options": (df.options or "") if df else "",
		"answerable": answerable,
	}


def _file_of(activity) -> dict:
	if not activity.source_file:
		return {"file_name": "", "file_url": ""}
	row = frappe.db.get_value("File", activity.source_file, ["file_name", "file_url"], as_dict=True)
	return {"file_name": (row or {}).get("file_name") or "", "file_url": (row or {}).get("file_url") or ""}


def raise_request(activity, values: dict, missing: list[dict]) -> str:
	"""Create the review request for ``activity`` and return its name. Runs as the
	system (outside the uploader's session); the caller sets the row to Needs Review."""
	_trigger, s = draft._load_spec(activity)
	file_name = _file_of(activity)["file_name"]
	lines = [item["reason"] for item in missing]
	request = frappe.get_doc(
		{
			"doctype": AR,
			"title": f"Review {file_name or activity.name}"[:140],
			"status": "Pending",
			"source": SOURCE,
			"document_type": s.target,
			"question": "\n".join([_("These values are needed before the draft can be saved:"), *lines]),
			"ref_doctype": ACTIVITY,
			"ref_name": activity.name,
			"intake_activity": activity.name,
			"intake_missing": frappe.as_json(
				{"missing": [_describe(s, item) for item in missing], "values": values}, indent=None
			),
		}
	)
	# Server-managed source and fields: only this module may write them.
	request.flags.jarvis_server_write = True
	request.insert(ignore_permissions=True)
	_share_with_owner(request, activity)
	return request.name


def _trigger_owner(activity_name: str) -> str | None:
	trigger = frappe.db.get_value(ACTIVITY, activity_name, "trigger")
	return frappe.db.get_value(TRIGGER, trigger, "owner") if trigger else None


def _share_with_owner(request, activity) -> None:
	"""Read access for the rule owner, so the request shows on their board and badge
	and the private source file opens for them. Only shares this adds are recorded on
	the request, and ``_unshare`` takes exactly those back when it is settled. Best
	effort: the request exists either way."""
	owner = _trigger_owner(activity.name)
	if not owner or owner in _NO_REVIEWER:
		return
	added = []
	targets = [(AR, request.name)] + ([("File", activity.source_file)] if activity.source_file else [])
	for doctype, name in targets:
		try:
			if frappe.db.exists("DocShare", {"share_doctype": doctype, "share_name": name, "user": owner}):
				continue  # access it already had is not ours to remove
			if doctype == "File" and frappe.db.get_value("File", name, "owner") == owner:
				continue
			frappe.share.add(doctype, name, owner, read=1, flags={"ignore_share_permission": True})
			added.append([doctype, name, owner])
		except Exception:
			frappe.log_error(title="jarvis.intake.review.share_failed", message=frappe.get_traceback())
	if added:
		payload = _payload(request) | {"shares": added}
		frappe.db.set_value(AR, request.name, "intake_missing", frappe.as_json(payload, indent=None))


def _unshare(request) -> None:
	"""Take back the shares ``raise_request`` added. A failure is logged and never undoes
	the decision."""
	for doctype, name, user in _payload(request).get("shares") or []:
		try:
			share = frappe.db.get_value(
				"DocShare", {"share_doctype": doctype, "share_name": name, "user": user}
			)
			if share:
				# A system clean-up as Administrator: the deciding user holds no share right
				# on the file, and DocShare's own delete hook checks the session user.
				with impersonate("Administrator"):
					frappe.delete_doc("DocShare", share, ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(title="jarvis.intake.review.unshare_failed", message=frappe.get_traceback())


# --------------------------------------------------------------------------- #
# Read (what the board shows)
# --------------------------------------------------------------------------- #
def _payload(request) -> dict:
	data = frappe.parse_json(request.get("intake_missing") or "{}")
	return data if isinstance(data, dict) else {}


def may_decide(request, user: str | None = None) -> bool:
	"""System Manager (the board's own rule for requests without a chat) or the owner
	of the rule that raised it."""
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return True
	owner = _trigger_owner(request.get("intake_activity") or "")
	return bool(owner) and owner == user


def _show(value) -> str:
	return "" if value is None else str(value)


def view(request) -> dict:
	"""What the review form renders: the missing fields, the values already read (a
	header list and the table), and the source file."""
	payload = _payload(request)
	activity_name = request.get("intake_activity")
	activity = frappe.db.get_value(ACTIVITY, activity_name, ["trigger", "source_file"], as_dict=True)
	out = {"missing": payload.get("missing") or [], "read": [], "table": None, "file": None}
	if not activity:
		return out
	values = payload.get("values") or {}
	trigger = frappe.db.get_value(TRIGGER, activity.trigger, draft._TRIGGER_FIELDS, as_dict=True)
	if trigger:
		s = intake_spec.load(trigger)
		meta = frappe.get_meta(s.target)
		table = (s.table or {}).get("fieldname")
		out["read"] = [
			{"label": draft._label(meta, f), "value": _show(values.get(f))}
			for f in values
			if f != table and meta.get_field(f) and not draft._blank(values.get(f))
		]
		child = intake_spec._child_doctype(s)
		if table and child:
			child_meta = frappe.get_meta(child)
			columns = s.table.get("columns") or []
			out["table"] = {
				"label": draft._label(meta, table),
				"columns": [draft._label(child_meta, c) for c in columns],
				"rows": [[_show(row.get(c)) for c in columns] for row in values.get(table) or []],
			}
	file = _file_of(frappe._dict(source_file=activity.source_file))
	if file["file_url"]:
		suffix = PurePosixPath(file["file_name"] or file["file_url"]).suffix.lower()
		out["file"] = {**file, "is_image": suffix in _IMAGE_SUFFIXES}
	return out


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #
def decide(request, decision: str, approve: bool) -> dict:
	"""The Intake branch of ``approvals_api.decide``. ``decision`` is a JSON object
	``{"<field>" or "<table>.<row>.<field>": value}``; it is ignored on reject."""
	if not may_decide(request):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	# The row lock closes the double-decide race and keeps a refused approval
	# (still Pending) from touching anything.
	status = frappe.db.get_value(AR, request.name, "status", for_update=True)
	if status != "Pending":
		frappe.throw(_("Approval {0} is already {1}").format(request.name, status))
	answers = _answers(decision) if approve else {}
	result = apply_decision(request, approve, answers)
	request.reload()
	# Same as the generic decide: the badge refresh and the trace comment hang on on_update.
	request.run_method("on_update")
	return {"ok": True, "resumed": False, **result}


def apply_decision(request, approve: bool, answers: dict) -> dict:
	"""Approve (build the draft from the stored reading plus ``answers``, no AI call) or
	reject (the upload ends Failed). Returns ``{"status", ...}``. Callers check who may
	decide and hold the request's row lock (see ``decide``)."""
	activity_name = request.get("intake_activity")
	activity = frappe.get_doc(ACTIVITY, activity_name) if frappe.db.exists(ACTIVITY, activity_name) else None
	return _approve(request, activity, answers) if approve else _reject(request, activity)


def _settle(request, status: str, text: str) -> dict:
	"""Record the outcome with a plain DB write (status and decision are permlevel-1
	fields; the generic decide writes them the same way)."""
	_unshare(request)
	frappe.db.set_value(
		AR,
		request.name,
		{
			"status": status,
			"decision": text[:_DECISION_CAP],
			"decided_by": frappe.session.user,
			"decided_at": now_datetime(),
		},
	)
	return {"status": status}


def _reject(request, activity) -> dict:
	if activity is not None and activity.status == "Needs Review":
		trigger = frappe.db.get_value(TRIGGER, activity.trigger, draft._TRIGGER_FIELDS, as_dict=True)
		try:
			draft._fail(activity, trigger or frappe._dict(), _("Rejected by reviewer"))
		except draft.DraftFailed:
			pass  # the row is Failed and written back; nothing more to do
		draft._notify(activity.event_user, f"Rejected by reviewer: {request.title}"[:140])
	return _settle(request, "Rejected", "Rejected by reviewer")


def _existing_draft(activity) -> tuple[str, str] | None:
	if (
		activity.get("draft_doctype")
		and activity.get("draft_name")
		and frappe.db.exists(activity.draft_doctype, activity.draft_name)
	):
		return activity.draft_doctype, activity.draft_name
	return None


def _approve(request, activity, answers: dict) -> dict:
	if activity is None:
		frappe.throw(_("The intake record for this request no longer exists. Reject it instead."))
	made = _existing_draft(activity)
	if made:
		# Never a second draft: close the request over the one that exists, and finish
		# the upload the earlier run left in review.
		if activity.status == "Needs Review":
			activity.db_set({"status": "Success", "summary": f"Draft ready: {made[0]} {made[1]}"})
		return _settle(request, "Approved", f"Draft already exists: {made[0]} {made[1]}") | {"draft": made[1]}
	if activity.status != "Needs Review":
		frappe.throw(_("This upload is no longer waiting for a review."))
	payload = _payload(request)
	trigger, s = draft._load_spec(activity)
	try:
		uploader = draft._uploader(activity, trigger)
		values = _merge(s, payload, answers, uploader)
		doctype, name = draft.create(activity, values, allow_missing=False)
	except draft.DraftFailed:
		summary = frappe.db.get_value(ACTIVITY, activity.name, "summary") or ""
		# The reviewer did their part and the draft was refused: answered, not rejected.
		return _settle(request, "Answered", f"Draft not created: {summary}")
	return _settle(request, "Approved", _decision_text(payload, answers, doctype, name)) | {"draft": name}


def _answers(decision: str) -> dict:
	try:
		answers = frappe.parse_json(decision)
	except Exception:
		answers = None
	if not isinstance(answers, dict):
		frappe.throw(_("Send the answers as a JSON object."))
	return answers


def _decision_text(payload: dict, answers: dict, doctype: str, name: str) -> str:
	said = ", ".join(
		f"{m['label']}{', row ' + str(m['row']) if m['row'] else ''} = {answers[m['key']]}"
		for m in payload.get("missing") or []
		if m["key"] in answers
	)
	return f"Draft ready: {doctype} {name}" + (f". Answered: {said}" if said else "")


def _merge(s, payload: dict, answers: dict, uploader: str) -> dict:
	"""The stored values with the reviewer's answers in, cleaned like model values.
	Only keys that were listed as missing (and answerable) are accepted; anything else in
	``answers`` is ignored. Throws, naming the fields, when a mandatory value is still
	blank or invalid, before anything is written."""
	table = (s.table or {}).get("fieldname")
	meta = frappe.get_meta(s.target)
	child = intake_spec._child_doctype(s)
	child_meta = frappe.get_meta(child) if child else None
	values = copy.deepcopy(payload.get("values") or {})
	bad: dict = {}
	wanted = []  # (item, df) for each listed field that has an answer
	for item in payload.get("missing") or []:
		answer = answers.get(item["key"])
		if not item.get("answerable") or draft._blank(answer):
			continue
		if not isinstance(answer, str | int | float):  # bool is an int; dicts and lists are not values
			bad[(item["field"], item["row"])] = "unreadable"
			continue
		df = (meta if item["row"] is None else child_meta).get_field(item["field"])
		target = values if item["row"] is None else values[table][item["row"] - 1]
		target[item["field"]] = answer
		wanted.append((item, df, target))
	cleaned, problems = draft.normalise(s, values)
	why = {(p["field"], p["row"]): p["reason"] for p in problems} | bad
	with impersonate(uploader):
		memo: dict = {}
		for item, df, _target in wanted:
			holder = cleaned if item["row"] is None else cleaned[table][item["row"] - 1]
			value = holder.get(item["field"])
			if df.fieldtype == "Link" and not draft._blank(value):
				name, _close = draft._resolve_one(df.options, value, fuzzy=False, memo=memo)
				holder[item["field"]] = name
				if name is None:
					why[(item["field"], item["row"])] = "not found"
			elif (
				df.fieldtype == "Select"
				and not draft._blank(value)
				and value not in (df.options or "").split("\n")
			):
				holder[item["field"]] = None
				why[(item["field"], item["row"])] = "not an option"
	still = draft.missing_fields(s, cleaned, [])
	if still:
		frappe.throw(
			_("Still needed: {0}").format("; ".join(_still_text(m, meta, child_meta, why) for m in still))
		)
	return cleaned


def _still_text(item: dict, meta, child_meta, why: dict) -> str:
	label = draft._label(meta if item["row"] is None else child_meta, item["field"])
	where = f"{label}, row {item['row']}" if item["row"] else label
	return f"{where} ({why.get((item['field'], item['row']), 'blank')})"


__all__ = ["SOURCE", "apply_decision", "decide", "may_decide", "raise_request", "view"]
