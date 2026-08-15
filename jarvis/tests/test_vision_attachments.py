"""Tests for the worker's attachment preparation: routing, the vision gate, and
the File read-permission check.

Needs DB (File doctype); inserts roll back via FrappeTestCase. The image/PDF ->
vision-part conversion itself is unit-tested in test_vision.py, so here we patch
those helpers and focus on _prepare_attachments' own logic. (Frappe's save_file
text-encodes binary content, so we don't rely on a round-tripped image here.)"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.worker import _prepare_attachments, _to_managed_attachments


def _make_file(name: str, content: bytes) -> dict:
	from frappe.utils.file_manager import save_file

	f = save_file(name, content, None, None, is_private=1)
	return {"file_url": f.file_url, "file_name": f.file_name}


class TestPrepareAttachments(FrappeTestCase):
	def test_no_attachments_passthrough(self):
		self.assertEqual(_prepare_attachments("hi", None, vision_ok=True), ("hi", []))

	def test_text_file_inlined(self):
		att = _make_file("notes.txt", b"hello world")
		msg, parts = _prepare_attachments("hi", [att], vision_ok=True)
		self.assertEqual(parts, [])
		self.assertIn("hello world", msg)

	def test_image_routed_to_vision(self):
		att = _make_file("pic.png", b"mocked")
		fake = {"mime": "image/jpeg", "data_b64": "QUJD", "file_name": "pic.png"}
		with patch("jarvis.chat.vision.image_part", return_value=fake) as m:
			msg, parts = _prepare_attachments("hi", [att], vision_ok=True)
		m.assert_called_once()
		self.assertEqual(parts, [fake])
		self.assertIn("sent for viewing", msg)

	def test_pdf_routed_to_vision(self):
		import io as _io

		from pypdf import PdfWriter

		w = PdfWriter()
		w.add_blank_page(width=100, height=100)
		buf = _io.BytesIO()
		w.write(buf)
		att = _make_file("doc.pdf", buf.getvalue())  # valid PDF (passes File.check_content)
		fake = [{"mime": "image/jpeg", "data_b64": "QUJD", "file_name": "doc.pdf-p1.jpg"}]
		with patch("jarvis.chat.vision.pdf_parts", return_value=(fake, 1)):
			msg, parts = _prepare_attachments("hi", [att], vision_ok=True)
		self.assertEqual(parts, fake)
		self.assertIn("sent as images", msg)

	def test_pdf_text_extracted_without_vision(self):
		# Vision off -> fall back to pypdf text extraction (digital PDFs).
		import io as _io

		from pypdf import PdfWriter

		w = PdfWriter()
		w.add_blank_page(width=100, height=100)
		buf = _io.BytesIO()
		w.write(buf)
		att = _make_file("digital.pdf", buf.getvalue())
		with patch("jarvis.tools.read_file._read_pdf", return_value={"text": "hello pdf"}):
			msg, parts = _prepare_attachments("hi", [att], vision_ok=False)
		self.assertEqual(parts, [])
		self.assertIn("hello pdf", msg)
		self.assertIn("extracted text", msg)

	def test_pdf_no_text_notes_scanned_without_vision(self):
		# Vision off + a PDF with no text layer -> a note, not silence.
		import io as _io

		from pypdf import PdfWriter

		w = PdfWriter()
		w.add_blank_page(width=100, height=100)
		buf = _io.BytesIO()
		w.write(buf)
		att = _make_file("scanned.pdf", buf.getvalue())
		msg, parts = _prepare_attachments("hi", [att], vision_ok=False)
		self.assertEqual(parts, [])
		self.assertIn("no extractable text", msg)

	def test_image_degrades_to_note_without_vision(self):
		att = _make_file("pic2.png", b"x")
		msg, parts = _prepare_attachments("hi", [att], vision_ok=False)
		self.assertEqual(parts, [])
		self.assertIn("couldn't be viewed", msg)

	def test_permission_denied_blocks_read(self):
		att = _make_file("secret.txt", b"hello")  # owned by Administrator
		user = "vision_perm_test@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "VT",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			msg, parts = _prepare_attachments("hi", [att], vision_ok=True)
		finally:
			frappe.set_user("Administrator")
		self.assertIn("No permission", msg)
		self.assertEqual(parts, [])

	def test_to_managed_attachments_shape(self):
		parts = [{"mime": "image/jpeg", "data_b64": "QUJD", "file_name": "x.jpg"}]
		self.assertEqual(
			_to_managed_attachments(parts),
			[{"type": "image", "mimeType": "image/jpeg", "fileName": "x.jpg", "content": "QUJD"}],
		)
