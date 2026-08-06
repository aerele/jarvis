"""New-chat prompt suggestions synthesised from the user's own chat titles.

Three things have to hold, and each has a test here:

1. The MATERIAL is clean. The model must never be fed the noise a real sidebar is
   full of — still-unnamed chats, greeting chats, one-off questions, and the same
   title five times over — or the suggestions describe nothing the user does.
2. The REFRESH is gated on activity, not just a clock, so an idle workspace costs
   zero model calls and an active one is capped.
3. The ENDPOINT is a cheap read: it returns the cache and never generates inline,
   and it cannot break the empty screen no matter what the queue does.

The gateway turn itself is never exercised — that path is
jarvis.chat.title._generate_via_gateway's, already covered there, and a test that
opened a real agent session would be a live-container test.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import suggestions, user_settings_api

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
USETT = "Jarvis User Settings"

USER = "sugg-user@example.test"
OTHER = "sugg-other@example.test"


def _ensure_user(email: str) -> None:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Sugg",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles("Jarvis User")


def _conv(owner: str, title: str, *, messages: int = 4, days_ago: int = 1) -> str:
	"""A conversation owned by `owner`, with `messages` real messages on it."""
	when = frappe.utils.add_days(frappe.utils.now_datetime(), -days_ago)
	doc = frappe.get_doc({"doctype": CONV, "title": title, "status": "Active"}).insert(
		ignore_permissions=True
	)
	frappe.db.set_value(CONV, doc.name, {"owner": owner, "last_active_at": when}, update_modified=False)
	for i in range(messages):
		m = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": doc.name,
				"seq": i + 1,
				"role": "user" if i % 2 == 0 else "assistant",
				"content": "x",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(MSG, m.name, "owner", owner, update_modified=False)
	return doc.name


class SuggestionsBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER)
		_ensure_user(OTHER)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		for u in (USER, OTHER):
			for name in frappe.get_all(CONV, filters={"owner": u}, pluck="name"):
				frappe.db.delete(MSG, {"conversation": name})
				frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
			for name in frappe.get_all(USETT, filters={"user": u}, pluck="name"):
				frappe.delete_doc(USETT, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


# --------------------------------------------------------------------------- #
# 1. Material
# --------------------------------------------------------------------------- #
class TestEligibleTitles(SuggestionsBase):
	def test_keeps_substantive_titles_newest_first(self):
		_conv(USER, "Sales Invoice Submission Alert", days_ago=5)
		_conv(USER, "Timesheet Report Date Range", days_ago=1)
		self.assertEqual(
			suggestions.eligible_titles(USER),
			["Timesheet Report Date Range", "Sales Invoice Submission Alert"],
		)

	def test_drops_unnamed_greeting_and_one_word_titles(self):
		_conv(USER, "New chat")
		_conv(USER, "hello")
		_conv(USER, "Greeting")
		_conv(USER, "Purchase Order Approval Flow")
		self.assertEqual(suggestions.eligible_titles(USER), ["Purchase Order Approval Flow"])

	def test_drops_one_off_conversations(self):
		# "Simple Addition Answer" style: a titled chat that was a single question.
		_conv(USER, "Simple Addition Answer", messages=2)
		_conv(USER, "Stock Reconciliation Draft", messages=4)
		self.assertEqual(suggestions.eligible_titles(USER), ["Stock Reconciliation Draft"])

	def test_tool_rows_do_not_make_a_one_off_look_substantive(self):
		# Tool rows outnumber prose in a real conversation, so counting them let a
		# single question that happened to call two tools pass the MIN_MESSAGES bar.
		name = _conv(USER, "One Question With Tools", messages=2)
		for i in range(6):
			m = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": name,
					"seq": 100 + i,
					"role": "tool",
					"content": "get_doc -> completed",
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value(MSG, m.name, "owner", USER, update_modified=False)
		frappe.db.commit()
		self.assertEqual(suggestions.eligible_titles(USER), [])

	def test_dedupes_repeated_titles(self):
		# A sidebar with five "System Behaviour Overview" must not spend five of the
		# 25 prompt slots saying the same thing.
		for d in range(1, 6):
			_conv(USER, "System Behaviour Overview", days_ago=d)
		_conv(USER, "Lead Conversion Report", days_ago=6)
		self.assertEqual(
			suggestions.eligible_titles(USER),
			["System Behaviour Overview", "Lead Conversion Report"],
		)

	def test_ignores_conversations_outside_the_window(self):
		_conv(USER, "Ancient Migration Notes", days_ago=suggestions.LOOKBACK_DAYS + 5)
		_conv(USER, "Recent Payroll Question", days_ago=2)
		self.assertEqual(suggestions.eligible_titles(USER), ["Recent Payroll Question"])

	def test_never_leaks_another_users_titles(self):
		_conv(OTHER, "Other Persons Secret Project")
		_conv(USER, "My Own Report")
		self.assertEqual(suggestions.eligible_titles(USER), ["My Own Report"])

	def test_caps_the_number_of_titles(self):
		for i in range(suggestions.MAX_TITLES + 6):
			_conv(USER, f"Distinct Report Number {i}", days_ago=1)
		self.assertEqual(len(suggestions.eligible_titles(USER)), suggestions.MAX_TITLES)

	def test_empty_for_a_user_with_no_history(self):
		self.assertEqual(suggestions.eligible_titles(USER), [])


# --------------------------------------------------------------------------- #
# 2. Refresh policy
# --------------------------------------------------------------------------- #
class TestRefreshGate(SuggestionsBase):
	def _stamp(self, days_ago: float, lines=({"title": "A", "prompt": "a b"},)) -> None:
		suggestions.store(USER, list(lines))
		frappe.db.set_value(
			USETT,
			{"user": USER},
			"prompt_suggestions_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days_ago),
			update_modified=False,
		)
		frappe.db.commit()

	def test_refreshes_when_never_synthesised(self):
		self.assertTrue(suggestions.needs_refresh(USER))

	def test_does_not_refresh_while_fresh(self):
		self._stamp(days_ago=1)
		_conv(USER, "Brand New Work", days_ago=0)
		self.assertFalse(suggestions.needs_refresh(USER), "a fresh cache must not be regenerated")

	def test_idle_user_never_costs_a_call(self):
		# Stale by the clock, but the user has not chatted since - the whole point
		# of the activity gate.
		_conv(USER, "Old Work", days_ago=40)
		self._stamp(days_ago=suggestions.REFRESH_AFTER_DAYS + 10)
		self.assertFalse(suggestions.needs_refresh(USER))

	def test_refreshes_when_stale_and_active(self):
		self._stamp(days_ago=suggestions.REFRESH_AFTER_DAYS + 1)
		_conv(USER, "Work Done Since The Stamp", days_ago=0)
		self.assertTrue(suggestions.needs_refresh(USER))

	def test_an_attempt_that_found_nothing_still_backs_off(self):
		# The gate reads the STAMP, not the stored value. Without this, a workspace
		# with no named history would queue a job on every empty-screen load.
		suggestions.touch(USER)
		self.assertEqual(suggestions.read(USER), [])
		self.assertFalse(suggestions.needs_refresh(USER))

	def test_a_user_with_no_history_is_stamped_not_retried(self):
		with patch.object(suggestions, "_generate_via_gateway") as gen:
			suggestions.refresh_job(USER)
		gen.assert_not_called()  # nothing to synthesise from
		self.assertFalse(suggestions.needs_refresh(USER), "must back off, not retry every load")

	def test_a_failed_generation_keeps_the_previous_strip(self):
		# A transient gateway blip must not blank a good strip - it stamps (so the
		# gateway is not hammered) and leaves the old suggestions in place.
		suggestions.store(USER, [{"title": "Invoices", "prompt": "Show overdue invoices"}])
		frappe.db.set_value(
			USETT,
			{"user": USER},
			"prompt_suggestions_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-(suggestions.REFRESH_AFTER_DAYS + 1)),
			update_modified=False,
		)
		_conv(USER, "Fresh Work Since Then", days_ago=0)
		frappe.db.commit()
		with patch.object(suggestions, "_generate_via_gateway", return_value=[]):
			suggestions.refresh_job(USER)
		self.assertEqual(suggestions.read(USER), [{"title": "Invoices", "prompt": "Show overdue invoices"}])
		self.assertFalse(suggestions.needs_refresh(USER))

	def test_enqueue_is_a_no_op_when_not_needed(self):
		self._stamp(days_ago=0)
		with patch("frappe.enqueue") as mock_enqueue:
			suggestions.enqueue_refresh(USER)
		mock_enqueue.assert_not_called()

	def test_enqueue_queues_when_needed(self):
		with patch("frappe.enqueue") as mock_enqueue:
			suggestions.enqueue_refresh(USER)
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs.get("queue"), "short")


# --------------------------------------------------------------------------- #
# 3. Parsing, storage, endpoint
# --------------------------------------------------------------------------- #
class TestParseAndStore(SuggestionsBase):
	def test_parses_label_and_prompt_pairs(self):
		out = suggestions.parse_lines(
			"Invoices | Show overdue invoices\n"
			"- Payroll | Draft a payroll summary\n"
			'1. Stock | "Reconcile stock counts"'
		)
		self.assertEqual(
			out,
			[
				{"title": "Invoices", "prompt": "Show overdue invoices"},
				{"title": "Payroll", "prompt": "Draft a payroll summary"},
				{"title": "Stock", "prompt": "Reconcile stock counts"},
			],
		)

	def test_keeps_a_line_that_forgot_its_label(self):
		# A missing label must not cost us a usable suggestion - the card just
		# renders the prompt on its own.
		self.assertEqual(
			suggestions.parse_lines("Show my open leads"),
			[{"title": "", "prompt": "Show my open leads"}],
		)

	def test_drops_blank_and_single_word_lines(self):
		self.assertEqual(
			suggestions.parse_lines("\n\nOk\nShow my open leads\n"),
			[{"title": "", "prompt": "Show my open leads"}],
		)

	def test_caps_the_count(self):
		many = "\n".join(f"Area {i} | Do the thing number {i}" for i in range(10))
		self.assertEqual(len(suggestions.parse_lines(many)), suggestions.WANT)

	def test_tolerates_junk(self):
		self.assertEqual(suggestions.parse_lines(None), [])
		self.assertEqual(suggestions.parse_lines(""), [])

	def test_store_and_read_round_trip(self):
		items = [{"title": "Invoices", "prompt": "Show overdue invoices"}]
		suggestions.store(USER, items)
		self.assertEqual(suggestions.read(USER), items)

	def test_read_upgrades_a_legacy_plain_string_cache(self):
		# A cache written by the first version of this feature must still render
		# rather than blanking the strip until the next refresh.
		suggestions.touch(USER)  # ensure the settings row exists to overwrite
		frappe.db.set_value(
			USETT,
			{"user": USER},
			"prompt_suggestions",
			json.dumps(["Show overdue invoices"]),
			update_modified=False,
		)
		frappe.db.commit()
		self.assertEqual(suggestions.read(USER), [{"title": "", "prompt": "Show overdue invoices"}])

	def test_read_is_empty_and_quiet_for_a_fresh_user(self):
		self.assertEqual(suggestions.read(USER), [])

	def test_read_survives_a_corrupt_cache(self):
		suggestions.store(USER, ["ok line"])
		frappe.db.set_value(USETT, {"user": USER}, "prompt_suggestions", "{not json", update_modified=False)
		frappe.db.commit()
		self.assertEqual(suggestions.read(USER), [])


class TestEndpoint(SuggestionsBase):
	def test_returns_the_cache_and_never_generates_inline(self):
		suggestions.store(USER, [{"title": "Invoices", "prompt": "Show overdue invoices"}])
		frappe.set_user(USER)
		with patch.object(suggestions, "_generate_via_gateway") as gen, patch("frappe.enqueue"):
			out = user_settings_api.get_prompt_suggestions()
		frappe.set_user("Administrator")
		self.assertTrue(out["ok"])
		self.assertEqual(
			out["data"]["suggestions"], [{"title": "Invoices", "prompt": "Show overdue invoices"}]
		)
		gen.assert_not_called()

	def test_survives_a_broken_queue(self):
		# A queue outage must not take the empty chat screen down with it.
		frappe.set_user(USER)
		with patch("frappe.enqueue", side_effect=RuntimeError("redis down")):
			out = user_settings_api.get_prompt_suggestions()
		frappe.set_user("Administrator")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["suggestions"], [])
