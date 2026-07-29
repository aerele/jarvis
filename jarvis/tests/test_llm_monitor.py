"""TDD tests for customer-side LLM monitor wrappers (Plan 3 Phase F).

Tests: account.get_llm_usage (short-circuit for direct tenants; passthrough
for proxy tenants; admin error surfaces as frappe.ValidationError) and
account.get_llm_connection_status (field remapping; no token material).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client
from jarvis.account import _has_llm_config
from jarvis.exceptions import AdminValidationError


class TestGetLlmUsage(FrappeTestCase):
	def setUp(self):
		self._proxy = frappe.db.get_single_value("Jarvis Settings", "proxy_active")

	def tearDown(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", self._proxy or 0)
		frappe.db.commit()

	def test_direct_tenant_returns_empty_shape_without_admin_call(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", 0)
		frappe.db.commit()
		with patch.object(admin_client, "get_llm_usage") as m:
			out = account.get_llm_usage()
		m.assert_not_called()
		self.assertEqual(out["applicable"], False)
		self.assertEqual(out["per_model"], [])
		self.assertEqual(out["used_vs_limit"], {"used_usd": 0.0, "limit_usd": None})

	def test_proxy_tenant_passes_admin_payload_through(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", 1)
		frappe.db.commit()
		fake = {
			"applicable": True,
			"period": "1M",
			"tokens_in": 10,
			"tokens_out": 20,
			"cost_usd": 0.42,
			"per_model": [{"model": "gpt-5.5", "tokens": 30, "cost": 0.42}],
			"used_vs_limit": {"used_usd": 0.42, "limit_usd": 5.0},
		}
		with patch.object(admin_client, "get_llm_usage", return_value=fake) as m:
			out = account.get_llm_usage()
		m.assert_called_once_with()
		self.assertEqual(out["applicable"], True)
		self.assertEqual(out["cost_usd"], 0.42)
		self.assertEqual(out["per_model"][0]["model"], "gpt-5.5")

	def test_admin_validation_error_surfaces_as_frappe_throw(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", 1)
		frappe.db.commit()
		with patch.object(
			admin_client, "get_llm_usage", side_effect=AdminValidationError("bifrost unreachable")
		):
			with self.assertRaises(frappe.ValidationError):
				account.get_llm_usage()


class TestGetLlmConnectionStatus(FrappeTestCase):
	def setUp(self):
		self._proxy = frappe.db.get_single_value("Jarvis Settings", "proxy_active")
		self._llm_model = frappe.db.get_single_value("Jarvis Settings", "llm_model")

	def tearDown(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", self._proxy or 0)
		frappe.db.set_single_value("Jarvis Settings", "llm_model", self._llm_model or "")
		frappe.db.commit()

	def test_remaps_admin_auth_status_fields(self):
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", 1)
		frappe.db.commit()
		raw = {
			"ok": True,
			"data": {
				"auth_profile_present": True,
				"profile_ids": ["openai"],
				"default_model": "gpt-5.5",
				"openai_profile_expires_ms": 1893456000000,
			},
		}
		with patch.object(admin_client, "post_llm_auth_status", return_value=raw) as m:
			out = account.get_llm_connection_status()
		m.assert_called_once_with()
		self.assertEqual(out["proxy_active"], True)
		self.assertEqual(out["auth_present"], True)
		self.assertEqual(out["oauth_expires_at"], 1893456000000)
		self.assertEqual(out["default_model"], "gpt-5.5")

	def test_direct_tenant_short_circuits_without_admin_call(self):
		# A DIRECT (single-model) tenant has no proxy auth profile to report -
		# the SPA's ConnectionPane used to render this as a misleading orange
		# "Not connected" instead of an accurate "Direct" state.
		frappe.db.set_single_value("Jarvis Settings", "proxy_active", 0)
		frappe.db.set_single_value("Jarvis Settings", "llm_model", "gpt-4o")
		frappe.db.commit()
		with patch.object(admin_client, "post_llm_auth_status") as m:
			out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertEqual(out["proxy_active"], False)
		self.assertEqual(out["auth_present"], False)
		self.assertEqual(out["default_model"], "gpt-4o")


class TestHasLlmConfig(FrappeTestCase):
	"""_has_llm_config must report a CREDENTIAL, not a leftover label.

	Driven with a stub rather than the live Single on purpose. The predicate is
	pure logic over a settings-like object, and the earlier site-state version of
	these tests was both order-dependent and wrong: it fought whatever another
	session happened to have configured on the shared test site, and it had to
	delete a real __Auth row to set up (a Password field survives a column
	blank), which leaked a secret into every later test.

	The behaviour under test: llm_provider / llm_model are labels that on_update
	mirrors from models[0]. They are not proof of a connection on their own, and
	jarvis.oauth.api.disconnect deliberately leaves them behind while removing the
	only usable credential. Testing the labels reported such a workspace as
	connected.
	"""

	class _Stub:
		def __init__(self, **kw):
			self._d = {
				"llm_provider": "",
				"llm_model": "",
				"llm_auth_mode": "api_key",
				"llm_oauth_account_email": "",
				"llm_oauth_connected_at": None,
				"models": [],
				"proxy_active": 0,
			}
			self._d.update(kw)
			self._key = kw.get("_key", "")

		def get(self, k, default=None):
			return self._d.get(k, default)

		def get_password(self, fieldname, raise_exception=True):
			return self._key

		def __getattr__(self, k):
			return self._d.get(k)

	def test_a_leftover_provider_label_with_no_credential_is_not_connected(self):
		"""The exact half-cleared shape jarvis.oauth.api.disconnect leaves."""
		s = self._Stub(llm_provider="OpenAI", llm_model="gpt-4o")
		self.assertFalse(_has_llm_config(s))

	def test_a_direct_tenant_with_a_stored_key_is_connected(self):
		s = self._Stub(llm_provider="OpenAI", llm_model="gpt-4o", _key="sk-real")
		self.assertTrue(_has_llm_config(s))

	def test_a_live_oauth_tenant_is_connected_without_an_api_key(self):
		s = self._Stub(llm_provider="Anthropic", llm_auth_mode="oauth", llm_oauth_account_email="a@b.c")
		self.assertTrue(_has_llm_config(s))

	def test_an_oauth_tenant_whose_connection_was_cleared_is_not_connected(self):
		s = self._Stub(llm_provider="Anthropic", llm_auth_mode="oauth")
		self.assertFalse(_has_llm_config(s))

	def test_a_pool_counts_even_with_the_mirrors_blank(self):
		"""A pool whose rows are all disabled leaves the mirror blank, but it
		still HOLDS credentials: paused, not disconnected."""
		self.assertTrue(_has_llm_config(self._Stub(models=[object()])))

	def test_proxy_active_alone_counts(self):
		"""proxy_active is derived from config and reset when it goes away, so a
		set flag is itself proof a pool exists. An existing monitor test relies
		on exactly this shape."""
		self.assertTrue(_has_llm_config(self._Stub(proxy_active=1)))

	def test_a_bare_workspace_is_not_connected(self):
		self.assertFalse(_has_llm_config(self._Stub()))
