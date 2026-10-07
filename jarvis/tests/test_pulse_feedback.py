"""Tests for the periodic business-pulse survey's backend: the lazy
period-key gate on ``Jarvis User Settings`` and the two whitelisted endpoints.

Fixture shape mirrors ``test_session_feedback.py`` (a dedicated disposable
user, real conversations via ``create_conversation``) rather than a
rollback-only one: the settings row is created by
``usage.get_or_create_user_settings`` and other tests commit, so every case
resets the two pulse fields itself instead of trusting a clean row.

The admin forward is always mocked - these never hit a real admin.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.exceptions import FrappeTypeError
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_conversation
from jarvis.chat.feedback import (
	_PULSE_PREVIOUS_MONTH_DAYS,
	PULSE_MAX_OFFERS,
	_pulse_period,
	_pulse_window_end,
	_pulse_window_start,
	_stored_key_is_ahead,
	pulse_context,
	submit_pulse_feedback,
)
from jarvis.chat.usage import get_or_create_user_settings

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
SETTINGS = "Jarvis User Settings"
TEST_USER = "jarvis-pulse-feedback-test@example.com"

_PUSH = "jarvis.admin_client.push_pulse_feedback"
_FEATURES = "jarvis.chat.feature_usage.get_used_features"
_NOW = "frappe.utils.now_datetime"
#: A key that can never be the current one, so the row reads as "last offered
#: in some earlier period".
_STALE_KEY = "M:1999-01"


def _ensure_test_user(user: str = TEST_USER) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Pulse",
			"last_name": "Feedback",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	doc.add_roles("System Manager")
	frappe.db.commit()


def _delete_conv(name: str) -> None:
	for turn in frappe.get_all(TURN, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(TURN, turn, ignore_permissions=True, force=True)
	for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
		frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
	if frappe.db.exists(CONV, name):
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	frappe.db.commit()


def _cleanup_user_conversations(user: str = TEST_USER) -> None:
	for name in frappe.get_all(CONV, filters={"owner": user}, pluck="name"):
		_delete_conv(name)


def _reviewed_month_activity():
	"""A moment inside the window the survey reviews right now, whatever day of
	the month the suite runs on: days 1..10 review the previous month, whose
	window ends before today, so "now" is NOT in it then."""
	now = frappe.utils.now_datetime()
	if now.day <= _PULSE_PREVIOUS_MONTH_DAYS:
		return now.replace(day=1, hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
	return now


class _PulseTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.settings = get_or_create_user_settings(TEST_USER).name
		self._set_pulse(None, 0)
		# One conversation with a settled turn inside the window, so the "had at
		# least one turn in the period" gate passes unless a case says otherwise.
		self.conv = create_conversation()
		self._set_turns(5)

	def tearDown(self):
		self._set_pulse(None, 0)
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _set_pulse(self, key, count):
		frappe.db.set_value(
			SETTINGS,
			self.settings,
			{"pulse_last_period_key": key, "pulse_offer_count": count},
			update_modified=False,
		)

	def _pulse(self):
		return frappe.db.get_value(
			SETTINGS, self.settings, ["pulse_last_period_key", "pulse_offer_count"], as_dict=True
		)

	def _set_turns(self, count, *, days_ago=None, conversation=None, active_at=None):
		conversation = conversation or self.conv
		active_at = active_at or (
			frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days_ago)
			if days_ago
			else _reviewed_month_activity()
		)
		frappe.db.set_value(
			CONV,
			conversation,
			{"turn_count": count, "last_active_at": active_at},
			update_modified=False,
		)
		# Bounded (previous-month) windows read the user's messages, not the
		# conversation counter, so mirror the turns with one user message.
		self._set_user_messages(conversation, [active_at] if count else [])

	def _set_user_messages(self, conversation, created):
		for name in frappe.get_all(MSG, filters={"conversation": conversation}, pluck="name"):
			frappe.delete_doc(MSG, name, ignore_permissions=True, force=True)
		for seq, when in enumerate(created, start=1):
			doc = frappe.get_doc(
				{"doctype": MSG, "conversation": conversation, "seq": seq, "role": "user", "content": "hi"}
			).insert(ignore_permissions=True)
			frappe.db.set_value(MSG, doc.name, "creation", when, update_modified=False)


class TestPulsePeriod(FrappeTestCase):
	"""The month the survey reviews: the previous one for the first 10 days."""

	def test_day_one_and_day_ten_review_the_previous_month(self):
		for day in (1, 10):
			key, label, is_previous = _pulse_period(datetime(2026, 10, day, 9, 0))
			self.assertEqual((key, label, is_previous), ("M:2026-09", "Last month, Sep 2026", True))

	def test_day_eleven_reviews_the_current_month(self):
		key, label, is_previous = _pulse_period(datetime(2026, 10, 11, 9, 0))
		self.assertEqual((key, label, is_previous), ("M:2026-10", "This month, Oct 2026", False))

	def test_first_of_january_reviews_the_previous_december(self):
		key, label, is_previous = _pulse_period(datetime(2027, 1, 1, 0, 5))
		self.assertEqual((key, label, is_previous), ("M:2026-12", "Last month, Dec 2026", True))


class TestPulseWindow(FrappeTestCase):
	def test_previous_month_review_starts_on_the_first_at_midnight(self):
		for day in (1, 10):
			start = _pulse_window_start(datetime(2026, 10, day, 15, 30))
			self.assertEqual(start, datetime(2026, 9, 1, 0, 0))

	def test_current_month_review_keeps_the_rolling_thirty_days(self):
		now = datetime(2026, 10, 11, 15, 30)
		self.assertEqual(_pulse_window_start(now), datetime(2026, 9, 11, 15, 30))

	def test_january_review_starts_on_the_first_of_december(self):
		self.assertEqual(_pulse_window_start(datetime(2027, 1, 5, 8, 0)), datetime(2026, 12, 1, 0, 0))

	def test_previous_month_review_ends_on_the_first_of_the_current_month(self):
		for day in (1, 10):
			end = _pulse_window_end(datetime(2026, 10, day, 15, 30))
			self.assertEqual(end, datetime(2026, 10, 1, 0, 0))

	def test_current_month_review_is_open_ended(self):
		self.assertIsNone(_pulse_window_end(datetime(2026, 10, 11, 15, 30)))


class TestStoredKeyIsAhead(FrappeTestCase):
	def test_later_month_is_ahead_equal_and_earlier_are_not(self):
		self.assertTrue(_stored_key_is_ahead("M:2026-10", "M:2026-09"))
		self.assertTrue(_stored_key_is_ahead("M:2027-01", "M:2026-12"))
		self.assertFalse(_stored_key_is_ahead("M:2026-09", "M:2026-09"))
		self.assertFalse(_stored_key_is_ahead("M:2026-08", "M:2026-09"))

	def test_blank_and_non_monthly_keys_are_never_ahead(self):
		for stored in (None, "", "W:2026-10-04", "D:2026-10-05"):
			self.assertFalse(_stored_key_is_ahead(stored, "M:2026-09"), stored)


class TestPulseContext(_PulseTestCase):
	def test_early_in_the_month_offers_the_previous_month(self):
		with patch(_NOW, return_value=datetime(2026, 10, 1, 9, 0)):
			self._set_turns(5, active_at=datetime(2026, 9, 20, 12, 0))
			with patch(_FEATURES, return_value={}) as features:
				result = pulse_context()
		# The feature sweep is bounded to September too.
		features.assert_called_once_with(TEST_USER, datetime(2026, 9, 1, 0, 0), datetime(2026, 10, 1, 0, 0))
		self.assertTrue(result["due"])
		self.assertEqual(result["period_key"], "M:2026-09")
		self.assertEqual(result["period_label"], "Last month, Sep 2026")
		self.assertTrue(result["period_is_previous"])
		self.assertEqual(self._pulse().pulse_last_period_key, "M:2026-09")

	def test_only_this_months_activity_early_in_the_month_is_not_due(self):
		"""A first chat on Oct 3 says nothing about September: the previous-month
		window ends on Oct 1, so the survey must not ask about a month the user
		never used, nor list October's features as last month's."""
		with patch(_NOW, return_value=datetime(2026, 10, 4, 9, 0)):
			self._set_turns(5, active_at=datetime(2026, 10, 3, 9, 0))
			self._set_user_messages(self.conv, [datetime(2026, 10, 3, 9, 0), datetime(2026, 10, 4, 8, 0)])
			with patch(_FEATURES) as features:
				self.assertFalse(pulse_context()["due"])
		features.assert_not_called()
		self.assertIsNone(self._pulse().pulse_last_period_key, "a non-offer burns nothing")

	def test_a_conversation_spanning_the_boundary_is_due_for_the_previous_month(self):
		"""One long-lived conversation used all September and again on Oct 2:
		its ``last_active_at`` is Oct 2, but the September messages still count."""
		with patch(_NOW, return_value=datetime(2026, 10, 5, 9, 0)):
			self._set_turns(40, active_at=datetime(2026, 10, 2, 9, 0))
			self._set_user_messages(self.conv, [datetime(2026, 9, 15, 10, 0), datetime(2026, 10, 2, 9, 0)])
			with patch(_FEATURES, return_value={}):
				result = pulse_context()
		self.assertTrue(result["due"])
		self.assertEqual(result["period_key"], "M:2026-09")

	def test_after_day_ten_offers_the_current_month_again(self):
		self._set_pulse("M:2026-09", PULSE_MAX_OFFERS)
		with patch(_NOW, return_value=datetime(2026, 10, 11, 9, 0)):
			self._set_turns(5, active_at=datetime(2026, 10, 9, 12, 0))
			with patch(_FEATURES, return_value={}) as features:
				result = pulse_context()
		features.assert_called_once()
		self.assertIsNone(features.call_args.args[2], "days 11+ stay open-ended")
		self.assertTrue(result["due"])
		self.assertEqual(result["period_key"], "M:2026-10")
		self.assertFalse(result["period_is_previous"])

	def test_answered_for_the_previous_month_is_not_re_asked_early(self):
		self._set_pulse("M:2026-09", PULSE_MAX_OFFERS)
		with patch(_NOW, return_value=datetime(2026, 10, 2, 9, 0)):
			self._set_turns(5, active_at=datetime(2026, 9, 20, 12, 0))
			with patch(_FEATURES) as features:
				self.assertFalse(pulse_context()["due"])
		features.assert_not_called()

	def test_answer_stored_early_in_the_month_by_the_old_code_is_not_re_asked(self):
		"""Before this fix days 1..10 stamped the CURRENT month's key. Such a user
		must not be re-asked on days 1..10 (computed key is the previous month)
		nor on day 11+ (computed key now equals the stored one, count capped)."""
		for day in (5, 11):
			with self.subTest(day=day):
				self._set_pulse("M:2026-10", PULSE_MAX_OFFERS)
				with patch(_NOW, return_value=datetime(2026, 10, day, 9, 0)):
					self._set_turns(5, active_at=datetime(2026, 9, 20, 12, 0))
					with patch(_FEATURES) as features:
						self.assertFalse(pulse_context()["due"])
				features.assert_not_called()
				row = self._pulse()
				self.assertEqual(row.pulse_last_period_key, "M:2026-10")
				self.assertEqual(row.pulse_offer_count, PULSE_MAX_OFFERS)

	def test_an_older_stored_key_is_still_asked_early_in_the_month(self):
		self._set_pulse("M:2026-08", PULSE_MAX_OFFERS)
		with patch(_NOW, return_value=datetime(2026, 10, 5, 9, 0)):
			self._set_turns(5, active_at=datetime(2026, 9, 20, 12, 0))
			with patch(_FEATURES, return_value={}):
				result = pulse_context()
		self.assertTrue(result["due"])
		self.assertEqual(result["period_key"], "M:2026-09")

	def test_due_when_never_offered(self):
		with patch(_FEATURES, return_value={}):
			result = pulse_context()
		self.assertTrue(result["due"])
		self.assertEqual(result["period_key"], _pulse_period()[0])
		self.assertEqual(result["period_label"], _pulse_period()[1])

	def test_an_offer_stamps_the_current_period_and_counts_one(self):
		with patch(_FEATURES, return_value={}):
			pulse_context()
		row = self._pulse()
		self.assertEqual(row.pulse_last_period_key, _pulse_period()[0])
		self.assertEqual(row.pulse_offer_count, 1)

	def test_maybe_later_re_offers_up_to_the_cap_then_goes_quiet(self):
		"""'Maybe later' is not recorded anywhere - the survey simply comes back
		on the next chat opens, three times in all, and then stops nagging."""
		with patch(_FEATURES, return_value={}):
			for expected in range(1, PULSE_MAX_OFFERS + 1):
				self.assertTrue(pulse_context()["due"], f"offer {expected} should be due")
				self.assertEqual(self._pulse().pulse_offer_count, expected)
			self.assertFalse(pulse_context()["due"], "capped, so quiet for the rest of the period")
		self.assertEqual(self._pulse().pulse_offer_count, PULSE_MAX_OFFERS, "the cap is not exceeded")

	def test_a_new_period_resets_the_offer_count(self):
		"""The cap must RESET on rollover, not merely stop decrementing: a user
		exhausted last month is due again this month, at count 1."""
		self._set_pulse(_STALE_KEY, PULSE_MAX_OFFERS)
		with patch(_FEATURES, return_value={}):
			self.assertTrue(pulse_context()["due"])
		row = self._pulse()
		self.assertEqual(row.pulse_last_period_key, _pulse_period()[0])
		self.assertEqual(row.pulse_offer_count, 1)

	def test_capped_never_computes_the_feature_list(self):
		"""The cheap period-key comparison gates the comparatively expensive
		feature-usage sweep (spec: performance review)."""
		self._set_pulse(_pulse_period()[0], PULSE_MAX_OFFERS)
		with patch(_FEATURES) as features:
			self.assertFalse(pulse_context()["due"])
		features.assert_not_called()

	def test_no_turns_at_all_is_not_due(self):
		self._set_turns(0)
		with patch(_FEATURES) as features:
			self.assertFalse(pulse_context()["due"])
		features.assert_not_called()
		self.assertIsNone(self._pulse().pulse_last_period_key, "a non-offer burns nothing")

	def test_turns_only_outside_the_window_are_not_due(self):
		self._set_turns(50, days_ago=60)
		with patch(_FEATURES) as features:
			self.assertFalse(pulse_context()["due"])
		features.assert_not_called()

	def test_turns_are_summed_across_the_users_conversations(self):
		"""A user with several shallow conversations still qualifies: the gate is
		the SUM of turn_count in the window, not any single conversation."""
		self._set_turns(0)
		second = create_conversation()
		self._set_turns(1, conversation=second)
		with patch(_FEATURES, return_value={}):
			self.assertTrue(pulse_context()["due"])

	def test_features_offered_may_be_empty_but_the_survey_is_still_due(self):
		# Stars and the open question are still worth asking; only the chip
		# question hides client-side when this list is empty.
		with patch(_FEATURES, return_value={}):
			result = pulse_context()
		self.assertTrue(result["due"])
		self.assertEqual(result["features_offered"], [])

	def test_both_endpoints_are_post_only(self):
		"""Both WRITE, and Frappe commits only for unsafe methods: reached over
		GET, pulse_context's offer claim (and a submit's silencing write) would
		be rolled back silently and the cap would never advance."""
		for fn in (pulse_context, submit_pulse_feedback):
			with self.subTest(fn=fn.__name__):
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])

	def test_a_user_with_no_settings_row_yet_is_offered_and_the_row_is_created(self):
		"""The settings row is created lazily by whichever surface needs it
		first, so the deciding read must tolerate its absence - and claiming the
		offer is the one path that creates it."""
		frappe.delete_doc(SETTINGS, self.settings, ignore_permissions=True, force=True)
		with patch(_FEATURES, return_value={}):
			self.assertTrue(pulse_context()["due"])
		self.settings = get_or_create_user_settings(TEST_USER).name
		row = self._pulse()
		self.assertEqual(row.pulse_last_period_key, _pulse_period()[0])
		self.assertEqual(row.pulse_offer_count, 1)

	def test_features_offered_are_the_used_keys_in_canonical_order(self):
		# feature_usage.FEATURES order, not alphabetical: the dialog renders the
		# chips in the order it receives them, and both endpoints agree on it.
		with patch(_FEATURES, return_value={"dashboard_builder": "2026-09-01", "file_box": "2026-09-02"}):
			result = pulse_context()
		self.assertEqual(result["features_offered"], ["file_box", "dashboard_builder"])

	def test_an_answer_during_the_sweep_is_never_overwritten_by_the_offer(self):
		"""The reviewer's repro: pulse_context reads count=1, then the (slow)
		feature sweep runs, and in that window the user answers the survey in
		another tab, which stores PULSE_MAX_OFFERS. A blind ``count + 1`` write
		afterwards dragged the answered marker back down to 2 and re-offered a
		survey already filled in. The offer is a compare-and-set now: it loses,
		reports not due, and the answered marker stays put."""
		current = _pulse_period()[0]
		self._set_pulse(current, 1)

		def answer_in_another_tab(*_args, **_kwargs):
			# Exactly what submit_pulse_feedback writes, landing mid-sweep.
			self._set_pulse(current, PULSE_MAX_OFFERS)
			return {}

		with patch(_FEATURES, side_effect=answer_in_another_tab):
			result = pulse_context()
		self.assertFalse(result["due"], "the offer lost the race and must not fire")
		row = self._pulse()
		self.assertEqual(row.pulse_offer_count, PULSE_MAX_OFFERS, "answered marker was not dragged back down")
		self.assertEqual(row.pulse_last_period_key, current)

	def test_two_chat_opens_racing_burn_exactly_one_offer(self):
		"""Both tabs read count=0 before either writes. Only the first claim sees
		rowcount 1; the second reads the row as already moved and reports not
		due instead of burning a second of the three monthly offers."""
		current = _pulse_period()[0]

		def other_tab_claims_first(*_args, **_kwargs):
			# The concurrent open's claim, landing between this call's read and
			# its own claim: key stamped, count 0 -> 1.
			self._set_pulse(current, 1)
			return {}

		with patch(_FEATURES, side_effect=other_tab_claims_first):
			result = pulse_context()
		self.assertFalse(result["due"])
		self.assertEqual(self._pulse().pulse_offer_count, 1, "one offer burned, not two")

	def test_a_rollover_claim_is_conditional_too(self):
		"""Same race across a period boundary: the stale-key branch must also
		lose to a write that landed first, not stamp over it."""
		self._set_pulse(_STALE_KEY, PULSE_MAX_OFFERS)
		current = _pulse_period()[0]

		def answer_lands_first(*_args, **_kwargs):
			self._set_pulse(current, PULSE_MAX_OFFERS)
			return {}

		with patch(_FEATURES, side_effect=answer_lands_first):
			result = pulse_context()
		self.assertFalse(result["due"])
		self.assertEqual(self._pulse().pulse_offer_count, PULSE_MAX_OFFERS)


class TestSubmitPulseFeedback(_PulseTestCase):
	def _item(self, push):
		push.assert_called_once()
		return push.call_args.args[0]

	def test_forwards_a_derived_payload(self):
		with patch(_PUSH) as push:
			res = submit_pulse_feedback(
				stars=4,
				features_offered=["file_box", "wiki"],
				features_selected=["file_box"],
				use_case_text="  saves us hours a week  ",
				note="  more charts please  ",
			)
		self.assertEqual(res, {"ok": True})
		item = self._item(push)
		self.assertEqual(item["kind"], "Pulse")
		self.assertEqual(item["stars"], 4)
		self.assertEqual(item["period_key"], _pulse_period()[0])
		self.assertEqual(item["features_offered"], ["file_box", "wiki"])
		self.assertEqual(item["features_selected"], ["file_box"])
		self.assertEqual(item["use_case_text"], "saves us hours a week")
		self.assertEqual(item["note"], "more charts please")
		self.assertEqual(item["user_ref"], TEST_USER)

	def test_accepts_json_encoded_lists_from_the_client(self):
		# frappe.client passes list args as JSON strings over HTTP.
		with patch(_PUSH) as push:
			submit_pulse_feedback(stars=5, features_offered='["wiki"]', features_selected='["wiki"]')
		item = self._item(push)
		self.assertEqual(item["features_offered"], ["wiki"])
		self.assertEqual(item["features_selected"], ["wiki"])

	def test_unknown_feature_keys_are_dropped(self):
		with patch(_PUSH) as push:
			submit_pulse_feedback(
				stars=3, features_offered=["wiki", "not_a_feature", 7], features_selected=["nope"]
			)
		item = self._item(push)
		self.assertEqual(item["features_offered"], ["wiki"])
		self.assertEqual(item["features_selected"], [])

	def test_rejects_stars_outside_one_to_five(self):
		# Non-numeric values are rejected too, but by a DIFFERENT layer: under a
		# request or a test run, @frappe.whitelist wraps the function in
		# validate_argument_types, so "abc"/None never reach the body and raise
		# FrappeTypeError (a TypeError) rather than our ValidationError. The
		# body's own int() guard still matters for non-request callers.
		for bad in (0, 6, -1, "abc", None):
			with self.subTest(stars=bad):
				with patch(_PUSH) as push:
					with self.assertRaises((frappe.ValidationError, FrappeTypeError)):
						submit_pulse_feedback(stars=bad, features_offered=[], features_selected=[])
				push.assert_not_called()
				self.assertIsNone(
					self._pulse().pulse_last_period_key, "a rejected submit must not silence the survey"
				)

	def test_accepts_the_whole_one_to_five_range(self):
		for stars in range(1, 6):
			with self.subTest(stars=stars):
				with patch(_PUSH) as push:
					submit_pulse_feedback(stars=stars, features_offered=[], features_selected=[])
				self.assertEqual(self._item(push)["stars"], stars)

	def test_answering_silences_the_survey_for_the_rest_of_the_period(self):
		with patch(_PUSH):
			submit_pulse_feedback(stars=5, features_offered=[], features_selected=[])
		row = self._pulse()
		self.assertEqual(row.pulse_last_period_key, _pulse_period()[0])
		self.assertEqual(row.pulse_offer_count, PULSE_MAX_OFFERS)
		with patch(_FEATURES) as features:
			self.assertFalse(pulse_context()["due"])
		features.assert_not_called()

	def test_answering_does_not_silence_the_next_period(self):
		with patch(_PUSH):
			submit_pulse_feedback(stars=5, features_offered=[], features_selected=[])
		# Simulate the rollover the same way a real month boundary presents it.
		self._set_pulse(_STALE_KEY, PULSE_MAX_OFFERS)
		with patch(_FEATURES, return_value={}):
			self.assertTrue(pulse_context()["due"])

	def test_a_failed_forward_never_surfaces_and_still_silences(self):
		with patch(_PUSH, side_effect=RuntimeError("admin down")):
			res = submit_pulse_feedback(stars=2, features_offered=[], features_selected=[])
		self.assertEqual(res, {"ok": True})
		self.assertEqual(self._pulse().pulse_offer_count, PULSE_MAX_OFFERS)

	def test_free_text_is_bounded(self):
		with patch(_PUSH) as push:
			submit_pulse_feedback(
				stars=1,
				features_offered=[],
				features_selected=[],
				use_case_text="x" * 5000,
				note="y" * 5000,
			)
		item = self._item(push)
		self.assertEqual(len(item["use_case_text"]), 1000)
		self.assertEqual(len(item["note"]), 1000)
