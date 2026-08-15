import re

# The single place an export becomes a File + download-card envelope, so every
# export tool (new export_query AND the existing report_pdf/export_excel) shares
# one shape. The chat surface renders the card automatically from this envelope
# via api._maybe_attach_artifact (keyed on the filename extension -> canvas._EXT_TYPE).


def _safe_base(title: str) -> str:
	"""File-doctype-safe base name from a caller title.

	A title can carry markup, slashes and other characters Frappe's File
	sanitiser rejects or rewrites - notably it re-introduces a ``/`` from a
	closing HTML tag (``</b>``), which then raises ``ValidationError: File name
	cannot have /`` deep inside ``save_file`` (a bug already hit for a sibling
	tool). Keep only filename-safe characters, cap the length, never return
	empty. The single filename sanitiser for every export tool."""
	base = re.sub(r"[^A-Za-z0-9._-]+", "-", title or "").strip("-.")
	return base[:80] or "export"


def save_export_file(
	filename: str,
	content: bytes,
	title: str,
	mime_type: str,
	dt: str | None = None,
	dn: str | None = None,
) -> dict:
	"""Persist ``content`` as a PRIVATE File and return the download-card envelope
	``{file_url, filename, title, mime_type, size_bytes, name}``.

	The base name comes from ``title`` (sanitised); the extension comes from
	``filename`` (it drives the chat render type). ``dt``/``dn`` default to None
	-> an UNATTACHED private File, whose ``has_permission`` falls through to
	owner-only, so an export is readable only by the user who ran it. Pass
	``dt``/``dn`` only to attach to a record the same audience may already read
	(e.g. download_pdf attaching to its own record)."""
	from frappe.exceptions import ValidationError
	from frappe.utils.file_manager import save_file

	from jarvis.exceptions import InvalidArgumentError

	ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
	fname = f"{_safe_base(title)}.{ext}"
	try:
		fdoc = save_file(fname, content, dt, dn, is_private=1)
	except ValidationError as e:
		# save_file raises ValidationError for a File-save constraint - notably
		# MaxFileSizeReachedError when the bytes exceed the site's max_file_size
		# (default 10 MB), which a large but under-row-ceiling export can hit. A big
		# export is legitimate, so translate to a clean, actionable error for BOTH
		# callers rather than an opaque 500 / Error Log.
		raise InvalidArgumentError(
			f"could not store the export file (it may exceed the site's maximum file "
			f"size - narrow the data or export fewer fields): {e}"
		) from e
	return {
		"file_url": fdoc.file_url,
		"filename": fdoc.file_name,
		"title": title or "Export",
		"mime_type": mime_type,
		"size_bytes": int(fdoc.file_size or len(content)),
		"name": fdoc.name,
	}
