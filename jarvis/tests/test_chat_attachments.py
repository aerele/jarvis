"""Tests for attachment -> canvas storage in jarvis.chat.api.send_message
(plan: 2026-08-11-chat-attachment-preview).

send_message stores EVERY attachment (image or not) as a canvas item on the user
message so both the SPA and the PWA render a clickable, previewable card, and it
no longer appends a "📎 name" text marker. The file BYTES reach the agent in the
worker from the `attachments` enqueue kwarg (decoupled from this text and the
stored canvas), so these tests patch `frappe.enqueue` and never run a worker.

Run ONLY this module (the full suite is destructive on a live dev site):
    bench --site jarvis-test.localhost run-tests --app jarvis \\
        --module jarvis.tests.test_chat_attachments
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import (
	_att_canvas_item,
	_att_type,
	create_conversation,
	get_canvas,
	send_message,
)
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"


def _atts(*pairs) -> str:
	"""JSON string of [{file_url, file_name}, ...] as the composer posts it."""
	return json.dumps([{"file_url": u, "file_name": n} for (u, n) in pairs])


def _send(conv, message="", attachments=None):
	"""Persist a user message without enqueueing a real turn."""
	with patch("frappe.enqueue"):
		return send_message(conv, message, attachments=attachments)


def _canvas_of(message_id) -> list:
	raw = frappe.db.get_value(MSG, message_id, "canvas")
	return frappe.parse_json(raw) if raw else []


class TestAttachmentCanvasStorage(FrappeTestCase):
	"""Runs as the fixture user (never Administrator) so a run cannot wipe real
	chat history; only TEST_USER's own conversations are created + cleaned."""

	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv = create_conversation()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	# ── pure helpers: the single source of every canvas item ──────────────────
	def test_att_type_maps_by_extension(self):
		self.assertEqual(_att_type({"file_name": "invoice.pdf"}), "pdf")
		self.assertEqual(_att_type({"file_name": "page.html"}), "html")
		self.assertEqual(_att_type({"file_name": "page.htm"}), "html")
		# svg is already an image (in _IMAGE_EXTS), so it renders as a thumbnail
		self.assertEqual(_att_type({"file_name": "logo.svg"}), "image")
		self.assertEqual(_att_type({"file_name": "photo.PNG"}), "image")
		self.assertEqual(_att_type({"file_name": "report.docx"}), "file")
		# no extension / url-only fallbacks
		self.assertEqual(_att_type({"file_name": "README"}), "file")
		self.assertEqual(_att_type({"file_url": "/private/files/x.pdf"}), "pdf")

	def test_att_canvas_item_shape(self):
		item = _att_canvas_item({"file_url": "/private/files/q3.pdf", "file_name": "q3.pdf"})
		self.assertEqual(set(item), {"name", "type", "file_url", "title"})
		self.assertEqual(item["type"], "pdf")
		self.assertEqual(item["file_url"], "/private/files/q3.pdf")
		self.assertEqual(item["title"], "q3.pdf")
		self.assertTrue(item["name"])  # a stable key for the v-for
		# missing file_name defaults the title by kind (never empty)
		self.assertEqual(_att_canvas_item({"file_url": "/private/files/a.pdf"})["title"], "file")
		self.assertEqual(_att_canvas_item({"file_url": "/private/files/a.png"})["title"], "image")

	# ── send path: non-image attachment becomes a previewable canvas card ─────
	def test_non_image_attachment_stored_as_canvas_and_no_marker(self):
		res = _send(self.conv, "", _atts(("/private/files/invoice.pdf", "invoice.pdf")))
		mid = res["message_id"]
		canvas = _canvas_of(mid)
		self.assertEqual(len(canvas), 1)
		self.assertEqual(canvas[0]["type"], "pdf")
		self.assertEqual(canvas[0]["file_url"], "/private/files/invoice.pdf")
		self.assertEqual(canvas[0]["title"], "invoice.pdf")
		# the "📎 name" marker is gone; attachment-only send leaves content empty
		content = frappe.db.get_value(MSG, mid, "content")
		self.assertNotIn("📎", content or "")
		self.assertEqual((content or "").strip(), "")

	def test_docx_attachment_is_type_file(self):
		res = _send(self.conv, "here", _atts(("/private/files/report.docx", "report.docx")))
		canvas = _canvas_of(res["message_id"])
		self.assertEqual(canvas[0]["type"], "file")
		# a typed message keeps its text verbatim, with no appended marker
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "content"), "here")

	def test_image_attachment_unchanged(self):
		res = _send(self.conv, "", _atts(("/private/files/photo.png", "photo.png")))
		canvas = _canvas_of(res["message_id"])
		self.assertEqual(len(canvas), 1)
		self.assertEqual(canvas[0]["type"], "image")
		self.assertEqual(canvas[0]["title"], "photo.png")

	def test_mixed_image_and_pdf_both_stored(self):
		res = _send(
			self.conv,
			"look",
			_atts(("/private/files/photo.png", "photo.png"), ("/private/files/q3.pdf", "q3.pdf")),
		)
		canvas = _canvas_of(res["message_id"])
		self.assertEqual([c["type"] for c in canvas], ["image", "pdf"])
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "content"), "look")

	def test_attachment_only_message_inserts_with_empty_content(self):
		# No text at all + one file: the doc must still insert (content == "").
		res = _send(self.conv, "", _atts(("/private/files/scan.pdf", "scan.pdf")))
		self.assertTrue(res.get("ok"))
		self.assertTrue(frappe.db.exists(MSG, res["message_id"]))
		self.assertEqual((frappe.db.get_value(MSG, res["message_id"], "content") or "").strip(), "")
		self.assertEqual(len(_canvas_of(res["message_id"])), 1)

	def test_no_attachments_leaves_canvas_null(self):
		res = _send(self.conv, "just text")
		self.assertIsNone(frappe.db.get_value(MSG, res["message_id"], "canvas"))
		self.assertEqual(frappe.db.get_value(MSG, res["message_id"], "content"), "just text")

	# ── delegated / File-Box seed path stores the same items (edge 12) ────────
	def test_delegated_send_stores_canvas_items(self):
		"""A delegated (ignore_permissions) send routes through the identical
		_att_canvas_item code, so its attachments become previewable cards too."""
		frappe.flags["jarvis_delegated_send"] = True
		try:
			res = _send(self.conv, "", _atts(("/private/files/seed.pdf", "seed.pdf")))
		finally:
			frappe.flags["jarvis_delegated_send"] = False
		canvas = _canvas_of(res["message_id"])
		self.assertEqual(canvas[0]["type"], "pdf")
		self.assertEqual(canvas[0]["title"], "seed.pdf")


class TestGetCanvasFileReadGate(FrappeTestCase):
	"""get_canvas must never return bytes the caller cannot read. Owning the
	conversation authorizes reading the TRANSCRIPT, not an arbitrary File whose
	url happens to sit on a canvas item - without a File-read gate a crafted (or
	replayed) file_url is a private-File exfil path (get_canvas returns the bytes
	as srcdoc content or a base64 data_url).

	Uses a restricted, non-admin Jarvis user: Administrator and System Manager
	bypass File row permissions and would hide the bug (the fixture TEST_USER
	holds System Manager, so it cannot demonstrate this gate)."""

	USER = "jarvis-canvas-gate@example.com"

	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		# An admin-owned private File the restricted user cannot read.
		self.secret = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "canvas-gate-secret.txt",
				"is_private": 1,
				"content": "TOP SECRET",
			}
		).insert(ignore_permissions=True)
		if not frappe.db.exists("User", self.USER):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": self.USER,
					"first_name": "canvas-gate",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
			user.add_roles("Jarvis User")
		# A conversation the restricted user OWNS (so get_canvas's ownership gate
		# passes and we reach the File-read gate), holding a message whose canvas
		# points at the admin-only file.
		self.conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "gate"}).insert(
			ignore_permissions=True
		)
		# Frappe stamps `owner` = the session user (Administrator) on insert and
		# ignores a dict `owner`, so force real ownership by the restricted user -
		# otherwise get_canvas would throw at the OWNERSHIP gate and never reach the
		# File-read gate this test exists to exercise.
		frappe.db.set_value(CONV, self.conv.name, "owner", self.USER)
		self.msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv.name,
				"seq": 1,
				"role": "user",
				"content": "",
				"canvas": frappe.as_json(
					[
						{
							"name": "cvx",
							"type": "file",
							"file_url": self.secret.file_url,
							"title": "canvas-gate-secret.txt",
						}
					]
				),
			}
		)
		self.msg.flags.ignore_permissions = True
		self.msg.insert(ignore_permissions=True)
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		frappe.delete_doc(MSG, self.msg.name, force=1, ignore_permissions=True)
		frappe.delete_doc("Jarvis Conversation", self.conv.name, force=1, ignore_permissions=True)
		frappe.delete_doc("File", self.secret.name, force=1, ignore_permissions=True)
		frappe.set_user(self._orig_user)

	def test_get_canvas_denies_a_file_the_caller_cannot_read(self):
		frappe.set_user(self.USER)
		# Precondition 1: the restricted user OWNS the conversation, so get_canvas's
		# ownership gate passes and control actually reaches the File-read gate.
		self.assertEqual(frappe.db.get_value(CONV, self.conv.name, "owner"), self.USER)
		# Precondition 2: the user genuinely cannot read the file, so the denial is
		# the gate at work, not an unrelated failure.
		self.assertFalse(frappe.has_permission("File", "read", doc=self.secret.name))
		# The raised error must be the FILE-read gate, not the ownership gate - assert
		# on its message so deleting the gate makes this test fail (as it must).
		with self.assertRaisesRegex(frappe.PermissionError, "no permission to read this file"):
			get_canvas(self.msg.name, name="cvx")
		frappe.set_user("Administrator")
