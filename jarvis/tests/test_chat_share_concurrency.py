"""Real connections exercise the conversation lock and unique recipient pair."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user, make_chat, make_user


class TestShareConcurrency(FrappeTestCase):
	def test_two_connections_adding_same_user_are_idempotent(self):
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
			if action == "read_after_role_removed":
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
				int(action == "read_after_role_removed"),
			)

			def other_request():
				frappe.init(site=site, sites_path=sites)
				frappe.connect()
				try:
					if action == "grant_after_delete":
						frappe.set_user("Administrator")
						frappe.delete_doc("User", recipient, ignore_permissions=True, force=True)
					elif action in ("grant_after_role_removed", "read_after_role_removed"):
						frappe.set_user("Administrator")
						frappe.get_doc("User", recipient).remove_roles("Jarvis User")
						assert frappe.db.get_value("User", recipient, "user_type") == "System User"
						assert not frappe.db.exists("Has Role", {"parent": recipient, "role": "Jarvis User"})
					else:
						frappe.set_user(owner)
						from jarvis.chat.sharing import set_shares

						set_shares(conversation, [recipient])
					frappe.db.commit()
				finally:
					frappe.db.rollback()
					frappe.destroy()

			with ThreadPoolExecutor(max_workers=1) as pool:
				pool.submit(other_request).result(timeout=30)
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
			if action == "archive":
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

				name = frappe.db.get_value(SHARE, {"conversation": conversation}, "name", for_update=True)
				with as_user("Administrator"):
					revoke_share(name)
			else:
				from jarvis.chat.sharing import set_shares

				with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
					set_shares(conversation, [recipient])
			frappe.db.commit()
			self.assertEqual(frappe.db.count(SHARE, {"conversation": conversation}), 0)
			if action == "archive":
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

	def test_archive_removes_grant_committed_after_its_snapshot(self):
		self._run_lifecycle_timeline("archive")

	def test_user_deletion_removes_grant_committed_after_its_snapshot(self):
		self._run_lifecycle_timeline("delete")

	def test_share_cannot_outlive_recipient_deleted_after_owner_snapshot(self):
		self._run_lifecycle_timeline("grant_after_delete")

	def test_owner_removal_sees_grant_committed_after_its_snapshot(self):
		self._run_lifecycle_timeline("owner_remove")

	def test_admin_revoke_sees_grant_committed_after_its_snapshot(self):
		self._run_lifecycle_timeline("admin_revoke")

	def test_share_refuses_recipient_role_removed_after_owner_snapshot(self):
		self._run_lifecycle_timeline("grant_after_role_removed")

	def test_old_owner_read_cannot_restore_revoked_access_through_roles_cache(self):
		self._run_lifecycle_timeline("read_after_role_removed")
