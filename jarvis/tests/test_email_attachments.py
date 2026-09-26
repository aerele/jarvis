"""Email attachment contract and permission checks; never queue real email."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from jarvis.chat.confirm_card import build_card
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.send_email import send_email


class TestEmailAttachments(TestCase):
	def setUp(self):
		self.files = {
			"FILE-A": SimpleNamespace(
				name="FILE-A",
				file_name="sales.xlsx",
				file_url="/private/files/sales.xlsx",
				is_folder=0,
				get_content=MagicMock(return_value=b"xlsx"),
			),
			"FILE-B": SimpleNamespace(
				name="FILE-B",
				file_name="summary.pdf",
				file_url="/files/summary.pdf",
				is_folder=0,
				get_content=MagicMock(return_value=b"pdf"),
			),
		}
		self.base = dict(
			recipients="recipient@example.invalid",
			subject="Report",
			content="Please see the report.",
			doctype="Customer",
			name="CUSTOMER-A",
		)

		def start(target, **kwargs):
			p = patch(target, **kwargs)
			self.addCleanup(p.stop)
			return p.start()

		self.db = start("frappe.db", new=MagicMock())
		self.db.exists.side_effect = lambda dt, name: dt != "File" or name in self.files
		start("frappe.get_doc", side_effect=lambda dt, name: self.files[name])
		start(
			"frappe.get_all",
			side_effect=lambda dt, filters, pluck: [
				f.name for f in self.files.values() if f.file_url == filters["file_url"]
			],
		)
		self.perm = start("frappe.has_permission", return_value=True)
		self.make = start("frappe.core.doctype.communication.email.make", return_value={"name": "MOCK-COMM"})
		start("frappe.sendmail", side_effect=AssertionError("real email forbidden"))
		start("frappe.enqueue", side_effect=AssertionError("queueing forbidden"))

	def test_dispatch_preserves_file_ids_and_resolves_local_urls(self):
		from jarvis.tools.registry import dispatch

		result = dispatch(
			"send_email",
			{**self.base, "attachments": ["FILE-A", "/files/summary.pdf", "/private/files/sales.xlsx"]},
		)
		self.assertEqual(result["communication_name"], "MOCK-COMM")
		self.assertEqual(self.make.call_args.kwargs["attachments"], ["FILE-A", "FILE-B"])
		for file in self.files.values():
			file.get_content.assert_called_once()
			self.perm.assert_any_call("File", "read", doc=file)

	def test_batch_keeps_each_messages_attachments_and_does_not_mutate_input(self):
		messages = [{**self.base, "attachments": ["FILE-A"]}, {**self.base, "attachments": ["FILE-B"]}]
		result = send_email(messages=messages)
		self.assertEqual(result["count"], 2)
		self.assertEqual(
			[c.kwargs["attachments"] for c in self.make.call_args_list], [["FILE-A"], ["FILE-B"]]
		)
		self.assertEqual(messages[0]["attachments"], ["FILE-A"])

	def test_invalid_later_batch_attachment_prevents_every_send(self):
		with self.assertRaises(PermissionDeniedError):
			send_email(
				messages=[{**self.base, "attachments": ["FILE-A"]}, {**self.base, "attachments": ["MISSING"]}]
			)
		self.make.assert_not_called()

	def test_denied_file_is_never_read_or_sent(self):
		self.perm.side_effect = lambda dt, *args, **kwargs: dt != "File"
		with self.assertRaises(PermissionDeniedError):
			send_email(**self.base, attachments=["FILE-A"])
		self.files["FILE-A"].get_content.assert_not_called()
		self.make.assert_not_called()

	def test_shared_url_uses_an_accessible_file_record(self):
		self.files["FILE-B"].file_url = self.files["FILE-A"].file_url
		self.perm.side_effect = lambda dt, *args, **kwargs: dt != "File" or kwargs["doc"].name == "FILE-B"
		send_email(**self.base, attachments=["/private/files/sales.xlsx"])
		self.assertEqual(self.make.call_args.kwargs["attachments"], ["FILE-B"])

	def test_malformed_attachments_are_not_silently_ignored(self):
		for value in [
			"FILE-A",
			{},
			[None],
			[""],
			[{"file_url": "/files/x"}],
			["https://example.invalid/x"],
			["/etc/passwd"],
			["FILE-A"] * 21,
		]:
			with self.subTest(value=value), self.assertRaises(InvalidArgumentError):
				send_email(**self.base, attachments=value)
		self.make.assert_not_called()

	def test_folder_or_remote_file_records_cannot_be_attached(self):
		self.files["FILE-A"].is_folder = 1
		with self.assertRaises(InvalidArgumentError):
			send_email(**self.base, attachments=["FILE-A"])
		self.files["FILE-A"].is_folder = 0
		self.files["FILE-A"].file_url = "https://example.invalid/report"
		with self.assertRaises(InvalidArgumentError):
			send_email(**self.base, attachments=["FILE-A"])
		self.make.assert_not_called()

	def test_missing_content_fails_before_email_creation(self):
		self.files["FILE-A"].get_content.side_effect = FileNotFoundError()
		with self.assertRaises(InvalidArgumentError):
			send_email(**self.base, attachments=["FILE-A"])
		self.make.assert_not_called()

	def test_batch_cannot_silently_ignore_top_level_attachments(self):
		with self.assertRaises(InvalidArgumentError):
			send_email(messages=[self.base], attachments=["FILE-A"])
		self.make.assert_not_called()

	def test_existing_print_format_and_plain_email_still_work(self):
		send_email(**self.base, print_format="Standard")
		self.assertEqual(self.make.call_args.kwargs["attachments"], [])
		self.assertEqual(self.make.call_args.kwargs["print_format"], "Standard")

	def test_single_and_batch_confirmation_show_filenames_without_reading_bytes(self):
		card = build_card("send_email", {**self.base, "attachments": ["FILE-A"]}, {})
		self.assertEqual(card["attachments"], ["sales.xlsx"])
		card = build_card("send_email", {"messages": [{**self.base, "attachments": ["FILE-B"]}]}, {})
		self.assertEqual(card["messages"][0]["attachments"], ["summary.pdf"])
		for file in self.files.values():
			file.get_content.assert_not_called()
		self.make.assert_not_called()

	def test_permission_is_rechecked_after_confirmation_preview(self):
		card = build_card("send_email", {**self.base, "attachments": ["FILE-A"]}, {})
		self.assertEqual(card["attachments"], ["sales.xlsx"])
		self.perm.side_effect = lambda dt, *args, **kwargs: dt != "File"
		with self.assertRaises(PermissionDeniedError):
			send_email(**self.base, attachments=["FILE-A"])
		self.files["FILE-A"].get_content.assert_not_called()
		self.make.assert_not_called()

	def test_generated_attachments_combine_with_a_print_format(self):
		send_email(**self.base, attachments=["FILE-A"], print_format="Standard")
		self.assertEqual(self.make.call_args.kwargs["attachments"], ["FILE-A"])
		self.assertEqual(self.make.call_args.kwargs["print_format"], "Standard")
