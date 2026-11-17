"""Bounded RQ work; framework retries never borrow a different actor."""

import json
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import frappe
from frappe.utils import nowdate
from frappe.utils.background_jobs import get_job

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import balances, catalog, store
from jarvis.payment_calendar.access import ScopeChanged, authorize_scope
from jarvis.payment_calendar.errors import CalendarSourceUnavailable, safe_code, safe_detail
from jarvis.payment_calendar.projection import ProjectionError
from jarvis.payment_calendar.snapshot import collect, elapsed_ms

ACTIVE_RQ = {"queued", "started", "deferred", "scheduled"}
# The RQ timeout is 300 s; a balances job past half of it is on its way to a timeout.
SLOW_JOB_MS = 150_000


def request_snapshot(scope: dict) -> dict:
	proof = authorize_scope(scope)
	digest = _digest({"site": str(frappe.local.site), "scope": scope, "fingerprint": proof["fingerprint"]})
	status = _submit(
		scope["actor"],
		digest,
		scope,
		proof["fingerprint"],
		kind="snapshot",
		method="jarvis.payment_calendar.jobs.run_snapshot",
		id_argument="snapshot_id",
	)
	return store.public_status(status)


def request_account_balances(company: str) -> dict:
	"""Admit (or join) the actor's balances job over every account they may total."""
	actor = frappe.session.user
	names = sorted(account.name for account in catalog.permitted_accounts(company))
	company = frappe.db.get_value("Company", company, "name")
	scope = {"actor": actor, "company": company, "accounts": names, "anchor": nowdate()}
	digest = _digest({"kind": "balances", "site": str(frappe.local.site), "scope": scope})
	status = _submit(
		actor,
		digest,
		scope,
		"",
		kind="balances",
		method="jarvis.payment_calendar.jobs.run_account_balances",
		id_argument="balances_id",
	)
	return store.public_status(status, "job_id")


def _digest(value):
	return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _submit(actor, digest, scope, fingerprint, *, kind, method, id_argument):
	"""Admit the job and put it on the long queue; the status row is the single source of truth."""
	expired = {"state": "expired", "code": "expired"}
	sid, created = store.admit(actor, digest, scope, fingerprint, kind=kind)
	if not created:
		status = store.load_status(actor, sid)
		if status:
			return status
		store.release_active(digest, sid)
		return expired
	try:
		queued = frappe.enqueue(
			method,
			queue="long",
			timeout=300,
			job_id="pcal:" + digest + ":" + sid,
			deduplicate=True,
			# Admission/status live in Redis; this request has no DB job row to commit.
			enqueue_after_commit=False,
			actor=actor,
			**{id_argument: sid},
		)
		if queued is None:
			_not_queued(kind, actor, sid, "not_queued")
	except Exception as exc:
		_not_queued(kind, actor, sid, type(exc).__name__)
	return store.load_status(actor, sid) or expired


def _not_queued(kind, actor, sid, reason):
	"""Mark the job failed; the log names the reason class, never the exception text.

	ERROR level: the production default hides WARNING, and this means the user got no result.
	"""
	store.fail(actor, sid, "worker_unavailable")
	frappe.logger("payment_calendar").error(f"worker_unavailable kind={kind} reason={reason}")


def observe_job(status):
	if status["state"] in ("queued", "running"):
		age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["queued_at"])).total_seconds()
		if age > 5:
			job = get_job("pcal:" + status["scope_hash"] + ":" + status["snapshot_id"])
			if not job or job.get_status(refresh=True) not in ACTIVE_RQ:
				# Log only a failure that was stored: a parallel poll or a finished job makes `fail` a no-op.
				if store.fail(status["scope"]["actor"], status["snapshot_id"], "job_lost"):
					kind = status.get("kind", "snapshot")
					frappe.logger("payment_calendar").error(f"job_lost kind={kind} age_s={int(age)}")
	return store.load_status(status["scope"]["actor"], status["snapshot_id"]) or status


def _cause(exc):
	if isinstance(exc, ScopeChanged):
		return "scope_changed"
	if isinstance(exc, (CalendarSourceUnavailable, ProjectionError)):
		return safe_code(exc, "source_contract_unavailable")
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
		# A row without a kind is a snapshot; a balances id never runs as one.
		if not status or status.get("kind", "snapshot") != "snapshot":
			frappe.logger().info("Payment Calendar job: expired_before_start")
			return
		if not store.mark_running(actor, snapshot_id):
			return
		stage = "sources"
		result = collect(status["scope"], status["fingerprint"])
		stage = "publish"
		authorize_scope(status["scope"], expected_fingerprint=status["fingerprint"], result=result)
		# The split is strict: an incomplete result fails here, before anything is stored.
		if not store.publish_snapshot(actor, snapshot_id, store.split_result(result, status["scope"])):
			frappe.logger().info("Payment Calendar job: result_discarded")
	except Exception as exc:
		_fail_safely(actor, snapshot_id, _cause(exc), exc, stage, started, "snapshot")


def run_account_balances(actor: str, balances_id: str) -> None:
	started, stage = monotonic(), "admission"
	try:
		if frappe.session.user != actor:
			raise frappe.PermissionError("Payment Calendar actor changed.")
		status = store.load_status(actor, balances_id)
		if not status or status.get("kind") != balances.KIND:
			frappe.logger().info("Payment Calendar job: expired_before_start")
			return
		if not store.mark_running(actor, balances_id):
			return
		stage = "sources"
		result = balances.collect(status["scope"])
		stage = "publish"
		if not store.publish(actor, balances_id, result):
			frappe.logger().info("Payment Calendar job: result_discarded")
			return
		_log_balances_done(started, len(result["accounts"]))
	except Exception as exc:
		_fail_safely(actor, balances_id, _balances_cause(exc), exc, stage, started, balances.KIND)


def _balances_cause(exc):
	# An account disabled or moved meanwhile is a changed scope, not a bug.
	if isinstance(exc, PermissionDeniedError):
		return "forbidden"
	if isinstance(exc, frappe.ValidationError) and not isinstance(exc, CalendarSourceUnavailable):
		return "scope_changed"
	return _cause(exc)


def _log_balances_done(started, accounts):
	duration = elapsed_ms(started)
	message = f"balances job complete duration_ms={duration} accounts={accounts}"
	logger = frappe.logger("payment_calendar")
	logger.info(message)
	if duration >= SLOW_JOB_MS:
		logger.warning(f"slow {message}")


def _fail_safely(actor, job_id, code, exc, stage, started, kind):
	# No exception reaches execute_job's retry/traceback logger. The source
	# context has already rolled back its read-only transaction on failure.
	try:
		store.fail(actor, job_id, code)
		if code in ("scope_changed", "forbidden", "expired"):
			# Expected outcomes are not errors, but an operator still sees which class caused them.
			frappe.logger("payment_calendar").warning(
				f"{kind} job {code} exception_class={type(exc).__name__} stage={stage}"
			)
			return
		body = {
			"code": code,
			"kind": kind,
			"exception_class": type(exc).__name__,
			"stage": stage,
			"duration_ms": elapsed_ms(started),
		}
		detail = safe_detail(exc)
		if detail:
			body["detail"] = detail
		frappe.log_error(title=f"Payment Calendar {kind}", message=json.dumps(body))
	except Exception:
		frappe.logger().warning("Payment Calendar job: safe_failure_unavailable")
