"""Security review PART 3 — exploit reproductions + fix proofs.

Covers the File-list IDOR (TASK 22), the macro role/ORM tightening + MAC-2
create-inject + scheduler skip (TASK 23/24), the approval decided-field forgery
(TASK 25), comment-tag grant-to-non-Jarvis + mention-notify leak (TASK 27/28),
the agent role/ORM tightening (TASK 29), the reviewer-gated agent apply (TASK
30), the finding audit-field freeze (TASK 31), the run-existence oracle (TASK
32) and the skill_bundle IP permlevel (TASK 33).

Every exploit is exercised through the SAME door a real attacker would use — a
perm-checked ``doc.insert()`` / ``doc.save()`` (the generic-REST path) or the
whitelisted endpoint — NOT ``ignore_permissions`` (which would mask the gate).
Fixtures the engine/server would create are minted with ``ignore_permissions``
and owner-reassigned, mirroring the real writers.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.chat import agents_api, approvals_api, docmeta_api, macro_scheduler

CONVERSATION = "Jarvis Conversation"
MACRO = "Jarvis Macro"
MACRO_RUN = "Jarvis Macro Run"
APPROVAL = "Jarvis Approval Request"
LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"

USER_A = "p3-usera@example.com"
USER_B = "p3-userb@example.com"
REVIEWER = "p3-reviewer@example.com"
OUTSIDER = "p3-outsider@example.com"  # System User, NO Jarvis role
AGENT_SLUG = "p3-test-agent"
PFX = "p3"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _ensure_user(email: str, roles: list[str]) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User")
	# Force the exact role set (drop System Manager so the role gates are real).
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
	have = set(frappe.get_roles(email))
	add = [r for r in roles if r not in have]
	if add:
		frappe.get_doc("User", email).add_roles(*add)
	# Drop any stray Jarvis roles OUTSIDER must not hold.
	strip = [
		r
		for r in ("Jarvis User", "Jarvis Skill Reviewer", "Jarvis Admin")
		if r not in roles and r in set(frappe.get_roles(email))
	]
	if strip:
		frappe.get_doc("User", email).remove_roles(*strip)
	return email


def _reassign(dt: str, name: str, owner: str) -> None:
	frappe.db.set_value(dt, name, "owner", owner, update_modified=False)


def _mk_conv(owner: str, title: str = "p3 conv") -> str:
	doc = frappe.get_doc({"doctype": CONVERSATION, "title": title})
	doc.insert(ignore_permissions=True)
	_reassign(CONVERSATION, doc.name, owner)
	return doc.name


def _mk_file(owner: str, tag: str, attached=None, is_private: int = 1):
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{tag}.txt",
			"content": f"content-{tag}",
			"is_private": is_private,
			"attached_to_doctype": attached[0] if attached else None,
			"attached_to_name": attached[1] if attached else None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_reassign("File", doc.name, owner)
	return doc


def _mk_macro(owner: str, name: str, **extra):
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": name,
			"enabled": 1,
			"steps": [{"prompt": "do the thing"}],
			**extra,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_reassign(MACRO, doc.name, owner)
	return doc


def _mk_approval(owner: str, *, conversation=None, status="Pending", **extra):
	doc = frappe.get_doc(
		{
			"doctype": APPROVAL,
			"title": f"{PFX} approval",
			"question": "Approve?",
			"status": status,
			"conversation": conversation,
			**extra,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_reassign(APPROVAL, doc.name, owner)
	return doc


def _mk_run(owner: str, *, status="completed", agent=AGENT_SLUG):
	doc = frappe.get_doc({"doctype": RUN, "agent": agent, "status": status})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_reassign(RUN, doc.name, owner)
	return doc


def _mk_finding(
	owner: str,
	*,
	agent=AGENT_SLUG,
	run=None,
	severity="blocker",
	title=f"{PFX}-finding",
	detail_md="orig detail",
	amount=1000,
	first_seen=None,
	last_seen=None,
):
	doc = frappe.get_doc(
		{
			"doctype": FINDING,
			"agent": agent,
			"run": run,
			"severity": severity,
			"title": title,
			"detail_md": detail_md,
			"amount": amount,
			"state": "open",
			"first_seen_run": first_seen,
			"last_seen_run": last_seen,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	_reassign(FINDING, doc.name, owner)
	return doc


class Part3Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A, ["Jarvis User"])
		_ensure_user(USER_B, ["Jarvis User"])
		_ensure_user(REVIEWER, ["Jarvis Skill Reviewer"])
		_ensure_user(OUTSIDER, [])  # System User, no Jarvis role
		if not frappe.db.exists(LISTING, AGENT_SLUG):
			frappe.get_doc(
				{
					"doctype": LISTING,
					"agent_slug": AGENT_SLUG,
					"title": "P3 Test Agent",
					"description": "test",
					"status": "Published",
					"nature": "Auditor",
					"skill_bundle": '[{"path":"SKILL.md","body":"PROPRIETARY VENDOR IP"}]',
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# The listing is committed in setUpClass (users/listing must survive the
		# per-test rollback); delete it so it never pollutes the shared
		# patterntest DB (e.g. the agents-marketplace listing count).
		frappe.db.rollback()
		if frappe.db.exists(LISTING, AGENT_SLUG):
			frappe.delete_doc(LISTING, AGENT_SLUG, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.db.rollback()
		# Committed residue (decide() commits): hard-delete p3 rows.
		for dt, filt in (
			(APPROVAL, {"title": ["like", f"{PFX}%"]}),
			(MACRO, {"macro_name": ["like", f"{PFX}%"]}),
		):
			for n in frappe.get_all(dt, filters=filt, pluck="name"):
				if dt == MACRO:
					# The scheduler now COMMITS a failed Macro Run for a slot it refused
					# (#471), so that row outlives the rollback above too.
					for r in frappe.get_all(MACRO_RUN, filters={"macro": n}, pluck="name"):
						frappe.delete_doc(MACRO_RUN, r, force=True, ignore_permissions=True)
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		frappe.db.set_single_value("Jarvis Settings", "agent_skills_sync_status", "")
		frappe.db.commit()


# --------------------------------------------------------------------------- #
# TASK 22 — File list IDOR
# --------------------------------------------------------------------------- #
class TestFileListScoping(Part3Base):
	def test_file_list_excludes_other_users_conversation_files(self):
		conv_a = _mk_conv(USER_A)
		fa = _mk_file(USER_A, f"{PFX}-a-secret", attached=(CONVERSATION, conv_a))
		conv_b = _mk_conv(USER_B)
		fb = _mk_file(USER_B, f"{PFX}-b-own", attached=(CONVERSATION, conv_b))
		# Non-Jarvis public file owned by A (must stay listable — permissive AND).
		pub = _mk_file(USER_A, f"{PFX}-pub", attached=None, is_private=0)
		# B's own non-Jarvis file (own attachments still listable).
		own_np = _mk_file(USER_B, f"{PFX}-b-note", attached=None, is_private=1)

		with _as(USER_B):
			names = set(
				frappe.get_list(
					"File",
					filters={"file_name": ["like", f"{PFX}-%"]},
					pluck="name",
					limit_page_length=0,
				)
			)

		self.assertNotIn(fa.name, names, "leak: B sees A's conversation-attached File")
		self.assertIn(fb.name, names, "regression: B lost their own conversation File")
		self.assertIn(pub.name, names, "over-restrict: a non-Jarvis public File vanished")
		self.assertIn(own_np.name, names, "over-restrict: B's own non-Jarvis File vanished")

	def test_sm_sees_all_conversation_files(self):
		conv_a = _mk_conv(USER_A)
		fa = _mk_file(USER_A, f"{PFX}-sm-a", attached=(CONVERSATION, conv_a))
		with _as("Administrator"):
			names = set(frappe.get_list("File", filters={"file_name": ["like", f"{PFX}-sm-%"]}, pluck="name"))
		self.assertIn(fa.name, names)


# --------------------------------------------------------------------------- #
# TASK 23 / 24 — Macros
# --------------------------------------------------------------------------- #
class TestMacroScoping(Part3Base):
	def test_non_jarvis_user_cannot_create_macro(self):
		with _as(OUTSIDER):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": MACRO,
						"macro_name": f"{PFX}-x",
						"steps": [{"prompt": "hi"}],
					}
				).insert()

	def test_jarvis_user_creates_own_macro(self):
		with _as(USER_A):
			m = frappe.get_doc(
				{
					"doctype": MACRO,
					"macro_name": f"{PFX}-ok",
					"steps": [{"prompt": "hi"}],
				}
			).insert()
		self.assertEqual(m.owner, USER_A)

	def test_macro_list_scoped_to_owner(self):
		ma = _mk_macro(USER_A, f"{PFX}-listA")
		mb = _mk_macro(USER_B, f"{PFX}-listB")
		with _as(USER_B):
			names = set(
				frappe.get_list(MACRO, filters={"macro_name": ["like", f"{PFX}-list%"]}, pluck="name")
			)
		self.assertIn(mb.name, names)
		self.assertNotIn(ma.name, names)

	def test_macro_run_cross_inject_rejected(self):
		# MAC-2: B inserts a Macro Run linked to A's macro.
		ma = _mk_macro(USER_A, f"{PFX}-mac2")
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": MACRO_RUN,
						"macro": ma.name,
						"status": "queued",
					}
				).insert()

	def test_scheduler_skips_barred_owner(self):
		# MAC-1: a due macro owned by a user who lost Jarvis access is skipped.
		m = _mk_macro(OUTSIDER, f"{PFX}-sched", schedule_enabled=1)
		frappe.db.set_value(
			MACRO,
			m.name,
			{
				"schedule_enabled": 1,
				"next_run_at": add_to_date(now_datetime(), hours=-1),
			},
			update_modified=False,
		)
		with patch("jarvis.chat.macros.run_macro") as mock_run:
			macro_scheduler.run_due_macros()
		called = [c.args[0] for c in mock_run.call_args_list]
		self.assertNotIn(m.name, called, "scheduler ran a barred owner's macro")
		# processed-as-skipped: schedule advanced past now.
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime())


# --------------------------------------------------------------------------- #
# TASK 25 — Approval decided-field forgery
# --------------------------------------------------------------------------- #
class TestApprovalForgery(Part3Base):
	def test_rest_insert_cannot_forge_decided_approval(self):
		with _as(USER_A):
			doc = frappe.get_doc(
				{
					"doctype": APPROVAL,
					"title": f"{PFX} forge",
					"question": "q?",
					"status": "Approved",
					"decision": "sneaky",
					"decided_by": USER_B,
				}
			)
			doc.insert()  # perm-checked (generic REST); permlevel strips forged fields
			name = doc.name
		row = frappe.db.get_value(APPROVAL, name, ["status", "decision", "decided_by"], as_dict=True)
		self.assertEqual(row.status, "Pending")
		self.assertFalse(row.decision)
		self.assertFalse(row.decided_by)

	def test_validate_invariant_rejects_new_decided_row(self):
		# Direct proof of the controller invariant (independent of permlevel).
		with _as(USER_A):
			doc = frappe.get_doc(
				{
					"doctype": APPROVAL,
					"title": f"{PFX} inv",
					"question": "q?",
					"status": "Approved",
					"decision": "x",
				}
			)
			doc.set("__islocal", 1)  # insert() sets this; validate runs with it set
			with self.assertRaises(frappe.PermissionError):
				doc._guard_decided_fields()

	def test_owner_can_read_but_not_write_decided_fields(self):
		# FIX 2: permlevel-1 on the decided fields closed WRITE (forgery) but also
		# stripped READ via the perm-checked frappe.client.get path for a non-SM
		# owner. A permlevel-1 READ-only Jarvis User row restores read WITHOUT
		# reopening write or row-level scoping.
		conv = _mk_conv(USER_A)
		appr = _mk_approval(USER_A, conversation=conv, status="Pending")
		# Server stamp (raw set_value, exactly like decide()'s SQL — bypasses
		# permlevel), then the owner reads it back through the perm-checked door.
		frappe.db.set_value(
			APPROVAL,
			appr.name,
			{
				"status": "Approved",
				"decision": "ok",
				"decided_by": USER_A,
			},
			update_modified=False,
		)
		frappe.db.commit()

		# (c) owner CAN now read status/decision/decided_by via client.get.
		with _as(USER_A):
			got = frappe.client.get(APPROVAL, appr.name)
		self.assertEqual(got.get("status"), "Approved")
		self.assertEqual(got.get("decision"), "ok")
		self.assertEqual(got.get("decided_by"), USER_A)

		# (b) owner still CANNOT write the decided fields (permlevel-1 write is
		# SM-only): the set_value is stripped, status stays Approved.
		with _as(USER_A):
			frappe.client.set_value(APPROVAL, appr.name, "status", "Rejected")
		self.assertEqual(frappe.db.get_value(APPROVAL, appr.name, "status"), "Approved")

		# (a) a non-owner still cannot read the row at all (row-level scoping via
		# has_approval_permission is unaffected by the permlevel READ grant).
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				frappe.client.get(APPROVAL, appr.name)

	def test_real_decide_stamps_fields(self):
		conv = _mk_conv(USER_A)
		appr = _mk_approval(USER_A, conversation=conv)
		with _as(USER_A), patch("jarvis.chat.api.send_message", return_value={"ok": True}):
			approvals_api.decide(appr.name, "Approve as drafted", 1)
		row = frappe.db.get_value(APPROVAL, appr.name, ["status", "decision", "decided_by"], as_dict=True)
		self.assertEqual(row.status, "Approved")
		self.assertEqual(row.decided_by, USER_A)
		self.assertEqual(row.decision, "Approve as drafted")


# --------------------------------------------------------------------------- #
# TASK 27 / 28 — Comment tagging
# --------------------------------------------------------------------------- #
class TestCommentTagging(Part3Base):
	def test_share_to_non_jarvis_user_rejected(self):
		m = _mk_macro(USER_A, f"{PFX}-share")
		with _as(USER_A):
			with self.assertRaises(frappe.ValidationError):
				docmeta_api.toggle_share("Jarvis Macro", m.name, OUTSIDER, "add")
			docmeta_api.toggle_share("Jarvis Macro", m.name, USER_B, "add")
		self.assertTrue(
			frappe.db.exists(
				"DocShare", {"share_doctype": "Jarvis Macro", "share_name": m.name, "user": USER_B}
			)
		)

	def test_add_comment_strips_mentions(self):
		m = _mk_macro(USER_A, f"{PFX}-comment")
		with _as(USER_A):
			row = docmeta_api.add_comment(
				"Jarvis Macro",
				m.name,
				'<div>hi <span class="mention" data-id="carol@example.com">@Carol</span></div>',
			)
		content = frappe.db.get_value("Comment", row["name"], "content")
		self.assertNotIn('class="mention"', content)
		self.assertNotIn("carol@example.com", content)
		self.assertIn("@Carol", content)  # readable text preserved
		with _as(USER_A):
			row2 = docmeta_api.add_comment("Jarvis Macro", m.name, "plain body")
		self.assertEqual(frappe.db.get_value("Comment", row2["name"], "content"), "plain body")


# --------------------------------------------------------------------------- #
# TASK 29 / 30 / 31 / 32 / 33 — Agents
# --------------------------------------------------------------------------- #
class TestAgentScoping(Part3Base):
	def test_non_jarvis_user_cannot_install_agent(self):
		with _as(OUTSIDER):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": INSTALLATION,
						"agent": AGENT_SLUG,
						"enabled": 1,
					}
				).insert()

	def test_finding_list_scoped_to_owner(self):
		fa = _mk_finding(USER_A, title=f"{PFX}-fA")
		fb = _mk_finding(USER_B, title=f"{PFX}-fB")
		with _as(USER_B):
			names = set(frappe.get_list(FINDING, filters={"title": ["like", f"{PFX}-f%"]}, pluck="name"))
		self.assertIn(fb.name, names)
		self.assertNotIn(fa.name, names)

	def test_apply_agents_requires_reviewer(self):
		with _as(USER_A):  # plain Jarvis User
			with self.assertRaises(frappe.PermissionError):
				agents_api.apply_agents()
		with (
			_as(REVIEWER),
			patch("jarvis.chat.agents_api.build_agent_push_payload", return_value=[]),
			patch("jarvis.chat.agents_api.frappe.enqueue", MagicMock()),
			patch("frappe.db.set_single_value", MagicMock()),
		):
			res = agents_api.apply_agents()
		self.assertTrue(res["ok"])

	def test_finding_owner_cannot_rewrite_audit_fields(self):
		f = _mk_finding(USER_A, severity="blocker", detail_md="orig detail", amount=1000)
		with _as(USER_A):
			doc = frappe.get_doc(FINDING, f.name)
			doc.severity = "note"
			doc.detail_md = ""
			doc.amount = 0
			doc.state = "resolved"
			doc.save()  # if_owner write
		row = frappe.db.get_value(FINDING, f.name, ["severity", "detail_md", "amount", "state"], as_dict=True)
		self.assertEqual(row.severity, "blocker", "audit severity was rewritten")
		self.assertEqual(row.detail_md, "orig detail", "audit detail was erased")
		self.assertEqual(row.amount, 1000, "audit amount was rewritten")
		self.assertEqual(row.state, "resolved", "legit state flip was blocked")
		# The engine (ignore_permissions) can still write audit fields.
		doc2 = frappe.get_doc(FINDING, f.name)
		doc2.severity = "warning"
		doc2.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(FINDING, f.name, "severity"), "warning")

	def test_list_findings_foreign_run_is_not_an_oracle(self):
		run_a = _mk_run(USER_A, status="completed")
		_mk_finding(USER_A, run=run_a.name, first_seen=run_a.name, last_seen=run_a.name)
		with _as(USER_B):
			res = agents_api.list_findings(run=run_a.name)
		self.assertEqual(res["total"], 0)
		self.assertEqual(res["rows"], [])
		# The owner still sees their own run's findings (gate is owner-correct).
		with _as(USER_A):
			mine = agents_api.list_findings(run=run_a.name)
		self.assertGreaterEqual(mine["total"], 1)

	def test_skill_bundle_hidden_from_normal_user(self):
		with _as(USER_A):
			out = agents_api.get_agent(AGENT_SLUG)
			self.assertNotIn("skill_bundle", out)
			rows = frappe.get_list(
				LISTING, filters={"agent_slug": AGENT_SLUG}, fields=["name", "skill_bundle"]
			)
		self.assertTrue(rows)
		self.assertNotIn("skill_bundle", rows[0], "skill_bundle leaked to a normal user")
		with _as("Administrator"):
			rows2 = frappe.get_list(
				LISTING, filters={"agent_slug": AGENT_SLUG}, fields=["name", "skill_bundle"]
			)
		self.assertIn("skill_bundle", rows2[0])
