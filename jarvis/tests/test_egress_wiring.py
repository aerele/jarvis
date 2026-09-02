"""Tests for slice 2c — the egress redactor wired at every chat boundary, plus
the once-per-turn tripwire.

Neutral fake brand ("acme"), so the app names no runtime brand. Each test caches a
rule set on Jarvis Settings, then drives the real boundary function/call-site and
asserts the brand family is scrubbed — and, at the authoritative boundaries, that
the tripwire fires on a hit and stays silent on the per-frame path.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import egress_rules
from jarvis.chat.agent_client import (
	_STREAM_ERROR_SENTINEL,
	_chat_final_failed,
	_chat_final_text,
	failed_final_error,
)
from jarvis.chat.egress_rules import _PATTERNS_FIELD, _TRIPWIRE_MESSAGE, SETTINGS
from jarvis.chat.events import parse_event
from jarvis.chat.settlement import _error_text
from jarvis.chat.turn_recovery import _latest_assistant_text

_LOBSTER = "\U0001f99e"
_RULES = [["acme", "remove"], [_LOBSTER, "remove"]]


def _cache_rules(rules=_RULES):
	frappe.db.set_value(SETTINGS, SETTINGS, _PATTERNS_FIELD, frappe.as_json(rules), update_modified=False)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	egress_rules._invalidate_memo()


class TestParseEventRedaction(FrappeTestCase):
	def setUp(self):
		_cache_rules()

	def test_assistant_text_and_delta_redacted(self):
		out = parse_event({"stream": "assistant", "data": {"text": "on acme", "delta": "acme"}})
		self.assertEqual(out["text"], "on ")
		self.assertEqual(out["delta"], "…")

	def test_tool_title_redacted(self):
		out = parse_event({"stream": "item", "data": {"kind": "tool", "title": "run acme now"}})
		self.assertEqual(out["tool_title"], "run  now")

	def test_lifecycle_error_redacted(self):
		out = parse_event({"stream": "lifecycle", "data": {"phase": "error", "error": "acme failed"}})
		self.assertEqual(out["error"], " failed")

	def test_no_rules_is_passthrough(self):
		_cache_rules([])
		out = parse_event({"stream": "assistant", "data": {"text": "on acme", "delta": "acme"}})
		self.assertEqual(out["text"], "on acme")  # fail-open: nothing cached -> no change

	def test_streaming_boundaries_never_fire_tripwire(self):
		# The anti-row-spam invariant: the per-frame path redacts SILENTLY. Only the
		# authoritative final/recovery boundaries tripwire (once per turn).
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			parse_event({"stream": "assistant", "data": {"text": "on acme", "delta": "acme"}})
			parse_event({"stream": "item", "data": {"kind": "tool", "title": "run acme"}})
			parse_event({"stream": "lifecycle", "data": {"phase": "error", "error": "acme failed"}})
		fire.assert_not_called()


class TestFinalTextExtractorIsPure(FrappeTestCase):
	def setUp(self):
		_cache_rules()

	def test_chat_final_text_returns_raw_unredacted(self):
		# F7: the extractor is PURE — redaction moved to the call sites so failed-final
		# classification sees raw text. It must NOT redact or fire here.
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			self.assertEqual(_chat_final_text({"message": {"content": "running on acme"}}), "running on acme")
			blocks = {"message": {"content": [{"type": "text", "text": "hi acme"}]}}
			self.assertEqual(_chat_final_text(blocks), "hi acme")
			self.assertIsNone(_chat_final_text({"message": {"content": ""}}))
		fire.assert_not_called()

	def test_failed_final_classified_on_raw_even_with_mangling_rule(self):
		# F7 regression: a cached rule that would delete a word of the failure sentinel
		# must NOT hide the failure. Because the extractor is pure, _chat_final_failed
		# still sees the intact sentinel and surfaces the error (no silent empty bubble).
		_cache_rules([["failed", "remove"]])  # would mangle the sentinel if applied first
		payload = {"message": {"content": _STREAM_ERROR_SENTINEL}}
		raw = _chat_final_text(payload)
		self.assertEqual(raw, _STREAM_ERROR_SENTINEL)  # pure: unredacted
		self.assertTrue(_chat_final_failed(payload, raw))  # still classified as failed


class TestFinalCallSiteRedaction(FrappeTestCase):
	"""Drive the relay_mux terminal call site end to end (via the shared mux
	harness) to prove the surfaced final text is redacted + flagged with run_id,
	and the error banner is redacted."""

	def setUp(self):
		_cache_rules()

	def _mux_lane(self, rec):
		from jarvis.chat.relay_mux import RelayMux

		mux = RelayMux(MagicMock(), "egress-target")
		lane = mux.register_run("run-xyz", rec.handler(), session_key="s1")
		return mux, lane

	def test_final_text_redacted_and_flagged_with_run_id(self):
		from jarvis.tests.test_relay_mux import _Recorder

		rec = _Recorder()
		mux, lane = self._mux_lane(rec)
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			mux._route_terminal(lane, {"state": "final", "message": {"content": "on acme"}})
			mux.dispatch()
		kind, payload = rec.terminal
		self.assertEqual(kind, "relay:final")
		self.assertEqual(payload["text"], "on ")
		fire.assert_called_once()
		self.assertEqual(fire.call_args.kwargs.get("run_id"), "run-xyz")  # OP#4 transcript pointer

	def test_terminal_error_message_redacted(self):
		from jarvis.tests.test_relay_mux import _Recorder

		rec = _Recorder()
		mux, lane = self._mux_lane(rec)
		mux._route_terminal(lane, {"state": "error", "errorMessage": "acme failed"})
		mux.dispatch()
		kind, payload = rec.terminal
		self.assertEqual(kind, "relay:error")
		self.assertEqual(payload["error"], " failed")


class TestRecoveryRedaction(FrappeTestCase):
	def setUp(self):
		_cache_rules()

	def test_recovery_string_content_redacted_and_flagged(self):
		msgs = [{"role": "assistant", "content": "recovered acme"}]
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			self.assertEqual(_latest_assistant_text(msgs), "recovered ")
			fire.assert_called_once()

	def test_recovery_block_list_redacted(self):
		msgs = [{"role": "assistant", "content": [{"type": "text", "text": "back to acme"}]}]
		with patch.object(egress_rules, "_fire_tripwire"):
			self.assertEqual(_latest_assistant_text(msgs), "back to ")

	def test_recovery_bare_text_field_redacted(self):
		# The third return path: a message with a bare `text` (no `content`).
		msgs = [{"role": "assistant", "text": "text field acme"}]
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			self.assertEqual(_latest_assistant_text(msgs), "text field ")
			fire.assert_called_once()

	def test_recovery_all_remove_falls_back_to_placeholder(self):
		# F1 on the recovery path: an all-remove reply must not collapse to "" (which
		# _recover_one reads as "no output yet" and hangs the turn to a false timeout).
		msgs = [{"role": "assistant", "content": _LOBSTER}]
		with patch.object(egress_rules, "_fire_tripwire"):
			self.assertEqual(_latest_assistant_text(msgs), "…")

	def test_recovery_clean_text_does_not_flag(self):
		msgs = [{"role": "assistant", "content": "all good"}]
		with patch.object(egress_rules, "_fire_tripwire") as fire:
			self.assertEqual(_latest_assistant_text(msgs), "all good")
			fire.assert_not_called()


class TestErrorBannerRedaction(FrappeTestCase):
	def setUp(self):
		_cache_rules()

	def test_failed_final_error_redacts_provider_detail(self):
		out = failed_final_error("acme gateway error (429): quota exceeded")
		self.assertNotIn("acme", out.lower())
		self.assertIn("quota exceeded", out)

	def test_error_text_redacts(self):
		self.assertEqual(_error_text({"error": "acme exploded"}), " exploded")

	def test_error_text_fallback_unaffected(self):
		self.assertEqual(_error_text({}), "The run ended with an error.")


class TestTripwireRow(FrappeTestCase):
	def test_fire_tripwire_writes_brandfree_row_with_identifiers(self):
		from jarvis.api_errors import DT

		before = frappe.db.count(DT, {"error_code": "egress_redaction"})
		egress_rules._fire_tripwire(conversation="conv-1", run_id="run-1")
		rows = frappe.get_all(
			DT,
			filters={"error_code": "egress_redaction"},
			fields=["message", "surface", "conversation", "run_id"],
			limit=1,
		)
		self.assertEqual(frappe.db.count(DT, {"error_code": "egress_redaction"}), before + 1)
		self.assertTrue(rows)
		# exact brand-free message (also proves nothing runtime-named is recorded)
		self.assertEqual(rows[0]["message"], _TRIPWIRE_MESSAGE)
		# identifiers ride along so an operator can jump to the transcript (OP#4)
		self.assertEqual(rows[0]["conversation"], "conv-1")
		self.assertEqual(rows[0]["run_id"], "run-1")

	def test_fire_tripwire_never_raises(self):
		with patch("jarvis.api_errors._ingest_one", side_effect=RuntimeError("boom")):
			egress_rules._fire_tripwire()  # swallowed


class TestPersistWiring(FrappeTestCase):
	def setUp(self):
		egress_rules._invalidate_memo()

	def test_sync_connection_forwards_patterns_to_persist(self):
		# The recurring scheduled sync must forward the backend-sent redaction_patterns to
		# egress_rules.persist (co-located with the release_notice.persist precedent).
		from jarvis import onboarding

		conn = {"redaction_patterns": [["x", "remove"]], "release_notice": {}, "agent_url": ""}
		with (
			patch("jarvis.onboarding.require_jarvis_admin"),
			patch("jarvis.onboarding.frappe.get_single") as gs,
			patch("jarvis.onboarding.admin_client.get_connection", return_value=conn),
			patch("jarvis.onboarding.release_notice.persist"),
			patch("jarvis.chat.egress_rules.persist") as persist,
		):
			gs.return_value.get_password.return_value = "x"  # api key/secret present
			onboarding.sync_connection()
		persist.assert_called_once_with([["x", "remove"]])
