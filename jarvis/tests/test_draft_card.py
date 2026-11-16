"""Draft cards on the server (``jarvis.chat.draft_card``).

The first class is pure (no site): the call test and the payload builder with a stand-in
field list, plus the shared fixture file the apps read. The second class builds real cards
on a site (CI runs it)."""

from __future__ import annotations

import json
import os
import unittest

from jarvis.chat import draft_card

FIXTURES = os.path.join(
	os.path.dirname(__file__), "..", "..", "frontend", "src", "lib", "draftCardFixtures.json"
)
SKIP = frozenset({"doctype", "name", "owner", "idx", "parent", "parenttype", "parentfield"})

# A stand-in DocType "Timesheet": fields `employee`, `note`, table `time_logs` of rows with
# `from_time`, `hours`.
_MAIN = {"employee", "note", "time_logs"}
_ROW = {"from_time", "hours"}


def _has_field(key):
	return key in _MAIN


def _child_has(key):
	return (lambda k: k in _ROW) if key == "time_logs" else None


def _build(verb, name, values, would=None, cont=False):
	return draft_card.payload_from(
		verb,
		"Timesheet",
		name,
		values,
		would,
		has_field=_has_field,
		child_has=_child_has,
		skip=SKIP,
		cont=cont,
	)


# Inputs of the hand-written fixture cards, whose `draft` comes from the pure builder over the
# stand-in "Timesheet" above. The builder-sourced ones are checked on a site, by
# TestFixturesMatchTheRealBuilder.
HAND_CALLS = {
	"create-ignored-field": ("create", "", {"employee": "EMP-1", "colour": "red"}),
	"update-table-rows": (
		"update",
		"TS-0001",
		{"time_logs": [{"from_time": "10:00", "hrs_worked": 2}, {"from_time": "11:00"}]},
	),
}


class TestDraftCallPure(unittest.TestCase):
	def test_is_draft_call_truth_table(self):
		"""A batch (docs / updates), a forced card or another tool must never be treated as a
		single-record draft: the draft panel can only show one record."""
		create = {"doctype": "ToDo", "values": {"description": "x"}}
		update = {"doctype": "ToDo", "name": "T1", "changes": {"status": "Closed"}}
		cases = [
			("create_doc", create, False, True),
			("update_doc", update, False, True),
			("create_doc", {**create, "docs": [create]}, False, False),
			("update_doc", {**update, "updates": [update]}, False, False),
			("create_doc", create, True, False),
			("update_doc", update, True, False),
			("delete_doc", update, False, False),
			("create_doc", {"doctype": "To Do; drop", "values": {}}, False, False),
			("update_doc", {"doctype": "ToDo", "changes": {"a": 1}}, False, False),
		]
		for tool, args, must, want in cases:
			with self.subTest(tool=tool, must=must, keys=sorted(args)):
				self.assertIs(draft_card.is_draft_call(tool, args, must_card=must), want)

	def test_create_payload_keeps_table_rows_and_names_the_ignored_fields(self):
		"""A key that is not a field (main form or child row) is named in `ignored` and not
		kept in `values`, so the save's silent drop is told to the person."""
		values = {
			"employee": "EMP-1",
			"colour": "red",
			"time_logs": [{"doctype": "Timesheet Detail", "from_time": "09:00", "hrs_worked": 2}],
		}
		out = _build("create", "", values)
		self.assertEqual(out["ignored"], ["colour", "time_logs.hrs_worked"])
		self.assertEqual(
			out["values"],
			{"employee": "EMP-1", "time_logs": [{"doctype": "Timesheet Detail", "from_time": "09:00"}]},
		)

	def test_a_row_with_only_its_doctype_key_ignores_nothing(self):
		"""The spike card flagged a child row's own `doctype` key as an unrecognized field."""
		out = _build("create", "", {"time_logs": [{"doctype": "Timesheet Detail", "from_time": "9"}]})
		self.assertEqual(out["ignored"], [])

	def test_an_unknown_row_key_is_listed_once_across_rows(self):
		"""Three rows with the same bad key name it once: the list is read by a person."""
		rows = [{"from_time": str(i), "hrs_worked": i} for i in range(3)]
		self.assertEqual(_build("create", "", {"time_logs": rows})["ignored"], ["time_logs.hrs_worked"])

	def test_a_key_the_trial_dropped_is_left_out_of_values(self):
		"""A field the permission filter removed from the trial doc must not appear in the
		draft, as it does not appear in the card's rows."""
		out = _build("create", "", {"employee": "E", "note": "n"}, would={"employee": "E"})
		self.assertEqual(out["values"], {"employee": "E"})

	def test_update_payload_carries_the_changes_continue_and_title(self):
		out = _build("update", "TS-0001", {"note": "Late"}, cont=True)
		self.assertEqual((out["verb"], out["name"], out["continue"]), ("update", "TS-0001", True))
		self.assertEqual(out["title"], "Timesheet TS-0001")
		self.assertEqual(_build("create", "", {"note": "n"})["title"], "New Timesheet")

	def test_a_real_field_that_shares_a_bookkeeping_name_is_kept_not_skipped(self):
		"""A DocType with a real field named like a default column must keep it, in the
		main form and in a row: the field check comes before the bookkeeping skip."""
		out = draft_card.payload_from(
			"create",
			"Thing",
			"",
			{"idx": 3, "rows": [{"parent": "p", "idx": 1}]},
			None,
			has_field=lambda k: k in {"idx", "rows"},
			child_has=lambda k: (lambda r: r == "parent") if k == "rows" else None,
			skip=SKIP,
		)
		self.assertEqual(out["ignored"], [])
		self.assertEqual(out["values"], {"idx": 3, "rows": [{"parent": "p", "idx": 1}]})

	def test_a_field_the_trial_dropped_is_named_in_dropped_not_ignored(self):
		"""A permission-filtered field must not vanish silently, and must not be called
		"not a field on this form" (that is what `ignored` means)."""
		out = _build("create", "", {"employee": "E", "note": "n", "colour": "red"}, would={"employee": "E"})
		self.assertEqual((out["dropped"], out["ignored"]), (["note"], ["colour"]))

	def test_hand_fixture_drafts_equal_the_pure_builder(self):
		"""The hand-written fixture cards cannot drift from the draft the builder gives for
		the same call; every item says where it came from."""
		with open(FIXTURES) as f:
			items = {item["id"]: item for item in json.load(f)}
		self.assertEqual({i["source"] for i in items.values()}, {"hand", "builder"})
		for key, (verb, name, values) in HAND_CALLS.items():
			with self.subTest(key):
				self.assertEqual(items[key]["source"], "hand")
				self.assertEqual(items[key]["card"]["draft"], _build(verb, name, values))
				self.assertTrue(items[key]["card"].get("diff") or items[key]["card"].get("rows"))
		self.assertNotIn("draft", items["sensitive"]["card"])
		self.assertEqual(items["sensitive"]["card"]["risk"], "sensitive")
		self.assertIsInstance(items["unreadable"]["card"]["draft"]["values"], str)


def _draft_with(verb, name, ignored, dropped):
	return {"verb": verb, "doctype": "Timesheet", "name": name, "ignored": ignored, "dropped": dropped}


def _sales_order_draft():
	"""A Sales Order draft with a table, as the builder gives it (stand-in field list)."""
	main = {"customer", "delivery_date", "items"}
	row = {"item_code", "qty", "rate"}
	return draft_card.payload_from(
		"create",
		"Sales Order",
		"",
		{
			"customer": "Acme",
			"delivery_date": "2026-12-01",
			"items": [{"item_code": "A", "qty": 1, "rate": 10, "colour": "red"}] * 5,
		},
		None,
		has_field=lambda k: k in main,
		child_has=lambda k: (lambda r: r in row) if k == "items" else None,
		skip=SKIP,
	)


class TestShortResultPure(unittest.TestCase):
	def test_shape_has_identity_and_ignored_fields_and_no_preview(self):
		"""A parked draft's model result is the short envelope: identity from the card's
		draft, the ignored names, the fixed note, and none of the would-be document."""
		res = draft_card.short_result("create_doc", _build("create", "", {"employee": "E", "colour": "r"}))
		self.assertEqual(
			res,
			{
				"ok": True,
				"data": {
					"status": "pending_confirmation",
					"tool": "create_doc",
					"draft": {"verb": "create", "doctype": "Timesheet", "name": ""},
					"ignored_fields": ["colour"],
					"note": draft_card.SHORT_NOTE,
				},
			},
		)

	def _assert_fits(self, draft, preview=None):
		data = draft_card.short_result("create_doc", draft, preview)["data"]
		bounded = {k: v for k, v in data.items() if k != "warnings"}
		self.assertLessEqual(len(json.dumps(bounded)), draft_card.SHORT_LIMIT)
		for key in ("status", "tool", "note"):
			self.assertIn(key, data)
		for key in ("verb", "doctype", "name"):
			self.assertIn(key, data["draft"])
		self.assertEqual(data["note"], draft_card.SHORT_NOTE)
		return data

	def test_a_sales_order_draft_with_table_rows_fits(self):
		"""A real-shaped Sales Order draft (table rows, an unknown row key) fits the bound and
		names the row key as `items.colour`."""
		data = self._assert_fits(_sales_order_draft())
		self.assertEqual(data["ignored_fields"], ["items.colour"])

	def test_a_140_character_non_ascii_record_name_still_fits(self):
		"""JSON escapes a non-ASCII character to 6 bytes, so a long Tamil name alone broke the
		bound; the name is shortened (kept from the start, ended with "...")."""
		name = "\u0b9a" * 140
		data = self._assert_fits(_draft_with("update", name, [], []))
		self.assertTrue(data["draft"]["name"].endswith("..."))
		self.assertTrue(name.startswith(data["draft"]["name"][:-3]))

	def test_5000_unknown_keys_are_capped_and_counted(self):
		"""Thousands of unknown keys give 10 clipped names and a count of the rest, so the
		work and the size do not grow with the call."""
		data = self._assert_fits(_draft_with("create", "", [f"made_up_{i}" for i in range(5000)], []))
		self.assertEqual(len(data["ignored_fields"]), 10)
		self.assertEqual(data["more_ignored"], 4990)
		self.assertNotIn("more_dropped", data)

	def test_a_long_field_name_is_clipped_to_40_characters(self):
		"""One 300-character key does not take the model's budget: it is clipped to 40."""
		data = self._assert_fits(_draft_with("create", "", ["k" * 300], []))
		self.assertEqual(data["ignored_fields"], ["k" * 40])

	def test_ten_long_non_ascii_names_and_a_long_name_cut_dropped_then_ignored_then_the_name(self):
		"""When even the capped lists do not fit, `dropped_fields` loses names before
		`ignored_fields` does, and each cut is counted."""
		long_names = [f"{i}" + "\u0b9a" * 39 for i in range(10)]
		data = self._assert_fits(_draft_with("update", "N" * 100, long_names, long_names))
		self.assertNotIn("dropped_fields", data)
		self.assertEqual(data["more_dropped"], 10)
		self.assertEqual(data["more_ignored"] + len(data["ignored_fields"]), 10)

	def test_dropped_fields_only_when_the_trial_dropped_some(self):
		"""`dropped_fields` names what a permission filter removed, and is absent otherwise."""
		bare = draft_card.short_result("create_doc", _build("create", "", {"employee": "E"}))
		self.assertNotIn("dropped_fields", bare["data"])
		cut = draft_card.short_result(
			"create_doc", _build("create", "", {"employee": "E", "note": "n"}, {"employee": "E"})
		)
		self.assertEqual(cut["data"]["dropped_fields"], ["note"])

	def test_the_trials_two_notes_ride_along_whole_and_cost_the_draft_nothing(self):
		"""A real-sized (524 character) jobs note and the hook caveat arrive byte for byte,
		and do not squeeze the doctype or the record name (clipping once cut the
		"read the document back" instruction and turned both into "...")."""
		queue_note = "Confirming will also start 2 background jobs. " * 11
		queue_note = queue_note[:524]
		draft = {
			"verb": "create",
			"doctype": "Sales Order",
			"name": "SO-0001",
			"ignored": ["colour"],
			"dropped": [],
		}
		preview = {"will_queue_note": queue_note, "hook_rolled_back": True}
		data = self._assert_fits(draft, preview)
		self.assertEqual(data["draft"]["doctype"], "Sales Order")
		self.assertEqual(data["draft"]["name"], "SO-0001")
		self.assertEqual(data["warnings"], [queue_note, draft_card.HOOK_WARNING])


class TestPendingDraftRowPure(unittest.TestCase):
	def test_only_a_pending_tool_row_with_a_draft_on_its_card_is_a_draft_row(self):
		"""`draft_card` on a history row must go false the moment the row is decided or its
		card is gone, or the live-draft rule would keep counting a settled draft."""
		card = {"title": "t", "draft": {"verb": "create"}}
		row = {"role": "tool", "tool_status": "pending", "pending_card": card}
		cases = [
			(row, True),
			({**row, "tool_status": ""}, False),
			({**row, "pending_card": None}, False),
			({**row, "pending_card": {"title": "t"}}, False),
			({**row, "pending_card": {"draft": "x"}}, False),
			({**row, "role": "assistant"}, False),
		]
		for case, want in cases:
			with self.subTest(case=case):
				self.assertIs(draft_card.is_pending_draft_row(case), want)


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except ImportError:  # the pure class above still runs without a site
	FrappeTestCase = None

if FrappeTestCase is not None:
	from unittest.mock import patch

	from jarvis import api
	from jarvis.chat import confirm_card, pending_confirm
	from jarvis.chat._record_summary import table_rows
	from jarvis.chat.pending_actions import _seal

	PA = "Jarvis Pending Action"
	FLAG = "jarvis_draft_cards"

	def _park_card(tool, args, would, draft):
		"""The card the park builds, as ``api._run_tool`` does."""
		card = confirm_card.build_card(tool, args, {"would": would}, draft)
		if draft:
			draft_card.attach(card, tool, args, would)
		return card

	def _as_json(card) -> str:
		return json.dumps(card, indent=1, sort_keys=True, default=str)

	class TestDraftCardOnSite(FrappeTestCase):
		def test_flag_off_is_todays_behaviour(self):
			"""With `jarvis_draft_cards` at 0 the park builds no draft and keeps the trial's
			name on a create card, so the cards of the old persona's `create_doc` calls do
			not change."""
			with patch.dict(frappe.conf, {FLAG: 0}):
				self.assertFalse(draft_card.enabled())
				args = {"doctype": "ToDo", "values": {"description": "x"}}
				self.assertFalse(draft_card.wanted("create_doc", args, must_card=False))
			card = _park_card("create_doc", args, {"name": "TD-trial", "description": "x"}, False)
			self.assertNotIn("draft", card)
			self.assertEqual(card["name"], "TD-trial")

		def test_flag_reads_as_a_cint(self):
			"""The flag is read with cint, so "1" and 1 turn it on and "0" keeps it off."""
			for raw, want in ((1, True), ("1", True), ("0", False), (0, False)):
				with self.subTest(raw=raw), patch.dict(frappe.conf, {FLAG: raw}):
					self.assertIs(draft_card.enabled(), want)

		def test_create_card_has_draft_and_no_trial_name_and_keeps_its_title(self):
			"""A draft create card is titled "New ToDo" inside `draft`, drops the name the trial
			allocated (a number the save does not keep) and names the field it left out."""
			args = {"doctype": "ToDo", "values": {"description": "x", "bogus": 1}}
			card = _park_card("create_doc", args, {"name": "TD-trial", "description": "x"}, True)
			self.assertEqual(card["draft"]["title"], "New ToDo")
			self.assertNotIn("title", card)
			self.assertIsNone(card["name"])
			self.assertEqual(card["draft"]["values"], {"description": "x"})
			self.assertEqual(card["draft"]["ignored"], ["bogus"])

		def test_update_card_keeps_its_name_and_its_own_title(self):
			"""An update card's `title` is the record's title-field value, which the card UI
			reads: a draft puts its own title under `draft` and leaves it alone."""
			todo = frappe.get_doc({"doctype": "ToDo", "description": "zz-draft-title"}).insert()
			args = {"doctype": "ToDo", "name": todo.name, "changes": {"status": "Closed"}}
			card = _park_card("update_doc", args, {"status": "Closed"}, True)
			self.assertEqual(card["name"], todo.name)
			self.assertEqual(card["title"], "zz-draft-title")
			self.assertEqual(card["draft"]["title"], f"ToDo {todo.name}")
			self.assertEqual(card["draft"]["values"], {"status": "Closed"})

		def test_row_doctype_key_is_not_unrecognized_and_unknown_fields_only_on_a_draft(self):
			"""A child row that carries its own `doctype` is not an unknown column (the spike
			card's false alarm); a made-up key is. `unknown_fields` is added only when a draft
			is asked for, so every other table dict keeps the keys it had."""
			meta = frappe.get_meta("User")
			role = {"doctype": "Has Role", "role": "System Manager"}
			plain = table_rows(meta, "roles", [{**role, "hrs_worked": 2}])
			self.assertEqual(plain["unknown_columns"], 1)
			self.assertNotIn("unknown_fields", plain)
			drawn = table_rows(meta, "roles", [{**role, "hrs_worked": 2}], draft=True)
			self.assertEqual(drawn["unknown_fields"], ["hrs_worked"])
			clean = table_rows(meta, "roles", [role], draft=True)
			self.assertEqual((clean["unknown_columns"], clean["unknown_fields"]), (0, []))

		def test_draft_table_shows_computed_list_columns_from_the_trial(self):
			"""A list-view cell the caller did not send shows with the trial's value on a
			draft card; without `draft` the table keeps only the keys the caller set."""
			args = {"doctype": "Web Form", "values": {"web_form_fields": [{"fieldname": "title"}]}}
			would = {"web_form_fields": [{"fieldname": "title", "fieldtype": "Data"}]}
			plain = confirm_card.build_card("create_doc", args, {"would": would})
			drawn = confirm_card.build_card("create_doc", args, {"would": would}, True)
			self.assertEqual(plain["tables"][0]["fieldnames"], ["fieldname"])
			self.assertIn("fieldtype", drawn["tables"][0]["fieldnames"])

	class TestParkPathAttachesTheDraft(FrappeTestCase):
		"""The real park (`api._run_tool` -> `pending_confirm.mint` -> `pending_actions.park`).

		Why the seal covers the draft: `_run_tool` sets `preview["card"]` (api.py park block),
		`mint` passes `card=preview["card"]` to `park` (pending_confirm.py), `park` builds the
		row from that dict (pending_actions/_park.py `frappe.get_doc({... "card": card ...})`),
		and the row's `validate` stores the canonical card text and then seals
		`card_sha256(self)` over it (jarvis_pending_action.py), so the hash is over exactly
		the card that was attached."""

		def setUp(self):
			super().setUp()
			self.owner = frappe.session.user
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			self.conv = (
				frappe.get_doc({"doctype": "Jarvis Conversation", "title": "draft park test"})
				.insert(ignore_permissions=True)
				.name
			)
			self.addCleanup(self._cleanup)

		def _cleanup(self):
			for rec in pending_confirm.list_for_owner(self.owner):
				try:
					pending_confirm.consume(
						rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
					)
				except Exception:
					pass
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE conversation=%(c)s", {"c": self.conv})
			frappe.db.delete(MSG, {"conversation": self.conv})
			frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
			# These tests commit mid-test (the park and the confirm do), so the class rollback keeps
			# the committed rows: commit the deletes too, or the Executed row outlives the class.
			frappe.db.commit()

		def _park(self, flag):
			args = {"doctype": "ToDo", "values": {"description": "draft-park-xyz"}}
			with patch.dict(frappe.conf, {FLAG: flag}):
				res = api._run_tool("create_doc", json.loads(json.dumps(args)), conversation=self.conv)
			self.assertEqual(res["data"]["status"], "pending_confirmation", res)
			names = frappe.get_all(PA, filters={"conversation": self.conv}, pluck="name")
			self.assertEqual(len(names), 1)
			fields = [*_seal.BOUND_FIELDS, "card", "preview", "summary", "sealed_call"]
			return args, frappe.db.get_value(PA, names[0], fields, as_dict=True)

		def test_flag_on_stores_the_draft_and_the_seal_covers_it(self):
			"""The stored card has `draft`, the sealed call verifies against that stored card,
			and removing the draft from the card would break the hash (the seal covers it)."""
			_args, row = self._park(1)
			card = json.loads(row.card)
			self.assertEqual(card["draft"]["verb"], "create")
			self.assertEqual(card["draft"]["values"], {"description": "draft-park-xyz"})
			self.assertEqual(card["draft"]["title"], "New ToDo")
			sealed = _seal.unseal_call(row)  # raises SealError on a card that is not what was sealed
			bare = {**row, "card": json.dumps({k: v for k, v in card.items() if k != "draft"})}
			self.assertNotEqual(_seal.card_sha256(bare), sealed["card_sha256"])

		def test_flag_off_parks_todays_card(self):
			"""With the flag off the stored card has no `draft` and equals what the builder
			(without `draft`) returns for the stored trial."""
			args, row = self._park(0)
			card = json.loads(row.card)
			card.pop("proposal_key", None)  # api adds it after the builder
			preview = {k: v for k, v in json.loads(row.preview).items() if k != "card"}
			expected = json.loads(
				json.dumps(confirm_card.build_card("create_doc", args, preview), default=str)
			)
			self.assertNotIn("draft", card)
			self.assertEqual(card, expected)
			_seal.unseal_call(row)

	class TestFixturesMatchTheRealBuilder(FrappeTestCase):
		"""The builder-sourced fixture cards (`frontend/src/lib/draftCardFixtures.json`) equal
		the full cards the real builder gives on core DocTypes. On a mismatch the message
		prints the real card as JSON so the fixture can be corrected from the CI log."""

		def _items(self):
			with open(FIXTURES) as f:
				return {i["id"]: i for i in json.load(f) if i["source"] == "builder"}

		def _same(self, key, card):
			want = self._items()[key]["card"]
			self.assertEqual(card, want, f"fixture {key}: the real card is\n{_as_json(card)}")

		def test_create_simple(self):
			"""A ToDo create: a stale fixture means the apps draw a card the server does not send."""
			args = {"doctype": "ToDo", "values": {"description": "Call the supplier"}}
			self._same(
				"create-simple", _park_card("create_doc", args, {"description": "Call the supplier"}, True)
			)

		def test_create_with_table(self):
			"""A Web Form with a Web Form Field row, whose list columns come from the trial."""
			args = {
				"doctype": "Web Form",
				"values": {
					"title": "Feedback",
					"web_form_fields": [{"fieldname": "email", "fieldtype": "Data"}],
				},
			}
			would = {
				"title": "Feedback",
				"web_form_fields": [
					{"fieldname": "email", "fieldtype": "Data", "label": "Email", "reqd": 0, "options": ""}
				],
			}
			self._same("create-with-table", _park_card("create_doc", args, would, True))

		def test_update_diff(self):
			"""A ToDo status change; the record's name is replaced by the fixture's TD-0001."""
			todo = frappe.get_doc(
				{"doctype": "ToDo", "description": "Call the supplier", "status": "Open"}
			).insert()
			args = {"doctype": "ToDo", "name": todo.name, "changes": {"status": "Closed"}}
			card = _park_card("update_doc", args, {"status": "Closed"}, True)
			card = json.loads(json.dumps(card, default=str).replace(todo.name, "TD-0001"))
			self._same("update-diff", card)

	class TestModelResultOnSite(FrappeTestCase):
		"""What `api._run_tool` returns to the model for a gated write."""

		def setUp(self):
			super().setUp()
			self.owner = frappe.session.user
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			self.conv = (
				frappe.get_doc({"doctype": "Jarvis Conversation", "title": "draft result test"})
				.insert(ignore_permissions=True)
				.name
			)
			self.addCleanup(self._cleanup)

		def _cleanup(self):
			for rec in pending_confirm.list_for_owner(self.owner):
				try:
					pending_confirm.consume(
						rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
					)
				except Exception:
					pass
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE conversation=%(c)s", {"c": self.conv})
			frappe.db.delete(MSG, {"conversation": self.conv})
			frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
			# These tests commit mid-test (the park and the confirm do), so the class rollback keeps
			# the committed rows: commit the deletes too, or the Executed row outlives the class.
			frappe.db.commit()

		def _run(self, flag, tool, args):
			with patch.dict(frappe.conf, {FLAG: flag}):
				return api._run_tool(tool, json.loads(json.dumps(args)), conversation=self.conv)

		def test_flag_on_returns_the_short_result_with_ignored_fields(self):
			"""A real parked create returns the short envelope (no `preview`, under the bound)
			and names the key the form does not have."""
			res = self._run(
				1, "create_doc", {"doctype": "ToDo", "values": {"description": "short-res", "bogus": 1}}
			)
			data = res["data"]
			self.assertEqual(data["status"], "pending_confirmation")
			self.assertNotIn("preview", data)
			self.assertEqual(data["draft"], {"verb": "create", "doctype": "ToDo", "name": ""})
			self.assertEqual(data["ignored_fields"], ["bogus"])
			self.assertLessEqual(len(json.dumps(data)), draft_card.SHORT_LIMIT)

		def test_flag_off_is_todays_behaviour(self):
			"""With the flag off the model still gets the whole trial under `preview` (and no
			card), exactly the keys it got before the short result existed."""
			res = self._run(0, "create_doc", {"doctype": "ToDo", "values": {"description": "old-res"}})
			self.assertEqual(set(res), {"ok", "data"})
			self.assertEqual(set(res["data"]), {"status", "preview", "tool"})
			self.assertEqual(res["data"]["tool"], "create_doc")
			self.assertEqual(res["data"]["preview"]["would"]["description"], "old-res")
			self.assertNotIn("card", res["data"]["preview"])

		def test_batch_create_is_not_shortened_with_the_flag_on(self):
			"""A batch `create_doc(docs=...)` is not a draft call: with the flag on it still
			returns the full `preview`, so only single-record drafts change shape."""
			docs = [{"doctype": "ToDo", "values": {"description": "batch-a"}}]
			res = self._run(1, "create_doc", {"docs": docs})
			self.assertEqual(res["data"]["status"], "pending_confirmation", res)
			self.assertIn("preview", res["data"])
			self.assertNotIn("draft", res["data"])

		def test_a_missing_mandatory_value_names_the_field_only_for_a_draft(self):
			"""A ToDo create without its `description` is refused with `error.fields` naming it
			when the flag is on; with the flag off the same refusal keeps today's shape."""
			args = {"doctype": "ToDo", "values": {"priority": "High"}}
			on = self._run(1, "create_doc", args)
			self.assertFalse(on["ok"])
			self.assertEqual(on["error"]["code"], "InvalidArgumentError")
			self.assertEqual(
				on["error"]["fields"],
				[{"fieldname": "description", "label": "Description", "where": "main"}],
			)
			off = self._run(0, "create_doc", args)
			self.assertFalse(off["ok"])
			self.assertNotIn("fields", off["error"])

		def test_another_refusal_keeps_its_shape_with_the_flag_on(self):
			"""A refusal that is not a missing value (a field value the DocType rejects) has no
			`fields`: only MandatoryError turns into named fields."""
			args = {"doctype": "ToDo", "values": {"description": "x", "status": "Not A Status"}}
			res = self._run(1, "create_doc", args)
			self.assertFalse(res["ok"])
			self.assertNotIn("fields", res["error"])

	from jarvis.chat import actions_api, settlement, turn_message_binding
	from jarvis.chat import api as chat_api
	from jarvis.tests._pending_action_helpers import (
		MSG,
		OTHER,
		OWNER,
		PendingActionTestMixin,
		as_user,
		fake_dispatch,
	)
	from jarvis.tests._transport_helpers import provision_legacy_site
	from jarvis.tests.test_pending_confirm import _purge_legacy

	_TODO_ARGS = {"doctype": "ToDo", "values": {"description": "pa-test draft source"}}
	_PLAIN_PREVIEW = {"would": {"doctype": "ToDo"}, "card": {"title": "Create a ToDo", "fields": []}}

	class TestEverySourceCarriesTheDraft(PendingActionTestMixin, FrappeTestCase):
		"""A parked draft card read back through each place the apps read a card from.

		History (`get_conversation`) rebuilds the row's card from the sealed record;
		`list_pending_confirmations`, the `action:pending` push and the `run:end` list all
		go through `pending_confirm.list_items_for_owner`, which hands the stored preview
		(with its `card`) over whole. These tests pin that none of them strips `draft`."""

		def setUp(self):
			super().setUp()
			provision_legacy_site(self)
			_purge_legacy(OWNER)
			self.addCleanup(_purge_legacy, OWNER)
			turns = patch("jarvis.chat.api._dispatch_turn", return_value=None)
			turns.start()
			self.addCleanup(turns.stop)
			_record_continuations(self)
			self.bound = []
			self.addCleanup(self._unbind)

		def _unbind(self):
			for conv in self.bound:
				frappe.cache().delete_value(turn_message_binding._key(conv))

		def _mint(self, conv, draft=True):
			"""Park one ToDo create for OWNER, as `api._run_tool` does; returns (token, card)."""
			card = _park_card("create_doc", _TODO_ARGS, {"description": "x"}, draft)
			preview = {"would": {"description": "x"}, "card": card} if draft else dict(_PLAIN_PREVIEW)
			token = pending_confirm.mint(
				conversation=conv,
				owner=OWNER,
				tool="create_doc",
				args=json.loads(json.dumps(_TODO_ARGS)),
				run_id="",
				preview=preview,
			)
			return token, json.loads(json.dumps(preview["card"], default=str))

		def _history_row(self, conv, token, user=OWNER):
			with as_user(user):
				body = chat_api.get_conversation(conv)
			[row] = [m for m in body["messages"] if m.get("tool_call_id") == token]
			return row

		def _on(self):
			patcher = patch.dict(frappe.conf, {FLAG: 1})
			patcher.start()
			self.addCleanup(patcher.stop)

		def test_all_four_sources_hand_over_the_same_draft_and_call_it_safe(self):
			"""If any one source rebuilt or whitelisted the card, the apps would draw a bare
			card for a draft on that path only (a reload, a resync or the run end)."""
			self._on()
			conv = self.make_conv()
			token, card = self._mint(conv)
			row = self._history_row(conv, token)
			with as_user(OWNER):
				resynced = actions_api.list_pending_confirmations(conversation=conv)["data"]["pending"]
				pushed = pending_confirm.item_for_owner_token(token, OWNER, conv, strict=True)
				terminal = settlement._extra_with_pending({}, OWNER, conv)["pending"]
			self.assertEqual(row["pending_card"]["draft"], card["draft"])
			self.assertIs(row["pending_card_safe"], True)
			self.assertIs(row["draft_card"], True)
			for name, items in (("resync", resynced), ("push", [pushed]), ("run:end", terminal)):
				with self.subTest(source=name):
					[item] = items
					self.assertEqual(item["preview"]["card"]["draft"], card["draft"])
					self.assertIs(item["pending_card_safe"], True)

		def test_an_unsafe_draft_card_is_redacted_whole(self):
			"""The redaction path must not let a fenced-off draft through `draft_card`: no card,
			not safe, and the row is not a draft."""
			self._on()
			conv = self.make_conv()
			token, _card = self._mint(conv)
			with patch.object(pending_confirm, "pending_action_is_safe_to_render", return_value=False):
				row = self._history_row(conv, token)
			self.assertIsNone(row["pending_card"])
			self.assertIs(row["pending_card_safe"], False)
			self.assertIs(row["draft_card"], False)

		def test_flag_off_is_todays_behaviour(self):
			"""With the flag off a tool row has no `draft_card` key, so an ordinary card's row
			has the keys it had; with it on the only new key is `draft_card`, False for a card
			that is not a draft."""
			conv = self.make_conv()
			token, _card = self._mint(conv, draft=False)
			with patch.dict(frappe.conf, {FLAG: 0}):
				off = self._history_row(conv, token)
			with patch.dict(frappe.conf, {FLAG: 1}):
				on = self._history_row(conv, token)
			self.assertNotIn("draft_card", off)
			self.assertEqual(set(on) - set(off), {"draft_card"})
			self.assertIs(on["draft_card"], False)
			self.assertNotIn("draft", on["pending_card"])

		def test_flag_off_still_marks_a_leftover_draft_card(self):
			"""A draft card parked while the flag was on and still Pending after it was turned off
			must keep reading `draft_card` true, or a typed draft would show beside it; a plain
			card's row adds no key."""
			conv = self.make_conv()
			token, _card = self._mint(conv)
			other = self.make_conv()  # one waiting card per chat
			plain, _plain = self._mint(other, draft=False)
			with patch.dict(frappe.conf, {FLAG: 0}):
				self.assertIs(self._history_row(conv, token)["draft_card"], True)
				self.assertNotIn("draft_card", self._history_row(other, plain))

		def _decided_row(self, conv, token):
			"""The row as stored, and as the app reads it, after the card was decided."""
			stored = frappe.db.get_value(
				MSG,
				{"conversation": conv, "tool_call_id": token},
				["pending_card", "action_outcome"],
				as_dict=True,
			)
			return stored, self._history_row(conv, token)

		def _assert_decided(self, conv, token, outcome):
			stored, row = self._decided_row(conv, token)
			self.assertFalse(stored.pending_card, "the decided row keeps no card")
			self.assertEqual(stored.action_outcome, outcome)
			self.assertEqual(row["action_outcome"], outcome)
			self.assertIs(row["draft_card"], False)

		def test_confirming_a_draft_clears_the_card_and_sets_the_outcome(self):
			"""The live-draft rule retires an older typed draft on a non-empty `action_outcome`;
			a confirm that cleared the card without one would leave two drafts on screen."""
			self._on()
			conv = self.make_conv()
			token, _card = self._mint(conv)
			with fake_dispatch(), as_user(OWNER):
				actions_api.confirm_tool(token, conversation=conv)
			self._assert_decided(conv, token, "confirmed")

		def test_discarding_a_draft_clears_the_card_and_sets_the_outcome(self):
			"""Same invariant as confirm, for the Discard button."""
			self._on()
			conv = self.make_conv()
			token, _card = self._mint(conv)
			with fake_dispatch(), as_user(OWNER):
				actions_api.dismiss_tool(token, conversation=conv)
			self._assert_decided(conv, token, "discarded")

		def test_superseding_a_draft_clears_the_card_and_sets_the_outcome(self):
			"""A newer proposal after the user spoke retires the draft card in park's own
			transaction (`_flip_superseded_chip`); the chip must carry `superseded`."""
			self._on()
			conv = self.make_conv()
			old, _card = self._mint(conv)
			trigger = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": conv,
					"seq": chat_api._next_seq(conv),
					"role": "user",
					"content": "make it another one",
					"origin": "human",
				}
			)
			trigger.flags.jarvis_server_write = True
			trigger.insert(ignore_permissions=True)
			frappe.db.commit()
			turn_message_binding.bind_turn_message(conv, trigger.name)
			self.bound.append(conv)
			new, _card = self._mint(conv)
			self.assertTrue(new)
			self._assert_decided(conv, old, "superseded")

		def test_a_stranger_gets_no_card_from_any_source_that_takes_a_conversation(self):
			"""Kept beside the sources test on purpose: reading as a different user must
			return no draft through history, resync, the push or the run-end list."""
			self._on()
			conv = self.make_conv()
			token, _card = self._mint(conv)
			with as_user(OTHER):
				with self.assertRaises(frappe.PermissionError):
					chat_api.get_conversation(conv)
				resynced = actions_api.list_pending_confirmations(conversation=conv)["data"]["pending"]
				pushed = pending_confirm.item_for_owner_token(token, OTHER, conv, strict=True)
				terminal = settlement._extra_with_pending({}, OTHER, conv)
			self.assertEqual(resynced, [])
			self.assertIsNone(pushed)
			self.assertNotIn("pending", terminal)

	from jarvis.chat import agent_notes

	class _ContinuationRecorder:
		"""Stands in for `actions_api.enqueue_continuation`: records the call and queues
		nothing, so no turn, relay target or hidden user row outlives the test."""

		def __init__(self):
			self.calls = []

		def __call__(self, conversation, receipt, **kwargs):
			self.calls.append((conversation, receipt, kwargs))
			return {}

	def _record_continuations(case):
		rec = _ContinuationRecorder()
		patcher = patch.object(actions_api, "enqueue_continuation", rec)
		patcher.start()
		case.addCleanup(patcher.stop)
		return rec

	_NOTE_PREFIX = "the user confirmed the draft and it was saved: "

	def _notes(conv) -> list[str]:
		return [
			e["text"]
			for e in agent_notes._parse(
				frappe.db.get_value("Jarvis Conversation", conv, "pending_agent_notes")
			)
		]

	class TestConfirmingAFinalDraftCostsNoTurn(PendingActionTestMixin, FrappeTestCase):
		"""A confirmed draft card settles through `actions_api.on_chat_settled`: a FINAL draft
		queues an agent note and no turn; every other settle keeps today's continuation."""

		def setUp(self):
			super().setUp()
			provision_legacy_site(self)
			self.cont = _record_continuations(self)

		def _park_draft(self, conv, args=None, flag=1):
			args = args or json.loads(json.dumps(_TODO_ARGS))
			with patch.dict(frappe.conf, {FLAG: flag}):
				card = _park_card("create_doc", args, {"description": "x"}, bool(flag))
			return self.park(
				conv, tool="create_doc", args=args, card=card, preview={"would": {}, "card": card}
			)

		def _confirm(self, token, conv, dispatch=None):
			with fake_dispatch(dispatch), as_user(OWNER):
				res = actions_api.confirm_tool(token, conversation=conv)
			return res, self.cont.calls

		def test_final_draft_confirm_starts_no_turn_and_queues_one_note(self):
			"""Without this a saved final draft still wakes the model for a turn nobody needs:
			no hidden user row, no continuation, exactly one note with the fixed text, and the
			receipt chip still written so the card is replaced by a receipt."""
			conv = self.make_conv()
			token = self._park_draft(conv)
			res, cont = self._confirm(token, conv)
			self.assertTrue(res["ok"], res)
			self.assertEqual(cont, [])
			[note] = _notes(conv)
			self.assertTrue(note.startswith(_NOTE_PREFIX), note)
			self.assertTrue(note[len(_NOTE_PREFIX) :].strip(), "the note carries the receipt text")
			chips = frappe.get_all(
				MSG, filters={"conversation": conv, "role": "tool"}, pluck="action_outcome"
			)
			self.assertEqual(chips, ["confirmed"])
			self.assertEqual(frappe.db.get_value(PA, token, "settled"), 1)

		def test_continue_true_draft_confirm_enqueues_one_continuation_and_no_note(self):
			"""A non-final step (`continue: true`) must still wake the agent for the next step;
			if it were treated as final the multi-step plan would stall after step one."""
			conv = self.make_conv()
			args = {**json.loads(json.dumps(_TODO_ARGS)), "continue": True}
			token = self._park_draft(conv, args)
			res, cont = self._confirm(token, conv)
			self.assertTrue(res["ok"], res)
			self.assertEqual(len(cont), 1)
			self.assertEqual(_notes(conv), [])

		def test_a_draft_whose_write_fails_still_gets_the_failure_continuation(self):
			"""A draft that did not save must tell the agent so (the failure scaffold). Treating
			any settled draft as final would leave the model believing it was saved."""

			def fail(tool, args):
				raise frappe.ValidationError("nope")

			conv = self.make_conv()
			token = self._park_draft(conv)
			res, cont = self._confirm(token, conv, fail)
			self.assertFalse(res["ok"], res)
			self.assertEqual(len(cont), 1)
			self.assertTrue(cont[0][2]["outcome"].startswith("failed"), cont)
			self.assertEqual(_notes(conv), [])

		def test_a_non_draft_submit_confirm_is_unchanged(self):
			"""A card with no `draft` (here a submit) keeps its continuation turn: the final-draft
			shortcut must not reach any other tool."""
			conv = self.make_conv()
			target = self.make_todo()  # a real, committed record: Confirm checks the target still exists
			token = self.park(conv, tool="submit_doc", args={"doctype": "ToDo", "name": target})
			res, cont = self._confirm(token, conv)
			self.assertTrue(res["ok"], res)
			self.assertEqual(len(cont), 1)
			self.assertEqual(_notes(conv), [])

		def test_flag_off_is_todays_behaviour(self):
			"""With the flag off a `create_doc` card has no draft, so its confirm enqueues the
			continuation exactly as before and queues no note."""
			conv = self.make_conv()
			token = self._park_draft(conv, flag=0)
			self.assertNotIn("draft", json.loads(frappe.db.get_value(PA, token, "card")))
			res, cont = self._confirm(token, conv)
			self.assertTrue(res["ok"], res)
			self.assertEqual(len(cont), 1)
			self.assertEqual(_notes(conv), [])

	class TestContinueNeverReachesTheWrite(FrappeTestCase):
		"""`continue` is a model-sent key, not a field of `create_doc`."""

		def setUp(self):
			super().setUp()
			self.owner = frappe.session.user
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			self.conv = (
				frappe.get_doc({"doctype": "Jarvis Conversation", "title": "draft continue test"})
				.insert(ignore_permissions=True)
				.name
			)
			self.cont = _record_continuations(self)
			self.addCleanup(self._cleanup)

		def _cleanup(self):
			frappe.db.delete("ToDo", {"description": "draft-continue-xyz"})
			frappe.db.delete(MSG, {"conversation": self.conv})
			frappe.db.delete("Jarvis Chat Turn", {"conversation": self.conv})
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE conversation=%(c)s", {"c": self.conv})
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
			frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
			# These tests commit mid-test (the park and the confirm do), so the class rollback keeps
			# the committed rows: commit the deletes too, or the Executed row outlives the class.
			frappe.db.commit()

		def test_continue_is_kept_off_the_record_the_card_values_and_the_stored_values(self):
			"""A `create_doc` call carrying `continue: true` must still create the record (the
			tool would raise on an unknown keyword) and `continue` must be a flag on the draft
			only, never a key of the card's values."""
			args = {"doctype": "ToDo", "values": {"description": "draft-continue-xyz"}, "continue": True}
			with patch.dict(frappe.conf, {FLAG: 1}):
				res = api._run_tool("create_doc", json.loads(json.dumps(args)), conversation=self.conv)
			self.assertEqual(res["data"]["status"], "pending_confirmation", res)
			[token] = frappe.get_all(PA, filters={"conversation": self.conv}, pluck="name")
			card = json.loads(frappe.db.get_value(PA, token, "card"))
			self.assertIs(card["draft"]["continue"], True)
			self.assertEqual(card["draft"]["values"], {"description": "draft-continue-xyz"})
			out = actions_api.confirm_tool(token, conversation=self.conv)
			self.assertTrue(out["ok"], out)
			self.assertTrue(frappe.db.exists("ToDo", {"description": "draft-continue-xyz"}))
			self.assertEqual(len(self.cont.calls), 1)  # continue: true wakes the agent
