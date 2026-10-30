"""The pure scene-cadence rule, driven by the JSON case table."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from jarvis.holiday_touch import cadence
from jarvis.holiday_touch.cadence import add_showing, may_play

CASES = Path(cadence.__file__).with_name("cadence_cases.json")
UTC = timezone.utc


def _dt(text):
	return datetime.fromisoformat(text)


class TestCadenceTable(unittest.TestCase):
	def test_table_is_well_formed(self):
		rows = json.loads(CASES.read_text())
		self.assertGreaterEqual(len(rows), 18)
		self.assertEqual(len({r["name"] for r in rows}), len(rows))
		for r in rows:
			self.assertEqual(set(r), {"name", "tz", "now", "times", "expect"})

	def test_every_row(self):
		for row in json.loads(CASES.read_text()):
			with self.subTest(case=row["name"]):
				got = may_play(_dt(row["now"]), [_dt(t) for t in row["times"]], row["tz"])
				self.assertIs(got, row["expect"])


class TestMayPlayEdges(unittest.TestCase):
	def test_times_order_does_not_matter(self):
		now = _dt("2026-10-28T12:00:00+00:00")
		a, b = _dt("2026-10-28T05:00:00+00:00"), _dt("2026-10-28T09:00:00+00:00")
		self.assertEqual(may_play(now, [a, b], "UTC"), may_play(now, [b, a], "UTC"))

	def test_naive_datetimes_are_read_as_utc(self):
		self.assertTrue(may_play(datetime(2026, 10, 28, 9, 0), [], "UTC"))
		self.assertFalse(may_play(datetime(2026, 10, 28, 10, 0), [datetime(2026, 10, 28, 9, 0)], "UTC"))


class TestAddShowing(unittest.TestCase):
	def test_first_showing_creates_state_in_local_date(self):
		now = _dt("2026-10-28T20:00:00+00:00")  # 01:30 on the 29th in Kolkata
		state = add_showing(None, now, "Asia/Kolkata")
		self.assertEqual(state, {"date": "2026-10-29", "times": ["2026-10-28T20:00:00+00:00"]})

	def test_second_showing_appends(self):
		first = add_showing(None, _dt("2026-10-28T09:00:00+00:00"), "UTC")
		second = add_showing(first, _dt("2026-10-28T14:00:00+00:00"), "UTC")
		self.assertEqual(second["date"], "2026-10-28")
		self.assertEqual(second["times"], ["2026-10-28T09:00:00+00:00", "2026-10-28T14:00:00+00:00"])

	def test_previous_dates_times_are_dropped(self):
		old = {"date": "2026-10-27", "times": ["2026-10-27T09:00:00+00:00", "2026-10-27T14:00:00+00:00"]}
		state = add_showing(old, _dt("2026-10-28T09:00:00+00:00"), "UTC")
		self.assertEqual(state, {"date": "2026-10-28", "times": ["2026-10-28T09:00:00+00:00"]})

	def test_non_utc_input_is_stored_as_utc(self):
		ist = timezone(timedelta(hours=5, minutes=30))
		state = add_showing(None, datetime(2026, 10, 28, 9, 0, tzinfo=ist), "Asia/Kolkata")
		self.assertEqual(state["times"], ["2026-10-28T03:30:00+00:00"])

	def test_garbage_state_is_ignored(self):
		for bad in (
			"x",
			3,
			{"times": "nope"},
			{"times": 5},
			{"date": "2026-10-28", "times": [None, 5, "junk"]},
			[],
		):
			with self.subTest(bad=bad):
				state = add_showing(bad, _dt("2026-10-28T09:00:00+00:00"), "UTC")
				self.assertEqual(state["times"], ["2026-10-28T09:00:00+00:00"])

	def test_state_round_trips_through_may_play(self):
		now = _dt("2026-10-28T09:00:00+00:00")
		state = add_showing(None, now, "UTC")
		times = [_dt(t) for t in state["times"]]
		self.assertFalse(may_play(now + timedelta(hours=1), times, "UTC"))
		self.assertTrue(may_play(now + timedelta(hours=4), times, "UTC"))
		self.assertEqual(state["times"], [t.astimezone(UTC).isoformat() for t in times])

	def test_malformed_zone_key_raises_value_error(self):
		# Contract: the caller catches ZoneInfoNotFoundError (unknown zone) and ValueError (malformed key).
		now = _dt("2026-10-28T09:00:00+00:00")
		for bad in ("", "../x", "/etc/passwd"):
			with self.subTest(tz=bad):
				with self.assertRaises(ValueError):
					may_play(now, [], bad)
				with self.assertRaises(ValueError):
					add_showing(None, now, bad)
		with self.assertRaises(ZoneInfoNotFoundError):
			may_play(now, [], "Mars/Olympus_Mons")
