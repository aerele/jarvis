"""Index migration patches, pinned to the thing they actually promise.

The trap they exist for: ``on_doctype_update()`` runs only when the DocType
DOCUMENT is saved (doctype.py calls ``run_module_method("on_doctype_update")``
from ``on_update``), and ``bench migrate`` re-imports a DocType only when its
``.json`` changes. Both index PRs edited ``.py`` controllers only, so on a real
bench the hook never fired, ``migrate`` exited 0, and the indexes silently never
existed. That is why "the patch runs without error" is NOT the contract worth
testing. The contract is:

    after the patch runs, the indexes EXIST -- and if they were absent, the
    patch is what created them.

So each test DROPS the real index, proves from ``information_schema`` that it is
gone, runs ``execute()``, and proves it is back. Anything weaker would pass
against a patch body of ``pass`` on a site whose indexes already existed, which
is precisely the false green these patches were written to end.

``information_schema.STATISTICS`` rather than ``SHOW INDEX``: Frappe table names
contain spaces, so column-position parsing of ``SHOW INDEX`` output silently
reads the wrong field.

Isolation: dropping an index is DDL on a shared table, and DDL implicitly
commits in MariaDB, so the framework's end-of-class rollback cannot undo it.
Every index this module touches is therefore restored in ``tearDown``, which
runs whether or not the test body raised, and again in ``tearDownClass`` as a
backstop against a crash between the drop and the restore.
"""

from unittest.mock import patch

import frappe
from frappe.modules.patch_handler import PatchType, get_patches_from_app
from frappe.tests.utils import FrappeTestCase

from jarvis.patches import v2_12_reload_agent_doctypes_for_indexes as agent_patch
from jarvis.patches import v2_12_reload_macro_doctypes_for_indexes as macro_patch
from jarvis.patches import v2_30_macro_run_conversation_index as conversation_patch
from jarvis.patches import v2_31_reload_agent_run_for_history_lock_index as history_lock_patch

# (doctype, index name, columns in SEQ_IN_INDEX order). Verified against a real
# bench with:
#   SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX)
#   FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() ...
AGENT_INDEXES = (
	("Jarvis Agent Finding", "owner_agent_fingerprint_index", ["owner", "agent", "fingerprint"]),
	("Jarvis Agent Finding", "owner_severity_state_index", ["owner", "severity", "state"]),
	("Jarvis Agent Run", "owner_started_creation_index", ["owner", "started_at", "creation"]),
)
MACRO_INDEXES = (
	("Jarvis Macro Run", "owner_creation_index", ["owner", "creation"]),
	("Jarvis Macro", "owner_macro_name_index", ["owner", "macro_name"]),
)
HISTORY_LOCK_INDEXES = (
	("Jarvis Agent Run", "conversation_agent_status_index", ["conversation", "agent", "status"]),
)
CONVERSATION_INDEX = (
	conversation_patch.DOCTYPE,
	conversation_patch.INDEX_NAME,
	conversation_patch.INDEX_FIELDS,
)
CONVERSATION_SEARCH_INDEX_SETTER = {
	"doc_type": conversation_patch.DOCTYPE,
	"field_name": conversation_patch.FIELDNAME,
	"property": "search_index",
}
CONVERSATION_PATCH_ENTRY = "jarvis.patches.v2_30_macro_run_conversation_index"


def _index_columns(doctype: str, index_name: str) -> list[str]:
	"""The index's columns as the DATABASE reports them, in index order.

	Empty list means the index does not exist.
	"""
	return frappe.db.sql(
		"""
		SELECT COLUMN_NAME
		FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA = DATABASE()
		  AND TABLE_NAME = %s
		  AND INDEX_NAME = %s
		ORDER BY SEQ_IN_INDEX
		""",
		(f"tab{doctype}", index_name),
		pluck=True,
	)


def _index_names(doctype: str) -> set[str]:
	return set(
		frappe.db.sql(
			"""
			SELECT DISTINCT INDEX_NAME
			FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
			""",
			(f"tab{doctype}",),
			pluck=True,
		)
	)


def _index_details(doctype: str, index_name: str) -> list[tuple[int, str]]:
	return list(
		frappe.db.sql(
			"""
			SELECT NON_UNIQUE, COLUMN_NAME
			FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE()
			  AND TABLE_NAME = %s
			  AND INDEX_NAME = %s
			ORDER BY SEQ_IN_INDEX
			""",
			(f"tab{doctype}", index_name),
		)
	)


def _drop_index(doctype: str, index_name: str) -> None:
	if _index_columns(doctype, index_name):
		frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP INDEX `{index_name}`")


def _restore_index(doctype: str, index_name: str, columns: list[str]) -> None:
	if not _index_columns(doctype, index_name):
		frappe.db.add_index(doctype, columns, index_name=index_name)


def _conversation_search_index_setter_rows(fields: list[str]) -> list[frappe._dict]:
	return frappe.get_all(
		"Property Setter",
		filters=CONVERSATION_SEARCH_INDEX_SETTER,
		fields=fields,
		order_by="name",
	)


def _conversation_search_index_setter_snapshots() -> list[dict]:
	return [
		{"doctype": "Property Setter", **dict(row)} for row in _conversation_search_index_setter_rows(["*"])
	]


def _delete_conversation_search_index_setters() -> None:
	for name in _conversation_search_index_setter_rows(["name"]):
		frappe.delete_doc("Property Setter", name.name, force=True, ignore_permissions=True)
	frappe.clear_cache(doctype=conversation_patch.DOCTYPE)


def _restore_conversation_state(setter_snapshots: list[dict], index_columns: list[str]) -> None:
	_delete_conversation_search_index_setters()
	for setter_snapshot in setter_snapshots:
		frappe.get_doc(setter_snapshot).insert(ignore_permissions=True)

	if _index_columns(*CONVERSATION_INDEX[:2]) != index_columns:
		_drop_index(*CONVERSATION_INDEX[:2])
		if index_columns:
			with patch.dict(frappe.flags, in_migrate=True):
				frappe.db.add_index(CONVERSATION_INDEX[0], index_columns, index_name=CONVERSATION_INDEX[1])

	frappe.clear_cache(doctype=conversation_patch.DOCTYPE)
	frappe.db.commit()


class _ReloadPatchChecks:
	"""Shared body, deliberately NOT a TestCase.

	Mixed into the two concrete cases below rather than subclassed from
	FrappeTestCase, because a TestCase base is itself collected and would run
	every test a second time against an unbound patch.
	"""

	PATCH = None
	INDEXES = ()

	@classmethod
	def _restore_all(cls):
		for doctype, index_name, columns in cls.INDEXES:
			_restore_index(doctype, index_name, columns)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Baseline for the whole class: whatever a previous crash left behind, the
		# indexes are present before the first test drops anything.
		cls._restore_all()

	@classmethod
	def tearDownClass(cls):
		cls._restore_all()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		# Unconditional: a failing assertion between the DROP and the patch call
		# must not leave the shared table without its index.
		self._restore_all()

	def test_patch_recreates_every_dropped_index(self):
		"""Drop them all, prove they are gone, run the patch, prove they are back.

		This is the whole point: ``reload_doc`` re-saves the DocType document,
		which is what fires ``on_doctype_update`` and therefore what creates the
		indexes on an EXISTING site.
		"""
		for doctype, index_name, _columns in self.INDEXES:
			_drop_index(doctype, index_name)
		for doctype, index_name, _columns in self.INDEXES:
			self.assertEqual(
				_index_columns(doctype, index_name),
				[],
				f"{index_name} on {doctype} should have been dropped before the patch runs",
			)

		self.PATCH.execute()

		for doctype, index_name, columns in self.INDEXES:
			self.assertEqual(
				_index_columns(doctype, index_name),
				columns,
				f"{index_name} on {doctype} was not recreated by the patch",
			)

	def test_running_twice_neither_errors_nor_duplicates(self):
		"""Patches re-run: ``bench migrate`` on a site that already applied this one
		still executes it after a reinstall, and a failed migrate is resumed. Two
		back-to-back runs must leave the table with exactly the indexes it had,
		with no second copy under a generated name.
		"""
		before = {doctype: _index_names(doctype) for doctype, _n, _c in self.INDEXES}

		self.PATCH.execute()
		self.PATCH.execute()

		for doctype, index_name, columns in self.INDEXES:
			self.assertEqual(_index_columns(doctype, index_name), columns)
			self.assertEqual(
				_index_names(doctype),
				before[doctype],
				f"a second run of the patch changed the index set on {doctype}",
			)

	def test_patch_is_registered_in_patches_txt(self):
		"""An unregistered patch never runs, which reproduces the exact silent
		failure it was written to fix."""
		entries = {
			line.strip()
			for line in frappe.get_file_items(frappe.get_app_path("jarvis", "patches.txt"))
			if line.strip()
		}
		self.assertIn(f"jarvis.patches.{self.PATCH.__name__.rsplit('.', 1)[-1]}", entries)

	def test_every_named_doctype_actually_defines_the_hook(self):
		"""``reload_doc`` on a doctype with no ``on_doctype_update`` is a no-op that
		still looks like a successful patch. Pin the patch's DOCTYPES list to
		modules that really carry the hook, so a rename cannot quietly empty it."""
		for scrubbed in self.PATCH.DOCTYPES:
			module = frappe.get_module(f"jarvis.jarvis.doctype.{scrubbed}.{scrubbed}")
			self.assertTrue(
				callable(getattr(module, "on_doctype_update", None)),
				f"{scrubbed} is named by the patch but defines no on_doctype_update",
			)


class TestReloadAgentDoctypesForIndexes(_ReloadPatchChecks, FrappeTestCase):
	PATCH = agent_patch
	INDEXES = AGENT_INDEXES


class TestReloadMacroDoctypesForIndexes(_ReloadPatchChecks, FrappeTestCase):
	PATCH = macro_patch
	INDEXES = MACRO_INDEXES


class TestReloadAgentRunForHistoryLockIndex(_ReloadPatchChecks, FrappeTestCase):
	PATCH = history_lock_patch
	INDEXES = HISTORY_LOCK_INDEXES


class TestMacroRunConversationIndexPatch(FrappeTestCase):
	"""The post-model-sync patch delivers a durable index on upgraded sites."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._class_setter_snapshots = _conversation_search_index_setter_snapshots()
		cls._class_index_columns = _index_columns(*CONVERSATION_INDEX[:2])
		cls._needs_class_restore = False

	@classmethod
	def tearDownClass(cls):
		try:
			if cls._needs_class_restore:
				_restore_conversation_state(cls._class_setter_snapshots, cls._class_index_columns)
		finally:
			super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._setter_snapshots = _conversation_search_index_setter_snapshots()
		self._index_columns = _index_columns(*CONVERSATION_INDEX[:2])
		self._needs_restore = False

	def tearDown(self):
		try:
			if self._needs_restore:
				_restore_conversation_state(self._setter_snapshots, self._index_columns)
		finally:
			super().tearDown()

	@staticmethod
	def _patch_mode():
		return patch.dict(frappe.flags, in_migrate=True, in_patch=True)

	def _arrange_missing_index_and_setter(self):
		self._needs_restore = True
		type(self)._needs_class_restore = True
		_delete_conversation_search_index_setters()
		_drop_index(*CONVERSATION_INDEX[:2])
		frappe.db.commit()
		self.assertEqual(_index_columns(*CONVERSATION_INDEX[:2]), [])
		self.assertEqual(_conversation_search_index_setter_rows(["name"]), [])

	def _execute_patch(self):
		with self._patch_mode():
			conversation_patch.execute()
			frappe.db.commit()

	def _add_index_in_patch_mode(self):
		with self._patch_mode():
			frappe.db.add_index(
				CONVERSATION_INDEX[0], CONVERSATION_INDEX[2], index_name=CONVERSATION_INDEX[1]
			)
			frappe.db.commit()

	def _assert_conversation_index(self):
		self.assertEqual(_index_columns(*CONVERSATION_INDEX[:2]), CONVERSATION_INDEX[2])
		self.assertEqual(
			_index_details(*CONVERSATION_INDEX[:2]),
			[(1, conversation_patch.FIELDNAME)],
		)

	def _assert_generated_search_index_setter(self):
		setters = _conversation_search_index_setter_rows(
			[
				"name",
				"doc_type",
				"doctype_or_field",
				"field_name",
				"property",
				"value",
				"property_type",
				"is_system_generated",
			]
		)
		self.assertEqual(len(setters), 1)
		setter = setters[0]
		self.assertEqual(
			setter.name,
			f"{conversation_patch.DOCTYPE}-{conversation_patch.FIELDNAME}-search_index",
		)
		self.assertEqual(setter.doc_type, conversation_patch.DOCTYPE)
		self.assertEqual(setter.doctype_or_field, "DocField")
		self.assertEqual(setter.field_name, conversation_patch.FIELDNAME)
		self.assertEqual(setter.property, "search_index")
		self.assertEqual(setter.value, "1")
		self.assertEqual(setter.property_type, "Check")
		self.assertEqual(int(setter.is_system_generated), 1)

	def test_migrate_mode_add_index_alone_does_not_make_the_setter(self):
		self._arrange_missing_index_and_setter()

		self._add_index_in_patch_mode()

		self._assert_conversation_index()
		self.assertEqual(_conversation_search_index_setter_rows(["name"]), [])

	def test_patch_recreates_the_conversation_index_and_keeps_it_after_reload(self):
		self._arrange_missing_index_and_setter()

		self._execute_patch()

		self._assert_conversation_index()
		self._assert_generated_search_index_setter()

		with self._patch_mode():
			frappe.reload_doc("jarvis", "doctype", "jarvis_macro_run", force=True)

		self._assert_conversation_index()
		self._assert_generated_search_index_setter()

	def test_patch_adds_metadata_for_an_existing_migrate_mode_index(self):
		self._arrange_missing_index_and_setter()
		self._add_index_in_patch_mode()

		self._execute_patch()

		self._assert_conversation_index()
		self._assert_generated_search_index_setter()

		with self._patch_mode():
			frappe.reload_doc("jarvis", "doctype", "jarvis_macro_run", force=True)

		self._assert_conversation_index()

	def test_patch_is_idempotent(self):
		self._arrange_missing_index_and_setter()

		self._execute_patch()
		first_index_names = _index_names(CONVERSATION_INDEX[0])
		self._assert_generated_search_index_setter()
		self._execute_patch()

		self._assert_conversation_index()
		self.assertEqual(_index_names(CONVERSATION_INDEX[0]), first_index_names)
		self._assert_generated_search_index_setter()

	def test_patch_is_registered(self):
		entries = frappe.get_file_items(frappe.get_app_path("jarvis", "patches.txt"))
		self.assertEqual(entries.count(CONVERSATION_PATCH_ENTRY), 1)

	def test_patch_is_registered_post_model_sync(self):
		entries = get_patches_from_app("jarvis", PatchType.post_model_sync)
		self.assertEqual(entries.count(CONVERSATION_PATCH_ENTRY), 1)
