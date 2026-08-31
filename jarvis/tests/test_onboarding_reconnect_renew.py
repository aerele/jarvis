"""Lapsed-customer reconnect->renew re-entry (bench half).

A returning Expired customer proves ownership with a reconnect code; the redeem bundle now carries
``renew_required`` so the wizard lands on plan/pay (and Pay calls renew() to reactivate the EXISTING
subscription + restart the container) instead of riding sync_connection to a container that was
stopped on expiry - the shipped forced-reconnect gate's strand. Also guards that renew() forwards the
chosen target_plan to admin (Frappe drops any request kwarg the signature does not name)."""

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


def _land(data):
	"""Drive _land_reconnect with the credential-writing side effects stubbed."""
	with _land_ctx():
		return onboarding._land_reconnect(data)


def _land_ctx(sync_side_effect=None):
	"""Stub _land_reconnect's side effects; yield the sync_connection mock."""
	from contextlib import contextmanager

	@contextmanager
	def _ctx():
		with (
			# get_single only feeds the patched tenant_authority.clear.
			patch("jarvis.onboarding.frappe.get_single"),
			patch("jarvis.onboarding.write_connection"),
			patch("jarvis.onboarding.grant_onboarding_admin"),
			patch("jarvis.tenant_authority.clear"),
			patch("jarvis.onboarding.sync_connection", side_effect=sync_side_effect) as sync,
		):
			yield sync

	return _ctx()


_READY = {
	"status": "ready",
	"api_key": "k",
	"api_secret": "s",
	"customer": "cust@jarvis.invalid",
	"customer_password": "p",
}


class TestReconnectRenewLanding(FrappeTestCase):
	def test_lapsed_bundle_lands_on_renew_payment(self):
		# renew_required -> the wizard must go to plan/pay (Pay = renew()), NOT connect to a
		# non-serving container and strand on "setting up your workspace".
		out = _land({**_READY, "subscription_status": "Expired", "renew_required": True})
		self.assertEqual(out["status"], "renew_payment")

	def test_pending_payment_still_lands_on_resume_payment(self):
		# Unchanged: an unfinished checkout (no container) resumes its Pending-Payment checkout.
		out = _land({**_READY, "subscription_status": "Pending Payment"})
		self.assertEqual(out["status"], "resume_payment")

	def test_active_bundle_still_lands_connected(self):
		# Unchanged: a paid, running account rides straight to its container.
		out = _land({**_READY, "subscription_status": "Active"})
		self.assertEqual(out["status"], "connected")

	def test_renew_required_wins_over_a_stale_status(self):
		# The explicit flag is authoritative: even if subscription_status reads oddly, a lapsed
		# account never rides sync_connection to a container that isn't serving.
		out = _land({**_READY, "subscription_status": "Active", "renew_required": True})
		self.assertEqual(out["status"], "renew_payment")

	def test_non_ready_bundle_is_surfaced_verbatim(self):
		# Unchanged: anything not ready (invalid/expired/awaiting_code) passes through untouched.
		self.assertEqual(_land({"status": "invalid"})["status"], "invalid")


class TestReconnectEagerTokenSync(FrappeTestCase):
	"""A ``connected`` reconnect syncs the token; the no-container outcomes must not."""

	def test_connected_eagerly_syncs_token(self):
		with _land_ctx() as sync:
			out = onboarding._land_reconnect({**_READY, "subscription_status": "Active"})
		self.assertEqual(out["status"], "connected")
		sync.assert_called_once_with(timeout_s=15)

	def test_renew_payment_does_not_sync(self):
		# No running container to sync to; the token reconciles later at renew()/finish_payment.
		with _land_ctx() as sync:
			out = onboarding._land_reconnect({**_READY, "renew_required": True})
		self.assertEqual(out["status"], "renew_payment")
		sync.assert_not_called()

	def test_resume_payment_does_not_sync(self):
		with _land_ctx() as sync:
			out = onboarding._land_reconnect({**_READY, "subscription_status": "Pending Payment"})
		self.assertEqual(out["status"], "resume_payment")
		sync.assert_not_called()

	def test_sync_failure_still_lands_connected(self):
		# Best-effort: a transient admin hiccup must not fail the landing (cron/button fall back).
		with _land_ctx(sync_side_effect=RuntimeError("admin unreachable")) as sync:
			out = onboarding._land_reconnect({**_READY, "subscription_status": "Active"})
		self.assertEqual(out["status"], "connected")
		sync.assert_called_once_with(timeout_s=15)


class TestRenewForwardsTargetPlan(FrappeTestCase):
	def test_renew_forwards_target_plan_to_admin(self):
		# THE Slice-4 bug: Frappe drops any request kwarg the whitelisted signature does not name,
		# so target_plan must be an explicit param AND forwarded, or a reconnect renewal onto a new
		# plan silently prices off the OLD one.
		with (
			patch("jarvis.onboarding.require_jarvis_admin"),
			patch("jarvis.onboarding.admin_client.renew", return_value={"ok": 1}) as renew,
			patch("jarvis.onboarding.onboarding_contract.augment_pay_page", side_effect=lambda d: d),
		):
			onboarding.renew(provider="razorpay", target_plan="PLAN-XYZ")
		self.assertEqual(renew.call_args.kwargs.get("target_plan"), "PLAN-XYZ")
		self.assertEqual(renew.call_args.kwargs.get("provider"), "razorpay")
