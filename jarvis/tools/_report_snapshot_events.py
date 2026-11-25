"""What a snapshot call tells the model (fixed texts, typed errors) and the operator
(`read` events, failure lines). No Redis access here: this is the bottom layer of the
snapshot modules, imported by all of them."""

from contextlib import contextmanager

import frappe

from jarvis import telemetry
from jarvis.exceptions import InvalidArgumentError, RetryLaterError

# Fixed strings: never a filter value, a report name or an exception message.
NOT_AVAILABLE = "This report snapshot expired or is not available. Run run_report again."
NO_ACCESS = "This report snapshot could not be reloaded with your current access. Run run_report again."
CHANGED = "This report snapshot changed while it was read. Send the same call again."
UNAVAILABLE = "Report snapshots are temporarily unavailable. Send the same call again."
PINNED_MISMATCH = "Report snapshot, scope or access changed. Inspect it again before querying."

_EVENT_SENT = "jarvis_report_snapshot_read_event"
_READS_MUTED = "jarvis_report_snapshot_reads_muted"


def log(reason, exc=None, *, level="warning"):
	"""A failure line in the INFO-pinned snapshot log: reason code and exception class only,
	never a filter value or a message."""
	telemetry.record_snapshot_failure(reason, exc, level=level)


def read_event(outcome, snapshot_id, meta=None, sql_executed=False, **extra):
	"""The one `read` event of a call; `extra` carries `reason`, `evicted` ..."""
	if getattr(frappe.local, _READS_MUTED, False):
		return
	setattr(frappe.local, _EVENT_SENT, True)
	telemetry.record_snapshot_event(
		"read",
		outcome,
		snapshot_id=snapshot_id,
		generation=(meta or {}).get("generation", 0),
		sql_executed=sql_executed,
		**extra,
	)


@contextmanager
def muted_reads():
	"""No `read` event inside: the post-reload inspect is part of a call that sends its own."""
	setattr(frappe.local, _READS_MUTED, True)
	try:
		yield
	finally:
		setattr(frappe.local, _READS_MUTED, False)


def begin_read():
	"""Start of one `query_report` call on a snapshot: no `read` event sent yet."""
	setattr(frappe.local, _EVENT_SENT, False)


def record_read_error(snapshot_id, exc):
	"""One `read` event for a failure no gate reported (a Deadline, a worker error)."""
	if not getattr(frappe.local, _EVENT_SENT, False):
		read_event("error", snapshot_id, reason=type(exc).__name__)


def unavailable(exc, where="read"):
	"""The retryable error for a Redis fault, logged. A Redis fault is never a miss (B.9.0)."""
	log(f"{where}_redis_error", exc)
	return RetryLaterError(UNAVAILABLE)


def redis_down(snapshot_id, exc, *, where="read"):
	"""`unavailable`, and the `read` event of the call."""
	error = unavailable(exc, where)
	read_event("redis_error", snapshot_id, reason=type(exc).__name__)
	return error


def not_available(snapshot_id, meta=None, outcome="not_available", **extra):
	read_event(outcome, snapshot_id, meta, **extra)
	return InvalidArgumentError(NOT_AVAILABLE)
