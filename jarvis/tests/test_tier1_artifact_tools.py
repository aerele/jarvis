"""Tests for the Tier-1 artifact-producer tools:
download_pdf, attach_to_doc, download_vcard.

The PDF + attachment paths mock the underlying Frappe helpers so the
test doesn't depend on wkhtmltopdf being installed in the bench - we
care about the tool contract (validation, permissions, envelope
shape), not about Frappe's PDF stack itself. download_vcard exercises
the real Contact.get_vcard() since it's a pure Python helper.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.attach_to_doc import attach_to_doc
from jarvis.tools.download_pdf import download_pdf
from jarvis.tools.download_vcard import download_vcard

# ---------------------------------------------------------------------
# download_pdf
# ---------------------------------------------------------------------


class TestDownloadPdfValidation(FrappeTestCase):
	"""Argument / permission validation. No bytes are actually rendered;
	these tests stop before the get_print call so the absence of
	wkhtmltopdf in the bench doesn't matter."""

	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			download_pdf("", "X-1")

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			download_pdf("User", "")

	def test_rejects_unknown_doc(self):
		with self.assertRaises(InvalidArgumentError):
			download_pdf("User", "does-not-exist@example.com")

	def test_rejects_when_no_read_permission(self):
		# Pick any doc that exists, then patch the permission check.
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		self.assertIsNotNone(user_name, "test bench must have at least one User")
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				download_pdf("User", user_name)

	def test_rejects_unknown_print_format(self):
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		with self.assertRaises(InvalidArgumentError):
			download_pdf("User", user_name, print_format="Print Format That Does Not Exist 9000")


class TestDownloadPdfEnvelope(FrappeTestCase):
	"""Happy path: mock get_print + save_file so the test is
	independent of PDF tooling, then assert the returned envelope
	shape - that's the contract the agent depends on."""

	def test_returns_expected_envelope_shape(self):
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		fake_bytes = b"%PDF-1.4 fake-content"
		fake_file_doc = MagicMock(
			name="fake-File-1",
			file_url="/private/files/User-fake.pdf",
			file_name="User-fake.pdf",
			file_size=len(fake_bytes),
		)
		fake_file_doc.name = "fake-File-1"

		with (
			patch(
				"frappe.get_print",
				return_value=fake_bytes,
			) as gp,
			patch(
				"frappe.utils.file_manager.save_file",
				return_value=fake_file_doc,
			) as sf,
		):
			out = download_pdf("User", user_name)

		gp.assert_called_once()
		sf.assert_called_once()
		# The save_file call must attach the new file to the source
		# record so the audit trail in the File doc reflects who asked.
		self.assertEqual(sf.call_args.kwargs.get("dt"), "User")
		self.assertEqual(sf.call_args.kwargs.get("dn"), user_name)
		self.assertEqual(sf.call_args.kwargs.get("is_private"), 1)

		# Envelope keys are the contract; pin every one. ("title" is the
		# clean display name for the chat artifact card — no hash suffix.)
		self.assertEqual(
			set(out.keys()), {"file_url", "filename", "title", "mime_type", "size_bytes", "name"}
		)
		self.assertEqual(out["mime_type"], "application/pdf")
		self.assertEqual(out["file_url"], "/private/files/User-fake.pdf")
		self.assertEqual(out["size_bytes"], len(fake_bytes))

	def test_rejects_empty_pdf_bytes(self):
		"""A print format that yields zero bytes is almost always a
		misconfigured letterhead / template; surface as Invalid rather
		than write a 0-byte File doc."""
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		with patch("frappe.get_print", return_value=b""):
			with self.assertRaises(InvalidArgumentError):
				download_pdf("User", user_name)


# ---------------------------------------------------------------------
# attach_to_doc
# ---------------------------------------------------------------------


class TestAttachToDocValidation(FrappeTestCase):
	def test_rejects_empty_file_url(self):
		with self.assertRaises(InvalidArgumentError):
			attach_to_doc("", "User", "Administrator")

	def test_rejects_empty_target_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			attach_to_doc("/private/files/x.pdf", "", "Administrator")

	def test_rejects_unknown_target(self):
		with self.assertRaises(InvalidArgumentError):
			attach_to_doc("/private/files/x.pdf", "User", "missing@example.com")

	def test_rejects_when_no_write_permission(self):
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				attach_to_doc("/private/files/x.pdf", "User", user_name)


class TestAttachToDocEnvelope(FrappeTestCase):
	def test_calls_add_attachments_and_returns_envelope(self):
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		file_url = "/private/files/some-attached.pdf"
		source_file_name = "source-File-1"

		# Signature-faithful fake: distinguishes the "resolve docname from
		# url" lookup from the "find the newly-attached File" lookup by
		# filters, instead of a single return_value that would mask
		# whichever value the tool actually passes to add_attachments.
		# Anything else (e.g. the tool's own frappe.db.exists("User", ...),
		# which delegates through get_value) falls through to the real
		# lookup - args/kwargs are forwarded verbatim so defaults like
		# fieldname="name" aren't clobbered.
		_real_get_value = frappe.db.get_value

		def _fake_get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			if doctype == "File" and isinstance(filters, dict) and "attached_to_doctype" in filters:
				return "attached-File-99"
			if doctype == "File" and filters == {"file_url": file_url}:
				return source_file_name
			return _real_get_value(doctype, *args, **kwargs)

		with (
			patch(
				"frappe.utils.file_manager.add_attachments",
			) as add_a,
			patch(
				"frappe.db.get_value",
				side_effect=_fake_get_value,
			),
		):
			out = attach_to_doc(file_url, "User", user_name)

		# add_attachments needs the File DOCNAME resolved from file_url,
		# never the raw url - passing the url raises DoesNotExistError
		# (add_attachments looks the entry up by File.name).
		add_a.assert_called_once_with("User", user_name, [source_file_name])
		self.assertEqual(
			set(out.keys()),
			{
				"file_url",
				"target_doctype",
				"target_name",
				"attached_file_name",
			},
		)
		self.assertEqual(out["file_url"], file_url)
		self.assertEqual(out["attached_file_name"], "attached-File-99")

	def test_raises_invalid_argument_when_file_url_unresolvable(self):
		user_name = frappe.db.get_value("User", filters={}, fieldname="name")
		missing_url = "/private/files/does-not-exist.pdf"
		_real_get_value = frappe.db.get_value

		def _fake_get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			if doctype == "File" and filters == {"file_url": missing_url}:
				return None
			return _real_get_value(doctype, *args, **kwargs)

		with patch("frappe.db.get_value", side_effect=_fake_get_value):
			with self.assertRaises(InvalidArgumentError):
				attach_to_doc(missing_url, "User", user_name)


class TestAttachToDocRealFlow(FrappeTestCase):
	"""End-to-end regression for F11: a real File's file_url must resolve
	to its docname and actually attach, with no mocking of
	add_attachments/save_url. Pre-fix this raises frappe.DoesNotExistError
	because add_attachments is handed the url instead of the docname."""

	def test_real_file_url_resolves_and_attaches(self):
		target_name = frappe.db.get_value("User", filters={}, fieldname="name")

		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "attach-to-doc-real-test.txt",
				"content": b"hello from attach_to_doc real-flow test",
				"is_private": 1,
			}
		)
		source.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("File", source.name, force=True, ignore_permissions=True))

		out = attach_to_doc(source.file_url, "User", target_name)

		self.assertIsNotNone(out["attached_file_name"])
		attached = frappe.get_doc("File", out["attached_file_name"])
		self.addCleanup(
			lambda: frappe.delete_doc("File", attached.name, force=True, ignore_permissions=True),
		)
		self.assertEqual(attached.attached_to_doctype, "User")
		self.assertEqual(attached.attached_to_name, target_name)
		self.assertEqual(attached.file_url, source.file_url)


# ---------------------------------------------------------------------
# download_vcard
# ---------------------------------------------------------------------


def _ensure_contact(name="Jarvis-Test-VCard-Contact"):
	if not frappe.db.exists("Contact", name):
		c = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": name,
				"email_ids": [{"email_id": "vcard-test@example.com", "is_primary": 1}],
				"phone_nos": [{"phone": "+1-555-0100", "is_primary_mobile_no": 1}],
			}
		)
		c.insert(ignore_permissions=True)
	return name


class TestDownloadVcard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.contact_name = _ensure_contact()

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			download_vcard("")

	def test_rejects_unknown_contact(self):
		with self.assertRaises(InvalidArgumentError):
			download_vcard("Definitely Not A Real Contact 9000")

	def test_returns_vcf_envelope_for_known_contact(self):
		out = download_vcard(self.contact_name)
		self.assertEqual(set(out.keys()), {"vcard", "filename", "mime_type"})
		self.assertEqual(out["mime_type"], "text/vcard")
		self.assertEqual(out["filename"], f"{self.contact_name}.vcf")
		# vCard spec: every serialization is bracketed by these envelope
		# lines, so we don't need to assert deep semantic content.
		self.assertIn("BEGIN:VCARD", out["vcard"])
		self.assertIn("END:VCARD", out["vcard"])
		# The contact's name should appear somewhere in the body so we
		# know we serialized the right record.
		self.assertIn(self.contact_name, out["vcard"])
