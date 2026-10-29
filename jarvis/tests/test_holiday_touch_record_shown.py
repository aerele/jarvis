"""Holiday touch ``record_shown``: the Christmas-track cadence write.

The endpoint answers ``{"play": bool}`` for the session user. It plays only when today's
answer carries a scene, and only when the cadence allows it; every play is recorded under a
row lock inside one ``txn.replay_on_conflict`` unit, so two requests at once cannot both play.

Most tests use a birthday (a scene on the day, no HRMS needed); the lead-up and day-2 cases
need HRMS and skip without it. The race tests use committed fixtures and a second real
connection (``jarvis.tests.race_harness``): the unit commits on purpose, so a rollback cannot
clean up after them.
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import frappe

from jarvis.chat import txn
from jarvis.holiday_touch import api, cadence, resolve
from jarvis.tests.race_harness import (
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_off,
	snapshot_isolation_on,
)
from jarvis.tests.test_holiday_touch_resolve import (
	DIWALI,
	HAS_HRMS,
	IST,
	USETT,
	_Base,
	_now,
	_set,
)
from jarvis.tests.test_snapshot_races_user_state import (
	_cleanup_user_state,
	_ensure_user,
	_insert_settings_row,
)

BIRTHDAY = "1990-11-08"


def _at(hour: int, minute: int = 0, on: date = DIWALI) -> datetime:
	"""``hour:minute`` LOCAL time in Kolkata on ``on``, as a UTC instant."""
	local = datetime(on.year, on.month, on.day, hour, minute, tzinfo=timezone(timedelta(hours=5, minutes=30)))
	return local.astimezone(timezone.utc)


def _stored(user: str) -> dict | None:
	raw = frappe.db.get_value(USETT, user, "holiday_touch_shown")
	return json.loads(raw) if raw else None


class _Case(_Base):
	def record(self, user: str, now: datetime) -> dict:
		"""``record_shown`` as ``user`` with the clock pinned to ``now``."""
		frappe.set_user(user)
		with mock.patch.object(api, "_now", return_value=now):
			return api.record_shown()


class TestEndpointShape(_Case):
	def test_it_is_a_post_only_whitelisted_endpoint_with_no_arguments(self):
		self.assertIn(api.record_shown, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.record_shown], ["POST"])
		self.assertEqual(api.record_shown.__code__.co_argcount, 0)
		self.assertNotIn(api.record_shown, frappe.guest_methods)

	def test_a_guest_is_denied_with_a_403(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError) as ctx:
			api.record_shown()
		self.assertEqual(ctx.exception.http_status_code, 403)

	def test_a_user_without_jarvis_access_is_denied_and_nothing_is_written(self):
		user = self.person(dob=BIRTHDAY)
		frappe.db.delete("Has Role", {"parent": user, "role": "Jarvis User"})
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			api.record_shown()
		self.assertFalse(frappe.db.exists(USETT, user))


class TestNoScene(_Case):
	"""A day with no scene never plays and never writes, even to create the settings row."""

	def assert_no_play(self, user: str, now: datetime) -> None:
		self.assertEqual(self.record(user, now), {"play": False})
		self.assertFalse(frappe.db.exists(USETT, user))

	def test_a_day_with_nothing_to_show(self):
		self.assert_no_play(self.person(), _at(9))

	def test_white_label_has_no_scene(self):
		user = self.person(dob=BIRTHDAY)
		_set("brand_logo", "/files/logo.png")
		self.assert_no_play(user, _at(9))

	def test_the_switch_off_has_no_scene(self):
		user = self.person(dob=BIRTHDAY)
		_set("holiday_touches_enabled", 0)
		self.assert_no_play(user, _at(9))

	def test_a_lead_up_day_has_no_scene(self):
		if not HAS_HRMS:
			self.skipTest("hrms is not installed on this site")
		user = self.person([(DIWALI, "Diwali", 0)])
		self.assert_no_play(user, _at(9, on=date(2026, 11, 6)))

	def test_day_two_of_a_run_has_no_scene(self):
		if not HAS_HRMS:
			self.skipTest("hrms is not installed on this site")
		rows = [(date(2026, 11, 7), "Diwali", 0), (date(2026, 11, 10), "Diwali", 0)]
		user = self.person(rows)
		self.assert_no_play(user, _at(9, on=date(2026, 11, 10)))

	def test_a_failing_answer_is_false_not_an_error(self):
		user = self.person(dob=BIRTHDAY)
		with (
			mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")),
			mock.patch("frappe.log_error"),
		):
			self.assertEqual(self.record(user, _at(9)), {"play": False})
		self.assertFalse(frappe.db.exists(USETT, user))


class TestCadence(_Case):
	def test_the_first_morning_showing_plays_and_the_second_does_not(self):
		user = self.person(dob=BIRTHDAY)
		self.assertEqual(self.record(user, _at(9)), {"play": True})
		self.assertEqual(self.record(user, _at(11)), {"play": False})
		self.assertEqual(len(_stored(user)["times"]), 1)

	def test_the_afternoon_waits_three_hours_after_the_morning(self):
		user = self.person(dob=BIRTHDAY)
		self.assertTrue(self.record(user, _at(12, 30))["play"])
		self.assertFalse(self.record(user, _at(15, 29))["play"])  # 2h59
		self.assertTrue(self.record(user, _at(15, 30))["play"])  # exactly 3h
		self.assertFalse(self.record(user, _at(18))["play"])  # both slots used
		self.assertEqual(len(_stored(user)["times"]), 2)

	def test_an_afternoon_showing_with_no_morning_one_plays_once(self):
		user = self.person(dob=BIRTHDAY)
		self.assertTrue(self.record(user, _at(14))["play"])
		self.assertFalse(self.record(user, _at(19))["play"])

	def test_a_refused_showing_is_not_recorded(self):
		user = self.person(dob=BIRTHDAY)
		self.record(user, _at(9))
		before = _stored(user)
		self.assertFalse(self.record(user, _at(10))["play"])
		self.assertEqual(_stored(user), before)

	def test_showings_are_stored_as_utc_with_the_local_date(self):
		user = self.person(dob=BIRTHDAY)
		now = _at(0, 30)  # 00:30 on the 8th in Kolkata is still the 7th in UTC
		self.assertTrue(self.record(user, now)["play"])
		state = _stored(user)
		self.assertEqual(state["date"], "2026-11-08")
		self.assertEqual(state["times"], [now.isoformat()])
		self.assertTrue(state["times"][0].endswith("+00:00"))
		self.assertTrue(state["times"][0].startswith("2026-11-07"))

	def test_an_existing_row_is_updated_in_place_without_touching_modified(self):
		user = self.person(dob=BIRTHDAY)
		frappe.get_doc({"doctype": USETT, "user": user}).insert(ignore_permissions=True)
		stamp = datetime(2020, 1, 1, 12, 0, 0)
		frappe.db.set_value(USETT, user, "modified", stamp, update_modified=False)
		self.assertTrue(self.record(user, _at(9))["play"])
		self.assertEqual(frappe.db.count(USETT, {"user": user}), 1)
		self.assertEqual(frappe.db.get_value(USETT, user, "modified"), stamp)

	def test_a_new_local_date_replaces_the_old_times(self):
		user = self.person(dob=BIRTHDAY)
		old = {"date": "2026-11-07", "times": ["2026-11-07T03:30:00+00:00", "2026-11-07T10:00:00+00:00"]}
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": json.dumps(old)}).insert(
			ignore_permissions=True
		)
		self.assertTrue(self.record(user, _at(9))["play"])
		self.assertEqual(_stored(user), {"date": "2026-11-08", "times": [_at(9).isoformat()]})

	def test_junk_in_the_stored_state_does_not_block_or_break_a_showing(self):
		user = self.person(dob=BIRTHDAY)
		frappe.get_doc({"doctype": USETT, "user": user, "holiday_touch_shown": "{not json"}).insert(
			ignore_permissions=True
		)
		self.assertTrue(self.record(user, _at(9))["play"])
		self.assertEqual(_stored(user)["times"], [_at(9).isoformat()])

	def test_the_stored_state_matches_what_the_answer_reads(self):
		user = self.person(dob=BIRTHDAY)
		now = _at(9)
		self.assertTrue(self.record(user, now)["play"])
		self.assertFalse(resolve.answer(user, now + timedelta(minutes=5))["may_play"])
		self.assertFalse(cadence.may_play(now + timedelta(minutes=5), [now], IST))

	def test_the_user_switch_off_means_false_and_no_write(self):
		user = self.person(dob=BIRTHDAY)
		frappe.get_doc({"doctype": USETT, "user": user, "show_holiday_touches": 0}).insert(
			ignore_permissions=True
		)
		self.assertEqual(self.record(user, _at(9)), {"play": False})
		self.assertIsNone(_stored(user))


class TestForceOverride(_Case):
	def test_the_override_plays_every_time_and_records_nothing(self):
		user = self.person()  # no occasion of its own
		with mock.patch.dict(frappe.conf, {"developer_mode": 1, "jarvis_holiday_touch_force": "diwali:day"}):
			self.assertEqual(self.record(user, _at(9)), {"play": True})
			self.assertEqual(self.record(user, _at(9, 5)), {"play": True})
		self.assertFalse(frappe.db.exists(USETT, user))

	def test_the_override_is_ignored_outside_developer_mode(self):
		user = self.person()
		with mock.patch.dict(frappe.conf, {"developer_mode": 0, "jarvis_holiday_touch_force": "diwali:day"}):
			self.assertEqual(self.record(user, _at(9)), {"play": False})

	def test_a_forced_lead_up_still_has_no_scene(self):
		user = self.person()
		with mock.patch.dict(
			frappe.conf, {"developer_mode": 1, "jarvis_holiday_touch_force": "diwali:leadup"}
		):
			self.assertEqual(self.record(user, _at(9)), {"play": False})


def _fail_on_shown_write(error, calls):
	"""A ``set_value`` that raises ``error`` for the showing field only (and counts the
	attempts in ``calls``), so the answer's own reads and the settings row's creation are
	untouched."""
	real = frappe.db.set_value

	def set_value(doctype, name, field=None, *args, **kwargs):
		if field == "holiday_touch_shown":
			calls.append(1)
			raise error
		return real(doctype, name, field, *args, **kwargs)

	return mock.patch.object(frappe.db, "set_value", set_value)


class TestFaultsNeverReachTheClient(_Case):
	def test_a_lost_race_on_a_row_created_in_this_request_is_false_not_a_500(self):
		user = self.person(dob=BIRTHDAY)  # no settings row yet: the endpoint creates it
		self.assertFalse(frappe.db.exists(USETT, user))
		calls = []
		conflict = frappe.QueryDeadlockError("Record has changed since last read")

		with _fail_on_shown_write(conflict, calls), mock.patch("frappe.log_error"):
			self.assertEqual(self.record(user, _at(9)), {"play": False})
		self.assertEqual(len(calls), 1)  # the row's creation is pending: no replay is safe

	def test_an_unexpected_fault_is_false_and_reported(self):
		user = self.person(dob=BIRTHDAY)
		with (
			_fail_on_shown_write(RuntimeError("boom"), []),
			mock.patch.object(api, "report") as report,
		):
			self.assertEqual(self.record(user, _at(9)), {"play": False})
		report.assert_called_once()

	def test_an_unknown_zone_in_the_answer_is_false(self):
		user = self.person(dob=BIRTHDAY)
		scene = {"scene": True, "tz": "Not/AZone"}
		with (
			mock.patch.object(resolve, "safe_answer", return_value=scene),
			mock.patch.object(api, "report"),
		):
			self.assertEqual(self.record(user, _at(9)), {"play": False})

	def test_a_site_without_the_field_answers_false(self):
		user = self.person(dob=BIRTHDAY)
		meta = frappe.get_meta(USETT)
		with mock.patch.object(type(meta), "has_field", side_effect=lambda f: f != "holiday_touch_shown"):
			self.assertEqual(self.record(user, _at(9)), {"play": False})


# --------------------------------------------------------------------------- #
# The race: two requests recording at once
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _db_is(other):
	"""Run the body on the ``other`` connection: what a second web worker sees."""
	main = frappe.local.db
	frappe.local.db = other
	try:
		yield
	finally:
		frappe.local.db = main


class TestRecordRace(_Case):
	"""A real second connection commits while this request holds an older read view.

	Fixtures are committed (the unit commits nothing, but the competing connection must
	see the user and the row) and removed in ``_cleanup_user_state``."""

	NOW = _at(9)

	def setUp(self):
		super().setUp()
		frappe.db.rollback()

	def fixture(self) -> str:
		user = f"jarvis-holiday-race-{frappe.generate_hash(length=8)}@example.test"
		_ensure_user(user)
		self.addCleanup(_cleanup_user_state, user)
		with other_connection() as other:
			_insert_settings_row(other, user)
			other.commit()
		return user

	@contextlib.contextmanager
	def as_the_request(self, user):
		"""The request: an access-checked session user whose answer carries a scene."""
		scene = {"scene": True, "tz": IST}
		frappe.set_user(user)
		with (
			mock.patch.object(api, "require_jarvis_access"),
			mock.patch.object(api, "_now", return_value=self.NOW),
			mock.patch.object(resolve, "safe_answer", return_value=scene),
		):
			yield

	def test_two_requests_at_once_play_exactly_once(self):
		def scenario():
			user = self.fixture()
			with (
				self.as_the_request(user),
				snapshot_isolation_on(),
				mock.patch.object(api, "report") as failed,
				mock.patch.object(txn, "report_lost_race", wraps=txn.report_lost_race) as lost,
			):
				open_read_view()  # this request has already read: its snapshot is older than...
				with other_connection() as other, _db_is(other):
					winner = api.record_shown()  # ...the other request, which records and commits
					other.commit()
				loser = api.record_shown()
			return winner, loser, user, failed.call_count + lost.call_count

		def check(result):
			winner, loser, user, errors = result
			self.assertEqual([winner, loser], [{"play": True}, {"play": False}])
			self.assertEqual(errors, 0)  # the replay absorbed the conflict: no give-up path ran
			self.assertEqual(len(_stored(user)["times"]), 1)

		assert_fix_closes_race(self, scenario, check)

	def test_a_harmless_concurrent_change_to_the_row_still_plays(self):
		"""A commit that does not use up the slot (any other write to the row) fails the lock
		with the engine's 1020; the replay must still record this request's showing."""

		def scenario():
			user = self.fixture()
			with self.as_the_request(user), snapshot_isolation_on():
				open_read_view()
				with other_connection() as other:
					other.sql(
						"UPDATE `tabJarvis User Settings` SET modified = NOW(6) WHERE name = %s", (user,)
					)
					other.commit()
				return user, api.record_shown()

		def check(result):
			user, answer = result
			self.assertEqual(answer, {"play": True})
			self.assertEqual(_stored(user), {"date": "2026-11-08", "times": [self.NOW.isoformat()]})

		assert_fix_closes_race(self, scenario, check)

	def test_a_lost_final_replay_is_false_not_a_500(self):
		user = self.fixture()
		calls = []
		conflict = frappe.QueryDeadlockError("Record has changed since last read")

		with (
			self.as_the_request(user),
			_fail_on_shown_write(conflict, calls),
			mock.patch("frappe.log_error"),
		):
			self.assertEqual(api.record_shown(), {"play": False})
		self.assertEqual(len(calls), 2)  # the unit ran, was replayed once, then gave up

	def test_the_row_lock_reads_the_present_when_the_engine_does_not_refuse_a_stale_row(self):
		"""On a server without snapshot isolation (MariaDB 10.x) nothing fails: a plain read
		would still see the old row and play twice. Only the locking read sees the winner."""
		user = self.fixture()
		with self.as_the_request(user), snapshot_isolation_off():
			open_read_view()
			with other_connection() as other, _db_is(other):
				self.assertEqual(api.record_shown(), {"play": True})
				other.commit()
			self.assertEqual(api.record_shown(), {"play": False})
		frappe.db.rollback()  # leave the old read view: what the winner committed is now visible
		self.assertEqual(len(_stored(user)["times"]), 1)

	def test_the_second_request_after_a_clean_first_one_is_refused(self):
		user = self.fixture()
		with self.as_the_request(user):
			self.assertEqual(api.record_shown(), {"play": True})
			frappe.db.commit()
			self.assertEqual(api.record_shown(), {"play": False})
		self.assertEqual(len(_stored(user)["times"]), 1)
