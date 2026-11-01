"""Server-owned execution for the Stock Variance auditor.

The delegate can start this execution, but it cannot supply a source path,
manifest, source row, evaluator output, or dashboard HTML. All source reads stay
in this request under the run-as user's normal Jarvis query permissions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote

import frappe

from jarvis.chat import stock_variance_native as native
from jarvis.chat import stock_variance_render as renderer
from jarvis.exceptions import InvalidArgumentError

AGENT = "stock-variance-auditor"
CONTRACT = "stock-variance-server-v1"
RUN = "Jarvis Agent Run"
INSTALLATION = "Jarvis Agent Installation"

# Keep a margin below the plugin's 30-second HTTP deadline.
MAX_WALL_SECONDS = 20.0
MAX_TOTAL_ROWS = 20_000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PAGES_PER_SOURCE = 100
MAX_STATEMENT_SECONDS = 2
_MAX_SAFE_INT = (1 << 53) - 1

_server_writeback = ContextVar("jarvis_stock_variance_server_writeback", default=False)


class CaptureFailure(Exception):
	"""A controlled failure that must produce a non-clean terminal result."""

	def __init__(self, detail: str, *, reason_code: str, state: str = "not_evaluable"):
		super().__init__(detail)
		self.detail = detail
		self.reason_code = reason_code
		self.state = state


@contextmanager
def server_writeback():
	"""Allow only this trusted runner to use delegate writeback tools."""
	token = _server_writeback.set(True)
	try:
		yield
	finally:
		_server_writeback.reset(token)


def is_server_writeback() -> bool:
	return bool(_server_writeback.get())


def _json_object(raw, label: str) -> dict:
	try:
		value = frappe.parse_json(raw or "{}")
	except (TypeError, ValueError) as exc:
		raise CaptureFailure(
			f"The saved {label} is unavailable. Start a new review.",
			reason_code="configuration_missing",
		) from exc
	if not isinstance(value, dict):
		raise CaptureFailure(
			f"The saved {label} is unavailable. Start a new review.",
			reason_code="configuration_missing",
		)
	return value


def _json_clone(value):
	"""Use the same JSON-shaped values that a private query result carries."""
	try:
		return json.loads(frappe.as_json(value))
	except (TypeError, ValueError, OverflowError) as exc:
		raise CaptureFailure(
			"A required stock source cannot be serialized. Run the review again.",
			reason_code="record_coverage_insufficient",
		) from exc


def _source_bytes(value) -> bytes:
	try:
		return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
	except (TypeError, ValueError, OverflowError) as exc:
		raise CaptureFailure(
			"A required stock source cannot be serialized. Run the review again.",
			reason_code="record_coverage_insufficient",
		) from exc


def _remaining(started: float) -> float:
	remaining = MAX_WALL_SECONDS - (time.monotonic() - started)
	if remaining <= 0:
		raise CaptureFailure(
			"The stock review exceeded its safe server time limit. Narrow the warehouse scope or dates.",
			reason_code="run_truncated_watermark",
			state="truncated",
		)
	return remaining


@contextmanager
def _private_query_context():
	"""Do not send intermediate source rows through the model result guard."""
	from jarvis.tools import _private_output

	with _private_output.request(True):
		yield


@contextmanager
def _source_statement_timeout(seconds: int):
	"""Apply a required per-statement source-read timeout on supported databases.

	The generic API helper deliberately treats an unavailable timeout as a no-op.
	That is not safe for this synchronous evaluator: an unbounded source statement
	could exceed the delegate HTTP deadline before the runner can write a typed
	failure. SQLite has no compatible statement timeout, so it is refused before a
	source read. PostgreSQL and MariaDB both have supported server-side controls.
	"""
	db_type = getattr(frappe.db, "db_type", "")
	previous = None
	reset = False
	try:
		if db_type == "mariadb":
			previous = frappe.db.sql("SELECT @@max_statement_time")[0][0]
			if not (0 < float(previous) <= seconds):
				frappe.db.set_execution_timeout(seconds)
				reset = True
		elif db_type == "postgres":
			previous = frappe.db.sql("SHOW statement_timeout")[0][0]
			frappe.db.set_execution_timeout(seconds)
			reset = True
		else:
			raise CaptureFailure(
				"The stock review requires database statement-timeout support.",
				reason_code="unsupported_customisation",
			)
	except CaptureFailure:
		raise
	except Exception as exc:
		raise CaptureFailure(
			"The stock review could not set its safe database time limit. Run it again.",
			reason_code="record_coverage_insufficient",
		) from exc
	body_failed = False
	try:
		yield
	except BaseException:
		body_failed = True
		raise
	finally:
		# A PostgreSQL statement timeout aborts its transaction. The source-capture
		# savepoint below recovers that transaction, and rolling back to it also
		# restores SET LOCAL. Issuing another SET LOCAL here would itself fail and
		# replace the timeout with an unrelated configuration failure.
		if reset and not (db_type == "postgres" and body_failed):
			try:
				if db_type == "mariadb":
					frappe.db.sql("SET SESSION max_statement_time = %s", (previous,))
				else:
					frappe.db.sql("SET LOCAL statement_timeout = %s", (previous,))
			except Exception as exc:
				raise CaptureFailure(
					"The stock review could not restore its database time limit. Run it again.",
					reason_code="record_coverage_insufficient",
				) from exc


@contextmanager
def _source_capture_savepoint():
	"""Recover an aborted source query without releasing the run execution claim.

	The runner locks the Agent Run before it enters this context. PostgreSQL marks
	the surrounding transaction aborted after a statement timeout, but ROLLBACK TO
	this later savepoint restores it while retaining that earlier row lock. That
	lets the runner record one typed terminal result instead of returning a 500 or
	leaving a claimed run running.
	"""
	savepoint = f"jarvis_stock_variance_source_{frappe.generate_hash(length=10)}"
	try:
		frappe.db.savepoint(savepoint)
	except Exception as exc:
		raise CaptureFailure(
			"The stock review could not prepare its safe source transaction. Run it again.",
			reason_code="record_coverage_insufficient",
		) from exc
	try:
		yield
	except BaseException:
		try:
			frappe.db.rollback(save_point=savepoint)
		except Exception as exc:
			# A connection-level failure can remove the savepoint. Reset the failed
			# transaction before the runner reclaims the run row for its typed failure
			# writeback. The ordinary timeout path above keeps the original row lock.
			try:
				frappe.db.rollback()
			except Exception:
				pass
			raise CaptureFailure(
				"The stock review could not recover its source transaction. Run it again.",
				reason_code="record_coverage_insufficient",
			) from exc
		raise
	else:
		try:
			frappe.db.release_savepoint(savepoint)
		except Exception as exc:
			# A PostgreSQL error while releasing the savepoint can leave this
			# transaction aborted just like a timed-out source statement. Recover it
			# before the caller tries to persist the typed failure.
			try:
				frappe.db.rollback(save_point=savepoint)
			except Exception:
				try:
					frappe.db.rollback()
				except Exception:
					pass
			raise CaptureFailure(
				"The stock review could not finalize its source transaction. Run it again.",
				reason_code="record_coverage_insufficient",
			) from exc


def _statement_timed_out(exc: Exception) -> bool:
	"""Use the active Frappe database adapter's timeout classification."""
	try:
		return bool(frappe.db.is_statement_timeout(exc))
	except Exception:
		return False


def _read_page(spec: dict, started: float) -> dict:
	"""Run one server-authored, permission-checked source page."""
	remaining = _remaining(started)
	from jarvis.tools.query import query

	statement_seconds = max(1, min(MAX_STATEMENT_SECONDS, math.ceil(remaining)))
	try:
		with _source_statement_timeout(statement_seconds):
			result = query(spec, confirm_large=True)
	except CaptureFailure:
		raise
	except frappe.PermissionError as exc:
		raise CaptureFailure(
			"The execution user cannot read the complete stock review source.",
			reason_code="permission_slice",
		) from exc
	except Exception as exc:
		if _statement_timed_out(exc) or time.monotonic() - started >= MAX_WALL_SECONDS:
			raise CaptureFailure(
				"The stock review exceeded its safe server time limit. Narrow the warehouse scope or dates.",
				reason_code="run_truncated_watermark",
				state="truncated",
			) from exc
		raise CaptureFailure(
			"A required stock source could not be read. Run the review again.",
			reason_code="record_coverage_insufficient",
		) from exc
	_remaining(started)
	if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
		raise CaptureFailure(
			"A required stock source returned an invalid result. Run the review again.",
			reason_code="record_coverage_insufficient",
		)
	return _json_clone(result)


def _fingerprint(spec: dict, started: float) -> dict:
	page = _read_page(copy.deepcopy(spec), started)
	rows = page["rows"]
	if page.get("more_rows") or len(rows) != 1 or not isinstance(rows[0], dict):
		raise CaptureFailure(
			"A stock source fingerprint is incomplete. Run the review again.",
			reason_code="record_coverage_insufficient",
		)
	return rows[0]


def _collect_source(
	key: str, spec: dict, started: float, *, already_read_bytes: int = 0
) -> tuple[list[dict], int]:
	"""Collect each cursor page, with row, byte, and deadline caps."""
	page_spec = copy.deepcopy(spec)
	base_where = list(page_spec.get("where") or [])
	limit = page_spec.get("limit")
	if type(limit) is not int or limit <= 0:
		raise CaptureFailure("The trusted stock query plan is invalid.", reason_code="configuration_missing")
	rows: list[dict] = []
	seen: set[str] = set()
	bytes_read = 0
	for _page in range(MAX_PAGES_PER_SOURCE):
		page = _read_page(page_spec, started)
		page_rows = page["rows"]
		for row in page_rows:
			if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
				raise CaptureFailure(
					"A stock source has an invalid cursor. Run the review again.",
					reason_code="record_coverage_insufficient",
				)
			if row["name"] in seen:
				raise CaptureFailure(
					"A stock source has a repeated cursor. Run the review again.",
					reason_code="record_coverage_insufficient",
				)
			seen.add(row["name"])
			rows.append(row)
			bytes_read += len(_source_bytes(row)) + 1
			if (
				len(rows) > native.source_row_limit(key)
				or bytes_read > MAX_SOURCE_BYTES
				or already_read_bytes + bytes_read > MAX_TOTAL_SOURCE_BYTES
			):
				raise CaptureFailure(
					"A stock source exceeds the safe source limit. Narrow the warehouse scope or dates.",
					reason_code="run_truncated_watermark",
					state="truncated",
				)
		if len(page_rows) < limit and not page.get("more_rows"):
			return rows, bytes_read
		if not page_rows:
			return rows, bytes_read
		page_spec["where"] = [
			*base_where,
			{"field": "r.name", "op": ">", "value": page_rows[-1]["name"]},
		]
		_remaining(started)
	raise CaptureFailure(
		"A stock source reached its safe page limit. Narrow the warehouse scope or dates.",
		reason_code="run_truncated_watermark",
		state="truncated",
	)


def capture(scope: dict, config: dict, *, started: float | None = None) -> tuple[dict, dict]:
	"""Collect stable native sources in memory, then make receipts server-side."""
	# These functions validate the server-stamped scope/config before any source
	# query is sent. No delegate-authored plan enters this path.
	queries = native.query_specs(scope, config)
	fingerprints = native.fingerprint_specs(scope, config)
	started = time.monotonic() if started is None else started
	data: dict = {"_source_errors": {}, "_byte_receipts": {}}
	source_counts: dict[str, int] = {}
	source_bytes = 0
	with _private_query_context():
		before = {key: _fingerprint(fingerprints[key], started) for key in native.FIELDS}
		for key in native.FIELDS:
			rows, byte_count = _collect_source(key, queries[key], started, already_read_bytes=source_bytes)
			data[key] = rows
			source_counts[key] = len(rows)
			source_bytes += byte_count
			if sum(source_counts.values()) > MAX_TOTAL_ROWS:
				raise CaptureFailure(
					"The selected stock review exceeds its safe total row limit. Narrow the warehouse scope or dates.",
					reason_code="run_truncated_watermark",
					state="truncated",
				)
		after = {key: _fingerprint(fingerprints[key], started) for key in native.FIELDS}

	for key in native.FIELDS:
		rows = data[key]
		body = _source_bytes({"rows": rows})
		data["_byte_receipts"][key] = {
			"byte_sha256": hashlib.sha256(body).hexdigest(),
			"bytes": len(body),
			"semantic_sha256": native.sha(native.semantic(rows)),
		}
	data["source_receipts"] = [
		{
			"source": key,
			"query_sha256": native.sha(queries[key]),
			"before": before[key],
			"after": after[key],
		}
		for key in native.FIELDS
	]
	receipt_body = _source_bytes(data["source_receipts"])
	data["_byte_receipts"]["source_receipts"] = {
		"byte_sha256": hashlib.sha256(receipt_body).hexdigest(),
		"bytes": len(receipt_body),
		"semantic_sha256": native.sha(native.semantic(data["source_receipts"])),
	}
	evidence = [
		{
			"source": key,
			"query_sha256": native.sha(queries[key]),
			"rows": source_counts[key],
			"semantic_sha256": data["_byte_receipts"][key]["semantic_sha256"],
			"before": before[key],
			"after": after[key],
		}
		for key in native.FIELDS
	]
	return data, {
		"source_counts": source_counts,
		"source_bytes": source_bytes,
		"source_digest": native.sha(evidence),
		"elapsed_ms": round((time.monotonic() - started) * 1000),
	}


def _failure_payload(scope: dict, failure: CaptureFailure) -> dict:
	return {
		"findings": [],
		"coverage": {
			native.TOKEN: {
				"state": failure.state,
				"reason_code": failure.reason_code,
				"detail": failure.detail,
			}
		},
		"scope": scope,
		"truncated": failure.state == "truncated",
		"rows_consumed": 0,
		"integrity_digest": None,
	}


def _store_evidence(run_name: str, evidence: dict) -> None:
	"""Persist hashes/counts only. Raw source rows never leave request memory."""
	frappe.db.set_value(
		RUN,
		run_name,
		{
			"assessment_output_json": frappe.as_json(evidence)[:60000],
			"source_bytes_read": max(0, int(evidence.get("source_bytes") or 0)),
		},
		update_modified=False,
	)


def _safe_text(value, default: str = "") -> str:
	"""Return a primitive string for the fixed plugin response contract."""
	if value is None:
		return default
	return value if isinstance(value, str) else str(value)


def _safe_count(value) -> int:
	"""Return a JSON-safe non-negative count, even for a corrupt stored row."""
	try:
		parsed = int(value)
	except (TypeError, ValueError, OverflowError):
		return 0
	return min(max(0, parsed), _MAX_SAFE_INT)


def _summary(result: dict, scope: dict) -> dict:
	"""Return exactly the plugin's fixed, raw-data-free response shape."""
	result = result if isinstance(result, dict) else {}
	run_name = result.get("run")
	row = (
		frappe.db.get_value(
			RUN,
			run_name,
			["status", "result_state", "dashboard", "findings_count", "blocker_count", "coverage_note"],
			as_dict=True,
		)
		if run_name
		else None
	) or {}
	dashboard_value = row.get("dashboard") or result.get("dashboard")
	dashboard = _safe_text(dashboard_value).strip() or None
	return {
		"run": _safe_text(run_name),
		"status": _safe_text(row.get("status") or result.get("status"), "failed"),
		"result_state": _safe_text(row.get("result_state"), "not_evaluable"),
		"findings_count": _safe_count(row.get("findings_count") or result.get("findings_count")),
		"blocker_count": _safe_count(row.get("blocker_count") or result.get("blocker_count")),
		"coverage_note": _safe_text(row.get("coverage_note") or result.get("coverage_note")),
		"dashboard": dashboard,
		"dashboard_url": f"/dashboards/{quote(str(dashboard), safe='')}" if dashboard else None,
		"execution_user": _safe_text(scope.get("execution_user")),
	}


def _persist(run_doc, inst, scope: dict, payload: dict, evidence: dict, html: str | None = None) -> dict:
	"""Use the existing validated writeback and dashboard lifecycle once."""
	from jarvis.chat import agent_runs
	from jarvis.tools import record_agent_run

	_store_evidence(run_doc.name, evidence)
	dashboard = None
	if html is not None:
		if len(html) > renderer.MAX_DASHBOARD_CHARS:
			raise CaptureFailure(
				"The rendered stock report exceeds the safe dashboard size. Narrow the warehouse scope or dates.",
				reason_code="run_truncated_watermark",
				state="truncated",
			)
		from jarvis.tools.save_agent_dashboard import _shadow_gate_html

		visible_html = (
			_shadow_gate_html(html) if (inst.get("activation_state") or "shadow") == "shadow" else html
		)
		# Do not attach it yet. record_agent_run attaches the same named dashboard
		# in its single terminal lifecycle transaction.
		visibility_owner = agent_runs._visibility_owner(inst)
		dashboard = agent_runs.persist_agent_dashboard(
			run_doc,
			inst,
			visible_html,
			title="Stock Count Review",
			set_on_run=False,
			owner_override=visibility_owner,
		)
		# Frappe stamps the current run-as user at insert time. Re-home both
		# dashboard visibility fields before linking it to the terminal run, as the
		# public dashboard writer does, so shadow output stays reviewer-only.
		if (
			visibility_owner
			and frappe.db.get_value("Jarvis Dashboard", dashboard, "owner") != visibility_owner
		):
			frappe.db.set_value(
				"Jarvis Dashboard",
				dashboard,
				{"owner": visibility_owner, "target_user": visibility_owner},
				update_modified=False,
			)
	with server_writeback():
		result = record_agent_run.record_agent_run(dashboard=dashboard, **payload)
	return _summary(result, scope)


def _reclaim_failure_claim(session_key: str, expected_run: str, scope: dict) -> dict | None:
	"""Recheck the claim before terminal failure writeback.

	A normal source-savepoint rollback keeps the original row lock. If a rare
	connection failure removed that savepoint, this short re-check obtains the lock
	again before writeback. A concurrent evaluator that completed while it was
	lost wins; this caller returns its already-safe terminal summary instead.
	"""
	row = frappe.db.get_value(
		RUN,
		{"session_key": session_key},
		["name", "status", "agent"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.name != expected_run:
		raise InvalidArgumentError("the active Stock Variance run is unavailable")
	if row.agent != AGENT:
		raise InvalidArgumentError("this server evaluator is only available to the Stock Variance auditor")
	if row.status != "running":
		return _summary({"run": row.name, "status": row.status}, scope)
	return None


def run_stock_variance_evaluation() -> dict:
	"""Run the only supported Stock Variance evaluator for the active run."""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"run_stock_variance_evaluation must be called by an agent delegate over its run session"
		)
	run_row = frappe.db.get_value(
		RUN,
		{"session_key": session_key},
		["name", "status", "agent", "installation", "scope_json", "assessment_config_json"],
		as_dict=True,
		# This is the execution claim. It remains held through source capture and
		# the terminal writeback, which commits before the runner returns. A retry
		# therefore waits, then reads the terminal row and returns its safe summary
		# without a second source read, dashboard, finding set, or activity row.
		for_update=True,
	)
	if not run_row:
		raise InvalidArgumentError("no agent run is bound to this session")
	if run_row.agent != AGENT:
		raise InvalidArgumentError("this server evaluator is only available to the Stock Variance auditor")
	scope = _json_object(run_row.scope_json, "review scope")
	if run_row.status != "running":
		return _summary({"run": run_row.name, "status": run_row.status}, scope)
	if not run_row.installation:
		raise InvalidArgumentError("run has no installation")
	run_doc = frappe.get_doc(RUN, run_row.name)
	inst = frappe.get_doc(INSTALLATION, run_row.installation)
	try:
		started = time.monotonic()
		config = _json_object(run_row.assessment_config_json, "review settings")
		# Old runs without a launch snapshot must not read a mutable install config.
		native.policy(scope, config)
		with _source_capture_savepoint():
			data, capture_evidence = capture(scope, config, started=started)
		_remaining(started)
		sheet = native.evaluate_native(scope, config, data)
		_remaining(started)
		payload = renderer.writeback_payload(sheet)
		evidence = {
			"contract": CONTRACT,
			"source_digest": capture_evidence["source_digest"],
			"source_counts": capture_evidence["source_counts"],
			"source_bytes": capture_evidence["source_bytes"],
			"elapsed_ms": capture_evidence["elapsed_ms"],
			"result_digest": sheet["integrity_digest"],
			"run_state": sheet["run_state"],
		}
		html = renderer.render(sheet)
		_remaining(started)
		return _persist(run_doc, inst, scope, payload, evidence, html=html)
	except native.Gap as exc:
		failure = CaptureFailure(str(exc), reason_code=exc.reason, state=exc.state)
	except CaptureFailure as exc:
		failure = exc
	except frappe.PermissionError:
		failure = CaptureFailure(
			"The execution user cannot read the complete stock review source.",
			reason_code="permission_slice",
		)
		frappe.logger("jarvis.stock_variance").warning("stock variance source permission denied")
	except Exception:
		failure = CaptureFailure(
			"The stock review could not complete safely. Run it again.",
			reason_code="record_coverage_insufficient",
		)
		# Do not put an exception trace here. It can carry source-row values from a
		# query or evaluator frame. The public result remains a typed safe failure.
		frappe.log_error(
			title="jarvis stock variance: server evaluator failed",
			message="The server-owned Stock Variance evaluator returned an unexpected error.",
		)

	# Fail closed in the same run. The existing typed coverage lifecycle prevents
	# A16 auto-resolution and produces a safe server fallback dashboard.
	evidence = {
		"contract": CONTRACT,
		"outcome": "unavailable",
		"reason_code": failure.reason_code,
		"state": failure.state,
	}
	terminal = _reclaim_failure_claim(session_key, run_row.name, scope)
	if terminal is not None:
		return terminal
	return _persist(run_doc, inst, scope, _failure_payload(scope, failure), evidence)
