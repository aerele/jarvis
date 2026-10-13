"""Feedback outbox: the bench keeps each feedback answer until admin has it.

Before this (audit F17, admin-v2#715) ``chat.feedback`` forwarded every answer to
admin inside the web request and dropped it on any failure while still telling
the UI ``ok``. Now the request stores the answer here and returns at once; a
background job delivers it after the commit, and a ``*/5`` sweep retries what
did not go through.

One ``Jarvis Feedback Outbox`` row per feedback identity, named from it
(``outbox_name``): a thumbs rating per reply (``Reply``), the session popup per
conversation (``Session``), the business survey per user and period (``Pulse``).
A later answer for the same identity, such as the note that follows a thumbs
down, REPLACES the row's payload, the same fold admin applies on its side.

States: ``Pending`` (stored, admin has not acknowledged it) -> ``Delivered``, or
``Failed`` (admin's validator refused the payload, or ``MAX_ATTEMPTS`` transient
failures over about a week). A bench without admin credentials, or one admin
refuses to authenticate, sends nothing and burns no attempt: its rows wait, so a
credential fix delivers the backlog.
Every new answer bumps ``revision`` and restarts the row as a fresh Pending, and a
delivery marks the row Delivered only while the revision it sent is current.

Bounds and privacy: the payload is exactly the item admin's ingest takes (never
the reply text; notes are already clipped by the caller), it sits at permlevel 1
so Desk shows delivery state without the answer itself, and ``prune`` deletes
delivered rows after ``DELIVERED_RETENTION_DAYS``, undelivered ones after
``UNDELIVERED_RETENTION_DAYS`` and the oldest rows above ``MAX_ROWS``, logging
how many undelivered answers it dropped.

Idempotency: admin upserts every kind on a natural key ((tenant, message_ref),
(tenant, session_ref), (tenant, period_key, user_ref)), so a re-send after an
ambiguous timeout folds onto the same admin row instead of counting twice.

No explicit commit anywhere: the request path commits with the request, the
delivery job and the sweep are committed by the job runtime. Neither holds a row
lock across the admin call: a row is read without a lock, pushed, and only then
written.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import NamedTuple

import frappe

from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

DT = "Jarvis Feedback Outbox"
KINDS = ("Reply", "Session", "Pulse")
PENDING, DELIVERED, FAILED = "Pending", "Delivered", "Failed"

#: Transient failures before a row is given up as Failed. With the backoff below
#: the last attempt comes about seven days after the first: long enough for a real
#: admin outage or a tenant re-provision (409 NoRunningTenant), and well inside
#: UNDELIVERED_RETENTION_DAYS.
MAX_ATTEMPTS = 32
#: Wait after the Nth failed attempt; the last value repeats. The sweep runs
#: every five minutes, so nothing shorter is meaningful.
BACKOFF_MINUTES = (5, 15, 60, 180, 360)
#: Wait after admin refuses this bench's credentials (401/403). Not an attempt:
#: the payload is fine, and a credential fix later must still deliver it.
AUTH_DEFER_SECONDS = 3600
#: How long the sweep leaves a fresh row to the job queued for it at accept time.
FIRST_JOB_GRACE_SECONDS = 120
#: Rows one sweep may send. Admin's ingest allows 120 pushes per tenant per hour
#: per kind and counts refused ones too. 6 every 5 minutes is 72 an hour, which
#: leaves the rest of that bucket to fresh taps while a backlog drains.
SWEEP_BATCH = 6
#: Floor on a rate-limit deferral when admin sends no usable retry_after.
MIN_RATE_LIMIT_DEFER_SECONDS = 60
DELIVERED_RETENTION_DAYS = 7
UNDELIVERED_RETENTION_DAYS = 30
#: Hard ceiling on the table between daily prunes; the oldest rows beyond it go.
MAX_ROWS = 5000
#: Rows one DELETE removes when trimming to the ceiling.
_PRUNE_CHUNK = 500
#: Backstop on one payload, counted in the characters of its own text (keys and
#: values, see ``_content_chars``), never in its JSON encoding: escaping can turn
#: one character into six (a control character becomes a \u escape), and this
#: cap must never refuse an answer the callers' per-field caps already accepted.
#: The largest such answer, a pulse survey with the 1000-char use-case text and
#: the 1000-char note, all seven feature keys twice and every ref at its 140-char
#: admin bound, is under 3,200 characters. Anything near this is a bug.
MAX_PAYLOAD_CHARS = 8000
_MAX_ERROR = 200
#: Admin contract codes for its own validators refusing the payload itself (a
#: malformed item, or one that is not JSON). Re-sending the same payload can never
#: pass, so these alone fail a row at once. Every other 4xx is retried: a 409
#: NoRunningTenant, and a concurrency error such as TimestampMismatchError or
#: DuplicateEntryError from two writes racing on the same admin row, pass later.
_PAYLOAD_REJECTION_CODES = frozenset({"InvalidChatFeedback", "InvalidFeedbackResponse", "InvalidArgument"})
_LOG_THROTTLE_KEY = "jarvis_feedback_outbox_last_log_hour"
_ROW_FIELDS = ["name", "kind", "payload", "status", "attempts", "revision", "next_retry_at", "last_error"]


class _Outcome(NamedTuple):
	"""What one push did. ``kind`` is ``delivered``, ``rejected`` (admin will never
	take this payload), ``retry`` (transient, counts an attempt) or ``deferred``
	(rate limited, or this bench's credentials refused: does not)."""

	kind: str
	error: str = ""
	retry_after: int = 0


def outbox_name(kind: str, ref: str, user: str) -> str:
	"""The row name for one feedback identity. Hashed so a long period key or an
	email address never has to fit, or be escaped into, a docname."""
	digest = hashlib.sha1(f"{kind}:{ref}:{user}".encode()).hexdigest()[:20]
	return f"{kind}-{digest}"


def accept(kind: str, ref: str, payload: dict) -> str:
	"""Store one feedback answer as Pending and queue its delivery for after the
	request commits. A row that already exists for this identity takes the new
	payload and starts over. Returns the row name.

	The caller has already derived and bounded ``payload``; it is stored as is."""
	if kind not in KINDS:
		frappe.throw(f"unknown feedback kind {kind!r}", frappe.ValidationError)
	if _content_chars(payload) > MAX_PAYLOAD_CHARS:
		frappe.throw("feedback payload is too large", frappe.ValidationError)
	# ensure_ascii=False: store the text as written. The default escapes every
	# non-ASCII character to \uXXXX (two of them for an emoji), six to twelve
	# times its size, which is no reason to make a Tamil note any bigger.
	body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
	user = frappe.session.user
	name = outbox_name(kind, ref, user)
	due = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=FIRST_JOB_GRACE_SECONDS)
	# A plain existence read first, not an UPDATE: an UPDATE that matches nothing
	# takes a gap lock, and two first taps holding the same gap would deadlock on
	# their inserts.
	revision = _replace(name, body, due) if frappe.db.exists(DT, name) else None
	if revision is None:
		revision = 1
		try:
			frappe.get_doc(
				{
					"doctype": DT,
					"kind": kind,
					"ref": ref,
					"user": user,
					"payload": body,
					"status": PENDING,
					"attempts": 0,
					"revision": 1,
					"next_retry_at": due,
				}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			# A second tap on the same reply won the insert; take its row over.
			revision = _replace(name, body, due) or revision
	_enqueue_delivery(name, revision)
	return name


def _replace(name: str, body: str, due) -> int | None:
	"""Put a new answer on an existing row and restart it as Pending. Returns the
	row's new revision, or None if there was no row to update."""
	t = frappe.qb.DocType(DT)
	(
		frappe.qb.update(t)
		.set(t.payload, body)
		.set(t.status, PENDING)
		.set(t.attempts, 0)
		.set(t.last_error, "")
		.set(t.next_retry_at, due)
		.set(t.delivered_at, None)
		.set(t.revision, t.revision + 1)
		.set(t.modified, frappe.utils.now_datetime())
		.where(t.name == name)
	).run()
	if _affected() != 1:
		return None
	return frappe.db.get_value(DT, name, "revision")


def _enqueue_delivery(name: str, revision: int) -> None:
	"""Queue ``deliver`` for after the commit, deduplicated per row AND revision:
	a repeat for the same answer is not doubled, while a newer answer gets its own
	job even if one for the older answer is still queued or running. Two jobs on
	one row are safe: each records only onto the revision it sent, and ``_record``
	re-sends when an older push may have reached admin last.

	Never raises. The queue is checked here, inside the request, and a Redis
	hiccup must not roll back an answer that is otherwise stored; the sweep
	sends a row whose job never ran."""
	try:
		frappe.enqueue(
			"jarvis.chat.feedback_outbox.deliver",
			queue="short",
			job_id=f"jarvis-feedback-outbox::{name}::{revision}",
			deduplicate=True,
			enqueue_after_commit=True,
			name=name,
		)
	except Exception:
		frappe.logger("jarvis.feedback_outbox").warning(f"could not queue delivery of {name}", exc_info=True)


def deliver(name: str) -> None:
	"""Background job: push one row to admin and record what happened.

	An admin failure never raises out of here, because a raise would roll back
	the attempt count and error this records. A database error still raises, and
	the job runtime retries a deadlock in a fresh transaction, which re-reads the
	row and pushes its current revision."""
	if not _admin_configured():
		return
	row = frappe.db.get_value(DT, name, _ROW_FIELDS, as_dict=True)
	if not row or row.status != PENDING:
		return
	# A row that already failed, was rate limited or had this bench's credentials
	# refused waits out its retry time; the sweep owns it then. A freshly accepted row (no attempt, no error yet) goes at
	# once: its retry time is only the grace the sweep gives this job.
	waiting = row.next_retry_at and row.next_retry_at > frappe.utils.now_datetime()
	if waiting and (row.attempts or row.last_error):
		return
	_record(row, _push(row))


def retry_due() -> None:
	"""``*/5`` sweep: send the Pending rows whose retry time has come, oldest due
	first, at most ``SWEEP_BATCH`` per run.

	Every push runs before any row is written, so no row lock is held across an
	admin call. The sweep stops at the first transient failure: admin is down,
	throttling or refusing this bench, and the rows after it would fail the same
	way, so they keep their place for the next run."""
	if not _admin_configured():
		return
	# Scheduler context: no session user whose permissions could apply.
	rows = frappe.get_all(
		DT,
		filters={"status": PENDING, "next_retry_at": ["<=", frappe.utils.now_datetime()]},
		fields=_ROW_FIELDS,
		order_by="next_retry_at asc",
		limit=SWEEP_BATCH,
	)
	results = []
	for row in rows:
		outcome = _push(row)
		results.append((row, outcome))
		if outcome.kind in ("retry", "deferred"):
			break
	for row, outcome in results:
		_record(row, outcome)


def _push(row) -> _Outcome:
	"""Send one row's payload to admin and classify the result. Never raises."""
	try:
		payload = json.loads(row.payload or "")
	except ValueError:
		payload = None
	if not isinstance(payload, dict):
		return _Outcome("rejected", "stored payload is unreadable")
	try:
		_pusher(row.kind)(payload)
	except AdminRateLimitedError as e:
		wait = max(int(e.retry_after_seconds or 0), MIN_RATE_LIMIT_DEFER_SECONDS)
		return _Outcome("deferred", "admin rate limit", wait)
	except AdminAuthError as e:
		# This bench's credentials, not the payload: wait for a fix without
		# spending the row's attempts.
		return _Outcome("deferred", str(e) or "admin refused this bench's credentials", AUTH_DEFER_SECONDS)
	except AdminValidationError as e:
		# A 4xx from admin. Only its own validators refusing the payload are final;
		# see _PAYLOAD_REJECTION_CODES for what else a 4xx can be.
		if getattr(e, "code", "") in _PAYLOAD_REJECTION_CODES:
			return _Outcome("rejected", str(e))
		return _Outcome("retry", str(e))
	except AdminUnreachableError as e:
		# Network, timeout, 5xx (AdminRejectedError included: nothing in feedback
		# ingest raises a permanent fleet code, so it is not trusted as final here).
		# Ingest is idempotent, so a send that may already have landed is safe to
		# repeat.
		return _Outcome("retry", str(e))
	except Exception as e:
		_log_unexpected_throttled()
		return _Outcome("retry", type(e).__name__)
	return _Outcome("delivered")


def _pusher(kind: str):
	from jarvis import admin_client

	return {
		"Reply": admin_client.push_chat_feedback,
		"Session": admin_client.push_session_feedback,
		"Pulse": admin_client.push_pulse_feedback,
	}[kind]


def _record(row, outcome: _Outcome) -> None:
	"""Write one push's outcome, but only onto the revision that was pushed: a
	newer answer saved meanwhile keeps its own fresh Pending state."""
	t = frappe.qb.DocType(DT)
	now = frappe.utils.now_datetime()
	query = frappe.qb.update(t)
	if outcome.kind != "deferred":
		# A deferral is not activity on the row: leaving ``modified`` alone lets an
		# answer that waits on credentials forever still age out in ``prune``.
		query = query.set(t.modified, now)
	terminal = False
	if outcome.kind == "delivered":
		query = (
			query.set(t.status, DELIVERED)
			.set(t.delivered_at, now)
			.set(t.next_retry_at, None)
			.set(t.last_error, "")
			.set(t.attempts, t.attempts + 1)
		)
	elif outcome.kind == "deferred":
		retry_at = frappe.utils.add_to_date(now, seconds=outcome.retry_after)
		query = query.set(t.next_retry_at, retry_at).set(t.last_error, outcome.error[:_MAX_ERROR])
	else:
		attempts = int(row.attempts or 0) + 1
		terminal = outcome.kind == "rejected" or attempts >= MAX_ATTEMPTS
		retry_at = None if terminal else frappe.utils.add_to_date(now, minutes=_backoff_minutes(attempts))
		query = (
			query.set(t.attempts, attempts)
			.set(t.status, FAILED if terminal else PENDING)
			.set(t.next_retry_at, retry_at)
			.set(t.last_error, outcome.error[:_MAX_ERROR])
		)
	query.where((t.name == row.name) & (t.revision == row.revision)).run()
	if _affected() == 1:
		if terminal:
			frappe.log_error(
				title="Feedback outbox: delivery gave up",
				message=f"row={row.name} kind={row.kind} attempts={int(row.attempts or 0) + 1} "
				f"error={outcome.error[:_MAX_ERROR]}",
			)
		return
	if outcome.kind == "delivered":
		# A newer answer was saved while this push was in flight. If that newer
		# revision is already Delivered, admin may still hold THIS older one: both
		# upsert the same admin row, and this push can commit there last even
		# though it left first. Send the current revision once more. A newer
		# revision that is Pending goes out on its own schedule (its backoff is
		# left alone), and a Failed one admin has refused, so neither is touched.
		(
			frappe.qb.update(t)
			.set(t.status, PENDING)
			.set(t.next_retry_at, now)
			.set(t.modified, now)
			.where((t.name == row.name) & (t.revision != row.revision) & (t.status == DELIVERED))
		).run()


def _content_chars(value) -> int:
	"""Characters of text in a payload, keys included, with no encoding overhead."""
	if isinstance(value, dict):
		return sum(_content_chars(k) + _content_chars(v) for k, v in value.items())
	if isinstance(value, list | tuple):
		return sum(_content_chars(v) for v in value)
	return len(value if isinstance(value, str) else str(value))


def _backoff_minutes(attempts: int) -> int:
	return BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES)) - 1]


def prune() -> None:
	"""Daily: keep the outbox bounded. Deletes delivered rows past their
	retention, undelivered rows with no activity in a long time (admin unreachable
	or refusing this bench that long, or the bench never onboarded), then the
	oldest rows above ``MAX_ROWS``, in chunks. Admin holds the durable copy of
	everything delivered; an undelivered answer is gone for good, so every run
	that drops any logs one line saying how many."""
	now = frappe.utils.now_datetime()
	frappe.db.delete(
		DT,
		{"status": DELIVERED, "delivered_at": ["<", frappe.utils.add_days(now, -DELIVERED_RETENTION_DAYS)]},
	)
	stale = {
		"status": ["!=", DELIVERED],
		"modified": ["<", frappe.utils.add_days(now, -UNDELIVERED_RETENTION_DAYS)],
	}
	expired = frappe.db.count(DT, stale)
	if expired:
		frappe.db.delete(DT, stale)
	trimmed = 0
	overflow = frappe.db.count(DT) - MAX_ROWS
	while overflow > 0:
		# Scheduler context: no session user whose permissions could apply.
		oldest = frappe.get_all(
			DT, fields=["name", "status"], order_by="modified asc", limit=min(overflow, _PRUNE_CHUNK)
		)
		if not oldest:
			break
		frappe.db.delete(DT, {"name": ["in", [r.name for r in oldest]]})
		trimmed += sum(1 for r in oldest if r.status != DELIVERED)
		overflow -= len(oldest)
	if expired or trimmed:
		frappe.log_error(
			title="Feedback outbox: undelivered feedback dropped",
			message=f"{expired + trimmed} undelivered answers dropped: {expired} with no activity for "
			f"{UNDELIVERED_RETENTION_DAYS} days, {trimmed} over the {MAX_ROWS}-row ceiling",
		)


def _admin_configured() -> bool:
	"""An admin URL and a credential set the admin client can sign in with. A
	bench missing either sends nothing and spends no attempt; its rows wait."""
	from jarvis import admin_client
	from jarvis.chat.usage_push import _admin_configured as url_configured

	return url_configured() and admin_client.has_credentials()


def _affected() -> int:
	"""Rows the last statement changed, read before anything else can run a
	statement on this connection (the discipline ``feedback._claim_session_feedback``
	follows)."""
	cursor = getattr(frappe.db, "_cursor", None)
	return int(cursor.rowcount) if cursor else 0


def _log_unexpected_throttled() -> None:
	"""Log an unexpected push failure at most once an hour; a fault that repeats
	on every sweep must not flood the Error Log. Never raises."""
	try:
		hour = int(time.time()) // 3600
		if frappe.cache.get_value(_LOG_THROTTLE_KEY, expires=True) == hour:
			return
		frappe.cache.set_value(_LOG_THROTTLE_KEY, hour, expires_in_sec=7200)
		frappe.log_error(title="Feedback outbox: push failed unexpectedly", message=frappe.get_traceback())
	except Exception:
		pass
