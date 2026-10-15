"""SPA-facing endpoints for the private-network onboarding: status, admin targets, the
admin reachability check and saving the public address (design spec 5.2, 6.1)."""

import json
import socket
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, private_network
from jarvis.exceptions import AdminAmbiguousError, AdminUnreachableError, AdminValidationError


def _read(field):
	return frappe.db.get_single_value("Jarvis Settings", field, cache=False)


class _AdminUser(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")


class TestNetworkStatus(_AdminUser):
	def test_a_public_bench_is_not_probed(self):
		with (
			patch.object(admin_client, "_public_host_configured", return_value=True),
			patch.object(admin_client, "private_public_address", return_value=""),
			patch.object(admin_client, "private_setup_info", side_effect=AssertionError("probed")),
		):
			out = private_network.network_status()
		self.assertEqual(
			out,
			{
				"host_public": True,
				"admin_reachable": None,
				"network_mode": "",
				"public_address": "",
				"test_mode": False,
			},
		)

	def test_a_private_bench_before_setup_probes_admin_and_leaks_no_address(self):
		for info, expected in (
			({"enabled": True, "caller_ip": "198.51.100.5"}, True),
			(AdminUnreachableError("down"), False),
		):
			with self.subTest(expected=expected):
				probe = {"side_effect": info} if isinstance(info, Exception) else {"return_value": info}
				with (
					patch.object(admin_client, "_public_host_configured", return_value=False),
					patch.object(admin_client, "private_public_address", return_value=""),
					patch.object(admin_client, "private_setup_info", **probe),
				):
					out = private_network.network_status()
				self.assertIs(out["admin_reachable"], expected)
				self.assertFalse(out["host_public"])
				self.assertEqual(out["network_mode"], "")
				# The admin address and the caller's own IP stay out until the customer picks.
				self.assertEqual(
					set(out),
					{"host_public", "admin_reachable", "network_mode", "public_address", "test_mode"},
				)

	def test_a_finished_private_setup_reports_mode_and_address_without_probing(self):
		with (
			patch.object(admin_client, "_public_host_configured", return_value=False),
			patch.object(admin_client, "private_public_address", return_value="https://8.8.8.8"),
			patch.object(admin_client, "private_setup_info", side_effect=AssertionError("probed")),
		):
			out = private_network.network_status()
		self.assertEqual(out["network_mode"], "private")
		self.assertEqual(out["public_address"], "https://8.8.8.8")
		self.assertIsNone(out["admin_reachable"])

	def test_test_mode_is_reported(self):
		with (
			patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False),
			patch.object(admin_client, "_public_host_configured", return_value=True),
		):
			self.assertTrue(private_network.network_status()["test_mode"])

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.network_status()


class TestCheckAdminConnection(_AdminUser):
	def _check(self, **patch_kwargs):
		with patch.object(admin_client, "private_setup_info", **patch_kwargs) as probe:
			out = private_network.check_admin_connection()
		return out, probe

	def test_enabled_and_reachable(self):
		out, probe = self._check(return_value={"enabled": True, "caller_ip": "198.51.100.5"})
		self.assertEqual(out, {"reachable": True, "enabled": True, "caller_ip": "198.51.100.5"})
		self.assertEqual(probe.call_args.kwargs["timeout_s"], 5)

	def test_switched_off_on_admin(self):
		out, _ = self._check(return_value={"enabled": False, "caller_ip": "198.51.100.5"})
		self.assertEqual(out, {"reachable": True, "enabled": False, "caller_ip": "198.51.100.5"})

	def test_unreachable_and_ambiguous_both_read_as_unreachable(self):
		for err in (AdminUnreachableError("down"), AdminAmbiguousError("read timeout")):
			with self.subTest(err=type(err).__name__):
				out, _ = self._check(side_effect=err)
				self.assertEqual(out, {"reachable": False, "enabled": False, "caller_ip": ""})

	def test_an_admin_without_the_method_is_reachable_but_not_enabled(self):
		out, _ = self._check(side_effect=AdminValidationError("Failed to get method for command"))
		self.assertEqual(out, {"reachable": True, "enabled": False, "caller_ip": ""})

	def test_a_junk_caller_ip_is_dropped(self):
		for junk in ("<script>", "10.0.0.999", "::1", "", None, 5):
			with self.subTest(junk=junk):
				out, _ = self._check(return_value={"enabled": True, "caller_ip": junk})
				self.assertEqual(out["caller_ip"], "")


class TestPrivateSetupTargets(_AdminUser):
	def _targets(self, url, resolved=None, error=None, conf=None):
		patches = [
			patch.object(admin_client, "_public_host_configured", return_value=False),
			patch.dict(frappe.conf, {"jarvis_admin_url": url, **(conf or {})}, clear=False),
			patch.object(
				private_network.socket,
				"getaddrinfo",
				side_effect=error,
				return_value=[
					(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in (resolved or [])
				],
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return private_network.private_setup_targets()

	def test_resolves_ipv4_server_side_sorted_and_unique(self):
		out = self._targets(
			"https://fleet.klerk.in", resolved=["178.104.214.114", "178.104.214.114", "1.2.3.4"]
		)
		self.assertEqual(out, {"admin_host": "fleet.klerk.in", "admin_ips": ["1.2.3.4", "178.104.214.114"]})

	def test_an_ip_literal_admin_url_needs_no_dns(self):
		out = self._targets("http://144.91.75.43:8083", error=AssertionError("no dns for a literal"))
		self.assertEqual(out, {"admin_host": "144.91.75.43", "admin_ips": ["144.91.75.43"]})

	def test_falls_back_to_site_config_when_dns_fails(self):
		out = self._targets(
			"https://fleet.klerk.in",
			error=socket.gaierror("no dns"),
			conf={"jarvis_admin_ips": ["178.104.214.114", "not-an-ip"]},
		)
		self.assertEqual(out["admin_ips"], ["178.104.214.114"])

	def test_the_fallback_accepts_a_comma_separated_string(self):
		out = self._targets(
			"https://fleet.klerk.in",
			error=socket.gaierror("x"),
			conf={"jarvis_admin_ips": "1.1.1.1, 8.8.8.8"},
		)
		self.assertEqual(out["admin_ips"], ["1.1.1.1", "8.8.8.8"])

	def test_no_dns_and_no_fallback_is_an_empty_list_not_an_error(self):
		out = self._targets("https://fleet.klerk.in", error=socket.gaierror("x"))
		self.assertEqual(out, {"admin_host": "fleet.klerk.in", "admin_ips": []})

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.private_setup_targets()


class TestSavePublicAddress(_AdminUser):
	def setUp(self):
		super().setUp()
		patcher = patch.object(admin_client, "_public_host_configured", return_value=False)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_good_address_sets_private_mode_in_canonical_form(self):
		out = private_network.save_public_address("HTTPS://Erp.Example.com/")
		self.assertEqual(out, {"ok": True, "public_address": "https://erp.example.com"})
		self.assertEqual(_read("network_mode"), "Private")
		self.assertEqual(_read("public_address"), "https://erp.example.com")

	def test_saving_does_not_fire_the_settings_on_update_hook(self):
		"""Jarvis Settings.on_update pushes credentials to the container; onboarding must not
		retrigger it (same reason write_connection uses db_set)."""
		with patch(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.JarvisSettings.on_update",
			side_effect=AssertionError("on_update fired"),
		):
			private_network.save_public_address("https://8.8.8.8")

	def test_a_bad_address_writes_nothing_and_says_why(self):
		before = (_read("network_mode"), _read("public_address"))
		out = private_network.save_public_address("http://erp.example.com")
		self.assertEqual(out["ok"], False)
		self.assertEqual(out["code"], "PRIVATE_ADDRESS_INVALID")
		self.assertEqual(out["message"], "Use an https:// address. Plain HTTP is not allowed.")
		self.assertEqual((_read("network_mode"), _read("public_address")), before)

	def test_a_bench_with_a_public_host_is_refused(self):
		with patch.object(admin_client, "_public_host_configured", return_value=True):
			out = private_network.save_public_address("https://8.8.8.8")
		self.assertEqual(out["ok"], False)
		self.assertEqual(out["code"], "PUBLIC_HOST_CONFIGURED")

	def test_test_mode_accepts_a_loopback_http_address(self):
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False):
			out = private_network.save_public_address("http://127.0.0.1:8000")
		self.assertEqual(out, {"ok": True, "public_address": "http://127.0.0.1:8000"})

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.save_public_address("https://8.8.8.8")


def _resp(status, body):
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status
	resp.json.return_value = body
	resp.text = json.dumps(body)
	return resp


def _thrown(exc_type, code, message="Admin's sentence.", **extra):
	"""The REAL wire shape of a raised refusal (jarvis_admin_v2 codes.refuse_private)."""
	return {
		"exc_type": exc_type,
		"exception": f"frappe.exceptions.{exc_type}: {message}",
		"_server_messages": json.dumps([json.dumps({"message": message})]),
		"error": {"code": code, "message": message, "recovery": "retry", **extra},
	}


class TestRefusalEndToEnd(_AdminUser):
	"""R1: admin's wire, through the real ``_do_post``, into ``_refusal`` - the code and
	admin's sentence arrive for every private-network refusal."""

	def _raised(self, status, body):
		with patch("requests.post", return_value=_resp(status, body)):
			with self.assertRaises(Exception) as caught:
				admin_client._do_post("https://admin.example.com/x", {}, {}, 5, "https://admin.example.com")
		return caught.exception

	def test_every_private_code_reaches_the_refusal(self):
		cases = [
			(403, "PermissionError", "EMAIL_NOT_CONFIRMED"),
			(403, "PermissionError", "NOT_PRIVATE_CUSTOMER"),
			(403, "PermissionError", "PRIVATE_NETWORK_DISABLED"),
			(409, "DuplicateEntryError", "PRIVATE_HOLD_UNAVAILABLE"),
			(409, "DuplicateEntryError", "CONNECTION_CHECK_BUSY"),
			(409, "DuplicateEntryError", "PRIVATE_ADDRESS_TAKEN"),
			(400, "InvalidRequestError", "PRIVATE_ADDRESS_INVALID"),
			(503, "ServiceUnavailableError", "PRIVATE_HOLD_UNAVAILABLE"),
		]
		for status, exc_type, code in cases:
			with self.subTest(code=code, status=status):
				err = self._raised(status, _thrown(exc_type, code, f"Sentence for {code}."))
				self.assertEqual(
					private_network._refusal(err),
					{"ok": False, "code": code, "message": f"Sentence for {code}."},
				)

	def test_invalid_request_is_the_generic_failure(self):
		err = self._raised(400, _thrown("InvalidRequestError", "INVALID_REQUEST", "Bad field."))
		out = private_network._refusal(err)
		self.assertEqual((out["code"], out["message"]), ("INVALID_REQUEST", private_network._UNEXPECTED))

	def test_a_coded_rate_limit_keeps_its_wait(self):
		body = _thrown(
			"RateLimitExceededError", "RATE_LIMITED_PRIVATE_NETWORK", "Slow down.", retry_after_seconds=90
		)
		out = private_network._refusal(self._raised(429, body))
		self.assertEqual(out["code"], "RATE_LIMITED_PRIVATE_NETWORK")
		self.assertEqual(out["retry_after_seconds"], 90)

	def test_an_auth_error_carrying_a_code_keeps_it(self):
		from jarvis import onboarding_contract
		from jarvis.exceptions import AdminAuthError

		err = AdminAuthError("Confirm your email first.", status_code=403, code="EMAIL_NOT_CONFIRMED")
		error, status = onboarding_contract.error_object(err)
		self.assertEqual((error["code"], status), ("EMAIL_NOT_CONFIRMED", 403))
		other = AdminAuthError("Pay first.", status_code=403, code="CUSTOMER_NOT_PAID")
		self.assertEqual(
			onboarding_contract.error_object(other)[0]["code"], onboarding_contract.BENCH_ADMIN_AUTH_FAILED
		)
		bare, _ = onboarding_contract.error_object(AdminAuthError("no", status_code=401))
		self.assertEqual(bare["code"], onboarding_contract.BENCH_ADMIN_AUTH_FAILED)

	def test_an_unknown_admin_code_reads_as_the_generic_failure(self):
		err = self._raised(409, _thrown("DuplicateEntryError", "SOMETHING_NEW", "Internal detail."))
		out = private_network._refusal(err)
		self.assertFalse(out["ok"])
		self.assertEqual(out["message"], private_network._UNEXPECTED)

	def test_a_plain_exception_does_not_crash_the_refusal(self):
		out = private_network._refusal(ValueError("boom"))
		self.assertFalse(out["ok"])
		self.assertEqual(out["message"], private_network._UNEXPECTED)


class TestProbeEndToEnd(_AdminUser):
	"""The guest probe over the real wire: a bare 429 is busy, not unreachable."""

	def _probe(self, status, body):
		with patch("requests.post", return_value=_resp(status, body)):
			return private_network.check_admin_connection()

	def test_a_bare_429_is_reachable_but_busy(self):
		out = self._probe(429, {"exc_type": "RateLimitExceededError", "exception": "too many"})
		self.assertTrue(out["reachable"])
		self.assertFalse(out["enabled"])
		self.assertEqual(out["message"], "Too many attempts. Try again in a minute.")

	def test_a_bare_429_makes_the_host_status_reachable(self):
		with (
			patch.object(admin_client, "_public_host_configured", return_value=False),
			patch.object(admin_client, "private_public_address", return_value=""),
			patch("requests.post", return_value=_resp(429, {"exc_type": "RateLimitExceededError"})),
		):
			self.assertIs(private_network.network_status()["admin_reachable"], True)

	def test_a_coded_refusal_is_reachable_but_not_enabled(self):
		body = _thrown("PermissionError", "PRIVATE_NETWORK_DISABLED", "Off.")
		out = self._probe(403, body)
		self.assertEqual(out, {"reachable": True, "enabled": False, "caller_ip": ""})


class TestFixRoundOne(_AdminUser):
	def test_a_slow_resolver_is_bounded(self):
		import time

		def slow(*args, **kwargs):
			time.sleep(1.5)
			return []

		with (
			patch.object(private_network, "_DNS_TIMEOUT_S", 0.2),
			patch.object(private_network.socket, "getaddrinfo", side_effect=slow),
		):
			started = time.monotonic()
			self.assertEqual(private_network._resolve_ipv4("slow.example.com"), [])
		self.assertLess(time.monotonic() - started, 1.0)

	def test_targets_are_empty_on_a_public_host(self):
		with (
			patch.object(admin_client, "_public_host_configured", return_value=True),
			patch.object(private_network.socket, "getaddrinfo", side_effect=AssertionError("dns")),
		):
			self.assertEqual(private_network.private_setup_targets(), {"admin_host": "", "admin_ips": []})

	def test_a_non_dict_setup_answer_reads_as_unreachable(self):
		for junk in ("oops", ["x"], 5):
			with self.subTest(junk=junk):
				with patch.object(admin_client, "private_setup_info", return_value=junk):
					out = private_network.check_admin_connection()
				self.assertEqual(out, {"reachable": False, "enabled": False, "caller_ip": ""})
