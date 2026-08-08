"""Cross-major shims for the Frappe / ERPNext APIs this app calls.

``pyproject.toml`` declares ``frappe = ">=15.0.0,<17.0.0"``, so the customer app
has to run on both majors. Three APIs moved between 15 and 16 in ways that are
not backward compatible, and each one was written against 16 and shipped onto
15 benches, where it raised at call time:

* ``File.get_content`` grew an ``encodings`` kwarg in 16. Passing it on 15 is a
  ``TypeError``, which broke every chat attachment and every ``read_file`` call.
* ``frappe.utils.xlsxutils`` was rewritten from openpyxl to xlsxwriter in 16.
  ``XLSXStyleBuilder`` does not exist on 15, and 15's ``make_xlsx`` cannot build
  a multi-tab workbook at all.
* ERPNext's ``get_itemised_tax`` takes the taxes child table on 15 and the
  parent document on 16.

Following the doctrine in :mod:`jarvis.learning.compat`, every shim probes a
capability (a signature, an importable symbol) instead of branching on
``frappe.__version__``. A version string says nothing about a backport, and the
bug this module fixes was caused by assuming one major's shape held everywhere.

Keep module-level imports to the standard library plus ``frappe``: these run in
the chat turn and tool-dispatch hot paths. Heavy optional deps (openpyxl,
xlsxwriter, erpnext) stay lazy inside the functions.
"""

import functools
import inspect

import frappe


@functools.cache
def _get_content_takes_encodings(cls) -> bool:
	"""Does this File class accept ``get_content(encodings=...)``? (Frappe 16)"""
	try:
		return "encodings" in inspect.signature(cls.get_content).parameters
	except (AttributeError, TypeError, ValueError):
		return False


def file_bytes(fdoc) -> bytes:
	"""Raw bytes of a File document, on either Frappe major.

	Frappe 16's ``get_content(encodings=[])`` skips its text-encoding guess loop
	and hands back raw bytes. Frappe 15 has no such kwarg; its ``get_content``
	attempts a single strict ``.decode()`` and falls back to bytes on
	``UnicodeDecodeError``. That is lossless either way: a strict decode only
	succeeds on valid UTF-8, so re-encoding restores the original bytes, and a
	binary (PDF, PNG, xlsx) fails the decode and arrives as bytes untouched.
	"""
	if _get_content_takes_encodings(type(fdoc)):
		content = fdoc.get_content(encodings=[])
	else:
		content = fdoc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8", "replace")
	return content


def xlsx_bytes(sheet_data: list[tuple[str, list]]) -> bytes:
	"""Build a one-or-many-tab .xlsx workbook and return its bytes.

	Probes for ``XLSXStyleBuilder`` (the Frappe 16 rewrite) rather than for the
	``xlsxwriter`` package: xlsxwriter is a Frappe 16 dependency, but nothing
	stops it being present in a Frappe 15 virtualenv, and taking the 16 path
	there would fail in ``make_xlsx`` instead of here.
	"""
	try:
		from frappe.utils.xlsxutils import XLSXStyleBuilder
	except ImportError:
		return _xlsx_bytes_openpyxl(sheet_data)
	return _xlsx_bytes_xlsxwriter(sheet_data)


def _xlsx_bytes_xlsxwriter(sheet_data: list[tuple[str, list]]) -> bytes:
	"""Frappe 16: mirror ``make_xlsx``'s own workbook options so dates format
	identically, then let it append a bold-header worksheet per tab."""
	from io import BytesIO

	import xlsxwriter
	from frappe.utils.xlsxutils import XLSXStyleBuilder, make_xlsx

	out = BytesIO()
	wb = xlsxwriter.Workbook(
		out,
		{
			"constant_memory": True,
			"default_date_format": XLSXStyleBuilder.get_datetime_format(),
		},
	)
	for name, data in sheet_data:
		make_xlsx(data, name, wb=wb)  # adds a worksheet to `wb`, returns None
	wb.close()
	return out.getvalue()


def _xlsx_bytes_openpyxl(sheet_data: list[tuple[str, list]]) -> bytes:
	"""Frappe 15: build the workbook here instead of via ``make_xlsx``.

	15's ``make_xlsx`` saves the whole workbook on every call and returns the
	buffer, so it can only ever produce one tab: a second call against the same
	write-only workbook dies inside openpyxl's temp-file handling. It also does
	``create_sheet(name, 0)``, which prepends, so looping would reverse the
	caller's tab order. This mirrors its cell handling (bold header row, date
	and datetime number formats, html unwrapping, illegal-character stripping)
	so a workbook exported on 15 matches one exported on 16.
	"""
	import datetime
	from io import BytesIO

	import openpyxl
	from frappe.utils.xlsxutils import ILLEGAL_CHARACTERS_RE, get_excel_date_format, handle_html
	from openpyxl.cell import WriteOnlyCell
	from openpyxl.styles import Font
	from openpyxl.workbook.child import INVALID_TITLE_REGEX

	wb = openpyxl.Workbook(write_only=True)
	date_format, time_format = get_excel_date_format()

	for sheet_name, data in sheet_data:
		ws = wb.create_sheet(INVALID_TITLE_REGEX.sub(" ", sheet_name))
		ws.row_dimensions[1].font = Font(name="Calibri", bold=True)
		for row in data:
			clean_row = []
			for item in row:
				value = handle_html(item) if isinstance(item, str) else item
				if isinstance(value, str) and next(ILLEGAL_CHARACTERS_RE.finditer(value), None):
					value = ILLEGAL_CHARACTERS_RE.sub("", value)
				if isinstance(value, datetime.date):  # datetime is a date subclass
					cell = WriteOnlyCell(ws, value=value)
					cell.number_format = (
						f"{date_format} {time_format}"
						if isinstance(value, datetime.datetime)
						else date_format
					)
					clean_row.append(cell)
				else:
					clean_row.append(value)
			ws.append(clean_row)

	out = BytesIO()
	wb.save(out)
	return out.getvalue()


def itemised_tax(doc, with_tax_account: bool = False) -> dict:
	"""Per-item tax breakup for a taxable document, on either ERPNext major.

	ERPNext 15 is ``get_itemised_tax(taxes, ...)`` and iterates the taxes child
	table. ERPNext 16 is ``get_itemised_tax(doc, ...)`` and reads
	``doc.precision()`` plus ``doc._item_wise_tax_details``.

	Dispatch on the first parameter's name, not on ``try/except TypeError``: the
	16 body raises ``TypeError`` of its own when ``_item_wise_tax_details`` is
	unset, so a blind fallback would swallow that and then fail differently on
	``doc.taxes``. The two majors also return different value shapes; both are
	passed through untouched, because the consumer is the model and inventing a
	common shape would mean fabricating fields one version does not compute.
	"""
	from erpnext.controllers.taxes_and_totals import get_itemised_tax

	try:
		first_param = next(iter(inspect.signature(get_itemised_tax).parameters))
	except (TypeError, ValueError, StopIteration):
		first_param = ""
	# Send the child table only when the callee explicitly asks for ``taxes``;
	# anything else (the 16 name, an unreadable signature, a future rename) gets
	# the document. Defaulting the other way would hand a doc-shaped callee a
	# child table, which is the failure this shim exists to prevent.
	target = doc.get("taxes") if first_param == "taxes" else doc
	return get_itemised_tax(target, with_tax_account=with_tax_account)


def permission_conditions(engine, doctype: str, table):
	"""Row-level permission predicate for a (possibly aliased) table, either major.

	Frappe 16 has ``Engine.get_permission_conditions(doctype, table)``, which
	builds the predicate against the pypika table it is handed and so is
	alias-safe. Frappe 15 has no such method at all: its permission logic lives
	in a separate ``Permission`` class that only does doctype-level
	``has_permission`` checks and yields no row predicate. Calling the 16 method
	on 15 raised ``AttributeError``, escaped the tool layer (the caller catches
	only ``PermissionError``) and returned HTTP 500 for every ``query`` call.

	Returns a criterion to AND into the WHERE, or ``None`` when the user is
	unrestricted. Raises ``frappe.PermissionError`` on both majors when the user
	has neither role permissions nor shared documents; the caller normalises it.
	"""
	if hasattr(engine, "get_permission_conditions"):  # Frappe 16
		return engine.get_permission_conditions(doctype, table)
	user = getattr(engine, "user", None) or frappe.session.user
	return _permission_conditions_v15(doctype, table, user)


def _permission_conditions_v15(doctype: str, table, user: str):
	"""Frappe 15: reuse ``DatabaseQuery.build_match_conditions``, the same engine
	``frappe.get_list`` runs on, so the composition (role permissions, the
	shared-only fallback, the if_owner constraint, User Permissions, and
	``permission_query_conditions`` hooks) is the framework's and not ours.

	It returns SQL qualified with the real table name (``tabToDo``.…), and this
	tool always aliases its tables (``FROM `tabToDo` `td```), so that fragment
	cannot go straight into the outer WHERE: MariaDB rejects it with "Unknown
	column". Rather than rewrite the framework's permission SQL, which would
	silently corrupt any hook that subqueries the same table, restrict the outer
	alias to a name set computed over the UNALIASED table. The framework's SQL is
	then correct exactly as written.
	"""
	from frappe.model.db_query import DatabaseQuery
	from pypika.terms import LiteralValue

	cond_sql = DatabaseQuery(doctype, user=user).build_match_conditions(as_condition=True)
	if not cond_sql:
		return None  # unrestricted, same as Frappe 16 returning None
	# frappe.db.sql percent-formats the statement it runs, so a LIKE pattern
	# coming from a hook has to be escaped or execution dies with "unsupported
	# format character". This is exactly what frappe's own get_match_cond does.
	inner = frappe.qb.DocType(doctype)
	sub = frappe.qb.from_(inner).select(inner.name).where(LiteralValue(cond_sql.replace("%", "%%")))
	return table.name.isin(sub)
