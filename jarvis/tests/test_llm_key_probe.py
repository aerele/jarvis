"""Tests for jarvis.llm_key_probe - the pre-save "Test" probe for one API-key
LLM pool model row (Settings -> AI models -> Edit -> API key -> Test).

Covers: the GLM/Z.ai insufficient-balance case that motivated this module,
the SSRF guard rejecting a private/loopback base_url (exercised end-to-end
through the real jarvis.chat.link_fetch guard, mocking only
socket.getaddrinfo - never the guard itself), api_key scrubbing out of a
provider error body, the local-provider (ollama/vllm) disclaimer, and the
System-Manager/Jarvis-Admin gate on the whitelisted endpoint.

Run: bench --site <site> run-tests --module jarvis.tests.test_llm_key_probe
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import llm_key_probe
from jarvis.chat import link_fetch


def _addrinfo(ip: str):
	"""One socket.getaddrinfo-shaped tuple carrying `ip` at index [4][0]
	(same shape jarvis.tests.test_link_fetch uses)."""
	return [(2, 1, 6, "", (ip, 443))]


PUBLIC_IP = "93.184.216.34"  # example.com - genuinely public/routable.
PRIVATE_IP = "10.0.0.5"


class TestExtractProviderMessage(FrappeTestCase):
	"""_extract_provider_message: pulling a human-readable error out of a
	provider's JSON body - the whole point of this feature."""

	def test_glm_insufficient_balance_shape(self):
		# The exact z.ai body that motivated this module.
		body = b'{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'
		msg = llm_key_probe._extract_provider_message(body)
		self.assertEqual(msg, "Insufficient balance or no resource package. Please recharge.")

	def test_openai_shaped_error(self):
		body = b'{"error":{"message":"Incorrect API key provided.","type":"invalid_request_error"}}'
		self.assertEqual(llm_key_probe._extract_provider_message(body), "Incorrect API key provided.")

	def test_bare_string_error_field(self):
		body = b'{"error":"bad request"}'
		self.assertEqual(llm_key_probe._extract_provider_message(body), "bad request")

	def test_error_object_with_only_a_code_falls_back_to_code(self):
		body = b'{"error":{"code":"429"}}'
		self.assertEqual(llm_key_probe._extract_provider_message(body), "429")

	def test_non_json_body_returns_raw_text(self):
		body = b"<html>upstream timeout</html>"
		self.assertIn("upstream timeout", llm_key_probe._extract_provider_message(body))

	def test_undecodable_body_never_raises(self):
		msg = llm_key_probe._extract_provider_message(b"\xff\xfe\x00\x01")
		self.assertIsInstance(msg, str)


class TestScrub(FrappeTestCase):
	def test_strips_literal_api_key(self):
		out = llm_key_probe._scrub("your key sk-secret-123 is invalid", "sk-secret-123")
		self.assertNotIn("sk-secret-123", out)
		self.assertIn("***", out)

	def test_caps_length(self):
		out = llm_key_probe._scrub("x" * 5000, "")
		self.assertLessEqual(len(out), llm_key_probe._MAX_DETAIL_LEN + len("...(truncated)"))

	def test_blank_key_is_a_noop_replace(self):
		# An empty api_key must never turn every message into "***" via a
		# blanket str.replace("", ...).
		self.assertEqual(llm_key_probe._scrub("hello world", ""), "hello world")


class TestProviderKindAndRequestShape(FrappeTestCase):
	def test_anthropic_uses_x_api_key_header_and_messages_endpoint(self):
		req = llm_key_probe._build_request("anthropic", "https://api.anthropic.com", "claude-x", "sk-a")
		self.assertEqual(req["url"], "https://api.anthropic.com/v1/messages")
		self.assertEqual(req["headers"]["x-api-key"], "sk-a")
		self.assertNotIn("Authorization", req["headers"])

	def test_gemini_key_rides_in_a_header_not_the_url(self):
		req = llm_key_probe._build_request(
			"gemini", "https://generativelanguage.googleapis.com", "gemini-2.5-pro", "sk-g"
		)
		self.assertNotIn("sk-g", req["url"])
		self.assertEqual(req["headers"]["x-goog-api-key"], "sk-g")

	def test_openai_kind_is_bearer_chat_completions(self):
		req = llm_key_probe._build_request("openai", "https://api.z.ai/api/paas/v4", "glm-4.6", "sk-z")
		self.assertEqual(req["url"], "https://api.z.ai/api/paas/v4/chat/completions")
		self.assertEqual(req["headers"]["Authorization"], "Bearer sk-z")

	def test_glm_zai_label_normalizes_into_the_openai_kind(self):
		# GLM / Z.ai has no native Bifrost provider - pool_serialize.normalize_provider
		# maps its label to "openai_compat", which must still speak the OpenAI wire
		# protocol (that's what z.ai's own API actually is).
		self.assertEqual(
			llm_key_probe._provider_kind(llm_key_probe.normalize_provider("GLM / Z.ai")), "openai"
		)

	def test_ollama_and_vllm_are_flagged_local(self):
		self.assertIn(llm_key_probe.normalize_provider("Ollama (local)"), llm_key_probe.LOCAL_PROVIDER_IDS)
		self.assertIn(llm_key_probe.normalize_provider("vLLM (local)"), llm_key_probe.LOCAL_PROVIDER_IDS)

	def test_public_provider_not_flagged_local(self):
		self.assertNotIn(llm_key_probe.normalize_provider("OpenAI"), llm_key_probe.LOCAL_PROVIDER_IDS)


class TestProbeApiKey(FrappeTestCase):
	"""probe_api_key with link_fetch.request_pinned mocked - no real network."""

	def test_missing_model_fails_fast_with_no_network_call(self):
		with mock.patch.object(link_fetch, "request_pinned") as rp:
			result = llm_key_probe.probe_api_key("OpenAI", "", "sk-x", "https://api.openai.com/v1")
		rp.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["checks"][0]["check"], "input")

	def test_missing_api_key_fails_fast(self):
		with mock.patch.object(link_fetch, "request_pinned") as rp:
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "", "https://api.openai.com/v1")
		rp.assert_not_called()
		self.assertFalse(result["ok"])

	def test_missing_base_url_fails_fast(self):
		with mock.patch.object(link_fetch, "request_pinned") as rp:
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "sk-x", "")
		rp.assert_not_called()
		self.assertFalse(result["ok"])

	def test_a_message_extraction_crash_never_escapes_probe_api_key(self):
		"""_extract_provider_message is best-effort against a body from whatever
		the customer-supplied base_url points to - probe_api_key's documented
		"NEVER raises" contract must hold even if that helper itself blows up
		on a pathological response (code-review finding: only ValueError/
		TypeError were caught inside the helper; anything else used to
		propagate all the way out as an unhandled 500)."""
		with (
			mock.patch.object(link_fetch, "request_pinned", return_value=(400, {}, b"whatever")),
			mock.patch.object(llm_key_probe, "_extract_provider_message", side_effect=RecursionError("boom")),
		):
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "sk-x", "https://api.openai.com/v1")
		self.assertFalse(result["ok"])
		self.assertIn("HTTP 400", result["checks"][-1]["detail"])

	def test_200_response_is_ok(self):
		with mock.patch.object(
			link_fetch,
			"request_pinned",
			return_value=(200, {}, b'{"choices":[{"message":{"content":"hi"}}]}'),
		):
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "sk-x", "https://api.openai.com/v1")
		self.assertTrue(result["ok"])
		self.assertEqual(result["provider"], "openai")
		self.assertFalse(result["local_endpoint"])

	def test_glm_insufficient_balance_surfaces_the_real_reason(self):
		"""THE motivating case: a valid key against a zero-balance z.ai account
		must surface z.ai's own message, not a bare "failed"."""
		body = b'{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'
		with mock.patch.object(link_fetch, "request_pinned", return_value=(400, {}, body)):
			result = llm_key_probe.probe_api_key(
				"GLM / Z.ai", "glm-4.6", "sk-real-but-unpaid", "https://api.z.ai/api/paas/v4"
			)
		self.assertFalse(result["ok"])
		detail = result["checks"][-1]["detail"]
		self.assertIn("Insufficient balance", detail)
		self.assertIn("recharge", detail)
		self.assertNotIn("sk-real-but-unpaid", detail)

	def test_provider_error_body_never_leaks_the_api_key(self):
		# A provider that (badly) echoes the credential back in its error body.
		body = b'{"error":{"message":"key sk-super-secret-999 is not authorized"}}'
		with mock.patch.object(link_fetch, "request_pinned", return_value=(401, {}, body)):
			result = llm_key_probe.probe_api_key(
				"OpenAI", "gpt-4o", "sk-super-secret-999", "https://api.openai.com/v1"
			)
		detail = result["checks"][-1]["detail"]
		self.assertNotIn("sk-super-secret-999", detail)
		self.assertIn("***", detail)

	def test_ssrf_blocked_endpoint_gets_a_locality_aware_message_for_local_providers(self):
		with mock.patch.object(
			link_fetch,
			"request_pinned",
			side_effect=link_fetch.LinkFetchError("Host resolves to a disallowed address"),
		):
			result = llm_key_probe.probe_api_key(
				"Ollama (local)", "llama3", "unused", "http://127.0.0.1:11434/v1"
			)
		self.assertFalse(result["ok"])
		self.assertTrue(result["local_endpoint"])
		detail = result["checks"][-1]["detail"]
		self.assertIn("container", detail)

	def test_ssrf_blocked_endpoint_for_a_non_local_provider_still_fails_cleanly(self):
		with mock.patch.object(
			link_fetch,
			"request_pinned",
			side_effect=link_fetch.LinkFetchError("Host resolves to a disallowed address"),
		):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "x", "sk-x", "http://169.254.169.254/v1"
			)
		self.assertFalse(result["ok"])
		self.assertFalse(result["local_endpoint"])
		# Non-local providers don't get the ollama/vllm-specific container wording.
		self.assertNotIn("container", result["checks"][-1]["detail"])


class TestProbeApiKeySsrfEndToEnd(FrappeTestCase):
	"""Exercises the REAL jarvis.chat.link_fetch guard (only socket.getaddrinfo
	is mocked - never request_pinned/the guard itself) so this asserts the
	actual SSRF rejection wiring, not just that probe_api_key handles a
	pre-canned LinkFetchError."""

	def test_private_ip_base_url_is_rejected_before_any_socket_open(self):
		with mock.patch("socket.getaddrinfo", return_value=_addrinfo(PRIVATE_IP)):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "some-model", "sk-x", "http://internal.example.com/v1"
			)
		self.assertFalse(result["ok"])
		detail = result["checks"][-1]["detail"]
		self.assertTrue(detail)

	def test_metadata_ip_base_url_is_rejected(self):
		with mock.patch("socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "some-model", "sk-x", "http://metadata.example.com/v1"
			)
		self.assertFalse(result["ok"])

	def test_unresolvable_host_is_rejected_not_raised(self):
		with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "some-model", "sk-x", "http://does-not-resolve.invalid/v1"
			)
		self.assertFalse(result["ok"])

	def test_non_http_scheme_is_rejected(self):
		result = llm_key_probe.probe_api_key("OpenAI-Compatible", "some-model", "sk-x", "file:///etc/passwd")
		self.assertFalse(result["ok"])


class TestTestLlmApiKeyGating(FrappeTestCase):
	"""The whitelisted endpoint: gated the same as save_llm_pool (Jarvis
	Admin / System Manager), and always attaches a caveat."""

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_guest_is_rejected(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			llm_key_probe.test_llm_api_key("OpenAI", "gpt-4o", "sk-x", "https://api.openai.com/v1")

	def test_administrator_gets_a_result_with_a_caveat(self):
		frappe.set_user("Administrator")
		with mock.patch.object(link_fetch, "request_pinned", return_value=(200, {}, b"{}")):
			result = llm_key_probe.test_llm_api_key("OpenAI", "gpt-4o", "sk-x", "https://api.openai.com/v1")
		self.assertTrue(result["ok"])
		self.assertIn("caveat", result)
		self.assertTrue(result["caveat"])

	def test_local_provider_caveat_mentions_the_container(self):
		frappe.set_user("Administrator")
		with mock.patch.object(link_fetch, "request_pinned", return_value=(200, {}, b"{}")):
			result = llm_key_probe.test_llm_api_key(
				"Ollama (local)", "llama3", "unused", "http://host.docker.internal:11434/v1"
			)
		self.assertIn("container", result["caveat"])
