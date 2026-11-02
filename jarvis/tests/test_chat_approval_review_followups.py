"""Sowmya re-review follow-ups on the reviewer grant: raw role/disable writes,
migration paging, comment moderation, API vs generic visibility parity, and a
board answer that settles only its own question when another is tagged."""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api, chat_asks, docmeta_api
from jarvis.tests import test_chat_approval_continuation as continuation_fixture
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = continuation_fixture.AR
CONV = continuation_fixture.CONV
GRANT = "Jarvis Approval Reviewer"


class _Stop(BaseException):
	"""Ends a real call path right after the call under test (not an Exception,
	so the path's own best-effort handlers cannot swallow it)."""


class TestChatApprovalReviewFollowups(continuation_fixture.ComposedChatReviewFixture, FrappeTestCase):
	def reviewer_reach(self):
		with as_user(self.reviewer):
			listed = self.chat in {
				r["name"] for r in approvals_api.list_approvals_page(search="QA756")["rows"]
			}
			try:
				approvals_api.get_approval(self.chat)
				detail = True
			except frappe.PermissionError:
				detail = False
			try:
				with patch("jarvis.chat.api.send_message") as send:
					approvals_api.decide(self.chat, "Yes")
				decided = True
			except frappe.PermissionError:
				frappe.db.rollback()
				decided = False
				send.assert_not_called()
		return {"listed": listed, "detail": detail, "decided": decided}

	def requester_sees_tag(self):
		with as_user(self.owner):
			return self.reviewer in {r["user"] for r in approvals_api.get_approval(self.chat)["reviewers"]}

	def test_raw_role_removal_and_raw_disable_leave_an_inert_dormant_grant(self):
		"""Sowmya #1 / N-M1 / D10a: own Has Role and User rows only."""
		denied = {"listed": False, "detail": False, "decided": False}
		for change in ("has role", "disabled"):
			with self.subTest(change=change):
				self.tag()
				if change == "has role":
					frappe.db.delete(
						"Has Role", {"parent": self.reviewer, "parenttype": "User", "role": "Jarvis Admin"}
					)
				else:
					frappe.db.set_value("User", self.reviewer, "enabled", 0)
				frappe.db.commit()
				frappe.clear_cache(user=self.reviewer)
				reach = self.reviewer_reach()
				grant_kept = bool(frappe.db.exists(GRANT, {"approval": self.chat, "user": self.reviewer}))
				hidden_from_requester = not self.requester_sees_tag()
				if change == "has role":
					ensure_user(self.reviewer, ("Jarvis User", "Jarvis Admin"))
				else:
					frappe.db.set_value("User", self.reviewer, "enabled", 1)
					frappe.db.commit()
				frappe.clear_cache(user=self.reviewer)
				shown_again = self.requester_sees_tag()
				with as_user(self.owner):
					approvals_api.untag_reviewer(self.chat, self.reviewer)
				self.assertEqual(
					(reach, grant_kept, hidden_from_requester, shown_again, self.requester_sees_tag()),
					(denied, True, True, True, False),
				)

	def test_migration_pages_in_batches_prints_counts_only_and_is_idempotent(self):
		"""Sowmya #3 / N-M2."""
		from jarvis.patches import v2_29_chat_approval_reviewers as patch_module

		names, shares = [], []
		for _ in range(5):
			name = self.fx.question("Chat")
			names.append(name)
			shares.append(self.fx.legacy_share(AR, name))
			shares.append(self.fx.legacy_share(CONV, self.conversation_of(name)))
		frappe.db.commit()
		batches = []
		real_delete = frappe.db.delete

		def delete(doctype, filters=None, *args, **kwargs):
			if doctype == "DocShare" and isinstance(filters, dict) and "name" in filters:
				batches.append(len(filters["name"][1]))
			return real_delete(doctype, filters, *args, **kwargs)

		out = io.StringIO()
		with patch.object(patch_module, "BATCH", 2), patch.object(frappe.db, "delete", side_effect=delete):
			with redirect_stdout(out):
				first = patch_module.execute()
				second = patch_module.execute()
		printed = out.getvalue()
		self.assertGreaterEqual(first["approval_shares_reset"], 5)
		self.assertGreaterEqual(first["conversation_shares_reset"], 5)
		self.assertEqual(second, {"approval_shares_reset": 0, "conversation_shares_reset": 0})
		self.assertFalse([s for s in shares if frappe.db.exists("DocShare", s)])
		self.assertLessEqual(max(batches), 2)
		self.assertGreaterEqual(len([b for b in batches if b]), 3)  # >= 3 pages of approval shares
		self.assertEqual(
			printed.splitlines(),
			[f"Chat review legacy access reset: {first}", f"Chat review legacy access reset: {second}"],
		)
		for private in (*names, *self.users, *self.conversations):
			self.assertNotIn(private, printed)

	def test_requester_without_manager_role_cannot_delete_a_reviewer_comment(self):
		"""Sowmya #5 / N-M4: the existing author-or-System-Manager rule."""
		self.tag()
		with as_user(self.reviewer):
			note = docmeta_api.add_comment(AR, self.chat, "QA756 reviewer note")["name"]
		self.comments.append(note)
		with as_user(self.owner), self.assertRaises(frappe.PermissionError):
			docmeta_api.delete_comment(note)
		self.assertTrue(frappe.db.exists("Comment", note))

	def test_review_api_and_generic_visibility_agree_with_the_policy(self):
		"""Sowmya #8 / N-M5: one matrix, live; API = requester + tagged eligible
		reviewer on scoped rows; generic = requester only; other sources unchanged."""
		untagged = self.extra_user("sm2", ("Jarvis User", "System Manager"))
		self.tag()
		self.tag(self.manager)
		with as_user(self.owner):
			orphan = frappe.get_doc(
				{
					"doctype": AR,
					"source": "Chat",
					"title": "QA756 orphan",
					"question": "?",
					"status": "Pending",
				}
			).insert(ignore_permissions=True)
		self.approvals.append(orphan.name)
		frappe.db.commit()
		with as_user(self.owner):
			approvals_api.tag_reviewer(orphan.name, self.reviewer)
		inline = self.fx.question("Chat")
		frappe.db.set_value(CONV, self.conversation_of(inline), "file_box", 1)
		frappe.db.commit()
		questions = {
			"scoped": self.chat,
			"conversation-less": orphan.name,
			"File Box": self.filebox,
			"inline": inline,
		}
		actors = {
			"requester": self.owner,
			"tagged admin": self.reviewer,
			"tagged manager": self.manager,
			"untagged manager": untagged,
			"ordinary": self.ordinary,
		}
		seen = {}
		for actor, user in actors.items():
			with as_user(user):
				page = {r["name"] for r in approvals_api.list_approvals_page(search="QA756")["rows"]}
				for label, name in questions.items():
					try:
						approvals_api.get_approval(name)
						detail = True
					except frappe.PermissionError:
						detail = False
					seen[(actor, label)] = (
						name in page,
						detail,
						bool(frappe.has_permission(AR, "read", doc=name)),
						bool(frappe.get_list(AR, filters={"name": name})),
					)
		api_and_generic = {
			"scoped": {"requester": (1, 1), "tagged admin": (1, 0), "tagged manager": (1, 0)},
			"conversation-less": {"requester": (1, 1), "tagged admin": (1, 0)},
			"File Box": {"requester": (1, 1), "tagged manager": (1, 1), "untagged manager": (1, 1)},
			"inline": {"requester": (1, 1), "tagged manager": (1, 1), "untagged manager": (1, 1)},
		}
		expected = {}
		for actor in actors:
			for label in questions:
				api, generic = api_and_generic[label].get(actor, (0, 0))
				expected[(actor, label)] = (bool(api), bool(api), bool(generic), bool(generic))
		self.assertEqual(seen, expected)

	def board_answer_with_two_open_questions(self):
		"""Q1 tagged to the admin reviewer, Q2 (agent-created) tagged to the manager,
		both Pending in one chat; the admin's decision of Q1 reaches the chat."""
		from jarvis.tools.create_doc import create_doc

		self.tag()
		conv = self.conversation_of(self.chat)
		with as_user(self.owner):
			q2 = create_doc(
				AR,
				{
					"title": "QA756 second",
					"source": "Chat",
					"conversation": conv,
					"question": "?",
					"status": "Pending",
				},
			)["name"]
			self.approvals.append(q2)
			frappe.db.commit()
			approvals_api.tag_reviewer(q2, self.manager)
		with (
			as_user(self.reviewer),
			continuation_fixture.real_send_without_worker(),
			patch("frappe.publish_realtime"),
		):
			self.assertEqual(approvals_api.decide(self.chat, "Yes")["continuation"], "done")
			frappe.db.commit()
		seed = self.user_messages(conv)[-1]
		self.assertEqual(seed.origin, "board_answer")
		return conv, q2, seed.name

	def test_a_board_answer_leaves_another_tagged_question_open(self):
		"""Sowmya #6 / C9 / N23."""
		conv, q2, seed = self.board_answer_with_two_open_questions()
		untagged = self.fx.question("Chat")
		frappe.db.set_value(AR, untagged, "conversation", conv)
		frappe.db.commit()
		with as_user(self.owner), patch("frappe.publish_realtime"):
			chat_asks.resolve_on_user_message(conv, seed_message=seed)
		after_board = (frappe.db.get_value(AR, q2, "status"), frappe.db.get_value(AR, untagged, "status"))
		with as_user(self.manager):
			reviewer_can_act = approvals_api.get_approval(q2)["can_act"]
		with as_user(self.owner), patch("frappe.publish_realtime"):
			chat_asks.resolve_on_user_message(conv)  # the owner's own message (no board seed)
		self.assertEqual(
			(after_board, reviewer_can_act, frappe.db.get_value(AR, q2, "status")),
			(("Pending", "Answered"), 1, "Answered"),
		)

	def test_both_turn_start_paths_pass_their_real_seed(self):
		"""N23: the pump prepare and the legacy handler call sites, run for real
		up to the answer step (only the turn claim / Redis bindings are stubbed)."""
		from jarvis.chat import connector_focus, prepare, turn_handler, turn_message_binding

		conv, _q2, seed = self.board_answer_with_two_open_questions()
		calls = []

		def resolve(conversation, seed_message=None):
			calls.append((conversation, seed_message))
			raise _Stop

		claimed = {
			"turn": {"conversation": conv, "version": 1, "seed_message": seed, "state": "queued"},
			"claimed": True,
			"skipped": None,
		}
		with (
			as_user(self.owner),
			patch.object(chat_asks, "resolve_on_user_message", side_effect=resolve),
			patch.object(turn_message_binding, "bind_turn_message"),
			patch.object(connector_focus, "bind_turn"),
			patch.object(prepare.txn, "replay_on_conflict", return_value=claimed),
		):
			with self.assertRaises(_Stop):
				prepare.run_prepare("qa756-run")
			with self.assertRaises(_Stop):
				turn_handler.handle_chat_send(
					{"conversation_id": conv, "message_id": seed, "run_id": "qa756-run"}
				)
		self.assertEqual(calls, [(conv, seed), (conv, seed)])
