"""A reviewer's recorded decision reaches the requester's chat exactly once, as the
requester, or leaves a durable requester-only recovery state.

The real send_message inserts, claims and commits here; only the worker
dispatch is stubbed (no turn, no outbound call). Realtime is captured at
frappe.publish_realtime so every notification path is observed unchanged.
"""

from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api, chat_asks
from jarvis.tests import test_chat_approval_reviewers as reviewers_fixture
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = reviewers_fixture.AR
CONV = reviewers_fixture.CONV
MSG = "Jarvis Chat Message"
FAILED_KIND = "approval:continuation_failed"
_FIXTURE_ATTRS = (
	"owner",
	"reviewer",
	"manager",
	"ordinary",
	"chat",
	"filebox",
	"files",
	"users",
	"conversations",
	"approvals",
)


class Realtime:
	"""Records delivered frappe.publish_realtime payloads; ``fail`` picks the ones
	whose delivery raises (with private values in the exception text)."""

	def __init__(self, fail=None):
		self.fail = fail
		self.delivered = []
		self.failed = []

	def __call__(self, event=None, message=None, *args, **kwargs):
		user = kwargs.get("user")
		if isinstance(message, dict) and self.fail and self.fail(user, message):
			self.failed.append((user, message))
			raise RuntimeError(f"QA756 realtime down for {user} {message}")
		self.delivered.append((user, message))

	def payloads(self, user, kind):
		return [m for u, m in self.delivered if u == user and isinstance(m, dict) and m.get("kind") == kind]

	def recipients(self, kind, name):
		return {
			u
			for u, m in self.delivered
			if isinstance(m, dict) and m.get("kind") == kind and m.get("name") == name
		}


@contextmanager
def real_send_without_worker(*, machine=False):
	"""The REAL send_message (seed insert, claim, commit). Only the turn dispatch
	and the managed site-readiness gate are stubbed, so no worker, provider or admin
	control plane is reached. ``machine`` picks the turn-machine (admission) branch;
	otherwise the legacy branch."""
	from jarvis import account
	from jarvis.chat import admission
	from jarvis.chat import api as chat_api

	with (
		patch.object(account, "_admin_chat_gate", return_value={"ready": True, "reason": None}),
		patch.object(admission, "turn_machine_enabled", return_value=machine),
		patch.object(chat_api, "_dispatch_turn", return_value=None) as dispatch,
		patch.object(chat_api, "send_message", wraps=chat_api.send_message) as send,
	):
		yield dispatch, send


@contextmanager
def failing_seed_insert(conversation):
	"""Raise right after the user seed row is inserted, before send_message commits."""
	from jarvis.jarvis.doctype.jarvis_chat_message.jarvis_chat_message import JarvisChatMessage

	raised = []

	def after_insert(doc):
		if doc.conversation == conversation and doc.role == "user":
			raised.append(doc.name)
			raise RuntimeError("QA756 injected failure after the seed insert")

	with patch.object(JarvisChatMessage, "after_insert", after_insert, create=True):
		yield raised


class ComposedChatReviewFixture:
	"""Reuses TestChatReviewers' users/questions/cleanup by composition, so its own
	tests are not collected again. Also removes this test's own audit artifacts."""

	def setUp(self):
		super().setUp()
		self.started = frappe.utils.now_datetime()
		self.comments, self.todos, self.extra_files = [], [], []
		self.fx = reviewers_fixture.TestChatReviewers("runTest")
		self.addCleanup(self.cleanup_own_artifacts)  # LIFO: after the fixture's cleanup
		self.addCleanup(self.fx.doCleanups)
		self.fx.setUp()
		for attr in _FIXTURE_ATTRS:
			setattr(self, attr, getattr(self.fx, attr))
		self.secret = "QA756 private " + uuid4().hex[:8]
		for name in (self.chat, self.filebox):
			frappe.db.set_value(AR, name, "question", self.secret + " question", update_modified=False)
		frappe.db.commit()

	def tag(self, user=None):
		with as_user(self.owner):
			return approvals_api.tag_reviewer(self.chat, user or self.reviewer)

	def extra_user(self, label, roles):
		user = self.owner.replace("-owner@", f"-{label}@")
		self.users.append(user)  # the fixture's own list: removed by its cleanup
		return ensure_user(user, roles)

	def failed_by_reviewer(self, decision="Yes"):
		"""The Sowmya MAJOR state: a reviewer decided while the requester could
		not be resumed (disabled), then the requester was re-enabled."""
		self.tag()
		self.set_enabled(self.owner, 0)
		with as_user(self.reviewer), real_send_without_worker(), patch("frappe.publish_realtime"):
			result = approvals_api.decide(self.chat, decision)
			frappe.db.commit()
		self.set_enabled(self.owner, 1)
		self.assertEqual(result.get("continuation"), "failed")
		return self.conversation_of(self.chat)

	def conversation_of(self, name):
		return frappe.db.get_value(AR, name, "conversation")

	def set_enabled(self, user, enabled):
		with as_user("Administrator"):
			doc = frappe.get_doc("User", user)
			doc.enabled = enabled
			doc.save(ignore_permissions=True)
			frappe.db.commit()

	def owned_todo(self):
		with as_user(self.owner):
			todo = frappe.get_doc({"doctype": "ToDo", "description": "QA756 trace target"}).insert()
		self.todos.append(todo.name)
		frappe.db.commit()
		return todo.name

	def user_messages(self, conversation):
		return frappe.get_all(
			MSG,
			filters={"conversation": conversation, "role": "user"},
			fields=["name", "owner", "origin", "content", "seq"],
			order_by="seq asc",
		)

	def new_error_logs(self):
		return frappe.get_all(
			"Error Log",
			filters={"creation": [">=", self.started]},
			fields=["name", "method", "error"],
		)

	def private_values_in_error_logs(self):
		"""Which private values any Error Log written by this test carries (G5/N16b)."""
		markers = {
			"owner": self.owner,
			"reviewer": self.reviewer,
			"manager": self.manager,
			"secret": self.secret,
			"traceback": "Traceback",
		}
		for name in (self.chat, self.filebox):
			markers["conversation " + name] = self.conversation_of(name)
		found = set()
		for log in self.new_error_logs():
			text = (log.method or "") + "\n" + (log.error or "")
			found |= {label for label, value in markers.items() if value and value in text}
		return sorted(found)

	def cleanup_own_artifacts(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for name in self.extra_files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, ignore_permissions=True, force=True, delete_permanently=True)
		if self.todos:
			frappe.db.delete("Comment", {"reference_doctype": "ToDo", "reference_name": ["in", self.todos]})
			frappe.db.delete("ToDo", {"name": ["in", self.todos]})
		files = [*self.files.values(), *self.extra_files]
		owned = {
			AR: self.approvals,
			CONV: self.conversations,
			"User": self.users,
			"Notification Settings": self.users,
			"File": files,
			"Comment": self.comments,
			"ToDo": self.todos,
		}
		for doctype, names in owned.items():
			names = [n for n in names if n]
			if names:
				frappe.db.delete("Version", {"ref_doctype": doctype, "docname": ["in", names]})
				frappe.db.delete(
					"Deleted Document", {"deleted_doctype": doctype, "deleted_name": ["in", names]}
				)
				# Timeline rows ("Shared", "Attachment", "Deleted") the stubbed
				# delete_dynamic_links job would otherwise have removed.
				frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": ["in", names]})
		# Timeline rows without a reference name (a delete's "Deleted" row) by this test's users.
		frappe.db.delete("Comment", {"owner": ["in", self.users], "creation": [">=", self.started]})
		for user in self.users:  # per-user meta cache Frappe keeps without a TTL
			for key in frappe.cache.get_keys(f"masked_fields::*::{user}"):
				frappe.cache.delete(key)
		# Archives of comments/files whose names this test never held (likes, uploads).
		for approval in self.approvals:
			frappe.db.delete(
				"Deleted Document",
				{
					"deleted_doctype": ["in", ["Comment", "File"]],
					"creation": [">=", self.started],
					"data": ["like", f'%"{approval}"%'],
				},
			)
		markers = [self.secret, "QA756", *self.approvals, *self.users, *self.conversations]
		for log in self.new_error_logs():
			text = (log.method or "") + (log.error or "")
			if (log.method or "").startswith(("jarvis.approval.", "approval resume")) or any(
				m in text for m in markers
			):
				frappe.db.delete("Error Log", {"name": log.name})
		frappe.db.commit()


class TestChatApprovalContinuation(ComposedChatReviewFixture, FrappeTestCase):
	def test_disabled_requester_leaves_a_durable_requester_only_continuation_failure(self):
		"""Sowmya MAJOR / N1: the owner identity guard refuses before any message."""
		self.tag()
		conv = self.conversation_of(self.chat)
		self.set_enabled(self.owner, 0)
		realtime = Realtime()
		decision = self.secret + " decision"
		with (
			as_user(self.reviewer),
			real_send_without_worker() as (dispatch, send),
			patch("frappe.publish_realtime", side_effect=realtime),
		):
			response = approvals_api.decide(self.chat, decision)
			frappe.db.commit()  # request end
		stored = frappe.get_doc(AR, self.chat)
		with as_user(self.reviewer):
			reviewer_view = approvals_api.get_approval(self.chat)
		self.set_enabled(self.owner, 1)
		with as_user(self.owner):
			requester_view = approvals_api.get_approval(self.chat)
		observed = {
			"response": {k: response.get(k) for k in ("ok", "status", "resumed", "continuation")},
			"resume_state": stored.get("resume_state"),
			"decision": (stored.status, stored.decision, stored.decided_by),
			"messages": len(self.user_messages(conv)),
			"send_calls": send.call_count,
			"dispatch_calls": dispatch.call_count,
			"requester_events": realtime.payloads(self.owner, FAILED_KIND),
			"reviewer_events": realtime.payloads(self.reviewer, FAILED_KIND),
			"reviewer_view": {
				"continuation": reviewer_view.get("continuation"),
				"can_continue": reviewer_view.get("can_continue") or 0,
				"conversation": reviewer_view.get("conversation") or "",
				"origin_page": reviewer_view.get("origin_page") or "",
			},
			"requester_view": {
				"continuation": requester_view.get("continuation"),
				"can_continue": requester_view.get("can_continue") or 0,
			},
			"private_values_in_error_logs": self.private_values_in_error_logs(),
		}
		self.assertEqual(
			observed,
			{
				"response": {"ok": True, "status": "Approved", "resumed": False, "continuation": "failed"},
				"resume_state": "failed",
				"decision": ("Approved", decision, self.reviewer),
				"messages": 0,
				"send_calls": 0,
				"dispatch_calls": 0,
				"requester_events": [{"kind": FAILED_KIND, "name": self.chat}],
				"reviewer_events": [],
				"reviewer_view": {
					"continuation": "failed",
					"can_continue": 0,
					"conversation": "",
					"origin_page": "",
				},
				"requester_view": {"continuation": "failed", "can_continue": 1},
				"private_values_in_error_logs": [],
			},
		)

	def test_send_refused_before_insert_records_failed_without_a_seed(self):
		"""N3: a known refusal (quota) creates no message and a durable failed state."""
		conv = self.conversation_of(self.chat)
		with (
			as_user(self.owner),
			real_send_without_worker() as (dispatch, send),
			patch("jarvis.chat.api.validate_can_send", return_value=(False, "QA756 quota reached")),
			patch("frappe.publish_realtime", side_effect=Realtime()),
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()  # request end
		observed = {
			"response": {k: response.get(k) for k in ("ok", "status", "resumed", "continuation")},
			"resume_state": frappe.get_doc(AR, self.chat).get("resume_state"),
			"messages": len(self.user_messages(conv)),
			"send_calls": send.call_count,
			"dispatch_calls": dispatch.call_count,
		}
		self.assertEqual(
			observed,
			{
				"response": {"ok": True, "status": "Approved", "resumed": False, "continuation": "failed"},
				"resume_state": "failed",
				"messages": 0,
				"send_calls": 1,
				"dispatch_calls": 0,
			},
		)

	def test_failure_after_seed_insert_never_commits_an_orphan_user_message(self):
		"""N2/C5: the request-end commit must not persist a seed the send never committed;
		the decision and its trace comment stay durable. Chat and File Box alike."""
		for source in ("Chat", "File Box"):
			with self.subTest(source=source):
				name = self.chat if source == "Chat" else self.filebox
				conv = self.conversation_of(name)
				todo = self.owned_todo()
				frappe.db.set_value(AR, name, {"ref_doctype": "ToDo", "ref_name": todo})
				frappe.db.commit()
				with (
					as_user(self.owner),
					real_send_without_worker() as (dispatch, send),
					failing_seed_insert(conv) as raised,
					patch("frappe.publish_realtime", side_effect=Realtime()),
				):
					response = approvals_api.decide(name, "Yes")
					frappe.db.commit()  # request end commits whatever the handler left open
				attachments = (send.call_args.kwargs.get("attachments") or "") if send.call_args else ""
				observed = {
					"injected": len(raised),
					"user_messages": len(self.user_messages(conv)),
					"dispatch_calls": dispatch.call_count,
					"status": frappe.db.get_value(AR, name, "status"),
					"trace_comment": bool(
						frappe.db.exists(
							"Comment",
							{
								"reference_doctype": "ToDo",
								"reference_name": todo,
								"content": ["like", f"%{name}%"],
							},
						)
					),
					"resumed": response.get("resumed"),
					"continuation": response.get("continuation"),
					"resume_state": frappe.get_doc(AR, name).get("resume_state") or None,
					"file_reattached": frappe.db.get_value("File", self.files[name], "file_name")
					in attachments,
				}
				chat = source == "Chat"
				if chat:  # non-Chat logging stays as today apart from the rollback (C5)
					observed["private_values_in_error_logs"] = self.private_values_in_error_logs()
				self.assertEqual(
					observed,
					{
						"injected": 1,
						"user_messages": 0,
						"dispatch_calls": 0,
						"status": "Approved",
						"trace_comment": True,
						"resumed": False,
						"continuation": "failed" if chat else None,
						"resume_state": "failed" if chat else None,
						"file_reattached": not chat,
						**({"private_values_in_error_logs": []} if chat else {}),
					},
				)

	def test_one_failed_settle_notification_neither_fails_the_send_nor_skips_others(self):
		"""Sowmya #7b / N16: after-commit delivery is per recipient and best effort."""
		self.tag()
		self.tag(self.manager)
		conv = self.conversation_of(self.chat)
		realtime = Realtime(fail=lambda user, m: user == self.reviewer and m.get("kind") == "action:settled")
		raised = None
		with (
			as_user(self.reviewer),
			real_send_without_worker() as (dispatch, _send),
			patch("frappe.publish_realtime", side_effect=realtime),
		):
			try:
				response = approvals_api.decide(self.chat, "Yes")
				frappe.db.commit()  # request end
			except Exception as exc:
				raised, response = type(exc).__name__, {}
				frappe.db.rollback()
		logs = [log for log in self.new_error_logs() if log.method == "jarvis.approval.notify_failed"]
		observed = {
			"raised": raised,
			"resumed": response.get("resumed"),
			"messages": len(self.user_messages(conv)),
			"dispatch_calls": dispatch.call_count,
			"settled_delivered_to": realtime.recipients("action:settled", self.chat),
			"failed_deliveries": len(realtime.failed),
			"notify_failed_logs": len(logs),
			"private_values_in_error_logs": self.private_values_in_error_logs(),
		}
		self.assertEqual(
			observed,
			{
				"raised": None,
				"resumed": True,
				"messages": 1,
				"dispatch_calls": 1,
				"settled_delivered_to": {self.owner, self.manager},
				"failed_deliveries": 1,
				"notify_failed_logs": 1,
				"private_values_in_error_logs": [],
			},
		)

	def test_tag_notification_failure_keeps_the_grant_and_notifies_the_requester(self):
		realtime = Realtime(fail=lambda user, m: user == self.reviewer and m.get("kind") == "approval:tagged")
		raised = None
		with patch("frappe.publish_realtime", side_effect=realtime):
			try:
				self.tag()
			except Exception as exc:
				raised = type(exc).__name__
				frappe.db.rollback()
		observed = {
			"raised": raised,
			"granted": bool(
				frappe.db.exists(reviewers_fixture.GRANT, {"approval": self.chat, "user": self.reviewer})
			),
			"requester_notified": bool(realtime.payloads(self.owner, "approval:tagged")),
			"private_values_in_error_logs": self.private_values_in_error_logs(),
		}
		self.assertEqual(
			observed,
			{
				"raised": None,
				"granted": True,
				"requester_notified": True,
				"private_values_in_error_logs": [],
			},
		)

	def test_answer_in_chat_owner_publish_failure_still_notifies_reviewers(self):
		"""G8: resolve_on_user_message's owner publish must not skip reviewer delivery."""
		self.tag()
		conv = self.conversation_of(self.chat)
		realtime = Realtime(fail=lambda user, m: user == self.owner and m.get("kind") == "action:settled")
		raised = None
		with as_user(self.owner), patch("frappe.publish_realtime", side_effect=realtime):
			try:
				chat_asks.resolve_on_user_message(conv)
				frappe.db.commit()
			except Exception as exc:
				raised = type(exc).__name__
				frappe.db.rollback()
		observed = {
			"raised": raised,
			"status": frappe.db.get_value(AR, self.chat, "status"),
			"reviewer_events": realtime.payloads(self.reviewer, "action:settled"),
			"private_values_in_error_logs": self.private_values_in_error_logs(),
		}
		self.assertEqual(
			observed,
			{
				"raised": None,
				"status": "Answered",
				"reviewer_events": [{"kind": "action:settled", "name": self.chat, "status": "Answered"}],
				"private_values_in_error_logs": [],
			},
		)

	def test_requester_retry_sends_the_original_decision_once_as_requester(self):
		"""N4/N5/N6 (new endpoint): only the requester sends the stored decision, once."""
		self.tag()
		conv = self.conversation_of(self.chat)
		self.set_enabled(self.owner, 0)
		decision = self.secret + " decision"
		with as_user(self.reviewer), real_send_without_worker(), patch("frappe.publish_realtime"):
			approvals_api.decide(self.chat, decision)
			frappe.db.commit()
		self.set_enabled(self.owner, 1)
		ensure_user(self.manager, ("Jarvis User", "System Manager"))
		for actor in (self.reviewer, self.manager, self.ordinary):
			with self.subTest(actor=actor), as_user(actor), real_send_without_worker() as (_d, send):
				with self.assertRaises(frappe.PermissionError):
					approvals_api.continue_approval_chat(self.chat)
				send.assert_not_called()
		with as_user(self.owner), real_send_without_worker() as (dispatch, _send):
			first = approvals_api.continue_approval_chat(self.chat)
			frappe.db.commit()
			again = approvals_api.continue_approval_chat(self.chat)
			frappe.db.commit()
		rows = self.user_messages(conv)
		reviewer_name = frappe.db.get_value("User", self.reviewer, "full_name")
		stored = frappe.get_doc(AR, self.chat)
		self.assertEqual(
			{
				"first": (first.get("ok"), first.get("continuation")),
				"again": (again.get("ok"), again.get("continuation"), again.get("already")),
				"messages": [(r.owner, r.origin) for r in rows],
				"first_line": rows[0].content.splitlines()[0] if rows else "",
				"dispatch_calls": dispatch.call_count,
				"resume_state": stored.get("resume_state"),
				"decision": (stored.status, stored.decision, stored.decided_by),
			},
			{
				"first": (True, "done"),
				"again": (True, "done", 1),
				"messages": [(self.owner, "board_answer")],
				"first_line": f"Decided by {reviewer_name}.",
				"dispatch_calls": 1,
				"resume_state": "done",
				"decision": ("Approved", decision, self.reviewer),
			},
		)

	def retry(self, actor=None):
		with as_user(actor or self.owner), real_send_without_worker() as (dispatch, send):
			result = approvals_api.continue_approval_chat(self.chat)
			frappe.db.commit()
		return result, dispatch, send

	def view(self, actor=None, name=None):
		with as_user(actor or self.owner):
			row = approvals_api.get_approval(name or self.chat)
		return {k: row.get(k) for k in ("continuation", "can_continue")}

	def test_retry_refusals_are_uniform_and_send_nothing(self):
		"""N4 / D6: identity and scope first, then state; no field values leak."""
		conv = self.failed_by_reviewer()
		self.tag(self.manager)
		untagged = self.extra_user("sm2", ("Jarvis User", "System Manager"))
		before = len(self.user_messages(conv))
		for actor in (self.reviewer, self.manager, untagged, self.ordinary, "Administrator"):
			with self.subTest(actor=actor), as_user(actor), real_send_without_worker() as (_d, send):
				with self.assertRaises(frappe.PermissionError) as refused:
					approvals_api.continue_approval_chat(self.chat)
				self.assertEqual(str(refused.exception), "Not permitted")
				send.assert_not_called()
		with as_user("Guest"), self.assertRaises(frappe.PermissionError):
			approvals_api.continue_approval_chat(self.chat)
		frappe.local.jarvis_dispatch_depth = 1  # inside a running tool
		try:
			with as_user(self.owner), self.assertRaises(frappe.PermissionError):
				approvals_api.continue_approval_chat(self.chat)
		finally:
			frappe.local.jarvis_dispatch_depth = 0
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[approvals_api.continue_approval_chat], ["POST"]
		)
		# Not a scoped Chat question: File Box, or a non-Chat (legacy NULL) source.
		frappe.db.set_value(
			AR, self.filebox, {"status": "Approved", "decision": "Yes", "decided_by": self.owner}
		)
		legacy = self.fx.question("Chat")
		frappe.db.set_value(
			AR, legacy, {"source": None, "status": "Approved", "decision": "Yes", "decided_by": self.owner}
		)
		frappe.db.commit()
		for name in (self.filebox, legacy):
			with self.subTest(name=name), as_user(self.owner), self.assertRaises(frappe.PermissionError):
				approvals_api.continue_approval_chat(name)
		# States that never offer a send.
		cases = {
			"Pending": {"status": "Pending", "decision": None, "decided_by": None},
			"Dismissed": {"status": "Dismissed", "decision": "x", "decided_by": self.owner},
			"Answered": {"status": "Answered", "decision": "x", "decided_by": self.owner},
			"untracked": {
				"status": "Approved",
				"decision": "x",
				"decided_by": self.owner,
				"resume_state": None,
			},
		}
		for label, values in cases.items():
			name = self.fx.question("Chat")
			frappe.db.set_value(AR, name, values)
			frappe.db.commit()
			with self.subTest(state=label), as_user(self.owner), real_send_without_worker() as (_d, send):
				with self.assertRaises(frappe.ValidationError):
					approvals_api.continue_approval_chat(name)
				send.assert_not_called()
		# A collaborative conversation is refused by the existing legacy-only guard.
		frappe.db.set_value(CONV, conv, "collaboration_active", 1)
		frappe.db.commit()
		with as_user(self.owner), self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			approvals_api.continue_approval_chat(self.chat)
		frappe.db.set_value(CONV, conv, "collaboration_active", 0)
		frappe.db.commit()
		self.assertEqual(len(self.user_messages(conv)), before)
		self.assertEqual(frappe.get_doc(AR, self.chat).get("resume_state"), "failed")

	def test_process_death_reads_pending_then_failed_and_one_retry_sends(self):
		"""N9: the decision committed, the continuation never ran."""
		from jarvis.chat import approval_reviewers

		self.tag()
		conv = self.conversation_of(self.chat)
		with as_user(self.reviewer):
			approval_reviewers.decide_pending(self.chat, "Yes", True)
		self.assertEqual(frappe.get_doc(AR, self.chat).get("resume_state"), "due")
		fresh = {"requester": self.view(), "reviewer": self.view(self.reviewer)}
		frappe.db.sql(
			"UPDATE `tabJarvis Approval Request` SET decided_at = %s WHERE name = %s",
			(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-181), self.chat),
		)
		frappe.db.commit()
		with as_user(self.owner):
			rows = approvals_api.list_approvals_page(filters={"status": "Decided"}, search="QA756")["rows"]
		stale = {
			"requester": self.view(),
			"reviewer": self.view(self.reviewer),
			"list": [(r["continuation"], r["can_continue"]) for r in rows if r["name"] == self.chat],
		}
		first, dispatch, _send = self.retry()
		again, _d, again_send = self.retry()
		self.assertEqual(
			{
				"fresh": fresh,
				"stale": stale,
				"first": first,
				"again": again,
				"again_sends": again_send.call_count,
				"messages": [r.origin for r in self.user_messages(conv)],
				"dispatch": dispatch.call_count,
			},
			{
				"fresh": {
					"requester": {"continuation": "pending", "can_continue": 0},
					"reviewer": {"continuation": "pending", "can_continue": 0},
				},
				"stale": {
					"requester": {"continuation": "failed", "can_continue": 1},
					"reviewer": {"continuation": "failed", "can_continue": 0},
					"list": [("failed", 1)],
				},
				"first": {"ok": True, "continuation": "done"},
				"again": {"ok": True, "continuation": "done", "already": 1},
				"again_sends": 0,
				"messages": ["board_answer"],
				"dispatch": 1,
			},
		)

	def test_turn_machine_queue_and_overload_never_bounce_the_claimed_seed(self):
		"""N10: the decision queues as a resume (exempt from overload) at the
		pre-check and under the shard lock; done means accepted, not replied."""
		from jarvis.chat import admission

		conv = self.conversation_of(self.chat)
		exempt = []
		real = admission.accept_or_queue

		def accept(**kwargs):
			exempt.append(bool(frappe.flags.get("jarvis_resume_exempt")))
			return real(**kwargs)

		with (
			as_user(self.owner),
			real_send_without_worker(machine=True),
			patch.object(admission, "accept_or_queue", side_effect=accept),
			patch.object(admission, "shard_overloaded", return_value=True),
			patch.object(admission, "_line_full", return_value=True),
			patch("frappe.publish_realtime"),
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
		self.assertEqual(
			{
				"continuation": response.get("continuation"),
				"messages": len(self.user_messages(conv)),
				"turns": frappe.db.count("Jarvis Chat Turn", {"conversation": conv}),
				"exempt_at_admission": exempt,
				"exempt_after": bool(frappe.flags.get("jarvis_resume_exempt")),
			},
			{
				"continuation": "done",
				"messages": 1,
				"turns": 1,
				"exempt_at_admission": [True],
				"exempt_after": False,
			},
		)

	def test_legacy_busy_fails_then_a_retry_after_idle_sends_once(self):
		"""N10 legacy branch: the real single-flight guard refuses before any insert."""
		conv = self.conversation_of(self.chat)
		with as_user(self.owner):
			busy = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": conv,
					"seq": 1,
					"role": "assistant",
					"content": "",
					"streaming": 1,
				}
			)
			busy.flags.jarvis_server_write = True
			busy.insert(ignore_permissions=True)
		frappe.db.commit()
		with as_user(self.owner), real_send_without_worker(), patch("frappe.publish_realtime"):
			refused = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
		state = frappe.get_doc(AR, self.chat).get("resume_state")
		frappe.db.set_value(MSG, busy.name, "streaming", 0)
		frappe.db.commit()
		sent, dispatch, _send = self.retry()
		self.assertEqual(
			{
				"refused": (refused.get("continuation"), refused.get("resumed")),
				"state_after_refusal": state,
				"sent": sent,
				"user_messages": [r.origin for r in self.user_messages(conv)],
				"dispatch": dispatch.call_count,
			},
			{
				"refused": ("failed", False),
				"state_after_refusal": "failed",
				"sent": {"ok": True, "continuation": "done"},
				"user_messages": ["board_answer"],
				"dispatch": 1,
			},
		)

	def test_a_later_ordinary_message_keeps_failed_and_the_retry_appends(self):
		"""N11: the requester's own later message is never touched or resent."""
		from jarvis.chat import api as chat_api

		conv = self.failed_by_reviewer()
		with as_user(self.owner), real_send_without_worker():
			chat_api.send_message(conversation=conv, message="QA756 unrelated follow-up")
			frappe.db.commit()
		later = self.user_messages(conv)
		state = frappe.get_doc(AR, self.chat).get("resume_state")
		sent, _d, _s = self.retry()
		rows = self.user_messages(conv)
		self.assertEqual(
			{
				"state_after_later_message": state,
				"sent": sent,
				"later_unchanged": (rows[0].name, rows[0].content) == (later[0].name, later[0].content),
				"origins": [r.origin for r in rows],
				"next_seq": rows[1].seq > rows[0].seq,
			},
			{
				"state_after_later_message": "failed",
				"sent": {"ok": True, "continuation": "done"},
				"later_unchanged": True,
				"origins": ["human", "board_answer"],
				"next_seq": True,
			},
		)

	def test_dispatch_failure_after_the_seed_commit_is_done_and_never_offered_again(self):
		"""N12 / N12b: the message was accepted; the existing orphan sweep owns it."""
		from jarvis.jarvis.doctype.jarvis_chat_message.jarvis_chat_message import JarvisChatMessage

		def late_callback(doc):
			if doc.conversation == conv:
				frappe.db.after_commit.add(lambda: (_ for _ in ()).throw(RuntimeError("QA756 late callback")))

		for label in ("dispatch raises", "after_commit raises"):
			with self.subTest(label):
				name = self.fx.question("Chat")
				conv = self.conversation_of(name)
				failure = (
					patch("jarvis.chat.api._dispatch_turn", side_effect=RuntimeError("QA756 dispatch down"))
					if label == "dispatch raises"
					else patch.object(JarvisChatMessage, "after_insert", late_callback, create=True)
				)
				realtime = Realtime()
				with (
					as_user(self.owner),
					real_send_without_worker(),
					failure,
					patch("frappe.publish_realtime", side_effect=realtime),
				):
					response = approvals_api.decide(name, "Yes")
					frappe.db.commit()
				logs = [
					log.error
					for log in self.new_error_logs()
					if log.method == "jarvis.approval.continuation_failed" and name in log.error
				]
				with as_user(self.owner):
					view = approvals_api.get_approval(name)
				self.assertEqual(
					{
						"continuation": response.get("continuation"),
						"messages": len(self.user_messages(conv)),
						"turns": frappe.db.count("Jarvis Chat Turn", {"conversation": conv}),
						"view": (view.get("continuation"), view.get("can_continue")),
						"failure_events": realtime.payloads(self.owner, FAILED_KIND),
						"logs": logs,
					},
					{
						"continuation": "done",
						"messages": 1,
						"turns": 0,
						"view": ("done", 0),
						"failure_events": [],
						"logs": [f"{name} RuntimeError"],
					},
				)

	def test_recovery_failure_reads_pending_and_never_errors(self):
		"""N12c: the send was refused and the recovery read itself failed."""
		from jarvis.chat import approval_reviewers

		with (
			as_user(self.owner),
			real_send_without_worker(),
			patch("jarvis.chat.api.validate_can_send", return_value=(False, "QA756 quota reached")),
			patch.object(
				approval_reviewers, "stored_resume_state", side_effect=RuntimeError("QA756 db gone")
			),
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
		self.assertEqual(
			(
				response.get("ok"),
				response.get("status"),
				response.get("continuation"),
				response.get("resumed"),
			),
			(True, "Approved", "pending", False),
		)
		self.assertEqual(frappe.get_doc(AR, self.chat).get("resume_state"), "due")
		self.assertEqual(self.private_values_in_error_logs(), [])

	def test_resume_state_cannot_be_planted_or_changed_through_the_orm(self):
		"""N14."""
		from frappe import client

		from jarvis.tools.create_doc import create_doc

		self.failed_by_reviewer()
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		conv = self.conversation_of(self.chat)

		def fresh(**extra):
			return {
				"doctype": AR,
				"title": "QA756 forged",
				"source": "Chat",
				"conversation": conv,
				"question": "Forged?",
				"status": "Pending",
				"resume_state": "done",
				**extra,
			}

		def privileged_insert():
			doc = frappe.get_doc(fresh())
			doc.insert(ignore_permissions=True)

		attempts = {
			"rest set done": (self.owner, lambda: client.set_value(AR, self.chat, "resume_state", "done")),
			"rest set due": (self.owner, lambda: client.set_value(AR, self.chat, "resume_state", "due")),
			"rest insert": (self.owner, lambda: client.insert(fresh())),
			"agent create_doc": (
				self.owner,
				lambda: create_doc(AR, {k: v for k, v in fresh().items() if k != "doctype"}),
			),
			"ignore_permissions insert": (self.owner, privileged_insert),
			"other manager write": (
				self.manager,
				lambda: client.set_value(AR, self.chat, "resume_state", "done"),
			),
		}
		outcomes = {}
		for label, (actor, attempt) in attempts.items():
			try:
				with as_user(actor):
					attempt()
				outcomes[label] = "saved"
			except Exception as exc:
				outcomes[label] = (
					"refused"
					if isinstance(exc, frappe.PermissionError) or "server-managed" in str(exc)
					else type(exc).__name__
				)
			outcomes[label] = (
				outcomes[label],
				frappe.get_doc(AR, self.chat).get("resume_state"),
				frappe.db.count(AR, {"title": "QA756 forged"}),
			)
			frappe.db.rollback()
		self.assertEqual(outcomes, {label: ("refused", "failed", 0) for label in attempts})

	def test_notification_lookup_and_user_cleanup_failures_are_isolated(self):
		"""N16: the settle recipients read raises; a cleanup publish raises."""
		from jarvis.chat import approval_reviewers

		self.tag()
		with (
			as_user(self.reviewer),
			real_send_without_worker(),
			patch.object(approval_reviewers, "reviewers", side_effect=RuntimeError("QA756 read down")),
			patch("frappe.publish_realtime", side_effect=Realtime()) as realtime,
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
		self.assertEqual((response.get("continuation"), response.get("resumed")), ("done", True))
		self.assertIn(self.owner, realtime.side_effect.recipients("action:settled", self.chat))
		other = self.fx.question("Chat")
		self.tag()
		with as_user(self.owner):
			approvals_api.tag_reviewer(other, self.reviewer)
		realtime = Realtime(
			fail=lambda user, m: user == self.reviewer and m.get("kind") == "approval:untagged"
		)
		with as_user("Administrator"), patch("frappe.publish_realtime", side_effect=realtime):
			user = frappe.get_doc("User", self.reviewer)
			user.set("roles", [r for r in user.roles if r.role != "Jarvis Admin"])
			user.save(ignore_permissions=True)
			frappe.db.commit()
		self.assertEqual(
			{
				"requester_told": sorted(
					m["name"] for m in realtime.payloads(self.owner, "approval:untagged")
				),
				"failed": len(realtime.failed),
				"private": self.private_values_in_error_logs(),
			},
			{"requester_told": sorted([self.chat, other]), "failed": 2, "private": []},
		)

	def test_not_migrated_site_keeps_todays_decide_and_offers_nothing(self):
		"""N17: code served before the migrate."""
		from jarvis.chat import approval_reviewers

		conv = self.conversation_of(self.chat)
		with (
			patch.object(approval_reviewers, "resume_tracked", return_value=False),
			as_user(self.owner),
			real_send_without_worker(),
			patch("frappe.publish_realtime"),
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
			view = approvals_api.get_approval(self.chat)
			with self.assertRaises(frappe.ValidationError) as refused:
				approvals_api.continue_approval_chat(self.chat)
		self.assertEqual(
			{
				"response": response,
				"messages": len(self.user_messages(conv)),
				"view_keys": {"continuation", "can_continue"} & set(view),
				"state": frappe.get_doc(AR, self.chat).get("resume_state") or None,
				"refusal": "migration" in str(refused.exception),
			},
			{
				"response": {"ok": True, "status": "Approved", "resumed": True, "continuation": ""},
				"messages": 1,
				"view_keys": set(),
				"state": None,
				"refusal": True,
			},
		)
		# Once migrated, that untracked (NULL) decided row is never offered a send.
		self.assertEqual(self.view(), {"continuation": "", "can_continue": 0})

	def test_deleted_chat_or_cleared_history_never_creates_or_resends(self):
		"""N18."""
		conv = self.failed_by_reviewer()
		conversations = frappe.db.count(CONV)
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.db.delete(CONV, {"name": conv})
		frappe.db.commit()
		gone_view = self.view()
		gone, _d, send = self.retry()
		gone_state = frappe.get_doc(AR, self.chat).get("resume_state")
		self.assertEqual(
			(gone_view, gone, send.call_count, gone_state, frappe.db.count(CONV)),
			(
				{"continuation": "failed", "can_continue": 0},
				{"ok": False, "continuation": "failed", "reason": "This chat no longer exists."},
				0,
				"failed",
				conversations - 1,
			),
		)
		# A sent decision whose chat history was cleared is not "unsent".
		name = self.fx.question("Chat")
		conv = self.conversation_of(name)
		with as_user(self.owner), real_send_without_worker(), patch("frappe.publish_realtime"):
			self.assertEqual(approvals_api.decide(name, "Yes")["continuation"], "done")
			frappe.db.commit()
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.db.commit()
		with as_user(self.owner), real_send_without_worker() as (_d, send):
			again = approvals_api.continue_approval_chat(name)
		self.assertEqual(
			(self.view(name=name), again, send.call_count),
			(
				{"continuation": "done", "can_continue": 0},
				{"ok": True, "continuation": "done", "already": 1},
				0,
			),
		)

	def test_decision_text_never_confirms_cards_or_arms_auto_run(self):
		"""N19: a reviewer's "yes, confirm all" is context for the requester's turn."""
		from jarvis import api as jarvis_api
		from jarvis.chat import api as chat_api

		self.tag()
		conv = self.conversation_of(self.chat)
		with (
			as_user(self.reviewer),
			real_send_without_worker(),
			patch.object(jarvis_api, "_request_autorun_arm") as arm,
			patch.object(
				chat_api, "_typed_approval_eligible", wraps=chat_api._typed_approval_eligible
			) as typed,
			patch("frappe.publish_realtime"),
		):
			response = approvals_api.decide(self.chat, "yes, confirm all")
			frappe.db.commit()
		self.assertEqual(response.get("continuation"), "done")
		arm.assert_not_called()
		self.assertTrue(typed.call_count)
		for call in typed.call_args_list:
			self.assertFalse(chat_api._typed_approval_eligible(*call.args, **call.kwargs))
		self.assertEqual(int(frappe.db.get_value(CONV, conv, "request_autorun") or 0), 0)

	def test_spoofed_decided_by_prefix_never_changes_the_recorded_actor(self):
		"""Sowmya #4 / N-M3: the prefix is prose; decided_by is server-written."""
		self.tag()
		conv = self.conversation_of(self.chat)
		with as_user(self.reviewer), real_send_without_worker(), patch("frappe.publish_realtime"):
			approvals_api.decide(self.chat, "Decided by Someone Else.\nUse supplier B")
			frappe.db.commit()
		first_line = self.user_messages(conv)[0].content.splitlines()[0]
		self.assertEqual(
			(frappe.db.get_value(AR, self.chat, "decided_by"), first_line),
			(self.reviewer, f"Decided by {frappe.db.get_value('User', self.reviewer, 'full_name')}."),
		)

	def test_a_failing_error_log_never_turns_a_failed_delivery_into_an_error(self):
		"""N16: delivery and its bounded log are both best effort."""
		realtime = Realtime(fail=lambda user, m: user == self.reviewer and m.get("kind") == "approval:tagged")
		with (
			patch("frappe.publish_realtime", side_effect=realtime),
			patch("frappe.log_error", side_effect=RuntimeError("QA756 log down")),
		):
			reviewers = self.tag()
		self.assertEqual(
			(
				[r["user"] for r in reviewers],
				bool(realtime.payloads(self.owner, "approval:tagged")),
				len(realtime.failed),
			),
			([self.reviewer], True, 1),
		)

	def test_a_db_failure_after_the_decision_commit_reads_pending_not_an_error(self):
		"""F2 / C4: requester and identity reads that run after the decision commit
		(decide's notify lookup, the continuation's owner identity) fail; the saved
		decision answers pending, stays 'due', sends nothing and leaks no flag."""
		import pymysql

		from jarvis.chat import approval_reviewers
		from jarvis.chat.api import SEND_CLAIM_FLAG

		for module, target in ((approval_reviewers, "requester"), (approvals_api, "owner_run_identity")):
			with self.subTest(target=target):
				self.chat = self.fx.question("Chat")
				self.tag()
				conv = self.conversation_of(self.chat)
				real = getattr(module, target)
				name = self.chat

				def failing(*args, _real=real, _name=name, **kwargs):
					if frappe.db.get_value(AR, _name, "status") != "Pending":  # only once decided
						raise pymysql.err.OperationalError(2013, "QA756 lost connection")
					return _real(*args, **kwargs)

				realtime = Realtime()
				with (
					patch.object(module, target, side_effect=failing),
					real_send_without_worker() as (dispatch, send),
					patch("frappe.publish_realtime", side_effect=realtime),
				):
					with as_user(self.reviewer):
						try:
							response = approvals_api.decide(self.chat, "Yes")
							frappe.db.commit()
						except Exception as exc:
							frappe.db.rollback()
							response = {"raised": type(exc).__name__}
				self.assertEqual(
					{
						"response": response,
						"state": frappe.get_doc(AR, self.chat).get("resume_state"),
						"status": frappe.db.get_value(AR, self.chat, "status"),
						"messages": len(self.user_messages(conv)),
						"sends": send.call_count,
						"failure_events": realtime.payloads(self.owner, FAILED_KIND),
						"flags": (
							bool(frappe.flags.get("jarvis_resume_exempt")),
							frappe.flags.get(SEND_CLAIM_FLAG),
						),
						"private": self.private_values_in_error_logs(),
					},
					{
						"response": {
							"ok": True,
							"status": "Approved",
							"resumed": False,
							"continuation": "pending",
						},
						"state": "due",
						"status": "Approved",
						"messages": 0,
						"sends": 0,
						"failure_events": [],
						"flags": (False, None),
						"private": [],
					},
				)

	def test_the_requesters_send_reads_pending_when_identity_is_unavailable(self):
		"""F2 on the retry endpoint: its own authorization runs first (unchanged); the
		continuation's identity read failing answers pending with no reason."""
		import pymysql

		self.failed_by_reviewer()
		with (
			patch.object(
				approvals_api,
				"owner_run_identity",
				side_effect=pymysql.err.OperationalError(2013, "QA756 lost connection"),
			),
			as_user(self.owner),
			real_send_without_worker() as (_dispatch, send),
		):
			result = approvals_api.continue_approval_chat(self.chat)
		self.assertEqual(
			(result, send.call_count, frappe.get_doc(AR, self.chat).get("resume_state")),
			({"ok": False, "continuation": "pending"}, 0, "failed"),
		)

	def test_an_accepted_send_without_a_verified_claim_never_reads_done(self):
		"""R4 guard: send_message said ok but no claim ran and the late claim did not win
		either: the outcome is unknown (pending), never a 'done' nobody verified."""
		from jarvis.chat import approval_reviewers

		self.tag()
		with (
			as_user(self.reviewer),
			patch("jarvis.chat.api.send_message", return_value={"ok": True}),
			patch.object(approval_reviewers, "claim_continuation", return_value=False),
			patch("frappe.publish_realtime"),
		):
			response = approvals_api.decide(self.chat, "Yes")
			frappe.db.commit()
		self.assertEqual(
			(
				response.get("continuation"),
				response.get("resumed"),
				frappe.get_doc(AR, self.chat).get("resume_state"),
			),
			("pending", False, "due"),
		)
