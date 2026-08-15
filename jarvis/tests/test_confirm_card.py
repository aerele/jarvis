"""Tests for jarvis.chat.confirm_card.build_card - the render-ready "what will
change" summary attached to a confirmation card at park (F9).

The builder shapes tool + args + the park-time preview into a structured card the
SPA renders; it never raises (a card is UX, not correctness) and returns None for
shapes it does not cover, so the SPA falls back to the raw preview.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.confirm_card import _MAX_ROWS, build_card


class TestCreateCard(FrappeTestCase):
	def test_create_lists_set_fields_from_would(self):
		would = {"name": "TODO-NEW-1", "description": "buy milk", "priority": "High"}
		card = build_card(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": "buy milk", "priority": "High"}},
			{"preview": True, "would": would},
		)
		self.assertEqual(card["kind"], "create")
		self.assertEqual(card["doctype"], "ToDo")
		self.assertEqual(card["name"], "TODO-NEW-1")
		labels = {r["value"] for r in card["rows"]}
		self.assertIn("buy milk", labels)
		self.assertIn("High", labels)

	def test_create_hides_fields_absent_from_permfiltered_would(self):
		# A field the model set but that is NOT in the perm-filtered ``would`` is
		# dropped (never leak a restricted field's value).
		card = build_card(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": "x", "secret_field": "SEE"}},
			{"would": {"name": "T-1", "description": "x"}},
		)
		vals = {r["value"] for r in card["rows"]}
		self.assertIn("x", vals)
		self.assertNotIn("SEE", vals)


class TestUpdateCard(FrappeTestCase):
	def test_update_shows_from_to_diff(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "old desc",
				"priority": "Medium",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("ToDo", todo.name, force=True, ignore_permissions=True))
		would = {"description": "new desc", "priority": "Low"}
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"description": "new desc", "priority": "Low"}},
			{"would": would},
		)
		self.assertEqual(card["kind"], "update")
		self.assertEqual(card["name"], todo.name)
		by_from = {d["from"]: d["to"] for d in card["diff"]}
		# OLD from the current doc, NEW from ``would``.
		self.assertEqual(by_from.get("old desc"), "new desc")
		self.assertEqual(by_from.get("Medium"), "Low")

	def test_update_skips_unchanged_and_permhidden(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "same",
				"priority": "Medium",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("ToDo", todo.name, force=True, ignore_permissions=True))
		would = {"description": "same"}  # no-op change; priority not in would
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"description": "same", "priority": "High"}},
			{"would": would},
		)
		# description is a no-op (from == to) and priority is not perm-visible.
		self.assertEqual(card["diff"], [])


class TestVerbCard(FrappeTestCase):
	def test_single_verb(self):
		card = build_card("submit_doc", {"doctype": "Sales Order", "name": "SO-1"}, {"would": {}})
		self.assertEqual(card["kind"], "verb")
		self.assertEqual(card["verb"], "submit")
		self.assertEqual(card["doctype"], "Sales Order")
		self.assertEqual(card["targets"], ["SO-1"])
		self.assertEqual(card["count"], 1)

	def test_bulk_verb_lists_targets(self):
		names = [f"SO-{i}" for i in range(3)]
		card = build_card("cancel_doc", {"doctype": "Sales Order", "names": names}, {})
		self.assertEqual(card["verb"], "cancel")
		self.assertEqual(card["count"], 3)
		self.assertEqual(card["targets"], names)

	def test_workflow_action_carries_action(self):
		card = build_card(
			"apply_workflow_action", {"doctype": "Sales Order", "name": "SO-1", "action": "Approve"}, {}
		)
		self.assertEqual(card["verb"], "apply")
		self.assertEqual(card["action"], "Approve")


class TestEmailAndMethodCards(FrappeTestCase):
	def test_email_shows_to_subject_body(self):
		card = build_card(
			"send_email", {"recipients": "a@x.test", "subject": "Hi", "content": "the body"}, {}
		)
		self.assertEqual(card["kind"], "email")
		self.assertEqual(card["to"], "a@x.test")
		self.assertEqual(card["subject"], "Hi")
		self.assertEqual(card["body"], "the body")

	def test_method_masks_secret_arg_keys(self):
		card = build_card(
			"run_method",
			{"method": "some.method", "args": {"doc": "X", "api_key": "SECRET", "password": "p"}},
			{},
		)
		self.assertEqual(card["kind"], "method")
		self.assertEqual(card["method"], "some.method")
		self.assertEqual(card["args"]["doc"], "X")
		self.assertEqual(card["args"]["api_key"], "[hidden]")
		self.assertEqual(card["args"]["password"], "[hidden]")


class TestBulkUpdateCard(FrappeTestCase):
	"""A batch ``update_doc(updates=[{name, changes}, ...])`` renders a per-record
	from->to card (kind ``bulk_update``) instead of the raw-JSON dump."""

	def _todo(self, **vals):
		vals.setdefault("description", "task")
		doc = frappe.get_doc({"doctype": "ToDo", **vals}).insert(ignore_permissions=True)
		self.addCleanup(lambda n=doc.name: frappe.delete_doc("ToDo", n, force=True, ignore_permissions=True))
		return doc

	def test_bulk_update_lists_per_record_from_to(self):
		t1 = self._todo(description="d1", priority="Medium")
		t2 = self._todo(description="d2", priority="Low")
		args = {
			"doctype": "ToDo",
			"updates": [
				{"name": t1.name, "changes": {"priority": "High"}},
				{"name": t2.name, "changes": {"priority": "High"}},
			],
		}
		would = {"count": 2, "doctype": "ToDo", "updated": [t1.name, t2.name]}
		card = build_card("update_doc", args, {"preview": True, "would": would})
		self.assertEqual(card["kind"], "bulk_update")
		self.assertEqual(card["doctype"], "ToDo")
		self.assertEqual(card["count"], 2)
		self.assertEqual(len(card["records"]), 2)
		self.assertEqual(card["records"][0]["name"], t1.name)
		# OLD from the current doc (Medium), NEW from the requested changes (High).
		self.assertTrue(any(d["from"] == "Medium" and d["to"] == "High" for d in card["records"][0]["diff"]))
		# each record surfaces the changed field labels for the collapsed summary
		self.assertTrue(card["records"][0]["fields"])

	def test_bulk_update_skips_noop_rows(self):
		t1 = self._todo(priority="High")
		args = {
			"doctype": "ToDo",
			"updates": [
				{"name": t1.name, "changes": {"priority": "High"}},  # already High -> no-op
			],
		}
		card = build_card("update_doc", args, {"would": {}})
		self.assertEqual(card["kind"], "bulk_update")
		self.assertEqual(card["records"][0]["diff"], [])

	def test_bulk_update_normalizes_typed_noop(self):
		# A field set to its CURRENT value - typed in the DB (a Date), a string in
		# the request - must not render a spurious "2026-07-20 -> 2026-07-20" row.
		t1 = self._todo(date="2026-07-20")
		args = {
			"doctype": "ToDo",
			"updates": [
				{"name": t1.name, "changes": {"date": "2026-07-20"}},
			],
		}
		card = build_card("update_doc", args, {"would": {}})
		self.assertEqual(card["records"][0]["diff"], [])

	def test_bulk_update_flags_varying_changes(self):
		t1 = self._todo()
		t2 = self._todo()
		args = {
			"doctype": "ToDo",
			"updates": [
				{"name": t1.name, "changes": {"priority": "High"}},
				{"name": t2.name, "changes": {"description": "changed"}},
			],
		}
		card = build_card("update_doc", args, {"would": {}})
		self.assertTrue(card["varying"])

	def test_bulk_update_homogeneous_not_varying(self):
		t1 = self._todo()
		t2 = self._todo()
		args = {
			"doctype": "ToDo",
			"updates": [
				{"name": t1.name, "changes": {"priority": "High"}},
				{"name": t2.name, "changes": {"priority": "High"}},
			],
		}
		card = build_card("update_doc", args, {"would": {}})
		self.assertFalse(card["varying"])

	def test_bulk_update_caps_records_and_reports_extra(self):
		todos = [self._todo() for _ in range(_MAX_ROWS + 2)]
		args = {
			"doctype": "ToDo",
			"updates": [{"name": t.name, "changes": {"priority": "High"}} for t in todos],
		}
		card = build_card("update_doc", args, {"would": {}})
		self.assertEqual(card["count"], _MAX_ROWS + 2)
		self.assertEqual(len(card["records"]), _MAX_ROWS)
		self.assertEqual(card["extra"], 2)


class TestBatchAndFallback(FrappeTestCase):
	def test_batch_create_from_would_created(self):
		would = {
			"created": [{"doctype": "ToDo", "name": "T-1"}, {"doctype": "ToDo", "name": "T-2"}],
			"notes": ["reused Supplier X"],
		}
		card = build_card("create_doc", {"docs": [{"doctype": "ToDo", "values": {}}]}, {"would": would})
		self.assertEqual(card["kind"], "batch_create")
		self.assertEqual(card["count"], 2)
		self.assertEqual([r["name"] for r in card["rows"]], ["T-1", "T-2"])

	def test_uncovered_tool_returns_none(self):
		# Phase 4 gave every GATED write a card, so share_doc/assign_to - this test's
		# old examples - render real cards now. The invariant it exists for is the
		# dispatch FALLTHROUGH, not those two tools: a shape build_card does not cover
		# returns None and the SPA falls back to the summary + raw preview. Re-pointed
		# at tools that have no card rather than deleted, so the fallback stays tested
		# no matter how many shapes later phases cover.
		self.assertIsNone(build_card("get_doc", {"doctype": "ToDo", "name": "T-1"}, {}))
		self.assertIsNone(build_card("no_such_tool", {"doctype": "ToDo", "name": "T-1"}, {}))

	def test_never_raises_on_bad_input(self):
		self.assertIsNone(build_card("update_doc", None, None))
		self.assertIsNone(build_card("create_doc", "not-a-dict", {}))

	def test_long_value_truncated(self):
		would = {"name": "T-1", "description": "z" * 500}
		card = build_card(
			"create_doc", {"doctype": "ToDo", "values": {"description": "z" * 500}}, {"would": would}
		)
		shown = card["rows"][0]["value"]
		self.assertLessEqual(len(shown), 200)
		self.assertTrue(shown.endswith("…"))


from unittest.mock import patch


class TestPhase1Rewire(FrappeTestCase):
	def test_update_card_masks_a_secret_named_field(self):
		# The single-update card had no is_secret call while the BULK card masked by
		# key name - so a Data field named api_token rendered plaintext on one card
		# and [hidden] on the other for the identical change.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert(ignore_permissions=True)
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"api_token": "sk-live-999"}},
			{"would": {"api_token": "sk-live-999"}},
		)
		self.assertNotIn("sk-live-999", str(card))
		self.assertTrue(all(d["to"] == "[hidden]" for d in card["diff"]))

	def test_update_card_renders_a_real_change(self):
		# Baseline shape check. NOT a no-op test - a text change passes against the
		# old raw comparison too. The cast-compare logic is covered at the unit level
		# in test_record_summary (TestSameValue) and at card level by the existing
		# test_bulk_update_normalizes_typed_noop.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert(ignore_permissions=True)
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"description": "y"}},
			{"would": {"description": "y"}},
		)
		self.assertEqual(card["kind"], "update")
		self.assertTrue(any(d["to"] == "y" for d in card["diff"]))

	def test_update_card_drops_an_unchanged_field(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert(ignore_permissions=True)
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"description": "x"}},
			{"would": {"description": "x"}},
		)
		self.assertEqual(card["diff"], [])

	def test_update_card_carries_the_record_title(self):
		# ToDo's title_field is `description`.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "probe title"}).insert(
			ignore_permissions=True
		)
		card = build_card(
			"update_doc",
			{"doctype": "ToDo", "name": todo.name, "changes": {"priority": "High"}},
			{"would": {"priority": "High"}},
		)
		self.assertIn("probe title", card["title"])

	def test_update_card_checks_read_permission(self):
		# _update_card:143 has the same unchecked get_doc as _bulk_update_card - the
		# spec and both review rounds named only the bulk one. Without this, a user
		# who cannot read the record sees its old values on a single-update card.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "secret"}).insert(ignore_permissions=True)
		with patch("frappe.model.document.Document.has_permission", return_value=False):
			card = build_card(
				"update_doc",
				{"doctype": "ToDo", "name": todo.name, "changes": {"description": "y"}},
				{"would": {"description": "y"}},
			)
		# The old value must not appear: the read failed, so `from` is empty.
		self.assertNotIn("secret", str(card))
		self.assertEqual(card["title"], "")

	def test_bulk_update_card_checks_read_permission_on_each_doc(self):
		# The pre-existing hole: get_doc checks nothing, and
		# apply_fieldlevel_read_permissions is permlevel-only, so a user who cannot
		# read a record could see its OLD values on an update card.
		#
		# The degrade is deliberate: the record still lists the caller's own proposed
		# changes (which they authored, so they are not a leak) with an EMPTY `from`,
		# and no title. Asserting "no diff rows" would be wrong - it contradicts the
		# implementation, which cannot skip rows it never loaded.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "secret-old"}).insert(
			ignore_permissions=True
		)
		with patch("frappe.model.document.Document.has_permission", return_value=False):
			card = build_card(
				"update_doc",
				{"doctype": "ToDo", "updates": [{"name": todo.name, "changes": {"description": "y"}}]},
				{},
			)
		self.assertNotIn("secret-old", str(card))  # the old value must not leak
		self.assertTrue(all(d["from"] == "" for r in card["records"] for d in r["diff"]))
		self.assertEqual(card["records"][0]["title"], "")

	def test_create_card_renders_child_tables(self):
		card = build_card(
			"create_doc",
			{
				"doctype": "Sales Invoice",
				"values": {
					"customer": "X",
					"items": [{"item_code": "I-1", "qty": 2}],
				},
			},
			{"would": {"name": "SI-1", "customer": "X", "items": [{"item_code": "I-1", "qty": 2}]}},
		)
		self.assertTrue(card.get("tables"))
		self.assertEqual(card["tables"][0]["count"], 1)


class TestVerbCardRecords(FrappeTestCase):
	def setUp(self):
		self.todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "verb card probe",
				"priority": "High",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.rollback)  # match the sibling classes' convention

	def test_single_delete_carries_a_record_summary(self):
		# THE motivating case: "Will delete this ToDo <name>" told you the ID and
		# nothing about the record.
		card = build_card("delete_doc", {"doctype": "ToDo", "name": self.todo.name}, {})
		self.assertEqual(card["kind"], "verb")
		self.assertEqual(len(card["records"]), 1)
		self.assertEqual(card["records"][0]["name"], self.todo.name)
		self.assertIn("verb card probe", card["records"][0]["title"])
		self.assertIn("High", {r["value"] for r in card["records"][0]["rows"]})

	def test_targets_and_count_are_unchanged(self):
		# Additive only: three existing tests assert these, and a client running the
		# previous bundle still reads `targets`.
		card = build_card("delete_doc", {"doctype": "ToDo", "name": self.todo.name}, {})
		self.assertEqual(card["targets"], [self.todo.name])
		self.assertEqual(card["count"], 1)

	def test_bulk_verb_carries_a_record_per_target(self):
		other = frappe.get_doc({"doctype": "ToDo", "description": "second"}).insert(ignore_permissions=True)
		card = build_card("cancel_doc", {"doctype": "ToDo", "names": [self.todo.name, other.name]}, {})
		self.assertEqual(card["count"], 2)
		self.assertEqual([r["name"] for r in card["records"]], [self.todo.name, other.name])

	def test_unreadable_target_degrades_to_name_only(self):
		# summary_rows returns None for missing OR unreadable; the card must leak
		# neither the title (typically the party name) nor any field.
		with patch("frappe.model.document.Document.has_permission", return_value=False):
			card = build_card("delete_doc", {"doctype": "ToDo", "name": self.todo.name}, {})
		self.assertEqual(card["records"][0]["name"], self.todo.name)
		self.assertEqual(card["records"][0]["title"], "")
		self.assertEqual(card["records"][0]["rows"], [])
		self.assertNotIn("verb card probe", str(card))

	def test_missing_target_renders_identically_to_unreadable(self):
		# No existence oracle: the two must be indistinguishable on the card.
		card = build_card("delete_doc", {"doctype": "ToDo", "name": "no-such-todo"}, {})
		self.assertEqual(card["records"][0], {"name": "no-such-todo", "title": "", "rows": []})

	def test_records_are_capped_at_MAX_ROWS(self):
		names = [f"fake-{i}" for i in range(_MAX_ROWS + 5)]
		card = build_card("delete_doc", {"doctype": "ToDo", "names": names}, {})
		self.assertEqual(len(card["records"]), _MAX_ROWS)
		self.assertEqual(card["count"], _MAX_ROWS + 5)
		self.assertEqual(card["extra"], 5)

	def test_workflow_action_still_carries_action_and_gains_records(self):
		card = build_card(
			"apply_workflow_action", {"doctype": "ToDo", "name": self.todo.name, "action": "Approve"}, {}
		)
		self.assertEqual(card["action"], "Approve")
		self.assertEqual(card["records"][0]["name"], self.todo.name)


class TestBatchCreateContent(FrappeTestCase):
	def _args(self, docs, notes=None):
		out = {"docs": docs}
		if notes is not None:
			out["notes"] = notes
		return out

	def test_each_record_carries_its_proposed_values(self):
		# The card listed {doctype, name} and threw the values away - even though
		# they were sitting in args.docs[i].values.
		args = self._args(
			[
				{"doctype": "ToDo", "values": {"description": "first", "priority": "High"}},
				{"doctype": "ToDo", "values": {"description": "second", "priority": "Low"}},
			]
		)
		would = {"created": [{"doctype": "ToDo", "name": "T-1"}, {"doctype": "ToDo", "name": "T-2"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertEqual(card["kind"], "batch_create")
		self.assertEqual(len(card["records"]), 2)
		self.assertEqual(card["records"][0]["name"], "T-1")
		self.assertIn("first", {r["value"] for r in card["records"][0]["rows"]})
		self.assertIn("Low", {r["value"] for r in card["records"][1]["rows"]})

	def test_model_authored_notes_are_NOT_on_the_card(self):
		# THE trust-boundary fix. `notes` is a tool arg the model writes; on the card
		# it reads as system truth. A prompt-injected agent could caption its own
		# confirmation.
		args = self._args(
			[{"doctype": "ToDo", "values": {"description": "x"}}],
			notes=["these already exist - confirming changes nothing"],
		)
		would = {
			"created": [{"doctype": "ToDo", "name": "T-1"}],
			"notes": ["these already exist - confirming changes nothing"],
		}
		card = build_card("create_doc", args, {"would": would})
		self.assertNotIn("notes", card)
		self.assertNotIn("confirming changes nothing", str(card))

	def test_mixed_doctype_batch_uses_per_item_meta(self):
		# A batch can mix doctypes; one meta for the card would mislabel every field
		# of every other doctype.
		args = self._args(
			[
				{"doctype": "ToDo", "values": {"description": "a todo"}},
				{"doctype": "Note", "values": {"title": "a note"}},
			]
		)
		would = {"created": [{"doctype": "ToDo", "name": "T-1"}, {"doctype": "Note", "name": "N-1"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertEqual(card["records"][0]["doctype"], "ToDo")
		self.assertEqual(card["records"][1]["doctype"], "Note")
		# Assert the LABEL, not the value: values render identically under the wrong
		# meta (values_rows falls back to fieldnames), so a value assertion stays
		# green even with the meta hoisted out of the loop. Note.title's label is
		# "Title"; under a hoisted ToDo meta it would render as the raw "title".
		self.assertIn({"label": "Title", "value": "a note"}, card["records"][1]["rows"])

	def test_a_non_dict_in_created_does_not_desync_the_pairing(self):
		# Filter-then-index would pair args.docs[1] with created[2].
		args = self._args(
			[
				{"doctype": "ToDo", "values": {"description": "first"}},
				{"doctype": "ToDo", "values": {"description": "second"}},
			]
		)
		would = {"created": ["garbage", {"doctype": "ToDo", "name": "T-2"}]}
		card = build_card("create_doc", args, {"would": would})
		# Whatever we render, "second" must never be labelled T-2's sibling wrongly:
		# the surviving pair is args.docs[1] <-> created[1].
		named = [r for r in card["records"] if r["name"] == "T-2"]
		self.assertEqual(len(named), 1)
		self.assertIn("second", {r["value"] for r in named[0]["rows"]})

	def test_a_list_table_rows_rejects_still_renders_as_N_rows(self):
		# An unknown doctype means _meta -> None -> table_rows returns None. Without
		# the table_keys pattern the child rows vanish ENTIRELY and a human approves
		# a create never seeing its line items.
		args = self._args(
			[
				{
					"doctype": "NoSuchDoctype9",
					"values": {"customer": "X", "items": [{"item_code": "I-1"}, {"item_code": "I-2"}]},
				}
			]
		)
		would = {"created": [{"doctype": "NoSuchDoctype9", "name": "N-1"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertIn("2 rows", {r["value"] for r in card["records"][0]["rows"]})

	def test_tables_past_MAX_TABLES_degrade_to_N_rows(self):
		# The spec's caps section promises the overflow degrades to "N rows", not
		# that it disappears.
		#
		# Uses REAL Table fields on a real doctype. An earlier version passed fake
		# field names to ToDo: table_rows returned None for every one, so zero tables
		# ever rendered, `rendered <= _MAX_TABLES` passed as 0 <= 3, and the
		# _MAX_TABLES break was never reached - the test named a cap it did not
		# exercise.
		from jarvis.chat._record_summary import _MAX_TABLES

		real_tables = ["items", "taxes", "pricing_rules", "packed_items"]
		self.assertGreater(len(real_tables), _MAX_TABLES)  # or this proves nothing
		values = {"customer": "X"}
		for fieldname in real_tables:
			values[fieldname] = [{"idx": 1}]
		args = self._args([{"doctype": "Sales Invoice", "values": values}])
		would = {"created": [{"doctype": "Sales Invoice", "name": "SI-1"}]}
		card = build_card("create_doc", args, {"would": would})
		rendered = len(card["records"][0]["tables"])
		# the cap actually bit: fewer rendered than were proposed
		self.assertEqual(rendered, _MAX_TABLES)
		self.assertLess(rendered, len(real_tables))
		# and every table key past the cap still shows up as an "N rows" row
		degraded = [r for r in card["records"][0]["rows"] if r["value"].endswith("row")]
		self.assertEqual(len(degraded), len(real_tables) - _MAX_TABLES)

	def test_child_tables_render_per_record(self):
		args = self._args(
			[
				{
					"doctype": "Sales Invoice",
					"values": {"customer": "X", "items": [{"item_code": "I-1", "qty": 2}]},
				}
			]
		)
		would = {"created": [{"doctype": "Sales Invoice", "name": "SI-1"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertTrue(card["records"][0]["tables"])
		self.assertEqual(card["records"][0]["tables"][0]["count"], 1)

	def test_secret_values_are_masked(self):
		args = self._args([{"doctype": "ToDo", "values": {"description": "x", "api_token": "sk-live-1"}}])
		would = {"created": [{"doctype": "ToDo", "name": "T-1"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertNotIn("sk-live-1", str(card))

	def test_create_docs_shim_gets_the_same_card(self):
		# The deprecated shim delegates to create_doc's batch path; build_card
		# dispatched on tool == "create_doc" only, so an old plugin still advertising
		# jarvis__create_docs fell back to the raw rendering.
		args = self._args([{"doctype": "ToDo", "values": {"description": "x"}}])
		would = {"created": [{"doctype": "ToDo", "name": "T-1"}]}
		card = build_card("create_docs", args, {"would": would})
		self.assertEqual(card["kind"], "batch_create")

	def test_old_bundle_keys_are_kept(self):
		# `rows` ({doctype,name}) and `count`/`extra` are what a client running the
		# previous bundle reads. Additive only.
		args = self._args([{"doctype": "ToDo", "values": {"description": "x"}}])
		would = {"created": [{"doctype": "ToDo", "name": "T-1"}]}
		card = build_card("create_doc", args, {"would": would})
		self.assertEqual(card["rows"], [{"doctype": "ToDo", "name": "T-1"}])
		self.assertEqual(card["count"], 1)


class TestBulkEmailCard(FrappeTestCase):
	def _msgs(self, n=2):
		return [
			{
				"doctype": "Sales Invoice",
				"name": f"SI-{i}",
				"recipients": f"c{i}@x.test",
				"subject": f"Invoice SI-{i}",
				"content": f"Dear customer {i}, your invoice is attached.",
			}
			for i in range(n)
		]

	def test_bulk_email_renders_a_card_not_none(self):
		# It returned None: you confirmed 20 irreversible emails seeing only
		# "send_email count=20 targets=[SI-0001, ...]" - document names, no
		# recipients, no subject, no body.
		card = build_card("send_email", {"messages": self._msgs(2)}, {})
		self.assertIsNotNone(card)
		self.assertEqual(card["kind"], "bulk_email")
		self.assertEqual(card["count"], 2)

	def test_each_message_shows_recipient_subject_and_body(self):
		card = build_card("send_email", {"messages": self._msgs(2)}, {})
		m = card["messages"][0]
		self.assertEqual(m["recipients"], "c0@x.test")
		self.assertEqual(m["subject"], "Invoice SI-0")
		self.assertIn("Dear customer 0", m["body"])

	def test_recipient_list_is_joined(self):
		card = build_card(
			"send_email",
			{"messages": [{"recipients": ["a@x.test", "b@x.test"], "subject": "s", "content": "c"}]},
			{},
		)
		self.assertEqual(card["messages"][0]["recipients"], "a@x.test, b@x.test")

	def test_bodies_use_the_bulk_body_budget(self):
		from jarvis.chat._record_summary import _MAX_BULK_BODY

		card = build_card(
			"send_email",
			{"messages": [{"recipients": "a@x.test", "subject": "s", "content": "x" * 5000}]},
			{},
		)
		self.assertLessEqual(len(card["messages"][0]["body"]), _MAX_BULK_BODY)

	def test_messages_are_capped_with_a_remainder(self):
		card = build_card("send_email", {"messages": self._msgs(_MAX_ROWS + 3)}, {})
		self.assertEqual(len(card["messages"]), _MAX_ROWS)
		self.assertEqual(card["count"], _MAX_ROWS + 3)
		self.assertEqual(card["extra"], 3)

	def test_single_email_shows_cc_bcc_and_print_format(self):
		# None of these rendered before - you could not see who was copied.
		card = build_card(
			"send_email",
			{
				"recipients": "a@x.test",
				"subject": "Hi",
				"content": "body",
				"cc": ["c@x.test"],
				"bcc": "b@x.test",
				"print_format": "Standard",
			},
			{},
		)
		self.assertEqual(card["kind"], "email")
		self.assertEqual(card["cc"], "c@x.test")
		self.assertEqual(card["bcc"], "b@x.test")
		self.assertEqual(card["print_format"], "Standard")


class TestShareAndAssignCards(FrappeTestCase):
	def setUp(self):
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "share probe"}).insert(
			ignore_permissions=True
		)

	def test_share_shows_grantee_and_flags(self):
		card = build_card(
			"share_doc",
			{"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test", "read": True, "write": True},
			{},
		)
		self.assertEqual(card["kind"], "share")
		self.assertEqual(card["grantee"], "x@y.test")
		on = {f["label"] for f in card["flags"] if f["on"]}
		self.assertEqual(on, {"Read", "Write"})

	def test_share_everyone_is_distinguishable_from_one_user(self):
		# read-for-one and everyone+write+share rendered IDENTICALLY before.
		card = build_card(
			"share_doc",
			{
				"doctype": "ToDo",
				"name": self.todo.name,
				"everyone": True,
				"read": True,
				"write": True,
				"share": True,
			},
			{},
		)
		self.assertTrue(card["everyone"])
		self.assertEqual(card["grantee"], "Everyone")

	def test_share_flags_use_the_tools_own_coercion(self):
		# share_doc does int(bool(flag)) - and bool("false") is True, so the string
		# "false" GRANTS write. The card must agree with the grant, not the request.
		# NOTE `read` is absent here, so it defaults ON - assert the full set, not
		# just assertIn, or the test hides the default.
		card = build_card(
			"share_doc", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test", "write": "false"}, {}
		)
		on = {f["label"] for f in card["flags"] if f["on"]}
		self.assertEqual(on, {"Read", "Write"})

	def test_share_read_defaults_ON_when_absent(self):
		# THE trap. share_doc.py:38 defaults read=True, so a call that never mentions
		# `read` still grants it. A card built from args.get("read") renders "Write
		# only" over a read+write grant - the card lying, on the tool that gates
		# BECAUSE of grant escalation.
		card = build_card(
			"share_doc", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test", "write": True}, {}
		)
		on = {f["label"] for f in card["flags"] if f["on"]}
		self.assertEqual(on, {"Read", "Write"})

	def test_assign_explicit_null_notify_is_not_the_default(self):
		# Absent -> tool's default True -> mail sent. Explicit None -> int(bool(None))
		# -> 0 -> no mail (assign_to.py:84). .get() truthiness cannot tell them apart.
		card = build_card(
			"assign_to", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test", "notify": None}, {}
		)
		self.assertFalse(card["notify"])

	def test_share_carries_the_target_summary(self):
		card = build_card("share_doc", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test"}, {})
		self.assertIn("share probe", card["records"][0]["title"])

	def test_assign_shows_assignee_and_the_emailed_description(self):
		card = build_card(
			"assign_to",
			{
				"doctype": "ToDo",
				"name": self.todo.name,
				"user": "x@y.test",
				"description": "please review by friday",
				"priority": "High",
			},
			{},
		)
		self.assertEqual(card["kind"], "assign")
		self.assertEqual(card["assignee"], "x@y.test")
		self.assertIn("please review by friday", card["description"])
		self.assertEqual(card["priority"], "High")

	def test_assign_notify_defaults_to_the_effective_value(self):
		# assign_to's signature defaults notify=True; the card must show what the
		# tool will DO, not "unset".
		card = build_card("assign_to", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test"}, {})
		self.assertTrue(card["notify"])

	def test_bulk_share_lists_every_target(self):
		other = frappe.get_doc({"doctype": "ToDo", "description": "second"}).insert(ignore_permissions=True)
		card = build_card(
			"share_doc",
			{"doctype": "ToDo", "names": [self.todo.name, other.name], "user": "x@y.test", "read": True},
			{},
		)
		self.assertEqual(card["count"], 2)
		self.assertEqual(len(card["records"]), 2)

	def test_unreadable_share_target_degrades_to_name_only(self):
		with patch("frappe.model.document.Document.has_permission", return_value=False):
			card = build_card(
				"share_doc", {"doctype": "ToDo", "name": self.todo.name, "user": "x@y.test"}, {}
			)
		self.assertEqual(card["records"][0]["title"], "")
		self.assertNotIn("share probe", str(card))


class TestSkillAndWikiCards(FrappeTestCase):
	def test_skill_renders_the_instructions_body(self):
		# The card was the literal string "create_custom_skill". You approved
		# persistent agent instructions - the prompt-injection persistence vector -
		# seeing nothing at all.
		card = build_card(
			"create_custom_skill",
			{
				"skill_name": "Weekly report",
				"description": "d",
				"instructions": "Always include the pipeline table." * 50,
			},
			{},
		)
		self.assertEqual(card["kind"], "skill")
		self.assertEqual(card["skill_name"], "Weekly report")
		self.assertIn("Always include the pipeline table.", card["instructions"])

	def test_skill_instructions_use_the_long_form_budget(self):
		from jarvis.chat._record_summary import _MAX_BODY

		card = build_card(
			"create_custom_skill", {"skill_name": "s", "description": "d", "instructions": "x" * 20000}, {}
		)
		self.assertGreater(len(card["instructions"]), 200)  # not the scalar cap
		self.assertLessEqual(len(card["instructions"]), _MAX_BODY)

	def test_skill_scope_shows_the_effective_value_not_the_request(self):
		# create_custom_skill.py:44-57 computes `requested` then hardcodes
		# "scope": "User". Echoing args.scope would claim a bench-wide skill while
		# creating a private one.
		card = build_card(
			"create_custom_skill",
			{"skill_name": "s", "description": "d", "instructions": "i", "scope": "Org"},
			{},
		)
		self.assertEqual(card["scope"], "User (private)")

	def test_wiki_flags_a_full_replace(self):
		card = build_card("update_wiki", {"slug": "pricing", "replace_body_md": "brand new body"}, {})
		self.assertEqual(card["kind"], "wiki")
		self.assertEqual(card["slug"], "pricing")
		self.assertEqual(card["mode"], "replace")
		self.assertIn("brand new body", card["body"])

	def test_wiki_append_is_not_a_replace(self):
		card = build_card("update_wiki", {"slug": "pricing", "append_md": "one more line"}, {})
		self.assertEqual(card["mode"], "append")
		self.assertIn("one more line", card["body"])

	def test_wiki_empty_replace_is_flagged_as_a_replace_not_meta(self):
		# update_wiki.py:146 is `is not None`, so replace_body_md="" WIPES the page.
		# Truthiness would classify the erase as an innocuous metadata edit.
		card = build_card("update_wiki", {"slug": "pricing", "replace_body_md": ""}, {})
		self.assertEqual(card["mode"], "replace")

	def test_wiki_append_wins_over_an_empty_replace_exactly_as_the_tool_does(self):
		# The tool applies APPEND FIRST (update_wiki.py:143-146; new page :165), so
		# replace_body_md="" + append_md=X slips the falsy both-args guard (:96) and
		# APPENDS X. A card checking replace first renders an EMPTY ERASE over a call
		# that persists text the card never showed - a phantom action and a hidden
		# real one in one shape, reachable by a prompt-injected agent.
		card = build_card(
			"update_wiki", {"slug": "s", "replace_body_md": "", "append_md": "injected section"}, {}
		)
		self.assertEqual(card["mode"], "append")
		self.assertIn("injected section", card["body"])

	def test_wiki_renders_the_summary_it_persists(self):
		# summary is stored (update_wiki.py:141-142); a summary-only call would
		# otherwise be an empty "meta" card over text that gets written.
		card = build_card("update_wiki", {"slug": "pricing", "summary": "what changed"}, {})
		self.assertIn("what changed", card["summary"])


class TestDescribeCall(FrappeTestCase):
	def test_describe_call_surfaces_user_skill_and_slug(self):
		from jarvis.api import _describe_call

		self.assertIn(
			"x@y.test", _describe_call("share_doc", {"doctype": "ToDo", "name": "T-1", "user": "x@y.test"})
		)
		self.assertIn("Weekly report", _describe_call("create_custom_skill", {"skill_name": "Weekly report"}))
		self.assertIn("pricing", _describe_call("update_wiki", {"slug": "pricing"}))
