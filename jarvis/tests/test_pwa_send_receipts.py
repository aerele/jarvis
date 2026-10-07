"""Boundary tests: actual receipt wrapper with a synthetic Redis/API boundary.

Does not establish database/Redis integration or ERP action correctness.
"""

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jarvis.chat
from jarvis.chat import pwa_send


class Cache:
	def __init__(self):
		self.values = {}
		self.fail_set = False
		self.fail_get = False

	def make_key(self, key):
		return "test-site:" + key

	def get(self, key):
		if self.fail_get:
			raise ConnectionError("cache unavailable")
		return self.values.get(key)

	def set(self, key, value, ex=None, nx=False):
		if self.fail_set:
			raise ConnectionError("cache unavailable")
		assert ex == pwa_send.RETENTION_SECONDS
		if nx and key in self.values:
			return False
		self.values[key] = value
		return True


class TestPwaSendReceipts(unittest.TestCase):
	def setUp(self):
		self.cache = Cache()
		self.api = SimpleNamespace(
			_get_owned_conversation=Mock(),
			send_message=Mock(
				return_value={
					"ok": True,
					"conversation_id": "A",
					"message_id": "M",
					"run_id": "R",
				}
			),
		)
		self.after_commit = []
		self.frappe = SimpleNamespace(
			cache=lambda: self.cache,
			session=SimpleNamespace(user="alice@example.test"),
			db=SimpleNamespace(commit=Mock(), after_commit=SimpleNamespace(add=self.after_commit.append)),
			DoesNotExistError=LookupError,
			throw=Mock(side_effect=ValueError("invalid request")),
		)
		for p in (
			patch.object(pwa_send, "frappe", self.frappe),
			patch.object(pwa_send, "require_jarvis_access"),
			patch.object(pwa_send, "refuse_in_tool_dispatch"),
			patch.object(jarvis.chat, "api", self.api, create=True),
			patch.dict(sys.modules, {"jarvis.chat.api": self.api}),
		):
			p.start()
			self.addCleanup(p.stop)
		self.request_id = "a" * 32

	def send(self, commit=True, **kwargs):
		result = pwa_send.send_message(self.request_id, conversation="A", message="private invoice", **kwargs)
		if commit:
			self.finish_transaction()
		return result

	def finish_transaction(self):
		callbacks, self.after_commit[:] = self.after_commit[:], []
		self.frappe.db.commit()
		for callback in callbacks:
			callback()

	def test_lost_ack_can_be_checked_without_another_send(self):
		result = self.send(attachments='[{"file_url":"/private/files/invoice.pdf"}]')
		self.assertEqual(pwa_send.check_delivery(self.request_id), result)
		self.assertEqual(self.api.send_message.call_count, 1)
		stored = next(iter(self.cache.values.values()))
		self.assertNotIn("private invoice", stored)
		self.assertNotIn("/private/files", stored)
		self.frappe.db.commit.assert_called_once()

	def test_duplicate_completed_id_returns_existing_outcome(self):
		self.assertEqual(self.send(), self.send())
		self.assertEqual(self.api.send_message.call_count, 1)

	def test_concurrent_duplicate_cannot_execute(self):
		def during_send(**kwargs):
			self.assertEqual(self.send(), {"delivery": "unknown"})
			return {"ok": False, "reason": "maintenance"}

		self.api.send_message.side_effect = during_send
		self.assertEqual(self.send()["result"]["reason"], "maintenance")
		self.assertEqual(self.api.send_message.call_count, 1)

	def test_same_id_with_changed_payload_does_not_replay(self):
		self.send()
		result = pwa_send.send_message(self.request_id, conversation="A", message="changed")
		self.assertEqual(result, {"delivery": "unknown"})
		self.assertEqual(self.api.send_message.call_count, 1)

	def test_exception_after_possible_execution_stays_unknown(self):
		self.api.send_message.side_effect = RuntimeError("after commit")
		with self.assertRaises(RuntimeError):
			self.send()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.assertEqual(self.api.send_message.call_count, 1)

	def test_commit_failure_cannot_publish_success(self):
		self.send(commit=False)
		self.frappe.db.commit.assert_not_called()
		self.frappe.db.commit.side_effect = RuntimeError("commit failed")
		with self.assertRaises(RuntimeError):
			self.finish_transaction()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})

	def test_typed_partial_confirmation_is_not_a_rejected_send(self):
		self.api.send_message.return_value = {
			"ok": False,
			"confirmed": True,
			"conversation_id": "A",
			"results": [{"private": "record"}],
		}
		self.send()
		checked = pwa_send.check_delivery(self.request_id)
		self.assertTrue(checked["result"]["confirmed"])
		self.assertFalse(checked["result"]["ok"])
		self.assertNotIn("results", checked["result"])

	def test_missing_expired_and_unavailable_receipts_are_unknown(self):
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
		self.send()
		self.cache.values.clear()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
		self.cache.fail_get = True
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})

	def test_cache_claim_failure_does_not_send(self):
		self.cache.fail_set = True
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.api.send_message.assert_not_called()

	def test_result_cache_failure_does_not_erase_real_response(self):
		def send(**kwargs):
			self.cache.fail_set = True
			return {"ok": False, "reason": "maintenance"}

		self.api.send_message.side_effect = send
		self.assertEqual(self.send()["result"]["reason"], "maintenance")
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})

	def test_another_user_cannot_read_receipt(self):
		self.send()
		self.frappe.session.user = "bob@example.test"
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})

	def test_permission_checks_apply_to_send_and_read(self):
		self.send()
		self.api._get_owned_conversation.side_effect = PermissionError()
		with self.assertRaises(PermissionError):
			pwa_send.check_delivery(self.request_id)
		with self.assertRaises(PermissionError):
			self.send()
		self.assertEqual(self.api.send_message.call_count, 1)
		pwa_send.require_jarvis_access.side_effect = PermissionError()
		with self.assertRaises(PermissionError):
			pwa_send.check_delivery("b" * 32)

	def test_invalid_id_is_rejected_before_cache_or_execution(self):
		with self.assertRaises(ValueError):
			pwa_send.send_message("bad/id", conversation="A")
		self.assertFalse(self.cache.values)
		self.api.send_message.assert_not_called()

	def test_result_is_not_visible_before_commit(self):
		result = self.send(commit=False)
		self.frappe.db.commit.assert_not_called()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
		self.finish_transaction()
		self.assertEqual(pwa_send.check_delivery(self.request_id), result)

	def test_deleted_conversation_is_unknown_but_permission_error_propagates(self):
		self.send()
		self.api._get_owned_conversation.side_effect = LookupError()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
		self.api._get_owned_conversation.side_effect = PermissionError()
		with self.assertRaises(PermissionError):
			pwa_send.check_delivery(self.request_id)

	def test_original_send_owns_conversation_access_validation(self):
		self.api.send_message.side_effect = PermissionError()
		with self.assertRaises(PermissionError):
			self.send()
		self.api._get_owned_conversation.assert_not_called()
		self.assertFalse(self.after_commit)

	def test_rollback_discards_receipt_callback(self):
		self.send(commit=False)
		self.after_commit.clear()  # Frappe rollback clears the after_commit manager.
		self.finish_transaction()
		self.assertEqual(pwa_send.check_delivery(self.request_id), {"delivery": "unknown"})
