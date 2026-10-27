"""Site pin: the doors the first wave missed (trigger LLM client, session pool,
send path, pump retries), fail-open status reads, and the settings reset."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import settings_reset
from jarvis.chat import pump, site_pin
from jarvis.exceptions import SITE_IS_COPY_CODE, SiteIsCopyError

SETTINGS = "Jarvis Settings"


def _set(**values) -> None:
	frappe.db.set_single_value(SETTINGS, values, update_modified=False)


class TestSitePinGates(FrappeTestCase):
	def setUp(self):
		# FrappeTestCase rolls back per class, so clear what a refusal wrote.
		_set(paired_site="", pairing_blocked=0, pairing_blocked_reason="")
		site_pin._read_failure_logged = False

	# -- I2: fail-open single-query read -------------------------------------

	def test_status_fails_open_when_read_raises(self):
		_set(paired_site="other.site")
		with (
			patch.object(site_pin, "_read_pin", side_effect=frappe.ValidationError("no field")),
			patch("frappe.log_error") as log,
		):
			st = site_pin.status()
			site_pin.status()
			site_pin.assert_not_copy()  # does not raise
		self.assertFalse(st["blocked"])
		self.assertEqual(log.call_count, 1)  # logged once per process

	def test_pin_if_unset_writes_nothing_when_read_fails(self):
		with (
			patch.object(site_pin, "_read_pin", side_effect=RuntimeError("db")),
			patch("frappe.log_error"),
		):
			self.assertFalse(site_pin.pin_if_unset())
		self.assertFalse(frappe.db.get_single_value(SETTINGS, "paired_site"))

	def test_single_query_read_matches_set_and_empty_pins(self):
		st = site_pin.status()
		self.assertEqual((st["blocked"], st["paired_site"], st["reason"]), (False, "", ""))
		_set(paired_site="other.site", pairing_blocked=1, pairing_blocked_reason="why")
		st = site_pin.status()
		self.assertEqual((st["blocked"], st["paired_site"], st["reason"]), (True, "other.site", "why"))
		_set(paired_site=frappe.local.site, pairing_blocked=0, pairing_blocked_reason="")
		self.assertFalse(site_pin.status()["blocked"])

	def test_read_does_not_use_meta_validated_single_value(self):
		with patch("frappe.db.get_single_value", side_effect=AssertionError("meta read")):
			site_pin.status()

	# -- C1: trigger LLM client ----------------------------------------------

	def test_llm_task_refuses_on_copy_site(self):
		from jarvis.triggers import llm_task_client

		_set(paired_site="other.site")
		with patch("requests.post") as post:
			with self.assertRaises(llm_task_client.LLMTaskError) as cm:
				llm_task_client.llm_task_complete("p", "i")
		post.assert_not_called()
		self.assertIn("other.site", str(cm.exception))
		self.assertNotIsInstance(cm.exception, llm_task_client.LLMTaskNotAvailableError)

	# -- I1: pooled sessions --------------------------------------------------

	def test_pool_checkout_refuses_on_copy_site(self):
		from jarvis.chat import agent_session_pool

		_set(paired_site="other.site")
		with patch.object(agent_session_pool, "_get_or_create_group") as group:
			with self.assertRaises(SiteIsCopyError):
				with agent_session_pool.checkout("ws://pooled"):
					pass
		group.assert_not_called()

	# -- I3: send path and retries -------------------------------------------

	def test_send_on_copy_site_returns_copy_message_and_enqueues_nothing(self):
		from jarvis.chat import api

		_set(paired_site="other.site")
		with (
			patch("frappe.enqueue") as enqueue,
			patch("jarvis.chat.api.create_or_focus_empty") as create,
		):
			out = api.send_message(conversation="", message="hello")
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "site_is_copy")
		self.assertEqual(out["message"], site_pin.copy_message("other.site"))
		enqueue.assert_not_called()
		create.assert_not_called()

	def test_copy_error_has_distinct_code(self):
		self.assertEqual(SiteIsCopyError("x").code, SITE_IS_COPY_CODE)

	def _run_hop_with_make_mux_error(self, exc):
		def boom(*_a):
			raise exc

		deps = SimpleNamespace(make_mux=boom)
		with (
			patch.object(pump, "_row_transport_mode", return_value="pump"),
			patch.object(pump.ts, "lease_acquire", return_value=(True, 5)),
			patch.object(pump, "_write_lease_mirror"),
			patch.object(pump, "_kill_switch_engaged", return_value=False),
			patch.object(pump, "_schedule_successor_on_exit") as sched,
			patch("frappe.log_error"),
		):
			out = pump.run_pump_hop("rt", hop_counter=0, deps=deps, transport_retry=1)
		self.assertEqual(out["exit"], "no_transport")
		return sched

	def test_make_mux_copy_error_spends_retry_budget(self):
		sched = self._run_hop_with_make_mux_error(SiteIsCopyError("other.site"))
		self.assertEqual(sched.call_args.kwargs["transport_retry"], pump.TRANSPORT_RETRY_MAX)

	def test_make_mux_other_error_keeps_retry_count(self):
		sched = self._run_hop_with_make_mux_error(RuntimeError("down"))
		self.assertEqual(sched.call_args.kwargs["transport_retry"], 1)

	# -- I4: settings reset ---------------------------------------------------

	def test_connection_and_full_reset_clear_the_pin(self):
		for spec in (settings_reset.CONNECTION, settings_reset.FULL):
			self.assertIn("paired_site", spec.blank)
			self.assertIn("pairing_blocked_reason", spec.blank)
			self.assertIn("pairing_blocked", spec.zero)
		for spec in (settings_reset.LLM,):
			self.assertNotIn("paired_site", spec.blank)

	def test_reset_apply_writes_the_pin_fields(self):
		settings = MagicMock()
		with (
			patch.object(settings_reset, "clear_settings_password"),
			patch("jarvis.admin_client.clear_cached_token"),
		):
			settings_reset.apply(settings, settings_reset.CONNECTION)
		written = {c.args[0]: c.args[1] for c in settings.db_set.call_args_list}
		self.assertEqual(written["paired_site"], "")
		self.assertEqual(written["pairing_blocked_reason"], "")
		self.assertEqual(written["pairing_blocked"], 0)

	# -- M5 -------------------------------------------------------------------

	def test_clear_pairing_block_reports_what_it_cleared(self):
		_set(paired_site="erp.example.com", pairing_blocked=1, pairing_blocked_reason="because")
		out = site_pin.clear_pairing_block()
		self.assertFalse(out["blocked"])
		self.assertEqual(out["cleared_paired_site"], "erp.example.com")
		self.assertEqual(out["cleared_reason"], "because")

	# -- no site context (threads, bare frappe.local) fails open ---------------

	def _run_in_thread(self, fn):
		out = {}

		def target():
			try:
				out["value"] = fn()
			except BaseException as e:
				out["error"] = e

		t = threading.Thread(target=target)
		t.start()
		t.join(10)
		return out

	def test_status_in_thread_without_site_is_not_blocked(self):
		out = self._run_in_thread(site_pin.status)
		self.assertNotIn("error", out)
		self.assertFalse(out["value"]["blocked"])

	def test_assert_not_copy_in_thread_without_site_does_not_raise(self):
		out = self._run_in_thread(site_pin.assert_not_copy)
		self.assertNotIn("error", out)

	def test_status_and_pin_without_site_attribute_fail_open(self):
		with patch.object(frappe, "local", SimpleNamespace()), patch("frappe.log_error") as log:
			self.assertFalse(site_pin.status()["blocked"])
			self.assertFalse(site_pin.pin_if_unset())
			site_pin.assert_not_copy()  # does not raise
		log.assert_not_called()
