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

	def __init__(self, tables=None, saved=None, draft=None, refused=()):
		self.tables = tables or {}
		self.saved = saved or {}
		self.draft = draft or {}
		self.refused = set(refused)

	def table_kind(self, field):
		return self.tables.get(field)

	def field_error(self, field, value):
		return "refused" if field in self.refused else ""

	def cell_error(self, table, field, value):
		return "refused" if field in self.refused else ""

	def saved_rows(self, table):
		return frozenset(self.saved.get(table, ()))

	def draft_rows(self, table):
		return frozenset(self.draft.get(table, ()))

	def mark(self, field, message, *, table=None, idx=None):
		return {"fieldname": field, "parentfield": table, "idx": idx, "message": message}


class TestDraftValuesPure(unittest.TestCase):
	def test_an_update_row_name_must_be_a_saved_row_or_one_the_draft_named(self):
		"""A made-up or foreign row name must be marked before the claim; otherwise it
		reaches the write, fails after the claim, and closes the card instead of marking
		the row. Saved rows, rows the draft named and new rows pass."""
		rules = _Rules(tables={"items": "Table"}, saved={"items": {"r1"}}, draft={"items": {"r9"}})
		rows = [{"name": "r1"}, {"name": "r9"}, {"name": "zz"}, {"label": "new"}, {"name": ""}]
		found = draft_values.errors({"items": rows}, rules, is_update=True)
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
		"""A malformed table must be a field mark, not a crash in the write; ``None`` is
		update_doc's own "clear the table" and passes."""
		rules = _Rules(tables={"items": "Table"})
		for bad in ("x", [1], {"name": "r1"}):
			with self.subTest(bad=bad):
				[mark] = draft_values.errors({"items": bad}, rules, is_update=True)
				self.assertEqual(mark["message"], draft_values.NOT_ROWS)
		self.assertEqual(draft_values.errors({"items": None}, rules, is_update=True), [])

	def test_an_update_writes_the_edits_over_the_drafts_own_changes(self):
		"""The form sends only what was edited: the draft's other changes must still be
		written (an unedited save is exactly the card), and a table sent replaces the
		draft's list for that table whole."""
		args = {"doctype": "T", "name": "N", "changes": {"status": "Open", "items": [{"name": "r1"}]}}
		same = draft_values.edited_call("update_doc", args, {})
		self.assertEqual(same, args)
		edited = draft_values.edited_call("update_doc", args, {"priority": "High"})
		self.assertEqual(edited["changes"], {"status": "Open", "items": [{"name": "r1"}], "priority": "High"})
		rows = [{"name": "r1"}, {"label": "new"}]
		self.assertEqual(
			draft_values.edited_call("update_doc", args, {"items": rows})["changes"]["items"], rows
		)
		self.assertEqual(args["changes"]["items"], [{"name": "r1"}], "the sealed call is never changed")

	def test_a_create_writes_exactly_the_form_and_keeps_a_typed_prompt_name(self):
		"""A create's form is its whole record: a field the person cleared must not come
		back from the draft. Only the name a prompt-named doctype's draft typed is kept
		when the form has no name field to send it."""
		args = {"doctype": "T", "values": {"a": 1, "name": "X-1"}, "continue": True}
		self.assertEqual(
			draft_values.edited_call("create_doc", args, {"b": 2}),
			{"doctype": "T", "values": {"b": 2}, "continue": True},
		)
		self.assertEqual(
			draft_values.edited_call("create_doc", args, {"b": 2}, prompt_name=True)["values"],
			{"b": 2, "name": "X-1"},
		)
		self.assertEqual(
			draft_values.edited_call("create_doc", args, {"name": "X-2"}, prompt_name=True)["values"],
			{"name": "X-2"},
		)


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except ImportError:  # the pure class above still runs without a site
	FrappeTestCase = None

if FrappeTestCase is not None:
	import threading
	from unittest.mock import patch

	from jarvis._commit_watch import CommitWatch
	from jarvis.chat import actions_api, agent_notes, confirm_card, draft_card, draft_save, pending_confirm
	from jarvis.chat.pending_actions import _execute
	from jarvis.tests._pending_action_helpers import (
		CONV,
		MSG,
		OTHER,
		OWNER,
		PA,
		PendingActionTestMixin,
		as_user,
		ensure_user,
	)
	from jarvis.tests._transport_helpers import provision_legacy_site
	from jarvis.tests.test_pending_confirm import _purge_legacy

	FLAG = "jarvis_draft_cards"
	WEB = "dce-web@example.com"  # may write a Web Form (Website Manager)
	MARK = "pa-test dce"  # every ToDo here; the mixin's cleanup removes "pa-test%"
	SUBMIT = {"kind": "submit", "action": None}
	NOTE_PREFIX = "the user confirmed the draft and it was saved: "

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
			ensure_user(WEB, ("Jarvis User", "Website Manager"))
			provision_legacy_site(self)
			for user in (OWNER, WEB):
				_purge_legacy(user)
				self.addCleanup(_purge_legacy, user)
			self.web_forms = []
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
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE owner_user=%(u)s", {"u": WEB})
			convs = frappe.get_all(CONV, filters={"owner": WEB}, pluck="name")
			if convs:
				frappe.db.delete(MSG, {"conversation": ["in", convs]})
				frappe.db.delete("Jarvis Chat Turn", {"conversation": ["in", convs]})
				frappe.db.delete(CONV, {"name": ["in", convs]})
			frappe.db.delete("Jarvis Agent Write", {"actor": WEB})
			if self.web_forms:
				frappe.db.delete("Web Form Field", {"parent": ["in", self.web_forms]})
				frappe.db.delete("Web Form", {"name": ["in", self.web_forms]})
			frappe.db.delete("Web Form", {"title": ["like", f"{MARK}%"]})
			frappe.db.delete("ToDo", {"description": ["like", f"%{MARK}%"]})
			frappe.db.commit()

		def mint(self, conv, tool, args, *, owner=OWNER, draft=True):
			"""Park ``tool(args)`` as ``api._run_tool`` does (card, draft, display row)."""
			would = args.get("values") if tool == "create_doc" else args.get("changes")
			card = confirm_card.build_card(tool, args, {"would": would}, draft)
			self.assertTrue(card)
			if draft:
				self.assertTrue(draft_card.attach(card, tool, args, would))
			token = pending_confirm.mint(
				conversation=conv,
				owner=owner,
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

		def tool_rows(self, conv):
			return frappe.get_all(
				MSG,
				filters={"conversation": conv, "role": ["in", ["tool", "assistant"]]},
				fields=["role", "tool_call_id", "tool_args", "tool_result", "action_outcome", "pending_card"],
			)

		def todos(self, text):
			"""ToDo records whose description holds ``MARK`` + ``text``."""
			return frappe.get_all("ToDo", filters={"description": ["like", f"%{MARK} {text}%"]}, pluck="name")

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

		def test_the_flag_off_refuses_and_leaves_the_card(self):
			"""Dark means dark: with ``jarvis_draft_cards`` off the endpoint writes nothing."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			with patch.dict(frappe.conf, {FLAG: 0}):
				out = self.save(token, conv, {"description": f"{MARK} flag off"})
			self.assertEqual((out["closed"], out["error"]["code"]), (True, draft_save.CLOSED))
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
			out = self.save(token, conv, {"description": f"{MARK} file box"})
			self.assertEqual((out["closed"], out["error"]["code"]), (True, draft_save.CLOSED))
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

		def test_a_second_save_and_a_late_confirm_write_nothing_more(self):
			"""Create pressed twice, or on two devices: one record. A second save is closed
			and Confirm on the saved token runs nothing."""
			conv, token = self.todo_draft()
			self.assertTrue(self.save(token, conv, {"description": f"{MARK} once"})["ok"])
			again = self.save(token, conv, {"description": f"{MARK} once"})
			self.assertEqual((again["closed"], again["error"]["code"]), (True, draft_save.CLOSED))
			self.assertFalse(self.confirm(token, conv)["ok"])
			self.assertEqual(len(self.todos("once")), 1)

	class TestRefusedBeforeTheClaim(_DraftCase):
		def test_a_missing_value_is_marked_and_the_card_is_untouched(self):
			"""A fixable save must keep the card open exactly as parked (still Pending, the
			same card and seal) with the field marked, and leave no record behind."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			count = frappe.db.count("ToDo", {"owner": OWNER})
			out = self.save(token, conv, {"priority": "High"})
			self.assertFalse(out["ok"])
			self.assertNotIn("closed", out)
			self.assertIn("description", [f["fieldname"] for f in out["error"]["fields"]])
			self.assert_untouched(token, before)
			self.assertEqual(frappe.db.count("ToDo", {"owner": OWNER}), count)

		def test_a_protected_field_is_marked_not_written(self):
			"""``docstatus`` sent from the form must be a refused mark, never a submit."""
			conv, token = self.todo_draft()
			before = self.sealed(token)
			out = self.save(token, conv, {"description": f"{MARK} protected", "docstatus": 1})
			[mark] = out["error"]["fields"]
			self.assertEqual((mark["fieldname"], mark["invalid"]), ("docstatus", 1))
			self.assert_untouched(token, before)
			self.assertEqual(self.todos("protected"), [])

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

		def test_a_sensitive_edit_is_refused_closed(self):
			"""Script put on a Web Form makes the write sensitive: the person's one click is
			not that card, and the write runs without ``allow_risky``, so it is refused
			before anything runs."""
			conv = self.make_conv()
			args = {"doctype": "Web Form", "values": {"title": f"{MARK} form", "doc_type": "ToDo"}}
			token = self.mint(conv, "create_doc", args)
			before = self.sealed(token)
			values = {**args["values"], "client_script": "console.log(1)"}
			out = self.save(token, conv, values)
			self.assertEqual((out["closed"], out["error"]["code"]), (True, draft_save.NEEDS_CARD))
			self.assert_untouched(token, before)
			self.assertFalse(frappe.db.exists("Web Form", {"title": f"{MARK} form"}))

		def test_a_stale_update_target_is_refused_closed_and_the_card_stays(self):
			"""A record changed after the draft must not be overwritten by it (update_doc
			checks no ``modified``); the row stays Pending for the agent to redo."""
			conv = self.make_conv()
			todo = self.make_todo()
			token = self.mint(
				conv, "update_doc", {"doctype": "ToDo", "name": todo, "changes": {"status": "Closed"}}
			)
			before = self.sealed(token)
			frappe.db.set_value("ToDo", todo, "priority", "High")
			frappe.db.commit()
			out = self.save(token, conv, {})
			self.assertEqual((out["closed"], out["error"]["code"]), (True, draft_save.STALE))
			self.assert_untouched(token, before)
			self.assertEqual(frappe.db.get_value("ToDo", todo, "status"), "Open")

		def test_a_row_another_decider_holds_is_busy_and_nothing_is_written(self):
			"""A Confirm, a Discard or a second device holding the row: the save must not
			wait or write; exactly one decider runs. A real second connection holds the row
			lock, as ``execute`` takes it."""
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
			self.assertEqual((out["closed"], out["error"]["code"]), (True, draft_save.BUSY))
			self.assertEqual(self.row(token).status, "Pending")
			self.assertEqual(self.todos("busy"), [])

	class TestTableRows(_DraftCase):
		"""An update's table is its complete final row list (``merge_child_rows``)."""

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
			self.web_forms.append(doc.name)
			return doc.name, [r.name for r in doc.web_form_fields]

		def draft_of(self, form, first):
			"""An update draft that touches 1 of the 3 rows, parked for WEB."""
			conv = self.make_conv(owner=WEB)
			changes = {"web_form_fields": [{"name": first, "label": "First changed"}]}
			args = {"doctype": "Web Form", "name": form, "changes": changes}
			return conv, self.mint(conv, "update_doc", args, owner=WEB)

		def rows(self, form):
			return frappe.get_all(
				"Web Form Field",
				filters={"parent": form, "parenttype": "Web Form", "parentfield": "web_form_fields"},
				fields=["name", "fieldname", "label"],
				order_by="idx asc",
			)

		def test_an_unedited_save_leaves_what_confirm_leaves(self):
			"""DATA LOSS guard: saving the card unedited must write the card's own call and
			no other, so the record ends with the same rows a plain Confirm leaves."""
			confirmed, (c1, _c2, _c3) = self.web_form()
			saved, (s1, _s2, _s3) = self.web_form()
			conv_c, token_c = self.draft_of(confirmed, c1)
			conv_s, token_s = self.draft_of(saved, s1)
			self.assertTrue(self.confirm(token_c, conv_c, user=WEB)["ok"])
			self.assertTrue(self.save(token_s, conv_s, {}, user=WEB)["ok"])
			shape = [(r.fieldname, r.label) for r in self.rows(confirmed)]
			self.assertEqual([(r.fieldname, r.label) for r in self.rows(saved)], shape)

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
			self.assertTrue(self.save(token, conv, {"web_form_fields": table}, user=WEB)["ok"])
			rows = self.rows(form)
			self.assertEqual([r.label for r in rows], ["First", "Second changed", "Third", "Fourth"])
			self.assertEqual([r.name for r in rows[:3]], [r1, r2, r3])

		def test_a_saved_row_left_out_is_removed_and_the_others_kept(self):
			"""Removal is by omission: the row the person removed goes, the rest stay as saved."""
			form, (r1, _r2, r3) = self.web_form()
			conv, token = self.draft_of(form, r1)
			values = {"web_form_fields": [{"name": r1}, {"name": r3}]}
			self.assertTrue(self.save(token, conv, values, user=WEB)["ok"])
			self.assertEqual([(r.name, r.label) for r in self.rows(form)], [(r1, "First"), (r3, "Third")])

		def test_a_row_name_not_on_the_record_is_marked_and_nothing_changes(self):
			"""A row name the record does not have is a mark on that row before the claim;
			the card stays open and every saved row stays."""
			form, (r1, _r2, _r3) = self.web_form()
			conv, token = self.draft_of(form, r1)
			before = self.sealed(token)
			values = {"web_form_fields": [{"name": r1}, {"name": "zz-not-a-row"}]}
			out = self.save(token, conv, values, user=WEB)
			[mark] = out["error"]["fields"]
			self.assertEqual(
				(mark["parentfield"], mark["fieldname"], mark["idx"]), ("web_form_fields", "name", 2)
			)
			self.assert_untouched(token, before)
			self.assertEqual(len(self.rows(form)), 3)

	class TestFailureAfterTheClaim(_DraftCase):
		def test_a_write_that_fails_after_the_claim_ends_failed_and_closed_with_one_receipt(self):
			"""Once claimed, a failure is never fixable (the card is over): the row ends
			Failed, its one display row says failed with the values that were tried, the
			agent is told once, and nothing is saved."""
			conv, token = self.todo_draft()

			def fail(*args, **kwargs):
				raise frappe.ValidationError("boom")

			with patch("jarvis.api.dispatch_confirmed", side_effect=fail):
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

	class TestNextStepOnTheReceipt(_DraftCase):
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
