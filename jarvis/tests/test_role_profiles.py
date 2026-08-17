"""Tests for the role -> agent-profile fixture and resolver, and the
compute-and-push-to-admin sync (spec
docs/superpowers/specs/2026-08-16-role-profile-agents-design.md).

Run ONLY with --case (bare --module silently skips TestCases), and pair it
with --module: --case alone walks every test file in the app and errors on
the first module lacking the class, rather than finding this one.
  bench --site site.jarvis run-tests --app jarvis --module jarvis.tests.test_role_profiles --case TestRoleProfiles
  bench --site site.jarvis run-tests --app jarvis --module jarvis.tests.test_role_profiles --case TestSyncRoleProfiles
  bench --site site.jarvis run-tests --app jarvis --module jarvis.tests.test_role_profiles --case TestSessionProfilePick
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import api as chat_api
from jarvis.chat import role_profiles

SESSION = "Jarvis Chat Session"


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

	def test_needed_profiles_order_stable_across_differently_ordered_user_listings(self):
		"""Fold-in from the J2 review: guards the sort fix in
		``role_profiles.needed_profiles``. Two ``frappe.get_all`` "User"
		listings that differ ONLY in row order must still produce the
		identical (list-equal) ``needed_profiles()`` output, since
		``sync_role_profiles``'s snapshot compare is a plain ``==`` against
		the previously pushed list - an order-only difference must never read
		as "changed"."""

		def _resolve(user):
			if user == "user-hr":
				return role_profiles.ProfileChoice(
					agent_id="role-hr",
					tier="standard",
					set_keys=("hr",),
					skills=("hrms-hr",),
					n_tools=1,
				)
			return role_profiles.ProfileChoice(
				agent_id="role-accounts",
				tier="standard",
				set_keys=("accounts",),
				skills=("erpnext-accounts",),
				n_tools=1,
			)

		def _make_get_all(user_order):
			def _get_all(doctype, filters=None, pluck=None, **kwargs):
				if doctype == "Has Role":
					return ["user-hr", "user-accounts"]
				return list(user_order)

			return _get_all

		with patch.object(role_profiles, "resolve_profile", side_effect=_resolve):
			with patch.object(frappe, "get_all", side_effect=_make_get_all(["user-hr", "user-accounts"])):
				first = role_profiles.needed_profiles()
			with patch.object(frappe, "get_all", side_effect=_make_get_all(["user-accounts", "user-hr"])):
				second = role_profiles.needed_profiles()

		self.assertEqual(first, second)
		self.assertEqual([p["slug"] for p in first], ["role-accounts", "role-hr"])


class TestRoleProfileConfigSync(FrappeTestCase):
	"""``jarvis.chat.role_profiles.sync_role_profile_config`` - the admin-pull
	+ cache + mirror + conditional re-push boundary (task BJ1).

	Mirrors ``TestSyncRoleProfiles`` above: the three new Settings fields
	(``role_profiles_config`` / ``_version`` / ``_synced_at``) only exist once
	a ``bench migrate`` has run for the DocType JSON change in this task, and
	this shared bench points at a live production tenancy, so the Settings
	Single is stood in with a plain dict behind ``frappe.db.get_single_value``
	/ ``set_value`` / ``set_single_value`` rather than written for real.
	"""

	@staticmethod
	def _config(version="v1", enabled=True, skill="hrms-hr"):
		return {
			"version": version,
			"enabled": enabled,
			"sets": {"hr": [skill]},
			"shared_core": ["frappe-core"],
			"mappings": {"HR User": "hr"},
			"tool_tiers": {"standard": {"allow": ["exec", "read"], "core": ["exec"]}},
		}

	@staticmethod
	def _fake_store(initial=None):
		"""Same shape as ``TestSyncRoleProfiles._fake_settings_store``, extended
		with a ``set_single_value`` fake for the ``enable_role_profiles`` mirror
		write."""
		store = dict(initial or {})

		def fake_get_single_value(doctype, fieldname, cache=True):
			assert doctype == role_profiles._SETTINGS
			return store.get(fieldname)

		def fake_set_value(doctype, name, values, *args, **kwargs):
			assert doctype == role_profiles._SETTINGS
			assert name == role_profiles._SETTINGS
			store.update(values)

		def fake_set_single_value(doctype, fieldname, value, *args, **kwargs):
			assert doctype == role_profiles._SETTINGS
			store[fieldname] = value

		return store, fake_get_single_value, fake_set_value, fake_set_single_value

	def test_success_caches_mirrors_and_pushes_on_version_change(self):
		store, fake_get, fake_set, fake_set_single = self._fake_store()
		resp = {"ok": True, "data": self._config(version="v1", enabled=True)}
		calls = []
		with (
			patch("jarvis.admin_client.get_role_profile_config", return_value=resp),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch.object(frappe.db, "set_single_value", side_effect=fake_set_single),
			patch.object(
				role_profiles, "_invalidate_config_cache", side_effect=lambda: calls.append("invalidate")
			),
			patch.object(
				role_profiles, "sync_role_profiles", side_effect=lambda **kw: calls.append(("sync", kw))
			),
		):
			result = role_profiles.sync_role_profile_config()

		self.assertEqual(result, {"synced": True})
		self.assertEqual(frappe.parse_json(store["role_profiles_config"])["version"], "v1")
		self.assertEqual(store["role_profiles_config_version"], "v1")
		self.assertIsNotNone(store.get("role_profiles_config_synced_at"))
		self.assertEqual(store["enable_role_profiles"], 1)
		# Invalidation must happen BEFORE the forced re-push, so the push
		# recomputes needed_profiles() from the NEW config, not a value BJ2's
		# accessor memoized earlier in this same request.
		self.assertEqual(calls, ["invalidate", ("sync", {"force": True})])

	def test_unchanged_version_does_not_repush(self):
		store, fake_get, fake_set, fake_set_single = self._fake_store(
			initial={"role_profiles_config_version": "v1"}
		)
		resp = {"ok": True, "data": self._config(version="v1", enabled=True)}
		with (
			patch("jarvis.admin_client.get_role_profile_config", return_value=resp),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch.object(frappe.db, "set_single_value", side_effect=fake_set_single),
			patch.object(role_profiles, "_invalidate_config_cache") as mock_invalidate,
			patch.object(role_profiles, "sync_role_profiles") as mock_sync,
		):
			result = role_profiles.sync_role_profile_config()

		self.assertEqual(result, {"synced": True})
		mock_invalidate.assert_not_called()
		mock_sync.assert_not_called()
		self.assertEqual(store["enable_role_profiles"], 1)

	def test_pull_failure_with_existing_cache_keeps_cache_and_mirror(self):
		store, fake_get, fake_set, fake_set_single = self._fake_store(
			initial={
				"role_profiles_config": frappe.as_json(self._config(version="v0")),
				"role_profiles_config_version": "v0",
				"enable_role_profiles": 1,
			}
		)
		with (
			patch(
				"jarvis.admin_client.get_role_profile_config", side_effect=RuntimeError("admin unreachable")
			),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch.object(frappe.db, "set_single_value", side_effect=fake_set_single),
		):
			before = dict(store)
			result = role_profiles.sync_role_profile_config()
			after = dict(store)

		self.assertEqual(result, {"synced": False})
		self.assertEqual(before, after)

	def test_pull_failure_with_no_cache_mirrors_off(self):
		store, fake_get, fake_set, fake_set_single = self._fake_store(
			initial={"enable_role_profiles": 1}  # stale ON from a prior tenancy; no cache present
		)
		with (
			patch(
				"jarvis.admin_client.get_role_profile_config", side_effect=RuntimeError("admin unreachable")
			),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch.object(frappe.db, "set_single_value", side_effect=fake_set_single),
		):
			result = role_profiles.sync_role_profile_config()

		self.assertEqual(result, {"synced": False})
		self.assertEqual(store["enable_role_profiles"], 0)

	def test_invalid_payload_is_failure_path_and_leaves_cache_untouched(self):
		store, fake_get, fake_set, fake_set_single = self._fake_store()
		bad = {"ok": True, "data": {"version": "v1"}}  # missing enabled/sets/mappings/tool_tiers
		with (
			patch("jarvis.admin_client.get_role_profile_config", return_value=bad),
			patch.object(frappe.db, "get_single_value", side_effect=fake_get),
			patch.object(frappe.db, "set_value", side_effect=fake_set),
			patch.object(frappe.db, "set_single_value", side_effect=fake_set_single),
		):
			result = role_profiles.sync_role_profile_config()

		self.assertEqual(result, {"synced": False})
		self.assertNotIn("role_profiles_config", store)
		self.assertEqual(store.get("enable_role_profiles", 0), 0)

	def test_invalidate_config_cache_is_safe_noop_when_nothing_memoized(self):
		role_profiles._invalidate_config_cache()  # must not raise

	def test_validate_rejects_bad_skill_slug_charset(self):
		bad = self._config()
		bad["sets"]["hr"] = ["HRMS_HR"]  # uppercase/underscore not allowed
		self.assertFalse(role_profiles._validate_role_profile_config(bad))

	def test_validate_rejects_core_tool_not_in_allow(self):
		bad = self._config()
		bad["tool_tiers"]["standard"]["core"] = ["exec", "not-allowed-tool"]
		self.assertFalse(role_profiles._validate_role_profile_config(bad))

	def test_validate_rejects_missing_key(self):
		bad = self._config()
		del bad["tool_tiers"]
		self.assertFalse(role_profiles._validate_role_profile_config(bad))

	def test_validate_accepts_well_formed_config(self):
		self.assertTrue(role_profiles._validate_role_profile_config(self._config()))


class FakeSess:
	"""Stub pooled ``AgentSession``: counts ``create_session`` calls so tests
	can assert the flag-gated legacy branch was (or was not) taken."""

	def __init__(self):
		self.calls = 0

	def create_session(self, label):
		self.calls += 1
		return "sess-key-1"


class TestSessionProfilePick(FrappeTestCase):
	"""``jarvis.chat.api._ensure_session_key`` flag-gated profile pick (task
	J3 / spec docs/superpowers/specs/2026-08-16-role-profile-agents-design.md
	§9).

	``_ensure_session_key`` ends with a real ``frappe.db.commit()`` (it
	always has - unrelated to this task), so - like
	``jarvis.tests.test_pump_pipeline``'s ``_PipelineCase`` - these tests
	call the real function and clean up their own committed rows in
	``tearDown`` rather than relying on ``FrappeTestCase``'s per-test
	savepoint rollback, which a real commit bypasses.

	``Jarvis Settings`` is stood in with a ``SimpleNamespace`` behind
	``frappe.get_single`` (mirrors ``TestSyncRoleProfiles``'s fake-store
	approach, adapted for the doc-attribute access ``_ensure_session_key``
	uses instead of ``frappe.db.get_single_value``); no real Settings row is
	touched.
	"""

	_USER = "rp-session-pick@example.com"

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", self._USER):
			u = frappe.get_doc({"doctype": "User", "email": self._USER, "first_name": "rp-session-pick"})
			u.append_roles("Jarvis User", "HR User")
			u.insert(ignore_permissions=True)

	def tearDown(self):
		# Real commits happened inside _ensure_session_key; undo them for
		# real rather than relying on the (already-bypassed) savepoint. Best-
		# effort (mirrors test_pump_pipeline._PipelineCase): a setUp failure
		# must surface as ITS OWN error, not get masked by a teardown
		# DoesNotExistError on a user that was never created.
		try:
			frappe.db.delete(SESSION, {"user": self._USER})
			if frappe.db.exists("User", self._USER):
				frappe.delete_doc("User", self._USER, force=True, ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			pass
		super().tearDown()

	@staticmethod
	def _fake_settings(*, enable_role_profiles, pushed=None):
		return SimpleNamespace(
			enable_role_profiles=enable_role_profiles,
			role_profiles_pushed=frappe.as_json(pushed) if pushed is not None else None,
			chat_device_id="dev-1",
			agent_url="ws://fake-agent",
			get_password=lambda *a, **k: "fake-token",
		)

	def test_flag_on_pushed_snapshot_has_role_self_addresses_the_agent(self):
		settings = self._fake_settings(
			enable_role_profiles=True,
			pushed=[{"slug": "role-hr", "skills": ["hrms-hr"], "tools_allow": ["exec"]}],
		)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess, profile=True)

		# ":dashboard:" is the chat-namespace marker session_lifecycle.py's
		# idle/orphan reaper and session_pin_sweep.py both key off - a key
		# missing it would leak this gateway session forever.
		self.assertTrue(key.startswith("agent:role-hr:dashboard:jarvis-chat-"))
		self.assertEqual(sess.calls, 0)
		row = frappe.get_doc(SESSION, {"session_key": key})
		self.assertEqual(row.profile_agent_id, "role-hr")
		self.assertEqual(row.profile_tier, "standard")
		self.assertEqual(row.profile_n_tools, 68)

	def test_flag_off_uses_legacy_create_session_path(self):
		settings = self._fake_settings(enable_role_profiles=False)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess, profile=True)

		self.assertEqual(sess.calls, 1)
		self.assertEqual(key, "sess-key-1")
		row = frappe.get_doc(SESSION, {"session_key": key})
		self.assertEqual(row.profile_tier, "full")
		self.assertEqual(row.profile_sets, "")
		self.assertEqual(row.profile_n_tools, 0)

	def test_flag_on_agent_not_in_pushed_snapshot_falls_back_to_legacy(self):
		# resolve_profile(self._USER) resolves to role-hr (real HR User role),
		# but the pushed snapshot only carries a DIFFERENT profile - never
		# self-address an agent that may not be rendered on the agent side.
		settings = self._fake_settings(
			enable_role_profiles=True,
			pushed=[{"slug": "role-accounts", "skills": [], "tools_allow": []}],
		)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess, profile=True)

		self.assertEqual(sess.calls, 1)
		self.assertEqual(key, "sess-key-1")

	def test_macro_style_call_profile_false_still_uses_legacy_even_if_resolvable(self):
		# Coordinator review finding #2 (spec §8 - non-chat sessions always
		# run `full`): a macro/scheduled dispatch never passes profile=True
		# (jarvis.chat.macros -> api._enqueue_turn's session-key call keeps
		# the default). Even with the flag on and a fully resolvable, pushed
		# profile, `profile=False` (the default - this call omits the kwarg
		# entirely, exactly like a macro caller would) must still take the
		# legacy create_session path, because macros.py's own dispatch
		# try/except relies on this function still THROWING on a
		# misconfigured agent rather than silently minting an unusable key.
		settings = self._fake_settings(
			enable_role_profiles=True,
			pushed=[{"slug": "role-hr", "skills": ["hrms-hr"], "tools_allow": ["exec"]}],
		)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess)  # profile omitted -> False

		self.assertEqual(sess.calls, 1)
		self.assertEqual(key, "sess-key-1")
		row = frappe.get_doc(SESSION, {"session_key": key})
		self.assertEqual(row.profile_tier, "full")

	def test_profile_true_missing_gateway_config_still_throws(self):
		# Coordinator review finding #2: the profile (self-addressed) path
		# skips create_session/sessions.create entirely, but a totally
		# unconfigured agent must still fail fast here - never silently mint
		# a session key nothing can ever connect to.
		settings = self._fake_settings(
			enable_role_profiles=True,
			pushed=[{"slug": "role-hr", "skills": ["hrms-hr"], "tools_allow": ["exec"]}],
		)
		settings.agent_url = ""
		settings.get_password = lambda *a, **k: ""
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			with self.assertRaises(frappe.ValidationError):
				chat_api._ensure_session_key(self._USER, sess=FakeSess(), profile=True)

	def test_missing_flag_attribute_degrades_to_legacy_not_a_crash(self):
		# Regression: a site whose code deployed ahead of its reload-doctype /
		# migrate has no `enable_role_profiles` attribute on the real Jarvis
		# Settings doc at all (AttributeError, not a falsy value) - caught
		# live via jarvis.tests.test_pump_pipeline.TestPumpPipelineE2E before
		# the getattr guard was added. A missing flag must behave exactly
		# like flag-off, never break session creation. profile=True so the
		# `getattr` guard (short-circuited behind `if profile and ...`) is
		# actually exercised.
		settings = SimpleNamespace(
			chat_device_id="dev-1",
			agent_url="ws://fake-agent",
			get_password=lambda *a, **k: "fake-token",
		)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess, profile=True)

		self.assertEqual(sess.calls, 1)
		self.assertEqual(key, "sess-key-1")
		row = frappe.get_doc(SESSION, {"session_key": key})
		self.assertEqual(row.profile_tier, "full")

	def test_missing_pushed_snapshot_attribute_degrades_to_legacy(self):
		# Narrower sibling of the missing-flag test above: the flag itself IS
		# present and on, but `role_profiles_pushed` is missing entirely (same
		# metadata-lag scenario, different field) - _pushed_profile_slugs'
		# own getattr guard must return an empty set rather than raise, which
		# routes to the legacy path exactly like an empty/unparseable snapshot
		# already does.
		settings = SimpleNamespace(
			enable_role_profiles=True,
			chat_device_id="dev-1",
			agent_url="ws://fake-agent",
			get_password=lambda *a, **k: "fake-token",
		)
		sess = FakeSess()
		with patch.object(chat_api.frappe, "get_single", return_value=settings):
			key = chat_api._ensure_session_key(self._USER, sess=sess, profile=True)

		self.assertEqual(sess.calls, 1)
		self.assertEqual(key, "sess-key-1")

	def test_scrub(self):
		# scrub is jarvis.chat.entities.scrub, imported (not duplicated) into
		# api.py; assert it through its use in the self-addressed key shape.
		self.assertEqual(chat_api.scrub("RP-HR@Example.com"), "rp-hr-example-com")
