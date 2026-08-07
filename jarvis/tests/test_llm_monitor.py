"""TDD tests for customer-side LLM monitor wrappers (Plan 3 Phase F).

Tests: account.get_llm_usage (short-circuit for direct tenants; passthrough
for proxy tenants; admin error surfaces as frappe.ValidationError),
account.get_llm_connection_status (field remapping; no token material) and
account.get_llm_connection_health (the member-tier verdict, jarvis#711: what a
non-admin may learn about the workspace, and what the payload must never carry).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client
from jarvis.account import _has_llm_config
from jarvis.exceptions import AdminValidationError
from jarvis.permissions import ensure_jarvis_user_role


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
		# Same shared-site hazard for the positive counterpart (jarvis#714): a
		# real completed turn elsewhere on the site would silently demote a
		# stale "unverified" case to "ok". Pinned false by default too, so the
		# existing "needs attention" expectations stay deterministic; only the
		# test that names the demotion overrides it.
		self._turn_ok_patch = patch.object(account, "_last_turn_succeeded", return_value=False)
		self._turn_ok_patch.start()
		self.addCleanup(self._turn_ok_patch.stop)

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
		self.assertEqual(out["attention_reason"], "sync_failed")

	def test_a_rejected_subscription_needs_attention(self):
		"""The fleet's own pool-wide probe said the account rejected a test
		request, and no completed turn has succeeded since to contradict it.
		That is a real verdict about the workspace, unlike the auth profile
		count."""
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
		self.assertEqual(out["attention_reason"], "subscription_unverified")

	def test_a_later_success_clears_a_stale_unverified_subscription(self):
		"""jarvis#714: "Needs attention" never cleared after the workspace
		recovered, because the only signal behind it was a snapshot only a
		future apply refreshes. A completed turn that succeeded since is
		first-hand proof the subscription that turn used is fine now."""
		self._seed(
			proxy_active=1,
			last_sync_status="ok (pool_update via admin)",
			llm_pool_synced_at="2026-07-30 10:00:00",
			last_subscription_status="unverified",
		)
		with patch.object(account, "_last_turn_succeeded", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "ok")
		self.assertEqual(out["attention_reason"], "")

	def test_a_failed_sync_does_not_self_clear_on_a_success(self):
		"""The counterweight to the test above: a later success only proves the
		container still serves whatever it last applied, never that a FAILED
		apply since landed. Clearing this off local chat evidence would make the
		badge lie about the one thing #713's sync-race fix actually repairs."""
		self._seed(
			proxy_active=1,
			last_sync_status="failed: fleet returned 502",
			llm_pool_synced_at="2026-07-30 10:00:00",
		)
		with patch.object(account, "_last_turn_succeeded", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					out = account.get_llm_connection_status()
		self.assertEqual(out["health"], "attention")
		self.assertEqual(out["attention_reason"], "sync_failed")

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
		self.assertEqual(out["attention_reason"], "turn_error")

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


MEMBER_USER = "jarvis-connhealth-member@example.com"

# Every field get_llm_connection_status hands an admin. The member endpoint must
# return NONE of them, and the assertion is written against this list rather than
# against a handful of names so a field added to the admin payload is covered the
# day it lands.
ADMIN_ONLY_FIELDS = (
	"pool_mode",
	"model_count",
	"routing_mode",
	"sync_status",
	"proxy_active",
	"disconnected",
	"health",
	"attention_reason",
	"auth_present",
	"oauth_expires_at",
	"profile_ids",
	"default_model",
)


def _ensure_member_user() -> None:
	"""A plain workspace MEMBER: a System User holding "Jarvis User" (as every
	real chat user does) and NOT System Manager or Jarvis Admin. Idempotent, and
	it strips the admin roles if a previous run or another suite granted them -
	this site is shared, and a member who quietly became an admin would turn the
	whole class into a tautology."""
	ensure_jarvis_user_role()  # the test site may not have run after_migrate
	if frappe.db.exists("User", MEMBER_USER):
		doc = frappe.get_doc("User", MEMBER_USER)
		roles = set(frappe.get_roles(MEMBER_USER))
		stale = {"System Manager", "Jarvis Admin"} & roles
		if stale:
			doc.remove_roles(*stale)
		if "Jarvis User" not in roles:
			doc.add_roles("Jarvis User")
		# Re-assert the two fields has_jarvis_access reads besides roles. Another
		# suite on this shared site disabling the row, or flipping it to a Website
		# User, would otherwise fail every test in this class on the guard rather
		# than on the behaviour each one names.
		if doc.enabled != 1 or doc.user_type != "System User":
			doc.enabled = 1
			doc.user_type = "System User"
			doc.save(ignore_permissions=True)
		frappe.db.commit()
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": MEMBER_USER,
			"first_name": "Conn",
			"last_name": "Member",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("Jarvis User")
	frappe.db.commit()


class TestGetLlmConnectionHealth(FrappeTestCase):
	"""The member-tier Status badge (jarvis#711).

	The badge used to be a hardcoded green "Connected" for anyone who was not an
	admin, because get_llm_connection_status is require_jarvis_admin and a member
	could not fetch a verdict at all. get_llm_connection_health is the member's
	own endpoint, and these tests pin the two halves of its contract: it tells a
	member the truth about whether chat works, and it tells them nothing else.

	Driven as a REAL non-admin session (frappe.set_user) through the whitelisted
	function, never by patching the guard and inspecting the body. The guard is
	half of what is being tested, so stubbing it would assert nothing.
	"""

	_FIELDS = (
		"proxy_active",
		"llm_model",
		"routing_mode",
		"last_sync_status",
		"last_subscription_status",
		"llm_pool_synced_at",
	)

	def setUp(self):
		_ensure_member_user()
		self._orig_user = frappe.session.user
		# frappe.get_single serves a CACHED doc, so a db.set_single_value written
		# here is invisible to the endpoint unless the cache is dropped first
		# (the stale-Single flake this suite has hit before).
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		self._saved = {f: frappe.db.get_single_value("Jarvis Settings", f) for f in self._FIELDS}
		# Same shared-site hazard the sibling class documents: health reads the
		# latest completed turn, which is real table data here, so one stray
		# errored assistant row would flip every "ok" case in this class. Pinned
		# by PATCH, never by deleting rows - wiping a shared site's data from a
		# test is how live tenants were destroyed here before. The cases that care
		# about turn history override these.
		for attr in ("_last_turn_errored", "_last_turn_succeeded"):
			p = patch.object(account, attr, return_value=False)
			p.start()
			self.addCleanup(p.stop)

	def tearDown(self):
		# Restore the session FIRST: everything below writes.
		frappe.set_user(self._orig_user)
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

	def _as_member(self, fn=None):
		"""Run ``fn`` (default: the member endpoint) in a real MEMBER_USER
		session and restore the caller's identity whatever happens."""
		fn = fn or account.get_llm_connection_health
		frappe.set_user(MEMBER_USER)
		try:
			return fn()
		finally:
			frappe.set_user(self._orig_user)

	def _seed_serving_pool(self, **overrides):
		"""A pool the container has confirmed and is serving. The base state every
		health case below perturbs by exactly one field."""
		values = {
			"proxy_active": 1,
			"last_sync_status": "ok (pool_update via admin)",
			"llm_pool_synced_at": "2026-07-30 10:00:00",
			"last_subscription_status": "",
		}
		values.update(overrides)
		self._seed(**values)

	# ---- the gate -------------------------------------------------------- #

	def test_a_member_may_call_it(self):
		"""The premise of the whole fix. A member could not reach any verdict
		before, which is why the badge was a constant."""
		self._seed_serving_pool()
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertEqual(out["state"], "ok")

	def test_a_member_still_cannot_reach_the_admin_endpoint(self):
		"""The new endpoint is ADDITIVE. get_llm_connection_status's guard is
		untouched, so the payload with the pool shape, model names and profile
		ids stays admin-only."""
		with self.assertRaises(frappe.PermissionError):
			self._as_member(account.get_llm_connection_status)

	def test_guest_is_refused(self):
		"""require_jarvis_access rejects Guest before roles are even consulted -
		is_system_user names Guest explicitly.

		This drives the Python function directly, so it exercises THAT in-body
		guard and nothing else. Frappe refuses a Guest on the HTTP path too, but
		the decorator does not wrap the function: that check lives in the request
		dispatcher (frappe.is_whitelisted), which no direct call reaches. The
		in-body guard is the one that has to hold, so it is the one under test.
		"""
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				account.get_llm_connection_health()
		finally:
			frappe.set_user(self._orig_user)

	# ---- what the payload may contain ------------------------------------ #

	def test_the_payload_is_exactly_one_field(self):
		"""Key-set equality, not a spot check of a few names: a field added here
		later has to be a deliberate edit to this assertion."""
		self._seed_serving_pool()
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertEqual(set(out.keys()), {"state"})

	def test_no_admin_field_appears_in_the_member_payload(self):
		"""Named forbidden fields, so a failure says WHICH one leaked. Shape
		(pool_mode / model_count / routing_mode / sync_status / proxy_active),
		credential presence (disconnected / auth_present / profile_ids), model
		names (default_model), OAuth timing (oauth_expires_at) and the reason
		behind "attention" are all absent."""
		self._seed_serving_pool()
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		for field in ADMIN_ONLY_FIELDS:
			self.assertNotIn(field, out, f"member payload leaked {field}")

	def test_the_state_is_always_from_the_member_vocabulary(self):
		self._seed_serving_pool()
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertIn(out["state"], account.MEMBER_HEALTH_STATES)
		self.assertNotIn("disconnected", account.MEMBER_HEALTH_STATES)

	def test_it_never_round_trips_to_admin(self):
		"""A proxied tenant is where get_llm_connection_status calls
		post_llm_auth_status. Doing that here would put proxy_active - a topology
		fact absent from the body - into the response LATENCY."""
		self._seed_serving_pool()
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status") as m:
				self._as_member()
		m.assert_not_called()

	# ---- the verdict itself ---------------------------------------------- #

	def test_a_member_sees_attention_not_green_when_a_save_failed(self):
		"""The defect, from the member's seat: a workspace whose last apply
		failed used to render green for them."""
		self._seed_serving_pool(last_sync_status="failed: fleet returned 502")
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertEqual(out["state"], "attention")

	def test_a_member_sees_applying_while_a_save_is_in_flight(self):
		self._seed_serving_pool(last_sync_status="pending: admin applying config")
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertEqual(out["state"], "applying")

	def test_a_member_sees_down_when_the_container_never_got_the_config(self):
		self._seed_serving_pool(llm_pool_synced_at="")
		with patch.object(account, "compute_pool_mode", return_value=True):
			out = self._as_member()
		self.assertEqual(out["state"], "down")

	def test_a_disconnected_workspace_is_down_with_no_state_of_its_own(self):
		""" "disconnected" is an admin-only distinction. Telling a member their
		workspace holds no credential is credential-presence disclosure, and it
		buys them nothing "down" does not already say. So the two workspaces are
		indistinguishable here - asserted as payload equality, not as two separate
		expectations that could drift apart."""
		with patch.object(account, "_has_llm_config", return_value=False):
			no_credential = self._as_member()
		self._seed_serving_pool(llm_pool_synced_at="")
		with patch.object(account, "compute_pool_mode", return_value=True):
			never_applied = self._as_member()
		self.assertEqual(no_credential, {"state": "down"})
		self.assertEqual(no_credential, never_applied)

	# ---- the disclosure decision ----------------------------------------- #

	def test_every_attention_cause_gives_the_member_one_identical_payload(self):
		"""The settled disclosure question (jarvis#711).

		_llm_health splits "attention" three ways for an admin, and one of those
		values - subscription_unverified - can only arise on a workspace that
		authenticates with a CHAT SUBSCRIPTION rather than an api key, so it names
		the credential kind. That is pool shape. A member therefore gets the
		verdict and no cause, and the cause is OMITTED rather than mapped: any
		member-visible value reachable only from subscription_unverified would
		recover it exactly.

		The three payloads are compared to each other, so this fails if a future
		change makes ANY of them distinguishable, not merely if a named field
		reappears.
		"""
		payloads = {}

		self._seed_serving_pool(last_sync_status="failed: fleet returned 502")
		with patch.object(account, "compute_pool_mode", return_value=True):
			payloads["sync_failed"] = self._as_member()

		self._seed_serving_pool()
		with patch.object(account, "_last_turn_errored", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				payloads["turn_error"] = self._as_member()

		self._seed_serving_pool(last_subscription_status="unverified")
		with patch.object(account, "compute_pool_mode", return_value=True):
			payloads["subscription_unverified"] = self._as_member()

		for cause, out in payloads.items():
			self.assertEqual(out, {"state": "attention"}, f"{cause} was not collapsed")
		self.assertEqual(len({tuple(sorted(p.items())) for p in payloads.values()}), 1)

	def test_the_admin_can_still_tell_those_three_apart(self):
		"""The control for the test above. If _llm_health stopped distinguishing
		the causes, the collapse assertion would pass vacuously and #714's work
		would have been silently undone - so assert here that the distinction
		still exists on the admin side, and is only dropped on the way to a
		member."""
		# No inline try/finally, unlike _as_member: this switches TO the identity
		# tearDown restores anyway, and tearDown's first statement is the restore,
		# so an assertion failing below cannot strand the shared site.
		frappe.set_user("Administrator")
		reasons = []

		self._seed_serving_pool(last_sync_status="failed: fleet returned 502")
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				reasons.append(account.get_llm_connection_status()["attention_reason"])

		self._seed_serving_pool()
		with patch.object(account, "_last_turn_errored", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
					reasons.append(account.get_llm_connection_status()["attention_reason"])

		self._seed_serving_pool(last_subscription_status="unverified")
		with patch.object(account, "compute_pool_mode", return_value=True):
			with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
				reasons.append(account.get_llm_connection_status()["attention_reason"])

		self.assertEqual(reasons, ["sync_failed", "turn_error", "subscription_unverified"])

	# ---- fail closed ------------------------------------------------------ #

	def test_an_unrecognised_health_value_degrades_to_down(self):
		"""The clamp. _llm_health is stubbed here BECAUSE the subject is what
		happens when it grows a fifth value the member vocabulary has no word
		for - there is no way to reach that branch through real settings. A state
		the SPA cannot map is how a badge falls back to its default, and the
		default this issue is about was green."""
		self._seed_serving_pool()
		with patch.object(account, "_llm_health", return_value=("quiescent", "")):
			with patch.object(account, "compute_pool_mode", return_value=True):
				out = self._as_member()
		self.assertEqual(out, {"state": "down"})


class TestGetLlmConnectionStatusIsUnchanged(FrappeTestCase):
	"""jarvis#711 added a member endpoint alongside the admin one and must not
	have touched the admin one. This pins the admin payload's exact key set, so
	a member-tier change that trimmed a field an admin still needs fails here."""

	def test_an_admin_still_gets_every_field(self):
		frappe.set_user("Administrator")
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		with patch.object(account, "_has_llm_config", return_value=True):
			with patch.object(account, "compute_pool_mode", return_value=True):
				with patch.object(account, "_last_turn_errored", return_value=False):
					with patch.object(account, "_last_turn_succeeded", return_value=False):
						with patch.object(admin_client, "post_llm_auth_status", return_value={"data": {}}):
							out = account.get_llm_connection_status()
		self.assertEqual(set(out.keys()), set(ADMIN_ONLY_FIELDS))
		self.assertNotIn("state", out)


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


class TestLastTurnSucceeded(FrappeTestCase):
	"""The positive counterpart (jarvis#714). Shares _latest_completed_turn_error
	with TestLastTurnErrored above, so the filters are asserted there once rather
	than a second time here."""

	def _call(self, rows):
		with patch.object(frappe, "get_all", return_value=rows):
			return account._last_turn_succeeded()

	def test_a_clean_latest_turn_reports_true(self):
		self.assertTrue(self._call([{"error": ""}]))

	def test_an_errored_latest_turn_reports_false(self):
		self.assertFalse(self._call([{"error": "gateway error"}]))

	def test_a_workspace_with_no_turns_yet_reports_false(self):
		"""Absence of evidence must not read as proof of success any more than
		TestLastTurnErrored lets it read as proof of failure."""
		self.assertFalse(self._call([]))


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
