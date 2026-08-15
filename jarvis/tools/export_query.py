"""Server-side, permission-checked bulk export of a doctype query to a
downloadable Excel/CSV file.

The rows are gathered and rendered SERVER-SIDE and never returned to the agent's
context (reference-not-rows), so an export is COMPLETE - it is not subject to the
model-facing result budget that caps get_list/query. Above a hard row ceiling the
resolver FAILS CLOSED (raises) rather than hand back a silently-partial file.

This is the tool for "export these records to Excel/CSV" - NOT get_list +
export_excel, whose rows pass through the model context and are truncatable."""

from jarvis import telemetry
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export import renderers, resolvers, save_export_file

_FORMATS = {
	"xlsx": (
		"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		renderers.xlsx,
	),
	"csv": ("text/csv", renderers.csv),
}


def export_query(
	doctype,
	filters=None,
	fields=None,
	order_by=None,
	format="xlsx",
	title=None,
) -> dict:
	"""Export ``doctype`` rows matching ``filters`` to ``format`` (``xlsx``|``csv``).

	Returns the download-card envelope ``{file_url, filename, title, mime_type,
	size_bytes, name}`` plus ``total`` (the TRUE, permission-filtered source count).
	Data is fetched and rendered server-side under the caller's permissions; it
	never enters the model context, so the export is complete (nothing truncated)."""
	fmt = (format or "xlsx").lower()
	if fmt not in _FORMATS:
		raise InvalidArgumentError(f"format must be one of {sorted(_FORMATS)}, got {format!r}")

	model = resolvers.from_query(doctype, filters=filters, fields=fields, order_by=order_by)
	model.meta["title"] = title or doctype

	mime, render = _FORMATS[fmt]
	env = save_export_file(f"x.{fmt}", render(model), title=title or doctype, mime_type=mime)
	env["total"] = model.total
	if model.meta.get("cells_truncated"):
		env["cells_truncated"] = True
		env["note"] = (
			"Some cells exceeded the spreadsheet cell-size limit and were truncated with "
			"a marker; the row count is complete."
		)
	telemetry.record_export_event(tool="export_query", fmt=fmt, rows=model.total, mode="sync")
	return env
