"""Edit & create + hold with missing fields (unified pending action PR-2d, AC-U20).

Decision 17: a File Box create whose ONLY problem is missing mandatory fields (the
collect-mode classifier, A25/A27) goes back to Jarvis once, then waits on the board
with ``needs_input``; D11: the approver's Edit & create is their own create, dry-run
before any claim, and resolves like Use existing only when the dropper can read it."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api, held_edit, held_parties, held_writes
from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _store
from jarvis.tests._pending_action_helpers import as_user, ensure_user
from jarvis.tests.test_filebox_held import (
	CONV,
	OWNER,
	PA,
	PARTY,
	TAX,
	WAITER,
	_Base,
	_batch,
	_supplier,
	fake_send,
)

SM = "fbh-sm@example.com"  # the approver: a System Manager who may create Suppliers
NOREAD = "fbh-noread@example.com"  # a dropper who can't read Suppliers
EXTRA = (SM, NOREAD)


def _missing(name=PARTY, tax=TAX, **values):
	"""A create whose only problem is an empty mandatory Supplier Type."""
	return _supplier(name, tax, supplier_type="", **values)


class _EditBase(_Base):
	def setUp(self):
		super().setUp()
		ensure_user(SM, ("Jarvis User", "System Manager", "Purchase Master Manager"))
		ensure_user(NOREAD, ("Jarvis User",))
		self._wipe_extra()
		self.addCleanup(self._wipe_extra)

	def _wipe_extra(self):
		frappe.db.rollback()
		names = frappe.db.sql_list(f"SELECT name FROM `tab{PA}` WHERE owner_user IN %(u)s", {"u": EXTRA})
		if names:
			frappe.db.sql(f"DELETE FROM `tab{WAITER}` WHERE parent IN %(n)s", {"n": tuple(names)})
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE name IN %(n)s", {"n": tuple(names)})
		frappe.db.delete(CONV, {"owner": ["in", EXTRA]})
		frappe.db.delete("Jarvis Agent Write", {"actor": ["in", EXTRA]})
		frappe.db.delete("Supplier", {"supplier_name": ["like", "zz-fbh%"]})
		frappe.db.commit()

	def held_missing(self, args=None, conv=None, user=OWNER):
		"""Two tries in one conversation: the first goes back, the second is held."""
		conv = conv or self.conv(owner=user)
		args = args or _missing()
		first = self.call("create_doc", args, conv, user=user)
		self.assertFalse(first["ok"], first)
		self.assert_held(self.call("create_doc", args, conv, user=user))
		return self.rows(user)[-1].name, conv

	def edit(self, name, values=None, user=SM):
		with as_user(user):
			return approvals_api.edit_and_create_held(name, json.dumps(values or {}))

	def row(self, name):
		return frappe.db.sql(f"SELECT * FROM `tab{PA}` WHERE name=%s", (name,), as_dict=True)[0]

	def logged(self, title, contains):
		return frappe.get_all(
			"Error Log",
			filters={"method": f"jarvis.pending_action.{title}", "error": ["like", f"%{contains}%"]},
			pluck="error",
		)

	def park_args(self, args, owner=OWNER, **kw):
		"""Park a held create directly (shapes the File Box policy never parks itself)."""
		keys = [held_parties.item_key(i) for i in held_parties.items_of("create_doc", args)]
		return pa.park(
			kind="file_box_held",
			owner_user=owner,
			exec_user=owner,
			tool="create_doc",
			args=args,
			summary="New supplier",
			conversation=self.conv(owner=owner),
			dedup_key=keys[0],
			dedup_keys=keys,
			**kw,
		)


class TestClassifier(_EditBase):
	"""Collect mode: only missing permlevel-0 mandatory fields hold; everything else bounces."""

	def test_a_missing_only_create_goes_back_once_then_is_held(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		real = Supplier.validate

		def chatty(doc):  # a controller that msgprints: the sandbox runs must not leak it
			frappe.msgprint("zz-fbh controller note")
			return real(doc)

		conv = self.conv()
		with patch.object(Supplier, "validate", chatty):
			first = self.call("create_doc", _missing(), conv)
		self.assert_refused(first, "InvalidArgumentError")
		self.assertIn(f"Supplier {PARTY}: Supplier Type", first["error"]["message"])
		self.assertIn("call again", first["error"]["message"])
		self.assertEqual(self.rows(), [])
		self.assertEqual(frappe.local.message_log, [], "clear_messages: no msgprint leak")
		with patch.object(Supplier, "validate", chatty):
			res = self.call("create_doc", _missing(), conv)
		self.assert_held(res)
		self.assertEqual(res["data"]["needs_input"], ["Supplier Type"])
		self.assertIn("fill the missing fields", res["data"]["note"])
		self.assertEqual(frappe.local.message_log, [])
		[row] = self.rows()
		self.assertEqual(
			json.loads(row.needs_input),
			[{"doc_index": 0, "doctype": "Supplier", "fieldname": "supplier_type"}],
		)
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": PARTY}))

	def test_another_key_or_conversation_gets_its_own_try(self):
		conv = self.conv()
		self.assertFalse(self.call("create_doc", _missing(), conv)["ok"])
		other = self.call("create_doc", _missing("zz-fbh Other", ""), conv)
		self.assert_refused(other, "InvalidArgumentError")
		self.assertFalse(self.call("create_doc", _missing(), self.conv())["ok"])
		self.assertEqual(self.rows(), [])

	def test_a_child_row_missing_field_is_held_with_its_table_and_row(self):
		name, _ = self.held_missing(_supplier(companies=[{}]))
		self.assertEqual(
			json.loads(self.row(name).needs_input),
			[
				{
					"doc_index": 0,
					"doctype": "Supplier",
					"fieldname": "company",
					"parentfield": "companies",
					"idx": 1,
				}
			],
		)
		[label] = held_writes.missing_labels(self.row(name).needs_input)
		self.assertRegex(label, r" row 1: Company$")
		with as_user(OWNER):  # the board adds the child column from the entry's field
			[entry] = approvals_api.get_pending_action(name)["needs_input"]
		self.assertEqual((entry["field"]["fieldname"], entry["field"]["label"]), ("company", "Company"))

	def test_only_the_second_of_two_suppliers_misses_fields(self):
		name, _ = self.held_missing(_batch(_supplier("zz-fbh One", ""), _missing("zz-fbh Two", "")))
		self.assertEqual(
			json.loads(self.row(name).needs_input),
			[{"doc_index": 1, "doctype": "Supplier", "fieldname": "supplier_type"}],
		)

	def test_a_name_with_separators_is_never_parsed(self):
		# MandatoryError reads "[Supplier, <name>]: <fields>": a parser would split here.
		name, _ = self.held_missing(_missing("zz-fbh A, B]: supplier_name", ""))
		self.assertEqual(held_writes.missing_labels(self.row(name).needs_input), ["Supplier Type"])

	def test_everything_else_bounces_every_time(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		def controller_mandatory(self):
			raise frappe.MandatoryError("GST Category is mandatory")

		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbh Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		cases = {
			"missing + a bad link": ("create_doc", _missing(country="zz-no-such")),
			"missing, then a bad link in the batch": (
				"create_doc",
				_batch(_missing("zz-fbh One", ""), _supplier("zz-fbh Two", "", country="zz-no-such")),
			),
			"field autoname": ("create_doc", {"doctype": "Item", "values": {"item_name": "zz-fbh item"}}),
			"update_doc": (
				"update_doc",
				{"doctype": "Supplier", "name": existing.name, "changes": {"supplier_type": ""}},
			),
		}
		for label, (tool, args) in cases.items():
			with self.subTest(label):
				conv = self.conv()
				for _ in range(2):
					self.assertFalse(self.call(tool, args, conv)["ok"])
		with patch.object(Supplier, "validate", controller_mandatory):
			conv = self.conv()
			for _ in range(2):
				res = self.call("create_doc", _missing("zz-fbh Ctl", ""), conv)
				self.assertIn("GST Category", res["error"]["message"])
		self.assertEqual(self.rows(), [])

	def test_a_missing_field_above_permlevel_0_bounces(self):
		df = frappe.get_meta("Supplier").get_field("supplier_type")
		items = held_parties.items_of("create_doc", _missing())
		self.assertEqual(len(held_writes.collect_missing("create_doc", items)), 1)
		with patch.object(df, "permlevel", 1):
			self.assertEqual(held_writes.collect_missing("create_doc", items), [])

	def test_a_missing_field_the_board_cant_fill_bounces(self):
		meta = frappe.get_meta("Supplier")
		df = meta.get_field("supplier_type")
		items = held_parties.items_of("create_doc", _missing())
		for fieldtype in ("Password", "Attach", "Signature", "Geolocation"):
			with self.subTest(fieldtype), patch.object(df, "fieldtype", fieldtype):
				self.assertEqual(held_writes.collect_missing("create_doc", items), [])
		# a required table with no rows: the board edits proposed rows, never adds one
		with patch.object(meta.get_field("companies"), "reqd", 1):
			complete = held_parties.items_of("create_doc", _supplier())
			self.assertEqual(held_writes.collect_missing("create_doc", complete), [])

	def test_collect_mode_hooks_run_under_the_dispatch_depth_guard(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		from jarvis.tools import registry

		real, seen = Supplier.validate, []

		def spy(doc):  # a tenant hook here must meet refuse_in_tool_dispatch
			seen.append(registry.in_tool_dispatch())
			return real(doc)

		items = held_parties.items_of("create_doc", _missing())
		with patch.object(Supplier, "validate", spy):
			self.assertEqual(len(held_writes.collect_missing("create_doc", items)), 1)
		self.assertEqual(seen, [True])
		self.assertFalse(registry.in_tool_dispatch(), "the depth is restored")

	def test_a_cache_outage_holds_instead_of_a_first_miss_every_time(self):
		import redis

		items = held_parties.items_of("create_doc", _missing())
		needs = [{"doc_index": 0, "doctype": "Supplier", "fieldname": "supplier_type"}]
		down, conv = redis.exceptions.ConnectionError("down"), self.conv()
		with patch.object(frappe.cache, "execute_command", side_effect=down):
			self.assertTrue(held_writes._missed_before(conv, items, needs))
		conv = self.conv()
		self.assertFalse(held_writes._missed_before(conv, items, needs), "healthy: the first miss")
		self.assertTrue(held_writes._missed_before(conv, items, needs))

	def test_the_ladder_names_the_missing_fields(self):
		_name, conv = self.held_missing()
		row = self.ladder()[conv]
		self.assertEqual(row["status"], "needs_approval")
		self.assertEqual(row["result"], "Needs approval — missing: Supplier Type")
		self.assertEqual(row["missing"], "Supplier Type")
		self.assertEqual(held_writes.missing_summary(list("ABCDE")), "A, B, C (+2 more)")
		self.assertEqual(held_writes.missing_summary(["A"]), "A")


class TestDetailAndCreate(_EditBase):
	def test_detail_shows_masked_values_labelled_needs_input_and_locks(self):
		name, _ = self.held_missing(_missing(bank_api_key="zz-SECRET-1"))
		with as_user(OWNER):
			res = approvals_api.get_pending_action(name)
		self.assertEqual(res["can_edit"], 1)
		[entry] = res["needs_input"]
		self.assertEqual(
			{k: entry[k] for k in ("doc_index", "doctype", "fieldname", "label")},
			{"doc_index": 0, "doctype": "Supplier", "fieldname": "supplier_type", "label": "Supplier Type"},
		)
		self.assertEqual((entry["field"]["fieldtype"], entry["field"]["reqd"]), ("Select", 1))
		[doc] = res["docs"]
		self.assertEqual(doc["values"]["supplier_name"], PARTY)
		self.assertEqual(doc["values"]["bank_api_key"], held_edit.MASK)
		self.assertEqual(doc["secret"], ["bank_api_key"])
		self.assertEqual(set(doc["locked"]), {"supplier_name", "tax_id"})
		self.assertNotIn("zz-SECRET-1", json.dumps(res))

	def test_create_is_refused_while_fields_are_missing(self):
		name, _ = self.held_missing()
		with patch("jarvis.api.dispatch") as dispatch, as_user(OWNER):
			res = approvals_api.decide_held_action(name, "create")
		self.assertEqual((res["ok"], res["reason_code"]), (False, "needs_input"))
		self.assertIn("Supplier Type", res["error"]["message"])
		dispatch.assert_not_called()
		self.assertEqual(self.row(name).status, "Pending")


class TestPatchRules(_EditBase):
	"""D11 step 2: what a patch may not touch (``held_edit.apply_patches``)."""

	def rules(self, args, patches, approver=SM):
		items = held_parties.items_of("create_doc", args)
		return held_edit.apply_patches(items, patches, approver)

	def refused(self, args, patch_, field, **where):
		_merged, errors = self.rules(args, [patch_])
		self.assertEqual(len(errors), 1, errors)
		self.assertEqual(errors[0]["fieldname"], field)
		for k, v in where.items():
			self.assertEqual(errors[0][k], v)
		return errors[0]["message"]

	def test_a_fill_merges_over_the_sealed_values(self):
		merged, errors = self.rules(_missing(), [{"supplier_type": "Company", "supplier_details": "x"}])
		self.assertEqual(errors, [])
		self.assertEqual(
			merged[0]["values"],
			{"supplier_name": PARTY, "tax_id": TAX, "supplier_type": "Company", "supplier_details": "x"},
		)
		# the dedup-key fields may be re-sent unchanged
		self.assertEqual(self.rules(_missing(), [{"supplier_name": f" {PARTY} ", "tax_id": TAX}])[1], [])

	def test_identity_secret_protected_unknown_and_name_are_refused(self):
		self.assertIn(
			"identifies", self.refused(_missing(), {"supplier_name": "zz-fbh Else"}, "supplier_name")
		)
		self.refused(_missing(), {"tax_id": "27ZZZZZ0000Z1Z1"}, "tax_id")
		self.assertIn("Secret", self.refused(_missing(), {"bank_api_key": "x"}, "bank_api_key"))
		for field in ("owner", "docstatus", "idx", "parent"):
			self.refused(_missing(), {field: "x"}, field)
		self.refused(_missing(), {"zz_no_field": "x"}, "zz_no_field")
		self.refused(_missing(), {"name": "SUP-X"}, "name")

	def test_a_permlevel_field_is_refused_not_silently_reset(self):
		df = frappe.get_meta("Supplier").get_field("supplier_details")
		with patch.object(df, "permlevel", 1):
			self.assertIn(
				"higher access", self.refused(_missing(), {"supplier_details": "x"}, "supplier_details")
			)
			# ... a proposed value in it too: the approver's insert would reset it
			self.refused(_missing(supplier_details="y"), {}, "supplier_details")
			self.assertEqual(self.rules(_missing(supplier_details="y"), [{}], "Administrator")[1], [])

	def test_the_approvers_rights_never_reach_a_value_the_board_did_not_show(self):
		"""S3: approving for another user, a proposed value the form can't show, or one
		that user couldn't set, is refused; the approver's own patch is theirs to make."""
		from frappe.model.meta import Meta

		args = _missing(zz_no_field="x", companies=[{"company": "zz Co", "zz_cell": "y"}])
		items = held_parties.items_of("create_doc", args)
		_merged, errors = held_edit.apply_patches(items, [{}], SM, exec_user=OWNER)
		self.assertEqual({e["fieldname"] for e in errors}, {"zz_no_field", "zz_cell"})
		self.assertEqual(held_edit.apply_patches(items, [{}], SM, exec_user=SM)[1], [])
		# editing a shown cell of the table never waves its hidden ones through
		patch_ = [{"zz_no_field": None, "companies": [{"company": "zz Co"}]}]
		self.assertIn(
			"zz_cell", [e["fieldname"] for e in held_edit.apply_patches(items, patch_, SM, OWNER)[1]]
		)
		df = frappe.get_meta("Supplier").get_field("supplier_details")
		levels = patch.object(
			Meta, "get_permlevel_access", lambda self, ptype="read", user=None: [0, 1] if user == SM else [0]
		)
		with patch.object(df, "permlevel", 1), levels:
			items = held_parties.items_of("create_doc", _missing(supplier_details="y"))
			_merged, errors = held_edit.apply_patches(items, [{}], SM, exec_user=OWNER)
			self.assertEqual([e["fieldname"] for e in errors], ["supplier_details"])
			patched = held_edit.apply_patches(items, [{"supplier_details": "z"}], SM, exec_user=OWNER)
			self.assertEqual(patched[1], [])

	def test_child_row_keys_and_new_rows_are_refused(self):
		args = _supplier(companies=[{}])
		for key in ("name", "parent", "parenttype", "parentfield", "idx", "docstatus", "owner"):
			self.refused(args, {"companies": [{key: "x"}]}, key, parentfield="companies", idx=1)
		self.refused(args, {"companies": [{}, {"company": "x"}]}, "companies")
		merged, errors = self.rules(args, [{"companies": [{"company": "zz Co"}]}])
		self.assertEqual((errors, merged[0]["values"]["companies"]), ([], [{"company": "zz Co"}]))

	def test_a_reference_to_another_record_of_the_batch_is_locked(self):
		address = {
			"doctype": "Address",
			"values": {
				"address_title": "zz-fbh Addr",
				"links": [{"link_doctype": "Supplier", "link_name": PARTY}],
			},
		}
		args = _batch(_missing(), address)
		items = held_parties.items_of("create_doc", args)
		self.assertIn("links.1.link_name", held_edit.locked_fields(items, 1))
		_merged, errors = self.rules(args, [{}, {"links": [{"link_name": "zz-fbh Else"}]}])
		self.assertEqual((errors[0]["doc_index"], errors[0]["fieldname"]), (1, "link_name"))
		self.assertIn("another record in this batch", errors[0]["message"])
		self.assertEqual(self.rules(args, [{}, {"links": [{"link_name": PARTY}]}])[1], [])


class TestEditAndCreate(_EditBase):
	def test_one_edit_creates_as_the_approver_and_resumes_every_file(self):
		name, conv_a = self.held_missing()
		conv_b = self.conv()
		self.assertFalse(self.call("create_doc", _missing(), conv_b)["ok"])
		self.assert_held(self.call("create_doc", _missing(), conv_b))  # joins the same row
		self.assertEqual(len(self.rows()), 1)
		ran_as = []
		real = approvals_api._edit_dry_run

		def spy(*a):
			ran_as.append(frappe.session.user)
			return real(*a)

		with patch.object(approvals_api, "_edit_dry_run", side_effect=spy):
			res = self.edit(name, {"supplier_type": "Company"})
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (True, "created", "Executed"))
		self.assertEqual((res["outcome"], res["waiters_count"]), ("confirmed", 2))
		supplier = frappe.get_doc("Supplier", {"supplier_name": PARTY})
		self.assertEqual(
			(supplier.owner, supplier.supplier_type), (SM, "Company"), "the approver's own create"
		)
		self.assertEqual(ran_as, [SM])
		row = self.row(name)
		self.assertEqual(
			(row.status, row.edited, row.decided_by, row.result_name), ("Executed", 1, SM, supplier.name)
		)
		self.assertEqual(row.reason, "Created on the Approval Board with edited values.")
		audit = frappe.get_all(
			"Jarvis Agent Write",
			filters={"actor": SM, "provenance": "approval", "provenance_name": name, "outcome": "applied"},
		)
		self.assertEqual(len(audit), 1)
		with as_user("Administrator"), fake_send() as sent:
			self.assertEqual(held_writes.resume_waiters(name), 2)
		self.assertEqual({c["conversation"] for c in sent}, {conv_a, conv_b})
		for call in sent:
			self.assertIn("with their own values", call["message"])
			# the data (approver included) sits inside the one DATA-quoted span
			self.assertEqual(call["message"].count("`"), 2)
			quoted = call["message"].split("`")[1]
			self.assertIn(f"-> Supplier {supplier.name} (created on the board by", quoted)

	def test_approving_for_someone_else_refuses_a_value_the_board_never_showed(self):
		name = self.park_args(_missing(zz_hidden="x"))
		res = self.edit(name, {"supplier_type": "Company"})
		self.assertEqual(res["reason_code"], "edit_refused")
		self.assertIn("zz_hidden", [e["fieldname"] for e in res["errors"]])
		self.assertEqual(self.row(name).status, "Pending")

	def test_refusals_claim_nothing(self):
		name, _ = self.held_missing()
		with patch.object(_store, "claim", wraps=_store.claim) as claim:
			res = self.edit(name, {"supplier_name": "zz-fbh Else"})
			self.assertEqual(res["reason_code"], "edit_refused")
			self.assertEqual(res["errors"][0]["label"], "Supplier Name")
			# the dry-run runs BEFORE any claim: a bad value, or the field left empty
			bad = self.edit(name, {"supplier_type": "Bogus"})
			self.assertEqual(bad["reason_code"], "invalid")
			self.assertIn("Bogus", bad["error"]["message"])
			self.assertNotIn("<", bad["error"]["message"])
			empty = self.edit(name, {})
			self.assertEqual(empty["reason_code"], "invalid")
			self.assertEqual(
				[(e["fieldname"], e["label"]) for e in empty["errors"]], [("supplier_type", "Supplier Type")]
			)
			with as_user(OWNER):
				other = approvals_api.edit_and_create_held(name, "{}")  # the owner: allowed
			self.assertEqual(other["reason_code"], "invalid")
		claim.assert_not_called()
		row = self.row(name)
		self.assertEqual((row.status, row.edited, row.decided_by), ("Pending", 0, None))
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": PARTY}))

	def test_malformed_values_are_a_validation_error_not_a_crash(self):
		name, _ = self.held_missing()
		for values in ("{not json", "[1, 2]", "null"):
			with self.subTest(values=values), as_user(SM), self.assertRaises(frappe.ValidationError) as cm:
				approvals_api.edit_and_create_held(name, values)
			self.assertIn("one per record", str(cm.exception))
		self.assertEqual(self.row(name).status, "Pending")

	def test_the_precheck_finds_an_existing_party_or_a_sibling_approval(self):
		name, _ = self.held_missing()
		mine = self.row(name)
		# another dropper's approval for the same party (owner-scoped rows never dedup across owners)
		theirs, _ = self.held_missing(user="fbh-other@example.com")
		with patch.object(_store, "claim", wraps=_store.claim) as claim:
			res = self.edit(mine.name, {"supplier_type": "Company"})
			self.assertEqual(res["reason_code"], "pending_elsewhere")
			self.assertIn(f"New supplier: {PARTY}", res["error"]["message"])
			self.set_row(theirs, status="Discarded")
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbh Exists", "tax_id": TAX}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()
			res = self.edit(mine.name, {"supplier_type": "Company"})
			self.assertEqual(res["reason_code"], "exists")
			self.assertEqual(res["candidates"][0]["gstin"], TAX)
		claim.assert_not_called()
		self.assertEqual(self.row(mine.name).status, "Pending")

	def test_the_precheck_names_another_users_approval_only_to_a_system_manager(self):
		"""S1: an owner learns another approval covers the record, never its summary."""
		name, _ = self.held_missing()
		theirs, _ = self.held_missing(user="fbh-other@example.com")
		self.set_row(theirs, summary="New supplier: zz-fbh Their Secret Co")
		res = self.edit(name, {"supplier_type": "Company"}, user=OWNER)
		self.assertEqual(res["reason_code"], "pending_elsewhere")
		self.assertNotIn("Their Secret", res["error"]["message"])
		self.assertNotIn(theirs, res["error"]["message"])
		res = self.edit(name, {"supplier_type": "Company"})
		self.assertIn("Their Secret", res["error"]["message"])

	def test_the_precheck_reads_the_identity_an_edit_gives_an_args_keyed_create(self):
		# No name and no tax id: an args-hash key, so nothing is locked and the edit names it.
		args = {"doctype": "Supplier", "values": {"supplier_type": "Company"}}
		[item] = held_parties.items_of("create_doc", args)
		self.assertTrue(held_parties.item_key(item).startswith("args:"))
		name = self.park_args(
			args, needs_input=[{"doc_index": 0, "doctype": "Supplier", "fieldname": "supplier_name"}]
		)
		theirs, _ = self.held_missing(user="fbh-other@example.com")  # holds gstin TAX
		fill = {"supplier_name": "zz-fbh Fresh", "tax_id": TAX}
		with patch.object(_store, "claim", wraps=_store.claim) as claim:
			self.assertEqual(self.edit(name, fill)["reason_code"], "pending_elsewhere")
			self.set_row(theirs, status="Discarded")
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbh Exists", "tax_id": TAX}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()
			res = self.edit(name, fill)
			self.assertEqual(res["reason_code"], "exists")
			self.assertEqual(res["candidates"][0]["gstin"], TAX)
		claim.assert_not_called()
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": "zz-fbh Fresh"}))

	def test_an_edited_batch_resume_counts_every_record_without_a_card(self):
		name, _ = self.held_missing(_batch(_supplier("zz-fbh One", ""), _missing("zz-fbh Two", "")))
		self.assertFalse(json.loads(self.row(name).card or "null"), "a needs_input batch parks card-less")
		res = self.edit(name, [{}, {"supplier_type": "Company"}])
		self.assertEqual((res["ok"], res["pa_status"]), (True, "Executed"))
		message = held_writes.resume_message(self.row(name))
		self.assertIn("All 2 records of the batch were created", message)
		self.assertIn("with their own values", message)
		self.assertNotIn("by the values you proposed", message)
		self.assertNotIn("records of the batch", message.split("`")[1], "bench text, outside the DATA")
		created = frappe._dict(status="Executed", edited=0, summary="x", dedup_keys=json.dumps(list("abc")))
		self.assertIn(
			"All 3 records of the batch were created: look the others up by the values you proposed",
			held_writes.resume_message(created),
		)

	def test_a_record_the_dropper_cant_open_rolls_everything_back(self):
		conv = self.conv(owner=NOREAD)
		args = _supplier("zz-fbh Hidden", "")
		keys = [held_parties.item_key(i) for i in held_parties.items_of("create_doc", args)]
		name = pa.park(
			kind="file_box_held",
			owner_user=NOREAD,
			exec_user=NOREAD,
			tool="create_doc",
			args=args,
			summary="New supplier: zz-fbh Hidden",
			conversation=conv,
			dedup_key=keys[0],
			dedup_keys=keys,
		)
		res = self.edit(name, {})
		self.assertEqual((res["ok"], res["reason_code"]), (False, "unreadable"))
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": "zz-fbh Hidden"}))
		row = self.row(name)
		self.assertEqual(
			(row.status, row.edited, row.decided_by), ("Pending", 0, None), "row back to Pending"
		)
		self.assertTrue(
			frappe.get_all(
				"Jarvis Agent Write", filters={"actor": SM, "outcome": "failed", "provenance_name": name}
			)
		)
		self.assertEqual(self.jobs, [])

	def test_a_failure_after_the_claim_ends_failed(self):
		name, _ = self.held_missing()
		with patch.object(approvals_api, "_edit_dry_run", return_value=None):  # the race the dry-run missed
			res = self.edit(name, {"supplier_type": "Bogus"})
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (False, "failed", "Failed"))
		self.assertEqual(res["outcome"], "failed")
		self.assertIn("Bogus", res["error"]["message"])
		row = self.row(name)
		self.assertEqual(
			(row.status, row.reason_code, row.edited, row.decided_by), ("Failed", "failed", 1, SM)
		)
		self.assertEqual({w.resume_state for w in self.waiters(name)}, {"due"})
		self.assertIn("FAILED", held_writes.resume_message(row))

	def test_edit_is_for_held_creates_only(self):
		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbh Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		self.call(
			"update_doc",
			{"doctype": "Supplier", "name": existing.name, "changes": {"supplier_details": "x"}},
			self.conv(),
		)
		[row] = self.rows()
		self.assertEqual(self.edit(row.name, {})["reason_code"], "edit_unavailable")
		with as_user(OWNER):
			self.assertEqual(approvals_api.get_pending_action(row.name)["can_edit"], 0)
		with as_user("fbh-other@example.com"):
			self.assertEqual(approvals_api.edit_and_create_held(row.name, "{}")["reason_code"], "not_found")

	def test_a_concurrent_edit_and_create_have_one_winner(self):
		conv = self.conv()
		self.assert_held(self.call("create_doc", _supplier(), conv))  # complete: Create is allowed
		[row] = self.rows()
		site, barrier, results, errors = frappe.local.site, threading.Barrier(2), {}, []

		def worker(label, user, fn):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(user)
			try:
				barrier.wait(timeout=10)
				results[label] = fn()
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		threads = [
			threading.Thread(
				target=worker,
				args=("edit", SM, lambda: approvals_api.edit_and_create_held(row.name, "{}")),
			),
			threading.Thread(
				target=worker,
				args=("create", OWNER, lambda: approvals_api.decide_held_action(row.name, "create")),
			),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		frappe.db.rollback()  # a fresh snapshot: the workers committed after this one began
		self.assertEqual(errors, [])
		self.assertEqual(sorted(r["ok"] for r in results.values()), [False, True], results)
		loser = next(r for r in results.values() if not r["ok"])
		self.assertIn(loser["reason_code"], ("busy", "executing", "already_handled"))
		self.assertEqual(frappe.db.count("Supplier", {"supplier_name": PARTY}), 1)
		self.assertEqual(self.row(row.name).status, "Executed")


class TestEditInterference(_EditBase):
	"""At most once when the insert's transaction was not ours start to finish: the
	claim is uncommitted, so a mid-dispatch rollback drops it and a mid-dispatch
	commit exposes it to the reconciler."""

	def edit_with(self, name, before, values=None):
		"""Edit & create with ``before()`` run inside the dispatch, ahead of the insert."""
		from jarvis import api

		real = api.dispatch_confirmed

		def dispatch(*a, **k):
			before()
			return real(*a, **k)

		with patch("jarvis.api.dispatch_confirmed", side_effect=dispatch):
			return self.edit(name, values or {"supplier_type": "Company"})

	def test_a_rollback_that_drops_the_claim_never_leaves_pending_with_a_record(self):
		name, _ = self.held_missing()
		res = self.edit_with(name, frappe.db.rollback)
		supplier = frappe.db.get_value("Supplier", {"supplier_name": PARTY})
		self.assertTrue(supplier, "the insert after the rollback was kept")
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (False, "partial", "Failed"))
		self.assertIn(f"Supplier {supplier}", res["error"]["message"])
		row = self.row(name)
		self.assertEqual(
			(row.status, row.reason_code, row.result_doctype, row.result_name, row.edited, row.decided_by),
			("Failed", "partial", "Supplier", supplier, 1, SM),
		)
		self.assertEqual(len(self.logged("claim_lost", name)), 1)
		self.assertEqual({w.resume_state for w in self.waiters(name)}, {"due"})
		self.assertIn("could not be verified", held_writes.resume_message(row))

	def test_a_failure_after_a_rollback_that_dropped_the_claim_ends_partial(self):
		name, _ = self.held_missing()
		with patch.object(approvals_api, "_edit_dry_run", return_value=None):
			res = self.edit_with(name, frappe.db.rollback, {"supplier_type": "Bogus"})
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (False, "partial", "Failed"))
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "partial"))
		self.assertEqual(len(self.logged("claim_lost", name)), 1)

	def test_a_commit_mid_dispatch_still_resolves_our_claim(self):
		name, _ = self.held_missing()
		res = self.edit_with(name, frappe.db.commit)
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (True, "created", "Executed"))
		row = self.row(name)
		supplier = frappe.db.get_value("Supplier", {"supplier_name": PARTY})
		self.assertEqual((row.status, row.result_name, row.edited), ("Executed", supplier, 1))

	def test_a_reconciler_verdict_during_a_committed_dispatch_stands(self):
		name, _ = self.held_missing()

		def committed_then_reaped():
			frappe.db.commit()  # the claim is visible now ...
			_store._terminal_update(name, ["Executing"], "Failed", reason_code="interrupted")
			frappe.db.commit()  # ... and the reconciler called it interrupted

		res = self.edit_with(name, committed_then_reaped)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assertIn("Late outcome: confirmed.", row.reason)
		self.assertEqual((res["reason_code"], res["pa_status"]), ("interrupted", "Failed"))
		self.assertEqual(len(self.logged("late_outcome", name)), 1)
		self.assertTrue(frappe.db.exists("Supplier", {"supplier_name": PARTY}), "the late create is kept")

	def test_a_late_failure_keeps_the_reconcilers_verdict(self):
		name, _ = self.held_missing()

		def committed_then_reaped():
			frappe.db.commit()
			_store._terminal_update(name, ["Executing"], "Failed", reason_code="interrupted")
			frappe.db.commit()

		with patch.object(approvals_api, "_edit_dry_run", return_value=None):
			res = self.edit_with(name, committed_then_reaped, {"supplier_type": "Bogus"})
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assertIn("Late outcome: partial.", row.reason)
		self.assertEqual((res["reason_code"], res["pa_status"]), ("partial", "Failed"))

	def test_a_claim_taken_after_the_rollback_is_not_ours(self):
		name, _ = self.held_missing()

		def rolled_back_then_claimed():
			frappe.db.rollback()
			_store.claim(name, SM)  # the same approver's other tab, mid-flight
			frappe.db.commit()

		res = self.edit_with(name, rolled_back_then_claimed)
		row = self.row(name)
		self.assertEqual(row.status, "Executing", "the other claim resolves it")
		self.assertEqual(res["pa_status"], "Executing")
		self.assertEqual(len(self.logged("late_outcome", name)), 1)

	def test_a_failure_reports_the_rows_real_status(self):
		from jarvis.chat.pending_actions import _execute

		name, _ = self.held_missing()
		real = _execute._discard_failed_dispatch

		def discard_then_skipped(*a, **k):
			real(*a, **k)
			# the rollback released the row: a Skip lands before the failure is written
			_store._terminal_update(name, ["Pending"], "Discarded", reason_code="discarded")
			frappe.db.commit()

		with (
			patch.object(approvals_api, "_edit_dry_run", return_value=None),
			patch.object(_execute, "_discard_failed_dispatch", side_effect=discard_then_skipped),
		):
			res = self.edit(name, {"supplier_type": "Bogus"})
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (False, "failed", "Discarded"))
		self.assertEqual(res["outcome"], "discarded")
		self.assertEqual(self.row(name).status, "Discarded")
