"""Runtime profile validation, privacy, and local storage failure boundaries."""

import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, asdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import runtime_profile as rp


def connection():
	return {
		"agent_url": "https://gateway.example.test",
		"tenant_authority_generation": 4,
		"tenant_authority_handle": "a" * 32,
	}


def envelope():
	contract = {"schema_version": 1, "profile_id": "gateway-v1", "values": asdict(rp.legacy_profile())}
	return {
		**contract,
		"revision": hashlib.sha256(rp._json(contract).encode()).hexdigest(),
		"runtime_binding": {
			"generation": 4,
			"handle": "a" * 32,
			"gateway_url": connection()["agent_url"],
			"image": "test-runtime:1",
		},
	}


class TestRuntimeProfileValidation(unittest.TestCase):
	def test_valid_profile_is_immutable(self):
		profile = rp.validate(envelope(), connection())
		self.assertEqual(profile, rp.legacy_profile())
		with self.assertRaises(FrozenInstanceError):
			profile.canvas_route = "/changed/"

	def test_rejects_malformed_and_oversized(self):
		for raw in (None, [], {}, "x", {**envelope(), "extra": "x" * rp.MAX_BYTES}):
			with self.subTest(raw_type=type(raw)), self.assertRaises(rp.RuntimeProfileError):
				rp.validate(raw)

	def test_rejects_unsupported_schema_and_revision(self):
		for key, value in (
			("schema_version", True),
			("schema_version", 2),
			("revision", "wrong"),
			("profile_id", "unknown"),
		):
			with self.subTest(key=key), self.assertRaises(rp.RuntimeProfileError):
				rp.validate({**envelope(), key: value})

	def test_routes_cannot_redirect_or_traverse_even_with_matching_digest(self):
		for value in (
			"https://evil.test/",
			"//evil.test/",
			"/a/../b/",
			"/a/%2e%2e/",
			"/a/%252f/",
			"/a\\b/",
			"/a?token=x",
			"/a#b",
			"/a\n/",
		):
			raw = envelope()
			raw["values"]["canvas_route"] = value
			raw["revision"] = hashlib.sha256(
				rp._json({k: raw[k] for k in ("schema_version", "profile_id", "values")}).encode()
			).hexdigest()
			with self.subTest(value=value), self.assertRaises(rp.RuntimeProfileError):
				rp.validate(raw)

	def test_cannot_broaden_media_root_or_reinterpret_old_transcripts(self):
		for key, value in (("media_root", "/home/node/"), ("message_metadata_key", "other_metadata")):
			raw = envelope()
			raw["values"][key] = value
			with self.assertRaises(rp.RuntimeProfileError):
				rp.validate(raw)

	def test_connection_binding_must_match(self):
		for key, value in (
			("agent_url", "https://other.test"),
			("tenant_authority_generation", 5),
			("tenant_authority_handle", "b" * 32),
		):
			with self.subTest(key=key), self.assertRaises(rp.RuntimeProfileError):
				rp.validate(envelope(), {**connection(), key: value})

	def test_profile_never_serializes_in_connection_response(self):
		raw = envelope()
		wrapped = rp.private_connection({**connection(), "runtime_profile": raw})
		self.assertEqual(wrapped.runtime_profile, raw)
		self.assertEqual(json.loads(json.dumps(wrapped)), connection())
		self.assertNotIn("runtime_profile", dict(wrapped))

	def test_old_admin_response_unchanged(self):
		data = {"chat_readiness": "Provisioning"}
		self.assertIs(rp.private_connection(data), data)

	def test_transport_negotiates_then_removes_profile_before_returning(self):
		with patch.object(frappe, "conf", frappe._dict()):
			from jarvis import admin_client

		response = Mock(status_code=200)
		response.json.return_value = {
			"message": {"ok": True, "data": {**connection(), "runtime_profile": envelope()}}
		}
		headers = {"Authorization": "Bearer test-only"}
		with patch.object(admin_client.requests, "post", return_value=response) as post:
			result = admin_client._do_post("https://admin.test/api", {}, headers, 8, "https://admin.test")
			self.assertEqual(post.call_args.kwargs["headers"]["X-Jarvis-Runtime-Profile"], "1")
		self.assertNotIn("X-Jarvis-Runtime-Profile", headers)
		self.assertEqual(json.loads(frappe.as_json(result)), connection())
		self.assertEqual(result.runtime_profile, envelope())


class TestRuntimeProfileStorage(unittest.TestCase):
	def setUp(self):
		self.local = SimpleNamespace()
		self.db = Mock()
		self.cache = Mock()
		self.cache.get_value.return_value = None
		self.db.get_value.return_value = None
		self.anchor = {**connection(), "admin_url": "https://admin.test", "customer": "test@example.test"}
		self.settings = frappe._dict(self.anchor)
		self.patches = [
			patch.object(rp.frappe, "local", self.local),
			patch.object(rp.frappe, "db", self.db, create=True),
			patch.object(rp.frappe, "cache", return_value=self.cache),
			patch.object(rp.frappe, "get_single", return_value=self.settings),
			patch.object(rp, "_anchor", return_value=self.anchor),
		]
		for p in self.patches:
			p.start()
			self.addCleanup(p.stop)

	def record(self):
		return {"anchor": self.anchor, "profile": envelope(), "fetched_at": "2026-09-25T00:00:00"}

	def test_cold_legacy_site_uses_fallback_without_caching_absence(self):
		self.assertEqual(rp.get_profile(), rp.legacy_profile())
		self.cache.set_value.assert_not_called()

	def test_redis_loss_uses_database_and_pins_snapshot(self):
		self.cache.get_value.side_effect = ConnectionError
		self.cache.set_value.side_effect = ConnectionError
		self.db.get_value.return_value = json.dumps(self.record())
		first = rp.get_profile()
		self.assertIs(rp.get_profile(), first)
		self.db.get_value.assert_called_once()
		self.assertNotIn("for_update", self.db.get_value.call_args.kwargs)

	def test_warm_cache_uses_one_durable_receipt_read_per_operation(self):
		blob = json.dumps(self.record())
		self.db.get_value.return_value = blob
		self.cache.get_value.return_value = {
			"digest": hashlib.sha256(blob.encode()).hexdigest(),
			"anchor": self.anchor,
			"profile": rp.legacy_profile(),
		}
		with patch.object(rp, "validate", side_effect=AssertionError("warm cache revalidated")):
			self.assertEqual(rp.get_profile(), rp.legacy_profile())
			rp.get_profile()
		self.db.get_value.assert_called_once()

	def test_site_binding_mismatch_fails_instead_of_falling_back(self):
		record = copy.deepcopy(self.record())
		record["anchor"]["customer"] = "other@example.test"
		self.db.get_value.return_value = json.dumps(record)
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()

	def test_late_cache_publication_cannot_resurrect_blocked_profile(self):
		old_blob = json.dumps(self.record())
		self.cache.get_value.return_value = {
			"digest": hashlib.sha256(old_blob.encode()).hexdigest(),
			"anchor": self.anchor,
			"profile": rp.legacy_profile(),
		}
		self.db.get_value.return_value = json.dumps({**self.record(), "unavailable": True})
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()

	def test_corrupt_durable_record_fails_instead_of_falling_back(self):
		self.db.get_value.return_value = "not json"
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()
		self.cache.set_value.assert_not_called()

	def test_invalid_refresh_preserves_database_and_cache(self):
		with self.assertRaises(rp.RuntimeProfileError):
			rp.persist({**envelope(), "revision": "bad"}, self.settings)
		self.db.set_value.assert_not_called()
		self.cache.delete_value.assert_not_called()

	def test_first_write_uses_private_namespace_and_after_commit_invalidation(self):
		with patch.object(rp.frappe, "get_doc") as get_doc:
			rp.persist(envelope(), self.settings)
			doc = get_doc.call_args.args[0]
			self.assertEqual((doc["name"], doc["parent"], doc["defkey"]), (rp.ROW, rp.PARENT, rp.KEY))
			get_doc.return_value.db_insert.assert_called_once_with()
		self.db.get_singles_dict.assert_called_with("Jarvis Settings", for_update=True)
		self.cache.set_value.assert_not_called()
		self.cache.delete_value.assert_not_called()
		self.db.after_commit.add.assert_called_once_with(rp._invalidate)
		self.db.after_rollback.add.assert_called_once_with(rp._forget_snapshot)
		rp._invalidate()
		self.cache.delete_value.assert_called_once_with(rp.CACHE_KEY)

	def test_existing_row_is_updated_without_hash_autoname_duplicate_insert(self):
		self.db.get_value.return_value = frappe._dict(parent=rp.PARENT, defkey=rp.KEY, defvalue="old")
		with patch.object(rp.frappe, "get_doc") as get_doc:
			rp.persist(envelope(), self.settings)
			get_doc.assert_not_called()
		self.db.set_value.assert_called_once()

	def test_readiness_poll_does_not_ingest_different_assignment(self):
		data = rp.ConnectionData({**connection(), "tenant_authority_generation": 3}, envelope())
		with patch.object(rp, "persist") as persist:
			rp.ingest_current(data)
			persist.assert_not_called()

	def test_explicit_unsupported_runtime_blocks_without_erasing_last_good(self):
		self.db.get_value.return_value = frappe._dict(
			parent=rp.PARENT, defkey=rp.KEY, defvalue=json.dumps(self.record())
		)
		rp.persist(None, self.settings, unavailable=True)
		written = json.loads(self.db.set_value.call_args.args[3])
		self.assertTrue(written["unavailable"])
		self.assertEqual(written["profile"], envelope())
		with self.assertRaises(rp.RuntimeProfileError):
			rp.get_profile()

	def test_explicit_null_profile_is_not_an_old_admin_response(self):
		data = rp.private_connection({**connection(), "runtime_profile": None})
		with self.assertRaises(rp.RuntimeProfileError):
			rp.ingest_current(data)
		self.db.set_value.assert_not_called()

	def test_status_is_value_free(self):
		self.db.get_value.return_value = json.dumps(self.record())
		result = rp.status()
		self.assertEqual(result["state"], "configured")
		self.assertEqual(set(result), {"state", "profile_id", "revision", "fetched_at"})
