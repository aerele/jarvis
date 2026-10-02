"""Scheduled job: clean up abandoned streaming Jarvis Chat Messages.

If an RQ worker is killed (OOM, deploy, host restart) mid-stream, its
Jarvis Chat Message row stays at streaming=1. This scan runs on Frappe's
scheduler every 5 minutes.

Rows with a gateway session_key are RECOVERABLE: agent persists the result.
They are PROMOTED to the recovering state for turn_recovery to finalize from
the snapshot, but only once they are definitely past any live worker (a live
turn self-marks recovering at the WS cap and never reaches here), so a
still-streaming turn is never flipped.

Genuinely unrecoverable rows (a row whose conversation / session_key is gone)
are errored after the short threshold.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from jarvis.chat import txn
from jarvis.chat.events import publish_to_user

# Error genuinely-abandoned rows (no session) after this.
STALE_THRESHOLD_SECONDS = 120
# Promote a managed row to recovering only once it is past the RQ worker cap,
# so it is definitely orphaned (no live worker survives past the cap, and a
# live turn self-marks recovering at the 600s WS cap well before this).
MANAGED_RECOVER_AFTER_SECONDS = 720
MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"
_ABANDONED = "Run abandoned (worker did not finish within the timeout)."


def scan_and_mark_errored() -> int:
	"""Scan stale streaming rows: promote recoverable managed rows to
	recovering, error the rest. Returns the count of rows ERRORED."""
	now = now_datetime()
	managed_cutoff = now - timedelta(seconds=MANAGED_RECOVER_AFTER_SECONDS)
	error_cutoff = now - timedelta(seconds=STALE_THRESHOLD_SECONDS)

	# LEFT JOIN so a streaming row whose conversation was deleted is still
	# handled (session_key resolves NULL -> errored), not silently dropped.
	rows = frappe.db.sql(
		"""
		SELECT m.name, m.conversation, m.creation, c.owner, c.session_key
		FROM `tabJarvis Chat Message` m
		LEFT JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.streaming = 1 AND m.recovering = 0
		""",
		as_dict=True,
	)

	errored = _sweep_orphan_turns(now)
	for r in rows:
		errored += _scan_one_row(r, now, managed_cutoff, error_cutoff)
	return errored


def _scan_one_row(r: dict, now, managed_cutoff, error_cutoff) -> int:
	"""One row's write as its own fresh-snapshot, replayable unit (P7). The scan
	above takes ONE snapshot for the whole batch, but MariaDB's snapshot-isolation
	check fires per WRITE, so a row this scan did not itself touch but another
	writer (the pump, a web request) committed since the scan's read must not
	crash the whole cycle - only this row is skipped (logged), every other row in
	the batch still gets processed. Returns 1 iff this row was ERRORED (never
	'recovering'), matching the caller's original count contract."""
	creation = r.get("creation")
	recoverable = bool((r.get("session_key") or "").strip())

	def unit() -> str:
		"""Returns 'errored', 'recovering' or 'skip'. Publish (a non-DB side effect)
		happens AFTER the winning commit, outside this unit, so a replay can never
		double-publish."""
		if recoverable:
			if creation and creation < managed_cutoff:
				frappe.db.set_value(MSG, r["name"], {"recovering": 1, "recovery_started_at": now})
				frappe.db.commit()
				return "recovering"
			return "skip"
		# Orphaned / no session: genuinely unrecoverable.
		if creation and creation < error_cutoff:
			frappe.db.set_value(MSG, r["name"], {"streaming": 0, "error": _ABANDONED})
			frappe.db.commit()
			return "errored"
		return "skip"

	try:
		outcome = txn.replay_on_conflict(unit, label=f"stale_scan row {r['name']}", fresh=True)
	except Exception as e:
		txn.report_lost_race(e, title="stale_scan: row failed")
		return 0

	if outcome == "errored":
		if r.get("owner"):
			publish_to_user(
				r["owner"],
				{
					"kind": "run:error",
					"conversation_id": r["conversation"],
					"message_id": r["name"],
					"error": _ABANDONED,
				},
			)
		return 1
	return 0


# Orphan sweep: recovery above keys on a streaming=1 ASSISTANT row, but on the
# legacy transport that placeholder is only created inside the worker. A turn whose
# RQ job never ran (enqueued toward workers that died - possible for up to the probe
# TTL, or ~420s after a hard kill while RQ's stale worker registration lingers) has
# no assistant row at all, so without this sweep it would hang as an unanswered
# user message forever.
#
# A seed that has a `Jarvis Chat Turn` row is NOT an orphan, whatever that Turn's
# state, and the candidate query below excludes it. The sweep predates the turn
# machine: its liveness probe is the legacy RQ job id, which the Relay Pump never
# creates, so under the pump every probe read "no job" and a turn that was only
# WAITING (`queued`, or `preparing` before its placeholder is attached: neither has
# an assistant row yet) looked dead after 180s. The sweep then re-dispatched it, and
# since admission is idempotent on run_id and not on the seed, that made a SECOND
# Turn for the same message. The pump is single-flight per conversation, so the two
# ran one after the other: the message was answered twice, and on an armed macro
# conversation a step's writes happened twice.
#   - Non-terminal Turn: the turn machine's to bound (pump.watchdog; admission.sweep
#     where the pump is not configured), not this sweep's. The sweep cannot do it
#     safely: a re-dispatch makes a second Turn, and an error row written over a Turn
#     that is still live is followed by a real answer later. The machine does NOT
#     bound every such state today. Known gaps, none of which this sweep covers:
#       * a `queued` Turn that holds a reservation its prepare never claims: the
#         watchdog reclaims it at 120s and deliberately skips the age-out, a live
#         pump reserves it again, and so on with no cap;
#       * a `preparing` Turn whose prepare dies before the placeholder is attached:
#         re-queued at 300s, promoted again, no attempt cap;
#       * a shard whose control row is `legacy` (the kill switch): pump.watchdog
#         skips the shard, and admission.sweep only looks at `queued` (aged out with
#         a marker) and `dispatching`, so a `preparing` Turn with no placeholder or
#         a pre-dispatch `recovering` one has no owner there.
#     Before the exclusion these ended, by accident, in the second strike's "never
#     started" row (after a duplicate Turn). They now stay unanswered until the user
#     cancels, where the state allows it (a user cancel acts on `queued`, `preparing`
#     and `ready` only, so not on a pre-dispatch `recovering` Turn). The bound belongs
#     in the watchdog.
#   - Terminal Turn with no reply row: a verdict, not a lost dispatch. Re-running it
#     would answer a message the user was told had been cancelled, and would overturn
#     a user's own cancel. The pump's queue age-out used to be the common source; it
#     now writes its own cancel marker (pump._write_age_out_marker), which is an
#     assistant row after the seed. What remains is rare: a best-effort marker write
#     that failed (age-out or user cancel), a recovery-budget error on a Turn that
#     never got a placeholder, the Phase-0 stopped-then-crashed cancel. Those are
#     skipped, neither re-run nor errored.
# What still heals is what the sweep was written for: a message the machine never
# saw (pure legacy, or the Turn insert that follows the committed seed never ran).
#
# One such message is not re-dispatched here: the step of a live macro run. The macro
# engine sends each step under a fixed id and comes back to a step by several roads
# (a repeated turn end, the capacity resume). A re-dispatch from here, under a random
# id, was a turn the engine did not know: it sent the step again, and on an armed run
# the step's writes happened twice. That message is handed to the engine
# (``_a_live_runs_step``, ``macros.heal_dangling_seed``), which gives it the step's own
# turn; the strike is the same marker, and a second strike also ends the run. Every
# other orphan in a run's chat is healed here as before: a message someone typed, a
# step message of a run parked for capacity, an older step message, the summarize turn.
ORPHAN_MIN_AGE_SECONDS = 180  # past any normal dequeue delay
ORPHAN_MAX_AGE_SECONDS = 3 * 3600  # don't touch history predating job ids
_ORPHAN_ERR = "Run was never started (no worker picked it up)."


def _sweep_orphan_turns(now) -> int:
	"""Find user messages with no assistant row after them and no Turn of their
	own, decide from the RQ job's actual state, and heal or surface them. Returns
	rows errored."""
	from frappe.utils.background_jobs import (
		get_job,
		get_job_status,
		get_workers,
	)

	rows = frappe.db.sql(
		"""
		SELECT m.name, m.conversation, m.seq, m.was_recovered, m.origin, c.owner
		FROM `tabJarvis Chat Message` m
		LEFT JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.role = 'user'
		  AND m.creation BETWEEN %(lo)s AND %(hi)s
		  AND NOT EXISTS (
			SELECT 1 FROM `tabJarvis Chat Turn` t WHERE t.seed_message = m.name)
		  AND NOT EXISTS (
			SELECT 1 FROM `tabJarvis Chat Message` a
			WHERE a.conversation = m.conversation
			  AND a.role = 'assistant' AND a.seq > m.seq)
		""",
		{
			"lo": now - timedelta(seconds=ORPHAN_MAX_AGE_SECONDS),
			"hi": now - timedelta(seconds=ORPHAN_MIN_AGE_SECONDS),
		},
		as_dict=True,
	)
	if not rows:
		return 0

	live_qnames = set()
	try:
		from jarvis.chat.pump import _registry_is_stale

		workers = get_workers()
		if _registry_is_stale(workers):
			# Workers alive but unregistered (a heartbeat gap or a queue-Redis
			# restart), or unverifiable: the queue is draining toward them, never
			# "nobody listens".
			live_qnames = None
		else:
			for w in workers:
				live_qnames.update(w.queue_names() or [])
	except Exception:
		live_qnames = None  # probe trouble: treat queued jobs as draining

	errored = 0
	for r in rows:
		# The conversation was deleted out from under this orphan (the LEFT JOIN
		# surfaces it as owner IS NULL). Redispatching or erroring onto a gone
		# conversation can only raise LinkValidationError — the Turn.conversation /
		# Message.conversation Links are required — so skip it: there is nothing to
		# heal and nobody to notify. This is both a real production hardening (the
		# module docstring already promises "a row whose conversation is gone" is
		# handled) AND what makes this scan hermetic on a shared bench, where a
		# concurrent test's rolled-back conversation would otherwise trip the global
		# sweep (WP-1d: test_chat_stale_scan isolation).
		if not r.get("owner"):
			continue
		job_id = f"jarvis-turn::{r['name']}::a{int(r['was_recovered'] or 0)}"
		try:
			status = get_job_status(job_id)
			# rq's JobStatus is a (str, Enum) mixin: str() gives
			# "JobStatus.QUEUED" on py3.11+, so compare the .value.
			status = getattr(status, "value", None) or (str(status) if status else None)
		except Exception:
			continue
		if status == "started":
			continue  # a worker owns it; the streaming scans take over
		orig_attachments = orig_context = None
		if status == "queued":
			job = get_job(job_id)
			origin = getattr(job, "origin", None)
			if live_qnames is None or (origin and origin in live_qnames):
				continue  # backlog draining toward live workers - leave it
			# Queued into a queue nobody listens on: salvage the payload
			# (attachments ride only the enqueue kwargs), cancel, heal.
			try:
				inner = (job.kwargs or {}).get("kwargs") or {}
				orig_attachments = inner.get("attachments")
				orig_context = inner.get("context")
			except Exception:
				pass
			try:
				job.cancel()
			except Exception:
				pass
		# P7: this row's healing/erroring write is its own fresh-snapshot unit: a
		# conflict here (another writer touched this row/conversation since the
		# scan's read at the top) must not stop the sweep from healing the REST of
		# the batch. Not replayed like the simpler streaming-row scan above: a
		# redispatch and job.cancel() above are not blindly safe to re-run after a
		# server-side abort (job.cancel() already fired once, a one-shot external
		# effect), so a lost race here is caught, logged and this row is skipped -
		# the next scan cycle picks it up again from the durable was_recovered marker.
		try:
			txn.fresh_snapshot()
			if _heal_or_error_orphan(r, orig_attachments, orig_context):
				errored += 1
		except Exception as e:
			txn.report_lost_race(e, title="stale_scan: orphan row failed")
	return errored


def _a_live_runs_step(r: dict) -> bool:
	"""Whether this orphan is the current step message of a `running` macro run, which
	the macro engine heals (see the note above ``ORPHAN_MIN_AGE_SECONDS``)."""
	if (r.get("origin") or "") != "macro":
		return False
	from jarvis.chat import macros

	return bool(macros.run_waiting_on_seed(r["conversation"], r["name"]))


def _heal_or_error_orphan(r: dict, orig_attachments, orig_context) -> bool:
	"""One orphan row's write: heal (redispatch) on the first strike, error on the
	second. Returns True iff the row was ERRORED (for the caller's count).

	Under the pump the second strike is reachable only when the first re-dispatch
	left no Turn (legacy fallback, or admission raised after the strike was stamped):
	a seed healed into a Turn is no longer a candidate."""
	a_runs_step = _a_live_runs_step(r)
	if a_runs_step and not int(r["was_recovered"] or 0):
		from jarvis.chat import macros

		# The engine stamps the strike itself, under the run's lock.
		healed = macros.heal_dangling_seed(r["conversation"], r["name"])
		if healed is not None:
			if healed.get("overloaded"):
				# As for the re-dispatch below: a full site must not cost the one strike.
				frappe.db.set_value(MSG, r["name"], "was_recovered", 0, update_modified=False)
				frappe.db.commit()
				frappe.logger("jarvis.chat.stale_scan").warning(
					f"macro step heal deferred (site overloaded); will retry: {r['name']}"
				)
			return False
		# Not the engine's after all: healed as any other orphan.
	if not int(r["was_recovered"] or 0):
		# Stamp the recovery-attempt marker BEFORE re-dispatch so the redispatch's
		# job id carries the ::a1 suffix and the shard-lock commit inside admission
		# makes it durable. CDX-19 (residual): if that re-dispatch hits a FULL admission
		# queue the redispatch returns {overloaded:True} WITHOUT creating a replacement
		# Turn/job: the momentary overload must NOT consume the one healing strike, or the
		# next scan would surface a spurious second-strike error. Reset the marker to 0 so a
		# later scan retries once capacity frees.
		frappe.db.set_value(MSG, r["name"], "was_recovered", 1, update_modified=False)
		from jarvis.chat.api import _redispatch_orphan

		_res = _redispatch_orphan(
			r["conversation"],
			r["name"],
			attachments=orig_attachments,
			context=orig_context,
		)
		if isinstance(_res, dict) and _res.get("overloaded"):
			frappe.db.set_value(MSG, r["name"], "was_recovered", 0, update_modified=False)
			frappe.db.commit()
			frappe.logger("jarvis.chat.stale_scan").warning(
				f"orphan redispatch deferred (site overloaded); will retry: {r['name']}"
			)
		return False
	# Second strike: give the user the normal error + retry surface.
	from jarvis.chat.api import _next_seq

	err = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": r["conversation"],
			"seq": _next_seq(r["conversation"]),
			"role": "assistant",
			"content": "",
			"streaming": 0,
			"error": _ORPHAN_ERR,
		}
	)
	err.insert(ignore_permissions=True)
	# The scan is a scheduler job, so nothing commits until it ends: make the error
	# row durable before the publish below points the user's client at it. A failed
	# insert never reaches here: the caller's report_lost_race rolls it back and the
	# next scan retries the row.
	frappe.db.commit()
	if r.get("owner"):
		publish_to_user(
			r["owner"],
			{
				"kind": "run:error",
				"conversation_id": r["conversation"],
				"message_id": err.name,
				"error": _ORPHAN_ERR,
			},
		)
	if a_runs_step:
		# The step got its one retry and still has no turn. Nothing will ever end for
		# it, so the run would sit `running` (and armed) until the stale-run sweep.
		from jarvis.chat import macros

		macros.fail_run_for_seed(r["conversation"], r["name"])
	return True
