"""Server-side, permission-checked bulk export of a doctype query to a
downloadable Excel/CSV file.

The rows are gathered and rendered SERVER-SIDE and never returned to the agent's
context (reference-not-rows), so an export is COMPLETE - it is not subject to the
model-facing result budget that caps get_list/query. Above a hard row ceiling the
resolver FAILS CLOSED (raises) rather than hand back a silently-partial file.

This is the tool for "export these records to Excel/CSV" - NOT get_list +
export_excel, whose rows pass through the model context and are truncatable."""

from jarvis import telemetry
from jarvis.exceptions import InvalidArgumentError, NoDataError, PermissionDeniedError
from jarvis.tools._export import renderers, resolvers, save_export_file

_FORMATS = {
	"xlsx": (
		"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		renderers.xlsx,
	),
	"csv": ("text/csv", renderers.csv),
}


def _outcome_for(exc: Exception) -> str:
	if isinstance(exc, NoDataError):
		return "no_data"
	if isinstance(exc, PermissionDeniedError):
		return "denied"
	return "rejected"  # InvalidArgumentError: ceiling, bad fields, file-too-large


def export_query(
	doctype,
	filters=None,
	fields=None,
	order_by=None,
	format="xlsx",
	title=None,
	parent_doctype=None,
) -> dict:
	"""Export ``doctype`` rows matching ``filters`` to ``format`` (``xlsx``|``csv``).

	Returns the download-card envelope ``{file_url, filename, title, mime_type,
	size_bytes, name}`` plus ``total`` (the TRUE, permission-filtered source count),
	and ``cells_truncated``/``note`` when an over-long cell was clipped. Data is
	fetched and rendered server-side under the caller's permissions; it never enters
	the model context, so the export is complete (nothing truncated). Pass
	``parent_doctype`` to export a child (Table) DocType, whose permission derives
	from its parent. Raises ``NoDataError`` when nothing matches (an empty file would
	look complete but hold nothing)."""
	fmt = str(format or "xlsx").lower()
	if fmt not in _FORMATS:
		raise InvalidArgumentError(f"format must be one of {sorted(_FORMATS)}, got {format!r}")
	mime, render = _FORMATS[fmt]

	# Every fail-closed exit emits a telemetry outcome too (not just success), so the
	# refused exports - the ones that would justify raising the ceiling - are visible.
	try:
		model = resolvers.from_query(
			doctype, filters=filters, fields=fields, order_by=order_by, parent_doctype=parent_doctype
		)
		if model.total == 0:
			raise NoDataError(f"No {doctype} records match those filters - nothing to export.")
		model.meta["title"] = title or doctype
		env = save_export_file(f"x.{fmt}", render(model), title=title or doctype, mime_type=mime)
	except (NoDataError, PermissionDeniedError, InvalidArgumentError) as e:
		telemetry.record_export_event(
			tool="export_query", fmt=fmt, rows=0, mode="sync", outcome=_outcome_for(e)
		)
		raise

	env["total"] = model.total
	if model.meta.get("cells_truncated"):
		env["cells_truncated"] = True
		env["note"] = (
			"Some cells exceeded the spreadsheet cell-size limit and were truncated with "
			"a marker; the row count is complete."
		)
	telemetry.record_export_event(tool="export_query", fmt=fmt, rows=model.total, mode="sync", outcome="ok")
	return env
