"""Issue #738. The auto-title and suggestions lanes pin a model+provider
(#531, dropping failover on purpose) and then swallow every failure so a
title/suggestions blip never breaks chat. That swallowing is correct - but it
means a credential/provider-auth fault on ONLY the pinned model produced no
signal at all: not the customer (by design) and not an operator either.

Two failure shapes reach ``_generate_via_gateway`` and both need the same
treatment:

  - an in-run provider failure, which the agent runtime reports as a lifecycle
    ``phase == "error"`` event carrying the provider's own error text and
    never raises (see agent_client.failed_final_error) - before this fix
    that text was read by nobody, auth fault or not;
  - a raised exception (connect/ack rejection, WS drop, timeout).

For each shape: an AUTH fault (401/403, "unauthorized", a provider's own
auth vocabulary) must emit ONE distinct, greppable Error Log
(jarvis.chat.title._log_pinned_lane_auth_fault) while the lane still falls
back exactly as before; an ORDINARY failure must stay swallowed with no new
signal, byte-identical to pre-#738 behaviour. A stale device-token pairing
fault (a whole-connection auth failure with its own self-heal path, not a
pinned-model credential problem) must NOT be classified as a #738 auth fault
even though its own text says "unauthorized".

SCOPE NOTE: the transport is stubbed, as everywhere else in this suite (see
test_throwaway_session_reclaim.py) - these tests assert what the bench does
with the events/exceptions the agent runtime can hand it, not the runtime's own behaviour.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import AgentUnreachableError

SKEY = "agent:main:dashboard:throwaway-1"

DISTINCT_TITLE_PREFIX = "jarvis.chat."
GENERIC_TITLE_AUTOTITLE = "auto-title: gateway generation failed"
GENERIC_TITLE_SUGGEST = "prompt suggestions: gateway generation failed"


def _pool_session(*, events=None, raises=None) -> MagicMock:
	"""A pooled AgentSession stub whose run is never reported active, so the
	reclaim in ``finally`` never blocks these tests on the busy-probe path
	covered by test_throwaway_session_reclaim.py."""
	sess = MagicMock()
	sess.create_session.return_value = SKEY
	sess.is_run_active.return_value = False
	if raises is not None:
		sess.stream_agent_turn.side_effect = raises
	else:
		sess.stream_agent_turn.return_value = iter(events or [])
	return sess


@contextlib.contextmanager
def _checkout_yielding(sess):
	@contextlib.contextmanager
	def _cm(_gateway_url):
		yield sess

	with patch("jarvis.chat.agent_session_pool.checkout", _cm):
		yield


def _distinct_titles(log_error_mock) -> list[str]:
	return [
		c.kwargs.get("title")
		for c in log_error_mock.call_args_list
		if "pinned lane auth fault" in (c.kwargs.get("title") or "")
	]


class TestTitleLanePinnedAuthSignal(FrappeTestCase):
	def _generate(self, sess):
		from jarvis.chat import title

		with _checkout_yielding(sess):
			return title._generate_via_gateway(
				"ws://gw.example",
				"Show me last month's overdue invoices",
				model="gpt-5.5-mini",
				provider="OpenAI",
			)

	def test_lifecycle_auth_fault_emits_signal_and_still_falls_back(self):
		sess = _pool_session(
			events=[
				{
					"kind": "lifecycle",
					"phase": "error",
					"error": "OpenAI API error (401): Incorrect API key provided",
				}
			]
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, "", "chat/title must still fall back gracefully")
		titles = _distinct_titles(log_error)
		self.assertEqual(len(titles), 1)
		self.assertTrue(titles[0].startswith(DISTINCT_TITLE_PREFIX))
		self.assertIn("title", titles[0])

	def test_lifecycle_ordinary_failure_stays_silent(self):
		"""No exception, no auth text - this is the pre-#738 case where nothing
		was ever logged at all. Must stay exactly that way: no new signal."""
		sess = _pool_session(
			events=[
				{
					"kind": "lifecycle",
					"phase": "error",
					"error": "Google Generative AI API error (429): quota exceeded",
				}
			]
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, "")
		log_error.assert_not_called()

	def test_exception_auth_fault_emits_signal_alongside_the_generic_log(self):
		sess = _pool_session(
			raises=AgentUnreachableError(
				"agent rejected: INVALID_REQUEST: authentication_error: invalid api key"
			)
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, "")
		all_titles = [c.kwargs.get("title") for c in log_error.call_args_list]
		self.assertIn(GENERIC_TITLE_AUTOTITLE, all_titles, "existing swallow behaviour must be untouched")
		self.assertEqual(len(_distinct_titles(log_error)), 1)

	def test_exception_ordinary_failure_only_logs_the_generic_row(self):
		sess = _pool_session(raises=AgentUnreachableError("agent turn timed out before lifecycle end"))

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, "")
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs.get("title"), GENERIC_TITLE_AUTOTITLE)
		self.assertEqual(_distinct_titles(log_error), [])

	def test_stale_device_pairing_fault_is_not_a_pinned_lane_auth_fault(self):
		"""A whole-connection auth failure (#525/#535's own self-heal path),
		not a pinned-model credential problem - must not widen #738's signal."""
		sess = _pool_session(
			raises=AgentUnreachableError(
				"connect rejected: INVALID_REQUEST: unauthorized: device token mismatch "
				"(rotate/reissue device token)",
				details={"authReason": "device_token_mismatch"},
			)
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, "")
		self.assertEqual(_distinct_titles(log_error), [])


class TestSuggestionsLanePinnedAuthSignal(FrappeTestCase):
	def _generate(self, sess):
		from jarvis.chat import suggestions

		with _checkout_yielding(sess):
			return suggestions._generate_via_gateway(
				"ws://gw.example",
				["Timesheet Report Date Range", "Purchase Order Approval Flow"],
				model="gpt-5.5-mini",
				provider="OpenAI",
			)

	def test_lifecycle_auth_fault_emits_signal_and_still_falls_back(self):
		sess = _pool_session(
			events=[
				{
					"kind": "lifecycle",
					"phase": "error",
					"error": "Anthropic API error (403): permission_denied",
				}
			]
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, [], "the suggestions strip must still fall back gracefully")
		titles = _distinct_titles(log_error)
		self.assertEqual(len(titles), 1)
		self.assertTrue(titles[0].startswith(DISTINCT_TITLE_PREFIX))
		self.assertIn("suggestions", titles[0])

	def test_lifecycle_ordinary_failure_stays_silent(self):
		sess = _pool_session(
			events=[{"kind": "lifecycle", "phase": "error", "error": "context overflow: prompt too large"}]
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, [])
		log_error.assert_not_called()

	def test_exception_auth_fault_emits_signal_alongside_the_generic_log(self):
		sess = _pool_session(
			raises=AgentUnreachableError("agent rejected: INVALID_REQUEST: 401 unauthorized")
		)

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, [])
		all_titles = [c.kwargs.get("title") for c in log_error.call_args_list]
		self.assertIn(GENERIC_TITLE_SUGGEST, all_titles)
		self.assertEqual(len(_distinct_titles(log_error)), 1)

	def test_exception_ordinary_failure_only_logs_the_generic_row(self):
		sess = _pool_session(raises=AgentUnreachableError("agent turn timed out before lifecycle end"))

		with patch("frappe.log_error") as log_error:
			result = self._generate(sess)

		self.assertEqual(result, [])
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs.get("title"), GENERIC_TITLE_SUGGEST)


class TestPinnedLaneAuthFaultReachesTheAdminFeed(FrappeTestCase):
	"""``_log_pinned_lane_auth_fault``'s shape has two contracts with
	``jarvis.api_errors`` that the tests above never exercise (they only see
	that ``frappe.log_error`` was called, not what the error-forwarding
	pipeline does with the row afterwards): the dotted ``jarvis.chat.<lane>:``
	title is what lets ``is_jarvis_error`` recognise it on the lifecycle path
	(no traceback), and the synthetic trailing exception line is what keeps
	its admin-feed fingerprint from folding into every other traceback-less
	log call here. Pinning both directly means a future reformat of the
	message can't silently sever the signal from the feed while every test
	above still passes."""

	def _logged(self, lane: str) -> tuple[str, str]:
		from jarvis.chat.title import _log_pinned_lane_auth_fault

		with patch("frappe.log_error") as log_error:
			_log_pinned_lane_auth_fault(
				lane,
				"OpenAI API error (401): Incorrect API key provided",
				model="gpt-5.5",
				provider="OpenAI",
			)
		call = log_error.call_args
		return call.kwargs["title"], call.kwargs["message"]

	def test_title_row_is_recognised_as_jarvis_origin(self):
		from jarvis import api_errors

		title, message = self._logged("title")
		self.assertTrue(api_errors.is_jarvis_error(title, message))

	def test_suggestions_row_is_recognised_as_jarvis_origin(self):
		from jarvis import api_errors

		title, message = self._logged("suggestions")
		self.assertTrue(api_errors.is_jarvis_error(title, message))

	def test_each_lane_gets_a_distinct_admin_feed_fingerprint(self):
		from jarvis import api_errors

		_, title_message = self._logged("title")
		_, suggestions_message = self._logged("suggestions")
		title_class, _, _ = api_errors._parse_traceback(title_message)
		suggestions_class, _, _ = api_errors._parse_traceback(suggestions_message)

		self.assertEqual(title_class, "JarvisPinnedTitleAuthError")
		self.assertEqual(suggestions_class, "JarvisPinnedSuggestionsAuthError")
		self.assertNotEqual(title_class, suggestions_class)
