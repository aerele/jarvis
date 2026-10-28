"""File Box card results link actual readable drafts, without bypassing approval."""

from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import filebox_cards
from jarvis.tests._filebox_invoice_helpers import InvoiceFixture
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.test_filebox_cards import _card
from jarvis.tests.test_filebox_held import CONV, MSG, OTHER, OWNER, PARTY, TAX, _Base


class TestNativeDraftLinks(_Base):
	def setUp(self):
		super().setUp()
		self.roles = set(frappe.get_roles(OWNER))
		ensure_user(OWNER, ("Jarvis User", "Purchase Master Manager", "Accounts Manager", "Stock User"))
		self.addCleanup(self.restore_roles)
		self.fixture = InvoiceFixture()
		self.addCleanup(self.fixture.cleanup)
		self.fixture.setup()
		self.addCleanup(self.drop_questions)

	def drop_questions(self):
		frappe.db.delete("Jarvis Approval Request", {"owner": OWNER, "source": "File Box"})
		frappe.db.commit()

	def restore_roles(self):
		frappe.set_user("Administrator")
		added = set(frappe.get_roles(OWNER)) - self.roles
		if added:
			frappe.db.delete("Has Role", {"parent": OWNER, "role": ["in", list(added)]})
			frappe.clear_cache(user=OWNER)
		frappe.db.commit()

	def reply(self, conversation, card):
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": 2,
				"role": "assistant",
				"content": _card(**card),
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def invoice_card(self):
		return {
			"doctype": "Purchase Invoice",
			"fields": {k: v for k, v in self.fixture.values.items() if k != "items"},
			"tables": {"items": self.fixture.values["items"]},
		}

	def assert_linked_draft(self, conv, msg, note):
		name = frappe.db.get_value(CONV, conv, "filebox_result_name")
		self.assertTrue(
			name,
			(
				note,
				frappe.get_all("Jarvis Approval Request", filters={"conversation": conv}, pluck="context_md"),
			),
		)
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertEqual((doc.docstatus, doc.grand_total, doc.company), (0, 100, self.fixture.company))
		self.assertIn(f"[{name}](/app/purchase-invoice/{quote(name, safe='').replace('.', '%2E')})", note)
		content = frappe.db.get_value(MSG, msg, "content")
		self.assertIn(note, content)
		self.assertNotIn("jarvis-action", content)
		self.assertIsNone(filebox_cards.park_from_turn(conv, msg))
		return name

	def test_turn_end_saves_one_linked_draft_with_auto_mode_off_or_on(self):
		for mode in (0, 1):
			with self.subTest(auto_mode=mode):
				conv = self.conv()
				frappe.db.set_value(CONV, conv, "skip_confirmation", mode)
				msg = self.reply(conv, self.invoice_card())
				# The finalize worker is Administrator; the check must use the owner.
				with patch("frappe.has_permission", wraps=frappe.has_permission) as permission:
					note = filebox_cards.park_from_turn(conv, msg)
				self.assert_linked_draft(conv, msg, note)
				self.assertTrue(any(c.kwargs.get("user") == OWNER for c in permission.call_args_list))
		self.assertEqual(frappe.db.count("Purchase Invoice", {"company": self.fixture.company}), 2)
		self.assertEqual(self.rows(), [])

	def test_confirm_card_links_a_draft_and_cannot_replay(self):
		conv = self.conv()
		card = self.invoice_card()
		msg = self.reply(conv, card)
		res = filebox_cards.park_confirmed(
			conv, "create", "Purchase Invoice", "", self.fixture.values, card=card, message=msg
		)
		self.assertTrue(res["ok"], res)
		self.assert_linked_draft(conv, msg, res["note"])
		replay = filebox_cards.park_confirmed(
			conv, "create", "Purchase Invoice", "", self.fixture.values, card=card, message=msg
		)
		self.assertFalse(replay["ok"])
		self.assertEqual(frappe.db.count("Purchase Invoice", {"company": self.fixture.company}), 1)

	def test_own_draft_update_keeps_link_and_master_creation_still_waits(self):
		conv = self.conv()
		msg = self.reply(conv, self.invoice_card())
		name = self.assert_linked_draft(conv, msg, filebox_cards.park_from_turn(conv, msg))
		msg = self.reply(
			conv,
			{
				"doctype": "Purchase Invoice",
				"verb": "update",
				"name": name,
				"fields": {"remarks": "Reviewed draft"},
			},
		)
		note = filebox_cards.park_from_turn(conv, msg)
		self.assertTrue(note.startswith("Updated Purchase Invoice ["), note)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", name, "remarks"), "Reviewed draft")
		for mode in (0, 1):
			with self.subTest(auto_mode=mode):
				conv = self.conv()
				frappe.db.set_value(CONV, conv, "skip_confirmation", mode)
				msg = self.reply(
					conv, {"doctype": "Supplier", "fields": {"supplier_name": PARTY, "tax_id": TAX}}
				)
				note = filebox_cards.park_from_turn(conv, msg)
				self.assertIn("Approval Board", note)
				self.assertNotIn("/app/", note)
				self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": PARTY}))

	def test_native_record_is_not_linked_for_a_requester_without_read_permission(self):
		conv = self.conv()
		msg = self.reply(conv, self.invoice_card())
		name = self.assert_linked_draft(conv, msg, filebox_cards.park_from_turn(conv, msg))
		result = {"ok": True, "data": {"doctype": "Purchase Invoice", "name": name}}
		self.assertTrue(frappe.has_permission("Purchase Invoice", "read", doc=name, user="Administrator"))
		self.assertFalse(frappe.has_permission("Purchase Invoice", "read", doc=name, user=OTHER))
		self.assertEqual(
			filebox_cards._note("create_doc", result, None, user=OTHER), f"Created Purchase Invoice {name}."
		)

	def test_a_draft_created_elsewhere_still_waits_for_approval(self):
		doc = self.fixture.insert("Purchase Invoice", **self.fixture.values)
		frappe.db.commit()
		conv = self.conv()
		msg = self.reply(
			conv,
			{
				"doctype": "Purchase Invoice",
				"verb": "update",
				"name": doc.name,
				"fields": {"remarks": "Do not apply"},
			},
		)
		note = filebox_cards.park_from_turn(conv, msg)
		self.assertIn("Approval Board", note)
		self.assertNotIn("/app/", note)
		self.assertNotEqual(frappe.db.get_value("Purchase Invoice", doc.name, "remarks"), "Do not apply")


class TestResultNoteBoundaries(FrappeTestCase):
	def test_unsaved_unreadable_and_markdown_labels_remain_plain(self):
		for name in ("Q]1", "N*1", "a`b", "a~b", "a\\b", "a\nb", "a[b"):
			with (
				self.subTest(name=name),
				patch("frappe.db.exists", return_value=True),
				patch("frappe.has_permission", return_value=True),
			):
				note = filebox_cards._note(
					"create_doc",
					{"ok": True, "data": {"doctype": "Supplier", "name": name}},
					None,
					user=OWNER,
				)
				self.assertNotIn("/app/", note)
		for exists, readable in ((False, True), (True, False)):
			with (
				self.subTest(exists=exists, readable=readable),
				patch("frappe.db.exists", return_value=exists),
				patch("frappe.has_permission", return_value=readable),
			):
				note = filebox_cards._note(
					"create_doc",
					{"ok": True, "data": {"doctype": "Supplier", "name": "SUP-1"}},
					None,
					user=OWNER,
				)
				self.assertEqual(note, "Created Supplier SUP-1.")

	def test_safe_names_use_canonical_encoded_desk_paths_as_requester(self):
		for name in ("SUPP/001", "A B", "X(1)", 'A&B <x> "q"', "供应商", "ST-1.", "ACME Pvt. Ltd.", "A.."):
			with (
				self.subTest(name=name),
				patch("frappe.db.exists", return_value=True),
				patch("frappe.has_permission", return_value=True) as permission,
			):
				note = filebox_cards._note(
					"create_doc",
					{"ok": True, "data": {"doctype": "Supplier", "name": name}},
					None,
					user=OWNER,
				)
				self.assertEqual(
					note,
					f"Created Supplier [{name}](/app/supplier/{quote(name, safe='').replace('.', '%2E')}).",
				)
				permission.assert_called_once_with("Supplier", "read", doc=name, user=OWNER)

	def test_pending_refused_or_no_target_never_becomes_a_link(self):
		cases = [
			({"ok": False}, None),
			({"ok": True, "data": {"held_for": "approval_board", "name": "SUP-1"}}, None),
			({"ok": True, "data": {}}, None),
			({"ok": True, "data": {"doctype": "Supplier", "name": "SUP-1"}}, "question"),
		]
		for result, asked in cases:
			with self.subTest(result=result), patch("frappe.has_permission") as permission:
				note = filebox_cards._note("create_doc", result, asked, user=OWNER)
				self.assertNotIn("/app/", note)
				permission.assert_not_called()

	def test_deleted_or_permission_revoked_during_lookup_keeps_plain_note(self):
		for error in (frappe.DoesNotExistError, frappe.PermissionError):
			with (
				self.subTest(error=error),
				patch("frappe.db.exists", return_value=True),
				patch("frappe.has_permission", side_effect=error),
			):
				note = filebox_cards._note(
					"create_doc",
					{"ok": True, "data": {"doctype": "Supplier", "name": "SUP-1"}},
					None,
					user=OWNER,
				)
				self.assertEqual(note, "Created Supplier SUP-1.")

	def test_legacy_helper_call_retains_plain_result(self):
		self.assertEqual(
			filebox_cards._note(
				"update_doc", {"ok": True, "data": {"doctype": "Supplier", "name": "SUP-1"}}, None
			),
			"Updated Supplier SUP-1.",
		)
