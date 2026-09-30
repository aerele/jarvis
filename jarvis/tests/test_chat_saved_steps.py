"""Tests for the saved live steps: the pump's published-line cache, the settlement
stamp onto the reply row, and the reload path that reads them back.

No test here needs the ``steps`` column, so the DB write is mocked.
"""

import inspect
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import api, pump, settlement


class TestRunStepLines(FrappeTestCase):
	def setUp(self):
		self.run_id = f"steps-test-{frappe.generate_hash(length=10)}"
		self.addCleanup(frappe.cache().delete_value, pump._run_step_lines_key(self.run_id))

	def texts(self):
		return [line["text"] for line in pump._read_run_step_lines(self.run_id)]

	def test_missing_key_reads_empty(self):
		self.assertEqual(pump._read_run_step_lines(self.run_id), [])

	def test_append_records_text_and_time(self):
		pump._append_run_step_line(self.run_id, "Checking the ledger")
		pump._append_run_step_line(self.run_id, "Counting invoices")
		lines = pump._read_run_step_lines(self.run_id)
		self.assertEqual([line["text"] for line in lines], ["Checking the ledger", "Counting invoices"])
		self.assertTrue(all(line["at"] for line in lines))

	def test_exact_repeat_is_skipped(self):
		pump._append_run_step_line(self.run_id, "One")
		pump._append_run_step_line(self.run_id, "Two")
		pump._append_run_step_line(self.run_id, "One")
		self.assertEqual(self.texts(), ["One", "Two"])

	def test_growing_resend_replaces_last_and_keeps_at(self):
		pump._append_run_step_line(self.run_id, "Looking")
		first_at = pump._read_run_step_lines(self.run_id)[0]["at"]
		with mock_patch("frappe.utils.now", return_value="2099-01-01 00:00:00"):
			pump._append_run_step_line(self.run_id, "Looking at the invoices")
		lines = pump._read_run_step_lines(self.run_id)
		self.assertEqual(len(lines), 1)
		self.assertEqual(lines[0]["text"], "Looking at the invoices")
		self.assertEqual(lines[0]["at"], first_at)

	def test_shorter_resend_is_skipped(self):
		pump._append_run_step_line(self.run_id, "Looking at the invoices")
		pump._append_run_step_line(self.run_id, "Looking")
		self.assertEqual(self.texts(), ["Looking at the invoices"])

	def test_list_is_capped(self):
		for i in range(45):
			pump._append_run_step_line(self.run_id, f"step {i:02d}")
		texts = self.texts()
		self.assertEqual(len(texts), 40)
		self.assertEqual(texts[0], "step 00")
		self.assertEqual(texts[-1], "step 39")


class TestStampSteps(FrappeTestCase):
	def test_writes_the_lines_for_the_reply(self):
		lines = [{"text": "One", "at": "2026-09-29 10:00:00"}]
		with (
			mock_patch.object(pump, "_read_run_step_lines", return_value=lines),
			mock_patch.object(settlement.ts, "_run_cas") as cas,
		):
			settlement._stamp_steps("MSG-1", "run-1")
		sql, params = cas.call_args.args
		self.assertIn("SET steps=%(s)s", sql)
		self.assertNotIn("modified", sql)
		self.assertEqual(params, {"s": frappe.as_json(lines), "m": "MSG-1"})

	def test_writes_nothing_without_lines(self):
		with (
			mock_patch.object(pump, "_read_run_step_lines", return_value=[]),
			mock_patch.object(settlement.ts, "_run_cas") as cas,
		):
			settlement._stamp_steps("MSG-1", "run-1")
		cas.assert_not_called()

	def test_a_cache_error_never_raises(self):
		with (
			mock_patch.object(pump, "_read_run_step_lines", side_effect=RuntimeError("redis down")),
			mock_patch.object(settlement.ts, "_run_cas") as cas,
		):
			settlement._stamp_steps("MSG-1", "run-1")
		cas.assert_not_called()

	def test_a_missing_column_settles_without_steps(self):
		# Code ahead of its migrate: the reply must still settle.
		with (
			mock_patch.object(pump, "_read_run_step_lines", return_value=[{"text": "One", "at": "x"}]),
			mock_patch.object(settlement.ts, "_run_cas", side_effect=RuntimeError("Unknown column")),
			mock_patch.object(frappe.db, "is_missing_column", return_value=True),
		):
			settlement._stamp_steps("MSG-1", "run-1")

	def test_any_other_write_error_still_raises(self):
		# A snapshot race must reach txn.replay_on_conflict, never be swallowed.
		with (
			mock_patch.object(pump, "_read_run_step_lines", return_value=[{"text": "One", "at": "x"}]),
			mock_patch.object(settlement.ts, "_run_cas", side_effect=RuntimeError("1020")),
			mock_patch.object(frappe.db, "is_missing_column", return_value=False),
			self.assertRaises(RuntimeError),
		):
			settlement._stamp_steps("MSG-1", "run-1")

	def test_settlement_stamps_steps_right_after_the_duration(self):
		src = inspect.getsource(settlement.invoke_settlement)
		pattern = (
			r"if\s+won\s+and\s+am\s*:\s*"
			r"_stamp_reply_duration\(\s*am\s*,\s*run_id\s*\)\s*"
			r"_stamp_steps\(\s*am\s*,\s*run_id\s*\)"
		)
		self.assertRegex(src, re.compile(pattern))


class TestRunPreambles(FrappeTestCase):
	# Which cached raw steps the runtime flagged, so a pump hop re-seeds the flag
	# and the saved reply still strips a preamble recorded before the hop.
	def setUp(self):
		self.run_id = f"preambles-test-{frappe.generate_hash(length=10)}"
		self.addCleanup(frappe.cache().delete_value, pump._run_preambles_key(self.run_id))

	def test_append_and_read(self):
		self.assertEqual(pump._read_run_preambles(self.run_id), [])
		pump._append_run_preamble(self.run_id, "I found the report. Checking filters.")
		pump._append_run_preamble(self.run_id, "I found the report. Checking filters.")
		pump._append_run_preamble(self.run_id, "Running it now.")
		self.assertEqual(
			pump._read_run_preambles(self.run_id),
			["I found the report. Checking filters.", "Running it now."],
		)

	def test_on_step_keeps_the_flag_and_a_hop_reseeds_it(self):
		on_step = inspect.getsource(pump._make_handler)
		self.assertRegex(
			on_step, r"if\s+preamble\s+and\s+raw\s*:\s*_append_run_preamble\(\s*rs\.run_id\s*,\s*raw\s*\)"
		)
		reattach = inspect.getsource(pump._reattach_lane)
		self.assertRegex(reattach, r"preamble_steps\s*=\s*_read_run_preambles\(\s*run_id\s*\)")


class TestLiveTurnSteps(FrappeTestCase):
	def run_it(self, published, raw):
		turn = SimpleNamespace(
			name="run-x",
			assistant_message="MSG-x",
			last_event_seq=3,
			pump_epoch=1,
			state="running",
		)
		qb = MagicMock()
		qb.from_.return_value.select.return_value.where.return_value.run.return_value = [turn]
		messages = [{"name": "MSG-x", "streaming": 1, "content": "Hello"}]
		with (
			mock_patch.object(frappe, "qb", qb),
			mock_patch.object(pump, "_read_run_step_lines", return_value=published),
			mock_patch.object(pump, "_read_run_steps", return_value=raw),
		):
			api._live_turn_steps(messages)
		return messages[0]

	def test_published_lines_win(self):
		m = self.run_it([{"text": "Redacted line", "at": "x"}], ["Secret raw line"])
		self.assertEqual(m["live_steps"], ["Redacted line"])
		# When each was seen, so the reloaded tab orders it among the saved tool rows.
		self.assertEqual(m["live_step_times"], ["x"])
		self.assertEqual(m["run_id"], "run-x")

	def test_falls_back_to_the_raw_copy(self):
		m = self.run_it([], ["Checking the ledger"])
		self.assertEqual(m["live_steps"], ["Checking the ledger"])
		self.assertEqual(m["live_step_times"], [])
