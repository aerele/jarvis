"""start_signup on a private-network bench asks admin first and refuses with the "not
available yet" sentence when private setup is off (design spec 5.3)."""

import json
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding, onboarding_contract
from jarvis.exceptions import (
	AdminAuthError,
	AdminContractError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

BILLING = {"contact_number": "+91 90000 00000"}
ADDRESS = "https://erp.acme.example.com"


class _Reached(Exception):
	"""Raised by the patched admin_client.signup to prove the gate let the call through."""


def _resp(status, body):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status
	resp.json.return_value = body
	resp.text = json.dumps(body)
	return resp


def _thrown(exc_type, code, message):
	"""The REAL wire shape of a raised admin refusal (no ``message`` key)."""
	return {
		"exc_type": exc_type,
		"exception": f"frappe.exceptions.{exc_type}: {message}",
		"_server_messages": json.dumps([json.dumps({"message": message})]),
		"error": {"code": code, "message": message, "recovery": "contact_support"},
	}


class TestStartSignupPrivateGate(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.local.response.pop, "error", None)

	def _start(self):
		return onboarding.start_signup(
			email="owner@acme.example.com",
			company="Acme",
			plan="Annual Plan",
			billing=BILLING,
			terms_accepted=True,
		)

	def _private(self, info=None, side_effect=None, private=True):
		return (
			patch.object(admin_client, "is_private_mode", return_value=private),
			patch.object(admin_client, "private_setup_info", return_value=info, side_effect=side_effect),
		)

	def test_switched_off_refuses_with_the_stamped_code_and_never_signs_up(self):
		a, b = self._private(info={"enabled": False, "caller_ip": "198.51.100.5"})
		with a, b, patch.object(admin_client, "signup", side_effect=AssertionError("signed up")):
			with self.assertRaises(frappe.ValidationError) as caught:
				self._start()
		self.assertIn(onboarding.PRIVATE_NETWORK_UNAVAILABLE, str(caught.exception))
		error = frappe.local.response["error"]
		self.assertEqual(error["code"], "PRIVATE_NETWORK_DISABLED")
		self.assertEqual(error["message"], "Private network setup isn't available yet. Contact support.")

	def test_an_admin_without_the_method_reads_as_not_available(self):
		a, b = self._private(side_effect=AdminValidationError("Failed to get method for command"))
		with a, b, patch.object(admin_client, "signup", side_effect=AssertionError("signed up")):
			with self.assertRaises(frappe.ValidationError):
				self._start()
		self.assertEqual(frappe.local.response["error"]["code"], "PRIVATE_NETWORK_DISABLED")

	def test_an_unreachable_admin_says_so_instead_of_claiming_it_is_off(self):
		a, b = self._private(side_effect=AdminUnreachableError("down"))
		with a, b, patch.object(admin_client, "signup", side_effect=AssertionError("signed up")):
			with self.assertRaises(frappe.ValidationError) as caught:
				self._start()
		self.assertIn("unreachable", str(caught.exception))
		self.assertNotEqual(frappe.local.response["error"]["code"], "PRIVATE_NETWORK_DISABLED")

	def test_a_bare_429_reads_as_too_many_attempts_not_as_off(self):
		a, b = self._private(side_effect=AdminRateLimitedError("slow down", retry_after_seconds=0))
		with a, b, patch.object(admin_client, "signup", side_effect=AssertionError("signed up")):
			with self.assertRaises(frappe.ValidationError) as caught:
				self._start()
		self.assertIn("Too many attempts. Try again in a minute.", str(caught.exception))
		error = frappe.local.response["error"]
		self.assertNotEqual(error["code"], "PRIVATE_NETWORK_DISABLED")
		self.assertEqual(error["message"], "Too many attempts. Try again in a minute.")

	def test_an_unknown_admin_failure_reads_as_not_available_never_a_crash(self):
		a, b = self._private(side_effect=AdminAuthError("nope", status_code=403))
		with a, b, patch.object(admin_client, "signup", side_effect=AssertionError("signed up")):
			with self.assertRaises(frappe.ValidationError):
				self._start()
		self.assertEqual(frappe.local.response["error"]["code"], "PRIVATE_NETWORK_DISABLED")

	def test_enabled_lets_the_signup_through(self):
		a, b = self._private(info={"enabled": True, "caller_ip": "198.51.100.5"})
		with a, b, patch.object(admin_client, "signup", side_effect=_Reached):
			with self.assertRaises(_Reached):
				self._start()

	def test_a_public_bench_never_asks_admin(self):
		a, b = self._private(private=False, side_effect=AssertionError("asked admin"))
		with a, b, patch.object(admin_client, "signup", side_effect=_Reached):
			with self.assertRaises(_Reached):
				self._start()


class TestSignupRefusalsOverTheWire(FrappeTestCase):
	"""Admin's own signup refusals, through the real ``_do_post``, as the SPA receives them:
	admin's sentence, its code stamped on the response, and a NON-auth HTTP status."""

	CASES = [
		(403, "PermissionError", "PRIVATE_NETWORK_DISABLED"),
		(400, "InvalidRequestError", "PRIVATE_ADDRESS_INVALID"),
		(409, "DuplicateEntryError", "PRIVATE_ADDRESS_TAKEN"),
		(400, "InvalidRequestError", "INVALID_REQUEST"),
	]

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.local.response.pop, "error", None)

	def _signup(self, status, body):
		patches = (
			patch.object(admin_client, "is_private_mode", return_value=True),
			patch.object(admin_client, "private_setup_info", return_value={"enabled": True}),
			patch.object(admin_client, "private_public_address", return_value=ADDRESS),
			patch.object(onboarding, "_has_admin_credentials", return_value=True),
			patch("requests.post", return_value=_resp(status, body)),
		)
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return onboarding.start_signup(
			email="owner@acme.example.com",
			company="Acme",
			plan="Annual Plan",
			billing=BILLING,
			terms_accepted=True,
		)

	def test_each_refusal_keeps_admins_sentence_and_code_and_is_not_an_auth_status(self):
		for status, exc_type, code in self.CASES:
			with self.subTest(code=code):
				frappe.local.response.pop("error", None)
				sentence = f"Admin's sentence for {code}."
				with patch.object(admin_client, "resume_pending_signup") as resume:
					with self.assertRaises(frappe.ValidationError) as caught:
						self._signup(status, _thrown(exc_type, code, sentence))
				resume.assert_not_called()
				self.assertIn(sentence, str(caught.exception))
				self.assertEqual(frappe.local.response["error"]["code"], code)
				# The wire status of a thrown ValidationError is its class' 417, whatever
				# admin used: never the 401/403 that frappe-ui reads as a lost session.
				self.assertEqual(caught.exception.http_status_code, 417)
				self.assertNotIn(caught.exception.http_status_code, (401, 403))

	def test_an_address_taken_conflict_does_not_trigger_the_failed_payment_resume(self):
		with patch.object(admin_client, "resume_pending_signup") as resume:
			with self.assertRaises(frappe.ValidationError):
				self._signup(409, _thrown("DuplicateEntryError", "PRIVATE_ADDRESS_TAKEN", "Taken."))
		resume.assert_not_called()


class TestGateOrder(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.local.response.pop, "error", None)

	def test_parked_money_refuses_before_admin_is_asked(self):
		with (
			patch.object(admin_client, "is_private_mode", return_value=True),
			patch.object(
				admin_client, "private_setup_info", side_effect=AssertionError("asked admin")
			) as asked,
			patch.object(onboarding_contract, "awaiting_reconciliation", return_value=True),
			patch.object(onboarding, "_refuse_while_money_is_parked", side_effect=_Reached),
		):
			with self.assertRaises(_Reached):
				onboarding.start_signup(
					email="owner@acme.example.com",
					company="Acme",
					plan="Annual Plan",
					billing=BILLING,
					terms_accepted=True,
				)
		asked.assert_not_called()


class TestPrivateCodesAreNeverADuplicateSignup(FrappeTestCase):
	def test_every_private_code_as_a_409_duplicate_entry_is_not_a_duplicate(self):
		for code in sorted(admin_client.PRIVATE_NETWORK_CODES):
			with self.subTest(code=code):
				err = AdminContractError(
					"Admin's sentence.", code=code, exc_type="DuplicateEntryError", http_status=409
				)
				self.assertFalse(onboarding_contract.is_duplicate_signup(err))

	def test_a_real_duplicate_still_is(self):
		err = AdminContractError("x", code="ACCOUNT_ALREADY_EXISTS", exc_type="DuplicateEntryError")
		self.assertTrue(onboarding_contract.is_duplicate_signup(err))


class TestSameCustomerResignup(FrappeTestCase):
	"""A declined card sends the customer back through ``start_signup`` on the same account
	and address. Admin answers PRIVATE_ADDRESS_TAKEN (its address gate runs before its
	duplicate check); the bench resumes only when the address is provably the customer's own."""

	TAKEN = AdminContractError(
		"Taken.", code="PRIVATE_ADDRESS_TAKEN", exc_type="DuplicateEntryError", http_status=409
	)

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.local.response.pop, "error", None)
		saved = onboarding_contract.load()
		self.addCleanup(onboarding_contract.save, saved)
		onboarding_contract.save({})

	def _start(self, *, current=ADDRESS, recorded=ADDRESS, creds=True, signup=None):
		if recorded:
			onboarding_contract.record_signup_address(recorded)
		patches = (
			patch.object(admin_client, "is_private_mode", return_value=bool(current)),
			patch.object(admin_client, "private_public_address", return_value=current),
			patch.object(admin_client, "private_setup_info", return_value={"enabled": True}),
			patch.object(admin_client, "assert_public_onboarding_host"),
			patch.object(onboarding, "_require_admin_url"),
			patch.object(onboarding, "_has_admin_credentials", return_value=creds),
			patch.object(onboarding, "write_connection"),
			patch.object(onboarding, "grant_onboarding_admin"),
			patch.object(admin_client, "signup", **(signup or {"side_effect": self.TAKEN})),
		)
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return onboarding.start_signup(
			email="owner@acme.example.com",
			company="Acme",
			plan="Annual Plan",
			billing=BILLING,
			terms_accepted=True,
		)

	def test_taken_on_the_recorded_address_with_credentials_resumes(self):
		with patch.object(admin_client, "resume_pending_signup", return_value={"order": "o1"}) as resume:
			out = self._start()
		resume.assert_called_once()
		self.assertEqual(out["order"], "o1")

	def test_taken_without_credentials_is_still_a_refusal(self):
		with patch.object(admin_client, "resume_pending_signup") as resume:
			with self.assertRaises(frappe.ValidationError):
				self._start(creds=False)
		resume.assert_not_called()

	def test_taken_on_a_different_address_than_the_recorded_one_is_a_refusal(self):
		with patch.object(admin_client, "resume_pending_signup") as resume:
			with self.assertRaises(frappe.ValidationError):
				self._start(current="https://other.acme.example.com")
		resume.assert_not_called()

	def test_taken_with_no_recorded_signup_address_is_a_refusal(self):
		with patch.object(admin_client, "resume_pending_signup") as resume:
			with self.assertRaises(frappe.ValidationError):
				self._start(recorded="")
		resume.assert_not_called()

	def test_a_successful_private_signup_records_the_canonical_address(self):
		self._start(recorded="", signup={"return_value": {}})
		self.assertEqual(onboarding_contract.signup_address(), ADDRESS)

	def test_a_public_signup_records_no_address(self):
		self._start(current="", recorded="", signup={"return_value": {}})
		self.assertEqual(onboarding_contract.signup_address(), "")
