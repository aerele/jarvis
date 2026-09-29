"""A jarvis-action card in a File Box reply runs through the File Box policy (#617):
a master is held on the Approval Board, a refused write (a Single) becomes a board
question, and the card leaves the chat so it can't also be confirmed there."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from jarvis.chat import filebox_cards
from jarvis.tests._pending_action_helpers import draft_doctype
from jarvis.tests.test_filebox_held import CONV, MSG, OWNER, PARTY, TAX, _Base

AR = "Jarvis Approval Request"


def _card(**a) -> str:
	return f"Here is the change:\n\n```jarvis-action\n{json.dumps(a)}\n```\n"


class TestParse(_Base):
	def test_only_a_doc_create_or_update_is_a_card(self):
		self.assertIsNone(filebox_cards.parse_action("no fence"))
		self.assertIsNone(filebox_cards.parse_action(_card(kind="email", doctype="ToDo")))
		self.assertIsNone(filebox_cards.parse_action(_card(verb="submit", doctype="Supplier")))
		self.assertIsNone(filebox_cards.parse_action("```jarvis-action\n{broken\n```"))
		self.assertEqual(filebox_cards.parse_action(_card(doctype="Supplier"))["doctype"], "Supplier")

	def test_fields_map_by_fieldname_or_label_and_blanks_drop_on_create(self):
		tool, args = filebox_cards.tool_call(
			{
				"doctype": "Supplier",
				"fields": [
					{"label": "supplier_name", "value": PARTY},
					{"label": "Tax Id", "value": TAX},
					{"label": "supplier_group", "value": ""},
				],
			}
		)
		self.assertEqual(tool, "create_doc")
		self.assertEqual(args["values"], {"supplier_name": PARTY, "tax_id": TAX})

	def test_both_missing_field_refusals_get_the_second_call(self):
		from jarvis.chat import held_sheets, held_writes

		self.assertTrue(filebox_cards._first_miss(held_writes._FIRST_MISS.format(missing="x")))
		self.assertTrue(filebox_cards._first_miss(held_sheets._FIRST_TRY.format(problems="x")))
		self.assertFalse(filebox_cards._first_miss("An unattended File Box run can't write X."))

	def test_an_unknown_field_or_an_update_without_a_name_is_unreadable(self):
		self.assertIsNone(
			filebox_cards.tool_call({"doctype": "Supplier", "fields": [{"label": "no such", "value": 1}]})
		)
		self.assertIsNone(filebox_cards.tool_call({"doctype": "Supplier", "verb": "update", "fields": []}))


class TestParkFromTurn(_Base):
	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_questions)

	def _drop_questions(self):
		frappe.db.delete(AR, {"owner": OWNER, "source": "File Box"})
		frappe.db.commit()

	def reply(self, conv: str, content: str) -> str:
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": 2, "role": "assistant", "content": content}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def supplier_card(self) -> str:
		return _card(
			kind="doc",
			verb="create",
			doctype="Supplier",
			fields=[{"label": "supplier_name", "value": PARTY}, {"label": "tax_id", "value": TAX}],
		)

	def test_a_master_card_is_held_on_the_board_and_leaves_the_chat(self):
		conv = self.conv()
		msg = self.reply(conv, self.supplier_card())
		note = filebox_cards.park_from_turn(conv, msg)
		[row] = self.rows()
		self.assertEqual((row.kind, row.status, row.tool), ("file_box_held", "Pending", "create_doc"))
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": PARTY}))
		content = frappe.db.get_value(MSG, msg, "content")
		self.assertNotIn("jarvis-action", content)
		self.assertIn(note, content)
		self.assertIn("Approval Board", note)
		# re-run (a retried finalize): the card is gone, nothing is parked twice
		self.assertIsNone(filebox_cards.park_from_turn(conv, msg))
		self.assertEqual(len(self.rows()), 1)

	def test_a_single_settings_update_becomes_a_board_question(self):
		conv = self.conv()
		msg = self.reply(
			conv,
			_card(
				kind="doc",
				verb="update",
				doctype="Buying Settings",
				name="Buying Settings",
				fields=[{"label": "supp_master_name", "value": "Supplier Name"}],
			),
		)
		filebox_cards.park_from_turn(conv, msg)
		self.assertEqual(self.rows(), [])
		ar = frappe.get_all(
			AR,
			filters={"conversation": conv},
			fields=["status", "source", "owner", "document_type", "options", "context_md"],
		)
		self.assertEqual(len(ar), 1)
		self.assertEqual((ar[0].status, ar[0].source, ar[0].owner), ("Pending", "File Box", OWNER))
		self.assertEqual(ar[0].document_type, "Buying Settings")
		self.assertIn("supp_master_name", ar[0].context_md)
		self.assertEqual(json.loads(ar[0].options), ["Done", "Skip"])
		self.assertNotIn("jarvis-action", frappe.db.get_value(MSG, msg, "content"))

	def test_an_unreadable_card_becomes_a_question_too(self):
		conv = self.conv()
		msg = self.reply(conv, _card(doctype="Supplier", fields=[{"label": "no such field", "value": 1}]))
		filebox_cards.park_from_turn(conv, msg)
		self.assertEqual(frappe.db.count(AR, {"conversation": conv}), 1)

	def test_an_ordinary_conversation_keeps_its_card(self):
		conv = self.conv(file_box=0)
		content = self.supplier_card()
		msg = self.reply(conv, content)
		self.assertIsNone(filebox_cards.park_from_turn(conv, msg))
		self.assertEqual(frappe.db.get_value(MSG, msg, "content"), content)
		self.assertEqual(self.rows(), [])
		self.assertTrue(frappe.db.exists(CONV, conv))

	def test_a_draft_card_auto_applies_is_stamped_and_says_created(self):
		dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		conv = self.conv()
		meta = frappe.get_meta(dt)
		field = next(f.fieldname for f in meta.fields if f.fieldtype in ("Data", "Small Text", "Text"))
		msg = self.reply(conv, _card(doctype=dt, fields=[{"label": field, "value": "x"}]))
		ret = {"ok": True, "data": {"doctype": dt, "name": "D-1"}}
		with patch("jarvis.api.dispatch_confirmed", return_value=ret) as dc:
			note = filebox_cards.park_from_turn(conv, msg)
		dc.assert_called_once()
		self.assertEqual(note, f"Created {dt} D-1.")
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_result_name"), "D-1")

	def test_the_turn_end_and_a_confirm_never_both_park(self):
		conv = self.conv()
		msg = self.reply(conv, self.supplier_card())
		filebox_cards.park_from_turn(conv, msg)
		with patch.object(filebox_cards, "_run") as run:
			res = filebox_cards.park_confirmed(conv, "create", "Supplier", "", {"supplier_name": PARTY})
		run.assert_not_called()
		self.assertFalse(res["ok"])
		self.assertEqual(len(self.rows()), 1)

	def test_a_stale_confirm_never_writes_another_card(self):
		conv = self.conv()
		first = {
			"kind": "doc",
			"verb": "create",
			"doctype": "Supplier",
			"fields": [{"label": "supplier_name", "value": PARTY}],
		}
		second = {**first, "fields": [{"label": "supplier_name", "value": "zz-fbh Second"}]}
		msg = self.reply(conv, _card(**first) + _card(**second))
		filebox_cards.park_from_turn(conv, msg)  # the turn end takes the first card
		with patch.object(filebox_cards, "_run") as run:
			stale = filebox_cards.park_confirmed(
				conv, "create", "Supplier", "", {"supplier_name": PARTY}, card=first
			)
		run.assert_not_called()
		self.assertFalse(stale["ok"])
		# the second card is still there, and a Confirm of exactly it parks it
		held = {"ok": True, "data": {"status": "pending_confirmation", "held_for": "approval_board"}}
		with patch.object(filebox_cards, "_run", return_value=held) as run:
			res = filebox_cards.park_confirmed(
				conv, "create", "Supplier", "", {"supplier_name": "zz-fbh Second"}, card=second, message=msg
			)
		self.assertTrue(res["ok"])
		self.assertEqual(run.call_args.args[1]["values"], {"supplier_name": "zz-fbh Second"})
		self.assertNotIn("jarvis-action", frappe.db.get_value(MSG, msg, "content"))

	def test_every_clients_copy_of_a_card_has_the_same_identity(self):
		fence = _card(
			doctype="Item",
			fields=[
				{"label": "is_stock_item", "value": True},
				{"label": "description", "value": None},
				{"label": "standard_rate", "value": 100.0},
			],
			tables=[{"fieldname": "uoms", "rows": [{"uom": "Nos", "conversion_factor": 1.0}]}],
		)
		server = filebox_cards.parse_action(fence)
		# the PWA stringifies field values (null as ""); a browser round trip drops ".0"
		pwa = {
			**server,
			"fields": [
				{"label": "is_stock_item", "value": "true"},
				{"label": "description", "value": ""},
				{"label": "standard_rate", "value": "100"},
			],
			"tables": [{"fieldname": "uoms", "rows": [{"uom": "Nos", "conversion_factor": 1}]}],
		}
		desktop = {
			**server,
			"kind": "doc",
			"fields": {"is_stock_item": True, "description": None, "standard_rate": 100},
		}
		desktop["tables"] = pwa["tables"]
		self.assertEqual(filebox_cards._ident(pwa), filebox_cards._ident(server))
		self.assertEqual(filebox_cards._ident(desktop), filebox_cards._ident(server))
		other_rows = {**server, "tables": [{"fieldname": "uoms", "rows": [{"uom": "Box"}]}]}
		self.assertNotEqual(filebox_cards._ident(other_rows), filebox_cards._ident(server))

	def test_an_identical_copy_leaves_with_the_card_it_repeats(self):
		conv = self.conv()
		card = self.supplier_card()
		msg = self.reply(conv, card + card)
		filebox_cards.park_from_turn(conv, msg)
		self.assertNotIn("jarvis-action", frappe.db.get_value(MSG, msg, "content"))
		self.assertEqual(len(self.rows()), 1)

	def test_an_update_note_says_updated(self):
		self.assertEqual(
			filebox_cards._note(
				"update_doc", {"ok": True, "data": {"doctype": "Supplier", "name": "S-1"}}, None
			),
			"Updated Supplier S-1.",
		)

	def test_a_write_waiting_on_an_earlier_approval_files_no_question(self):
		conv = self.conv()
		msg = self.reply(conv, self.supplier_card())
		waiting = {"ok": False, "error": {"code": "ApprovalPendingError", "message": "pending"}}
		with patch.object(filebox_cards, "_run", return_value=waiting):
			note = filebox_cards.park_from_turn(conv, msg)
		self.assertEqual(frappe.db.count(AR, {"conversation": conv}), 0)
		self.assertIn("earlier approval", note)
		self.assertNotIn("jarvis-action", frappe.db.get_value(MSG, msg, "content"))

	def test_a_failed_question_puts_the_card_back_for_the_retry(self):
		conv = self.conv()
		content = _card(doctype="Supplier", fields=[{"label": "no such field", "value": 1}])
		msg = self.reply(conv, content)
		with patch.object(filebox_cards, "_ask", side_effect=RuntimeError("db down")):
			with self.assertRaises(RuntimeError):
				filebox_cards.park_from_turn(conv, msg)
		self.assertEqual(frappe.db.get_value(MSG, msg, "content"), content)
		filebox_cards.park_from_turn(conv, msg)  # the retry
		self.assertEqual(frappe.db.count(AR, {"conversation": conv}), 1)
