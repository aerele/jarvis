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

from jarvis.chat import agent_session_pool, error_taxonomy, turn_handler, worker
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

	def test_an_exhausted_quota_is_its_own_terminal_code(self):
		# An upstream provider's own decline is actionable in a way a retry is
		# not - it must not collapse into "gateway". #823 splits the old catch-all
		# "provider" on the axis the customer's next step turns on: an exhausted
		# balance is terminal until it is topped up, while a rate limit clears in
		# seconds and stays retryable.
		env = error_taxonomy.classify("OpenAI error: insufficient_quota, check your billing")
		self.assertEqual(env["code"], "quota-exhausted")
		self.assertFalse(env["retryable"])
		throttled = error_taxonomy.classify("rate_limit_exceeded: slow down")
		self.assertEqual(throttled["code"], "throttled")
		self.assertTrue(throttled["retryable"])

	def test_an_ambiguous_429_mentioning_a_quota_stays_retryable(self):
		# Review finding, and the reason the exhausted markers carry no bare
		# English words. Gemini writes "You exceeded your current quota" for an
		# ordinary per-minute throttle exactly as it does for a spent balance;
		# matching the bare word "quota" would call that terminal and strip the
		# Retry button off a failure that clears in seconds. Terminal is the
		# expensive verdict and has to be earned by an unambiguous marker.
		env = error_taxonomy.classify(
			"Google Generative AI API error (429): You exceeded your current quota."
		)
		self.assertEqual(env["code"], "throttled")
		self.assertTrue(env["retryable"])

	def test_no_exhausted_marker_is_a_bare_english_word(self):
		# The discipline behind the list, pinned so a later edit cannot quietly
		# re-add "quota" or "billing" and reintroduce the stranding bug.
		for marker in error_taxonomy.MARKERS["exhausted"]:
			self.assertTrue(
				"_" in marker or " " in marker,
				f"{marker!r} is a bare word; an exhausted marker must be a slug or a phrase",
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
		self.assertEqual(codes, {"unreachable", "quota-exhausted", "gateway"})

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


class TestErrorTaxonomyRetryable(unittest.TestCase):
	"""jarvis#823. Before this, every turn failure got a Retry button except
	`cancelled`, so a revoked key, a model that does not exist and a spent quota
	each offered an action that could not possibly work. Every code now carries an
	explicit verdict, and this pins which failures may offer one at all."""

	TERMINAL = {
		"agent-unpaired",
		"auth-invalid",
		"cancelled",
		"context-overflow",
		"model-not-found",
		"quota-exhausted",
	}
	RETRYABLE = {
		"gateway",
		"internal",
		"provider",
		"recovery-expired",
		"throttled",
		"timeout",
		"unreachable",
	}

	def test_every_code_has_a_verdict_and_the_two_sets_are_the_whole_taxonomy(self):
		self.assertEqual(
			self.TERMINAL | self.RETRYABLE,
			set(error_taxonomy.TURN_ERROR_CODES),
			"a code added to the taxonomy needs a verdict here",
		)
		self.assertEqual(self.TERMINAL & self.RETRYABLE, set(), "a code cannot be both")
		for code in self.TERMINAL:
			self.assertFalse(error_taxonomy.TURN_ERROR_CODES[code], f"{code} must be terminal")
		for code in self.RETRYABLE:
			self.assertTrue(error_taxonomy.TURN_ERROR_CODES[code], f"{code} must be retryable")

	def test_envelope_takes_retryable_from_the_one_table(self):
		self.assertFalse(error_taxonomy.envelope("auth-invalid")["retryable"])
		self.assertTrue(error_taxonomy.envelope("throttled")["retryable"])

	def test_an_unknown_code_stays_retryable(self):
		# A code this build cannot name is not evidence that a retry is pointless.
		# "This can never work" is the one verdict never to reach for on no
		# evidence, so an unknown code keeps its Retry button.
		self.assertTrue(error_taxonomy.envelope("some-future-code-v2")["retryable"])

	def test_the_guess_tier_is_forced_retryable_whatever_the_table_says(self):
		# The keyword ladder is the LAST resort and it is wrong often enough that
		# it may never produce a terminal verdict. If it ever matched its way to a
		# terminal code, this forces the harmless answer instead.
		env = error_taxonomy.envelope("auth-invalid", confidence="guess")
		self.assertTrue(env["retryable"])
		self.assertEqual(env["confidence"], "guess")


class TestErrorTaxonomyTiers(unittest.TestCase):
	"""The three tiers, most trustworthy first: a machine code we already hold,
	then structure parsed out of provider prose, then the legacy keyword ladder.
	The point of the ordering is that a terminal verdict is only ever reached on
	real evidence."""

	def test_tier1_the_gateways_own_rejection_code_wins(self):
		# The gateway rejects an RPC with {code, message, details}. A pairing
		# rejection has ALREADY survived one self-heal reconnect by the time it is
		# classified, so it is terminal: another Retry click repeats a repair that
		# just failed. The wire shape puts the real reason in details.authReason
		# behind a generic outer code, so both are read.
		exc = AgentUnreachableError(
			"agent rejected: INVALID_REQUEST: unauthorized",
			code="INVALID_REQUEST",
			details={"authReason": "device_token_mismatch"},
		)
		env = error_taxonomy.classify("agent rejected: INVALID_REQUEST: unauthorized", exc)
		self.assertEqual(env["code"], "agent-unpaired")
		self.assertFalse(env["retryable"])
		self.assertEqual(env["confidence"], "code")

	def test_tier1_the_internal_pairing_codes_are_covered_too(self):
		exc = AgentUnreachableError("rejected", code="device-not-paired")
		self.assertEqual(error_taxonomy.classify("rejected", exc)["code"], "agent-unpaired")

	def test_tier2_reads_the_vendors_own_http_status(self):
		# The status a provider writes into its sentence is real structure, not a
		# keyword: "(404)" means the model is not there whatever prose surrounds it.
		cases = {
			"Anthropic error (404): no route for that model": "model-not-found",
			"OpenAI error (401): bad key": "auth-invalid",
			"Vendor error (402): pay up": "quota-exhausted",
			"Vendor error (429): slow down": "throttled",
		}
		for text, code in cases.items():
			env = error_taxonomy.classify(text)
			self.assertEqual(env["code"], code, text)
			self.assertEqual(env["confidence"], "parsed", text)

	def test_tier2_does_not_read_a_bare_number_as_a_status(self):
		# A three-digit number in prose is not a status. Reading one as a status
		# would classify a token count or a model name as a terminal failure.
		self.assertEqual(error_taxonomy.classify("the model returned 404 tokens")["code"], "gateway")

	def test_tier2_prefers_an_unambiguous_reason_over_the_bare_status(self):
		# A 429 that names an UNAMBIGUOUS exhaustion slug is terminal, not
		# ordinary back-pressure. Same status, opposite advice, so the marker
		# wins - but only a marker that cannot mean anything else.
		self.assertEqual(
			error_taxonomy.classify("error (429): usage_limit_reached")["code"], "quota-exhausted"
		)

	def test_tier2_carries_the_reset_clock_the_provider_named(self):
		env = error_taxonomy.classify('rate limit; "resets_in_seconds": 2400')
		self.assertEqual(env["data"], {"resets_in_seconds": 2400})
		self.assertEqual(
			error_taxonomy.classify("usage_limit_reached; resets in 45 minutes")["data"],
			{"resets_in_seconds": 2700},
		)
		# A clock survives the guess tier too, so an unrecognised failure that DID
		# name its wait still tells the customer how long rather than "a moment".
		self.assertEqual(
			error_taxonomy.classify("something odd happened; retry-after: 90")["data"],
			{"resets_in_seconds": 90},
		)
		# A nonsense clock is noise, not a promise to show a customer.
		self.assertIsNone(error_taxonomy.classify("retry-after: 0")["data"])

	def test_tier3_still_guesses_but_only_as_a_last_resort(self):
		# Nothing structured in the text, so the legacy ladder answers - and says
		# so, which is what keeps its verdict retryable.
		env = error_taxonomy.classify("LLM request failed: network connection error.")
		self.assertEqual(env["code"], "gateway")
		self.assertEqual(env["confidence"], "guess")
		self.assertTrue(env["retryable"])

	def test_classify_is_total(self):
		# This runs on the error path; a second exception here would strand the
		# turn's spinner.
		for raw in (None, "", 42, object()):
			env = error_taxonomy.classify(raw)
			self.assertIn("code", env)
			self.assertIsInstance(env["retryable"], bool)

	def test_the_context_overflow_park_markers_are_deliberately_excluded(self):
		# turn_handler routes a relay:error containing the literal "context
		# overflow" into the auto-compact park branch (the agent compacts and
		# retries, so the turn parks for snapshot recovery rather than erroring).
		# That branch runs BEFORE any classification and must keep its position:
		# this taxonomy is display classification and never steers routing. The
		# terminal variants OTHER vendors use, where no compaction retry is
		# coming, are the ones that earn the code.
		self.assertNotIn(
			"context overflow",
			[m for markers in error_taxonomy.MARKERS.values() for m in markers],
		)
		self.assertEqual(
			error_taxonomy.classify("This model's maximum context length is 8192 tokens")["code"],
			"context-overflow",
		)


class TestErrorTaxonomyPersistence(unittest.TestCase):
	"""Persistence parity. The classification used to live only on the realtime
	event, so reloading the page re-guessed the verdict from the stored string and
	could contradict the card the customer had just read. The row now carries the
	envelope, and these pin the shape every writer uses."""

	def test_the_row_values_carry_code_and_retryable_and_clear_streaming(self):
		env = error_taxonomy.classify("Vendor error (401): bad key")
		values = error_taxonomy.error_row_values("Vendor error (401): bad key", env)
		self.assertEqual(values["error_code"], "auth-invalid")
		self.assertEqual(values["error_retryable"], 0)
		self.assertEqual(values["streaming"], 0)

	def test_a_retryable_failure_stores_a_truthy_flag(self):
		env = error_taxonomy.classify("rate limit")
		self.assertEqual(error_taxonomy.error_row_values("rate limit", env)["error_retryable"], 1)

	def test_the_error_text_is_capped_so_a_runaway_provider_cannot_fill_the_column(self):
		env = error_taxonomy.envelope("gateway")
		values = error_taxonomy.error_row_values("x" * 5000, env)
		self.assertEqual(len(values["error"]), error_taxonomy.ERROR_TEXT_MAX_CHARS)

	def test_the_sql_params_and_the_set_value_dict_agree(self):
		# Six writers use one of these two shapes; they must record the same thing
		# or a turn's verdict depends on which code path failed it.
		env = error_taxonomy.classify("Vendor error (404): no such model")
		values = error_taxonomy.error_row_values("boom", env)
		params = error_taxonomy.error_row_params("boom", env, m="MSG-1")
		self.assertEqual(params["err"], values["error"])
		self.assertEqual(params["err_code"], values["error_code"])
		self.assertEqual(params["err_retryable"], values["error_retryable"])
		self.assertEqual(params["err_data"], values["error_data"])
		self.assertEqual(params["m"], "MSG-1")
		# Every column the assignment string names must be bound.
		for key in ("err", "err_code", "err_retryable", "err_data"):
			self.assertIn(f"%({key})s", error_taxonomy.MSG_ERROR_ASSIGNMENTS)

	def test_the_published_event_carries_the_flag_the_retry_button_is_gated_on(self):
		env = error_taxonomy.classify("Vendor error (429): slow down; retry-after: 120")
		extra = error_taxonomy.publish_extra(env)
		self.assertEqual(extra["code"], "throttled")
		self.assertTrue(extra["retryable"])
		self.assertEqual(extra["resets_in_seconds"], 120)
		# No clock named, no key - never an invented number.
		self.assertNotIn(
			"resets_in_seconds", error_taxonomy.publish_extra(error_taxonomy.envelope("gateway"))
		)

	def test_a_stored_envelope_round_trips_and_matches_what_was_written(self):
		# What settlement wrote is what the terminal re-publish backstop reads, so
		# a lost settlement publish is redelivered with the SAME verdict rather
		# than a third opinion classified afresh.
		env = error_taxonomy.classify("Vendor error (401): bad key")
		row = error_taxonomy.error_row_values("Vendor error (401): bad key", env)
		back = error_taxonomy.stored_envelope(row)
		self.assertEqual(back["code"], env["code"])
		self.assertEqual(back["retryable"], env["retryable"])

	def test_a_pre_823_row_reports_nothing_stored_so_the_caller_falls_back(self):
		# Absence of the CODE is the test, never falsiness of retryable: a stored
		# error_retryable of 0 is a meaningful terminal verdict, not a missing one.
		self.assertIsNone(error_taxonomy.stored_envelope({"error": "old row", "error_code": None}))
		self.assertIsNone(error_taxonomy.stored_envelope({}))
		terminal = {"error_code": "auth-invalid", "error_retryable": 0}
		self.assertIsNotNone(error_taxonomy.stored_envelope(terminal))
		self.assertFalse(error_taxonomy.stored_envelope(terminal)["retryable"])

	def test_a_malformed_stored_blob_never_breaks_the_error_path(self):
		env = error_taxonomy.stored_envelope({"error_code": "gateway", "error_data": "not json"})
		self.assertEqual(env["code"], "gateway")
		self.assertIsNone(env["data"])


class TestErrorTaxonomyParity(unittest.TestCase):
	"""The drift ratchet (#823). jarvis#757 and #760 both shipped out of three
	hand-synced copies of this taxonomy. One contract file, asserted from BOTH
	suites, is what stops a fourth: frontend/src/lib/errors.test.js asserts the JS
	tables against the same JSON. Neither side reads it at runtime, so there is no
	packaging dependency - it exists purely to make drift loud."""

	@staticmethod
	def _contract():
		import json
		import os

		path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chat", "turn_error_codes.json")
		with open(path) as fh:
			return json.load(fh)

	def test_the_python_code_table_matches_the_contract(self):
		contract = self._contract()
		want = {code: spec["retryable"] for code, spec in contract["codes"].items()}
		self.assertEqual(error_taxonomy.TURN_ERROR_CODES, want)

	def test_the_python_text_ladder_matches_the_contract(self):
		# The ladder is the other half of the drift surface: two ladders that
		# disagree make one pre-#823 row read differently before and after a
		# refresh, which is the same defect by another route.
		contract = self._contract()
		got = {name: list(markers) for name, markers in error_taxonomy.MARKERS.items()}
		self.assertEqual(got, contract["markers"])
		status = {str(k): v for k, v in error_taxonomy.HTTP_STATUS_CODES.items()}
		self.assertEqual(status, contract["http_status"])

	def test_every_status_and_marker_target_is_a_real_code(self):
		for code in error_taxonomy.HTTP_STATUS_CODES.values():
			self.assertIn(code, error_taxonomy.TURN_ERROR_CODES)


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
		# #823 split the old catch-all `provider`: an insufficient balance is an
		# unambiguous exhaustion, which is terminal and sends the customer to their
		# plan rather than at a Retry button that cannot help.
		published = self._run(error="insufficient credit")
		self.assertEqual(published.get("code"), "quota-exhausted")
		self.assertFalse(published.get("retryable"))
