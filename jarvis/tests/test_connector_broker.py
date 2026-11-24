"""Unit tests for the frappe-facing parts of ``jarvis.connectors.broker`` that
were hardened in the security pass: recursive argument redaction, fail-CLOSED
egress policy on a read error, and routing the Test-connection probe through the
SAME circuit breaker + concurrency cap as a real call.

Plain ``unittest`` (no bench): ``frappe`` is mocked at the module boundary where
touched, ``mcp_client.fetch_tools`` is mocked so no socket is opened, and the
breaker/cap run against the in-memory ``limits.InMemoryStore``. The pure gate /
SSRF / limits state machines have their own hermetic suites; this file covers
only the broker glue those fixes changed.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from jarvis.connectors import broker, mcp_client, mcp_wire, ssrf
from jarvis.connectors.limits import InMemoryStore


class TestMcpCode(unittest.TestCase):
	"""``broker._mcp_code`` maps an McpError kind to the stable broker error code. A
	header/body mismatch (HTTP 4xx carrying JSON-RPC -32020) is a protocol_error, not a
	generic http_error, so refresh.after_call re-lists the tools."""

	def test_header_mismatch_is_protocol_error(self):
		exc = mcp_client.McpError(
			"bad headers", kind=mcp_client.ERR_HTTP, code=400, rpc={"code": mcp_wire.RPC_HEADER_MISMATCH}
		)
		self.assertEqual(broker._mcp_code(exc), "protocol_error")

	def test_plain_http_error_stays_http_error(self):
		exc = mcp_client.McpError("nope", kind=mcp_client.ERR_HTTP, code=400, rpc={"code": -32602})
		self.assertEqual(broker._mcp_code(exc), "http_error")

	def test_http_error_without_rpc_stays_http_error(self):
		exc = mcp_client.McpError("nope", kind=mcp_client.ERR_HTTP, code=403)
		self.assertEqual(broker._mcp_code(exc), "http_error")


class _Row(dict):
	"""Document-like stand-in: attribute access + ``.get``, so ``row.name`` and
	``row.get("base_url")`` both work like a real Jarvis Connector row."""

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc


class TestRecursiveRedaction(unittest.TestCase):
	def test_nested_secret_key_is_redacted(self):
		out = broker._redact_value({"config": {"token": "SECRET-abc", "name": "ok"}, "id": 3})
		self.assertEqual(out["config"]["token"], "***")
		self.assertEqual(out["config"]["name"], "ok")
		self.assertEqual(out["id"], 3)

	def test_secret_inside_a_list_is_redacted(self):
		out = broker._redact_value({"items": [{"password": "p"}, {"ok": 1}]})
		self.assertEqual(out["items"][0]["password"], "***")
		self.assertEqual(out["items"][1]["ok"], 1)

	def test_long_nested_string_is_clipped(self):
		out = broker._redact_value({"a": {"b": "x" * 500}})
		self.assertTrue(out["a"]["b"].endswith("..."))
		self.assertEqual(len(out["a"]["b"]), 203)

	def test_a_string_between_80_and_200_characters_is_kept_whole(self):
		text = "an ordinary sentence about a ticket " * 4
		self.assertEqual(broker._redact_value({"note": text.strip()})["note"], text.strip())

	def test_a_bare_key_is_an_id_not_a_secret(self):
		out = broker._redact_value({"key": "user-42", "issue_key": "ABC-1", "teamKey": "ENG", "apiKey": "x"})
		self.assertEqual(out["key"], "user-42")
		self.assertEqual(out["issue_key"], "ABC-1")
		self.assertEqual(out["teamKey"], "ENG")
		self.assertEqual(out["apiKey"], "***")

	def test_every_known_id_name_is_shown(self):
		names = (
			"key",
			"issue_key",
			"project_key",
			"projectKey",
			"teamKey",
			"idempotency_key",
			"page_token",
			"next_page_token",
			"nextPageToken",
			"cursor",
		)
		for name in names:
			with self.subTest(name=name):
				self.assertEqual(broker._redact_value({name: "visible-1"})[name], "visible-1")

	def test_concatenated_and_oddly_cased_secret_names_are_still_masked(self):
		names = (
			"clientsecret",
			"client_secret",
			"xapikeyheader",
			"X-Api-Key-Header",
			"authToken2",
			"AUTHTOKEN",
			"accesstoken",
			"bearerValue",
			"privatekey",
			"private_key_pem",
			"passwordConfirm",
			"passwd",
			"credentialsJson",
			"Authorization",
			"sessiontoken",
			"secretkey",
			"accesskey",
			"signingkey",
			"encryptionkey",
			"stripeApiKey",
		)
		for name in names:
			with self.subTest(name=name):
				self.assertEqual(broker._redact_value({name: "hunter2-value"})[name], "***")

	def test_a_secret_value_under_a_harmless_key_is_masked_by_the_value_scrub(self):
		url = "https://b.s3.amazonaws.com/r?X-Amz-Signature=deadbeef01&X-Amz-Expires=60"
		out = broker._redact_value({"key": url, "cursor": "ghp_" + "r" * 36})
		self.assertNotIn("deadbeef01", out["key"])
		self.assertNotIn("ghp_", out["cursor"])

	def test_credential_shaped_values_are_masked_whatever_the_key(self):
		url = (
			"https://b.s3.amazonaws.com/r?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2Fs3"
			"&X-Amz-Signature=deadbeef01&X-Amz-Security-Token=tok123abc&X-Amz-Expires=60"
		)
		out = broker._redact_value({"download": url, "items": [{"note": "key AKIAIOSFODNN7EXAMPLE"}]})
		blob = json.dumps(out)
		for leaked in ("AKIAIOSFODNN7EXAMPLE", "deadbeef01", "tok123abc"):
			self.assertNotIn(leaked, blob)
		self.assertIn("X-Amz-Expires=60", blob)

	def test_a_secret_is_masked_before_the_clip_not_cut_in_half(self):
		text = "x" * 190 + " ghp_" + "z" * 36
		out = broker._redact_value({"note": text})["note"]
		self.assertNotIn("ghp_", out)

	def test_clean_masks_secrets_in_an_apps_error_text(self):
		text = broker._clean("rejected token ghp_" + "q" * 36 + " for AKIAIOSFODNN7EXAMPLE")
		self.assertNotIn("ghp_", text)
		self.assertNotIn("AKIAIOSFODNN7EXAMPLE", text)
		self.assertIn("rejected token", text)

	def test_the_connector_log_row_gets_the_scrubbed_args_and_message(self):
		fake = mock.MagicMock()
		fake.as_json = lambda obj: json.dumps(obj)
		row = _Row(name="conn-1")
		args = {"key": "user-42", "apiKey": "hunter2-hunter2", "body": "see AKIAIOSFODNN7EXAMPLE"}
		with (
			mock.patch.object(broker, "frappe", fake),
			mock.patch.object(broker, "log_call") as log_call,
		):
			broker._log(row, "put", "tool_error", "bad token ghp_" + "q" * 36, 0.0, "run-1", args, None)
		kwargs = log_call.call_args.kwargs
		self.assertIn('"key": "user-42"', kwargs["args_summary"])
		self.assertNotIn("hunter2", kwargs["args_summary"])
		self.assertNotIn("AKIAIOSFODNN7EXAMPLE", kwargs["args_summary"])
		self.assertNotIn("ghp_", kwargs["message"])

	def test_a_list_of_args_is_redacted_too(self):
		fake = mock.MagicMock()
		fake.as_json = lambda obj: json.dumps(obj)
		with mock.patch.object(broker, "frappe", fake):
			summary = broker._redact_args([{"password": "p4ss"}])
		self.assertNotIn("p4ss", summary)

	def test_redact_args_serialized_summary_hides_nested_token(self):
		fake = mock.MagicMock()
		fake.as_json = lambda obj: json.dumps(obj)
		with mock.patch.object(broker, "frappe", fake):
			summary = broker._redact_args({"config": {"token": "SECRET-abc"}})
		self.assertIn("***", summary)
		self.assertNotIn("SECRET-abc", summary)


class TestEgressFailClosed(unittest.TestCase):
	def test_read_error_denies(self):
		fake = mock.MagicMock()
		fake.db.get_single_value.side_effect = RuntimeError("db down")
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			self.assertFalse(broker._egress_allowed("api.example.com"))

	def test_hook_error_denies(self):
		def _boom(_host):
			raise RuntimeError("hook broke")

		fake = mock.MagicMock()
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", _boom):
			self.assertFalse(broker._egress_allowed("api.example.com"))

	def test_empty_policy_still_allows(self):
		fake = mock.MagicMock()
		fake.db.get_single_value.return_value = None  # unset -> allow-all (design default)
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			self.assertTrue(broker._egress_allowed("api.example.com"))


class TestEgressAddressHook(unittest.TestCase):
	def _allowed(self, raw, address="34.1.2.3"):
		fake = mock.MagicMock()
		fake.db.get_single_value.return_value = raw
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			return broker._egress_address_allowed(address)

	def test_hook_exposes_address_check(self):
		self.assertIs(broker._egress_allowed.address_allowed, broker._egress_address_allowed)

	def test_ip_deny_rule_blocks_address(self):
		self.assertFalse(self._allowed("!0x22.1.2.3"))

	def test_glob_ip_deny_rules_block_address(self):
		self.assertFalse(self._allowed("!34.1.2.*", "34.1.2.5"))
		self.assertTrue(self._allowed("!34.1.2.*", "34.1.3.5"))
		self.assertFalse(self._allowed("!2606:4700:4700::*", "2606:4700:4700::1111"))
		self.assertTrue(self._allowed("!2606:4700:4700::*", "2606:4700:4701::1"))

	def test_hostname_rules_and_empty_policy_allow_address(self):
		self.assertTrue(self._allowed("!evil.com\napi.github.com"))
		self.assertTrue(self._allowed(None))

	def test_read_error_denies(self):
		fake = mock.MagicMock()
		fake.db.get_single_value.side_effect = RuntimeError("db down")
		with mock.patch.object(broker, "frappe", fake), mock.patch.object(broker, "egress_hook", None):
			self.assertFalse(broker._egress_address_allowed("34.1.2.3"))


class TestGuardedTestProbe(unittest.TestCase):
	"""``broker.test_connector`` must run the outbound probe inside the same
	breaker + concurrency cap as a real call, so the Test button cannot bypass the
	worker protection."""

	def _row(self, **kw):
		row = {"name": "conn-1", "base_url": "https://api.example.com/mcp"}
		row.update(kw)
		return _Row(row)

	def _probe_result(self, **kw):
		# The shape mcp_client.probe returns (Phase A): tools plus the era/cache hints
		# _test_probe threads back to persist_probe_result.
		result = {
			"tools": [{"name": "t"}],
			"era": "modern",
			"protocol_version": "2026-07-28",
			"tools_ttl_ms": 60000,
			"capabilities": {},
		}
		result.update(kw)
		return result

	def test_success_returns_tools(self):
		store = InMemoryStore()
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "probe", return_value=self._probe_result()),
		):
			out = broker.test_connector(self._row())
		self.assertTrue(out["ok"])
		self.assertEqual(out["tools"], [{"name": "t"}])
		# The era/cache hints ride back so persist_probe_result can store them.
		self.assertEqual(out["protocol_version"], "2026-07-28")
		self.assertEqual(out["tools_ttl_ms"], 60000)

	def test_repeated_transport_failures_open_the_circuit(self):
		store = InMemoryStore()
		boom = ssrf.SsrfError("unreachable", kind=ssrf.ERR_CONNECT_FAILED)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "probe", side_effect=boom),
		):
			row = self._row()
			for _ in range(broker.CB_THRESHOLD):
				out = broker.test_connector(row)
				self.assertEqual(out["error"]["code"], "transport_error")
			# The next probe is fast-failed by the now-open breaker, without a call.
			with mock.patch.object(broker.mcp_client, "probe") as probe:
				blocked = broker.test_connector(row)
			probe.assert_not_called()
		self.assertEqual(blocked["error"]["code"], "circuit_open")

	def test_ssrf_block_does_not_open_the_circuit(self):
		# A policy block (blocked address) is not endpoint health; it must not count
		# toward the breaker.
		store = InMemoryStore()
		boom = ssrf.SsrfError("blocked", kind=ssrf.ERR_BLOCKED_ADDRESS)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "probe", side_effect=boom),
		):
			row = self._row()
			for _ in range(broker.CB_THRESHOLD + 2):
				out = broker.test_connector(row)
				self.assertEqual(out["error"]["code"], "ssrf_blocked")

	def test_a_bad_key_rpc_error_does_not_open_the_circuit(self):
		# Mailchimp Transactional answers a bad key with HTTP 200 and a null-id
		# JSON-RPC error. Through the real client that is an rpc_error carrying the
		# server's reason, and an auth problem never counts toward the breaker.
		store = InMemoryStore()
		reply = {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Invalid API key"}, "id": None}

		def seam(url, **_kw):
			resp = mock.MagicMock(status=200, headers={"Content-Type": "application/json"})
			resp.stream.return_value = iter([json.dumps(reply).encode("utf-8")])
			resp.read1 = None
			return resp, mock.MagicMock(), url

		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker, "_credential", return_value="bad-key"),
			mock.patch.object(mcp_client.ssrf, "open_pinned_request", side_effect=seam),
			mock.patch.object(broker.CircuitBreaker, "record_failure") as record_failure,
		):
			row = self._row()
			for _ in range(broker.CB_THRESHOLD + 2):
				out = broker.test_connector(row)
				self.assertEqual(out["error"], {"code": "rpc_error", "message": "Invalid API key"})
		record_failure.assert_not_called()

	def test_at_capacity_when_cap_full(self):
		store = InMemoryStore()
		# Pre-fill the per-connector concurrency counter to its limit so the probe's
		# own slot acquisition trips the cap.
		store.set(f"cc:{'conn-1'}", broker.CC_LIMIT, 60)
		with (
			mock.patch.object(broker, "_store", return_value=store),
			mock.patch.object(broker.mcp_client, "probe", return_value=self._probe_result()) as probe,
		):
			out = broker.test_connector(self._row())
		probe.assert_not_called()
		self.assertEqual(out["error"]["code"], "at_capacity")


class TestDoCallPassthrough(unittest.TestCase):
	"""``_do_call`` threads the row's stored era and the x-mcp-header mirrors derived
	from the cached inputSchema into ``mcp_client.run_tool`` (spec 2026-07-28)."""

	def _row(self, **kw):
		row = {
			"name": "conn-1",
			"base_url": "https://api.example.com/mcp",
			"tools_cache": json.dumps(
				{
					"tools": [
						{
							"name": "t",
							"inputSchema": {
								"properties": {"tenant": {"type": "string", "x-mcp-header": "X-Tenant"}}
							},
						}
					]
				}
			),
		}
		row.update(kw)
		return _Row(row)

	def test_stored_era_and_header_params_are_passed(self):
		with mock.patch.object(broker.mcp_client, "run_tool", return_value={}) as run_tool:
			broker._do_call(
				self._row(mcp_protocol_version="2026-07-28"),
				"t",
				{"tenant": "acme"},
				"",
				broker._NullBreaker(),
			)
		_, kwargs = run_tool.call_args
		self.assertEqual(kwargs["protocol_version"], "2026-07-28")
		self.assertEqual(kwargs["header_params"], {"Mcp-Param-X-Tenant": "acme"})

	def test_no_stored_era_means_the_shipped_legacy_handshake(self):
		# A row tested before the field existed must NOT pay era detection on a
		# chat turn: it speaks the legacy version exactly as shipped.
		with mock.patch.object(broker.mcp_client, "run_tool", return_value={}) as run_tool:
			broker._do_call(self._row(), "t", {"tenant": "acme"}, "", broker._NullBreaker())
		_, kwargs = run_tool.call_args
		self.assertEqual(kwargs["protocol_version"], broker.mcp_client.DEFAULT_PROTOCOL_VERSION)
		self.assertEqual(kwargs["header_params"], {"Mcp-Param-X-Tenant": "acme"})


class TestCallHonoursSwitchedOffAction(unittest.TestCase):
	"""``broker.call`` on a read-only action the user switched off (allowed=0)
	returns the ``action_denied`` envelope and never reaches the remote server."""

	def _row(self, allowed):
		action = {"action": "fetch_doc", "allowed": allowed, "read_only": 1, "destructive": 0}
		return _Row(name="conn-1", enabled=1, allowed_actions=[action], tools_cache=None)

	def _call(self, row):
		with (
			mock.patch.object(broker, "_resolve_row", return_value=row),
			mock.patch.object(broker, "_execute", return_value={"content": []}) as execute,
			mock.patch.object(broker, "_log"),
			mock.patch.object(broker.refresh, "after_call"),
		):
			return broker.call("docs", "fetch_doc", {}), execute

	def test_switched_off_read_only_action_is_denied(self):
		out, execute = self._call(self._row(allowed=0))
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "action_denied")
		execute.assert_not_called()

	def test_allowed_read_only_action_runs(self):
		out, execute = self._call(self._row(allowed=1))
		self.assertTrue(out["ok"])
		execute.assert_called_once()


class TestPrecheck(unittest.TestCase):
	"""``broker.precheck`` runs the checks ``call`` makes before it sends anything,
	so a confirmation card is never parked for a call that cannot run."""

	_TOOLS = {"tools": [{"name": "create", "inputSchema": {"type": "object", "required": ["title"]}}]}

	def _row(self, *, allowed=1, enabled=1, status="Passed", cache=True):
		action = {"action": "create", "allowed": allowed, "read_only": 0, "destructive": 0}
		return _Row(
			name="conn-1",
			enabled=enabled,
			allowed_actions=[action],
			last_test_status=status,
			tools_cache=json.dumps(self._TOOLS) if cache else None,
		)

	def _precheck(self, row, args):
		with (
			mock.patch.object(broker, "_resolve_row", return_value=row),
			mock.patch.object(broker, "_log") as log,
			mock.patch.object(broker.refresh, "after_call"),
			mock.patch.object(broker, "_execute") as execute,
			mock.patch.object(broker.mcp_client, "run_tool") as sent,
		):
			out = broker.precheck("docs", "create", args)
		execute.assert_not_called()
		sent.assert_not_called()  # nothing ever leaves the bench
		return out, log

	def test_a_runnable_call_passes_and_writes_no_row(self):
		out, log = self._precheck(self._row(), {"title": "t"})
		self.assertIsNone(out)
		log.assert_not_called()

	def test_each_refusal_returns_its_code_and_writes_one_row(self):
		cases = (
			(self._row(allowed=0), {"title": "t"}, "action_denied"),
			(self._row(enabled=0), {"title": "t"}, "connector_disabled"),
			(self._row(status=""), {"title": "t"}, "connector_not_ready"),
			(self._row(cache=False), {"title": "t"}, "connector_not_ready"),
			(self._row(), {}, "invalid_arguments"),
		)
		for row, args, code in cases:
			with self.subTest(code=code):
				out, log = self._precheck(row, args)
				self.assertEqual(out["ok"], False)
				self.assertEqual(out["error"]["code"], code)
				log.assert_called_once()
				self.assertEqual(log.call_args.args[2], code)

	def test_unknown_connector_is_refused_without_a_row(self):
		with (
			mock.patch.object(
				broker, "_resolve_row", side_effect=broker._BrokerError("connector_not_found", "none")
			),
			mock.patch.object(broker, "_log") as log,
		):
			out = broker.precheck("nope", "create", {})
		self.assertEqual(out["error"]["code"], "connector_not_found")
		log.assert_not_called()  # no row to log against, as in ``call``

	def test_an_unexpected_error_lets_the_real_call_decide(self):
		with (
			mock.patch.object(broker, "_resolve_row", side_effect=RuntimeError("db down")),
			mock.patch.object(broker, "frappe", mock.MagicMock()),
		):
			self.assertIsNone(broker.precheck("docs", "create", {}))


class _DoesNotExist(Exception):
	"""Stand-in for ``frappe.DoesNotExistError`` - a REAL class so ``_resolve_row``'s
	``except frappe.DoesNotExistError`` can catch it under a mocked frappe."""


class TestResolveRowDeletedBetweenLookupAndLoad(unittest.TestCase):
	"""A row deleted between the name lookup (``get_all``) and the load
	(``get_doc``) used to raise ``DoesNotExistError`` straight through
	``resolve_for_status`` / ``call_connector`` to a 500. ``_resolve_row`` now
	treats that as "not found", so the ordinary ``connector_not_found`` envelope is
	what surfaces - a DB outage (any OTHER exception) still propagates."""

	def _frappe(self, *, get_doc_error):
		fake = mock.MagicMock()
		fake.DoesNotExistError = _DoesNotExist
		fake.session.user = "u@example.com"
		fake.get_all.return_value = ["conn-1"]  # the name lookup DID find a row
		fake.get_doc.side_effect = get_doc_error  # ... which is gone by load time
		return fake

	def test_deleted_row_becomes_connector_not_found(self):
		fake = self._frappe(get_doc_error=_DoesNotExist("deleted"))
		with mock.patch.object(broker, "frappe", fake):
			with self.assertRaises(broker._BrokerError) as cm:
				broker._resolve_row("github")
		self.assertEqual(cm.exception.code, "connector_not_found")

	def test_resolve_for_status_returns_none_when_row_vanished(self):
		fake = self._frappe(get_doc_error=_DoesNotExist("deleted"))
		with mock.patch.object(broker, "frappe", fake):
			self.assertIsNone(broker.resolve_for_status("github"))

	def test_db_outage_on_load_is_not_swallowed_as_not_found(self):
		# A DB outage is not "not found": it must propagate, never be masked as a
		# clean connector_not_found envelope.
		fake = self._frappe(get_doc_error=RuntimeError("db down"))
		with mock.patch.object(broker, "frappe", fake):
			with self.assertRaises(RuntimeError):
				broker._resolve_row("github")


if __name__ == "__main__":
	unittest.main()
