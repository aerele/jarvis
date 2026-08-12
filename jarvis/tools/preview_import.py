"""Read-only preview of a would-be Data Import.

Given an attached file and a target DocType, returns the column-to-field mapping, up to
10 sample rows, validation warnings (blocking vs advisory), the parent-record count, and
whether the caller can actually run the import - WITHOUT creating anything. The whole
safety core (caller-scoped File read, reused Frappe import gate, secret masking, bounds,
blocking classification) lives in ``_import_preview.build_import_preview``.

Use this BEFORE ``run_import`` to show the customer what a raw file load would produce,
and to catch a mismatched file / blocking values early. For data that needs any
transformation before it becomes records, use ``create_doc(docs=[...])`` instead - import
is only for loading a file as-is.
"""

from jarvis.tools._import_preview import build_import_preview


def preview_import(
	doctype: str,
	file_url: str | None = None,
	filename: str | None = None,
	import_type: str = "Insert New Records",
	mapping: dict | None = None,
) -> dict:
	"""Preview a Data Import without creating anything.

	Identify the file by ``file_url`` (exact, preferred) or ``filename`` (most recent
	readable match). ``import_type`` is one of "Insert New Records" /
	"Update Existing Records" / "Insert or Update Records". ``mapping`` optionally forces
	a column onto a field: ``{header_or_index: fieldname | "table.field" | "Don't Import"}``.

	Requires the caller's read permission on the file and import permission on the target
	DocType. Runs as the calling user.
	"""
	return build_import_preview(
		doctype=doctype,
		file_url=file_url,
		filename=filename,
		import_type=import_type,
		mapping=mapping,
	)
