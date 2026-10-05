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
from jarvis.tests._agent_access import allow_listing_for

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


def _mk_macro_run(owner: str, macro: str, conversation: str, status: str = "running") -> str:
	"""A run row the way the engine writes one: inserted server-side, then owned by
	the macro's owner."""
	doc = frappe.get_doc(
		{
			"doctype": MACRO_RUN,
			"macro": macro,
			"conversation": conversation,
			"status": status,
			"current_step": 0,
			"total_steps": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	_reassign(MACRO_RUN, doc.name, owner)
	return doc.name


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
		# jarvis#1062: deny-by-default access. USER_A reads this listing through
		# get_agent (the skill_bundle-hiding test), which now 403s an ungranted
		# non-admin before it can assert anything about the payload.
		allow_listing_for(AGENT_SLUG, roles=["Jarvis User"])
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
			# take_finding_to_chat commits its seed conversation too.
			(CONVERSATION, {"title": ["like", f"Finding: {PFX}%"]}),
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
		# The sweep tells every Jarvis Admin on the site that this owner's schedules
		# went off, and commits it: drop exactly the notices this test causes.
		notices = {"subject": ["like", "Macro schedules switched off:%"]}
		before = set(frappe.get_all("Notification Log", filters=notices, pluck="name"))

		def drop_notices():
			for n in set(frappe.get_all("Notification Log", filters=notices, pluck="name")) - before:
				frappe.delete_doc("Notification Log", n, force=True, ignore_permissions=True)
			frappe.db.commit()

		self.addCleanup(drop_notices)
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
		# processed-as-skipped: the owner cannot run it, so the schedule is switched off
		# (it used to be moved on, and then skipped again on every later slot).
		row = frappe.db.get_value(MACRO, m.name, ["schedule_enabled", "next_run_at"], as_dict=True)
		self.assertFalse(row.schedule_enabled)
		self.assertIsNone(row.next_run_at)

	# --- A Macro Run row is engine state: its owner may read it, never write it --- #
	# MAC-2 guarded CREATE only. An owner could still UPDATE their own run row over
	# generic REST and repoint its `conversation` at another user's chat; the engine
	# trusted the row and dispatched the next step there AS that user.

	def test_owner_cannot_repoint_their_run_at_another_users_conversation(self):
		ma = _mk_macro(USER_A, f"{PFX}-repoint")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A))
		victim_conv = _mk_conv(USER_B)
		with _as(USER_A):
			doc = frappe.get_doc(MACRO_RUN, run)
			doc.conversation = victim_conv
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_owner_cannot_edit_their_run_row(self):
		# Setting a terminal status by hand skipped the code that disarms an armed
		# run's conversation; `trigger` and `current_step` feed the unattended budget.
		ma = _mk_macro(USER_A, f"{PFX}-edit")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A))
		with _as(USER_A):
			doc = frappe.get_doc(MACRO_RUN, run)
			doc.status = "completed"
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_owner_cannot_delete_their_run_row(self):
		ma = _mk_macro(USER_A, f"{PFX}-delete")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A))
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc(MACRO_RUN, run)

	def test_owner_can_still_read_their_run_row(self):
		ma = _mk_macro(USER_A, f"{PFX}-read")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A))
		with _as(USER_A):
			self.assertTrue(frappe.get_doc(MACRO_RUN, run).has_permission("read"))
		with _as(USER_B):
			self.assertFalse(frappe.get_doc(MACRO_RUN, run).has_permission("read"))

	def test_stop_is_still_the_owners_and_only_the_owners(self):
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-stop")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A))
		with _as(USER_B):
			with self.assertRaises(frappe.PermissionError):
				macros.stop_macro_run(run)
		with _as(USER_A):
			macros.stop_macro_run(run)
		self.assertEqual(frappe.db.get_value(MACRO_RUN, run, "status"), "stopped")

	def test_engine_never_advances_a_run_into_another_users_conversation(self):
		# Defence in depth: even if a row is ever tampered with, the engine must not
		# act on a run whose owner is not the owner of the conversation it points at.
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-engine")
		victim_conv = _mk_conv(USER_B)
		# The victim's chat is itself an armed run conversation. Failing the tampered
		# run must not reach into it: `_finish` would disarm it.
		frappe.db.set_value(CONVERSATION, victim_conv, "skip_confirmation", 1, update_modified=False)
		run = _mk_macro_run(USER_A, ma.name, victim_conv)
		with patch("jarvis.chat.api._enqueue_turn") as dispatch:
			macros.advance_after_turn(victim_conv, errored=False)
		dispatch.assert_not_called()
		self.assertEqual(frappe.db.get_value(MACRO_RUN, run, "status"), "failed")
		self.assertEqual(frappe.db.get_value(CONVERSATION, victim_conv, "skip_confirmation"), 1)

	def test_engine_never_advances_a_run_of_another_users_macro(self):
		# The other leg of the same check: the run and its conversation are the
		# owner's, the MACRO it points at is not.
		from jarvis.chat import macros

		mb = _mk_macro(USER_B, f"{PFX}-engine-macro")
		conv = _mk_conv(USER_A)
		run = _mk_macro_run(USER_A, mb.name, conv)
		with patch("jarvis.chat.api._enqueue_turn") as dispatch:
			macros.advance_after_turn(conv, errored=False)
		dispatch.assert_not_called()
		self.assertEqual(frappe.db.get_value(MACRO_RUN, run, "status"), "failed")

	def test_engine_never_resumes_a_parked_run_into_another_users_conversation(self):
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-engine-resume")
		run = _mk_macro_run(USER_A, ma.name, _mk_conv(USER_B), status="waiting_capacity")
		with patch("jarvis.chat.api._enqueue_turn") as dispatch:
			macros.resume_waiting_capacity_runs()
		dispatch.assert_not_called()
		self.assertEqual(frappe.db.get_value(MACRO_RUN, run, "status"), "failed")

	def test_a_parked_run_whose_conversation_was_deleted_is_not_called_tampering(self):
		# "Delete all conversations" blanks the link on the owner's own runs. That is
		# an ordinary state, so it must fail plainly and not raise the mismatch alarm.
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-engine-gone")
		run = _mk_macro_run(USER_A, ma.name, None, status="waiting_capacity")
		with patch("jarvis.chat.api._enqueue_turn") as dispatch:
			macros.resume_waiting_capacity_runs()
		dispatch.assert_not_called()
		row = frappe.db.get_value(MACRO_RUN, run, ["status", "error"], as_dict=True)
		self.assertEqual((row.status, row.error), ("failed", macros._CONVERSATION_GONE_ERROR))

	# Each layer is proven on its own. The role row and the hook mask each other: on a
	# migrated site the row alone refuses a write, so a test that only saves a doc
	# cannot tell whether the hook still works, and the other way round.

	def test_the_run_hook_alone_refuses_every_write(self):
		from jarvis.chat.macro_permissions import has_macro_run_permission

		ma = _mk_macro(USER_A, f"{PFX}-hook")
		doc = frappe.get_doc(MACRO_RUN, _mk_macro_run(USER_A, ma.name, _mk_conv(USER_A)))
		for ptype in ("write", "create", "delete", "submit", "cancel", "amend", "share", "import"):
			for user in (USER_A, USER_B, "Administrator"):
				self.assertFalse(has_macro_run_permission(doc, ptype, user), f"{ptype} allowed for {user}")
		self.assertTrue(has_macro_run_permission(doc, "read", USER_A))
		self.assertFalse(has_macro_run_permission(doc, "read", USER_B))
		self.assertTrue(has_macro_run_permission(doc, "read", "Administrator"))

	def test_the_run_doctype_alone_grants_read_only(self):
		import json

		path = frappe.get_app_path("jarvis", "jarvis", "doctype", "jarvis_macro_run", "jarvis_macro_run.json")
		with open(path) as f:
			rows = json.load(f)["permissions"]
		self.assertTrue(rows)
		for row in rows:
			granted = [k for k in ("write", "create", "delete", "submit", "cancel", "amend") if row.get(k)]
			self.assertEqual(granted, [], f"{row.get('role')} may {granted} a run row")

	# --- The same trust mistake on the macro itself: the summary link ---------- #
	# `merge_conversation` names the throwaway chat a summary is being generated in.
	# When ANY turn ends, the engine looks a macro up by that field, reads that
	# conversation's reply, then deletes the conversation with permissions ignored.

	def test_owner_cannot_point_their_macros_summary_at_another_users_conversation(self):
		# Whatever a save carries in the field is dropped: the stored value wins.
		ma = _mk_macro(USER_A, f"{PFX}-merge-point")
		victim_conv = _mk_conv(USER_B)
		with _as(USER_A):
			doc = frappe.get_doc(MACRO, ma.name)
			doc.merge_conversation = victim_conv
			doc.merge_status = "pending"
			doc.save()
		self.assertFalse(frappe.db.get_value(MACRO, ma.name, "merge_conversation"))

	def test_a_new_macro_cannot_arrive_with_a_summary_link(self):
		victim_conv = _mk_conv(USER_B)
		with _as(USER_A):
			m = frappe.get_doc(
				{
					"doctype": MACRO,
					"macro_name": f"{PFX}-merge-new",
					"steps": [{"prompt": "hi"}],
					"merge_conversation": victim_conv,
					"merge_status": "pending",
				}
			).insert()
		row = frappe.db.get_value(MACRO, m.name, ["merge_conversation", "merge_status"], as_dict=True)
		self.assertFalse(row.merge_conversation)
		self.assertFalse(row.merge_status, "a new macro arrived already 'summarizing', with no turn coming")

	def test_a_stale_save_does_not_wipe_a_summary_that_just_started(self):
		# The form was loaded, then the engine started a summary, then the form saved.
		# The save carries the old empty link; it must neither be refused nor win.
		ma = _mk_macro(USER_A, f"{PFX}-merge-stale")
		own_conv = _mk_conv(USER_A)
		with _as(USER_A):
			doc = frappe.get_doc(MACRO, ma.name)
			frappe.db.set_value(MACRO, ma.name, "merge_conversation", own_conv, update_modified=False)
			doc.description = "saved from a stale form"
			doc.save()
		self.assertEqual(frappe.db.get_value(MACRO, ma.name, "merge_conversation"), own_conv)

	def test_only_the_owner_can_start_a_summary(self):
		# Summarize was gated on READ, so anyone who could see a macro could overwrite
		# its owner's summary. It writes to the macro, so it needs write.
		from jarvis.chat import macros_api

		overseer = _ensure_user("p3-overseer@example.com", ["System Manager", "Jarvis User"])
		ma = _mk_macro(USER_A, f"{PFX}-merge-reader", steps=[{"prompt": "a"}, {"prompt": "b"}])
		with _as(overseer):
			self.assertTrue(frappe.get_doc(MACRO, ma.name).has_permission("read"), "premise: can read it")
			with self.assertRaises(frappe.PermissionError):
				macros_api.summarize_macro(ma.name)
		self.assertFalse(frappe.db.get_value(MACRO, ma.name, "merge_conversation"))

	def test_a_summary_started_for_another_owner_runs_in_that_owners_chat(self):
		# Administrator passes every permission check. The throwaway chat must still be
		# the macro owner's, or the engine (rightly) refuses to land the summary.
		from jarvis.chat import macros_api

		ma = _mk_macro(USER_A, f"{PFX}-merge-admin", steps=[{"prompt": "a"}, {"prompt": "b"}])
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
			macros_api.summarize_macro(ma.name)
		conv = frappe.db.get_value(MACRO, ma.name, "merge_conversation")
		self.assertEqual(frappe.db.get_value(CONVERSATION, conv, "owner"), USER_A)

	def test_a_summary_whose_conversation_was_deleted_fails_without_an_alarm(self):
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-merge-gone")
		gone = _mk_conv(USER_A)
		frappe.db.set_value(
			MACRO, ma.name, {"merge_status": "pending", "merge_conversation": gone}, update_modified=False
		)
		frappe.delete_doc(CONVERSATION, gone, force=True, ignore_permissions=True)
		with patch("frappe.log_error") as log:
			macros.advance_after_turn(gone, errored=False)
		self.assertEqual(frappe.db.get_value(MACRO, ma.name, "merge_status"), "failed")
		# Nothing at all: neither the tamper alarm nor a swallowed failure in the path.
		self.assertEqual(log.call_args_list, [], "a deleted chat was logged as an error")

	def test_engine_never_lands_a_summary_from_another_users_conversation(self):
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-merge-engine")
		victim_conv = _mk_conv(USER_B)
		frappe.db.set_value(
			MACRO,
			ma.name,
			{"merge_status": "pending", "merge_conversation": victim_conv},
			update_modified=False,
		)
		with patch("jarvis.chat.macros.publish_to_user") as publish:
			macros.advance_after_turn(victim_conv, errored=False)
		self.assertTrue(
			frappe.db.exists(CONVERSATION, victim_conv), "another user's conversation was deleted"
		)
		# An open form learns the outcome only from this event; it goes to the macro's
		# owner and says nothing about the other conversation.
		sent = [c.args for c in publish.call_args_list if c.args[1].get("kind") == "macro:merged"]
		self.assertEqual(len(sent), 1, publish.call_args_list)
		self.assertEqual((sent[0][0], sent[0][1]["status"]), (USER_A, "failed"))
		self.assertNotIn(victim_conv, frappe.as_json(sent[0][1]))
		row = frappe.db.get_value(MACRO, ma.name, ["merge_status", "merge_conversation"], as_dict=True)
		self.assertEqual((row.merge_status, row.merge_conversation or ""), ("failed", ""))

	def test_only_the_owner_can_start_a_run(self):
		# A run executes as the macro's OWNER, with the owner's permissions, and
		# uncarded when the macro is armed. It was gated on READ, so anyone who could
		# only see a macro (oversight, a read share) chose when the owner's steps ran.
		from jarvis.chat import macros_api

		overseer = _ensure_user("p3-overseer@example.com", ["System Manager", "Jarvis User"])
		ma = _mk_macro(USER_A, f"{PFX}-run-reader")
		with _as(overseer):
			self.assertTrue(frappe.get_doc(MACRO, ma.name).has_permission("read"), "premise: can read it")
			with self.assertRaises(frappe.PermissionError):
				macros_api.run_macro(ma.name)
		self.assertFalse(frappe.get_all(MACRO_RUN, filters={"macro": ma.name}))

	def test_an_owner_without_the_jarvis_user_role_can_still_run_their_own_macro(self):
		# The gate is "you are the owner", not "you have write": write comes from the
		# Jarvis User role row, and an owner who holds System Manager instead of it
		# would have had every scheduled run of their own macro refused.
		from jarvis.chat import macros

		owner = _ensure_user("p3-smowner@example.com", ["System Manager"])
		ma = _mk_macro(owner, f"{PFX}-run-smowner")
		with _as(owner):
			self.assertFalse(frappe.get_doc(MACRO, ma.name).has_permission("write"), "premise: no write")
			with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
				out = macros.run_macro(ma.name)
		self.assertTrue(out.get("ok"), out)

	def test_a_run_is_not_started_for_an_owner_who_can_no_longer_use_jarvis(self):
		# Administrator passes the owner gate for anyone's macro, but the run would
		# execute AS that owner with nobody at the keyboard (#469).
		from jarvis.chat import macros

		ma = _mk_macro(USER_A, f"{PFX}-run-barred")
		with (
			patch("jarvis.permissions.has_jarvis_access", return_value=False),
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			self.assertRaises(frappe.PermissionError),
		):
			macros.run_macro(ma.name)
		enqueue.assert_not_called()
		self.assertFalse(frappe.get_all(MACRO_RUN, filters={"macro": ma.name}))

	def test_a_summary_is_not_started_for_an_owner_who_cannot_run_unattended(self):
		# Started by someone else, the summary turn runs AS the macro's owner with
		# nobody at the keyboard: the same rule as a scheduled run (#469).
		from jarvis.chat import macros_api

		ma = _mk_macro(USER_A, f"{PFX}-merge-barred", steps=[{"prompt": "a"}, {"prompt": "b"}])
		with (
			patch("jarvis.permissions.is_valid_unattended_owner", return_value=False),
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			self.assertRaises(frappe.PermissionError),
		):
			macros_api.summarize_macro(ma.name)
		enqueue.assert_not_called()
		self.assertFalse(frappe.db.get_value(MACRO, ma.name, "merge_conversation"))

	def test_a_stale_save_does_not_put_a_landed_summary_back_to_summarizing(self):
		# A form opened while a summary was running still holds "pending". The summary
		# lands (the engine does not bump `modified`), then the form is saved. Written
		# back, "pending" has no turn coming and a manual Run is refused for good.
		ma = _mk_macro(USER_A, f"{PFX}-merge-stale-status")
		own_conv = _mk_conv(USER_A)
		frappe.db.set_value(
			MACRO, ma.name, {"merge_status": "pending", "merge_conversation": own_conv}, update_modified=False
		)
		with _as(USER_A):
			stale = frappe.get_doc(MACRO, ma.name)
			frappe.db.set_value(
				MACRO,
				ma.name,
				{"merge_status": "ready", "merged_prompt": "the summary", "merge_conversation": ""},
				update_modified=False,
			)
			stale.description = "edited in a form that was open all along"
			stale.save()
		row = frappe.db.get_value(
			MACRO,
			ma.name,
			["merge_status", "merged_prompt", "merge_conversation", "description"],
			as_dict=True,
		)
		self.assertEqual((row.merge_status, row.merged_prompt), ("ready", "the summary"))
		self.assertFalse(row.merge_conversation)
		self.assertEqual(row.description, "edited in a form that was open all along")

	def test_editing_the_steps_as_a_summary_lands_still_discards_that_summary(self):
		# The other direction must keep working: a save that CLEARS the summary because
		# the steps changed is current even if a summary landed a moment earlier. Kept,
		# the macro would run a summary of steps it no longer has.
		from jarvis.chat import macros_api

		ma = _mk_macro(USER_A, f"{PFX}-merge-steps", steps=[{"prompt": "a"}, {"prompt": "b"}])
		frappe.db.set_value(
			MACRO, ma.name, {"merge_status": "ready", "merged_prompt": "of a and b"}, update_modified=False
		)
		with _as(USER_A):
			macros_api.update_macro(ma.name, steps=frappe.as_json([{"prompt": "a"}, {"prompt": "c"}]))
		row = frappe.db.get_value(MACRO, ma.name, ["merge_status", "merged_prompt"], as_dict=True)
		self.assertFalse(row.merge_status)
		self.assertFalse(row.merged_prompt)

	def test_a_macro_deleted_while_its_summary_ran_ends_quietly(self):
		from jarvis.chat import macros

		conv = _mk_conv(USER_A)
		with patch("frappe.log_error") as log:
			self.assertTrue(macros._drop_summary_link_unless_one_owner("p3-no-such-macro", conv))
		self.assertEqual(log.call_args_list, [])
		self.assertTrue(frappe.db.exists(CONVERSATION, conv))

	def test_an_ordinary_save_of_a_macro_being_summarized_still_works(self):
		# The form saves while a summary is pending and sends the link back unchanged.
		ma = _mk_macro(USER_A, f"{PFX}-merge-save")
		own_conv = _mk_conv(USER_A)
		frappe.db.set_value(
			MACRO, ma.name, {"merge_status": "pending", "merge_conversation": own_conv}, update_modified=False
		)
		with _as(USER_A):
			doc = frappe.get_doc(MACRO, ma.name)
			doc.description = "edited while summarizing"
			doc.save()
		self.assertEqual(frappe.db.get_value(MACRO, ma.name, "merge_conversation"), own_conv)

	# --- engine records are not renamable ---------------------------------------- #
	# A rename rewrites every link to a record in place, with no permission check on
	# the rows that hold those links: a run row would follow its conversation onto
	# whatever the conversation became.

	def _engine_records(self):
		ma = _mk_macro(USER_A, f"{PFX}-rename")
		conv = _mk_conv(USER_A)
		return ((MACRO, ma.name), (CONVERSATION, conv), (MACRO_RUN, _mk_macro_run(USER_A, ma.name, conv)))

	def test_an_engine_record_refuses_to_be_renamed(self):
		for dt, name in self._engine_records():
			with self.subTest(dt=dt), _as(USER_A):
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc(dt, name).rename(f"{PFX}-renamed-{frappe.generate_hash(length=6)}")
			self.assertTrue(frappe.db.exists(dt, name), f"{dt} was renamed")

	def test_an_engine_record_refuses_to_be_merged_into_another_users(self):
		ma = _mk_macro(USER_A, f"{PFX}-rename-conv")
		own_conv = _mk_conv(USER_A)
		run = _mk_macro_run(USER_A, ma.name, own_conv)
		victim_conv = _mk_conv(USER_B)
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			frappe.get_doc(CONVERSATION, own_conv).rename(victim_conv, merge=True)
		self.assertEqual(frappe.db.get_value(MACRO_RUN, run, "conversation"), own_conv)
		self.assertTrue(frappe.db.exists(CONVERSATION, own_conv))

	def test_renaming_an_engine_record_is_not_an_api_method(self):
		from frappe.model.document import Document

		frappe.is_whitelisted(Document.rename)  # control: this check can pass
		for dt, name in self._engine_records():
			method = frappe.get_doc(dt, name).rename
			with self.subTest(dt=dt), self.assertRaises(frappe.PermissionError):
				frappe.is_whitelisted(method.__func__)

	def test_a_server_side_rename_of_an_engine_record_is_refused_too(self):
		# Code that calls the rename machinery directly, not through the document.
		for dt, name in self._engine_records():
			with self.subTest(dt=dt), self.assertRaises(frappe.PermissionError):
				frappe.rename_doc(dt, name, f"{PFX}-moved-{frappe.generate_hash(length=6)}", force=True)


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

	# ------------------------------------------------------------------ #
	# jarvis#1062 polish: take_finding_to_chat commits its seed conversation
	# BEFORE attempting to seed it; a failed seed must not leave an orphaned,
	# empty "Finding: ..." conversation behind (FindingsPanel no longer
	# navigates on ok:False, so nothing cleans it up client-side either).
	# ------------------------------------------------------------------ #
	def test_take_finding_to_chat_seed_failure_leaves_no_conversation_row(self):
		f = _mk_finding(USER_A, title=f"{PFX}-cleanup-fail")
		conv_filters = {"title": f"Finding: {f.title}"[:140]}
		self.assertFalse(frappe.db.exists(CONVERSATION, conv_filters))
		with _as(USER_A):
			with patch(
				"jarvis.chat.api.send_message",
				return_value={"ok": False, "reason": "llm_not_configured"},
			):
				res = agents_api.take_finding_to_chat(f.name)
		self.assertFalse(res["ok"])
		self.assertEqual(res["reason"], "llm_not_configured")
		self.assertIsNone(res.get("conversation"))
		self.assertFalse(
			frappe.db.exists(CONVERSATION, conv_filters),
			"a failed seed must not leave an orphaned conversation behind",
		)

	def test_take_finding_to_chat_seed_success_keeps_the_conversation(self):
		"""Control: the cleanup path must not have swallowed the real case."""
		f = _mk_finding(USER_A, title=f"{PFX}-cleanup-ok")
		conv_filters = {"title": f"Finding: {f.title}"[:140]}
		with _as(USER_A):
			with patch(
				"jarvis.chat.api.send_message",
				return_value={"ok": True, "run_id": "r1", "message_id": "m1"},
			):
				res = agents_api.take_finding_to_chat(f.name)
		self.assertTrue(res["ok"])
		self.assertIsNotNone(res["conversation"])
		self.assertTrue(frappe.db.exists(CONVERSATION, conv_filters))

	def test_take_finding_to_chat_seed_is_stamped_agent(self):
		"""P0a: the seed send runs under message_origin("agent")."""
		f = _mk_finding(USER_A, title=f"{PFX}-origin")
		seen = []

		def _send(**kw):
			seen.append(frappe.flags.get("jarvis_message_origin"))
			return {"ok": True, "run_id": "r1", "message_id": "m1"}

		with _as(USER_A), patch("jarvis.chat.api.send_message", side_effect=_send):
			agents_api.take_finding_to_chat(f.name)
		self.assertEqual(seen, ["agent"])
		self.assertIsNone(frappe.flags.get("jarvis_message_origin"))
