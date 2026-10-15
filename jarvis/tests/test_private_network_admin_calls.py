"""The four admin calls of the private-network onboarding (contract sections 4.1 and 4.3)."""

import json
from unittest.mock import MagicMock, patch

import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.exceptions import (
	AdminAuthError,
	AdminContractError,
	AdminRateLimitedError,
	AdminValidationError,
)

NS = "/api/method/jarvis_admin_v2.api.network."


def _resp(status, body):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status
	resp.json.return_value = body
	resp.text = json.dumps(body)
	return resp


class TestCallShapes(FrappeTestCase):
	def test_private_setup_info_is_a_guest_call(self):
		reply = {"enabled": True, "caller_ip": "198.51.100.5"}
		with (
			patch.object(admin_client, "_post_guest", return_value=reply) as guest,
			patch.object(admin_client, "_post", side_effect=AssertionError("must not authenticate")),
		):
			self.assertEqual(admin_client.private_setup_info(), reply)
		guest.assert_called_once_with(path=NS + "private_setup_info", body={}, timeout_s=8)

	def test_private_setup_info_takes_a_short_timeout(self):
		with patch.object(admin_client, "_post_guest", return_value={}) as guest:
			admin_client.private_setup_info(timeout_s=5)
		self.assertEqual(guest.call_args.kwargs["timeout_s"], 5)

	def test_reserve_private_host_is_authenticated_and_has_no_arguments(self):
		reply = {"host_ip": "198.51.100.27", "reach_url": "https://reach.h3.fleet.example/", "hold_until": None}
		with (
			patch.object(admin_client, "_post", return_value=reply) as post,
			patch.object(admin_client, "_post_guest", side_effect=AssertionError("must authenticate")),
		):
			self.assertEqual(admin_client.reserve_private_host(), reply)
		post.assert_called_once_with(path=NS + "reserve_private_host", body={}, timeout_s=60)

	def test_start_connection_check_sends_the_site_name(self):
		with patch.object(admin_client, "_post", return_value={"check": "JCC-000123"}) as post:
			out = admin_client.start_connection_check("erp.example.com")
		self.assertEqual(out, {"check": "JCC-000123"})
		post.assert_called_once_with(
			path=NS + "start_connection_check", body={"site_name": "erp.example.com"}, timeout_s=30
		)

	def test_get_connection_check_sends_the_check_name(self):
		reply = {"status": "Verified", "reason": "ok", "checked_at": "2026-10-14 09:12:03"}
		with patch.object(admin_client, "_post", return_value=reply) as post:
			self.assertEqual(admin_client.get_connection_check("JCC-000123"), reply)
		post.assert_called_once_with(path=NS + "get_connection_check", body={"check": "JCC-000123"}, timeout_s=15)

	def test_the_admin_app_namespace_override_is_honoured(self):
		import frappe

		with (
			patch.dict(frappe.conf, {"jarvis_admin_app": "jarvis_admin"}, clear=False),
			patch.object(admin_client, "_post_guest", return_value={}) as guest,
		):
			admin_client.private_setup_info()
		self.assertEqual(
			guest.call_args.kwargs["path"], "/api/method/jarvis_admin.api.network.private_setup_info"
		)


_PERMISSION = "PermissionError"
_DUPLICATE = "DuplicateEntryError"
_INVALID = "InvalidRequestError"
_RATE = "RateLimitExceededError"


def _thrown(exc_type, code, message="Admin's sentence.", **extra):
	"""The REAL wire shape of a raised refusal (jarvis_admin_v2 codes.refuse_private):
	exc_type / exception / _server_messages next to a top-level ``error``, no ``message``."""
	return {
		"exc_type": exc_type,
		"exception": f"frappe.exceptions.{exc_type}: {message}",
		"_server_messages": json.dumps([json.dumps({"message": message})]),
		"error": {"code": code, "message": message, "recovery": "retry", **extra},
	}


class TestRefusalsReachTheBenchWithTheirCode(FrappeTestCase):
	"""Every private-network refusal keeps its machine code whatever HTTP status or
	exception class admin used (code-first routing in ``_do_post``)."""

	def _do_post(self, status, body):
		with patch("requests.post", return_value=_resp(status, body)):
			return admin_client._do_post(
				"https://admin.example.com/x", {}, {}, 5, "https://admin.example.com"
			)

	def test_each_code_on_its_real_status(self):
		cases = [
			(403, _PERMISSION, "EMAIL_NOT_CONFIRMED"),
			(403, _PERMISSION, "NOT_PRIVATE_CUSTOMER"),
			(403, _PERMISSION, "PRIVATE_NETWORK_DISABLED"),
			(409, _DUPLICATE, "PRIVATE_HOLD_UNAVAILABLE"),
			(409, _DUPLICATE, "PRIVATE_ADDRESS_TAKEN"),
			(409, _DUPLICATE, "CONNECTION_CHECK_BUSY"),
			(400, _INVALID, "PRIVATE_ADDRESS_INVALID"),
			(400, _INVALID, "INVALID_REQUEST"),
		]
		for status, exc_type, code in cases:
			with self.subTest(code=code, status=status):
				with self.assertRaises(AdminContractError) as caught:
					self._do_post(status, _thrown(exc_type, code, f"Sentence for {code}."))
				self.assertEqual(caught.exception.code, code)
				self.assertEqual(str(caught.exception), f"Sentence for {code}.")

	def test_a_403_refusal_is_not_an_auth_failure(self):
		with self.assertRaises(AdminContractError) as caught:
			self._do_post(403, _thrown(_PERMISSION, "EMAIL_NOT_CONFIRMED", "Confirm first."))
		self.assertNotIsInstance(caught.exception, AdminAuthError)

	def test_a_coded_rate_limit_keeps_its_code_and_wait(self):
		body = _thrown(_RATE, "RATE_LIMITED_PRIVATE_NETWORK", "Too many tests.", retry_after_seconds=120)
		with self.assertRaises(AdminRateLimitedError) as caught:
			self._do_post(429, body)
		self.assertEqual(caught.exception.code, "RATE_LIMITED_PRIVATE_NETWORK")
		self.assertEqual(caught.exception.retry_after_seconds, 120)

	def test_a_private_code_survives_a_503(self):
		with self.assertRaises(AdminContractError) as caught:
			self._do_post(503, _thrown("ServiceUnavailableError", "PRIVATE_HOLD_UNAVAILABLE", "No."))
		self.assertEqual(caught.exception.code, "PRIVATE_HOLD_UNAVAILABLE")

	def test_a_403_without_a_private_code_is_still_an_auth_failure(self):
		body = {"exc_type": _PERMISSION, "exception": "x", "_server_messages": json.dumps(
			[json.dumps({"message": "Not permitted"})]
		)}
		with self.assertRaises(AdminAuthError) as caught:
			self._do_post(403, body)
		self.assertEqual(caught.exception.status_code, 403)

	def test_an_invalid_request_refusal_writes_no_error_log(self):
		with patch.object(admin_client.frappe, "log_error") as log:
			with self.assertRaises(AdminContractError):
				self._do_post(400, _thrown(_INVALID, "PRIVATE_ADDRESS_INVALID"))
			# also when the code is absent (an older admin): the class alone is recognised
			with self.assertRaises(AdminValidationError):
				self._do_post(400, {"exc_type": _INVALID, "_server_messages": json.dumps(
					[json.dumps({"message": "bad"})]
				)})
		log.assert_not_called()

	def test_a_returned_shape_under_message_still_works(self):
		body = {"message": {"ok": False, "error": {"code": "EMAIL_NOT_CONFIRMED", "message": "Confirm."}}}
		with self.assertRaises(AdminContractError) as caught:
			self._do_post(409, body)
		self.assertEqual(caught.exception.code, "EMAIL_NOT_CONFIRMED")
