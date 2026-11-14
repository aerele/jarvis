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
from jarvis.intake import guards, notify

INTAKE_MAP_CACHE_KEY = "jarvis:intake_map"
# Short TTL backstops a bust that races a concurrent rebuild.
_CACHE_TTL_S = 300
_READABLE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "pdf")
ACTIVITY = "Jarvis Trigger Activity"
# A permanent skip: the row can never be read, so File Box offers no retry.
UNREADABLE_TYPE_SUMMARY = "This file type can't be read for intake"
DUPLICATE_SUMMARY = "Same file as an earlier upload"
# Also permanent: the source File was deleted before (or while) the job read it.
FILE_REMOVED_SUMMARY = "The file was removed"
PERMANENT_SUMMARIES = (UNREADABLE_TYPE_SUMMARY, FILE_REMOVED_SUMMARY)


def is_permanent(summary: str | None) -> bool:
	"""A row that Retry cannot change: the file cannot be read or is gone, the rule's target
	is refused, or the uploader may not create the doctype."""
	text = summary or ""
	return (
		text in PERMANENT_SUMMARIES
		or text == guards.TARGET_REFUSED_SUMMARY
		or text.startswith(guards.CREATE_REFUSAL_PREFIX)
	)


# Held from the duplicate check until the row commits; the TTL is only the crash backstop.
_LOCK_TTL_S = 15
_LOCK_WAIT_S = 5
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


def _skip_context() -> bool:
	"""Installs, migrations and imports never capture; neither does a File the intake worker
	itself attaches to a draft (it would queue the draft's own photo again)."""
	flags = frappe.local.flags
	return bool(
		flags.in_install
		or flags.in_patch
		or flags.in_migrate
		or flags.in_import
		or flags.get("jarvis_intake_attach")
	)


def _capture(doc, event: str, check_changes: bool) -> None:
	try:
		if _skip_context() or doc.flags.get("jarvis_intake_attach"):
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
		if not guards.is_desk_user(user):
			return  # a portal or signup user may upload, but never starts an AI read
		if not doc.get("content_hash"):
			frappe.log_error(
				title="Jarvis intake: file has no content hash",
				message=f"File {doc.name} has no content_hash, so intake skipped it.",
			)
			return
		for rule in matched:
			_queue_one(doc, rule, event, user)
	except Exception:
		try:
			frappe.log_error(title="Jarvis intake: capture failed")
		except Exception:
			pass


def on_record_save(doc, method=None) -> None:
	"""``doc_events "*"`` on_update: a Record rule also captures files that Frappe bound to
	the record through an Attach or Attach Image field. Those are bound with a plain DB
	write, so no File save fires; this runs after Frappe's own on_update hook has bound them.
	Costs one cached-map lookup for a doctype no Record rule watches."""
	try:
		# A rule's own save must not rebuild the map it just busted.
		if _skip_context() or doc.doctype in ("File", "Jarvis Trigger"):
			return
		rules = intake_map()["record"].get(doc.doctype)
		user = frappe.session.user
		if not rules or not doc.name or not user or user == "Guest" or not guards.is_desk_user(user):
			return
		since = _bound_since(doc)
		if since is False:
			return
		filters = {"attached_to_doctype": doc.doctype, "attached_to_name": doc.name, "is_folder": 0}
		if since:
			filters["creation"] = [">=", since]
		# System context: the uploader is whoever saved the record, checked per rule below.
		files = frappe.get_all(
			"File",
			filters=filters,
			fields=[
				"name",
				"file_name",
				"file_url",
				"content_hash",
				"attached_to_doctype",
				"attached_to_name",
			],
		)
		copies = _intake_copy_urls(doc) if files else set()
		files = [f for f in files if f.file_url not in copies]
		if not files:
			return
		for rule in rules:
			_capture_bound(rule, files, user)
	except Exception:
		frappe.log_error(title="Jarvis intake: capture failed")


def _bound_since(doc):
	"""Only files bound during THIS save count, never the older attachments of a record
	that is saved again (they cost an AI call each and were attached for other reasons).
	Returns the earliest ``File.creation`` that counts (the record's previous ``modified``),
	None for a new record (everything attached now counts), or False when this save captures
	nothing: a submit or cancel."""
	if doc.get("_action") in ("submit", "cancel"):
		return False
	if doc.flags.in_insert:
		return None
	before = doc.get_doc_before_save()
	if not before or not before.get("modified"):
		return False  # unknown history: capturing everything would be the old bug
	return before.modified


def _intake_copy_urls(doc) -> set:
	"""``file_url`` of the source files of the intake rows whose draft is ``doc``: the copy
	of a source file that a draft carries is not a new upload."""
	activity, source = frappe.qb.DocType(ACTIVITY), frappe.qb.DocType("File")
	rows = (
		frappe.qb.from_(activity)
		.join(source)
		.on(source.name == activity.source_file)
		.select(source.file_url)
		.where((activity.draft_doctype == doc.doctype) & (activity.draft_name == doc.name))
		.run()
	)
	return {r[0] for r in rows if r[0]}


def _capture_bound(rule: dict, files: list, user: str) -> None:
	"""Queue the files of ``files`` this rule has no row for yet (a row of any status counts,
	so a Retry stays the uploader's decision and a later save never re-queues)."""
	done = set(
		frappe.get_all(  # system context: capture runs for any saver
			ACTIVITY,
			filters={"trigger": rule["name"], "source_file": ["in", [f.name for f in files]]},
			pluck="source_file",
		)
	)
	for f in files:
		if f.name not in done and f.content_hash:
			# No bell for an unreadable type here: the user saved a record, not uploaded a file.
			_queue_one(f, rule, "on_update", user, bell=False)


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


def _queue_one(doc, rule: dict, event: str, user: str, bell: bool = True) -> None:
	file_name = doc.get("file_name") or ""
	ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
	# A Record rule drafts per record, so the same scan on another record is new work.
	record = _attached_name(doc) if rule["intake_source"] == "Record" else None
	if ext not in _READABLE_EXTENSIONS:
		row, duplicate_of = _insert_row(doc, rule, event, "Skipped", UNREADABLE_TYPE_SUMMARY)
	elif refusal := guards.create_refusal(user, rule["intake_target_doctype"]):
		# No AI call, no cap use: the draft could never be created for this uploader.
		row, duplicate_of = _insert_row(doc, rule, event, "Skipped", refusal)
	else:
		row, duplicate_of = _insert_unless_duplicate(doc, rule, event, record)
	status, summary = row.status, row.summary
	if bell and status == "Skipped" and not duplicate_of:
		# An unreadable type will never be drafted; the uploader may have no Jarvis access,
		# so the bell is how they learn of it. A duplicate stays quiet.
		notify.row_ended(row, summary)
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


def _insert_row(doc, rule: dict, event: str, status: str, summary: str, duplicate_of: str | None = None):
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
			"target_docname": _attached_name(doc),
			"doc_event": event,
			"event_user": frappe.session.user,
			"summary": summary,
			"duplicate_of": duplicate_of,
		}
	).insert(ignore_permissions=True)
	return row, duplicate_of


def _attached_name(doc) -> str:
	name = doc.get("attached_to_name") or ""
	return "" if name.startswith("new-") else name


def _insert_unless_duplicate(doc, rule: dict, event: str, record: str | None):
	"""Check for an earlier row and insert this one under one Redis lock, so two uploads of
	the same bytes at once cannot both pass the check. (The worker re-checks after its claim:
	that is the guarantee, this keeps the second row from being Queued at all.)"""
	release = _lock(rule["name"], doc.content_hash, record)
	try:
		duplicate_of = earlier_row(rule["name"], doc.content_hash, record)
		if duplicate_of:
			return _insert_row(doc, rule, event, "Skipped", DUPLICATE_SUMMARY, duplicate_of)
		return _insert_row(doc, rule, event, "Queued", f"Queued {doc.get('file_name') or doc.name}"[:140])
	finally:
		# The lock must outlive this function: the row is not visible to another request
		# until the transaction commits.
		_release_when_settled(release)


def _lock(trigger: str, file_hash: str, record: str | None):
	"""Take the (rule, bytes, record) lock, waiting a few seconds. Returns the release
	callable, or None when Redis is unavailable (the worker's re-check still protects)."""
	try:
		cache = frappe.cache()
		lock = cache.lock(
			cache.make_key(f"jarvis:intake:capture:{trigger}:{file_hash}:{record or ''}"),
			timeout=_LOCK_TTL_S,
			blocking_timeout=_LOCK_WAIT_S,
		)
		if not lock.acquire(blocking=True):
			return None
	except Exception:
		return None

	def release():
		try:
			lock.release()
		except Exception:
			pass  # the TTL is the backstop

	return release


def _release_when_settled(release) -> None:
	if not release:
		return
	try:
		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)
	except Exception:
		release()


def earlier_row(
	trigger: str,
	file_hash: str,
	record: str | None = None,
	*,
	exclude: str | None = None,
	created_before=None,
) -> str | None:
	"""The first row of this rule that already stands for the same bytes, or None.

	Failed and Skipped rows never produced a draft, so a re-upload may retry. A row that
	did (Success, or Needs Review with a draft) only counts while its draft still exists:
	a deleted draft would otherwise leave the file a duplicate for good, with nothing
	retryable. In-flight and waiting-for-review rows (no draft yet) always count.

	``record`` narrows a Record-source rule to the record the file is attached to. The worker
	passes ``exclude`` (its own row) and ``created_before`` (its creation) so that of two rows
	for the same file only the older one proceeds."""
	filters = {"trigger": trigger, "file_hash": file_hash, "status": ["not in", ["Failed", "Skipped"]]}
	if record is not None:
		filters["target_docname"] = record
	if exclude:
		filters["name"] = ["!=", exclude]
	if created_before:
		filters["creation"] = ["<=", created_before]
	rows = frappe.get_all(  # system context: the capture hook runs for any uploader
		ACTIVITY,
		filters=filters,
		fields=["name", "draft_doctype", "draft_name"],
		order_by="creation asc",
	)
	for r in rows:
		if not r.draft_name or frappe.db.exists(r.draft_doctype, r.draft_name):
			return r.name
	return None
