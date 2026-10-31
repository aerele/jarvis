"""Bounded RQ work; framework retries never borrow a different actor."""

import json
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import frappe
from frappe.utils.background_jobs import get_job

from jarvis.payment_calendar import store
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.projection import ProjectionError
from jarvis.payment_calendar.snapshot import collect

ACTIVE_RQ = {"queued", "started", "deferred", "scheduled"}


def public_status(status):
	return {
		key: status.get(key)
		for key in ("snapshot_id", "state", "code", "queued_at", "started_at", "completed_at", "expires_at")
	}


def request_snapshot(scope: dict) -> dict:
	proof = authorize_scope(scope)
	digest = sha256(
		json.dumps(
			{"site": str(frappe.local.site), "scope": scope, "fingerprint": proof["fingerprint"]},
			sort_keys=True,
		).encode()
	).hexdigest()
	actor = scope["actor"]
	sid, created = store.admit(actor, digest, scope, proof["fingerprint"])
	if not created:
		status = store.load_status(actor, sid)
		if status:
			return public_status(status)
		store.release_active(digest, sid)
		return {"snapshot_id": None, "state": "expired", "code": "expired"}
	try:
		queued = frappe.enqueue(
			"jarvis.payment_calendar.jobs.run_snapshot",
			queue="long",
			timeout=300,
			job_id="pcal:" + digest + ":" + sid,
			deduplicate=True,
			# Admission/status live in Redis; this request has no DB job row to commit.
			enqueue_after_commit=False,
			actor=actor,
			snapshot_id=sid,
		)
		if queued is None:
			store.fail(actor, sid, "worker_unavailable")
	except Exception:
		store.fail(actor, sid, "worker_unavailable")
	return public_status(store.load_status(actor, sid) or {"state": "expired", "code": "expired"})


def observe_job(status):
	if status["state"] in ("queued", "running"):
		age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["queued_at"])).total_seconds()
		if age > 5:
			job = get_job("pcal:" + status["scope_hash"] + ":" + status["snapshot_id"])
			if not job or job.get_status(refresh=True) not in ACTIVE_RQ:
				store.fail(status["scope"]["actor"], status["snapshot_id"], "job_lost")
	return store.load_status(status["scope"]["actor"], status["snapshot_id"]) or status


def _cause(exc):
	if isinstance(exc, ScopeChanged):
		return "scope_changed"
	if isinstance(exc, (CalendarSourceUnavailable, ProjectionError)):
		return exc.code if exc.code in store.SAFE_CODES else "source_contract_unavailable"
	if isinstance(exc, (frappe.PermissionError, frappe.DoesNotExistError)):
		return "forbidden"
	if isinstance(exc, TimeoutError) or type(exc).__name__ == "JobTimeoutException":
		return "timeout"
	return "internal_error"


def run_snapshot(actor: str, snapshot_id: str) -> None:
	started, stage = monotonic(), "admission"
	try:
		if frappe.session.user != actor:
			raise frappe.PermissionError("Payment Calendar actor changed.")
		status = store.load_status(actor, snapshot_id)
		if not status:
			frappe.logger().info("Payment Calendar job: expired_before_start")
			return
		if not store.mark_running(actor, snapshot_id):
			return
		stage = "sources"
		result = collect(status["scope"], status["fingerprint"])
		stage = "publish"
		authorize_scope(status["scope"], expected_fingerprint=status["fingerprint"], result=result)
		if not store.publish(actor, snapshot_id, result):
			frappe.logger().info("Payment Calendar job: result_discarded")
	except Exception as exc:
		# No exception reaches execute_job's retry/traceback logger. The source
		# context has already rolled back its read-only transaction on failure.
		code = _cause(exc)
		try:
			store.fail(actor, snapshot_id, code)
			if code not in ("scope_changed", "forbidden", "expired"):
				frappe.log_error(
					title="Payment Calendar snapshot",
					message=json.dumps(
						{
							"code": code,
							"exception_class": type(exc).__name__,
							"stage": stage,
							"duration_ms": int((monotonic() - started) * 1000),
						}
					),
				)
		except Exception:
			frappe.logger().warning("Payment Calendar job: safe_failure_unavailable")
