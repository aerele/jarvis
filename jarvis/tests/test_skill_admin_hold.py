"""What an admin's hold, release and delete of another user's skill do
(``jarvis.chat.skills_admin``, through ``skills_admin_api``).

What is pinned here:

* a hold switches the skill off and disarms it, bumps ``modified``, rejects its
  Pending promotion request in the same transaction (never through
  ``_stamp_decision``, which commits), writes the audit row and the Comment, tells
  the owner (never the reason in the notice), and nothing of it survives a failure
  before the commit;
* a held skill is not served (``get_skill``, ``find_skills``, ``/slug``), un-shadows a
  same-name private row, and is not promoted: not requested, not approved from a held
  source, not widened onto a held published copy, the request left Pending;
* the controller guard keeps the stored hold on every save, ignoring permissions too,
  and refuses switching a held skill on or arming it, which is what refuses the SPA
  form saved after the hold;
* release with and without switching back on; delete with Pending and Rejected
  requests, by an admin and by the owner (single and bulk);
* who is told about a published copy; the owner's notices on the list envelope.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from jarvis.chat import custom_skills_api, skill_permissions, skills_admin, skills_admin_api
from jarvis.exceptions import InvalidArgumentError
from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import SkillOnHoldError
from jarvis.tests import test_skill_admin_api as base
from jarvis.tests.race_harness import other_connection

SKILL = base.SKILL
PROMO = base.PROMO
TAG = base.TAG
OWNER = base.OWNER
OTHER = base.OTHER
REVIEWER = base.REVIEWER
ADMIN = base.ADMIN
CLEAR_PROJECTION = {"over_budget": False, "at_budget": False, "strict_would_fail": False}


class HoldBase(base.SkillAdminBase):
	def _hold(self, skill, reason="Under review", user=ADMIN):
		frappe.set_user(user)
		return skills_admin_api.admin_hold(skill, reason)

	def _request(self, skill, to_scope="Org"):
		"""A Pending promotion request of OWNER's skill, as OWNER files it (committed)."""
		frappe.set_user(OWNER)
		return custom_skills_api.request_skill_promotion(skill, to_scope)["request"]

	def _reject(self, request):
		frappe.set_user(REVIEWER)
		custom_skills_api.decide_skill_promotion(request, 0, note="No")

	def _status(self, request):
		return frappe.db.get_value(PROMO, request, ["status", "reviewer", "decision_note"], as_dict=True)


# --------------------------------------------------------------------------- #
# Hold
# --------------------------------------------------------------------------- #
class TestHold(HoldBase):
	def test_a_hold_switches_off_disarms_records_and_tells_the_owner(self):
		skill = self._skill(OWNER, "hold", allow_approve_run=1)
		before = self._row(skill, "modified").modified
		out = self._hold(skill, "Leaks <b>prices</b>")
		self.assertEqual(out, {"ok": True, "needs_apply": False, "used_by_macros": {"total": 0, "armed": 0}})
		row = self._row(skill, "admin_hold", "admin_hold_reason", "enabled", "allow_approve_run", "modified")
		self.assertEqual(
			(row.admin_hold, row.admin_hold_reason, row.enabled, row.allow_approve_run),
			(1, "Leaks <b>prices</b>", 0, 0),
		)
		self.assertGreater(row.modified, before)  # a Desk form loaded earlier fails its save
		self.assertEqual(
			self._log(skill),
			[
				{
					"verb": "hold",
					"admin": ADMIN,
					"skill_name": f"{TAG}-hold",
					"skill_owner": OWNER,
					"reason": "Leaks <b>prices</b>",
					"needs_apply": 0,
				}
			],
		)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": SKILL, "reference_name": skill},
			fields=["comment_email", "content"],
		)
		self.assertEqual([c.comment_email for c in comments], [ADMIN])
		(note,) = self._notes(OWNER)
		self.assertTrue(note.subject.startswith(skills_admin.ADMIN_HOLD_NOTICE))
		self.assertFalse(note.document_name)
		self.assertNotIn("prices", note.email_content)  # Desk renders the body as HTML
		self.assertEqual(self._notes(ADMIN), [])

	def test_a_repeat_updates_the_reason(self):
		skill = self._skill(OWNER, "repeat")
		self._hold(skill, "First")
		self._hold(skill, "Second")
		self.assertEqual(self._row(skill, "admin_hold_reason").admin_hold_reason, "Second")
		self.assertEqual([r.verb for r in self._log(skill)], ["hold", "hold"])

	def test_a_reason_is_required_and_bounded(self):
		skill = self._skill(OWNER, "reason")
		for bad in ("", "  ", None, "x" * 501, ["why"]):
			with self.subTest(reason=bad):
				with self.assertRaises(frappe.ValidationError):
					self._hold(skill, bad)
		self.assertEqual(self._row(skill, "admin_hold").admin_hold, 0)

	def test_a_company_skill_needs_an_apply(self):
		skill = self._skill(OWNER, "hold-org", scope="Org")
		self.assertTrue(self._hold(skill)["needs_apply"])
		self.assertEqual(self._log(skill)[0].needs_apply, 1)

	def test_the_pending_request_is_rejected_in_the_same_transaction(self):
		skill = self._skill(OWNER, "hold-req")
		request = self._request(skill)
		with patch.object(custom_skills_api, "_stamp_decision") as stamp:
			self._hold(skill, "Stop it")
		stamp.assert_not_called()  # it commits: the act would no longer be one transaction
		status = self._status(request)
		self.assertEqual((status.status, status.reviewer), ("Rejected", ADMIN))
		self.assertIn("on hold", status.decision_note)

	def test_nothing_survives_a_failure_before_the_commit(self):
		skill = self._skill(OWNER, "atomic")
		request = self._request(skill)
		frappe.set_user(ADMIN)
		with patch.object(skills_admin.admin_log, "write", side_effect=RuntimeError("audit down")):
			with self.assertRaises(RuntimeError):
				skills_admin_api.admin_hold(skill, "Never lands")
		frappe.db.rollback()  # what the request does with the error
		row = self._row(skill, "admin_hold", "enabled")
		self.assertEqual((row.admin_hold, row.enabled), (0, 1))
		self.assertEqual(self._status(request).status, "Pending")

	def test_a_failed_notice_does_not_fail_the_hold(self):
		skill = self._skill(OWNER, "notice-fails")
		with patch.object(skills_admin, "_recipients", return_value={"zz-not-a-user@example.com"}):
			self.assertTrue(self._hold(skill)["ok"])
		self.assertEqual(self._row(skill, "admin_hold").admin_hold, 1)

	def test_a_held_skill_is_not_served(self):
		from jarvis.chat.custom_skills import invoked_skill_slugs
		from jarvis.tools.find_skills import find_skills
		from jarvis.tools.get_skill import get_skill

		skill = self._skill(OWNER, "served")
		slug = f"{TAG}-served"
		frappe.set_user(OWNER)
		self.assertEqual(get_skill(slug)["instructions"], "served instructions")
		self.assertIn(slug, [s["skill_name"] for s in find_skills(slug)["skills"]])
		self.assertEqual(invoked_skill_slugs(f"/{slug} now", user=OWNER), {slug})
		self._hold(skill)
		frappe.set_user(OWNER)
		with self.assertRaises(InvalidArgumentError):
			get_skill(slug)
		self.assertNotIn(slug, [s["skill_name"] for s in find_skills(slug)["skills"]])
		self.assertEqual(invoked_skill_slugs(f"/{slug} now", user=OWNER), set())

	def test_holding_a_reviewed_row_unshadows_a_private_row_of_that_name(self):
		from jarvis.tools.get_skill import get_skill

		slug = f"{TAG}-shadow"
		# The private row first: the fixture inserts as Administrator, and a second
		# Administrator-owned row of the same name would be refused.
		self._skill(OWNER, "shadow-own", skill_name=slug)
		reviewed = self._skill("Administrator", "shadow-role", scope="Role", skill_name=slug)
		frappe.set_user(OWNER)
		self.assertEqual(get_skill(slug)["instructions"], "shadow-role instructions")
		self._hold(reviewed)
		frappe.set_user(OWNER)
		self.assertEqual(get_skill(slug)["instructions"], "shadow-own instructions")

	def test_held_skills_lists_it_for_an_operator(self):
		skill = self._skill(OWNER, "operator")
		self._hold(skill, "Operator sees this")
		frappe.set_user("Administrator")
		found = {r.name: r for r in skill_permissions.held_skills()}
		self.assertEqual(found[skill].admin_hold_reason, "Operator sees this")


# --------------------------------------------------------------------------- #
# Promotion (D2)
# --------------------------------------------------------------------------- #
@patch("jarvis.chat.custom_skills_api.project_org_promotion_push", return_value=CLEAR_PROJECTION)
class TestPromotion(HoldBase):
	def test_a_held_skill_cannot_be_requested(self, _projection):
		skill = self._skill(OWNER, "promo-req")
		self._hold(skill)
		frappe.set_user(OWNER)
		with self.assertRaises(SkillOnHoldError) as ctx:
			custom_skills_api.request_skill_promotion(skill, "Org")
		self.assertIn("Under review", str(ctx.exception))
		self.assertFalse(frappe.db.exists(PROMO, {"skill": skill}))

	def test_approval_of_a_held_source_is_refused_and_left_pending(self, _projection):
		skill = self._skill(OWNER, "promo-src")
		request = self._request(skill)
		# Held without going through admin_hold, which would reject the request: this is
		# the approval that reads the hold, as one in flight when the hold committed.
		frappe.db.set_value(SKILL, skill, {"admin_hold": 1, "admin_hold_reason": "Racing"})
		frappe.db.commit()
		frappe.set_user(REVIEWER)
		with self.assertRaises(SkillOnHoldError) as ctx:
			custom_skills_api.decide_skill_promotion(request, 1)
		self.assertIn("Racing", str(ctx.exception))
		frappe.db.rollback()
		self.assertEqual(self._status(request).status, "Pending")
		self.assertFalse(frappe.db.exists(SKILL, {"source_skill": skill}))

	def test_widening_onto_a_held_published_copy_is_refused(self, _projection):
		source = self._skill(OWNER, "promo-lin")
		copy = self._skill("Administrator", "promo-copy", scope="Role", source_skill=source)
		self._hold(copy, "Copy held")
		request = self._request(source, "Org")
		frappe.set_user(REVIEWER)
		with self.assertRaises(SkillOnHoldError) as ctx:
			custom_skills_api.decide_skill_promotion(request, 1)
		self.assertIn("Copy held", str(ctx.exception))
		frappe.db.rollback()
		self.assertEqual(self._status(request).status, "Pending")
		row = self._row(copy, "scope", "enabled", "admin_hold")
		self.assertEqual((row.scope, row.enabled, row.admin_hold), ("Role", 0, 1))


# --------------------------------------------------------------------------- #
# The controller guard
# --------------------------------------------------------------------------- #
class TestControllerGuard(HoldBase):
	def test_a_save_keeps_the_stored_hold_even_ignoring_permissions(self):
		skill = self._skill(OWNER, "guard")
		self._hold(skill, "Stored")
		frappe.set_user("Administrator")
		doc = frappe.get_doc(SKILL, skill)
		doc.admin_hold = 0
		doc.admin_hold_reason = "Forged"
		doc.description = "edited"
		doc.save(ignore_permissions=True)
		row = self._row(skill, "admin_hold", "admin_hold_reason", "description")
		self.assertEqual((row.admin_hold, row.admin_hold_reason, row.description), (1, "Stored", "edited"))

	def test_an_insert_cannot_carry_a_hold(self):
		frappe.set_user(OWNER)
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": f"{TAG}-insert",
				"description": "d",
				"instructions": "i",
				"admin_hold": 1,
				"admin_hold_reason": "Self-made",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		row = self._row(doc.name, "admin_hold", "admin_hold_reason")
		self.assertEqual((row.admin_hold, row.admin_hold_reason), (0, None))

	def test_a_held_skill_cannot_be_switched_on_or_armed(self):
		skill = self._skill(OWNER, "switch")
		self._hold(skill, "Reason named")
		frappe.set_user("Administrator")
		for field in ("enabled", "allow_approve_run"):
			with self.subTest(field=field):
				doc = frappe.get_doc(SKILL, skill)
				doc.set(field, 1)
				with self.assertRaises(SkillOnHoldError) as ctx:
					doc.save(ignore_permissions=True)
				self.assertIn("Reason named", str(ctx.exception))
		row = self._row(skill, "enabled", "allow_approve_run")
		self.assertEqual((row.enabled, row.allow_approve_run), (0, 0))

	def test_the_spa_form_saved_after_the_hold_is_refused(self):
		# The SPA sends both switches and no timestamp (plan check item 4), so the
		# guard, not ``modified``, is what refuses a form loaded before the hold.
		skill = self._skill(OWNER, "stale")
		frappe.set_user(OWNER)
		loaded = custom_skills_api.get_custom_skill(skill)
		self._hold(skill, "Too late")
		frappe.set_user(OWNER)
		with self.assertRaises(SkillOnHoldError):
			custom_skills_api.update_custom_skill(
				skill, description="late edit", enabled=loaded["enabled"], allow_approve_run=0
			)
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "description").description, "stale description")
		custom_skills_api.update_custom_skill(skill, description="kept off", enabled=0, allow_approve_run=0)
		row = self._row(skill, "admin_hold", "description", "enabled")
		self.assertEqual((row.admin_hold, row.description, row.enabled), (1, "kept off", 0))

	def test_the_owner_reads_the_hold_through_the_skill_apis_only(self):
		skill = self._skill(OWNER, "owner-reads", shared_with=[{"user": OTHER}])
		self._hold(skill, "Owner reads this")
		frappe.set_user(OWNER)
		mine = custom_skills_api.get_custom_skill(skill)
		self.assertEqual((mine["admin_hold"], mine["admin_hold_reason"]), (1, "Owner reads this"))
		page = custom_skills_api.list_custom_skills_page(search=TAG)
		row = next(r for r in page["rows"] if r["name"] == skill)
		self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (1, "Owner reads this"))
		row = next(r for r in custom_skills_api.list_custom_skills() if r["name"] == skill)
		self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (1, "Owner reads this"))
		frappe.set_user(OTHER)
		shared = custom_skills_api.get_custom_skill(skill)
		self.assertEqual((shared["admin_hold"], shared["admin_hold_reason"]), (1, ""))


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
class TestRelease(HoldBase):
	def test_release_lifts_the_hold_and_leaves_the_skill_off(self):
		skill = self._skill(OWNER, "release", scope="Role", allow_approve_run=1)
		self._hold(skill)
		out = skills_admin_api.admin_release(skill)
		self.assertEqual(out, {"ok": True, "restored": False, "needs_apply": False})
		row = self._row(skill, "admin_hold", "admin_hold_reason", "enabled", "allow_approve_run")
		self.assertEqual(
			(row.admin_hold, row.admin_hold_reason, row.enabled, row.allow_approve_run), (0, None, 0, 0)
		)
		self.assertEqual([r.verb for r in self._log(skill)], ["hold", "release"])
		subjects = sorted(n.subject for n in self._notes(OWNER))
		self.assertTrue(any(s.startswith(skills_admin.ADMIN_RELEASE_NOTICE) for s in subjects))

	def test_release_can_switch_a_shared_skill_back_on_never_armed(self):
		for scope, needs_apply in (("Role", False), ("Org", True)):
			with self.subTest(scope=scope):
				skill = self._skill(OWNER, f"restore-{scope.lower()}", scope=scope, allow_approve_run=1)
				self._hold(skill)
				out = skills_admin_api.admin_release(skill, restore=1)
				self.assertEqual(out, {"ok": True, "restored": True, "needs_apply": needs_apply})
				row = self._row(skill, "admin_hold", "enabled", "allow_approve_run")
				self.assertEqual((row.admin_hold, row.enabled, row.allow_approve_run), (0, 1, 0))

	def test_restore_is_refused_on_a_private_skill(self):
		skill = self._skill(OWNER, "restore-private")
		self._hold(skill)
		with self.assertRaises(frappe.ValidationError):
			skills_admin_api.admin_release(skill, restore="true")
		self.assertEqual(self._row(skill, "admin_hold").admin_hold, 1)

	def test_a_skill_that_is_not_held_is_left_alone(self):
		skill = self._skill(OWNER, "not-held")
		frappe.set_user(ADMIN)
		self.assertEqual(
			skills_admin_api.admin_release(skill), {"ok": True, "restored": False, "needs_apply": False}
		)
		self.assertEqual(self._log(skill), [])

	def test_restore_must_be_a_checkbox(self):
		skill = self._skill(OWNER, "restore-arg", scope="Role")
		self._hold(skill)
		for bad in ("maybe", 2, ["1"], {"x": 1}):
			with self.subTest(value=bad):
				with self.assertRaises(frappe.ValidationError):
					skills_admin_api.admin_release(skill, restore=bad)


# --------------------------------------------------------------------------- #
# Delete (D3)
# --------------------------------------------------------------------------- #
class TestDelete(HoldBase):
	def test_an_admin_deletes_a_skill_with_a_pending_request(self):
		skill = self._skill(OWNER, "del-pending")
		request = self._request(skill)
		frappe.set_user(ADMIN)
		out = skills_admin_api.admin_delete(skill)
		self.assertEqual(out, {"ok": True, "needs_apply": False, "used_by_macros": {"total": 0, "armed": 0}})
		self.assertFalse(frappe.db.exists(SKILL, skill))
		status = self._status(request)
		self.assertEqual((status.status, status.reviewer), ("Rejected", ADMIN))
		self.assertIn("deleted", status.decision_note)
		# The audit row outlives the skill.
		self.assertEqual([(r.verb, r.skill_owner) for r in self._log(skill)], [("delete", OWNER)])
		(note,) = self._notes(OWNER)
		self.assertTrue(note.subject.startswith(skills_admin.ADMIN_DELETE_NOTICE))
		self.assertFalse(note.document_name)

	def test_an_admin_deletes_a_skill_with_a_rejected_request(self):
		skill = self._skill(OWNER, "del-rejected")
		request = self._request(skill)
		self._reject(request)
		frappe.set_user(ADMIN)
		self.assertTrue(skills_admin_api.admin_delete(skill)["ok"])
		self.assertFalse(frappe.db.exists(SKILL, skill))
		# Kept as history, its Link pointing at the deleted name.
		self.assertEqual(frappe.db.get_value(PROMO, request, ["status", "skill"]), ("Rejected", skill))

	def test_the_owner_deletes_a_skill_with_a_rejected_request(self):
		for bulk in (False, True):
			with self.subTest(bulk=bulk):
				skill = self._skill(OWNER, f"own-del-{int(bulk)}")
				self._reject(self._request(skill))
				frappe.set_user(OWNER)
				if bulk:
					out = custom_skills_api.delete_custom_skills_bulk(json.dumps([skill]))
					self.assertEqual(out, {"deleted": 1, "skipped": []})
				else:
					self.assertEqual(custom_skills_api.delete_custom_skill(skill), {"ok": True})
				self.assertFalse(frappe.db.exists(SKILL, skill))

	def test_the_owner_deletes_a_skill_with_a_pending_request(self):
		# The request's Link is exempt from link checks, so without the rejection a
		# reviewer would be left a request for a skill that no longer exists.
		for bulk in (False, True):
			with self.subTest(bulk=bulk):
				skill = self._skill(OWNER, f"own-del-pending-{int(bulk)}")
				request = self._request(skill)
				frappe.set_user(OWNER)
				if bulk:
					out = custom_skills_api.delete_custom_skills_bulk(json.dumps([skill]))
					self.assertEqual(out, {"deleted": 1, "skipped": []})
				else:
					self.assertEqual(custom_skills_api.delete_custom_skill(skill), {"ok": True})
				self.assertFalse(frappe.db.exists(SKILL, skill))
				status = self._status(request)
				self.assertEqual(
					(status.status, status.decision_note),
					("Rejected", "Rejected: the owner deleted this skill."),
				)

	def test_a_failed_bulk_delete_leaves_the_request_pending(self):
		skill = self._skill(OWNER, "own-del-fails")
		request = self._request(skill)
		frappe.set_user(OWNER)
		with patch.object(frappe, "delete_doc", side_effect=RuntimeError("delete fails")):
			out = custom_skills_api.delete_custom_skills_bulk(json.dumps([skill]))
		self.assertEqual(out, {"deleted": 0, "skipped": [{"name": skill, "reason": "error"}]})
		self.assertTrue(frappe.db.exists(SKILL, skill))
		self.assertEqual(self._status(request).status, "Pending")

	def test_a_company_skill_needs_an_apply(self):
		skill = self._skill(OWNER, "del-org", scope="Org")
		frappe.set_user(ADMIN)
		self.assertTrue(skills_admin_api.admin_delete(skill)["needs_apply"])
		self.assertEqual(self._log(skill)[0].needs_apply, 1)

	def test_a_learned_skill_is_refused(self):
		skill = self._skill("Administrator", "del-learned", managed_by_learning=1)
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.ValidationError):
			skills_admin_api.admin_delete(skill)
		self.assertTrue(frappe.db.exists(SKILL, skill))


# --------------------------------------------------------------------------- #
# Locks (skills_admin module docstring)
# --------------------------------------------------------------------------- #
class TestLocks(HoldBase):
	def test_the_requests_are_locked_by_name_with_no_range(self):
		# A range lock on (skill, status) before the skill row deadlocks with
		# request_skill_promotion, which inserts into that range holding the row.
		skill = self._skill(OWNER, "lock-by-name")
		request = self._request(skill)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
			self.assertEqual(skills_admin.lock_requests([]), [])
			spy.assert_not_called()
			self.assertEqual(skills_admin.lock_requests([request]), [request])
		(sql,) = [str(c.args[0]) for c in spy.call_args_list if "FOR UPDATE" in str(c.args[0])]
		self.assertIn("`name` IN", sql)
		self.assertNotIn("status", sql.lower())
		frappe.db.rollback()

	def test_a_request_filed_while_acting_refuses_the_act(self):
		# Filed (here: a rejected one made Pending again, committed on another
		# connection) between the request locks and the skill row lock: the locking
		# re-read sees it, where a plain one would read the old snapshot.
		skill = self._skill(OWNER, "lock-moved")
		request = self._request(skill)
		self._reject(request)
		real_lock_row = skills_admin._lock_row

		def file_then_lock(name):
			with other_connection() as other:
				other.sql(f"UPDATE `tab{PROMO}` SET status = 'Pending' WHERE name = %s", request)
				other.commit()
			return real_lock_row(name)

		frappe.set_user(ADMIN)
		with patch.object(skills_admin, "_lock_row", side_effect=file_then_lock):
			with self.assertRaises(frappe.ValidationError):
				skills_admin_api.admin_hold(skill, "Too late")
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "admin_hold").admin_hold, 0)
		self.assertEqual(self._status(request).status, "Pending")


# --------------------------------------------------------------------------- #
# Who is told
# --------------------------------------------------------------------------- #
class TestNotices(HoldBase):
	def test_a_published_copy_tells_its_author_and_the_reviewers(self):
		source = self._skill(OWNER, "pub-src")
		copy = self._skill("Administrator", "pub-copy", scope="Role", source_skill=source)
		self._hold(copy)
		self.assertEqual(len(self._notes(OWNER)), 1)
		self.assertEqual(len(self._notes(REVIEWER)), 1)
		self.assertEqual(self._notes(ADMIN), [])  # never the admin who acted

	def test_a_copy_whose_source_is_gone_tells_the_reviewers_only(self):
		source = self._skill(OWNER, "gone-src")
		copy = self._skill("Administrator", "gone-copy", scope="Role", source_skill=source)
		frappe.delete_doc(SKILL, source, force=True, ignore_permissions=True)
		frappe.db.commit()
		self._hold(copy)
		self.assertEqual(self._notes(OWNER), [])
		self.assertEqual(len(self._notes(REVIEWER)), 1)

	def test_the_owners_list_carries_five_notices_until_dismissed(self):
		skills = [self._skill(OWNER, f"notice-{i}") for i in range(6)]
		for skill in skills:
			self._hold(skill)
		frappe.set_user(OWNER)
		notices = custom_skills_api.list_custom_skills_page(search=TAG)["notices"]
		self.assertEqual(len(notices), 5)
		self.assertEqual(set(notices[0]), {"name", "message", "creation"})
		self.assertEqual(custom_skills_api.dismiss_skill_notices([notices[0]["name"]])["dismissed"], 1)
		# Another user's notice is never touched.
		frappe.set_user(OTHER)
		self.assertEqual(custom_skills_api.dismiss_skill_notices([notices[1]["name"]])["dismissed"], 0)
		frappe.set_user(OWNER)
		# An empty list dismisses none; only an omitted one dismisses them all.
		self.assertEqual(custom_skills_api.dismiss_skill_notices([])["dismissed"], 0)
		self.assertEqual(custom_skills_api.dismiss_skill_notices("[]")["dismissed"], 0)
		self.assertEqual(custom_skills_api.dismiss_skill_notices()["dismissed"], 5)
		self.assertEqual(custom_skills_api.list_custom_skills_page(search=TAG)["notices"], [])

	def test_notices_ride_the_first_page_only_and_never_break_it(self):
		self._hold(self._skill(OWNER, "notice-page"))
		frappe.set_user(OWNER)
		self.assertEqual(len(custom_skills_api.list_custom_skills_page(search=TAG)["notices"]), 1)
		self.assertEqual(custom_skills_api.list_custom_skills_page(search=TAG, start=20)["notices"], [])
		with patch.object(skills_admin, "owner_notices", side_effect=RuntimeError("log down")):
			page = custom_skills_api.list_custom_skills_page(search=TAG)
		self.assertEqual(page["notices"], [])
		self.assertTrue(page["rows"])

	def test_dismiss_refuses_what_is_not_a_list_of_names(self):
		frappe.set_user(OWNER)
		for bad in ("not json", '{"a": 1}', [1], [""], 7):
			with self.subTest(names=bad):
				with self.assertRaises(frappe.ValidationError):
					custom_skills_api.dismiss_skill_notices(bad)
