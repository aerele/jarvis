"""Single settings must wait for approval and retain version/policy checks."""

import json
from unittest.mock import patch

import frappe

from jarvis.chat import approvals_api, held_sheet_board, held_sheet_seal, held_sheets, held_writes
from jarvis.chat.pending_actions import _park, _seal, _sheet, _store
from jarvis.tests._pending_action_helpers import as_user
from jarvis.tests.test_filebox_sheet_apply import _ApplyBase
from jarvis.tests.test_filebox_sheets import OTHER, OWNER
from jarvis.tools import _write_risk

DT = "Buying Settings"
FIELD = "maintain_same_rate"
DEFAULT_KEYS = ("supplier_group", "supp_master_name", FIELD, "buying_price_list")


class TestFileBoxSingleSettings(_ApplyBase):
	def setUp(self):
		super().setUp()
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		frappe.set_user("Administrator")
		self.saved_singles = frappe.db.sql(
			"SELECT doctype, field, value FROM `tabSingles` WHERE doctype=%s", DT, as_dict=True
		)
		self.saved_defaults = frappe.get_all(
			"DefaultValue",
			filters={"parent": "__default", "defkey": ["in", DEFAULT_KEYS]},
			fields=["*"],
			limit_page_length=0,
		)
		self.setter_filter = {
			"doc_type": "Supplier",
			"field_name": "naming_series",
			"property": ["in", ["hidden", "reqd"]],
		}
		self.saved_setters = frappe.get_all(
			"Property Setter", filters=self.setter_filter, fields=["*"], limit_page_length=0
		)
		self.roles = [r.role for r in frappe.get_doc("User", OWNER).roles]
		self.addCleanup(self.restore_settings)
		user = frappe.get_doc("User", OWNER)
		user.set(
			"roles",
			[
				{"role": r}
				for r in (
					"Jarvis User",
					"Purchase Manager",
					"Purchase Master Manager",
					"Purchase User",
					"Item Manager",
				)
			],
		)
		user.save(ignore_permissions=True)
		frappe.db.set_single_value(DT, {FIELD: 1, "supp_master_name": "Supplier Name"})
		frappe.db.set_default("supp_master_name", "Supplier Name")
		frappe.db.commit()

	def restore_settings(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete("Singles", {"doctype": DT})
		for row in self.saved_singles:
			frappe.db.sql(
				"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)",
				(row.doctype, row.field, row.value),
			)
		frappe.db.delete("DefaultValue", {"parent": "__default", "defkey": ["in", DEFAULT_KEYS]})
		for row in self.saved_defaults:
			frappe.get_doc({**row, "doctype": "DefaultValue"}).db_insert()
		frappe.db.delete("Property Setter", self.setter_filter)
		for row in self.saved_setters:
			frappe.get_doc({**row, "doctype": "Property Setter"}).db_insert()
		user = frappe.get_doc("User", OWNER)
		user.set("roles", [{"role": r} for r in self.roles])
		user.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()

	def args(self, **changes):
		return {"doctype": DT, "name": DT, "changes": changes or {FIELD: 0}}

	def value(self):
		return frappe.db.get_single_value(DT, FIELD, cache=False)

	def proposed(self):
		conv = self.conv()
		turn = self.turn(conv)
		self.assert_added(self.call("update_doc", self.args(), conv), 1)
		return conv, turn, self.sheet(conv)

	def sealed(self):
		conv, turn, row = self.proposed()
		self.set_turn(turn, state="done")
		held_sheet_seal.seal_turn(conv, turn)
		return conv, self.row(row.name)

	def test_collects_an_ordinary_single_without_writing_and_lists_settings(self):
		_, _, row = self.proposed()
		self.assertEqual(self.value(), 1)
		[record] = self.records(row)
		self.assertEqual((record["op"], record["name"], record["category"]), ("update", DT, "settings"))
		self.assertEqual(len(record["snapshot"]), 4)
		self.assertEqual(held_sheets._title(record), DT)
		self.assertIn("buying settings", held_sheet_board.counts_line({DT: 1}))

	def test_apply_is_explicit_and_replay_cannot_write_again(self):
		_, row = self.sealed()
		self.assertEqual(self.value(), 1)
		done = self.applied(row.name)
		self.assertEqual(done.status, "Executed")
		self.assertEqual(self.value(), 0)
		self.assertFalse(self.apply(row.name)["ok"])
		self.assertEqual(self.value(), 0)

	def test_legacy_hold_waits_and_includes_a_single_target_snapshot(self):
		conv = self.conv()
		with patch.object(held_sheets, "enabled", return_value=False):
			res = self.call("update_doc", self.args(), conv)
		self.assertTrue(res["ok"], res)
		name = held_writes.waiting_on(conv)
		self.assertTrue(name)
		self.assertEqual(len(_seal.unseal_call(_store.get_row(name))["targets"]), 1)
		self.assertEqual(self.value(), 1)

	def test_native_single_versions_change_after_a_normal_settings_edit(self):
		before = _park.snapshot_targets("update_doc", self.args())
		self.assertEqual(len(before), 1)
		self.assertEqual(before[0][:2], [DT, DT])
		frappe.db.set_single_value(DT, FIELD, 0)
		frappe.db.commit()
		self.assertNotEqual(_park.snapshot_targets("update_doc", self.args()), before)

	def test_canonical_name_create_and_batch_restrictions(self):
		conv = self.conv()
		cases = [
			("create_doc", {"doctype": DT, "values": {FIELD: 0}}),
			("update_doc", {**self.args(), "name": "wrong-name"}),
			("update_doc", {**self.args(), "name": ""}),
			("update_doc", {"doctype": DT, "updates": [{"name": DT, "changes": {FIELD: 0}}]}),
		]
		for tool, args in cases:
			with self.subTest(args=args):
				self.assertFalse(self.call(tool, args, conv)["ok"])
		self.assertEqual(self.sheets(), [])
		self.assertEqual(self.value(), 1)

	def test_uninitialized_and_partial_single_states_are_not_given_synthetic_versions(self):
		for partial in (False, True):
			with self.subTest(partial=partial):
				frappe.db.delete("Singles", {"doctype": DT})
				if partial:
					frappe.db.sql(
						"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s,'docstatus','0')", DT
					)
				frappe.clear_document_cache(DT, DT)
				frappe.db.commit()
				self.assertEqual(_park.snapshot_targets("update_doc", self.args()), [])
				result = self.call("update_doc", self.args(), self.conv())
				self.assertFalse(result["ok"])
				self.assertIn("initializ", result["error"]["message"].lower())

	def test_single_stale_change_bounces_without_overwriting_the_new_value(self):
		conv, row = self.sealed()
		frappe.db.set_single_value(DT, "allow_negative_rates_for_items", 1)
		frappe.db.commit()
		self.apply(row.name)
		result = self.run_job()
		self.assertEqual(result.status, "Pending")
		self.assertTrue(result.apply_errors)
		self.assertEqual(self.value(), 1)
		# Edit preserves the sealed snapshot: even an explicit retry cannot bypass it.
		result = self.applied(row.name, {"0": {"action": "edit", "values": {FIELD: 0}}})
		self.assertEqual(result.status, "Pending")
		self.assertEqual(self.value(), 1)
		with as_user(OWNER):
			self.assertTrue(approvals_api.discard_sheet(row.name)["ok"])
		turn = self.turn(conv)
		self.assert_added(self.call("update_doc", self.args(), conv), 1)
		self.set_turn(turn, state="done")
		held_sheet_seal.seal_turn(conv, turn)
		fresh = next(s for s in self.sheets() if s.conversation == conv and s.status == "Pending")
		self.assertNotEqual(fresh.name, row.name)
		result = self.applied(fresh.name)
		self.assertEqual(result.status, "Executed")
		self.assertEqual(self.value(), 0)

	def test_worker_rechecks_the_final_single_policy_after_apply_was_requested(self):
		_, row = self.sealed()
		self.apply(row.name)
		real_risk = _write_risk.risk_of
		with patch.object(
			_write_risk,
			"risk_of",
			side_effect=lambda tool, args: "crm_settings_sync"
			if args.get("doctype") == DT
			else real_risk(tool, args),
		):
			result = self.run_job()
		self.assertEqual(result.status, "Pending")
		self.assertEqual(self.value(), 1)

	def test_owner_permission_is_rechecked_and_other_user_cannot_approve(self):
		_, row = self.sealed()
		with as_user(OTHER):
			self.assertFalse(approvals_api.apply_sheet(row.name, "{}")["ok"])
		self.assertEqual(self.jobs(), [])
		frappe.get_doc("User", OWNER).remove_roles("Purchase Manager")
		frappe.db.commit()
		self.apply(row.name)
		self.assertEqual(self.run_job().status, "Pending")
		self.assertEqual(self.value(), 1)

	def test_single_secret_fields_and_denied_modules_are_not_admitted(self):
		for dt, values in [
			("System Settings", {"language": "en"}),
			("Jarvis Settings", {"enabled": 0}),
			(DT, {"api_key": "not-a-real-secret"}),
		]:
			with self.subTest(doctype=dt):
				item = {"doctype": dt, "name": dt, "op": "update", "values": values}
				self.assertIsNotNone(held_writes._denied_item(item))
		# ERPNext Integrations is not the denied Integrations module. Its real
		# Password field must still be blocked before any initialization check.
		item = {
			"doctype": "Plaid Settings",
			"name": "Plaid Settings",
			"op": "update",
			"values": {"plaid_secret": "fixture"},
		}
		self.assertIn("secret", held_writes._denied_item(item)["error"]["message"])
		for enabled in (0, 1):
			item = {
				"doctype": "CRM Settings",
				"name": "CRM Settings",
				"op": "update",
				"values": {_write_risk.CRM_SYNC_FIELD: enabled},
			}
			self.assertIsNotNone(held_writes._denied_item(item))

	def test_legacy_execution_still_refuses_a_new_guarded_structure_risk(self):
		conv = self.conv()
		with patch.object(held_sheets, "enabled", return_value=False):
			self.assertTrue(self.call("update_doc", self.args(), conv)["ok"])
		name = held_writes.waiting_on(conv)
		real_risk = _write_risk.risk_of
		with (
			as_user(OWNER),
			patch.object(
				_write_risk,
				"risk_of",
				side_effect=lambda tool, args: "crm_settings_sync"
				if args.get("doctype") == DT
				else real_risk(tool, args),
			),
		):
			result = approvals_api.decide_held_action(name, "create")
		self.assertFalse(result["ok"])
		self.assertEqual(self.value(), 1)

	def test_a_failed_single_save_cannot_leave_unapproved_values_in_document_cache(self):
		from jarvis.tools.update_doc import _update_one

		def fail_after_cache(*args):
			_update_one(*args)
			self.assertEqual(frappe.get_cached_doc(DT, DT).get(FIELD), 0)
			raise frappe.ValidationError("fixture rollback")

		record = {"doctype": DT, "name": DT, "op": "update", "values": {FIELD: 0}}
		with as_user(OWNER), patch("jarvis.tools.update_doc._update_one", side_effect=fail_after_cache):
			with self.assertRaises(_sheet._Failed):
				_sheet._update(0, record, record["values"], [])
		self.assertEqual(self.value(), 1)
		self.assertEqual(frappe.get_cached_doc(DT, DT).get(FIELD), 1)

	def test_repeated_setting_proposals_merge_and_remain_behind_the_sheet(self):
		conv, _, row = self.proposed()
		self.assert_added(self.call("update_doc", self.args(allow_negative_rates_for_items=0), conv), 1)
		[record] = self.records(self.row(row.name))
		self.assertEqual(set(record["values"]), {FIELD, "allow_negative_rates_for_items"})
		self.assertEqual(self.value(), 1)

	def test_discard_does_not_apply_a_setting(self):
		_, row = self.sealed()
		with as_user(OWNER):
			res = approvals_api.discard_sheet(row.name)
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.value(), 1)

	def test_an_action_card_reaches_the_same_sheet_without_a_manual_question(self):
		from jarvis.chat import filebox_cards

		conv = self.conv()
		self.turn(conv)
		card = {
			"kind": "doc",
			"verb": "update",
			"doctype": DT,
			"name": DT,
			"fields": [{"label": FIELD, "value": 0}],
		}
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": conv,
				"role": "assistant",
				"seq": 2,
				"content": "```jarvis-action\n" + json.dumps(card) + "\n```",
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		filebox_cards.park_from_turn(conv, doc.name)
		self.assertEqual(len(self.records(self.sheet(conv))), 1)
		self.assertFalse(frappe.db.exists("Jarvis Approval Request", {"conversation": conv}))
		self.assertNotIn("jarvis-action", frappe.db.get_value("Jarvis Chat Message", doc.name, "content"))
		self.assertEqual(self.value(), 1)

	def test_merged_risk_is_checked_on_the_whole_record_at_apply(self):
		_, row = self.sealed()
		real_risk = _write_risk.risk_of
		with patch.object(
			_write_risk,
			"risk_of",
			side_effect=lambda tool, args: "sensitive"
			if args.get("doctype") == DT and FIELD in args.get("changes", {})
			else real_risk(tool, args),
		):
			result = self.apply(row.name)
		self.assertFalse(result["ok"])
		self.assertEqual(self.jobs(), [])
		self.assertEqual(self.value(), 1)

	def test_settings_also_hold_up_the_draft_and_a_sealed_sheet_refuses_more_writes(self):
		conv, _, row = self.proposed()
		refusal = held_sheets.draft_refusal(conv, row)
		self.assertEqual(refusal["error"]["code"], "ApprovalPendingError")
		turn = row.collecting_turn
		self.set_turn(turn, state="done")
		held_sheet_seal.seal_turn(conv, turn)
		self.assertEqual(self.call("update_doc", self.args(), conv)["error"]["code"], "ApprovalPendingError")
		self.assertEqual(self.value(), 1)
