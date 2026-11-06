"""Unit tests for jarvis.triggers.llm_task_client - the tenant agent gateway
``llm-task`` HTTP client (jarvis-admin-v2#597). No DB, no network: ``requests``
and the Jarvis Settings lookup are mocked.
"""

import json
import unittest
from unittest.mock import Mock, patch

from jarvis.triggers import llm_task_client as lt


def _streamed_response(status=200, body=b"{}"):
	"""A Mock mimicking requests' streamed response context manager (same
	shape used by jarvis.chat.generated_media's fetch_media tests)."""
	resp = Mock()
	resp.status_code = status
	resp.iter_content = Mock(return_value=[body] if body else [b""])
	cm = Mock()
	cm.__enter__ = Mock(return_value=resp)
	cm.__exit__ = Mock(return_value=False)
	return cm


def _settings(agent_url="ws://agent.host:9000"):
	settings = Mock()
	settings.agent_url = agent_url
	return settings


def _sent_json(kwargs):
	"""The JSON body the plugin route was sent (the exact signed bytes)."""
	return json.loads(kwargs["data"])


class TestLLMTaskComplete(unittest.TestCase):
	def setUp(self):
		# The signer and the session claim are other modules' concerns (J3a / J1); this
		# file pins what the client sends to the plugin route.
		for target, kwargs in (
			("jarvis.chat.plugin_routes.signed_route_headers", {"return_value": {"X-Jarvis-Token": "k"}}),
			("jarvis.chat.session_claims.claim", {}),
		):
			patcher = patch(target, **kwargs)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_success_prefers_string_details_json(self):
		body = json.dumps(
			{
				"ok": True,
				"result": {
					"content": [{"type": "text", "text": '"escaped text"'}],
					"details": {"json": "All good.", "provider": "anthropic", "model": "x"},
				},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=body)) as post,
		):
			out = lt.llm_task_complete("do the thing", "<untrusted-data>{}</untrusted-data>")
		self.assertEqual(out, "All good.")
		args, kwargs = post.call_args
		self.assertEqual(args, ("POST", "http://agent.host:9000/jarvis/llm-task"))
		self.assertNotIn("Authorization", kwargs["headers"])
		self.assertFalse(kwargs["allow_redirects"])
		self.assertTrue(kwargs["stream"])
		payload = _sent_json(kwargs)
		self.assertEqual(list(payload), ["args"])
		self.assertEqual(
			payload["args"],
			{
				"prompt": "do the thing",
				"input": "<untrusted-data>{}</untrusted-data>",
				"schema": {"type": "string"},
				"maxTokens": lt._MAX_TOKENS,
				"timeoutMs": 60_000,
			},
		)
		# provider/model must never be sent - overriding the tenant's own
		# configured model must stay impossible from this client.
		self.assertNotIn("provider", payload["args"])
		self.assertNotIn("model", payload["args"])
		# the HTTP read timeout is kept above the runtime's timeoutMs so the
		# client never gives up before the gateway would.
		self.assertGreater(kwargs["timeout"][1], payload["args"]["timeoutMs"] / 1000)

	def test_custom_timeout_scales_timeoutms_and_stays_below_http_timeout(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch(
				"requests.request", return_value=_streamed_response(body=b'{"ok": true, "result": {}}')
			) as post,
		):
			with self.assertRaises(lt.LLMTaskError):  # empty result -> raises, timing still checked below
				lt.llm_task_complete("p", "i", timeout=30)
		args, kwargs = post.call_args
		self.assertEqual(_sent_json(kwargs)["args"]["timeoutMs"], 30_000)
		self.assertGreater(kwargs["timeout"][1], 30)

	def test_falls_back_to_content_text_when_json_not_a_string(self):
		body = json.dumps(
			{
				"ok": True,
				"result": {
					"content": [{"type": "text", "text": "prose reply"}],
					"details": {"json": {"nested": True}},
				},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=body)),
		):
			out = lt.llm_task_complete("p", "i")
		self.assertEqual(out, "prose reply")

	def _object_call(self, result):
		body = json.dumps({"ok": True, "result": result}).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=body)) as post,
		):
			out = lt.llm_task_complete("p", "i", expect_object=True)
		return out, _sent_json(post.call_args.kwargs)["args"]

	def test_expect_object_returns_details_json_dict_and_asks_for_the_union_schema(self):
		out, args = self._object_call(
			{"content": [{"type": "text", "text": "ignored"}], "details": {"json": {"finding": "ok"}}}
		)
		self.assertEqual(out, {"finding": "ok"})
		self.assertEqual(args["schema"], {"type": ["object", "string"]})

	def test_expect_object_parses_the_content_text_when_details_json_is_not_a_dict(self):
		out, _args = self._object_call(
			{
				"content": [{"type": "text", "text": '{"lookup": {"tool": "get", "args": {}}}'}],
				"details": {"json": "str"},
			}
		)
		self.assertEqual(out, {"lookup": {"tool": "get", "args": {}}})

	def test_expect_object_returns_plain_text_when_the_model_ignored_the_contract(self):
		out, _args = self._object_call({"content": [{"type": "text", "text": "just prose"}]})
		self.assertEqual(out, "just prose")

	def test_expect_object_string_details_json_becomes_the_finding(self):
		from jarvis.triggers.lookups import parse_reply

		out, _args = self._object_call({"details": {"json": "Looks fine to me."}})
		self.assertEqual(out, "Looks fine to me.")
		self.assertEqual(parse_reply(out)[:2], ("finding", "Looks fine to me."))

	def test_default_call_still_asks_for_a_string_schema(self):
		body = json.dumps({"ok": True, "result": {"details": {"json": "fine"}}}).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=body)) as post,
		):
			self.assertEqual(lt.llm_task_complete("p", "i"), "fine")
		self.assertEqual(_sent_json(post.call_args.kwargs)["args"]["schema"], {"type": "string"})

	def test_tool_not_available_raises_typed_error(self):
		body = json.dumps(
			{
				"ok": False,
				"status": 404,
				"error": {"type": "not_found", "message": "Tool not available: llm-task"},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(status=404, body=body)),
		):
			with self.assertRaises(lt.LLMTaskNotAvailableError):
				lt.llm_task_complete("p", "i")

	def test_404_with_non_json_body_still_raises_not_available(self):
		# An older runtime that doesn't know the /tools/invoke route at all can
		# 404 with a plain-text/HTML body, not the {"error": {...}} envelope -
		# the status code alone must still resolve to "not available" so the
		# caller falls back instead of treating it as a generic parse failure.
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch(
				"requests.request",
				return_value=_streamed_response(status=404, body=b"<html>not found</html>"),
			),
		):
			with self.assertRaises(lt.LLMTaskNotAvailableError):
				lt.llm_task_complete("p", "i")

	def test_other_gateway_error_raises_base_error_not_not_available(self):
		body = json.dumps(
			{"ok": False, "status": 400, "error": {"type": "invalid_request", "message": "bad args"}}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(status=400, body=body)),
		):
			with self.assertRaises(lt.LLMTaskError) as ctx:
				lt.llm_task_complete("p", "i")
		self.assertNotIsInstance(ctx.exception, lt.LLMTaskNotAvailableError)
		self.assertIn("bad args", str(ctx.exception))

	def test_missing_agent_url_raises_without_network_call(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings(agent_url="")),
			patch("requests.request") as post,
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")
		post.assert_not_called()

	def test_plugin_refusal_raises_fixed_error_without_fallback_signal(self):
		for status in (401, 403):
			with (
				patch("frappe.get_cached_doc", return_value=_settings()),
				patch(
					"requests.request",
					return_value=_streamed_response(status=status, body=b'{"error": "unauthorized"}'),
				),
			):
				with self.assertRaises(lt.LLMTaskError) as ctx:
					lt.llm_task_complete("p", "i")
			self.assertNotIsInstance(ctx.exception, lt.LLMTaskNotAvailableError)

	def test_oversize_response_raises(self):
		big = b"x" * (lt._MAX_RESPONSE_BYTES + 1)
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=big)),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_non_json_response_raises(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", return_value=_streamed_response(body=b"not json")),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_network_error_raises_base_error(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", side_effect=RuntimeError("boom")),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_settings_lookup_failure_propagates_untyped(self):
		# Deliberately NOT wrapped in LLMTaskError: frappe.get_cached_doc("Jarvis
		# Settings") failing (DoesNotExistError, a DB error) is outside this
		# client's HTTP-transport contract. jarvis.triggers.llm_action._complete
		# is the layer responsible for catching this into a Failed activity.
		with patch("frappe.get_cached_doc", side_effect=RuntimeError("db unavailable")):
			with self.assertRaises(RuntimeError):
				lt.llm_task_complete("p", "i")

	def test_headers_never_echoed_into_raised_error_message(self):
		# The client builds its own error messages from the parsed gateway
		# payload / a fixed literal - never from the request object (which
		# would carry the signing headers - so a raised error can never echo a key.
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.request", side_effect=RuntimeError("super-secret-token")),
		):
			with self.assertRaises(lt.LLMTaskError) as ctx:
				lt.llm_task_complete("p", "i")
		self.assertNotIn("super-secret-token", str(ctx.exception))


_EXPIRED = "Anthropic: not logged in. Please run /login"
_POOL = [
	{"provider": "anthropic", "model": "claude-a"},
	{"provider": "openai-1", "model": "gpt-b"},
	{"provider": "xai", "model": "grok-c"},
]


class TestLLMTaskFailover(unittest.TestCase):
	"""jarvis-admin-v2#826: a trigger whose primary model's sign-in died must walk the pool."""

	def _run(self, attempts, *, refs=_POOL, expired=(), serving=None, **kwargs):
		"""``attempts``: one entry per llm_task_complete call - a str reply or an Exception."""
		calls = []

		def fake(prompt, input_payload, timeout=60, expect_object=False, model_ref=None):
			calls.append(model_ref)
			out = attempts[len(calls) - 1]
			if isinstance(out, Exception):
				raise out
			return out

		upstream = serving or (
			lambda model, provider: {"claude-a": "anthropic", "gpt-b": "openai"}.get(model, "")
		)
		refs_patch = (
			patch.object(lt, "pool_model_refs", side_effect=refs)
			if isinstance(refs, Exception)
			else patch.object(lt, "pool_model_refs", return_value=refs)
		)
		with (
			patch.object(lt, "llm_task_complete", side_effect=fake),
			refs_patch,
			patch(
				"jarvis.subscription_health.expired_entries", return_value=[{"upstream": u} for u in expired]
			),
			patch("jarvis.subscription_health.upstream_for_serving", side_effect=upstream),
			patch("jarvis.subscription_health.note_turn_error") as note,
		):
			try:
				return lt.llm_task_complete_with_failover("p", "i", **kwargs), calls, note, None
			except lt.LLMTaskError as e:
				return None, calls, note, e

	def test_expired_primary_retries_on_next_pool_model_and_feeds_health(self):
		out, calls, note, err = self._run([lt.LLMTaskError(_EXPIRED), "found it"])
		self.assertEqual(out, "found it")
		self.assertIsNone(err)
		self.assertEqual(calls, [None, {"provider": "openai-1", "model": "gpt-b"}])
		note.assert_called_once()
		self.assertEqual(note.call_args.args[1], "subscription-expired")

	def test_upstream_already_flagged_expired_is_skipped(self):
		out, calls, _note, _err = self._run([lt.LLMTaskError(_EXPIRED), "from grok"], expired=("openai",))
		self.assertEqual(out, "from grok")
		self.assertEqual(calls, [None, {"provider": "xai", "model": "grok-c"}])

	def test_flagged_primary_is_not_called_at_all(self):
		out, calls, _note, _err = self._run(["from gpt"], expired=("anthropic",))
		self.assertEqual(out, "from gpt")
		self.assertEqual(calls, [{"provider": "openai-1", "model": "gpt-b"}])

	def test_override_refused_keeps_the_original_error(self):
		refused = lt.LLMTaskError("Plugin LLM completion cannot override the target model.")
		_out, calls, _note, err = self._run([lt.LLMTaskError(_EXPIRED), refused])
		self.assertEqual(str(err), _EXPIRED)
		self.assertEqual(len(calls), 2)

	def test_pool_route_missing_keeps_the_original_error(self):
		_out, calls, _note, err = self._run(
			[lt.LLMTaskError(_EXPIRED)], refs=lt.LLMTaskNotAvailableError("not found")
		)
		self.assertEqual(str(err), _EXPIRED)
		self.assertEqual(calls, [None])

	def test_non_credential_error_does_not_retry(self):
		_out, calls, note, err = self._run([lt.LLMTaskError("llm-task request timed out")])
		self.assertEqual(str(err), "llm-task request timed out")
		self.assertEqual(calls, [None])
		note.assert_not_called()

	def test_every_model_failing_raises_the_first_error(self):
		attempts = [lt.LLMTaskError(_EXPIRED)] * 3
		_out, calls, _note, err = self._run(attempts)
		self.assertEqual(str(err), _EXPIRED)
		self.assertEqual(len(calls), 3)

	def test_not_available_is_never_retried(self):
		_out, calls, _note, err = self._run([lt.LLMTaskNotAvailableError("tool not available")])
		self.assertIsInstance(err, lt.LLMTaskNotAvailableError)
		self.assertEqual(calls, [None])

	def test_model_ref_is_sent_as_provider_and_model(self):
		body = json.dumps({"ok": True, "result": {"details": {"json": "ok"}}}).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("jarvis.chat.plugin_routes.signed_route_headers", return_value={"X-Jarvis-Token": "k"}),
			patch("jarvis.chat.session_claims.claim"),
			patch("requests.request", return_value=_streamed_response(body=body)) as post,
		):
			lt.llm_task_complete("p", "i", model_ref={"provider": "openai-1", "model": "gpt-b"})
		args = _sent_json(post.call_args.kwargs)["args"]
		self.assertEqual((args["provider"], args["model"]), ("openai-1", "gpt-b"))

	def test_pool_model_refs_reads_the_plugin_route_in_pool_order(self):
		body = json.dumps(
			{
				"ok": True,
				"models": [
					{"ref": "a-0/m1", "provider": "a-0", "model": "m1"},
					{"ref": "b-1/m2", "provider": "b-1", "model": "m2"},
				],
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("jarvis.chat.plugin_routes.signed_route_headers", return_value={"X-Jarvis-Token": "k"}),
			patch("jarvis.chat.session_claims.claim"),
			patch("requests.request", return_value=_streamed_response(body=body)) as post,
		):
			refs = lt.pool_model_refs()
		self.assertEqual(post.call_args.args, ("POST", "http://agent.host:9000/jarvis/llm-task/models"))
		self.assertEqual(refs, [{"provider": "a-0", "model": "m1"}, {"provider": "b-1", "model": "m2"}])

	def test_pool_model_refs_404_is_not_available(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("jarvis.chat.plugin_routes.signed_route_headers", return_value={"X-Jarvis-Token": "k"}),
			patch("jarvis.chat.session_claims.claim"),
			patch("requests.request", return_value=_streamed_response(status=404, body=b"nope")),
		):
			with self.assertRaises(lt.LLMTaskNotAvailableError):
				lt.pool_model_refs()


class TestEvaluateUsesFailover(unittest.TestCase):
	def test_trigger_evaluation_goes_through_the_failover_entry_point(self):
		from jarvis.triggers import llm_action

		with patch.object(lt, "llm_task_complete_with_failover", return_value="finding") as f:
			self.assertEqual(llm_action._evaluate("p", "i", []), ("finding", None))
		f.assert_called_once()


if __name__ == "__main__":
	unittest.main()
