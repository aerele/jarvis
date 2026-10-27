"""A company's Account must not reuse another company's approval or ledger."""

import json
from unittest.mock import patch
from uuid import uuid4

import frappe

from jarvis import api
from jarvis.chat import approvals_api, held_edit, held_parties, held_sheets, held_writes
from jarvis.chat.pending_actions import _seal, _sheet
from jarvis.tests import test_filebox_held as held_tests
from jarvis.tests import test_filebox_sheet_apply as sheet_tests
from jarvis.tests._pending_action_helpers import as_user, ensure_user


class _AccountCompanies:
	actor = held_tests.OWNER

	def setUp(self):
		super().setUp()
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		self.original_roles = set(frappe.get_roles(self.actor))
		ensure_user(self.actor, ("Jarvis User", "Accounts Manager", "Purchase Master Manager"))
		frappe.clear_cache(user=self.actor)
		self.prefix = "zz-j767 " + uuid4().hex[:8]
		self.account_name = self.prefix + " Creditors (USD)"
		self.companies = []
		self.parents = {}
		self.addCleanup(self.cleanup_accounts)
		with as_user("Administrator"):
			for suffix in ("A", "B"):
				name = self.prefix + " Company " + suffix
				company = frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": name,
						"abbr": uuid4().hex[:4] + suffix,
						"default_currency": "INR",
						"country": "India",
					}
				)
				company.name = name
				company.db_insert()
				self.companies.append(name)
				parent = frappe.get_doc(
					{
						"doctype": "Account",
						"account_name": self.prefix + " Liabilities",
						"company": name,
						"is_group": 1,
						"root_type": "Liability",
					}
				)
				parent.flags.ignore_mandatory = True
				parent.insert(ignore_permissions=True)
				self.parents[name] = parent.name
			frappe.db.commit()

	def cleanup_accounts(self):
		frappe.db.rollback()
		for company in self.companies:
			frappe.db.delete("Account", {"company": company})
			frappe.db.delete("Company", {"name": company})
			frappe.clear_document_cache("Company", company)
		added_roles = set(frappe.get_roles(self.actor)) - self.original_roles
		if added_roles:
			frappe.db.delete(
				"Has Role", {"parent": self.actor, "parenttype": "User", "role": ["in", list(added_roles)]}
			)
			frappe.clear_cache(user=self.actor)
		frappe.db.commit()

	def args(self, company):
		return {
			"doctype": "Account",
			"values": {
				"account_name": self.account_name,
				"company": company,
				"parent_account": self.parents[company],
				"account_type": "Payable",
				"account_currency": "USD",
				"is_group": 0,
			},
		}

	def item(self, company):
		return held_parties.items_of("create_doc", self.args(company))[0]


class TestAccountCompanyIdentity(_AccountCompanies, held_tests._Base):
	def test_legacy_dedup_key_distinguishes_the_selected_company(self):
		one, two = (self.item(c) for c in self.companies)
		self.assertNotEqual(held_parties.item_key(one), held_parties.item_key(two))

	def test_sheet_identity_distinguishes_the_selected_company(self):
		one, two = (set(held_sheets.identities(self.item(c))) for c in self.companies)
		self.assertTrue(one.isdisjoint(two), (one, two))

	def test_pending_account_in_another_company_is_not_joined(self):
		first, second = self.conv(), self.conv()
		self.assert_held(self.call("create_doc", self.args(self.companies[0]), first))
		self.assert_held(self.call("create_doc", self.args(self.companies[1]), second))
		rows = self.rows()
		self.assertEqual(len(rows), 2)
		self.assertEqual({r.conversation for r in rows}, {first, second})
		self.assertEqual(frappe.db.count("Account", {"account_name": self.account_name}), 0)

	def test_native_controller_refuses_parent_from_another_company(self):
		first, second = self.companies
		args = self.args(first)
		args["values"]["parent_account"] = self.parents[second]
		result = self.call("create_doc", args, self.conv())
		self.assertFalse(result["ok"], result)
		self.assertIn("does not belong to company", result["error"]["message"])
		self.assertEqual(self.rows(), [])
		self.assertFalse(frappe.db.exists("Account", {"account_name": self.account_name}))

	def test_same_company_pending_account_still_joins_one_proposal(self):
		first, second = self.conv(), self.conv()
		for conversation in (first, second):
			self.assert_held(self.call("create_doc", self.args(self.companies[0]), conversation))
		[row] = self.rows()
		self.assertEqual(len(self.waiters(row.name)), 2)

	def test_company_correction_after_approval_gets_its_own_confirmation(self):
		conversation = self.conv()
		wrong, corrected = self.companies
		self.assert_held(self.call("create_doc", self.args(wrong), conversation))
		[row] = self.rows()
		with as_user(held_tests.OWNER):
			result = approvals_api.decide_held_action(row.name, "create")
		self.assertTrue(result["ok"], result)
		self.assertTrue(frappe.db.exists("Account", {"account_name": self.account_name, "company": wrong}))
		self.assertFalse(
			frappe.db.exists("Account", {"account_name": self.account_name, "company": corrected})
		)
		self.assert_held(self.call("create_doc", self.args(corrected), conversation))
		rows = self.rows()
		self.assertEqual(len(rows), 2)
		self.assertEqual(sum(r.status == "Pending" for r in rows), 1)
		self.assertFalse(
			frappe.db.exists("Account", {"account_name": self.account_name, "company": corrected})
		)

	def existing(self, company, **changes):
		with as_user("Administrator"):
			doc = frappe.get_doc({"doctype": "Account", **self.args(company)["values"], **changes})
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		return doc

	def test_all_four_identity_contracts_agree(self):
		for company in self.companies:
			item = self.item(company)
			key = held_parties.item_key(item)
			self.assertEqual(held_sheets.identities(item), [key])
			self.assertEqual(_sheet.lock_keys([item]), [key])
			self.assertEqual(
				_sheet._recent_keys(self.actor, [item], [{"index": 0, "action": "created"}]),
				[_seal.open_key(self.actor, key)],
			)

	def test_company_case_space_number_and_punctuation(self):
		item = self.item(self.companies[0])

		def key(**changes):
			return held_parties.item_key({**item, "values": {**item["values"], **changes}})

		self.assertEqual(key(), key(company=" " + self.companies[0].upper() + " "))
		self.assertNotEqual(key(account_number="1000"), key(account_number="1001"))
		self.assertNotEqual(key(company="A-B"), key(company="A B"))
		self.assertNotEqual(key(company="B@C", account_number="A"), key(company="C", account_number="A@B"))

	def test_missing_company_is_refused_before_preview_or_parking(self):
		for value in (None, "", "  ", False, []):
			with self.subTest(company=value):
				args = self.args(self.companies[0])
				args["values"]["company"] = value
				with patch("jarvis.api._run_preview") as preview:
					res = self.call("create_doc", args, self.conv())
				self.assertFalse(res["ok"], res)
				self.assertIn("explicit company", res["error"]["message"])
				preview.assert_not_called()
		self.assertEqual(self.rows(), [])

	def test_existing_account_is_reported_only_in_the_requested_company(self):
		first, second = self.companies
		existing = self.existing(first)
		res = self.call("create_doc", self.args(first), self.conv())
		self.assertFalse(res["ok"], res)
		for text in (existing.name, first, "USD", "already exists"):
			self.assertIn(text, res["error"]["message"])
		self.assertNotIn(second, res["error"]["message"])
		self.assert_held(self.call("create_doc", self.args(second), self.conv()))

	def test_scoped_lookup_preserves_company_read_permissions(self):
		first, second = self.companies
		self.existing(first)
		self.existing(second)
		with as_user("Administrator"):
			permission = frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": self.actor,
					"allow": "Company",
					"for_value": first,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()

		def clean_permission():
			frappe.db.delete("User Permission", {"name": permission.name})
			frappe.db.commit()
			frappe.clear_cache(user=self.actor)

		self.addCleanup(clean_permission)
		frappe.clear_cache(user=self.actor)
		with as_user(self.actor):
			self.assertEqual(len(held_parties.existing_of(self.item(first))), 1)
			self.assertEqual(held_parties.existing_of(self.item(second)), [])

	def test_use_existing_cannot_choose_another_company(self):
		requested, other = self.companies
		foreign = self.existing(other)
		self.assert_held(self.call("create_doc", self.args(requested), self.conv()))
		[row] = self.rows()
		with as_user(self.actor):
			result = approvals_api.decide_held_action(row.name, "use_existing", foreign.name)
		self.assertFalse(result["ok"], result)
		self.assertEqual(self.rows()[0].status, "Pending")
		self.assertFalse(
			frappe.db.exists("Account", {"account_name": self.account_name, "company": requested})
		)

	def test_use_existing_cannot_choose_a_different_currency(self):
		company = self.companies[0]
		self.assert_held(self.call("create_doc", self.args(company), self.conv()))
		[row] = self.rows()
		conflict = self.existing(company, account_currency="INR")
		with as_user(self.actor):
			result = approvals_api.decide_held_action(row.name, "use_existing", conflict.name)
		self.assertFalse(result["ok"], result)
		self.assertEqual(self.rows()[0].status, "Pending")

	def test_malformed_account_title_is_not_treated_as_a_lookup_operator(self):
		args = self.args(self.companies[0])
		args["values"]["account_name"] = ["is", "set"]
		result = self.call("create_doc", args, self.conv())
		self.assertFalse(result["ok"], result)
		self.assertNotIn("already exists", result["error"]["message"])
		self.assertEqual(self.rows(), [])

	def test_approval_currency_conflict_does_not_offer_an_unusable_action(self):
		company = self.companies[0]
		self.assert_held(self.call("create_doc", self.args(company), self.conv()))
		[row] = self.rows()
		self.existing(company, account_currency="INR")
		with as_user(self.actor):
			for decide in (
				lambda: approvals_api.decide_held_action(row.name, "create"),
				lambda: approvals_api.edit_and_create_held(row.name, {"account_currency": "USD"}),
			):
				result = decide()
				self.assertFalse(result["ok"], result)
				self.assertNotIn("Choose Use existing", result["error"]["message"])
				self.assertIn("currency", result["error"]["message"])
		self.assertEqual(self.rows()[0].status, "Pending")

	def test_edit_company_does_not_reuse_the_old_company_recent_key(self):
		original, corrected = self.companies
		conversation = self.conv()
		self.assert_held(self.call("create_doc", self.args(original), conversation))
		[row] = self.rows()
		self.assertNotIn("company", held_edit.locked_fields([self.item(original)], 0))
		with as_user(self.actor):
			result = approvals_api.edit_and_create_held(
				row.name, {"company": corrected, "parent_account": self.parents[corrected]}
			)
		self.assertTrue(result["ok"], result)
		self.assertTrue(
			frappe.db.exists("Account", {"account_name": self.account_name, "company": corrected})
		)
		self.assertFalse(
			frappe.db.exists("Account", {"account_name": self.account_name, "company": original})
		)
		self.assert_held(self.call("create_doc", self.args(original), conversation))

	def test_legacy_hmac_rows_do_not_block_other_companies(self):
		first, second = self.companies
		self.assert_held(self.call("create_doc", self.args(first), self.conv()))
		[row] = self.rows()
		legacy = _seal.open_key(self.actor, "name:Account:" + held_parties.norm_name(self.account_name))
		sealed = row.sealed_call
		self.set_row(row.name, open_key=legacy, dedup_keys=json.dumps([legacy]))
		self.assert_held(self.call("create_doc", self.args(second), self.conv()))
		self.assertEqual(len(self.rows()), 2)
		self.assertEqual(self.rows()[0].sealed_call, sealed)

	def test_old_same_company_card_and_new_card_cannot_create_twice(self):
		company = self.companies[0]
		self.assert_held(self.call("create_doc", self.args(company), self.conv()))
		[old] = self.rows()
		legacy = _seal.open_key(self.actor, "name:Account:" + held_parties.norm_name(self.account_name))
		self.set_row(old.name, open_key=legacy, dedup_keys=json.dumps([legacy]))
		self.assert_held(self.call("create_doc", self.args(company), self.conv()))
		self.assertEqual(len(self.rows()), 2, "Old HMAC keys age out; sealed proposals are not migrated")
		with as_user(self.actor):
			self.assertTrue(approvals_api.decide_held_action(old.name, "create")["ok"])
			new = self.rows()[1]
			result = approvals_api.decide_held_action(new.name, "create")
		self.assertFalse(result["ok"], result)
		self.assertEqual(
			frappe.db.count("Account", {"company": company, "account_name": self.account_name}), 1
		)

	def test_company_edit_with_existing_target_preserves_proposal(self):
		original, corrected = self.companies
		self.assert_held(self.call("create_doc", self.args(original), self.conv()))
		[row] = self.rows()
		existing = self.existing(corrected)
		with as_user(self.actor):
			result = approvals_api.edit_and_create_held(
				row.name, {"company": corrected, "parent_account": self.parents[corrected]}
			)
		self.assertFalse(result["ok"], result)
		self.assertIn(existing.name, result["error"]["message"])
		self.assertEqual(self.rows()[0].status, "Pending")
		self.assertEqual(self.rows()[0].sealed_call, row.sealed_call)
		self.assertFalse(
			frappe.db.exists("Account", {"company": original, "account_name": self.account_name})
		)

	def test_ambiguous_account_aliases_bind_to_neither_company(self):
		records = [held_sheets._record(self.item(c)) for c in self.companies]
		dependent = held_sheets._record(self.item(self.companies[0]))
		dependent["values"] = {
			**dependent["values"],
			"account_name": self.account_name + " child",
			"parent_account": self.account_name,
		}
		held_sheets.link_deps([*records, dependent])
		self.assertEqual(dependent["depends_on"], [])
		remap = {}
		for record, target in zip(records, ("native A", "native B"), strict=True):
			held_sheets.learn(record, target, remap)
		self.assertEqual(held_sheets.remapped(dependent, remap)["parent_account"], self.account_name)

	def test_one_account_bare_alias_still_resolves(self):
		record = held_sheets._record(self.item(self.companies[0]))
		dependent = held_sheets._record(self.item(self.companies[0]))
		dependent["values"] = {
			**dependent["values"],
			"account_name": "child",
			"parent_account": self.account_name,
		}
		held_sheets.link_deps([record, dependent])
		self.assertEqual(dependent["depends_on"], [0])
		remap = {}
		held_sheets.learn(record, "native account", remap)
		self.assertEqual(held_sheets.remapped(dependent, remap)["parent_account"], "native account")

	def test_ambiguous_alias_stays_unresolved_between_the_two_account_creates(self):
		one, two = [held_sheets._record(self.item(c)) for c in self.companies]
		child = held_sheets._record(self.item(self.companies[0]))
		child["values"] = {**child["values"], "account_name": "child", "parent_account": self.account_name}
		records = [one, child, two]
		held_sheets.link_deps(records)
		seen = []

		def try_record(record, index, remap):
			if index == 1:
				seen.append(held_sheets.remapped(record, remap)["parent_account"])
			else:
				held_sheets.learn(record, f"native {index}", remap)

		with patch.object(held_sheets, "_try", side_effect=try_record):
			held_sheets._dry_run(records, [0, 1, 2])
		self.assertEqual(seen, [self.account_name], "Ambiguity is known before either Account is created")


class TestAccountCompanySheets(_AccountCompanies, sheet_tests._ApplyBase):
	actor = sheet_tests.OWNER

	def test_ambiguous_alias_between_account_creates_rolls_back_the_apply(self):
		first, second = self.companies
		group_a, group_b = self.args(first), self.args(second)
		for group in (group_a, group_b):
			group["values"].update(is_group=1, account_type="")
		child = self.args(first)
		child["values"].update(
			account_name=self.account_name + " child",
			parent_account=self.account_name + " - " + frappe.db.get_value("Company", first, "abbr"),
		)
		_, row = self.sheet_of(group_a, child, group_b)
		done = self.applied(
			row.name, {"1": {"action": "edit", "values": {"parent_account": self.account_name}}}
		)
		self.assertEqual(done.status, "Pending", done.apply_errors)
		self.assertTrue(done.apply_errors)
		self.assertEqual(frappe.db.count("Account", {"account_name": ["like", self.account_name + "%"]}), 0)

	def test_collecting_old_sheet_refreshes_only_derived_account_identity(self):
		conv = self.conv()
		self.turn(conv)
		first, second = self.companies
		self.assert_added(self.call("create_doc", self.args(first), conv))
		row = self.sheet(conv)
		records = self.records(row)
		original_values = records[0]["values"].copy()
		records[0]["identity"] = ["name:Account:" + held_parties.norm_name(self.account_name)]
		self.set_row(row.name, sealed_call=_seal.seal_call(row, {"records": records}, []))
		self.assert_added(self.call("create_doc", self.args(first), conv), 1)
		self.assert_added(self.call("create_doc", self.args(second), conv), 2)
		records = self.records(self.sheet(conv))
		self.assertEqual(len(records), 2)
		self.assertEqual(records[0]["values"], original_values)
		self.assertEqual(records[0]["identity"], held_sheets.identities(self.item(first)))
		self.assertEqual(records[1]["values"]["company"], second)

	def test_accounts_in_two_companies_remain_two_records_and_apply(self):
		_, row = self.sheet_of(*(self.args(c) for c in self.companies))
		self.assertEqual(len(self.records(row)), 2)
		for company in self.companies:
			self.assertFalse(
				frappe.db.exists("Account", {"account_name": self.account_name, "company": company})
			)
		done = self.applied(row.name)
		self.assertEqual(done.status, "Executed", done.apply_errors)
		for company in self.companies:
			self.assertTrue(
				frappe.db.exists("Account", {"account_name": self.account_name, "company": company})
			)
		self.assertFalse(self.apply(row.name)["ok"])

	def test_reproposal_changes_only_its_company_record(self):
		conversation = self.conv()
		self.turn(conversation)
		for company in self.companies:
			self.assert_added(self.call("create_doc", self.args(company), conversation))
		changed = self.args(self.companies[0])
		changed["values"]["account_currency"] = "INR"
		self.assert_added(self.call("create_doc", changed, conversation), 2)
		rows = self.records(self.sheet(conversation))
		self.assertEqual([r["values"]["account_currency"] for r in rows], ["INR", "USD"])

	def test_use_existing_cannot_cross_company(self):
		first, other = self.companies
		_, row = self.sheet_of(self.args(first))
		with as_user("Administrator"):
			foreign = frappe.get_doc({"doctype": "Account", **self.args(other)["values"]}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()
		result = self.apply(row.name, {"0": {"action": "use_existing", "existing": foreign.name}})
		self.assertFalse(result["ok"], result)
		self.assertEqual(self.row(row.name).status, "Pending")

	def test_account_created_in_desk_is_reused_in_its_company(self):
		company = self.companies[0]
		_, row = self.sheet_of(self.args(company))
		with as_user("Administrator"):
			frappe.get_doc({"doctype": "Account", **self.args(company)["values"]}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()
		done = self.applied(row.name)
		self.assertEqual(done.status, "Executed", done.apply_errors)
		self.assertEqual(self.outcome(done)["records"][0]["action"], "existing")
		self.assertEqual(
			frappe.db.count("Account", {"company": company, "account_name": self.account_name}), 1
		)

	def test_currency_conflict_is_not_silently_reused(self):
		company = self.companies[0]
		_, row = self.sheet_of(self.args(company))
		with as_user("Administrator"):
			frappe.get_doc(
				{"doctype": "Account", **self.args(company)["values"], "account_currency": "INR"}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		done = self.applied(row.name)
		self.assertEqual(done.status, "Pending")
		self.assertIn("currency", done.apply_errors.lower())
