"""Every admin code a user agent push can fail with: retryable or not, and its sentence."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.admin_client import _envelope_code
from jarvis.chat import user_agent_sync
from jarvis.exceptions import AdminContractError, AdminUnreachableError
from jarvis.tests._user_agent_helpers import DOCTYPE, UserAgentCase

RETRY = (
	"apply_in_progress",
	"job_running",
	"overloaded",
	"healthz_timeout",
	"user_agents_not_loaded",
	"browser_start_failed",
	"RateLimitExceeded",
	"FleetError",
	"admin_unreachable",
	"changed_during_sync",
)
SUPPORT = (
	"roster_shape_ambiguous",
	"sandbox_registry_unreadable",
	"user_agents_unreadable",
	"sandbox_keygen_failed",
	"sandbox_users_failed",
	"sandbox_workspace_refresh_failed",
	"browser_guard_missing",
)
HANDLED = (
	"InvalidUserAgents",
	"user_agents_disabled",
	"user_agents_unsupported_fleet",
	"user_agents_unsupported_image",
	"sandbox_image_unconfigured",
	"NoRunningTenant",
	"browser_limit",
	"browser_image_unconfigured",
)
SUPPORT_SENTENCE = (
	"Jarvis could not set up this agent on your workspace. Contact support and share this code: {}."
)


class TestSyncCodeTable(FrappeTestCase):
	def test_every_code_has_a_class_and_a_plain_sentence(self):
		for code in RETRY + SUPPORT + HANDLED:
			with self.subTest(code=code):
				self.assertEqual(user_agent_sync.is_retryable(code), code in RETRY)
				sentence = user_agent_sync.sentence_for(code)
				self.assertTrue(sentence.strip())
				self.assertTrue(sentence.endswith("."))
				self.assertNotIn("\u2014", sentence)  # no em dash
				self.assertNotIn("Error Log", sentence)

	def test_a_retryable_code_offers_retry_sync(self):
		for code in RETRY:
			with self.subTest(code=code):
				self.assertIn("Retry sync", user_agent_sync.sentence_for(code))

	def test_a_support_code_is_named_in_the_support_sentence(self):
		self.assertEqual(set(SUPPORT), set(user_agent_sync.NEEDS_SUPPORT))
		for code in SUPPORT:
			with self.subTest(code=code):
				self.assertEqual(user_agent_sync.sentence_for(code), SUPPORT_SENTENCE.format(code))

	def test_job_running_and_healthz_timeout_say_what_is_happening(self):
		self.assertIn("busy with another job", user_agent_sync.sentence_for("job_running"))
		self.assertIn("did not come back in time", user_agent_sync.sentence_for("healthz_timeout"))
		for code in ("job_running", "healthz_timeout"):
			self.assertIn("Retry sync in a minute", user_agent_sync.sentence_for(code))

	def test_an_unknown_code_fails_and_names_the_code(self):
		self.assertFalse(user_agent_sync.is_retryable("brand_new_code"))
		self.assertEqual(
			user_agent_sync.sentence_for("brand_new_code"), SUPPORT_SENTENCE.format("brand_new_code")
		)

	def test_codes_are_read_from_every_admin_error_shape(self):
		for code in RETRY[:5] + SUPPORT + HANDLED:
			with self.subTest(code=code):
				self.assertEqual(user_agent_sync.classify(AdminContractError("x", code=code)), code)
		self.assertEqual(
			user_agent_sync.classify(AdminUnreachableError("502", admin_code="healthz_timeout")),
			"healthz_timeout",
		)
		self.assertEqual(
			user_agent_sync.classify(AdminUnreachableError("502", admin_code="sandbox_keygen_failed")),
			"sandbox_keygen_failed",
		)
		self.assertEqual(user_agent_sync.classify(AdminUnreachableError("down")), "admin_unreachable")
		# A value that is not a short identifier is never stored or shown.
		self.assertEqual(
			user_agent_sync.classify(AdminUnreachableError("502", admin_code="<b>x y</b>")),
			"admin_unreachable",
		)

	def test_the_envelope_code_keeps_only_an_identifier(self):
		self.assertEqual(_envelope_code({"ok": False, "error": {"code": "job_running"}}), "job_running")
		self.assertEqual(_envelope_code({"ok": False, "error": {"code": "two words"}}), "")
		self.assertEqual(_envelope_code({"ok": False}), "")
		self.assertEqual(_envelope_code("not a dict"), "")


class TestSyncCodeOutcome(UserAgentCase):
	def _push_failing_with(self, code):
		row = self.make_agent(synced=False, agent_name=code[:40])
		with (
			patch(
				"jarvis.admin_client.post_push_user_agents", side_effect=AdminContractError("x", code=code)
			),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			user_agent_sync.run_sync()
		state = frappe.db.get_value(
			DOCTYPE, row.name, ["sync_status", "sync_error", "sync_error_message"], as_dict=True
		)
		frappe.db.delete(DOCTYPE, row.name)
		return state

	def test_each_code_lands_on_the_row_as_code_and_sentence(self):
		for code in RETRY[:5] + SUPPORT + HANDLED + ("brand_new_code",):
			with self.subTest(code=code):
				state = self._push_failing_with(code)
				self.assertEqual(state.sync_error, code)
				self.assertEqual(state.sync_status, "Pending" if code in RETRY else "Failed")
				self.assertEqual(state.sync_error_message, user_agent_sync.sentence_for(code))
				if code in SUPPORT or code == "brand_new_code":
					self.assertIn(code, state.sync_error_message)
