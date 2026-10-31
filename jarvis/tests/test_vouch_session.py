"""vouch_session: the site says whether it started a plugin session.

Plugin verification is patched at ``jarvis.api.validate_plugin_request``. Redis claims,
the site_config instance id and Jarvis Settings counters are restored in tearDown
because e2e2 is a shared site.
"""

import contextlib
import hashlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.installer import update_site_config
from frappe.tests.utils import FrappeTestCase

from jarvis._plugin_auth import PluginAuthError
from jarvis.api import vouch_session
from jarvis.chat import foreign_session, session_claims, site_identity
from jarvis.tests.test_api import _FakeRequest

SETTINGS = "Jarvis Settings"
KEY = "agent:main:dashboard:vouch-j1"
CLAIM_PREFIX = "jarvis:session_claim:"


def _claim_key(key: str) -> str:
	return CLAIM_PREFIX + hashlib.sha256(key.lower().encode()).hexdigest()


def _drop_claim(key: str) -> None:
	frappe.cache().delete_value(_claim_key(key))


def _drop_throttles(key: str) -> None:
	digest = hashlib.sha256(key.encode()).hexdigest()[:12]
	for name in (digest, "mismatch:" + digest):
		frappe.cache().delete_value("jarvis:foreign_session:" + name)


def _seen() -> int:
	return int(frappe.db.get_single_value(SETTINGS, "foreign_sessions_seen") or 0)


class TestVouchSession(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._had_id = "jarvis_instance_id" in frappe.get_site_config()
		self._old_id = frappe.get_site_config().get("jarvis_instance_id")
		self._old_counts = {
			f: frappe.db.get_single_value(SETTINGS, f)
			for f in ("foreign_sessions_seen", "foreign_session_last_at")
		}
		_drop_claim(KEY)
		self.addCleanup(_drop_claim, KEY)
		self.addCleanup(_drop_throttles, KEY)
		_drop_throttles(KEY)

	def tearDown(self):
		if self._had_id:
			update_site_config("jarvis_instance_id", self._old_id)
			frappe.conf["jarvis_instance_id"] = self._old_id
		else:
			update_site_config("jarvis_instance_id", "None")
			frappe.conf.pop("jarvis_instance_id", None)
		for field, value in self._old_counts.items():
			frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
		super().tearDown()

	@contextlib.contextmanager
	def _request(self, *, verified=True, key=KEY):
		if verified:
			verify = patch("jarvis.api.validate_plugin_request", return_value=key)
		else:
			err = PluginAuthError(http_status=401, code="AuthenticationError", message="bad signature")
			verify = patch("jarvis.api.validate_plugin_request", side_effect=err)
		with patch.object(frappe, "request", _FakeRequest({"X-Jarvis-Token": "t"}), create=True), verify:
			yield

	def test_create_session_claims_key(self):
		from jarvis.chat.agent_client import AgentSession

		client = AgentSession.__new__(AgentSession)
		client._request = MagicMock(return_value={"payload": {"key": KEY}})
		self.assertFalse(session_claims.is_claimed(KEY))
		self.assertEqual(client.create_session(), KEY)
		self.assertTrue(session_claims.is_claimed(KEY))

	def test_claim_survives_clear_cache(self):
		session_claims.claim(KEY)
		frappe.clear_cache()
		self.assertTrue(session_claims.is_claimed(KEY))

	def test_claim_is_case_insensitive(self):
		session_claims.claim(KEY.upper())
		self.assertTrue(session_claims.is_claimed(KEY))

	def test_vouch_known_for_chat_row(self):
		doc = frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": KEY, "user": "Administrator"}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Jarvis Chat Session", doc.name, force=True)
		with self._request(), patch.object(foreign_session, "record") as rec:
			result = vouch_session()
		self.assertTrue(result["known"])
		rec.assert_not_called()

	def test_vouch_known_for_claim(self):
		session_claims.claim(KEY)
		with self._request(), patch.object(foreign_session, "record") as rec:
			result = vouch_session()
		self.assertTrue(result["known"])
		rec.assert_not_called()

	def test_vouch_lowercases_before_lookup(self):
		session_claims.claim(KEY)
		with self._request(key=KEY.upper()), patch.object(foreign_session, "record"):
			self.assertTrue(vouch_session()["known"])

	def test_vouch_unknown_records_foreign_session(self):
		before = _seen()
		with self._request(), patch("frappe.enqueue") as enq:
			result = vouch_session()
		self.assertFalse(result["known"])
		self.assertEqual(_seen(), before + 1)
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.foreign_session.report")

	def test_vouch_never_known_from_key_shape(self):
		with self._request(key="agent:main:dashboard:someone-else"), patch.object(foreign_session, "record"):
			self.assertFalse(vouch_session()["known"])

	def test_vouch_requires_signature(self):
		with self._request(verified=False):
			result = vouch_session()
		self.assertFalse(result["ok"])
		self.assertEqual(frappe.local.response.http_status_code, 401)

	def test_vouch_returns_stable_instance_id(self):
		with self._request():
			first = vouch_session()["instance_id"]
			second = vouch_session()["instance_id"]
		self.assertRegex(first, r"^[0-9a-f]{32}$")
		self.assertEqual(first, second)
		self.assertEqual(frappe.get_site_config()["jarvis_instance_id"], first)

	def test_vouch_records_instance_mismatch(self):
		"""A key WITH a row (a restored database) must still alert, unmocked."""
		doc = frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": KEY, "user": "Administrator"}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Jarvis Chat Session", doc.name, force=True)
		before = _seen()
		with self._request(), patch("frappe.enqueue") as enq:
			result = vouch_session(pinned_instance_id="f" * 32)
			again = vouch_session(pinned_instance_id="f" * 32)
		self.assertTrue(result["known"] and again["known"])
		self.assertEqual(_seen(), before + 1)
		self.assertEqual(enq.call_count, 1)
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.foreign_session.report")

	def test_vouch_lookup_error_is_503_never_known(self):
		with (
			self._request(),
			patch.object(foreign_session, "has_row_for_key", side_effect=RuntimeError("db")),
			patch("jarvis.chat.site_pin.pin_if_unset") as pin,
		):
			result = vouch_session()
		self.assertEqual(frappe.local.response.http_status_code, 503)
		self.assertFalse(result["ok"])
		self.assertNotIn("known", result)
		pin.assert_not_called()

	def test_vouch_unwritable_site_config_returns_empty_instance_id(self):
		update_site_config("jarvis_instance_id", "None")
		frappe.conf.pop("jarvis_instance_id", None)
		session_claims.claim(KEY)
		with (
			self._request(),
			patch("frappe.installer.update_site_config", side_effect=OSError("read-only")),
		):
			result = vouch_session()
		self.assertTrue(result["known"])
		self.assertEqual(result["instance_id"], "")

	def test_vouch_matching_pin_records_nothing(self):
		session_claims.claim(KEY)
		with self._request(), patch.object(foreign_session, "record") as rec:
			own = vouch_session()["instance_id"]
			vouch_session(pinned_instance_id=own)
		rec.assert_not_called()

	def test_instance_id_race_writes_once(self):
		"""Another worker writes the id while this one waits for the lock: this one
		must adopt it and write nothing."""
		update_site_config("jarvis_instance_id", "None")
		frappe.conf.pop("jarvis_instance_id", None)
		winner = "a" * 32
		writes = []

		@contextlib.contextmanager
		def racing_lock(*args, **kwargs):
			update_site_config("jarvis_instance_id", winner)  # the other worker's write
			yield

		def counting_update(key, value, *a, **kw):
			writes.append(value)

		with (
			patch.object(frappe.cache, "lock", racing_lock),
			patch("frappe.installer.update_site_config", counting_update),
		):
			first = site_identity.instance_id()
			second = site_identity.instance_id()
		self.assertEqual((first, second), (winner, winner))
		self.assertEqual(writes, [])
