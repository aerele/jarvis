"""Real transactional admission of actor/Auth/seed/Turn as one unit."""

import uuid
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import admission, pump
from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_test_site import require_collaboration_test_site
from jarvis.tests._share_helpers import OWNER, RECIPIENT, SHARE, as_user, make_chat, make_user


class TestCollaborationAdmission(FrappeTestCase):
	def setUp(self):
		require_collaboration_test_site(frappe.local.site)
		super().setUp()
		self.previous = frappe.session.user
		frappe.set_user("Administrator")
		self.before = frappe.db.get_single_value("Jarvis Settings", "chat_sharing_enabled")
		for user in (OWNER, RECIPIENT):
			make_user(user)
		self.conversation, _ = make_chat()
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
		from jarvis.chat.sharing import set_shares

		with as_user(OWNER):
			set_shares(self.conversation, [RECIPIENT])
		self.share = frappe.db.get_value(
			SHARE, {"conversation": self.conversation, "user": RECIPIENT}, "name"
		)
		frappe.db.set_value(SHARE, self.share, {"access_mode": "Chat", "access_revision": 2})
		frappe.db.commit()
		self.run = uuid.uuid4().hex
		self.patches = [
			patch.dict(
				frappe.conf,
				{
					"chat_collaboration_enabled": 1,
					"chat_collaboration_reader_floor": 1,
					"chat_collaboration_runtime_floor": 3,
					admission.FLAG: 1,
				},
			),
			patch.object(
				pump,
				"transport_predicates_from_row",
				return_value={"mode": "pump", "pump_active": True, "configured": True},
			),
			patch.object(pump, "ensure_pump"),
			patch.object(pump, "lpush_wake"),
			patch.object(admission, "_line_full", return_value=False),
		]
		for item in self.patches:
			item.start()

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		bindings = frappe.get_all(c.BINDING, {"conversation": self.conversation}, pluck="name")
		for binding in bindings:
			frappe.db.delete(c.SESSION, {"collaboration_binding": binding})
		frappe.db.delete(c.BINDING, {"conversation": self.conversation})
		frappe.delete_doc(c.CONVERSATION, self.conversation, ignore_permissions=True, force=True)
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", self.before)
		frappe.db.commit()
		for item in reversed(self.patches):
			item.stop()
		frappe.set_user(self.previous)
		super().tearDown()

	def accept(self, actor=RECIPIENT, run=None, **kwargs):
		from jarvis.chat.collaboration_admission import SharedAdmission

		return admission.accept_or_queue(
			conversation=self.conversation,
			run_id=run or self.run,
			seed_message=None,
			seed_content="recipient request",
			seed_origin="human",
			dispatch=Mock(),
			collaboration=SharedAdmission(actor),
			**kwargs,
		)

	def rows(self):
		binding = frappe.db.get_value(c.BINDING, {"logical_run_id": self.run}, "name")
		return (
			frappe.db.count(c.BINDING, {"logical_run_id": self.run}),
			frappe.db.count(c.SESSION, {"collaboration_binding": binding}) if binding else 0,
			frappe.db.count("Jarvis Chat Message", {"collaboration_binding": binding}) if binding else 0,
			frappe.db.count("Jarvis Chat Turn", {"name": self.run}),
		)

	def test_pump_admission_atomically_stamps_all_four_rows(self):
		result = self.accept()
		self.assertTrue(result["ok"])
		self.assertEqual(self.rows(), (1, 1, 1, 1))
		turn = frappe.get_doc("Jarvis Chat Turn", self.run)
		seed = frappe.get_doc("Jarvis Chat Message", turn.seed_message)
		self.assertEqual((turn.actor, seed.actor, seed.owner), (RECIPIENT,) * 3)
		self.assertEqual(turn.collaboration_binding, seed.collaboration_binding)
		self.assertEqual(turn.state, "queued")

	def test_same_request_replay_creates_no_duplicate_and_does_not_dispatch(self):
		self.accept()
		result = self.accept()
		self.assertTrue(result["duplicate"])
		self.assertEqual(self.rows(), (1, 1, 1, 1))

	def test_duplicate_cannot_be_replayed_by_another_actor(self):
		self.accept()
		with self.assertRaises(frappe.PermissionError):
			self.accept(actor=OWNER)
		self.assertEqual(self.rows(), (1, 1, 1, 1))

	def test_duplicate_after_revoke_does_not_reveal_or_resume_old_turn(self):
		self.accept()
		frappe.db.delete(SHARE, {"name": self.share})
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.accept()
		self.assertEqual(self.rows(), (1, 1, 1, 1))

	def test_overload_inserts_nothing_and_does_not_activate_thread(self):
		with patch.object(admission, "_line_full", return_value=True):
			result = self.accept()
		self.assertTrue(result["overloaded"])
		self.assertEqual(self.rows(), (0, 0, 0, 0))
		self.assertFalse(c.is_collaborative(self.conversation))

	def test_pure_legacy_fallback_refuses_instead_of_dispatching_without_turn(self):
		with patch.object(
			pump,
			"transport_predicates_from_row",
			return_value={"mode": "legacy", "pump_active": False, "configured": True},
		):
			with self.assertRaises(frappe.PermissionError):
				self.accept()
		self.assertEqual(self.rows(), (0, 0, 0, 0))

	def test_phase_zero_rq_path_keeps_same_identity_and_turn(self):
		with patch.object(
			pump,
			"transport_predicates_from_row",
			return_value={"mode": "legacy", "pump_active": False, "configured": False},
		):
			result = self.accept()
		self.assertTrue(result["ok"])
		self.assertEqual(self.rows(), (1, 1, 1, 1))

	def test_failure_at_each_insert_rolls_back_binding_auth_seed_turn_and_marker(self):
		from frappe.model.document import Document

		original = Document.insert
		for doctype in (c.BINDING, c.SESSION, "Jarvis Chat Message", "Jarvis Chat Turn"):

			def fail(doc, *args, **kwargs):
				if doc.doctype == doctype:
					raise RuntimeError("planted insert failure")
				return original(doc, *args, **kwargs)

			with self.subTest(doctype=doctype), patch.object(Document, "insert", fail):
				with self.assertRaises(RuntimeError):
					self.accept()
			self.assertEqual(self.rows(), (0, 0, 0, 0))
			self.assertFalse(c.is_collaborative(self.conversation))
