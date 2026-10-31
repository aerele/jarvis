"""J4b: pair a new chat device with the one-time bootstrap codes admin returns on a
passed site proof, instead of the gateway owner token.

Fake websocket frames, no network. Nothing here reads or writes Jarvis Settings:
the keypair/credential readers and admin are patched, so no snapshot is needed.
"""

import logging
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis.chat import device as chat_device
from jarvis.chat.agent_client import AgentSession
from jarvis.chat.device import ChatDeviceCredentials
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests.test_chat_agent_client import (
	_challenge,
	_frame,
	_make_bootstrap_creds,
	_not_paired_error,
	_ScriptedWS,
	_verify_connect_signature,
)

CODE1 = "code-one-secret-aaaa"
CODE2 = "code-two-secret-bbbb"


def _codes_creds(codes=(CODE1, CODE2)) -> ChatDeviceCredentials:
	from dataclasses import replace

	return replace(_make_bootstrap_creds(""), bootstrap_codes=tuple(codes))


def _invalid_error(ws) -> str:
	return _frame(
		{
			"type": "res",
			"id": ws.sent[-1]["id"],
			"ok": False,
			"error": {
				"code": "INVALID_REQUEST",
				"message": "bootstrap token invalid",
				"details": {"code": "AUTH_BOOTSTRAP_TOKEN_INVALID"},
			},
		}
	)


def _hello_ok(ws, token="tok-issued") -> str:
	return _frame(
		{"type": "res", "id": ws.sent[-1]["id"], "ok": True, "payload": {"auth": {"deviceToken": token}}}
	)


def _ws(reply):
	"""A scripted socket: challenge, then ``reply(ws)``."""
	ws = _ScriptedWS([_challenge(), None])
	ws._frames[1] = lambda: reply(ws)
	return ws


def _connect(creds, sockets, **extra):
	it = iter(sockets)
	update_mock = MagicMock(return_value=True)
	with (
		patch(
			"jarvis.chat.agent_client.websocket.create_connection",
			side_effect=lambda *a, **kw: next(it),
		),
		patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		patch("jarvis.chat.agent_client.update_device_token", update_mock),
		patch("jarvis.chat.agent_client.time.sleep"),
	):
		sess = AgentSession.connect("ws://t")
	return sess, update_mock


class TestBootstrapPairing(FrappeTestCase):
	def test_mechanism_a_with_codes_never_reads_agent_token(self):
		resp = {"mode": "mechanism_a", "accepted": True, "bootstrap_codes": [CODE1, CODE2]}
		priv = _make_bootstrap_creds("").private_key
		with (
			patch.object(chat_device, "_ensure_keypair_under_lock", return_value=("dev", "pub", priv)),
			patch.object(chat_device, "_read_credentials", return_value=None),
			patch.object(
				chat_device, "_read_gateway_token", side_effect=AssertionError("owner token was read")
			),
			patch("jarvis.chat.device.admin_client.request_chat_pairing", return_value=resp),
			patch("jarvis.chat.pairing_proof.issue_nonce", return_value="n" * 32),
			patch("jarvis.onboarding.fetch_callback_key_if_missing"),
		):
			creds = chat_device._establish_pairing()
		self.assertEqual(creds.bootstrap_codes, (CODE1, CODE2))
		self.assertEqual(creds.bootstrap_token, "")
		self.assertTrue(creds.needs_bootstrap)

	def test_handshake_presents_bootstrap_token_and_signs_with_it(self):
		creds = _codes_creds()
		ws = _ws(lambda w: _hello_ok(w))
		sess, _ = _connect(creds, [ws])
		sent = ws.sent[0]["params"]
		self.assertEqual(sent["auth"], {"bootstrapToken": CODE1})
		_verify_connect_signature(sent, CODE1, creds.public_key)
		with self.assertRaises(Exception):
			_verify_connect_signature(sent, CODE2, creds.public_key)
		sess.close()

	def test_retries_code1_then_code2_on_invalid(self):
		creds = _codes_creds()
		pending = _ws(_not_paired_error)
		refused = _ws(_invalid_error)
		approved = _ws(_hello_ok)
		sess, _ = _connect(creds, [pending, refused, approved])
		self.assertEqual(pending.sent[0]["params"]["auth"], {"bootstrapToken": CODE1})
		self.assertEqual(refused.sent[0]["params"]["auth"], {"bootstrapToken": CODE1})
		self.assertEqual(approved.sent[0]["params"]["auth"], {"bootstrapToken": CODE2})
		_verify_connect_signature(approved.sent[0]["params"], CODE2, creds.public_key)
		sess.close()

	def test_both_codes_invalid_fails_closed_without_looping(self):
		creds = _codes_creds()
		sockets = [_ws(_invalid_error), _ws(_invalid_error)]
		with self.assertRaises(AgentUnreachableError) as cm:
			_connect(creds, sockets)
		self.assertEqual(cm.exception.code, "pairing-pending")
		self.assertTrue(all(w.closed for w in sockets))

	def test_adopts_device_token_after_approval(self):
		creds = _codes_creds()
		sess, update_mock = _connect(creds, [_ws(_not_paired_error), _ws(_hello_ok)])
		self.assertEqual(sess._creds.device_token, "tok-issued")
		update_mock.assert_called_once_with("tok-issued", device_id=creds.device_id)
		sess.close()

	def test_legacy_owner_token_path_unchanged_without_codes(self):
		creds = _make_bootstrap_creds("gw-tok")
		self.assertTrue(creds.needs_bootstrap)
		ws = _ws(_hello_ok)
		sess, _ = _connect(creds, [ws])
		sent = ws.sent[0]["params"]
		self.assertEqual(sent["auth"], {"token": "gw-tok"})
		_verify_connect_signature(sent, "gw-tok", creds.public_key)
		sess.close()
		# No codes from admin: the legacy owner-token credentials are built.
		priv = creds.private_key
		with patch.object(chat_device, "_read_gateway_token", return_value="gw-tok"):
			legacy = chat_device._pair_mechanism_a(device_id="d", pub_b64u="p", priv=priv)
		self.assertEqual((legacy.bootstrap_token, legacy.bootstrap_codes), ("gw-tok", ()))

	def test_device_token_beats_codes_in_auth_block(self):
		from dataclasses import replace

		creds = replace(_codes_creds(), device_token="dev-tok")
		ws = _ws(lambda w: _frame({"type": "res", "id": w.sent[-1]["id"], "ok": True, "payload": {}}))
		sess, _ = _connect(creds, [ws])
		self.assertEqual(ws.sent[0]["params"]["auth"], {"deviceToken": "dev-tok"})
		sess.close()

	def test_codes_never_logged(self):
		creds = _codes_creds()
		sockets = [_ws(_not_paired_error), _ws(_invalid_error), _ws(_hello_ok)]
		with self.assertLogs(level=logging.DEBUG) as logs:
			sess, _ = _connect(creds, sockets)
			sess.close()
		exhausted = [_ws(_invalid_error), _ws(_invalid_error)]
		with self.assertLogs(level=logging.DEBUG) as logs2:
			with self.assertRaises(AgentUnreachableError) as cm:
				_connect(_codes_creds(), exhausted)
		blob = "\n".join(logs.output + logs2.output) + str(cm.exception) + repr(cm.exception)
		self.assertNotIn(CODE1, blob)
		self.assertNotIn(CODE2, blob)
