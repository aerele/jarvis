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
	_FIELDS = (
		"proxy_active",
		"llm_model",
		"routing_mode",
		"last_sync_status",
		"last_subscription_status",
		"llm_pool_synced_at",
	)

	def setUp(self):
		# frappe.get_single serves a CACHED doc, so a db.set_single_value written
		# here is invisible to the endpoint unless the cache is dropped first
		# (the stale-Single flake this suite has hit before).
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		self._saved = {f: frappe.db.get_single_value("Jarvis Settings", f) for f in self._FIELDS}
		# health now also reads the latest completed turn (#678), which is real
		# table data on a SHARED test site: one stray errored assistant row would
		# flip every "ok" case in this class. Pinned false by default so each test
		# exercises only the signal it names; the cases that care about turn
		# history override it, and _last_turn_errored has its own class below.
		# Neutralised by PATCH, not by deleting rows: wiping a shared site's data
		# from a test is how live tenants were destroyed here before.
		self._turn_patch = patch.object(account, "_last_turn_errored", return_value=False)
		self._turn_patch.start()
		self.addCleanup(self._turn_patch.stop)

	def tearDown(self):
		for field, value in self._saved.items():
			frappe.db.set_single_value("Jarvis Settings", field, value)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")

	def _seed(self, **values):
		"""Write Single fields and make them visible to frappe.get_single."""
		for field, value in values.items():
			frappe.db.set_single_value("Jarvis Settings", field, value)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")

	def test_remaps_admin_auth_status_fields(self):
		self._seed(proxy_active=1)
		raw = {
			"ok": True,
			"data": {
				"auth_profile_present": True,
				"profile_ids": ["openai"],
				"default_model": "gpt-5.5",
				"openai_profile_expires_ms": 1893456000000,
			},
		}
		# pool_primary_model is stubbed so default_model does not depend on
		# whatever models[] the shared test site happens to hold - the local
		# primary is preferred over admin's value, and its own preference is
		# asserted in test_default_model_prefers_the_local_pool_primary below.
		with patch.object(account, "pool_primary_model", return_value=""):
			with patch.object(admin_client, "post_llm_auth_status", return_value=raw) as m:
				out = account.get_llm_connection_status()
		m.assert_called_once_with()
		self.assertEqual(out["proxy_active"], True)
		self.assertEqual(out["auth_present"], True)
		self.assertEqual(out["oauth_expires_at"], 1893456000000)
		self.assertEqual(out["default_model"], "gpt-5.5")

	def test_default_model_prefers_the_local_pool_primary(self):
		"""Admin answers default_model with the Bifrost virtual endpoint
		("openai_compat/jarvis-pool"), which is not a model the customer picked.
		The bench knows which member the container runs first, so that wins."""
		self._seed(proxy_active=1)
		raw = {"data": {"default_model": "openai_compat/jarvis-pool"}}
		with patch.object(account, "pool_primary_model", return_value="glm-4.7"):
			with patch.object(admin_client, "post_llm_auth_status", return_value=raw):
				out = account.get_llm_connection_status()
		self.assertEqual(out["default_model"], "glm-4.7")

	# ---- health: admin's auth_profile_present is not a verdict (#561) ------- #
	#
	# compute_pool_mode is stubbed throughout rather than satisfied with real
	# models[] rows, for the same reasons the DIRECT test below stubs
	# _has_llm_config: seeding a real pool means writing encrypted Password
	# fields into a shared Single (which survive a column blank and leak into
	# every later test on the site), and reading the live models[] would make the
	# outcome depend on whatever another test left behind. The subject here is
	# which SIGNAL decides health, not how a pool is detected.

	def test_a_serving_pool_is_not_disconnected_when_admin_reports_no_profiles(self):
		"""The bug. cliproxy had both subscription auth files loaded and was
		answering turns with them, while admin's post_llm_auth_status returned
		auth_profile_present:false with an empty profile_ids - and the badge
		rendered red "Not connected" over demonstrably working chat.

		The admin claim is still passed through verbatim for support; it just
		stops deciding anything.
		"""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		raw = {"data": {"auth_profile_present": False, "profile_ids": []}}
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value=raw):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "ok")
		self.assertFalse(out["disconnected"])
		self.assertFalse(out["auth_present"])
		self.assertTrue(out["pool_mode"])

	def test_a_pool_the_container_never_received_is_still_reported_down(self):
		"""The counterweight: health must not be a hardcoded green. With no
		confirmed apply the container is not serving this config at all, which is
		the same evidence is_ready_for_chat refuses to open chat on."""
		self._seed(
			proxy_active=1,
			last_sync_status="failed: admin unreachable",
			llm_pool_synced_at="",
		)
		raw = {"data": {"auth_profile_present": True, "profile_ids": ["openai"]}}
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value=raw):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "down")

	def test_a_failed_apply_on_a_serving_pool_needs_attention(self):
		"""Applied before, so the container keeps serving its previous config -
		broken enough to flag, not broken enough to call disconnected."""
		self._seed(
			proxy_active=1,
			last_sync_status="failed: subscription needs re-authentication (blocked)",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "attention")

	def test_a_rejected_subscription_needs_attention(self):
		"""The fleet's own pool-wide probe said the account rejected a test
		request. That is a real verdict about the workspace, unlike the auth
		profile count."""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
			last_subscription_status="unverified",
		)
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "attention")

	def test_a_failing_turn_stops_a_clean_apply_reading_green(self):
		"""#678. Every input a confirmed apply gives us was clean - the config
		reached the container and the probe saw nothing wrong - while every chat
		turn failed against a base URL nothing served. A green badge there sent
		people looking in the wrong place, so the turns themselves have to count.
		"""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
			last_subscription_status="unchecked",
		)
		with patch.object(account, "_last_turn_errored", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "attention")

	def test_a_failing_turn_never_downgrades_past_attention(self):
		"""It is evidence that something downstream is wrong, never that the
		config failed to arrive. "down" is reserved for an unconfirmed apply,
		because that is the value is_ready_for_chat also refuses chat on, and the
		two must not disagree."""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		with patch.object(account, "_last_turn_errored", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					out = account.get_llm_connection_status()
		self.assertNotEqual(out["health"], "down")

	def test_a_save_in_flight_still_wins_over_a_failing_turn(self):
		"""Ordering guard: while a save is in flight the container is still on its
		previous config, so the turn that just failed says nothing about the
		config being applied now. "applying" has to keep precedence."""
		self._seed(
			proxy_active=1,
			last_sync_status="pending: admin applying config",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		with patch.object(account, "_last_turn_errored", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "applying")

	def test_an_unchecked_subscription_is_not_treated_as_a_failure(self):
		"""An "unchecked" verdict means nobody looked (a no-op apply runs no probe
		at all), which must not read as evidence of a problem."""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
			last_subscription_status="unchecked",
		)
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "ok")

	def test_a_save_in_flight_reports_applying(self):
		self._seed(
			proxy_active=1,
			last_sync_status="pending: admin applying config",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "applying")

	def test_shape_fields_describe_the_pool_rather_than_models_zero(self):
		"""The Model/Provider/Auth-mode triple came from the legacy models[0]
		mirror, so a 4-model pool was described by one member. These are the
		fields the SPA replaces it with."""
		self._seed(proxy_active=1, routing_mode="failover")
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				out = account.get_llm_connection_status()
		self.assertTrue(out["pool_mode"])
		self.assertEqual(out["routing_mode"], "failover")
		self.assertIn("model_count", out)
		self.assertIn("sync_status", out)

	def test_a_disconnected_workspace_is_down_and_says_so(self):
		with patch.object(account, "_has_llm_config", return_value=False):
			with patch.object(admin_client, "post_llm_auth_status") as m:
				out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertTrue(out["disconnected"])
		self.assertEqual(out["health"], "down")

	def test_direct_tenant_short_circuits_without_admin_call(self):
		# A DIRECT (single-model) tenant has no proxy auth profile to report -
		# the SPA's ConnectionPane used to render this as a misleading orange
		# "Not connected" instead of an accurate "Direct" state.
		#
		# _has_llm_config is stubbed rather than satisfied with a real stored
		# key. The subject here is the SHORT-CIRCUIT and the field remap, not the
		# predicate, which TestHasLlmConfig below covers exhaustively over a stub.
		# Satisfying it for real would mean writing a Password field, and blanking
		# one afterwards leaves the secret in __Auth and leaks a test key into
		# every later test on the site. It would also make the outcome depend on
		# whatever models[] the shared test site happens to hold.
		self._seed(proxy_active=0, llm_model="gpt-4o")
		with patch.object(account, "_has_llm_config", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status") as m:
				out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertEqual(out["proxy_active"], False)
		self.assertEqual(out["disconnected"], False)
		self.assertEqual(out["auth_present"], False)
		self.assertEqual(out["default_model"], "gpt-4o")

	def test_a_leftover_model_label_alone_reports_disconnected(self):
		"""The behaviour change, asserted at the endpoint and not just on the
		predicate: a workspace holding only a MIRROR (llm_model) with no
		credential behind it is disconnected, and must not leak the label out as
		a default_model the SPA would render as a healthy "Direct" state.

		This is the shape jarvis.oauth.api.disconnect leaves behind - it clears
		the OAuth side and deliberately keeps llm_provider / llm_model.
		"""
		self._seed(proxy_active=0, llm_model="gpt-4o")
		with patch.object(account, "_has_llm_config", return_value=False):
			with patch.object(admin_client, "post_llm_auth_status") as m:
				out = account.get_llm_connection_status()
		m.assert_not_called()
		self.assertEqual(out["disconnected"], True)
		self.assertEqual(out["default_model"], "")


class TestLastTurnErrored(FrappeTestCase):
	"""The query itself. Driven through frappe.get_all rather than by writing
	rows: this suite runs against a SHARED site, and the filters are the part
	that silently rots (a renamed field would make the call return nothing and
	the badge go permanently green, which is the bug this was written to fix)."""

	def _call(self, rows):
		with patch.object(frappe, "get_all", return_value=rows) as m:
			out = account._last_turn_errored()
		return out, m.call_args

	def test_an_errored_latest_turn_reports_true(self):
		out, _ = self._call([{"error": "LLM request failed: network connection error."}])
		self.assertTrue(out)

	def test_a_clean_latest_turn_reports_false(self):
		out, _ = self._call([{"error": ""}])
		self.assertFalse(out)

	def test_a_workspace_with_no_turns_yet_reports_false(self):
		"""A brand new workspace has never run a turn. That is an absence of
		evidence, not a failure, and must not paint the badge."""
		out, _ = self._call([])
		self.assertFalse(out)

	def test_whitespace_is_not_an_error(self):
		out, _ = self._call([{"error": "   "}])
		self.assertFalse(out)

	def test_it_reads_only_the_latest_completed_assistant_turn(self):
		"""Pins the filters. Assistant rows only (a user message has no error), and
		neither a streaming nor a recovering row has finished failing yet - a
		recovering turn is parked for snapshot recovery and may still succeed."""
		_, call = self._call([])
		self.assertEqual(call.args[0], "Jarvis Chat Message")
		self.assertEqual(
			call.kwargs["filters"],
			{"role": "assistant", "streaming": 0, "recovering": 0},
		)
		self.assertEqual(call.kwargs["order_by"], "creation desc")
		self.assertEqual(call.kwargs["limit"], 1)

	def test_a_stopped_turn_carries_no_error_so_it_cannot_flag(self):
		"""Cancelling writes `stopped`, never `error` (turn_handler), so a user
		hitting stop leaves the badge alone. Painting red on a deliberate stop is
		the #561 false-alarm shape, and this is the guard against it returning."""
		out, _ = self._call([{"error": None}])
		self.assertFalse(out)


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
