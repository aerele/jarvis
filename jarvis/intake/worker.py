"""Document Intake: the background job behind one Queued Activity row.

Stages: guard -> limits -> slot -> read (skipped when a reading is saved) ->
``draft.finish``. One model call per file, ever: the reading is saved before the
draft stage, so a retry after a draft failure costs nothing. There are no sleeps
or polling loops: a job with no free slot leaves its row Queued, and every
finished job (plus the 5-minute ``pump``) starts the next waiting row.
"""

import json
import traceback

import frappe
from frappe.utils import add_to_date, cint, now_datetime, nowdate

from jarvis import _redis_lock, compat
from jarvis.intake import draft, reader
from jarvis.triggers import llm_action
from jarvis.triggers import llm_task_client as lt

ACTIVITY = "Jarvis Trigger Activity"
SLOTS = 2
# Longer than the slowest job (90 s read plus draft), so a crashed holder frees itself.
_SLOT_TTL_S = 360
_DEFAULT_SITE_CAP = 300
_PUMP_AGE_MINUTES = 2

_NO_IMAGE_SUMMARY = "This site's AI model can't read images for intake yet."
_READ_FAILED_SUMMARY = "Reading the file failed. Managers can see the details in this activity."
_DRAFT_FAILED_SUMMARY = "Creating the draft failed. Managers can see the details in this activity."


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


def _enqueue_next(finished: str) -> None:
	"""A slot just freed: start the oldest waiting row of the site."""
	try:
		# System context: a background job picks the next row whoever uploaded it.
		nxt = frappe.get_all(
			ACTIVITY,
			filters={"action_type": "Intake", "status": "Queued", "name": ["!=", finished]},
			pluck="name",
			order_by="creation asc",
			limit=1,
		)
		if nxt:
			_enqueue(nxt[0])
	except Exception:
		frappe.log_error(title="Jarvis Intake: could not start the next file")


def _over_cap(trigger: str, trigger_cap: int) -> bool:
	return (
		llm_action.daily_count(trigger) >= trigger_cap
		or cint(compat.cache_get_fresh(f"{site_cap_key()}:count")) >= _site_cap()
	)


def _count_call(trigger: str) -> None:
	llm_action._bump(llm_action._cap_key(trigger))
	llm_action._bump(site_cap_key())


def run_job(activity: str) -> None:
	"""Run one Queued Intake row. Expected failures end on the row, never in RQ."""
	row = frappe.get_doc(ACTIVITY, activity) if frappe.db.exists(ACTIVITY, activity) else None
	if not row or row.status != "Queued":
		return  # already handled (a duplicate job, or the row was cancelled)
	slot = None
	try:
		slot = _run_guarded(row)
	except Exception:
		frappe.db.rollback()
		_set(row, status="Failed", summary=_READ_FAILED_SUMMARY, detail=traceback.format_exc()[-2000:])
		frappe.log_error(title="Jarvis Intake: job crashed")
	finally:
		if slot is not False:
			_enqueue_next(activity)


def _run_guarded(row):
	"""Return False when no slot was free (the row stays Queued and nothing chains)."""
	trigger = (
		frappe.db.get_value(
			"Jarvis Trigger",
			row.trigger,
			[
				"enabled",
				"action_type",
				"llm_daily_cap",
				"intake_spec",
				"intake_target_doctype",
				"intake_instructions",
				"allow_incomplete_drafts",
			],
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
	trigger_cap = cint(trigger.llm_daily_cap) or llm_action._DEFAULT_DAILY_CAP
	if needs_ai and _over_cap(row.trigger, trigger_cap):
		_set(row, status="Skipped", summary="daily limit reached")
		return True
	slot = _claim_slot()
	if not slot:
		return False
	try:
		_run_stages(row, trigger, needs_ai)
	finally:
		_release_slot(slot)
	return True


def _valid_owner(user: str) -> bool:
	from jarvis.permissions import is_valid_unattended_owner

	return is_valid_unattended_owner(user)


def _run_stages(row, trigger, needs_ai: bool) -> None:
	from jarvis.intake import spec

	_set(row, status="Running", summary="Reading the file", attempts=cint(row.attempts) + 1)
	if needs_ai:
		s = spec.load(trigger)
		try:
			reading, note = reader.read_file(row.source_file, s, before_call=lambda: _count_call(row.trigger))
		except lt.LLMTaskNotAvailableError as e:
			_set(row, status="Failed", summary=llm_action._NO_AGENT_UPDATE_MESSAGE, detail=str(e)[:2000])
			return
		except lt.LLMTaskError as e:
			_set(row, status="Failed", summary=_READ_FAILED_SUMMARY, detail=str(e)[:2000])
			return
		except reader.UnreadableFile as e:
			_set(row, status="Failed", summary=str(e), detail=str(e))
			return
		if reading.get("error") == "NO_IMAGE":
			_set(row, status="Failed", summary=_NO_IMAGE_SUMMARY, detail="The model reported NO_IMAGE.")
			return
		# Saved before the draft stage: a retry reuses it instead of paying for another read.
		_set(row, reading_json=json.dumps(reading), detail=note)
	reading = json.loads(row.reading_json)
	try:
		draft.finish(row, reading)
	except Exception as e:
		frappe.db.rollback()
		_set(row, status="Failed", summary=_DRAFT_FAILED_SUMMARY, detail=f"{e}"[:2000])


def pump() -> None:
	"""Cron backstop: a row whose job was lost (restart, crash) or that found no slot
	still moves. Starts up to two rows older than two minutes when a slot looks free."""
	cache = frappe.cache()
	free = sum(1 for key in slot_keys() if not cache.exists(key))
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
