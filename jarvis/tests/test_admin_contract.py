"""Replay the control plane's PUBLISHED wire fixtures through this bench's reader.

The fixtures in ``jarvis/tests/fixtures/admin_contract/`` are a copy of what
jarvis-admin-v2 asserts its own serializer produces (see PROVENANCE.md). Every
test here feeds one of them to ``requests.post`` and lets the real
``admin_client`` parse it, so what is under test is the actual boundary and not
a local restatement of it.

That is the whole point. The last time each side kept its own statement of this
contract, admin reworded its duplicate-signup message, the bench's
failed-payment resume string-matched the old sentence, and BOTH suites stayed
green while every declined-card customer hit a dead end - because the only copy
of that sentence left in this tree was the bench suite's own fixture.

Three properties these tests exist to hold:

  - the reader handles the contract's TWO wire shapes (a returned envelope nests
    the error under Frappe's ``message`` key; a raised exception parks the same
    object at the top level next to ``exc_type``);
  - a code, a capability flag and a recovery hint survive the crossing intact,
    including ones this build has never heard of;
  - an admin too old to send a code FAILS CLOSED onto the generic path - it
    never falls back to reading the sentence.
"""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding_contract
from jarvis.exceptions import (
	AdminContractError,
	AdminRateLimitedError,
	AdminValidationError,
)
from jarvis.tests.test_onboarding_sync import _restore_settings, _snapshot_settings

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "admin_contract")


def _load(name: str) -> dict:
	with open(os.path.join(FIXTURE_DIR, f"{name}.json")) as fh:
		return json.load(fh)


def _all_fixtures() -> list[dict]:
	return [_load(f[: -len(".json")]) for f in sorted(os.listdir(FIXTURE_DIR)) if f.endswith(".json")]


def _mock_response(status_code: int, body: dict):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status_code
	resp.json.return_value = body
	resp.text = json.dumps(body)
	return resp


# Which admin_client call answers each fixture's endpoint. The client function is
# the unit under test; the arguments are whatever gets it as far as the wire.
_CALLERS = {
	"jarvis_admin_v2.billing.signup.signup": lambda: admin_client.signup(
		"someone@example.com", "Acme", "some-plan"
	),
	"jarvis_admin_v2.billing.signup.resume_pending_signup": lambda: admin_client.resume_pending_signup(
		"some-plan"
	),
	"jarvis_admin_v2.billing.signup.get_signup_payment_state": admin_client.get_signup_payment_state,
	"jarvis_admin_v2.billing.signup.check_signup_payment_status": admin_client.check_signup_payment_status,
	"jarvis_admin_v2.api.tenant.confirm_payment": lambda: admin_client.confirm_payment({}),
}


class _ContractCase(FrappeTestCase):
	"""Credentials + admin URL for the authenticated fixtures, and a stubbed
	site URL so the guest signup call's pre-flight doesn't reach for one.

	Settings are snapshotted with the onboarding suite's own helpers: Frappe
	Singles are not rolled back between tests, so a run against a real site must
	put the operator's onboarded state back exactly as it found it."""

	def setUp(self):
		self._snap = _snapshot_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("jarvis_admin_url", "https://admin.example.com")
		settings.db_set("jarvis_admin_api_key", "contract-key")
		settings.db_set("jarvis_admin_api_secret", "contract-secret")
		# No OAuth password: the legacy api_key path reaches _do_post in one hop,
		# which is the parser these tests are about.
		settings.db_set("jarvis_admin_customer_email", "")
		settings.db_set("jarvis_admin_customer_password", "")
		settings.db_set(onboarding_contract.CONTEXT_FIELD, "")
		frappe.db.commit()
		frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)
		self._get_url = patch("frappe.utils.get_url", return_value="https://erp.example.com")
		self._get_url.start()

	def tearDown(self):
		self._get_url.stop()
		_restore_settings(self._snap)
		frappe.cache().delete_value(admin_client._OAUTH_CACHE_KEY)

	def replay(self, name: str):
		"""Run the fixture through the real client. Returns admin's data on a
		success fixture; raises exactly what the bench would raise otherwise."""
		fixture = _load(name)
		caller = _CALLERS[fixture["endpoint"]]
		with patch(
			"requests.post",
			MagicMock(return_value=_mock_response(fixture["http_status"], fixture["body"])),
		):
			return caller()

	def replay_error(self, name: str, expected=AdminContractError):
		with self.assertRaises(expected) as ctx:
			self.replay(name)
		return ctx.exception


class TestEveryFixtureCrossesTheBoundary(_ContractCase):
	"""The sweep: every published fixture, through the real parser, with its
	machine-readable half intact on the other side. A new fixture copied from
	admin joins this test by existing."""

	def test_every_fixture_keeps_its_code(self):
		for fixture in _all_fixtures():
			name = fixture["name"]
			body = fixture["body"]
			with self.subTest(fixture=name):
				if fixture["http_status"] == 200:
					data = self.replay(name)
					expected = body["message"]["data"]
					self.assertEqual(
						data.get("code"),
						expected.get("code"),
						"the state code must reach the facade unaltered",
					)
					for flag in ("can_initiate_payment", "can_check_status", "can_reconnect"):
						if flag in expected:
							self.assertEqual(
								data.get(flag),
								expected[flag],
								f"{flag} decides whether a button may be rendered",
							)
					continue
				err = self.replay_error(name, expected=Exception)
				coded = body.get("error") or body.get("message", {}).get("error") or {}
				if not coded.get("code"):
					# legacy_v1: no machine code anywhere. Fail closed - the
					# generic class, never a coded one built out of prose.
					self.assertNotIsInstance(err, AdminContractError)
					continue
				self.assertEqual(
					getattr(err, "code", ""),
					coded["code"],
					"a coded refusal must arrive as a code, not as a sentence",
				)

	def test_no_fixture_speaks_a_newer_contract_than_this_bench(self):
		"""A fixture from a BREAKING admin release must fail the suite rather
		than be quietly re-pinned: the facade has to be read again first."""
		for fixture in _all_fixtures():
			with self.subTest(fixture=fixture["name"]):
				self.assertLessEqual(
					int(fixture["contract_version"]),
					onboarding_contract.CONTRACT_VERSION,
					"admin shipped a breaking contract version; read the facade, don't re-pin",
				)


class TestTheTwoWireShapes(_ContractCase):
	"""One reader, two depths. A whitelisted method's RETURN value and a RAISED
	exception do not serialize the same way, and a consumer that only knows one
	of them silently loses the contract on the other."""

	def test_thrown_shape_error_at_the_top_level(self):
		fixture = _load("duplicate_409")
		self.assertIn("error", fixture["body"], "fixture must be the THROWN shape")
		self.assertNotIn("message", fixture["body"])
		err = self.replay_error("duplicate_409")
		self.assertEqual(err.code, onboarding_contract.ACCOUNT_ALREADY_EXISTS)
		self.assertEqual(err.exc_type, "DuplicateEntryError")
		self.assertEqual(err.recovery, "authenticate_or_reconnect")
		self.assertEqual(err.http_status, 409)

	def test_returned_shape_error_under_the_message_key(self):
		fixture = _load("payment_already_active")
		self.assertIn("error", fixture["body"]["message"], "fixture must be the RETURNED shape")
		self.assertNotIn("error", fixture["body"])
		err = self.replay_error("payment_already_active")
		self.assertEqual(err.code, onboarding_contract.PAYMENT_ALREADY_ACTIVE)
		self.assertEqual(err.recovery, "continue_setup")
		self.assertEqual(err.http_status, 409)
		# The extras ride along: a wizard reloading onto this answer renders the
		# subscription status without a second call.
		self.assertEqual(err.error.get("subscription_status"), "Active")

	def test_both_shapes_are_the_same_class_to_a_caller(self):
		self.assertIsInstance(self.replay_error("duplicate_409"), AdminValidationError)
		self.assertIsInstance(self.replay_error("payment_already_active"), AdminValidationError)


class TestOldAdminFailsClosed(_ContractCase):
	"""A fleet mid-upgrade still has control planes that predate the contract.
	Their duplicate rejection carries Frappe's exc_type and a SENTENCE, and the
	sentence is exactly what must not be read."""

	def test_legacy_duplicate_has_no_code_and_is_not_a_contract_error(self):
		err = self.replay_error("legacy_v1_duplicate_409", expected=AdminValidationError)
		self.assertNotIsInstance(err, AdminContractError)
		self.assertEqual(getattr(err, "code", ""), "")
		self.assertEqual(err.exc_type, "DuplicateEntryError")

	def test_legacy_duplicate_still_reaches_the_resume(self):
		"""exc_type is the signal that spans every admin ever deployed: every
		version of _reject_duplicate_email back to the v2 fork throws
		frappe.DuplicateEntryError. That is why no prose fallback is needed."""
		err = self.replay_error("legacy_v1_duplicate_409", expected=AdminValidationError)
		self.assertTrue(onboarding_contract.is_duplicate_signup(err))

	def test_an_unknown_shape_is_not_half_read(self):
		"""Fail closed on anything that isn't the published contract: a body with
		an ``error`` object but no code is treated as "no contract", not as a
		coded refusal with an empty code."""
		body = {"exc_type": "ValidationError", "error": {"message": "something went wrong"}}
		with (
			patch("requests.post", MagicMock(return_value=_mock_response(409, body))),
			self.assertRaises(AdminValidationError) as ctx,
		):
			admin_client.resume_pending_signup("some-plan")
		self.assertNotIsInstance(ctx.exception, AdminContractError)


class TestCapabilityFlagsAndDisclosure(_ContractCase):
	"""The fields a payment surface must render FROM rather than re-derive."""

	def test_awaiting_manual_reconciliation_survives_with_its_pay_gate(self):
		"""Money the gateway holds that we could not credit. The CODE is the
		ordinary pending one on purpose - a decline would invite a second
		payment - so it is the flag, and can_initiate_payment, that suppress the
		Pay affordance. Losing either in transit re-charges a customer."""
		data = self.replay("reconcile_unmatched_payment")
		self.assertEqual(data["code"], onboarding_contract.PAYMENT_CONFIRMATION_PENDING)
		self.assertTrue(data["awaiting_manual_reconciliation"])
		self.assertFalse(data["can_initiate_payment"])
		self.assertTrue(data["gateway_consulted"])

	def test_gateway_consulted_is_carried_not_inferred(self):
		"""False on the passive poll's answers and true only when a provider was
		really reached IN THIS CALL, so "checked just now" can never be rendered
		as "and the gateway confirmed it"."""
		self.assertTrue(self.replay("reconcile_paid_active")["gateway_consulted"])
		self.assertFalse(self.replay("account_reconnect_required")["gateway_consulted"])

	def test_reconnect_is_offered_from_the_flag(self):
		data = self.replay("account_reconnect_required")
		self.assertEqual(data["code"], onboarding_contract.ACCOUNT_RECONNECT_REQUIRED)
		self.assertTrue(data["can_reconnect"])
		self.assertFalse(data["can_initiate_payment"])

	def test_authorized_mandate_refuses_a_second_intent(self):
		"""Money has already moved on an authorized mandate; a new intent would
		authorize a second one and charge the signup fee twice."""
		data = self.replay("reconcile_authorized_pending_confirm")
		self.assertEqual(data["code"], onboarding_contract.PAYMENT_AUTHORIZED_PENDING_CONFIRM)
		self.assertFalse(data["can_initiate_payment"])
		self.assertEqual(data["recovery"], "confirm_payment")

	def test_due_today_disclosure_crosses_intact(self):
		"""What the customer is actually charged today, which the wizard used to
		compute from price_inr and get wrong whenever a signup fee or an anchored
		trial was involved."""
		data = self.replay("reconcile_pending")
		self.assertEqual(data["due_today_inr"], 12000.0)
		self.assertEqual(data["signup_fee_inr"], 0.0)
		self.assertEqual(data["plan"], {"name": "oc-test-annual", "label": "oc-test-annual"})

	def test_effective_trial_crosses_intact(self):
		"""The trial as anchored on THIS attempt, not the plan's advertised
		length - the number a resumed wizard has to render."""
		data = self.replay("state_mandate_pending_razorpay")
		self.assertEqual(data["effective_trial_days"], 14)
		self.assertEqual(data["due_today_inr"], 0.0)
		self.assertIn("effective_first_charge_at", data)

	def test_verification_expiry_crosses_intact(self):
		data = self.replay("verification_required")
		self.assertEqual(data["code"], onboarding_contract.SIGNUP_VERIFICATION_REQUIRED)
		self.assertTrue(data["pending_verification"])
		self.assertIn("verification_expires_at", data)

	def test_attempt_id_and_generation_cross_intact(self):
		"""The opaque handle a support conversation may quote, and the fence a
		late callback is checked against. Neither is derivable locally."""
		data = self.replay("resume_intent")
		self.assertEqual(data["attempt_id"], "att_placeholder")
		self.assertEqual(data["generation"], 2)

	def test_a_field_this_build_never_heard_of_still_arrives(self):
		"""Additive-only is admin's rule; passing everything through unfiltered
		is what makes it true on this side. A bench that dropped unknown keys
		would turn every additive release into a coordinated one."""
		fixture = _load("reconcile_pending")
		body = json.loads(json.dumps(fixture["body"]))
		body["message"]["data"]["some_future_field"] = {"nested": 1}
		with patch("requests.post", MagicMock(return_value=_mock_response(200, body))):
			data = admin_client.check_signup_payment_status()
		self.assertEqual(data["some_future_field"], {"nested": 1})


class TestRateLimitIsNotADecline(_ContractCase):
	"""Back off, and your payment failed, are opposite instructions - and a
	caller that can only read the status has to guess which one it got."""

	def test_coded_429_carries_its_code_and_backoff(self):
		err = self.replay_error("payment_check_rate_limited", expected=AdminRateLimitedError)
		self.assertEqual(err.code, onboarding_contract.PAYMENT_CHECK_RATE_LIMITED)
		self.assertEqual(err.retry_after_seconds, 1234)
		self.assertEqual(err.recovery, "retry")

	def test_the_facade_does_not_turn_it_into_a_decline(self):
		err = self.replay_error("payment_check_rate_limited", expected=AdminRateLimitedError)
		error, status = onboarding_contract.error_object(err)
		self.assertEqual(status, 429)
		self.assertEqual(error["code"], onboarding_contract.PAYMENT_CHECK_RATE_LIMITED)
		self.assertNotEqual(error["code"], onboarding_contract.PAYMENT_DECLINED)


class TestDeclineCodes(_ContractCase):
	def test_mandate_decline_carries_the_code(self):
		err = self.replay_error("payment_declined_mandate")
		self.assertEqual(err.code, onboarding_contract.PAYMENT_DECLINED)

	def test_a_declined_check_is_a_report_not_a_refusal(self):
		"""A check never creates or replaces an intent, so a decline comes back
		as an ordinary 200 answer with a code - the caller decides whether to
		open a replacement."""
		data = self.replay("reconcile_declined")
		self.assertEqual(data["code"], onboarding_contract.PAYMENT_DECLINED)
		self.assertTrue(data["can_initiate_payment"])
		self.assertEqual(data["recovery"], "retry")

	def test_terminal_signup_is_not_a_checkout(self):
		data = self.replay("signup_terminal")
		self.assertEqual(data["code"], onboarding_contract.SIGNUP_TERMINAL)


class TestTheCorpusIsWhatItClaims(_ContractCase):
	"""PROVENANCE.md carries a checksum table. A table nobody recomputes is a
	comment, and a vendored file edited in place to make a test pass is exactly
	the drift this directory exists to prevent."""

	def _provenance(self) -> str:
		with open(os.path.join(FIXTURE_DIR, "PROVENANCE.md")) as fh:
			return fh.read()

	def _declared(self) -> dict:
		out = {}
		for line in self._provenance().splitlines():
			parts = line.split()
			if len(parts) == 2 and len(parts[0]) == 64 and parts[1].endswith(".json"):
				out[parts[1]] = parts[0]
		return out

	def test_every_vendored_file_matches_its_recorded_checksum(self):
		declared = self._declared()
		self.assertTrue(declared, "PROVENANCE.md must carry the vendored checksum table")
		for name, want in declared.items():
			with self.subTest(fixture=name):
				with open(os.path.join(FIXTURE_DIR, name), "rb") as fh:
					got = hashlib.sha256(fh.read()).hexdigest()
				self.assertEqual(got, want, f"{name} differs from the vendored copy - re-sync, do not re-pin")

	def test_every_file_is_either_vendored_or_declares_itself_bench_authored(self):
		"""No third category. A file that is neither a checksummed copy of
		admin's nor an explicit bench-authored case is a local invention wearing
		the authority of a contract."""
		declared = self._declared()
		for name in sorted(os.listdir(FIXTURE_DIR)):
			if not name.endswith(".json"):
				continue
			with self.subTest(fixture=name):
				if name in declared:
					continue
				produced_by = _load(name[: -len(".json")]).get("produced_by", "")
				self.assertTrue(
					produced_by.startswith("BENCH-AUTHORED"),
					f"{name} is not in the vendored table and does not declare itself bench-authored",
				)


class TestTheDuplicateCodeBranchIsReal(_ContractCase):
	"""Every VENDORED duplicate fixture carries ``exc_type``, so the bench's
	``error.code`` branch was never the thing under test - delete it and the
	suite stayed green. The code-only fixture is what tests it."""

	def test_a_coded_duplicate_with_no_exc_type_is_still_a_duplicate(self):
		err = self.replay_error("duplicate_409_code_only")
		self.assertEqual(err.code, onboarding_contract.ACCOUNT_ALREADY_EXISTS)
		self.assertIsNone(err.exc_type, "the fixture must carry no exc_type, or it tests nothing")
		self.assertTrue(
			onboarding_contract.is_duplicate_signup(err),
			"the code alone must reach the resume; exc_type is Frappe's framing, not the contract",
		)

	def test_the_poll_has_a_verification_state_of_its_own(self):
		"""The state a wizard sits in longest. Its capability flags differ from
		the resume endpoint's answer for the same cohort: nothing has been
		charged, so there is nothing to initiate and nothing to check."""
		data = self.replay("state_verification_required")
		self.assertEqual(data["code"], onboarding_contract.SIGNUP_VERIFICATION_REQUIRED)
		self.assertTrue(data["pending_verification"])
		self.assertFalse(data["can_initiate_payment"])
		self.assertFalse(data["can_check_status"])
		self.assertIn("verification_expires_at", data)


class TestReaderEdges(_ContractCase):
	def test_a_codeless_top_level_error_does_not_hide_the_coded_one(self):
		"""Two ``error`` objects, only one of them the contract's. Stopping at
		the first one seen reported "no contract" for a response that carried
		one, which fails closed into the generic branch and loses the code."""
		body = {
			"error": {"message": "gateway says something went wrong"},
			"message": {
				"ok": False,
				"error": {"code": "PAYMENT_ALREADY_ACTIVE", "recovery": "continue_setup"},
			},
		}
		with (
			patch("requests.post", MagicMock(return_value=_mock_response(409, body))),
			self.assertRaises(AdminContractError) as ctx,
		):
			admin_client.resume_pending_signup("some-plan")
		self.assertEqual(ctx.exception.code, onboarding_contract.PAYMENT_ALREADY_ACTIVE)

	def test_a_permanent_refusal_is_not_reported_as_an_outage(self):
		"""AdminRejectedError is a SUBCLASS of AdminUnreachableError, so a facade
		that checks the parent first turns a deterministic refusal into "admin is
		unwell, keep waiting" - jarvis #542, in a new place."""
		from jarvis.exceptions import AdminRejectedError

		error, status = onboarding_contract.error_object(
			AdminRejectedError(
				"admin returned a 502 error: bad provider slug",
				code="FleetConfigError",
				detail="bad provider slug",
			)
		)
		self.assertEqual(error["code"], "FleetConfigError")
		self.assertEqual(error["message"], "bad provider slug")
		self.assertNotEqual(error["code"], onboarding_contract.BENCH_ADMIN_UNREACHABLE)
		self.assertEqual(status, 502)

	def test_a_malformed_contract_version_does_not_break_the_page(self):
		out = onboarding_contract.success({"code": "X", "contract_version": "not-a-number"})
		self.assertEqual(out["contract_version"], onboarding_contract.CONTRACT_VERSION)


class TestFacadeErrorObjects(_ContractCase):
	"""What the SPA is handed when a call fails: admin's own error object, not a
	re-derivation of it."""

	def test_contract_error_passes_through_verbatim(self):
		err = self.replay_error("payment_already_active")
		error, status = onboarding_contract.error_object(err)
		self.assertEqual(status, 409)
		self.assertEqual(error["code"], onboarding_contract.PAYMENT_ALREADY_ACTIVE)
		self.assertEqual(error["recovery"], "continue_setup")
		self.assertEqual(error["subscription_status"], "Active")

	def test_uncoded_rejection_gets_a_bench_code_not_an_invented_admin_one(self):
		"""An admin too old to speak the contract must not have a code invented
		for it: a BENCH_* code says where the answer came from, and cannot
		collide with admin's append-only vocabulary."""
		err = self.replay_error("legacy_v1_duplicate_409", expected=AdminValidationError)
		error, status = onboarding_contract.error_object(err)
		self.assertEqual(error["code"], onboarding_contract.BENCH_ADMIN_REJECTED)
		self.assertEqual(error["exc_type"], "DuplicateEntryError")
		self.assertEqual(status, 409)


class TestDetailsRejectionIsNotAPaymentFailure(_ContractCase):
	"""Admin refusing what the CUSTOMER typed is a different failure from admin
	refusing the request, and it used to be answered as the latter.

	`BillingMetadataRejected` is a ValidationError SUBCLASS, and admin_client
	matches exc_type by exact string, so it fell through the allowlist into the
	unknown branch and came out as BENCH_ADMIN_REJECTED. That code renders as "the
	payment service refused this request" over "Check the status, or start a new
	payment" - and no payment service had been contacted, nothing existed to
	check, and retrying identical data failed identically forever. The customer
	dead-ended three screens past the field that caused it."""

	def test_billing_metadata_rejection_gets_its_own_code(self):
		err = AdminValidationError("billing.gstin is not a valid GSTIN")
		err.exc_type = "BillingMetadataRejected"
		error, status = onboarding_contract.error_object(err)
		self.assertEqual(error["code"], onboarding_contract.BENCH_SIGNUP_DETAILS_REJECTED)
		self.assertEqual(status, 409)
		# The precise server sentence is what makes this actionable; it must survive.
		self.assertEqual(error["message"], "billing.gstin is not a valid GSTIN")

	def test_an_unknown_exc_type_still_routes_on_the_field_prefix(self):
		"""An admin build whose exception class this bench has never heard of still
		names the field by admin's own convention, so the customer is not punished
		for a version skew."""
		err = AdminValidationError("billing.contact_number is not a valid phone number")
		err.exc_type = "SomeFutureRejection"
		error, _status = onboarding_contract.error_object(err)
		self.assertEqual(error["code"], onboarding_contract.BENCH_SIGNUP_DETAILS_REJECTED)

	def test_an_ordinary_rejection_is_untouched(self):
		"""The generic path must NOT be widened: a refusal that is not about a field
		the customer typed still answers BENCH_ADMIN_REJECTED."""
		err = AdminValidationError("Unsupported payment provider.")
		err.exc_type = "ValidationError"
		error, _status = onboarding_contract.error_object(err)
		self.assertEqual(error["code"], onboarding_contract.BENCH_ADMIN_REJECTED)

	def test_the_predicate_is_not_prose_matching(self):
		"""It keys on the exception class and on a NAMESPACED field prefix, never on
		a sentence. A reworded message that still names its field keeps working; a
		message that merely mentions billing does not qualify."""
		self.assertTrue(onboarding_contract.is_details_rejection("", "BillingMetadataRejected"))
		self.assertTrue(onboarding_contract.is_details_rejection("billing.city is too long"))
		self.assertFalse(onboarding_contract.is_details_rejection("your billing could not be processed"))
		self.assertFalse(onboarding_contract.is_details_rejection(""))
