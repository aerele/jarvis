"""Tests for v2_33: a ChatGPT-subscription workspace moves from gpt-5.6-terra to gpt-5.6-sol.

Only Terra (the old default) moves; other picks, api-key sites and conversation pins
stay. A moved pool row queues one resync for after the migrate; the resync job never
calls admin inside a migrate. The patch never raises.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.patches import v2_33_openai_subscription_default_sol as p

SETTINGS = "Jarvis Settings"
POOL_ROW = "Jarvis LLM Pool Model"
_FIELDS = ("llm_auth_mode", "llm_provider", "llm_model")
TERRA, SOL = "gpt-5.6-terra", "gpt-5.6-sol"


class _Base(FrappeTestCase):
	def setUp(self):
		snap = {f: frappe.db.get_single_value(SETTINGS, f) for f in _FIELDS}
		self.addCleanup(lambda: [frappe.db.set_single_value(SETTINGS, f, v) for f, v in snap.items()])
		self.addCleanup(frappe.clear_cache, doctype=SETTINGS)
		# The shared test site may carry pool rows of its own: hide them.
		existing = frappe.get_all(POOL_ROW, filters={"parent": SETTINGS}, pluck="name")
		for name in existing:
			frappe.db.set_value(POOL_ROW, name, "enabled", 0, update_modified=False)
		self.addCleanup(
			lambda: [frappe.db.set_value(POOL_ROW, n, "enabled", 1, update_modified=False) for n in existing]
		)
		enqueue = patch.object(frappe, "enqueue")
		self.enqueue = enqueue.start()
		self.addCleanup(enqueue.stop)

	def _site(self, mode, provider, model=""):
		frappe.db.set_single_value(SETTINGS, "llm_auth_mode", mode)
		frappe.db.set_single_value(SETTINGS, "llm_provider", provider)
		frappe.db.set_single_value(SETTINGS, "llm_model", model)
		frappe.clear_cache(doctype=SETTINGS)

	def _row(self, model, idx, upstream="openai", credential_type="subscription"):
		accounts = [
			{"upstream": upstream, "account_ref": "SUB_soltest", "label": "sol test", "oauth_blob": {}}
		]
		row = frappe.get_doc(
			{
				"doctype": POOL_ROW,
				"parent": SETTINGS,
				"parenttype": SETTINGS,
				"parentfield": "models",
				"idx": idx,
				"enabled": 1,
				"provider": "openai",
				"model": model,
				"tier": "strong",
				"credential_type": credential_type,
				"rotation": "sticky",
				"subscription_accounts": json.dumps(accounts),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, POOL_ROW, {"name": row.name})
		frappe.clear_cache(doctype=SETTINGS)
		return row.name

	def _model(self, name):
		return frappe.db.get_value(POOL_ROW, name, "model")

	def _default(self):
		return frappe.db.get_single_value(SETTINGS, "llm_model")

	def _resyncs(self):
		return [c for c in self.enqueue.call_args_list if c.args and c.args[0] == p._RESYNC_JOB]


class TestDirectChatGPTLogin(_Base):
	def test_terra_default_moves_to_sol_with_no_resync(self):
		self._site("oauth", "OpenAI", TERRA)
		p.execute()
		self.assertEqual(self._default(), SOL)
		self.assertEqual(self._resyncs(), [])

	def test_another_pick_is_kept(self):
		self._site("oauth", "OpenAI", "gpt-5.6-luna")
		p.execute()
		self.assertEqual(self._default(), "gpt-5.6-luna")

	def test_api_key_site_is_untouched(self):
		self._site("api_key", "OpenAI", TERRA)
		p.execute()
		self.assertEqual(self._default(), TERRA)


class TestChatGPTPool(_Base):
	def setUp(self):
		super().setUp()
		self._site("subscription", "OpenAI", TERRA)

	def test_terra_row_and_its_mirror_move_and_one_resync_is_queued(self):
		row = self._row(TERRA, idx=101)
		p.execute()
		self.assertEqual(self._model(row), SOL)
		self.assertEqual(self._default(), SOL)
		self.assertEqual(len(self._resyncs()), 1)
		self.assertTrue(self._resyncs()[0].kwargs.get("enqueue_after_commit"))

	def test_other_picks_are_kept(self):
		row = self._row("gpt-6-astra", idx=101)
		p.execute()
		self.assertEqual(self._model(row), "gpt-6-astra")
		self.assertEqual(self._resyncs(), [])

	def test_a_pool_that_already_has_sol_is_left_alone(self):
		terra, sol = self._row(TERRA, idx=101), self._row(SOL, idx=102)
		p.execute()
		self.assertEqual((self._model(terra), self._model(sol)), (TERRA, SOL))
		# llm_model mirrors the first row (still Terra), so it must not move either.
		self.assertEqual(self._default(), TERRA)
		self.assertEqual(self._resyncs(), [])

	def test_a_non_chatgpt_subscription_row_is_untouched(self):
		row = self._row(TERRA, idx=101, upstream="xai")
		p.execute()
		self.assertEqual(self._model(row), TERRA)

	def test_second_run_is_a_no_op(self):
		row = self._row(TERRA, idx=101)
		p.execute()
		p.execute()
		self.assertEqual(self._model(row), SOL)
		self.assertEqual(len(self._resyncs()), 1)


class TestNeverRaises(_Base):
	def test_a_failure_rolls_back_and_is_logged(self):
		self._site("oauth", "OpenAI", TERRA)
		with (
			patch.object(p, "_repoint_workspace_default", side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
		):
			p.execute()  # must not raise
		self.assertEqual(self._default(), TERRA)
		log_error.assert_called_once()


class TestResyncJob(_Base):
	def test_skips_inside_a_migrate(self):
		before = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		self.addCleanup(setattr, frappe.flags, "in_migrate", before)
		with patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync") as resync:
			p.resync_after_default_move()
		resync.assert_not_called()

	def test_resyncs_a_configured_workspace(self):
		with (
			patch("jarvis.account._has_llm_config", return_value=True),
			patch("jarvis.onboarding._has_admin_credentials", return_value=True),
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync") as resync,
		):
			p.resync_after_default_move()
		resync.assert_called_once()

	def test_a_resync_error_does_not_raise(self):
		with (
			patch("jarvis.account._has_llm_config", return_value=True),
			patch("jarvis.onboarding._has_admin_credentials", return_value=True),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync",
				side_effect=RuntimeError("admin down"),
			),
			patch.object(frappe, "log_error") as log_error,
		):
			p.resync_after_default_move()
		log_error.assert_called_once()
