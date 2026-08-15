"""Return a Contact's vCard serialization as plain text.

The bundled ``frappe.contacts.doctype.contact.contact.download_vcard``
writes the vcf to ``frappe.response`` for an HTTP download cycle; that
shape doesn't compose with a JSON tool envelope. We call the same
underlying ``Contact.get_vcard()`` directly and return the serialized
text (vCards are typically < 1 KB so inlining in the response is
cheap; no need for a File doctype roundtrip).

Permission contract: ``Contact.check_permission()`` (the same gate the
bundled handler uses) enforces read on the Contact doc; a user who
can't read the contact can't export it.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import require_doctype_and_name


def download_vcard(name: str) -> dict:
	"""Render Contact ``name`` as a vCard and return the text + filename.

	Returns ``{vcard, filename, mime_type}``. ``vcard`` is the
	.vcf-formatted text the chat surface can either render verbatim
	(it's human-readable enough) or hand the customer for download.
	"""
	require_doctype_and_name("Contact", name)

	if not frappe.db.exists("Contact", name):
		raise InvalidArgumentError(f"unknown Contact: {name}")

	contact = frappe.get_doc("Contact", name)
	# check_permission() raises PermissionError (Frappe's own class)
	# rather than our PermissionDeniedError - that's fine because the
	# plugin's frappe-client surfaces the Frappe envelope verbatim;
	# the agent will see a clean PermissionError code instead of a
	# 500. Don't intercept.
	contact.check_permission()

	vcard = contact.get_vcard().serialize()
	return {
		"vcard": vcard,
		"filename": f"{name}.vcf",
		"mime_type": "text/vcard",
	}
