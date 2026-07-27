"""Tests for jarvis.account wrappers and admin_client shims for the
/jarvis-account page. admin_client is mocked - these are unit tests of
the customer-side glue, not of the admin endpoints themselves."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client
from jarvis.exceptions import AdminValidationError

_SNAPSHOTTED_FIELDS = (
	"jarvis_admin_url",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"agent_url",
	"agent_token",
)


def _snapshot_settings() -> dict:
	s = frappe.get_single("Jarvis Settings")
	snap = {}
	for f in _SNAPSHOTTED_FIELDS:
		v = (
			s.get_password(f, raise_exception=False)
			if f.endswith(("_key", "_secret", "_token"))
			else s.get(f)
		)
		snap[f] = v or ""
	return snap


def _restore_settings(snap: dict) -> None:
	s = frappe.get_single("Jarvis Settings")
	for f, v in snap.items():
		s.db_set(f, v)
	frappe.db.commit()


class TestIsOnboarded(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot_settings()

	def tearDown(self):
		_restore_settings(self._snap)

	def test_true_when_admin_key_set(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_api_key", "ak-abc")
		s.db_set("jarvis_admin_api_secret", "as-abc")
		frappe.db.commit()
		self.assertEqual(account.is_onboarded(), {"onboarded": True})

	def test_false_when_admin_key_blank(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_api_key", "")
		s.db_set("jarvis_admin_api_secret", "")
		frappe.db.commit()
		self.assertEqual(account.is_onboarded(), {"onboarded": False})


class TestAccountWrappers(FrappeTestCase):
	def test_get_account_returns_admin_payload(self):
		fake = {
			"subscription_status": "Active",
			"plan": {"name": "p1"},
			"days_remaining": 12,
			"upgrade_plans": [],
		}
		with patch.object(admin_client, "get_account_summary", return_value=fake) as m:
			out = account.get_account()
		m.assert_called_once_with()
		self.assertEqual(out, fake)

	def test_preview_upgrade_passes_target_plan_through(self):
		fake = {"prorated_inr": 500, "diff_per_day": 50.0, "days_remaining": 10, "total_period_days": 30}
		with patch.object(admin_client, "preview_upgrade", return_value=fake) as m:
			out = account.preview_upgrade("plan-pro")
		m.assert_called_once_with("plan-pro")
		self.assertEqual(out, fake)

	def test_start_upgrade_passes_target_plan_through(self):
		fake = {
			"razorpay_order_id": "order_X",
			"razorpay_key_id": "k",
			"amount_inr": 500,
			"target_plan": "plan-pro",
		}
		with patch.object(admin_client, "start_upgrade", return_value=fake) as m:
			out = account.start_upgrade("plan-pro")
		m.assert_called_once_with("plan-pro")
		self.assertEqual(out, fake)

	def test_get_account_surfaces_admin_validation_error_as_frappe_throw(self):
		"""_surface() converts AdminValidationError to frappe.throw so the
		page sees Frappe's standard red toast text instead of a traceback."""
		with patch.object(
			admin_client, "get_account_summary", side_effect=AdminValidationError("plan disabled")
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				account.get_account()
		self.assertIn("plan disabled", str(cm.exception))

	def test_preview_upgrade_surfaces_validation_error(self):
		with patch.object(
			admin_client, "preview_upgrade", side_effect=AdminValidationError("downgrade not supported")
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				account.preview_upgrade("plan-cheap")
		self.assertIn("downgrade not supported", str(cm.exception))


class TestAccountGatesFailClosed(FrappeTestCase):
	"""get_account and preview_upgrade are System-Manager-only.

	The rejection itself is covered by the canonical parametrized sweep in
	test_role_gates.py (both endpoints are entries in GATED_ENDPOINTS, which
	asserts Guest is refused and Administrator is not). This class adds the one
	property that sweep cannot express: the gate must run BEFORE _surface()
	reaches admin_client, so an unauthorised caller can neither leak the
	payload into admin's logs nor burn an admin request per attempt.
	"""

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_rejection_happens_before_any_admin_round_trip(self):
		frappe.set_user("Guest")
		with (
			patch.object(admin_client, "get_account_summary") as get_summary,
			patch.object(admin_client, "preview_upgrade") as prev,
		):
			with self.assertRaises(frappe.PermissionError):
				account.get_account()
			with self.assertRaises(frappe.PermissionError):
				account.preview_upgrade("plan-pro")
		get_summary.assert_not_called()
		prev.assert_not_called()


class TestAdminChatGate(FrappeTestCase):
	"""jarvis.account._admin_chat_gate — the final managed ready-gate for
	is_ready_for_chat. Fail-open, v1-tolerant, positive verdict cached ~2 min.
	admin_client.get_connection is mocked."""

	# The gate now mirrors the release notice onto Jarvis Settings as a side
	# effect (persist({}) when the mock carries none), so snapshot/restore those
	# fields to avoid clobbering a real site's operator state.
	_RELEASE_FIELDS = (
		"release_notice_active",
		"latest_jarvis_version",
		"release_notice_message",
	)

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

	def test_release_notice_persisted_on_gate(self):
		# The gate mirrors an active notice so boot can read it; the returned
		# verdict shape is unchanged (release_notice never rides the gate reply).
		notice = {"active": True, "version": "9.9.9", "message": "Please update"}
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Ready", "release_notice": notice},
		):
			out = account._admin_chat_gate()
		self.assertEqual(out, {"ready": True, "reason": None, "billing_notice": {}})
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 1)
		self.assertEqual(s.latest_jarvis_version, "9.9.9")
		self.assertEqual(s.release_notice_message, "Please update")

	def test_release_notice_cleared_on_gate(self):
		"""The transition that unblocks an updated tenant: admin stops sending a
		notice, so the mirror must zero rather than keep the stale block up."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("release_notice_active", 1)
		s.db_set("latest_jarvis_version", "9.9.9")
		s.db_set("release_notice_message", "stale")
		frappe.db.commit()
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			account._admin_chat_gate()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 0)
		self.assertEqual(s.release_notice_message, "")

	def test_blocks_when_admin_not_ready(self):
		with patch.object(
			admin_client, "get_connection", return_value={"chat_readiness": "Provisioning"}
		) as gc:
			self.assertEqual(
				account._admin_chat_gate(),
				{"ready": False, "reason": "container_provisioning", "detail": "", "billing_notice": {}},
			)
		# Uses the short 8s budget so a slow admin can't stall the SPA/boot path.
		gc.assert_called_once_with(timeout_s=8)

	def test_suspended_is_distinct_from_provisioning(self):
		"""A revoked subscription must NOT read as "still starting up": that
		tells the customer to wait for a container that is never coming back
		instead of renewing."""
		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "Suspended",
				"chat_readiness_reason": "Your subscription has expired.",
			},
		):
			self.assertEqual(
				account._admin_chat_gate(),
				{
					"ready": False,
					"reason": "subscription_suspended",
					"detail": "Your subscription has expired.",
					"billing_notice": {},
				},
			)

	def test_suspended_without_reason_still_classifies(self):
		"""Older admin sends the state with no sentence — the code must still be
		the billing one; the SPA supplies its own fallback copy."""
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Suspended"}):
			self.assertEqual(
				account._admin_chat_gate(),
				{"ready": False, "reason": "subscription_suspended", "detail": "", "billing_notice": {}},
			)

	def test_allows_when_admin_ready(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)

	def test_v1_tolerant_when_key_absent(self):
		# v1 admin (or a v2 not surfacing chat_readiness) → no opinion → allow.
		with patch.object(
			admin_client, "get_connection", return_value={"agent_url": "ws://x", "tenant_status": "running"}
		):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)

	def test_fails_open_on_admin_error(self):
		from jarvis.exceptions import AdminUnreachableError

		with patch.object(
			admin_client, "get_connection", side_effect=AdminUnreachableError("admin is unreachable")
		):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)
		# Fail-open verdict must NOT be negative-cached: a recovered admin is
		# re-probed on the next call rather than being blocked for the TTL.
		self.assertIsNone(frappe.cache().get_value(account._CHAT_GATE_CACHE_KEY))

	def test_billing_notice_is_passed_through(self):
		# The expiry banner rides this verdict; admin owns the wording, the gate
		# only forwards it - on both the ready and the suspended paths.
		notice = {"phase": "expiring", "admin_message": "ends soon", "member_message": "ask admin"}
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Ready", "billing_notice": notice},
		):
			self.assertEqual(account._admin_chat_gate()["billing_notice"], notice)
		frappe.cache().delete_value(account._CHAT_GATE_CACHE_KEY)
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Suspended", "billing_notice": notice},
		):
			self.assertEqual(account._admin_chat_gate()["billing_notice"], notice)

	def test_positive_verdict_is_cached(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}) as gc:
			account._admin_chat_gate()
			account._admin_chat_gate()
		# Second call served from the positive cache → one admin round-trip.
		gc.assert_called_once()

	def test_not_ready_verdict_is_not_cached(self):
		# A transient block must clear on the next call, not stick for the TTL.
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Provisioning"}):
			account._admin_chat_gate()
		self.assertIsNone(frappe.cache().get_value(account._CHAT_GATE_CACHE_KEY))
