import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.api import call_tool


class TestCallToolStandardAuth(FrappeTestCase):
	"""Direct-Python invocation path: behaves like Phase 1, runs as the current session user."""

	def test_calls_tool_and_returns_result(self):
		result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertEqual(result["ok"], True)
		self.assertEqual(result["data"]["doctype"], "Customer")

	def test_accepts_json_string_args(self):
		result = call_tool(tool="get_schema", args='{"doctype": "Customer"}')
		self.assertEqual(result["ok"], True)

	def test_unknown_tool_returns_error_envelope(self):
		result = call_tool(tool="not_a_tool", args={})
		self.assertEqual(result["ok"], False)
		self.assertEqual(result["error"]["code"], "ToolNotFoundError")

	def test_invalid_args_returns_error_envelope(self):
		result = call_tool(tool="get_doc", args={"doctype": "Customer"})
		self.assertEqual(result["ok"], False)
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")


class _FakeRequest:
	"""Minimal request stand-in for the plugin-auth tests.

	Carries headers (the original use case) plus the raw body bytes so the
	HMAC validator can compute a body sha256. Defaults to empty body, which
	matches every non-signature test path.
	"""

	def __init__(self, headers: dict[str, str], body: bytes = b""):
		self.headers = headers
		self._body = body

	def get_data(self, cache: bool = True) -> bytes:
		return self._body


SIGNATURE_HEADERS = ("X-Jarvis-Signature", "X-Jarvis-Nonce", "X-Jarvis-Timestamp")


def sign_plugin_headers(token: str, session_key: str, body: bytes = b"") -> dict[str, str]:
	"""Build the three signature headers the bench now requires (JF-014).

	Mirrors ``signRequest`` in jarvis-openclaw-plugin's frappe-client.ts:
	HMAC-SHA256 over "session | sha256(body) | nonce | timestamp", keyed by
	the gateway token. A fresh nonce per call keeps the Redis dedup window
	from rejecting back-to-back calls in the same test.
	"""
	import hashlib
	import hmac as _hmac
	import secrets
	import time

	nonce = secrets.token_hex(16)
	ts = str(int(time.time()))
	canonical = "|".join([session_key, hashlib.sha256(body).hexdigest(), nonce, ts]).encode("utf-8")
	return {
		"X-Jarvis-Signature": _hmac.new(token.encode("utf-8"), canonical, hashlib.sha256).hexdigest(),
		"X-Jarvis-Nonce": nonce,
		"X-Jarvis-Timestamp": ts,
	}


class TestCallToolPluginAuth(FrappeTestCase):
	"""Plugin-auth path: X-Jarvis-Token + X-Jarvis-Session → Frappe resolves
	the user from Jarvis Chat Session and dispatches as them.

	(The earlier shape required an X-Jarvis-User header which the plugin
	resolved via a separate HTTPS call. That round-trip was removed
	2026-05-18 - Frappe owns the session→user mapping, so it looks the
	user up itself. See architecture.md ‘Path A v2'.)
	"""

	SESSION_KEY = "agent:test:plugin-auth"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		# Use a dedicated token for plugin-auth tests so we don't depend on
		# real agent config. db_set bypasses on_update - the value persists
		# only for this test class.
		cls._original_token = settings.get_password("agent_token", raise_exception=False) or ""
		settings.db_set("agent_token", "plugin-auth-test-token")
		# Seed a Jarvis Chat Session row so the user-resolution lookup has
		# something to find. Use a sentinel key so we can clean up cleanly.
		_cleanup_session(cls.SESSION_KEY)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Session",
				"session_key": cls.SESSION_KEY,
				"user": "Administrator",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("agent_token", cls._original_token)
		_cleanup_session(cls.SESSION_KEY)
		frappe.db.commit()
		super().tearDownClass()

	def _with_headers(
		self,
		headers: dict[str, str],
		*,
		body: bytes = b"",
		request_ip: str = "127.0.0.1",
		sign: bool = True,
	):
		"""Context manager: fakes ``frappe.request`` AND
		``frappe.local.request_ip``. Defaults to loopback so the
		C2 IP-allowlist check passes; tests targeting the IP path
		pass a different ``request_ip``.

		Since JF-014 the bench requires signed plugin requests, so bearer +
		session headers are auto-signed here (what every shipped plugin
		build does). These tests are about session→user resolution, not the
		signature; ``sign=False`` opts out.
		"""
		import contextlib

		if (
			sign
			and headers.get("X-Jarvis-Token")
			and headers.get("X-Jarvis-Session")
			and not any(h in headers for h in SIGNATURE_HEADERS)
		):
			headers = dict(headers)
			headers.update(sign_plugin_headers(headers["X-Jarvis-Token"], headers["X-Jarvis-Session"], body))

		req_patch = patch.object(frappe, "request", _FakeRequest(headers, body=body), create=True)

		# request_ip patch: frappe.local is a thread-local; just set the
		# attribute and restore on exit.
		@contextlib.contextmanager
		def _ip_ctx():
			prior = getattr(frappe.local, "request_ip", None)
			frappe.local.request_ip = request_ip
			try:
				yield
			finally:
				if prior is None:
					try:
						del frappe.local.request_ip
					except AttributeError:
						pass
				else:
					frappe.local.request_ip = prior

		@contextlib.contextmanager
		def _combined():
			with req_patch, _ip_ctx():
				yield

		return _combined()

	def test_valid_token_and_session_dispatches_as_session_user(self):
		"""Frappe resolves the user from the X-Jarvis-Session header alone."""
		seen_user: dict[str, str] = {}

		def spy_dispatch(name, args):
			seen_user["user"] = frappe.session.user
			return {"doctype": args["doctype"], "fields": []}

		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": self.SESSION_KEY,
			}
		):
			with patch("jarvis.api.dispatch", side_effect=spy_dispatch):
				with patch("jarvis.api._persist_and_publish_tool_call"):
					result = call_tool(tool="get_schema", args={"doctype": "Customer"})

		self.assertEqual(result["ok"], True)
		self.assertEqual(seen_user["user"], "Administrator")

	def test_invalid_token_returns_401(self):
		with self._with_headers(
			{
				"X-Jarvis-Token": "wrong-token",
				"X-Jarvis-Session": self.SESSION_KEY,
			}
		):
			result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertEqual(result["ok"], False)
		self.assertEqual(result["error"]["code"], "AuthenticationError")
		self.assertEqual(frappe.local.response.http_status_code, 401)

	def test_token_without_session_header_returns_400(self):
		with self._with_headers({"X-Jarvis-Token": "plugin-auth-test-token"}):
			result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertEqual(result["ok"], False)
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertIn("X-Jarvis-Session", result["error"]["message"])

	def test_token_with_unknown_session_returns_400(self):
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": "agent:nonexistent:xyz",
			}
		):
			result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertEqual(result["ok"], False)
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertIn("unknown session", result["error"]["message"])

	def test_session_user_restored_after_dispatch(self):
		"""set_user is wrapped in try/finally - the calling user is preserved."""
		original = frappe.session.user
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": self.SESSION_KEY,
			}
		):
			with patch("jarvis.api._persist_and_publish_tool_call"):
				call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertEqual(frappe.session.user, original)

	def _patched_session_lookup(self, *, row_device: str, current_device: str):
		"""Patch context that fakes:
		  - The Chat Session row's user lookup returns "Administrator" so
		    the existing user-resolution path succeeds.
		  - The Chat Session row's chat_device_id lookup returns
		    ``row_device`` (or "" to opt out of binding).
		  - Jarvis Settings.chat_device_id returns ``current_device``.
		Avoids requiring a real DB column (the JSON definition adds
		``chat_device_id`` but the migration runs at deploy time).
		"""
		original_get_value = frappe.db.get_value
		original_get_single_value = frappe.db.get_single_value

		def _fake_get_value(*args, **kwargs):
			# (doctype, filters, fieldname) positional OR kwargs.
			doctype = args[0] if args else kwargs.get("doctype")
			fieldname = args[2] if len(args) > 2 else kwargs.get("fieldname")
			if doctype == "Jarvis Chat Session":
				if fieldname == "user":
					return "Administrator"
				if fieldname == "chat_device_id":
					return row_device
			return original_get_value(*args, **kwargs)

		def _fake_get_single_value(doctype, field, *a, **kw):
			if doctype == "Jarvis Settings" and field == "chat_device_id":
				return current_device
			return original_get_single_value(doctype, field, *a, **kw)

		return (
			patch("jarvis.api.frappe.db.get_value", side_effect=_fake_get_value),
			patch("jarvis.api.frappe.db.get_single_value", side_effect=_fake_get_single_value),
		)

	def test_session_bound_to_old_device_rejected_after_repair(self):
		"""C2 stretch (2026-06-16 review): if the bench re-pairs the chat
		device after a session was issued, that session's chat_device_id
		snapshot won't match the current device id. The session is
		rejected with 401 AuthenticationError - bounds leaked-session
		replay to the window before the next operator re-pair."""
		gv, gsv = self._patched_session_lookup(
			row_device="old-device-id-from-before-repair",
			current_device="current-device-id-after-repair",
		)
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": "agent:test:any-session",
			}
		):
			with gv, gsv:
				with patch("frappe.db.exists", return_value=True):
					result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AuthenticationError")
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertIn("previous device pairing", result["error"]["message"])
		self.assertIn("start a new chat", result["error"]["message"])
		self.assertIn("Do not retry", result["error"]["message"])

	def test_stale_pairing_rejection_notifies_the_conversation(self):
		"""jarvis #712: a silent permanent 401 leaves nothing in the
		transcript and no realtime push - the customer just watches the
		conversation stop working with no explanation. The rejection must
		now also persist+publish an honest receipt (the same seam a normal
		dispatched tool call uses), not just return the bare 401."""
		session_key = "agent:test:stale-notify"
		frappe.cache().delete_value(f"jarvis:stale_pairing_notified:{session_key}")
		gv, gsv = self._patched_session_lookup(
			row_device="old-device-id-from-before-repair",
			current_device="current-device-id-after-repair",
		)
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": session_key,
			}
		):
			with gv, gsv:
				with patch("frappe.db.exists", return_value=True):
					with patch("jarvis.api._persist_and_publish_tool_call") as persist_spy:
						result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertFalse(result["ok"])
		persist_spy.assert_called_once()
		kwargs = persist_spy.call_args.kwargs
		self.assertEqual(kwargs["session_key"], session_key)
		self.assertEqual(kwargs["tool"], "get_schema")
		self.assertFalse(kwargs["result"]["ok"])
		self.assertIn("start a new chat", kwargs["result"]["error"]["message"])

	def test_stale_pairing_rejection_is_deduped_per_session(self):
		"""Every tool call in the same broken turn hits this same
		rejection - without a guard each would write another identical
		receipt and bury the transcript. Only the FIRST notifies."""
		session_key = "agent:test:stale-dedupe"
		frappe.cache().delete_value(f"jarvis:stale_pairing_notified:{session_key}")
		gv, gsv = self._patched_session_lookup(
			row_device="old-device-id-from-before-repair",
			current_device="current-device-id-after-repair",
		)
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": session_key,
			}
		):
			with gv, gsv:
				with patch("frappe.db.exists", return_value=True):
					with patch("jarvis.api._persist_and_publish_tool_call") as persist_spy:
						call_tool(tool="get_schema", args={"doctype": "Customer"})
						call_tool(tool="get_doc", args={"doctype": "Customer", "name": "x"})
		persist_spy.assert_called_once()

	def test_session_bound_to_current_device_accepted(self):
		"""Sanity check: a session whose chat_device_id matches the
		current bench device_id dispatches normally. No regression for
		the happy path."""
		gv, gsv = self._patched_session_lookup(
			row_device="matching-device-id",
			current_device="matching-device-id",
		)
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": "agent:test:any-session",
			}
		):
			with gv, gsv:
				with patch("frappe.db.exists", return_value=True):
					with patch("jarvis.api.dispatch", return_value={"doctype": "Customer", "fields": []}):
						with patch("jarvis.api._persist_and_publish_tool_call"):
							result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertTrue(result["ok"], msg=result)

	def test_pre_migration_row_without_device_id_passes(self):
		"""Backwards-compat: a row without ``chat_device_id`` (pre-migration
		session or pre-fix bench) must continue to dispatch normally so
		call_tool doesn't 500 on the first call after a deploy."""
		gv, gsv = self._patched_session_lookup(
			row_device="",  # empty = pre-migration session
			current_device="current-device-id",
		)
		with self._with_headers(
			{
				"X-Jarvis-Token": "plugin-auth-test-token",
				"X-Jarvis-Session": "agent:test:any-session",
			}
		):
			with gv, gsv:
				with patch("frappe.db.exists", return_value=True):
					with patch("jarvis.api.dispatch", return_value={"doctype": "Customer", "fields": []}):
						with patch("jarvis.api._persist_and_publish_tool_call"):
							result = call_tool(tool="get_schema", args={"doctype": "Customer"})
		self.assertTrue(result["ok"], msg=result)


def _cleanup_session(session_key: str) -> None:
	names = frappe.get_all(
		"Jarvis Chat Session",
		filters={"session_key": session_key},
		pluck="name",
	)
	for name in names:
		frappe.delete_doc("Jarvis Chat Session", name, ignore_permissions=True, force=True)
	frappe.db.commit()


# Sprint-1 / 2026-06-16 C2: layered plugin-auth defenses.
# See jarvis/_plugin_auth.py for the design.


class _PluginAuthTestBase(FrappeTestCase):
	"""Shared scaffolding: seed agent_token + a known Jarvis Chat Session
	so the existing call_tool flow has a user to dispatch under."""

	SESSION_KEY = "agent:test:c2"
	TOKEN = "c2-test-token"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._orig_token = settings.get_password("agent_token", raise_exception=False) or ""
		settings.db_set("agent_token", cls.TOKEN)
		_cleanup_session(cls.SESSION_KEY)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Session",
				"session_key": cls.SESSION_KEY,
				"user": "Administrator",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("agent_token", cls._orig_token)
		_cleanup_session(cls.SESSION_KEY)
		frappe.db.commit()
		super().tearDownClass()

	def _with_headers(self, headers, *, body=b"", request_ip="127.0.0.1"):
		import contextlib

		req_patch = patch.object(frappe, "request", _FakeRequest(headers, body=body), create=True)

		@contextlib.contextmanager
		def _ip_ctx():
			prior = getattr(frappe.local, "request_ip", None)
			frappe.local.request_ip = request_ip
			try:
				yield
			finally:
				if prior is None:
					try:
						del frappe.local.request_ip
					except AttributeError:
						pass
				else:
					frappe.local.request_ip = prior

		@contextlib.contextmanager
		def _combined():
			with req_patch, _ip_ctx():
				yield

		return _combined()

	def _call(self, *, request_ip="127.0.0.1", extra_headers=None, body=b"", sign=True):
		headers = {
			"X-Jarvis-Token": self.TOKEN,
			"X-Jarvis-Session": self.SESSION_KEY,
		}
		if extra_headers:
			headers.update(extra_headers)
		# JF-014: signed is the production default. Tests that drive the
		# signature themselves already supply the headers; the legacy
		# bearer-only path is exercised explicitly with sign=False.
		if sign and not any(h in headers for h in SIGNATURE_HEADERS):
			headers.update(sign_plugin_headers(self.TOKEN, self.SESSION_KEY, body))
		with self._with_headers(headers, body=body, request_ip=request_ip):
			with patch("jarvis.api._persist_and_publish_tool_call"):
				return call_tool(tool="get_schema", args={"doctype": "Customer"})


class TestC2RateLimit(_PluginAuthTestBase):
	"""60 calls/min per session_key. Leaked-token spam protection."""

	def setUp(self):
		super().setUp()
		# Reset the rate-limit counter for this session before each test.
		# plugin_auth.py uses raw redis-py methods (set/incr/expire) which
		# are unnamespaced, so use raw delete here too.
		try:
			frappe.cache().delete(f"jarvis:plugin_rl:{self.SESSION_KEY}")
		except Exception:
			pass

	def test_under_limit_accepted(self):
		# A handful of calls back-to-back should all succeed.
		for _ in range(5):
			result = self._call()
			self.assertTrue(result["ok"], msg=result)

	def test_over_limit_returns_429(self):
		"""Exhaust the bucket; the 61st call is rejected with 429."""
		# Burn the budget directly via the rate-limit key to avoid 60
		# round-trips through call_tool's dispatch. Raw redis-py set so
		# the key matches plugin_auth.py's unnamespaced incr.
		cache = frappe.cache()
		key = f"jarvis:plugin_rl:{self.SESSION_KEY}"
		cache.set(key, 60, ex=60)
		result = self._call()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "RateLimitExceededError")
		self.assertEqual(frappe.local.response.http_status_code, 429)


class TestC2HmacSignature(_PluginAuthTestBase):
	"""Phase-2 HMAC: replay-proof signed requests. Plugin still sends
	bearer for backwards compat; the signature is additive."""

	def _signed_headers(
		self, *, body: bytes, ts: int = None, nonce: str = "deadbeefdeadbeefdeadbeef", bad_sig: bool = False
	):
		import hashlib
		import hmac as _hmac
		import time

		if ts is None:
			ts = int(time.time())
		body_hash = hashlib.sha256(body).hexdigest()
		canonical = "|".join(
			[
				self.SESSION_KEY,
				body_hash,
				nonce,
				str(ts),
			]
		).encode("utf-8")
		sig = _hmac.new(self.TOKEN.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
		if bad_sig:
			# Flip a hex char so the sig fails validation but stays
			# the right length / character set.
			sig = ("0" if sig[0] != "0" else "1") + sig[1:]
		return {
			"X-Jarvis-Signature": sig,
			"X-Jarvis-Nonce": nonce,
			"X-Jarvis-Timestamp": str(ts),
		}

	def setUp(self):
		super().setUp()
		# Clear nonce-dedup keys from a previous test in the same class.
		# plugin_auth.py writes via raw redis-py SET NX (unnamespaced)
		# so we must delete via the raw redis method, not delete_value.
		cache = frappe.cache()
		for nonce in (
			"deadbeefdeadbeefdeadbeef",
			"oldtsnonce12345678",
			"badsignonce123456789",
			"replaynonce1234567890",
		):
			try:
				cache.delete(f"jarvis:plugin_nonce:{self.SESSION_KEY}:{nonce}")
			except Exception:
				pass

	def test_valid_signature_accepted(self):
		body = b'{"tool":"get_schema","args":{"doctype":"Customer"}}'
		extra = self._signed_headers(body=body)
		result = self._call(extra_headers=extra, body=body)
		self.assertTrue(result["ok"], msg=result)

	def test_partial_signature_headers_rejected_with_400(self):
		"""sig present but nonce missing is a client bug or downgrade
		attempt; either way, reject."""
		body = b'{"tool":"x"}'
		extra = self._signed_headers(body=body)
		del extra["X-Jarvis-Nonce"]
		result = self._call(extra_headers=extra, body=body)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertIn("partial signature", result["error"]["message"].lower())

	def test_old_timestamp_rejected(self):
		"""A captured-and-replayed request from 10 minutes ago must
		fail the timestamp skew window."""
		import time

		body = b"{}"
		extra = self._signed_headers(body=body, ts=int(time.time()) - 600, nonce="oldtsnonce12345678")
		result = self._call(extra_headers=extra, body=body)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AuthenticationError")

	def test_bad_signature_rejected(self):
		body = b'{"tool":"get_schema","args":{"doctype":"Customer"}}'
		extra = self._signed_headers(body=body, bad_sig=True, nonce="badsignonce123456789")
		result = self._call(extra_headers=extra, body=body)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AuthenticationError")

	def test_replayed_nonce_rejected(self):
		"""Same nonce used twice within the 120s TTL must fail the
		second time even if everything else is valid."""
		body = b'{"tool":"get_schema","args":{"doctype":"Customer"}}'
		extra = self._signed_headers(body=body, nonce="replaynonce1234567890")
		# First call: accepted (and the nonce is consumed).
		result = self._call(extra_headers=extra, body=body)
		self.assertTrue(result["ok"], msg=result)
		# Second call with SAME headers: rejected with 401.
		result = self._call(extra_headers=extra, body=body)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AuthenticationError")
		self.assertIn("nonce", result["error"]["message"].lower())


class TestJF014UnsignedPathRetired(_PluginAuthTestBase):
	"""JF-014 (2026-07-26): the unsigned bearer-only path is retired.

	A request carrying none of the three signature headers used to be
	accepted on bearer + session alone, so a captured agent_token replayed
	arbitrary unsigned tool bodies. Enforcement is now the default; the
	site_config key ``jarvis_plugin_allow_unsigned`` is the documented,
	WARN-logged escape hatch for un-upgraded plugins.

	The trailing block covers the review remediation: the reject is the
	operator's only signal, so it carries the remedy; the audit row behind
	it is throttled; and the hatch's usage count is readable by an operator.
	"""

	CONF_KEY = "jarvis_plugin_allow_unsigned"
	_ABSENT = object()

	def setUp(self):
		super().setUp()
		from jarvis import _plugin_auth

		# The rate-limit counter is keyed on the session, which this class
		# shares with TestC2RateLimit - and that class deliberately leaves
		# the bucket exhausted for up to 60s. The audit gate is likewise
		# per-session with a 60s TTL, so it must not leak across tests.
		try:
			cache = frappe.cache()
			cache.delete(f"jarvis:plugin_rl:{self.SESSION_KEY}")
			for key in self._audit_gate_keys():
				cache.delete(key)
			cache.delete(_plugin_auth._UNSIGNED_COUNTER_PREFIX + frappe.utils.today())
		except Exception:
			pass

	def _audit_gate_keys(self):
		from jarvis import _plugin_auth

		return [
			_plugin_auth._UNSIGNED_AUDIT_GATE_PREFIX + self.SESSION_KEY,
			_plugin_auth._UNSIGNED_AUDIT_GATE_PREFIX + self.SESSION_KEY + ":other",
		]

	def _conf(self, value):
		"""Set the escape-hatch site_config key for the duration of the
		block. ``value=self._ABSENT`` removes the key entirely (the state
		of every bench that never opted in)."""
		import contextlib

		@contextlib.contextmanager
		def _ctx():
			conf = frappe.local.conf
			missing = object()
			prior = conf.get(self.CONF_KEY, missing)
			if value is self._ABSENT:
				conf.pop(self.CONF_KEY, None)
			else:
				conf[self.CONF_KEY] = value
			try:
				yield
			finally:
				if prior is missing:
					conf.pop(self.CONF_KEY, None)
				else:
					conf[self.CONF_KEY] = prior

		return _ctx()

	def test_unsigned_request_rejected_by_default(self):
		"""The enforcement guard: delete the reject branch in
		_plugin_auth and this test fails."""
		with self._conf(self._ABSENT):
			result = self._call(sign=False)
		self.assertFalse(result["ok"], msg=result)
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertEqual(frappe.local.response.http_status_code, 400)
		self.assertIn("signature headers required", result["error"]["message"])

	def test_signed_request_still_accepted_with_flag_off(self):
		"""No regression for the path every shipped plugin build uses."""
		with self._conf(self._ABSENT):
			result = self._call()
		self.assertTrue(result["ok"], msg=result)

	def test_off_or_junk_flag_values_keep_enforcement(self):
		"""Fail closed: only an explicit on-literal re-opens the path."""
		for value in (self._ABSENT, 0, "0", "", None, False, "false", "no", "maybe"):
			with self.subTest(flag=value), self._conf(value):
				result = self._call(sign=False)
				self.assertFalse(result["ok"], msg=f"flag={value!r} allowed an unsigned request")
				self.assertEqual(result["error"]["code"], "InvalidArgumentError")

	def test_partial_signature_headers_rejected_regardless_of_flag(self):
		"""The escape hatch re-enables *unsigned* requests, never
		half-signed ones - that shape is a downgrade attempt."""
		for value in (self._ABSENT, 0, 1):
			with self.subTest(flag=value), self._conf(value):
				result = self._call(extra_headers={"X-Jarvis-Signature": "a" * 64})
				self.assertFalse(result["ok"], msg=f"flag={value!r} allowed partial headers")
				self.assertEqual(result["error"]["code"], "InvalidArgumentError")
				self.assertIn("partial signature", result["error"]["message"].lower())

	def test_unsigned_allowed_when_flag_on_and_warns_with_sunset_date(self):
		from unittest.mock import MagicMock

		from jarvis import _plugin_auth

		logger = MagicMock()
		with self._conf(1), patch.object(_plugin_auth.frappe, "logger", return_value=logger):
			result = self._call(sign=False)
		self.assertTrue(result["ok"], msg=result)
		warned = " ".join(str(arg) for call in logger.warning.call_args_list for arg in call.args)
		self.assertIn("DEPRECATED unsigned request", warned)
		self.assertIn(_plugin_auth._UNSIGNED_SUNSET_DATE, warned)
		self.assertIn(self.CONF_KEY, warned)

	def test_legacy_allowed_requests_are_counted(self):
		"""Operators need a number to watch before flipping the flag off."""
		from jarvis import _plugin_auth

		cache = frappe.cache()
		key = _plugin_auth._UNSIGNED_COUNTER_PREFIX + frappe.utils.today()
		try:
			cache.delete(key)
		except Exception:
			self.skipTest("cache unavailable")
		with self._conf(1):
			self.assertTrue(self._call(sign=False)["ok"])
			self.assertTrue(self._call(sign=False)["ok"])
		raw = cache.get(key)
		self.assertEqual(int(raw.decode() if isinstance(raw, bytes) else raw), 2)

	# --- review remediation: UX P0-1 (remedy in the message) + adversarial
	# P2-7 (throttle the reject audit) + UX P1-1 (operator-visible count).

	def test_reject_message_carries_the_remedy(self):
		"""The 400 is the operator's ONLY signal - it must name both the
		real fix and the exact stopgap command, with the removal date."""
		from jarvis import _plugin_auth

		with self._conf(self._ABSENT):
			result = self._call(sign=False)
		msg = result["error"]["message"]
		self.assertFalse(result["ok"], msg=result)
		self.assertIn("signature headers required", msg)
		self.assertIn("Upgrade the agent plugin", msg)
		self.assertIn(f"bench --site <site> set-config {self.CONF_KEY} 1", msg)
		self.assertIn(_plugin_auth._UNSIGNED_SUNSET_DATE, msg)

	def test_unsigned_reject_audit_is_throttled_per_session(self):
		"""Every reject still happens; only the Error Log row is gated."""
		from jarvis import _plugin_auth

		with self._conf(self._ABSENT), patch.object(_plugin_auth, "_audit_log") as audit:
			for _ in range(5):
				self.assertFalse(self._call(sign=False)["ok"])
			self.assertEqual(audit.call_count, 1, msg=audit.call_args_list)
			self.assertEqual(audit.call_args.args[0], "plugin_auth: unsigned request rejected")
			# A different session is a different operator symptom: not gated.
			headers = {
				"X-Jarvis-Token": self.TOKEN,
				"X-Jarvis-Session": self.SESSION_KEY + ":other",
			}
			with self._with_headers(headers):
				with patch("jarvis.api._persist_and_publish_tool_call"):
					self.assertFalse(call_tool(tool="get_schema", args={"doctype": "Customer"})["ok"])
			self.assertEqual(audit.call_count, 2, msg=audit.call_args_list)

	def test_audit_gate_fails_open_when_cache_is_unreachable(self):
		"""A Redis outage must never make the audit trail quieter."""
		from jarvis import _plugin_auth

		with patch.object(_plugin_auth.frappe, "cache", side_effect=RuntimeError("redis down")):
			self.assertTrue(_plugin_auth._should_audit_unsigned_reject("s1"))
			self.assertTrue(_plugin_auth._should_audit_unsigned_reject("s1"))

	def test_escape_hatch_status_is_none_while_enforcing(self):
		from jarvis import _plugin_auth

		with self._conf(self._ABSENT):
			self.assertIsNone(_plugin_auth.unsigned_escape_hatch_status())

	def test_escape_hatch_status_reports_recent_allowed_count(self):
		"""The operator-facing count must be read back from the same raw keys
		the allow path increments."""
		from jarvis import _plugin_auth

		cache = frappe.cache()
		today = frappe.utils.today()
		yesterday = frappe.utils.add_days(today, -1)
		try:
			cache.delete(_plugin_auth._UNSIGNED_COUNTER_PREFIX + today)
			cache.delete(_plugin_auth._UNSIGNED_COUNTER_PREFIX + str(yesterday))
		except Exception:
			self.skipTest("cache unavailable")
		cache.set(_plugin_auth._UNSIGNED_COUNTER_PREFIX + str(yesterday), b"3", ex=60)
		with self._conf(1):
			self.assertTrue(self._call(sign=False)["ok"])
			status = _plugin_auth.unsigned_escape_hatch_status()
		self.assertEqual(status["conf_key"], self.CONF_KEY)
		self.assertEqual(status["sunset_date"], _plugin_auth._UNSIGNED_SUNSET_DATE)
		self.assertEqual(status["window_days"], _plugin_auth._UNSIGNED_COUNTER_DAYS)
		# 1 from the call just made today + the 3 seeded for yesterday.
		self.assertEqual(status["recent_allowed"], 4)
		cache.delete(_plugin_auth._UNSIGNED_COUNTER_PREFIX + str(yesterday))

	def test_escape_hatch_status_says_unknown_when_cache_is_unreachable(self):
		from jarvis import _plugin_auth

		with self._conf(1), patch.object(_plugin_auth.frappe, "cache", side_effect=RuntimeError("down")):
			status = _plugin_auth.unsigned_escape_hatch_status()
		self.assertIsNotNone(status)
		self.assertIsNone(status["recent_allowed"])


class TestRotateAgentTokenEndpoint(FrappeTestCase):
	"""C2 PR-3C: bench-side orchestrator. Generates fresh randomness,
	pushes via admin (which proxies to fleet which recreates the
	container against the new env), persists locally on success."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._original_token = settings.get_password("agent_token", raise_exception=False) or ""
		settings.db_set("agent_token", "before-rotation-token")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# A successful rotation stores the token in __Auth (masked column);
		# drop that row so a stale rotated token can't shadow the restored
		# value via get_password's __Auth fallback in later suites.
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password("Jarvis Settings", "Jarvis Settings", "agent_token")
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("agent_token", cls._original_token)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		# rotate_agent_token requires System Manager; tests run as Admin.
		frappe.set_user("Administrator")
		# Reset to a known starting token (column write shadows any __Auth
		# row a prior test's rotation left - get_password short-circuits on
		# a non-masked column value).
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("agent_token", "before-rotation-token")
		frappe.db.commit()

	def _call_rotate(self):
		from jarvis.api import rotate_agent_token

		return rotate_agent_token()

	def test_happy_path_persists_new_token_after_admin_success(self):
		from jarvis import admin_client

		seen = {}

		def _spy(*, new_token):
			seen["pushed"] = new_token
			return {"action": "recreate", "result": "ok"}

		with patch.object(admin_client, "post_rotate_agent_token", side_effect=_spy):
			res = self._call_rotate()
		self.assertTrue(res["ok"], msg=res)
		self.assertIn("rotated_at", res["data"])
		# Locally persisted token must equal what we pushed to admin.
		settings = frappe.get_single("Jarvis Settings")
		stored = settings.get_password("agent_token")
		self.assertEqual(stored, seen["pushed"])
		# Token is 64 hex chars (secrets.token_hex(32)).
		self.assertRegex(stored, r"^[0-9a-f]{64}$")
		# And it changed from the seeded value.
		self.assertNotEqual(stored, "before-rotation-token")

	def test_admin_failure_does_not_persist_new_token(self):
		"""Mid-rotation admin failure must leave the bench's stored
		token UNTOUCHED. fleet-agent rolled the container back per
		PR-3A, so both sides stay in lockstep on the OLD token."""
		from jarvis import admin_client

		with patch.object(
			admin_client,
			"post_rotate_agent_token",
			side_effect=admin_client.AdminUnreachableError("network down"),
		):
			res = self._call_rotate()
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "AdminUnreachableError")
		self.assertEqual(frappe.local.response.http_status_code, 502)
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.get_password("agent_token"),
			"before-rotation-token",
			"old token must survive an admin-side rotation failure",
		)

	def test_rate_limited_returns_429_with_retry_after(self):
		from jarvis import admin_client

		with patch.object(
			admin_client,
			"post_rotate_agent_token",
			side_effect=admin_client.AdminRateLimitedError(
				"rate limit hit",
				retry_after_seconds=120,
			),
		):
			res = self._call_rotate()
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "RateLimitExceeded")
		self.assertEqual(res["error"]["retry_after_seconds"], 120)
		self.assertEqual(frappe.local.response.http_status_code, 429)

	def test_non_system_manager_rejected(self):
		"""rotate_agent_token must reject non-System-Manager callers."""
		# Make a fresh user with no roles beyond default.
		user_email = "rat-test-no-role@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "T",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		try:
			frappe.set_user(user_email)
			with self.assertRaises(frappe.PermissionError):
				self._call_rotate()
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("User", user_email, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestC2AgentTokenExpiry(_PluginAuthTestBase):
	"""C2 (2026-06-16 review): time-bounded agent_token.

	Tokens older than ``Jarvis Settings.agent_token_max_age_days``
	are rejected with AgentTokenExpired so the bench enforces periodic
	rotation hygiene. max_age=0 disables (legacy escape hatch).
	"""

	def _patch_settings(self, *, max_age_days: int, age_days: int | None):
		"""Patch _plugin_auth's view of Jarvis Settings: max_age + issued_at.

		``age_days=None`` simulates a legacy token with no issued_at field
		(must NOT expire - returning False from _agent_token_expired)."""
		import datetime as _dt
		from unittest.mock import MagicMock

		fake = MagicMock()
		fake.agent_token_max_age_days = max_age_days
		if age_days is None:
			fake.agent_token_issued_at = None
		else:
			fake.agent_token_issued_at = _dt.datetime.now() - _dt.timedelta(days=age_days)
		fake.get_password.return_value = self.TOKEN
		return patch("jarvis._plugin_auth.frappe.get_single", return_value=fake)

	def test_expiry_disabled_when_max_age_zero(self):
		"""Legacy escape hatch: max_age=0 = no expiry check."""
		with self._patch_settings(max_age_days=0, age_days=1000):
			result = self._call()
		self.assertTrue(result["ok"], msg=result)

	def test_unset_issued_at_does_not_expire_token(self):
		"""Pre-fix token with no issued_at must NOT 401 - operator gets
		a one-time grace window to rotate (cron warns separately)."""
		with self._patch_settings(max_age_days=90, age_days=None):
			result = self._call()
		self.assertTrue(result["ok"], msg=result)

	def test_within_age_window_accepted(self):
		"""A 30-day-old token in a 90-day window is fine."""
		with self._patch_settings(max_age_days=90, age_days=30):
			result = self._call()
		self.assertTrue(result["ok"], msg=result)

	def test_past_max_age_rejected_with_agent_token_expired(self):
		"""A 100-day-old token in a 90-day window is rejected. The error
		code is distinct (``AgentTokenExpired``) so the bench's UI can
		render a "rotate now" CTA instead of a generic 401."""
		with self._patch_settings(max_age_days=90, age_days=100):
			result = self._call()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AgentTokenExpired")
		self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertIn("rotate", result["error"]["message"].lower())

	def test_exactly_at_max_age_rejected(self):
		"""Boundary: age_days == max_age_days hits the >= cutoff."""
		with self._patch_settings(max_age_days=90, age_days=90):
			result = self._call()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "AgentTokenExpired")


class TestDispatchFromSessionResultBudget(FrappeTestCase):
	"""Task 2: enforce_result_budget wires into the openclaw-agent-only path
	(_dispatch_from_session), not the shared _dispatch_and_wrap path that the
	dashboard builder/desk/external call_tool callers use."""

	def _dispatch(self, result_data):
		from jarvis.api import _dispatch_from_session

		with (
			patch("jarvis.api._run_tool", return_value={"ok": True, "data": result_data}),
			patch("jarvis.api.impersonate"),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("jarvis.tools._delegate_capability.tool_denial", return_value=None),
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api.frappe.db.get_value", return_value="conv1"),
		):
			return _dispatch_from_session(
				user="Administrator",
				session_key="agent:test:session",
				tool="get_list",
				args={"doctype": "Customer"},
			)

	def test_over_budget_list_result_comes_back_truncated(self):
		big = [{"name": f"C{i}", "blob": "x" * 80} for i in range(3000)]
		env = self._dispatch(big)
		self.assertTrue(env["ok"])
		self.assertTrue(env["data"]["_truncated"])
		self.assertLess(env["data"]["shown"], 3000)

	def test_small_list_result_is_unchanged(self):
		small = [{"name": "C1"}]
		with patch("jarvis.api.telemetry.record_budget_event") as record_mock:
			env = self._dispatch(small)
		self.assertTrue(env["ok"])
		self.assertEqual(env["data"], small)
		# An in-budget result yields no guard event, so nothing is recorded.
		record_mock.assert_not_called()

	def test_error_envelope_passes_through(self):
		"""An error envelope (ok=False) is returned verbatim and the guard is not
		even invoked - the size cap only applies to successful `data` results."""
		from jarvis.api import _dispatch_from_session

		err = {"ok": False, "error": {"code": "X", "message": "y"}}
		with (
			patch("jarvis.api._run_tool", return_value=err),
			patch("jarvis.api.impersonate"),
			patch("jarvis.api._persist_and_publish_tool_call"),
			patch("jarvis.tools._delegate_capability.tool_denial", return_value=None),
			patch("jarvis.api._get_header", return_value=None),
			patch("jarvis.api.frappe.db.get_value", return_value="conv1"),
			patch("jarvis.api.enforce_result_budget") as g,
		):
			result = _dispatch_from_session(
				user="Administrator",
				session_key="agent:test:session",
				tool="get_list",
				args={"doctype": "Customer"},
			)
		self.assertEqual(result, err)
		g.assert_not_called()

	def test_dispatch_current_user_result_not_truncated(self):
		"""_dispatch_current_user (dashboard-builder/desk/external call_tool)
		is deliberately uncapped - the budget guard lives ONLY on the openclaw
		agent path (_dispatch_from_session), not the shared path this uses."""
		from jarvis.api import _dispatch_current_user

		big = [{"name": f"C{i}", "blob": "x" * 80} for i in range(3000)]
		with patch("jarvis.api._run_tool", return_value={"ok": True, "data": big}):
			env = _dispatch_current_user("get_list", {})
		self.assertTrue(env["ok"])
		self.assertIsInstance(env["data"], list)
		self.assertEqual(len(env["data"]), 3000)
		self.assertEqual(env["data"], big)

	def test_session_path_emits_budget_telemetry(self):
		"""The session path reports the truncation to telemetry with the
		ORIGINAL (pre-truncation) size, not the shrunk one."""
		big = [{"name": f"C{i}", "blob": "x" * 80} for i in range(3000)]
		expected_chars = len(json.dumps(big, default=str, ensure_ascii=False, separators=(",", ":")))
		with patch("jarvis.api.telemetry.record_budget_event") as record_mock:
			self._dispatch(big)
		record_mock.assert_called_once()
		_, kwargs = record_mock.call_args
		self.assertEqual(kwargs["tool"], "get_list")
		self.assertEqual(kwargs["outcome"], "truncated")
		self.assertEqual(kwargs["original_chars"], expected_chars)
		self.assertEqual(kwargs["total"], 3000)
		# The truncated (shrunk) count is reported, distinct from the original size.
		self.assertLess(kwargs["shown"], 3000)
