"""A pinned one-shot must be recognisable as pinned from the log alone.

Issue #531. The three throwaway one-shots (auto-title, pattern polish, prefix
prewarm) each pass an explicit model/provider on openclaw's ``agent`` RPC, and
openclaw answers that by dropping the run's model-fallback chain entirely:
``hasExplicitRunOverride`` forces ``modelOverrideSource: "user"``, and
``resolveEffectiveModelFallbacks`` returns ``[]`` for any source but ``"auto"``.
There is no per-request way to opt back in - ``AgentParamsSchema`` is
``additionalProperties: false`` and carries no such field, and ``sessions.patch``
rejects the marker outright (closed PR #517).

The pin stays (prewarm must warm ONE model's cache; titles must stay cheap), so
the fix is to make the inability to fail over unambiguous. Two things carry it,
both asserted here: a pinned run id is prefixed differently from an unpinned
one, which makes openclaw's own ``embedded run failover decision: runId=...
next=none`` line self-describing, and the bench logs one line per pinned turn
naming the run id and the pinned model.

SCOPE NOTE: the transport is stubbed here, as everywhere else in this suite, so
these tests assert INTENT - what the bench puts on the wire and what it records
about it. They cannot observe openclaw's failover resolver or the log lines it
emits; that behaviour was read off the shipped 2026.6.8 bundle, not from a test.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.openclaw_client import (
	ONESHOT_RUN_PREFIX,
	PINNED_ONESHOT_RUN_PREFIX,
	OpenclawSession,
	oneshot_run_id,
)

LOG = "jarvis.chat.openclaw_client"
SKEY = "agent:main:dashboard:throwaway-1"


def _bare_session() -> OpenclawSession:
	"""An OpenclawSession with no WS behind it - callers stub the transport."""
	return OpenclawSession.__new__(OpenclawSession)


def _streaming_session() -> OpenclawSession:
	"""A session whose ``agent`` request acks and then ends the run at once."""
	sess = _bare_session()
	frames = iter(
		[
			{"type": "res", "id": "req1", "ok": True, "payload": {"runId": "run1"}},
			{
				"type": "event",
				"payload": {"runId": "run1", "stream": "lifecycle", "data": {"phase": "end"}},
			},
		]
	)
	sess._send = lambda method, params: "req1"
	sess._recv = lambda timeout_s: next(frames, None)
	return sess


@contextlib.contextmanager
def _checkout_yielding(sess):
	@contextlib.contextmanager
	def _cm(_gateway_url):
		yield sess

	with patch("jarvis.chat.openclaw_session_pool.checkout", _cm):
		yield


def _pool_session(reply: str) -> MagicMock:
	sess = MagicMock()
	sess.create_session.return_value = SKEY
	sess.is_run_active.return_value = False
	sess.stream_agent_turn.return_value = iter([{"kind": "assistant", "text": reply}])
	return sess


class TestPinnedRunId(FrappeTestCase):
	"""openclaw uses the idempotency key verbatim as the run id, so the run id
	is the one field guaranteed to reach every failover log line."""

	def test_run_id_announces_the_pin_and_keeps_the_caller_key(self):
		run_id = oneshot_run_id("title", SKEY, model="gpt-5.5-mini", provider=None)

		self.assertTrue(run_id.startswith(PINNED_ONESHOT_RUN_PREFIX))
		self.assertIn("title", run_id)
		self.assertIn(SKEY, run_id, "the run id must still point back at its session")

	def test_an_unpinned_one_shot_is_not_labelled_pinned(self):
		"""A one-shot whose model resolved empty keeps its fallbacks, so its
		``next=none`` really would mean the chain is dead."""
		run_id = oneshot_run_id("prewarm", "abc", model="", provider=None)

		self.assertTrue(run_id.startswith(ONESHOT_RUN_PREFIX))
		self.assertFalse(run_id.startswith(PINNED_ONESHOT_RUN_PREFIX))

	def test_a_bare_provider_still_counts_as_pinned(self):
		"""openclaw drops the chain on either field, not just model."""
		run_id = oneshot_run_id("polish", "abc", model=None, provider="openai")

		self.assertTrue(run_id.startswith(PINNED_ONESHOT_RUN_PREFIX))

	def test_run_id_stays_clear_of_openclaws_own_key_namespace(self):
		"""openclaw parses ``exec-approval-followup:`` keys as an approval id."""
		for prefix in (PINNED_ONESHOT_RUN_PREFIX, ONESHOT_RUN_PREFIX):
			self.assertFalse(prefix.startswith("exec-approval-followup:"))

	def test_run_id_fits_the_console_truncation(self):
		"""openclaw truncates console fields at 200 chars; a clipped run id
		would not join back onto anything."""
		self.assertLess(len(oneshot_run_id("polish", SKEY, model="gpt-5.5", provider=None)), 200)


class TestPinnedTurnIsLogged(FrappeTestCase):
	"""The bench-side half: a line that says the chain was given up on purpose."""

	def test_fire_agent_records_the_pin(self):
		sess = _bare_session()
		sess._request = lambda method, params, *, timeout_s: {"payload": {"runId": "run1"}}

		with self.assertLogs(LOG, level="INFO") as logs:
			sess.fire_agent(SKEY, "warmup", "idem-1", model="gpt-5.5", provider="openai")

		line = "\n".join(logs.output)
		self.assertIn("idem-1", line, "the log must name the run id openclaw will report")
		self.assertIn("gpt-5.5", line)
		self.assertIn("fallbacks=none-by-design", line)

	def test_unpinned_fire_agent_stays_quiet(self):
		"""No pin, no lost chain, nothing to disambiguate."""
		sess = _bare_session()
		sess._request = lambda method, params, *, timeout_s: {"payload": {"runId": "run1"}}

		with self.assertNoLogs(LOG, level="INFO"):
			sess.fire_agent(SKEY, "warmup", "idem-1")

	def test_stream_agent_turn_records_the_pin(self):
		sess = _streaming_session()

		with self.assertLogs(LOG, level="INFO") as logs:
			list(sess.stream_agent_turn(SKEY, "hello", "idem-2", model="gpt-5.5"))

		line = "\n".join(logs.output)
		self.assertIn("idem-2", line)
		self.assertIn("fallbacks=none-by-design", line)

	def test_unpinned_stream_agent_turn_stays_quiet(self):
		sess = _streaming_session()

		with self.assertNoLogs(LOG, level="INFO"):
			list(sess.stream_agent_turn(SKEY, "hello", "idem-2"))


class TestOneShotsMintPinnedRunIds(FrappeTestCase):
	"""Each of the three one-shots has to opt in, or its own log stays unreadable."""

	def test_auto_title(self):
		from jarvis.chat import title

		sess = _pool_session("A Nice Title")
		with _checkout_yielding(sess):
			title._generate_via_gateway(
				"ws://gw.example",
				"Show me last month's overdue invoices",
				model="gpt-5.5-mini",
				provider=None,
			)

		self.assertTrue(sess.stream_agent_turn.call_args.args[2].startswith(PINNED_ONESHOT_RUN_PREFIX))

	def test_pattern_polish(self):
		from jarvis.learning import polish

		sess = _pool_session("- a polished bullet")
		settings = MagicMock()
		settings.agent_url = "http://gw.example"
		with (
			_checkout_yielding(sess),
			patch("jarvis.learning.polish.frappe.get_single", return_value=settings),
			patch(
				"jarvis.chat.turn_handler._resolve_model_and_provider",
				return_value=("gpt-5.5", None),
			),
		):
			polish._run_gateway_turn("- rewrite this bullet")

		self.assertTrue(sess.stream_agent_turn.call_args.args[2].startswith(PINNED_ONESHOT_RUN_PREFIX))

	def _warm(self, llm_model: str) -> str:
		"""Run one prewarm against a stubbed gateway; return the run id it minted."""
		from jarvis.chat import prewarm

		settings = MagicMock()
		settings.agent_url = "https://gw.example"
		settings.llm_model = llm_model
		settings.llm_auth_mode = "api_key"
		settings.llm_provider = "OpenAI"

		for key in (prewarm._warm_cooldown_key(), prewarm._warm_last_key()):
			frappe.cache().delete_value(key)
			self.addCleanup(frappe.cache().delete_value, key)

		sess = MagicMock()
		sess.create_session.return_value = "sk_throwaway"
		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=settings),
		):
			OC.connect.return_value = sess
			self.assertTrue(prewarm.warm_prefix())

		return sess.fire_agent.call_args.args[2]

	def test_prefix_prewarm(self):
		self.assertTrue(self._warm("gpt-5.5").startswith(PINNED_ONESHOT_RUN_PREFIX))

	def test_prewarm_without_a_configured_model_is_not_labelled_pinned(self):
		"""The label has to follow the RPC params, not the call site's intent:
		an empty llm_model sends no model, so the run keeps its fallbacks."""
		self.assertTrue(self._warm("").startswith(ONESHOT_RUN_PREFIX))
