"""Tests for jarvis.account.stop_autopay_to_pay - the tenant-side wrapper for the
Past-Due pay-now flow's step one (kill the still-live Razorpay mandate). admin_client
is mocked; these unit-test the customer-side glue only - the require_jarvis_admin gate
(C1: the only who-can-act control, since the admin hop is single-identity) and the
requesting_user forwarding (I4 audit). Mirrors the gate/forwarding conventions in
test_account.py (TestAccountGatesFailClosed / TestAccountWrappers)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client


class TestStopAutopayToPay(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_requires_jarvis_admin_before_any_admin_round_trip(self):
		# C1: a non-admin is refused BEFORE the mandate-cancel round-trip ever fires.
		frappe.set_user("Guest")
		with patch.object(admin_client, "stop_autopay_to_pay") as spy:
			with self.assertRaises(frappe.PermissionError):
				account.stop_autopay_to_pay()
		spy.assert_not_called()

	def test_forwards_requesting_user_as_the_session_user(self):
		# I4: the human clicking is forwarded across the single-identity hop for audit.
		fake = {"ok": True, "outcome": "neutralized"}
		with patch.object(admin_client, "stop_autopay_to_pay", return_value=fake) as m:
			out = account.stop_autopay_to_pay()
		m.assert_called_once_with(requesting_user=frappe.session.user)
		self.assertEqual(out, fake)
