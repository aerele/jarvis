"""A jarvis-action card in a File Box reply runs through the File Box policy (#617):
a master is held on the Approval Board, a refused write (a Single) becomes a board
question, and the card leaves the chat so it can't also be confirmed there."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import frappe

from jarvis.chat import filebox_cards
from jarvis.tests._pending_action_helpers import draft_doctype
from jarvis.tests.test_filebox_held import CONV, MSG, OWNER, PARTY, TAX, _await_lock_wait, _Base

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

	def test_a_capitalised_or_padded_verb_reads_as_the_lower_case_one(self):
		for raw, want in (("Create", "create"), (" create ", "create"), ("UPDATE", "update")):
			a = filebox_cards.parse_action(_card(doctype="Supplier", verb=raw))
			self.assertEqual(a["verb"], want, raw)

	def test_an_unknown_or_non_text_verb_is_not_a_card(self):
		self.assertIsNone(filebox_cards.parse_action(_card(doctype="Supplier", verb="draft")))
		self.assertIsNone(filebox_cards.parse_action(_card(doctype="Supplier", verb=5)))
		self.assertIsNone(filebox_cards.parse_action(_card(doctype="Supplier", verb=["create"])))

	def test_a_block_without_a_verb_still_has_none(self):
		a = filebox_cards.parse_action(_card(doctype="Supplier"))
		self.assertNotIn("verb", a)

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

	def reply(self, conv: str, content: str, seq: int = 2) -> str:
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": "assistant", "content": content}
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

	# --- #762: a re-shown card files one board question, not two ---

	def settings_card(self, value="Supplier Name", fields=None) -> str:
		"""A Buying Settings update: the File Box policy refuses it and a human must handle it."""
		return _card(
			kind="doc",
			verb="update",
			doctype="Buying Settings",
			name="Buying Settings",
			fields=fields if fields is not None else [{"label": "supp_master_name", "value": value}],
		)

	def questions(self, conv: str, **filters) -> list:
		return frappe.get_all(
			AR,
			filters={"conversation": conv, "source": "File Box", **filters},
			fields=["name", "status", "context_md", "modified"],
			order_by="creation asc",
		)

	def test_a_reshown_card_files_one_question(self):
		conv = self.conv()
		first = self.reply(conv, self.settings_card(), seq=2)
		second = self.reply(conv, self.settings_card(), seq=4)
		filebox_cards.park_from_turn(conv, first)
		note = filebox_cards.park_from_turn(conv, second)
		self.assertEqual(len(self.questions(conv, status="Pending")), 1)
		self.assertIn("Approval Board", note)
		content = frappe.db.get_value(MSG, second, "content")
		self.assertNotIn("jarvis-action", content)
		self.assertIn(note, content)

	def test_a_reshown_card_publishes_no_second_approval_event(self):
		conv = self.conv()
		first = self.reply(conv, self.settings_card(), seq=2)
		second = self.reply(conv, self.settings_card(), seq=4)
		with patch.object(filebox_cards, "publish_to_user") as pub:
			filebox_cards.park_from_turn(conv, first)
			self.assertEqual(pub.call_count, 1)
			self.assertEqual(pub.call_args.args[1]["kind"], "approval:new")
			filebox_cards.park_from_turn(conv, second)
		self.assertEqual(pub.call_count, 1)

	def test_a_reused_question_keeps_its_position_and_logs_one_content_free_line(self):
		conv = self.conv()
		first = self.reply(conv, self.settings_card(), seq=2)
		second = self.reply(conv, self.settings_card(), seq=4)
		filebox_cards.park_from_turn(conv, first)
		[before] = self.questions(conv)
		with patch("jarvis.chat.latency.get_logger") as log:
			filebox_cards.park_from_turn(conv, second)
		[after] = self.questions(conv)
		self.assertEqual(after.modified, before.modified)
		lines = [c.args[0] % c.args[1:] for c in log.return_value.info.call_args_list]
		[line] = [x for x in lines if "filebox_question reused" in x]
		self.assertIn("filebox_question reused", line)
		self.assertIn(conv, line)
		self.assertIn(before.name, line)
		self.assertNotIn("Supplier Name", line)

	def test_a_different_change_files_its_own_question(self):
		conv = self.conv()
		first = self.reply(conv, self.settings_card("Supplier Name"), seq=2)
		second = self.reply(conv, self.settings_card("Naming Series"), seq=4)
		filebox_cards.park_from_turn(conv, first)
		filebox_cards.park_from_turn(conv, second)
		self.assertEqual(len(self.questions(conv, status="Pending")), 2)

	def test_an_answered_question_lets_the_card_ask_again(self):
		conv = self.conv()
		first = self.reply(conv, self.settings_card(), seq=2)
		filebox_cards.park_from_turn(conv, first)
		[ar] = self.questions(conv)
		frappe.db.set_value(AR, ar.name, {"status": "Answered", "decision": "Done"})
		frappe.db.commit()
		second = self.reply(conv, self.settings_card(), seq=4)
		filebox_cards.park_from_turn(conv, second)
		self.assertEqual(len(self.questions(conv)), 2)
		self.assertEqual(len(self.questions(conv, status="Pending")), 1)

	def test_a_different_reason_still_reuses_the_question(self):
		conv = self.conv()
		card = _card(doctype="Supplier", fields=[{"label": "no such field", "value": 1}])
		first = self.reply(conv, card, seq=2)
		second = self.reply(conv, card, seq=4)
		filebox_cards.park_from_turn(conv, first)
		other = {"ok": False, "error": {"message": "A different refusal this time."}}
		with patch.object(filebox_cards, "_unreadable", return_value=other):
			filebox_cards.park_from_turn(conv, second)
		[ar] = self.questions(conv)
		self.assertNotIn("A different refusal", ar.context_md)

	def test_another_conversation_never_reuses_it(self):
		one, two = self.conv(), self.conv()
		filebox_cards.park_from_turn(one, self.reply(one, self.settings_card()))
		filebox_cards.park_from_turn(two, self.reply(two, self.settings_card()))
		self.assertEqual(len(self.questions(one)), 1)
		self.assertEqual(len(self.questions(two)), 1)

	def test_the_question_lookup_runs_under_a_fresh_conversation_lock(self):
		from frappe.model.document import Document

		from jarvis.chat.pending_actions import _store

		conv = self.conv()
		msg = self.reply(conv, self.settings_card())
		events: list[str] = []
		real_commit, real_lock = frappe.db.commit, _store.lock_conversation
		real_get_all, real_insert = frappe.get_all, Document.insert
		real_run = filebox_cards._run

		def commit(*a, **k):
			events.append("commit")
			return real_commit(*a, **k)

		def lock(c):
			events.append(f"lock:{c}")
			return real_lock(c)

		def run(*a, **k):
			try:
				return real_run(*a, **k)
			finally:
				events.append("run")

		def get_all(*a, **k):
			if (k.get("filters") or {}).get("conversation") == conv and "lock:" + conv in events:
				events.append("lookup")
			return real_get_all(*a, **k)

		def insert(self_, *a, **k):
			if self_.doctype == AR:
				events.append("insert")
			return real_insert(self_, *a, **k)

		with (
			patch.object(frappe.db, "commit", commit),
			patch.object(_store, "lock_conversation", lock),
			patch.object(frappe, "get_all", get_all),
			patch.object(Document, "insert", insert),
			patch.object(filebox_cards, "_run", run),
		):
			filebox_cards.park_from_turn(conv, msg)
		self.assertIn(f"lock:{conv}", events)
		at = events.index(f"lock:{conv}")
		# the tool run is over, then a commit, then the lock: the claim's commit alone is not enough
		self.assertEqual(events[at - 2 : at], ["run", "commit"])
		lookup, insert_at = events.index("lookup"), events.index("insert")
		self.assertLess(at, lookup)
		self.assertLess(lookup, insert_at)
		self.assertNotIn("commit", events[at : insert_at + 1])

	def test_map_form_cards_with_different_values_file_two_questions(self):
		conv = self.conv()
		one = self.reply(conv, self.settings_card(fields={"supp_master_name": "Supplier Name"}), seq=2)
		two = self.reply(conv, self.settings_card(fields={"supp_master_name": "Naming Series"}), seq=4)
		filebox_cards.park_from_turn(conv, one)
		filebox_cards.park_from_turn(conv, two)
		rows = self.questions(conv)
		self.assertEqual(len(rows), 2)
		self.assertIn("supp_master_name: Supplier Name", rows[0].context_md)
		self.assertIn("supp_master_name: Naming Series", rows[1].context_md)

	def table_card(self, rows) -> str:
		# a table the doctype does not have: the card is unreadable, so it becomes a question
		return _card(doctype="Supplier", tables=[{"fieldname": "no_such_table", "rows": rows}])

	def test_rows_that_differ_file_two_questions(self):
		conv = self.conv()
		one = self.reply(conv, self.table_card([{"uom": "Nos", "qty": 1}]), seq=2)
		two = self.reply(conv, self.table_card([{"uom": "Box", "qty": 1}]), seq=4)
		filebox_cards.park_from_turn(conv, one)
		filebox_cards.park_from_turn(conv, two)
		self.assertEqual(len(self.questions(conv)), 2)

	def test_the_board_question_lists_each_row(self):
		conv = self.conv()
		rows = [{"uom": "Nos", "qty": 2, "note": ""}, {"uom": "Box", "qty": 1.0, "note": None}]
		filebox_cards.park_from_turn(conv, self.reply(conv, self.table_card(rows)))
		[ar] = self.questions(conv)
		self.assertIn("- no_such_table: 2 row(s)", ar.context_md)
		self.assertIn("\n  - row 1: qty=2, uom=Nos\n", ar.context_md)
		self.assertIn("\n  - row 2: qty=1, uom=Box\n", ar.context_md)

	def test_a_non_object_table_row_renders_and_files(self):
		conv = self.conv()
		card = self.table_card(["loose text"])
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=2))
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=4))
		[ar] = self.questions(conv)
		self.assertIn("  - row 1: loose text", ar.context_md)

	def test_a_long_card_reshown_is_reused(self):
		conv = self.conv()
		a = {"doctype": "Supplier", "fields": [{"label": "no such field", "value": ""}]}
		pad = 9995 - len(filebox_cards._card_text(a))
		a["fields"][0]["value"] = "x" * pad
		self.assertEqual(len(filebox_cards._card_text(a)), 9995)
		card = _card(**a)
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=2))
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=4))
		self.assertEqual(len(self.questions(conv)), 1)

	def test_map_form_tables_with_different_rows_file_two_questions(self):
		conv = self.conv()

		def card(tables):
			return _card(doctype="Supplier", tables=tables)

		cards = [
			card({"no_such_table": {"rows": [{"uom": "Nos"}]}}),
			card({"no_such_table": {"rows": [{"uom": "Box"}]}}),
			card({"no_such_table": [{"uom": "Carton"}]}),  # a bare list of rows
		]
		for seq, content in enumerate(cards, 1):
			filebox_cards.park_from_turn(conv, self.reply(conv, content, seq=seq))
		rows = self.questions(conv)
		self.assertEqual(len(rows), 3)
		for row, uom in zip(rows, ("Nos", "Box", "Carton"), strict=True):
			self.assertIn(f"  - row 1: uom={uom}", row.context_md)

	def test_a_table_whose_rows_are_not_a_list_does_not_raise(self):
		conv = self.conv()
		card = _card(doctype="Supplier", tables={"no_such_table": {"rows": "abc"}})
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=2))
		filebox_cards.park_from_turn(conv, self.reply(conv, card, seq=4))
		[ar] = self.questions(conv)
		self.assertNotIn("row(s)", ar.context_md)

	def test_two_parks_of_the_same_card_racing_end_with_one_question(self):
		"""The rival park (its own connection) queues on the conversation lock while this
		one holds it between lock and insert, then finds the question and reuses it."""
		conv = self.conv()
		first = self.reply(conv, self.settings_card(), seq=2)
		second = self.reply(conv, self.settings_card(), seq=4)
		site, errors, real = frappe.local.site, [], filebox_cards._card_text

		def rival():
			frappe.init(site=site)
			frappe.connect()
			try:
				filebox_cards.park_from_turn(conv, second)
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				frappe.destroy()

		def racing(*a):
			if not hasattr(racing, "thread"):  # the lock is held; the lookup has not run
				racing.thread = threading.Thread(target=rival)
				racing.thread.start()
				_await_lock_wait(conv)
			return real(*a)

		with patch.object(filebox_cards, "_card_text", side_effect=racing):
			filebox_cards.park_from_turn(conv, first)
			racing.thread.join(timeout=60)
		self.assertEqual(errors, [])
		frappe.db.rollback()  # a fresh snapshot of what the rival committed
		self.assertEqual(len(self.questions(conv)), 1)
		self.assertNotIn("jarvis-action", frappe.db.get_value(MSG, second, "content"))
