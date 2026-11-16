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


# Inputs of the fixture cards whose `draft` and `title` come from the builder.
FIXTURE_CALLS = {
	"create-simple": ("create", "", {"employee": "EMP-1", "note": "Weekly"}),
	"create-with-table": (
		"create",
		"",
		{"employee": "EMP-1", "time_logs": [{"doctype": "Timesheet Detail", "from_time": "09:00"}]},
	),
	"create-ignored-field": ("create", "", {"employee": "EMP-1", "colour": "red"}),
	"update-diff": ("update", "TS-0001", {"note": "Late"}),
	"update-table-rows": ("update", "TS-0001", {"time_logs": [{"from_time": "10:00", "hrs_worked": 2}]}),
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

	def test_update_payload_carries_the_changes_and_continue(self):
		out = _build("update", "TS-0001", {"note": "Late"}, cont=True)
		self.assertEqual((out["verb"], out["name"], out["continue"]), ("update", "TS-0001", True))
		self.assertEqual(draft_card.title_of("update", "Timesheet", "TS-0001"), "Timesheet TS-0001")
		self.assertEqual(draft_card.title_of("create", "Timesheet", ""), "New Timesheet")

	def test_fixture_drafts_equal_what_the_builder_produces(self):
		"""The fixture file the apps render cannot rot: its create-* and update-* `draft` and
		`title` are regenerated from the builder and compared."""
		with open(FIXTURES) as f:
			items = {item["id"]: item for item in json.load(f)}
		for key, (verb, name, values) in FIXTURE_CALLS.items():
			with self.subTest(key):
				card = items[key]["card"]
				self.assertEqual(items[key]["expect"], "draft")
				self.assertEqual(card["draft"], _build(verb, name, values))
				self.assertEqual(card["title"], draft_card.title_of(verb, "Timesheet", name))
		self.assertNotIn("draft", items["sensitive"]["card"])
		self.assertEqual(items["sensitive"]["card"]["risk"], "sensitive")
		self.assertIsInstance(items["unreadable"]["card"]["draft"]["values"], str)


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except ImportError:  # the pure class above still runs without a site
	FrappeTestCase = None

if FrappeTestCase is not None:
	from unittest.mock import patch

	from jarvis.chat import confirm_card

	def _park_card(tool, args, would, draft):
		"""The card the park builds, as ``api._run_tool`` does."""
		card = confirm_card.build_card(tool, args, {"would": would}, draft)
		if draft:
			draft_card.attach(card, tool, args, would)
		return card

	class TestDraftCardOnSite(FrappeTestCase):
		ROLE_ROW = {"doctype": "Has Role", "role": "System Manager"}

		def test_flag_off_is_todays_behaviour(self):
			"""With `jarvis_draft_cards` unset the park builds no draft: no `draft`, no
			`title`, and the trial's name stays on a create card, so the cards of the old
			persona's `create_doc` calls do not change."""
			frappe.conf.pop("jarvis_draft_cards", None)
			self.assertFalse(draft_card.enabled())
			args = {"doctype": "ToDo", "values": {"description": "x"}}
			self.assertFalse(draft_card.wanted("create_doc", args, must_card=False))
			card = _park_card("create_doc", args, {"name": "TD-trial", "description": "x"}, False)
			self.assertNotIn("draft", card)
			self.assertNotIn("title", card)
			self.assertEqual(card["name"], "TD-trial")

		def test_flag_on_enables_the_draft(self):
			"""The flag is a cint, so "1" and 1 both turn it on and "0" keeps it off."""
			for raw, want in ((1, True), ("1", True), ("0", False), (0, False)):
				with self.subTest(raw=raw), patch.dict(frappe.conf, {"jarvis_draft_cards": raw}):
					self.assertIs(draft_card.enabled(), want)

		def test_create_card_has_draft_title_and_no_trial_name(self):
			"""A draft create card names the record "New ToDo" and drops the name the trial
			allocated, which would show a number the save does not keep."""
			args = {"doctype": "ToDo", "values": {"description": "x", "bogus": 1}}
			card = _park_card("create_doc", args, {"name": "TD-trial", "description": "x"}, True)
			self.assertEqual(card["title"], "New ToDo")
			self.assertIsNone(card["name"])
			self.assertEqual(card["draft"]["values"], {"description": "x"})
			self.assertEqual(card["draft"]["ignored"], ["bogus"])

		def test_update_card_keeps_its_name(self):
			"""An update draft is titled by the record's own name."""
			args = {"doctype": "ToDo", "name": "TD-1", "changes": {"status": "Closed"}}
			card = _park_card("update_doc", args, {"status": "Closed"}, True)
			self.assertEqual((card["name"], card["title"]), ("TD-1", "ToDo TD-1"))
			self.assertEqual(card["draft"]["values"], {"status": "Closed"})

		def test_row_doctype_key_is_not_an_unrecognized_field(self):
			"""A child row that carries its own `doctype` is not counted as an unknown column
			(the spike card's false alarm), and a made-up key is named in `unknown_fields`."""
			meta = frappe.get_meta("User")
			from jarvis.chat._record_summary import table_rows

			out = table_rows(meta, "roles", [{**self.ROLE_ROW, "hrs_worked": 2}])
			self.assertEqual((out["unknown_columns"], out["unknown_fields"]), (1, ["hrs_worked"]))
			clean = table_rows(meta, "roles", [self.ROLE_ROW])
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
