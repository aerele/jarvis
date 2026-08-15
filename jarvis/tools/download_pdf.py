"""Generate a PDF for a Frappe document via a print format and stash
it in the customer's File DocType so the chat surface can hand the
customer a download link.

The bundled Frappe handler (``frappe.utils.print_format.download_pdf``)
writes to ``frappe.local.response`` for an HTTP download cycle; that
shape doesn't compose with a JSON tool envelope. We call the underlying
``frappe.get_print(..., as_pdf=True)`` instead, then save the bytes via
``file_manager.save_file`` so the result is auth-gated through the same
File doctype the Desk attachments UI uses.

Permission contract: the user must have **read** permission on the
record (``frappe.has_permission(doctype, name, "read")``) AND **print**
permission on the doctype (Frappe's ``validate_print_permission``,
invoked inside ``get_print``). A user who can't read the record can't
PDF it; a user who can read but can't print (e.g. a Print Role gate)
gets a clean InvalidArgumentError instead of a stack trace.

The PDF is stored as a **private** File (``is_private=1``), so the
returned URL is auth-gated by the customer's bench session. The agent
hands back ``file_url`` plus ``filename`` + ``size_bytes`` + a stable
``name`` (File doc id) so a follow-up tool call can re-attach the same
file to another doc without regenerating.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name


def download_pdf(
	doctype: str,
	name: str,
	print_format: str | None = None,
	letterhead: str | None = None,
	no_letterhead: bool = False,
	language: str | None = None,
) -> dict:
	"""Render ``doctype/name`` as a PDF via the named print format and
	store the bytes in a private File doc attached to the record.

	Returns ``{file_url, filename, mime_type, size_bytes, name}`` where
	``name`` is the File doc id (so a follow-up attach_to_doc can reuse
	it instead of re-rendering).
	"""
	require_doctype_and_name(doctype, name)

	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")

	if not frappe.has_permission(doctype, ptype="read", doc=name):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	if print_format and not frappe.db.exists("Print Format", print_format):
		raise InvalidArgumentError(f"unknown Print Format: {print_format}")

	# frappe.get_print handles print permission internally + applies the
	# named language for translation-aware print formats.
	from frappe.utils.file_manager import save_file
	from frappe.utils.print_format import print_language

	with print_language(language):
		pdf_bytes = frappe.get_print(
			doctype,
			name,
			print_format=print_format,
			as_pdf=True,
			letterhead=letterhead,
			no_letterhead=bool(no_letterhead),
		)

	if not pdf_bytes:
		raise InvalidArgumentError(
			f"PDF generation for {doctype} {name} produced no content",
		)

	safe_name = name.replace(" ", "-").replace("/", "-")
	filename = f"{safe_name}.pdf"

	file_doc = save_file(
		fname=filename,
		content=pdf_bytes,
		dt=doctype,
		dn=name,
		is_private=1,
	)

	return {
		"file_url": file_doc.file_url,
		"filename": file_doc.file_name,
		"title": name,  # clean title for the chat artifact card (no hash suffix)
		"mime_type": "application/pdf",
		"size_bytes": int(file_doc.file_size or len(pdf_bytes)),
		"name": file_doc.name,
	}
