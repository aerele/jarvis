"""Scheduled job: clean up abandoned streaming Jarvis Chat Messages.

If an RQ worker is killed (OOM, deploy, host restart) mid-stream, its
Jarvis Chat Message row stays at streaming=1. This scan runs on Frappe's
scheduler every 5 minutes.

Managed rows (with a gateway session_key, on a managed bench) are RECOVERABLE:
openclaw persists the result. They are PROMOTED to the recovering state for
turn_recovery to finalize from the snapshot, but only once they are definitely
past any live worker (a live managed turn self-marks recovering at the WS cap
and never reaches here), so a still-streaming turn is never flipped.

Genuinely unrecoverable rows (self-hosted bench, or a row whose conversation /
session_key is gone) are errored after the short threshold.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from jarvis.chat.events import publish_to_user

# Error genuinely-abandoned rows (self-hosted / no session) after this.
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
	from jarvis import selfhost

	now = now_datetime()
	self_hosted = selfhost.is_self_hosted()
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
		creation = r.get("creation")
		recoverable = (not self_hosted) and bool((r.get("session_key") or "").strip())
		if recoverable:
			if creation and creation < managed_cutoff:
				frappe.db.set_value(
					MSG,
					r["name"],
					{
						"recovering": 1,
						"recovery_started_at": now,
					},
				)
			continue
		# Self-hosted / orphaned / no session: genuinely unrecoverable.
		if creation and creation < error_cutoff:
			frappe.db.set_value(MSG, r["name"], {"streaming": 0, "error": _ABANDONED})
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
			errored += 1
	frappe.db.commit()
	return errored


# Orphan sweep: recovery above keys on a streaming=1 ASSISTANT row, but that
# placeholder is only created inside the worker. A turn whose RQ job never ran
# (enqueued toward workers that died - possible for up to the probe TTL, or
# ~420s after a hard kill while RQ's stale worker registration lingers) has no
# assistant row at all, so without this sweep it would hang as an unanswered
# user message forever.
ORPHAN_MIN_AGE_SECONDS = 180  # past any normal dequeue delay
ORPHAN_MAX_AGE_SECONDS = 3 * 3600  # don't touch history predating job ids
_ORPHAN_ERR = "Run was never started (no worker picked it up)."


def _sweep_orphan_turns(now) -> int:
	"""Find user messages with no assistant row after them, decide from the
	RQ job's actual state, and heal or surface them. Returns rows errored."""
	from frappe.utils.background_jobs import (
		get_job,
		get_job_status,
		get_workers,
	)

	rows = frappe.db.sql(
		"""
		SELECT m.name, m.conversation, m.seq, m.was_recovered, c.owner
		FROM `tabJarvis Chat Message` m
		LEFT JOIN `tabJarvis Conversation` c ON c.name = m.conversation
		WHERE m.role = 'user'
		  AND m.creation BETWEEN %(lo)s AND %(hi)s
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
		for w in get_workers():
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
		# Lost (no job / canceled / dead-queue). Heal once, then surface.
		if not int(r["was_recovered"] or 0):
			# Stamp the recovery-attempt marker BEFORE re-dispatch so the redispatch's
			# job id carries the ::a1 suffix and the shard-lock commit inside admission
			# makes it durable. CDX-19 (residual): if that re-dispatch hits a FULL admission
			# queue the redispatch returns {overloaded:True} WITHOUT creating a replacement
			# Turn/job — the momentary overload must NOT consume the one healing strike, or the
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
			continue
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
		errored += 1
	return errored
