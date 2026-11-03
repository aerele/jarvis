"""The bench half of the connection test (design spec 6.3, contract section 3)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import network_check
from jarvis.network_check import answer, compute_mac

NOW = 1_792_000_000
SECRET = "s3cret-value"
CID = "a" * 32
NONCE = "b" * 64
# Fixed vector from the contract's reference code (hmac-sha256 with the derived key):
# secret "s3cret-value", cid a*32, nonce b*64, exp 1792000000, site "erp.example.com".
VECTOR = "f1f222256e65d8fec1bcf656cc7f440aa249ffca94831cea28378fc95687b777"
REFUSED = {"ok": False}


class TestComputeMac(FrappeTestCase):
	def test_shared_cross_repo_vector(self):
		# Same vector as admin's jarvis_admin_v2/tests/fleet/test_connection_verdict.py and contract section 3.
		# If this fails, admin and bench disagree on the MAC and every live test would fail.
		self.assertEqual(
			compute_mac("0123456789abcde", "a" * 32, "b" * 64, 1792000000, "erp.example.com"),
			"3a9edb71b1be0ad3335eea801fbe28e0e419aa570730d1701d431233060688dc",
		)

	def test_matches_the_contract_vector(self):
		self.assertEqual(compute_mac(SECRET, CID, NONCE, NOW, "erp.example.com"), VECTOR)

	def test_every_input_changes_the_mac(self):
		base = compute_mac(SECRET, CID, NONCE, NOW, "erp.example.com")
		self.assertNotEqual(base, compute_mac("other", CID, NONCE, NOW, "erp.example.com"))
		self.assertNotEqual(base, compute_mac(SECRET, "c" * 32, NONCE, NOW, "erp.example.com"))
		self.assertNotEqual(base, compute_mac(SECRET, CID, "c" * 64, NOW, "erp.example.com"))
		self.assertNotEqual(base, compute_mac(SECRET, CID, NONCE, NOW + 1, "erp.example.com"))
		self.assertNotEqual(base, compute_mac(SECRET, CID, NONCE, NOW, "other.example.com"))


class TestAnswer(FrappeTestCase):
	def setUp(self):
		self._ip = "198.51.100.9"
		frappe.local.request_ip = self._ip
		self._clear_limiter()
		for patcher in (
			patch("jarvis.network_check._now", return_value=NOW),
			patch("jarvis.network_check._api_secret", return_value=SECRET),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def tearDown(self):
		self._clear_limiter()

	def _clear_limiter(self):
		for ip in (self._ip, "198.51.100.10"):
			frappe.local.request_ip = ip
			frappe.cache().delete(network_check._rate_limit_key())
		frappe.local.request_ip = self._ip

	def _ok(self, **over):
		args = {"v": 1, "cid": CID, "nonce": NONCE, "exp": NOW + 60}
		args.update(over)
		return answer(**args)

	def test_a_valid_request_gets_the_contract_answer(self):
		out = self._ok()
		self.assertEqual(
			out,
			{
				"v": 1,
				"site": frappe.local.site,
				"mac": compute_mac(SECRET, CID, NONCE, NOW + 60, frappe.local.site),
			},
		)

	def test_the_endpoint_is_guest_and_post_only(self):
		self.assertIn(answer, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[answer], ["POST"])

	def test_malformed_requests_get_the_same_bare_refusal(self):
		bad = [
			{"v": 2},
			{"v": True},  # bool is an int in Python; the contract wants the integer 1
			{"v": "1"},
			{"v": None},
			{"cid": "A" * 32},
			{"cid": "a" * 31},
			{"cid": "a" * 33},
			{"cid": "a" * 32 + "\n"},  # a regex with $ would accept this
			{"cid": "g" * 32},
			{"cid": None},
			{"nonce": "b" * 63},
			{"nonce": "B" * 64},
			{"nonce": "z" * 64},
			{"exp": str(NOW + 60)},
			{"exp": float(NOW + 60)},
			{"exp": True},
			{"exp": None},
		]
		for over in bad:
			with self.subTest(over=over):
				self.assertEqual(self._ok(**over), REFUSED)

	def test_the_clock_window_edges(self):
		self.assertNotEqual(self._ok(exp=NOW - 30), REFUSED)
		self.assertEqual(self._ok(exp=NOW - 31), REFUSED)
		self.assertNotEqual(self._ok(exp=NOW + 300), REFUSED)
		self.assertEqual(self._ok(exp=NOW + 301), REFUSED)

	def test_refuses_without_an_admin_api_secret(self):
		for secret in ("", None):
			with self.subTest(secret=secret), patch("jarvis.network_check._api_secret", return_value=secret):
				self.assertEqual(self._ok(), REFUSED)

	def test_does_not_look_at_network_mode_or_any_setting(self):
		"""A bench that reconnected without the private setup must still answer, and the
		endpoint reads no setting other than the secret."""
		with patch("frappe.db.get_single_value", side_effect=AssertionError("settings read")):
			self.assertNotEqual(self._ok(), REFUSED)

	def test_writes_nothing_to_the_database(self):
		before = frappe.db.transaction_writes
		self._ok()
		self._ok(v=2)
		self.assertEqual(frappe.db.transaction_writes, before)

	def test_rate_limited_per_ip(self):
		for _ in range(network_check.RATE_LIMIT):
			self.assertNotEqual(self._ok(), REFUSED)
		self.assertEqual(self._ok(), REFUSED)  # one past the limit inside the window
		frappe.local.request_ip = "198.51.100.10"  # another caller is not affected
		self.assertNotEqual(self._ok(), REFUSED)

	def test_the_limit_is_a_flood_ceiling_for_a_shared_gateway_ip(self):
		# Behind a gateway every caller shares one IP, so the limit must sit far above any
		# real probe rate (documented deviation from the plan's 60 a minute).
		self.assertEqual(network_check.RATE_LIMIT, 600)

	def test_malformed_requests_do_not_spend_the_budget(self):
		"""Junk POSTs must not be able to lock out the real probe that shares the IP."""
		for _ in range(network_check.RATE_LIMIT + 5):
			self.assertEqual(self._ok(v=2), REFUSED)
		self.assertNotEqual(self._ok(), REFUSED)

	def test_the_shape_check_runs_before_any_secret_read(self):
		with patch("jarvis.network_check._api_secret", side_effect=AssertionError("secret read")):
			self.assertEqual(self._ok(v=2), REFUSED)

	def test_a_cache_failure_does_not_take_the_endpoint_down(self):
		with patch.object(network_check.frappe, "cache", side_effect=Exception("redis down")):
			self.assertFalse(network_check._rate_limited())

	def test_a_counter_left_without_a_ttl_is_re_armed(self):
		key = network_check._rate_limit_key()
		frappe.cache().set(key, network_check.RATE_LIMIT + 5)  # no TTL, already over the limit
		self.assertTrue(network_check._rate_limited())
		self.assertGreater(frappe.cache().ttl(key), 0)

	def test_the_key_does_not_grow_with_the_caller_ip(self):
		frappe.local.request_ip = "1.2.3.4, " + "x" * 5000
		self.assertLess(len(network_check._rate_limit_key()), 120)


class TestAnswerRealSecretRead(FrappeTestCase):
	"""No patch of ``_api_secret``: the real get_decrypted_password path, no writes."""

	def setUp(self):
		frappe.local.request_ip = "198.51.100.11"
		frappe.cache().delete(network_check._rate_limit_key())
		self.addCleanup(lambda: frappe.cache().delete(network_check._rate_limit_key()))
		patcher = patch("jarvis.network_check._now", return_value=NOW)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_real_read_answers_or_refuses_without_writing(self):
		secret = network_check._api_secret()
		before = frappe.db.transaction_writes
		with patch("frappe.db.get_single_value", side_effect=AssertionError("settings read")):
			out = answer(v=1, cid=CID, nonce=NONCE, exp=NOW + 60)
		self.assertEqual(frappe.db.transaction_writes, before)
		if secret:
			site = frappe.local.site
			self.assertEqual(out["mac"], compute_mac(secret, CID, NONCE, NOW + 60, site))
		else:
			self.assertEqual(out, REFUSED)


class TestAnswerThroughTheRealHandler(FrappeTestCase):
	"""Dispatch through ``frappe.handler.execute_cmd`` so the whitelist wrapper (and, on a
	strict bench, the type-annotation requirement) is exercised, not a bare function call."""

	def setUp(self):
		frappe.local.request_ip = "198.51.100.12"
		frappe.cache().delete(network_check._rate_limit_key())
		self.addCleanup(lambda: frappe.cache().delete(network_check._rate_limit_key()))
		prev_request = getattr(frappe.local, "request", None)
		prev_form = frappe.local.form_dict
		self.addCleanup(setattr, frappe.local, "request", prev_request)
		self.addCleanup(setattr, frappe.local, "form_dict", prev_form)
		for patcher in (
			patch("jarvis.network_check._now", return_value=NOW),
			patch("jarvis.network_check._api_secret", return_value=SECRET),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def _dispatch(self, body):
		from frappe.handler import execute_cmd
		from frappe.utils import set_request

		set_request(method="POST", path="/api/method/jarvis.network_check.answer")
		frappe.local.form_dict = frappe._dict(body)
		return execute_cmd("jarvis.network_check.answer")

	def test_every_parameter_is_annotated_and_not_coerced(self):
		import typing

		hints = typing.get_type_hints(answer)
		params = {k: v for k, v in hints.items() if k != "return"}
		self.assertEqual(params, dict.fromkeys(("v", "cid", "nonce", "exp"), typing.Any))

	def test_a_well_formed_body_gets_the_mac(self):
		out = self._dispatch({"v": 1, "cid": CID, "nonce": NONCE, "exp": NOW + 60})
		self.assertEqual(out["mac"], compute_mac(SECRET, CID, NONCE, NOW + 60, frappe.local.site))

	def test_a_malformed_body_is_the_bare_refusal_not_an_exception(self):
		for body in (
			{"v": "1", "cid": CID, "nonce": NONCE, "exp": NOW + 60},
			{"v": 1, "cid": CID, "nonce": NONCE, "exp": str(NOW + 60)},
			{"v": 1, "cid": ["x"], "nonce": {"a": 1}, "exp": 1.5},
			{},
		):
			with self.subTest(body=body):
				self.assertEqual(self._dispatch(body), REFUSED)
