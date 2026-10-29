"""The audit-artifact classifier parses JSON; formatting never decides access.

Raw-SQL fixtures cover what no ordinary producer writes (compact, reordered,
escaped or malformed JSON) next to valid unrelated, File Box, legacy and other
source controls. Every case is read through list, count, reportview, REST get
and has_permission, for an untagged System Manager, a System Manager requester
and Administrator; all three must agree with the expected classification.
"""

import json
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tests import test_chat_approval_audit_privacy as audit_fixture
from jarvis.tests import test_chat_approval_continuation as continuation_fixture
from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = continuation_fixture.AR
CONV = continuation_fixture.CONV
OPEN, REQUESTER, ROOT = "open", "requester", "root"  # who besides Administrator may read


class TestChatApprovalAuditJson(
	audit_fixture.AuditReach, continuation_fixture.ComposedChatReviewFixture, FrappeTestCase
):
	maxDiff = None

	def setUp(self):
		super().setUp()
		self.raw = {"Deleted Document": [], "Version": [], "File": []}
		self.addCleanup(self.drop_raw_rows)
		ensure_user(self.owner, ("Jarvis User", "System Manager"))
		self.conv = self.conversation_of(self.chat)
		self.fbconv = self.conversation_of(self.filebox)

	def drop_raw_rows(self):
		frappe.db.rollback()
		for doctype, names in self.raw.items():
			if names:
				frappe.db.delete(doctype, {"name": ["in", names]})
		frappe.db.commit()

	def deleted(self, doctype, data, deleted_name=None):
		doc = frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": doctype,
				"deleted_name": deleted_name or "qa756-gone-" + uuid4().hex[:10],
				"data": data if isinstance(data, str) else json.dumps(data, indent=1),
			}
		)
		doc.name = "qa756-dd-" + uuid4().hex[:12]
		doc.db_insert()
		self.raw["Deleted Document"].append(doc.name)
		return doc.name

	def version(self, ref_doctype, docname):
		doc = frappe.get_doc(
			{"doctype": "Version", "ref_doctype": ref_doctype, "docname": docname, "data": "{}"}
		)
		doc.name = "qa756-v-" + uuid4().hex[:12]
		doc.db_insert()
		self.raw["Version"].append(doc.name)
		return doc.name

	def classify(self, cases):
		"""{label: (doctype, name, who)} -> assert every surface, every actor."""
		frappe.db.commit()
		seen, expected = {}, {}
		for label, (doctype, name, who) in cases.items():
			for actor, allowed in (
				("manager", who == OPEN),
				("requester", who in (OPEN, REQUESTER)),
				("Administrator", True),
			):
				user = {"manager": self.manager, "requester": self.owner}.get(actor, actor)
				with as_user(user):
					seen[f"{label} | {actor}"] = self.generic_reach(doctype, name)
				expected[f"{label} | {actor}"] = self.visible() if allowed else self.hidden()
		self.assertEqual(seen, expected)

	def comment_link(self, reference_name, doctype=AR, **extra):
		return {"reference_doctype": doctype, "reference_name": reference_name, "content": "private", **extra}

	def test_comment_and_file_archives_are_classified_by_parsed_json(self):
		"""json-predicate §3 a-p."""
		todo = self.owned_todo()
		legacy = self.fx.question("Chat")
		frappe.db.set_value(AR, legacy, "source", None)
		q = self.chat
		dd = "Deleted Document"
		cases = {
			"a native pretty": (dd, self.deleted("Comment", self.comment_link(q)), REQUESTER),
			"b compact": (
				dd,
				self.deleted(
					"Comment",
					f'{{"reference_doctype":"Jarvis Approval Request","reference_name":"{q}","content":"private"}}',
				),
				REQUESTER,
			),
			"c reordered spaced": (
				dd,
				self.deleted(
					"Comment",
					f'{{ "content" :"private" ,\n\t"reference_name"  :  "{q}","reference_doctype"\n: "Jarvis Approval Request" }}',
				),
				REQUESTER,
			),
			"d unicode escaped": (
				dd,
				self.deleted(
					"Comment",
					f'{{"reference_doctype":"\\u004aarvis Approval Request","reference_name":"{q}"}}',
				),
				REQUESTER,
			),
			"e case": (
				dd,
				self.deleted("Comment", self.comment_link(q, "jarvis approval request")),
				REQUESTER,
			),
			"e trailing spaces": (
				dd,
				self.deleted("Comment", self.comment_link(q, "Jarvis Approval Request  ")),
				REQUESTER,
			),
			"f invalid json": (dd, self.deleted("Comment", '{"reference_doctype": "ToDo", '), ROOT),
			"g scalar": (dd, self.deleted("Comment", '"Jarvis Approval Request"'), ROOT),
			"h array": (dd, self.deleted("Comment", f'["Jarvis Approval Request", "{q}"]'), ROOT),
			"i doctype object": (
				dd,
				self.deleted("Comment", {"reference_doctype": {"x": AR}, "reference_name": q}),
				ROOT,
			),
			"i doctype array": (
				dd,
				self.deleted("Comment", {"reference_doctype": [AR], "reference_name": q}),
				ROOT,
			),
			"i doctype number": (
				dd,
				self.deleted("Comment", {"reference_doctype": 7, "reference_name": q}),
				ROOT,
			),
			"i doctype boolean": (
				dd,
				self.deleted("Comment", {"reference_doctype": True, "reference_name": q}),
				ROOT,
			),
			"j name missing": (dd, self.deleted("Comment", {"reference_doctype": AR}), ROOT),
			"j name null": (dd, self.deleted("Comment", self.comment_link(None)), ROOT),
			"j name number": (dd, self.deleted("Comment", self.comment_link(5)), ROOT),
			"j name empty": (dd, self.deleted("Comment", self.comment_link("")), ROOT),
			"k unknown approval": (dd, self.deleted("Comment", self.comment_link("qa756-nowhere")), ROOT),
			"l ToDo": (dd, self.deleted("Comment", self.comment_link(todo, "ToDo")), OPEN),
			"m no doctype key": (dd, self.deleted("Comment", {"content": "QA756 note"}), OPEN),
			"n doctype null": (
				dd,
				self.deleted("Comment", {"reference_doctype": None, "content": "x"}),
				OPEN,
			),
			"o File Box question": (dd, self.deleted("Comment", self.comment_link(self.filebox)), OPEN),
			"p legacy NULL-source question": (dd, self.deleted("Comment", self.comment_link(legacy)), OPEN),
			"File compact": (
				dd,
				self.deleted(
					"File", f'{{"attached_to_doctype":"Jarvis Approval Request","attached_to_name":"{q}"}}'
				),
				REQUESTER,
			),
			"File unrelated": (
				dd,
				self.deleted("File", {"attached_to_doctype": "ToDo", "attached_to_name": todo}),
				OPEN,
			),
			"File unattached": (dd, self.deleted("File", {"file_name": "qa756.txt"}), OPEN),
			"File invalid json": (dd, self.deleted("File", "not json"), ROOT),
			"other doctype invalid json": (dd, self.deleted("ToDo", "not json"), OPEN),
		}
		self.classify(cases)

	def test_approval_archives_follow_the_live_rules_and_fail_closed(self):
		"""D2: archive source/conversation/owner; current File Box flag and owner."""
		dd = "Deleted Document"

		def archived(**data):
			return self.deleted(AR, {"name": "x", "owner": self.owner, **data})

		cases = {
			"chat, private conversation": (dd, archived(source="Chat", conversation=self.conv), REQUESTER),
			"chat, conversation gone": (dd, archived(source="Chat", conversation="qa756-gone"), REQUESTER),
			"chat, no conversation": (dd, archived(source="Chat"), REQUESTER),
			"chat, conversation gone, other owner": (
				dd,
				archived(source="Chat", conversation="qa756-gone", owner=self.manager),
				"manager-only",
			),
			"chat in a File Box conversation": (dd, archived(source="Chat", conversation=self.fbconv), OPEN),
			"File Box naming a private conversation": (
				dd,
				archived(source="File Box", conversation=self.conv),
				OPEN,
			),
			"legacy, source missing": (dd, archived(conversation=self.conv), OPEN),
			"legacy, source null": (dd, archived(source=None, conversation=self.conv), OPEN),
			"Agent Review": (dd, archived(source="Agent Review", conversation=self.conv), OPEN),
			"wiki proposal": (dd, archived(source="File Box Wiki", conversation=self.conv), OPEN),
			"chat, owner and conversation missing": (dd, self.deleted(AR, {"source": "Chat"}), ROOT),
			"source object": (dd, archived(source={"v": "File Box"}, conversation=self.conv), ROOT),
			"invalid json": (dd, self.deleted(AR, "{"), ROOT),
			"scalar": (dd, self.deleted(AR, "42"), ROOT),
			"array": (dd, self.deleted(AR, "[]"), ROOT),
			# a live row always decides for its name
			"live File Box row wins": (
				dd,
				self.deleted(
					AR, {"source": "Chat", "conversation": self.conv, "owner": self.owner}, self.filebox
				),
				OPEN,
			),
			"live scoped row wins": (
				dd,
				self.deleted(AR, {"source": "File Box", "conversation": self.fbconv}, self.chat),
				REQUESTER,
			),
		}
		manager_only = {k for k, v in cases.items() if v[2] == "manager-only"}
		self.classify({k: v for k, v in cases.items() if k not in manager_only})
		# An archived question whose requester is someone else: hidden from this
		# requester; the archived owner (here the manager) is its requester.
		for label in manager_only:
			with as_user(self.owner):
				self.assertEqual(self.generic_reach(dd, cases[label][1]), self.hidden(), label)
			with as_user(self.manager):
				self.assertEqual(self.generic_reach(dd, cases[label][1]), self.visible(), label)

	def test_versions_follow_their_approval_comment_or_archive(self):
		"""Version: live parent, else the parent's archive; unknown parent root-only."""
		todo = self.owned_todo()
		with as_user(self.owner):
			todo_comment = frappe.get_doc(
				{
					"doctype": "Comment",
					"comment_type": "Comment",
					"reference_doctype": "ToDo",
					"reference_name": todo,
					"content": "QA756 note",
				}
			).insert(ignore_permissions=True)
		self.comments.append(todo_comment.name)
		gone = "qa756-gone-" + uuid4().hex[:8]
		self.deleted(
			AR, {"name": gone, "source": "Chat", "conversation": self.conv, "owner": self.owner}, gone
		)
		archived_comment = "qa756-c-" + uuid4().hex[:8]
		self.deleted("Comment", self.comment_link(self.chat), archived_comment)
		archived_todo_comment = "qa756-c-" + uuid4().hex[:8]
		self.deleted("Comment", self.comment_link(todo, "ToDo"), archived_todo_comment)
		v = "Version"
		self.classify(
			{
				"live scoped question": (v, self.version(AR, self.chat), REQUESTER),
				"live File Box question": (v, self.version(AR, self.filebox), OPEN),
				"archived scoped question": (v, self.version(AR, gone), REQUESTER),
				"unknown question": (v, self.version(AR, "qa756-nowhere"), ROOT),
				"comment on ToDo": (v, self.version("Comment", todo_comment.name), OPEN),
				"archived comment on the question": (v, self.version("Comment", archived_comment), REQUESTER),
				"archived comment on ToDo": (v, self.version("Comment", archived_todo_comment), OPEN),
				"orphan comment, no archive": (v, self.version("Comment", "qa756-nowhere"), ROOT),
				"unrelated doctype": (v, self.version("ToDo", todo), OPEN),
			}
		)

	def test_untagged_manager_cannot_restore_a_private_question_archive(self):
		from frappe.core.doctype.deleted_document.deleted_document import restore

		with as_user(self.owner):
			frappe.client.delete(AR, self.chat)
			frappe.db.commit()
		name = frappe.db.get_value("Deleted Document", {"deleted_doctype": AR, "deleted_name": self.chat})
		self.assertTrue(name)
		with as_user(self.manager), self.assertRaises(frappe.PermissionError):
			restore(name)
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists(AR, self.chat))
		self.assertFalse(frappe.db.get_value("Deleted Document", name, "restored"))

	def attached(self, doctype, name, raw=False):
		"""A File row attached to ``doctype``/``name`` (raw for shapes the controller refuses)."""
		if raw:
			file = "qa756-f-" + uuid4().hex[:10]
			frappe.db.sql(
				"INSERT INTO `tabFile` (name, file_name, attached_to_doctype, attached_to_name, is_private,"
				" creation, modified, owner, modified_by) VALUES (%s, %s, %s, %s, 1, NOW(), NOW(), %s, %s)",
				(file, file + ".txt", doctype, name, self.owner, self.owner),
			)
			self.raw["File"].append(file)
			return file
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "qa756-" + uuid4().hex[:8] + ".txt",
				"content": "QA756 attachment",
				"is_private": 1,
				"attached_to_doctype": doctype,
				"attached_to_name": name,
			}
		).insert(ignore_permissions=True)
		self.extra_files.append(doc.name)
		return doc.name

	def test_file_versions_follow_the_attached_approval_or_its_archive(self):
		"""F3 / V1 controls: only approval-attached files change; unknown parent root-only."""
		todo = self.owned_todo()
		legacy = self.fx.question("Chat")
		frappe.db.set_value(AR, legacy, "source", None)
		gone_file = "qa756-gonef-" + uuid4().hex[:8]
		self.deleted("File", {"attached_to_doctype": AR, "attached_to_name": self.chat}, gone_file)
		gone_todo_file = "qa756-gonef-" + uuid4().hex[:8]
		self.deleted("File", {"attached_to_doctype": "ToDo", "attached_to_name": todo}, gone_todo_file)
		v = "Version"
		self.classify(
			{
				"scoped question attachment": (
					v,
					self.version("File", self.attached(AR, self.chat)),
					REQUESTER,
				),
				"File Box question attachment": (
					v,
					self.version("File", self.attached(AR, self.filebox)),
					OPEN,
				),
				"legacy NULL-source attachment": (v, self.version("File", self.attached(AR, legacy)), OPEN),
				"ToDo attachment": (v, self.version("File", self.attached("ToDo", todo)), OPEN),
				"private conversation attachment (unchanged policy)": (
					v,
					self.version("File", self.files[self.chat]),
					OPEN,
				),
				"approval attachment without a name": (
					v,
					self.version("File", self.attached(AR, "", raw=True)),
					ROOT,
				),
				"archived scoped attachment": (v, self.version("File", gone_file), REQUESTER),
				"archived ToDo attachment": (v, self.version("File", gone_todo_file), OPEN),
				"orphan File Version": (v, self.version("File", "qa756-nowhere"), ROOT),
			}
		)

	def test_an_unclassifiable_approval_archive_hides_its_derived_artifacts_too(self):
		"""SQL UNKNOWN in an all-archives-must-pass check is a failing archive: a Chat
		archive with neither owner nor conversation is root-only, and so are the
		Versions and comment archives that point at it."""
		gone = "qa756-gone-" + uuid4().hex[:8]
		self.deleted(AR, {"name": gone, "source": "Chat"}, gone)
		self.classify(
			{
				"its own archive": (
					"Deleted Document",
					frappe.db.get_value("Deleted Document", {"deleted_name": gone}),
					ROOT,
				),
				"its Version": ("Version", self.version(AR, gone), ROOT),
				"its comment archive": (
					"Deleted Document",
					self.deleted("Comment", self.comment_link(gone)),
					ROOT,
				),
			}
		)

	def test_a_loaded_audit_row_that_is_gone_is_not_readable(self):
		"""R1 hardening: a NAMED row missing at check time denies read; creating,
		unnamed documents and Administrator are unchanged."""
		from jarvis.chat import approval_reviewers

		name = self.version(AR, self.chat)
		frappe.db.commit()
		with as_user(self.manager):
			loaded = frappe.get_doc("Version", name)
		frappe.db.delete("Version", {"name": name})
		frappe.db.commit()
		self.assertEqual(
			{
				"gone, read": approval_reviewers.has_version_permission(loaded, "read", self.manager),
				"gone, create": approval_reviewers.has_version_permission(loaded, "create", self.manager),
				"unnamed": approval_reviewers.has_version_permission(
					frappe.new_doc("Version"), "read", self.manager
				),
				"Administrator": approval_reviewers.has_version_permission(loaded, "read", "Administrator"),
				"archive gone, read": approval_reviewers.has_deleted_document_permission(
					"qa756-dd-missing", "read", self.manager
				),
			},
			{
				"gone, read": False,
				"gone, create": True,
				"unnamed": True,
				"Administrator": True,
				"archive gone, read": False,
			},
		)
