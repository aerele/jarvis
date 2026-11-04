"""A file in a shared chat opens only when the reader has read on the File."""

from unittest.mock import patch

import frappe

from jarvis.tests._share_helpers import OWNER, RECIPIENT, as_user
from jarvis.tests.test_chat_share_files import TestSharedFiles
from jarvis.tests.test_chat_sharing import SharingCase


def _readable_by(*allowed):
	def permission(doctype, ptype, doc=None, user=None, **kwargs):
		return user in allowed

	return permission


class TestSharedChatFileAccess(SharingCase):
	file = TestSharedFiles.file

	def open_as(self, user, url):
		from jarvis.chat.sharing import get_shared_file

		with as_user(user):
			return get_shared_file(self.conversation, url)

	def test_recipient_without_read_is_refused_when_owner_has_read(self):
		self.share()
		doc = self.file()
		with (
			patch("frappe.has_permission", side_effect=_readable_by(OWNER)),
			self.assertRaises(frappe.PermissionError),
		):
			self.open_as(RECIPIENT, doc.file_url)

	def test_recipient_with_read_gets_the_file(self):
		from frappe.core.doctype.file.file import File

		self.share()
		doc = self.file()
		with (
			patch("frappe.has_permission", side_effect=_readable_by(OWNER, RECIPIENT)),
			patch.object(File, "get_content", return_value=b"shared bytes"),
		):
			self.open_as(RECIPIENT, doc.file_url)
		self.assertEqual(frappe.local.response.filecontent, b"shared bytes")

	def test_recipient_with_read_is_refused_when_owner_lost_read(self):
		self.share()
		doc = self.file()
		with (
			patch("frappe.has_permission", side_effect=_readable_by(RECIPIENT)),
			self.assertRaises(frappe.PermissionError),
		):
			self.open_as(RECIPIENT, doc.file_url)

	def test_owner_opens_own_file_unchanged(self):
		from frappe.core.doctype.file.file import File

		doc = self.file()
		seen = []

		def permission(doctype, ptype, doc=None, user=None, **kwargs):
			seen.append(user)
			return user == OWNER

		with (
			patch("frappe.has_permission", side_effect=permission),
			patch.object(File, "get_content", return_value=b"own bytes"),
		):
			from jarvis.chat.shared_files import read_file

			with as_user(OWNER):
				row = read_file(frappe.get_doc("Jarvis Conversation", self.conversation), doc.file_url)
		self.assertEqual(row.name, doc.name)
		self.assertEqual(set(seen), {OWNER})
