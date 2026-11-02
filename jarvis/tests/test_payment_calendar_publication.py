"""A publication observed during a status request must deliver a full authorized result."""

import inspect
import unittest
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import api

status_body = inspect.unwrap(api.get_snapshot_status)


class TestPaymentCalendarPublication(unittest.TestCase):
	def setUp(self):
		self.running = {"snapshot_id": "a" * 32, "state": "running"}
		self.complete = {
			**self.running,
			"state": "complete",
			"scope": dict.fromkeys(
				(
					"company",
					"accounts",
					"anchor",
					"selectable_start",
					"selectable_end",
					"window_start",
					"window_end",
				)
			),
		}
		self.payload = dict.fromkeys(
			("projection", "currency", "floor", "generated_at", "bank_code", "unavailable")
		)
		self.payload["projection"] = {"days": {}}

	def test_publication_between_load_and_observation_reloads_authorized_result(self):
		with (
			patch.object(
				api, "_load", side_effect=[(self.running, None), (self.complete, self.payload)]
			) as load,
			patch.object(api.jobs, "observe_job", return_value=self.complete),
		):
			result = status_body("a" * 32)
		self.assertEqual(result["state"], "complete")
		self.assertEqual(result["projection"], self.payload["projection"])
		self.assertEqual(load.call_count, 2)

	def test_result_arriving_after_status_read_returns_complete_metadata(self):
		with (
			patch.object(
				api, "_load", side_effect=[(self.running, self.payload), (self.complete, self.payload)]
			),
			patch.object(api.jobs, "observe_job") as observe,
		):
			result = status_body("a" * 32)
		self.assertEqual(result["state"], "complete")
		self.assertEqual(result["projection"], self.payload["projection"])
		observe.assert_not_called()

	def test_revocation_during_completed_result_reload_refuses_delivery(self):
		with (
			patch.object(api, "_load", side_effect=[(self.running, None), frappe.PermissionError]),
			patch.object(api.jobs, "observe_job", return_value=self.complete),
			self.assertRaises(frappe.PermissionError),
		):
			status_body("a" * 32)

	def test_expiry_during_completed_result_reload_refuses_delivery(self):
		with (
			patch.object(api, "_load", side_effect=[(self.running, None), api.SnapshotExpired]),
			patch.object(api.jobs, "observe_job", return_value=self.complete),
			self.assertRaises(api.SnapshotExpired),
		):
			status_body("a" * 32)

	def test_already_complete_result_does_not_repeat_full_row_authorization(self):
		with patch.object(api, "_load", return_value=(self.complete, self.payload)) as load:
			result = status_body("a" * 32)
		self.assertEqual(result["state"], "complete")
		self.assertEqual(load.call_count, 1)
