"""Group files are deliberately disclosed by their source actor, never an owner credential fallback."""

import io
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.datastructures import FileStorage, MultiDict

from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_bindings import insert_message
from jarvis.tests._collaboration_committed import CommittedCollaborationCase, repeatable_read
from jarvis.tests._collaboration_test_site import require_collaboration_test_site
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user


class TestCollaborationFiles(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))

	def file(self, *, actor=RECIPIENT, hidden=0, suffix="txt", content=b"shared actor bytes"):
		require_collaboration_test_site(frappe.local.site)
		filename = f"collaboration-{uuid.uuid4().hex}.{suffix}"
		path = Path(frappe.get_site_path("private", "files", filename))
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)
		self.addCleanup(path.unlink, missing_ok=True)
		with as_user(actor):
			doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": filename,
					"file_url": f"/private/files/{filename}",
					"is_private": 1,
				}
			)
			doc.set_new_name()
			doc.db_insert()
		self.records.append(("File", doc.name))
		row = insert_message(
			self.bound,
			{
				"role": "user",
				"hidden": hidden,
				"canvas": json.dumps(
					[
						{
							"name": "artifact",
							"type": "html" if suffix == "html" else "file",
							"title": filename,
							"file_url": doc.file_url,
						}
					]
				),
			},
		)
		frappe.db.commit()
		return doc, row

	def download(self, url, user=OWNER):
		from jarvis.chat.collaboration_api import get_file

		with as_user(user):
			get_file(self.conversation, url)

	def test_file_uses_source_actor_permission_and_same_bytes_without_impersonation(self):
		doc, _ = self.file()
		self.assertFalse(frappe.has_permission("File", "read", doc=doc, user=OWNER))
		with as_user(OWNER), patch("frappe.set_user", side_effect=AssertionError("impersonation")):
			from jarvis.chat.collaboration_api import get_file

			get_file(self.conversation, doc.file_url)
		self.assertEqual(frappe.local.response.filecontent, "shared actor bytes")
		self.assertEqual(frappe.local.response_headers["X-Content-Type-Options"], "nosniff")

	def test_owner_readable_row_does_not_replace_source_actor_unreadable_row(self):
		doc, _ = self.file(actor=OUTSIDER)
		frappe.db.set_value("File", doc.name, "owner", OWNER)
		frappe.db.commit()
		self.assertTrue(
			frappe.has_permission("File", "read", doc=frappe.get_doc("File", doc.name), user=OWNER)
		)
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)

	def test_hidden_text_only_and_forged_binding_sources_do_not_authorize_files(self):
		doc, row = self.file(hidden=1)
		frappe.db.set_value("Jarvis Chat Message", self.message, "content", doc.file_url)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)
		frappe.db.set_value("Jarvis Chat Message", row.name, {"hidden": 0, "actor": OWNER})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)

	def test_exact_url_private_flag_and_live_reader_grant_are_required(self):
		doc, _ = self.file()
		for url in (
			doc.file_url.upper(),
			doc.file_url + "?x=1",
			"/private/files/../secret",
			"https://example.invalid/file",
		):
			with self.subTest(url=url), self.assertRaises(frappe.PermissionError):
				self.download(url)
		frappe.db.set_value("File", doc.name, "is_private", 0)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)
		frappe.db.set_value("File", doc.name, "is_private", 1)
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url, RECIPIENT)

	def test_html_preview_is_static_and_tool_detail_is_projected(self):
		doc, row = self.file(
			suffix="html", content=b"<h1>Visible</h1><script>alert(1)</script><p>api_key=SECRET_SENTINEL</p>"
		)
		from jarvis.chat.collaboration_api import get_canvas, get_tool_detail

		with as_user(OWNER):
			result = get_canvas(row.name, "artifact")
		text = json.dumps(result)
		self.assertIn("Visible", text)
		self.assertNotIn("<script", text)
		self.assertNotIn("SECRET_SENTINEL", text)
		self.assertNotIn("/private/files/", text)
		tool = insert_message(
			self.bound,
			{
				"role": "tool",
				"tool_name": "get_doc",
				"tool_result": json.dumps({"password": "SECRET_SENTINEL", "value": "Visible"}),
			},
		)
		frappe.db.commit()
		with as_user(OWNER):
			result = get_tool_detail(tool.name)
		self.assertNotIn("SECRET_SENTINEL", json.dumps(result, default=str))
		self.assertEqual(result["actor"]["id"], RECIPIENT)

	def test_upload_is_private_owned_by_actor_and_not_attached_until_send(self):
		from jarvis.chat.collaboration_api import upload_file

		file = FileStorage(stream=io.BytesIO(b"new actor attachment"), filename="shared-upload.txt")
		request = frappe._dict(files=MultiDict({"file": file}))
		with as_user(RECIPIENT), patch.object(frappe.local, "request", request, create=True):
			result = frappe.call(
				upload_file,
				conversation=self.conversation,
				actor=OWNER,
				is_private=0,
				attached_to_doctype=c.CONVERSATION,
				attached_to_name=self.conversation,
			)
		doc = frappe.get_doc("File", result["name"])
		self.records.append(("File", doc.name))
		self.assertEqual(doc.owner, RECIPIENT)
		self.assertEqual(doc.is_private, 1)
		self.assertFalse(doc.attached_to_doctype)
		self.assertTrue(doc.file_url.startswith("/private/files/"))
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.download(doc.file_url)

	def test_view_only_and_oversized_upload_refuse_before_saving(self):
		from jarvis.chat.collaboration_api import upload_file

		before = frappe.db.count("File", {"owner": RECIPIENT})
		request = frappe._dict(
			files=MultiDict({"file": FileStorage(stream=io.BytesIO(b"too much"), filename="large.txt")})
		)
		with (
			as_user(RECIPIENT),
			patch.object(frappe.local, "request", request, create=True),
			patch("frappe.utils.file_manager.get_max_file_size", return_value=2),
		):
			with self.assertRaises(frappe.ValidationError):
				upload_file(self.conversation)
		frappe.db.set_value(c.SHARE, self.share, "access_mode", "View")
		frappe.db.commit()
		with as_user(RECIPIENT), self.assertRaises(frappe.PermissionError):
			upload_file(self.conversation)
		self.assertEqual(frappe.db.count("File", {"owner": RECIPIENT}), before)

	def test_concurrent_file_permission_change_is_not_read_from_an_old_snapshot(self):
		from jarvis.tests.race_harness import open_read_view, other_connection, snapshot_isolation_off

		doc, _ = self.file()
		with repeatable_read(), snapshot_isolation_off():
			open_read_view()
			self.assertEqual(frappe.db.get_value("File", doc.name, "owner"), RECIPIENT)
			with other_connection() as other:
				other.sql("UPDATE `tabFile` SET owner=%s WHERE name=%s", (OWNER, doc.name))
				other.commit()
			self.assertEqual(frappe.db.get_value("File", doc.name, "owner"), RECIPIENT)
			with self.assertRaises(frappe.PermissionError):
				self.download(doc.file_url)

	def test_concurrent_hidden_attachment_is_not_authorized_from_an_old_snapshot(self):
		from jarvis.tests.race_harness import open_read_view, other_connection, snapshot_isolation_off

		doc, row = self.file()
		with repeatable_read(), snapshot_isolation_off():
			open_read_view()
			self.assertEqual(frappe.db.get_value("Jarvis Chat Message", row.name, "hidden"), 0)
			with other_connection() as other:
				other.sql("UPDATE `tabJarvis Chat Message` SET hidden=1 WHERE name=%s", row.name)
				other.commit()
			self.assertEqual(frappe.db.get_value("Jarvis Chat Message", row.name, "hidden"), 0)
			with self.assertRaises(frappe.PermissionError):
				self.download(doc.file_url)

	def test_download_does_not_lock_the_transcript_for_other_writers(self):
		from jarvis.tests.race_harness import other_connection

		doc, row = self.file()
		self.download(doc.file_url)
		with other_connection() as other:
			other.sql("SET SESSION innodb_lock_wait_timeout=1")
			other.sql("UPDATE `tabJarvis Chat Message` SET content=content WHERE name=%s", row.name)
			other.commit()
