"""Durable background exports. All state transitions are fenced by an attempt token."""

import contextlib
import importlib.util
import json
import os
import secrets
import shutil
import time
from datetime import timezone
from pathlib import Path

import frappe
from frappe.query_builder.functions import Coalesce
from frappe.utils import add_to_date, cint, get_datetime
from pypika.terms import ExistsCriterion

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.clock import now_datetime
from jarvis.tools._export.saved_queries import fingerprint
from jarvis.tools._export.streaming import ExportLimit, prepare

DOCTYPE = "Jarvis Export Job"
ACTIVE = ("queued", "running", "paused")
TERMINAL = ("ready", "too large", "too slow", "failed", "cancelled", "expired")
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
]


JOB_FIELDS = STATE_FIELDS + [
	"conversation",
	"source",
	"highlights",
	"permission_fingerprint",
	"attempt_token",
	"heartbeat",
	"attempts",
	"retries",
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


def limits():
	return (
		max(1, min(200_000, int(frappe.conf.get("jarvis_export_row_limit", 200_000)))),
		max(1, int(frappe.conf.get("jarvis_export_max_mb", 50))) * 1024 * 1024,
	)


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


def _conversation():
	from jarvis.tools._agent_run_ctx import get_session_key

	key = get_session_key()
	if not key:
		raise InvalidArgumentError("Start a large export from a Jarvis chat so its file can be delivered.")
	name = frappe.db.get_value(
		"Jarvis Conversation", {"session_key": key, "owner": frappe.session.user}, "name"
	)
	if not name:
		raise InvalidArgumentError("The export needs a current chat owned by you. Start it again in chat.")
	return name


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


def existing(source, fmt, highlights=None, *, join=False):
	"""Revalidate access before returning either active state or a recent private file."""
	# Reuse is optional. Users without job access can still use immediate exports
	# under the source DocTypes' read/export permissions.
	if not frappe.has_permission(DOCTYPE, "read"):
		return None
	rows = frappe.get_list(
		DOCTYPE,
		filters={
			"owner": frappe.session.user,
			"fingerprint": _key(source, fmt, highlights),
			"status": ["in", [*ACTIVE, "ready"]],
		},
		fields=["name"],
		order_by="creation desc",
		limit=10,
	)
	if not rows:
		return None
	prepared = prepare(source, limits()[0], highlights)
	permission_key = _permission_fingerprint(prepared)
	for row in rows:
		job = _get_job(row.name)
		if job.status in ACTIVE:
			if join:
				_join(job.name)
			return public(job)
		if (
			job.permission_fingerprint == permission_key
			and job.finished_at
			and get_datetime(job.finished_at) >= add_to_date(now_datetime(), minutes=-10)
			and _file_exists(job)
		):
			if join:
				_join(job.name)
			return public(job)
	return None


def _join(name):
	# A persistent initial card is also the subscription to this shared job.
	announce(name, initial=True, conversation=_conversation())
	announce(name)


def enqueue(source, fmt, title=None, highlights=None):
	_supported(fmt)
	frappe.has_permission(DOCTYPE, "read", throw=True)
	conversation = _conversation()
	prepared = prepare(source, limits()[0], highlights)
	with _lock():
		found = existing(source, fmt, highlights, join=True)
		if found:
			return found
		job = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"owner": frappe.session.user,
				"conversation": conversation,
				"title": (title or "Export")[:140],
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
	announce(job.name, initial=True)
	dispatch()
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


def public(job, files=None):
	def iso(value):
		return get_datetime(value).replace(tzinfo=timezone.utc).isoformat() if value else None

	data = {
		"export_job": job.name,
		"title": job.title,
		"status": job.status,
		"rows_written": job.rows_written or 0,
		"started_at": iso(job.started_at),
		"finished_at": iso(job.finished_at),
		"expires_at": iso(job.expires_at),
		"next_attempt": iso(job.next_attempt),
		"cancel_requested": bool(job.cancel_requested),
		"error": job.error,
		"format": job.format,
		"size_bytes": job.size_bytes or 0,
		"limited_by_spec": job.limited_by_spec or None,
		"cells_truncated": bool(job.cells_truncated),
		"note": "The file will appear in this chat. Use the card to cancel; repeat export_query to check its state.",
	}
	if job.status == "ready" and job.expires_at and get_datetime(job.expires_at) <= now_datetime():
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
	announce(name)
	if job.status != "running" or not job.started_at:
		dispatch()
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
			# Reserve before enqueue. A failed enqueue is recovered by the reaper.
			# Persist the capacity reservation before RQ can start this attempt.
			_commit()
			frappe.enqueue(
				"jarvis.tools._export.jobs.run",
				queue="long",
				timeout=900,
				job_id="export-" + token,
				name=row.name,
				token=token,
			)
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


def run(name, token):
	from jarvis._session import impersonate
	from jarvis.tools._export.database import MariaDBStream, Watchdog, connection_settings
	from jarvis.tools._export.streaming import write

	if not _attempt(name, token):
		_finish(name, token, "cancelled", "Export cancelled.")
		announce(name)
		return
	job = _get_job(name)
	directory = _directory(name, token)
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
				raise ExportLimit("failed", "No records match your current permissions and filters.")
			_publish_file(job, token, path, result, _permission_fingerprint(prepared))
	except ExportLimit as exc:
		_finish(name, token, exc.status, str(exc))
	except Exception as exc:
		from jarvis import _failure_kind
		from jarvis.exceptions import PermissionDeniedError

		status = "too slow" if _failure_kind.too_slow(exc) else "failed"
		message = (
			str(exc)
			if isinstance(exc, (PermissionDeniedError, InvalidArgumentError))
			else ("The export could not finish. Please run it again or narrow the filters.")
		)
		_finish(name, token, status, message)
		frappe.log_error(title="Background export failed", message=frappe.get_traceback())
	finally:
		if watchdog and watchdog.thread.is_alive():
			watchdog.stop()
		if db:
			db.close()
		if directory.exists():
			shutil.rmtree(directory)
		if not handed_back and frappe.db.exists(DOCTYPE, name):
			from jarvis.telemetry import record_export_event

			finished = _get_job(name)
			record_export_event(
				"export_query",
				finished.format,
				finished.rows_written or 0,
				mode="background",
				outcome=finished.status,
			)
		announce(name)
		dispatch()


def _publish_file(job, token, path, result, permission_key):
	from jarvis.tools._export.envelope import _safe_base

	# Read current subscription/deletion state before locking publication.
	_commit()
	# Conversation deletion takes this same row lock before its trash hooks.
	# Serialize publication with deletion before locking the job itself.
	# A deleted original chat can have transferred this shared job to another
	# subscriber while the worker was streaming. Read the current destination.
	while True:
		conversation = frappe.db.get_value(DOCTYPE, job.name, "conversation")
		if not conversation:
			frappe.db.rollback()
			return
		if frappe.db.get_value("Jarvis Conversation", conversation, "name", for_update=True):
			break
		frappe.db.rollback()
		if frappe.db.get_value(DOCTYPE, job.name, "conversation") == conversation:
			_finish(job.name, token, "cancelled", "The export's conversation was deleted.")
			return
		# End the stale lookup snapshot before retrying a transferred conversation.
		_commit()
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
			}
		)
		# Supported on both release lines: validate the prewritten file normally,
		# but do not load/rehash/deduplicate it or apply the unrelated upload cap.
		file.flags.copy_from_existing_file = True
		file.insert(ignore_permissions=True)
		frappe.db.set_value(
			DOCTYPE,
			job.name,
			{
				**result,
				"status": "ready",
				"file": file.name,
				"size_bytes": size,
				"finished_at": now_datetime(),
				"expires_at": add_to_date(now_datetime(), days=1),
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


def _cards(name, *, for_update=False):
	m = frappe.qb.DocType("Jarvis Chat Message")
	c = frappe.qb.DocType("Jarvis Conversation")
	j = frappe.qb.DocType(DOCTYPE)
	query = (
		frappe.qb.from_(m)
		.inner_join(c)
		.on(c.name == m.conversation)
		.inner_join(j)
		.on((j.name == m.ref_name) & (j.owner == c.owner))
		.select(m.name, m.conversation)
		.where(
			(m.ref_doctype == DOCTYPE)
			& (m.ref_name == name)
			& (m.role == "assistant")
			& (m.tool_call_id == f"export:{name}:card")
		)
		.orderby(m.creation, m.name)
	)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


@_rollback_on_error()
def announce(name, initial=False, recovery=False, conversation=None):
	"""Each participating chat receives a card and idempotent state messages."""
	if not frappe.db.exists(DOCTYPE, name):
		return
	job = _get_job(name)
	if not initial and not recovery and job.status not in (*TERMINAL, "paused"):
		return
	conversations = (
		[conversation or job.conversation]
		if initial
		else list(dict.fromkeys([job.conversation, *(card.conversation for card in _cards(name))]))
	)
	for destination in conversations:
		_announce_one(name, destination, initial=initial, recovery=recovery)
	if not initial:
		# Concurrent transitions and new subscriptions must remain recoverable.
		# Checking delivered cards in the UPDATE closes the subscribe/fanout race.
		j = frappe.qb.DocType(DOCTYPE)
		card = frappe.qb.DocType("Jarvis Chat Message").as_("card")
		delivered = frappe.qb.DocType("Jarvis Chat Message").as_("delivered")
		c = frappe.qb.DocType("Jarvis Conversation")
		delivery = (
			frappe.qb.from_(delivered)
			.select(delivered.name)
			.where(
				(delivered.conversation == card.conversation)
				& (delivered.role == "assistant")
				& (delivered.tool_call_id == f"export:{name}:{'retry' if recovery else job.status}")
			)
		)
		pending = (
			frappe.qb.from_(card)
			.inner_join(c)
			.on(c.name == card.conversation)
			.where(c.owner == j.owner)
			.select(card.name)
			.where(
				(card.ref_name == j.name)
				& (card.ref_doctype == DOCTYPE)
				& (card.tool_call_id == f"export:{name}:card")
				& (card.role == "assistant")
			)
			.where(~ExistsCriterion(delivery))
		)
		(
			frappe.qb.update(j)
			.set(j.announced_status, job.status)
			.where((j.name == name) & (j.status == job.status))
			.where(~ExistsCriterion(pending))
		).run()
		# Persist delivery reconciliation so the independent reaper sees it.
		_commit()


@_rollback_on_error()
def _announce_one(name, conversation, *, initial, recovery):
	from jarvis._session import impersonate
	from jarvis.api import _locked_insert_chat_message
	from jarvis.chat.events import publish_to_user

	# Start a fresh snapshot before locking the conversation and job for delivery.
	_commit()
	# All delivery, subscription and deletion paths lock conversation then job.
	owner = frappe.db.get_value("Jarvis Conversation", conversation, "owner", for_update=True)
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
	content = (
		"Your export is ready."
		if job.status == "ready"
		else "Preparing your export."
		if initial
		else job.error or "This export has expired; ask again."
	)
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
		if initial and message and conversation == job.conversation:
			frappe.db.set_value(DOCTYPE, name, "card_message", message)
		if initial and message and job.status in (*TERMINAL, "paused"):
			# Joining a finished job must remain recoverable if delivery stops after
			# the new subscription commits but before its completion message.
			frappe.db.set_value(DOCTYPE, name, "announced_status", "")
		if not initial:
			# Serialized on the job row: one owner bell even with many chat deliveries.
			if message and not frappe.db.exists(
				"Notification Log",
				{
					"document_type": DOCTYPE,
					"document_name": name,
					"subject": content,
				},
			):
				frappe.get_doc(
					{
						"doctype": "Notification Log",
						"for_user": job.owner,
						"from_user": job.owner,
						"type": "Alert",
						"subject": content,
						"document_type": DOCTYPE,
						"document_name": name,
					}
				).insert(ignore_permissions=True)
			card = frappe.db.get_value(
				"Jarvis Chat Message",
				{
					"conversation": conversation,
					"role": "assistant",
					"tool_call_id": f"export:{name}:card",
				},
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
								"export_job": name,
								"title": job.title,
								"rows_written": job.rows_written,
								"finished_at": public(job)["finished_at"],
							},
						}
					),
				)
		# Commit the message, bell and subscription together before realtime publication.
		_commit()
	if message:
		publish_to_user(
			job.owner,
			{
				"kind": "export:changed",
				"conversation_id": conversation,
				"export_job": name,
				"message_id": message,
			},
		)


def reap():
	"""Five-minute recovery, expiry, notification reconciliation, orphan sweep."""
	if frappe.db.db_type != "mariadb" or not frappe.db.table_exists(DOCTYPE):
		return
	from frappe.utils.background_jobs import get_job

	# Each worklist is bounded independently so old rows cannot starve live jobs.
	names = set()
	cutoffs = {"now": now_datetime(), "old": add_to_date(frappe.utils.now_datetime(), days=-30)}
	j = frappe.qb.DocType(DOCTYPE)
	for condition in (
		j.status == "running",
		j.status.isin(["queued", "paused"]) & (Coalesce(j.card_message, "") == ""),
		j.status.notin(["queued", "running"]) & (Coalesce(j.announced_status, "") != j.status),
		(j.status == "ready") & (j.expires_at < cutoffs["now"]),
		j.status.notin(list(ACTIVE)) & (j.creation < cutoffs["old"]),
	):
		names.update(
			r[0]
			for r in frappe.qb.from_(j).select(j.name).where(condition).orderby(j.creation).limit(200).run()
		)
	rows = [frappe._dict(name=name) for name in names]
	for row in rows:
		try:
			job = _get_job(row.name)
			if not job.card_message:
				announce(job.name, initial=True)
			if job.status == "running" and get_datetime(job.heartbeat) < add_to_date(
				now_datetime(), minutes=-1
			):
				rq_job = get_job("export-" + job.attempt_token)
				if (
					not job.started_at
					and rq_job
					and str(rq_job.get_status()).lower().split(".")[-1] == "queued"
				):
					continue
				with _lock():
					# Serialize with final File publication, which fences on this row.
					# A plain reload followed by UPDATE could overwrite a just-ready job.
					if not frappe.db.get_value(DOCTYPE, job.name, "name", for_update=True):
						continue
					job = _get_job(job.name)
					if job.status != "running" or get_datetime(job.heartbeat) >= add_to_date(
						now_datetime(), minutes=-1
					):
						continue
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
			if job.status == "ready" and job.expires_at and get_datetime(job.expires_at) < now_datetime():
				_delete_file(job)
				frappe.db.set_value(DOCTYPE, job.name, {"status": "expired", "file": None})
				# Persist expiry and run after-commit file cleanup before notifying the owner.
				_commit()
				job = _get_job(job.name)
			announce(job.name)
			if job.status in TERMINAL and get_datetime(job.creation) < cutoffs["old"]:
				_delete_file(job)
				frappe.db.delete(DOCTYPE, {"name": job.name})
				# Persist retention deletion and run its after-commit file cleanup.
				_commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Export recovery failed", message=frappe.get_traceback())
	# A deletion transaction may have started before a just-published File became
	# visible in its repeatable-read snapshot. Sweep registered orphans too.
	# End the sweep snapshot so concurrently published orphan Files are visible.
	_commit()
	f = frappe.qb.DocType("File")
	for (file_name,) in (
		frappe.qb.from_(f)
		.left_join(j)
		.on(j.name == f.attached_to_name)
		.select(f.name)
		.where((f.attached_to_doctype == DOCTYPE) & j.name.isnull())
		.limit(200)
	).run():
		_delete_generated_file(file_name)
	# Commit orphan deletion and execute its after-commit filesystem cleanup.
	_commit()
	root = Path(frappe.get_site_path("private", "jarvis_exports"))
	if root.exists():
		for directory in root.iterdir():
			if directory.is_dir() and directory.stat().st_mtime < time.time() - 900:
				name, _, token = directory.name.partition("-")
				if not _attempt(name, token):
					shutil.rmtree(directory)
	# A process can die between rename and the File/ready transaction committing.
	for path in Path(frappe.get_site_path("private", "files")).glob("jarvis-export-*.*"):
		if path.stat().st_mtime < time.time() - 900 and not frappe.db.exists(
			"File", {"file_url": "/private/files/" + path.name}
		):
			path.unlink()
	dispatch()


def _delete_file(job):
	if job.file and frappe.db.exists("File", job.file):
		frappe.db.set_value(DOCTYPE, job.name, "file", None)
		_delete_generated_file(job.file)


def _delete_generated_file(name):
	file = frappe.get_doc({"doctype": "File", **frappe.db.get_value("File", name, FILE_FIELDS, as_dict=True)})
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
	# Cards are subscriptions. Removing the last chat revokes the attempt;
	# otherwise transfer the primary delivery address to a surviving chat.
	for row in frappe.get_all(DOCTYPE, filters={"conversation": doc.name}, fields=["name"]):
		job = _get_job(row.name, for_update=True)
		remaining = [card for card in _cards(job.name, for_update=True) if card.conversation != doc.name]
		if remaining:
			frappe.db.set_value(
				DOCTYPE,
				job.name,
				{
					"conversation": remaining[0].conversation,
					"card_message": remaining[0].name,
					"announced_status": "",
				},
			)
			continue
		_delete_file(job)
		if job.attempt_token:
			shutil.rmtree(_directory(job.name, job.attempt_token), ignore_errors=True)
		frappe.db.delete(DOCTYPE, {"name": job.name})
