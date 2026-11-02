"""The site never sends the gateway owner token (Task J3b).

Narrow chat scopes, signed plugin routes for the four owner-only actions, media with the
device token, model overrides on sessions.patch, ping with the device token, and the
guards that used to read ``agent_token``. Every network call is patched.
"""

import hashlib
import hmac
import json
import os
import re
from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import plugin_routes
from jarvis.chat.agent_client import _REQUESTED_SCOPES, AgentSession
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._plugin_route_fixtures import SESSION, patch_signer
from jarvis.tests._settings_snapshot import JarvisSettingsSnapshot


def setUpModule():
	install_synthetic_runtime_profile()


AGENT_URL = "ws://agent.host:9000"
HTTP_BASE = "http://agent.host:9000"
KEY = "k" * 64


def _response(status=200, body=b"", payload=None):
	resp = Mock()
	resp.status_code = status
	resp.content = body
	resp.json = Mock(return_value=payload if payload is not None else {})
	return resp


class TestScopes(FrappeTestCase):
	def test_requested_scopes_have_no_admin(self):
		self.assertEqual(_REQUESTED_SCOPES, ["operator.read", "operator.write"])
		for scope in ("operator.admin", "operator.pairing"):
			self.assertNotIn(scope, _REQUESTED_SCOPES)


class TestSignedRoutes(FrappeTestCase):
	def _request(self, **kwargs):
		with (
			patch("jarvis.chat.plugin_routes._callback_key", return_value=KEY),
			patch("jarvis.chat.plugin_routes._agent_token", return_value=""),
			patch("requests.request", return_value=_response(payload={"ok": True, "payload": {}})) as req,
		):
			plugin_routes.route_request(AGENT_URL, **kwargs)
		return req.call_args

	def test_post_signs_the_exact_bytes_sent(self):
		call = self._request(
			method="POST", path="/jarvis/llm-task", session_key=SESSION, body={"args": {"prompt": "p"}}
		)
		sent = call.kwargs["data"]
		self.assertEqual(json.loads(sent), {"args": {"prompt": "p"}})
		h = call.kwargs["headers"]
		canonical = "|".join(
			[SESSION, hashlib.sha256(sent).hexdigest(), h["X-Jarvis-Nonce"], h["X-Jarvis-Timestamp"]]
		)
		expected = hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
		self.assertEqual(h["X-Jarvis-Signature"], expected)
		self.assertEqual(h["X-Jarvis-Token"], KEY)
		self.assertFalse(call.kwargs["allow_redirects"])
		self.assertNotIn("Authorization", h)

	def test_get_signs_an_empty_body(self):
		call = self._request(method="GET", path="/jarvis/canvas/a.svg", session_key=SESSION)
		self.assertEqual(call.args, ("GET", f"{HTTP_BASE}/jarvis/canvas/a.svg"))
		h = call.kwargs["headers"]
		canonical = "|".join(
			[SESSION, hashlib.sha256(b"").hexdigest(), h["X-Jarvis-Nonce"], h["X-Jarvis-Timestamp"]]
		)
		expected = hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
		self.assertEqual(h["X-Jarvis-Signature"], expected)

	def test_canvas_path_keeps_nested_names_and_refuses_traversal(self):
		self.assertEqual(plugin_routes.canvas_path("charts/x y.svg"), "/jarvis/canvas/charts/x%20y.svg")
		for bad in ("", "../etc/passwd", "a/../b", "a//b", "./a"):
			with self.assertRaises(plugin_routes.PluginRouteError):
				plugin_routes.canvas_path(bad)

	def test_missing_key_or_url_raises_a_fixed_error(self):
		with (
			patch("jarvis.chat.plugin_routes._callback_key", return_value=""),
			patch("jarvis.chat.plugin_routes._agent_token", return_value=""),
			patch("requests.request") as req,
		):
			with self.assertRaises(plugin_routes.PluginRouteError) as ctx:
				plugin_routes.route_request(AGENT_URL, "GET", "/jarvis/canvas/a.svg", SESSION)
			self.assertEqual(str(ctx.exception), "No plugin signing key is stored on this site.")
			with self.assertRaises(plugin_routes.PluginRouteError):
				plugin_routes.route_request("", "GET", "/jarvis/canvas/a.svg", SESSION)
		req.assert_not_called()

	def test_patch_route_refuses_fields_outside_the_policy(self):
		with patch("requests.request") as req:
			with self.assertRaises(plugin_routes.PluginRouteError):
				plugin_routes.patch_session_policy(AGENT_URL, SESSION, {"model": "x"})
		req.assert_not_called()

	def test_session_action_error_carries_only_the_scrubbed_gateway_text(self):
		refusal = _response(status=502, payload={"error": "session not found"})
		with patch_signer(), patch("requests.request", return_value=refusal):
			with self.assertRaises(plugin_routes.PluginRouteError) as ctx:
				plugin_routes.delete_session(AGENT_URL, SESSION)
		self.assertIn("session not found", str(ctx.exception))
		# Any other status gets a fixed message, even when the body says "not found".
		for status in (401, 403, 404, 500):
			body = {"error": "Not Found: session not found"}
			with patch_signer(), patch("requests.request", return_value=_response(status, payload=body)):
				with self.assertRaises(plugin_routes.PluginRouteError) as ctx:
					plugin_routes.delete_session(AGENT_URL, SESSION)
			self.assertNotIn("not found", str(ctx.exception).lower())
			self.assertIn(f"HTTP {status}", str(ctx.exception))


class TestCanvasUsesPluginRoute(FrappeTestCase):
	def test_canvas_fetch_uses_plugin_route(self):
		from jarvis.chat import canvas

		with (
			patch_signer(),
			patch("requests.request", return_value=_response(body=b"<svg/>")) as req,
		):
			out = canvas.fetch_canvas(AGENT_URL, SESSION, "charts/x.svg")
		self.assertEqual(out, (b"<svg/>", "svg"))
		self.assertEqual(req.call_args.args, ("GET", f"{HTTP_BASE}/jarvis/canvas/charts/x.svg"))
		headers = req.call_args.kwargs["headers"]
		self.assertEqual(headers["X-Jarvis-Session"], SESSION)
		self.assertNotIn("Authorization", headers)

	def test_canvas_fetch_never_passes_a_traversal_name(self):
		from jarvis.chat import canvas

		with patch_signer(), patch("requests.request") as req:
			self.assertIsNone(canvas.fetch_canvas(AGENT_URL, SESSION, "../secret.html"))
		req.assert_not_called()

	def test_canvas_fetch_without_a_session_makes_no_call(self):
		from jarvis.chat import canvas

		with patch("requests.request") as req:
			self.assertIsNone(canvas.fetch_canvas(AGENT_URL, "", "a.svg"))
		req.assert_not_called()

	def test_persist_rich_outputs_signs_canvas_with_the_conversation_session(self):
		from jarvis.chat import turn_handler

		settings = Mock(agent_url=AGENT_URL)
		settings.get_password = Mock(return_value="dev-tok")

		def fake_get_value(doctype, name, field):
			return SESSION if field == "session_key" else ""

		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.db.get_value", side_effect=fake_get_value),
			patch("jarvis.chat.canvas.persist_canvases", return_value=[]) as persist,
			patch("jarvis.chat.turn_handler._publish_to_user"),
		):
			turn_handler.persist_rich_outputs("MSG-1", "C1", "u@x", "run1", 0)
		persist.assert_called_once_with("MSG-1", "", AGENT_URL, SESSION)


class TestMediaUsesDeviceBearer(FrappeTestCase):
	def test_media_fetch_uses_device_bearer(self):
		from jarvis.chat import turn_handler

		asked = []
		settings = Mock(agent_url=AGENT_URL)

		def get_decrypted_password(doctype, name, fieldname, raise_exception=True):
			asked.append(fieldname)
			return {"chat_device_token": "dev-tok"}.get(fieldname, "")

		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.utils.password.get_decrypted_password", get_decrypted_password),
			patch("frappe.db.get_value", return_value=""),
			patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
			patch("jarvis.chat.generated_media.seed_media", return_value=[]) as seed,
			patch("jarvis.chat.generated_media.seed_media_urls", return_value=[]) as seed_urls,
			patch("jarvis.chat.turn_handler._publish_to_user"),
		):
			turn_handler.persist_rich_outputs(
				"MSG-1", "C1", "u@x", "run1", 0, media_rels=["/home/node/a.png"], media_urls=[{"url": "/m/1"}]
			)
		self.assertEqual(seed.call_args.args[2], "dev-tok")
		self.assertEqual(seed_urls.call_args.args[2], "dev-tok")
		self.assertNotIn("agent_token", asked)

	def test_media_download_sends_the_device_token_as_bearer(self):
		from jarvis.chat import generated_media

		resp = Mock(status_code=200)
		resp.iter_content = Mock(return_value=[b"PNG"])
		cm = Mock()
		cm.__enter__ = Mock(return_value=resp)
		cm.__exit__ = Mock(return_value=False)
		with patch("requests.get", return_value=cm) as get:
			out = generated_media.fetch_media(AGENT_URL, "dev-tok", "/home/node/media/a.png")
		self.assertEqual(out, b"PNG")
		self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer dev-tok"})


class TestTriggerUsesRoute(FrappeTestCase):
	def test_trigger_claims_session_and_uses_plugin_route(self):
		from jarvis.triggers import llm_task_client

		order = MagicMock()
		body = json.dumps({"ok": True, "result": {"details": {"json": "fine"}}}).encode()
		resp = Mock(status_code=200)
		resp.iter_content = Mock(return_value=[body])
		cm = Mock()
		cm.__enter__ = Mock(return_value=resp)
		cm.__exit__ = Mock(return_value=False)
		order.request.return_value = cm
		with (
			patch("frappe.get_cached_doc", return_value=Mock(agent_url=AGENT_URL)),
			patch("jarvis.chat.site_pin.assert_not_copy"),
			patch_signer(),
			patch("jarvis.chat.session_claims.claim", order.claim),
			patch("requests.request", order.request),
		):
			self.assertEqual(llm_task_client.llm_task_complete("p", "i"), "fine")
		self.assertEqual([c[0] for c in order.mock_calls[:2]], ["claim", "request"])
		claimed = order.claim.call_args.args[0]
		self.assertTrue(re.fullmatch(r"agent:main:trigger-[0-9a-f]{32}", claimed))
		call = order.request.call_args
		self.assertEqual(call.args, ("POST", f"{HTTP_BASE}/jarvis/llm-task"))
		self.assertEqual(call.kwargs["headers"]["X-Jarvis-Session"], claimed)
		self.assertNotIn("Authorization", call.kwargs["headers"])
		self.assertEqual(list(json.loads(call.kwargs["data"])), ["args"])


class TestSessionActions(FrappeTestCase):
	def _session(self):
		return AgentSession.__new__(AgentSession)

	def test_delete_session_goes_through_the_plugin_route(self):
		with (
			patch("frappe.db.get_single_value", return_value=AGENT_URL),
			patch_signer(),
			patch("requests.request", return_value=_response(payload={"ok": True, "payload": {}})) as req,
		):
			self._session().delete_session(SESSION)
		self.assertEqual(req.call_args.args, ("POST", f"{HTTP_BASE}/jarvis/sessions/delete"))
		self.assertEqual(json.loads(req.call_args.kwargs["data"]), {"key": SESSION})
		self.assertEqual(req.call_args.kwargs["headers"]["X-Jarvis-Session"], SESSION)

	def test_delete_session_passes_timeout_through_as_the_read_timeout(self):
		with (
			patch("frappe.db.get_single_value", return_value=AGENT_URL),
			patch_signer(),
			patch("requests.request", return_value=_response(payload={"ok": True, "payload": {}})) as req,
		):
			self._session().delete_session(SESSION, timeout_s=4)
			self.assertEqual(req.call_args.kwargs["timeout"][1], 4)
			self._session().delete_session(SESSION)
			self.assertEqual(req.call_args.kwargs["timeout"][1], 10)

	def test_delete_session_refusal_surfaces_as_agent_unreachable_with_the_gateway_text(self):
		refusal = _response(status=502, payload={"error": "session not found"})
		with (
			patch("frappe.db.get_single_value", return_value=AGENT_URL),
			patch_signer(),
			patch("requests.request", return_value=refusal),
		):
			with self.assertRaises(AgentUnreachableError) as ctx:
				self._session().delete_session(SESSION)
		# session_lifecycle treats "not found" as an already-deleted session.
		self.assertIn("not found", str(ctx.exception).lower())

	def test_patch_session_policy_goes_through_the_plugin_route(self):
		policy = {"inheritedToolAllow": ["read"], "inheritedToolDeny": ["browser"]}
		with (
			patch("frappe.db.get_single_value", return_value=AGENT_URL),
			patch_signer(),
			patch("requests.request", return_value=_response(payload={"ok": True, "payload": {}})) as req,
		):
			self._session().patch_session_policy(SESSION, policy)
		self.assertEqual(req.call_args.args, ("POST", f"{HTTP_BASE}/jarvis/sessions/patch"))
		self.assertEqual(json.loads(req.call_args.kwargs["data"]), {"key": SESSION, "patch": policy})


class TestModelOverrideRidesSessionsPatch(FrappeTestCase):
	def _session(self):
		sess = AgentSession.__new__(AgentSession)
		sess.calls = []
		sess._request = lambda method, params, *, timeout_s, **kw: (
			sess.calls.append((method, params)) or {"payload": {"runId": "r1"}}
		)
		return sess

	def test_fire_agent_patches_the_session_and_keeps_the_agent_params_clean(self):
		sess = self._session()
		sess.fire_agent("sk", "warm", "idem", model="gpt-5.5", provider="openai-codex")
		self.assertEqual(sess.calls[0], ("sessions.patch", {"key": "sk", "model": "openai-codex/gpt-5.5"}))
		method, params = sess.calls[1]
		self.assertEqual(method, "agent")
		self.assertNotIn("model", params)
		self.assertNotIn("provider", params)

	def test_fire_agent_without_an_override_sends_one_request(self):
		sess = self._session()
		sess.fire_agent("sk", "warm", "idem")
		self.assertEqual([m for m, _ in sess.calls], ["agent"])


class TestPing(FrappeTestCase):
	def test_ping_uses_device_token(self):
		from jarvis import agent_ws
		from jarvis.tests.test_chat_agent_client import _build_session

		with patch("jarvis.chat.agent_client.AgentSession.connect") as connect:
			agent_ws.ping(AGENT_URL)
		connect.assert_called_once_with(AGENT_URL)
		connect.return_value.close.assert_called_once()

		# What that connect presents: the device token, the narrow scopes, no owner token.
		from jarvis.tests.test_chat_agent_client import _challenge, _frame, _make_creds, _ScriptedWS

		creds = _make_creds()

		def _ok():
			return _frame({"type": "res", "id": scripted.sent[-1]["id"], "ok": True, "payload": {}})

		scripted = _ScriptedWS([_challenge("nonce-xyz"), _ok])
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		):
			sess = AgentSession.connect("ws://t")
		params = scripted.sent[0]["params"]
		self.assertEqual(params["auth"], {"deviceToken": creds.device_token})
		self.assertEqual(params["scopes"], ["operator.read", "operator.write"])
		sess.close()


class TestMechanismANoOwnerToken(FrappeTestCase):
	def test_mechanism_a_sends_no_owner_token(self):
		from jarvis.chat import device

		with patch("jarvis.chat.device._read_gateway_token", side_effect=AssertionError("owner token read")):
			creds = device._pair_mechanism_a(
				device_id="d", pub_b64u="p", priv=Mock(), bootstrap_codes=("code-1", "code-2")
			)
		self.assertEqual(creds.bootstrap_token, "")
		self.assertEqual(creds.current_bootstrap_code, "code-1")
		self.assertTrue(creds.needs_bootstrap)


class TestRotateRefused(JarvisSettingsSnapshot, FrappeTestCase):
	def test_rotate_agent_token_refused_with_callback_key(self):
		from jarvis import api as jarvis_api

		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Administrator")
		with (
			patch("jarvis._plugin_auth._callback_key", return_value=KEY),
			patch("jarvis.admin_client.post_rotate_agent_token") as post,
		):
			with self.assertRaises(frappe.PermissionError) as ctx:
				jarvis_api.rotate_agent_token()
		post.assert_not_called()
		self.assertIn("Token rotation is handled by Jarvis support.", str(ctx.exception))


class TestGuards(FrappeTestCase):
	def test_session_creation_guard_needs_only_the_agent_url(self):
		from jarvis.chat import api as chat_api

		unconfigured = Mock(agent_url="", enable_role_profiles=0)
		with patch("frappe.get_single", return_value=unconfigured):
			with self.assertRaises(frappe.ValidationError):
				chat_api._ensure_session_key("u@x.com")

	def test_ping_guard_checks_the_paired_device_not_the_owner_token(self):
		from jarvis import diagnostics

		settings = Mock(agent_url=AGENT_URL)
		settings.get_password = Mock(side_effect=AssertionError("agent_token read"))
		with (
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.diagnostics.require_jarvis_admin"),
			patch("jarvis.chat.device.has_paired_token", return_value=False),
		):
			out = diagnostics.ping_agent()
		self.assertEqual(out["kind"], "config")


class TestStrictClaim(FrappeTestCase):
	def test_strict_claim_raises_on_a_cache_error(self):
		from jarvis.chat import session_claims

		with patch("frappe.cache", side_effect=RuntimeError("redis down")):
			self.assertFalse(session_claims.is_claimed(SESSION))  # lenient: never raises
			with self.assertRaises(RuntimeError):
				session_claims.is_claimed_strict(SESSION)

	def test_vouch_session_answers_503_when_the_claim_lookup_errors(self):
		from jarvis import api as jarvis_api

		with (
			patch("jarvis.api.validate_plugin_request", return_value=SESSION),
			patch("jarvis.api._request_body_bytes", return_value=b""),
			patch("jarvis.chat.foreign_session.has_row_for_key", return_value=False),
			patch("jarvis.chat.session_claims.is_claimed_strict", side_effect=RuntimeError("redis down")),
			patch("jarvis.chat.foreign_session.record") as record,
			patch.object(frappe, "log_error"),
		):
			out = jarvis_api.vouch_session()
		self.assertEqual(frappe.local.response.http_status_code, 503)
		self.assertNotIn("known", out)
		record.assert_not_called()


# Files allowed to read ``agent_token`` with get_password, exact (relative to the app root).
_ALLOWED_AGENT_TOKEN_READERS = {
	"_plugin_auth.py",
	"_http.py",
	"onboarding.py",
	os.path.join("chat", "device.py"),
	os.path.join("chat", "plugin_routes.py"),
}
_AGENT_TOKEN_READ = re.compile(r"""get_password\(\s*(?:[^()]*?,\s*)?["']agent_token["']""", re.DOTALL)


class TestNoGatewayCallReadsAgentToken(FrappeTestCase):
	def test_no_gateway_call_reads_agent_token(self):
		root = os.path.dirname(
			os.path.dirname(os.path.abspath(__file__))
		)  # this checkout, not the bench copy
		offenders = []
		for folder, dirs, files in os.walk(root):
			dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "node_modules", "public")]
			for name in files:
				if not name.endswith(".py"):
					continue
				path = os.path.join(folder, name)
				rel = os.path.relpath(path, root)
				with open(path, encoding="utf-8") as f:
					if _AGENT_TOKEN_READ.search(f.read()) and rel not in _ALLOWED_AGENT_TOKEN_READERS:
						offenders.append(rel)
		self.assertEqual(offenders, [])

	def test_the_scan_pattern_catches_the_shapes_it_must(self):
		for text in (
			'settings.get_password("agent_token")',
			"s.get_password('agent_token', raise_exception=False)",
			'frappe.get_single("Jarvis Settings").get_password(\n\t"agent_token",\n)',
		):
			self.assertTrue(_AGENT_TOKEN_READ.search(text), text)
		self.assertFalse(_AGENT_TOKEN_READ.search('settings.get_password("chat_device_token")'))
