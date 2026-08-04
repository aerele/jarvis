"""The two v2_12 index-reload patches, pinned to the thing they actually promise.

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

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.patches import v2_12_reload_agent_doctypes_for_indexes as agent_patch
from jarvis.patches import v2_12_reload_macro_doctypes_for_indexes as macro_patch

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


def _drop_index(doctype: str, index_name: str) -> None:
	if _index_columns(doctype, index_name):
		frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP INDEX `{index_name}`")


def _restore_index(doctype: str, index_name: str, columns: list[str]) -> None:
	if not _index_columns(doctype, index_name):
		frappe.db.add_index(doctype, columns, index_name=index_name)


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
