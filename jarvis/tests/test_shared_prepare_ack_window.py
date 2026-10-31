"""#773 Step 6 for shared chats: the shared producer stores the same size-based
``chat.send`` ack window as personal prepare.

The agent ingests the whole message before it acks, so a big send needs a longer
window (legacy ``turn_handler._ack_timeout_s``: 30s, +30s per image part, +10s per
10k inlined characters, at most 180s). Personal prepare stores it in the dispatch
payload; the shared producer (``collaboration_workers.build_payload``) must store
the same value from the ACTOR's real assembled prompt, without changing anything
else it stores. The shared pump waits that long after the frame is written; a
payload from before the key existed keeps the 15s window.

Driven through the real ``prepare.run_prepare`` on the existing shared-prepare
fixture (``test_chat_collaboration_prepare``), real prompt assembly under the
actor's permissions, real File records owned by the actor.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

import frappe

from jarvis.chat import prepare, pump, turn_handler
from jarvis.tests import test_chat_collaboration_prepare as _shared_prepare
from jarvis.tests import test_chat_collaboration_pump as _shared_pump
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._share_helpers import RECIPIENT


def setUpModule():
	install_synthetic_runtime_profile()


def _png() -> bytes:
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, format="PNG")
	return buf.getvalue()


class TestSharedPrepareStoresAckWindow(_shared_prepare.TestCollaborationPrepare):
	def setUp(self):
		super().setUp()
		self.gateway.capacity = 400_000  # room for the image parts in the budget check
		self.assembled: list = []
		real = turn_handler.assemble_prompt

		def spy(*args, **kwargs):
			ap = real(*args, **kwargs)
			self.assembled.append(ap)
			return ap

		item = patch.object(turn_handler, "assemble_prompt", spy)
		item.start()
		self.addCleanup(item.stop)

	def _attach(self, files: list[tuple[str, bytes]]) -> None:
		attachments = []
		for name, content in files:
			doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": name,
					"content": content,
					"is_private": 1,
					"attached_to_doctype": "Jarvis Conversation",
					"attached_to_name": self.conversation,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			frappe.db.set_value("File", doc.name, "owner", RECIPIENT, update_modified=False)
			self.addCleanup(
				lambda n=doc.name: frappe.delete_doc("File", n, force=True, ignore_permissions=True)
			)
			attachments.append({"file_url": doc.file_url, "file_name": name})
		frappe.db.set_value(
			"Jarvis Chat Turn", self.turn.name, "dispatch_payload", json.dumps({"attachments": attachments})
		)
		frappe.db.commit()

	def _prepared(self) -> dict:
		result = prepare.run_prepare(self.turn.name)
		self.assertTrue(result.get("ready"), result)
		self.assertEqual(len(self.assembled), 1)
		return json.loads(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "dispatch_payload"))

	def _assert_actor_payload(self, payload: dict) -> None:
		# Nothing else the shared producer stores changes.
		self.assertEqual(payload["session_key"], self.bound.session_key)
		self.assertEqual(payload["chat_user"], RECIPIENT)
		self.assertEqual(payload["collaboration_runtime"]["model_ref"], "provider/actor-model")
		self.assertIsNone(payload["thinking"])
		self.assertNotIn("OWNER_PRIVATE_SESSION", json.dumps(payload))

	def test_plain_request_gets_the_base_window(self):
		payload = self._prepared()
		self._assert_actor_payload(payload)
		ap = self.assembled[0]
		self.assertEqual(ap.vision_parts, [])
		# The shared transcript is fenced into the prompt, so assembly counts it as
		# inlined text; the window follows the same input the legacy path uses.
		self.assertEqual(
			payload.get("ack_timeout_s"), turn_handler._ack_timeout_s(0, ap.inlined_prompt_chars)
		)
		self.assertGreaterEqual(payload["ack_timeout_s"], 30.0)
		self.assertLess(payload["ack_timeout_s"], 60.0)

	def test_inlined_text_extends_the_window(self):
		self._attach([("figures.txt", ("row,amount\n" + "line-item,1250.00\n" * 300).encode())])
		payload = self._prepared()
		ap = self.assembled[0]
		self.assertGreater(ap.inlined_prompt_chars, 5000, "premise: the text was inlined")
		self.assertEqual(
			payload.get("ack_timeout_s"), turn_handler._ack_timeout_s(0, ap.inlined_prompt_chars)
		)
		self.assertGreater(payload["ack_timeout_s"], 30.0)
		self._assert_actor_payload(payload)

	def _vision(self, count: int) -> dict:
		self._attach([(f"page-{i}.png", _png()) for i in range(count)])
		with (
			patch.object(turn_handler, "_vision_enabled", return_value=True),
			patch("jarvis.chat.vision.supports_vision", return_value=True),
		):
			payload = self._prepared()
		ap = self.assembled[0]
		self.assertEqual(len(ap.vision_parts), count, "premise: every image became a vision part")
		self.assertEqual(len(payload["attachments"]), count)
		self._assert_actor_payload(payload)
		return payload

	def test_three_images(self):
		payload = self._vision(3)
		ap = self.assembled[0]
		self.assertEqual(
			payload.get("ack_timeout_s"), turn_handler._ack_timeout_s(3, ap.inlined_prompt_chars)
		)
		self.assertGreaterEqual(payload["ack_timeout_s"], 120.0)

	def test_six_images_reach_the_ceiling(self):
		payload = self._vision(6)
		self.assertEqual(payload.get("ack_timeout_s"), 180.0)


class TestSharedPumpUsesStoredWindow(_shared_pump.TestCollaborationPump):
	"""The shared pump waits the stored window after the write; an old payload
	without the key keeps the existing 15s."""

	def _window(self, ack_timeout_s=None) -> float:
		if ack_timeout_s is not None:
			payload = json.loads(frappe.db.get_value("Jarvis Chat Turn", self.turn.name, "dispatch_payload"))
			payload["ack_timeout_s"] = ack_timeout_s
			frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "dispatch_payload", json.dumps(payload))
			frappe.db.commit()
		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		return self.context.pending_acks[self.turn.name].deadline - pump._monotonic()

	def test_old_payload_keeps_fifteen_seconds(self):
		self.assertAlmostEqual(self._window(), pump.ACK_TIMEOUT_S, delta=0.5)

	def test_stored_window_is_used(self):
		self.assertAlmostEqual(self._window(180.0), 180.0, delta=0.5)


def load_tests(loader, standard_tests, pattern):
	"""Run only the tests defined in this module's classes, not the inherited
	fixture suites (they run in their own modules)."""
	suite = unittest.TestSuite()

	def walk(tests):
		for t in tests:
			if isinstance(t, unittest.TestSuite):
				walk(t)
			elif t._testMethodName in type(t).__dict__:
				suite.addTest(t)

	walk(standard_tests)
	return suite
