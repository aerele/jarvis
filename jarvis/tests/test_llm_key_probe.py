"""Tests for jarvis.llm_key_probe - the pre-save "Test" probe for one API-key
LLM pool model row (Settings -> AI models -> Edit -> API key -> Test).

Covers: the GLM/Z.ai insufficient-balance case that motivated this module,
the SSRF guard rejecting a private/loopback base_url (exercised end-to-end
through the real jarvis.chat.link_fetch guard, mocking only
socket.getaddrinfo - never the guard itself), api_key scrubbing out of a
provider error body, the local-provider (ollama/vllm) disclaimer, and the
System-Manager/Jarvis-Admin gate on the whitelisted endpoint.

Plus the two defects the "Test" button shipped with:

  #680 - an endpoint this bench cannot reach is "unverified", not "failed".
         Driven through the real guard, because the claim is about which
         failures the network layer actually produces.
  #679 - a saved key can be probed without retyping it. Driven through a real
         encrypted doc save, because the claim is that the SERVER reads it.

Run: bench --site <site> run-tests --module jarvis.tests.test_llm_key_probe
"""

from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import llm_key_probe
from jarvis.chat import link_fetch
from jarvis.jarvis.pool_serialize import stored_api_keys_by_provider
from jarvis.tests.test_unified_llm_config import _RT3SettingsTestCase


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

	def test_ssrf_blocked_endpoint_is_unverified_for_local_providers(self):
		# Was asserted as a plain failure. An address the guard refuses is one the
		# bench declined to dial, so it is "we could not check", not "your key is
		# bad" - and the wording has to point at the container, which CAN dial it.
		with mock.patch.object(
			link_fetch,
			"request_pinned",
			side_effect=link_fetch.LinkFetchError(
				"Host resolves to a disallowed address", kind=link_fetch.ERR_BLOCKED_ADDRESS
			),
		):
			result = llm_key_probe.probe_api_key(
				"Ollama (local)", "llama3", "unused", "http://127.0.0.1:11434/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)
		self.assertFalse(result["ok"])
		self.assertTrue(result["local_endpoint"])
		self.assertIn("container", result["checks"][-1]["detail"])

	def test_ssrf_blocked_endpoint_is_unverified_for_a_non_local_provider_too(self):
		# The old assertion here was assertNotIn("container", ...): the container
		# hedge used to be reserved for ollama/vllm. That was the bug - the network
		# gap is a property of the ADDRESS, not of the provider id, and a customer
		# pointing OpenAI-Compatible at their own LAN hits exactly the same wall.
		with mock.patch.object(
			link_fetch,
			"request_pinned",
			side_effect=link_fetch.LinkFetchError(
				"Host resolves to a disallowed address", kind=link_fetch.ERR_BLOCKED_ADDRESS
			),
		):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "x", "sk-x", "http://169.254.169.254/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)
		self.assertFalse(result["ok"])
		self.assertFalse(result["local_endpoint"])
		self.assertIn("container", result["checks"][-1]["detail"])


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


class TestUnreachableIsNotAFailure(FrappeTestCase):
	"""#680: a base_url only the tenant's container can reach answers HTTP 200
	from inside the container and nothing at all from this bench. Reporting that
	as "Test failed." told the customer their working configuration was broken,
	and they did not click Save.

	Every case here drives the REAL jarvis.chat.link_fetch guard, mocking only
	socket.getaddrinfo / the socket open - never probe_api_key's own classifier,
	and never request_pinned. That matters: the whole claim is about WHICH
	failures the network layer really produces, so a test that hands the probe a
	pre-canned kind would be asserting its own fixture.
	"""

	def test_a_host_this_bench_cannot_resolve_is_unverified(self):
		# The literal #680 repro: host.docker.internal has no answer out here.
		with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "gpt-4o", "sk-x", "http://host.docker.internal:9000/openai/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)
		self.assertFalse(result["ok"])
		self.assertIn("container", result["checks"][-1]["detail"])

	def test_a_private_address_is_unverified(self):
		with mock.patch("socket.getaddrinfo", return_value=_addrinfo(PRIVATE_IP)):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "gpt-4o", "sk-x", "http://llm.internal/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)
		self.assertIn("container", result["checks"][-1]["detail"])

	def test_a_dead_socket_is_unverified(self):
		# Resolves fine and publicly, but nothing is listening. Still no answer,
		# so still nothing learned about the credential.
		with (
			mock.patch("socket.getaddrinfo", return_value=_addrinfo(PUBLIC_IP)),
			mock.patch.object(link_fetch, "_open_pinned", side_effect=OSError("connection refused")),
		):
			result = llm_key_probe.probe_api_key(
				"OpenAI-Compatible", "gpt-4o", "sk-x", "https://api.example.com/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)

	def test_a_provider_that_answered_and_said_no_is_still_a_real_failure(self):
		"""The guard on all of the above. Softening a genuine 401 into "could not
		check" would trade one lie for a worse one - the customer would never be
		told their key is wrong."""
		body = b'{"error":{"message":"Incorrect API key provided."}}'
		with mock.patch.object(link_fetch, "request_pinned", return_value=(401, {}, body)):
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "sk-bad", "https://api.openai.com/v1")
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_FAIL)
		self.assertFalse(result["ok"])
		self.assertIn("Incorrect API key", result["checks"][-1]["detail"])

	def test_an_unusable_url_is_a_real_failure_not_a_network_excuse(self):
		# There IS something concrete for the customer to fix here, so it stays red.
		result = llm_key_probe.probe_api_key("OpenAI-Compatible", "gpt-4o", "sk-x", "file:///etc/passwd")
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_FAIL)

	def test_a_pass_is_still_a_pass(self):
		with mock.patch.object(link_fetch, "request_pinned", return_value=(200, {}, b"{}")):
			result = llm_key_probe.probe_api_key("OpenAI", "gpt-4o", "sk-x", "https://api.openai.com/v1")
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_PASS)
		self.assertTrue(result["ok"])

	def test_the_caveat_stops_claiming_a_test_that_never_ran(self):
		"""The old caveat said "Tested from the bench's network". When nothing was
		reachable, no test happened at all, and that sentence was the only thing
		under a red banner trying to explain it."""
		frappe.set_user("Administrator")
		with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
			result = llm_key_probe.test_llm_api_key(
				"OpenAI-Compatible", "gpt-4o", "sk-x", "http://host.docker.internal:9000/openai/v1"
			)
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_UNVERIFIED)
		self.assertNotIn("Tested from", result["caveat"])
		self.assertIn("not a verdict on your key", result["caveat"])


class TestLinkFetchErrorKinds(FrappeTestCase):
	"""The classification llm_key_probe branches on is set at link_fetch's raise
	sites. Asserted here directly so a reworded message can never silently move a
	failure between "definitive" and "could not reach"."""

	def test_unresolvable_host_raises_the_unresolved_kind(self):
		with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
			with self.assertRaises(link_fetch.LinkFetchError) as cm:
				link_fetch.request_pinned("http://nope.invalid/v1", method="POST")
		self.assertEqual(cm.exception.kind, link_fetch.ERR_UNRESOLVED)

	def test_private_address_raises_the_blocked_kind(self):
		with mock.patch("socket.getaddrinfo", return_value=_addrinfo(PRIVATE_IP)):
			with self.assertRaises(link_fetch.LinkFetchError) as cm:
				link_fetch.request_pinned("http://internal.example.com/v1", method="POST")
		self.assertEqual(cm.exception.kind, link_fetch.ERR_BLOCKED_ADDRESS)

	def test_bad_scheme_raises_the_invalid_url_kind(self):
		with self.assertRaises(link_fetch.LinkFetchError) as cm:
			link_fetch.request_pinned("file:///etc/passwd")
		self.assertEqual(cm.exception.kind, link_fetch.ERR_INVALID_URL)

	def test_an_unclassified_error_is_treated_as_definitive(self):
		# Fail closed: anything raised without an explicit kind must NOT be
		# softened into "we could not check".
		self.assertEqual(link_fetch.LinkFetchError("boom").kind, link_fetch.ERR_RESPONSE)
		self.assertNotIn(link_fetch.ERR_RESPONSE, link_fetch.UNREACHABLE_KINDS)


class TestStoredApiKeysByProvider(FrappeTestCase):
	"""The single lookup shared by save_llm_pool's secret merge and the Test
	button (#679). Pure, so exercised on plain rows."""

	def _row(self, provider, api_key, credential_type="api_key"):
		return frappe._dict({"provider": provider, "api_key": api_key, "credential_type": credential_type})

	def test_keys_are_canonicalised_by_provider(self):
		keys = stored_api_keys_by_provider([self._row("OpenAI", "sk-1")])
		self.assertEqual(keys, {"openai": "sk-1"})

	def test_a_blank_key_never_masks_a_real_one(self):
		# A half-filled second row on the same vendor must not erase the credential.
		keys = stored_api_keys_by_provider([self._row("openai", "sk-1"), self._row("openai", "")])
		self.assertEqual(keys["openai"], "sk-1")

	def test_two_rows_on_one_provider_collapse_last_non_empty_wins(self):
		# Exactly what save_llm_pool's merge does, so Test cannot probe a key the
		# row will not hold after the next save.
		keys = stored_api_keys_by_provider([self._row("openai", "sk-1"), self._row("openai", "sk-2")])
		self.assertEqual(keys["openai"], "sk-2")

	def test_subscription_rows_carry_no_api_key(self):
		keys = stored_api_keys_by_provider(
			[self._row("openai", "should-be-ignored", credential_type="subscription")]
		)
		self.assertEqual(keys, {})


class TestProbeWithAStoredKey(_RT3SettingsTestCase):
	"""#679: with a key already saved, the Test button was disabled and read
	"Re-enter the key to test it", so changing ONLY a base URL - the edit where a
	test is worth the most - could not be validated at all unless the customer
	still had a credential they may have pasted months ago.

	The key here is written through a real doc save, so it is genuinely encrypted
	into __Auth and genuinely masked in memory afterwards: the point of the fix is
	that the SERVER can read it, and a fixture that just held the plaintext on the
	row would prove nothing about that.
	"""

	STORED = "sk-stored-abcdef123456"

	def setUp(self):
		super().setUp()
		self._clear_models()
		# Frappe caches a Single; a stale one here would serve the previous test's
		# models table and quietly decide what this probe finds.
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		settings = frappe.get_single("Jarvis Settings")
		row = frappe.new_doc("Jarvis LLM Pool Model")
		row.provider = "openai"
		row.model = "gpt-4o"
		row.tier = "strong"
		row.order = 0
		row.enabled = 1
		row.credential_type = "api_key"
		row.api_key = self.STORED
		row.base_url = "https://api.openai.com/v1"
		settings.append("models", row)
		with (
			mock.patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}),
			mock.patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "creds_update"}),
		):
			settings.save()
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _probe(self, **kw):
		"""Run the real whitelisted endpoint, capturing the request the probe
		would have put on the wire."""
		captured = {}

		def _fake(url, **rq):
			captured["url"] = url
			captured["headers"] = rq.get("headers") or {}
			return (200, {}, b"{}")

		args = {
			"provider": "OpenAI",
			"model": "gpt-4o",
			"api_key": "",
			"base_url": "https://gateway.example.com/v1",
		}
		args.update(kw)
		with mock.patch.object(link_fetch, "request_pinned", side_effect=_fake):
			result = llm_key_probe.test_llm_api_key(**args)
		return result, captured

	def test_the_stored_key_is_really_encrypted_at_rest(self):
		# Guards the fixture itself: if the row still held plaintext in memory,
		# every assertion below would pass without the server decrypting anything.
		reloaded = frappe.get_single("Jarvis Settings")
		self.assertNotEqual(reloaded.models[0].api_key, self.STORED)
		self.assertEqual(reloaded.models[0].get_password("api_key"), self.STORED)

	def test_a_new_base_url_is_probed_with_the_saved_key(self):
		"""The #679 scenario end to end: nothing typed into the key field, a new
		base URL, and the request still carries the real credential."""
		result, captured = self._probe(use_stored_key=1)
		self.assertTrue(result["ok"])
		self.assertEqual(captured["headers"]["Authorization"], f"Bearer {self.STORED}")
		self.assertTrue(captured["url"].startswith("https://gateway.example.com/v1"))

	def test_the_key_never_travels_back_to_the_browser(self):
		result, _ = self._probe(use_stored_key=1)
		self.assertNotIn(self.STORED, json.dumps(result))

	def test_the_probe_uses_the_same_key_the_save_merge_would(self):
		"""If these two ever diverge, a green Test attests to a credential Save
		does not send. They share one helper precisely so they cannot."""
		_, captured = self._probe(use_stored_key=1)
		merged = stored_api_keys_by_provider(frappe.get_single("Jarvis Settings").get("models"))
		self.assertEqual(captured["headers"]["Authorization"], f"Bearer {merged['openai']}")

	def test_a_typed_key_beats_the_stored_one(self):
		# A stale flag from an old client must never override what was just typed.
		_, captured = self._probe(api_key="sk-freshly-typed", use_stored_key=1)
		self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-freshly-typed")

	def test_the_stored_key_is_opt_in(self):
		# Without the flag the endpoint behaves exactly as before: a blank key is
		# a blank key. The stored credential is never reached for implicitly.
		result, captured = self._probe()
		self.assertFalse(result["ok"])
		self.assertEqual(captured, {})
		self.assertIn("Enter an API key", result["checks"][-1]["detail"])

	def test_a_provider_with_no_saved_key_says_exactly_that(self):
		"""The degenerate case: the row claimed a saved key and there is none under
		this provider - it was removed, or the provider was switched since the panel
		loaded. "Enter an API key" would be true but would not explain anything."""
		result, captured = self._probe(provider="Anthropic", use_stored_key=1)
		self.assertFalse(result["ok"])
		self.assertEqual(result["verdict"], llm_key_probe.VERDICT_FAIL)
		self.assertEqual(captured, {})
		self.assertIn("No saved key found", result["checks"][-1]["detail"])

	def test_guest_cannot_reach_a_stored_key(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			llm_key_probe.test_llm_api_key("OpenAI", "gpt-4o", "", "https://x.example.com/v1", 1)
