"""Fresh actor ERP permissions override stale per-request and Redis caches."""

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT, as_user


class TestCollaborationPermissions(CollaborationCase):
	def snapshot(self):
		from jarvis.chat.collaboration_permissions import actor_permissions

		return actor_permissions(RECIPIENT)

	def test_removed_erp_role_cannot_survive_in_warm_role_cache(self):
		with as_user("Administrator"):
			frappe.get_doc("User", RECIPIENT).add_roles("Accounts User")
		with as_user(RECIPIENT):
			self.assertTrue(frappe.has_permission("Sales Invoice", "read"))
			self.assertIn("Accounts User", frappe.get_roles())
			frappe.db.delete("Has Role", {"parent": RECIPIENT, "role": "Accounts User"})
			# Deliberately keep the old cache warm: no User.save invalidation.
			self.assertIn("Accounts User", frappe.get_roles())
			with self.snapshot():
				self.assertNotIn("Accounts User", frappe.get_roles())
				self.assertFalse(frappe.has_permission("Sales Invoice", "read"))

	def test_snapshot_runs_as_recipient_and_does_not_borrow_owner_roles(self):
		with as_user("Administrator"):
			frappe.get_doc("User", OWNER).add_roles("Accounts User")
		with as_user(OWNER):
			self.assertTrue(frappe.has_permission("Sales Invoice", "read"))
			with self.snapshot():
				self.assertEqual(frappe.session.user, RECIPIENT)
				self.assertFalse(frappe.has_permission("Sales Invoice", "read"))
			self.assertEqual(frappe.session.user, OWNER)

	def test_new_user_permission_beats_a_cached_unrestricted_dictionary(self):
		with as_user("Administrator"):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": RECIPIENT,
					"allow": "User",
					"for_value": RECIPIENT,
					"applicable_for": "ToDo",
				}
			).insert(ignore_permissions=True)
		# Seed a stale value deliberately, as a request that started before the
		# restriction could do. The scoped snapshot must not trust it.
		frappe.cache.hset("user_permissions", RECIPIENT, {})
		with as_user(RECIPIENT):
			self.assertEqual(get_user_permissions(), {})
			with self.snapshot():
				permissions = get_user_permissions()
				self.assertEqual(permissions["User"][0]["doc"], RECIPIENT)
				self.assertEqual(permissions["User"][0]["applicable_for"], "ToDo")

	def test_normal_permissions_match_framework_output_without_cache_poisoning(self):
		with as_user(RECIPIENT):
			expected_roles = set(frappe.get_roles())
			expected_permissions = get_user_permissions()
			with self.snapshot():
				self.assertEqual(set(frappe.get_roles()), expected_roles)
				self.assertEqual(get_user_permissions(), expected_permissions)

	def test_permission_snapshot_is_scoped_and_does_not_publish_roles_to_redis(self):
		with as_user(RECIPIENT):
			before = frappe.cache.hget("roles", RECIPIENT, generator=lambda: [])
			with self.snapshot():
				self.assertIn("Jarvis User", frappe.get_roles())
			frappe.local.cache = {}
			self.assertEqual(frappe.cache.hget("roles", RECIPIENT), before)
