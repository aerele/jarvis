"""Query a complete owner-scoped Prepared Report without generating another run."""

import re

from jarvis.exceptions import InvalidArgumentError, SnapshotReloadedError
from jarvis.tools import _report_snapshot_reload, _report_snapshots
from jarvis.tools._report_query_process import WALL_SECONDS, Deadline, analyze_report
from jarvis.tools._report_query_source import ReportSource, _db_budget
from jarvis.tools._spreadsheet_query import MAX_QUERY_CHARS

# The plugin aborts a call at 30 s (frappe-client.ts); the handback of a reload keeps 3 s of margin.
CALL_BUDGET_S = 27
MIN_INSPECT_S = 2


def query_report(prepared_report: str, sql=None, version=None, max_rows=100):
	if not isinstance(prepared_report, str) or not prepared_report or len(prepared_report) > 140:
		raise InvalidArgumentError(
			"prepared_report must be the exact ID in the run_report response field run."
		)
	if sql is not None and (not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_QUERY_CHARS):
		raise InvalidArgumentError("sql must be one SELECT of at most 12,000 characters, or null to inspect.")
	version = version or None
	if version is not None and (not isinstance(version, str) or not re.fullmatch("[a-f0-9]{64}", version)):
		raise InvalidArgumentError("version must be the token returned by report inspection.")
	if sql is not None and version is None:
		raise InvalidArgumentError("Inspect the prepared report first, then supply its version with SQL.")
	if type(max_rows) is not int or not 1 <= max_rows <= 200:
		raise InvalidArgumentError("max_rows must be an integer from 1 to 200.")
	if not prepared_report.startswith("rs_"):
		return _query(prepared_report, sql, version, max_rows, snapshot=False)
	started = _report_snapshot_reload.clock()
	_report_snapshots.begin_read()
	try:
		# Before Deadline(): the snapshot checks and a reload must not spend the query's budget.
		reloaded = _report_snapshots.ensure_live(prepared_report)
		if isinstance(reloaded, _report_snapshots.ReloadInfo):
			raise _after_reload(prepared_report, sql, max_rows, reloaded, started)
		return _query(prepared_report, sql, version, max_rows, snapshot=True)
	except Exception as exc:
		_report_snapshots.record_read_error(prepared_report, exc)
		raise


def _query(prepared_report, sql, version, max_rows, *, snapshot, seconds=WALL_SECONDS, record=True):
	deadline = Deadline(seconds)
	with _db_budget(deadline):
		source = (
			_report_snapshots.SnapshotSource(prepared_report, version=version)
			if snapshot
			else ReportSource(prepared_report, version=version)
		)
		content, metadata = source.read()
	result = analyze_report(
		content,
		metadata,
		authorize=source.authorize,
		validate=source.validate,
		sql=sql,
		max_rows=max_rows,
		deadline=deadline,
	)
	deadline.check()
	source.confirm()
	if not snapshot:
		return {**result, **source.receipt()}
	receipt = source.receipt(sql_executed=sql is not None)
	if record:
		source.record_live(sql_executed=sql is not None)
	return {**result, **receipt}


def _after_reload(snapshot_id, sql, max_rows, reloaded, started):
	"""The error that answers a call that triggered a reload (B.9.5, shape B). Every such
	call ends here, whatever it asked: the model inspects again and the SQL of the call
	never runs. It inspects under what is left of the call budget to hand over the new
	version, else the error carries the note alone. One `read` event is sent for the call."""
	left = CALL_BUDGET_S - (_report_snapshot_reload.clock() - started)
	inspected = _inspect(snapshot_id, max_rows, min(WALL_SECONDS, left)) if left >= MIN_INSPECT_S else None
	_report_snapshots.read_event(
		"reloaded",
		snapshot_id,
		{"generation": reloaded.generation},
		**({"evicted": True} if reloaded.evicted else {}),
	)
	return _reload_error(inspected, reloaded, sql_given=sql is not None)


def _inspect(snapshot_id, max_rows, seconds):
	"""The inspect result, or None when it failed for any reason (a deadline, a busy worker,
	a refusal, an error): the reload happened, so the call still answers with the reload note
	and the next inspect shows the real state. No `read` event of its own."""
	try:
		with _report_snapshots.muted_reads():
			return _query(snapshot_id, None, None, max_rows, snapshot=True, seconds=seconds, record=False)
	except Exception as exc:
		_report_snapshots.log("inspect_after_reload_failed", exc)
		return None


def _reload_error(inspected, reloaded, *, sql_given):
	"""The one place that words the error of a reloaded call: the reload note, with the new
	version when the inspect fitted the budget. Wire code `snapshot_reloaded`."""
	version = inspected.get("version") if inspected else None
	if reloaded.perm_changed:
		changed = (
			"Access or report definition and data changed"
			if reloaded.data_changed
			else "Access or report definition changed"
		)
	else:
		changed = "Data changed" if reloaded.data_changed else "Data unchanged"
	message = (
		f"The report snapshot expired and was reloaded at {reloaded.reloaded_at} with the same filters. "
		f"{changed}. "
		+ ("Your SQL did not run. " if sql_given else "")
		+ (f"Send it again with version {version}. " if version else "Inspect it again, then send your SQL. ")
		+ "Do not combine numbers from before the reload with numbers after it."
	)
	return SnapshotReloadedError(message, version=version)
