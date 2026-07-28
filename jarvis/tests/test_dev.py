"""Tests for jarvis.dev - the customer-side reset_onboarding endpoint.

Sandbox mode (Jarvis Settings.sandbox_mode) and jarvis.dev.is_sandbox_mode /
_dev_guard / is_dev_mode_active were removed as a dead feature. reset_onboarding
now gates on System Manager alone via frappe.only_for, which was always the
real security boundary (sandbox mode was documented as self-attested UX, not
hardening)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.dev import _RESET_CLEAR_FIELDS, reset_onboarding

SETTINGS = "Jarvis Settings"


_PASSWORD_FIELDS = {
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"agent_token",
	"chat_device_private_key",
	"chat_device_token",
	"llm_api_key",
}


# reset_onboarding() clears these OUTSIDE the _RESET_CLEAR_FIELDS loop (see
# jarvis/dev.py), so snapshotting that tuple alone left them wiped for every
# test that ran after this module in the same shard. llm_auth_mode is `reqd`
# on Jarvis Settings, so a blank one makes the NEXT full .save() of the Single
# anywhere in the shard die with MandatoryError - which is exactly how this
# leak surfaced, in an unrelated module, only once shard balancing happened to
# put the two together. Snapshotted raw rather than through _read so a NULL
# datetime restores as NULL instead of "".
_RESET_EXTRA_FIELDS = (
	"llm_auth_mode",
	"llm_oauth_account_email",
	"llm_oauth_connected_at",
	"preset",
	"proxy_active",
	"proxy_recommended",
)


def _read(s, field):
	"""Read a Jarvis Settings field, password-safe."""
	if field in _PASSWORD_FIELDS:
		return s.get_password(field, raise_exception=False) or ""
	return s.get(field) or ""


def _snapshot():
	"""Snapshot every reset-affected field so tests run against a real site
	(not test_site) don't trash the operator's actual onboarded state."""
	s = frappe.get_single(SETTINGS)
	snap = {f: _read(s, f) for f in (*_RESET_CLEAR_FIELDS, "llm_provider")}
	snap.update({f: s.get(f) for f in _RESET_EXTRA_FIELDS})
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
	s.db_set("llm_provider", "OpenAI")
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
		# llm_provider resets to default rather than going blank (Select).
		self.assertEqual(s.llm_provider, "Anthropic")

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
		with self.assertRaises(frappe.PermissionError):
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
