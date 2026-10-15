"""The held fleet host: stored in the signup context, refreshed by the poll that follows the
email click, and read back by reserve_host (design spec 8.2, contract 4.3 and 4.4)."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis import admin_client, onboarding, onboarding_contract, private_network
from jarvis.exceptions import AdminUnreachableError

HOLD = {
	"host_ip": "198.51.100.27",
	"reach_url": "https://reach.h3.fleet.example/",
	"hold_until": "2026-10-17 09:10:00",
}


def _live_hold():
	return {**HOLD, "hold_until": str(add_to_date(now_datetime(), hours=5, as_datetime=True))[:19]}


def _thrown(exc_type, code, message="Admin's sentence.", **extra):
	"""The REAL wire shape of a raised admin refusal (no top-level ``message``)."""
	return {
		"exc_type": exc_type,
		"exception": f"frappe.exceptions.{exc_type}: {message}",
		"_server_messages": json.dumps([json.dumps({"message": message})]),
		"error": {"code": code, "message": message, "recovery": "retry", **extra},
	}


@contextmanager
def _admin_answers(status, body):
	"""Run the real ``_do_post`` against an admin answer of this status and body."""
	resp = MagicMock(spec=requests.Response)
	resp.status_code = status
	resp.json.return_value = body
	resp.text = json.dumps(body)

	def post(**_kw):
		return admin_client._do_post("https://admin.example.com/x", {}, {}, 5, "https://admin.example.com")

	with patch("requests.post", return_value=resp), patch.object(admin_client, "_post", side_effect=post):
		yield


class _Fresh(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self._saved = frappe.db.get_single_value("Jarvis Settings", onboarding_contract.CONTEXT_FIELD)
		self.addCleanup(self._restore)
		onboarding_contract.clear()

	def _restore(self):
		frappe.db.set_single_value(
			"Jarvis Settings", onboarding_contract.CONTEXT_FIELD, self._saved or "", update_modified=False
		)


class TestHoldStorage(_Fresh):
	def test_a_valid_hold_is_stored_and_read_back(self):
		self.assertEqual(onboarding_contract.store_private_hold(HOLD), HOLD)
		self.assertEqual(onboarding_contract.private_hold(), HOLD)

	def test_the_hold_reaches_the_browser_in_the_wire_context(self):
		onboarding_contract.store_private_hold(HOLD)
		self.assertEqual(onboarding_contract.wire()["private_hold"], HOLD)

	def test_an_invalid_hold_is_dropped_not_stored(self):
		for bad in (
			None,
			{},
			"198.51.100.27",
			{"host_ip": "not-an-ip", "reach_url": HOLD["reach_url"]},
			{"host_ip": HOLD["host_ip"], "reach_url": "ftp://reach.example/"},
			{"host_ip": HOLD["host_ip"], "reach_url": "https://"},
			{"host_ip": HOLD["host_ip"], "reach_url": "https://u:p@reach.example/"},
			{"host_ip": HOLD["host_ip"]},
			{"host_ip": "", "reach_url": "", "hold_until": None},
			{**HOLD, "hold_until": 12345},
		):
			with self.subTest(bad=bad):
				onboarding_contract.store_private_hold(HOLD)
				self.assertEqual(onboarding_contract.store_private_hold(bad), {})
				self.assertEqual(onboarding_contract.private_hold(), {})

	def test_a_repeat_of_the_same_hold_does_not_write(self):
		onboarding_contract.store_private_hold(HOLD)
		with patch.object(onboarding_contract, "save", side_effect=AssertionError("rewrote")):
			onboarding_contract.store_private_hold(dict(HOLD))

	def test_a_null_hold_until_is_kept_for_a_tenant_host(self):
		tenant = {**HOLD, "hold_until": None}
		self.assertEqual(onboarding_contract.store_private_hold(tenant)["hold_until"], None)


class TestPollHook(_Fresh):
	def test_the_poll_stores_a_hold(self):
		onboarding._absorb_signup_state({"pending_verification": False, "private_hold": HOLD})
		self.assertEqual(onboarding_contract.private_hold(), HOLD)

	def test_a_null_hold_in_the_poll_forgets_an_expired_one(self):
		onboarding_contract.store_private_hold(HOLD)
		onboarding._absorb_signup_state({"private_hold": None})
		self.assertEqual(onboarding_contract.private_hold(), {})

	def test_a_poll_without_the_key_leaves_the_hold_alone(self):
		onboarding_contract.store_private_hold(HOLD)
		onboarding._absorb_signup_state({"pending_verification": True})
		self.assertEqual(onboarding_contract.private_hold(), HOLD)

	def test_the_check_variant_stores_it_too(self):
		onboarding._absorb_signup_state({"private_hold": HOLD}, from_check=True)
		self.assertEqual(onboarding_contract.private_hold(), HOLD)


class TestReserveHost(_Fresh):
	def test_success_stores_and_returns_the_hold(self):
		with patch.object(admin_client, "reserve_private_host", return_value=HOLD):
			out = private_network.reserve_host()
		self.assertEqual(out, {"ok": True, **HOLD})
		self.assertEqual(onboarding_contract.private_hold(), HOLD)

	def test_a_tenant_host_comes_back_with_a_null_hold_until(self):
		with patch.object(admin_client, "reserve_private_host", return_value={**HOLD, "hold_until": None}):
			out = private_network.reserve_host()
		self.assertTrue(out["ok"])
		self.assertIsNone(out["hold_until"])

	def test_a_403_refusal_is_returned_with_its_code(self):
		body = _thrown("PermissionError", "EMAIL_NOT_CONFIRMED", "Confirm your email first.")
		with _admin_answers(403, body):
			out = private_network.reserve_host()
		self.assertEqual(
			out, {"ok": False, "code": "EMAIL_NOT_CONFIRMED", "message": "Confirm your email first."}
		)

	def test_a_409_hold_unavailable_is_returned_with_its_code(self):
		body = _thrown("DuplicateEntryError", "PRIVATE_HOLD_UNAVAILABLE", "No server free.")
		with _admin_answers(409, body):
			out = private_network.reserve_host()
		self.assertEqual(out["code"], "PRIVATE_HOLD_UNAVAILABLE")
		self.assertFalse(out["ok"])

	def test_unreachable_admin_is_a_refusal_not_an_exception(self):
		with patch.object(admin_client, "reserve_private_host", side_effect=AdminUnreachableError("down")):
			out = private_network.reserve_host()
		self.assertFalse(out["ok"])
		self.assertEqual(out["code"], onboarding_contract.BENCH_ADMIN_UNREACHABLE)

	def test_a_malformed_answer_is_refused_and_clears_the_stored_hold(self):
		onboarding_contract.store_private_hold(HOLD)
		with patch.object(admin_client, "reserve_private_host", return_value={"host_ip": "nope"}):
			out = private_network.reserve_host()
		self.assertEqual(out["code"], onboarding_contract.BENCH_ADMIN_REJECTED)
		self.assertEqual(onboarding_contract.private_hold(), {})

	def test_a_legacy_tenant_host_without_an_address_is_not_called_unexpected(self):
		legacy = {"host_ip": "", "reach_url": "", "hold_until": None}
		with patch.object(admin_client, "reserve_private_host", return_value=legacy):
			out = private_network.reserve_host()
		self.assertFalse(out["ok"])
		self.assertNotEqual(out["message"], private_network._UNEXPECTED)
		self.assertTrue(out["message"])

	def test_a_half_empty_tenant_host_reads_as_no_address_either_way_round(self):
		for host_ip, reach_url in (("203.0.113.9", ""), ("", "https://erp.example.com")):
			raw = {"host_ip": host_ip, "reach_url": reach_url, "hold_until": None}
			with self.subTest(raw=raw), patch.object(admin_client, "reserve_private_host", return_value=raw):
				out = private_network.reserve_host()
				self.assertFalse(out["ok"])
				self.assertEqual(out["message"], private_network._NO_ADDRESS)
				self.assertEqual(onboarding_contract.private_hold(), {})

	def test_an_empty_answer_is_still_unexpected(self):
		for raw in (
			{},
			{"host_ip": "", "reach_url": ""},
			{"host_ip": "", "reach_url": "", "hold_until": "x"},
		):
			with self.subTest(raw=raw), patch.object(admin_client, "reserve_private_host", return_value=raw):
				out = private_network.reserve_host()
				self.assertEqual(out["message"], private_network._UNEXPECTED)

	def test_requires_a_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			private_network.reserve_host()


class TestRateLimitedReserve(_Fresh):
	"""Admin counts every reserve call (10/hour); a live stored hold answers instead."""

	def _limited(self):
		body = _thrown(
			"RateLimitExceededError", "RATE_LIMITED_PRIVATE_NETWORK", "Slow down.", retry_after_seconds=300
		)
		return _admin_answers(429, body)

	def test_a_live_stored_hold_is_returned_as_cached(self):
		live = _live_hold()
		onboarding_contract.store_private_hold(live)
		with self._limited():
			out = private_network.reserve_host()
		self.assertEqual(out, {"ok": True, **live, "cached": True})

	def test_a_tenant_host_hold_with_null_hold_until_counts_as_live(self):
		tenant = {**HOLD, "hold_until": None}
		onboarding_contract.store_private_hold(tenant)
		with self._limited():
			out = private_network.reserve_host()
		self.assertEqual(out, {"ok": True, **tenant, "cached": True})

	def test_a_stored_hold_is_served_whatever_its_hold_until(self):
		# admin's zone is unknown to the bench: no time comparison
		old = {**HOLD, "hold_until": "2020-01-01 00:00:00"}
		onboarding_contract.store_private_hold(old)
		with self._limited():
			out = private_network.reserve_host()
		self.assertEqual(out, {"ok": True, **old, "cached": True})

	def test_no_stored_hold_is_the_plain_refusal(self):
		with self._limited():
			out = private_network.reserve_host()
		self.assertFalse(out["ok"])
		self.assertEqual(out["code"], "RATE_LIMITED_PRIVATE_NETWORK")
