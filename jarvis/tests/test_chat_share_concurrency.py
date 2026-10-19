"""Real connections exercise the conversation lock and unique recipient pair."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user, make_chat, make_user
from jarvis.tests.race_harness import (
	assert_fix_closes_race,
	open_read_view,
	snapshot_isolation_off,
	snapshot_isolation_on,
)


class TestShareConcurrency(FrappeTestCase):
	def test_two_connections_adding_same_user_are_idempotent(self):
		self._duplicate_grants(snapshot_check=False)

	def _duplicate_grants(self, *, snapshot_check):
		self.assertNotIn(
			frappe.local.site, {"jarvis.localhost", "jarvis_admin.localhost", "jarvis_helpdesk.localhost"}
		)
		site = frappe.local.site
		previous = frappe.session.user
		setting_before = frappe.db.sql(
			"SELECT value FROM tabSingles WHERE doctype=%s AND field=%s",
			("Jarvis Settings", "chat_sharing_enabled"),
		)
		created = []
		conversation = None
		try:
			frappe.set_user("Administrator")
			for user in (OWNER, RECIPIENT):
				if not frappe.db.exists("User", user):
					created.append(user)
				make_user(user)
			conversation, _ = make_chat()
			frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
			# Worker connections must see committed fixtures. The dedicated test
			# site and finally cleanup make this a bounded transaction test.
			frappe.db.commit()
			sites = str(Path(frappe.get_site_path()).resolve().parent)
			barrier = Barrier(2)

			def add():
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				try:
					assert frappe.local.site == site
					frappe.set_user(OWNER)
					from jarvis.chat.sharing import set_shares

					with snapshot_isolation_on() if snapshot_check else snapshot_isolation_off():
						open_read_view()
						barrier.wait(timeout=10)
						result = set_shares(conversation, [RECIPIENT])
						frappe.db.commit()
						return result
				finally:
					frappe.db.rollback()
					frappe.destroy()

			with ThreadPoolExecutor(max_workers=2) as pool:
				futures = [pool.submit(add) for _ in range(2)]
				results = [future.result(timeout=30) for future in futures]
			frappe.db.rollback()
			self.assertEqual(frappe.db.count(SHARE, {"conversation": conversation}), 1)
			self.assertTrue(
				all([row["name"] for row in result["users"]] == [RECIPIENT] for result in results)
			)
		finally:
			with as_user("Administrator"):
				if conversation:
					frappe.delete_doc(
						"Jarvis Conversation", conversation, ignore_permissions=True, force=True
					)
				for user in created:
					frappe.delete_doc("User", user, ignore_permissions=True, force=True)
				if setting_before:
					frappe.db.set_single_value(
						"Jarvis Settings", "chat_sharing_enabled", setting_before[0][0]
					)
				else:
					frappe.db.delete(
						"Singles", {"doctype": "Jarvis Settings", "field": "chat_sharing_enabled"}
					)
				frappe.db.commit()
			frappe.set_user(previous)

	def _run_lifecycle_timeline(self, action):
		"""A request's old snapshot must not hide another request's committed grant."""
		self.assertNotIn(
			frappe.local.site, {"jarvis.localhost", "jarvis_admin.localhost", "jarvis_helpdesk.localhost"}
		)
		site = frappe.local.site
		sites = str(Path(frappe.get_site_path()).resolve().parent)
		owner = "share-lifecycle-owner@example.com"
		recipient = "share-lifecycle-recipient@example.com"
		previous = frappe.session.user
		before = frappe.db.sql(
			"SELECT value FROM tabSingles WHERE doctype=%s AND field=%s",
			("Jarvis Settings", "chat_sharing_enabled"),
		)
		conversation = None
		try:
			with as_user("Administrator"):
				self.assertFalse(frappe.db.exists("User", owner))
				self.assertFalse(frappe.db.exists("User", recipient))
				make_user(owner)
				make_user(recipient)
				if action in ("grant_after_role_removed", "read_after_role_removed"):
					# Keep a genuine System User after removing Jarvis access;
					# otherwise User.validate downgrades the account to Website User.
					frappe.get_doc("User", recipient).add_roles("Accounts User")
			conversation, _ = make_chat(owner)
			frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
			initial_share = action in (
				"read_after_role_removed",
				"read_marker",
				"read_prior_write",
				"delete_conflict",
			)
			if initial_share:
				from jarvis.chat.sharing import set_shares

				with as_user(owner):
					set_shares(conversation, [recipient])
			frappe.db.commit()
			frappe.set_user(owner)
			# Establish the lifecycle/owner request's repeatable-read snapshot.
			doc = frappe.get_doc("Jarvis Conversation", conversation)
			self.assertTrue(frappe.db.exists("User", recipient))
			self.assertEqual(
				frappe.db.count(SHARE, {"conversation": conversation}),
				int(initial_share),
			)

			def other_request():
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				try:
					result = None
					if action == "grant_after_delete":
						frappe.set_user("Administrator")
						frappe.delete_doc("User", recipient, ignore_permissions=True, force=True)
					elif action in ("grant_after_role_removed", "read_after_role_removed"):
						frappe.set_user("Administrator")
						frappe.get_doc("User", recipient).remove_roles("Jarvis User")
						assert frappe.db.get_value("User", recipient, "user_type") == "System User"
						assert not frappe.db.exists("Has Role", {"parent": recipient, "role": "Jarvis User"})
					elif action in ("read_marker", "read_prior_write"):
						name = frappe.db.get_value(SHARE, {"conversation": conversation}, "name")
						result = now_datetime() + timedelta(seconds=1)
						frappe.db.set_value(SHARE, name, "seen_marker", result, update_modified=False)
					elif action == "delete_conflict":
						name = frappe.db.get_value(SHARE, {"conversation": conversation}, "name")
						modified = frappe.db.get_value(SHARE, name, "modified")
						frappe.db.set_value(
							SHARE,
							name,
							"modified",
							get_datetime(modified) + timedelta(microseconds=1),
							update_modified=False,
						)
					else:
						frappe.set_user(owner)
						from jarvis.chat.sharing import set_shares

						set_shares(conversation, [recipient])
						result = frappe.db.get_value(SHARE, {"conversation": conversation}, "name")
					frappe.db.commit()
					return result
				finally:
					frappe.db.rollback()
					frappe.destroy()

			with ThreadPoolExecutor(max_workers=1) as pool:
				competing = pool.submit(other_request).result(timeout=30)
			if action in ("grant_after_role_removed", "read_after_role_removed"):
				# Force a miss in this request too; warming B's shared roles
				# cache would mask an ordinary-read stale Has Role snapshot.
				frappe.cache.hdel("roles", recipient)
			if action == "read_after_role_removed":
				from jarvis.chat.sharing import get_shares

				# A's in-flight owner read can see its old snapshot. It must
				# never let a fresh C request read through a poisoned roles cache.
				get_shares(conversation)

				def recipient_request():
					frappe.init(site=site, sites_path=sites)
					frappe.connect()
					try:
						frappe.set_user(recipient)
						from jarvis.chat.sharing import get_shared_conversation

						try:
							return get_shared_conversation(conversation)
						except frappe.PermissionError:
							return {"refused": "app_access"}
					finally:
						frappe.db.rollback()
						frappe.destroy()

				with ThreadPoolExecutor(max_workers=1) as pool:
					response = pool.submit(recipient_request).result(timeout=30)
				self.assertIn(response, ({"refused": "not_shared"}, {"refused": "app_access"}))
				return
			if action in ("read_marker", "read_prior_write"):
				from jarvis.chat.sharing import get_shared_conversation

				if action == "read_prior_write":
					frappe.db.set_value("Jarvis Conversation", conversation, "title", "UNCOMMITTED-READ")
					with as_user(recipient), self.assertRaises(frappe.QueryDeadlockError):
						get_shared_conversation(conversation)
					frappe.db.rollback()
					self.assertEqual(
						frappe.db.get_value("Jarvis Conversation", conversation, "title"), doc.title
					)
				with as_user(recipient):
					response = get_shared_conversation(conversation)
				self.assertEqual(response["conversation"]["name"], conversation)
				self.assertEqual(
					frappe.db.get_value(SHARE, {"conversation": conversation}, "seen_marker"),
					get_datetime(competing),
				)
				frappe.db.commit()
				return
			if action == "delete_conflict":
				frappe.db.set_value("Jarvis Conversation", conversation, "title", "UNCOMMITTED-DELETE")
				with as_user("Administrator"), self.assertRaises(frappe.QueryDeadlockError):
					frappe.delete_doc("User", recipient, ignore_permissions=True, force=True)
				frappe.db.rollback()
				self.assertEqual(frappe.db.get_value("Jarvis Conversation", conversation, "title"), doc.title)
				self.assertTrue(frappe.db.exists("User", recipient))
				self.assertEqual(frappe.db.count(SHARE, {"conversation": conversation}), 1)
				# A new request may retry the whole User deletion; a hook may
				# never replay its fragment after losing the caller's writes/locks.
				with as_user("Administrator"):
					frappe.delete_doc("User", recipient, ignore_permissions=True, force=True)
			elif action == "archive_endpoint":
				from jarvis.chat.api import archive_conversation

				archive_conversation(conversation)
			elif action == "archive":
				doc.status = "Archived"
				doc.save(ignore_permissions=True)
			elif action == "delete":
				with as_user("Administrator"):
					frappe.delete_doc("User", recipient, ignore_permissions=True, force=True)
			elif action == "owner_remove":
				from jarvis.chat.sharing import set_shares

				self.assertEqual(set_shares(conversation, [])["users"], [])
			elif action == "admin_revoke":
				from jarvis.chat.sharing import revoke_share

				with as_user("Administrator"):
					revoke_share(competing)
			else:
				from jarvis.chat.sharing import set_shares

				with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
					set_shares(conversation, [recipient])
			frappe.db.commit()
			self.assertEqual(frappe.db.count(SHARE, {"conversation": conversation}), 0)
			if action in ("archive", "archive_endpoint"):
				doc = frappe.get_doc("Jarvis Conversation", conversation)
				doc.status = "Active"
				doc.save(ignore_permissions=True)
				frappe.db.commit()
				from jarvis.chat.sharing import is_shared_with_me

				with as_user(recipient):
					self.assertFalse(is_shared_with_me(conversation))
		finally:
			frappe.db.rollback()
			with as_user("Administrator"):
				if conversation and frappe.db.exists("Jarvis Conversation", conversation):
					frappe.delete_doc(
						"Jarvis Conversation", conversation, ignore_permissions=True, force=True
					)
				for user in (recipient, owner):
					if frappe.db.exists("User", user):
						frappe.delete_doc("User", user, ignore_permissions=True, force=True)
				if before:
					frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", before[0][0])
				else:
					frappe.db.delete(
						"Singles", {"doctype": "Jarvis Settings", "field": "chat_sharing_enabled"}
					)
				frappe.db.commit()
			frappe.set_user(previous)

	def _legacy_timeline(self, action):
		# These cases pin current-lock semantics without engine snapshot
		# checking. The explicit ON-mode tests below cover rejection/replay.
		with snapshot_isolation_off():
			self._run_lifecycle_timeline(action)

	def test_archive_removes_grant_committed_after_its_snapshot(self):
		self._legacy_timeline("archive")

	def test_user_deletion_removes_grant_committed_after_its_snapshot(self):
		self._legacy_timeline("delete")

	def test_share_cannot_outlive_recipient_deleted_after_owner_snapshot(self):
		self._legacy_timeline("grant_after_delete")

	def test_owner_removal_sees_grant_committed_after_its_snapshot(self):
		self._legacy_timeline("owner_remove")

	def test_admin_revoke_sees_grant_committed_after_its_snapshot(self):
		self._legacy_timeline("admin_revoke")

	def test_share_refuses_recipient_role_removed_after_owner_snapshot(self):
		self._legacy_timeline("grant_after_role_removed")

	def test_old_owner_read_cannot_restore_revoked_access_through_roles_cache(self):
		self._legacy_timeline("read_after_role_removed")

	def test_snapshot_duplicate_grants_replay_from_the_conversation_lock(self):
		assert_fix_closes_race(self, lambda: self._duplicate_grants(snapshot_check=True))

	def test_snapshot_owner_removal_replays_the_whole_diff(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("owner_remove"))

	def test_snapshot_admin_revoke_replays_its_whole_unit(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("admin_revoke"))

	def test_snapshot_deleted_recipient_is_revalidated_after_replay(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("grant_after_delete"))

	def test_snapshot_revoked_role_is_revalidated_after_replay(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("grant_after_role_removed"))

	def test_snapshot_transcript_read_replays_and_preserves_newer_seen_marker(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("read_marker"))

	def test_snapshot_archive_uses_existing_whole_operation_replay(self):
		with snapshot_isolation_on():
			assert_fix_closes_race(self, lambda: self._run_lifecycle_timeline("archive_endpoint"))

	def test_snapshot_read_conflict_must_not_replay_after_a_callers_write(self):
		with snapshot_isolation_on():
			self._run_lifecycle_timeline("read_prior_write")

	def test_snapshot_user_delete_conflict_rolls_back_all_the_callers_work(self):
		with snapshot_isolation_on():
			self._run_lifecycle_timeline("delete_conflict")
