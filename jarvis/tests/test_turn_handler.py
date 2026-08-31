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
from jarvis.exceptions import AgentUnreachableError
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
			"jarvis.chat.agent_session_pool.AgentSession.connect",
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
			"jarvis.chat.agent_session_pool.AgentSession.connect",
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

	def test_stale_device_binding_remints_the_session(self):
		"""jarvis #712: a conversation's session_key was minted under a
		device pairing that no longer exists (a tenant re-pair happened
		after this conversation's first turn). Reusing it would leave
		every tool call on this conversation 401ing forever (jarvis.api's
		guard deliberately never self-heals). handle_chat_send must treat
		it like "no session yet" and mint a fresh one under the CURRENT
		pairing instead."""
		SESSION = "Jarvis Chat Session"
		old_key = frappe.db.get_value("Jarvis Conversation", self.conv, "session_key")
		frappe.get_doc(
			{
				"doctype": SESSION,
				"session_key": old_key,
				"user": TEST_USER,
				"chat_device_id": "old-device-before-repair",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		settings = frappe.get_single("Jarvis Settings")
		original_device_id = settings.chat_device_id
		settings.db_set("chat_device_id", "new-device-after-repair")
		frappe.db.commit()

		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "agent:freshly-minted"
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		try:
			with patch(
				"jarvis.chat.agent_session_pool.AgentSession.connect",
				return_value=fake_sess,
			):
				with patch("jarvis.chat.worker.publish_to_user"):
					turn_handler.handle_chat_send(
						{
							"conversation_id": self.conv,
							"message_id": self.user_msg,
							"run_id": "r-stale-repair",
						}
					)

			fake_sess.create_session.assert_called_once()
			new_key = frappe.db.get_value("Jarvis Conversation", self.conv, "session_key")
			self.assertEqual(new_key, "agent:freshly-minted")
			self.assertNotEqual(new_key, old_key)
			new_row_device = frappe.db.get_value(SESSION, {"session_key": new_key}, "chat_device_id")
			self.assertEqual(new_row_device, "new-device-after-repair")
		finally:
			settings.db_set("chat_device_id", original_device_id)
			frappe.db.commit()
			frappe.db.delete(SESSION, {"session_key": old_key})
			frappe.db.delete(SESSION, {"session_key": "agent:freshly-minted"})
			frappe.db.commit()


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


class TestServerScriptsClause(unittest.TestCase):
	"""_server_scripts_clause: gated to report/script/console turns; on/off from
	is_safe_exec_enabled, '' otherwise / on failure."""

	def test_enabled_yields_on(self):
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True):
			self.assertEqual(turn_handler._server_scripts_clause("create a report"), "; server scripts: on")

	def test_disabled_yields_off(self):
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=False):
			self.assertEqual(
				turn_handler._server_scripts_clause("build a query report"), "; server scripts: off"
			)

	def test_non_report_turn_skips_the_gate(self):
		# No report/script/console word -> "" without even hitting the gate.
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True) as gate:
			self.assertEqual(turn_handler._server_scripts_clause("hi, show me overdue invoices"), "")
			gate.assert_not_called()

	def test_gate_error_yields_empty(self):
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", side_effect=RuntimeError("boom")):
			self.assertEqual(turn_handler._server_scripts_clause("script report please"), "")


class TestClassifyError(unittest.TestCase):
	"""_classify_error is the single, shared source for a turn error's `code`
	(#702) - settlement.py, pump.py and prepare.py all delegate to it, and
	frontend/src/lib/errors.js mirrors it for the no-`code` reload path. Pure
	string classification, no DB/site access, mirroring TestOrgLocaleClause
	above."""

	def test_the_702_string_is_gateway_not_internal(self):
		# The exact wire text observed in #702: the agent's own generic wording
		# for a mid-run failure that was actually a device-pairing file caught
		# mid-rewrite, nothing to do with the network. Must NOT fall into
		# "unreachable" (that is reserved for OUR OWN failure to reach the
		# gateway) and must NOT fall into "internal" (that headline offers no
		# next step; "gateway" tells the customer to retry).
		code = turn_handler._classify_error("LLM request failed: network connection error.")
		self.assertEqual(code, "gateway")
		self.assertNotEqual(code, "unreachable")
		self.assertNotEqual(code, "internal")

	def test_our_own_unreachable_gateway_stays_unreachable(self):
		# A genuine pre-ack transport failure (WE couldn't reach the agent) is a
		# DIFFERENT failure surface than #702's mid-run gateway hiccup and must
		# keep its own code - retrying the same way won't help either, but the
		# customer-facing story ("I couldn't reach the assistant") is honest
		# about what actually happened.
		self.assertEqual(turn_handler._classify_error("ws open failed: connect ECONNREFUSED"), "unreachable")
		exc = AgentUnreachableError("agent WS closed: 1006")
		self.assertEqual(turn_handler._classify_error("agent WS closed: 1006", exc=exc), "unreachable")

	def test_provider_rejection_stays_provider(self):
		# An upstream LLM provider's own decline (quota/billing/rate limit) is
		# actionable in a way a retry is not - must not collapse into "gateway".
		code = turn_handler._classify_error(
			"Google Generative AI API error (429): You exceeded your current quota."
		)
		self.assertEqual(code, "provider")

	def test_connection_timed_out_is_unreachable_not_timeout(self):
		# "connection timed out" names a transport failure (we could not reach
		# the gateway), not a generic timeout (the model took too long). Must
		# agree with classifyTurnErrorCode in frontend/src/lib/errors.js, which
		# already put this phrase under "unreachable".
		self.assertEqual(turn_handler._classify_error("connection timed out"), "unreachable")

	def test_worker_backstop_text_stays_internal_on_a_reload(self):
		# turn_handler's outer `except Exception` backstop stamps code="internal"
		# directly (bypassing this function) on the LIVE event; a reloaded
		# conversation only has the persisted string and must reclassify it the
		# same way, not fall into the new "gateway" default.
		code = turn_handler._classify_error("unexpected worker error: TypeError")
		self.assertEqual(code, "internal")

	def test_recovery_and_timeout_unaffected_by_the_new_default(self):
		self.assertEqual(
			turn_handler._classify_error("Run did not finish within the recovery window."),
			"recovery-expired",
		)
		self.assertEqual(turn_handler._classify_error("request timed out after 30s"), "timeout")
		exc = AgentUnreachableError("chat.send timed out", code="turn-timeout")
		self.assertEqual(turn_handler._classify_error("chat.send timed out", exc=exc), "timeout")

	def test_three_distinct_customer_actions_are_actually_distinct(self):
		# #702 requirement: a genuine network/timeout failure, a provider
		# rejection, and a transient gateway fault must not collapse into the
		# same code (which is what drives the headline+hint in errors.js).
		codes = {
			turn_handler._classify_error("ws open failed"),
			turn_handler._classify_error("insufficient credit"),
			turn_handler._classify_error("LLM request failed: network connection error."),
		}
		self.assertEqual(codes, {"unreachable", "provider", "gateway"})

	def test_empty_and_none_degrade_to_gateway_not_a_crash(self):
		self.assertEqual(turn_handler._classify_error(""), "gateway")
		self.assertEqual(turn_handler._classify_error(None), "gateway")

	def test_a_definite_pre_ack_rejection_stays_unreachable_when_exc_is_passed(self):
		# #702 review: pump._handle_ack_failure holds an AgentUnreachableError for
		# a definite pre-ack rejection (e.g. "policy_denied", no keyword match in
		# the text). Passing that exc through (as turn_handler's own equivalent
		# pre-ack path already does) must classify it "unreachable", not fall
		# into the new mid-run "gateway" default meant for a run that already
		# started.
		exc = AgentUnreachableError("chat.send rejected: policy_denied: nope")
		code = turn_handler._classify_error("chat.send rejected: policy_denied: nope", exc=exc)
		self.assertEqual(code, "unreachable")


class TestPumpClassifyErrorForwardsExc(unittest.TestCase):
	"""pump._classify_error (#702 review): must forward `exc` to
	turn_handler._classify_error rather than only ever guessing from text, or
	a definite pre-ack rejection it holds an AgentUnreachableError for
	silently drops that signal and falls into the "gateway" default."""

	def test_exc_is_forwarded_and_changes_the_result(self):
		from jarvis.chat import pump
		from jarvis.exceptions import AgentUnreachableError as AUE

		text = "chat.send rejected: policy_denied: nope"
		self.assertEqual(pump._classify_error(text), "gateway", "text alone has no keyword match")
		self.assertEqual(
			pump._classify_error(text, AUE(text)),
			"unreachable",
			"the same text with `exc` passed must match turn_handler's own pre-ack path",
		)


class TestPrepareErrorCodeOverride(FrappeTestCase):
	"""prepare._prepare_error's `code` param (#702 review): a caller that
	already knows the cause (a local bug, not a gateway/relay signal) must be
	able to skip _classify_error's guess entirely - otherwise a generic
	message like "Could not prepare the message." falls into the "gateway"
	default and tells the customer a bug in our own code is a brief hiccup
	worth retrying."""

	def _run(self, *, code=None, error="boom"):
		from jarvis.chat import prepare

		published = {}
		with (
			patch("jarvis.chat.turn_state.prepare_errored", return_value=True),
			patch(
				"jarvis.chat.turn_state.publish_fenced",
				side_effect=lambda *a, **kw: published.update(kw),
			),
			patch("frappe.db.set_value"),
			patch("frappe.db.commit"),
		):
			prepare._prepare_error("run1", 1, "msg1", "conv1", "user1", error, code=code)
		return published

	def test_explicit_code_bypasses_classification(self):
		published = self._run(code="internal", error="Could not prepare the message.")
		self.assertEqual(published.get("code"), "internal")

	def test_no_explicit_code_falls_back_to_classify_error(self):
		published = self._run(error="insufficient credit")
		self.assertEqual(published.get("code"), "provider")
