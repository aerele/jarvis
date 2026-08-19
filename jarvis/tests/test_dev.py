"""Tests for jarvis.dev - the customer-side reset_onboarding endpoint.

Sandbox mode (Jarvis Settings.sandbox_mode) and jarvis.dev.is_sandbox_mode /
_dev_guard / is_dev_mode_active were removed as a dead feature. reset_onboarding
now gates on System Manager alone via frappe.only_for, which was always the
real security boundary (sandbox mode was documented as self-attested UX, not
hardening)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.dev import (
	_PASSWORD_FIELDS,
	_RESET_CLEAR_FIELDS,
	_RESET_DEFAULT_FIELDS,
	_RESET_LITERAL_DEFAULTS,
	_RESET_NULL_FIELDS,
	_RESET_ZERO_FIELDS,
	reset_onboarding,
)
from jarvis.tests._role_guard import enforced_role_guards

SETTINGS = "Jarvis Settings"


def _read(s, field):
	"""Read a Jarvis Settings field, password-safe."""
	if field in _PASSWORD_FIELDS:
		return s.get_password(field, raise_exception=False) or ""
	return s.get(field) or ""


def _snapshot():
	"""Snapshot every reset-affected field so tests run against a real site
	(not test_site) don't trash the operator's actual onboarded state."""
	s = frappe.get_single(SETTINGS)
	defaults = (*_RESET_DEFAULT_FIELDS, *_RESET_LITERAL_DEFAULTS)
	snap = {f: _read(s, f) for f in (*_RESET_CLEAR_FIELDS, *defaults)}
	# Raw, not via _read: a NULL datetime must restore as NULL, not "". llm_auth_mode
	# is reqd on the Single, so a blank one left behind makes the next full .save()
	# anywhere in the shard die with MandatoryError.
	snap.update({f: s.get(f) for f in (*_RESET_NULL_FIELDS, *_RESET_ZERO_FIELDS)})
	return snap


def _restore(snap):
	s = frappe.get_single(SETTINGS)
	for f, v in snap.items():
		s.db_set(f, v)
	frappe.db.commit()


def _seed_onboarded_state():
	"""Plant non-empty values in every field reset_onboarding will clear."""
	s = frappe.get_single(SETTINGS)
	for f in _RESET_CLEAR_FIELDS:
		s.db_set(f, f"seed-{f}")
	for f in _RESET_NULL_FIELDS:
		s.db_set(f, "2026-01-01 00:00:00")
	for f in _RESET_ZERO_FIELDS:
		s.db_set(f, 1)
	for f in (*_RESET_DEFAULT_FIELDS, *_RESET_LITERAL_DEFAULTS):
		s.db_set(f, "oauth" if f == "llm_auth_mode" else "OpenAI")
	frappe.db.commit()


class TestResetOnboarding(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot()
		_seed_onboarded_state()

	def tearDown(self):
		_restore(self._snap)

	def test_clears_all_targeted_fields(self):
		out = reset_onboarding()
		self.assertTrue(out["ok"])
		s = frappe.get_single(SETTINGS)
		for f in _RESET_CLEAR_FIELDS:
			self.assertEqual(_read(s, f), "", f"{f} should be blank after reset")
		for f in _RESET_NULL_FIELDS:
			self.assertIsNone(s.get(f), f"{f} should be NULL after reset, not an empty string")
		for f in _RESET_ZERO_FIELDS:
			self.assertIn(int(s.get(f) or 0), (0,), f"{f} should be 0 after reset")
		# These go back to a default rather than blank.
		self.assertEqual(s.llm_provider, "Anthropic")
		self.assertEqual(s.llm_auth_mode, frappe.get_meta(SETTINGS).get_field("llm_auth_mode").default)

	def test_settings_can_still_be_saved_after_a_reset(self):
		"""llm_auth_mode is reqd and db_set skips validation, so blanking it used to
		leave the Single unsaveable - surfacing as MandatoryError in unrelated code."""
		reset_onboarding()
		frappe.get_single(SETTINGS).save()  # must not raise MandatoryError

	def test_clears_the_credential_the_bench_actually_authenticates_with(self):
		"""admin_client prefers the OAuth password grant over the api-key pair, so a
		reset that leaves it still reaches the control plane as the old customer."""
		reset_onboarding()
		s = frappe.get_single(SETTINGS)
		self.assertEqual(s.jarvis_admin_customer_email or "", "")
		self.assertEqual(s.get_password("jarvis_admin_customer_password", raise_exception=False) or "", "")
		self.assertIn("jarvis_admin_customer_password", _PASSWORD_FIELDS, "__Auth row must be dropped too")

	def test_preserves_unrelated_settings(self):
		s = frappe.get_single(SETTINGS)
		s.db_set("jarvis_admin_url", "https://admin.example.com")
		s.db_set("token_budget_monthly", 50000)
		s.db_set("llm_temperature", 0.4)
		frappe.db.commit()
		reset_onboarding()
		s = frappe.get_single(SETTINGS)
		self.assertEqual(s.jarvis_admin_url, "https://admin.example.com")
		self.assertEqual(int(s.token_budget_monthly), 50000)
		self.assertAlmostEqual(float(s.llm_temperature), 0.4)


class TestResetOnboardingGuards(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot()

	def tearDown(self):
		frappe.set_user("Administrator")
		_restore(self._snap)

	def test_rejects_when_non_system_manager(self):
		# Use a Guest who lacks System Manager role.
		frappe.set_user("Guest")
		with enforced_role_guards(), self.assertRaises(frappe.PermissionError):
			reset_onboarding()


class TestResetOnboardingWipe(FrappeTestCase):
	"""wipe_data=True (the CLI default) also factory-resets workspace content."""

	def setUp(self):
		self._snap = _snapshot()
		_seed_onboarded_state()
		frappe.db.delete("Jarvis Macro", {"macro_name": "dev-reset-wipe"})
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "dev-reset-wipe"})
		conv.flags.ignore_mandatory = True
		conv.flags.ignore_links = True
		conv.insert(ignore_permissions=True)
		macro = frappe.get_doc(
			{"doctype": "Jarvis Macro", "macro_name": "dev-reset-wipe", "steps": [{"prompt": "hi"}]}
		)
		macro.flags.ignore_mandatory = True
		macro.flags.ignore_links = True
		macro.insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Jarvis Conversation", {"title": "dev-reset-wipe"})
		frappe.db.delete("Jarvis Macro", {"macro_name": "dev-reset-wipe"})
		frappe.db.commit()
		_restore(self._snap)

	def test_wipe_data_deletes_content(self):
		out = reset_onboarding(wipe_data=True)
		self.assertTrue(out["data"]["wiped_doctypes"])
		self.assertEqual(frappe.db.count("Jarvis Conversation"), 0)
		self.assertEqual(frappe.db.count("Jarvis Macro"), 0)

	def test_default_keeps_content(self):
		out = reset_onboarding()
		self.assertEqual(out["data"]["wiped_doctypes"], [])
		self.assertEqual(frappe.db.count("Jarvis Conversation", {"title": "dev-reset-wipe"}), 1)
		self.assertEqual(frappe.db.count("Jarvis Macro", {"macro_name": "dev-reset-wipe"}), 1)


class TestResetUnpairsTheContainer(FrappeTestCase):
	"""The field wipe below clears this bench's device credentials, but the
	PAIRING lives in the container: any surviving copy of that token would keep
	chat access to a workspace the operator just reset."""

	def setUp(self):
		self._snap = _snapshot()
		_seed_onboarded_state()
		for target in ("post_subscription_disconnect", "unpair_chat_devices"):
			pt = patch(f"jarvis.admin_client.{target}")
			self.addCleanup(pt.stop)
			setattr(self, target, pt.start())

	def tearDown(self):
		_restore(self._snap)

	def test_unpairs_while_the_bench_can_still_reach_admin(self):
		"""Ordering: after the wipe the api credentials are gone, so the call must
		happen before it."""
		seen = {}
		self.unpair_chat_devices.side_effect = lambda: seen.update(
			agent_url=frappe.db.get_single_value(SETTINGS, "agent_url")
		)
		reset_onboarding()
		self.assertTrue(self.unpair_chat_devices.called, "reset left the container paired")
		self.assertTrue(seen.get("agent_url"), "unpaired after the credentials were wiped")

	def test_a_failed_unpair_never_blocks_the_reset(self):
		"""A dead container is often the very reason for the reset."""
		self.unpair_chat_devices.side_effect = Exception("fleet agent down")
		out = reset_onboarding()
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.get_single(SETTINGS).agent_url or "", "")

	def test_skipped_when_no_container_is_wired(self):
		frappe.get_single(SETTINGS).db_set("agent_url", "")
		frappe.db.commit()
		reset_onboarding()
		self.assertFalse(self.unpair_chat_devices.called)


class TestResetOnboardingEndpoint(FrappeTestCase):
	"""The whitelisted jarvis.onboarding.reset_onboarding wrapper behind the
	'Reset onboarding' settings button. Its whole job is gate + coerce +
	delegate to jarvis.dev.reset_onboarding (exhaustively covered above), so
	these assert exactly that contract with dev patched out."""

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_requires_system_manager(self):
		from jarvis.onboarding import reset_onboarding as endpoint

		frappe.set_user("Guest")
		with enforced_role_guards(), self.assertRaises(frappe.PermissionError):
			endpoint()

	def test_rejects_jarvis_admin_who_is_not_system_manager(self):
		"""Stricter-than-sibling gate: the Jarvis Admin (desk) role alone is not
		enough — only System Manager runs the wipe. Mirrors the frontend button
		gated on is_system_manager, not the combined isSM (a Jarvis-Admin-only
		seat must neither see nor be able to execute it)."""
		from jarvis.onboarding import reset_onboarding as endpoint
		from jarvis.permissions import JARVIS_ADMIN_ROLE, ensure_jarvis_admin_role

		ensure_jarvis_admin_role()
		email = "reset-ob-jarvis-admin-only@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Reset OB",
					"send_welcome_email": 0,
					"roles": [{"role": JARVIS_ADMIN_ROLE}],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(lambda: frappe.delete_doc("User", email, ignore_permissions=True, force=True))

		roles = set(frappe.get_roles(email))
		self.assertIn(JARVIS_ADMIN_ROLE, roles)
		self.assertNotIn("System Manager", roles)

		frappe.set_user(email)
		with enforced_role_guards(), self.assertRaises(frappe.PermissionError):
			endpoint()

	@patch("jarvis.dev.reset_onboarding", return_value={"ok": True, "data": {}})
	def test_defaults_to_full_wipe_matching_the_cli(self, m):
		from jarvis.onboarding import reset_onboarding as endpoint

		endpoint()
		m.assert_called_once_with(wipe_data=True)

	@patch("jarvis.dev.reset_onboarding", return_value={"ok": True, "data": {}})
	def test_coerces_http_string_false(self, m):
		"""A falsy string must forward False (never a truthy non-empty string), so
		a stray wipe_data can't trigger the destructive content wipe. Coerced by
		the @whitelist bool annotation in request/test context, with cint() as the
		belt-and-suspenders fallback."""
		from jarvis.onboarding import reset_onboarding as endpoint

		endpoint(wipe_data="0")
		m.assert_called_once_with(wipe_data=False)

	@patch(
		"jarvis.dev.reset_onboarding",
		return_value={"ok": True, "data": {"cleared_fields": [], "wiped_doctypes": ["X"]}},
	)
	def test_returns_dev_payload_unchanged(self, m):
		from jarvis.onboarding import reset_onboarding as endpoint

		out = endpoint()
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["wiped_doctypes"], ["X"])
