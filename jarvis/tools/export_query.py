"""Server-side, permission-checked bulk export of a doctype query to a
downloadable Excel/CSV file.

The rows are gathered and rendered SERVER-SIDE and never returned to the agent's
context (reference-not-rows), so an export is COMPLETE - it is not subject to the
model-facing result budget that caps get_list/query. Above a hard row ceiling the
resolver FAILS CLOSED (raises) rather than hand back a silently-partial file.

This is the tool for "export these records to Excel/CSV" - NOT get_list +
export_excel, whose rows pass through the model context and are truncatable.
Records a plain filter cannot pick ("suppliers with no invoice in a year": a NOT
EXISTS, a join, an aggregate) are exported with ``spec``, the same structured
query the ``query`` tool takes, compiled under the same permission gates."""

import time

from jarvis import telemetry
from jarvis.exceptions import (
	ExportTooSlowError,
	InvalidArgumentError,
	NoDataError,
	PermissionDeniedError,
)
from jarvis.tools._export import renderers, resolvers, save_export_file

# Seconds the whole export may take before it is refused instead of saved. The plugin
# aborts every call_tool at 30 s (jarvis-openclaw-plugin src/frappe-client.ts): a file
# finished after that is one nobody receives, and the agent retries the same heavy
# query. Checked after the query and after the render, BEFORE the File is written,
# so a refused export leaves nothing behind. The margin to 30 s is the save itself.
_EXPORT_DEADLINE_S = 25

_FORMATS = {
	"xlsx": (
		"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		renderers.xlsx,
	),
	"csv": ("text/csv", renderers.csv),
}


def _detail(spec, exc: Exception) -> str:
	"""Which form failed and why, short, for the export telemetry."""
	return (f"spec: {exc}" if spec else str(exc))[:200]


def _outcome_for(exc: Exception) -> str:
	if isinstance(exc, NoDataError):
		return "no_data"
	if isinstance(exc, ExportTooSlowError):
		return "too_slow"
	if isinstance(exc, PermissionDeniedError):
		return "denied"
	return "rejected"  # InvalidArgumentError: ceiling, bad fields, file-too-large


def export_query(
	doctype=None,
	filters=None,
	fields=None,
	order_by=None,
	format="xlsx",
	title=None,
	parent_doctype=None,
	highlights=None,
	spec=None,
) -> dict:
	"""Export ``doctype`` rows matching ``filters`` - or every row of the ``query``
	``spec`` - to ``format`` (``xlsx``|``csv``). Exactly one of ``doctype`` and
	``spec``; ``filters`` / ``fields`` / ``order_by`` / ``parent_doctype`` belong to
	the ``doctype`` form. Headers are the fields' labels.

	Returns the download-card envelope ``{file_url, filename, title, mime_type,
	size_bytes, name}`` plus ``total`` (the TRUE, permission-filtered source count),
	and ``cells_truncated``/``note`` when an over-long cell was clipped. Data is
	fetched and rendered server-side under the caller's permissions; it never enters
	the model context, so the export is complete (nothing truncated). Pass
	``parent_doctype`` to export a child (Table) DocType, whose permission derives
	from its parent. Raises ``NoDataError`` when nothing matches (an empty file would
	look complete but hold nothing).

	One exception to "complete": a ``spec`` with its own ``limit`` exports only that
	many rows (a deliberate top-N), and ``total`` is then the file's row count. When
	the limit actually cut the result the envelope carries ``limited_by_spec: <N>``
	and a ``note`` saying the file holds the first N rows only; call again WITHOUT
	``limit`` in the spec to export every matching row. A limit copied from a
	``query`` call is usually not wanted here. When nothing was cut, neither key is
	set.

	A column masked for the user (Frappe 16 field masking) is masked in the file as
	in a list; a computed column over a masked field is refused. An export that
	would outlast the call timeout (a slow query or a very large render) is refused
	before any file is saved (``ExportTooSlowError``): narrow it or split it by a
	filter.

	``highlights`` (xlsx only) uses the same column/operator/value/style/scope
	rules as export_excel, referring to exact exported field headers. Formatting
	runs after cell sanitation, without returning source rows to the agent.
	``highlight_count`` counts rules, not matching cells."""
	fmt = str(format or "xlsx").lower()
	if fmt not in _FORMATS:
		raise InvalidArgumentError(f"format must be one of {sorted(_FORMATS)}, got {format!r}")
	mime, render = _FORMATS[fmt]
	budget = _Budget(_EXPORT_DEADLINE_S)

	# Every fail-closed exit emits a telemetry outcome too (not just success), so the
	# refused exports - the ones that would justify raising the ceiling - are visible.
	try:
		if fmt == "csv" and highlights is not None:
			raise InvalidArgumentError("highlights requires format xlsx; CSV cannot store formatting")
		if bool(doctype) == bool(spec):
			raise InvalidArgumentError(
				"pass doctype (with filters) for records a plain filter selects, or spec (the query "
				"tool's structured query) for anything else - one of the two"
			)
		if spec:
			if any(v is not None for v in (filters, fields, order_by, parent_doctype)):
				raise InvalidArgumentError(
					"with spec, put the filters, columns and order in the spec itself "
					"(where / select / order_by)"
				)
			model = resolvers.from_spec(spec)
		else:
			model = resolvers.from_query(
				doctype, filters=filters, fields=fields, order_by=order_by, parent_doctype=parent_doctype
			)
		source = model.meta.get("doctype") or doctype
		if model.total == 0:
			raise NoDataError(f"No {source} records match - nothing to export.")
		budget.check()
		model.meta["title"] = title or source
		content = render(model, highlights=highlights) if fmt == "xlsx" else render(model)
		budget.check()
		env = save_export_file(f"x.{fmt}", content, title=title or source, mime_type=mime)
	except (NoDataError, PermissionDeniedError, InvalidArgumentError) as e:
		telemetry.record_export_event(
			tool="export_query",
			fmt=fmt,
			rows=0,
			mode="sync",
			outcome=_outcome_for(e),
			detail=_detail(spec, e),
		)
		raise
	except Exception as e:
		# A database error or an unexpected failure is still an export that did not
		# happen: count it, then let the caller's error handling report it.
		telemetry.record_export_event(
			tool="export_query", fmt=fmt, rows=0, mode="sync", outcome="error", detail=_detail(spec, e)
		)
		raise

	env["total"] = model.total
	if fmt == "xlsx":
		env["highlight_count"] = model.meta["highlight_count"]
	notes = []
	if model.meta.get("cells_truncated"):
		env["cells_truncated"] = True
		notes.append(
			"Some cells exceeded the spreadsheet cell-size limit and were truncated with "
			"a marker; the row count is complete."
		)
	if model.meta.get("limited_by_spec"):
		cap = env["limited_by_spec"] = model.meta["limited_by_spec"]
		notes.append(
			f"The file holds only the first {cap} rows because the spec's limit was {cap}, "
			f"and more rows match. To export every matching row, call again without "
			f"limit in the spec."
		)
	if notes:
		env["note"] = " ".join(notes)
	telemetry.record_export_event(tool="export_query", fmt=fmt, rows=model.total, mode="sync", outcome="ok")
	return env


class _Budget:
	"""The wall-clock time one export call may use, from the moment it starts."""

	def __init__(self, seconds: int):
		self.seconds = seconds
		self._start = time.monotonic()

	def check(self) -> None:
		"""Refuse the export once it is over budget. Called before the File is saved,
		so nothing is written for a call whose caller has already given up."""
		if time.monotonic() - self._start > self.seconds:
			raise ExportTooSlowError(
				f"this export took longer than {self.seconds} seconds to build, more than a call "
				f"may take, so no file was saved. Narrow it (a date range or a status filter), "
				f"or split it by a filter into a few exports."
			)
