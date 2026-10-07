"""Real receipt transactions; dispatch itself is stubbed (no ERP/LLM side effects).

Run on a dedicated .test site after migrate. These tests intentionally commit,
roll back, and open concurrent connections; never run on a production site.
"""

import json
import threading
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import api
from jarvis.chat import send_requests as receipts


class TestChatSendRequests(unittest.TestCase):
	def keep_patch(self, context):
		value = context.start()
		self.addCleanup(context.stop)
		return value

	def setUp(self):
		if not getattr(frappe.local, "site", "").endswith(".test"):
			self.skipTest("requires a dedicated .test database")
		self.user = frappe.session.user
		self.request_id = uuid.uuid4().hex
		self.keys = []
		self.success = {"ok": True, "conversation_id": "A", "message_id": "M", "run_id": "R"}
		self.real_send = api.send_message
		self.dispatch = self.keep_patch(patch.object(api, "send_message", return_value=self.success))
		self.owned = self.keep_patch(patch.object(api, "_get_owned_conversation"))
		self.access = self.keep_patch(patch.object(receipts, "require_jarvis_access"))

	def tearDown(self):
		frappe.set_user(self.user)
		frappe.db.rollback()
		for key in self.keys:
			frappe.db.delete(receipts.DOCTYPE, {"name": key})
		frappe.db.commit()

	def send(self, **kwargs):
		self.keys.append(receipts._key(self.request_id))
		return receipts.send_message(self.request_id, conversation="A", message="private invoice", **kwargs)

	def test_duplicate_and_lost_ack_reuse_committed_result(self):
		first = self.send()
		frappe.db.commit()
		self.assertEqual(receipts.check_delivery(self.request_id), first)
		self.assertEqual(self.send(), first)
		self.dispatch.assert_called_once()
		row = receipts._read(self.keys[0])
		self.assertNotIn("private invoice", json.dumps(row))

	def test_every_payload_field_is_bound_to_the_id(self):
		self.send()
		frappe.db.commit()
		for change in (
			{"attachments": '[{"file_url":"/private/files/other.pdf"}]'},
			{"context": '{"doctype":"Sales Invoice","name":"OTHER"}'},
			{"approval_tokens": '["other"]'},
			{"model_override": "other"},
			{"thinking_override": "high"},
			{"voice": True},
			{"auto_mode": 1},
		):
			with self.subTest(change=next(iter(change))):
				self.assertEqual(self.send(**change)["reason"], "request_mismatch")
		self.dispatch.assert_called_once()

	def test_failure_after_dispatch_survives_rollback_and_does_not_replay(self):
		self.dispatch.side_effect = RuntimeError("effect may have happened")
		with self.assertRaises(RuntimeError):
			self.send()
		frappe.db.rollback()
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.dispatch.assert_called_once()

	def test_result_rollback_keeps_durable_pending_claim(self):
		self.send()
		frappe.db.rollback()
		self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.dispatch.assert_called_once()

	def test_unknown_and_changed_user_cannot_read_or_replay_original_receipt(self):
		self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		self.send()
		frappe.db.commit()
		frappe.set_user("Guest")  # access mocked here: prove independent user keying
		self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		frappe.set_user(self.user)
		self.owned.side_effect = frappe.PermissionError()
		with self.assertRaises(frappe.PermissionError):
			receipts.check_delivery(self.request_id)
		with self.assertRaises(frappe.PermissionError):
			self.send()
		self.dispatch.assert_called_once()

	def test_access_and_identifier_checks_precede_claim(self):
		self.access.side_effect = frappe.PermissionError()
		with self.assertRaises(frappe.PermissionError):
			self.send()
		self.access.side_effect = None
		with self.assertRaises(frappe.ValidationError):
			receipts.send_message("invalid")
		self.assertFalse(receipts._read(receipts._key(self.request_id)))
		self.dispatch.assert_not_called()

	def test_deleted_conversation_stays_unknown(self):
		self.send()
		frappe.db.commit()
		self.owned.side_effect = frappe.DoesNotExistError()
		self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.dispatch.assert_called_once()

	def test_typed_partial_confirmation_is_settled_without_storing_business_results(self):
		self.dispatch.return_value = {
			"ok": False,
			"confirmed": True,
			"conversation_id": "A",
			"results": [{"private": "record"}],
		}
		self.send()
		frappe.db.commit()
		r = receipts.check_delivery(self.request_id)
		self.assertTrue(r["result"]["confirmed"])
		self.assertNotIn("results", r["result"])
		self.send()
		self.dispatch.assert_called_once()

	def test_concurrent_duplicate_cannot_dispatch_and_cannot_read_uncommitted_result(self):
		site, sites_path = frappe.local.site, frappe.local.sites_path
		self.keys.append(receipts._key(self.request_id))
		entered, release = threading.Event(), threading.Event()
		errors = []

		settle = receipts._settle

		def hold_settlement(key, result):
			settle(key, result)
			entered.set()
			if not release.wait(10):
				raise TimeoutError("test release")

		self.keep_patch(patch.object(receipts, "_settle", side_effect=hold_settlement))

		def winner():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user(self.user)
			try:
				receipts.send_message(self.request_id, conversation="A", message="private invoice")
				frappe.db.commit()
			except Exception as exc:
				errors.append(exc)
			finally:
				frappe.destroy()

		thread = threading.Thread(target=winner)
		thread.start()
		try:
			self.assertTrue(entered.wait(10))
			self.assertEqual(self.send(), {"delivery": "unknown"})
			self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
			frappe.db.commit()  # release duplicate's short read lock
		finally:
			release.set()
			thread.join(15)
		self.assertFalse(thread.is_alive())
		self.assertEqual(errors, [])
		frappe.db.rollback()  # fresh repeatable-read snapshot
		self.assertEqual(receipts.check_delivery(self.request_id)["result"], self.success)
		self.dispatch.assert_called_once()

	def test_lost_claim_commit_ack_never_dispatches_or_reclaims(self):
		commit = frappe.db.commit

		def lost_ack():
			commit()
			raise ConnectionError("commit ack lost")

		with patch.object(frappe.db, "commit", side_effect=lost_ack):
			with self.assertRaises(ConnectionError):
				self.send()
		frappe.db.rollback()
		self.assertEqual(self.send(), {"delivery": "unknown"})
		self.dispatch.assert_not_called()

	def test_missing_receipt_schema_fails_closed_before_dispatch(self):
		with patch.object(receipts, "_read", side_effect=RuntimeError("schema unavailable")):
			with self.assertRaises(RuntimeError):
				self.send()
			self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		self.dispatch.assert_not_called()

	def test_simultaneous_missing_claims_use_unique_insert_without_gap_locks(self):
		site, sites_path = frappe.local.site, frappe.local.sites_path
		read = receipts._read
		for same_id in (True, False):
			with self.subTest(same_id=same_id):
				self.dispatch.reset_mock()
				ids = [uuid.uuid4().hex, uuid.uuid4().hex]
				if same_id:
					ids[1] = ids[0]
				self.keys.extend(receipts._key(i) for i in ids)
				barrier, local = threading.Barrier(2), threading.local()
				errors = []

				def racing_read(key, **kwargs):
					value = read(key, **kwargs)
					if not getattr(local, "ready", False):
						local.ready = True
						barrier.wait(10)  # both transactions observed a missing key
					return value

				def worker(request_id):
					frappe.init(site=site, sites_path=sites_path)
					frappe.connect()
					frappe.set_user(self.user)
					try:
						receipts.send_message(request_id, conversation="A", message="private invoice")
						frappe.db.commit()
					except Exception as exc:
						errors.append(exc)
					finally:
						frappe.destroy()

				with patch.object(receipts, "_read", side_effect=racing_read):
					threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
					for thread in threads:
						thread.start()
					for thread in threads:
						thread.join(15)
					self.assertTrue(all(not t.is_alive() for t in threads))
				self.assertEqual(errors, [])
				self.assertEqual(self.dispatch.call_count, 1 if same_id else 2)

	def test_pre_effect_refusal_is_settled_but_post_effect_failure_is_unknown(self):
		def refuse(**kwargs):
			frappe.flags.jarvis_send_receipt_guard["safe"] = True
			raise frappe.ValidationError("This is a macro run. Start a new chat.")

		self.dispatch.side_effect = refuse
		result = self.send()
		self.assertEqual(result["delivery"], "settled")
		self.assertFalse(result["result"]["ok"])
		self.assertIn("macro run", result["result"]["message"])
		frappe.db.commit()
		self.assertFalse(receipts.check_delivery(self.request_id)["result"]["ok"])
		self.send()
		self.dispatch.assert_called_once()

	def test_receipt_filters_private_summaries_and_preserves_confirmation_failure(self):
		self.dispatch.return_value = {
			"ok": False,
			"confirmed": True,
			"conversation_id": "A",
			"error": {"type": "InvalidConfirmation", "message": "private business text"},
			"typed_rejection": {"discarded": [{"token": "T", "summary": "private invoice"}], "skipped": []},
		}
		self.send()
		frappe.db.commit()
		row = receipts._read(self.keys[0])
		self.assertNotIn("private", row.result_json)
		result = receipts.check_delivery(self.request_id)["result"]
		self.assertEqual(result["error"]["type"], "InvalidConfirmation")
		self.assertEqual(result["typed_rejection"]["discarded"], [{"token": "T"}])

	def test_wipe_keeps_only_tombstone_and_cannot_resurrect_or_replay(self):
		self.send()
		frappe.db.commit()
		from jarvis import onboarding

		# Exercise the actual wipe entry point without deleting unrelated test-site data.
		with patch.object(onboarding, "_WIPE_DOCTYPES", ()):
			onboarding._wipe_workspace_content()
		frappe.db.commit()
		row = receipts._read(self.keys[0])
		self.assertEqual(row.state, "erased")
		self.assertFalse(row.result_json)
		self.assertFalse(row.conversation)
		self.assertFalse(row.fingerprint)
		receipts._settle(self.keys[0], self.success)
		frappe.db.commit()
		self.assertEqual(receipts._read(self.keys[0]).state, "erased")
		self.assertEqual(self.send()["delivery"], "unknown")
		self.dispatch.assert_called_once()

	def test_real_pipeline_macro_refusal_keeps_actionable_reason(self):
		self.dispatch.side_effect = self.real_send
		self.owned.return_value = frappe._dict(name="A", skip_confirmation=1, origin_page="")
		with (
			patch.object(api, "has_jarvis_access", return_value=True),
			patch.object(api, "validate_can_send", return_value=(True, None)),
		):
			result = self.send()
		self.assertFalse(result["result"]["ok"])
		self.assertEqual(result["result"]["reason"], "macro_run")
		frappe.db.commit()
		self.assertIn("Start a new chat", receipts.check_delivery(self.request_id)["result"]["message"])

	def test_real_effect_boundary_prevents_rejection_after_possible_work(self):
		def uncertain(**kwargs):
			frappe.flags.jarvis_send_receipt_guard["safe"] = True
			api._send_receipt_effect_boundary()
			raise frappe.ValidationError("too late to prove no effect")

		self.dispatch.side_effect = uncertain
		with self.assertRaises(frappe.ValidationError):
			self.send()
		frappe.db.rollback()
		self.assertEqual(receipts.check_delivery(self.request_id), {"delivery": "unknown"})
		self.send()
		self.dispatch.assert_called_once()
