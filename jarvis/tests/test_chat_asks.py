"""Tests for chat-sourced approvals (notify-approvals design Part 2).

Covers ``jarvis.chat.chat_asks`` — fence parsing (last fence wins, malformed
skipped, options captured), ``materialize_from_turn`` (row shape, owner,
approval:new publish, dedupe), ``resolve_on_user_message`` (Pending Chat rows
flip to Answered; File-Box/decided rows untouched) — plus the
``approvals_api`` extensions: ``decide()`` on a Chat-source row resumes the
conversation WITHOUT re-attaching the conversation's File, the status filter
accepts "Answered", the ``awaiting_reply`` lane (inclusion/exclusion ladder,
first page only), and source NULL = File Box back-compat.

Uses ``unittest.TestCase`` with explicit commits + prefix-based cleanup (like
test_feature_pages_api), since awaiting_reply/decide run raw owner-scoped SQL
and need REAL users. Every row this module creates carries a ``ca-`` marker so
``_wipe_all`` removes it regardless of owner.
"""

from __future__ import annotations

import contextlib
import json
import unittest
from unittest.mock import patch

import frappe

from jarvis.chat.approvals_api import (
	decide,
	dismiss_approval,
	get_approval,
	list_approvals_page,
	restore_approval,
)
from jarvis.chat.chat_asks import (
	materialize_from_turn,
	parse_ask,
	question_excerpt,
	resolve_on_user_message,
)

USER_A = "ca-user-a@example.com"
USER_B = "ca-user-b@example.com"

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"


def _fence(payload, suffix="") -> str:
	"""A ```jarvis-ask fenced block; ``payload`` may be a raw string
	(malformed-JSON cases) or any JSON-dumpable value."""
	body = payload if isinstance(payload, str) else json.dumps(payload)
	return f"```jarvis-ask{suffix}\n{body}\n```"


def _ask(q="Which supplier is this from?", type="single", options=("Acme", "Globex")):
	return {"q": q, "type": type, "options": list(options)}


# --------------------------------------------------------------------------- #
# module fixtures / helpers
# --------------------------------------------------------------------------- #
def _ensure_user(email: str) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	roles = set(frappe.get_roles(email))
	if "System Manager" in roles:
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	# Approvals endpoints are chat-surface: now require the Jarvis User role
	# (security review TASK 8).
	if "Jarvis User" not in roles:
		frappe.get_doc("User", email).add_roles("Jarvis User")
		frappe.db.commit()
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_A)
	_ensure_user(USER_B)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe_all() -> None:
	for conv in frappe.get_all(CONV, filters={"title": ["like", "ca-%"]}, pluck="name"):
		for ap in frappe.get_all(APPROVAL, filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc(APPROVAL, ap, force=True, ignore_permissions=True)
		frappe.db.delete(MSG, {"conversation": conv})
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": CONV, "attached_to_name": conv},
			pluck="name",
		):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	for name in frappe.get_all(APPROVAL, filters={"title": ["like", "ca-%"]}, pluck="name"):
		frappe.delete_doc(APPROVAL, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_conv(owner, title, status="Active") -> str:
	with _as(owner):
		doc = frappe.get_doc({"doctype": CONV, "title": title, "status": status})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _add_msg(conv, seq, role, content, streaming=0, recovering=0, error="") -> None:
	frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": seq,
			"role": role,
			"content": content,
			"streaming": streaming,
			"recovering": recovering,
			"error": error,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _mk_approval(
	owner,
	title,
	status="Pending",
	conversation=None,
	source=None,
	question="q?",
	options='["Approve","Reject"]',
	decision=None,
) -> str:
	with _as(owner):
		d = {
			"doctype": APPROVAL,
			"title": title,
			"status": status,
			"conversation": conversation,
			"question": question,
			"options": options,
		}
		if source is not None:
			d["source"] = source
		if decision is not None:
			d["decision"] = decision
		doc = frappe.get_doc(d)
		doc.insert(ignore_permissions=True)
	if source is None:
		# The doctype defaults source to "File Box" on insert; a PRE-FIELD row
		# has NULL. Force NULL so the back-compat (NULL = File Box) paths are
		# genuinely exercised.
		frappe.db.set_value(APPROVAL, doc.name, "source", None, update_modified=False)
	frappe.db.commit()
	return doc.name


def _attach_file(conv: str, owner: str, file_name="ca-attach.txt") -> str:
	with _as(owner):
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": CONV,
				"attached_to_name": conv,
				"is_private": 1,
				"content": "hello world",
			}
		)
		f.flags.ignore_permissions = True
		f.insert(ignore_permissions=True)
	frappe.db.commit()
	return f.name


# =========================================================================== #
# parse_ask / question_excerpt — pure parsing, ports of ChatView.askOf
# =========================================================================== #
class TestParseAsk(unittest.TestCase):
	def test_options_captured(self):
		qs = parse_ask("Need a call.\n\n" + _fence([_ask()]))
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["q"], "Which supplier is this from?")
		self.assertEqual(qs[0]["type"], "single")
		self.assertEqual(qs[0]["options"], ["Acme", "Globex"])

	def test_last_fence_wins(self):
		content = (
			_fence([_ask(q="old question?", options=("A", "B"))])
			+ "\nsome prose in between\n"
			+ _fence([_ask(q="new question?", options=("C", "D"))])
		)
		qs = parse_ask(content)
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["q"], "new question?")
		self.assertEqual(qs[0]["options"], ["C", "D"])

	def test_malformed_json_skipped(self):
		self.assertEqual(parse_ask(_fence('{"q": not json')), [])
		self.assertEqual(parse_ask(_fence('"just a string"')), [])
		self.assertEqual(parse_ask(_fence("42")), [])
		self.assertEqual(parse_ask("no fence at all?"), [])
		self.assertEqual(parse_ask(None), [])

	def test_questions_wrapper_accepted(self):
		qs = parse_ask(_fence({"questions": [_ask()]}))
		self.assertEqual(len(qs), 1)

	def test_fence_grammar_matches_client(self):
		# trailing spaces/tabs after the tag are allowed (the [ \t]* in _ASK_RE)
		self.assertEqual(len(parse_ask(_fence([_ask()], suffix="  \t"))), 1)
		# but no newline after the tag = no match ("```jarvis-askX" style)
		self.assertEqual(parse_ask("```jarvis-ask {}```"), [])

	def test_normalization_mirrors_client(self):
		qs = parse_ask(
			_fence(
				[
					{"q": "ok to proceed?", "type": "boolean"},  # boolean -> yesno
					{"q": "pick one", "type": "bogus", "options": ["x"]},  # unknown -> single
					{"q": "no options, not a field type", "type": "multi"},  # dropped
					{"question": "alt key works?", "type": "text"},  # question alias
					{"type": "single", "options": ["y"]},  # empty q dropped
				]
			)
		)
		self.assertEqual(
			[(q["q"], q["type"]) for q in qs],
			[("ok to proceed?", "yesno"), ("pick one", "single"), ("alt key works?", "text")],
		)

	def test_caps_six_questions_eight_options(self):
		qs = parse_ask(_fence([_ask(q=f"q{i}?") for i in range(9)]))
		self.assertEqual(len(qs), 6)
		qs = parse_ask(_fence([_ask(options=[f"o{i}" for i in range(12)])]))
		self.assertEqual(len(qs[0]["options"]), 8)

	def test_question_excerpt_fence(self):
		self.assertEqual(
			question_excerpt("prose\n" + _fence([_ask(q="Which one?")])),
			"Which one?",
		)

	def test_question_excerpt_prose_tail(self):
		out = question_excerpt("I drafted the invoice. Should I submit it now?  \n")
		self.assertTrue(out.endswith("Should I submit it now?"))
		long = ("x" * 400) + " Should I submit it now?"
		self.assertLessEqual(len(question_excerpt(long)), 140)


# =========================================================================== #
# materialize_from_turn — row creation + publish + dedupe
# =========================================================================== #
class TestMaterialize(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-mat")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _rows(self):
		return frappe.get_all(
			APPROVAL,
			filters={"conversation": self.conv},
			fields=["name", "title", "status", "source", "question", "options", "owner"],
		)

	def test_creates_pending_chat_row_and_publishes(self):
		content = "ca-lead-in\n" + _fence([_ask()])
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			name = materialize_from_turn(self.conv, content)
		self.assertTrue(name)
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.source, "Chat")
		self.assertEqual(row.title, "Which supplier is this from?")
		self.assertEqual(row.question, "Which supplier is this from?")
		self.assertEqual(json.loads(row.options), ["Acme", "Globex"])
		# owner = the CONVERSATION owner (board visibility), not the session user
		self.assertEqual(row.owner, USER_A)
		pub.assert_called_once()
		user, payload = pub.call_args[0]
		self.assertEqual(user, USER_A)
		self.assertEqual(payload["kind"], "approval:new")
		self.assertEqual(payload["conversation_id"], self.conv)
		self.assertEqual(payload["name"], name)
		self.assertEqual(payload["question"], "Which supplier is this from?")

	def test_no_fence_and_malformed_fence_skip(self):
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			self.assertIsNone(materialize_from_turn(self.conv, "plain question?"))
			self.assertIsNone(materialize_from_turn(self.conv, _fence("{broken")))
		self.assertEqual(self._rows(), [])
		pub.assert_not_called()

	def test_dedupe_on_pending_chat_row(self):
		content = _fence([_ask()])
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			first = materialize_from_turn(self.conv, content)
			second = materialize_from_turn(self.conv, content)
		self.assertTrue(first)
		self.assertIsNone(second)
		self.assertEqual(len(self._rows()), 1)

	def test_dedupe_ignores_filebox_pending(self):
		# A Pending File-Box row (source NULL — a pre-field row) must not
		# block a chat ask in the same conversation.
		_mk_approval(USER_A, "ca-appr-fb", "Pending", self.conv, source=None)
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, _fence([_ask()]))
		self.assertTrue(name)
		self.assertEqual(len(self._rows()), 2)

	def test_multi_question_joins_text_and_drops_options(self):
		content = _fence([_ask(q="first?"), _ask(q="second?", options=("X", "Y"))])
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, content)
		row = frappe.get_doc(APPROVAL, name)
		self.assertEqual(row.question, "first?\nsecond?")
		self.assertEqual(row.title, "first?")
		# one decision field can't answer two questions via chips
		self.assertFalse(row.options)

	def test_title_capped_at_100(self):
		q = "why " * 40 + "?"
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, _fence([_ask(q=q)]))
		self.assertEqual(len(frappe.db.get_value(APPROVAL, name, "title")), 100)


# =========================================================================== #
# resolve_on_user_message — reply-in-chat flips Pending Chat rows to Answered
# =========================================================================== #
class TestResolveOnUserMessage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-res")
		self.other = _mk_conv(USER_A, "ca-conv-res-other")
		self.chat_pending = _mk_approval(USER_A, "ca-appr-chat", "Pending", self.conv, source="Chat")
		self.fb_pending = _mk_approval(USER_A, "ca-appr-fbnull", "Pending", self.conv, source=None)
		self.chat_decided = _mk_approval(
			USER_A, "ca-appr-chat-done", "Approved", self.conv, source="Chat", decision="ok"
		)
		self.other_pending = _mk_approval(USER_A, "ca-appr-other", "Pending", self.other, source="Chat")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_flips_only_pending_chat_rows_of_that_conversation(self):
		with _as(USER_A):
			resolve_on_user_message(self.conv)

		def st(n):
			return frappe.db.get_value(APPROVAL, n, ["status", "decision"], as_dict=True)

		flipped = st(self.chat_pending)
		self.assertEqual(flipped.status, "Answered")
		self.assertEqual(flipped.decision, "(answered in chat)")
		# NULL source = File Box: untouched (back-compat)
		self.assertEqual(st(self.fb_pending).status, "Pending")
		# already-decided chat row untouched
		self.assertEqual(st(self.chat_decided).status, "Approved")
		# other conversation untouched
		self.assertEqual(st(self.other_pending).status, "Pending")
		# audit stamps
		row = frappe.db.get_value(APPROVAL, self.chat_pending, ["decided_by", "decided_at"], as_dict=True)
		self.assertEqual(row.decided_by, USER_A)
		self.assertTrue(row.decided_at)

	def test_answered_row_cannot_be_decided_again(self):
		with _as(USER_A):
			resolve_on_user_message(self.conv)
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			decide(self.chat_pending, "too late", 1)


# =========================================================================== #
# decide() — Chat-source rows resume WITHOUT the File re-attach
# =========================================================================== #
class TestDecideChatSource(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-dec")
		_attach_file(self.conv, USER_A)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_chat_row_resumes_without_reattach(self):
		ap = _mk_approval(USER_A, "ca-appr-dec-chat", "Pending", self.conv, source="Chat")
		with _as(USER_A), patch("jarvis.chat.api.send_message", return_value={"ok": True}) as sm:
			res = decide(ap, "Acme", 1)
		self.assertTrue(res["ok"])
		self.assertTrue(res["resumed"])
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Approved")
		sm.assert_called_once()
		kw = sm.call_args.kwargs
		self.assertEqual(kw["conversation"], self.conv)
		self.assertIsNone(kw["attachments"])  # the guard: no file re-attach
		self.assertIn("Acme", kw["message"])

	def test_filebox_row_still_reattaches(self):
		# source NULL (pre-field row) = File Box: the re-attach must survive.
		ap = _mk_approval(USER_A, "ca-appr-dec-fb", "Pending", self.conv, source=None)
		with _as(USER_A), patch("jarvis.chat.api.send_message", return_value={"ok": True}) as sm:
			decide(ap, "Approve as drafted", 1)
		sm.assert_called_once()
		attachments = sm.call_args.kwargs["attachments"]
		self.assertTrue(attachments)
		self.assertEqual(json.loads(attachments)[0]["file_name"], "ca-attach.txt")


# =========================================================================== #
# dismiss / restore — clear a request off the board without acting
# =========================================================================== #
class TestDismissApproval(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-dis")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_dismiss_never_resumes_the_chat(self):
		ap = _mk_approval(USER_A, "ca-appr-dis", "Pending", self.conv, source="Chat")
		with _as(USER_A), patch("jarvis.chat.api.send_message") as sm:
			res = dismiss_approval(ap)
		self.assertEqual(res["status"], "Dismissed")
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Dismissed")
		# the whole point: the agent is told NOTHING
		sm.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "decided_by"), USER_A)

	def test_dismiss_rejects_non_pending(self):
		ap = _mk_approval(USER_A, "ca-appr-dis2", "Approved", self.conv, decision="yes")
		with _as(USER_A), self.assertRaises(Exception):
			dismiss_approval(ap)

	def test_dismiss_out_of_pending_and_decided(self):
		ap = _mk_approval(USER_A, "ca-appr-dis3", "Pending", self.conv, source="Chat")
		with _as(USER_A):
			dismiss_approval(ap)
			# not in the Pending badge, not in Decided, but findable via Dismissed
			pend = {r["name"] for r in list_approvals_page(filters={"status": "Pending"})["rows"]}
			dec = {r["name"] for r in list_approvals_page(filters={"status": "Decided"})["rows"]}
			dis = {r["name"] for r in list_approvals_page(filters={"status": "Dismissed"})["rows"]}
		self.assertNotIn(ap, pend)
		self.assertNotIn(ap, dec)
		self.assertIn(ap, dis)

	def test_restore_puts_it_back_pending(self):
		ap = _mk_approval(USER_A, "ca-appr-dis4", "Pending", self.conv, source="Chat")
		with _as(USER_A):
			dismiss_approval(ap)
			res = restore_approval(ap)
		self.assertEqual(res["status"], "Pending")
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Pending")
		self.assertFalse(frappe.db.get_value(APPROVAL, ap, "decided_by"))

	def test_restore_only_from_dismissed(self):
		ap = _mk_approval(USER_A, "ca-appr-dis5", "Approved", self.conv, decision="yes")
		with _as(USER_A), self.assertRaises(Exception):
			restore_approval(ap)

	def test_dismiss_permission_scoped(self):
		ap = _mk_approval(USER_A, "ca-appr-dis6", "Pending", self.conv, source="Chat")
		# USER_B does not own the conversation and is not SM
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			dismiss_approval(ap)


# =========================================================================== #
# list_approvals_page — Answered filter, source back-compat, awaiting_reply
# =========================================================================== #
class TestApprovalsPageExtensions(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-page")
		self.pending_fb = _mk_approval(USER_A, "ca-page-fb", "Pending", self.conv, source=None)
		self.pending_chat = _mk_approval(USER_A, "ca-page-chat", "Pending", self.conv, source="Chat")
		self.answered = _mk_approval(
			USER_A,
			"ca-page-ans",
			"Answered",
			self.conv,
			source="Chat",
			decision="(answered in chat)",
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw)

	def test_answered_status_filter(self):
		res = self._page(filters={"status": "Answered"})
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.answered)

	def test_answered_outside_pending_and_decided(self):
		def names(r):
			return {x["name"] for x in r["rows"]}

		self.assertNotIn(self.answered, names(self._page()))  # default Pending
		self.assertNotIn(self.answered, names(self._page(filters={"status": "Decided"})))
		self.assertIn(self.answered, names(self._page(filters={"status": "All"})))

	def test_source_null_reads_as_filebox(self):
		rows = {r["name"]: r for r in self._page(filters={"status": "All"})["rows"]}
		self.assertEqual(rows[self.pending_fb]["source"], "File Box")
		self.assertEqual(rows[self.pending_chat]["source"], "Chat")
		with _as(USER_A):
			self.assertEqual(get_approval(self.pending_fb)["source"], "File Box")
			self.assertEqual(get_approval(self.pending_chat)["source"], "Chat")


class TestAwaitingReply(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		# included: prose "?" WITH work depth (a tool call) — a real question
		# mid-task, not an opener greeting
		self.conv_q = _mk_conv(USER_A, "ca-conv-await-q")
		_add_msg(self.conv_q, 1, "user", "draft the invoice")
		_add_msg(self.conv_q, 2, "tool", "create_doc result")
		_add_msg(self.conv_q, 3, "assistant", "I drafted it. Should I submit the invoice now?")
		# included: jarvis-ask fence always qualifies (no depth needed), even
		# with NO Pending chat row (e.g. a malformed/recovery-finalized fence)
		self.conv_fence = _mk_conv(USER_A, "ca-conv-await-fence")
		_add_msg(self.conv_fence, 1, "user", "hi")
		_add_msg(self.conv_fence, 2, "assistant", "Pick one.\n" + _fence("{broken json"))
		# excluded: a shallow prose "?" with one user turn and no tools is an
		# opener greeting, not a genuine awaiting-input (the flood we prevent)
		self.conv_greet = _mk_conv(USER_A, "ca-conv-await-greet")
		_add_msg(self.conv_greet, 1, "user", "hi")
		_add_msg(self.conv_greet, 2, "assistant", "Hi! What would you like to work on?")
		# excluded: reply is not a question
		self.conv_done = _mk_conv(USER_A, "ca-conv-await-done")
		_add_msg(self.conv_done, 1, "user", "hi")
		_add_msg(self.conv_done, 2, "assistant", "All done. The invoice is submitted.")
		# excluded: still streaming
		self.conv_stream = _mk_conv(USER_A, "ca-conv-await-stream")
		_add_msg(self.conv_stream, 1, "user", "hi")
		_add_msg(self.conv_stream, 2, "assistant", "Working on it?", streaming=1)
		# excluded: errored turn
		self.conv_err = _mk_conv(USER_A, "ca-conv-await-err")
		_add_msg(self.conv_err, 1, "user", "hi")
		_add_msg(self.conv_err, 2, "assistant", "Should I retry?", error="boom")
		# excluded: the user already replied (last message role = user)
		self.conv_replied = _mk_conv(USER_A, "ca-conv-await-replied")
		_add_msg(self.conv_replied, 1, "assistant", "Which supplier?")
		_add_msg(self.conv_replied, 2, "user", "Acme")
		# excluded: a Pending Chat approval row already covers it
		self.conv_rowed = _mk_conv(USER_A, "ca-conv-await-rowed")
		_add_msg(self.conv_rowed, 1, "assistant", "Choose.\n" + _fence([_ask()]))
		_mk_approval(USER_A, "ca-await-rowed", "Pending", self.conv_rowed, source="Chat")
		# excluded for A: owned by B (and given work depth so it qualifies for B)
		self.conv_b = _mk_conv(USER_B, "ca-conv-await-b")
		_add_msg(self.conv_b, 1, "user", "run B's report")
		_add_msg(self.conv_b, 2, "tool", "report ran")
		_add_msg(self.conv_b, 3, "assistant", "B, should I proceed?")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _lane(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw).get("awaiting_reply")

	def test_inclusion_exclusion_ladder(self):
		lane = self._lane()
		convs = {r["conversation"] for r in lane}
		self.assertEqual(convs, {self.conv_q, self.conv_fence})
		for r in lane:
			self.assertEqual(set(r.keys()), {"conversation", "title", "question_excerpt", "last_at"})
			self.assertTrue(r["title"].startswith("ca-conv-await"))
			self.assertLessEqual(len(r["question_excerpt"]), 140)
			self.assertTrue(r["last_at"])
		# the shallow opener greeting is NOT surfaced (the flood we prevent)
		self.assertNotIn(self.conv_greet, convs)
		by_conv = {r["conversation"]: r for r in lane}
		self.assertTrue(by_conv[self.conv_q]["question_excerpt"].endswith("Should I submit the invoice now?"))
		# newest first: conv_greet was seeded last but excluded, so conv_fence
		# (seeded after conv_q) leads
		self.assertEqual(lane[0]["conversation"], self.conv_fence)

	def test_owner_scoped(self):
		lane_b = self._lane(user=USER_B)
		self.assertEqual({r["conversation"] for r in lane_b}, {self.conv_b})

	def test_first_page_only(self):
		with _as(USER_A):
			res = list_approvals_page(start=20)
		self.assertNotIn("awaiting_reply", res)

	def test_pending_chat_row_suppresses_and_answered_releases(self):
		# The Pending Chat row hides conv_rowed; answering in chat (resolve)
		# flips the row to Answered... and the conversation would reappear —
		# except a real reply also flips the last message to role=user, so
		# add that too and it stays out. Both invariants in one flow.
		self.assertNotIn(self.conv_rowed, {r["conversation"] for r in self._lane()})
		with _as(USER_A):
			resolve_on_user_message(self.conv_rowed)
		self.assertIn(self.conv_rowed, {r["conversation"] for r in self._lane()})
		_add_msg(self.conv_rowed, 2, "user", "Acme")
		self.assertNotIn(self.conv_rowed, {r["conversation"] for r in self._lane()})


if __name__ == "__main__":
	unittest.main()
