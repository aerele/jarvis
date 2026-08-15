"""Tests for the recurring business-note greeting cadence.

Covers the three guarantees the every-third-new-chat greeting rests on:

- **Single increment** — the counter bumps once per *genuinely-new* chat
  (``create_or_focus_empty``'s create branch), never when the focus path
  returns an existing empty conversation.
- **Cadence** — ``maybe_greet`` shows the card only when the new-chat count
  is a positive multiple of three.
- **Durability** — a permanent dismissal (and the counter) live in the
  ``Jarvis User Settings`` DB row, so ``frappe.clear_cache`` can neither
  resurrect a killed greeting nor reset the cadence.

Runs as a dedicated System User so ``_require_system_user`` and
``_voice_features_enabled`` (operator toggle, forced ON here) both pass.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import create_or_focus_empty
from jarvis.chat.greeting import dismiss_greeting, maybe_greet

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
PREF = "Jarvis User Settings"
NOTE = "Jarvis Voice Note"
SETTINGS = "Jarvis Settings"
TEST_USER = "jarvis-greeting-test@example.com"


def _ensure_test_user(user: str = TEST_USER) -> None:
	"""Create the fixture System User if missing; idempotent."""
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Jarvis",
			"last_name": "Greeting",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager")
	frappe.db.commit()


def _cleanup(user: str = TEST_USER) -> None:
	"""Drop every disposable row this suite might have written for `user`:
	conversations (+ their messages), the greeting preference row, and any
	voice notes."""
	for name in frappe.get_all(CONV, filters={"owner": user}, pluck="name"):
		for child in frappe.get_all(MSG, filters={"conversation": name}, pluck="name"):
			frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
		frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
	if frappe.db.exists(PREF, user):
		frappe.delete_doc(PREF, user, ignore_permissions=True, force=True)
	for note in frappe.get_all(NOTE, filters={"owner": user}, pluck="name"):
		frappe.delete_doc(NOTE, note, ignore_permissions=True, force=True)
	frappe.db.commit()


class _GreetingTestCase(FrappeTestCase):
	"""Run as the fixture System User with voice features forced ON, and
	restore the original session user + operator toggle on teardown."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Force the operator toggle ON so _voice_features_enabled() never
		# suppresses the card independently of the cadence under test.
		settings = frappe.get_single(SETTINGS)
		cls._vf_snap = settings.voice_features_enabled
		settings.db_set("voice_features_enabled", 1, update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single(SETTINGS)
		settings.db_set("voice_features_enabled", cls._vf_snap, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup()

	def tearDown(self):
		_cleanup()
		frappe.set_user(self._orig_user)

	def _set_count(self, count: int) -> None:
		"""Ensure a settings row exists for the fixture user and pin its
		new-chat counter to `count`."""
		from jarvis.chat.usage import get_or_create_user_settings

		get_or_create_user_settings(TEST_USER)
		frappe.db.set_value(PREF, {"user": TEST_USER}, "business_greeting_chat_count", count)

	def _count(self) -> int:
		return frappe.utils.cint(frappe.db.get_value(PREF, TEST_USER, "business_greeting_chat_count"))


class TestIncrement(_GreetingTestCase):
	def test_increment_only_on_genuine_create(self):
		"""The counter bumps on a genuinely-new chat, but the focus path
		(returning the just-created empty conversation) must NOT increment."""
		# No empty conversation exists -> create branch runs -> count == 1.
		first = create_or_focus_empty()
		self.assertTrue(frappe.db.exists(CONV, first))
		self.assertEqual(self._count(), 1)

		# The conversation just made is still empty, so this call FOCUSES it
		# rather than creating a new one: same name back, count unchanged.
		second = create_or_focus_empty()
		self.assertEqual(second, first)
		self.assertEqual(self._count(), 1)
		# And no second conversation leaked from the focus path.
		self.assertEqual(len(frappe.get_all(CONV, filters={"owner": TEST_USER})), 1)

	def test_counter_failure_never_breaks_chat_creation(self):
		"""A raising increment_new_chat_count is swallowed: chat creation
		must still return a real conversation."""
		with patch(
			"jarvis.chat.greeting.increment_new_chat_count",
			side_effect=RuntimeError("boom"),
		):
			name = create_or_focus_empty()
		self.assertTrue(name)
		self.assertTrue(frappe.db.exists(CONV, name))


class TestCadence(_GreetingTestCase):
	def test_cadence_multiples_of_three(self):
		"""The card shows only on positive multiples of three."""
		for count, expected in ((1, False), (2, False), (3, True), (4, False), (6, True)):
			self._set_count(count)
			self.assertEqual(
				maybe_greet()["show_card"],
				expected,
				f"count={count} should give show_card={expected}",
			)

	def test_zero_count_no_card(self):
		"""A fresh user with no preference row (count 0) never sees the card:
		zero is a multiple of three but not a *positive* one."""
		self.assertFalse(frappe.db.exists(PREF, TEST_USER))
		self.assertFalse(maybe_greet()["show_card"])


class TestDurabilityAndSuppression(_GreetingTestCase):
	def test_dismiss_is_permanent_and_survives_cache_clear(self):
		"""Dismissed lives in the DB row, so a cache flush can't resurrect the
		greeting even when the count is a multiple of three."""
		dismiss_greeting()
		self._set_count(6)
		frappe.clear_cache()
		# clear_cache may reset frappe.local; re-establish the session identity
		# (the point under test is DB durability, not who the caller is).
		frappe.set_user(TEST_USER)
		self.assertFalse(maybe_greet()["show_card"])

	def test_business_note_suppresses(self):
		"""Any Business voice note the user already wrote stops the greeting,
		even on a multiple-of-three chat."""
		frappe.get_doc(
			{
				"doctype": NOTE,
				"transcript": "We are a bakery in Chennai; wholesale to cafes.",
				"context_type": "Business",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self._set_count(3)
		self.assertFalse(maybe_greet()["show_card"])
