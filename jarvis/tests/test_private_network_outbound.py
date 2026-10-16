"""The outbound half of the connection test and the start/poll proxies (design spec 6.1, 6.6)."""

from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding_contract, private_network
from jarvis.exceptions import AdminContractError
from jarvis.tests.test_private_network_hold import _admin_answers, _live_hold, _thrown

REACH = "https://reach.h3.fleet.example/"
HOLD = _live_hold()
_PERMISSION = "PermissionError"
_DUPLICATE = "DuplicateEntryError"
_INVALID = "InvalidRequestError"
_RATE = "RateLimitExceededError"


def _response(status=200):
	from unittest.mock import MagicMock

	resp = MagicMock()
	resp.status_code = status
	return resp


class TestRunOutboundCheck(FrappeTestCase):
	def setUp(self):
		# Hermetic on any dev site: tests that need test mode on set it themselves.
		conf = patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 0}, clear=False)
		conf.start()
		self.addCleanup(conf.stop)

	def _run(self, get_effect=None, status=200, url=REACH):
		with patch("requests.Session.get", side_effect=get_effect, return_value=_response(status)) as get:
			out = private_network.run_outbound_check(url)
		return out, get

	def test_a_completed_handshake_with_any_status_passes(self):
		for status in (200, 404, 502):
			with self.subTest(status=status):
				out, _ = self._run(status=status)
				self.assertEqual(out, {"ok": True, "reason": "ok"})

	def test_the_request_uses_five_seconds_normal_tls_and_no_redirects(self):
		_, get = self._run()
		args, kwargs = get.call_args
		self.assertEqual(args[0], REACH)
		self.assertEqual(kwargs["timeout"], (5, 5))
		self.assertIs(kwargs["allow_redirects"], False)
		self.assertTrue(kwargs.get("verify", True))

	def test_the_body_is_not_read_and_the_connection_is_closed(self):
		out, get = self._run()
		self.assertIs(get.call_args.kwargs["stream"], True)
		get.return_value.close.assert_called_once()
		self.assertEqual([c[0] for c in get.return_value.method_calls], ["close"])

	def test_the_probe_ignores_proxy_and_netrc_environment(self):
		seen = []

		def spy(session, *a, **kw):
			seen.append(session.trust_env)
			return _response(200)

		with patch("requests.Session.get", spy):
			private_network.run_outbound_check(REACH)
		self.assertEqual(seen, [False])

	def test_failures_map_to_the_four_reasons(self):
		cases = [
			(requests.exceptions.SSLError("bad cert"), "tls"),
			(requests.exceptions.ConnectTimeout("slow"), "timeout"),
			(requests.exceptions.ReadTimeout("slow"), "timeout"),
			(requests.exceptions.ConnectionError("[Errno 111] Connection refused"), "refused"),
			(requests.exceptions.ConnectionError("[Errno -2] Name or service not known"), "dns"),
			(requests.exceptions.ConnectionError("Temporary failure in name resolution"), "dns"),
			(requests.exceptions.ConnectionError("[Errno 113] No route to host"), "timeout"),
		]
		for effect, reason in cases:
			with self.subTest(reason=reason, effect=str(effect)):
				out, _ = self._run(get_effect=effect)
				self.assertEqual(out, {"ok": False, "reason": reason})

	def test_plain_http_is_refused_outside_test_mode_and_never_requested(self):
		out, get = self._run(url="http://reach.h3.fleet.example/")
		self.assertEqual(out, {"ok": False, "reason": "bad_target"})
		get.assert_not_called()

	def test_plain_http_is_allowed_in_test_mode(self):
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False):
			out, get = self._run(url="http://127.0.0.1:8001/")
		self.assertEqual(out, {"ok": True, "reason": "ok"})
		get.assert_called_once()

	def test_a_malformed_target_is_refused(self):
		for url in ("", "not a url", "ftp://reach.example/", "https://", "https://u:p@reach.example/", None):
			with self.subTest(url=url):
				out, get = self._run(url=url)
				self.assertEqual(out, {"ok": False, "reason": "bad_target"})
				get.assert_not_called()


class _Fresh(FrappeTestCase):
	def setUp(self):
		# Hermetic on any dev site: tests that need test mode on set it themselves.
		conf = patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 0}, clear=False)
		conf.start()
		self.addCleanup(conf.stop)
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self._saved = frappe.db.get_single_value("Jarvis Settings", onboarding_contract.CONTEXT_FIELD)
		self.addCleanup(
			frappe.db.set_single_value,
			"Jarvis Settings",
			onboarding_contract.CONTEXT_FIELD,
			self._saved or "",
			update_modified=False,
		)
		onboarding_contract.clear()


class TestStartConnectionTest(_Fresh):
	def test_starts_the_admin_check_and_runs_the_outbound_test_against_the_stored_hold(self):
		onboarding_contract.store_private_hold(HOLD)
		with (
			patch.object(
				admin_client, "start_connection_check", return_value={"check": "JCC-000123"}
			) as start,
			patch("requests.Session.get", return_value=_response(200)) as get,
		):
			out = private_network.start_connection_test()
		self.assertEqual(out, {"ok": True, "check": "JCC-000123", "outbound": {"ok": True, "reason": "ok"}})
		start.assert_called_once_with(site_name=frappe.local.site)
		self.assertEqual(get.call_args.args[0], HOLD["reach_url"])

	def test_the_target_never_comes_from_the_browser(self):
		onboarding_contract.store_private_hold(HOLD)
		frappe.form_dict.reach_url = "https://evil.example/"
		frappe.form_dict.host_ip = "203.0.113.99"
		self.addCleanup(frappe.form_dict.pop, "reach_url", None)
		self.addCleanup(frappe.form_dict.pop, "host_ip", None)
		with (
			patch.object(admin_client, "start_connection_check", return_value={"check": "JCC-000124"}),
			patch("requests.Session.get", return_value=_response(200)) as get,
		):
			private_network.start_connection_test()
		self.assertEqual(get.call_args.args[0], HOLD["reach_url"])

	def test_with_no_stored_hold_it_reserves_one_first(self):
		with (
			patch.object(admin_client, "reserve_private_host", return_value=HOLD) as reserve,
			patch.object(admin_client, "start_connection_check", return_value={"check": "JCC-000125"}),
			patch("requests.Session.get", return_value=_response(200)) as get,
		):
			out = private_network.start_connection_test()
		reserve.assert_called_once()
		self.assertTrue(out["ok"])
		self.assertEqual(get.call_args.args[0], HOLD["reach_url"])

	def test_any_stored_hold_is_used_without_a_time_comparison(self):
		# hold_until is in admin's zone: the bench never judges expiry, admin does.
		onboarding_contract.store_private_hold({**HOLD, "hold_until": "2020-01-01 00:00:00"})
		with (
			patch.object(admin_client, "reserve_private_host", side_effect=AssertionError("reserved")),
			patch.object(admin_client, "start_connection_check", return_value={"check": "JCC-000126"}),
			patch("requests.Session.get", return_value=_response(200)) as get,
		):
			private_network.start_connection_test()
		self.assertEqual(get.call_args.args[0], HOLD["reach_url"])

	def test_a_reserve_refusal_stops_before_any_probe(self):
		body = _thrown(_PERMISSION, "EMAIL_NOT_CONFIRMED", "Confirm first.")
		with (
			_admin_answers(403, body),
			patch.object(admin_client, "start_connection_check", side_effect=AssertionError("started")),
			patch("requests.Session.get", side_effect=AssertionError("probed")),
		):
			out = private_network.start_connection_test()
		self.assertEqual((out["ok"], out["code"]), (False, "EMAIL_NOT_CONFIRMED"))

	def test_admin_refusals_on_their_real_wire_skip_the_outbound_test(self):
		onboarding_contract.store_private_hold(HOLD)
		cases = [
			(409, _DUPLICATE, "CONNECTION_CHECK_BUSY", {}),
			(409, _DUPLICATE, "PRIVATE_HOLD_UNAVAILABLE", {}),
			(403, _PERMISSION, "NOT_PRIVATE_CUSTOMER", {}),
			(400, _INVALID, "INVALID_REQUEST", {}),
			(429, _RATE, "RATE_LIMITED_PRIVATE_NETWORK", {"retry_after_seconds": 120}),
		]
		for status, exc_type, code, extra in cases:
			with self.subTest(code=code):
				with (
					_admin_answers(status, _thrown(exc_type, code, "No.", **extra)),
					patch("requests.Session.get", side_effect=AssertionError("probed")),
				):
					out = private_network.start_connection_test()
				self.assertEqual((out["ok"], out["code"]), (False, code))
				if extra:
					self.assertEqual(out["retry_after_seconds"], 120)

	def test_an_unknown_admin_code_reads_as_generic_copy(self):
		onboarding_contract.store_private_hold(HOLD)
		with (
			_admin_answers(409, _thrown(_DUPLICATE, "SOMETHING_NEW", "Internal detail.")),
			patch("requests.Session.get", side_effect=AssertionError("probed")),
		):
			out = private_network.start_connection_test()
		self.assertFalse(out["ok"])
		self.assertNotIn("Internal detail", out["message"])

	def test_an_answer_without_a_check_name_is_refused(self):
		onboarding_contract.store_private_hold(HOLD)
		with patch.object(admin_client, "start_connection_check", return_value={}):
			out = private_network.start_connection_test()
		self.assertEqual(out["code"], onboarding_contract.BENCH_ADMIN_REJECTED)

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.start_connection_test()


class TestConnectionTestStatus(_Fresh):
	def test_proxies_the_admin_state(self):
		reply = {"status": "Unreachable", "reason": "timeout", "checked_at": "2026-10-14 09:12:03"}
		with patch.object(admin_client, "get_connection_check", return_value=reply) as get:
			out = private_network.connection_test_status("JCC-000123")
		self.assertEqual(out, {"ok": True, **reply})
		get.assert_called_once_with("JCC-000123")

	def test_a_malformed_check_name_never_reaches_admin(self):
		for bad in ("", "JCC-", "jcc-1", "JCC-1; drop", "../x", "JCC-" + "9" * 20):
			with (
				self.subTest(bad=bad),
				patch.object(admin_client, "get_connection_check", side_effect=AssertionError("forwarded")),
			):
				out = private_network.connection_test_status(bad)
				self.assertEqual((out["ok"], out["code"]), (False, "INVALID_REQUEST"))

	def test_an_admin_refusal_is_returned_with_its_code(self):
		with _admin_answers(403, _thrown(_PERMISSION, "NOT_PRIVATE_CUSTOMER", "Not yours.")):
			out = private_network.connection_test_status("JCC-000123")
		self.assertEqual((out["ok"], out["code"]), (False, "NOT_PRIVATE_CUSTOMER"))

	def test_an_unknown_check_reads_as_a_stale_result(self):
		with _admin_answers(404, {"exc_type": "DoesNotExistError", "exception": "x"}):
			out = private_network.connection_test_status("JCC-000999")
		self.assertEqual(out["ok"], True)
		self.assertEqual((out["status"], out["reason"]), ("Failed", "stale"))

	def test_an_uncoded_validation_error_is_not_read_as_stale(self):
		with _admin_answers(400, {"exc_type": "ValidationError", "exception": "x"}):
			out = private_network.connection_test_status("JCC-000999")
		self.assertFalse(out["ok"])

	def test_a_malformed_admin_answer_does_not_crash(self):
		with patch.object(admin_client, "get_connection_check", return_value=None):
			out = private_network.connection_test_status("JCC-000123")
		self.assertEqual(out["ok"], True)

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.connection_test_status("JCC-000123")


class TestStateChangingEndpointsArePostOnly(FrappeTestCase):
	def test_each_one_allows_only_post(self):
		for fn in (
			private_network.start_connection_test,
			private_network.reserve_host,
			private_network.save_public_address,
		):
			with self.subTest(fn=fn.__name__):
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
