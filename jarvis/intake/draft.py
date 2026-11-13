"""Document Intake: match the reading to the ERP, then draft (or ask a reviewer).

``finish`` is the last stage of an intake job: merge the trigger's defaults under the
model's reading, resolve Link values, then either create the draft or raise a review
request. Everything that reads ERP data or writes the draft runs as the uploader (never
the rule owner, never Administrator), so the draft holds only values the uploader may
see and exists only if the uploader may create it.

Expected outcomes set the row's final status here and return; unexpected errors raise
and the worker turns them into a Failed row.
"""

import difflib
import math
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime, getdate

from jarvis._session import impersonate
from jarvis.chat.filebox import attach_file_to_doc
from jarvis.intake import spec as intake_spec

ACTIVITY = "Jarvis Trigger Activity"
TRIGGER = "Jarvis Trigger"
_CANDIDATES = 3
# Close names are looked up among the newest records only: bounded work per unresolved value.
_CANDIDATE_POOL = 200
_TRIGGER_FIELDS = [
	"intake_source",
	"intake_record_doctype",
	"intake_target_doctype",
	"intake_spec",
	"intake_instructions",
	"intake_status_field",
	"intake_link_field",
	"allow_incomplete_drafts",
]


class DraftFailed(Exception):
	"""The row has been set to Failed with a safe summary; nothing more to do."""


def _blank(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def _escape_like(value: str) -> str:
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- #
# Merge and match
# --------------------------------------------------------------------------- #
def merge_values(s, reading: dict) -> dict:
	"""Header values and child rows from the reading, limited to what the spec asked for
	(the model cannot set any other field), with ``defaults`` under them: a read value wins
	only when it is not blank."""
	values = {}
	for fieldname in s.fields:
		values[fieldname] = reading.get(fieldname)
	for fieldname, default in s.defaults.items():
		if _blank(values.get(fieldname)):
			values[fieldname] = default
	if s.table:
		columns = s.table.get("columns") or []
		rows = reading.get(s.table["fieldname"])
		values[s.table["fieldname"]] = [
			{c: row.get(c) for c in columns}
			for row in (rows if isinstance(rows, list) else [])
			if isinstance(row, dict)
		]
	return values


def _readable_names(doctype: str, filters=None, or_filters=None, fields=None, limit=None) -> list[dict]:
	"""``frappe.get_list`` as the current (uploader) session. No read permission on the
	doctype counts as no records: the value stays unresolved and nothing leaks."""
	try:
		return frappe.get_list(
			doctype,
			filters=filters,
			or_filters=or_filters,
			fields=fields or ["name"],
			limit_page_length=limit or 0,
		)
	except frappe.PermissionError:
		return []


def _find_exact(doctype: str, value: str) -> str | None:
	rows = _readable_names(doctype, filters={"name": value}, limit=1)
	return rows[0]["name"] if rows else None


def _find_close_match(doctype: str, value: str) -> str | None:
	"""One record whose name or title equals ``value`` ignoring case; None when there
	are none or several (a guess between two is worse than a blank)."""
	title_field = frappe.get_meta(doctype).title_field
	fields = ["name"] + ([title_field] if title_field and title_field != "name" else [])
	pattern = _escape_like(value)
	or_filters = [[doctype, f, "like", pattern] for f in fields]
	wanted = value.casefold()
	# The LIKE only narrows; the exact comparison below is the real test.
	hits = {
		r["name"]
		for r in _readable_names(doctype, or_filters=or_filters, fields=fields, limit=20)
		if any(str(r.get(f) or "").casefold() == wanted for f in fields)
	}
	return hits.pop() if len(hits) == 1 else None


def _candidates(doctype: str, value: str) -> list[str]:
	rows = _readable_names(doctype, limit=_CANDIDATE_POOL)
	by_fold = {r["name"].casefold(): r["name"] for r in rows}
	close = difflib.get_close_matches(value.casefold(), list(by_fold), n=_CANDIDATES, cutoff=0.6)
	return [by_fold[c] for c in close]


def _resolve_one(doctype: str, value, *, fuzzy: bool, memo: dict) -> tuple[str | None, list[str]]:
	"""(resolved name or None, candidates). Always as the current session user. ``memo``
	lives for one ``resolve_links`` call: a sheet repeating the same value queries once."""
	if _blank(value):
		return None, []
	text = str(value).strip()
	key = (doctype, text, fuzzy)
	if key not in memo:
		memo[key] = _lookup(doctype, text, fuzzy)
	return memo[key]


def _lookup(doctype: str, text: str, fuzzy: bool) -> tuple[str | None, list[str]]:
	name = _find_exact(doctype, text)
	# The database compares names case-insensitively; a field that is not a match field
	# keeps only a value that is the record's name exactly, never a re-cased guess.
	if name and not fuzzy and name != text:
		name = None
	if not name and fuzzy:
		name = _find_close_match(doctype, text)
	return (name, []) if name else (None, _candidates(doctype, text))


_NUMBER_TYPES = {"Int", "Float", "Currency", "Percent"}
_CURRENCY_END = re.compile(
	r"(?i)^[\s\u20b9$\u20ac\u00a3\u00a5]*(?:(?:inr|usd|eur|gbp|rs)\b\.?\s*)?|"
	r"\s*(?:\b(?:inr|usd|eur|gbp|rs)\.?|[\u20b9$\u20ac\u00a3\u00a5])?\s*$"
)
_DATE_TOKENS = {"yyyy": "%Y", "dd": "%d", "mm": "%m"}


def _site_formats() -> tuple[str, str]:
	"""(number format, date format) from System Settings, with Frappe's defaults."""
	return (
		frappe.get_system_settings("number_format") or "#,###.##",
		frappe.get_system_settings("date_format") or "yyyy-mm-dd",
	)


def _number_pattern(number_format: str) -> re.Pattern:
	"""Regex for a number written in the site's format: optional sign, digits grouped the
	way the format groups them (3-3-3, or 2-2-3 for the Indian format), at most one
	decimal separator that is the format's own. Anything else is not a number."""
	from frappe.utils.number_format import NumberFormat

	info = NumberFormat.from_string(number_format)
	group, decimal = re.escape(info.thousands_separator), re.escape(info.decimal_separator)
	if number_format == "#,##,###.##":
		whole = rf"\d{{1,2}}(?:{group}\d{{2}})*{group}\d{{3}}|\d+"
	else:
		whole = rf"\d{{1,3}}(?:{group}\d{{3}})+|\d+"
	# "#.###" and "#,###" have no decimal separator (it is ""): integers only, or an
	# over-long group such as "1.2345" would read as 1234.5.
	fraction = rf"(?:{decimal}(\d+))?" if info.decimal_separator else "()"
	return re.compile(rf"([+-]?)({whole}){fraction}")


def _number(value, fieldtype: str, number_format: str):
	"""A float/int, or None when ``value`` is not a plain number in the site's format.
	Frappe would turn "N/A" into 0 (and a wrong separator into a wrong value), so anything
	unparseable must stay blank."""
	if isinstance(value, bool) or not isinstance(value, str | int | float):
		return None
	if isinstance(value, str):
		text = value.replace("\u00a0", " ").replace("\u202f", " ")
		text = text.strip()
		sign = text[0] if text[:1] in ("+", "-") else ""
		match = _number_pattern(number_format).fullmatch(sign + _CURRENCY_END.sub("", text[len(sign) :]))
		if not match:
			return None
		from frappe.utils.number_format import NumberFormat

		info = NumberFormat.from_string(number_format)
		digits = match.group(2).replace(info.thousands_separator, "")
		number = float(f"{match.group(1)}{digits}.{match.group(3) or 0}")
	else:
		number = float(value)
	if not math.isfinite(number):
		return None
	if fieldtype == "Int" and number != int(number):
		return None
	return int(number) if number == int(number) else number


def _date_format_string(date_format: str) -> str:
	out = date_format.lower()
	for token, code in _DATE_TOKENS.items():
		out = out.replace(token, code)
	return out


def _date(value, fieldtype: str, date_format: str) -> str | None:
	"""ISO text as is; any other text only in the site's own date format. A guess
	between day-first and month-first would silently store the wrong date."""
	if not isinstance(value, str):
		return None
	text = value.strip()
	if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", text):
		try:
			return str(getdate(text) if fieldtype == "Date" else get_datetime(text))
		except Exception:
			return None
	fmt = _date_format_string(date_format)
	candidates = [fmt] if fieldtype == "Date" else [f"{fmt} %H:%M:%S", f"{fmt} %H:%M", fmt]
	for candidate in candidates:
		try:
			parsed = datetime.strptime(text, candidate)
		except ValueError:
			continue
		return str(parsed.date() if fieldtype == "Date" else parsed)
	return None


def _clean(df, value):
	"""(cleaned value, reason or None). A reason means the value must stay blank."""
	if _blank(value):
		return None, None
	if isinstance(value, list | dict):
		return None, "unreadable"
	if df.fieldtype in _NUMBER_TYPES:
		number = _number(value, df.fieldtype, _site_formats()[0])
		return (number, None) if number is not None else (None, "not a number")
	if df.fieldtype in ("Date", "Datetime"):
		parsed = _date(value, df.fieldtype, _site_formats()[1])
		return (parsed, None) if parsed else (None, "not a date")
	return value, None


def normalise(s, values: dict) -> tuple[dict, list[dict]]:
	"""Header values and child cells made safe to insert: numbers parsed (thousands
	separators and currency signs dropped), dates parsed, lists/dicts refused. Anything
	unusable becomes blank and is listed as ``{field, row, value, candidates, reason}``."""
	meta = frappe.get_meta(s.target)
	table = (s.table or {}).get("fieldname")
	out, problems = dict(values), []

	def clean(df, value, row):
		cleaned, reason = _clean(df, value)
		if reason:
			problems.append(
				{"field": df.fieldname, "row": row, "value": value, "candidates": [], "reason": reason}
			)
		return cleaned

	for fieldname, value in values.items():
		df = meta.get_field(fieldname)
		if fieldname != table and df:
			out[fieldname] = clean(df, value, None)
	child = intake_spec._child_doctype(s)
	if table and child:
		child_meta = frappe.get_meta(child)
		out[table] = [
			{
				k: clean(child_meta.get_field(k), v, i) if child_meta.get_field(k) else v
				for k, v in row.items()
			}
			for i, row in enumerate(values.get(table) or [], start=1)
		]
	return out, problems


def resolve_links(s, values: dict) -> tuple[dict, list[dict]]:
	"""Resolve Link values as the current session user (call inside ``impersonate``).

	Fields in ``s.match``: exact name, else ONE case-insensitive match on the name or
	the title field. Other Link fields (header or child column): only an exact name.
	Values that are not plain numbers, dates or scalars are blanked first (see
	``normalise``). Anything not resolved is blanked and listed as
	``{field, row, value, candidates}`` (``reason`` too for a normalise entry); ``row`` is None for a header field and the
	1-based row number for a child column. Returns ``(new values, unresolved)``."""
	meta = frappe.get_meta(s.target)
	out, unresolved = normalise(s, values)
	memo: dict = {}

	def resolve(df, value, row):
		name, candidates = _resolve_one(
			df.options, value, fuzzy=df.fieldname in s.match and row is None, memo=memo
		)
		if name is None and not _blank(value):
			unresolved.append({"field": df.fieldname, "row": row, "value": value, "candidates": candidates})
		return name

	for df in meta.fields:
		if df.fieldtype == "Link" and df.fieldname in out and not _blank(out[df.fieldname]):
			out[df.fieldname] = resolve(df, out[df.fieldname], None)

	table = (s.table or {}).get("fieldname")
	child = intake_spec._child_doctype(s)
	if table and child:
		child_meta = frappe.get_meta(child)
		rows = []
		for i, row in enumerate(out.get(table) or [], start=1):
			row = dict(row)
			for df in child_meta.fields:
				if df.fieldtype == "Link" and not _blank(row.get(df.fieldname)):
					row[df.fieldname] = resolve(df, row[df.fieldname], i)
			rows.append(row)
		out[table] = rows
	return out, unresolved


# --------------------------------------------------------------------------- #
# What is still missing
# --------------------------------------------------------------------------- #
def _label(meta, fieldname: str) -> str:
	df = meta.get_field(fieldname)
	return (df.label if df and df.label else fieldname).strip()


def missing_fields(s, values: dict, unresolved: list[dict]) -> list[dict]:
	"""Mandatory header fields and mandatory child columns (per row) that are blank,
	each ``{field, row, value, candidates, reason}``. Unresolved links were blanked, so
	they appear here through the same blank check, with the reason from ``unresolved``."""
	header, columns = intake_spec.mandatory_fields(s)
	meta = frappe.get_meta(s.target)
	out = []

	def add(fieldname, row, label):
		hit = next((u for u in unresolved if u["field"] == fieldname and u["row"] == row), None)
		out.append(
			{
				"field": fieldname,
				"row": row,
				"value": hit["value"] if hit else None,
				"candidates": hit["candidates"] if hit else [],
				"reason": _issue_text(label, row, hit),
			}
		)

	for fieldname in header:
		if _blank(values.get(fieldname)):
			add(fieldname, None, _label(meta, fieldname))
	table = (s.table or {}).get("fieldname")
	child = intake_spec._child_doctype(s)
	if table and child:
		child_meta = frappe.get_meta(child)
		rows = values.get(table) or []
		if not rows and meta.get_field(table).reqd:
			out.append(
				{
					"field": table,
					"row": None,
					"value": None,
					"candidates": [],
					"reason": f"{_label(meta, table)}: no rows read",
				}
			)
		for i, row in enumerate(rows, start=1):
			for fieldname in columns:
				if _blank(row.get(fieldname)):
					add(fieldname, i, _label(child_meta, fieldname))
	return out


def _issue_text(label: str, row, hit: dict | None) -> str:
	"""One line for a reviewer or the comment: 'Qty, row 3: unreadable',
	'Item BRK-2299: not found, closest BRK-2290'."""
	if hit and hit.get("reason"):
		return f"{label}, row {row}: {hit['reason']}" if row else f"{label}: {hit['reason']}"
	if hit:
		close = ", ".join(hit["candidates"])
		where = f", row {row}" if row else ""
		return f"{label} {hit['value']}{where}: not found" + (f", closest {close}" if close else "")
	return f"{label}, row {row}: unreadable" if row else f"{label}: unreadable"


# --------------------------------------------------------------------------- #
# finish and create
# --------------------------------------------------------------------------- #
def _fail(row, trigger, summary: str, detail: str = ""):
	"""End the row Failed (``detail`` is a short reason, never raw exception text), mark the
	source record's status field Failed too, and stop the caller."""
	row.db_set({"status": "Failed", "summary": summary, "detail": detail[:140]})
	_guarded(_write_back, row, trigger, "Failed", None)
	raise DraftFailed(summary)


def _load_spec(row):
	trigger = frappe.db.get_value(TRIGGER, row.trigger, _TRIGGER_FIELDS, as_dict=True) or frappe._dict()
	return trigger, intake_spec.load(trigger)


def _uploader(row, trigger) -> str:
	from jarvis.permissions import is_valid_unattended_owner

	user = row.event_user
	# Checked again here: a review approval can run days after the worker's own check.
	if not is_valid_unattended_owner(user):
		_fail(row, trigger, _("The uploader can't be used for unattended work."))
	return user


def finish(activity, reading: dict) -> None:
	"""Turn the model's reading into a draft or a review request and end the row.

	``reading`` may hold the reserved ``_note`` (for example "Read 10 of 14 pages"); it is
	popped from a copy and appended to the final summary so the uploader sees it."""
	reading = dict(reading)
	note = reading.pop("_note", "") or ""
	try:
		_finish(activity, reading, note)
	except DraftFailed:
		return


def _finish(row, reading: dict, note: str) -> None:
	trigger, s = _load_spec(row)
	uploader = _uploader(row, trigger)
	with impersonate(uploader):
		values, unresolved = resolve_links(s, merge_values(s, reading))
		missing = missing_fields(s, values, unresolved)
	if missing and not s.allow_incomplete:
		_review(row, trigger, s, values, missing, note)
		return
	create(row, values, allow_missing=bool(missing), unresolved=unresolved, note=note)


def _with_note(summary: str, note: str) -> str:
	return f"{summary}. {note}" if note else summary


def _review(row, trigger, s, values: dict, missing: list[dict], note: str) -> None:
	from jarvis.intake import review  # Task 8; imported late so tests can patch it

	approval = review.raise_request(row, values, missing)
	count = len(missing)
	summary = _with_note(f"Waiting for a reviewer: {count} field(s) need a value", note)
	row.db_set({"status": "Needs Review", "approval_request": approval, "summary": summary})
	_guarded(_write_back, row, trigger, "Needs Review", None)
	_notify(row.event_user, f"Waiting for a reviewer: {count} field(s) need a value")


def _ignored_blanks(s, values: dict, missing: list[dict], unresolved: list[dict]) -> list[str]:
	"""Lines for the draft's comment: every mandatory field left blank, then each optional
	field that was read as blank or whose link did not resolve."""
	meta = frappe.get_meta(s.target)
	lines = [m["reason"] for m in missing]
	seen = {(m["field"], m["row"]) for m in missing}
	child = intake_spec._child_doctype(s)
	child_meta = frappe.get_meta(child) if child else None
	for u in unresolved:
		if (u["field"], u["row"]) not in seen:
			label = _label(child_meta if u["row"] else meta, u["field"])
			lines.append(_issue_text(label, u["row"], u))
			seen.add((u["field"], u["row"]))
	for fieldname in s.fields:
		if (fieldname, None) not in seen and _blank(values.get(fieldname)):
			lines.append(f"{_label(meta, fieldname)}: unreadable")
	table = (s.table or {}).get("fieldname")
	if table and child_meta:
		for i, row in enumerate(values.get(table) or [], start=1):
			for fieldname in s.table.get("columns") or []:
				if (fieldname, i) not in seen and _blank(row.get(fieldname)):
					lines.append(f"{_label(child_meta, fieldname)}, row {i}: unreadable")
	return lines


def create(
	activity,
	values: dict,
	*,
	allow_missing: bool,
	unresolved: list[dict] | None = None,
	note: str | None = None,
) -> tuple[str, str]:
	"""Insert the draft as the uploader, attach the source file, comment on blanks, end the
	row, write back to the source record and tell the uploader. Returns
	``(doctype, name)``. A permission or validation refusal ends the row Failed and raises
	``DraftFailed`` (callers outside ``finish`` catch it). ``values`` are already resolved;
	``note`` defaults to the one saved with the reading."""
	row = activity
	trigger, s = _load_spec(row)
	uploader = _uploader(row, trigger)
	unresolved = unresolved or []
	if note is None:
		note = frappe.parse_json(row.get("reading_json") or "{}").get("_note", "") or ""
	with impersonate(uploader):
		missing = missing_fields(s, values, unresolved)
		doc = _existing_draft(row)
		resumed = doc is not None
		if not resumed:
			doc = _insert(row, trigger, s, values, allow_missing)
			# Linked at once: if anything below raises, a retry finds this draft
			# instead of inserting a second one.
			row.db_set({"draft_doctype": doc.doctype, "draft_name": doc.name})
		lines = _ignored_blanks(s, values, missing, unresolved)
		_attach_and_comment(row, doc, lines, resumed)
	status = "Needs Review" if missing else "Success"
	summary = f"Draft ready: {doc.doctype} {doc.name}"
	if lines:
		summary += f" ({len(lines)} field(s) to check)"
	row.db_set(
		{
			"status": status,
			"summary": _with_note(summary, note),
			"draft_doctype": doc.doctype,
			"draft_name": doc.name,
		}
	)
	_guarded(_write_back, row, trigger, status, doc.name)
	_notify(uploader, f"Draft ready: {doc.doctype} {doc.name}", doc)
	return doc.doctype, doc.name


def _existing_draft(row):
	"""The draft an earlier run of this row already made, if it still exists."""
	if (
		row.get("draft_doctype")
		and row.get("draft_name")
		and frappe.db.exists(row.draft_doctype, row.draft_name)
	):
		return frappe.get_doc(row.draft_doctype, row.draft_name)  # to call add_comment
	return None


def _guarded(fn, *args) -> None:
	"""A side step (comment, attach, write-back) must never undo or hide the draft."""
	try:
		fn(*args)
	except Exception:
		frappe.log_error(
			title=f"jarvis.intake.draft.{getattr(fn, '__name__', 'step')}_failed",
			message=frappe.get_traceback(),
		)


def _already_attached(row, doc) -> bool:
	url = frappe.db.get_value("File", row.source_file, "file_url")
	return bool(
		url
		and frappe.db.exists(
			"File", {"file_url": url, "attached_to_doctype": doc.doctype, "attached_to_name": doc.name}
		)
	)


def _attach_and_comment(row, doc, lines: list[str], resumed: bool) -> None:
	"""Attach the source file and leave one Comment on the blanks; both are best effort.
	On a resume the Comment is only added when the first run never got to it."""
	attached = {}
	if row.source_file and not (resumed and _already_attached(row, doc)):
		try:
			attached = attach_file_to_doc(row.source_file, doc.doctype, doc.name)
		except Exception:
			frappe.log_error(title="jarvis.intake.draft.attach_failed", message=frappe.get_traceback())
	extra = [attached["attach_error"]] if attached.get("attach_error") else []
	has_comment = resumed and frappe.db.exists(
		"Comment",
		{"reference_doctype": doc.doctype, "reference_name": doc.name, "comment_type": "Comment"},
	)
	if (lines or extra) and not has_comment:
		_guarded(doc.add_comment, "Comment", "<br>".join(escape_html(line) for line in lines + extra))


def _insert(row, trigger, s, values: dict, allow_missing: bool):
	payload = {"doctype": s.target}
	table = (s.table or {}).get("fieldname")
	for fieldname, value in values.items():
		if fieldname == table:
			continue
		if not _blank(value):
			payload[fieldname] = value
	if table:
		payload[table] = [{k: v for k, v in r.items() if not _blank(v)} for r in values.get(table) or []]
	savepoint = "jarvis_intake_draft"
	frappe.db.savepoint(savepoint)
	try:
		doc = frappe.get_doc(payload).insert(ignore_mandatory=allow_missing)
	except frappe.PermissionError:
		frappe.db.rollback(save_point=savepoint)
		_fail(row, trigger, _("You can't create {0}").format(s.target))
	except frappe.ValidationError as e:
		frappe.db.rollback(save_point=savepoint)
		_fail(row, trigger, _("The draft could not be saved: {0}").format(_short(e)), _short(e))
	except Exception as e:
		# A duplicate name or a type error from a hook: nothing half-written stays.
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="jarvis.intake.draft.insert_failed", message=frappe.get_traceback())
		_fail(row, trigger, _("The draft could not be saved: {0}").format(_short(e)), _short(e))
	return doc


def _short(e: Exception) -> str:
	text = frappe.utils.strip_html(str(e)).strip().splitlines()[0] if str(e).strip() else type(e).__name__
	return text[:140]


def _write_back(row, trigger, status: str, draft_name: str | None) -> None:
	"""Record sources only: the two fields the admin configured, set with a plain DB write
	(a system write, outside impersonation) and nothing else on the record is touched."""
	if trigger.get("intake_source") != "Record" or not row.target_docname:
		return
	record_doctype = trigger.get("intake_record_doctype")
	if not record_doctype:
		return
	meta = frappe.get_meta(record_doctype)
	updates = {}
	status_field = trigger.get("intake_status_field")
	link_field = trigger.get("intake_link_field")
	if status_field and meta.has_field(status_field):
		updates[status_field] = status
	if link_field and draft_name and meta.has_field(link_field):
		updates[link_field] = draft_name
	if updates:
		frappe.db.set_value(record_doctype, row.target_docname, updates, update_modified=False)


def _notify(user: str, subject: str, doc=None) -> None:
	"""Bell notification to the uploader, in its own savepoint so a failure never undoes
	the draft. The desk bell renders the subject as HTML, so it is escaped."""
	savepoint = "jarvis_intake_notice"
	try:
		frappe.db.savepoint(savepoint)
		log = {
			"doctype": "Notification Log",
			"for_user": user,
			"type": "Alert",
			"subject": escape_html(subject[:140]),
		}
		if doc:
			log.update({"document_type": doc.doctype, "document_name": doc.name})
		frappe.get_doc(log).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="jarvis.intake.draft.notify_failed", message=frappe.get_traceback())
