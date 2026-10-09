"""The one structure change chat may make: a guarded Custom Field (round 2, J1b-cf).

One new Custom Field, or an edit of one that changes no column, each only through a
confirmation card with no trial run; everything else about database structure stays
refused. These tests run REAL DDL, so every one targets a scratch ``custom=1``
DocType made in ``setUpClass`` under a unique name and torn down by
``addClassCleanup`` (fields, table, DocType, caches), which then asserts nothing is
left. Never ToDo or any shared doctype. SERIAL ONLY: one run of this module per site
at a time (it holds real structure locks and drops its own tables). It writes no
file of the site, and reconciles only its own rows (``reconcile_own``).

Continuation turns are written for real and never started (``_Hermetic``).
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import actions_api
from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _seal
from jarvis.tests._pending_action_helpers import (
	AGENT_WRITE,
	CONV,
	MSG,
	OWNER,
	PA,
	SM_USER,
	PendingActionTestMixin,
	as_user,
)
from jarvis.tests.test_panel_failure import _Hermetic
from jarvis.tools import _custom_field_guard as cfg
from jarvis.tools import _guarded_structure as gs
from jarvis.tools import _write_risk as wr

CF = "Custom Field"
STRUCTURAL_LINE = "Confirming changes the database structure for every user and cannot be undone."
# 113 Data columns beside Frappe's own: the most a table holds before MariaDB's
# row-size limit (1118) refuses one more varchar(140). Measured on MariaDB 11.4.
WIDE_FIELDS = 113


class PlantedError(Exception):
	"""An unexpected failure planted after the structure change committed."""


def cf_validation_hook(doc, method=None):
	raise frappe.ValidationError("planted: a hook refused the field")


def cf_crash_hook(doc, method=None):
	raise PlantedError("planted crash")


def _columns(doctype: str) -> set[str]:
	"""The real table's columns, lower-cased (never a cache)."""
	return set(gs.table_columns(doctype))


def _field_rows(doctype: str) -> list[str]:
	return frappe.get_all(CF, filters={"dt": doctype}, pluck="fieldname")


class _ScratchBase(_Hermetic, PendingActionTestMixin, FrappeTestCase):
	"""A scratch DocType per test class, a System Manager who is not Administrator
	as the person, and one conversation per test."""

	WIDE = False

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._t0 = frappe.utils.now_datetime()
		# Named and registered for teardown BEFORE anything is made: a DocType whose
		# CREATE TABLE fails has already committed its own row.
		cls.dt = "Jcf Scratch " + uuid.uuid4().hex[:6]
		cls.wide = "Jcf Wide " + uuid.uuid4().hex[:6]
		cls.scratch = [cls.dt, cls.wide] if cls.WIDE else [cls.dt]
		cls.addClassCleanup(cls._drop_scratch)
		cls._make_doctype(cls.dt, 2)
		if cls.WIDE:
			cls._make_doctype(cls.wide, WIDE_FIELDS)

	@classmethod
	def _make_doctype(cls, name: str, fields: int) -> str:
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": name,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				# ``who`` (a Link to User) is there for the fetch_from checks.
				"fields": [
					{"label": "Who", "fieldname": "who", "fieldtype": "Link", "options": "User"},
					*(
						{
							"label": f"Title {i}",
							"fieldname": f"title_{i}" if i else "title",
							"fieldtype": "Data",
						}
						for i in range(fields - 1)
					),
				],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return name

	@classmethod
	def _drop_scratch(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.local.doc_events_hooks = None
		for dt in cls.scratch:
			fields = frappe.get_all(CF, filters={"dt": dt}, pluck="name")
			frappe.db.delete(CF, {"dt": dt})
			frappe.db.delete("Property Setter", {"doc_type": dt})
			if fields:
				frappe.db.delete("Version", {"ref_doctype": CF, "docname": ["in", fields]})
			frappe.delete_doc_if_exists("DocType", dt, force=True)
			frappe.db.delete("Deleted Document", {"deleted_doctype": "DocType", "deleted_name": dt})
			# Deleting a DocType keeps its table; this one was made here, so drop it
			# (and with it every column a test added).
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{dt}`")
			frappe.clear_cache(doctype=dt)
		# What making and deleting the scratch DocTypes (and Frappe 16's permission
		# log of each field) wrote beside them.
		frappe.db.delete(
			"Error Log", {"method": ["like", "jarvis.pending_action.%"], "creation": [">=", cls._t0]}
		)
		frappe.db.delete(
			"Comment",
			{"comment_type": "Deleted", "reference_doctype": "DocType", "creation": [">=", cls._t0]},
		)
		if frappe.db.table_exists("Permission Log"):
			frappe.db.delete(
				"Permission Log", {"for_doctype": "DocType", "for_document": ["in", cls.scratch]}
			)
		frappe.db.commit()
		for dt in cls.scratch:
			assert not frappe.db.exists("DocType", dt), f"{dt}: scratch DocType left behind"
			assert not frappe.db.sql("SHOW TABLES LIKE %s", f"tab{dt}"), f"{dt}: scratch table left behind"
			assert not frappe.db.exists(CF, {"dt": dt}), f"{dt}: scratch Custom Field left behind"
			assert not gs.table_columns(dt), f"{dt}: scratch columns left behind"

	def setUp(self):
		super().setUp()
		self.conv = self.make_conv(SM_USER)
		self._published = []
		self.addCleanup(self._reset_scratch)

	def _reset_scratch(self):
		"""Each test starts from the bare scratch table: fields and their columns go."""
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.local.doc_events_hooks = None
		for dt in self.scratch:
			keep = {"title", "who"} | {f"title_{i}" for i in range(1, WIDE_FIELDS)}
			fields = frappe.get_all(CF, filters={"dt": dt}, pluck="name")
			frappe.db.delete(CF, {"dt": dt})
			frappe.db.delete("Property Setter", {"doc_type": dt})
			if fields:
				frappe.db.delete("Version", {"ref_doctype": CF, "docname": ["in", fields]})
			frappe.db.commit()
			for column in sorted(_columns(dt)):
				if column.startswith(("jcf_", "custom_")) and column not in keep:
					frappe.db.sql_ddl(f"ALTER TABLE `tab{dt}` DROP COLUMN `{column}`")
			frappe.clear_cache(doctype=dt)
		frappe.db.commit()

	# -- helpers ---------------------------------------------------------------

	def new_field(self, fieldname="jcf_note", dt=None, **values) -> dict:
		values = {"dt": dt or self.dt, "fieldname": fieldname, "label": "Note", "fieldtype": "Data", **values}
		return {"doctype": CF, "values": values}

	def run_tool(self, tool, args, conv=None, user=SM_USER) -> dict:
		with (
			as_user(user),
			patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: self._published.append(p)),
		):
			return api._run_tool(tool, args, conversation=conv or self.conv)

	def park_new(self, **kw) -> str:
		r = self.run_tool("create_doc", self.new_field(**kw))
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def reconcile_own(self, name: str) -> None:
		"""The reconciler's two structure steps for THIS test's row only: the real
		``pa.reconcile()`` also reaps, settles and retries every other row on the site."""
		from jarvis.chat.pending_actions import _reconcile

		_reconcile._reap_interrupted(only=name)
		_reconcile._retry_structure_cleanups(only=name)

	def card_name(self, conv=None) -> str:
		names = frappe.get_all(
			PA, filters={"conversation": conv or self.conv, "status": "Pending"}, pluck="name"
		)
		self.assertEqual(len(names), 1, "exactly one card waits")
		return names[0]

	def confirm(self, name: str, conv=None, user=SM_USER, **kw) -> dict:
		with as_user(user):
			return pa.execute(name, conversation=conv or self.conv, **kw)

	def assert_refused(self, r, code="InvalidArgumentError", says=()):
		self.assertFalse(r.get("ok"), r)
		self.assertEqual(r["error"]["code"], code, r)
		for text in says if isinstance(says, list | tuple) else [says]:
			self.assertIn(text, r["error"]["message"], r)
		self.assertFalse(frappe.db.exists(PA, {"conversation": self.conv}), "no card parked")

	def assert_untouched(self, dt=None, fieldname="jcf_note"):
		frappe.db.rollback()
		dt = dt or self.dt
		self.assertNotIn(fieldname, _field_rows(dt), "no Custom Field row")
		self.assertNotIn(fieldname, _columns(dt), "no column")

	def last_continuation(self) -> str:
		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "origin": "continuation", "hidden": 1},
			pluck="content",
			order_by="seq desc",
			limit=1,
		)
		self.assertTrue(rows, "no continuation was sent")
		return rows[0]

	def audit_outcomes(self, tool="create_doc") -> list[str]:
		return frappe.get_all(
			AGENT_WRITE,
			filters={"tool": tool, "ref_doctype": CF, "actor": SM_USER},
			pluck="outcome",
			order_by="creation asc",
		)

	def hook(self, handler: str, event="on_update") -> None:
		"""Register a Custom Field doc_events handler for this test only."""
		hooks = copy.deepcopy(frappe.get_doc_hooks())
		hooks.setdefault(CF, {}).setdefault(event, []).append(
			f"jarvis.tests.test_custom_field_exception.{handler}"
		)
		frappe.local.doc_events_hooks = hooks
		self.addCleanup(setattr, frappe.local, "doc_events_hooks", None)

	@contextmanager
	def no_row_size_check(self):
		"""Plant the failure the estimate exists to prevent: let the park through so
		the real ALTER meets MariaDB's row-size limit."""
		with patch.object(gs, "ROW_SIZE_LIMIT", 10**9):
			yield


# --------------------------------------------------------------------------- #
# The shared machinery
# --------------------------------------------------------------------------- #
class TestSchemaDiff(_ScratchBase):
	WIDE = True

	def _meta_with(self, **field):
		meta = frappe.get_meta(self.dt, cached=False)
		meta.append("fields", {"fieldname": "jcf_probe", "fieldtype": "Data", "is_custom_field": 1, **field})
		return meta

	def test_a_new_field_is_exactly_one_add_column_and_nothing_runs(self):
		before = _columns(self.dt)
		statements, table = gs.schema_changes(self.dt, self._meta_with())
		self.assertEqual(statements, [f"ALTER TABLE `tab{self.dt}` ADD COLUMN `jcf_probe` varchar(140)"])
		self.assertEqual(table.columns["jcf_probe"].get_definition(), "varchar(140)")
		self.assertEqual(_columns(self.dt), before, "recorded, never executed")
		self.assertNotIn("sql_ddl", frappe.db.__dict__, "the real sql_ddl is back")

	def test_an_unchanged_table_records_nothing(self):
		statements, _table = gs.schema_changes(self.dt, frappe.get_meta(self.dt, cached=False))
		self.assertEqual(statements, [])

	def test_unique_and_index_are_extra_statements(self):
		statements, _t = gs.schema_changes(self.dt, self._meta_with(unique=1))
		self.assertGreater(len(statements), 1, statements)
		statements, _t = gs.schema_changes(self.dt, self._meta_with(search_index=1))
		self.assertTrue(any("ADD INDEX" in s for s in statements), statements)

	def test_sort_field_drift_is_reported(self):
		"""Frappe adds an index for the sort field when it is missing: `creation` on
		Frappe 15, `modified` on Frappe 16 (each version's table is born with the
		other one). A table carrying that drift is not altered from chat."""
		sort_field = "creation" if frappe.__version__.startswith("15") else "modified"
		meta = frappe.get_meta(self.dt, cached=False)
		meta.sort_field = sort_field
		statements, _t = gs.schema_changes(self.dt, meta)
		self.assertEqual(statements, [f"ALTER TABLE `tab{self.dt}` ADD INDEX `{sort_field}`(`{sort_field}`)"])
		frappe.db.set_value("DocType", self.dt, "sort_field", sort_field, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)
		self.addCleanup(self._restore_sort_field)
		r = self.run_tool("create_doc", self.new_field())
		self.assert_refused(r, "structure_refused", "other structure changes waiting")
		self.assert_untouched()

	def _restore_sort_field(self):
		default = "modified" if frappe.__version__.startswith("15") else "creation"
		frappe.db.set_value("DocType", self.dt, "sort_field", default, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)

	def test_column_bytes_match_mariadb(self):
		for column_type, expected in (
			("varchar(140)", 562),
			("varchar(10)", 41),
			("varchar(64)", 258),
			("int(11)", 4),
			("bigint(20)", 8),
			("tinyint(4)", 1),
			("decimal(21,9)", 10),
			("text", 10),
			("longtext", 12),
			("date", 3),
			("datetime(6)", 8),
			("time(6)", 6),
		):
			self.assertEqual(gs.column_bytes(column_type), expected, column_type)

	def test_row_size_of_a_full_table(self):
		"""The wide scratch table is at MariaDB's limit: no room for one more Data
		column, room for a Long Text one (12 bytes), and the database agrees."""
		columns = gs.table_columns(self.wide)
		size = gs.row_size(columns)
		self.assertLessEqual(size, gs.ROW_SIZE_LIMIT)
		self.assertGreater(size + 563, gs.ROW_SIZE_LIMIT, "one more varchar(140) does not fit")
		self.assertFalse(gs.room_for(columns, "varchar(140)"))
		with patch.object(gs, "ROW_SIZE_MARGIN", 0):
			self.assertTrue(gs.room_for(columns, "longtext"))
		with self.assertRaises(Exception) as caught:
			frappe.db.sql_ddl(f"ALTER TABLE `tab{self.wide}` ADD COLUMN `jcf_over` varchar(140)")
		self.assertEqual(caught.exception.args[0], 1118)

	def test_lock_is_exclusive_and_released(self):
		with gs.structure_lock(self.dt):
			self.assertTrue(gs.lock_held(self.dt))
			with self.assertRaises(gs.StructureBusyError):
				with gs.structure_lock(self.dt):
					pass
			self.assertTrue(gs.lock_held(self.dt), "the failed attempt did not drop the holder's lock")
		self.assertFalse(gs.lock_held(self.dt))
		with gs.structure_lock(self.dt):
			pass

	def test_lock_survives_a_cache_clear_and_dies_with_its_connection(self):
		"""M1. The lock is a database lock on its own connection: no cache clear can
		drop it (Frappe 16 deletes every cache key that contains the DocType's name
		when a Custom Field is saved), and a worker that dies lets go of it at once."""
		with gs.structure_lock(self.dt):
			frappe.clear_cache(doctype=self.dt)
			frappe.clear_cache()
			with self.assertRaises(gs.StructureBusyError):
				with gs.structure_lock(self.dt):
					pass
		holder = gs.structure_lock(self.dt)
		holder.__enter__()
		try:
			with self.assertRaises(gs.StructureBusyError):
				with gs.structure_lock(self.dt):
					pass
			gs._held()[self.dt].close()  # the worker's connection is gone
			with gs.structure_lock(self.dt):
				pass
		finally:
			gs._held().pop(self.dt, None)
		# Another form's lock is its own, also when one name contains the other.
		with gs.structure_lock(self.dt), gs.structure_lock(self.dt[:-1]):
			pass

	def test_a_lock_killed_on_the_server_is_no_longer_held(self):
		"""``lock_held`` asks the server who holds the lock: a connection the server
		dropped (wait_timeout, a DBA's KILL, a failover) took the lock with it."""
		with gs.structure_lock(self.dt):
			self.assertTrue(gs.lock_held(self.dt))
			frappe.db.sql(f"KILL {int(gs._held()[self.dt].thread_id())}")
			self.assertFalse(gs.lock_held(self.dt))

	def test_lock_wait_is_shortened_then_put_back(self):
		before = frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0]
		with gs.short_lock_wait():
			self.assertEqual(
				frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0], gs.LOCK_WAIT_TIMEOUT_S
			)
		self.assertEqual(frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0], before)


# --------------------------------------------------------------------------- #
# Park: checks and the card
# --------------------------------------------------------------------------- #
class TestParkChecks(_ScratchBase):
	WIDE = True

	def test_a_valid_field_parks_a_structural_card_and_nothing_exists(self):
		before = _columns(self.dt)
		with patch("jarvis.api._run_preview") as trial:
			r = self.run_tool("create_doc", self.new_field(description="Shown under the field"))
		trial.assert_not_called()  # described, never trial-run
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertTrue(r["data"]["preview"]["described"])
		row = self.row(self.card_name())
		card = frappe.parse_json(row.card)
		self.assertEqual((card["kind"], card["doctype"]), ("create", CF))
		self.assertEqual(card["risk_line"], STRUCTURAL_LINE)
		self.assertEqual((card["risk"], card["structural"]), ("sensitive", True), "the full-card rendering")
		shown = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(shown.get("Field Description"), "Shown under the field", "set attributes are shown")
		self.assertNotIn("approve_run", card)
		self.assertEqual(_columns(self.dt), before, "no column after the park")
		self.assertEqual(_field_rows(self.dt), [], "no Custom Field row after the park")

	def test_each_check_refuses_naming_the_field(self):
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt}` ADD COLUMN `jcf_leftover` varchar(140)")
		cases = [
			(self.new_field("title"), ["title", "already has a field"]),
			(self.new_field("jcf_leftover"), ["jcf_leftover", "already has a column"]),
			(self.new_field("owner"), ["owner"]),
			(self.new_field("jcf_note", fieldtype="Table", options="DocField"), ["Table"]),
			(self.new_field("jcf_note", fieldtype="HTML"), ["HTML"]),
			(self.new_field("jcf_note", fieldtype="Nope"), ["Nope"]),
			(self.new_field("jcf_note", unique=1), ["jcf_note", "unique"]),
			(self.new_field("jcf_note", search_index=1), ["jcf_note", "index"]),
			(self.new_field("jcf_note", is_virtual=1), ["jcf_note", "virtual"]),
			(self.new_field("Jcf Note!"), ["Jcf Note!"]),
			(self.new_field("jcf_" + "x" * 70), ["64"]),
			(self.new_field("jcf_note", dt="Jarvis No Such DocType"), ["Jarvis No Such DocType"]),
			(self.new_field("jcf_note", dt=self.dt.lower()), [self.dt]),
			(self.new_field("jcf_note", fieldtype="Link", options="Jarvis No Such DocType"), ["Link"]),
			(self.new_field("jcf_note", dt=self.wide), ["jcf_note", "no room"]),
			({"doctype": CF, "values": {"dt": self.dt, "fieldtype": "Section Break"}}, ["label"]),
			(self.new_field("jcf_note", colour="red"), ["colour"]),
			(self.new_field("jcf_note", dt=self.dt + " "), [self.dt]),
			(self.new_field(123), ["fieldname"]),
			(self.new_field("jcf_note", is_system_generated=1), ["is_system_generated"]),
			(self.new_field("jcf_note", module="Core"), ["module"]),
			(self.new_field("jcf_note", default="it's"), ["default"]),
			(self.new_field("jcf_note", fetch_from="who.no_such_field"), ["fetch_from"]),
			(self.new_field("jcf_note", fetch_from="no_link.full_name"), ["fetch_from"]),
			(self.new_field("jcf_note", fetch_from="who.api_key"), ["fetch_from"]),
			(self.new_field("jcf_note", fetch_from="who.new_password"), ["fetch_from"]),
			# An ordinary Date at permlevel 0: only its secret-looking name stops it.
			(self.new_field("jcf_note", fetch_from="who.last_password_reset_date"), ["fetch_from"]),
			(self.new_field("jcf_note", fetch_from="title.full_name"), ["fetch_from"]),
		]
		for args, says in cases:
			with self.subTest(args=args["values"]):
				self.assert_refused(self.run_tool("create_doc", args), says=says)
		self.assert_untouched()
		self.assertEqual(_field_rows(self.wide), [])

	def test_a_park_never_touches_the_form_other_requests_read(self):
		"""B1. The schema diff is run on a private copy of the form's fields. Frappe 16
		keeps the object ``get_meta`` returns in a process-wide cache by reference, so a
		copy that was not private made every later insert of the form fail (an unknown
		column) with nothing confirmed."""
		before = [df.fieldname for df in frappe.get_meta(self.dt).fields]
		self.park_new()
		for meta in self._every_cached_meta(self.dt):
			self.assertEqual([df.fieldname for df in meta.fields], before)
		with as_user(SM_USER):
			frappe.get_doc({"doctype": self.dt, "title": "after the park"}).insert()
		frappe.db.rollback()

	def _every_cached_meta(self, dt: str) -> list:
		metas = [frappe.get_meta(dt), frappe.get_meta(dt, cached=False)]
		client_cache = getattr(frappe, "client_cache", None)  # Frappe 16: outlives the request
		if client_cache is not None and client_cache.get_value(f"doctype_meta::{dt}"):
			metas.append(client_cache.get_value(f"doctype_meta::{dt}"))
		local = (getattr(frappe.local, "cache", None) or {}).get("doctype_meta", {})  # Frappe 15
		if isinstance(local, dict) and local.get(dt):
			metas.append(local[dt])
		return metas

	def test_refused_settings_are_refused_however_they_are_spelled(self):
		"""B2. The checks read the values exactly as the write stores them: "true",
		"yes", "on" are 1 and "1,000" is 1000 to the save, so they are to the checks."""
		for key in ("unique", "search_index", "is_virtual"):
			for spelling in (True, "true", "True", "yes", "on", "1", 1, " 1 ", 2):
				with self.subTest(key=key, spelling=spelling):
					r = self.run_tool("create_doc", self.new_field(**{key: spelling}))
					self.assert_refused(r, says="jcf_note")
		# A length is honoured as written: "1,000" is a varchar(1000) to the row size too.
		columns = gs.table_columns(self.wide)
		with patch.object(gs, "ROW_SIZE_MARGIN", gs.ROW_SIZE_LIMIT - gs.row_size(columns) - 600):
			ok = self.run_tool("create_doc", self.new_field(dt=self.wide, length=140))
			self.assertTrue(ok.get("ok"), ok)
			frappe.db.delete(PA, {"conversation": self.conv})
			frappe.db.commit()
			for spelling in ("1,000", "1000", 1000, " 1000 "):
				r = self.run_tool("create_doc", self.new_field(dt=self.wide, length=spelling))
				self.assert_refused(r, says="no room")
		self.assert_untouched()

	def test_targets_chat_never_customises(self):
		"""M3. Core and virtual DocTypes, Jarvis's own, and the security / audit ones."""
		for dt in (
			"DocField",
			"User",
			"Role",
			"User Permission",
			"Server Script",
			"System Settings",
			"Version",
			"Error Log",
			"Custom Field",
			"Property Setter",
			"Jarvis Conversation",
			"Jarvis Pending Action",
		):
			with self.subTest(dt=dt):
				r = self.run_tool("create_doc", self.new_field(dt=dt))
				self.assert_refused(r, "structure_refused", dt)
				self.assertEqual(r["error"]["desk_path"], "/app/customize-form")
				self.assertFalse(frappe.db.exists(CF, {"dt": dt, "fieldname": "jcf_note"}))

	def test_fetch_from_and_expressions_are_checked_and_flagged(self):
		"""M2 + D2: a fetch that resolves to a readable, ordinary field is allowed and
		the card says what it does; an ``eval:`` expression gets the code line."""
		r = self.run_tool(
			"create_doc",
			self.new_field(fetch_from="who.full_name", depends_on="eval:doc.title", permlevel=1),
		)
		self.assertTrue(r.get("ok"), r)
		line = frappe.parse_json(self.row(self.card_name()).card)["risk_line"]
		self.assertTrue(line.startswith(STRUCTURAL_LINE), line)
		self.assertIn("This runs code for every user.", line)
		self.assertIn("This changes who can see or edit.", line)
		self.assertIn("copies data from the linked record", line)

	def test_a_plain_field_carries_only_the_structural_line(self):
		self.park_new()
		self.assertEqual(frappe.parse_json(self.row(self.card_name()).card)["risk_line"], STRUCTURAL_LINE)

	def test_the_card_shows_exactly_what_is_written(self):
		"""N2. The card, the checks and the write all read ONE stored form of the
		values: a spelling the save turns into 1 is shown as set, and saved as shown."""
		args = self.new_field(
			ignore_user_permissions="TRUE", read_only="true", allow_on_submit="y", length="1,000", bold="no"
		)
		args["doctype"] = "custom field"  # canonicalised too
		r = self.run_tool("create_doc", args)
		self.assertTrue(r.get("ok"), r)
		name = self.card_name()
		row = self.row(name)
		card = frappe.parse_json(row.card)
		self.assertEqual(card["doctype"], CF)
		shown = {r["label"]: r["value"] for r in card["rows"]}
		meta = frappe.get_meta(CF)
		for key in ("ignore_user_permissions", "read_only", "allow_on_submit"):
			self.assertEqual(shown[meta.get_label(key)], "Yes", key)
		self.assertEqual(shown[meta.get_label("bold")], "No")
		self.assertEqual(str(shown[meta.get_label("length")]).replace(",", ""), "1000")
		sealed = _seal.unseal_call(row)["args"]
		self.assertEqual(sealed["doctype"], CF)
		self.assertEqual(
			{
				k: sealed["values"][k]
				for k in ("ignore_user_permissions", "read_only", "allow_on_submit", "length", "bold")
			},
			{"ignore_user_permissions": 1, "read_only": 1, "allow_on_submit": 1, "length": 1000, "bold": 0},
		)
		self.assertTrue(self.confirm(name)["ok"])
		frappe.db.rollback()
		written = frappe.db.get_value(
			CF,
			f"{self.dt}-jcf_note",
			["ignore_user_permissions", "read_only", "allow_on_submit", "length", "bold"],
		)
		self.assertEqual(written, (1, 1, 1, 1000, 0))

	def test_a_fetch_is_stored_trimmed_and_shown_as_stored(self):
		r = self.run_tool("create_doc", self.new_field(fetch_from="  who.full_name \n"))
		self.assertTrue(r.get("ok"), r)
		row = self.row(self.card_name())
		self.assertEqual(_seal.unseal_call(row)["args"]["values"]["fetch_from"], "who.full_name")
		shown = {r["label"]: r["value"] for r in frappe.parse_json(row.card)["rows"]}
		self.assertIn("who.full_name", shown.values())
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.commit()
		for bad in ("who. full_name", "who.full_name\nwho.email", "who .full_name"):
			self.assert_refused(
				self.run_tool("create_doc", self.new_field(fetch_from=bad)), says="fetch_from"
			)

	def test_cards_that_would_die_at_confirm_are_refused_at_park(self):
		for args, says in (
			(self.new_field(reqd="yes", hidden="true"), "default"),
			(self.new_field(name="x"), "name"),
		):
			with self.subTest(args=args["values"]):
				self.assert_refused(self.run_tool("create_doc", args), says=says)
		# A padded type is no longer a card that dies: it is carded, sealed and saved
		# as the type the save stores ("Data").
		ok = self.run_tool("create_doc", self.new_field(fieldtype=" Data ", reqd=1, hidden=1, default="a"))
		self.assertTrue(ok.get("ok"), ok)
		row = self.row(self.card_name())
		self.assertEqual(_seal.unseal_call(row)["args"]["values"]["fieldtype"], "Data")
		self.assertTrue(self.confirm(row.name)["ok"])
		self.assertEqual(frappe.db.get_value(CF, f"{self.dt}-jcf_note", "fieldtype"), "Data")

	def test_more_forms_chat_never_customises(self):
		"""Child tables of a denied form (derived), and access / token / audit records."""
		for dt in (
			"User Email",
			"Block Module",
			"Workflow Transition",
			"Webhook Header",
			"Notification Recipient",
			"DocShare",
			"Document Share Key",
			"OAuth Bearer Token",
			"Deleted Document",
			"Scheduled Job Log",
			"Email Queue",
			"Email Queue Recipient",
		):
			if not frappe.db.exists("DocType", dt):
				continue
			with self.subTest(dt=dt), self.assertRaises(wr.StructureRefusedError):
				cfg._check_target(dt)
		cfg._check_target("Communication")  # left allowed (owner)
		cfg._check_target("ToDo")
		cfg._check_target(self.dt)

	def test_someone_who_cannot_read_the_linked_form_cannot_fetch_from_it(self):
		with patch.object(cfg, "_can_read", return_value=False):
			r = self.run_tool("create_doc", self.new_field(fetch_from="who.full_name"))
		self.assert_refused(r, says="fetch_from")

	def test_a_field_named_by_its_label_gets_frappes_own_fieldname(self):
		args = {
			"doctype": CF,
			"values": {"dt": self.dt, "label": "Delivery Notes", "fieldtype": "Small Text"},
		}
		self.assertTrue(self.run_tool("create_doc", args)["ok"])
		out = self.confirm(self.card_name())
		self.assertTrue(out["ok"], out)
		self.assertIn("custom_delivery_notes", _columns(self.dt))

	def test_a_leftover_property_setter_refuses(self):
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doctype_or_field": "DocField",
				"doc_type": self.dt,
				"field_name": "jcf_note",
				"property": "length",
				"property_type": "Int",
				"value": "500",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.assert_refused(self.run_tool("create_doc", self.new_field()), says=["jcf_note", "customis"])

	def test_someone_who_is_not_a_system_manager_is_refused_at_park(self):
		conv = self.make_conv(OWNER)
		r = self.run_tool("create_doc", self.new_field(), conv=conv, user=OWNER)
		self.assertEqual((r["ok"], r["error"]["code"]), (False, "PermissionDeniedError"), r)
		self.assertFalse(frappe.db.exists(PA, {"conversation": conv}))
		self.assert_untouched()

	def test_the_off_switch_refuses_like_any_structure_write(self):
		frappe.conf[gs.OFF_SWITCH] = 1
		self.addCleanup(frappe.conf.pop, gs.OFF_SWITCH, None)
		before = frappe.db.count(AGENT_WRITE, {"ref_doctype": CF, "outcome": "refused", "actor": SM_USER})
		r = self.run_tool("create_doc", self.new_field())
		self.assert_refused(r, "structure_refused", "set up in Desk")
		self.assertEqual(r["error"]["desk_path"], "/app/customize-form")
		self.assertEqual(
			frappe.db.count(AGENT_WRITE, {"ref_doctype": CF, "outcome": "refused", "actor": SM_USER}),
			before + 1,
		)
		field = self._make_field()
		r = self.run_tool("update_doc", {"doctype": CF, "name": field, "changes": {"label": "Other"}})
		self.assert_refused(r, "structure_refused")

	def _switch_in_the_file(self, on: bool) -> None:
		"""Set / unset the off switch in a site_config.json, as an operator does,
		WITHOUT touching this process's cached ``frappe.conf``. Never the site's real
		file (another run on the site would read it): a copy in a temporary
		directory, which the reader is pointed at through ``frappe.local.site_path``
		for exactly the calls ``disabled()`` makes."""
		import json
		import os
		import tempfile

		if not getattr(self, "_config_dir", None):
			self._config_dir = tempfile.mkdtemp(prefix="jcf-site-config-")
			self.addCleanup(__import__("shutil").rmtree, self._config_dir, True)
			real_read = gs._read_json
			real_dir = frappe.local.site_path

			def _read(path, *, missing_ok=False):
				if os.path.dirname(path) == real_dir:
					path = os.path.join(self._config_dir, os.path.basename(path))
				return real_read(path, missing_ok=missing_ok)

			reader = patch.object(gs, "_read_json", side_effect=_read)
			reader.start()
			self.addCleanup(reader.stop)
		with open(os.path.join(frappe.local.site_path, "site_config.json")) as f:
			config = json.load(f)
		config.pop(gs.OFF_SWITCH, None)
		if on:
			config[gs.OFF_SWITCH] = 1
		with open(os.path.join(self._config_dir, "site_config.json"), "w") as f:
			f.write(json.dumps(config, indent=1))

	def test_the_off_switch_is_read_from_the_file_not_a_cached_config(self):
		"""Frappe 16 keeps site_config.json per web worker for 60 s (config.py :149,
		``frappe/__init__.py`` :200): a worker that started before the operator set the
		switch went on parking and CONFIRMING cards for up to a minute."""
		name = self.park_new()
		self._switch_in_the_file(True)
		self.assertFalse(frappe.conf.get(gs.OFF_SWITCH), "this worker's cached config still says: not set")
		out = self.confirm(name)
		self.assertEqual((out["ok"], out["error"]["code"]), (False, "structure_refused"), out)
		self.assert_untouched()
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.commit()
		self.assert_refused(self.run_tool("create_doc", self.new_field()), "structure_refused")
		self._switch_in_the_file(False)
		name = self.park_new()  # allowed again, no restart
		self.assertTrue(self.confirm(name)["ok"])
		self.assertIn("jcf_note", _columns(self.dt))

	def test_an_unreadable_config_fails_closed(self):
		with patch.object(gs, "_read_json", side_effect=OSError("planted")):
			self.assert_refused(self.run_tool("create_doc", self.new_field()), "structure_refused")

	def test_text_and_number_spellings_are_stored_as_frappe_stores_them(self):
		"""Cycle 3: a numeric default on a text field is kept as its text, a default
		that is neither text nor a number is refused, and a padded precision is
		trimmed: the card, the seal and the write agree."""
		self.assert_refused(self.run_tool("create_doc", self.new_field(default=True)), says="default")
		r = self.run_tool("create_doc", self.new_field(default=7))
		self.assertTrue(r.get("ok"), r)
		row = self.row(self.card_name())
		self.assertEqual(_seal.unseal_call(row)["args"]["values"]["default"], "7")
		self.assertTrue(self.confirm(row.name)["ok"])
		self.assertEqual(frappe.db.get_value(CF, f"{self.dt}-jcf_note", "default"), "7")
		other = self.make_conv(SM_USER)
		r = self.run_tool(
			"create_doc", self.new_field("jcf_amount", fieldtype="Float", precision=" 2"), conv=other
		)
		self.assertTrue(r.get("ok"), r)
		row = self.row(self.card_name(other))
		self.assertEqual(_seal.unseal_call(row)["args"]["values"]["precision"], "2")

	def test_the_fetch_refusal_reads_whole_in_the_envelope(self):
		r = self.run_tool("create_doc", self.new_field(fetch_from="who.no_such_field"))
		self.assert_refused(r, says=["customer.customer_name", "a Link field of this form"])
		self.assertNotIn("Use '.'", r["error"]["message"])
		self.assertNotIn("<", r["error"]["message"])

	def test_a_confirm_under_the_lock_still_needs_the_exact_doctype(self):
		"""Defence in depth: no shipped route reaches a guarded confirm with a
		doctype in another letter case (the park canonicalises it), and one that did
		is refused, not written through a case-insensitive database."""
		args = self.new_field()
		args["doctype"] = "custom field"
		with as_user(SM_USER), gs.structure_lock(self.dt):
			r = api.dispatch_confirmed("create_doc", args, allow_risky=True)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assert_untouched()

	def _make_field(self, fieldname="jcf_made", **values) -> str:
		"""A Custom Field made as from Desk (no tool call: the guard is inert)."""
		with as_user(SM_USER):
			doc = frappe.get_doc(
				{
					"doctype": CF,
					"dt": self.dt,
					"fieldname": fieldname,
					"label": "Made",
					"fieldtype": "Data",
					**values,
				}
			).insert()
		frappe.db.commit()
		return doc.name

	def test_only_one_field_per_card_and_nothing_else_is_widened(self):
		two = [self.new_field("jcf_a"), self.new_field("jcf_b")]
		refused = [
			("create_doc", {"docs": two}),
			("create_doc", {"docs": [self.new_field("jcf_a")]}),
			("delete_doc", {"doctype": CF, "name": f"{self.dt}-title"}),
			("create_doc", {"doctype": "DocType", "values": {"name": "Jarvis Cf Nope", "module": "Custom"}}),
			("create_doc", {"doctype": "Inventory Dimension", "values": {"reference_document": self.dt}}),
			("delete_doc", {"doctype": "Workflow", "name": "x"}),
			("update_doc", {"doctype": CF, "updates": [{"name": "x", "changes": {"label": "y"}}]}),
			("update_doc", {"doctype": "DocType", "name": self.dt, "changes": {"sort_field": "creation"}}),
		]
		for tool, args in refused:
			with self.subTest(tool=tool, args=args):
				self.assert_refused(self.run_tool(tool, args), "structure_refused")
		self.assertEqual(wr.risk_of("create_doc", self.new_field()), "custom_field_new")
		self.assertEqual(
			wr.risk_of("update_doc", {"doctype": CF, "name": "x", "changes": {"label": "y"}}),
			"custom_field_edit",
		)
		self.assertEqual(wr.risk_of("create_doc", {"doctype": "Property Setter", "values": {}}), "sensitive")
		self.assert_untouched(fieldname="jcf_a")

	def test_routes_without_a_card_still_refuse(self):
		from jarvis.chat import approvals_api

		args = self.new_field()
		self.assert_refused(self.run_tool("preview_doc", args), "structure_refused")
		res = approvals_api._edit_dry_run("create_doc", args, [{"op": "create", **args}])
		self.assertIn("set up in Desk", (res or {}).get("message") or str(res))
		box = self.make_conv(SM_USER)
		frappe.db.set_value(CONV, box, "file_box", 1, update_modified=False)
		frappe.db.commit()
		r = self.run_tool("create_doc", args, conv=box)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		with as_user(SM_USER):
			r = api.dispatch_confirmed("create_doc", args, allow_risky=True)
		self.assertEqual(r["error"]["code"], "structure_refused", "no lock held: not the card's confirm")
		with as_user(SM_USER), gs.structure_lock(self.dt):
			r = api.dispatch_confirmed("create_doc", args)
		self.assertEqual(r["error"]["code"], "structure_refused", "an uncarded caller never gets it")
		# The Approval Board's Edit & create, a legacy token / approve-and-run dispatch
		# and a File Box sheet never hold the lock, so they never run it.
		from jarvis.chat import held_sheets
		from jarvis.chat.pending_actions import _execute

		held = frappe._dict(tool="create_doc", exec_user=SM_USER, kind="file_box_held", name="jcf-row")
		result, _interfered, crash = _execute._dispatch(held, args, user=SM_USER)
		self.assertEqual((crash, result["error"]["code"]), ("", "structure_refused"), result)
		record = {"tool": "create_doc", "args": args, "exec_user": SM_USER}
		with as_user(SM_USER):
			r = actions_api._dispatch_call(record, "jcf-token", "", "jcf crash")
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		with as_user(SM_USER):
			r = held_sheets.file_questions("create_doc", args, box)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assert_untouched()

	def test_a_legacy_token_card_is_refused(self):
		with patch("jarvis.chat.pending_confirm.pa_minting", return_value=False):
			r = self.run_tool("create_doc", self.new_field())
		self.assert_refused(r, "structure_refused")

	def test_every_uncarded_mode_parks_instead_of_running(self):
		for flags in ({"auto_mode": 1}, {"request_autorun": 1, "request_autorun_at": frappe.utils.now()}):
			conv = self.make_conv(SM_USER)
			frappe.db.set_value(CONV, conv, flags, update_modified=False)
			frappe.db.commit()
			with patch("jarvis.api.dispatch_confirmed") as dispatched:
				r = self.run_tool("create_doc", self.new_field(), conv=conv)
			self.assertEqual(r["data"]["status"], "pending_confirmation", (flags, r))
			dispatched.assert_not_called()
		self.assert_untouched()

	def test_the_draft_panel_turns_it_into_the_gated_card(self):
		with as_user(SM_USER), patch("jarvis.chat.events.publish_to_user"):
			r = actions_api.apply_action(
				{
					"verb": "create",
					"doctype": CF,
					"values": self.new_field()["values"],
					"conversation": self.conv,
				}
			)
		self.assertEqual((r["ok"], r.get("parked")), (True, True), r)
		card = frappe.parse_json(self.row(self.card_name()).card)
		self.assertEqual(card["risk_line"], STRUCTURAL_LINE)
		self.assert_untouched()


# --------------------------------------------------------------------------- #
# Confirm
# --------------------------------------------------------------------------- #
class TestConfirm(_ScratchBase):
	WIDE = True

	def test_confirm_makes_the_field_and_its_column_once(self):
		name = self.park_new()
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		self.assertEqual((out["pa_status"], out["outcome"]), ("Executed", "confirmed"))
		self.assertEqual(_field_rows(self.dt), ["jcf_note"])
		self.assertIn("jcf_note", _columns(self.dt))
		self.assertTrue(frappe.get_meta(self.dt).has_field("jcf_note"), "the form shows it")
		self.assertEqual(self.audit_outcomes(), ["applied"])
		self.assertIn("[System] Applied:", self.last_continuation())

		again = self.confirm(name)
		self.assertEqual((again["ok"], again["reason_code"]), (False, "already_handled"), again)
		self.assertEqual(_field_rows(self.dt), ["jcf_note"], "a second Confirm runs nothing")
		self.assertEqual(self.audit_outcomes(), ["applied"])
		self.assertEqual(frappe.db.get_value(CF, f"{self.dt}-jcf_note", "owner"), SM_USER)

	def test_confirm_through_the_endpoint(self):
		name = self.park_new()
		with as_user(SM_USER):
			out = actions_api.confirm_tool(name, self.conv)
		self.assertTrue(out["ok"], out)
		self.assertIn("jcf_note", _columns(self.dt))

	def test_a_field_without_a_column_is_made_too(self):
		name = self.park_new(fieldname="jcf_section", fieldtype="Section Break", label="More")
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(_field_rows(self.dt), ["jcf_section"])
		self.assertNotIn("jcf_section", _columns(self.dt))

	def test_a_held_lock_says_try_again_and_keeps_the_card(self):
		name = self.park_new()
		with gs.structure_lock(self.dt):  # another confirm on the same form is running
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(
			(out["error"]["code"], out["error"]["kind"]), ("RetryLaterError", "retry_later"), out
		)
		self.assertEqual(self.row(name).status, "Pending", "the card is still there to confirm")
		self.assert_untouched()
		self.assertEqual(self.audit_outcomes(), [], "nothing ran")
		self.assertTrue(self.confirm(name)["ok"], "and confirms once the other one is done")
		self.assertIn("jcf_note", _columns(self.dt))

	def test_the_lock_is_held_across_the_whole_confirm(self):
		seen = {}
		real = api.dispatch

		def _during(tool, args):
			seen["held"] = gs.lock_held(self.dt)
			seen["wait"] = frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0]
			seen["status"] = frappe.db.get_value(PA, name, "status")
			try:
				with gs.structure_lock(self.dt):
					seen["second"] = "taken"
			except gs.StructureBusyError:
				seen["second"] = "busy"
			return real(tool, args)

		name = self.park_new()
		with patch("jarvis.api.dispatch", side_effect=_during):
			self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(
			seen, {"held": True, "wait": gs.LOCK_WAIT_TIMEOUT_S, "status": "Executing", "second": "busy"}
		)
		self.assertFalse(gs.lock_held(self.dt), "released afterwards")
		self.assertNotEqual(frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0], gs.LOCK_WAIT_TIMEOUT_S)

	def test_the_lock_is_still_held_inside_the_alter(self):
		"""M1: sampled inside ``updatedb``, after Custom Field's own cache clear (which
		on Frappe 16 deletes every cache key naming the DocType), where a second
		confirm on the same form must still find the lock taken."""
		seen = {}
		real = frappe.db.updatedb

		def _in_updatedb(doctype, meta=None):
			seen["held"] = gs.lock_held(self.dt)
			try:
				with gs.structure_lock(self.dt):
					seen["second"] = "taken"
			except gs.StructureBusyError:
				seen["second"] = "busy"
			return real(doctype, meta)

		name = self.park_new()
		with patch.object(frappe.db, "updatedb", side_effect=_in_updatedb):
			self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(seen, {"held": True, "second": "busy"})

	def test_a_confirm_without_its_card_cannot_make_a_field(self):
		"""The real tool, nothing patched: ``dispatch_confirmed`` alone (no lock, so
		not the card's confirm) makes no Custom Field even with ``allow_risky``."""
		before = frappe.db.count(CF)
		with as_user(SM_USER):
			r = api.dispatch_confirmed("create_doc", self.new_field(), allow_risky=True)
		self.assertEqual(r["error"]["code"], "structure_refused", r)
		self.assertEqual(frappe.db.count(CF), before)
		self.assert_untouched()

	def test_a_clean_up_that_fails_is_retried_then_left_for_the_reconciler(self):
		with self.no_row_size_check():
			name = self.park_new(dt=self.wide)
			calls = []
			real = gs.clean_up

			def _flaky(undo, **kw):
				calls.append(1)
				if len(calls) == 1:
					raise RuntimeError("planted")
				return real(undo, **kw)

			with patch.object(gs, "clean_up", side_effect=_flaky):
				out = self.confirm(name)
		self.assertEqual(len(calls), 2, "retried once")
		self.assertEqual(out["outcome"], "failed")
		self.assert_untouched(self.wide)

		with self.no_row_size_check():
			frappe.db.delete(PA, {"conversation": self.conv})
			frappe.db.commit()
			name = self.park_new(dt=self.wide)
			with patch.object(gs, "clean_up", side_effect=RuntimeError("planted")):
				out = self.confirm(name)
		self.assertEqual(out["outcome"], "partial", "not proven clean")
		self.assertIn("could not be completed", out["error"]["message"])
		self.assertEqual(_field_rows(self.wide), ["jcf_note"], "still half-made")
		self.assertTrue(self.row(name).sealed_undo, "kept: the reconciler finishes it")
		self.assertTrue(self.logged("structure_cleanup_failed"))
		self.reconcile_own(name)
		self.assert_untouched(self.wide)
		self.assertFalse(self.row(name).sealed_undo)
		self.assertTrue(self.logged("structure_cleanup", "was removed"))

	def test_the_checks_run_again_under_the_lock(self):
		name = self.park_new()
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt}` ADD COLUMN `jcf_note` varchar(140)")
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("already has a column", out["error"]["message"])
		self.assertEqual(out["error"]["kind"], "not_fixable", "a structure write never self-corrects")
		self.assertEqual((out["pa_status"], out["outcome"]), ("Failed", "failed"))
		self.assertEqual(_field_rows(self.dt), [])
		self.assertIn("Retrying will not fix it", self.last_continuation())
		self.assertEqual(self.audit_outcomes(), ["failed"])

	def test_a_refusal_under_the_lock_is_filed_once_as_refused(self):
		"""Drift that appeared after the park: refused at Confirm, one audit row."""
		name = self.park_new()
		sort_field = "creation" if frappe.__version__.startswith("15") else "modified"
		default = "modified" if frappe.__version__.startswith("15") else "creation"
		frappe.db.set_value("DocType", self.dt, "sort_field", sort_field, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)
		try:
			out = self.confirm(name)
		finally:
			frappe.db.set_value("DocType", self.dt, "sort_field", default, update_modified=False)
			frappe.db.commit()
			frappe.clear_cache(doctype=self.dt)
		self.assertEqual(
			(out["ok"], out["error"]["code"], out["outcome"]), (False, "structure_refused", "failed")
		)
		self.assertEqual(self.audit_outcomes(), ["refused"])
		self.assert_untouched()

	def test_a_lock_wait_timeout_is_retry_later_and_leaves_nothing(self):
		"""Another session is inside the table: the ALTER cannot get its metadata
		lock, gives up after the short wait, and the field row that had already
		committed is removed."""
		name = self.park_new()
		other = frappe.db.get_connection()
		self.addCleanup(other.close)
		cursor = other.cursor()
		cursor.execute("START TRANSACTION")
		cursor.execute(f"SELECT COUNT(*) FROM `tab{self.dt}`")
		cursor.fetchall()
		# If the short wait ever stops applying, the ALTER would wait a day behind
		# this session: let go after 30 s so the test fails instead of hanging.
		import threading

		release = threading.Timer(30, lambda: cursor.connection.kill(cursor.connection.thread_id()))
		release.daemon = True
		release.start()
		self.addCleanup(release.cancel)
		started = frappe.utils.now_datetime()
		with patch.object(gs, "LOCK_WAIT_TIMEOUT_S", 1):
			out = self.confirm(name)
		self.assertLess((frappe.utils.now_datetime() - started).total_seconds(), 20, "gave up quickly")
		release.cancel()
		other.rollback()
		self.assertFalse(out["ok"], out)
		self.assertEqual(
			(out["error"]["code"], out["error"]["kind"]), ("RetryLaterError", "retry_later"), out
		)
		self.assertEqual(
			(out["pa_status"], out["outcome"]), ("Failed", "failed"), "nothing is left: not partial"
		)
		self.assert_untouched()
		self.assertFalse(frappe.get_meta(self.dt).has_field("jcf_note"))
		self.assertIn("The system was busy", self.last_continuation())

	def test_a_row_size_failure_removes_the_half_made_field(self):
		"""Review focus 2. The ALTER fails with MariaDB 1118 AFTER Frappe committed
		the field row: without the clean-up the form could not be opened by anyone."""
		with self.no_row_size_check():
			name = self.park_new(dt=self.wide)
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		# D1: the clean-up proved nothing is left, so this is a plain failure.
		self.assertEqual(
			(out["pa_status"], out["reason_code"], out["outcome"]), ("Failed", "failed", "failed")
		)
		self.assertEqual(out["error"]["kind"], "not_fixable")
		self.assertIn("row size limit", out["error"]["message"])
		self.assertIn("was removed", out["error"]["message"], "the receipt says what was removed")
		self.assertIn("jcf_note", out["error"]["message"])
		self.assert_untouched(self.wide)
		self.assertFalse(frappe.get_meta(self.wide).has_field("jcf_note"), "cache cleared")
		with as_user(SM_USER):
			frappe.get_list(self.wide, fields=["name", "title"])  # the form still lists
		content = self.last_continuation()
		self.assertIn("nothing was changed. Retrying will not fix it", content)
		self.assertIn("was removed", content)
		self.assertNotIn("correct only the fields named", content)
		self.assertEqual(self.audit_outcomes(), ["failed"])
		self.assertFalse(self.row(name).correction_message, "never offered a correction")
		self.assertFalse(self.row(name).sealed_undo, "cleaned up: nothing kept for a retry")

	def test_a_hook_that_fails_after_the_column_was_added(self):
		"""The row and the column both committed before the hook raised: the field
		is complete, so it is kept, and the outcome says partly applied."""
		self.hook("cf_validation_hook")
		name = self.park_new()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["reason_code"], out["outcome"]), ("partial", "partial"))
		self.assertEqual(out["error"]["kind"], "not_fixable")
		self.assertIn("left in place", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(_field_rows(self.dt), ["jcf_note"])
		self.assertIn("jcf_note", _columns(self.dt), "no row without its column")
		self.assertTrue(frappe.get_meta(self.dt).has_field("jcf_note"))
		self.assertEqual(self.audit_outcomes(), ["partial"])

	def test_an_unexpected_crash_after_the_commit_is_partial_and_says_what_is_left(self):
		self.hook("cf_crash_hook")
		name = self.park_new()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["error"]["code"], out["outcome"]), ("InternalError", "partial"))
		self.assertIn("left in place", out["error"]["message"])
		self.assertTrue(self.logged("dispatch_crashed"), "a crash still leaves its Error Log row")
		self.assertEqual(self.audit_outcomes(), ["partial"])

	def test_a_failure_before_anything_committed_is_a_plain_failure(self):
		self.hook("cf_validation_hook", event="validate")
		name = self.park_new()
		out = self.confirm(name)
		self.assertEqual((out["ok"], out["outcome"], out["reason_code"]), (False, "failed", "failed"), out)
		self.assertEqual(out["error"]["kind"], "not_fixable")
		self.assert_untouched()
		self.assertEqual(self.audit_outcomes(), ["failed"])

	def test_clean_up_leaves_other_fields_alone(self):
		"""Only the field THIS confirmation made, and only when its column is
		missing: never a layout field (no column by design), never another
		person's field, never one made before the claim."""
		now = frappe.utils.now_datetime()
		other = frappe.utils.add_to_date(now, seconds=-3)
		self._orphan("jcf_break", "Section Break", SM_USER, creation=now)
		self._orphan("jcf_theirs", "Data", SM_USER, creation=other)
		# The row is identified exactly: its name and the timestamp this confirmation
		# wrote it with (no owner, no clock comparison). A field of the same name
		# written at any other moment is someone else's; with no stamp, nothing is.
		for fieldname in ("jcf_break", "jcf_theirs"):
			self.assertNotIn("was removed", cfg.cleanup_half_made_field(self.dt, fieldname, written=str(now)))
		self.assertNotIn("was removed", cfg.cleanup_half_made_field(self.dt, "jcf_theirs", written=None))
		self.assertEqual(sorted(_field_rows(self.dt)), ["jcf_break", "jcf_theirs"])
		self._orphan("jcf_mine", "Data", "Administrator", creation=now)  # whoever confirmed
		self.assertIn("was removed", cfg.cleanup_half_made_field(self.dt, "jcf_mine", written=str(now)))
		self.assertNotIn("jcf_mine", _field_rows(self.dt))

	def _orphan(self, fieldname, fieldtype, owner, creation=None, dt=None) -> None:
		"""A Custom Field row with no column: what a failed ALTER leaves."""
		now = creation or frappe.utils.now_datetime()
		doc = frappe.get_doc(
			{
				"doctype": CF,
				"dt": dt or self.dt,
				"fieldname": fieldname,
				"label": fieldname,
				"fieldtype": fieldtype,
			}
		)
		doc.name = f"{dt or self.dt}-{fieldname}"
		doc.owner = doc.modified_by = owner
		doc.creation = doc.modified = now
		doc.db_insert()
		frappe.db.commit()

	def _die_mid_confirm(self, name: str) -> None:
		"""The worker is killed after Frappe committed the field row and before the
		ALTER: the row stays Executing with a field that has no column."""

		def _killed(query, debug=False):
			# Frappe's sql_ddl: commit (the field row, and with it the stamp of what this
			# confirmation wrote), then the ALTER that never runs: the worker is gone.
			frappe.db.commit()
			raise SystemExit("worker killed")

		with patch.object(frappe.db, "sql_ddl", side_effect=_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		self.assertEqual(self.row(name).status, "Executing")
		self.assertTrue(self.row(name).sealed_undo, "what to clean up rides the row")
		self.assertEqual(_field_rows(self.dt), ["jcf_note"])
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11), "n": name},
		)
		frappe.db.commit()

	def test_the_reconciler_cleans_up_an_interrupted_confirm(self):
		name = self.park_new()
		self._die_mid_confirm(name)
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assert_untouched()
		self.assertFalse(frappe.get_meta(self.dt).has_field("jcf_note"))
		self.assertTrue(self.logged("structure_cleanup", "was removed"))
		self.assertEqual(self.audit_outcomes(), ["partial"])

	def test_a_clean_up_that_keeps_failing_ends_as_partial_and_is_logged_once(self):
		name = self.park_new()
		self._die_mid_confirm(name)
		with patch.object(gs, "clean_up", side_effect=RuntimeError("planted")):
			for _ in range(3):
				self.reconcile_own(name)
			self.assertEqual(self.row(name).status, "Executing", "kept for the next run")
			self.assertEqual(
				len(self.logged("structure_cleanup_failed", name)), 1, "logged once, not per run"
			)
			frappe.db.sql(
				f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
				{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-25), "n": name},
			)
			frappe.db.commit()
			self.reconcile_own(name)
			self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "partial"))
		self.assertFalse(row.sealed_undo)
		self.assertEqual(len(self.logged("structure_needs_a_person", name)), 1)

	def test_a_stamp_that_could_not_be_written_is_never_reported_clean(self):
		with self.no_row_size_check():
			name = self.park_new(dt=self.wide)
			with patch.object(gs, "_stamp_rows", return_value=0):
				out = self.confirm(name)
		self.assertEqual(out["outcome"], "partial", "may remain: not proven")
		self.assert_untouched(self.wide)  # found through the sealed call all the same

	def test_a_lock_lost_during_the_confirm_is_never_reported_clean(self):
		self.hook("cf_validation_hook", event="validate")  # fails before anything commits
		name = self.park_new()
		real = api.dispatch

		def _lock_dies(tool, args):
			frappe.db.sql(f"KILL {int(gs._held()[self.dt].thread_id())}")
			return real(tool, args)

		with patch("jarvis.api.dispatch", side_effect=_lock_dies):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["outcome"], "partial", "the lock was lost: could not be verified")
		self.assertTrue(self.logged("structure_lock_lost", name))

	def test_the_reconciler_waits_for_the_lock(self):
		name = self.park_new()
		self._die_mid_confirm(name)
		with gs.structure_lock(self.dt):
			self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Executing", "left for the next run")
		self.assertEqual(_field_rows(self.dt), ["jcf_note"], "no clean-up without the lock")
		self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Failed")
		self.assert_untouched()


# --------------------------------------------------------------------------- #
# The column-free edit
# --------------------------------------------------------------------------- #
class TestEdit(_ScratchBase):
	def setUp(self):
		super().setUp()
		with as_user(SM_USER):
			doc = frappe.get_doc(
				{
					"doctype": CF,
					"dt": self.dt,
					"fieldname": "jcf_made",
					"label": "Made",
					"fieldtype": "Data",
					"description": "Before",
				}
			).insert()
		frappe.db.commit()
		self.field = doc.name

	def edit(self, **changes) -> dict:
		return {"doctype": CF, "name": self.field, "changes": changes}

	def park_edit(self, **changes) -> str:
		r = self.run_tool("update_doc", self.edit(**changes))
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def stored(self, *fields):
		frappe.db.rollback()
		return frappe.db.get_value(CF, self.field, list(fields))

	def test_a_label_edit_parks_a_structural_card_then_applies(self):
		before = _columns(self.dt)
		with patch("jarvis.api._run_preview") as trial:
			name = self.park_edit(label="Renamed", description="After", reqd=1, hidden=0)
		trial.assert_not_called()
		card = frappe.parse_json(self.row(name).card)
		self.assertEqual((card["kind"], card["structural"]), ("update", True))
		self.assertEqual(
			card["risk_line"], STRUCTURAL_LINE + " This changes who can see or edit.", "reqd: D2"
		)
		diff = {d["label"]: (d["from"], d["to"]) for d in card["diff"]}
		self.assertEqual(diff.get("Label"), ("Made", "Renamed"))
		self.assertEqual(self.stored("label", "description"), ("Made", "Before"), "nothing changed at park")
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		self.assertEqual(self.stored("label", "description", "reqd"), ("Renamed", "After", 1))
		self.assertEqual(_columns(self.dt), before, "no column changed")
		self.assertEqual(self.audit_outcomes("update_doc"), ["applied"])
		self.assertEqual(self.confirm(name)["reason_code"], "already_handled")

	def test_a_change_to_the_column_is_refused_with_a_desk_path(self):
		for changes in (
			{"fieldtype": "Int"},
			{"length": 500},
			{"unique": 1},
			{"search_index": 1},
			{"fieldname": "jcf_other"},
			{"dt": "ToDo"},
			{"is_virtual": 1},
			{"label": "Fine", "fieldtype": "Small Text"},
		):
			with self.subTest(changes=changes):
				r = self.run_tool("update_doc", self.edit(**changes))
				self.assert_refused(r, "structure_refused")
				self.assertEqual(r["error"]["desk_path"], f"/app/custom-field/{self.field}")
		self.assertEqual(self.stored("label", "fieldtype"), ("Made", "Data"))

	def test_an_edit_the_schema_diff_objects_to_is_refused(self):
		"""A new default rewrites the column (MODIFY): not column-free."""
		r = self.run_tool("update_doc", self.edit(default="x"))
		self.assert_refused(r, "structure_refused", "changes the column")
		self.assertEqual(self.stored("default"), None)

	def test_echoed_unchanged_column_keys_are_not_a_column_change(self):
		name = self.park_edit(label="Renamed", fieldtype="Data", length=0, unique=0)
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.stored("label"), "Renamed")

	def test_missing_and_system_fields_are_refused(self):
		r = self.run_tool("update_doc", {"doctype": CF, "name": "Jarvis-Nope", "changes": {"label": "x"}})
		self.assert_refused(r, says="Jarvis-Nope")
		self.assert_refused(self.run_tool("update_doc", self.edit(colour="red")), says="colour")
		self.assert_refused(self.run_tool("update_doc", self.edit()), says="changes")
		frappe.db.set_value(CF, self.field, "is_system_generated", 1, update_modified=False)
		frappe.db.commit()
		self.assert_refused(self.run_tool("update_doc", self.edit(label="x")), "structure_refused")

	def test_someone_who_is_not_a_system_manager_is_refused_at_park(self):
		conv = self.make_conv(OWNER)
		r = self.run_tool("update_doc", self.edit(label="x"), conv=conv, user=OWNER)
		self.assertEqual((r["ok"], r["error"]["code"]), (False, "PermissionDeniedError"), r)

	def test_the_before_image_rides_the_confirmation_row(self):
		name = self.park_edit(label="Renamed")
		seen = {}
		real = api.dispatch

		def _during(tool, args):
			row = pa.get_row(name) if hasattr(pa, "get_row") else self.row(name)
			seen["undo"] = _seal.unseal_undo(row)
			return real(tool, args)

		with patch("jarvis.api.dispatch", side_effect=_during):
			self.assertTrue(self.confirm(name)["ok"])
		undo = seen["undo"]
		self.assertEqual((undo["risk"], undo["name"]), ("custom_field_edit", self.field))
		self.assertEqual((undo["before"]["label"], undo["before"]["description"]), ("Made", "Before"))
		self.assertFalse(self.row(name).sealed_undo, "dropped once the card is settled")

	def test_a_failure_after_the_edit_committed_restores_the_field(self):
		self.hook("cf_validation_hook")
		name = self.park_edit(label="Renamed", description="After")
		held = []
		real = gs.clean_up

		def _cleaning(undo, **kw):
			held.append(gs.lock_held(self.dt))
			return real(undo, **kw)

		with patch.object(gs, "clean_up", side_effect=_cleaning):
			out = self.confirm(name)
		self.assertEqual(held, [True], "the clean-up runs under the form's lock")
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["reason_code"], out["outcome"]), ("failed", "failed"), "D1: restored")
		self.assertEqual(out["error"]["kind"], "not_fixable")
		self.assertIn("was undone", out["error"]["message"])
		self.assertEqual(self.stored("label", "description"), ("Made", "Before"), "the before-image is back")
		self.assertEqual(frappe.get_meta(self.dt).get_field("jcf_made").label, "Made", "cache cleared")
		self.assertIn("was undone", self.last_continuation())
		self.assertEqual(self.audit_outcomes("update_doc"), ["failed"])

	def test_clean_up_never_overwrites_a_later_edit(self):
		"""The worker died before the card's save; the same person then edited the
		field in Desk. The reconciler must leave that edit alone."""
		name = self.park_edit(label="Renamed")

		def _killed(tool, args):
			raise SystemExit("worker killed")

		with patch("jarvis.api.dispatch", side_effect=_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		with as_user(SM_USER):
			doc = frappe.get_doc(CF, self.field)
			doc.label = "Set in Desk"
			doc.save()
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11), "n": name},
		)
		frappe.db.commit()
		self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Failed")
		self.assertEqual(self.stored("label"), "Set in Desk")

	def test_an_edit_made_after_the_cards_own_save_is_kept(self):
		"""The card's save committed (and was stamped), the worker died, and the field
		was then edited in Desk: that later edit is not this confirmation's to undo."""
		name = self.park_edit(label="Renamed")
		real = api.dispatch

		def _saved_then_killed(tool, args):
			real(tool, args)
			raise SystemExit("worker killed")

		with patch("jarvis.api.dispatch", side_effect=_saved_then_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		with as_user(SM_USER):
			doc = frappe.get_doc(CF, self.field)
			doc.label = "Set in Desk"
			doc.save()
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11), "n": name},
		)
		frappe.db.commit()
		self.reconcile_own(name)
		self.assertEqual(self.stored("label"), "Set in Desk")
		self.assertTrue(self.logged("structure_cleanup", "changed again"))

	def test_the_reconciler_restores_an_interrupted_edit(self):
		name = self.park_edit(label="Renamed")
		real = api.dispatch

		def _saved_then_killed(tool, args):
			real(tool, args)  # the edit commits (updatedb), then the worker dies
			raise SystemExit("worker killed")

		with patch("jarvis.api.dispatch", side_effect=_saved_then_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		self.assertEqual(self.stored("label"), "Renamed")
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11), "n": name},
		)
		frappe.db.commit()
		self.reconcile_own(name)
		self.assertEqual(self.stored("label"), "Made", "back to the before-image")
		self.assertTrue(self.logged("structure_cleanup", "was undone"))

	def test_an_edit_park_never_touches_the_form_other_requests_read(self):
		"""B1, the edit: an unconfirmed {reqd, hidden, label} must not be enforced."""
		self.park_edit(label="Renamed", reqd=1, read_only=1)
		for meta in (frappe.get_meta(self.dt), frappe.get_meta(self.dt, cached=False)):
			df = meta.get_field("jcf_made")
			self.assertEqual((df.label, df.reqd, df.hidden), ("Made", 0, 0))
		client_cache = getattr(frappe, "client_cache", None)
		cached = client_cache.get_value(f"doctype_meta::{self.dt}") if client_cache is not None else None
		if cached:
			self.assertEqual(cached.get_field("jcf_made").reqd, 0)
		with as_user(SM_USER):
			frappe.get_doc({"doctype": self.dt, "title": "no jcf_made given"}).insert()
		frappe.db.rollback()

	def test_column_changes_are_refused_however_they_are_spelled(self):
		"""B2, the edit."""
		for key in ("unique", "search_index", "is_virtual"):
			for spelling in (True, "true", "True", "yes", "on", "1", 1, " 1 "):
				with self.subTest(key=key, spelling=spelling):
					self.assert_refused(
						self.run_tool("update_doc", self.edit(**{key: spelling})), "structure_refused"
					)
		for spelling in ("1,000", "1000", 1000, " 1000 "):
			with self.subTest(length=spelling):
				self.assert_refused(
					self.run_tool("update_doc", self.edit(length=spelling)), "structure_refused"
				)
		self.assertEqual(self.stored("unique", "search_index", "length"), (0, 0, 0))
		self.assertEqual(_columns(self.dt), _columns(self.dt))

	def test_an_edit_gets_the_same_target_and_value_rules(self):
		"""M3 and the minors: a denied target, a Link to nothing, a default that would
		leave the table drifting, a fetch that does not resolve or reads a secret."""
		with patch.object(cfg, "_DENIED_TARGETS", cfg._DENIED_TARGETS | {self.dt}):
			r = self.run_tool("update_doc", self.edit(label="x"))
		self.assert_refused(r, "structure_refused", self.dt)
		for changes, says in (
			({"default": "it's"}, "default"),
			({"fetch_from": "who.no_such_field"}, "fetch_from"),
			({"fetch_from": "who.api_key"}, "fetch_from"),
			({"fieldtype": "Data", "options": "x", "fetch_from": "nope.full_name"}, "fetch_from"),
		):
			with self.subTest(changes=changes):
				self.assert_refused(self.run_tool("update_doc", self.edit(**changes)), says=says)
		with as_user(SM_USER):
			link = frappe.get_doc(
				{
					"doctype": CF,
					"dt": self.dt,
					"fieldname": "jcf_link",
					"label": "L",
					"fieldtype": "Link",
					"options": "User",
				}
			).insert()
		frappe.db.commit()
		r = self.run_tool(
			"update_doc", {"doctype": CF, "name": link.name, "changes": {"options": "Jarvis No Such DocType"}}
		)
		self.assert_refused(r, says="Link")

	def test_repointing_a_link_checks_every_fetch_through_it(self):
		"""N1. A fetch through the link must still resolve, and still be allowed,
		against the link's NEW target; else every insert of the form breaks."""
		with as_user(SM_USER):
			link = frappe.get_doc(
				{
					"doctype": CF,
					"dt": self.dt,
					"fieldname": "jcf_link",
					"label": "L",
					"fieldtype": "Link",
					"options": "User",
				}
			).insert()
			frappe.get_doc(
				{
					"doctype": CF,
					"dt": self.dt,
					"fieldname": "jcf_f2",
					"label": "F2",
					"fieldtype": "Data",
					"fetch_from": "jcf_link.email",
				}
			).insert()
		frappe.db.commit()
		for target in ("DocType", "Role"):
			r = self.run_tool(
				"update_doc", {"doctype": CF, "name": link.name, "changes": {"options": target}}
			)
			self.assert_refused(r, says=["jcf_f2", "fetch"])
		self.assertEqual(frappe.db.get_value(CF, link.name, "options"), "User")
		with as_user(SM_USER):
			frappe.get_doc({"doctype": self.dt, "title": "still inserts"}).insert()
		frappe.db.rollback()
		# With no fetch through it, the same edit is an ordinary column-free one.
		frappe.db.delete(CF, {"name": f"{self.dt}-jcf_f2"})
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)
		r = self.run_tool("update_doc", {"doctype": CF, "name": link.name, "changes": {"options": "Role"}})
		self.assertTrue(r.get("ok"), r)

	def test_the_edit_card_shows_every_property_that_changes(self):
		"""N2, the edit."""
		args = {
			"doctype": "custom field",
			"name": self.field,
			"changes": {"read_only": "yes", "label": "New L"},
		}
		r = self.run_tool("update_doc", args)
		self.assertTrue(r.get("ok"), r)
		name = self.card_name()
		row = self.row(name)
		labels = {d["label"] for d in frappe.parse_json(row.card)["diff"]}
		meta = frappe.get_meta(CF)
		self.assertEqual(labels, {meta.get_label("read_only"), meta.get_label("label")})
		sealed = _seal.unseal_call(row)["args"]
		self.assertEqual((sealed["doctype"], sealed["changes"]), (CF, {"read_only": 1, "label": "New L"}))
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.stored("read_only", "label"), (1, "New L"))

	def test_an_edit_default_that_is_not_text_never_crashes(self):
		self.assert_refused(self.run_tool("update_doc", self.edit(default=True)), says="default")
		r = self.run_tool("update_doc", self.edit(default=7))
		self.assertFalse(r.get("ok"), r)  # a new default rewrites the column: Desk
		self.assertEqual(r["error"]["code"], "structure_refused", r)

	def test_edits_that_would_die_at_confirm_are_refused_at_park(self):
		self.assert_refused(self.run_tool("update_doc", self.edit(fieldtype=" Int ")), "structure_refused")
		self.assert_refused(self.run_tool("update_doc", self.edit(name="x", label="y")), says="name")
		self.assert_refused(self.run_tool("update_doc", self.edit(reqd=1, hidden="on")), says="default")
		# The field's own type, padded, is that type: sealed and saved as stored.
		name = self.park_edit(fieldtype=" Data ", label="Padded")
		self.assertEqual(_seal.unseal_call(self.row(name))["args"]["changes"]["fieldtype"], "Data")
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.stored("fieldtype", "label"), ("Data", "Padded"))

	def test_lowering_a_restriction_gets_the_access_line_too(self):
		frappe.db.set_value(CF, self.field, {"hidden": 1, "permlevel": 2}, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)
		name = self.park_edit(hidden=0, permlevel=0)
		self.assertIn(
			"This changes who can see or edit.", frappe.parse_json(self.row(name).card)["risk_line"]
		)
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.commit()
		name = self.park_edit(label="x", hidden=1, permlevel=2)  # echoed, unchanged
		self.assertEqual(frappe.parse_json(self.row(name).card)["risk_line"], STRUCTURAL_LINE)

	def test_an_edit_card_names_what_the_change_touches(self):
		"""D2: structural line first, then code and access as the changes call for."""
		name = self.park_edit(label="Renamed")
		self.assertEqual(frappe.parse_json(self.row(name).card)["risk_line"], STRUCTURAL_LINE)
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.commit()
		name = self.park_edit(hidden=1, depends_on="eval:doc.title", fetch_from="who.full_name")
		line = frappe.parse_json(self.row(name).card)["risk_line"]
		self.assertTrue(line.startswith(STRUCTURAL_LINE), line)
		for part in ("This runs code for every user.", "This changes who can see or edit.", "copies data"):
			self.assertIn(part, line)

	def test_a_change_frappe_normalises_is_still_restored(self):
		"""The row this confirmation wrote is known by its exact timestamp, so a value
		Frappe rewrites on save (insert_after "append", translatable on a type that
		has none) is still recognised as this confirmation's own and undone."""
		with as_user(SM_USER):
			num = frappe.get_doc(
				{"doctype": CF, "dt": self.dt, "fieldname": "jcf_num", "label": "N", "fieldtype": "Int"}
			).insert()
		frappe.db.commit()
		self.hook("cf_validation_hook")
		r = self.run_tool(
			"update_doc",
			{
				"doctype": CF,
				"name": num.name,
				"changes": {"translatable": 1, "insert_after": "append", "label": "N2"},
			},
		)
		self.assertTrue(r.get("ok"), r)
		out = self.confirm(self.card_name())
		self.assertFalse(out["ok"], out)
		self.assertIn("was undone", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(CF, num.name, "label"), "N")

	def test_a_stale_card_does_not_run(self):
		name = self.park_edit(label="Renamed")
		with as_user(SM_USER):
			doc = frappe.get_doc(CF, self.field)
			doc.description = "Changed in Desk"
			doc.save()
		frappe.db.commit()
		out = self.confirm(name)
		self.assertEqual((out["ok"], out["reason_code"]), (False, "stale"), out)
		self.assertEqual(self.stored("label"), "Made")


# --------------------------------------------------------------------------- #
# The guard itself stays as narrow as it was
# --------------------------------------------------------------------------- #
class TestGuardStaysNarrow(_ScratchBase):
	def _cf(self, fieldname: str):
		return frappe.get_doc(
			{"doctype": CF, "dt": self.dt, "fieldname": fieldname, "label": fieldname, "fieldtype": "Data"}
		)

	def test_inert_outside_a_tool_call(self):
		"""Desk, REST and scheduler saves never meet the guard."""
		with as_user(SM_USER):
			self._cf("jcf_desk").insert()
			doc = frappe.get_doc(CF, f"{self.dt}-jcf_desk")
			doc.label = "From Desk"
			doc.save()
		self.assertIn("jcf_desk", _columns(self.dt))

	def test_a_card_admits_its_one_field_and_nothing_else(self):
		allow = wr.allow_entries("create_doc", self.new_field("jcf_first"))
		self.assertEqual([(a.doctype, sorted(a.risks)) for a in allow], [(CF, ["custom_field_new"])])
		with as_user(SM_USER), wr.guard_scope(allow):
			self._cf("jcf_first").insert()
			with self.assertRaises(wr.WriteRefusedError):
				self._cf("jcf_second").insert()
			with self.assertRaises(wr.WriteRefusedError):
				frappe.delete_doc(CF, f"{self.dt}-jcf_first")
			doc = frappe.get_doc("DocType", self.dt)
			with self.assertRaises(wr.WriteRefusedError):
				doc.save()
		self.assertEqual(_field_rows(self.dt), ["jcf_first"])

	def test_an_edit_card_admits_only_its_field(self):
		with as_user(SM_USER):
			self._cf("jcf_one").insert()
			self._cf("jcf_two").insert()
		one, two = f"{self.dt}-jcf_one", f"{self.dt}-jcf_two"
		allow = wr.allow_entries("update_doc", {"doctype": CF, "name": one, "changes": {"label": "x"}})
		with as_user(SM_USER), wr.guard_scope(allow):
			doc = frappe.get_doc(CF, two)
			doc.label = "other"
			with self.assertRaises(wr.WriteRefusedError):
				doc.save()
			with self.assertRaises(wr.WriteRefusedError):
				self._cf("jcf_three").insert()  # an edit card never admits a new field
			doc = frappe.get_doc(CF, one)
			doc.label = "x"
			doc.save()
		self.assertEqual(frappe.db.get_value(CF, one, "label"), "x")

	def test_no_allow_no_structure_write(self):
		with as_user(SM_USER), wr.guard_scope([]):
			with self.assertRaises(wr.WriteRefusedError):
				self._cf("jcf_none").insert()
		self.assert_untouched(fieldname="jcf_none")

	def test_a_guarded_allow_is_never_carried_into_a_job(self):
		allow = wr.allow_entries("update_doc", {"doctype": CF, "name": "x", "changes": {"label": "y"}})
		with wr.guard_scope(allow) as state:
			self.assertEqual(wr._allow_payload(state), [])
