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

import time
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_session_pool, empty_reply_recovery, turn_handler, worker
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)
from jarvis.tests.test_chat_worker import (
	_fake_event_stream,
	_make_conversation_with_user_message,
)


def setUpModule():
	install_synthetic_runtime_profile()


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


class TestHandleChatSendForwardsRunIdToTheHandoffHint(FrappeTestCase):
	"""The legacy transport: handle_chat_send must hand its run_id to the prompt
	assembly, or the Payment Calendar hand-off hint lines carry run_id null."""

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

	def test_the_hint_telemetry_line_carries_the_turns_run_id(self):
		from jarvis import telemetry

		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with (
			patch("jarvis.chat.agent_session_pool.AgentSession.connect", return_value=fake_sess),
			patch("jarvis.chat.worker.publish_to_user"),
			patch("jarvis.chat.turn_handler.payment_calendar_handoff_enabled", return_value=True),
			patch("jarvis.chat.turn_handler._visible_as", return_value=True),
			patch.object(telemetry, "_emit") as emit,
		):
			turn_handler.handle_chat_send(
				{
					"conversation_id": self.conv,
					"message_id": self.user_msg,
					"run_id": "r-pc-legacy",
					"context": {"handoff_pages": ["payment-calendar"]},
				}
			)
		lines = [c.args[0] for c in emit.call_args_list if c.args[0]["kind"] == "pc_handoff_hint"]
		self.assertEqual([(ln["outcome"], ln["run_id"]) for ln in lines], [("on", "r-pc-legacy")])


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


class TestPreAckFailureRecordsDeadSignIn(FrappeTestCase):
	"""A pre-ack run error is final too: a dead sign-in in it must become a chat signal."""

	DEAD = "chat.send rejected: 401 auth_unavailable: unauthorized (openai/gpt-5.6)"

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

	def _fail_pre_ack(self, text):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = AgentUnreachableError(text)
		from jarvis import subscription_health

		with (
			patch("jarvis.chat.agent_session_pool.AgentSession.connect", return_value=fake_sess),
			patch("jarvis.chat.worker.publish_to_user") as pub,
			patch.object(subscription_health, "record_chat_failure") as rec,
		):
			turn_handler.handle_chat_send(
				{"conversation_id": self.conv, "message_id": self.user_msg, "run_id": "r-preack"}
			)
		return rec, [c.args[1] for c in pub.call_args_list if c.args[1].get("kind") == "run:error"]

	def test_a_pre_ack_dead_sign_in_records_the_signal(self):
		rec, errors = self._fail_pre_ack(self.DEAD)
		self.assertEqual([e.get("changed_data") for e in errors], [False])
		rec.assert_called_once_with("openai")

	def test_a_pre_ack_unrelated_error_records_nothing(self):
		rec, errors = self._fail_pre_ack("agent WS closed: 1006")
		self.assertEqual(len(errors), 1)
		rec.assert_not_called()


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


class TestAppVersionsClause(unittest.TestCase):
	"""_app_versions_clause names the CURATED platform-stack apps + versions
	(jarvis first) in the context line so the agent answers "what version / what
	environment are you" truthfully - Jarvis as one app among the others in the
	customer's Frappe/ERPNext bench. Each app's read is isolated; a wider failure
	degrades to ''."""

	def _run(self, *, installed, versions):
		# An app NOT in ``versions`` => ``frappe.get_attr`` RAISES AttributeError,
		# exactly as the real accessor does for an app whose __init__ has no
		# __version__ (getattr with no default). Mirroring that is what lets these
		# tests actually exercise the per-app skip path.
		def fake_get_attr(dotted):
			app = dotted.split(".", 1)[0]
			if app not in versions:
				raise AttributeError(f"module '{app}' has no attribute '__version__'")
			return versions[app]

		with (
			patch("frappe.get_installed_apps", return_value=installed),
			patch("frappe.get_attr", side_effect=fake_get_attr),
		):
			return turn_handler._app_versions_clause()

	def test_names_curated_stack_jarvis_first(self):
		clause = self._run(
			installed=["frappe", "erpnext", "jarvis", "hrms"],
			versions={"jarvis": "1.4.0", "frappe": "15.1.0", "erpnext": "15.2.0", "hrms": "15.0.0"},
		)
		# Curated order (jarvis first), not install order; india_compliance absent.
		self.assertEqual(clause, "; apps: jarvis 1.4.0, frappe 15.1.0, erpnext 15.2.0, hrms 15.0.0")

	def test_only_curated_apps_are_named(self):
		# telephony is installed + versioned but NOT in the platform stack -> omitted.
		clause = self._run(
			installed=["frappe", "jarvis", "telephony"],
			versions={"jarvis": "1.4.0", "frappe": "15.1.0", "telephony": "0.0.1"},
		)
		self.assertEqual(clause, "; apps: jarvis 1.4.0, frappe 15.1.0")
		self.assertNotIn("telephony", clause)

	def test_uninstalled_curated_app_is_skipped(self):
		clause = self._run(installed=["frappe", "jarvis"], versions={"jarvis": "1.4.0", "frappe": "15.1.0"})
		self.assertEqual(clause, "; apps: jarvis 1.4.0, frappe 15.1.0")

	def test_one_version_read_failure_skips_only_that_app(self):
		# frappe is installed but has no resolvable __version__ (get_attr raises);
		# jarvis still renders - the clause is NOT blanked. Per-app isolation guard.
		clause = self._run(installed=["jarvis", "frappe"], versions={"jarvis": "1.4.0"})
		self.assertEqual(clause, "; apps: jarvis 1.4.0")

	def test_no_resolvable_versions_yields_empty_clause(self):
		self.assertEqual(self._run(installed=["jarvis", "frappe"], versions={}), "")

	def test_installed_apps_failure_yields_empty_clause(self):
		with patch("frappe.get_installed_apps", side_effect=RuntimeError("boom")):
			self.assertEqual(turn_handler._app_versions_clause(), "")


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

	def test_provider_rate_limit_has_its_own_category(self):
		# An upstream LLM provider's own decline (quota/billing/rate limit) is
		# actionable in a way a retry is not - must not collapse into "gateway".
		code = turn_handler._classify_error(
			"Google Generative AI API error (429): You exceeded your current quota."
		)
		self.assertEqual(code, "rate-limit")

	def test_specific_rejection_survives_transport_wrapper(self):
		for text, expected in [("401 Unauthorized", "authentication"), ("insufficient credit", "billing")]:
			with self.subTest(text=text):
				self.assertEqual(
					turn_handler._classify_error(text, exc=AgentUnreachableError(text)), expected
				)

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
		self.assertEqual(codes, {"unreachable", "billing", "gateway"})

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


class TestNoteEmptyReply(FrappeTestCase):
	"""One telemetry line per classified empty reply: it measures how often the model
	sends back nothing and at what context size."""

	def _lines(self, err, code, logger=None):
		logger = logger or MagicMock()
		with patch("jarvis.chat.latency.get_logger", return_value=logger):
			turn_handler._note_empty_reply("r1", "no-such-conv", err, code)
		return [c.args for c in logger.info.call_args_list]

	def test_each_variant_is_named(self):
		for err, code, variant in (
			("\u26a0\ufe0f Agent couldn't generate a response. Please try again.", "empty-reply", "plain"),
			("Agent couldn\u2019t generate a response.", "empty-reply", "bare"),
			("Agent couldn't generate a response: 500 from upstream", "empty-reply", "other"),
			("Agent couldn't generate a response. Note: some tool actions ...", "empty-reply-tools", "tools"),
		):
			with self.subTest(variant=variant):
				lines = self._lines(err, code)
				self.assertEqual(len(lines), 1)
				self.assertEqual(
					lines[0][0],
					"empty_reply run_id=%s conversation=%s variant=%s last_total_tokens=%s context_pct=%s",
				)
				self.assertEqual(lines[0][1:], ("r1", "no-such-conv", variant, "", ""))

	def test_long_whitespace_is_read_in_linear_time(self):
		text = "Agent couldn't generate a response." + " " * 30000 + "x"
		started = time.monotonic()
		self.assertIsNone(empty_reply_recovery.EMPTY_REPLY_RE.match(text))
		self.assertEqual(self._lines(text, "empty-reply")[0][3], "bare", "only 300 characters are read")
		self.assertLess(time.monotonic() - started, 0.5)

	def test_a_failed_token_lookup_still_writes_the_line(self):
		with patch.object(frappe.db, "sql", side_effect=RuntimeError("db down")):
			lines = self._lines("Agent couldn't generate a response.", "empty-reply")
		self.assertEqual(lines[0][1:], ("r1", "no-such-conv", "bare", "", ""))

	def test_other_codes_write_nothing(self):
		self.assertEqual(self._lines("429 rate_limit_error", "rate-limit"), [])

	def test_a_lifecycle_error_frame_also_writes_the_line(self):
		logger = MagicMock()
		with (
			patch.object(turn_handler, "_mark_errored"),
			patch.object(turn_handler, "_publish_to_user"),
			patch("jarvis.chat.latency.get_logger", return_value=logger),
		):
			turn_handler._handle_event_inner(
				{"kind": "lifecycle", "phase": "error", "error": "Agent couldn't generate a response."},
				conversation_id="c1",
				assistant_msg_name="m1",
				tool_msg_by_call_id={},
				user="u@x",
				run_id="r1",
				batcher=MagicMock(),
			)
		self.assertEqual([c.args[1:4] for c in logger.info.call_args_list], [("r1", "c1", "bare")])

	def test_a_logging_failure_never_raises(self):
		logger = MagicMock()
		logger.info.side_effect = RuntimeError("log down")
		self._lines("Agent couldn't generate a response.", "empty-reply", logger)


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
		self.assertEqual(published.get("code"), "billing")


class TestCompactionEventPublishesRunStatus(FrappeTestCase):
	"""The runtime brackets an automatic compaction with a ``compaction``
	stream event; the turn handler must surface it to the chat as a
	``run:status`` so the UI can show a transient "reorganising" state
	without treating the run as errored or finished."""

	def test_compaction_event_publishes_run_status(self):
		with patch.object(turn_handler, "_publish_to_user") as pub:
			turn_handler._handle_event_inner(
				{"kind": "compaction", "phase": "start", "completed": False},
				conversation_id="c1",
				assistant_msg_name="m1",
				tool_msg_by_call_id={},
				user="Administrator",
				run_id="r1",
				batcher=MagicMock(),
			)
			turn_handler._handle_event_inner(
				{"kind": "compaction", "phase": "end", "completed": True},
				conversation_id="c1",
				assistant_msg_name="m1",
				tool_msg_by_call_id={},
				user="Administrator",
				run_id="r1",
				batcher=MagicMock(),
			)
		statuses = [c.args[1]["status"] for c in pub.call_args_list if c.args[1]["kind"] == "run:status"]
		self.assertEqual(statuses, ["compacting", "compacted"])


class TestMaybeApplyLlmSwitch(FrappeTestCase):
	"""jarvis#1425 review (live e2e2, 2026-09-27): on the default chat path a
	normal reply ending must nudge a HELD switch forward directly at the
	assistant message's own terminal write, not rely solely on admission's
	settle hooks (which a run_id gap on this path can miss). Covers the
	trigger itself plus its wiring into both terminal-write exits
	(``_mark_errored`` for error/abandon; the clean lifecycle.end commit is
	covered by TestRunAgentTurnRelayTerminals-style coverage in
	test_chat_worker.py owning that path's behavior)."""

	def test_calls_try_apply_when_a_switch_is_active(self):
		from jarvis.chat import llm_switch

		with (
			patch.object(llm_switch, "is_active", return_value=True),
			patch.object(llm_switch, "try_apply") as try_apply,
		):
			turn_handler._maybe_apply_llm_switch()
		try_apply.assert_called_once()

	def test_skips_try_apply_when_no_switch_is_active(self):
		from jarvis.chat import llm_switch

		with (
			patch.object(llm_switch, "is_active", return_value=False),
			patch.object(llm_switch, "try_apply") as try_apply,
		):
			turn_handler._maybe_apply_llm_switch()
		try_apply.assert_not_called()

	def test_never_raises_when_llm_switch_blows_up(self):
		from jarvis.chat import llm_switch

		with patch.object(llm_switch, "is_active", side_effect=RuntimeError("redis down")):
			turn_handler._maybe_apply_llm_switch()  # must not raise

	def test_mark_errored_triggers_the_switch_poke_after_its_own_commit(self):
		with (
			patch("frappe.db.set_value") as set_value,
			patch("frappe.db.commit") as commit,
			patch.object(turn_handler, "_maybe_apply_llm_switch") as poke,
		):
			manager = MagicMock()
			manager.attach_mock(set_value, "set_value")
			manager.attach_mock(commit, "commit")
			manager.attach_mock(poke, "poke")
			turn_handler._mark_errored("m1", "boom")
		# The poke must run, and strictly AFTER the terminal write's own commit -
		# the whole point is that _inflight()'s read sees streaming=0 already.
		names = [c[0] for c in manager.mock_calls]
		self.assertIn("commit", names)
		self.assertIn("poke", names)
		self.assertLess(names.index("commit"), names.index("poke"))


class TestSessionRepairLegacy(FrappeTestCase):
	"""jarvis-admin-v2#753: the pre-send self-repair on the legacy path. It runs inside
	the pooled checkout, after the #712 stale-device heal and before the mint branch."""

	KEY = "agent:fake"

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hello there")
		self.info_lines: list[str] = []
		logger = MagicMock()
		logger.info.side_effect = lambda *a, **k: self.info_lines.append(a[0] % a[1:] if a[1:] else a[0])
		p = patch("jarvis.chat.latency.get_logger", return_value=logger)
		p.start()
		self.addCleanup(p.stop)
		self._conf_prev = frappe.local.conf.get("jarvis_session_repair_enabled", "<absent>")
		frappe.local.conf.pop("jarvis_session_repair_enabled", None)
		self._clear_error_log()

	def tearDown(self):
		if self._conf_prev == "<absent>":
			frappe.local.conf.pop("jarvis_session_repair_enabled", None)
		else:
			frappe.local.conf["jarvis_session_repair_enabled"] = self._conf_prev
		self._clear_error_log()
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	@staticmethod
	def _clear_error_log():
		frappe.db.delete("Error Log", {"method": ["like", "jarvis.chat.session_repair.%"]})
		frappe.db.delete("Jarvis Chat Session", {"session_key": ["like", "agent:minted-%"]})
		frappe.db.commit()

	@staticmethod
	def _rows(title):
		return frappe.db.count("Error Log", {"method": f"jarvis.chat.session_repair.{title}"})

	def _run(self, *, describe=None, restore=None, run_id="r-rep"):
		"""Drive one turn against a MagicMock session. ``describe`` / ``restore``:
		a value to return, or an exception instance to raise. Returns
		(fake_sess, publish mock)."""
		fake_sess = MagicMock()
		for name, effect in (
			("describe_session", describe),
			("restore_session", restore),
		):
			method = getattr(fake_sess, name)
			if isinstance(effect, BaseException):
				method.side_effect = effect
			elif effect is not None:
				method.return_value = effect
		fake_sess.create_session.return_value = f"agent:minted-{frappe.generate_hash(length=6)}"
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[{"kind": "lifecycle", "phase": "end"}, {"kind": "relay:final", "text": None}]
		)
		with (
			patch("jarvis.chat.agent_session_pool.AgentSession.connect", return_value=fake_sess),
			patch("jarvis.chat.worker.publish_to_user") as pub,
			patch.object(turn_handler, "_session_model_patch", return_value=(True, "p/m")),
		):
			turn_handler.handle_chat_send(
				{"conversation_id": self.conv, "message_id": self.user_msg, "run_id": run_id}
			)
		return fake_sess, pub

	def _repair_lines(self):
		return [ln for ln in self.info_lines if ln.startswith("session_repair ")]

	@staticmethod
	def _names(fake_sess):
		return [c[0] for c in fake_sess.method_calls if c[0] in _GATEWAY_STEPS]

	@staticmethod
	def _sent(fake_sess) -> str:
		return fake_sess.chat_send.call_args.args[1]

	@staticmethod
	def _waking(pub):
		return [
			c.args[1]
			for c in pub.call_args_list
			if c.args[1].get("kind") == "run:status" and c.args[1].get("status") == "waking"
		]

	def test_healthy_session_is_probed_once_and_sent_unchanged(self):
		fake, pub = self._run(describe={"sessionId": "s1", "archived": False})
		self.assertEqual(self._names(fake), ["describe_session", "set_session_model", "chat_send"])
		self.assertEqual(fake.chat_send.call_args.args[0], self.KEY)
		self.assertEqual(self._waking(pub), [])
		self.assertEqual(self._rows("probe_failed"), 0)

	def test_magicmock_row_is_absent_not_a_repair(self):
		fake, _pub = self._run()
		self.assertNotIn("restore_session", self._names(fake))
		self.assertEqual(self._rows("probe_failed"), 0)

	def test_archived_session_is_restored_before_the_model_patch_on_the_same_key(self):
		fake, pub = self._run(describe={"sessionId": "sid-9", "archived": True, "archivedAt": 1})
		self.assertEqual(
			self._names(fake), ["describe_session", "restore_session", "set_session_model", "chat_send"]
		)
		fake.restore_session.assert_called_once_with(self.KEY, "sid-9")
		self.assertEqual(fake.chat_send.call_args.args[0], self.KEY)
		self.assertEqual(frappe.db.get_value("Jarvis Conversation", self.conv, "session_key"), self.KEY)
		fake.create_session.assert_not_called()
		self.assertEqual(len(self._waking(pub)), 1)

	def test_tombstoned_session_makes_no_mutating_call_and_still_sends(self):
		# Tombstone alone, and tombstone + archived: UNREPAIRABLE. No restore, no waking,
		# no Error Log row; the send goes ahead and fails into the "session-ended" copy.
		for archived in (False, True):
			with self.subTest(archived=archived):
				agent_session_pool._POOL.clear()
				_cleanup_user_conversations()
				self.conv, self.user_msg = _make_conversation_with_user_message("hello there")
				fake, pub = self._run(
					describe={
						"sessionId": "sid-9",
						"archived": archived,
						"restartRecoveryStatus": "tombstoned",
					},
					run_id=f"r-tomb-{archived}",
				)
				self.assertEqual(self._names(fake), ["describe_session", "set_session_model", "chat_send"])
				self.assertEqual(fake.chat_send.call_args.args[0], self.KEY)
				self.assertIn("hello there", self._sent(fake))
				self.assertEqual(self._waking(pub), [])
				for title in ("refused", "probe_refused", "probe_failed"):
					self.assertEqual(self._rows(title), 0)
				self.assertIn("outcome=unrepairable", self._repair_lines()[-1])

	def test_a_stale_device_key_is_minted_not_probed_and_waking_is_published_once(self):
		session_doctype = "Jarvis Chat Session"
		frappe.get_doc(
			{
				"doctype": session_doctype,
				"session_key": self.KEY,
				"user": TEST_USER,
				"chat_device_id": "old-device-before-repair",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		settings = frappe.get_single("Jarvis Settings")
		original_device_id = settings.chat_device_id
		settings.db_set("chat_device_id", "new-device-after-repair")
		frappe.db.commit()
		try:
			fake, pub = self._run(describe={"sessionId": "s", "archived": True})
			fake.describe_session.assert_not_called()
			fake.restore_session.assert_not_called()
			fake.create_session.assert_called_once()
			self.assertEqual(len(self._waking(pub)), 1, "the #712 block's waking is the only one")
		finally:
			settings.db_set("chat_device_id", original_device_id)
			frappe.db.commit()
			frappe.db.delete(session_doctype, {"session_key": self.KEY})
			frappe.db.commit()

	def test_a_conversation_without_a_key_mints_and_is_never_probed(self):
		frappe.db.set_value("Jarvis Conversation", self.conv, "session_key", "")
		frappe.db.commit()
		fake, pub = self._run(describe={"sessionId": "s", "archived": True})
		fake.describe_session.assert_not_called()
		fake.create_session.assert_called_once()
		self.assertEqual(len(self._waking(pub)), 1)

	def test_a_temporary_restore_failure_is_todays_retryable_unreachable_error(self):
		err = AgentUnreachableError("sessions.patch timed out", code="ack-timeout")
		with patch.object(turn_handler, "_admission_settle") as settle:
			fake, pub = self._run(describe={"sessionId": "sid-9", "archived": True}, restore=err)
		fake.chat_send.assert_not_called()
		settle.assert_called_once()
		self.assertEqual(settle.call_args.args[:2], ("r-rep", "errored"))
		errors = [c.args[1] for c in pub.call_args_list if c.args[1].get("kind") == "run:error"]
		self.assertEqual(len(errors), 1)
		self.assertEqual(errors[0].get("code"), "unreachable")
		self.assertIs(errors[0].get("changed_data"), False)
		row = frappe.get_all(
			MSG, filters={"conversation": self.conv, "role": "assistant"}, fields=["error", "streaming"]
		)[0]
		self.assertEqual(row["streaming"], 0)
		self.assertTrue(row["error"])

	def test_a_definitive_refusal_still_sends_and_logs_one_refused_row(self):
		err = AgentUnreachableError(
			"sessions.patch rejected: INVALID_REQUEST: nope",
			code="INVALID_REQUEST",
			rejected_method="sessions.patch",
		)
		fake, _pub = self._run(describe={"sessionId": "sid-9", "archived": True}, restore=err)
		fake.chat_send.assert_called_once()
		self.assertEqual(self._rows("refused"), 1)
		rows = frappe.get_all("Error Log", {"method": "jarvis.chat.session_repair.refused"}, pluck="error")
		self.assertNotIn(self.KEY, str(rows))

	def test_a_collab_key_is_never_probed(self):
		frappe.db.set_value(
			"Jarvis Conversation", self.conv, "session_key", "agent:main:dashboard:collab:abc"
		)
		frappe.db.commit()
		fake, _pub = self._run(describe={"sessionId": "s", "archived": True})
		fake.describe_session.assert_not_called()
		fake.restore_session.assert_not_called()
		self.assertIn("outcome=skipped", self._repair_lines()[-1])

	def test_a_temporary_probe_failure_changes_nothing(self):
		err = AgentUnreachableError("unavailable", code="UNAVAILABLE")
		fake, _pub = self._run(describe=err)
		fake.chat_send.assert_called_once()
		self.assertNotIn("restore_session", self._names(fake))
		self.assertEqual(self._rows("probe_failed"), 0)

	def test_the_session_repair_info_line_names_the_legacy_path_and_run_id(self):
		self._run(describe={"sessionId": "sid-9", "archived": True}, run_id="r-line")
		lines = [ln for ln in self.info_lines if ln.startswith("session_repair ")]
		self.assertEqual(len(lines), 1)
		self.assertIn("outcome=restored", lines[0])
		self.assertIn("path=legacy", lines[0])
		self.assertIn("run_id=r-line", lines[0])
		self.assertNotIn(self.KEY, lines[0])

	def test_kill_switch_off_sends_no_describe(self):
		for token in ("", 0, "0", False, "false", "off", "no"):
			with self.subTest(token=token):
				# One turn per conversation, and a fresh pool (it caches the first fake).
				agent_session_pool._POOL.clear()
				_cleanup_user_conversations()
				self.conv, self.user_msg = _make_conversation_with_user_message("hello there")
				frappe.local.conf["jarvis_session_repair_enabled"] = token
				fake, _pub = self._run(
					describe={"sessionId": "s", "archived": True, "restartRecoveryStatus": "tombstoned"},
					run_id=f"r-off-{token!r}",
				)
				fake.describe_session.assert_not_called()
				fake.chat_send.assert_called_once()

	def test_flag_absent_probes(self):
		fake, _pub = self._run(describe={"sessionId": "s", "archived": False})
		fake.describe_session.assert_called_once_with(self.KEY)


_GATEWAY_STEPS = {
	"describe_session",
	"restore_session",
	"set_session_model",
	"chat_send",
	"create_session",
}
