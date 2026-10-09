"""A full (sensitive or structure) card shows a child table as it will be STORED.

The write tools read a value before they save it (``_field_values.check_values``:
"yes" is 1, "1,000" is 1000, "2026-10-3" is 2026-10-03), Frappe casts numbers and
trims a Select, a kept row keeps the cells the call does not name, a cell above the
requester's permission level is dropped, and a Password cell that is all asterisks
is left alone. The card drew the call's raw rows instead, so a row sent as
``mandatory: "yes"`` read "No" and was stored as 1.

Each case here builds the card, then makes the SAME call through the write tool and
compares the card's cells with the stored row.

A scratch ``custom=1`` form with a child table (made in ``setUpClass`` under unique
names, dropped with its tables afterwards) is marked sensitive for the test only.
SERIAL ONLY: it commits its fixtures, so run one copy per site at a time.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, cstr, flt

from jarvis.chat import confirm_card
from jarvis.chat.confirm_card import build_card
from jarvis.tests._pending_action_helpers import SM_USER, as_user, ensure_user
from jarvis.tools import _write_risk as wr
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc

SENSITIVE = {"risk": "sensitive", "risk_line": "This changes who can see or edit."}
# What a caller may send for a Check, and what the save stores.
CHECKS = (
	("yes", 1),
	("true", 1),
	("on", 1),
	("Y", 1),
	("no", 0),
	("false", 0),
	(True, 1),
	(1, 1),
	("1", 1),
	(" 1 ", 1),
	(0, 0),
)


class TestCardShowsWhatIsStored(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_user(SM_USER, ("Jarvis User", "System Manager"))
		tag = uuid.uuid4().hex[:6]
		cls.child, cls.form = f"Jcs Row {tag}", f"Jcs Form {tag}"
		cls.addClassCleanup(cls._drop)
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": cls.child,
				"module": "Custom",
				"custom": 1,
				"istable": 1,
				"fields": [
					{"fieldname": "label", "label": "Label", "fieldtype": "Data", "in_list_view": 1},
					{"fieldname": "flag", "label": "Flag", "fieldtype": "Check"},
					{"fieldname": "count", "label": "Count", "fieldtype": "Int"},
					{"fieldname": "ratio", "label": "Ratio", "fieldtype": "Float"},
					{"fieldname": "amount", "label": "Amount", "fieldtype": "Currency"},
					{"fieldname": "due", "label": "Due", "fieldtype": "Date"},
					{"fieldname": "kind", "label": "Kind", "fieldtype": "Select", "options": "Open\nHeld"},
					{"fieldname": "locked", "label": "Locked", "fieldtype": "Data", "permlevel": 1},
					{"fieldname": "pass_key", "label": "Pass Key", "fieldtype": "Password"},
					{"fieldname": "api_token", "label": "Api Token", "fieldtype": "Data"},
				],
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": cls.form,
				"module": "Custom",
				"custom": 1,
				"autoname": "field:title",
				"fields": [
					{"fieldname": "title", "label": "Title", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "is_group", "label": "Is Group", "fieldtype": "Check"},
					{"fieldname": "secret_code", "label": "Secret Code", "fieldtype": "Password"},
					{"fieldname": "access_token", "label": "Access Token", "fieldtype": "Data"},
					{"fieldname": "rows", "label": "Rows", "fieldtype": "Table", "options": cls.child},
				],
				# Level 0 only: nobody but Administrator writes the child's level-1 cell.
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def _drop(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for dt in (cls.form, cls.child):
			frappe.delete_doc_if_exists("DocType", dt, force=True)
			frappe.db.delete("Deleted Document", {"deleted_doctype": "DocType", "deleted_name": dt})
			frappe.db.delete("__Auth", {"doctype": dt})
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{dt}`")
			frappe.clear_cache(doctype=dt)
		frappe.db.delete("Comment", {"reference_doctype": "DocType", "reference_name": ["like", "Jcs %"]})
		if frappe.db.table_exists("Permission Log"):
			frappe.db.delete("Permission Log", {"for_document": ["in", [cls.form, cls.child]]})
		frappe.db.commit()
		for dt in (cls.form, cls.child):
			assert not frappe.db.exists("DocType", dt), f"{dt} was left behind"
			assert not frappe.db.sql("SHOW TABLES LIKE %s", f"tab{dt}"), f"tab{dt} was left behind"

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# The scratch form stands for any sensitive doctype, for this test only.
		marked = patch.dict(wr.SENSITIVE_DOCTYPES, {self.form: "access"})
		marked.start()
		self.addCleanup(marked.stop)
		self.addCleanup(self._wipe)

	def _wipe(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.delete(self.child)
		frappe.db.delete(self.form)
		frappe.db.delete("__Auth", {"doctype": self.child})
		frappe.db.commit()

	# -- helpers ---------------------------------------------------------------
	def make(self, title: str, rows: list) -> "frappe.model.document.Document":
		doc = frappe.get_doc({"doctype": self.form, "title": title, "rows": rows}).insert()
		frappe.db.commit()
		return doc

	def stored(self, title: str) -> list[dict]:
		"""The rows as the write left them (same transaction; ``_wipe`` rolls back)."""
		return frappe.get_all(
			self.child, filters={"parent": title}, fields=["*"], order_by="idx", ignore_permissions=True
		)

	def cells(self, table: dict) -> list[dict]:
		"""A card table as one dict per row: fieldname -> the cell as drawn."""
		return [dict(zip(table["fieldnames"], r["cells"], strict=True)) for r in table["rows"]]

	def shown(self, value, fieldtype: str) -> str:
		"""How the card draws a STORED value of ``fieldtype``."""
		df = frappe._dict(fieldtype=fieldtype, fieldname="x", options="")
		return confirm_card.fmt_full(value, df)

	def assert_matches_stored(self, drawn: dict, row: dict, fields: tuple) -> None:
		types = {df.fieldname: df.fieldtype for df in frappe.get_meta(self.child).fields}
		for fieldname in fields:
			self.assertEqual(
				drawn.get(fieldname, ""),
				self.shown(row.get(fieldname), types[fieldname]),
				f"{fieldname}: the card's cell is the stored cell",
			)

	def update_card(self, title: str, rows: list) -> dict:
		args = {"doctype": self.form, "name": title, "changes": {"rows": rows}}
		with as_user(SM_USER):
			return build_card("update_doc", args, {"would": {"rows": rows}, **SENSITIVE})

	def table_diff(self, card: dict) -> dict:
		return next(d for d in card["diff"] if d["label"] == "Rows")

	# -- M1: words, padding, grouping --------------------------------------------
	def test_every_spelling_of_a_check_is_drawn_as_it_is_stored(self):
		for sent, stored in CHECKS:
			with self.subTest(sent=sent):
				title = f"chk-{uuid.uuid4().hex[:6]}"
				values = {"title": title, "rows": [{"label": "a", "flag": sent}]}
				card = build_card("create_doc", {"doctype": self.form, "values": values}, dict(SENSITIVE))
				drawn = self.cells(card["tables"][0])[0]
				with as_user(SM_USER):
					create_doc(self.form, {"title": title, "rows": [{"label": "a", "flag": sent}]})
				row = self.stored(title)[0]
				self.assertEqual(row.flag, stored)
				self.assertEqual(drawn["flag"], "Yes" if stored else "No", "the card says what was stored")

	def test_a_create_card_draws_numbers_dates_and_selects_as_stored(self):
		row = {
			"label": "a",
			"flag": "yes",
			"count": "1,000",
			"ratio": "1,234.5",
			"amount": " 7 ",
			"due": "2026-10-3",
			"kind": " Held ",
		}
		values = {"title": "create-1", "rows": [dict(row)]}
		card = build_card("create_doc", {"doctype": self.form, "values": values}, dict(SENSITIVE))
		drawn = self.cells(card["tables"][0])[0]
		with as_user(SM_USER):
			create_doc(self.form, {"title": "create-1", "rows": [dict(row)]})
		stored = self.stored("create-1")[0]
		self.assertEqual(
			(stored.flag, stored.count, flt(stored.ratio), flt(stored.amount), cstr(stored.due), stored.kind),
			(1, 1000, 1234.5, 7.0, "2026-10-03", "Held"),
		)
		self.assert_matches_stored(drawn, stored, ("flag", "count", "ratio", "amount", "due", "kind"))
		self.assertEqual((drawn["flag"], drawn["count"], drawn["kind"]), ("Yes", "1000", "Held"))

	def test_an_update_card_draws_kept_changed_and_new_rows_as_stored(self):
		doc = self.make("upd-1", [{"label": "kept", "flag": 0, "count": 5}, {"label": "changed", "flag": 0}])
		kept, changed = (r.name for r in doc.rows)
		rows = [
			{"name": kept},
			{"name": changed, "flag": "yes", "count": " 7 ", "kind": " Open "},
			{"label": "new", "flag": "true", "count": "1,000", "ratio": "1,234.5", "due": "2026-10-3"},
		]
		card = self.update_card("upd-1", [dict(r) for r in rows])
		drawn = self.cells(self.table_diff(card)["to_table"])
		with as_user(SM_USER):
			update_doc(self.form, "upd-1", {"rows": [dict(r) for r in rows]})
		stored = self.stored("upd-1")
		self.assertEqual(
			[(r.label, r.flag, r.count) for r in stored],
			[("kept", 0, 5), ("changed", 1, 7), ("new", 1, 1000)],
		)
		for shown, row in zip(drawn, stored, strict=True):
			self.assert_matches_stored(shown, row, ("label", "flag", "count", "ratio", "due", "kind"))
		self.assertEqual([d["flag"] for d in drawn], ["No", "Yes", "Yes"])

	def test_a_batch_create_and_a_bulk_update_draw_the_stored_form(self):
		docs = [
			{
				"doctype": self.form,
				"values": {
					"title": "b1",
					"is_group": "yes",
					"rows": [{"label": "a", "flag": "on", "count": "1,000"}],
				},
			},
			{
				"doctype": self.form,
				"values": {"title": "b2", "is_group": "no", "rows": [{"label": "b", "flag": "false"}]},
			},
		]
		would = {"created": [{"doctype": self.form, "name": "b1"}, {"doctype": self.form, "name": "b2"}]}
		card = build_card("create_doc", {"docs": docs}, {"would": would, **SENSITIVE})
		first, second = card["records"]
		self.assertEqual({r["label"]: r["value"] for r in first["rows"]}["Is Group"], "Yes")
		self.assertEqual({r["label"]: r["value"] for r in second["rows"]}["Is Group"], "No")
		self.assertEqual(self.cells(first["tables"][0])[0]["flag"], "Yes")
		self.assertEqual(self.cells(first["tables"][0])[0]["count"], "1000")
		self.assertEqual(self.cells(second["tables"][0])[0]["flag"], "No")
		one = self.make("bulk-1", [{"label": "x", "flag": 0}])
		updates = [
			{
				"name": "bulk-1",
				"changes": {"is_group": "yes", "rows": [{"name": one.rows[0].name, "flag": "Y"}]},
			}
		]
		with as_user(SM_USER):
			card = build_card("update_doc", {"doctype": self.form, "updates": updates}, dict(SENSITIVE))
			diff = {d["label"]: d for d in card["records"][0]["diff"]}
			self.assertEqual((diff["Is Group"]["from"], diff["Is Group"]["to"]), ("No", "Yes"))
			self.assertEqual(self.cells(diff["Rows"]["to_table"])[0]["flag"], "Yes")
			update_doc(self.form, updates=updates)
		self.assertEqual(frappe.db.get_value(self.form, "bulk-1", "is_group"), 1)
		self.assertEqual(self.stored("bulk-1")[0].flag, 1)

	def test_an_ordinary_bulk_card_draws_a_top_level_word_as_stored(self):
		"""Not a full card: its tables stay "N rows", but ``is_group: "yes"`` drew as
		No while the save stored 1."""
		self.make("ord-1", [])
		updates = [{"name": "ord-1", "changes": {"is_group": "yes"}}]
		with as_user(SM_USER):
			card = build_card("update_doc", {"doctype": self.form, "updates": updates}, {})
		[row] = card["records"][0]["diff"]
		self.assertEqual((row["from"], row["to"]), ("No", "Yes"))

	# -- m1: secrets in a row ----------------------------------------------------
	def test_a_secret_cell_says_what_happens_to_it(self):
		doc = self.make(
			"sec-1",
			[
				{"label": "kept", "pass_key": "s3cret-one", "api_token": "tok-one"},
				{"label": "changed", "pass_key": "s3cret-two", "api_token": "tok-two"},
				{"label": "cleared", "pass_key": "s3cret-three", "api_token": "tok-three"},
				{"label": "dummy", "pass_key": "s3cret-four", "api_token": "tok-four"},
			],
		)
		kept, changed, cleared, dummy = (r.name for r in doc.rows)
		rows = [
			{"name": kept},
			{"name": changed, "pass_key": "new-pass", "api_token": "new-token"},
			{"name": cleared, "pass_key": "", "api_token": ""},
			{"name": dummy, "pass_key": "*" * 8, "api_token": "*" * 8},
			{"label": "new", "pass_key": "fresh", "api_token": "fresh-token"},
		]
		card = self.update_card("sec-1", [dict(r) for r in rows])
		drawn = self.cells(self.table_diff(card)["to_table"])
		self.assertEqual(
			[(d["pass_key"], d["api_token"]) for d in drawn],
			[
				("set", "set"),
				("changed", "changed"),
				("cleared", "cleared"),
				# A Password cell of asterisks is Frappe's own mask and is left alone;
				# on a plain Data cell the asterisks themselves are stored.
				("unchanged", "changed"),
				("set", "set"),
			],
		)
		everything = cstr(card)
		for secret in ("s3cret", "tok-", "new-pass", "new-token", "fresh"):
			self.assertNotIn(secret, everything, "never the value")
		with as_user(SM_USER):
			update_doc(self.form, "sec-1", {"rows": [dict(r) for r in rows]})
		stored = self.stored("sec-1")
		self.assertEqual(stored[3].api_token, "*" * 8, "the Data cell really holds the asterisks now")
		row = frappe.get_doc(self.child, stored[3].name)
		self.assertEqual(row.get_password("pass_key"), "s3cret-four", "the Password cell was left alone")
		self.assertEqual(frappe.get_doc(self.child, stored[1].name).get_password("pass_key"), "new-pass")

	def _top(self, title: str, **changes) -> dict:
		"""The update card's top-level rows, label -> (from, to)."""
		args = {"doctype": self.form, "name": title, "changes": changes}
		with as_user(SM_USER):
			card = build_card("update_doc", args, {"would": dict(changes), **SENSITIVE})
		return {d["label"]: (d["from"], d["to"]) for d in card["diff"]}

	def test_a_top_level_secret_says_what_is_really_stored(self):
		"""The same rule as in a row. Only a Password field is masked by Frappe: all
		asterisks sent back to one leave its secret alone; on a secret-named Data
		field the asterisks are the new value."""
		stars = "*" * 9
		frappe.get_doc(
			{
				"doctype": self.form,
				"title": "top-1",
				"secret_code": "s3cret-pass",
				"access_token": "tok-first",
			}
		).insert()
		frappe.db.commit()
		rows = self._top("top-1", secret_code=stars, access_token=stars)
		self.assertEqual(rows["Secret Code"], ("set", "unchanged"), "Frappe's own mask sent back")
		self.assertEqual(rows["Access Token"], ("set", "changed"))
		with as_user(SM_USER):
			update_doc(self.form, "top-1", {"secret_code": stars, "access_token": stars})
		doc = frappe.get_doc(self.form, "top-1")
		self.assertEqual(doc.get_password("secret_code"), "s3cret-pass", "the Password kept its secret")
		self.assertEqual(doc.access_token, stars, "the Data field holds the asterisks")
		frappe.db.rollback()
		rows = self._top("top-1", secret_code="another-pass", access_token="tok-second")
		self.assertEqual(
			(rows["Secret Code"], rows["Access Token"]), (("set", "changed"), ("set", "changed"))
		)
		rows = self._top("top-1", secret_code="", access_token="")
		self.assertEqual(
			(rows["Secret Code"], rows["Access Token"]), (("set", "cleared"), ("set", "cleared"))
		)
		self.assertNotIn(
			"Access Token", self._top("top-1", access_token="tok-first", is_group=1), "echoed: no row"
		)
		for values, token in (
			({"secret_code": "fresh-pass", "access_token": "fresh-token"}, "set"),
			# On a create, asterisks in a Password are Frappe's mask (nothing is set);
			# in the Data field they are stored as they are.
			({"secret_code": stars, "access_token": stars}, "set"),
		):
			title = f"top-new-{uuid.uuid4().hex[:5]}"
			args = {"doctype": self.form, "values": {"title": title, **values}}
			card = build_card("create_doc", args, dict(SENSITIVE))
			shown = {r["label"]: r["value"] for r in card["rows"]}
			self.assertEqual(shown["Access Token"], token)
			self.assertEqual(shown["Secret Code"], "set" if values["secret_code"] != stars else "not set")
			self.assertNotIn("fresh", cstr(card), "never the value")
			with as_user(SM_USER):
				create_doc(self.form, {"title": title, **values})
			self.assertEqual(frappe.db.get_value(self.form, title, "access_token"), values["access_token"])
		for secret in ("s3cret", "tok-", "another-pass"):
			self.assertNotIn(
				secret, cstr(self._top("top-1", secret_code="another-pass", access_token="tok-second"))
			)
		# The same in a bulk update and a batch create.
		updates = [{"name": "top-1", "changes": {"secret_code": stars, "access_token": stars}}]
		with as_user(SM_USER):
			card = build_card("update_doc", {"doctype": self.form, "updates": updates}, dict(SENSITIVE))
		diff = {d["label"]: (d["from"], d["to"]) for d in card["records"][0]["diff"]}
		self.assertEqual(diff, {"Secret Code": ("set", "unchanged"), "Access Token": ("set", "changed")})
		docs = [
			{"doctype": self.form, "values": {"title": "top-b", "secret_code": stars, "access_token": stars}}
		]
		card = build_card(
			"create_doc",
			{"docs": docs},
			{"would": {"created": [{"doctype": self.form, "name": "top-b"}]}, **SENSITIVE},
		)
		shown = {r["label"]: r["value"] for r in card["records"][0]["rows"]}
		self.assertEqual((shown["Secret Code"], shown["Access Token"]), ("not set", "set"))

	# -- m2: a cell above the requester's level ----------------------------------
	def test_a_cell_the_requester_may_not_write_is_not_drawn_as_changed(self):
		doc = self.make("lvl-1", [{"label": "kept", "locked": "stays"}])
		rows = [
			{"name": doc.rows[0].name, "label": "edited", "locked": "forced"},
			{"label": "new", "locked": "forced"},
		]
		card = self.update_card("lvl-1", [dict(r) for r in rows])
		drawn = self.cells(self.table_diff(card)["to_table"])
		self.assertEqual([d["label"] for d in drawn], ["edited", "new"])
		self.assertNotIn("forced", cstr(card), "the save drops it, so the card does not show it")
		self.assertNotIn("stays", cstr(card), "nor a stored value the requester may not read")
		with as_user(SM_USER):
			update_doc(self.form, "lvl-1", {"rows": [dict(r) for r in rows]})
		stored = self.stored("lvl-1")
		self.assertEqual([(r.label, r.locked) for r in stored], [("edited", "stays"), ("new", None)])
		# Administrator writes every level, and the card then shows it.
		args = {
			"doctype": self.form,
			"name": "lvl-1",
			"changes": {"rows": [{"name": stored[0].name, "locked": "x"}]},
		}
		card = build_card("update_doc", args, {"would": args["changes"], **SENSITIVE})
		self.assertEqual(self.cells(self.table_diff(card)["to_table"])[0]["locked"], "x")

	# -- m3: nothing changes -----------------------------------------------------
	def test_a_table_kept_whole_by_name_is_not_drawn_as_a_change(self):
		doc = self.make("same-1", [{"label": "a", "flag": 1, "pass_key": "s3cret"}, {"label": "b"}])
		rows = [{"name": r.name} for r in doc.rows]
		card = self.update_card("same-1", rows)
		self.assertEqual(card["diff"], [], "no row for a table whose rows stay as they are")
		card = self.update_card(
			"same-1", [{"name": doc.rows[0].name, "flag": "yes"}, {"name": doc.rows[1].name}]
		)
		self.assertEqual(card["diff"], [], "an echoed cell in another spelling is not a change either")
		card = self.update_card("same-1", rows[:1])
		self.assertEqual(self.table_diff(card)["to"], "1 row", "a removed row is one")

	def test_the_number_helpers_agree_with_frappe(self):
		self.assertEqual((cint("1000"), flt("1,234.5")), (1000, 1234.5))
