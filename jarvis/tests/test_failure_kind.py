"""R2-3: a failure at Confirm is classified (fixable / not fixable / retry later), the
kind rides the envelope, and the continuation picks its scaffold from it.

Rows marked CLASS-ONLY assert ``kind_of`` on an exception object; each is paired with
a REAL document save below that produces the same kind end to end."""

from __future__ import annotations

import ast
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from jarvis import _failure_kind as fk
from jarvis import api
from jarvis.chat import actions_api
from jarvis.chat import api as chat_api
from jarvis.exceptions import InvalidArgumentError, InvalidFieldValueError, RetryLaterError
from jarvis.tests import _erpnext_masters as masters

# Every class the plan allows as FIXABLE, imported here so a silent addition or
# removal in jarvis._failure_kind fails this test.
EXPECTED_FIXABLE = (
	frappe.exceptions.MandatoryError,
	frappe.exceptions.LinkValidationError,
	frappe.exceptions.CancelledLinkError,
	frappe.exceptions.DuplicateEntryError,
	frappe.exceptions.UniqueValidationError,
	frappe.exceptions.InvalidEmailAddressError,
	frappe.exceptions.InvalidPhoneNumberError,
	frappe.exceptions.InvalidNameError,
	frappe.exceptions.CharacterLengthExceededError,
	frappe.exceptions.DataTooLongException,
	frappe.exceptions.DoesNotExistError,
	InvalidFieldValueError,
)
MARIADB_BAD_DATE = (
	"Incorrect date value: '3 Oct 2026' for column `_db`.`tabToDo`.`date` at row 1"  # MariaDB quoting
)
MYSQL_BAD_DATE = "Incorrect datetime value: '31/31/2026' for column 'starts_on' at row 1"  # MySQL quoting


def _envelope(tool: str, args: dict) -> dict:
	"""The real write path's envelope (savepoint + translation), nothing committed."""
	return api._dispatch_and_wrap(tool, args, True)


class TestKindOf(FrappeTestCase):
	def test_every_fixable_class_is_listed_and_fixable(self):
		self.assertEqual(set(fk.FIXABLE_CLASSES), set(EXPECTED_FIXABLE))
		for cls in EXPECTED_FIXABLE:
			with self.subTest(cls=cls.__name__):
				self.assertEqual(fk.kind_of(cls("x")), fk.FIXABLE)

	def test_retry_later_classes(self):
		for exc in (frappe.QueryDeadlockError("d"), frappe.QueryTimeoutError("t"), RetryLaterError("held")):
			with self.subTest(exc=type(exc).__name__):
				self.assertEqual(fk.kind_of(exc), fk.RETRY_LATER)

	def test_plain_validation_error_is_not_fixable(self):
		# CLASS-ONLY; real rows: credit limit, frozen accounts, a bad status (below).
		self.assertEqual(fk.kind_of(frappe.ValidationError("Credit limit crossed")), fk.NOT_FIXABLE)

	def test_unknown_exception_is_not_fixable(self):
		for exc in (
			Exception("boom"),
			KeyError("k"),
			frappe.PermissionError("no"),
			InvalidArgumentError("x"),
		):
			with self.subTest(exc=type(exc).__name__):
				self.assertEqual(fk.kind_of(exc), fk.NOT_FIXABLE)

	def test_a_refusal_code_is_never_fixable(self):
		class Refused(frappe.MandatoryError):
			code = "structure_refused"

		self.assertEqual(fk.kind_of(Refused("Custom Field")), fk.NOT_FIXABLE)

	def test_erpnext_business_rules_are_not_fixable(self):
		# CLASS-ONLY; real rows: insufficient stock, closed accounting period (below).
		classes = fk._erpnext_not_fixable()
		names = {c.__name__ for c in classes}
		self.assertTrue({"NegativeStockError", "ClosedAccountingPeriod", "BudgetError"} <= names, names)
		for cls in classes:
			with self.subTest(cls=cls.__name__):
				self.assertEqual(fk.kind_of(cls("x")), fk.NOT_FIXABLE)

	def test_bad_date_both_quoting_styles(self):
		found = fk.bad_date(Exception(1292, MARIADB_BAD_DATE))
		self.assertEqual(
			found, {"type": "date", "value": "3 Oct 2026", "doctype": "ToDo", "fieldname": "date"}
		)
		found = fk.bad_date(Exception(1292, MYSQL_BAD_DATE))
		self.assertEqual((found["type"], found["fieldname"], found["doctype"]), ("datetime", "starts_on", ""))
		self.assertEqual(fk.kind_of(Exception(1292, MYSQL_BAD_DATE)), fk.FIXABLE)

	def test_bad_date_driver_classes(self):
		import pymysql

		errors = [pymysql.err.OperationalError(1292, MARIADB_BAD_DATE)]
		try:
			import MySQLdb

			errors.append(MySQLdb.OperationalError(1292, MARIADB_BAD_DATE))
		except ImportError:
			pass
		for exc in errors:
			with self.subTest(driver=type(exc).__module__):
				self.assertEqual(fk.kind_of(exc), fk.FIXABLE)
				self.assertEqual(fk.bad_date(exc)["fieldname"], "date")

	def test_other_database_errors_stay_not_fixable(self):
		for exc in (
			Exception(1292, "Truncated incorrect DOUBLE value: 'abc'"),
			Exception(1062, "Duplicate entry"),
			Exception(1406, MARIADB_BAD_DATE),
			Exception("Incorrect date value"),
		):
			with self.subTest(args=exc.args):
				self.assertIsNone(fk.bad_date(exc))
				self.assertEqual(fk.kind_of(exc), fk.NOT_FIXABLE)


class TestEnvelopeKind(FrappeTestCase):
	def test_table(self):
		rows = [
			({"ok": False, "error": {"code": "InvalidArgumentError", "kind": "fixable"}}, fk.FIXABLE),
			({"ok": False, "error": {"code": "RetryLaterError", "kind": "retry_later"}}, fk.RETRY_LATER),
			({"ok": False, "error": {"code": "InvalidArgumentError", "message": "x"}}, fk.NOT_FIXABLE),
			({"ok": False, "error": {"code": "SomeNewCode", "kind": "fixable"}}, fk.NOT_FIXABLE),
			({"ok": False, "error": {"code": "RetryLaterError", "kind": "fixable"}}, fk.NOT_FIXABLE),
			({"ok": False, "error": {"code": "structure_refused", "kind": "fixable"}}, fk.NOT_FIXABLE),
			({"ok": False, "error": {"code": "sensitive_refused"}}, fk.NOT_FIXABLE),
			(
				{
					"ok": False,
					"reason_code": "structure_refused",
					"error": {"code": "InvalidArgumentError", "kind": "fixable"},
				},
				fk.NOT_FIXABLE,
			),
			({"ok": False, "error": {"type": "InternalError", "kind": "fixable"}}, fk.NOT_FIXABLE),
			({"ok": False}, fk.NOT_FIXABLE),
			(None, fk.NOT_FIXABLE),
			("boom", fk.NOT_FIXABLE),
		]
		for result, want in rows:
			with self.subTest(result=result):
				self.assertEqual(fk.envelope_kind(result), want)


class TestRealSavesClassified(FrappeTestCase):
	"""Real saves through the write path; the class rolls back."""

	def test_missing_mandatory_is_fixable(self):
		r = _envelope("create_doc", {"doctype": "ToDo", "values": {"priority": "High"}})
		self.assertEqual(
			(r["ok"], r["error"]["code"], r["error"]["kind"]), (False, "InvalidArgumentError", "fixable")
		)

	def test_link_to_a_missing_record_is_fixable(self):
		r = _envelope(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": "j2a link", "allocated_to": "nobody-j2a@example.com"},
			},
		)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)

	def test_duplicate_name_is_fixable(self):
		args = {"doctype": "UOM", "values": {"uom_name": "_J2A Duplicate UOM"}}
		self.assertTrue(_envelope("create_doc", args)["ok"])
		r = _envelope("create_doc", args)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertIn("already exists", r["error"]["message"])

	def test_bad_select_option_is_fixable_and_names_the_field(self):
		r = _envelope(
			"create_doc", {"doctype": "ToDo", "values": {"description": "j2a", "priority": "Urgent"}}
		)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertIn("Priority (priority)", r["error"]["message"])
		self.assertIn("Urgent", r["error"]["message"])

	def test_bad_status_reaches_frappes_own_check_and_is_not_fixable(self):
		# The type check leaves ``status`` to the controller; Frappe's own Select check
		# raises a plain ValidationError, which is NOT on the allow-list.
		r = _envelope("create_doc", {"doctype": "ToDo", "values": {"description": "j2a", "status": "Bogus"}})
		self.assertEqual(
			(r["ok"], r["error"]["code"], r["error"]["kind"]), (False, "InvalidArgumentError", "not_fixable")
		)

	def test_a_deadlock_is_retry_later_and_enveloped(self):
		def _deadlock(tool, args):
			raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")

		with patch("jarvis.api.dispatch", side_effect=_deadlock):
			r = _envelope("create_doc", {"doctype": "ToDo", "values": {"description": "j2a"}})
		self.assertEqual(
			(r["ok"], r["error"]["code"], r["error"]["kind"]), (False, "RetryLaterError", "retry_later")
		)
		self.assertNotIn("Deadlock", r["error"]["message"])

	def test_a_deadlock_drops_what_the_tool_queued_for_after_commit(self):
		# A real deadlock rolls the whole transaction back in the database, taking the
		# savepoint with it; simulated here by a full rollback inside the tool.
		def _queued():
			pass

		def _deadlock(tool, args):
			frappe.db.rollback()
			frappe.db.after_commit.add(_queued)
			raise frappe.QueryDeadlockError("Deadlock found")

		with patch("jarvis.api.dispatch", side_effect=_deadlock):
			r = _envelope("create_doc", {"doctype": "ToDo", "values": {"description": "j2a"}})
		self.assertEqual(r["error"]["kind"], "retry_later")
		self.assertNotIn(_queued, frappe.db.after_commit._functions, "a rolled-back write sends nothing")

	def test_a_real_deadlock_inside_a_batch_is_retry_later(self):
		# The real shape inside run_atomic_batch: InnoDB rolls the whole transaction
		# back (its savepoint included) before the driver raises.
		from jarvis.tools import create_doc as create_doc_module

		real = create_doc_module._insert_one
		calls = []

		def _second_deadlocks(doctype, values, **kw):
			calls.append(doctype)
			if len(calls) == 2:
				frappe.db._conn.rollback()
				raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")
			return real(doctype, values, **kw)

		docs = [{"doctype": "ToDo", "values": {"description": f"j2a batch {i}"}} for i in range(2)]
		with patch.object(create_doc_module, "_insert_one", side_effect=_second_deadlocks):
			r = _envelope("create_doc", {"docs": docs})
		self.assertEqual(
			(r["ok"], r["error"]["code"], r["error"]["kind"]), (False, "RetryLaterError", "retry_later"), r
		)

	def test_an_unexpected_error_still_raises(self):
		with patch("jarvis.api.dispatch", side_effect=KeyError("boom")), self.assertRaises(KeyError):
			_envelope("create_doc", {"doctype": "ToDo", "values": {"description": "j2a"}})

	def test_extra_keys_leave_the_plugin_rendering_alone(self):
		# The plugin renders a failed tool as `${code}: ${message}` (the Jarvis plugin's
		# toolError), reading only those two keys; `kind` is additive. Its
		# contract test is the plugin's; this pins the bench side of that shape.
		r = _envelope("create_doc", {"doctype": "ToDo", "values": {"priority": "High"}})
		err = r["error"]
		self.assertTrue({"code", "message", "kind"} <= set(err), err)
		self.assertIsInstance(err["code"], str)
		self.assertIsInstance(err["message"], str)
		self.assertTrue(f"{err['code']}: {err['message']}".startswith("InvalidArgumentError: "))


class TestBadDateOnBothDrivers(FrappeTestCase):
	"""A date the database refuses (Frappe does not parse dates) is a FIXABLE
	InvalidArgumentError naming the field, on pymysql and on mysqlclient."""

	def _bad_insert(self):
		mark = api._msglog_mark()
		frappe.db.savepoint("j2a_bad_date")
		try:
			frappe.get_doc(
				{"doctype": "ToDo", "description": "j2a bad date", "date": "garbage-date"}
			).db_insert()
		except Exception as e:
			frappe.db.rollback(save_point="j2a_bad_date")
			return e, api._translate_write_error(e, mark)
		self.fail("the database accepted a bad date")

	def _assert_fixable(self, exc, envelope):
		self.assertEqual(exc.args[0], 1292, exc)
		self.assertEqual(
			(envelope["error"]["code"], envelope["error"]["kind"]), ("InvalidArgumentError", "fixable")
		)
		self.assertIn("Due Date (date)", envelope["error"]["message"])
		self.assertIn("garbage-date", envelope["error"]["message"])

	def test_the_sites_own_driver(self):
		self._assert_fixable(*self._bad_insert())

	def test_pymysql_driver(self):
		# v16 with ``use_mysqlclient: 0`` (and every v15 site) talks pymysql.
		from frappe.database.mariadb.database import MariaDBDatabase

		conf = frappe.local.conf
		db = MariaDBDatabase(
			socket=conf.db_socket,
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_user or conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		db.connect()
		original = frappe.local.db
		frappe.local.db = db
		try:
			exc, envelope = self._bad_insert()
		finally:
			frappe.local.db = original
			db.rollback()
			db.close()
		self.assertEqual(type(exc).__module__.split(".")[0], "pymysql")
		self._assert_fixable(exc, envelope)

	def test_mysqlclient_driver(self):
		try:
			from frappe.database.mariadb.mysqlclient import MariaDBDatabase
		except ImportError:
			self.skipTest("mysqlclient is not a driver on this Frappe version")
		conf = frappe.local.conf
		db = MariaDBDatabase(
			socket=conf.db_socket,
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_user or conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		db.connect()
		original = frappe.local.db
		frappe.local.db = db
		try:
			exc, envelope = self._bad_insert()
		finally:
			frappe.local.db = original
			db.rollback()
			db.close()
		self.assertEqual(type(exc).__module__.split(".")[0], "MySQLdb")
		self._assert_fixable(exc, envelope)


_CO = masters.COMPANY


class TestRealBusinessRulesAreNotFixable(FrappeTestCase):
	"""Real ERPNext saves (serial: one class, its own company / customer / items).
	Nothing commits: the class rolls back, and every global setting it touches is
	restored in a cleanup."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")
		frappe.set_user("Administrator")
		masters.clear_cache_after_rollback(cls)
		masters.ensure_ledger_company()
		masters.ensure_item("_J2A Stock Item", is_stock_item=1, valuation_rate=10)
		masters.ensure_item("_J2A Service Item", is_stock_item=0, valuation_rate=10)
		masters.ensure_stock_entry_type("Material Issue")
		# The journals post in Feb and Mar 2026; the order is dated today, delivered in 5 days.
		masters.ensure_fiscal_years("2026-02-15", "2026-03-15", today(), add_days(today(), 5))
		cls.customer = masters.ensure_customer("_J2A Credit Customer", credit_limit=100)
		cls.price_list = masters.ensure_selling_price_list()
		# With no one to extend a limit, ERPNext throws only "Please contact your administrator"
		# and drops the "Credit limit has been crossed" text; a fresh site has no such user.
		masters.ensure_credit_controller("j2a-credit-controller@example.com")
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": _CO, "warehouse_name": "Stores"})
		cls.allow_negative = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
		cls.addClassCleanup(
			frappe.db.set_single_value, "Stock Settings", "allow_negative_stock", cls.allow_negative
		)

	def _journal(self, posting_date: str) -> str:
		cash, income = frappe.db.get_value("Company", _CO, ["default_cash_account", "default_income_account"])
		return (
			frappe.get_doc(
				{
					"doctype": "Journal Entry",
					"company": _CO,
					"posting_date": posting_date,
					"accounts": [
						{"account": cash, "debit_in_account_currency": 50},
						{"account": income, "credit_in_account_currency": 50},
					],
				}
			)
			.insert()
			.name
		)

	def _submit(self, doctype: str, name: str) -> dict:
		r = _envelope("submit_doc", {"doctype": doctype, "name": name})
		self.assertFalse(r["ok"], r)
		self.assertEqual(frappe.db.get_value(doctype, name, "docstatus"), 0, "rolled back")
		return r["error"]

	def test_credit_limit(self):
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": _CO,
				"customer": self.customer,
				"selling_price_list": self.price_list,
				"transaction_date": today(),
				"delivery_date": add_days(today(), 5),
				"items": [{"item_code": "_J2A Service Item", "qty": 1, "rate": 1000}],
			}
		).insert()
		err = self._submit("Sales Order", so.name)
		self.assertIn("Credit limit", err["message"])
		self.assertEqual(err["kind"], "not_fixable")

	def test_insufficient_stock(self):
		from erpnext.stock.stock_ledger import NegativeStockError

		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": _CO,
				"stock_entry_type": "Material Issue",
				"items": [
					{
						"item_code": "_J2A Stock Item",
						"qty": 5,
						"s_warehouse": self.warehouse,
						"basic_rate": 10,
					}
				],
			}
		).insert()
		frappe.db.savepoint("j2a_stock_class")
		with self.assertRaises(NegativeStockError):
			frappe.get_doc("Stock Entry", se.name).submit()
		frappe.db.rollback(save_point="j2a_stock_class")
		err = self._submit("Stock Entry", se.name)
		self.assertIn("needed in Warehouse", err["message"])
		self.assertEqual(err["kind"], "not_fixable")

	def test_closed_accounting_period(self):
		from erpnext.accounts.doctype.accounting_period.accounting_period import ClosedAccountingPeriod

		je = self._journal("2026-02-15")
		frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "_J2A Feb",
				"company": _CO,
				"start_date": "2026-02-01",
				"end_date": "2026-02-28",
				"closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
			}
		).insert()
		frappe.db.savepoint("j2a_period_class")
		with self.assertRaises(ClosedAccountingPeriod):
			frappe.get_doc("Journal Entry", je).submit()
		frappe.db.rollback(save_point="j2a_period_class")
		err = self._submit("Journal Entry", je)
		self.assertIn("closed Accounting Period", err["message"])
		self.assertEqual(err["kind"], "not_fixable")

	def test_frozen_accounts_date(self):
		je = self._journal("2026-03-15")
		if frappe.get_meta("Company").has_field("accounts_frozen_till_date"):  # v16: per company
			before = frappe.db.get_value("Company", _CO, "accounts_frozen_till_date")
			self.addCleanup(frappe.db.set_value, "Company", _CO, "accounts_frozen_till_date", before)
			frappe.db.set_value("Company", _CO, "accounts_frozen_till_date", "2026-12-31")
		else:  # v15: one site-wide date
			before = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
			self.addCleanup(frappe.db.set_single_value, "Accounts Settings", "acc_frozen_upto", before)
			frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", "2026-12-31")
		err = self._submit("Journal Entry", je)
		self.assertIn("not authorized to add or update entries", err["message"])
		self.assertEqual(err["kind"], "not_fixable")


class TestReceiptCaps(FrappeTestCase):
	def test_failure_text_reaches_the_assistant_up_to_600_characters(self):
		long = "x" * 2000
		text = actions_api._confirm_receipt_text(
			{"tool": "submit_doc", "args": {"doctype": "ToDo", "name": "T-1"}},
			{"ok": False, "error": {"code": "InvalidArgumentError", "message": long}},
		)
		failure = text.split("FAILED. ", 1)[1]
		self.assertEqual(len(failure), actions_api.RECEIPT_ROW_CAP)
		self.assertTrue(failure.endswith("..."))

	def test_more_than_200_characters_survive(self):
		message = "Credit limit crossed. " + "y" * 300
		text = actions_api._confirm_receipt_text(
			{"tool": "submit_doc", "args": {"doctype": "ToDo", "name": "T-1"}},
			{"ok": False, "error": {"code": "InvalidArgumentError", "message": message}},
		)
		self.assertIn(message, text)

	def test_detail_is_included_when_it_adds_something(self):
		err = {"code": "InvalidArgumentError", "message": "Could not save.", "detail": "Row 2: qty missing"}
		self.assertEqual(actions_api._failure_text(err), "Could not save. Row 2: qty missing")
		self.assertEqual(actions_api._failure_text({"message": "Same", "detail": "Same"}), "Same")

	def test_total_cap_counts_the_rest(self):
		receipts = [f"row {i} FAILED. " + "z" * 580 for i in range(20)]
		joined = actions_api._join_capped(receipts)
		self.assertLessEqual(len(joined), actions_api.RECEIPT_TOTAL_CAP)
		self.assertRegex(joined, r"and \d+ more\.$")
		shown = joined.count("FAILED.")
		self.assertTrue(joined.endswith(f"and {20 - shown} more."))
		self.assertEqual(actions_api._join_capped(["a", "b"]), "a b")
		self.assertTrue(actions_api._join_capped(["q" * 9000]).startswith("q"), "the first always shows")


class TestOutcomeVocabulary(FrappeTestCase):
	def _prompt(self, receipt="the receipt", **kwargs):
		with patch.object(chat_api, "_enqueue_turn", return_value={}) as enq:
			chat_api.enqueue_continuation("conv-j2a-x", receipt, **kwargs)
		return enq.call_args.args[1]

	def test_each_outcome_selects_its_scaffold(self):
		self.assertIn("[System] Applied:", self._prompt(outcome="ok"))
		fixable = self._prompt(outcome="failed_fixable")
		self.assertIn("correct only the fields named in the failure", fixable)
		self.assertIn("NEW confirmation card", fixable)
		not_fixable = self._prompt(outcome="failed_not_fixable")
		self.assertIn("Do NOT automatically retry", not_fixable)
		self.assertIn("suggest what the user could do", not_fixable)
		self.assertNotIn("correct only", not_fixable)
		busy = self._prompt(outcome="failed_retry_later")
		self.assertIn("The system was busy (a lock or timeout); nothing was changed.", busy)
		self.assertIn("do not retry by yourself", busy)
		for outcome in ("unknown", "partial"):
			self.assertIn("could NOT be verified", self._prompt(outcome=outcome))
		for outcome in ("failed_fixable", "failed_not_fixable", "failed_retry_later", "unknown", "mixed"):
			self.assertNotIn("Applied:", self._prompt(outcome=outcome, applied=""), outcome)

	def test_an_unknown_outcome_raises(self):
		with self.assertRaises(ValueError):
			self._prompt(outcome="failed-fixable")

	def test_failed_is_a_deprecated_alias_of_not_fixable(self):
		self.assertEqual(self._prompt(failed=True), self._prompt(outcome="failed_not_fixable"))
		self.assertEqual(
			self._prompt(failed=True, applied="ran X"), self._prompt(outcome="mixed", applied="ran X")
		)

	def test_unknown_and_partial_win_over_failed(self):
		self.assertIn("could NOT be verified", self._prompt(failed=True, outcome="partial"))

	def test_mixed_lists_each_kind_in_its_own_data_span(self):
		p = self._prompt(
			receipt="unused",
			outcome="mixed",
			spans={
				"applied": "A ok",
				"fixable": "B FAILED. qty",
				"busy": "D FAILED. lock",
				"not_fixable": "C FAILED. closed period",
			},
		)
		self.assertNotIn("unused", p, "not_fixable takes the place of the receipt")
		for span in ("`A ok`", "`B FAILED. qty`", "`D FAILED. lock`", "`C FAILED. closed period`"):
			self.assertIn(span, p)
		self.assertIn("may be corrected once", p)
		none_ran = self._prompt(receipt="C FAILED. x", outcome="mixed", spans={"fixable": "B FAILED. y"})
		self.assertIn("None of the changes", none_ran)
		self.assertNotIn("Applied, quoted", none_ran)

	def test_mixed_without_kinds_keeps_the_old_scaffold(self):
		p = self._prompt(receipt="B FAILED.", outcome="mixed", spans={"applied": "A ok"})
		self.assertEqual(p, chat_api._CONTINUATION_PROMPT_MIXED.format(receipt="B FAILED.", applied="A ok"))
		self.assertEqual(
			p, self._prompt(receipt="B FAILED.", outcome="mixed", applied="A ok"), "deprecated alias"
		)

	def test_an_unknown_span_raises(self):
		with self.assertRaises(ValueError):
			self._prompt(outcome="mixed", spans={"fixabel": "x"})


def _item(name, status="Failed", outcome="failed", result=None, **extra):
	return {
		"name": name,
		"tool": "update_doc",
		"args": {"doctype": "ToDo", "name": name},
		"status": status,
		"outcome": outcome,
		"result": result,
		"reason_code": "" if status == "Executed" else "failed",
		**extra,
	}


FIXABLE_RESULT = {
	"ok": False,
	"error": {"code": "InvalidArgumentError", "kind": "fixable", "message": "Missing qty"},
}
STOCK_RESULT = {
	"ok": False,
	"error": {"code": "InvalidArgumentError", "kind": "not_fixable", "message": "stock"},
}
BUSY_RESULT = {"ok": False, "error": {"code": "RetryLaterError", "kind": "retry_later", "message": "busy"}}
OK_RESULT = {"ok": True, "data": {"name": "X"}}


class TestSettledPlan(FrappeTestCase):
	def plan(self, *items):
		return actions_api._settled_plan(list(items))

	def test_partial_and_unknown_win_over_a_fixable_failure(self):
		p = self.plan(_item("A", outcome="partial", result=FIXABLE_RESULT))
		self.assertEqual((p["outcome"], p["stamp"]), ("partial", []))
		p = self.plan(_item("A", outcome="unknown", result=BUSY_RESULT), _item("B", result=FIXABLE_RESULT))
		self.assertEqual((p["outcome"], p["stamp"]), ("unknown", []))
		p = self.plan(_item("A", "Executed", "confirmed", OK_RESULT), _item("B", outcome="partial"))
		self.assertEqual(p["outcome"], "partial")

	def test_one_kind(self):
		self.assertEqual(self.plan(_item("A", result=FIXABLE_RESULT))["outcome"], "failed_fixable")
		self.assertEqual(self.plan(_item("A", result=STOCK_RESULT))["outcome"], "failed_not_fixable")
		self.assertEqual(self.plan(_item("A", result=BUSY_RESULT))["outcome"], "failed_retry_later")
		self.assertEqual(self.plan(_item("A", result=None))["outcome"], "failed_not_fixable")
		self.assertEqual(self.plan(_item("A", "Executed", "confirmed", OK_RESULT))["outcome"], "ok")

	def test_a_fixable_failure_is_stamped(self):
		self.assertEqual(self.plan(_item("A", result=FIXABLE_RESULT))["stamp"], ["A"])
		self.assertEqual(self.plan(_item("A", result=STOCK_RESULT))["stamp"], [])

	def test_a_correction_that_fails_is_not_fixable(self):
		p = self.plan(_item("B", result=FIXABLE_RESULT, corrects="A"))
		self.assertEqual((p["outcome"], p["stamp"]), ("failed_not_fixable", []))

	def test_a_late_settle_is_not_fixable(self):
		p = self.plan(_item("A", result=FIXABLE_RESULT, late=True))
		self.assertEqual((p["outcome"], p["stamp"]), ("failed_not_fixable", []))

	def test_a_busy_correction_or_late_settle_stays_busy(self):
		# Only FIXABLE is downgraded: try-again-later is still true.
		for extra in ({"corrects": "A"}, {"late": True}):
			with self.subTest(extra=extra):
				self.assertEqual(
					self.plan(_item("B", result=BUSY_RESULT, **extra))["outcome"], "failed_retry_later"
				)

	def test_a_correction_that_runs_carries_the_stamp(self):
		p = self.plan(_item("B", "Executed", "confirmed", OK_RESULT, corrects="A"))
		self.assertEqual((p["outcome"], p["stamp"]), ("ok", ["B"]))

	def test_a_refusal_is_never_fixable(self):
		refused = {"ok": False, "error": {"code": "structure_refused", "kind": "fixable", "message": "Desk"}}
		self.assertEqual(self.plan(_item("A", result=refused))["outcome"], "failed_not_fixable")

	def test_mixed(self):
		p = self.plan(
			_item("A", "Executed", "confirmed", OK_RESULT),
			_item("B", result=FIXABLE_RESULT),
			_item("C", result=STOCK_RESULT),
			_item("D", result=BUSY_RESULT),
		)
		self.assertEqual(p["outcome"], "mixed")
		spans = p["spans"]
		self.assertIn("name=B ", spans["fixable"])
		self.assertIn("name=C ", spans["not_fixable"])
		self.assertIn("name=D ", spans["busy"])
		self.assertIn("name=A ", spans["applied"])
		self.assertEqual(p["receipt"], spans["not_fixable"])
		self.assertEqual(p["stamp"], ["B"])
		p = self.plan(_item("B", result=FIXABLE_RESULT), _item("C", result=STOCK_RESULT))
		self.assertEqual((p["outcome"], p["spans"]["applied"]), ("mixed", ""))

	def test_one_cap_across_every_failure_span(self):
		import re

		long = {"ok": False, "error": {"code": "InvalidArgumentError", "message": "m" * 590}}
		busy = {**long, "error": {**long["error"], "code": "RetryLaterError", "kind": "retry_later"}}
		fix = {**long, "error": {**long["error"], "kind": "fixable"}}
		items = [_item(f"F{i}", result=fix) for i in range(4)]
		items += [_item(f"B{i}", result=busy) for i in range(4)]
		items += [_item(f"N{i}", result=long) for i in range(4)]
		spans = self.plan(*items)["spans"]
		failures = " ".join(spans[k] for k in ("fixable", "busy", "not_fixable"))
		self.assertLessEqual(len(failures), actions_api.RECEIPT_TOTAL_CAP + 2)
		hidden = 0
		for key in ("fixable", "busy", "not_fixable"):
			with self.subTest(span=key):
				self.assertIn("FAILED.", spans[key], "no span is dropped entirely")
				shown = spans[key].count("FAILED.")
				more = re.search(r"and (\d+) more\.$", spans[key])
				self.assertEqual(
					int(more.group(1)) if more else 0, 4 - shown, "overflow counted in its own span"
				)
				hidden += 4 - shown
		self.assertGreater(hidden, 0)

	def test_a_small_span_after_a_large_one_still_shows(self):
		long = {"ok": False, "error": {"code": "InvalidArgumentError", "message": "m" * 590}}
		fix = {**long, "error": {**long["error"], "kind": "fixable"}}
		busy = {"ok": False, "error": {"code": "RetryLaterError", "kind": "retry_later", "message": "busy"}}
		items = [_item(f"F{i}", result=fix) for i in range(12)] + [_item("B0", result=busy)]
		spans = self.plan(*items)["spans"]
		self.assertIn("name=B0 ", spans["busy"])
		self.assertRegex(spans["fixable"], r"and \d+ more\.$")


class TestEveryContinuationCallerIsPinned(FrappeTestCase):
	"""Every ``enqueue_continuation`` call site, by module and function, and whether it
	passes ``outcome=``. A new caller, or one passing the deprecated ``failed=``, fails."""

	EXPECTED = [
		("jarvis/chat/actions_api.py", "apply_action", False),  # an ok draft-panel apply (File Box)
		("jarvis/chat/actions_api.py", "apply_action", False),  # an ok draft-panel apply
		("jarvis/chat/actions_api.py", "approve_and_run", True),
		("jarvis/chat/actions_api.py", "_confirm_core", True),
		("jarvis/chat/actions_api.py", "on_chat_settled", True),
		("jarvis/chat/api.py", "_confirm_typed_items", True),
		("jarvis/chat/panel_failure.py", "_continue", True),  # a failed draft-panel save (R2-9)
	]
	NAMES = {"enqueue_continuation", "_enqueue_cont"}

	def test_callers(self):
		root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		found = []
		for dirpath, _dirs, files in os.walk(root):
			if "/tests" in dirpath.replace(root, ""):
				continue
			for fname in files:
				if not fname.endswith(".py"):
					continue
				path = os.path.join(dirpath, fname)
				rel = "jarvis/" + os.path.relpath(path, root)
				tree = ast.parse(open(path).read())
				for func in ast.walk(tree):
					if not isinstance(func, ast.FunctionDef):
						continue
					for node in ast.walk(func):
						if not isinstance(node, ast.Call):
							continue
						callee = (
							node.func.attr
							if isinstance(node.func, ast.Attribute)
							else getattr(node.func, "id", "")
						)
						if callee not in self.NAMES:
							continue
						kwargs = {k.arg for k in node.keywords}
						self.assertNotIn(
							"failed", kwargs, f"{rel}:{node.lineno} passes the deprecated failed="
						)
						found.append((rel, func.name, "outcome" in kwargs))
		self.assertEqual(sorted(found), sorted(self.EXPECTED))
		self.assertEqual(len(found), 7)


class TestTypedBulkEnvelope(FrappeTestCase):
	def test_a_non_dict_confirmation_is_enveloped_with_code(self):
		with (
			patch("jarvis.chat.actions_api._confirm_core", return_value=None),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			out = chat_api._confirm_typed_items("conv-j2a", [{"token": "t1", "position": 1}])
		self.assertEqual(out["error"], {"code": "InternalError", "message": "confirmation failed"})

	def test_a_legacy_confirm_never_offers_a_correction(self):
		# No Pending Action row to stamp, so "once" could not be enforced.
		self.assertEqual(actions_api._legacy_outcome(FIXABLE_RESULT), "failed_not_fixable")
		self.assertEqual(actions_api._legacy_outcome(BUSY_RESULT), "failed_retry_later")
		self.assertEqual(actions_api._legacy_outcome(STOCK_RESULT), "failed_not_fixable")
		self.assertEqual(actions_api._legacy_outcome({"ok": True}), "ok")
		self.assertEqual(actions_api._legacy_outcome(None), "failed_not_fixable")

	def test_legacy_batch_outcome(self):
		ok = ("A ok", {"ok": True})
		fixable = ("B FAILED.", FIXABLE_RESULT)
		busy = ("C FAILED.", BUSY_RESULT)
		self.assertEqual(chat_api._legacy_batch_continuation([ok])[1], "ok")
		# A legacy card has no row to stamp: a fixable failure is not offered a correction.
		self.assertEqual(chat_api._legacy_batch_continuation([fixable])[1], "failed_not_fixable")
		self.assertEqual(chat_api._legacy_batch_continuation([busy])[1], "failed_retry_later")
		self.assertEqual(chat_api._legacy_batch_continuation([ok, busy])[1], "failed_not_fixable")
