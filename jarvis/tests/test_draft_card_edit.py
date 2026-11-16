"""Saving an edited draft card (``actions_api.save_draft_card``, ``jarvis.chat.draft_save``).

The first class is pure and runs without a site. The site classes park real draft cards
through ``pending_confirm.mint`` (with the card's display row) and save them for real, as
the core DocTypes ToDo and Web Form. Every continuation is recorded, never queued, and
every row a test makes is removed."""

from __future__ import annotations

import json
import unittest

from jarvis.chat import draft_values


class _Rules:
	"""Fixed site answers for ``draft_values.errors``."""

	def __init__(self, tables=None, saved=None, refused=()):
		self.tables = tables or {}
		self.saved = saved or {}
		self.refused = set(refused)

	def table_kind(self, field):
		return self.tables.get(field)

	def field_error(self, field, value):
		return "refused" if field in self.refused else ""

	def cell_error(self, table, field, value):
		return "refused" if field in self.refused else ""

	def saved_rows(self, table):
		return frozenset(self.saved.get(table, ()))

	def mark(self, field, message, *, table=None, idx=None):
		return {"fieldname": field, "parentfield": table, "idx": idx, "message": message}


class TestDraftValuesPure(unittest.TestCase):
	def test_an_update_row_name_must_be_a_saved_row_or_one_the_draft_named(self):
		"""A made-up or foreign row name must be marked before the claim; otherwise it
		reaches the write, fails after the claim, and closes the card instead of marking
		the row. Saved rows, rows the draft named and new rows pass."""
		rules = _Rules(tables={"items": "Table"}, saved={"items": {"r1"}})
		rows = [{"name": "r1"}, {"name": "r9"}, {"name": "zz"}, {"label": "new"}, {"name": ""}]
		base = {"items": [{"name": "r9"}]}
		found = draft_values.errors({"items": rows}, rules, is_update=True, base=base)
		self.assertEqual(
			found,
			[{"fieldname": "name", "parentfield": "items", "idx": 3, "message": draft_values.NOT_A_ROW}],
		)

	def test_a_row_name_on_a_create_is_a_cell_like_any_other(self):
		"""The row identity rule is for updates only: on a create a row ``name`` goes to
		the cell rules (which refuse it), so a create can never pick its rows' names."""
		rules = _Rules(tables={"items": "Table"}, saved={"items": {"r1"}}, refused={"name"})
		found = draft_values.errors({"items": [{"name": "r1"}]}, rules, is_update=False)
		self.assertEqual([(f["fieldname"], f["idx"], f["message"]) for f in found], [("name", 1, "refused")])
		self.assertEqual(draft_values.errors({"items": [{"name": "r1"}]}, rules, is_update=True), [])

	def test_only_new_rows_for_a_populated_table_are_refused_before_the_write(self):
		"""update_doc refuses a nameless list for a table with saved rows; caught after the
		claim it would close the card. A Table MultiSelect is replaced as a set and an
		empty table has nothing to match, so neither is refused."""
		new_only = [{"label": "x"}]
		rules = _Rules(
			tables={"items": "Table", "tags": "Table MultiSelect"}, saved={"items": {"r1"}, "tags": {"t1"}}
		)
		[mark] = draft_values.errors({"items": new_only}, rules, is_update=True)
		self.assertEqual((mark["fieldname"], mark["message"]), ("items", draft_values.KEEP_A_ROW))
		self.assertEqual(draft_values.errors({"tags": new_only}, rules, is_update=True), [])
		empty = _Rules(tables={"items": "Table"})
		self.assertEqual(draft_values.errors({"items": new_only}, empty, is_update=True), [])

	def test_a_table_that_is_not_a_list_of_rows_is_refused_and_none_clears_it(self):
		"""A malformed table must be a field mark, not a crash in the write; ``None`` clears
		the table and passes."""
		rules = _Rules(tables={"items": "Table"})
		for bad in ("x", [1], {"name": "r1"}):
			with self.subTest(bad=bad):
				[mark] = draft_values.errors({"items": bad}, rules, is_update=True)
				self.assertEqual(mark["message"], draft_values.NOT_ROWS)
		self.assertEqual(draft_values.errors({"items": None}, rules, is_update=True), [])

	def test_a_draft_row_index_must_name_one_drafted_row_once(self):
		"""``_draft_row`` says which drafted row a sent row is: an index that is not one,
		or one two rows both claim, would copy the wrong drafted cells into the record."""
		rules = _Rules(tables={"items": "Table"})
		base = {"items": [{"x": 1}, {"x": 2}]}
		rows = [{"_draft_row": 0}, {"_draft_row": 5}, {"_draft_row": "1"}, {"_draft_row": True}]
		rows += [{"_draft_row": 1}, {"_draft_row": 1}]
		found = draft_values.errors({"items": rows}, rules, is_update=False, base=base)
		self.assertEqual(
			[(f["idx"], f["message"]) for f in found],
			[
				(2, draft_values.NOT_A_DRAFTED_ROW),
				(3, draft_values.NOT_A_DRAFTED_ROW),
				(4, draft_values.NOT_A_DRAFTED_ROW),
				(6, draft_values.TWICE),
			],
		)
		on_update = _Rules(tables={"items": "Table"}, refused={"_draft_row"})
		[mark] = draft_values.errors({"items": [{"_draft_row": 0}]}, on_update, is_update=True, base=base)
		self.assertEqual(mark["message"], "refused", "an update's rows are keyed by name only")

	def test_an_update_writes_the_edits_over_the_drafts_own_changes(self):
		"""The form sends only what was edited: the draft's other changes must still be
		written (an unedited save is exactly the card), a table sent replaces the draft's
		list for that table whole, and None is sent on to set a field blank."""
		args = {"doctype": "T", "name": "N", "changes": {"status": "Open", "items": [{"name": "r1"}]}}
		self.assertEqual(draft_values.edited_call("update_doc", args, {}), args)
		edited = draft_values.edited_call("update_doc", args, {"priority": "High", "date": None})
		self.assertEqual(
			edited["changes"], {"status": "Open", "items": [{"name": "r1"}], "priority": "High", "date": None}
		)
		rows = [{"name": "r1"}, {"label": "new"}]
		self.assertEqual(
			draft_values.edited_call("update_doc", args, {"items": rows})["changes"]["items"], rows
		)
		self.assertEqual(args["changes"]["items"], [{"name": "r1"}], "the sealed call is never changed")

	def test_a_create_keeps_every_drafted_key_the_form_did_not_send(self):
		"""DATA LOSS guard: a value the form cannot show (an attachment, a hidden field, a
		typed prompt name) must still be written; an unedited save is exactly the card;
		None or "" clears a field."""
		sealed = {"a": 1, "img": "https://example.com/x.png", "name": "X-1", "zero": 0}
		args = {"doctype": "T", "values": sealed, "continue": True}
		self.assertEqual(draft_values.edited_call("create_doc", args, {}), args)
		edited = draft_values.edited_call("create_doc", args, {"b": 2, "a": "", "name": None})
		self.assertEqual(
			edited,
			{
				"doctype": "T",
				"values": {"img": "https://example.com/x.png", "zero": 0, "b": 2},
				"continue": True,
			},
		)
		self.assertEqual(sealed["a"], 1, "the sealed call is never changed")

	def test_a_create_table_merges_rows_by_their_drafted_index(self):
		"""A sent table is the final rows: a row from the draft keeps the cells the form did
		not send and loses the ones sent empty; a new row is added; a drafted row no sent
		row names is removed; a table not sent keeps its drafted rows, None clears it."""
		drafted = [{"x": 1, "hidden": "keep"}, {"x": 2}, {"x": 3, "note": "drop"}]
		args = {"doctype": "T", "values": {"items": drafted, "tags": [{"tag": "t"}]}}
		sent = [{"_draft_row": 0, "x": 10}, {"_draft_row": 2, "note": ""}, {"x": 9, "blank": ""}]
		tables = {"items", "tags"}
		edited = draft_values.edited_call("create_doc", args, {"items": sent}, tables=tables)["values"]
		self.assertEqual(edited["items"], [{"x": 10, "hidden": "keep"}, {"x": 3}, {"x": 9}])
		self.assertEqual(edited["tags"], [{"tag": "t"}])
		cleared = draft_values.edited_call("create_doc", args, {"tags": None}, tables=tables)["values"]
		self.assertNotIn("tags", cleared)

	def test_only_cells_the_client_sent_empty_are_cleared_from_a_drafted_row(self):
		"""A drafted cell the client did not send must pass through exactly as drafted, even
		when it is blank: stripping every blank would silently change what a plain Confirm
		writes. A cell sent as None or "" is the person clearing it."""
		args = {"doctype": "T", "values": {"items": [{"a": "", "b": "x"}]}}
		merge = draft_values.edited_call
		kept = merge("create_doc", args, {"items": [{"_draft_row": 0, "c": 1}]}, tables={"items"})
		self.assertEqual(kept["values"]["items"], [{"a": "", "b": "x", "c": 1}])
		cleared = merge("create_doc", args, {"items": [{"_draft_row": 0, "b": ""}]}, tables={"items"})
		self.assertEqual(cleared["values"]["items"], [{"a": ""}])
		new_row = merge("create_doc", args, {"items": [{"b": None, "c": 2}]}, tables={"items"})
		self.assertEqual(new_row["values"]["items"], [{"c": 2}])

	def test_a_table_sent_as_an_empty_list_clears_every_row_and_a_string_is_refused(self):
		"""``[]`` is the complete final row list (no rows), so every drafted row must go; a
		table cleared with "" would be read by one half of the code as blank and refused by
		the other, so validation must refuse it with a mark on a create and an update."""
		args = {"doctype": "T", "values": {"items": [{"x": 1}, {"x": 2}]}}
		edited = draft_values.edited_call("create_doc", args, {"items": []}, tables={"items"})
		self.assertEqual(edited["values"]["items"], [])
		rules = _Rules(tables={"items": "Table"})
		for is_update in (False, True):
			with self.subTest(is_update=is_update):
				[mark] = draft_values.errors({"items": ""}, rules, is_update=is_update)
				self.assertEqual((mark["fieldname"], mark["message"]), ("items", draft_values.NOT_ROWS))

	def test_zero_false_and_zero_point_zero_are_written_not_cleared(self):
		"""A falsy value is a real answer (a quantity of 0, an unchecked box): treating it as
		"clear" would drop it from the write and let the default apply instead. Top level and
		in a row cell, on a create and an update; JSON text tells 0, false and 0.0 apart."""
		sent = {"qty": 0, "flag": False, "rate": 0.0}
		args = {"doctype": "T", "values": {"items": [{"x": 1}]}}
		wrote = draft_values.edited_call(
			"create_doc", args, {**sent, "items": [{"_draft_row": 0, **sent}]}, tables={"items"}
		)
		text = json.dumps(sent)
		self.assertEqual(json.dumps({k: wrote["values"][k] for k in sent}), text)
		self.assertEqual(json.dumps({k: wrote["values"]["items"][0][k] for k in sent}), text)
		update = draft_values.edited_call("update_doc", {"doctype": "T", "name": "N", "changes": {}}, sent)
		self.assertEqual(json.dumps(update["changes"]), text)

	def test_a_draft_row_that_is_negative_float_or_empty_is_refused(self):
		"""``-1`` would index from the end of the drafted rows and ``1.0`` is not an index
		(only a whole int is): either would copy the wrong drafted cells into the record,
		so each must be a mark on its row."""
		rules = _Rules(tables={"items": "Table"})
		base = {"items": [{"x": 1}, {"x": 2}]}
		rows = [{"_draft_row": -1}, {"_draft_row": 1.0}, {"_draft_row": None}, {"_draft_row": 1}]
		found = draft_values.errors({"items": rows}, rules, is_update=False, base=base)
		self.assertEqual(
			[(f["idx"], f["message"]) for f in found],
			[(i, draft_values.NOT_A_DRAFTED_ROW) for i in (1, 2, 3)],
		)


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except ImportError:  # the pure class above still runs without a site
	FrappeTestCase = None

if FrappeTestCase is not None:
	import threading
	from unittest.mock import patch

	from jarvis import api
	from jarvis._commit_watch import CommitWatch
	from jarvis.chat import actions_api, agent_notes, confirm_card, draft_card, draft_save, pending_confirm
	from jarvis.chat.pending_actions import _execute, _store
	from jarvis.tests._pending_action_helpers import (
		CONV,
		MSG,
		OTHER,
		OWNER,
		PA,
		PendingActionTestMixin,
		as_user,
	)
	from jarvis.tests._transport_helpers import provision_legacy_site
	from jarvis.tests.test_pending_confirm import _purge_legacy

	FLAG = "jarvis_draft_cards"
	MARK = "pa-test dce"  # every ToDo and Web Form here; the mixin's cleanup removes "pa-test%"
	IMAGE = "https://example.com/pa-test-dce.png"  # not a /files URL: Frappe attaches nothing
	SUBMIT = {"kind": "submit", "action": None}
	NOTE_PREFIX = "the user confirmed the draft and it was saved: "

	def _fail(*args, **kwargs):
		raise frappe.ValidationError("boom")

	class _Continuations:
		"""Stands in for ``actions_api.enqueue_continuation``: records the call and runs
		the ``settled`` compare-and-set the real hidden user row commits (``claim``),
		but queues no turn."""

		def __init__(self):
			self.calls = []

		def __call__(self, conversation, receipt, **kwargs):
			self.calls.append((conversation, receipt, kwargs))
			if kwargs.get("claim"):
				kwargs["claim"]()
			return {}

	class _DraftCase(PendingActionTestMixin, FrappeTestCase):
		def setUp(self):
			super().setUp()
			provision_legacy_site(self)
			for user in (OWNER, OTHER):
				_purge_legacy(user)
				self.addCleanup(_purge_legacy, user)
			self.addCleanup(self._wipe)
			flag = patch.dict(frappe.conf, {FLAG: 1})
			flag.start()
			self.addCleanup(flag.stop)
			self.cont = _Continuations()
			recorder = patch.object(actions_api, "enqueue_continuation", self.cont)
			recorder.start()
			self.addCleanup(recorder.stop)

		def _wipe(self):
			frappe.db.rollback()
			frappe.set_user(self._pa_orig_user)
			forms = frappe.get_all("Web Form", filters={"title": ["like", f"{MARK}%"]}, pluck="name")
			if forms:
				frappe.db.delete("Web Form Field", {"parent": ["in", forms]})
				frappe.db.delete("Web Form", {"name": ["in", forms]})
			frappe.db.delete("ToDo", {"description": ["like", f"%{MARK}%"]})
			frappe.db.commit()

		def website_manager(self):
			"""OTHER may write Web Forms, for this test only."""
			# Registered first: a failure between the grant and its commit must not leak the
			# role. ``remove_roles`` skips a role the user does not have.
			self.addCleanup(self._revoke)
			frappe.get_doc("User", OTHER).add_roles("Website Manager")
			frappe.db.commit()

		def _revoke(self):
			frappe.db.rollback()
			frappe.set_user(self._pa_orig_user)
			frappe.get_doc("User", OTHER).remove_roles("Website Manager")
			frappe.db.commit()

		def mint(self, conv, tool, args, *, owner=OWNER, exec_user=None, draft=True):
			"""Park ``tool(args)`` as ``api._run_tool`` does (card, draft, display row)."""
			would = args.get("values") if tool == "create_doc" else args.get("changes")
			card = confirm_card.build_card(tool, args, {"would": would}, draft)
			self.assertTrue(card)
			if draft:
				self.assertTrue(draft_card.attach(card, tool, args, would))
			token = pending_confirm.mint(
				conversation=conv,
				owner=owner,
				exec_user=exec_user,
				tool=tool,
				args=json.loads(json.dumps(args)),
				run_id="",
				preview={"would": would, "card": card},
			)
			self.assertTrue(token)
			return token

		def todo_draft(self, description=f"{MARK} original", **extra):
			conv = self.make_conv()
			args = {"doctype": "ToDo", "values": {"description": description}, **extra}
			return conv, self.mint(conv, "create_doc", args)

		def form_draft(self, values, *, owner=OWNER):
			"""A Web Form create draft (a title is added); ``(conv, token)``."""
			conv = self.make_conv(owner=owner)
			title = f"{MARK} {frappe.generate_hash(length=8)}"
			args = {"doctype": "Web Form", "values": {"title": title, "doc_type": "ToDo", **values}}
			return conv, self.mint(conv, "create_doc", args, owner=owner)

		def save(self, token, conv, values=None, user=OWNER):
			with as_user(user):
				return actions_api.save_draft_card(token, conv, json.dumps(values or {}))

		def confirm(self, token, conv, user=OWNER):
			with as_user(user):
				return actions_api.confirm_tool(token, conversation=conv)

		def sealed(self, token):
			return frappe.db.get_value(PA, token, ["status", "card", "sealed_call"], as_dict=True)

		def assert_untouched(self, token, before):
			"""Still Pending, and its card and sealed call exactly as parked."""
			self.assertEqual(self.sealed(token), before)
			self.assertEqual(before.status, "Pending")

		def assert_closed(self, out, code, message=None):
			self.assertIs(out.get("closed"), True, out)
			self.assertEqual(out["error"]["code"], code)
			if message is not None:
				self.assertEqual(out["error"]["message"], message)

		def tool_rows(self, conv):
			return frappe.get_all(
				MSG,
				filters={"conversation": conv, "role": ["in", ["tool", "assistant"]]},
				fields=["role", "tool_call_id", "tool_args", "tool_result", "action_outcome", "pending_card"],
			)

		def todos(self, text):
			"""ToDo records whose description holds ``MARK`` + ``text``."""
			return frappe.get_all("ToDo", filters={"description": ["like", f"%{MARK} {text}%"]}, pluck="name")

		def form_rows(self, form):
			return frappe.get_all(
				"Web Form Field",
				filters={"parent": form, "parenttype": "Web Form", "parentfield": "web_form_fields"},
				fields=["name", "fieldname", "label", "description"],
				order_by="idx asc",
			)

		def notes(self, conv):
			raw = frappe.db.get_value(CONV, conv, "pending_agent_notes")
			return [e["text"] for e in agent_notes._parse(raw)]

	class TestWhoMaySave(_DraftCase):
		def test_another_user_cannot_save_the_owners_draft(self):
			"""The new endpoint must carry apply_action's owner check: a stranger who knows
			the token writes nothing and the card stays open."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			with self.assertRaises(frappe.PermissionError):
				self.save(token, conv, {"description": f"{MARK} stranger"}, user=OTHER)
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("stranger"), [])

		def test_a_stranger_with_their_own_chat_and_the_owners_token_is_refused(self):
			"""Owning SOME conversation is not owning the card: the row's owner and its own
			conversation must both match, or the stranger writes into the owner's card."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			own = self.make_conv(owner=OTHER)
			out = self.save(token, own, {"description": f"{MARK} borrowed"}, user=OTHER)
			self.assert_closed(out, draft_save.CLOSED)
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("borrowed"), [])

		def test_a_card_that_runs_as_someone_else_is_refused(self):
			"""The edited write runs as the person saving: a card sealed to run as another
			user must never be written under the saver's rights instead."""
			conv = self.make_conv()
			args = {"doctype": "ToDo", "values": {"description": f"{MARK} other runner"}}
			token = self.mint(conv, "create_doc", args, exec_user=OTHER)
			before = self.sealed(token)
			self.assert_closed(self.save(token, conv, {}), draft_save.CLOSED)
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("other runner"), [])

		def test_a_stranger_saving_an_executed_draft_gets_the_refusal_never_the_record(self):
			"""The owner's saved draft answers "saved" with the record's name and link, but only
			to the owner: a stranger with the token must get the refusal a stranger gets on a
			Pending card (nothing about the record), with the owner's chat or their own."""
			conv, token = self.todo_draft()
			saved = self.save(token, conv, {"description": f"{MARK} owner saved"})
			self.assertTrue(saved["ok"], saved)
			own = self.make_conv(owner=OTHER)
			out = self.save(token, own, {"description": f"{MARK} stranger"}, user=OTHER)
			self.assert_closed(out, draft_save.CLOSED, draft_save._CLOSED)
			for leaked in ("name", "doc_url", "verb"):
				self.assertNotIn(leaked, out)
			_conv2, pending = self.todo_draft()
			own2 = self.make_conv(owner=OTHER)
			self.assertEqual(out, self.save(pending, own2, {}, user=OTHER))
			with self.assertRaises(frappe.PermissionError):
				self.save(token, conv, {}, user=OTHER)
			self.assertEqual(self.todos("stranger"), [])

		def test_the_flag_off_refuses_and_leaves_the_card(self):
			"""Dark means dark: with ``jarvis_draft_cards`` off the endpoint writes nothing."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			with patch.dict(frappe.conf, {FLAG: 0}):
				out = self.save(token, conv, {"description": f"{MARK} flag off"})
			self.assert_closed(out, draft_save.CLOSED)
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("flag off"), [])

		def test_a_file_box_chat_and_a_collaborative_chat_are_refused(self):
			"""A File Box chat only drafts (apply_action routes it to its own policy), so it
			is never written here; a collaborative chat is refused by the same gateway as
			apply_action."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			frappe.db.set_value(CONV, conv, "file_box", 1)
			frappe.db.commit()
			self.assert_closed(self.save(token, conv, {"description": f"{MARK} file box"}), draft_save.CLOSED)
			self.assert_untouched(token, before)
			frappe.db.set_value(CONV, conv, "file_box", 0)
			if frappe.db.has_column(CONV, "collaboration_active"):
				frappe.db.set_value(CONV, conv, "collaboration_active", 1)
				frappe.db.commit()
				with self.assertRaises(frappe.PermissionError):
					self.save(token, conv, {"description": f"{MARK} shared"})
				self.assert_untouched(token, before)
			self.assertEqual(self.todos("file box") + self.todos("shared"), [])

	class TestEditedCreate(_DraftCase):
		def test_the_edited_value_is_written_shown_once_and_the_row_ends_executed_edited(self):
			"""The save must write the person's value, not the model's; its one decided row
			(the card's own, flipped in place: no second receipt row) must show that value;
			and the row must say it was edited, so audit tells it from a plain Confirm."""
			conv, token = self.todo_draft()
			out = self.save(token, conv, {"description": f"{MARK} edited"})
			self.assertTrue(out["ok"], out)
			self.assertEqual(out["verb"], "create")
			row = self.row(token)
			self.assertEqual((row.status, row.edited, row.result_doctype), ("Executed", 1, "ToDo"))
			self.assertEqual(out["name"], row.result_name)
			self.assertIn("edited", frappe.db.get_value("ToDo", row.result_name, "description"))
			self.assertEqual(self.todos("original"), [])
			[decided] = self.tool_rows(conv)
			self.assertEqual((decided.tool_call_id, decided.action_outcome), (token, "confirmed"))
			self.assertFalse(decided.pending_card)
			self.assertIn(f"{MARK} edited", decided.tool_args)
			self.assertNotIn("original", decided.tool_args)

		def test_the_dry_run_cannot_change_what_is_written_and_stored(self):
			"""Frappe's row init adds keys to the dicts a dry run is given. The trial gets a
			copy, so whatever it does to its argument must reach neither the write nor the
			stored args: here the trial mutates the description it was given."""
			conv, token = self.todo_draft()
			real = api._run_preview

			def mutating(tool, args):
				args["values"]["description"] = f"{MARK} MUTATED"
				return real(tool, args)

			with patch.object(api, "_run_preview", side_effect=mutating):
				out = self.save(token, conv, {"description": f"{MARK} as sent"})
			self.assertTrue(out["ok"], out)
			self.assertEqual(frappe.db.get_value("ToDo", out["name"], "description"), f"{MARK} as sent")
			[decided] = self.tool_rows(conv)
			self.assertNotIn("MUTATED", decided.tool_args)

		def test_a_drafted_value_the_form_cannot_show_is_kept(self):
			"""DATA LOSS guard: an attachment the model drafted is not on the form, so the form
			never sends it; an unedited save and a save of another field must both still
			write it, as a plain Confirm would."""
			self.website_manager()
			for values in ({}, {"success_message": f"{MARK} thanks"}):
				with self.subTest(values=values):
					conv, token = self.form_draft({"meta_image": IMAGE}, owner=OTHER)
					out = self.save(token, conv, values, user=OTHER)
					self.assertTrue(out["ok"], out)
					saved = frappe.db.get_value(
						"Web Form", out["name"], ["meta_image", "success_message"], as_dict=True
					)
					self.assertEqual(saved.meta_image, IMAGE)
					self.assertEqual(saved.success_message, values.get("success_message"))

		def test_a_create_table_keeps_drafted_cells_adds_and_removes_rows_by_index(self):
			"""A row sent with ``_draft_row`` keeps the drafted cells the form did not send, a
			new row is added, a drafted row left out is removed; a bad index is a mark and
			changes nothing, so the same card can then be saved."""
			self.website_manager()
			drafted = [
				{
					"fieldname": "description",
					"fieldtype": "Data",
					"label": "First",
					"description": "help one",
				},
				{"fieldname": "status", "fieldtype": "Data", "label": "Second"},
				{"fieldname": "priority", "fieldtype": "Data", "label": "Third"},
			]
			conv, token = self.form_draft({"web_form_fields": drafted}, owner=OTHER)
			before = self.sealed(token)
			bad = self.save(token, conv, {"web_form_fields": [{"_draft_row": 7}]}, user=OTHER)
			[mark] = bad["error"]["fields"]
			self.assertEqual(
				(mark["parentfield"], mark["fieldname"], mark["idx"]), ("web_form_fields", "_draft_row", 1)
			)
			self.assert_untouched(token, before)
			table = [
				{"_draft_row": 0, "label": "First changed"},
				{"_draft_row": 2},
				{"fieldname": "date", "fieldtype": "Data", "label": "New"},
			]
			out = self.save(token, conv, {"web_form_fields": table}, user=OTHER)
			self.assertTrue(out["ok"], out)
			rows = [(r.fieldname, r.label, r.description) for r in self.form_rows(out["name"])]
			self.assertEqual(
				rows,
				[
					("description", "First changed", "help one"),
					("priority", "Third", None),
					("date", "New", None),
				],
			)

		def test_an_empty_value_clears_a_drafted_field_and_a_drafted_cell(self):
			"""The form says "the person emptied this" with "": the drafted value must not be
			written back, at the top level or in a row."""
			self.website_manager()
			rows = [
				{"fieldname": "description", "fieldtype": "Data", "label": "First", "description": "help"}
			]
			conv, token = self.form_draft({"success_message": "Thanks", "web_form_fields": rows}, owner=OTHER)
			values = {"success_message": "", "web_form_fields": [{"_draft_row": 0, "description": ""}]}
			out = self.save(token, conv, values, user=OTHER)
			self.assertTrue(out["ok"], out)
			self.assertFalse(frappe.db.get_value("Web Form", out["name"], "success_message"))
			[row] = self.form_rows(out["name"])
			self.assertEqual((row.label, row.description), ("First", None))

		def test_a_final_draft_queues_the_note_and_no_turn_and_continue_queues_one(self):
			"""A4's rule must hold for an edited save too: a final draft costs no turn (one
			note), and a card the agent marked ``continue`` wakes it for the next step. The
			flag is read from the stored card, never from the client."""
			conv, token = self.todo_draft()
			self.assertTrue(self.save(token, conv, {"description": f"{MARK} final"})["ok"])
			self.assertEqual(self.cont.calls, [])
			[note] = self.notes(conv)
			self.assertTrue(note.startswith(NOTE_PREFIX), note)
			chained, step = self.todo_draft(f"{MARK} step", **{"continue": True})
			self.assertTrue(self.save(step, chained, {"description": f"{MARK} step one"})["ok"])
			self.assertEqual(len(self.cont.calls), 1)
			self.assertEqual(self.notes(chained), [])

	class TestRetriesAndEndedCards(_DraftCase):
		def test_a_second_save_answers_as_the_first_and_a_late_confirm_writes_nothing(self):
			"""A double click or a retry after a lost reply must say "saved" again (a "draft
			it again" answer would make the person create a duplicate), write nothing, and
			Confirm on the saved token must run nothing."""
			conv, token = self.todo_draft()
			first = self.save(token, conv, {"description": f"{MARK} once"})
			self.assertTrue(first["ok"], first)
			again = self.save(token, conv, {"description": f"{MARK} once"})
			self.assertTrue(again["ok"], again)
			self.assertEqual((again["name"], again["doc_url"]), (first["name"], first["doc_url"]))
			self.assertFalse(self.confirm(token, conv)["ok"])
			self.assertEqual(len(self.todos("once")), 1)

		def test_a_card_that_ended_another_way_says_how_it_ended(self):
			"""Saved by a plain Confirm, failed, or discarded: each answers closed with words
			that are true for it, and none writes anything."""
			saved_conv, saved = self.todo_draft(f"{MARK} confirmed")
			self.assertTrue(self.confirm(saved, saved_conv)["ok"])
			failed_conv, failed = self.todo_draft(f"{MARK} failed")
			with patch("jarvis.api.dispatch_confirmed", side_effect=_fail):
				self.assertFalse(self.confirm(failed, failed_conv)["ok"])
			gone_conv, gone = self.todo_draft(f"{MARK} gone")
			with as_user(OWNER):
				actions_api.dismiss_tool(gone, conversation=gone_conv)
			cases = (
				(saved, saved_conv, draft_save._ALREADY_SAVED),
				(failed, failed_conv, draft_save._NOT_SAVED),
				(gone, gone_conv, draft_save._CLOSED),
			)
			for token, conv, message in cases:
				with self.subTest(message=message):
					self.assert_closed(
						self.save(token, conv, {"description": f"{MARK} late"}), draft_save.CLOSED, message
					)
			self.assertEqual(self.todos("late"), [])
			self.assertEqual(len(self.todos("confirmed")), 1)

		def test_a_closed_reply_says_the_agent_was_told_only_when_the_row_settled(self):
			"""The desktop adds "Jarvis could not be told" when ``assistant_told`` is false, so
			a second Save on a card the agent was already told about (a plain Confirm that
			won, a Failed row) must say true, and false for a row not yet settled."""
			won_conv, won = self.todo_draft(f"{MARK} won")
			self.assertTrue(self.confirm(won, won_conv)["ok"])
			lost_conv, lost = self.todo_draft(f"{MARK} lost again")
			with patch("jarvis.api.dispatch_confirmed", side_effect=_fail):
				self.assertFalse(self.confirm(lost, lost_conv)["ok"])
			for token, conv, message in (
				(won, won_conv, draft_save._ALREADY_SAVED),
				(lost, lost_conv, draft_save._NOT_SAVED),
			):
				for settled in (1, 0):
					with self.subTest(message=message, settled=settled):
						self.set_col(token, settled=settled)
						out = self.save(token, conv, {})
						self.assert_closed(out, draft_save.CLOSED, message)
						self.assertIs(out["assistant_told"], bool(settled))

		def test_a_failed_row_that_is_partial_answers_closed_as_partial(self):
			"""A save that part-way failed may have written something: "was not saved" would be
			false, so the reply must say it did not finish and carry ``outcome: partial``."""
			conv, token = self.todo_draft()
			self.set_col(token, status="Failed", reason_code="partial", settled=1)
			out = self.save(token, conv, {})
			self.assert_closed(out, draft_save.CLOSED, draft_save._UNFINISHED)
			self.assertEqual(out["outcome"], "partial")
			self.assertIs(out["assistant_told"], True)
			self.set_col(token, settled=0)
			self.assertIs(self.save(token, conv, {})["assistant_told"], False)

		def test_a_confirm_that_ran_the_card_while_a_failed_save_rolled_back_wins(self):
			"""Between the failed write's rollback and the row's end the card is Pending and
			unlocked: a Confirm landing there runs it. The save must then say the card was
			saved, never "couldn't save", or the person re-drafts a record that exists."""
			conv, token = self.todo_draft()
			todo = self.make_todo()
			real = _execute._discard_failed_dispatch

			def rollback_then_confirm(*args, **kwargs):
				real(*args, **kwargs)
				_store._terminal_update(
					token, ["Pending"], "Executed", decided_by=OWNER, result_doctype="ToDo", result_name=todo
				)
				frappe.db.commit()

			with (
				patch("jarvis.api.dispatch_confirmed", side_effect=_fail),
				patch.object(_execute, "_discard_failed_dispatch", side_effect=rollback_then_confirm),
			):
				out = self.save(token, conv, {"description": f"{MARK} lost"})
			self.assert_closed(out, draft_save.CLOSED, draft_save._ALREADY_SAVED)
			row = self.row(token)
			self.assertEqual((row.status, row.edited), ("Executed", 0))
			self.assertEqual(self.todos("lost"), [])

	class TestRefusedBeforeTheClaim(_DraftCase):
		def test_a_missing_value_is_marked_and_the_card_is_untouched(self):
			"""A fixable save must keep the card open exactly as parked (still Pending, the
			same card and seal) with the field marked, and leave no record behind."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			count = frappe.db.count("ToDo", {"owner": OWNER})
			out = self.save(token, conv, {"description": ""})
			self.assertFalse(out["ok"])
			self.assertNotIn("closed", out)
			self.assertIn("description", [f["fieldname"] for f in out["error"]["fields"]])
			self.assert_untouched(token, before)
			self.assertEqual(frappe.db.count("ToDo", {"owner": OWNER}), count)

		def test_values_that_are_not_a_map_are_refused_with_a_reply_the_form_can_show(self):
			"""Malformed JSON, a list, a number or "null" must not surface as a server error:
			the form gets the "could not be read" reply and the card is untouched."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			expected = {
				"ok": False,
				"error": {"code": "InvalidArgumentError", "message": draft_save._UNREADABLE, "fields": []},
			}
			for raw in ("{not json", "[1]", "5", "null", [1], 5):
				with self.subTest(raw=raw), as_user(OWNER):
					self.assertEqual(actions_api.save_draft_card(token, conv, raw), expected)
			self.assert_untouched(token, before)

		def test_a_missing_value_the_form_cannot_fill_is_closed_and_the_card_stays(self):
			"""When the missing value cannot be marked in the form, a fixable reply would leave
			the person stuck: ``draft_save`` must answer closed (``_NOT_HERE``) with the card
			still Pending. The mark step is made to say "cannot mark"."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			with patch.object(actions_api, "_mark_fields", return_value=False) as mark:
				out = self.save(token, conv, {"description": ""})
			self.assertTrue(mark.called)
			self.assert_closed(out, "InvalidArgumentError")
			self.assertTrue(out["error"]["message"].endswith(draft_save._NOT_HERE), out)
			self.assert_untouched(token, before)

		def test_a_protected_field_is_marked_not_written(self):
			"""``docstatus`` sent from the form must be a refused mark, never a submit."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			out = self.save(token, conv, {"description": f"{MARK} protected", "docstatus": 1})
			[mark] = out["error"]["fields"]
			self.assertEqual((mark["fieldname"], mark["invalid"]), ("docstatus", 1))
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("protected"), [])

		def test_a_key_the_write_tool_refuses_is_marked_in_plain_words(self):
			"""``amended_from`` and ``_``-names are refused by create_doc in a message meant for
			the model; the form must get a plain mark first."""
			conv, token = self.todo_draft()
			out = self.save(token, conv, {"amended_from": "TD-1", "_assign": "[]"})
			marks = {f["fieldname"]: f["message"] for f in out["error"]["fields"]}
			self.assertEqual(marks, {"amended_from": draft_values.CANT_SET, "_assign": draft_values.CANT_SET})

		def test_a_field_the_form_cannot_edit_is_refused_in_plain_words(self):
			"""An attachment is a real field the form cannot edit: sending it, set or emptied,
			is refused with words that are true ("can't be edited here", never "has no
			field"), and the drafted value stays on the card."""
			conv, token = self.form_draft({"meta_image": IMAGE})
			before = self.sealed(token)
			for value in ("https://example.com/other.png", ""):
				with self.subTest(value=value):
					[mark] = self.save(token, conv, {"meta_image": value})["error"]["fields"]
					self.assertEqual(
						(mark["fieldname"], mark["message"]), ("meta_image", draft_values.CANT_EDIT)
					)
			self.assert_untouched(token, before)

		def test_a_field_above_the_users_level_is_marked_not_reset(self):
			"""Frappe would reset a value above the writer's permission level without a word;
			the save must refuse it with a mark instead."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			df = frappe.get_meta("ToDo").get_field("priority")
			with patch.object(df, "permlevel", 1):
				out = self.save(token, conv, {"description": f"{MARK} level", "priority": "High"})
			self.assertEqual([f["fieldname"] for f in out["error"]["fields"]], ["priority"])
			self.assert_untouched(token, before)

		def test_a_trial_the_form_cannot_fix_is_closed_and_the_card_stays(self):
			"""A permission denial is no value the form fixes: a fixable reply with nothing
			marked would leave the person stuck in the form. Closed, nothing claimed."""
			conv, token = self.form_draft({})  # OWNER may not create a Web Form
			before = self.sealed(token)
			out = self.save(token, conv, {})
			self.assertIs(out.get("closed"), True, out)
			self.assertTrue(out["error"]["message"].endswith(draft_save._NOT_HERE), out)
			self.assert_untouched(token, before)

		def test_a_sensitive_edit_is_refused_closed(self):
			"""Script put on a Web Form makes the write sensitive: the person's one click is
			not that card, and the write runs without ``allow_risky``, so it is refused
			before anything runs."""
			conv, token = self.form_draft({})
			before = self.sealed(token)
			out = self.save(token, conv, {"client_script": "console.log(1)"})
			self.assert_closed(out, draft_save.NEEDS_CARD)
			self.assert_untouched(token, before)

		def test_a_stale_update_target_is_refused_closed_and_the_card_stays(self):
			"""A record changed after the draft must not be overwritten by it (update_doc
			checks no ``modified``); the row stays Pending for the agent to redo."""
			conv = self.make_conv()
			todo = self.make_todo()
			args = {"doctype": "ToDo", "name": todo, "changes": {"status": "Closed"}}
			token = self.mint(conv, "update_doc", args)
			before = self.sealed(token)
			frappe.db.set_value("ToDo", todo, "priority", "High")
			frappe.db.commit()
			self.assert_closed(self.save(token, conv, {}), draft_save.STALE)
			self.assert_untouched(token, before)
			self.assertEqual(frappe.db.get_value("ToDo", todo, "status"), "Open")

		def test_a_row_another_decider_holds_is_busy_and_the_form_stays(self):
			"""A Confirm, a Discard or a second device holding the row: the save must not wait,
			write, or close the form (the person's edits would be lost); it says try again.
			A real second connection holds the row lock, as ``execute`` takes it."""
			conv, token = self.todo_draft()
			site, locked, release = frappe.local.site, threading.Event(), threading.Event()
			errors = []

			def holder():
				frappe.init(site=site)
				frappe.connect()
				try:
					frappe.db.sql(f"SELECT name FROM `tab{PA}` WHERE name=%(n)s FOR UPDATE", {"n": token})
					locked.set()
					release.wait(timeout=20)
					frappe.db.rollback()
				except Exception as e:  # pragma: no cover
					errors.append(e)
					locked.set()
				finally:
					frappe.destroy()

			t = threading.Thread(target=holder)
			t.start()
			try:
				self.assertTrue(locked.wait(timeout=20))
				out = self.save(token, conv, {"description": f"{MARK} busy"})
			finally:
				release.set()
				t.join(timeout=20)
			self.assertEqual(errors, [])
			self.assertEqual(
				out,
				{"ok": False, "error": {"code": draft_save.BUSY, "message": draft_save._BUSY, "fields": []}},
			)
			self.assertEqual(self.row(token).status, "Pending")
			self.assertEqual(self.todos("busy"), [])

		def test_a_card_another_claim_is_running_keeps_the_form_open(self):
			"""A Confirm that already claimed the card is still running: the same "try again"
			with the form kept, not a closed reply that drops the person's edits."""
			conv, token = self.todo_draft()
			self.set_col(token, status="Executing")
			out = self.save(token, conv, {"description": f"{MARK} running"})
			self.assertNotIn("closed", out)
			self.assertEqual((out["error"]["code"], out["error"]["fields"]), (draft_save.BUSY, []))
			self.assertEqual(self.todos("running"), [])

	class TestRulesOnRealMeta(FrappeTestCase):
		def test_a_row_carrying_bookkeeping_keys_is_refused(self):
			"""A row's ``parent``, ``parenttype``, ``idx`` or ``docstatus`` would move or
			re-parent it: each is a plain refused mark, on a create and on an update."""
			row = {"parent": "x", "parenttype": "Web Form", "idx": 3, "docstatus": 1}
			for is_update in (False, True):
				with self.subTest(is_update=is_update):
					found = draft_values.validate(
						"Web Form", {"web_form_fields": [row]}, is_update=is_update, user="Administrator"
					)
					self.assertEqual(
						{(f["fieldname"], f["message"]) for f in found},
						{(key, draft_values.CANT_SET) for key in row},
					)

	class TestTableRows(_DraftCase):
		"""An update's table is its complete final row list (``merge_child_rows``)."""

		def setUp(self):
			super().setUp()
			self.website_manager()

		def web_form(self):
			doc = frappe.get_doc(
				{
					"doctype": "Web Form",
					"title": f"{MARK} rows {frappe.generate_hash(length=8)}",
					"doc_type": "ToDo",
					"web_form_fields": [
						{"fieldname": "description", "fieldtype": "Data", "label": "First"},
						{"fieldname": "status", "fieldtype": "Data", "label": "Second"},
						{"fieldname": "priority", "fieldtype": "Data", "label": "Third"},
					],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			return doc.name, [r.name for r in doc.web_form_fields]

		def draft_of(self, form, first):
			"""An update draft that touches 1 of the 3 rows, parked for OTHER."""
			conv = self.make_conv(owner=OTHER)
			changes = {"web_form_fields": [{"name": first, "label": "First changed"}]}
			args = {"doctype": "Web Form", "name": form, "changes": changes}
			return conv, self.mint(conv, "update_doc", args, owner=OTHER)

		def test_an_unedited_save_leaves_what_confirm_leaves(self):
			"""DATA LOSS guard: saving the card unedited must write the card's own call and
			no other, so the record ends with the same rows a plain Confirm leaves."""
			confirmed, (c1, _c2, _c3) = self.web_form()
			saved, (s1, _s2, _s3) = self.web_form()
			conv_c, token_c = self.draft_of(confirmed, c1)
			conv_s, token_s = self.draft_of(saved, s1)
			self.assertTrue(self.confirm(token_c, conv_c, user=OTHER)["ok"])
			self.assertTrue(self.save(token_s, conv_s, {}, user=OTHER)["ok"])
			shape = [(r.fieldname, r.label) for r in self.form_rows(confirmed)]
			self.assertEqual([(r.fieldname, r.label) for r in self.form_rows(saved)], shape)

		def test_every_row_plus_a_new_one_and_a_changed_cell_ends_with_four_rows(self):
			"""A table sent whole keeps each named row, changes only the cell edited, and
			adds the row with no name."""
			form, (r1, r2, r3) = self.web_form()
			conv, token = self.draft_of(form, r1)
			table = [
				{"name": r1},
				{"name": r2, "label": "Second changed"},
				{"name": r3},
				{"fieldname": "date", "fieldtype": "Data", "label": "Fourth"},
			]
			self.assertTrue(self.save(token, conv, {"web_form_fields": table}, user=OTHER)["ok"])
			rows = self.form_rows(form)
			self.assertEqual([r.label for r in rows], ["First", "Second changed", "Third", "Fourth"])
			self.assertEqual([r.name for r in rows[:3]], [r1, r2, r3])

		def test_a_saved_row_left_out_is_removed_and_the_others_kept(self):
			"""Removal is by omission: the row the person removed goes, the rest stay as saved."""
			form, (r1, _r2, r3) = self.web_form()
			conv, token = self.draft_of(form, r1)
			values = {"web_form_fields": [{"name": r1}, {"name": r3}]}
			self.assertTrue(self.save(token, conv, values, user=OTHER)["ok"])
			self.assertEqual(
				[(r.name, r.label) for r in self.form_rows(form)], [(r1, "First"), (r3, "Third")]
			)

		def test_a_row_name_not_on_the_record_is_marked_and_nothing_changes(self):
			"""A row name the record does not have is a mark on that row before the claim;
			the card stays open and every saved row stays."""
			form, (r1, _r2, _r3) = self.web_form()
			conv, token = self.draft_of(form, r1)
			before = self.sealed(token)
			values = {"web_form_fields": [{"name": r1}, {"name": "zz-not-a-row"}]}
			out = self.save(token, conv, values, user=OTHER)
			[mark] = out["error"]["fields"]
			self.assertEqual(
				(mark["parentfield"], mark["fieldname"], mark["idx"]), ("web_form_fields", "name", 2)
			)
			self.assert_untouched(token, before)
			self.assertEqual(len(self.form_rows(form)), 3)

		def test_an_update_sends_none_to_set_a_field_blank(self):
			"""On an update None is "make it blank": update_doc sets it, and the draft's own
			changes still go with it."""
			form, (r1, _r2, _r3) = self.web_form()
			frappe.db.set_value("Web Form", form, "success_message", "Thanks")
			frappe.db.commit()
			conv, token = self.draft_of(form, r1)
			self.assertTrue(self.save(token, conv, {"success_message": None}, user=OTHER)["ok"])
			self.assertFalse(frappe.db.get_value("Web Form", form, "success_message"))
			self.assertEqual(self.form_rows(form)[0].label, "First changed")

	class TestFailureAfterTheClaim(_DraftCase):
		def test_a_write_that_fails_after_the_claim_ends_failed_and_closed_with_one_receipt(self):
			"""Once claimed, a failure is never fixable (the card is over): the row ends
			Failed, its one display row says failed with the values that were tried, the
			agent is told once, and nothing is saved."""
			conv, token = self.todo_draft()
			with patch("jarvis.api.dispatch_confirmed", side_effect=_fail):
				out = self.save(token, conv, {"description": f"{MARK} tried"})
			self.assertIs(out["closed"], True)
			self.assertNotIn("fields", out["error"])
			self.assertIs(out["assistant_told"], True)
			row = self.row(token)
			self.assertEqual((row.status, row.reason_code, row.edited), ("Failed", "failed", 1))
			[decided] = self.tool_rows(conv)
			self.assertEqual((decided.tool_call_id, decided.action_outcome), (token, "failed"))
			self.assertIn(f"{MARK} tried", decided.tool_args)
			self.assertEqual(len(self.cont.calls), 1)
			self.assertEqual(self.todos("tried"), [])

	class TestReceiptRow(_DraftCase):
		def _next_step(self, conv, token):
			raw = frappe.db.get_value(MSG, {"conversation": conv, "tool_call_id": token}, "tool_result")
			return (json.loads(raw or "{}").get("data") or {}).get("suggested_next")

		def test_a_draft_create_gets_the_next_step_and_an_update_or_a_plain_card_does_not(self):
			"""The desktop reads Submit only from the receipt row: a draft create confirmed
			plainly must carry it there; an update and a card that is not a draft keep
			today's receipt. The suggestion itself is stubbed: this pins where it goes."""
			with patch.object(actions_api, "_suggest_next", return_value=SUBMIT):
				created, token = self.todo_draft(f"{MARK} next")
				self.assertTrue(self.confirm(token, created)["ok"])
				updated = self.make_conv()
				todo = self.make_todo()
				args = {"doctype": "ToDo", "name": todo, "changes": {"status": "Closed"}}
				update = self.mint(updated, "update_doc", args)
				self.assertTrue(self.confirm(update, updated)["ok"])
				plain_conv = self.make_conv()
				create = {"doctype": "ToDo", "values": {"description": f"{MARK} plain"}}
				plain = self.mint(plain_conv, "create_doc", create, draft=False)
				self.assertTrue(self.confirm(plain, plain_conv)["ok"])
			self.assertEqual(self._next_step(created, token), SUBMIT)
			self.assertIsNone(self._next_step(updated, update))
			self.assertIsNone(self._next_step(plain_conv, plain))

		def test_a_flip_with_no_outcome_says_failed(self):
			"""A card's row flipped with no outcome would lose its card and become a bare
			activity row (the live-draft rule would not retire it); it must say "failed"."""
			conv, token = self.todo_draft()
			api.persist_tool_receipt(conv, "create_doc", {}, None, action_outcome=None, flip_token=token)
			[row] = self.tool_rows(conv)
			self.assertEqual((row.tool_call_id, row.action_outcome), (token, "failed"))
			self.assertFalse(row.pending_card)

		def test_flag_off_is_todays_behaviour(self):
			"""With the flag off a confirmed card's receipt has no next step, even a draft
			card left from when it was on, and ``_dispatch`` still runs a confirmed card with
			``allow_risky`` and the plain commit watch."""
			plain_conv = self.make_conv()
			create = {"doctype": "ToDo", "values": {"description": f"{MARK} off plain"}}
			plain = self.mint(plain_conv, "create_doc", create, draft=False)
			left, leftover = self.todo_draft(f"{MARK} off left")
			with (
				patch.dict(frappe.conf, {FLAG: 0}),
				patch.object(actions_api, "_suggest_next", return_value=SUBMIT),
			):
				self.assertTrue(self.confirm(plain, plain_conv)["ok"])
				self.assertTrue(self.confirm(leftover, left)["ok"])
			self.assertIsNone(self._next_step(plain_conv, plain))
			self.assertIsNone(self._next_step(left, leftover))
			row = frappe._dict(self.row(plain), kind="chat")
			seen = []

			def confirmed(tool, args, **kwargs):
				seen.append(kwargs.get("allow_risky"))
				return {"ok": True, "data": {}}

			with (
				patch("jarvis.api.dispatch_confirmed", side_effect=confirmed),
				patch.object(CommitWatch, "arm", autospec=True, side_effect=CommitWatch.arm) as arm,
			):
				result, interfered, crash = _execute._dispatch(row, {"doctype": "ToDo", "values": {}})
			frappe.db.rollback()
			self.assertEqual((result["ok"], interfered, crash, seen), (True, False, "", [True]))
			self.assertFalse(arm.call_args.kwargs.get("savepoint"))
