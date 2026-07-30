"""Tests for jarvis.chat.turn_handler.handle_chat_send.

Phase 1 of the chat-bridge refactor extracts the turn body out of
``jarvis.chat.worker.run_agent_turn`` into
``jarvis.chat.turn_handler.handle_chat_send``. The worker function is
preserved as a thin shim that builds the payload dict and calls
``handle_chat_send``. The behavioural coverage is owned by
``test_chat_worker.py`` (which must keep passing unchanged); these
tests pin only the payload-mapping contract between the shim and the
handler so a future refactor cannot silently change the payload shape.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_session_pool, turn_handler, worker
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)
from jarvis.tests.test_chat_worker import (
	_fake_event_stream,
	_make_conversation_with_user_message,
)

MSG = "Jarvis Chat Message"


class TestHandleChatSendAcceptsPayloadDict(FrappeTestCase):
	"""``handle_chat_send`` is the new entry point. It must accept a
	payload dict with conversation_id/message_id/run_id and drive a
	turn end-to-end with the same effect as the RQ shim.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hello")

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_payload_dict_drives_a_full_turn(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "ok", "delta": "ok"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch(
			"jarvis.chat.agent_session_pool.OpenclawSession.connect",
			return_value=fake_sess,
		):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				turn_handler.handle_chat_send(
					{
						"conversation_id": self.conv,
						"message_id": self.user_msg,
						"run_id": "r-payload",
					}
				)

		# The assistant placeholder was created, content was persisted,
		# streaming flipped off. (Behavioural depth lives in
		# test_chat_worker; we only need a smoke here.)
		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "assistant"},
			fields=["content", "streaming"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["content"], "ok")
		self.assertEqual(rows[0]["streaming"], 0)

		# run:start ... run:end bracket was published via the worker-
		# module indirection, confirming the patch path still wins for
		# code that now lives in turn_handler.
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("run:start", kinds)
		self.assertIn("run:end", kinds)

	def test_attachments_and_context_default_to_none(self):
		"""The payload omits ``attachments`` and ``context``; the handler
		must treat them as None and not blow up on missing keys."""
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch(
			"jarvis.chat.agent_session_pool.OpenclawSession.connect",
			return_value=fake_sess,
		):
			with patch("jarvis.chat.worker.publish_to_user"):
				turn_handler.handle_chat_send(
					{
						"conversation_id": self.conv,
						"message_id": self.user_msg,
						"run_id": "r-optional",
					}
				)
		# Reached this line without KeyError: the payload contract for
		# the optional fields holds.
		self.assertTrue(True)


class TestRunAgentTurnShimForwardsToHandleChatSend(FrappeTestCase):
	"""The RQ entry point ``worker.run_agent_turn`` is now a thin shim
	that constructs the payload dict and calls ``handle_chat_send``.
	Pin the payload shape so a future refactor can't drop a field.
	"""

	def test_shim_builds_expected_payload(self):
		with patch("jarvis.chat.worker.handle_chat_send") as fake:
			worker.run_agent_turn(
				"conv-1",
				"msg-1",
				"run-1",
				attachments=[{"file_url": "/private/files/x.txt", "file_name": "x.txt"}],
				context={"doctype": "Sales Invoice", "name": "SINV-0001"},
			)

		fake.assert_called_once()
		(payload,), _ = fake.call_args
		self.assertEqual(payload["conversation_id"], "conv-1")
		self.assertEqual(payload["message_id"], "msg-1")
		self.assertEqual(payload["run_id"], "run-1")
		self.assertEqual(
			payload["attachments"],
			[{"file_url": "/private/files/x.txt", "file_name": "x.txt"}],
		)
		self.assertEqual(
			payload["context"],
			{"doctype": "Sales Invoice", "name": "SINV-0001"},
		)

	def test_shim_defaults_attachments_and_context_to_none(self):
		with patch("jarvis.chat.worker.handle_chat_send") as fake:
			worker.run_agent_turn("conv-2", "msg-2", "run-2")

		fake.assert_called_once()
		(payload,), _ = fake.call_args
		self.assertIsNone(payload["attachments"])
		self.assertIsNone(payload["context"])


class TestOrgLocaleClause(unittest.TestCase):
	"""_org_locale_clause folds the default Company's region + the site's
	date / number / timezone formats into the context line so the agent
	stops defaulting to US conventions; any read failure degrades to ''."""

	def _run(self, *, company, cached=None, singles=None, default=None, fiscal=None):
		cached = cached or {}
		singles = singles or {}
		# get_fiscal_year is patched in ALL cases so the base locale tests stay
		# deterministic on sites that carry a real Fiscal Year; pass ``fiscal``
		# (a dict) to opt into a mocked FY, default raises -> no fy clause.
		fy_kwargs = {"return_value": fiscal} if fiscal is not None else {"side_effect": RuntimeError("no fy")}
		with (
			patch("frappe.defaults.get_global_default", return_value=company),
			patch("frappe.get_cached_value", side_effect=lambda dt, name, field: cached.get(field, "")),
			patch("frappe.db.get_single_value", side_effect=lambda dt, field: singles.get(field, "")),
			patch("frappe.db.get_default", return_value=default),
			patch("erpnext.accounts.utils.get_fiscal_year", **fy_kwargs),
		):
			return turn_handler._org_locale_clause()

	def test_full_company_locale(self):
		clause = self._run(
			company="Acme Ltd",
			cached={"country": "India", "default_currency": "INR"},
			singles={
				"date_format": "dd-mm-yyyy",
				"number_format": "#,##,###.##",
				"time_zone": "Asia/Kolkata",
			},
		)
		self.assertTrue(clause.startswith("; "))
		self.assertIn("org: Acme Ltd (India, INR)", clause)
		self.assertIn("dates dd-mm-yyyy", clause)
		self.assertIn("numbers #,##,###.##", clause)
		self.assertIn("tz Asia/Kolkata", clause)

	def test_no_company_falls_back_to_region(self):
		clause = self._run(
			company=None,
			singles={"country": "Germany", "date_format": "dd.mm.yyyy"},
			default="EUR",
		)
		self.assertIn("region: Germany, EUR", clause)
		self.assertNotIn("org:", clause)
		self.assertIn("dates dd.mm.yyyy", clause)

	def test_long_company_name_is_capped(self):
		clause = self._run(
			company="Globex Trading Company Limited w.e.f 01/04/2024 (Formerly Globex Trading Private Limited)",
			cached={"country": "India", "default_currency": "INR"},
		)
		self.assertIn("org: Globex Trading Company", clause)
		self.assertIn("...", clause)  # truncation marker
		self.assertNotIn("Private Limited", clause)  # long tail dropped
		self.assertIn("(India, INR)", clause)

	def test_fiscal_year_folded_in(self):
		"""The current fiscal year rides the locale clause so accounting
		turns don't spend a provider round trip on jarvis__get_fiscal_year."""
		clause = self._run(
			company="Acme Ltd",
			cached={"country": "India", "default_currency": "INR"},
			fiscal={"name": "2026-2027", "year_start_date": "2026-04-01", "year_end_date": "2027-03-31"},
		)
		self.assertIn("fy 2026-2027 (2026-04-01..2027-03-31)", clause)

	def test_fiscal_year_failure_degrades_silently(self):
		"""No Fiscal Year record (or no ERPNext) must not break the clause."""
		clause = self._run(
			company="Acme Ltd",
			cached={"country": "India", "default_currency": "INR"},
		)
		self.assertIn("org: Acme Ltd (India, INR)", clause)
		self.assertNotIn("fy ", clause)

	def test_read_failure_yields_empty_clause(self):
		with patch("frappe.defaults.get_global_default", side_effect=RuntimeError("db down")):
			self.assertEqual(turn_handler._org_locale_clause(), "")

	def test_empty_site_yields_empty_clause(self):
		self.assertEqual(self._run(company=None, default=None), "")
