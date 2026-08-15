"""Auto-tell of Data Import completion (Slice B).

When a background import started by ``run_import`` (Slice A) finishes, Jarvis posts
a completion message into the ORIGINATING chat on its own - "✓ Import into <doctype>
from <file>: N imported, M failed" - so the user never has to ask "did it work?".
Stuck / blocked / dead imports are announced too (never silent).

TWO triggers drive ONE classifier - do NOT delete either:
  * The ``*/2`` scheduler POLL (:func:`announce_finished_imports` with no filter) is
    the CORRECTNESS GUARANTEE. It is the ONLY thing that catches a worker/container
    death (deploy restart, reboot, eviction, work-horse OOM) - no in-process hook
    survives a killed process. It will look "redundant" once the fast path works;
    it is not. Never remove it.
  * The ``after_job`` HOOK (:func:`on_after_job`) is a best-effort LATENCY shortcut:
    it taps the same classifier for one import the instant its job ends, so the
    healthy/errored/timed-out/blocked case pings in seconds instead of <=2 min. It
    may silently vanish (absent on a Frappe build, mis-registered, ``short`` queue
    starved) and the poll simply carries everything - safe degradation, watch
    ``import_announce_stats().hook_rate_24h``.

Kill switch: ``frappe.conf.jarvis_import_announce_disabled`` stops BOTH the poll and
the hook (a broken hook *registration* needs a code revert - no flag can reach it).

v16/v17: this module MUST import clean on Frappe v16 - it is wired into ``after_job``,
which runs in the ``finally`` of EVERY background job on the bench, with no core
try/except, so a module-top ImportError here would fail every job. Keep top-level
imports to ``frappe`` + always-present utils; defer ``get_import_status`` /
``is_job_enqueued`` into function bodies.
"""

import frappe
from frappe.utils import now_datetime

_ANNOUNCEMENT = "Jarvis Import Announcement"


# ---------------------------------------------------------------------------
# Confirm-time binding (T1) - create the tracking row after a confirmed run_import
# ---------------------------------------------------------------------------
def bind_after_run_import(record: dict, result) -> None:
	"""Best-effort: after a CONFIRMED ``run_import`` dispatch, create one
	``Jarvis Import Announcement`` so the poll + hook can announce the import's
	completion into the originating chat.

	NEVER fails the confirm - the import already ran; a failed bind is ``log_error``'d
	(PC-3) and costs at most one silent import (no announcement), an accepted
	sub-second residual (O-1), NOT a reverse-orphan sweep.

	SECURITY (PC-4 / F4): binds to the TOKEN's OWN guarded ``conversation``/``owner``
	(``record`` came from ``pending_confirm.consume``), NEVER the client-supplied
	``passed_conv`` - else an attacker could post a Jarvis-spoofed completion into a
	victim's chat.
	"""
	try:
		if (record or {}).get("tool") != "run_import":
			return
		if not (isinstance(result, dict) and result.get("ok")):
			return
		# Kill-switch (AJ-6): while the announcer is disabled, do NOT create rows either -
		# else a backlog accumulates unobserved and burst-replays stale completions on
		# re-enable (review finding). One flag stops bind + poll + hook.
		if frappe.conf.get("jarvis_import_announce_disabled"):
			return
		data = result.get("data") or {}
		di = data.get("data_import")
		conversation = record.get("conversation")  # guarded (from consume); never passed_conv
		owner = record.get("owner")
		if not (di and conversation and owner):
			return
		# Pre-migrate window: the binding + poll ship in the SAME release as the
		# doctype; no-op cleanly until migrate creates the table (edge 13).
		if not frappe.db.exists("DocType", _ANNOUNCEMENT):
			return
		# Idempotent: ``data_import`` is unique. A re-confirm or a race must not raise a
		# DuplicateEntry into the confirm path.
		if frappe.db.exists(_ANNOUNCEMENT, {"data_import": di}):
			return
		doc = frappe.new_doc(_ANNOUNCEMENT)
		doc.data_import = di
		doc.conversation = conversation
		doc.owner = owner  # the conversation owner (respected by set_user_and_timestamp)
		doc.kicked_off_at = now_datetime()
		doc.announced = 0
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# A concurrent bind won the unique(data_import) race - benign, not a failure. A unique
		# FIELD violation raises UniqueValidationError (DuplicateEntryError is primary-key only).
		pass
	except Exception:
		frappe.log_error(title="import announce bind failed", message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Cascade (T5) - a deleted conversation must not orphan / block on its rows
# ---------------------------------------------------------------------------
def on_conversation_trash(doc, method=None) -> None:
	"""Second ``on_trash`` handler for ``Jarvis Conversation`` (alongside
	``admission.on_conversation_trash``): delete this conversation's announcement
	rows so its Link does not block deletion (``LinkExistsError``) and no
	``announced=0`` row is orphaned for the poll. Runs inside the trash txn, so a
	failure correctly rolls the whole delete back rather than half-deleting."""
	if not frappe.db.exists("DocType", _ANNOUNCEMENT):
		return
	frappe.db.delete(_ANNOUNCEMENT, {"conversation": doc.name})


# ---------------------------------------------------------------------------
# The classifier + poll (T2) and the message-post (T3)
# ---------------------------------------------------------------------------
_TERMINAL_STATUSES = frozenset({"Success", "Partial Success", "Error", "Timed Out"})
_STALE_GRACE_MINUTES = 5  # O-5: absorb the enqueue-registration lag before "didn't complete"
_CEILING_MINUTES = 720  # O-2: absolute age past which a still-"enqueued" import is presumed dead
_WORKLIST_LIMIT = 50  # PC-6: drain across ticks; a completion burst can't scan-storm one tick
_MAX_ROW_ATTEMPTS = 5  # bounded retry: quarantine a row whose classify raises this many ticks
_DEADMAN_AGE_MINUTES = 30  # PC-3: an announced=0 row older than this is "stuck in the pipeline"
_DEADMAN_BACKLOG = 25  # PC-3: alarm when this many pile up (poll wedged / kill-switch left on)
_DEADMAN_DEDUPE_HOURS = 20  # alarm ~once/day, not every tick


def _age_minutes(dt) -> float:
	if not dt:
		# A missing kicked_off_at must fail LOUD (past every threshold) so the row gets
		# announced/quarantined, not sit at age 0 forever - invisible to grace/ceiling/deadman.
		return float("inf")
	from frappe.utils import get_datetime

	return (now_datetime() - get_datetime(dt)).total_seconds() / 60.0


def _is_job_enqueued(di: str) -> bool:
	"""True while the import's RQ job is QUEUED/STARTED. Isolated behind this helper
	(deferred import) for v16/v17 brittleness + module import-safety (PC-7/AJ-2)."""
	from frappe.utils.background_jobs import is_job_enqueued

	return is_job_enqueued(f"data_import||{di}")


def _read_tally(di: str) -> dict | None:
	"""The import outcome via RAW reads - deliberately NOT get_import_status, which
	runs check_permission('read') and throws under a suspended/limited owner, leaving
	an announced=0 limbo behind a terminal status (AJ-3). Works whether the poll
	(Administrator) or the hook-enqueued classify (the import user) invokes it.
	Returns None if the Data Import was deleted (a poison row - PC-5). success/failed
	are summed (get_import_status OMITS those keys for an all-failed/zero-log import -
	PC-2)."""
	d = frappe.db.get_value("Data Import", di, ["status", "payload_count"], as_dict=True)
	if not d:
		return None
	rows = frappe.db.sql(
		"SELECT success, COUNT(*) AS c FROM `tabData Import Log` WHERE data_import=%(d)s GROUP BY success",
		{"d": di},
		as_dict=True,
	)
	success = sum(r.c for r in rows if r.success)
	failed = sum(r.c for r in rows if not r.success)
	return {"status": d.status, "total_records": d.payload_count, "success": success, "failed": failed}


def _has_template_warnings(di: str) -> bool:
	tw = frappe.db.get_value("Data Import", di, "template_warnings")
	if not tw:
		return False
	try:
		# template_warnings is a JSON list; parse it rather than string-compare "[]" so a
		# format drift ("[ ]", "{}") can't be misread as "has warnings".
		return bool(frappe.parse_json(tw))
	except Exception:
		return bool(str(tw).strip())


def _neutralize(s) -> str:
	"""Disarm an untrusted filename/label server-side so it cannot break out of the message
	text on EITHER frontend (PC-4). The shared markdown renderer's link rule matches a literal
	``[ ]( )`` even when the characters are backslash-escaped, so we REMOVE the structural
	markdown metacharacters (replace with a space) rather than escape them - escaping would
	still render an attacker-controlled link. HTML-escape angle brackets too."""
	s = str(s)
	s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
	for ch in ("`", "*", "_", "[", "]", "(", ")", "\\"):
		s = s.replace(ch, " ")
	return s


def _di_context(di: str) -> tuple[str, str]:
	d = frappe.db.get_value("Data Import", di, ["reference_doctype", "import_file"], as_dict=True) or {}
	doctype = d.get("reference_doctype") or "records"
	fu = d.get("import_file") or ""
	fname = fu.rsplit("/", 1)[-1] if fu else "the file"
	return _neutralize(doctype), _neutralize(fname)


def _link(di: str) -> str:
	"""ABSOLUTE link to the Data Import - the only way a failed-import message lets the user
	open the record (no frontend reads ref_doctype/ref_name, PC-2). Must be an absolute
	``http(s)://`` URL: the shared markdown renderer only linkifies ``https?://`` URLs, so a
	relative ``/app/...`` path renders as inert literal text (review finding)."""
	from urllib.parse import quote

	url = frappe.utils.get_url("/app/data-import/" + quote(di, safe=""))
	return f"[{_neutralize(di)}]({url})"


def _completion_copy(tally: dict, di: str) -> tuple[str, str]:
	"""(reason, content) for a covered terminal import - branch on STATUS FIRST (an Error /
	Timed Out import must never render as a green "done", even with a 0/0 tally - review
	finding), then on the RECORDS counts. Filename escaped, link to the Data Import (PC-2)."""
	doctype, fname = _di_context(di)
	link = _link(di)
	s, f, status = tally["success"], tally["failed"], tally["status"]
	head = f"Import into **{doctype}** from **{fname}**"
	if status == "Timed Out":
		return (
			"timed_out",
			f"⚠️ {head} timed out — **{s}** imported, **{f}** failed before it stopped. Check {link}.",
		)
	if status == "Error":
		return "error", f"✗ {head} failed — **{s}** imported, **{f}** failed. Check {link}."
	if s == 0 and f == 0:
		return "done", f"✓ {head}: nothing to import — 0 records. ({link})"
	if f == 0:
		return "done", f"✓ {head}: **{s}** record(s) imported. ({link})"
	if s == 0:
		return "error", f"✗ {head} failed — **0** imported, **{f}** failed. Check {link}."
	return "partial", f"⚠️ {head}: **{s}** imported, **{f}** failed. Check {link}."


def _copy_blocked(di: str) -> str:
	doctype, fname = _di_context(di)
	return (
		f"✗ Import into **{doctype}** from **{fname}** was blocked — nothing imported. "
		f"Check the file's columns ({_link(di)})."
	)


def _copy_interrupted(tally: dict, di: str) -> str:
	doctype, fname = _di_context(di)
	return (
		f"⚠️ Import into **{doctype}** from **{fname}** didn't complete — **{tally['success']}** "
		f"record(s) imported so far. Check {_link(di)}."
	)


def _copy_status_unknown(di: str) -> str:
	doctype, fname = _di_context(di)
	return (
		f"⚠️ Import into **{doctype}** from **{fname}** — status unknown after a long wait. Check {_link(di)}."
	)


def _set_reason(ann: str, reason: str) -> None:
	# WHERE announced=0: never overwrite the reason of an already-terminal row - a late
	# concurrent classify must not corrupt the by_reason stats (matches the CAS discipline).
	frappe.db.sql(
		"UPDATE `tabJarvis Import Announcement` SET reason=%(r)s WHERE name=%(n)s AND announced=0",
		{"r": reason, "n": ann},
	)
	frappe.db.commit()


def _mark_terminal(ann: str, reason: str, source: str) -> None:
	"""Flip announced=1 WITHOUT posting (poison row / no resolvable owner / archived) so
	the row leaves the worklist and never re-polls; ``reason`` records why it aborted."""
	frappe.db.sql(
		"UPDATE `tabJarvis Import Announcement` SET announced=1, announced_via=%(s)s, reason=%(r)s "
		"WHERE name=%(n)s AND announced=0",
		{"s": source, "r": reason, "n": ann},
	)
	frappe.db.commit()


def _bump_attempts(row: dict) -> None:
	"""Bounded retry (PC-5): a row whose classify raises EVERY tick would re-log forever and,
	below the deadman's backlog threshold, never alarm. Count consecutive failures and, after
	_MAX_ROW_ATTEMPTS, quarantine it terminal (reason="poison_error", surfaced in by_reason)."""
	try:
		n = (row.get("attempts") or 0) + 1
		if n >= _MAX_ROW_ATTEMPTS:
			_mark_terminal(row["name"], "poison_error", "poll")
		else:
			frappe.db.set_value(_ANNOUNCEMENT, row["name"], "attempts", n, update_modified=False)
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()


def _post_completion(row: dict, source: str, reason: str, content: str) -> str:
	"""Post the completion message into the originating chat and flip announced=1,
	EXACTLY once. Conversation-lock-FIRST (PC-1): commit -> FOR UPDATE conversation ->
	natural-key dedupe on Jarvis Chat Message -> insert -> CAS announced=1 (same lock)
	-> commit -> publish AFTER commit. Owner resolved fail-closed BEFORE impersonate
	(impersonate(None) is a silent Administrator no-op - AJ-3/PC-4)."""
	conv = row["conversation"]
	di = row["data_import"]
	ann = row["name"]
	c = frappe.db.get_value("Jarvis Conversation", conv, ["owner", "status"], as_dict=True)
	if not c or not c.owner:
		# Conversation gone / owner unresolved on a live row: never post as Administrator.
		_mark_terminal(ann, "no_conversation", source)
		return "aborted"
	if c.status == "Archived":
		# EC5: don't post into a dead thread; mark terminal so it stops polling.
		_mark_terminal(ann, "archived", source)
		return "archived"

	from jarvis._session import impersonate
	from jarvis.api import _locked_insert_chat_message
	from jarvis.chat import events

	msg = None
	with impersonate(c.owner):
		frappe.db.commit()  # commit-first: the FOR UPDATE is the first statement (REPEATABLE-READ)
		msg = _locked_insert_chat_message(
			conv,
			{
				"role": "assistant",
				"content": content,
				"hidden": 0,
				"ref_doctype": "Data Import",
				"ref_name": di,
			},
			# Exactly-once is THIS natural-key dedupe (PC-1), not the flag: a concurrent
			# poll + fast-path classify serialize on the conversation lock, and the loser
			# sees the winner's message here and skips the insert. role="assistant" is
			# LOAD-BEARING: a tool receipt (role="tool") for a get_doc of THIS Data Import -
			# which the run_import note explicitly invites ("Ask me to check on it") - also
			# carries ref_doctype="Data Import"/ref_name=di, and without the role scope it
			# would false-match and SILENTLY suppress the real completion (only the
			# announcement writes an assistant row with this ref).
			dedupe_filters={
				"conversation": conv,
				"role": "assistant",
				"ref_doctype": "Data Import",
				"ref_name": di,
			},
		)
		# CAS the row terminal inside the SAME lock. announced=1 whether we posted or
		# deduped - the flag only stops the poll re-checking.
		frappe.db.sql(
			"UPDATE `tabJarvis Import Announcement` SET announced=1, announced_via=%(s)s, reason=%(r)s "
			"WHERE name=%(n)s AND announced=0",
			{"s": source, "r": reason, "n": ann},
		)
		frappe.db.commit()
	if msg:
		events.publish_to_user(
			c.owner, {"kind": "import:finished", "conversation_id": conv, "message_id": msg}
		)
	return "announced"


def _classify_and_announce(row: dict, source: str) -> str:
	"""Decide one un-announced row's fate and act. Returns a count key. The completion
	TRIGGER is job-done (is_job_enqueued == False), NOT status - 'Partial Success' is a
	MID-import status written after row 1."""
	ann = row["name"]
	di = row["data_import"]
	enqueued = _is_job_enqueued(di)
	age = _age_minutes(row["kicked_off_at"])
	prev_reason = row.get("reason") or ""

	# 1) Job still enqueued -> legitimately running; skip WITHOUT reading the (unindexed) Data
	#    Import Log tally. EXCEPT past the O-2 ceiling: a full worker/container death leaves
	#    is_job_enqueued True FOREVER (the Redis job hash is never reaped), so an absolute-age
	#    backstop surfaces it. Cheap-check-first also stops a backlog of long-running imports
	#    from starving newer FINISHED rows behind them in the worklist.
	if enqueued and age <= _CEILING_MINUTES:
		if prev_reason == "stuck_seen":
			_set_reason(ann, "")  # a transient not-enqueued blip resolved; clear the marker
		return "running"

	# We need the outcome from here.
	tally = _read_tally(di)
	if tally is None:
		_mark_terminal(ann, "data_import_deleted", source)  # PC-5 poison row
		return "poison"
	if enqueued:  # enqueued AND past the ceiling -> presumed dead (worker/container death)
		return _post_completion(row, source, "status_unknown", _copy_status_unknown(di))

	status = tally["status"]
	terminal = status in _TERMINAL_STATUSES
	payload = tally["total_records"] or 0
	covered = (tally["success"] + tally["failed"]) >= payload if payload else True

	# --- job NOT enqueued from here ---

	# 2) Blocked: the job returned WITHOUT a terminal status (import_data early-returns on
	#    blocking warnings) - Pending + template_warnings, nothing imported (PC-2).
	if not terminal:
		if status == "Pending" and _has_template_warnings(di):
			return _post_completion(row, source, "blocked", _copy_blocked(di))
		# 3) Interrupted/stuck. Guard with the ~5-min grace (enqueue-registration lag) AND
		#    a two-tick confirmation - a transient Redis is_job_enqueued blip must not
		#    false-announce "didn't complete" (O-5).
		if age < _STALE_GRACE_MINUTES:
			return "running"
		if prev_reason != "stuck_seen":
			_set_reason(ann, "stuck_seen")  # first sighting; confirm on the next tick
			return "stuck_pending"
		return _post_completion(row, source, "interrupted", _copy_interrupted(tally, di))

	# 4) Terminal status.
	if not covered:
		# Terminal (usually "Partial Success") but success+failed < payload_count -> the
		# worker was hard-killed mid-run; never a clean tally (PC-2).
		return _post_completion(row, source, "interrupted", _copy_interrupted(tally, di))
	reason, content = _completion_copy(tally, di)
	return _post_completion(row, source, reason, content)


def _deadman_watch() -> None:
	"""Poll-only tail (PC-3): if the backlog of announced=0 rows older than the grace
	exceeds a threshold - the poll wedged, the kill-switch was left on, or every row is
	silently skipping - log_error once/~day (deduped like recovery_rate_watch). Folded
	into the poll, NOT a second cron."""
	from frappe.utils import add_to_date

	cutoff = add_to_date(now_datetime(), minutes=-_DEADMAN_AGE_MINUTES)
	backlog = frappe.db.count(_ANNOUNCEMENT, {"announced": 0, "kicked_off_at": ["<", cutoff]})
	if backlog < _DEADMAN_BACKLOG:
		return
	title = "import_announce: un-announced backlog"
	since = add_to_date(now_datetime(), hours=-_DEADMAN_DEDUPE_HOURS)
	if frappe.db.sql(
		"SELECT name FROM `tabError Log` WHERE method=%(t)s AND creation>=%(s)s LIMIT 1",
		{"t": title, "s": since},
	):
		return
	frappe.log_error(
		title=title,
		message=(
			f"{backlog} import announcement(s) un-announced for >{_DEADMAN_AGE_MINUTES}m - the poll "
			f"may be wedged or the kill-switch left on (or a burst of long-running imports). "
			f"See import_announce_stats."
		),
	)


def announce_finished_imports(data_import: str | None = None, source: str = "poll") -> dict:
	"""The classifier entry, driven by TWO callers (do not delete either - see the module
	docstring): the ``*/2`` poll (no ``data_import`` - drains a bounded worklist, the
	correctness guarantee) and the ``after_job`` fast path (one ``data_import`` - the
	latency shortcut). Self-wraps per row + returns structured counts. Kill-switch +
	DocType-existence guards first (pre-migrate + operator off-switch)."""
	if frappe.conf.get("jarvis_import_announce_disabled"):
		return {"skipped": "disabled"}
	if not frappe.db.exists("DocType", _ANNOUNCEMENT):
		return {"skipped": "no_doctype"}
	filters = {"announced": 0}
	if data_import:
		filters["data_import"] = data_import
	rows = frappe.get_all(
		_ANNOUNCEMENT,
		filters=filters,
		fields=["name", "data_import", "conversation", "owner", "kicked_off_at", "reason", "attempts"],
		order_by="creation asc",
		limit=(1 if data_import else _WORKLIST_LIMIT),
	)
	counts = {"checked": len(rows)}
	for r in rows:
		try:
			outcome = _classify_and_announce(r, source)
		except Exception:
			# Restore per-row isolation: release any conversation lock taken mid-_post_completion
			# and discard partial writes so they don't bleed into the next row (review finding).
			frappe.db.rollback()
			outcome = "errored"
			frappe.log_error(title="import_announce: row failed", message=frappe.get_traceback())
			_bump_attempts(r)  # bounded retry -> quarantine a deterministically-failing row
		counts[outcome] = counts.get(outcome, 0) + 1
	if not data_import:  # poll-only tail
		try:
			_deadman_watch()
		except Exception:
			frappe.log_error(title="import_announce: deadman failed", message=frappe.get_traceback())
	return counts


# ---------------------------------------------------------------------------
# The after_job fast path (T8) - a latency shortcut layered on the poll
# ---------------------------------------------------------------------------
def on_after_job(method=None, kwargs=None, result=None, **_) -> None:
	"""``after_job`` hook - runs in the ``finally`` of EVERY background job on the bench,
	inside a bare core loop with NO try/except. So it MUST be import-safe on every Frappe
	version (no risky module-top imports - AJ-2) and MUST NEVER raise (a raise here would
	replace the host job's own result bench-wide). The fast-path LATENCY shortcut: when a
	Data Import job ends, enqueue a scoped completion-classify so a healthy/errored/blocked
	import pings in SECONDS instead of <=2 min. The ``*/2`` poll stays the correctness
	guarantee - this hook cannot survive a worker/container death.
	"""
	try:
		# AJ-1: identify by METHOD (``job_id`` is NOT passed to after_job). Anchoring on
		# the method (not "kwargs has a data_import key") is also what prevents self-
		# recursion - the enqueued classify's method is announce_finished_imports, which
		# never matches. On the ~100% non-matching path this is a single in-memory string
		# compare: ZERO db/cache/conf reads (AJ-1 / perf floor).
		if method != "frappe.core.doctype.data_import.data_import.start_import":
			return
		name = (kwargs or {}).get("data_import")
		if not name:
			return
		# AJ-6: the kill-switch gates the hook's enqueue traffic too (before any enqueue) -
		# one flag stops BOTH the poll and the fast path. frappe.conf is an in-memory dict,
		# so this stays off the hot non-matching path above.
		if frappe.conf.get("jarvis_import_announce_disabled"):
			return
		# Enqueue (Redis-only, no DB) - isolated from this job's finally, so the classify's
		# conversation lock + insert + publish never runs in the worker's cleanup. Scoped to
		# this one import; source="hook" for attribution; deduplicate collapses a re-fired
		# after_job (execute_job's deadlock-retry unwind) while the classify is still queued.
		frappe.enqueue(
			"jarvis.chat.import_announce.announce_finished_imports",
			queue="short",
			job_id=f"jarvis_announce||{name}",
			deduplicate=True,
			data_import=name,
			source="hook",
		)
	except Exception:
		# NEVER propagate out of the job's finally (AJ-2). log_error itself does a DB write
		# that can throw inside a poisoned finally, so guard it too.
		try:
			frappe.log_error(title="import_announce: after_job failed", message=frappe.get_traceback())
		except Exception:
			pass
