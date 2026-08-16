"""Tests for the role -> agent-profile fixture and resolver, and the
compute-and-push-to-admin sync (spec
docs/superpowers/specs/2026-08-16-role-profile-agents-design.md).

Run ONLY with --case (bare --module silently skips TestCases), and pair it
with --module: --case alone walks every test file in the app and errors on
the first module lacking the class, rather than finding this one.
  bench --site site.jarvis run-tests --app jarvis --module jarvis.tests.test_role_profiles --case TestRoleProfiles
  bench --site site.jarvis run-tests --app jarvis --module jarvis.tests.test_role_profiles --case TestSyncRoleProfiles
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import role_profiles


class TestRoleProfiles(FrappeTestCase):
	def _mk_user(self, email, roles):
		if not frappe.db.exists("User", email):
			u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0]})
			u.append_roles(*roles)
			u.insert(ignore_permissions=True)
		return email

	def test_admin_roles_pin_main(self):
		u = self._mk_user("rp-admin@example.com", ["Jarvis User", "System Manager"])
		c = role_profiles.resolve_profile(u)
		self.assertIsNone(c.agent_id)
		self.assertEqual(c.tier, "full")

	def test_single_role_maps_to_set(self):
		u = self._mk_user("rp-hr@example.com", ["Jarvis User", "HR User"])
		c = role_profiles.resolve_profile(u)
		self.assertEqual(c.agent_id, "role-hr")
		self.assertIn("hrms-hr", c.skills)
		self.assertIn("frappe-core", c.skills)  # shared core present
		self.assertNotIn("erpnext-accounts", c.skills)  # other domains absent

	def test_multi_role_merges_sorted(self):
		u = self._mk_user("rp-multi@example.com", ["Jarvis User", "HR User", "Accounts User"])
		c = role_profiles.resolve_profile(u)
		self.assertEqual(c.agent_id, "role-accounts+hr")  # sorted set keys
		self.assertIn("hrms-payroll", c.skills)
		self.assertIn("erpnext-accounts", c.skills)

	def test_unmapped_roles_fall_back_to_main(self):
		u = self._mk_user("rp-none@example.com", ["Jarvis User", "Translator"])
		c = role_profiles.resolve_profile(u)
		self.assertIsNone(c.agent_id)

	def test_resolution_error_falls_back_to_main(self):
		c = role_profiles.resolve_profile(None)  # type: ignore[arg-type]
		self.assertIsNone(c.agent_id)

	def test_resolution_exception_falls_back_to_main(self):
		# frappe.get_roles(None) resolves to the session user rather than
		# raising, so the test above exercises the deterministic `not user`
		# guard, not the except branch. Force a genuine exception here to
		# cover the "absolute" fallback rule (spec §3 rule 3) for real.
		u = self._mk_user("rp-boom@example.com", ["Jarvis User", "HR User"])
		with patch.object(role_profiles.frappe, "get_roles", side_effect=RuntimeError("boom")):
			c = role_profiles.resolve_profile(u)
		self.assertIsNone(c.agent_id)
		self.assertEqual(c.tier, "full")

	def test_standard_tools_allow_excludes_drops_keeps_features(self):
		allow = set(role_profiles.standard_tools_allow())
		self.assertEqual(len(allow), 68)
		for kept in (
			"exec",
			"read",
			"canvas",
			"pdf",
			"image",
			"jarvis__read_wiki",
			"jarvis__update_wiki",
			"jarvis__create_custom_skill",
			"jarvis__run_import",
			"jarvis__query",
			"jarvis__save_dashboard",
		):
			self.assertIn(kept, allow)
		for dropped in (
			"cron",
			"browser",
			"web_search",
			"sessions_spawn",
			"jarvis__record_agent_run",
			"skill_workshop",
		):
			self.assertNotIn(dropped, allow)

	def test_tool_universe_is_94(self):
		# standard_tools_allow() (68) + STANDARD_DROP_TOOLS (26) must
		# reconstruct the full evidence-captured 94-tool universe with no
		# overlap and no gap (spec §2).
		allow = set(role_profiles.standard_tools_allow())
		drop = set(role_profiles.STANDARD_DROP_TOOLS)
		self.assertEqual(len(drop), 26)
		self.assertEqual(allow & drop, set())
		self.assertEqual(len(allow | drop), 94)

	def test_shared_core_membership(self):
		shared = role_profiles.SHARED_CORE_SKILLS
		self.assertEqual(len(shared), 28)
		# all frappe-* and jarvis-* skills are shared core
		self.assertEqual(len([s for s in shared if s.startswith("frappe-")]), 11)
		self.assertEqual(len([s for s in shared if s.startswith("jarvis-")]), 9)
		# named additions from spec §3
		for slug in (
			"erpnext-setup",
			"erpnext-utilities",
			"erpnext-regional",
			"erpnext-communication",
			"erpnext-integrations",
			"erpnext-bulk-transaction",
			"ocr-data-entry",
			"macro-merge",
		):
			self.assertIn(slug, shared)
		# role-set-claimed skills are NOT in shared core
		for slug in ("erpnext-accounts", "hrms-hr", "erpnext-projects", "erpnext-selling"):
			self.assertNotIn(slug, shared)
		# shared core and role-set skills never overlap, and their union is
		# every skill any of the 6 role sets can add
		claimed = set()
		for skills in role_profiles.SKILL_SETS.values():
			claimed |= skills
		self.assertEqual(shared & claimed, set())
		self.assertEqual(len(shared | claimed), 45)


class TestSyncRoleProfiles(FrappeTestCase):
	"""``jarvis.chat.role_profiles.sync_role_profiles`` - the compute-then-
	push-to-admin boundary (spec J2 / task-J2-brief.md).

	``needed_profiles`` is patched to a fixed fixture so these tests are
	isolated from whatever real ``Jarvis User``-holding users already exist
	on the shared test site (created by ``TestRoleProfiles`` above, or by
	prior runs).

	The Jarvis Settings snapshot fields (``role_profiles_pushed`` /
	``role_profiles_pushed_at``) are stood in with an in-memory fake behind
	``frappe.db.get_single_value`` / ``frappe.db.set_value`` rather than
	written to the real Single doctype: those two columns only exist once a
	``bench migrate`` has run for this DocType JSON change, and this shared
	bench points at a live production tenancy, so exercising the snapshot
	*logic* does not need to depend on that migration having been run here.
	"""

	@staticmethod
	def _fixture():
		return [{"slug": "role-hr", "skills": ["hrms-hr"], "tools_allow": ["exec"]}]

	@staticmethod
	def _fake_settings_store(initial=None):
		"""Returns ``(store, get_single_value, set_value)``: a plain dict plus
		two callables shaped like the real ``frappe.db`` methods, scoped to
		just the two fields ``sync_role_profiles`` touches."""
		store = dict(initial or {})

		def fake_get_single_value(doctype, fieldname, cache=True):
			assert doctype == role_profiles._SETTINGS
			return store.get(fieldname)

		def fake_set_value(doctype, name, values, *args, **kwargs):
			assert doctype == role_profiles._SETTINGS
			assert name == role_profiles._SETTINGS
			store.update(values)

		return store, fake_get_single_value, fake_set_value

	def test_first_sync_pushes_and_stamps_settings(self):
		store, fake_get, fake_set = self._fake_settings_store()
		with (
			patch.object(role_profiles, "needed_profiles", return_value=self._fixture()),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch("jarvis.admin_client.post_push_role_profiles", return_value={"ok": True}) as mock_push,
		):
			result = role_profiles.sync_role_profiles()

		self.assertTrue(result["pushed"])
		self.assertEqual(result["profiles"], self._fixture())
		mock_push.assert_called_once_with(role_profiles=self._fixture())
		self.assertEqual(frappe.parse_json(store["role_profiles_pushed"]), self._fixture())
		self.assertIsNotNone(store.get("role_profiles_pushed_at"))

	def test_unchanged_second_call_is_a_noop(self):
		store, fake_get, fake_set = self._fake_settings_store()
		with (
			patch.object(role_profiles, "needed_profiles", return_value=self._fixture()),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch("jarvis.admin_client.post_push_role_profiles", return_value={"ok": True}) as mock_push,
		):
			first = role_profiles.sync_role_profiles()
			mock_push.reset_mock()
			second = role_profiles.sync_role_profiles()

		self.assertTrue(first["pushed"])
		self.assertFalse(second["pushed"])
		self.assertEqual(second["profiles"], self._fixture())
		mock_push.assert_not_called()

	def test_force_repushes_even_when_unchanged(self):
		store, fake_get, fake_set = self._fake_settings_store()
		with (
			patch.object(role_profiles, "needed_profiles", return_value=self._fixture()),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch("jarvis.admin_client.post_push_role_profiles", return_value={"ok": True}) as mock_push,
		):
			role_profiles.sync_role_profiles()
			mock_push.reset_mock()
			result = role_profiles.sync_role_profiles(force=True)

		self.assertTrue(result["pushed"])
		mock_push.assert_called_once_with(role_profiles=self._fixture())

	def test_push_exception_returns_pushed_false_and_never_raises(self):
		store, fake_get, fake_set = self._fake_settings_store()
		with (
			patch.object(role_profiles, "needed_profiles", return_value=self._fixture()),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch(
				"jarvis.admin_client.post_push_role_profiles",
				side_effect=RuntimeError("admin unreachable"),
			),
		):
			before = dict(store)
			result = role_profiles.sync_role_profiles()  # must not raise
			after = dict(store)

		self.assertFalse(result["pushed"])
		self.assertEqual(result["profiles"], self._fixture())
		self.assertEqual(before, after)
