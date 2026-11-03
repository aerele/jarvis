"""Query a complete owner-scoped Prepared Report without generating another run."""

import re

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._report_query_process import Deadline, analyze_report
from jarvis.tools._report_query_source import ReportSource, _db_budget
from jarvis.tools._spreadsheet_query import MAX_QUERY_CHARS


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
	deadline = Deadline()
	with _db_budget(deadline):
		source = ReportSource(prepared_report, version=version)
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
	return {**result, **source.receipt()}
