"""Document Intake: capture listener.

``File`` after_insert / on_update hooks that turn an upload into a queued
intake job when an enabled Intake rule watches the folder or the record type.

HOT-PATH PROPERTY: the hooks run on every File save, so with no Intake rule
they cost one cached-map read and return, with no DB query. They must also
NEVER raise: a failure here may not break the user's upload.
"""

import frappe
from frappe.utils import cint

from jarvis import compat

INTAKE_MAP_CACHE_KEY = "jarvis:intake_map"
# Short TTL backstops a bust that races a concurrent rebuild.
_CACHE_TTL_S = 300
_READABLE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "pdf")
ACTIVITY = "Jarvis Trigger Activity"
# A permanent skip: the row can never be read, so File Box offers no retry.
UNREADABLE_TYPE_SUMMARY = "This file type can't be read for intake"
# Also permanent: the source File was deleted before (or while) the job read it.
FILE_REMOVED_SUMMARY = "The file was removed"
PERMANENT_SUMMARIES = (UNREADABLE_TYPE_SUMMARY, FILE_REMOVED_SUMMARY)
_RULE_FIELDS = (
	"name",
	"trigger_name",
	"intake_source",
	"intake_folder",
	"intake_record_doctype",
	"intake_target_doctype",
)


def _delete_cache_key() -> None:
	frappe.cache().delete_value(INTAKE_MAP_CACHE_KEY)


def clear_intake_cache() -> None:
	"""Bust the intake registry now AND again on commit, so a concurrent request
	cannot re-cache the map from the still-old committed rows (same reasoning as
	``jarvis.triggers.engine.clear_cache``)."""
	_delete_cache_key()
	try:
		frappe.db.after_commit.add(_delete_cache_key)
	except Exception:
		pass


def _build_intake_map() -> dict:
	# System context: the registry is shared by every uploader, so it must not
	# depend on who triggered the build.
	rows = frappe.get_all(
		"Jarvis Trigger",
		filters={"enabled": 1, "action_type": "Intake"},
		fields=list(_RULE_FIELDS),
	)
	out: dict = {"folder": {}, "record": {}}
	for r in rows:
		if r.intake_source == "Folder" and r.intake_folder:
			out["folder"].setdefault(r.intake_folder, []).append(dict(r))
		elif r.intake_source == "Record" and r.intake_record_doctype:
			out["record"].setdefault(r.intake_record_doctype, []).append(dict(r))
	return out


def intake_map() -> dict:
	"""{"folder": {path: [rule]}, "record": {doctype: [rule]}}, cached.

	Fail safe like the trigger map: when the doctype is unreachable (app deployed,
	site not migrated yet) return an empty map WITHOUT caching it."""
	cached = compat.cache_get_memoized(INTAKE_MAP_CACHE_KEY)
	if cached is None:
		try:
			cached = _build_intake_map()
		except Exception:
			return {"folder": {}, "record": {}}
		frappe.cache().set_value(INTAKE_MAP_CACHE_KEY, cached, expires_in_sec=_CACHE_TTL_S)
	return cached


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def on_file_insert(doc, method=None) -> None:
	_capture(doc, "after_insert", check_changes=False)


def on_file_update(doc, method=None) -> None:
	# Insert fires on_update right after after_insert; the insert hook owns that capture.
	if doc.flags.in_insert:
		return
	_capture(doc, "on_update", check_changes=True)


def _capture(doc, event: str, check_changes: bool) -> None:
	try:
		flags = frappe.local.flags
		if flags.in_install or flags.in_patch or flags.in_migrate or flags.in_import:
			return
		if cint(doc.get("is_folder")):
			return
		user = frappe.session.user
		if not user or user == "Guest":
			return
		rules = intake_map()
		if not rules["folder"] and not rules["record"]:
			return
		matched = _matching_rules(doc, rules, check_changes)
		if not matched:
			return
		if not doc.get("content_hash"):
			frappe.log_error(
				title="Jarvis Intake: file has no content hash",
				message=f"File {doc.name} has no content_hash, so intake skipped it.",
			)
			return
		for rule in matched:
			_queue_one(doc, rule, event, user)
	except Exception:
		try:
			frappe.log_error(title="Jarvis Intake: capture failed")
		except Exception:
			pass


def _matching_rules(doc, rules: dict, check_changes: bool) -> list[dict]:
	"""Rules this save triggers. On insert everything present counts. On update
	only a CHANGE counts: a file newly attached to a saved record, or moved into
	a watched folder. Any other edit must not re-capture the file."""
	matched: list[dict] = []
	before = doc.get_doc_before_save() if check_changes else None
	if check_changes and not before:
		return matched
	if doc.get("folder") and (not check_changes or before.get("folder") != doc.folder):
		matched.extend(rules["folder"].get(doc.folder, []))
	attached_to = doc.get("attached_to_name") or ""
	# A "new-..." name is a form that is not saved yet; the real name arrives later.
	if attached_to and not attached_to.startswith("new-") and doc.get("attached_to_doctype"):
		was_empty = check_changes and not before.get("attached_to_name")
		if not check_changes or was_empty:
			matched.extend(rules["record"].get(doc.attached_to_doctype, []))
	return matched


def _queue_one(doc, rule: dict, event: str, user: str) -> None:
	file_name = doc.get("file_name") or ""
	ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
	attached_name = doc.get("attached_to_name") or ""
	if attached_name.startswith("new-"):
		attached_name = ""
	status, summary, duplicate_of = "Queued", f"Queued {doc.get('file_name') or doc.name}"[:140], None
	if ext not in _READABLE_EXTENSIONS:
		status, summary = "Skipped", UNREADABLE_TYPE_SUMMARY
	else:
		duplicate_of = _earlier_row(rule["name"], doc.content_hash)
		if duplicate_of:
			status, summary = "Skipped", "Same file as an earlier upload"
	row = frappe.get_doc(
		{
			"doctype": ACTIVITY,
			"trigger": rule["name"],
			"trigger_label": rule["trigger_name"],
			"action_type": "Intake",
			"status": status,
			"source_file": doc.name,
			"file_hash": doc.content_hash,
			"target_doctype": rule["intake_target_doctype"],
			"target_docname": attached_name,
			"doc_event": event,
			"event_user": frappe.session.user,
			"summary": summary,
			"duplicate_of": duplicate_of,
		}
	).insert(ignore_permissions=True)
	if status != "Queued":
		return
	frappe.enqueue(
		"jarvis.intake.worker.run_job",
		queue="default",
		enqueue_after_commit=True,
		job_id=f"intake:{row.name}",
		deduplicate=True,
		activity=row.name,
	)


def _earlier_row(trigger: str, file_hash: str) -> str | None:
	"""The first row of this rule that already stands for the same bytes, or None.

	Failed and Skipped rows never produced a draft, so a re-upload may retry. A row that
	did (Success, or Needs Review with a draft) only counts while its draft still exists:
	a deleted draft would otherwise leave the file a duplicate for good, with nothing
	retryable. In-flight and waiting-for-review rows (no draft yet) always count."""
	rows = frappe.get_all(  # system context: the capture hook runs for any uploader
		ACTIVITY,
		filters={"trigger": trigger, "file_hash": file_hash, "status": ["not in", ["Failed", "Skipped"]]},
		fields=["name", "draft_doctype", "draft_name"],
		order_by="creation asc",
	)
	for r in rows:
		if not r.draft_name or frappe.db.exists(r.draft_doctype, r.draft_name):
			return r.name
	return None
