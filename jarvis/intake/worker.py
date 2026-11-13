"""Document Intake: the background job behind one Queued Activity row.

Stages: guard -> limits -> slot -> read (skipped when a reading is saved) ->
``draft.finish``. One model call per file, ever: the reading is saved before the
draft stage, so a retry after a draft failure costs nothing. There are no sleeps
or polling loops: a job with no free slot leaves its row Queued, and every
finished job (plus the 5-minute ``pump``) starts the next waiting row.
"""

import json
import re
import traceback

import frappe
from frappe.utils import add_to_date, cint, now_datetime, nowdate
from frappe.utils.background_jobs import is_job_enqueued

from jarvis import _redis_lock, compat
from jarvis._session import impersonate
from jarvis.intake import _db, capture, draft, notify, reader, spec
from jarvis.triggers import llm_action
from jarvis.triggers import llm_task_client as lt

ACTIVITY = "Jarvis Trigger Activity"
SLOTS = 2
# Longer than the slowest job (90 s read plus draft), so a crashed holder frees itself.
_SLOT_TTL_S = 360
_DEFAULT_SITE_CAP = 300
_PUMP_AGE_MINUTES = 2
_STUCK_MINUTES = 10
_MAX_ATTEMPTS = 3
# Waiting rows looked at beyond the free slots, so rows already in the queue can be skipped.
_CHAIN_LOOKAHEAD = 10

NO_IMAGE_SUMMARY = "This site's AI model can't read images for intake yet."
_READ_FAILED_SUMMARY = "Reading the file failed. Managers can see the details in this activity."
_DRAFT_FAILED_SUMMARY = "Creating the draft failed. Managers can see the details in this activity."


def _reason(e: Exception) -> str:
	"""Short, safe text for ``detail``: class name plus a trimmed message with file paths
	cut out. The uploader can open their own row, so no traceback or long raw text goes on it;
	the full traceback is in the Error Log."""
	text = re.sub(r"(?:/[\w.\-]+)+", "", str(e)).strip()
	return f"{type(e).__name__}: {text}"[:200]


def _log(e: Exception) -> str:
	"""Send the traceback to the Error Log and return the short reason for the row."""
	frappe.log_error(title="jarvis.intake.worker", message=traceback.format_exc())
	return _reason(e)


def slot_keys() -> list[str]:
	return [f"jarvis:intake:slot:{i}" for i in range(SLOTS)]


def site_cap_key() -> str:
	return f"jarvis:intakecap:{nowdate().replace('-', '')}"


def _site_cap() -> int:
	try:
		cap = cint(frappe.db.get_single_value("Jarvis Settings", "intake_daily_cap"))
	except Exception:
		cap = 0
	return cap if cap > 0 else _DEFAULT_SITE_CAP


def _set(row, **values) -> None:
	"""Save a status change and publish it: the SPA polls this row while the job runs."""
	row.db_set(values)
	if values.get("status") in ("Failed", "Skipped"):
		# The uploader may have no Jarvis access: the bell is how they learn of it.
		notify.row_ended(row, values.get("summary") or row.summary or "")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the polling UI must see each stage


def _claim_slot() -> str | None:
	for key in slot_keys():
		if _redis_lock.claim(key, _SLOT_TTL_S):
			return key
	return None


def _release_slot(key: str) -> None:
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(key))
	except Exception:
		pass  # the TTL is the backstop


def _enqueue(activity: str) -> None:
	frappe.enqueue(
		"jarvis.intake.worker.run_job",
		queue="default",
		job_id=f"intake:{activity}",
		deduplicate=True,
		activity=activity,
	)


def _free_slots() -> int:
	cache = frappe.cache()
	return sum(1 for key in slot_keys() if not cache.exists(key))


def _enqueue_next(finished: str) -> None:
	"""A slot just freed: start as many of the oldest waiting rows as there are free
	slots, so a burst of uploads keeps every stream busy (one row per finished job
	would let a 100-file burst fall back to a single stream). A row whose job is already
	in the queue is skipped: enqueueing it again is a no-op, and it would eat the turn."""
	try:
		free = _free_slots()
		if not free:
			return
		# System context: a background job picks the next rows whoever uploaded them.
		waiting = frappe.get_all(
			ACTIVITY,
			filters={"action_type": "Intake", "status": "Queued", "name": ["!=", finished]},
			pluck="name",
			order_by="creation asc",
			limit=free + _CHAIN_LOOKAHEAD,
		)
		for name in waiting:
			if free <= 0:
				break
			if is_job_enqueued(f"intake:{name}"):
				continue
			_enqueue(name)
			free -= 1
	except Exception:
		frappe.log_error(title="Jarvis Intake: could not start the next file")


def _over_cap(trigger: str, trigger_cap: int) -> bool:
	return (
		llm_action.daily_count(trigger) >= trigger_cap
		or cint(compat.cache_get_fresh(f"{site_cap_key()}:count")) >= _site_cap()
	)


def _count_call(trigger: str) -> None:
	llm_action.bump(llm_action.cap_key(trigger))
	llm_action.bump(site_cap_key())


def run_job(activity: str) -> None:
	"""Run one Queued Intake row. Expected failures end on the row, never in RQ.

	A queued job runs as whoever enqueued it (the uploader, an owner retrying, the
	scheduler). The system bookkeeping here (status writes, notices, the review request)
	must not depend on that, so it runs as Administrator. The ERP reads and the draft
	write still switch to the uploader inside ``draft`` (never as Administrator)."""
	with impersonate("Administrator"):
		_run_job(activity)


def _run_job(activity: str) -> None:
	row = frappe.get_doc(ACTIVITY, activity) if frappe.db.exists(ACTIVITY, activity) else None
	if not row or row.status != "Queued":
		return  # already handled (a duplicate job, or the row was cancelled)
	slot = None
	try:
		slot = _run_guarded(row)
	except Exception as e:
		frappe.db.rollback()
		_set(row, status="Failed", summary=_READ_FAILED_SUMMARY, detail=_log(e))
	finally:
		if slot is not False:
			_enqueue_next(activity)


def _run_guarded(row):
	"""Return False when no slot was free (the row stays Queued and nothing chains)."""
	trigger = (
		frappe.db.get_value(
			"Jarvis Trigger",
			row.trigger,
			["enabled", "action_type", "llm_daily_cap", *spec.TRIGGER_FIELDS],
			as_dict=True,
		)
		if row.trigger
		else None
	)
	if not trigger or not cint(trigger.enabled) or trigger.action_type != "Intake":
		_set(row, status="Skipped", summary="rule turned off")
		return True
	if not _valid_owner(row.event_user):
		_set(
			row,
			status="Failed",
			summary="The person who uploaded this file can no longer be used to create drafts.",
		)
		return True
	needs_ai = not row.reading_json
	trigger_cap = cint(trigger.llm_daily_cap) or llm_action.DEFAULT_DAILY_CAP
	if needs_ai and _over_cap(row.trigger, trigger_cap):
		_set(row, status="Skipped", summary="daily limit reached")
		return True
	slot = _claim_slot()
	if not slot:
		return False
	try:
		if not _claim_row(row):
			return False  # another job owns this row; this one did no work
		row.status, row.attempts = "Running", cint(row.attempts) + 1
		_run_stages(row, trigger, needs_ai)
	finally:
		_release_slot(slot)
	return True


def _claim_row(row) -> bool:
	"""Move Queued -> Running in ONE conditional UPDATE and report whether this job won.
	Two jobs for a row (a duplicate enqueue, a pump racing a chain) can both have read
	it as Queued; only one UPDATE matches, so only one job makes the AI call."""
	activity = frappe.qb.DocType(ACTIVITY)
	won = (
		_db.update_rowcount(
			frappe.qb.update(activity)
			.set(activity.status, "Running")
			.set(activity.attempts, cint(row.attempts) + 1)
			# A raw UPDATE skips the ORM's modified stamp: without it the pump's "stuck"
			# test would see the row as old for a moment after the claim.
			.set(activity.modified, now_datetime())
			.where((activity.name == row.name) & (activity.status == "Queued"))
		)
		== 1
	)
	if won:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the polling UI must see Running
	return won


def _valid_owner(user: str) -> bool:
	from jarvis.permissions import is_valid_unattended_owner

	return is_valid_unattended_owner(user)


def _run_stages(row, trigger, needs_ai: bool) -> None:
	_set(row, summary="Reading the file")
	if needs_ai:
		s = spec.load(trigger)
		try:
			reading, note = reader.read_file(row.source_file, s, before_call=lambda: _count_call(row.trigger))
		except lt.LLMTaskNotAvailableError as e:
			_set(row, status="Failed", summary=llm_action.NO_AGENT_UPDATE_MESSAGE, detail=_reason(e))
			return
		except lt.LLMTaskError as e:
			# Expected (agent refusal, timeout): a short reason, no Error Log noise.
			_set(row, status="Failed", summary=_READ_FAILED_SUMMARY, detail=_reason(e))
			return
		except frappe.DoesNotExistError:
			_set(
				row,
				status="Failed",
				summary=capture.FILE_REMOVED_SUMMARY,
				detail="The source File no longer exists.",
			)
			return
		except reader.UnreadableFile as e:
			_set(row, status="Failed", summary=str(e), detail=str(e))
			return
		if reading.get("error") == "NO_IMAGE":
			_set(row, status="Failed", summary=NO_IMAGE_SUMMARY, detail="The model reported NO_IMAGE.")
			return
		if note:
			# Inside the reading so it survives a draft-only retry; draft.finish pops it.
			reading["_note"] = note
		# Saved before the draft stage: a retry reuses it instead of paying for another read.
		_set(row, reading_json=json.dumps(reading))
	reading = json.loads(row.reading_json)
	note = reading.get("_note")
	_set(row, summary=f"Creating the draft. {note}" if note else "Creating the draft")
	try:
		draft.finish(row, reading)
	except Exception as e:
		frappe.db.rollback()
		_set(row, status="Failed", summary=_DRAFT_FAILED_SUMMARY, detail=_log(e))


def _recover_stuck() -> None:
	"""A job killed mid-run (OOM, restart) leaves its row Running for good. Past the
	slot TTL plus margin, retry it (it keeps any saved reading) or give up after 3 runs."""
	# System context: scheduler job, not a user request.
	stuck = frappe.get_all(
		ACTIVITY,
		filters={
			"action_type": "Intake",
			"status": "Running",
			"modified": ["<", add_to_date(now_datetime(), minutes=-_STUCK_MINUTES)],
		},
		fields=["name", "attempts"],
	)
	for r in stuck:
		if cint(r.attempts) < _MAX_ATTEMPTS:
			frappe.db.set_value(
				ACTIVITY, r.name, {"status": "Queued", "summary": "Retrying after an interruption"}
			)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job must publish the state
			_enqueue(r.name)
		else:
			frappe.db.set_value(
				ACTIVITY, r.name, {"status": "Failed", "summary": "Stopped unexpectedly. Retry it."}
			)
			who = frappe.db.get_value(ACTIVITY, r.name, ["event_user", "source_file"], as_dict=True)
			notify.row_ended(who, "Stopped unexpectedly. Retry it.")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job must publish the state


def pump() -> None:
	"""Cron backstop: a row whose job was lost (restart, crash) or that found no slot
	still moves. Starts up to two rows older than two minutes when a slot looks free."""
	with impersonate("Administrator"):
		_pump()


def _pump() -> None:
	_recover_stuck()
	free = _free_slots()
	if not free:
		return
	# System context: scheduler job, not a user request.
	rows = frappe.get_all(
		ACTIVITY,
		filters={
			"action_type": "Intake",
			"status": "Queued",
			"creation": ["<", add_to_date(now_datetime(), minutes=-_PUMP_AGE_MINUTES)],
		},
		pluck="name",
		order_by="creation asc",
		limit=free,
	)
	for name in rows:
		_enqueue(name)
