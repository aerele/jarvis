"""Document Intake: the background job behind one Queued Activity row.

Stages: guard -> limits -> slot -> claim -> read (skipped when a reading is saved) ->
``draft.finish``. One model call per file, ever: the reading is saved before the
draft stage, so a retry after a draft failure costs nothing. A call counts against the
limits only once it returned a real reading. There are no sleeps or polling loops: a job
with no free slot, or over a daily limit, leaves its row Queued, and every finished job
(plus the 5-minute ``pump``) starts the next waiting row.

Anything that goes wrong BEFORE the claim (slot, limits, the claim itself) leaves the row
Queued: nothing ran and nothing was charged, so failing the row would only strand it.
"""

import json
import re

import frappe
from frappe.query_builder.functions import IfNull
from frappe.utils import add_to_date, cint, get_datetime, now_datetime, nowdate
from frappe.utils.background_jobs import is_job_enqueued

from jarvis import _redis_lock, compat
from jarvis._session import impersonate
from jarvis.intake import _db, capture, draft, guards, notify, reader, spec
from jarvis.triggers import llm_action
from jarvis.triggers import llm_task_client as lt

ACTIVITY = "Jarvis Trigger Activity"
SLOTS = 2
# Longer than the slowest job (90 s read plus draft), so a crashed holder frees itself.
_SLOT_TTL_S = 360
_DEFAULT_SITE_CAP = 300
_DEFAULT_USER_CAP = 50
_PUMP_AGE_MINUTES = 2
_STUCK_MINUTES = 10
_MAX_ATTEMPTS = 3
# Waiting rows looked at beyond the free slots, so rows already in the queue can be skipped.
_CHAIN_LOOKAHEAD = 10

NO_IMAGE_SUMMARY = "This site's AI model can't read images for intake yet."
NO_AGENT_UPDATE_SUMMARY = "Intake needs the latest agent update on this site."
# A row over a daily limit is not failed: it waits for the day to turn (the pump retries it
# once ``modified`` is before today). A paused site keeps its rows Queued and retried.
WAITING_PREFIX = "Waiting:"
PAUSED_SUMMARY = "Intake is paused on this site"
# This many failed rows within an hour tell the site's managers once.
_FAILURE_ALERT_COUNT = 5
_READ_FAILED_SUMMARY = "Reading the file failed. Managers can see the details in this activity."
_DRAFT_FAILED_SUMMARY = "Creating the draft failed. Managers can see the details in this activity."


def _reason(e: Exception) -> str:
	"""Short, safe text for ``detail``: class name plus a trimmed message with file paths
	cut out. The uploader can open their own row, so no traceback or long raw text goes on it;
	the full traceback is in the Error Log."""
	text = re.sub(r"(?:/[\w.\-]+)+", "", str(e)).strip()
	return f"{type(e).__name__}: {text}"[:200]


def _log(e: Exception, title: str) -> str:
	"""Send the traceback to the Error Log under a title that names the failure class and
	return the short reason for the row."""
	frappe.log_error(title=title, message=frappe.get_traceback())
	return _reason(e)


def _log_class(title: str, message: str) -> None:
	"""One Error Log per failure class per hour: an agent that is down fails every file the
	same way, and a log row each would bury everything else."""
	try:
		if _redis_lock.claim(f"jarvis:intake:errlog:{title}", 3600):
			frappe.log_error(title=title, message=message)
	except Exception:
		pass


def slot_keys() -> list[str]:
	return [f"jarvis:intake:slot:{i}" for i in range(SLOTS)]


def site_cap_key() -> str:
	return f"jarvis:intakecap:{nowdate().replace('-', '')}"


def user_cap_key(user: str) -> str:
	return f"jarvis:intakeuser:{user}:{nowdate().replace('-', '')}"


def _setting(fieldname: str, default: int) -> int:
	"""An Int setting; a value never saved reads as ``default``, an explicit 0 stays 0.
	``get_single_value`` cannot tell the two apart (an unset Int reads as 0), so the
	Singles row is asked whether a value was ever stored."""
	try:
		singles = frappe.qb.DocType("Singles")
		rows = (
			frappe.qb.from_(singles)
			.select(singles.value)
			.where((singles.doctype == "Jarvis Settings") & (singles.field == fieldname))
			.run()
		)
	except Exception:
		return default
	return cint(rows[0][0]) if rows and rows[0][0] not in (None, "") else default


def _site_cap() -> int:
	"""Reads per day across the site. 0 pauses intake for everyone."""
	return _setting("intake_daily_cap", _DEFAULT_SITE_CAP)


def _user_cap() -> int:
	"""Reads per day for one uploader. 0 means no per-user limit."""
	return _setting("intake_daily_cap_per_user", _DEFAULT_USER_CAP)


def _used(key: str, **durable_filters) -> int:
	"""Today's reads for the counter ``key``. Redis holds the count; when it is gone (a
	flush, an eviction) the day's ``read_on`` stamps on the Activity rows give the same
	number, so a flush cannot silently reset a limit."""
	raw = compat.cache_get_fresh(f"{key}:count")
	if raw is not None:
		return cint(raw)
	return frappe.db.count(ACTIVITY, {"action_type": "Intake", "read_on": nowdate(), **durable_filters})


def _cap_hit(row, trigger_cap: int) -> str:
	"""Which limit stops this row today: ``paused``, ``rule``, ``site``, ``user`` or ``""``."""
	site_cap = _site_cap()
	if site_cap <= 0:
		return "paused"
	if _used(llm_action.cap_key(row.trigger), trigger=row.trigger) >= trigger_cap:
		return "rule"
	if _used(site_cap_key()) >= site_cap:
		return "site"
	user_cap = _user_cap()
	if user_cap > 0 and _used(user_cap_key(row.event_user), event_user=row.event_user) >= user_cap:
		return "user"
	return ""


def _set(row, quiet: bool = False, **values) -> None:
	"""Save a status change and publish it: the SPA polls this row while the job runs.
	``quiet`` skips the uploader's bell (a duplicate is not news)."""
	row.db_set(values)
	if values.get("status") in ("Failed", "Skipped") and not quiet:
		# The uploader may have no Jarvis access: the bell is how they learn of it.
		notify.row_ended(row, values.get("summary") or row.summary or "")
	try:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the polling UI must see each stage
	except Exception:
		# A failed publish must not leave a half-open transaction for the next stage.
		frappe.db.rollback()
		frappe.log_error(title="Jarvis intake: could not publish a status change")


def _end(row, status: str, summary: str, detail: str = "", trigger=None) -> None:
	"""End the row Failed or Skipped, mark a Record rule's source record the same way, and
	(for a failure) tell the managers when failures pile up."""
	_set(row, status=status, summary=summary, detail=detail)
	if trigger is None and row.trigger:
		trigger = frappe.db.get_value("Jarvis Trigger", row.trigger, spec.TRIGGER_FIELDS, as_dict=True)
	if trigger:
		draft.write_back(row, trigger, status)
	if status == "Failed":
		_alert_managers_if_failing()


def _alert_managers_if_failing() -> None:
	"""Five failed rows in the last hour is a site problem (agent down, wrong model), not a
	bad file: tell the System Managers once an hour. Counted from the rows, so it survives a
	Redis flush."""
	try:
		recent = frappe.db.count(
			ACTIVITY,
			{
				"action_type": "Intake",
				"status": "Failed",
				"modified": [">", add_to_date(now_datetime(), hours=-1)],
			},
		)
		if recent < _FAILURE_ALERT_COUNT or not _redis_lock.claim("jarvis:intake:failalert", 3600):
			return
		for manager in _managers():
			notify.notify(
				manager,
				f"Document Intake: {recent} files failed in the last hour. "
				"Open the Jarvis Trigger Activity list and check the Failed rows.",
			)
	except Exception:
		frappe.log_error(title="Jarvis intake: could not alert the managers")


def _managers() -> list[str]:
	"""Enabled System Managers (not Administrator): the people who can fix a site problem."""
	holders = frappe.get_all(  # system context: scheduler/job code, not a user request
		"Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"
	)
	users = frappe.get_all("User", filters={"name": ["in", holders or [""]], "enabled": 1}, pluck="name")
	return [u for u in users if u not in ("Administrator", "Guest")]


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


def _waiting_rows(limit: int, *, older_than=None, exclude: str | None = None) -> list[str]:
	"""Oldest Queued rows that may start now. A row that waits for a daily limit is left out
	until the day has turned (it was last touched before today), so the rows behind it are not
	starved by a capped rule; a paused site's rows stay in line, as everything is paused."""
	table = frappe.qb.DocType(ACTIVITY)
	query = (
		frappe.qb.from_(table)
		.select(table.name)
		.where((table.action_type == "Intake") & (table.status == "Queued"))
		.where(
			~IfNull(table.summary, "").like(f"{WAITING_PREFIX}%") | (table.modified < get_datetime(nowdate()))
		)
		.orderby(table.creation)
		.limit(limit)
	)
	if exclude:
		query = query.where(table.name != exclude)
	if older_than:
		query = query.where(table.creation < older_than)
	return [r[0] for r in query.run()]


def _enqueue_next(finished: str) -> None:
	"""A slot just freed: start as many of the oldest waiting rows as there are free
	slots, so a burst of uploads keeps every stream busy (one row per finished job
	would let a 100-file burst fall back to a single stream). A row whose job is already
	in the queue is skipped: enqueueing it again is a no-op, and it would eat the turn."""
	try:
		free = _free_slots()
		if not free:
			return
		for name in _waiting_rows(free + _CHAIN_LOOKAHEAD, exclude=finished):
			if free <= 0:
				break
			if is_job_enqueued(f"intake:{name}"):
				continue
			_enqueue(name)
			free -= 1
	except Exception:
		frappe.log_error(title="Jarvis intake: could not start the next file")


def _count_call(row) -> None:
	"""One real reading: count it against the rule, the site and the uploader."""
	llm_action.bump(llm_action.cap_key(row.trigger))
	llm_action.bump(site_cap_key())
	if row.event_user:
		llm_action.bump(user_cap_key(row.event_user))


def run_job(activity: str) -> None:
	"""Run one Queued Intake row. Expected failures end on the row, never in RQ.

	A queued job runs as whoever enqueued it (the uploader, an owner retrying, the
	scheduler). The system bookkeeping here (status writes, notices, the review request)
	must not depend on that, so it runs as Administrator. The ERP reads and the draft
	write still switch to the uploader inside ``draft`` (never as Administrator)."""
	with impersonate("Administrator"):
		_run_job(activity)


def _run_job(activity: str) -> None:
	try:
		row = frappe.get_doc(ACTIVITY, activity)
	except frappe.DoesNotExistError:
		return  # the row was cancelled
	if row.status != "Queued":
		return  # already handled (a duplicate job)
	slot = None
	try:
		slot = _run_guarded(row)
	except Exception as e:
		frappe.db.rollback()
		_end(row, "Failed", _READ_FAILED_SUMMARY, _log(e, "Jarvis intake: job failed"))
	finally:
		if slot is not False:
			_enqueue_next(activity)


def _load_rule(row):
	if not row.trigger:
		return None
	return frappe.db.get_value(
		"Jarvis Trigger",
		row.trigger,
		["enabled", "action_type", "llm_daily_cap", *spec.TRIGGER_FIELDS],
		as_dict=True,
	)


def _may_not_run(row, trigger, needs_ai: bool) -> bool:
	"""End the row when it must not run at all (and say why); True when it was ended."""
	if not trigger or not cint(trigger.enabled) or trigger.action_type != "Intake":
		_end(row, "Skipped", "rule turned off", trigger=trigger)
	elif guards.refused_doctype(trigger.intake_target_doctype) or (
		trigger.intake_source == "Record" and guards.refused_doctype(trigger.intake_record_doctype)
	):
		# Saved before the list of refused doctypes grew, or edited around validation.
		_end(row, "Failed", guards.TARGET_REFUSED_SUMMARY, trigger=trigger)
	elif not _valid_owner(row.event_user):
		_end(
			row,
			"Failed",
			"The person who uploaded this file can no longer be used to create drafts.",
			trigger=trigger,
		)
	elif refusal := guards.create_refusal(row.event_user, trigger.intake_target_doctype):
		# Checked again here (a role may have changed since capture) and before any AI call.
		_end(row, "Skipped", refusal, trigger=trigger)
	elif needs_ai and not reader.vision_available():
		_end(row, "Failed", NO_IMAGE_SUMMARY, "The site's model is not a vision model.", trigger=trigger)
	else:
		return False
	return True


def _defer_if_capped(row, trigger) -> bool:
	"""Leave the row Queued with a summary that names the limit (True), or go on (False)."""
	trigger_cap = cint(trigger.llm_daily_cap) or llm_action.DEFAULT_DAILY_CAP
	hit = _cap_hit(row, trigger_cap)
	if not hit:
		return False
	if hit == "paused":
		wording = PAUSED_SUMMARY
	else:
		who = {"rule": "for this rule", "site": "for the site", "user": "for your uploads"}[hit]
		wording = f"{WAITING_PREFIX} daily limit reached {who} (resets tomorrow)"
	if hit != "paused" or row.summary != wording:
		# Always stamped, even with the same text: ``modified`` is what keeps the pump away
		# until tomorrow (see ``_waiting_rows``).
		_set(row, summary=wording)
	if hit != "paused":
		_bell_once_a_day(row.event_user, wording)
	return True


def _bell_once_a_day(user: str, wording: str) -> None:
	"""One bell per uploader per day for a deferral: a 50-file burst is one notice."""
	try:
		if user and _redis_lock.claim(f"jarvis:intake:capbell:{user}:{nowdate()}", 86400):
			notify.notify(user, f"Intake: {wording[len(WAITING_PREFIX) :].strip()}")
	except Exception:
		pass


def _run_guarded(row):
	"""Return False when the row did not start (no slot, or an error before the claim): it
	stays Queued and nothing chains."""
	trigger = _load_rule(row)
	needs_ai = not row.reading_json
	if _may_not_run(row, trigger, needs_ai):
		return True
	try:
		if needs_ai and _defer_if_capped(row, trigger):
			return True
		slot = _claim_slot()
	except Exception:
		return _stay_queued(row)
	if not slot:
		return False
	try:
		try:
			won = _claim_row(row)
		except Exception:
			return _stay_queued(row)
		if not won:
			return False  # another job owns this row; this one did no work
		row.status, row.attempts = "Running", cint(row.attempts) + 1
		if needs_ai and _is_duplicate(row, trigger):
			return True
		_run_stages(row, trigger, needs_ai)
	finally:
		_release_slot(slot)
	return True


def _stay_queued(row) -> bool:
	"""A Redis outage or a deadlock before the claim: nothing ran, nothing was charged. The
	pump tries the row again, so log it (once an hour per class) and leave it alone."""
	frappe.db.rollback()
	_log_class("Jarvis intake: could not start a job", frappe.get_traceback())
	return False


def _is_duplicate(row, trigger) -> bool:
	"""Re-check, after the claim, for an older row that stands for the same bytes: the
	capture check is not atomic, and two uploads at once can both pass it. Of two rows the
	older one proceeds; this one ends Skipped without a model call or a bell."""
	record = row.target_docname if trigger.intake_source == "Record" else None
	earlier = capture.earlier_row(
		row.trigger, row.file_hash, record, exclude=row.name, created_before=row.creation
	)
	if not earlier:
		return False
	_set(row, quiet=True, status="Skipped", summary=capture.DUPLICATE_SUMMARY, duplicate_of=earlier)
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
	if needs_ai and not _read(row, trigger):
		return
	reading = json.loads(row.reading_json)
	note = reading.get("_note")
	_set(row, summary=f"Creating the draft. {note}" if note else "Creating the draft")
	try:
		draft.finish(row, reading)
	except Exception as e:
		frappe.db.rollback()
		_end(row, "Failed", _DRAFT_FAILED_SUMMARY, _log(e, "Jarvis intake: draft failed"), trigger)


def _read(row, trigger) -> bool:
	"""The one model call. Saves the reading (and counts the call) and returns True, or ends
	the row and returns False."""
	s = spec.load(trigger)
	try:
		reading, note = reader.read_file(row.source_file, s)
	except lt.LLMTaskNotAvailableError as e:
		_log_class("Jarvis intake: agent update needed", str(e))
		_end(row, "Failed", NO_AGENT_UPDATE_SUMMARY, _reason(e), trigger)
		return False
	except lt.LLMTaskError as e:
		# Expected (agent refusal, timeout): a short reason on the row, one Error Log an hour.
		_log_class("Jarvis intake: read failed", str(e))
		_end(row, "Failed", _READ_FAILED_SUMMARY, _reason(e), trigger)
		return False
	except frappe.DoesNotExistError:
		_end(row, "Failed", capture.FILE_REMOVED_SUMMARY, "The source File no longer exists.", trigger)
		return False
	except reader.UnreadableFile as e:
		_end(row, "Failed", str(e), str(e), trigger)
		return False
	if reading.get("error") == "NO_IMAGE" or reader.is_empty_reading(reading, s):
		# A text-only model often answers an all-null object instead of NO_IMAGE: a blank
		# draft (or review) of that would look like a read. Not counted: nothing was read.
		_end(row, "Failed", NO_IMAGE_SUMMARY, "The model read nothing from the file.", trigger)
		return False
	_count_call(row)
	if note:
		# Inside the reading so it survives a draft-only retry; draft.finish pops it.
		reading["_note"] = note
	# Saved before the draft stage: a retry reuses it instead of paying for another read.
	# ``read_on`` is the durable record of today's count (see ``_used``).
	_set(row, reading_json=json.dumps(reading), read_on=nowdate())
	return True


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
		# One bad row (a failed write, a lost queue) must not strand the rest.
		try:
			_recover_one(r)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Jarvis intake: could not recover a stuck row",
				message=f"{r.name}\n\n{frappe.get_traceback()}",
			)


def _recover_one(r) -> None:
	if cint(r.attempts) < _MAX_ATTEMPTS:
		frappe.db.set_value(
			ACTIVITY, r.name, {"status": "Queued", "summary": "Retrying after an interruption"}
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- scheduler job must publish the state; the caller rolls back on failure
		_enqueue(r.name)
	else:
		row = frappe.get_doc(ACTIVITY, r.name)
		_end(row, "Failed", "Stopped unexpectedly. Retry it.")


def pump() -> None:
	"""Cron backstop: a row whose job was lost (restart, crash) or that found no slot
	still moves. Starts up to two rows older than two minutes when a slot looks free."""
	with impersonate("Administrator"):
		_pump()


def _pump() -> None:
	_recover_stuck()
	rules = capture.intake_map()
	if not rules["folder"] and not rules["record"]:
		return  # no enabled Intake rule: nothing new can be waiting for a read
	free = _free_slots()
	if not free:
		return
	for name in _waiting_rows(free, older_than=add_to_date(now_datetime(), minutes=-_PUMP_AGE_MINUTES)):
		_enqueue(name)
