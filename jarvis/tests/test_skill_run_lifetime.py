"""How long an approved skill run ("Approve & run") stays open.

A run lasts until the reply ends, a new message, a failed write or Halt (covered in
``test_skill_approve_and_run``). This module covers what bounds it in between:

- waiting on one of the run's cards never costs the run: confirming the card moves
  the run's activity stamp to now;
- a run with no activity for ``_SKILL_AUTORUN_IDLE_S`` is not driven by anything (a
  worker died), so its next write parks;
- a card of the run that fails, a write that raises, an archive and a retry end it.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import actions_api, pending_confirm, turn_message_binding
from jarvis.chat import api as chat_api
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _cleanup_user_conversations, _ensure_test_user
from jarvis.tests.test_skill_approve_and_run import _park_pending_card, _stamp_autorun

PING = ("run_method", {"method": "frappe.ping"})
CARD_PATHS = ("pending action", "legacy token")


def _ago(seconds: int):
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-seconds)


def _run(conv: str):
	return frappe.db.get_value(CONV, conv, ["skill_autorun", "skill_autorun_at"], as_dict=True)


def _age_s(stamp) -> float:
	return (frappe.utils.now_datetime() - frappe.utils.get_datetime(stamp)).total_seconds()


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		self._sweep()

	def tearDown(self):
		frappe.set_user(self._orig)
		self._sweep()

	@staticmethod
	def _sweep():
		frappe.db.rollback()
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			turn_message_binding.clear_run_cancel(conv)
		_cleanup_user_conversations(TEST_USER)
		for name in frappe.get_all("ToDo", filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _open_run(self, *, idle_s: int = 0) -> str:
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv, at=_ago(idle_s))
		frappe.db.commit()
		return conv

	def _confirm_a_card(self, conv: str, path: str, result: dict) -> dict:
		"""Park a delete card on ``conv`` and confirm it, the write itself answered by
		``result``. ``path`` picks how the card is stored: a pending action (today), or
		a legacy token (a site before the pending-action table)."""
		todo = frappe.get_doc({"doctype": "ToDo", "description": "lifetime-card"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		with (
			patch("jarvis.chat.pending_confirm._pa_ready", return_value=path == "pending action"),
			patch("jarvis.api.dispatch_confirmed", return_value=result),
			patch("jarvis.api.persist_tool_receipt"),
			patch("jarvis.chat.admission.publish_action_confirmed"),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			token = _park_pending_card(conv, TEST_USER, todo.name)
			self.assertEqual(pending_confirm.is_pending_action(token), path == "pending action")
			return actions_api._confirm_core(token, conv)

	def _covered_write_runs(self, conv: str) -> bool:
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
			api._run_tool(*PING, conversation=conv)
		return disp.called


class TestTheNoActivityNet(_Base):
	def test_a_run_idle_for_twenty_minutes_is_still_open(self):
		conv = self._open_run(idle_s=20 * 60)
		self.assertTrue(self._covered_write_runs(conv))

	def test_a_run_idle_past_the_net_parks_its_next_write(self):
		conv = self._open_run(idle_s=api._SKILL_AUTORUN_IDLE_S + 60)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			res = api._run_tool(*PING, conversation=conv)
		self.assertFalse(disp.called)
		self.assertEqual(res["data"]["status"], "pending_confirmation")

	def test_the_net_is_half_an_hour(self):
		self.assertEqual(api._SKILL_AUTORUN_IDLE_S, 1800)

	def test_a_run_nothing_is_driving_is_ended_where_it_parks(self):
		# Otherwise the card this write parks would hide the run from the reaper, and
		# confirming that card would bring the run back.
		conv = self._open_run(idle_s=api._SKILL_AUTORUN_IDLE_S + 60)
		with patch("jarvis.api.dispatch_confirmed"):
			res = api._run_tool(*PING, conversation=conv)
		self.assertEqual(res["data"]["status"], "pending_confirmation")
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)
		(card,) = pending_confirm.list_for_owner(TEST_USER, conversation=conv)
		with (
			patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}),
			patch("jarvis.api.persist_tool_receipt"),
			patch("jarvis.chat.admission.publish_action_confirmed"),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			actions_api._confirm_core(card["token"], conv)
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)
		self.assertFalse(self._covered_write_runs(conv))

	def test_a_run_paused_on_a_card_is_kept_however_long_it_waits(self):
		conv = self._open_run(idle_s=3 * 3600)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "lifetime-paused"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		_park_pending_card(conv, TEST_USER, todo.name)
		# A write that arrives while that card waits is refused or parked, never run,
		# and the run is still there for the card's Confirm.
		with patch("jarvis.api.dispatch_confirmed") as disp:
			api._run_tool(*PING, conversation=conv)
		self.assertFalse(disp.called)
		self.assertEqual(int(_run(conv).skill_autorun), 1)


class TestACardWaitDoesNotCostTheRun(_Base):
	def test_confirming_a_card_moves_the_stamp(self):
		conv = self._open_run(idle_s=3 * 3600)
		turn_message_binding.keep_skill_autorun_open(conv)
		run = _run(conv)
		self.assertEqual(int(run.skill_autorun), 1)
		self.assertLess(_age_s(run.skill_autorun_at), 60)

	def test_only_an_open_run_with_a_stamp_is_touched(self):
		closed = _make_conv(TEST_USER)
		turn_message_binding.keep_skill_autorun_open(closed)
		self.assertIsNone(_run(closed).skill_autorun_at)
		# A run with no stamp is malformed: the gate parks it, and it stays that way.
		malformed = self._open_run()
		frappe.db.set_value(CONV, malformed, "skill_autorun_at", None, update_modified=False)
		turn_message_binding.keep_skill_autorun_open(malformed)
		self.assertIsNone(_run(malformed).skill_autorun_at)
		turn_message_binding.keep_skill_autorun_open(None)  # no conversation: no error

	def test_a_run_resumes_after_a_three_hour_wait_on_its_card(self):
		for path in CARD_PATHS:
			with self.subTest(card=path):
				conv = self._open_run(idle_s=3 * 3600)
				# Before the click the run is idle past the net: a write would park.
				self.assertGreater(_age_s(_run(conv).skill_autorun_at), api._SKILL_AUTORUN_IDLE_S)
				res = self._confirm_a_card(conv, path, {"ok": True, "data": {}})
				self.assertTrue(res.get("ok"), res)
				run = _run(conv)
				self.assertEqual(int(run.skill_autorun), 1)
				self.assertLess(_age_s(run.skill_autorun_at), 60)
				# The run's next write goes through without a card.
				self.assertTrue(self._covered_write_runs(conv))


class TestWhatEndsARun(_Base):
	def test_a_card_of_the_run_that_fails(self):
		failed = {"ok": False, "error": {"code": "ValidationError", "message": "no"}}
		for path in CARD_PATHS:
			with self.subTest(card=path):
				conv = self._open_run()
				self._confirm_a_card(conv, path, failed)
				self.assertEqual(int(_run(conv).skill_autorun or 0), 0)

	def test_a_write_that_raises(self):
		conv = self._open_run()
		with patch("jarvis.api._run_covered_write", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				api._run_tool(*PING, conversation=conv)
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)

	def test_archiving_the_chat(self):
		conv = self._open_run()
		chat_api.archive_conversation(conv)
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)

	def test_a_card_that_fails_before_it_runs(self):
		# The record the card would delete is gone by the time it is confirmed.
		conv = self._open_run()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "lifetime-gone"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		token = _park_pending_card(conv, TEST_USER, todo.name)
		self.assertTrue(pending_confirm.is_pending_action(token))
		frappe.delete_doc("ToDo", todo.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		with (
			patch("jarvis.api.dispatch_confirmed") as disp,
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			res = actions_api._confirm_core(token, conv)
		self.assertFalse(disp.called, res)
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)

	def test_archiving_keeps_a_halt_signal_that_was_standing(self):
		conv = self._open_run()
		turn_message_binding.request_run_cancel(conv)
		turn_message_binding.end_skill_autorun_if_open(conv, "archived", keep_halt=True)
		self.assertEqual(int(_run(conv).skill_autorun or 0), 0)
		self.assertTrue(turn_message_binding.is_run_cancel_requested(conv))

	def test_ending_a_run_that_is_not_open_writes_nothing(self):
		conv = _make_conv(TEST_USER)
		with patch.object(turn_message_binding, "clear_skill_autorun") as clear:
			turn_message_binding.end_skill_autorun_if_open(conv, "retry")
			turn_message_binding.end_skill_autorun_if_open(None, "retry")
		clear.assert_not_called()


class TestOnlyTheServerWritesTheRunFields(_Base):
	def test_a_save_carries_what_is_stored(self):
		conv = self._open_run(idle_s=40 * 60)
		stored = frappe.db.get_value(CONV, conv, ["skill_autorun_at", "skill_autorun_skill"], as_dict=True)
		doc = frappe.get_doc(CONV, conv)
		doc.skill_autorun_at = frappe.utils.now_datetime()
		doc.skill_autorun_skill = "some-other-skill"
		doc.title = "renamed"
		doc.save()
		after = frappe.db.get_value(
			CONV, conv, ["skill_autorun_at", "skill_autorun_skill", "title"], as_dict=True
		)
		self.assertEqual(after.skill_autorun_at, stored.skill_autorun_at)
		self.assertEqual(after.skill_autorun_skill, stored.skill_autorun_skill)
		self.assertEqual(after.title, "renamed")

	def test_a_document_loaded_before_the_server_wrote_them_saves_cleanly(self):
		conv = _make_conv(TEST_USER)
		doc = frappe.get_doc(CONV, conv)  # loaded with no run
		# The server stamps the fields meanwhile, without touching ``modified`` (as its
		# raw writes do), so this stale document still saves.
		frappe.db.set_value(
			CONV,
			conv,
			{"skill_autorun_at": _ago(60), "skill_autorun_skill": "stamped-meanwhile"},
			update_modified=False,
		)
		frappe.db.commit()
		stored = frappe.db.get_value(CONV, conv, ["skill_autorun_at", "skill_autorun_skill"], as_dict=True)
		self.assertIsNone(doc.skill_autorun_at)
		doc.title = "stale"
		doc.save()
		after = frappe.db.get_value(CONV, conv, ["skill_autorun_at", "skill_autorun_skill"], as_dict=True)
		self.assertEqual(after.skill_autorun_at, stored.skill_autorun_at)
		self.assertEqual(after.skill_autorun_skill, stored.skill_autorun_skill)

	def test_a_new_conversation_cannot_be_born_with_them(self):
		doc = frappe.get_doc(
			{
				"doctype": CONV,
				"title": "born",
				"skill_autorun_at": frappe.utils.now_datetime(),
				"skill_autorun_skill": "x",
			}
		).insert()
		self.assertIsNone(doc.skill_autorun_at)
		self.assertIsNone(doc.skill_autorun_skill)
