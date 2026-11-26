"""Payment Calendar S4b B1: the three cache DocTypes (plan D-3, section 8) and their deny functions.

The tables hold derived, shared data. No user but Administrator may read or write them, even when a
Custom DocPerm grants a role. The deny functions live in ``jarvis.permissions``; ``hooks.py`` registers
them since task B7. The end-to-end checks here patch ``frappe.get_hooks`` both ways: the controls drop the
deny functions to show that the grant alone opens a table; the denial tests add them (the real hooks.py
registration is tested in ``test_payment_calendar_cache_hooks``).

The DocTypes reach ``test_jarvis`` through ``reload_doc`` only (never a migrate of a shared site).
Nothing here commits: every test runs in a savepoint that tearDown rolls back.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_doctypes
"""

import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import permissions

HEAD = "Jarvis Payment Calendar Cache Head"
GENERATION = "Jarvis Payment Calendar Cache Generation"
ROW = "Jarvis Payment Calendar Cache Row"
CACHE_DOCTYPES = (HEAD, GENERATION, ROW)
USER = "pc-cache-doctypes-user@example.com"
MANAGER = "pc-cache-doctypes-manager@example.com"
SAVEPOINT = "pcal_cache_doctypes"
HAS_PERMISSION_HOOK = "jarvis.permissions.deny_cache_table_permission"
QUERY_HOOK = "jarvis.permissions.deny_cache_table_query_conditions"


def _index_columns(doctype: str) -> dict[str, list[str]]:
	"""Column list of every index of the table, by index name, in index order."""
	indexes: dict[str, list[tuple[int, str]]] = {}
	for row in frappe.db.sql(f"show index from `tab{doctype}`", as_dict=True):
		indexes.setdefault(row["Key_name"], []).append((row["Seq_in_index"], row["Column_name"]))
	return {name: [column for _, column in sorted(columns)] for name, columns in indexes.items()}


def _is_unique(doctype: str, index_name: str) -> bool:
	rows = frappe.db.sql(f"show index from `tab{doctype}`", as_dict=True)
	return any(row["Key_name"] == index_name and not row["Non_unique"] for row in rows)


def _head(**fields) -> dict:
	return {
		"doctype": HEAD,
		"name": fields.pop("name", frappe.generate_hash(length=12)),
		"company": "Cache Meta Test Co",
		"side": "Receivable",
		**fields,
	}


def _ensure_user(email: str, role: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "pccache",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(role)


def _hooks_with_deny(real_get_hooks):
	"""`frappe.get_hooks` with the deny functions on all three DocTypes (hooks.py registers them since B7)."""

	def get_hooks(hook=None, default=None, app_name=None):
		found = real_get_hooks(hook, default, app_name)
		if hook not in ("has_permission", "permission_query_conditions"):
			return found
		function = HAS_PERMISSION_HOOK if hook == "has_permission" else QUERY_HOOK
		merged = {**found}
		for doctype in CACHE_DOCTYPES:
			kept = [path for path in _as_list(merged.get(doctype)) if path != function]
			merged[doctype] = [*kept, function]
		return merged

	return get_hooks


def _hooks_without_deny(real_get_hooks):
	"""`frappe.get_hooks` without the deny functions: the control that shows the grant alone opens the table."""

	def get_hooks(hook=None, default=None, app_name=None):
		found = real_get_hooks(hook, default, app_name)
		if hook not in ("has_permission", "permission_query_conditions"):
			return found
		function = HAS_PERMISSION_HOOK if hook == "has_permission" else QUERY_HOOK
		merged = {**found}
		for doctype in CACHE_DOCTYPES:
			merged[doctype] = [path for path in _as_list(merged.get(doctype)) if path != function]
		return merged

	return get_hooks


def _as_list(value) -> list:
	return [value] if isinstance(value, str) else list(value or [])


class CacheDocTypeCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for name in ("head", "generation", "row"):
			frappe.reload_doc("jarvis", "doctype", f"jarvis_payment_calendar_cache_{name}")
		for doctype in CACHE_DOCTYPES:
			frappe.clear_cache(doctype=doctype)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=SAVEPOINT)
		for doctype in CACHE_DOCTYPES:
			frappe.clear_cache(doctype=doctype)
		frappe.local.role_permissions = {}


class TestCacheDocTypeMeta(CacheDocTypeCase):
	def test_all_three_live_in_the_jarvis_module(self):
		for doctype in CACHE_DOCTYPES:
			self.assertEqual(frappe.get_meta(doctype).module, "Jarvis", doctype)

	def test_no_json_declares_a_permission_row(self):
		for doctype in CACHE_DOCTYPES:
			path = Path(frappe.get_app_path("jarvis", "jarvis", "doctype", frappe.scrub(doctype)))
			self.assertEqual(json.loads((path / f"{path.name}.json").read_text())["permissions"], [], doctype)

	def test_the_only_stored_permission_row_is_the_default_frappe_adds(self):
		# Frappe adds a System Manager row to a DocType with none (doctype.py before_insert). The deny
		# hooks close it (TestCustomPermissionStaysDenied); no other role may hold a row.
		for doctype in CACHE_DOCTYPES:
			roles = frappe.get_all("DocPerm", filters={"parent": doctype}, pluck="role")
			self.assertEqual(set(roles) - {"System Manager"}, set(), doctype)

	def test_all_three_are_closed_to_the_desk(self):
		for doctype in CACHE_DOCTYPES:
			meta = frappe.get_meta(doctype)
			self.assertEqual(
				(meta.track_changes, meta.in_create, meta.read_only, meta.istable, meta.issingle),
				(0, 1, 1, 0, 0),
				doctype,
			)

	def test_company_is_plain_data_and_no_field_links_to_another_record(self):
		# A Link would put the table under Company deletion rules and the TDR auto-delete (D-3).
		for doctype in CACHE_DOCTYPES:
			meta = frappe.get_meta(doctype)
			if meta.has_field("company"):
				self.assertEqual(meta.get_field("company").fieldtype, "Data", doctype)
			linked = [f.fieldname for f in meta.fields if f.fieldtype in ("Link", "Dynamic Link", "Table")]
			self.assertEqual(linked, [], doctype)

	def test_head_and_generation_carry_company_creation_and_row_carries_neither(self):
		self.assertTrue(frappe.get_meta(HEAD).has_field("company_creation"))
		self.assertTrue(frappe.get_meta(GENERATION).has_field("company_creation"))
		self.assertFalse(frappe.get_meta(ROW).has_field("company"))

	def test_head_holds_the_durable_state_of_d3(self):
		meta = frappe.get_meta(HEAD)
		for field in (
			"company",
			"side",
			"company_creation",
			"current_generation",
			"claim_token",
			"claim_expires",
			"purge_epoch",
			"hop_seq",
			"memo_until",
			"backoff_level",
			"failures",
			"last_failure",
			"last_opened",
			"last_success",
			"last_duration_ms",
			"last_rows",
			"peak_rss_mb",
			"max_age_s_today",
		):
			self.assertTrue(meta.has_field(field), field)
		self.assertEqual(meta.get_field("side").options.split("\n"), ["Receivable", "Payable", "Bank"])

	def test_generation_holds_the_states_and_evidence_of_d3(self):
		meta = frappe.get_meta(GENERATION)
		self.assertEqual(
			meta.get_field("state").options.split("\n"),
			["building", "ready", "current", "retired", "failed", "purged"],
		)
		for field in (
			"company",
			"side",
			"anchor",
			"as_of",
			"deadline",
			"cache_build",
			"row_count",
			"bytes",
			"checksum",
			"payload",
			"peak_rss_mb",
			"read_view_ms",
			"max_write_txn_ms",
			"failure_class",
			"hook_fingerprint",
		):
			self.assertTrue(meta.has_field(field), field)

	def test_generation_keeps_the_ledger_lines_of_its_build_as_an_int(self):
		field = frappe.get_meta(GENERATION).get_field("ledger_lines")
		self.assertEqual(field.fieldtype, "Int")

	def test_row_holds_every_d2_field_with_amounts_as_text(self):
		meta = frappe.get_meta(ROW)
		for field in ("original", "paid", "credit", "discount", "remaining", "account_remaining"):
			self.assertEqual(meta.get_field(field).fieldtype, "Data", field)
		for field in (
			"generation",
			"row_id",
			"voucher_type",
			"voucher_no",
			"party_type",
			"party",
			"party_name",
			"account",
			"posting_date",
			"due_date",
			"term",
			"allocation_basis",
			"allocation_reason",
			"settlement",
			"kind",
			"permission",
			"residue",
			"hide_in_window",
			"window_refusal",
			"window_due_dates",
		):
			self.assertTrue(meta.has_field(field), field)
		self.assertEqual(meta.get_field("permission").fieldtype, "JSON")
		self.assertEqual(meta.get_field("window_due_dates").fieldtype, "JSON")


class TestCacheDocTypeTables(CacheDocTypeCase):
	def test_head_company_side_is_unique(self):
		self.assertTrue(_is_unique(HEAD, "company_side"))
		self.assertEqual(_index_columns(HEAD)["company_side"], ["company", "side"])
		frappe.get_doc(_head()).db_insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(_head()).db_insert()

	def test_head_accepts_the_same_company_on_another_side(self):
		frappe.get_doc(_head()).db_insert()
		frappe.get_doc(_head(side="Payable")).db_insert()
		self.assertEqual(frappe.db.count(HEAD, {"company": "Cache Meta Test Co"}), 2)

	def test_generation_names_come_from_a_sequence(self):
		first = frappe.get_doc(
			{"doctype": GENERATION, "company": "Cache Meta Test Co", "side": "Bank", "state": "building"}
		).insert(ignore_permissions=True)
		second = frappe.get_doc(
			{"doctype": GENERATION, "company": "Cache Meta Test Co", "side": "Bank", "state": "building"}
		).insert(ignore_permissions=True)
		self.assertTrue(str(first.name).isdigit())
		self.assertGreater(int(second.name), int(first.name))

	def test_generation_has_an_index_for_the_side_janitor(self):
		self.assertEqual(_index_columns(GENERATION)["company_side_state"], ["company", "side", "state"])

	def test_generation_keeps_a_short_failure_trace(self):
		field = frappe.get_meta(GENERATION).get_field("failure_trace")
		self.assertEqual(field.fieldtype, "Long Text")

	def test_generation_has_the_indexes_of_the_tick_and_the_status(self):
		columns = _index_columns(GENERATION)
		self.assertEqual(columns["state_deadline"], ["state", "deadline"])
		self.assertEqual(columns["state_modified"], ["state", "modified"])
		self.assertEqual(columns["as_of"], ["as_of"])

	def test_the_index_hooks_are_idempotent(self):
		from jarvis.jarvis.doctype.jarvis_payment_calendar_cache_generation import (
			jarvis_payment_calendar_cache_generation as generation,
		)
		from jarvis.jarvis.doctype.jarvis_payment_calendar_cache_row import (
			jarvis_payment_calendar_cache_row as row,
		)

		before = _index_columns(GENERATION), _index_columns(ROW)
		for _ in range(2):
			generation.on_doctype_update()
			row.on_doctype_update()
		self.assertEqual((_index_columns(GENERATION), _index_columns(ROW)), before)

	def test_row_indexes_lead_with_the_generation(self):
		columns = _index_columns(ROW)
		self.assertEqual(columns["generation_due_date_name"], ["generation", "due_date", "name"])
		self.assertEqual(columns["generation_party_name_name"], ["generation", "party_name", "name"])

	def test_row_name_is_set_by_the_writer(self):
		name = f"{7:012d}{1:08d}"
		frappe.get_doc(
			{
				"doctype": ROW,
				"name": name,
				"generation": 7,
				"row_id": "a" * 64,
				"remaining": "10.00",
				"permission": "{}",
				"window_due_dates": "[]",
			}
		).db_insert()
		self.assertEqual(frappe.db.get_value(ROW, name, "remaining"), "10.00")
		self.assertEqual(name, "000000000007" + "00000001")

	def test_the_controllers_refuse_a_rename(self):
		frappe.get_doc(_head(name="rename-me")).db_insert()
		with self.assertRaises(Exception):
			frappe.rename_doc(HEAD, "rename-me", "renamed", force=True)


class TestDenyFunctions(CacheDocTypeCase):
	def test_only_administrator_passes_has_permission(self):
		self.assertTrue(permissions.deny_cache_table_permission(doc=None, ptype="read", user="Administrator"))
		for user in (USER, "Guest", "system-manager@example.com"):
			self.assertFalse(permissions.deny_cache_table_permission(doc=None, ptype="read", user=user), user)

	def test_has_permission_defaults_to_the_session_user(self):
		self.assertTrue(permissions.deny_cache_table_permission(doc=None, ptype="read"))
		frappe.set_user("Guest")
		self.assertFalse(permissions.deny_cache_table_permission(doc=None, ptype="read"))

	def test_query_condition_is_empty_for_administrator_and_closed_for_everyone_else(self):
		self.assertEqual(permissions.deny_cache_table_query_conditions("Administrator"), "")
		self.assertEqual(permissions.deny_cache_table_query_conditions(USER), "1=0")
		self.assertEqual(permissions.deny_cache_table_query_conditions("Guest"), "1=0")


class TestCustomPermissionStaysDenied(CacheDocTypeCase):
	"""A Custom DocPerm read grant for Accounts User still returns 0 rows (plan section 8)."""

	def setUp(self):
		super().setUp()
		_ensure_user(USER, "Accounts User")
		for doctype in CACHE_DOCTYPES:
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": "Accounts User",
					"permlevel": 0,
					"read": 1,
				}
			).insert(ignore_permissions=True)
			frappe.clear_cache(doctype=doctype)
		frappe.local.role_permissions = {}
		self.head = _head()["name"]
		frappe.get_doc(_head(name=self.head)).db_insert()

	def test_the_grant_alone_lets_the_user_read(self):
		# The control: without the deny hooks the Custom DocPerm works, so the denial below is the hooks'.
		with patch("frappe.get_hooks", side_effect=_hooks_without_deny(frappe.get_hooks)):
			frappe.set_user(USER)
			self.assertEqual(len(frappe.get_list(HEAD, pluck="name")), 1)

	def test_the_deny_hooks_close_list_and_document_reads(self):
		real = frappe.get_hooks
		with patch("frappe.get_hooks", side_effect=_hooks_with_deny(real)):
			frappe.set_user(USER)
			self.assertEqual(frappe.get_list(HEAD, pluck="name"), [])
			self.assertFalse(frappe.has_permission(HEAD, "read", doc=frappe.get_doc(HEAD, self.head)))

	def test_the_deny_hooks_leave_administrator_alone(self):
		real = frappe.get_hooks
		with patch("frappe.get_hooks", side_effect=_hooks_with_deny(real)):
			self.assertEqual(len(frappe.get_list(HEAD, pluck="name")), 1)


class TestDefaultSystemManagerRowStaysDenied(CacheDocTypeCase):
	"""Frappe adds a System Manager DocPerm to a DocType with none; the deny hooks close it."""

	def setUp(self):
		super().setUp()
		_ensure_user(MANAGER, "System Manager")
		self.head = _head()["name"]
		frappe.get_doc(_head(name=self.head)).db_insert()

	def test_the_default_row_alone_lets_a_system_manager_read(self):
		with patch("frappe.get_hooks", side_effect=_hooks_without_deny(frappe.get_hooks)):
			frappe.set_user(MANAGER)
			self.assertEqual(len(frappe.get_list(HEAD, pluck="name")), 1)

	def test_the_deny_hooks_close_list_and_document_reads(self):
		real = frappe.get_hooks
		with patch("frappe.get_hooks", side_effect=_hooks_with_deny(real)):
			frappe.set_user(MANAGER)
			self.assertEqual(frappe.get_list(HEAD, pluck="name"), [])
			self.assertFalse(frappe.has_permission(HEAD, "read", doc=frappe.get_doc(HEAD, self.head)))


class TestLedgerLinesMigration(FrappeTestCase):
	"""E10-3: the migrate adds `ledger_lines` to a generation table that already holds rows.

	A migrate reloads a DocType whose JSON hash differs from the stored `migration_hash` (and, without a hash,
	whose JSON `modified` is newer). The test drops the column, puts back the hash and `modified` of the release
	before the column, and reloads the way the migrate does. It commits (a DDL statement does) and removes its
	own rows at the end.
	"""

	COMPANY = "Cache Ledger Lines Test Co"
	BEFORE_THE_COLUMN = "2026-11-25 10:00:00.000000"

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(self.restore)
		self.restore()

	def restore(self):
		frappe.db.rollback()
		frappe.db.sql(f"delete from `tab{GENERATION}` where company = %s", (self.COMPANY,))
		frappe.db.commit()
		self.reload(force=True)

	def reload(self, *, force=False):
		frappe.reload_doc("jarvis", "doctype", "jarvis_payment_calendar_cache_generation", force=force)
		frappe.clear_cache(doctype=GENERATION)

	def has_column(self) -> bool:
		return bool(frappe.db.sql(f"show columns from `tab{GENERATION}` like 'ledger_lines'"))

	def test_the_json_is_newer_than_the_release_before_the_column(self):
		path = Path(frappe.get_app_path("jarvis", "jarvis", "doctype", frappe.scrub(GENERATION)))
		modified = json.loads((path / f"{path.name}.json").read_text())["modified"]
		self.assertGreater(modified, self.BEFORE_THE_COLUMN)

	def test_a_reload_adds_the_column_to_a_populated_table_and_keeps_its_rows(self):
		frappe.get_doc(
			{"doctype": GENERATION, "company": self.COMPANY, "side": "Bank", "state": "retired"}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.db.sql_ddl(f"alter table `tab{GENERATION}` drop column ledger_lines")
		frappe.db.set_value(
			"DocType",
			GENERATION,
			{"modified": self.BEFORE_THE_COLUMN, "migration_hash": "release-before-the-column"},
			update_modified=False,
		)
		frappe.db.commit()
		self.assertFalse(self.has_column())
		self.reload()
		self.assertTrue(self.has_column())
		self.assertEqual(
			frappe.db.sql(f"select ledger_lines from `tab{GENERATION}` where company = %s", (self.COMPANY,)),
			((0,),),
		)

	def test_a_second_reload_changes_nothing(self):
		self.reload(force=True)
		self.reload(force=True)
		self.assertEqual(len(frappe.db.sql(f"show columns from `tab{GENERATION}` like 'ledger_lines'")), 1)
