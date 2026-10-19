"""Recipient file resolution must never use the recipient's ERP permissions."""

import json
from unittest.mock import patch

import frappe

from jarvis.tests._share_helpers import MESSAGE, OUTSIDER, OWNER, RECIPIENT, as_user, make_message, payload
from jarvis.tests.test_chat_sharing import SharingCase


class TestSharedFiles(SharingCase):
	def file(
		self, url="/private/files/share.pdf", filename="share.pdf", private=1, *, readable=True, message=None
	):
		# No disk writes: bytes are supplied only by the selected File document.
		with as_user(OWNER):
			doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": filename,
					"file_url": url,
					"is_private": private,
					"attached_to_doctype": "Jarvis Conversation",
					"attached_to_name": self.conversation,
				}
			)
			doc.set_new_name()
			doc.db_insert()
		if message is None:
			message = self.message
		frappe.db.set_value(
			MESSAGE,
			message,
			"canvas",
			json.dumps(
				[
					{
						"name": "artifact",
						"type": "html" if filename.endswith(".html") else "file",
						"title": "Shared file",
						"file_url": url,
					}
				]
			),
		)
		return doc

	def download(self, url):
		from jarvis.chat.sharing import get_shared_file

		with as_user(RECIPIENT):
			return get_shared_file(self.conversation, url)

	def test_exact_canvas_membership_not_text_or_hidden(self):
		self.share()
		url = "/private/files/not-attached.pdf"
		frappe.db.set_value(MESSAGE, self.message, "content", url)
		with self.assertRaises(frappe.PermissionError):
			self.download(url)
		hidden = make_message(self.conversation, hidden=1)
		self.file(url, message=hidden)
		with self.assertRaises(frappe.PermissionError):
			self.download(url)

	def test_owner_unreadable_private_mismatch_and_folder_refused(self):
		self.share()
		doc = self.file()
		with patch("frappe.has_permission", return_value=False), self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)
		frappe.db.set_value("File", doc.name, "is_private", 0)
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)
		frappe.db.set_value("File", doc.name, {"is_private": 1, "is_folder": 1})
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)

	def test_path_variants_and_remote_urls_refused_even_if_exact_canvas_item(self):
		self.share()
		doc = self.file()
		for url in (
			"/private/files/../secret.pdf",
			"/private/files/a%2fsecret.pdf",
			"/private/files/a%20b.pdf",
			"/private/files//a.pdf",
			"/private/files/a?x=1",
			"/private/files/a#b",
			"https://example.com/a.pdf",
			"//example.com/a.pdf",
			"/files/\\secret.pdf",
		):
			frappe.db.set_value(
				MESSAGE, self.message, "canvas", json.dumps([{"name": "artifact", "file_url": url}])
			)
			frappe.db.set_value("File", doc.name, "file_url", url)
			with (
				self.subTest(url=url),
				patch("frappe.has_permission", return_value=True),
				patch(
					"frappe.core.doctype.file.file.File.get_content",
					side_effect=AssertionError("unsafe file read"),
				),
				self.assertRaises(frappe.PermissionError),
			):
				self.download(url)

	def test_multiple_rows_choose_and_read_same_owner_readable_row_without_impersonation(self):
		self.share()
		first = self.file()
		second = self.file()
		read_names = []

		def permission(doctype, ptype, doc=None, user=None, **kwargs):
			self.assertEqual((doctype, ptype, user), ("File", "read", OWNER))
			return doc.name == second.name

		def content(doc):
			read_names.append(doc.name)
			return b"allowed bytes" if doc.name == second.name else b"UNREADABLE-SECRET"

		from frappe.core.doctype.file.file import File

		with (
			as_user(RECIPIENT),
			patch("frappe.set_user", side_effect=AssertionError("impersonation")),
			patch("frappe.has_permission", side_effect=permission),
			patch.object(File, "get_content", content),
		):
			from jarvis.chat.sharing import get_shared_file

			get_shared_file(self.conversation, first.file_url)
		self.assertEqual(read_names, [second.name])
		self.assertEqual(frappe.local.response.filecontent, b"allowed bytes")

	def test_mime_and_disposition_are_server_owned(self):
		from frappe.core.doctype.file.file import File

		self.share()
		for suffix, expected, disposition in (
			("pdf", "application/pdf", "inline"),
			("png", "image/png", "inline"),
			("jpeg", "image/jpeg", "inline"),
			("gif", "image/gif", "inline"),
			("webp", "image/webp", "inline"),
			("html", "text/html", "attachment"),
			("svg", "image/svg+xml", "attachment"),
			("csv", "text/csv", "attachment"),
		):
			url = f"/private/files/share.{suffix}"
			self.file(url, f"share.{suffix}")
			with (
				self.subTest(suffix=suffix),
				patch("frappe.has_permission", return_value=True),
				patch.object(File, "get_content", return_value=b"file bytes"),
			):
				self.download(url)
			self.assertEqual(frappe.local.response.content_type, expected)
			self.assertEqual(frappe.local.response.display_content_as, disposition)
			# Frappe emits response_headers at the HTTP boundary; a nested
			# response['headers'] value alone never reaches the browser.
			self.assertEqual(frappe.local.response_headers.get("X-Content-Type-Options"), "nosniff")

	def test_html_preview_is_static_sanitized_and_secret_scrubbed(self):
		from frappe.core.doctype.file.file import File

		from jarvis.chat.sharing import get_shared_canvas

		self.share()
		self.file("/private/files/share.html", "share.html")
		html = """<html><head><meta http-equiv="refresh" content="0; url=https://attacker.invalid"></head><body>
<h1>Summary</h1><p>api_key=HTML-SECRET</p><script>fetch('/api/method/frappe.auth.get_logged_user')</script>
<a href="https://attacker.invalid">leave</a><form action="https://attacker.invalid"><button>send</button></form>
<iframe src="https://attacker.invalid"></iframe><img src="https://attacker.invalid/pixel" onerror="alert(1)">
<svg onload="alert(1)"></svg><object data="https://attacker.invalid"></object>
<div style="background:url(https://attacker.invalid)">visible</div></body></html>"""
		with (
			as_user(RECIPIENT),
			patch("frappe.has_permission", return_value=True),
			patch.object(File, "get_content", return_value=html),
		):
			result = get_shared_canvas(self.message, "artifact", dark=1)
		text = payload(result)
		self.assertIn("Summary", text)
		self.assertIn("visible", text)
		for forbidden in (
			"HTML-SECRET",
			"attacker.invalid",
			"<script",
			"<iframe",
			"<form",
			"<svg",
			'http-equiv="refresh"',
			"onerror",
			"<object",
		):
			self.assertNotIn(forbidden, text)
		self.assertIn("default-src 'none'", text)
		self.assertNotIn("/private/files/", text)

	def test_file_and_canvas_refuse_outsiders_after_revocation(self):
		from frappe.core.doctype.file.file import File

		from jarvis.chat.sharing import get_shared_canvas, get_shared_file

		self.file("/private/files/share.html", "share.html")
		self.share()
		with (
			as_user(RECIPIENT),
			patch("frappe.has_permission", return_value=True),
			patch.object(File, "get_content", return_value="<p>Visible</p>"),
		):
			get_shared_file(self.conversation, "/private/files/share.html")
			self.assertIn("Visible", get_shared_canvas(self.message, "artifact")["content"])
		self.share([])
		for user in (RECIPIENT, OUTSIDER):
			with as_user(user), self.assertRaises(frappe.PermissionError):
				get_shared_file(self.conversation, "/private/files/share.html")
			with as_user(user), self.assertRaises(frappe.PermissionError):
				get_shared_canvas(self.message, "artifact")
