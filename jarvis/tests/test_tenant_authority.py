"""Tests for the monotonic tenant-authority generation guard (review plan 04
P0-5 / P0-6): the bench consumer of admin's ``tenant_authority_generation`` +
opaque ``tenant_authority_handle`` connection receipt.

Covers the pure decision function, the write_connection integration (the exact
stale-response race, the equal-generation identity breach, the old-admin
compatibility rule), the reset semantics, and the safe repair-required chat-gate
state. admin_client is mocked; nothing here talks to a real control plane."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client, onboarding, settings_reset, tenant_authority

GEN = tenant_authority.GEN_FIELD
HANDLE = tenant_authority.HANDLE_FIELD


class TestAuthorityDecide(FrappeTestCase):
	"""The pure monotonic-acceptance rule. No I/O - these pin the exact branch
	behaviour so a mutation to the guard is caught here first."""

	def test_first_connection_accepts(self):
		# Nothing stored yet (fresh bench / post-reset): the first fenced
		# connection is accepted on its own terms.
		self.assertEqual(tenant_authority.decide(None, None, 3, "A"), tenant_authority.ACCEPT)

	def test_higher_generation_accepts(self):
		self.assertEqual(tenant_authority.decide(3, "A", 4, "B"), tenant_authority.ACCEPT)

	def test_lower_generation_rejects(self):
		# The core of P0-5: a slow poll carrying an OLDER generation must lose.
		self.assertEqual(tenant_authority.decide(4, "B", 3, "A"), tenant_authority.REJECT)

	def test_equal_generation_same_handle_is_idempotent(self):
		self.assertEqual(tenant_authority.decide(4, "B", 4, "B"), tenant_authority.ACCEPT)

	def test_equal_generation_different_handle_is_invariant_error(self):
		with self.assertRaises(tenant_authority.AuthorityInvariantError):
			tenant_authority.decide(4, "B", 4, "C")

	def test_equal_generation_missing_handle_stays_idempotent(self):
		# A missing handle on either side can't PROVE a mismatch, so it must not
		# escalate to an invariant error.
		self.assertEqual(tenant_authority.decide(4, "B", 4, None), tenant_authority.ACCEPT)
		self.assertEqual(tenant_authority.decide(4, None, 4, "C"), tenant_authority.ACCEPT)

	def test_generation_less_is_compat(self):
		# Old admin: no generation key at all.
		self.assertEqual(tenant_authority.decide(4, "B", None, None), tenant_authority.COMPAT)

	def test_zero_stored_is_the_no_authority_sentinel(self):
		# generation 0 == "no authority link yet"; it collapses to "nothing
		# stored", so even an equal-0 payload with a different handle is accepted
		# (the pre-backfill window where recency legitimately rules).
		self.assertEqual(tenant_authority.decide(0, "A", 0, "B"), tenant_authority.ACCEPT)
		self.assertEqual(tenant_authority.decide(0, "A", 5, "B"), tenant_authority.ACCEPT)

	def test_incoming_zero_is_rejected_against_a_real_stored_generation(self):
		# A real authority write advanced us to 5; a payload back at 0 is stale.
		self.assertEqual(tenant_authority.decide(5, "B", 0, "A"), tenant_authority.REJECT)


# Fields the integration tests mutate; snapshot/restore so a run against a real
# site (Frappe Singles are not rolled back between tests) is left untouched.
_FIELDS = ("agent_url", GEN, HANDLE, "last_sync_status", "jarvis_admin_customer_email")

# Password fields the principal-change tests exercise (write_connection's
# api_key/api_secret path) alongside agent_token, which the pre-existing tests
# already wrote. Snapshotted/restored the same way: get_password before, drop
# the __Auth row and re-set it (or clear it) after.
_PASSWORD_FIELDS = (
	"agent_token",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"jarvis_admin_customer_password",
)


class _SettingsSnapshotCase(FrappeTestCase):
	def setUp(self):
		# The stale/invariant logs are deduped in the cache; clear them so a prior
		# run cannot swallow the log this test asserts on.
		frappe.cache().delete_keys("jarvis:tenant_authority_stale")
		frappe.cache().delete_keys("jarvis:tenant_authority_invariant")
		s = frappe.get_single("Jarvis Settings")
		self._snap = {f: s.get(f) for f in _FIELDS}
		self._passwords = {f: s.get_password(f, raise_exception=False) or "" for f in _PASSWORD_FIELDS}

	def tearDown(self):
		from jarvis._password_utils import clear_settings_password, set_settings_password

		s = frappe.get_single("Jarvis Settings")
		for f, v in self._snap.items():
			s.db_set(f, v)
		for f, v in self._passwords.items():
			if v:
				set_settings_password(s, f, v)
			else:
				clear_settings_password(s, f)
		frappe.db.commit()

	def _store(self, agent_url, gen, handle):
		"""Seed a currently-accepted connection directly."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("agent_url", agent_url)
		s.db_set(GEN, gen)
		s.db_set(HANDLE, handle)
		frappe.db.commit()

	def _read(self):
		# The generation is normalised the same way the guard consumes it
		# (tenant_authority._norm_generation): a Single stores its value as text in
		# tabSingles, and on a bench not yet migrated to include the Int field it
		# reads back as a string - which the production code casts, so the test
		# asserts on the cast value rather than the raw storage form.
		s = frappe.get_single("Jarvis Settings")
		gen = s.get(GEN)
		return s.get("agent_url"), int(gen or 0), (s.get(HANDLE) or "")


class TestWriteConnectionGuard(_SettingsSnapshotCase):
	def test_stale_response_race_holds_the_connection(self):
		"""The exact review race: P2 stores generation N+1 (container B); the slow
		P1 with generation N (container A) arrives last and must NOT overwrite."""
		# P2 - the newer poll - lands first and is accepted.
		onboarding.write_connection({"agent_url": "ws://B", "agent_token": "tokB", GEN: 6, HANDLE: "B"})
		self.assertEqual(self._read(), ("ws://B", 6, "B"))
		# P1 - older generation - arrives late. Held, not applied.
		onboarding.write_connection({"agent_url": "ws://A", "agent_token": "tokA", GEN: 5, HANDLE: "A"})
		self.assertEqual(self._read(), ("ws://B", 6, "B"))

	def test_higher_generation_overwrites(self):
		self._store("ws://A", 5, "A")
		onboarding.write_connection({"agent_url": "ws://B", "agent_token": "tokB", GEN: 6, HANDLE: "B"})
		self.assertEqual(self._read(), ("ws://B", 6, "B"))

	def test_equal_generation_different_handle_holds_and_repolls(self):
		"""Equal generation, different serving container: write_connection HOLDS the
		current connection (never overwrites, never wipes) so the next poll can
		retry - "hold + re-poll", never a downgrade onto the other container, never
		a raise that would 500 a payment/confirm caller. Recorded once for ops."""
		self._store("ws://A", 6, "A")
		with patch.object(frappe, "log_error") as log:
			onboarding.write_connection({"agent_url": "ws://C", "agent_token": "tokC", GEN: 6, HANDLE: "C"})
		# Held: the previous connection AND its receipt are intact (not wiped).
		self.assertEqual(self._read(), ("ws://A", 6, "A"))
		# Surfaced for operators (deduped to once).
		self.assertTrue(log.called)

	def test_generation_less_payload_writes_without_regressing(self):
		"""Old admin (no generation key): the connection is still written, but the
		stored generation must NOT drop back - that would re-open the race."""
		self._store("ws://A", 6, "A")
		onboarding.write_connection({"agent_url": "ws://D", "agent_token": "tokD"})
		self.assertEqual(self._read(), ("ws://D", 6, "A"))

	def test_first_fenced_connection_on_a_fresh_bench(self):
		self._store("", 0, "")
		onboarding.write_connection({"agent_url": "ws://A", "agent_token": "tokA", GEN: 3, HANDLE: "A"})
		self.assertEqual(self._read(), ("ws://A", 3, "A"))

	def test_new_principal_at_the_old_generation_is_accepted_not_held(self):
		"""jarvis#693: a stale (generation, handle) left over from a PREVIOUS admin
		principal must not be compared against a fresh principal's own count. The
		generation is a per-customer namespace, so a brand-new customer's first
		claim is always generation 1 - identical to whatever the old customer last
		held if the bench never got past 1 either. Without clearing on principal
		change, this lands on the equal-generation/different-handle branch and is
		HELD forever (the exact bug: pinned to a dead pool container forever,
		reproduced end to end on 2026-08-05/06). A payload carrying new admin
		credentials AND a connection in the SAME call (the shape a signup response
		can take) must still land the new container."""
		# The OLD principal's accepted connection: generation 1, container A.
		self._store("ws://A", 1, "A")
		onboarding.write_connection(
			{
				"api_key": "new-key",
				"api_secret": "new-secret",
				"customer": "cust-new@jarvis.invalid",
				"agent_url": "ws://B",
				"agent_token": "tokB",
				GEN: 1,  # the NEW customer's own first claim - also generation 1
				HANDLE: "B",  # a different container than the old principal's
			}
		)
		# Accepted: new principal, new container, new generation namespace - not
		# held as a divergence against the old principal's leftover state.
		self.assertEqual(self._read(), ("ws://B", 1, "B"))

	def test_principal_change_alone_clears_the_stored_authority(self):
		"""Isolate the mechanism: credentials for a new principal arriving WITHOUT
		a connection in the same call (the plain jarvis.onboarding.signup shape -
		agent_url is not part of a signup response) must still forget the old
		principal's (generation, handle), so the connection that follows in a
		LATER call is judged on its own terms."""
		self._store("ws://A", 6, "A")
		onboarding.write_connection({"api_key": "new-key", "api_secret": "new-secret"})
		_url, gen, handle = self._read()
		self.assertEqual((gen, handle), (0, ""))
		# The old connection itself is untouched by a credentials-only call - only
		# the accepted-authority receipt is forgotten, exactly like tenant_authority
		# .clear()'s documented contract.
		self.assertEqual(_url, "ws://A")

	def test_plain_agent_url_sync_does_not_clear_authority(self):
		"""Regression guard: a call with no new principal (a daily sync_connection
		poll, or a slow/duplicate connection payload) must NOT clear the accepted
		authority - only credentials arriving trigger the reset."""
		self._store("ws://A", 6, "A")
		onboarding.write_connection({"agent_url": "ws://A", "agent_token": "tokA", GEN: 6, HANDLE: "A"})
		self.assertEqual(self._read(), ("ws://A", 6, "A"))

	def test_standalone_customer_password_does_not_clear_authority(self):
		"""A lone customer_password is not proof of a new admin login: admin
		re-serves it on every poll for up to an hour after minting it
		(billing/signup.py ``_serve_signup_password``, kept-until-TTL not
		deleted-on-read), and a fully-connected bench can still hit that poll
		within the window. Clearing on this field alone would drop the P0-5
		protection on an otherwise healthy connection for no reason - the real
		new-principal signal is api_key/api_secret/customer, which always
		accompanies a GENUINE new login and already trips the clear above."""
		self._store("ws://A", 6, "A")
		onboarding.write_connection({"customer_password": "rotated-secret"})
		self.assertEqual(self._read(), ("ws://A", 6, "A"))

	def test_signup_then_confirm_payment_across_two_calls_lands_the_new_container(self):
		"""The real shape jarvis#693 reproduced in: credentials and the connection
		arrive in TWO SEPARATE write_connection calls, not one. ``onboarding.signup``
		persists the new principal's api_key/customer with no agent_url yet (nothing
		to connect to before payment); ``finish_payment`` writes agent_url later, in
		its own call, once admin's confirm_payment returns the container. Between
		them this bench still held a PREVIOUS principal's (generation, handle) from
		a prior tenancy that was never reset (an admin-side purge_customer with no
		matching bench-side reset_onboarding, the documented two-step dev recipe run
		as one step). The new customer's own first claim is generation 1 - the same
		number the old principal was already sitting at - so without the clear on
		the credentials-only call, the confirm_payment call's guard() sees an
		equal-generation/different-handle divergence and holds forever."""
		self._store("ws://A", 1, "A")
		# signup(): new principal, no container yet.
		onboarding.write_connection(
			{"api_key": "new-key", "api_secret": "new-secret", "customer": "cust-new@jarvis.invalid"}
		)
		# finish_payment(): the connection arrives in its OWN call, later.
		onboarding.write_connection({"agent_url": "ws://B", "agent_token": "tokB", GEN: 1, HANDLE: "B"})
		self.assertEqual(self._read(), ("ws://B", 1, "B"))


class TestResetClearsAuthority(_SettingsSnapshotCase):
	def test_disconnect_agent_transport_clears_authority(self):
		"""Self-serve workspace reset: the rebuilt container is a new generation,
		so the accepted receipt must be forgotten or the re-attach poll is rejected
		as 'older'."""
		self._store("ws://A", 6, "A")
		settings = frappe.get_single("Jarvis Settings")
		onboarding._disconnect_agent_transport(settings)
		_url, gen, handle = self._read()
		self.assertEqual((gen, handle), (0, ""))

	def test_reset_onboarding_cli_clears_both_fields(self):
		"""The bench reset-onboarding CLI composes from settings_reset.CONNECTION;
		both authority fields must be in its clear set."""
		self.assertIn(HANDLE, settings_reset.CONNECTION.blank)
		self.assertIn(GEN, settings_reset.CONNECTION.zero)


class TestRepairRequiredGate(FrappeTestCase):
	"""review plan 04 P0-6: admin's SupportRequired envelope must surface as a
	safe blocked state - not 'still provisioning', not 'renew'."""

	_RELEASE_FIELDS = ("release_notice_active", "latest_jarvis_version", "release_notice_message")

	def setUp(self):
		frappe.cache().delete_value(account._CHAT_GATE_CACHE_KEY)
		s = frappe.get_single("Jarvis Settings")
		self._rn_snap = {f: s.get(f) for f in self._RELEASE_FIELDS}

	def tearDown(self):
		frappe.cache().delete_value(account._CHAT_GATE_CACHE_KEY)
		s = frappe.get_single("Jarvis Settings")
		for f, v in self._rn_snap.items():
			s.db_set(f, v)
		frappe.db.commit()

	def test_support_required_maps_to_safe_repair_state(self):
		reason = "Your payment is safe — please don't retry payment; we'll have you back shortly."
		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "SupportRequired",
				"chat_readiness_reason": reason,
				"code": "TENANT_AUTHORITY_REPAIR_REQUIRED",
			},
		):
			out = account._admin_chat_gate()
		self.assertFalse(out["ready"])
		self.assertEqual(out["reason"], "authority_repair_required")
		self.assertEqual(out["detail"], reason)
		# No renew CTA, no waiting-for-container framing.
		self.assertNotEqual(out["reason"], "subscription_suspended")
		self.assertNotEqual(out["reason"], "container_provisioning")
		# Nothing to renew: no billing notice rides the safe state.
		self.assertEqual(out["billing_notice"], {})
		# A safe blocked state is never cached as a positive verdict.
		self.assertIsNone(frappe.cache().get_value(account._CHAT_GATE_CACHE_KEY))
