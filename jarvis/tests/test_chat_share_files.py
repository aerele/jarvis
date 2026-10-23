"""Recipient file resolution must never use the recipient's ERP permissions."""

import json
from types import SimpleNamespace
from unittest import TestCase
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

	def test_collation_matches_never_select_a_different_canvas_url(self):
		from jarvis.chat.shared_files import read_file
		from jarvis.chat.sharing import _require_share

		self.share()
		docs = [
			self.file("/private/files/ShareCase.pdf", "ShareCase.pdf"),
			self.file("/private/files/sharecase.pdf", "sharecase.pdf"),
		]
		docs.sort(key=lambda doc: doc.name)
		requested = docs[1]
		for doc in docs:
			self.assertTrue(frappe.has_permission("File", "read", doc=doc, user=OWNER))
		frappe.db.set_value(
			MESSAGE,
			self.message,
			"canvas",
			json.dumps([{"name": "exact", "type": "file", "file_url": requested.file_url}]),
		)
		for doc in docs:
			frappe.share.add("File", doc.name, RECIPIENT, read=1, flags={"ignore_share_permission": True})
		with as_user(RECIPIENT):
			conversation, _ = _require_share(self.conversation)
			selected = read_file(conversation, requested.file_url)
		self.assertEqual(selected.name, requested.name)
		self.assertEqual(selected.file_url, requested.file_url)

	def test_real_private_file_permissions_and_bytes(self):
		from pathlib import Path
		from uuid import uuid4

		self.share()
		filename = f"share-permission-{uuid4().hex}.txt"
		path = Path(frappe.get_site_path("private", "files", filename))
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(b"owner-authorized fixture bytes")
		self.addCleanup(path.unlink, missing_ok=True)
		doc = self.file(f"/private/files/{filename}", filename)
		frappe.share.add("File", doc.name, RECIPIENT, read=1, flags={"ignore_share_permission": True})
		self.download(doc.file_url)
		content = frappe.local.response.filecontent
		self.assertEqual(
			content.encode() if isinstance(content, str) else content, b"owner-authorized fixture bytes"
		)
		# The File is still a visible canvas attachment, but its real attached
		# document now belongs to an outsider. No permission/byte mock is used.
		from jarvis.tests._share_helpers import make_chat

		other, _ = make_chat(OUTSIDER)
		frappe.db.set_value("File", doc.name, {"attached_to_name": other, "owner": OUTSIDER})
		self.assertFalse(
			frappe.has_permission("File", "read", doc=frappe.get_doc("File", doc.name), user=OWNER)
		)
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

	def test_multiple_rows_choose_and_read_same_readable_row_without_impersonation(self):
		self.share()
		first = self.file()
		second = self.file()
		read_names = []

		def permission(doctype, ptype, doc=None, user=None, **kwargs):
			self.assertEqual((doctype, ptype), ("File", "read"))
			self.assertIn(user, (OWNER, RECIPIENT))
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
				response = self.download(url)
			self.assertEqual(frappe.local.response.content_type, expected)
			self.assertEqual(frappe.local.response.display_content_as, disposition)
			self.assertEqual(response.mimetype, expected)
			self.assertEqual(response.get_data(), b"file bytes")
			self.assertTrue(response.headers["Content-Disposition"].startswith(disposition))
			self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

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


class TestDownloadResponse(TestCase):
	def test_download_without_v16_response_headers(self):
		from jarvis.chat.shared_files import download

		headers = getattr(frappe.local, "response_headers", None)
		previous_response = dict(frappe.local.response)
		try:
			if headers is not None:
				del frappe.local.response_headers
			doc = SimpleNamespace(file_name="shared.html", get_content=lambda: b"<p>fixture bytes</p>")
			response = download(doc)
			self.assertEqual(response.get_data(), b"<p>fixture bytes</p>")
			self.assertEqual(response.mimetype, "text/html")
			self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
			self.assertIn("attachment", response.headers["Content-Disposition"])
			self.assertIn("shared.html", response.headers["Content-Disposition"])
		finally:
			frappe.local.response.clear()
			frappe.local.response.update(previous_response)
			if headers is not None:
				frappe.local.response_headers = headers
