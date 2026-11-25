"""Tests for v2_33: a direct ChatGPT-login workspace moves from gpt-5.6-terra to gpt-5.6-sol.

Only Terra (the old default) moves, and only on a DIRECT workspace: there each turn
sends llm_model, so no sync or restart is needed. A pool workspace is left alone
(changing a pool's primary restarts its container). Nothing is ever enqueued. The
patch never raises.
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
_POOL_MODE = "jarvis.jarvis.pool_serialize.compute_pool_mode"


class _Base(FrappeTestCase):
	def setUp(self):
		snap = {f: frappe.db.get_single_value(SETTINGS, f) for f in _FIELDS}
		self.addCleanup(lambda: [frappe.db.set_single_value(SETTINGS, f, v) for f, v in snap.items()])
		self.addCleanup(frappe.clear_cache, doctype=SETTINGS)
		# The shared test site may carry pool rows of its own: hide them.
		existing = frappe.get_all(POOL_ROW, filters={"parent": SETTINGS, "enabled": 1}, pluck="name")
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

	def _row(self, model, idx=101, upstream="openai"):
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
				"credential_type": "subscription",
				"rotation": "sticky",
				"subscription_accounts": json.dumps(accounts),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, POOL_ROW, {"name": row.name})
		frappe.clear_cache(doctype=SETTINGS)
		return row.name

	def _run(self, pool_mode):
		with patch(_POOL_MODE, return_value=pool_mode):
			p.execute()

	def _model(self, name):
		return frappe.db.get_value(POOL_ROW, name, "model")

	def _default(self):
		return frappe.db.get_single_value(SETTINGS, "llm_model")


class TestDirectWorkspace(_Base):
	def test_flat_chatgpt_login_moves_to_sol(self):
		self._site("oauth", "OpenAI", TERRA)
		self._run(pool_mode=False)
		self.assertEqual(self._default(), SOL)

	def test_lone_row_moves_with_its_mirror(self):
		# Settings copies the lone row into llm_model on the next save, so both move.
		self._site("subscription", "openai", TERRA)
		row = self._row(TERRA)
		self._run(pool_mode=False)
		self.assertEqual((self._default(), self._model(row)), (SOL, SOL))

	def test_another_pick_is_kept(self):
		self._site("oauth", "OpenAI", "gpt-5.6-luna")
		self._run(pool_mode=False)
		self.assertEqual(self._default(), "gpt-5.6-luna")

	def test_api_key_site_is_untouched(self):
		self._site("api_key", "OpenAI", TERRA)
		self._run(pool_mode=False)
		self.assertEqual(self._default(), TERRA)

	def test_second_run_is_a_no_op(self):
		self._site("subscription", "openai", TERRA)
		row = self._row(TERRA)
		self._run(pool_mode=False)
		self._run(pool_mode=False)
		self.assertEqual((self._default(), self._model(row)), (SOL, SOL))

	def test_nothing_is_enqueued(self):
		self._site("oauth", "OpenAI", TERRA)
		self._run(pool_mode=False)
		self.enqueue.assert_not_called()


class TestPoolWorkspace(_Base):
	def test_pool_is_left_on_its_saved_model(self):
		self._site("subscription", "openai", TERRA)
		row = self._row(TERRA)
		self._run(pool_mode=True)
		self.assertEqual((self._default(), self._model(row)), (TERRA, TERRA))
		self.enqueue.assert_not_called()


class TestNeverRaises(_Base):
	def test_a_failure_rolls_back_and_is_logged(self):
		self._site("subscription", "openai", TERRA)
		row = self._row(TERRA)
		with (
			patch.object(p, "_openai_subscription_pool_rows", side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
		):
			self._run(pool_mode=False)  # must not raise
		self.assertEqual((self._default(), self._model(row)), (TERRA, TERRA))
		log_error.assert_called_once()
