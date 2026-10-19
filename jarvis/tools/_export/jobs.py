"""Durable background exports. All state transitions are fenced by an attempt token."""

import contextlib
import importlib.util
import json
import os
import secrets
import shutil
import time
import traceback
from datetime import timezone
from pathlib import Path

import frappe
from frappe.query_builder.functions import Coalesce
from frappe.utils import add_to_date, cint, get_datetime

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export.clock import now_datetime
from jarvis.tools._export.saved_queries import fingerprint
from jarvis.tools._export.streaming import ExportLimit, prepare

DOCTYPE = "Jarvis Export Job"
ACTIVE = ("queued", "running", "paused")
TERMINAL = ("ready", "too large", "too slow", "failed", "cancelled", "expired")
# Rows that age out by creation date. A ready row leaves only by expiring first (its File would
# otherwise be swept as an orphan); an expired row is kept for ROW_RETENTION_DAYS after it expired.
AGED_OUT = ("too large", "too slow", "failed", "cancelled")
ROW_RETENTION_DAYS = 30
EXPORTS_FOLDER = "Home/Jarvis Exports"
FILE_FIELDS = ["name", "owner", "is_private", "file_url", "file_name"]
STATE_FIELDS = [
	"name",
	"owner",
	"title",
	"status",
	"rows_written",
	"started_at",
	"finished_at",
	"expires_at",
	"next_attempt",
	"cancel_requested",
	"error",
	"format",
	"size_bytes",
	"limited_by_spec",
	"cells_truncated",
	"file",
	# What the card shows without parsing `source` on every poll. "attempt N of 2" comes
	# from `retries`: a maintenance pause raises before `attempts` is incremented.
	"source_doctype",
	"column_count",
	"asked_at",
	"attempts",
	"retries",
]


JOB_FIELDS = STATE_FIELDS + [
	"conversation",
	"source",
	"highlights",
	"permission_fingerprint",
	"attempt_token",
	"heartbeat",
	"card_message",
	"announced_status",
	"creation",
]


def _get_job(name, *, for_update=False):
	job = frappe.db.get_value(DOCTYPE, name, JOB_FIELDS, as_dict=True, for_update=for_update)
	if not job:
		frappe.throw("Export not found", frappe.DoesNotExistError)
	return job


@contextlib.contextmanager
def _rollback_on_error():
	"""Release row locks and discard partial writes if a transaction boundary fails."""
	try:
		yield
	except Exception:
		frappe.db.rollback()
		raise


def _commit():
	# Call sites document why independent workers/delivery need this boundary.
	with _rollback_on_error():
		frappe.db.commit()  # nosemgrep: frappe-manual-commit


def _traceback_text(exc, cause):
	errors = [error for error in (exc, cause) if error is not None]
	if len(errors) == 2 and errors[0] is errors[1]:
		errors.pop()
	chunks = ["".join(traceback.format_exception(type(e), e, e.__traceback__)) for e in errors]
	return "\n".join(chunks) or frappe.get_traceback()


def _log_failure(name, title, exc=None, *, cause=None, token=None, doctype=DOCTYPE, **context):
	"""Leave a committed Error Log that names the job. Never raises.

	Durable on purpose: ``log_error`` joins the open transaction and ``_finish`` starts with a
	rollback, so a log written the plain way is lost with the failed work. Roll back first
	(that work is discarded anyway), insert, commit. The job's ``source`` and ``highlights``
	hold filter values and are never written; the message is always explicit, because without
	one Frappe dumps the caller's local variables. A stale attempt (``token`` no longer matches)
	is not a failure and logs nothing."""
	with contextlib.suppress(Exception):
		trace = _traceback_text(exc, cause)
		if getattr(frappe.local, "request", None):
			# A web request: Frappe 16 attaches the request's form_dict (the tool arguments)
			# to an Error Log, so these tails go to the log file instead.
			frappe.logger("jarvis.export").error(f"{title}: {name}: {exc!r}\n{trace}")
			return
		frappe.db.rollback()
		if token is not None and frappe.db.get_value(DOCTYPE, name, "attempt_token") != token:
			return
		lines = [f"{doctype or 'subject'}: {name}"]
		if doctype == DOCTYPE:
			row = frappe.db.get_value(
				DOCTYPE,
				name,
				["owner", "format", "status", "attempts", "retries", "rows_written"],
				as_dict=True,
			)
			lines += [f"{key}: {value}" for key, value in (row or {}).items()]
		lines += [f"{key}: {value}" for key, value in context.items()]
		for label, error in (("error", exc), ("cause", cause)):
			if error is not None:
				lines.append(f"{label}: {type(error).__name__}: {error}")
		frappe.log_error(
			title=f"jarvis.export {title}: {name}"[:140],
			message="\n".join([*lines, "", trace]),
			reference_doctype=doctype,
			reference_name=name if doctype else None,
		)
		_commit()


def _safely(step, name, call, *args, doctype=DOCTYPE, **kwargs):
	"""Run one step whose failure must not stop the steps after it; log the failure."""
	try:
		return call(*args, **kwargs)
	except Exception as exc:
		_log_failure(name, step, exc, doctype=doctype)


def _after(name):
	"""The tail every attempt ends with. A failing announce must not skip dispatch."""
	_safely("announce", name, announce, name)
	_safely("dispatch", name, dispatch)


def limits():
	return (
		max(1, min(200_000, int(frappe.conf.get("jarvis_export_row_limit", 200_000)))),
		max(1, int(frappe.conf.get("jarvis_export_max_mb", 50))) * 1024 * 1024,
	)


def retention_days():
	"""Days a finished file is kept. 0 keeps it (and its job row) until the chat is deleted."""
	# cint: an empty or string value must not raise inside a publish.
	return max(0, cint(frappe.conf.get("jarvis_export_retention_days", 30)))


def effective_expiry(job):
	"""When a ready file stops being kept, or None for never.

	The stored ``expires_at`` was set when the file was published. Lowering the setting later
	applies to files already stored; raising it never extends them; 0 leaves the stored date."""
	stored = get_datetime(job.expires_at) if job.expires_at else None
	days = retention_days()
	if not days or not job.finished_at:
		return stored
	cap = add_to_date(get_datetime(job.finished_at), days=days)
	return min(stored, cap) if stored else cap


def ensure_exports_folder():
	"""Create the folder exports are filed in, in its own transaction. True once it exists.

	Own transaction on purpose: the File insert validates its folder Link with a plain read, so
	a publisher whose snapshot predates another publisher's folder commit would fail a finished
	export. Callers run this before any row lock."""
	if frappe.db.exists("File", EXPORTS_FOLDER):
		return True
	frappe.get_doc(
		{"doctype": "File", "file_name": "Jarvis Exports", "is_folder": 1, "folder": "Home"}
	).insert(ignore_permissions=True, ignore_if_duplicate=True)
	# A worker creates it as the export's owner; the folder belongs to the site.
	frappe.db.set_value("File", EXPORTS_FOLDER, "owner", "Administrator", update_modified=False)
	_commit()
	return True


def after_migrate():
	# Best effort: a missing folder is rebuilt by the first publish, so it never blocks a migrate.
	if frappe.db.table_exists(DOCTYPE):
		_safely("exports folder", "migrate", ensure_exports_folder, doctype=None)


@contextlib.contextmanager
def _lock():
	"""One site-wide admission/dedup lock, held only for short database work."""
	# End the request snapshot before serializing admission with other connections.
	_commit()
	key = "jv-export:" + fingerprint(frappe.local.site)[:40]
	if not frappe.db.sql("SELECT GET_LOCK(%s, 5)", (key,))[0][0]:
		raise InvalidArgumentError("Exports are busy; please try again shortly.")
	try:
		with _rollback_on_error():
			yield
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))


class BackgroundUnavailable(InvalidArgumentError):
	"""A short, user-readable reason a background export cannot be started."""


XLSXWRITER_MISSING = (
	"Excel files this large need a server update (the xlsxwriter package). "
	"I can export it as CSV instead, which works now."
)


def _xlsxwriter_available():
	# Frappe 16 pins xlsxwriter; a Frappe 15 bench only has it after `bench setup requirements`.
	return importlib.util.find_spec("xlsxwriter") is not None


def kill_switch_on():
	# cint, so a "0" stored as a string (bench set-config without -p) does not disable exports.
	return bool(cint(frappe.conf.get("jarvis_background_export_disabled")))


def _supported(fmt=None):
	if kill_switch_on():
		raise BackgroundUnavailable("Background exports are turned off on this site.")
	if frappe.db.db_type != "mariadb":
		raise BackgroundUnavailable("This site's database does not support background exports.")
	if frappe.conf.maintenance_mode:
		raise BackgroundUnavailable("This site is being updated. Please export again after the update.")
	if fmt == "xlsx" and not _xlsxwriter_available():
		raise BackgroundUnavailable(XLSXWRITER_MISSING)


NO_CHAT = "Start it from a Jarvis chat so the file can be delivered there."


def current_conversation():
	"""The caller's current Jarvis chat (their own), or None. Never raises."""
	from jarvis.tools._agent_run_ctx import get_session_key

	key = get_session_key()
	if not key:
		return None
	return (
		frappe.db.get_value("Jarvis Conversation", {"session_key": key, "owner": frappe.session.user}, "name")
		or None
	)


def _permission_fingerprint(prepared):
	# Recent-file reuse must not survive changed row scope or field masking.
	return fingerprint(
		{
			"sql": prepared.sql,
			"params": prepared.params,
			"mask": {
				a: {name: (df.fieldtype, df.options) for name, df in fields.items()}
				for a, fields in prepared.masked.items()
			},
		}
	)


def _key(source, fmt, highlights):
	return fingerprint({"source": source, "format": fmt, "highlights": highlights or []})


def existing(source, fmt, highlights=None, *, conversation, prepared=None):
	"""Revalidate access before returning either active state or a recent private file.

	Per chat: the card lives in one conversation, so a request in another chat starts its own
	export. A caller with no chat (a macro, a delegate) never reuses one."""
	# Reuse is optional. Users without job access can still use immediate exports
	# under the source DocTypes' read/export permissions.
	if not conversation or not frappe.has_permission(DOCTYPE, "read"):
		return None
	rows = frappe.get_list(
		DOCTYPE,
		filters={
			"owner": frappe.session.user,
			"conversation": conversation,
			"fingerprint": _key(source, fmt, highlights),
			"status": ["in", [*ACTIVE, "ready"]],
		},
		fields=["name"],
		order_by="creation desc",
		limit=10,
	)
	if not rows:
		return None
	prepared = prepared or prepare(source, limits()[0], highlights)
	permission_key = _permission_fingerprint(prepared)
	for row in rows:
		job = _get_job(row.name)
		if job.status in ACTIVE:
			return public(job)
		if (
			job.permission_fingerprint == permission_key
			and job.finished_at
			and get_datetime(job.finished_at) >= add_to_date(now_datetime(), minutes=-10)
			and _file_exists(job)
		):
			return public(job)
	return None


def enqueue(source, fmt, title=None, highlights=None):
	_supported(fmt)
	# Every refusal below is a BackgroundUnavailable: export_query composes its reason with
	# the original "narrow it" advice, so the agent always hears both.
	if not frappe.has_permission(DOCTYPE, "read"):
		raise BackgroundUnavailable("Your role cannot run background exports.")
	conversation = current_conversation()
	if not conversation:
		raise BackgroundUnavailable(NO_CHAT)
	try:
		prepared = prepare(source, limits()[0], highlights)
	except (InvalidArgumentError, PermissionDeniedError) as exc:
		raise BackgroundUnavailable(str(exc)) from None
	try:
		with _lock():
			found = existing(source, fmt, highlights, conversation=conversation, prepared=prepared)
			if found:
				return found
			job = frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"owner": frappe.session.user,
					"conversation": conversation,
					"title": (title or prepared.doctype or "Export")[:140],
					"source_doctype": prepared.doctype or None,
					"column_count": len(prepared.columns),
					"asked_at": now_datetime(),
					"format": fmt,
					"source": json.dumps(source),
					"highlights": json.dumps(highlights),
					"status": "queued",
					"fingerprint": _key(source, fmt, highlights),
					"permission_fingerprint": _permission_fingerprint(prepared),
				}
			)
			job.flags.jarvis_server_write = True
			job.insert(ignore_permissions=True)
			# Persist the new job before another transaction creates its card or dispatches it.
			_commit()
	except BackgroundUnavailable:
		raise
	except InvalidArgumentError as exc:
		# The site lock timed out before any row was written.
		raise BackgroundUnavailable(str(exc)) from None
	# The job is committed: nothing below may fail the tool call.
	_safely("announce", job.name, announce, job.name, initial=True)
	_safely("dispatch", job.name, dispatch)
	return public(_get_job(job.name))


def _ready_file(job, files=None):
	if not job.file:
		return None
	if files is None:
		file = frappe.db.get_value("File", job.file, FILE_FIELDS, as_dict=True)
	else:
		file = files.get(job.file)
	if not file or file.owner != job.owner or not file.is_private:
		return None
	file = frappe.get_doc({"doctype": "File", **file})
	return file if Path(file.get_full_path()).is_file() else None


def _file_exists(job):
	return _ready_file(job) is not None


# The one wording of the de-dup rule; the descriptor and the persona use the same sentence.
NOTE = (
	"The file will appear in this chat's export card, which shows progress and offers Cancel. "
	"Do not call export_query again to check on it: a repeat in this chat returns the running "
	"export, or the file it made in the last 10 minutes; otherwise it starts a new export."
)


def public(job, files=None):
	def iso(value):
		return get_datetime(value).replace(tzinfo=timezone.utc).isoformat() if value else None

	expiry = effective_expiry(job)
	data = {
		"export_job": job.name,
		"title": job.title,
		"status": job.status,
		"rows_written": job.rows_written or 0,
		"started_at": iso(job.started_at),
		"finished_at": iso(job.finished_at),
		"expires_at": iso(expiry),
		"next_attempt": iso(job.next_attempt),
		"cancel_requested": bool(job.cancel_requested),
		"error": job.error,
		"format": job.format,
		"size_bytes": job.size_bytes or 0,
		"limited_by_spec": job.limited_by_spec or None,
		"cells_truncated": bool(job.cells_truncated),
		"source_doctype": job.source_doctype or None,
		"column_count": job.column_count or 0,
		"asked_at": iso(job.asked_at),
		"attempts": job.attempts or 0,
		"retries": job.retries or 0,
		"note": NOTE,
	}
	if job.status == "ready" and expiry and expiry <= now_datetime():
		data.update(status="expired", error="This export has expired; ask for a new export.")
	elif job.status == "ready":
		file = _ready_file(job, files)
		if file:
			data.update(
				file_url=file.file_url, filename=file.file_name, name=file.name, total=job.rows_written
			)
		else:
			data.update(status="expired", error="This file is no longer available; ask for a new export.")
	return data


def _check_owner(job, *, cancel=False):
	user = frappe.session.user
	if user == "Guest" or (job.owner != user and (cancel or "System Manager" not in frappe.get_roles(user))):
		frappe.throw("Export not found", frappe.PermissionError)


@frappe.whitelist()
def get_jobs(names: str | list[str]):
	if isinstance(names, str):
		names = json.loads(names)
	if not isinstance(names, list) or len(names) > 100 or any(not isinstance(n, str) for n in names):
		frappe.throw("Pass up to 100 export IDs")
	out = dict.fromkeys(names)
	if not names:
		return out
	rows = frappe.get_list(
		DOCTYPE, filters={"name": ["in", list(out)]}, fields=STATE_FIELDS, limit_page_length=100
	)
	for job in rows:
		_check_owner(job)
	file_names = {job.file for job in rows if job.status == "ready" and job.file}
	files = (
		{
			file.name: file
			for file in frappe.get_list(
				"File", filters={"name": ["in", list(file_names)]}, fields=FILE_FIELDS, limit_page_length=100
			)
		}
		if file_names
		else {}
	)
	for job in rows:
		out[job.name] = public(job, files)
	return out


@frappe.whitelist(methods=["POST"])
def cancel(name: str):
	job = _get_job(name)
	_check_owner(job, cancel=True)
	with _lock():
		job = _get_job(job.name)
		if job.status in ACTIVE:
			frappe.db.set_value(DOCTYPE, name, "cancel_requested", 1)
			if job.status != "running" or not job.started_at:
				frappe.db.set_value(
					DOCTYPE,
					name,
					{"status": "cancelled", "finished_at": now_datetime(), "error": "Export cancelled."},
				)
			# Make the cancellation visible to the independent watchdog immediately.
			_commit()
	# The cancel is committed: nothing below may fail the tool call.
	_safely("announce", name, announce, name)
	if job.status != "running" or not job.started_at:
		_safely("dispatch", name, dispatch)
	return public(_get_job(name))


def dispatch():
	# The kill switch holds the queue: queued jobs wait and resume when it is switched back off.
	if frappe.conf.maintenance_mode or frappe.db.db_type != "mariadb" or kill_switch_on():
		return
	from jarvis.chat.pump import _probe_worker_count, _registry_is_stale

	workers = _probe_worker_count("long")
	if workers is None or (workers == 0 and _registry_is_stale()):
		workers = 1
	if not workers:
		return
	capacity = max(1, workers - 1)
	with _lock():
		running = frappe.get_all(DOCTYPE, filters={"status": "running"}, fields=["owner"])
		owners = {r.owner for r in running}
		slots = max(0, capacity - len(running))
		while slots:
			# Filter before LIMIT and reselect after each owner reservation. A large
			# backlog belonging to one owner cannot hide another owner's runnable job.
			j = frappe.qb.DocType(DOCTYPE)
			query = (
				frappe.qb.from_(j)
				.select(j.name, j.owner)
				.where(j.status.isin(["queued", "paused"]))
				.where(Coalesce(j.cancel_requested, 0) == 0)
				.where(j.next_attempt.isnull() | (j.next_attempt <= now_datetime()))
			)
			if owners:
				query = query.where(j.owner.notin(list(owners)))
			waiting = query.orderby(j.creation, j.name).limit(1).run(as_dict=True)
			if not waiting:
				break
			row = waiting[0]
			token = secrets.token_hex(16)
			frappe.db.set_value(
				DOCTYPE,
				row.name,
				{
					"status": "running",
					"attempt_token": token,
					"heartbeat": now_datetime(),
					"rows_written": 0,
					"started_at": None,
					"error": "",
				},
			)
			# Reserve before enqueue. A failed enqueue hands the reservation back.
			# Persist the capacity reservation before RQ can start this attempt.
			_commit()
			try:
				frappe.enqueue(
					"jarvis.tools._export.jobs.run",
					queue="long",
					timeout=900,
					job_id="export-" + token,
					name=row.name,
					token=token,
				)
			except Exception as exc:
				# Redis is likely down. Give the reservation back, so the reaper does not charge
				# it against the one retry, and reserve nothing more.
				_return_to_queue(row.name, token)
				_log_failure(row.name, "enqueue", exc)
				break
			owners.add(row.owner)
			slots -= 1


def _directory(name, token):
	if not name.isalnum() or not token.isalnum():
		raise ValueError("Invalid export attempt")
	return Path(frappe.get_site_path("private", "jarvis_exports", name + "-" + token))


def _return_to_queue(name, token):
	"""Hand a reserved attempt back to the queue, unspent. Fenced: a stale token changes nothing."""
	j = frappe.qb.DocType(DOCTYPE)
	(
		frappe.qb.update(j)
		.set(j.status, "queued")
		.set(j.attempt_token, "")
		.set(j.started_at, None)
		.where((j.name == name) & (j.attempt_token == token) & (j.status == "running"))
	).run()
	_commit()


def _attempt(name, token):
	row = frappe.db.get_value(DOCTYPE, name, ["attempt_token", "status", "cancel_requested"], as_dict=True)
	return bool(row and row.attempt_token == token and row.status == "running" and not row.cancel_requested)


GENERIC_FAILURE = (
	"The export could not finish. Ask again in a few minutes; if it keeps failing, your "
	'administrator can find "jarvis.export" in the Error Log.'
)


class _NoRecords(ExportLimit):
	"""Nothing matches the caller's permissions and filters: a normal outcome, not logged."""


def run(name, token):
	"""One attempt, fenced by its token. Nothing in it may skip the announce/dispatch tail."""
	try:
		if not _attempt(name, token):
			_settle(name, token, "cancelled", "Export cancelled.")
			_after(name)
			return
		job = _get_job(name)
		directory = _directory(name, token)
	except Exception as exc:
		_log_failure(name, "start", exc, token=token)
		_settle(name, token, "failed", GENERIC_FAILURE)
		_after(name)
		return
	_execute(job, token, directory)


def _execute(job, token, directory):
	from jarvis._session import impersonate
	from jarvis.tools._export.database import MariaDBStream, Watchdog, connection_settings
	from jarvis.tools._export.streaming import write

	name = job.name
	db = watchdog = None
	handed_back = False
	started = time.monotonic()
	try:
		with impersonate(job.owner):
			if not frappe.db.get_value("User", job.owner, "enabled"):
				raise ExportLimit("failed", "The export owner no longer has an active account.")
			if frappe.conf.maintenance_mode:
				raise ExportLimit("paused", "Paused for a site update; retrying in about five minutes.")
			if kill_switch_on():
				# An attempt reserved just before the flip waits in the queue. No retry is spent.
				_return_to_queue(name, token)
				handed_back = True
				return
			if job.format == "xlsx" and not _xlsxwriter_available():
				raise ExportLimit("failed", XLSXWRITER_MISSING)
			j = frappe.qb.DocType(DOCTYPE)
			(
				frappe.qb.update(j)
				.set(j.attempts, j.attempts + 1)
				.set(j.started_at, now_datetime())
				.set(j.heartbeat, now_datetime())
				.where((j.name == name) & (j.attempt_token == token) & (j.status == "running"))
			).run()
			# Expose the running attempt before starting its independent heartbeat connection.
			_commit()
			row_cap, size_cap = limits()
			prepared = prepare(json.loads(job.source), row_cap, json.loads(job.highlights or "null"))
			directory.mkdir(parents=True, exist_ok=False, mode=0o700)
			path = directory / "export.part"
			db = MariaDBStream(connection_settings())
			db.open()
			watchdog = Watchdog(db, name, token, frappe.local.site, job.owner, job.conversation)
			watchdog.started = started
			watchdog.start()
			try:
				watchdog.check()
				result = write(
					prepared,
					db.stream_rows(prepared.sql, prepared.params),
					path,
					job.format,
					row_cap,
					size_cap,
					watchdog.check,
					watchdog.progress,
				)
			except Exception:
				watchdog.stop()
				with contextlib.suppress(Exception):
					db.cancel_own_query()
				if watchdog.reason:
					raise watchdog.reason from None
				raise
			finally:
				watchdog.stop()
				db.close()
			watchdog.check()
			if result["rows_written"] == 0:
				raise _NoRecords("failed", "No records match your current permissions and filters.")
			_publish_file(job, token, path, result, _permission_fingerprint(prepared))
	except ExportLimit as exc:
		if exc.status == "failed" and not isinstance(exc, _NoRecords):
			# The watchdog's own exception is the interesting one; re-raising its reason erased it.
			cause = watchdog.cause if watchdog else None
			_log_failure(name, "failed", exc, cause=cause, token=token, elapsed_s=_elapsed(started))
		_settle(name, token, exc.status, str(exc))
	except Exception as exc:
		from jarvis import _failure_kind
		from jarvis.exceptions import PermissionDeniedError

		status = "too slow" if _failure_kind.too_slow(exc) else "failed"
		known = isinstance(exc, (PermissionDeniedError, InvalidArgumentError))
		_log_failure(name, status, exc, token=token, elapsed_s=_elapsed(started))
		_settle(name, token, status, str(exc) if known else GENERIC_FAILURE)
	finally:
		# Every step is isolated: a stuck watchdog or a lock timeout must neither fail the RQ
		# job nor skip dispatch(), which would leave queued exports waiting for the reaper.
		if watchdog and watchdog.thread.is_alive():
			_safely("tail", name, watchdog.stop)
		if db:
			_safely("tail", name, db.close)
		_safely("tail", name, _remove_directory, directory)
		if not handed_back:
			_safely("tail", name, _record_telemetry, name)
		_after(name)


def _elapsed(started):
	return round(time.monotonic() - started, 1)


def _settle(name, token, status, error):
	"""``_finish`` that cannot raise. A lost terminal write is recovered by the reaper."""
	try:
		_finish(name, token, status, error)
	except Exception as exc:
		_log_failure(name, "finish", exc)


def _remove_directory(directory):
	if directory.exists():
		shutil.rmtree(directory)


def _record_telemetry(name):
	if frappe.db.exists(DOCTYPE, name):
		from jarvis.telemetry import record_export_event

		finished = _get_job(name)
		record_export_event(
			"export_query",
			finished.format,
			finished.rows_written or 0,
			mode="background",
			outcome=finished.status,
		)


def _publish_file(job, token, path, result, permission_key):
	from jarvis.tools._export.envelope import _safe_base

	# The folder is made first, in its own transaction, so no lock is held while it is created.
	# A folder that cannot be made files the export under Attachments; it never fails the export.
	folder = EXPORTS_FOLDER if _safely("exports folder", job.name, ensure_exports_folder) else None
	# Read current deletion state before locking publication.
	_commit()
	# Conversation deletion takes this same row lock before its trash hooks, so lock the
	# chat first: a deleted chat cancels the export instead of publishing into the void.
	conversation = frappe.db.get_value(DOCTYPE, job.name, "conversation")
	if not conversation or not frappe.db.get_value(
		"Jarvis Conversation", conversation, "name", for_update=True
	):
		frappe.db.rollback()
		_finish(job.name, token, "cancelled", "The export's conversation was deleted.")
		return
	row = frappe.db.get_value(
		DOCTYPE, job.name, ["attempt_token", "status", "cancel_requested"], as_dict=True, for_update=True
	)
	if not row or row.attempt_token != token or row.status != "running" or row.cancel_requested:
		frappe.db.rollback()
		_finish(job.name, token, "cancelled", "Export cancelled.")
		return
	# A short display suffix must never overwrite an existing file on collision.
	# Both paths are on the site's private filesystem; link is atomic and exclusive.
	while True:
		filename = f"jarvis-export-{_safe_base(job.title)}-{secrets.token_hex(3)}.{job.format}"
		target = Path(frappe.get_site_path("private", "files", filename))
		try:
			os.link(path, target)
			break
		except FileExistsError:
			continue
	size = path.stat().st_size
	path.unlink()
	try:
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"is_private": 1,
				"file_url": "/private/files/" + filename,
				"file_size": size,
				"owner": job.owner,
				"attached_to_doctype": DOCTYPE,
				"attached_to_name": job.name,
				**({"folder": folder} if folder else {}),
			}
		)
		# Supported on both release lines: validate the prewritten file normally,
		# but do not load/rehash/deduplicate it or apply the unrelated upload cap.
		file.flags.copy_from_existing_file = True
		file.insert(ignore_permissions=True)
		days = retention_days()
		frappe.db.set_value(
			DOCTYPE,
			job.name,
			{
				**result,
				"status": "ready",
				"file": file.name,
				"size_bytes": size,
				"finished_at": now_datetime(),
				"expires_at": add_to_date(now_datetime(), days=days) if days else None,
				"permission_fingerprint": permission_key,
				"error": "",
			},
		)
		# Publish File and ready state atomically before any delivery transaction.
		_commit()
	except Exception:
		frappe.db.rollback()
		target.unlink(missing_ok=True)
		raise


def _finish(name, token, status, error):
	frappe.db.rollback()
	row = frappe.db.get_value(
		DOCTYPE,
		name,
		["attempt_token", "status", "retries", "cancel_requested"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.attempt_token != token or row.status != "running":
		frappe.db.rollback()
		return
	if row.cancel_requested:
		status, error = "cancelled", "Export cancelled."
	values = {"status": status, "error": error, "finished_at": now_datetime()}
	if status == "paused":
		if row.retries:
			values.update(
				status="failed",
				error="The export was interrupted again after its one retry. Please run it again.",
			)
		else:
			values.update(retries=1, next_attempt=add_to_date(now_datetime(), minutes=5), finished_at=None)
	frappe.db.set_value(DOCTYPE, name, values)
	# Persist the fenced terminal/retry state before notification and dispatch.
	_commit()


@_rollback_on_error()
def announce(name, initial=False, recovery=False):
	"""Deliver the card or a state message to the job's own chat. Idempotent."""
	if not frappe.db.exists(DOCTYPE, name):
		return
	job = _get_job(name)
	if job.status == "expired":
		# Expiry is routine: no chat message, no bell. The card shows its own "Expired" state.
		return
	if not initial and not recovery and job.status not in (*TERMINAL, "paused"):
		return
	_announce_one(name, initial=initial, recovery=recovery)


def _message_text(job, initial):
	if initial:
		return f"Export started: {job.title}."
	if job.status == "ready":
		return f"{job.title} is ready ({(job.rows_written or 0):,} rows)."
	return job.error or f"{job.title} ended."


@_rollback_on_error()
def _announce_one(name, *, initial, recovery):
	from jarvis._session import impersonate
	from jarvis.api import _locked_insert_chat_message
	from jarvis.chat.events import publish_to_user

	# Start a fresh snapshot before locking the conversation and job for delivery.
	_commit()
	# All delivery and deletion paths lock conversation then job.
	conversation = frappe.db.get_value(DOCTYPE, name, "conversation")
	owner = conversation and frappe.db.get_value(
		"Jarvis Conversation", conversation, "owner", for_update=True
	)
	if not owner or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.db.rollback()
		return
	job = _get_job(name)
	if owner != job.owner:
		frappe.db.rollback()
		frappe.throw("Export chat not found", frappe.PermissionError)
	if not initial and not recovery and job.status not in (*TERMINAL, "paused"):
		frappe.db.rollback()
		return
	key = f"export:{name}:{'card' if initial else 'retry' if recovery else job.status}"
	content = _message_text(job, initial)
	with impersonate(job.owner):
		message = _locked_insert_chat_message(
			conversation,
			{
				"role": "assistant",
				"content": content,
				"tool_call_id": key,
				"tool_result": json.dumps({"ok": True, "data": public(job)}, default=str),
				"ref_doctype": DOCTYPE,
				"ref_name": name,
				"hidden": 0,
			},
			dedupe_filters={"conversation": conversation, "role": "assistant", "tool_call_id": key},
		)
		if initial and message:
			frappe.db.set_value(DOCTYPE, name, "card_message", message)
		if not initial and not recovery:
			if message:
				_notify_owner(job, content)
			_refresh_card_summary(job, conversation)
			# Set with the message, inserted or already there: the reaper re-announces any
			# row whose marker differs from its status, so a dedupe hit must still settle it.
			frappe.db.set_value(DOCTYPE, name, "announced_status", job.status)
		# Commit the message, bell and marker together before realtime publication.
		_commit()
	if message:
		publish_to_user(
			job.owner,
			{
				"kind": "export:changed",
				"conversation_id": conversation,
				"export_job": name,
				"message_id": message,
				"status": job.status,
				"title": job.title,
				"initial": bool(initial),
			},
		)


def _notify_owner(job, subject):
	# Serialized on the job row: one owner bell per state.
	if not frappe.db.exists("User", job.owner) or frappe.db.exists(
		"Notification Log", {"document_type": DOCTYPE, "document_name": job.name, "subject": subject}
	):
		return
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": job.owner,
			"from_user": job.owner,
			"type": "Alert",
			"subject": subject,
			"document_type": DOCTYPE,
			"document_name": job.name,
		}
	).insert(ignore_permissions=True)


def _refresh_card_summary(job, conversation):
	"""The card keeps a durable summary for the day the job row is gone."""
	card = frappe.db.get_value(
		"Jarvis Chat Message",
		{"conversation": conversation, "role": "assistant", "tool_call_id": f"export:{job.name}:card"},
		"name",
	)
	if card:
		frappe.db.set_value(
			"Jarvis Chat Message",
			card,
			"tool_result",
			json.dumps(
				{
					"ok": True,
					"data": {
						"export_job": job.name,
						"title": job.title,
						"status": job.status,
						"rows_written": job.rows_written,
						"finished_at": public(job)["finished_at"],
					},
				}
			),
		)


def reap():
	"""Five-minute recovery, expiry, notification reconciliation, orphan sweep."""
	if frappe.db.db_type != "mariadb" or not frappe.db.table_exists(DOCTYPE):
		return
	try:
		old = add_to_date(frappe.utils.now_datetime(), days=-ROW_RETENTION_DAYS)
		for name in _reap_worklist(old):
			_reap_row(name, old)
		# A deletion transaction may have started before a just-published File became
		# visible in its repeatable-read snapshot. Sweep registered orphans too.
		_safely("orphan sweep", "reaper", _sweep_orphan_files, doctype=None)
		_safely("scratch sweep", "reaper", _sweep_attempt_directories, doctype=None)
		# A process can die between rename and the File/ready transaction committing.
		_safely("straggler sweep", "reaper", _sweep_stragglers, doctype=None)
	finally:
		_safely("dispatch", "reaper", dispatch, doctype=None)


def _reap_worklist(old):
	# Each worklist is bounded independently so old rows cannot starve live jobs.
	names = set()
	now = now_datetime()
	j = frappe.qb.DocType(DOCTYPE)
	for condition in (
		j.status == "running",
		j.status.isin(["queued", "paused"]) & (Coalesce(j.card_message, "") == ""),
		# Expiry is quiet: an expired job is never announced.
		j.status.notin(["queued", "running", "expired"]) & (Coalesce(j.announced_status, "") != j.status),
		(j.status == "ready") & _past_expiry(j, now),
		j.status.isin(list(AGED_OUT)) & (j.creation < old),
		# Counted from when it expired: counting from creation would delete it in the pass that
		# expires it, and the card's "Expired" state would never show.
		(j.status == "expired") & (j.expires_at < _expired_cutoff()),
	):
		names.update(
			r[0]
			for r in frappe.qb.from_(j).select(j.name).where(condition).orderby(j.creation).limit(200).run()
		)
	return names


def _expired_cutoff():
	# expires_at is naive UTC like every lifecycle stamp; creation is site time (see reap()).
	return add_to_date(now_datetime(), days=-ROW_RETENTION_DAYS)


def _past_expiry(j, now):
	"""SQL twin of ``effective_expiry``: stored date passed, or older than a lowered setting."""
	days = retention_days()
	passed = j.expires_at < now
	return passed | (j.finished_at < add_to_date(now, days=-days)) if days else passed


def _reap_row(name, old):
	"""Isolated steps: a row whose recovery or announce keeps failing is still purged at 30 days."""
	_safely("recovery", name, _recover_row, name)
	_safely("retention", name, _purge_if_old, name, old)
	_safely("announce", name, _announce_if_present, name)


def _recover_row(name):
	from frappe.utils.background_jobs import get_job

	job = _get_job(name)
	if not job.card_message:
		announce(job.name, initial=True)
	if job.status == "running" and get_datetime(job.heartbeat) < add_to_date(now_datetime(), minutes=-1):
		rq_job = get_job("export-" + job.attempt_token)
		if not job.started_at and rq_job and str(rq_job.get_status()).lower().split(".")[-1] == "queued":
			return
		with _lock():
			# Serialize with final File publication, which fences on this row.
			# A plain reload followed by UPDATE could overwrite a just-ready job.
			if not frappe.db.get_value(DOCTYPE, job.name, "name", for_update=True):
				return
			job = _get_job(job.name)
			if job.status != "running" or get_datetime(job.heartbeat) >= add_to_date(
				now_datetime(), minutes=-1
			):
				return
			status = "cancelled" if job.cancel_requested else ("failed" if job.retries else "queued")
			frappe.db.set_value(
				DOCTYPE,
				job.name,
				{
					"status": status,
					"attempt_token": "",
					"retries": 1,
					"next_attempt": now_datetime(),
					"finished_at": now_datetime() if status != "queued" else None,
					"error": "Export cancelled."
					if job.cancel_requested
					else "The export worker stopped. "
					+ ("Retrying once." if status == "queued" else "Please run it again."),
				},
			)
			# Revoke the old attempt durably before deleting its files or retrying.
			_commit()
		if job.attempt_token:
			shutil.rmtree(_directory(job.name, job.attempt_token), ignore_errors=True)
		job = _get_job(job.name)
		if job.status == "queued":
			announce(job.name, recovery=True)
	expiry = effective_expiry(job)
	if job.status == "ready" and expiry and expiry < now_datetime():
		_delete_file(job)
		# Quiet: no chat message and no bell. expires_at records when it expired, which also
		# starts the 30 day row retention. The marker stops the reaper looking at it again.
		frappe.db.set_value(
			DOCTYPE,
			job.name,
			{"status": "expired", "file": None, "expires_at": expiry, "announced_status": "expired"},
		)
		# Persist expiry and run after-commit file cleanup.
		_commit()


def _purge_if_old(name, old):
	if not frappe.db.exists(DOCTYPE, name):
		return
	job = _get_job(name)
	if _row_is_old(job, old):
		_delete_file(job)
		frappe.db.delete(DOCTYPE, {"name": job.name})
		# Persist retention deletion and run its after-commit file cleanup.
		_commit()


def _row_is_old(job, old):
	if job.status == "expired":
		return bool(job.expires_at) and get_datetime(job.expires_at) < _expired_cutoff()
	return job.status in AGED_OUT and get_datetime(job.creation) < old


def _announce_if_present(name):
	if frappe.db.exists(DOCTYPE, name):
		announce(name)


def _sweep_orphan_files():
	# End the sweep snapshot so concurrently published orphan Files are visible.
	_commit()
	f = frappe.qb.DocType("File")
	j = frappe.qb.DocType(DOCTYPE)
	orphans = (
		frappe.qb.from_(f)
		.left_join(j)
		.on(j.name == f.attached_to_name)
		.select(f.name)
		.where((f.attached_to_doctype == DOCTYPE) & j.name.isnull())
		.limit(200)
	).run()
	for (file_name,) in orphans:
		_safely("orphan file", file_name, _delete_orphan, file_name, doctype="File")


def _delete_orphan(file_name):
	_delete_generated_file(file_name)
	# One commit per file: a failure must not undo the deletes before it.
	_commit()


def _sweep_attempt_directories():
	root = Path(frappe.get_site_path("private", "jarvis_exports"))
	if root.exists():
		for directory in root.iterdir():
			_safely("scratch directory", directory.name, _remove_stale_directory, directory, doctype=None)


def _remove_stale_directory(directory):
	# These race with run()'s own cleanup, so a vanished entry is not an error.
	with contextlib.suppress(FileNotFoundError):
		if directory.is_dir() and directory.stat().st_mtime < time.time() - 900:
			name, _, token = directory.name.partition("-")
			if not _attempt(name, token):
				shutil.rmtree(directory)


def _sweep_stragglers():
	for path in Path(frappe.get_site_path("private", "files")).glob("jarvis-export-*.*"):
		_safely("straggler file", path.name, _remove_straggler, path, doctype=None)


def _remove_straggler(path):
	with contextlib.suppress(FileNotFoundError):
		if path.stat().st_mtime < time.time() - 900 and not frappe.db.exists(
			"File", {"file_url": "/private/files/" + path.name}
		):
			path.unlink()


def _delete_file(job):
	if job.file and frappe.db.exists("File", job.file):
		frappe.db.set_value(DOCTYPE, job.name, "file", None)
		_delete_generated_file(job.file)


def _delete_generated_file(name):
	row = frappe.db.get_value("File", name, FILE_FIELDS, as_dict=True)
	if not row:
		return  # already gone (a concurrent sweep or a deleted chat)
	file = frappe.get_doc({"doctype": "File", **row})
	path = Path(file.get_full_path()).resolve()
	frappe.delete_doc("File", name, ignore_permissions=True)
	# Prewritten Files intentionally have no content hash. Frappe's normal trash
	# hook only removes bytes when a hash proves they are unshared. Our unique URL
	# supplies that check without reading the whole generated file into memory.
	if (
		file.is_private
		and path.parent == Path(frappe.get_site_path("private", "files")).resolve()
		and path.name.startswith("jarvis-export-")
		and not frappe.db.exists("File", {"file_url": file.file_url})
	):
		frappe.db.after_commit.add(lambda: path.unlink(missing_ok=True))


def on_conversation_trash(doc, method=None):
	# A job belongs to one chat: deleting the chat revokes the attempt and removes its files.
	# The hook can run before the DocType is migrated in.
	if not frappe.db.table_exists(DOCTYPE):
		return
	for row in frappe.get_all(DOCTYPE, filters={"conversation": doc.name}, fields=["name"]):
		job = _get_job(row.name, for_update=True)
		_delete_file(job)
		if job.attempt_token:
			shutil.rmtree(_directory(job.name, job.attempt_token), ignore_errors=True)
		frappe.db.delete(DOCTYPE, {"name": job.name})
