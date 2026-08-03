"""Validation + envelope tests for the Tier-3 desk-mirror action tools.

Each tool fires a side effect (sends mail, writes a Comment, opens a
DocShare row, creates a ToDo, mutates _user_tags, etc.). The tests
here mock the underlying Frappe helper so the asserts are about
- argument validation
- permission gating
- envelope shape
- the wrapper forwarding the right kwargs

without firing real emails / mutating shared bench state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools.add_comment import add_comment
from jarvis.tools.add_tag import add_tag
from jarvis.tools.assign_to import assign_to
from jarvis.tools.follow_document import follow_document
from jarvis.tools.remove_tag import remove_tag
from jarvis.tools.send_email import send_email
from jarvis.tools.share_doc import share_doc
from jarvis.tools.unassign_from import unassign_from
from jarvis.tools.unfollow_document import unfollow_document
from jarvis.tools.unshare_doc import unshare_doc
from jarvis.tools.update_comment import update_comment


def _all_exist():
	return patch("frappe.db.exists", return_value=True)


def _no_exists():
	return patch("frappe.db.exists", return_value=False)


def _allow_perm():
	return patch("frappe.has_permission", return_value=True)


def _deny_perm():
	return patch("frappe.has_permission", return_value=False)


# ---------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------


class TestSendEmail(FrappeTestCase):
	def test_rejects_missing_args(self):
		with self.assertRaises(InvalidArgumentError):
			send_email("", "Subj", "Body", "User", "x")
		with self.assertRaises(InvalidArgumentError):
			send_email("to@x.com", "", "Body", "User", "x")
		with self.assertRaises(InvalidArgumentError):
			send_email("to@x.com", "Subj", "", "User", "x")
		with self.assertRaises(InvalidArgumentError):
			send_email("to@x.com", "Subj", "Body", "", "x")

	def test_rejects_unknown_reference_doc(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			send_email("to@x.com", "Subj", "Body", "User", "Unknown")

	def test_returns_envelope_and_forwards_send_email_true(self):
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "COMM-X-001"},
			) as mk,
		):
			out = send_email(
				"to@example.com",
				"Hello",
				"<p>Hi</p>",
				"User",
				"test@example.com",
				cc=["cc@x.com"],
			)
		# The wrapper must always force send_email=True; otherwise
		# the call would silently log a Communication without sending.
		self.assertIs(mk.call_args.kwargs.get("send_email"), True)
		self.assertEqual(out["communication_name"], "COMM-X-001")
		self.assertEqual(out["subject"], "Hello")

	def test_plain_text_body_becomes_newline_preserving_html(self):
		# The agent composes a plain-text body with \n newlines (persona:
		# "plain text, \n newlines, no Markdown"), but Communication.content is
		# HTML - so without conversion every paragraph collapses into one
		# run-on block on delivery. The tool must turn newlines into <br>.
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "C"},
			) as mk,
		):
			send_email("to@x.com", "Subj", "Para one.\n\nPara two.", "User", "x")
		sent = mk.call_args.kwargs["content"]
		self.assertIn("<br>", sent)
		self.assertIn("Para one.", sent)
		self.assertIn("Para two.", sent)
		# the blank line between paragraphs survives as two breaks
		self.assertGreaterEqual(sent.count("<br>"), 2)

	def test_plain_text_html_special_chars_are_escaped(self):
		# Plain text is escaped before newline->br so a stray < or & can't
		# break the HTML body or inject markup.
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "C"},
			) as mk,
		):
			send_email("to@x.com", "Subj", "a < b & c", "User", "x")
		sent = mk.call_args.kwargs["content"]
		self.assertIn("&lt;", sent)
		self.assertIn("&amp;", sent)

	def test_html_like_content_is_escaped_not_rendered(self):
		# The contract is plain text: anything tag-shaped is escaped so it shows
		# literally rather than being interpreted (or eaten) as HTML.
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "C"},
			) as mk,
		):
			send_email("to@x.com", "Subj", "<p>Hi</p>", "User", "x")
		self.assertEqual(mk.call_args.kwargs["content"], "&lt;p&gt;Hi&lt;/p&gt;")

	def test_angle_bracketed_text_survives(self):
		# The regression that pushed us off an is_html guard: an email address
		# or doctype name in angle brackets must NOT vanish as an unknown tag.
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "C"},
			) as mk,
		):
			send_email("to@x.com", "Subj", "Reach me at <a@b.com>", "User", "x")
		self.assertIn("&lt;a@b.com&gt;", mk.call_args.kwargs["content"])

	def test_batch_messages_also_convert_newlines(self):
		# The batch path routes through _send_one, so it gets the same
		# conversion - lock it so a refactor can't skip it.
		with (
			_all_exist(),
			patch(
				"frappe.core.doctype.communication.email.make",
				return_value={"name": "C"},
			) as mk,
		):
			send_email(
				messages=[
					{
						"recipients": "to@x.com",
						"subject": "S",
						"content": "Line one.\n\nLine two.",
						"doctype": "User",
						"name": "x",
					}
				]
			)
		self.assertIn("<br>", mk.call_args.kwargs["content"])


# ---------------------------------------------------------------------
# add_comment / update_comment
# ---------------------------------------------------------------------


class TestAddComment(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			add_comment("", "x", "body")
		with self.assertRaises(InvalidArgumentError):
			add_comment("User", "x", "")

	def test_rejects_unknown_doc(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			add_comment("User", "missing", "body")

	def test_forwards_session_user_as_comment_author(self):
		fake_comment = MagicMock()
		fake_comment.name = "Comment-001"
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.utils.add_comment",
				return_value=fake_comment,
			) as ac,
			patch(
				"frappe.db.get_value",
				return_value="Admin User",
			),
		):
			out = add_comment("User", "test@example.com", "Hello")
		# The agent doesn't specify "who" - the wrapper must pull
		# the session user and forward it as both comment_email +
		# the resolved full_name as comment_by.
		self.assertIn(
			"comment_email",
			ac.call_args.kwargs,
		)
		self.assertEqual(out["comment_name"], "Comment-001")


class TestUpdateComment(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			update_comment("", "body")
		with self.assertRaises(InvalidArgumentError):
			update_comment("Comment-001", "")

	def test_rejects_unknown_comment(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			update_comment("Comment-Not-Real", "body")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.utils.update_comment",
			) as uc,
		):
			out = update_comment("Comment-001", "new body")
		uc.assert_called_once()
		self.assertEqual(out, {"comment_name": "Comment-001", "content": "new body"})


# ---------------------------------------------------------------------
# share_doc / unshare_doc
# ---------------------------------------------------------------------


class TestShareDoc(FrappeTestCase):
	def test_rejects_when_no_user_and_not_everyone(self):
		with _all_exist():
			with self.assertRaises(InvalidArgumentError):
				share_doc("User", "test@example.com")

	def test_rejects_unknown_doc(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			share_doc("User", "missing", user="someone@x.com")

	def test_returns_envelope_and_forwards_perms(self):
		with (
			_all_exist(),
			patch(
				"frappe.share.add",
			) as sa,
		):
			out = share_doc(
				"User",
				"test@example.com",
				user="other@example.com",
				read=True,
				write=True,
				share=False,
			)
		sa.assert_called_once()
		self.assertEqual(sa.call_args.kwargs.get("read"), 1)
		self.assertEqual(sa.call_args.kwargs.get("write"), 1)
		self.assertEqual(sa.call_args.kwargs.get("share"), 0)
		self.assertTrue(out["read"])

	def test_supports_everyone_without_user(self):
		with (
			_all_exist(),
			patch(
				"frappe.share.add",
			),
		):
			out = share_doc("User", "test@example.com", everyone=True)
		self.assertTrue(out["everyone"])


class TestUnshareDoc(FrappeTestCase):
	def test_rejects_empty_user(self):
		with self.assertRaises(InvalidArgumentError):
			unshare_doc("User", "test@example.com", "")

	def test_returns_envelope(self):
		with _all_exist(), patch("frappe.share.remove") as rm:
			out = unshare_doc("User", "test@example.com", "other@x.com")
		rm.assert_called_once()
		self.assertEqual(out["user"], "other@x.com")


# ---------------------------------------------------------------------
# assign_to / unassign_from
# ---------------------------------------------------------------------


class TestAssignTo(FrappeTestCase):
	def test_rejects_empty_user(self):
		with self.assertRaises(InvalidArgumentError):
			assign_to("User", "test@example.com", "")

	def test_forwards_assign_to_as_list(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.assign_to.add",
			) as ad,
		):
			out = assign_to(
				"User",
				"test@example.com",
				"other@example.com",
				description="follow up",
				notify=False,
			)
		ad.assert_called_once()
		args = ad.call_args.args[0]
		self.assertEqual(args["assign_to"], ["other@example.com"])
		self.assertEqual(args["doctype"], "User")
		self.assertEqual(args["name"], "test@example.com")
		self.assertEqual(args["notify"], 0)
		self.assertEqual(out["user"], "other@example.com")


class TestUnassignFrom(FrappeTestCase):
	def test_rejects_empty_user(self):
		with self.assertRaises(InvalidArgumentError):
			unassign_from("User", "test@example.com", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.assign_to.remove",
			) as rm,
		):
			out = unassign_from("User", "test@example.com", "other@x.com")
		rm.assert_called_once()
		self.assertEqual(out["user"], "other@x.com")


# ---------------------------------------------------------------------
# add_tag / remove_tag
# ---------------------------------------------------------------------


class TestAddTag(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			add_tag("User", "test@example.com", "")

	def test_rejects_when_no_write_perm(self):
		with _all_exist(), _deny_perm(), self.assertRaises(PermissionDeniedError):
			add_tag("User", "test@example.com", "urgent")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"frappe.desk.doctype.tag.tag.add_tag",
			) as at,
		):
			out = add_tag("User", "test@example.com", "urgent", color="#ff0000")
		at.assert_called_once()
		self.assertEqual(at.call_args.kwargs.get("tag"), "urgent")
		self.assertEqual(at.call_args.kwargs.get("color"), "#ff0000")
		self.assertEqual(out["tag"], "urgent")


class TestRemoveTag(FrappeTestCase):
	def test_rejects_empty(self):
		with self.assertRaises(InvalidArgumentError):
			remove_tag("User", "test@example.com", "")

	def test_returns_envelope(self):
		with (
			_all_exist(),
			_allow_perm(),
			patch(
				"frappe.desk.doctype.tag.tag.remove_tag",
			) as rt,
		):
			out = remove_tag("User", "test@example.com", "urgent")
		rt.assert_called_once()
		self.assertEqual(out["tag"], "urgent")


# ---------------------------------------------------------------------
# follow_document / unfollow_document
# ---------------------------------------------------------------------


class TestFollowDocument(FrappeTestCase):
	def test_rejects_unknown_doc(self):
		with _no_exists(), self.assertRaises(InvalidArgumentError):
			follow_document("User", "missing")

	def test_defaults_to_session_user(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.document_follow.follow_document",
				return_value=True,
			) as f,
		):
			out = follow_document("User", "test@example.com")
		# No `user` passed -> wrapper falls back to session.user
		self.assertIsNotNone(out["user"])
		self.assertTrue(out["followed"])
		# The underlying helper receives the session user verbatim.
		self.assertEqual(f.call_args.kwargs.get("user"), out["user"])

	def test_accepts_explicit_user(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.document_follow.follow_document",
				return_value=True,
			),
		):
			out = follow_document(
				"User",
				"test@example.com",
				user="other@example.com",
			)
		self.assertEqual(out["user"], "other@example.com")


class TestUnfollowDocument(FrappeTestCase):
	def test_returns_envelope(self):
		with (
			_all_exist(),
			patch(
				"frappe.desk.form.document_follow.unfollow_document",
				return_value=True,
			),
		):
			out = unfollow_document("User", "test@example.com")
		self.assertTrue(out["unfollowed"])
