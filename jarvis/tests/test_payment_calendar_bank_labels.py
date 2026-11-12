"""Bank name and masked number at read time (D6, B4, B6, AB4): never stored, never noisy, never an error."""

import json
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import access, api, bank_labels, snapshot, store
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

NUMBER = "50100012343281"
MASKED = "•••• 3281"


def deny_bank_account_read():
	real = frappe.has_permission

	def check(doctype=None, ptype="read", *args, **kwargs):
		if doctype == "Bank Account":
			return False
		return real(doctype, ptype, *args, **kwargs)

	return patch("frappe.has_permission", side_effect=check)


def bank_account_queries(spy):
	return [call for call in spy.call_args_list if call.args and call.args[0] == "Bank Account"]


class BankAccountFixtures(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_bank_labels")
		self.fx = CalendarFixtures()
		update_safety_floor(self.fx.company, "20", 0)
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.serial = 0

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_bank_labels")

	def bank_account(self, account=None, *, number=NUMBER, bank="HDFC Bank", **fields):
		self.serial += 1
		values = {
			"account_name": f"Account {self.serial}",
			"bank": bank,
			"account": account or self.fx.bank,
			"company": self.fx.company,
			"is_company_account": 1,
			"disabled": 0,
			"bank_account_no": number,
			**fields,
		}
		frappe.set_user("Administrator")
		try:
			return self.fx.insert("Bank Account", f"BA{self.serial}", **values)
		finally:
			frappe.set_user(self.user)

	def cash_accounts(self, count):
		frappe.set_user("Administrator")
		try:
			return [self.fx.account(f"Cash{index}", "Cash", "Asset") for index in range(count)]
		finally:
			frappe.set_user(self.user)


class TestBankLabels(BankAccountFixtures):
	def labels(self, *accounts):
		return bank_labels.bank_labels(self.fx.company, list(accounts or [self.fx.bank]))

	def test_one_company_bank_account_gives_bank_and_masked_last_four(self):
		self.assertTrue(frappe.has_permission("Bank Account", "read"))
		self.bank_account()
		self.assertEqual(self.labels()[self.fx.bank], {"bank": "HDFC Bank", "reference": MASKED})

	def test_unreadable_bank_account_gives_nulls_and_no_messages(self):
		self.bank_account()
		frappe.local.message_log = []
		with deny_bank_account_read(), patch("frappe.get_list") as read:
			labels = self.labels()
		read.assert_not_called()
		self.assertEqual(labels[self.fx.bank], {"bank": None, "reference": None})
		self.assertEqual(frappe.local.message_log, [])

	def test_two_matches_on_one_account_give_nulls(self):
		self.bank_account(number="50100012343281")
		self.bank_account(number="50100099999999", bank="ICICI Bank")
		self.assertEqual(self.labels()[self.fx.bank], {"bank": None, "reference": None})

	def test_disabled_other_company_and_personal_accounts_are_ignored(self):
		ignored = {"disabled": 1}, {"company": self.fx.company + "x"}, {"is_company_account": 0}
		for fields in ignored:
			with self.subTest(fields):
				self.bank_account(bank="Ignored Bank", **fields)
				self.assertEqual(self.labels()[self.fx.bank], {"bank": None, "reference": None})
		# One valid account next to the ignored ones still counts as exactly one.
		self.bank_account()
		self.assertEqual(self.labels()[self.fx.bank], {"bank": "HDFC Bank", "reference": MASKED})

	def test_reference_needs_at_least_eight_characters(self):
		for number, reference in (
			("1234567", None),
			("12345678", "•••• 5678"),
			("", None),
			(None, None),
		):
			with self.subTest(number=number):
				frappe.db.rollback(save_point="calendar_bank_labels")
				frappe.db.savepoint("calendar_bank_labels")
				self.bank_account(number=number)
				self.assertEqual(self.labels()[self.fx.bank], {"bank": "HDFC Bank", "reference": reference})

	def test_reference_needs_digits_in_the_last_four_places(self):
		for number, reference in (
			("XXXXXXXX", None),
			("1234XXXX", None),
			("123456\u00b2\u00b2", None),
			("XXXX5678", "•••• 5678"),
		):
			with self.subTest(number=number):
				frappe.db.rollback(save_point="calendar_bank_labels")
				frappe.db.savepoint("calendar_bank_labels")
				self.bank_account(number=number)
				self.assertEqual(self.labels()[self.fx.bank], {"bank": "HDFC Bank", "reference": reference})

	def test_account_without_a_bank_account_gets_nulls(self):
		self.assertEqual(self.labels()[self.fx.bank], {"bank": None, "reference": None})

	def test_every_requested_account_gets_an_entry_in_one_query(self):
		accounts = self.cash_accounts(25)
		for account in accounts[:24]:
			self.bank_account(account)
		with patch("frappe.get_list", wraps=frappe.get_list) as read:
			labels = self.labels(*accounts)
		self.assertEqual(len(bank_account_queries(read)), 1)
		self.assertEqual(set(labels), set(accounts))
		# More than the default page of 20 rows: nothing is cut off.
		self.assertEqual(len([v for v in labels.values() if v["bank"]]), 24)
		self.assertEqual(labels[accounts[24]], {"bank": None, "reference": None})

	def test_filters_are_exactly_the_d6_rule_with_user_permissions_applied(self):
		with patch("frappe.get_list", return_value=[]) as read:
			self.labels()
		(doctype,), kwargs = read.call_args
		self.assertEqual(doctype, "Bank Account")
		self.assertEqual(
			kwargs["filters"],
			{
				"account": ["in", [self.fx.bank]],
				"company": self.fx.company,
				"is_company_account": 1,
				"disabled": 0,
			},
		)
		self.assertEqual(kwargs["fields"], ["account", "bank", "bank_account_no"])
		self.assertNotIn("ignore_permissions", kwargs)

	def test_a_failing_read_gives_nulls_and_removes_only_its_own_messages(self):
		def noisy(*args, **kwargs):
			frappe.msgprint("PRIVATE no permission for Bank Account")
			raise frappe.PermissionError("PRIVATE")

		frappe.local.message_log = []
		frappe.msgprint("earlier message")
		with patch("frappe.get_list", side_effect=noisy):
			labels = self.labels()
		self.assertEqual(labels[self.fx.bank], {"bank": None, "reference": None})
		self.assertEqual(len(frappe.local.message_log), 1)
		self.assertNotIn("PRIVATE", json.dumps(frappe.local.message_log))

	def test_a_failing_read_is_logged_without_its_message(self):
		with (
			patch("frappe.get_list", side_effect=frappe.PermissionError("PRIVATE_BANK_NUMBER")),
			patch("frappe.logger") as logger,
		):
			self.labels()
		logger.assert_called_with("payment_calendar")
		logger.return_value.warning.assert_called_once_with("bank label read failed: PermissionError")

	def test_messages_left_by_a_successful_read_are_removed_too(self):
		def noisy(*args, **kwargs):
			frappe.msgprint("noise")
			return [frappe._dict(account=self.fx.bank, bank="HDFC Bank", bank_account_no=NUMBER)]

		frappe.local.message_log = []
		with patch("frappe.get_list", side_effect=noisy):
			labels = self.labels()
		self.assertEqual(labels[self.fx.bank]["reference"], MASKED)
		self.assertEqual(frappe.local.message_log, [])


class TestCapabilitiesBankLabels(BankAccountFixtures):
	def accounts(self):
		frappe.local.message_log = []
		result = api.get_capabilities(self.fx.company)
		self.assertTrue(result["ok"], result)
		return {a["name"]: a for a in result["data"]["accounts"]}

	def test_catalog_rows_carry_bank_and_reference(self):
		self.bank_account()
		row = self.accounts()[self.fx.bank]
		self.assertEqual((row["bank"], row["reference"]), ("HDFC Bank", MASKED))
		self.assertEqual(row["account_type"], "Bank")

	def test_catalog_reads_bank_accounts_once_for_every_account(self):
		accounts = self.cash_accounts(24)
		for account in accounts:
			self.bank_account(account)
		with patch("frappe.get_list", wraps=frappe.get_list) as read:
			rows = self.accounts()
		self.assertEqual(len(bank_account_queries(read)), 1)
		self.assertEqual({rows[a]["bank"] for a in accounts}, {"HDFC Bank"})
		self.assertIn(self.fx.bank, rows)

	def test_unreadable_bank_account_keeps_the_catalog_ok_with_null_fields_and_no_messages(self):
		self.bank_account()
		with deny_bank_account_read():
			row = self.accounts()[self.fx.bank]
		self.assertEqual((row["bank"], row["reference"]), (None, None))
		self.assertEqual(frappe.local.message_log, [])


class TestStatusBank(BankAccountFixtures):
	def setUp(self):
		super().setUp()
		self.fx.invoice((40, 60))
		self.scope = normalize_scope(self.fx.company, [self.fx.bank])
		self.proof = access.authorize_scope(self.scope)
		sources = {
			side: read_obligations(
				self.fx.company, side, self.fx.today, self.scope["window_start"], self.scope["window_end"]
			)
			for side in ("Payable", "Receivable")
		}
		# The stored total differs from the row sum: the status must pass it through unchanged.
		self.stored_bank = {
			"total": "123.45",
			"accounts": [{"account": self.fx.bank, "balance": "100.00"}],
			"currency": "INR",
			"precision": 2,
		}
		self.result = snapshot.build_result(
			self.scope,
			sources,
			self.stored_bank,
			None,
			{},
			currency="INR",
			floor=get_settings(self.fx.company),
			precision=2,
			generated_at="2026-10-24T10:00:00+00:00",
		)
		self.serial_digest = 0

	def publish(self, result):
		self.serial_digest += 1
		sid, _ = store.admit(self.user, str(self.serial_digest) * 64, self.scope, self.proof["fingerprint"])
		store.mark_running(self.user, sid)
		store.publish_snapshot(self.user, sid, store.split_result(result, self.scope))
		return sid

	def status(self, result=None):
		frappe.local.message_log = []
		return api.get_snapshot_status(self.publish(result or self.result))

	def test_status_bank_lists_each_account_with_name_type_bank_and_reference(self):
		self.bank_account()
		response = self.status()
		self.assertTrue(response["ok"], response)
		name, account_type = frappe.db.get_value("Account", self.fx.bank, ["account_name", "account_type"])
		self.assertEqual(
			response["data"]["bank"],
			{
				"total": "123.45",
				"currency": "INR",
				"precision": 2,
				"accounts": [
					{
						"account": self.fx.bank,
						"account_name": name,
						"account_type": account_type,
						"bank": "HDFC Bank",
						"reference": MASKED,
						"balance": "100.00",
					}
				],
			},
		)

	def test_status_totals_are_the_stored_values_not_recomputed(self):
		bank = self.status()["data"]["bank"]
		self.assertEqual(bank["total"], "123.45")
		self.assertEqual(bank["accounts"][0]["balance"], "100.00")

	def test_bank_labels_are_never_stored_in_the_result(self):
		self.bank_account()
		sid = self.publish(self.result)
		api.get_snapshot_status(sid)
		stored = store.load_part(self.user, sid, "status")["bank"]
		self.assertEqual(stored, self.stored_bank)
		self.assertNotIn("HDFC", json.dumps(stored))

	def test_unreadable_bank_account_gives_null_labels_ok_true_and_empty_messages(self):
		self.bank_account()
		with deny_bank_account_read():
			response = self.status()
		self.assertTrue(response["ok"], response)
		row = response["data"]["bank"]["accounts"][0]
		self.assertEqual((row["bank"], row["reference"]), (None, None))
		self.assertEqual(frappe.local.message_log, [])

	def test_revoked_bank_account_read_shows_on_the_next_status_call(self):
		self.bank_account()
		sid = self.publish(self.result)
		self.assertEqual(api.get_snapshot_status(sid)["data"]["bank"]["accounts"][0]["bank"], "HDFC Bank")
		with deny_bank_account_read():
			after = api.get_snapshot_status(sid)
		self.assertIsNone(after["data"]["bank"]["accounts"][0]["bank"])
		self.assertEqual(api.get_snapshot_status(sid)["data"]["bank"]["accounts"][0]["bank"], "HDFC Bank")

	def test_status_reads_bank_accounts_with_one_query(self):
		self.bank_account()
		sid = self.publish(self.result)
		with patch("frappe.get_list", wraps=frappe.get_list) as read:
			api.get_snapshot_status(sid)
		self.assertEqual(len(bank_account_queries(read)), 1)

	def test_no_selected_accounts_gives_a_null_bank_block(self):
		response = self.status({**self.result, "bank": None})
		self.assertTrue(response["ok"], response)
		self.assertIsNone(response["data"]["bank"])
