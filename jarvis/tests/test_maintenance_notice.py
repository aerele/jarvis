"""Stream E — bench maintenance-hold mirror + send gate (app tasks A1 + A2).

Pure toggle owned by the control plane: the bench mirrors {active, message} via the
marker-aware persist_from_connection (old CP / no marker -> clear; present key ->
authoritative; marker present + absent key -> keep last-known, so the destroy+reprovision
window can't flip a live hold off). The send gate reads the mirror and refuses with reason
"maintenance". CP resolver/verb coverage lives in the jarvis_admin_v2 suite.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import maintenance_notice


class TestMaintenanceNotice(FrappeTestCase):
	def tearDown(self):
		maintenance_notice.persist({"active": False})

	def test_persist_active_and_boot(self):
		maintenance_notice.persist({"active": True, "message": "up"})
		p = maintenance_notice.boot_payload()
		self.assertTrue(p["active"])
		self.assertEqual(p["message"], "up")

	def test_present_false_clears(self):
		maintenance_notice.persist({"active": True})
		maintenance_notice.persist({"active": False})
		self.assertFalse(maintenance_notice.boot_payload()["active"])

	def test_absent_key_keeps_last_known(self):
		# The D-path unresolved-payload case: None must NOT clear a live hold.
		maintenance_notice.persist({"active": True, "message": "up"})
		maintenance_notice.persist(None)
		self.assertTrue(maintenance_notice.boot_payload()["active"])


class TestMaintenanceGate(FrappeTestCase):
	def test_gate_blocks_when_active(self):
		from jarvis.chat.policy import _maintenance_hold

		with patch("jarvis.maintenance_notice.boot_payload", return_value={"active": True}):
			self.assertTrue(_maintenance_hold())

	def test_gate_allows_when_inactive(self):
		from jarvis.chat.policy import _maintenance_hold

		with patch("jarvis.maintenance_notice.boot_payload", return_value={"active": False}):
			self.assertFalse(_maintenance_hold())

	def test_gate_fails_open(self):
		from jarvis.chat.policy import _maintenance_hold

		with patch("jarvis.maintenance_notice.boot_payload", side_effect=RuntimeError):
			self.assertFalse(_maintenance_hold())


class TestMaintenanceCheck(FrappeTestCase):
	"""check() re-pulls the connection from admin and refreshes the local mirror. It is
	de-duped by a short cache and folds the release refresh into the same round-trip; a
	failed round-trip must never drop a live hold."""

	def setUp(self):
		frappe.cache().delete_value(maintenance_notice._CHECK_CACHE_KEY)
		maintenance_notice.persist({"active": False})

	def tearDown(self):
		frappe.cache().delete_value(maintenance_notice._CHECK_CACHE_KEY)
		maintenance_notice.persist({"active": False})

	def test_check_keeps_last_known_on_admin_error(self):
		maintenance_notice.persist({"active": True, "message": "up"})
		with patch("jarvis.admin_client.get_connection", side_effect=RuntimeError("down")):
			out = maintenance_notice.check()  # must not raise
		self.assertTrue(out["active"])  # a failed poll must not clear a live hold
		self.assertTrue(maintenance_notice.boot_payload()["active"])

	def test_check_throttles_to_one_round_trip_in_window(self):
		with (
			patch(
				"jarvis.admin_client.get_connection", return_value={"maintenance": {"active": False}}
			) as gc,
			patch("jarvis.release_notice.persist"),
		):
			maintenance_notice.check()
			maintenance_notice.check()  # within the cache window -> no second call
		self.assertEqual(gc.call_count, 1)

	def test_check_refreshes_both_notices_and_applies_maintenance(self):
		conn = {
			"maintenance_supported": True,
			"maintenance": {"active": True, "message": "rolling"},
			"release_notice": {"active": False},
		}
		with (
			patch("jarvis.admin_client.get_connection", return_value=conn),
			patch("jarvis.release_notice.persist") as rp,
		):
			out = maintenance_notice.check()
		rp.assert_called_once()  # one round-trip refreshes the release mirror too
		self.assertTrue(out["active"])
		self.assertEqual(out["message"], "rolling")

	def test_check_absent_maintenance_key_keeps_last_known(self):
		# Current CP (marker present), transient payload with no maintenance key -> keep last-known.
		maintenance_notice.persist({"active": True, "message": "up"})
		conn = {"maintenance_supported": True, "release_notice": {"active": False}}
		with (
			patch("jarvis.admin_client.get_connection", return_value=conn),
			patch("jarvis.release_notice.persist"),
		):
			out = maintenance_notice.check()
		self.assertTrue(out["active"])  # marker present + absent key -> keep last-known

	def test_check_calls_llm_switch_reconcile(self):
		"""2026 review (second pass): every open chat polling check() while the
		hold banner is up must also drive a held switch forward - independent
		of a reply ending or the Settings page's own poll."""
		with (
			patch("jarvis.chat.llm_switch.reconcile") as mock_reconcile,
			patch("jarvis.admin_client.get_connection", return_value={"maintenance": {"active": False}}),
			patch("jarvis.release_notice.persist"),
		):
			maintenance_notice.check()

		mock_reconcile.assert_called_once()

	def test_check_survives_llm_switch_reconcile_failure(self):
		"""Guarded: a failure driving the switch must never raise out of
		check() or change its own return shape - the mirror refresh below
		still runs normally."""
		with (
			patch("jarvis.chat.llm_switch.reconcile", side_effect=RuntimeError("redis down")),
			patch("jarvis.admin_client.get_connection", return_value={"maintenance": {"active": False}}),
			patch("jarvis.release_notice.persist"),
			patch("frappe.log_error") as mock_log,
		):
			out = maintenance_notice.check()  # must not raise

		mock_log.assert_called()
		self.assertEqual(out, {"active": False, "message": ""})

	def test_check_old_cp_clears_not_strands(self):
		# A rolled-back CP that no longer speaks maintenance (no marker) -> the bench CLEARS its
		# mirror rather than stranding forever behind a hold the old CP can't lift (finding App-1/#3).
		maintenance_notice.persist({"active": True, "message": "up"})
		conn = {"release_notice": {"active": False}}  # old CP: no marker, no maintenance key
		with (
			patch("jarvis.admin_client.get_connection", return_value=conn),
			patch("jarvis.release_notice.persist"),
		):
			out = maintenance_notice.check()
		self.assertFalse(out["active"])


class TestPersistFromConnection(FrappeTestCase):
	"""The marker-aware entry point every full-payload persist site calls (review App-1/#1/#3)."""

	def tearDown(self):
		maintenance_notice.persist({"active": False})

	def test_marker_absent_clears_mirror(self):
		# Old / rolled-back CP (no marker) must CLEAR, not strand -- even with a present hold dict.
		maintenance_notice.persist({"active": True, "message": "up"})
		maintenance_notice.persist_from_connection({"maintenance": {"active": True}})
		self.assertFalse(maintenance_notice.boot_payload()["active"])

	def test_marker_present_absent_key_keeps(self):
		# Current CP, transient/partial payload (no maintenance key) -> keep last-known.
		maintenance_notice.persist({"active": True, "message": "up"})
		maintenance_notice.persist_from_connection({"maintenance_supported": True})
		self.assertTrue(maintenance_notice.boot_payload()["active"])

	def test_marker_present_key_is_authoritative(self):
		maintenance_notice.persist({"active": False})
		maintenance_notice.persist_from_connection(
			{"maintenance_supported": True, "maintenance": {"active": True, "message": "up"}}
		)
		self.assertTrue(maintenance_notice.boot_payload()["active"])
		maintenance_notice.persist_from_connection({"maintenance_supported": True, "maintenance": {}})
		self.assertFalse(maintenance_notice.boot_payload()["active"])  # {} = authoritative clear

	def test_empty_connection_clears(self):
		# A degenerate/empty success ({} -- no marker) clears (conscious fail = un-hold, not strand).
		maintenance_notice.persist({"active": True})
		maintenance_notice.persist_from_connection({})
		self.assertFalse(maintenance_notice.boot_payload()["active"])

	def test_breadcrumb_only_when_clearing_active_hold(self):
		# AC2 anti-spam: the deploy-order diagnostic fires when a markerless (old-CP) payload clears
		# an ACTIVE hold, but NOT on a markerless not-held poll (else every poll would log-spam).
		def _logged_clear(logger):
			return any(
				c.args and "clearing hold" in str(c.args[0]) for c in logger.return_value.info.call_args_list
			)

		maintenance_notice.persist({"active": True, "message": "up"})
		with patch("frappe.logger") as logger:
			maintenance_notice.persist_from_connection({})  # old CP, hold ACTIVE -> clear + log
			self.assertTrue(_logged_clear(logger))
		with patch("frappe.logger") as logger:
			maintenance_notice.persist_from_connection({})  # already not held -> clear no-op, no log
			self.assertFalse(_logged_clear(logger))

	def test_write_connection_does_not_mirror_maintenance(self):
		# A4 canary: the CP billing/confirm_payment path (_billing_actions) bypasses
		# _connection_payload, so its payload carries NO maintenance_supported marker. That is
		# harmless ONLY because finish_payment -> write_connection does not mirror maintenance; if
		# it did, a marker-less billing payload would CLEAR a live hold. Pin the invariant.
		import inspect

		from jarvis import onboarding

		# Assert the persist CALL is absent (not the bare word "maintenance", which could appear in an
		# innocuous comment) -- the real invariant is "write_connection does not mirror maintenance".
		self.assertNotIn("maintenance_notice", inspect.getsource(onboarding.write_connection))
