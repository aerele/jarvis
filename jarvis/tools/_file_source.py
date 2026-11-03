"""Exact File references, bounded storage reads and prepared-report handoffs."""

from urllib.parse import parse_qsl, urlsplit, urlunsplit

import frappe
from frappe.core.doctype.file.file import File

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

MISSING = "no matching file found"
HANDOFF = "prepared-report-handoff-v1"
MAX_BYTES = 10 * 1024 * 1024


class FileCapacityError(InvalidArgumentError):
	"""The authorized source exceeds its byte budget."""


class FileStorageError(InvalidArgumentError):
	"""The storage adapter cannot provide a bounded local read."""


def resolve_file(file_url=None, filename=None, file_id=None):
	"""Prefer the immutable File ID; never choose an arbitrary duplicated URL."""
	for value, limit in ((file_id, 140), (file_url, 2048), (filename, 256)):
		if value is not None and (not isinstance(value, str) or not value or len(value) > limit):
			raise InvalidArgumentError("Use a valid attachment reference.")
	if file_url:
		parts = urlsplit(file_url)
		query = parse_qsl(parts.query, keep_blank_values=True)
		if parts.fragment or any(key != "fid" for key, _ in query) or len(query) > 1:
			raise PermissionDeniedError(MISSING)
		if query:
			fid = query[0][1]
			if not fid or (file_id and file_id != fid):
				raise PermissionDeniedError(MISSING)
			file_id = fid
			file_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
	if file_id:
		try:
			doc = frappe.get_cached_doc("File", file_id)
		except frappe.DoesNotExistError:
			raise PermissionDeniedError(MISSING) from None
		if (file_url and file_url != doc.file_url) or (filename and filename != doc.file_name):
			raise PermissionDeniedError(MISSING)
		return doc
	if file_url:
		try:
			matches = frappe.get_list(
				"File", filters={"file_url": file_url}, pluck="name", limit_page_length=2
			)
		except frappe.PermissionError:
			raise PermissionDeniedError(MISSING) from None
		if len(matches) != 1:
			raise PermissionDeniedError(MISSING)
		return frappe.get_cached_doc("File", matches[0])
	# Keep the existing plain-file filename convenience, but no archive guessing.
	from jarvis.tools.read_file import _resolve_file

	doc = _resolve_file(None, filename)
	if (doc.file_name or "").lower().endswith(
		(".zip", ".gz")
	) or doc.attached_to_doctype == "Prepared Report":
		raise InvalidArgumentError("Use the exact archive attachment URL or File ID.")
	return doc


def prepared_handoff(doc):
	"""Known report Files never fall through to generic decoding on any denial."""
	if getattr(doc, "attached_to_doctype", None) != "Prepared Report":
		return None
	from jarvis.tools._report_query_source import ReportSource

	try:
		source = ReportSource(doc.attached_to_name)
		if doc.name != source.file.name:
			raise PermissionDeniedError(MISSING)
	except FileCapacityError:
		raise
	except (InvalidArgumentError, PermissionDeniedError, frappe.PermissionError, frappe.DoesNotExistError):
		raise PermissionDeniedError(MISSING) from None
	except Exception:
		# A failed native-report handoff is terminal, including hook failures.
		raise PermissionDeniedError(MISSING) from None
	return {
		"read_contract": HANDOFF,
		"kind": "prepared_report_artifact",
		"coverage": {"complete": False},
		"prepared_report": source.name,
		"next_query": {"tool": "query_report", "args": {"prepared_report": source.name}},
		"note": "This is a stored prepared report. Inspect it with query_report, then pin its report version. Do not decode or export it through another reader.",
	}


def require_file_read(doc):
	if not frappe.has_permission("File", "read", doc=doc.name):
		raise PermissionDeniedError(MISSING)


def read_bounded(doc, limit=MAX_BYTES):
	"""Read at most limit+1 bytes after the caller's authoritative access gate."""
	if int(doc.file_size or 0) > limit:
		raise FileCapacityError("The source exceeds the 10 MiB input limit. No complete answer was returned.")
	# Custom remote storage must supply a bounded adapter before this path supports it.
	if doc.is_folder or doc.is_remote_file or getattr(type(doc), "get_content", None) is not File.get_content:
		raise FileStorageError("This storage adapter cannot provide a bounded source read.")
	doc.validate_file_path()
	doc.validate_file_url()
	try:
		# Security: File metadata is permission-gated and validated by Frappe;
		# archive/member names never participate in this path.
		with open(doc.get_full_path(), "rb") as stream:  # nosemgrep: frappe-security-file-traversal
			content = stream.read(limit + 1)
	except OSError:
		raise InvalidArgumentError("The stored file is unavailable. Select a current attachment.") from None
	if len(content) > limit:
		raise FileCapacityError("The source exceeds the 10 MiB input limit. No complete answer was returned.")
	return content
