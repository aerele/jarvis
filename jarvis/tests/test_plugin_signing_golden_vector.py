"""Golden vectors for the plugin <-> site request signature.

The fixture is an identical copy of the plugin repo's ``tests/fixtures/plugin-signing-vectors.json``
(same bytes, same cases). Both sides check the same canonical string and signature, so a change to
either implementation that the other does not follow fails a test in one repo. Test key only.
"""

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis import _plugin_auth
from jarvis.chat import plugin_routes

_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "plugin_signing_vectors.json").read_text("utf-8"))
KEY = _VECTORS["key"]
CASES = _VECTORS["cases"]


def _hmac_hex(canonical: bytes) -> str:
	return hmac.new(KEY.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class TestPluginSigningGoldenVector(FrappeTestCase):
	def test_fixture_has_cases(self):
		self.assertGreaterEqual(len(CASES), 3)

	def test_canonical_string_and_signature_match_the_vector(self):
		for case in CASES:
			with self.subTest(case["name"]):
				body = case["body"].encode("utf-8")
				self.assertEqual(hashlib.sha256(body).hexdigest(), case["body_sha256"])
				canonical = _plugin_auth._canonical_request(
					session_key=case["session_key"],
					body_bytes=body,
					nonce=case["nonce"],
					timestamp_s=case["timestamp"],
				)
				self.assertEqual(canonical.decode("utf-8"), case["canonical"])
				self.assertEqual(_hmac_hex(canonical), case["signature"])

	def test_validate_signature_accepts_the_vector(self):
		for case in CASES:
			with self.subTest(case["name"]):
				nonce_store = MagicMock()
				nonce_store.set.return_value = True
				with (
					patch.object(_plugin_auth, "time", SimpleNamespace(time=lambda ts=case["timestamp"]: ts)),
					patch.object(_plugin_auth.frappe, "cache", return_value=nonce_store),
				):
					_plugin_auth._validate_signature(
						token=KEY,
						session_key=case["session_key"],
						body_bytes=case["body"].encode("utf-8"),
						signature=case["signature"],
						nonce=case["nonce"],
						timestamp_str=str(case["timestamp"]),
					)
				nonce_store.set.assert_called_once()

	def test_signed_route_headers_produce_the_vector(self):
		for case in CASES:
			with self.subTest(case["name"]):
				with (
					patch.object(plugin_routes, "_callback_key", return_value=KEY),
					patch.object(
						plugin_routes, "secrets", SimpleNamespace(token_hex=lambda n: case["nonce"])
					),
					patch.object(
						plugin_routes, "time", SimpleNamespace(time=lambda ts=case["timestamp"]: ts)
					),
				):
					headers = plugin_routes.signed_route_headers(
						case["session_key"], case["body"].encode("utf-8")
					)
				self.assertEqual(headers["X-Jarvis-Signature"], case["signature"])
				self.assertEqual(headers["X-Jarvis-Nonce"], case["nonce"])
				self.assertEqual(headers["X-Jarvis-Timestamp"], str(case["timestamp"]))
				self.assertEqual(headers["X-Jarvis-Session"], case["session_key"])
				self.assertEqual(headers["X-Jarvis-Token"], KEY)

	def test_signed_route_headers_build_the_canonical_string_with_the_shared_helper(self):
		"""One canonical-string implementation: the site signs with what it verifies with."""
		with (
			patch.object(plugin_routes, "_callback_key", return_value=KEY),
			patch.object(
				plugin_routes, "_canonical_request", wraps=_plugin_auth._canonical_request
			) as helper,
		):
			plugin_routes.signed_route_headers("agent:main:main", b"{}")
		helper.assert_called_once()
		self.assertEqual(helper.call_args.kwargs["session_key"], "agent:main:main")
		self.assertEqual(helper.call_args.kwargs["body_bytes"], b"{}")
