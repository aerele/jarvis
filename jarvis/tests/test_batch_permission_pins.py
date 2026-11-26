"""Source pins and identity checks for the batch permission evaluator (plan S2b-batch, sections 5.5, 5.8, 5.8a).

The identity tests rebind one real Frappe binding at a time and always restore it, so no test leaves a
Frappe name patched. Source-hash tests patch the accepted hash, never the Frappe callable.
"""

import functools
import gc
import hashlib
import importlib
import inspect
import os
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import FunctionType
from unittest.mock import patch

import frappe
from frappe.model import document as document_module
from frappe.model.document import Document, LazyChildTable, LazyDocument, _DocumentBase
from frappe.model.meta import Meta
from frappe.tests.utils import FrappeTestCase

from jarvis.payment_calendar import compatibility

MAJOR = "16"


def _plain(*args, **kwargs):
	return None


def _wrapping(function):
	@functools.wraps(function)
	def wrapper(*args, **kwargs):
		return function(*args, **kwargs)

	return wrapper


@contextmanager
def rebound(owner, attribute, replacement):
	"""Rebind `owner.attribute` for the block and put the original back, whatever happens."""
	saved = owner.__dict__[attribute] if isinstance(owner, type) else getattr(owner, attribute)
	setattr(owner, attribute, replacement)
	try:
		yield
	finally:
		setattr(owner, attribute, saved)


def forget_process_memos():
	"""Make this process look freshly forked: no parsed file, resolved path or resolved name is remembered."""
	for memo in (
		compatibility._source_indexes,
		compatibility._real_paths,
		compatibility._pin_sources,
		compatibility._source_paths,
		compatibility._binding_modules,
	):
		memo.clear()


def drop_persisted(directory):
	"""Delete the Redis records of the source files under a temporary directory."""
	real = os.path.realpath(directory)
	pattern = frappe.cache.make_key(f"jarvis:pcal:srcdefs:{real}*")
	for key in frappe.cache.execute_command("KEYS", pattern) or []:
		frappe.cache.execute_command("DEL", key)


def _genuine(live, row):
	"""The pinned definition under foreign wrappers (a profiler or a test patch), or None."""
	seen = set()
	while live is not None and id(live) not in seen:
		seen.add(id(live))
		if compatibility._is_definition(
			live, row.wrapper[0] if row.wrapper else row.file, row.wrapper[1] if row.wrapper else row.qualname
		):
			return live
		live = getattr(live, "_profiler_original", None) or getattr(live, "__wrapped__", None)
	return None


@contextmanager
def clean_bindings():
	"""Put the genuine Frappe function back under every foreign wrapper while a test runs.

	Apps in `apps.txt` (a profiler, `ugly_code`) patch Frappe in every bench process. The identity check
	rightly rejects those, so the tests that need a clean site remove them for their own duration only.
	"""
	saved = []
	try:
		for row in _module_rows():
			module = importlib.import_module(row.binding)
			live = getattr(module, row.attribute)
			genuine = _genuine(live, row)
			if genuine is not None and genuine is not live:
				saved.append((module, row.attribute, live))
				setattr(module, row.attribute, genuine)
		yield
	finally:
		for module, attribute, live in reversed(saved):
			setattr(module, attribute, live)


class CleanBindingsCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		manager = clean_bindings()
		manager.__enter__()
		cls.addClassCleanup(manager.__exit__, None, None, None)


@contextmanager
def registered(cls, doctype="Currency"):
	"""Make `cls` the lazy controller of a doctype without tables, as `get_lazy_controller` would."""
	registry = frappe.lazy_controllers.setdefault(frappe.local.site, {})
	saved = registry.get(doctype)
	registry[doctype] = cls
	try:
		yield
	finally:
		if saved is None:
			registry.pop(doctype, None)
		else:
			registry[doctype] = saved


@contextmanager
def without_attribute(owner, attribute):
	saved = owner.__dict__[attribute]
	delattr(owner, attribute)
	try:
		yield
	finally:
		setattr(owner, attribute, saved)


def _controllers(doctype):
	meta = frappe.get_meta(doctype)
	children = [document_module.get_controller(df.options) for df in meta.get_table_fields()]
	return document_module.get_lazy_controller(doctype), children


def _module_rows():
	"""B and S rows that bind a module attribute and apply on this site."""
	return [
		row
		for row in compatibility.PERMISSION_LOOKUP
		if row.kind in ("B", "S")
		and compatibility._is_module(row.binding)
		and compatibility._row_applies(row)
	]


class TestDefinedSourceHash(CleanBindingsCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = Path(directory.name)
		self.addCleanup(drop_persisted, self.directory)

	def _write(self, name, text):
		path = self.directory / name
		path.write_text(text)
		return str(path)

	def test_hash_equals_the_inspect_source_of_functions_methods_and_nested_functions(self):
		from frappe.permissions import get_doc_name

		for qualname, target in (
			("get_doc_name", get_doc_name),
			("Document.get", Document.get),
			("Document._table_fieldnames", Document._table_fieldnames.func),
		):
			with self.subTest(qualname=qualname):
				file = inspect.getsourcefile(target)
				self.assertEqual(
					compatibility.defined_source_hash(file, qualname),
					hashlib.sha256(inspect.getsource(target).encode()).hexdigest(),
				)

	def test_nested_locals_and_decorators_are_found(self):
		import frappe.permissions as permissions

		inner = permissions.has_permission
		path = inspect.getsourcefile(inner.__wrapped__)
		qualname = "print_has_permission_check_logs.<locals>.inner"
		self.assertEqual(
			compatibility.defined_source_hash(path, qualname),
			hashlib.sha256(inspect.getsource(inner.__code__).encode()).hexdigest(),
		)
		# A decorated definition starts at its first decorator, so the decorator is inside the hash.
		self.assertEqual(
			compatibility.defined_source_hash(path, "has_permission"),
			hashlib.sha256(inspect.getsource(inner.__wrapped__).encode()).hexdigest(),
		)
		self.assertIn(
			"@print_has_permission_check_logs",
			inspect.getsource(inner.__wrapped__),
		)

	def test_unknown_name_and_unreadable_file_give_none(self):
		path = self._write("a.py", "def f():\n\treturn 1\n")
		self.assertIsNone(compatibility.defined_source_hash(path, "g"))
		self.assertIsNone(compatibility.defined_source_hash(path, "f.<locals>.g"))
		self.assertIsNone(compatibility.defined_source_hash(str(self.directory / "missing.py"), "f"))
		broken = self._write("broken.py", "def f(:\n")
		self.assertIsNone(compatibility.defined_source_hash(broken, "f"))

	def test_second_call_parses_nothing(self):
		path = self._write("a.py", "def f():\n\treturn 1\n")
		compatibility.defined_source_hash(path, "f")
		with patch.object(compatibility.ast, "parse", wraps=compatibility.ast.parse) as parse:
			compatibility.defined_source_hash(path, "f")
		parse.assert_not_called()

	def test_a_changed_mtime_reparses_that_file_only(self):
		first = self._write("a.py", "def f():\n\treturn 1\n")
		second = self._write("b.py", "def f():\n\treturn 1\n")
		old = compatibility.defined_source_hash(first, "f")
		compatibility.defined_source_hash(second, "f")
		# Same size, different content: only the modification time tells the two apart.
		Path(first).write_text("def f():\n\treturn 2\n")
		stat = os.stat(first)
		os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
		with patch.object(compatibility.ast, "parse", wraps=compatibility.ast.parse) as parse:
			new = compatibility.defined_source_hash(first, "f")
			compatibility.defined_source_hash(second, "f")
		self.assertNotEqual(old, new)
		self.assertEqual(parse.call_count, 1)

	def test_a_changed_size_reparses(self):
		path = self._write("a.py", "def f():\n\treturn 1\n")
		old = compatibility.defined_source_hash(path, "f")
		stat = os.stat(path)
		Path(path).write_text("def f():\n\treturn 11\n")
		os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
		self.assertNotEqual(old, compatibility.defined_source_hash(path, "f"))

	def test_last_definition_wins_and_conditional_definitions_are_found(self):
		path = self._write(
			"a.py",
			"if True:\n\tdef f():\n\t\treturn 1\n\ndef g():\n\tdef f():\n\t\treturn 9\n\ndef f():\n\treturn 2\n",
		)
		expected = hashlib.sha256(b"def f():\n\treturn 2\n").hexdigest()
		self.assertEqual(compatibility.defined_source_hash(path, "f"), expected)
		self.assertIsNotNone(compatibility.defined_source_hash(path, "g.<locals>.f"))


class TestPermissionPins(FrappeTestCase):
	def test_installed_sources_match_the_accepted_hashes(self):
		self.assertEqual(compatibility.permission_pin_drift(), [])

	def test_every_pin_is_hashed_from_an_installed_source(self):
		for name in compatibility._PERMISSION_PINS[MAJOR]:
			with self.subTest(name=name):
				resolved = compatibility._resolve_pin(name)
				if resolved is None:
					self.assertIn(name.split(".")[0], compatibility._OPTIONAL_APPS)
					continue
				self.assertIsNotNone(compatibility.defined_source_hash(*resolved))

	def test_each_pin_drifts_when_its_accepted_hash_changes(self):
		for name in compatibility._PERMISSION_PINS[MAJOR]:
			if compatibility._resolve_pin(name) is None:
				continue
			with (
				self.subTest(name=name),
				patch.dict(compatibility._PERMISSION_PINS[MAJOR], {name: ("0" * 64,)}),
			):
				self.assertIn(name, compatibility.permission_pin_drift())

	def test_the_playground_value_check_drifts_when_a_read_ptype_becomes_a_write_ptype(self):
		if not compatibility._importable("jarvis_playground"):
			self.skipTest("jarvis_playground is not installed")
		from jarvis_playground.visitors import access

		with patch.object(access, "WRITE_PTYPES", frozenset({"write", "select"})):
			self.assertEqual(
				compatibility.permission_pin_drift(), ["jarvis_playground.visitors.access.WRITE_PTYPES"]
			)

	def test_value_check_failure_for_the_company_restriction_masters(self):
		from erpnext.stock.doctype.company_restriction import company_restriction

		self.assertEqual(company_restriction.RESTRICTABLE_MASTER_DOCTYPES, ("Item", "Customer", "Supplier"))
		with patch.object(company_restriction, "RESTRICTABLE_MASTER_DOCTYPES", ("Item", "Customer")):
			self.assertEqual(
				compatibility.permission_pin_drift(),
				[
					"erpnext.stock.doctype.company_restriction.company_restriction.RESTRICTABLE_MASTER_DOCTYPES"
				],
			)

	def test_an_unreadable_pinned_file_is_drift(self):
		name = "frappe.permissions.get_doc_name"
		with patch.object(compatibility, "defined_source_hash", return_value=None):
			self.assertIn(name, compatibility.permission_pin_drift())

	def test_an_unsupported_major_drifts_everything(self):
		with patch.object(frappe, "__version__", "17.0.0"):
			drift = compatibility.permission_pin_drift()
		self.assertIn("frappe.permissions.has_permission", drift)
		self.assertEqual(drift, sorted(drift))

	def test_playground_is_pinned_only_when_importable(self):
		name = "jarvis_playground.visitors.access.has_permission"
		self.assertIn(name, compatibility._PERMISSION_PINS[MAJOR])
		with patch.object(compatibility, "_importable", lambda package: package != "jarvis_playground"):
			self.assertNotIn(name, compatibility.permission_pin_drift())
			self.assertEqual(compatibility.permission_pin_drift(), [])

	def test_a_name_that_cannot_be_resolved_is_drift_unless_its_app_is_optional(self):
		with patch.object(compatibility, "_resolve_pin", return_value=None):
			drift = compatibility.permission_pin_drift()
		self.assertIn("erpnext.stock.doctype.company_restriction.company_restriction.has_permission", drift)
		self.assertIn("frappe.permissions.has_permission", drift)

	def test_permission_drift_never_changes_the_reader_pin_drift(self):
		before = compatibility.reader_pin_drift()
		with patch.dict(
			compatibility._PERMISSION_PINS[MAJOR], {"frappe.permissions.has_permission": ("0" * 64,)}
		):
			self.assertIn("frappe.permissions.has_permission", compatibility.permission_pin_drift())
			self.assertEqual(compatibility.reader_pin_drift(), before)

	def test_reader_code_never_parses_source(self):
		with patch.object(compatibility.ast, "parse", wraps=compatibility.ast.parse) as parse:
			compatibility.reader_pin_drift()
			compatibility.source_profile()
		parse.assert_not_called()

	def test_second_pin_check_parses_nothing_and_is_fast(self):
		compatibility.permission_pin_drift()
		with patch.object(compatibility.ast, "parse", wraps=compatibility.ast.parse) as parse:
			elapsed = min(self._timed(compatibility.permission_pin_drift) for _ in range(5))
		parse.assert_not_called()
		self.assertLessEqual(elapsed, 0.005)

	@staticmethod
	def _timed(function):
		start = time.perf_counter()
		function()
		return time.perf_counter() - start

	def test_the_controller_allowlist_is_the_e7_set(self):
		self.assertEqual(
			compatibility.CONTROLLER_ALLOWLIST,
			frozenset(
				{
					("SalesInvoice", "__init__"),
					("PurchaseInvoice", "__init__"),
					("PaymentEntry", "__init__"),
					("PaymentEntry", "setup_party_account_field"),
					("JournalEntry", "__init__"),
					("SellingController", "__setup__"),
					("BuyingController", "__setup__"),
					("SubcontractingController", "__init__"),
				}
			),
		)
		self.assertIsInstance(compatibility.CONTROLLER_ALLOWLIST, frozenset)


class TestPersistedDefinitions(CleanBindingsCase):
	"""The definitions of a file are kept in Redis, so a forked job parses nothing that nobody changed."""

	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = Path(directory.name)
		self.addCleanup(drop_persisted, self.directory)
		self.path = str(self.directory / "a.py")
		Path(self.path).write_text("def f():\n\treturn 1\n")
		self.expected = hashlib.sha256(b"def f():\n\treturn 1\n").hexdigest()

	def _parses(self, call):
		with patch.object(compatibility.ast, "parse", wraps=compatibility.ast.parse) as parse:
			result = call()
		return result, parse.call_count

	def test_a_new_process_parses_nothing_when_redis_holds_the_definitions(self):
		self.assertEqual(compatibility.defined_source_hash(self.path, "f"), self.expected)
		forget_process_memos()
		result, parses = self._parses(lambda: compatibility.defined_source_hash(self.path, "f"))
		self.assertEqual((result, parses), (self.expected, 0))
		self.assertEqual(compatibility.defined_line(self.path, "f"), 1)

	def test_the_whole_pin_and_identity_check_parses_nothing_in_a_new_process(self):
		lazy_cls, children = _controllers("Sales Invoice")
		self.assertEqual(compatibility.permission_pin_drift(), [])
		self.assertEqual(compatibility.permission_identity_failures(lazy_cls, children), [])
		forget_process_memos()

		def check():
			return compatibility.permission_pin_drift(), compatibility.permission_identity_failures(
				lazy_cls, children
			)

		result, parses = self._parses(check)
		self.assertEqual(result, ([], []))
		self.assertEqual(parses, 0)

	def test_a_stored_record_never_hides_a_pin_whose_accepted_hash_is_wrong(self):
		name = "frappe.permissions.get_doc_name"
		self.assertEqual(compatibility.permission_pin_drift(), [])  # warms the Redis records
		forget_process_memos()
		with patch.dict(compatibility._PERMISSION_PINS[MAJOR], {name: ("0" * 64,)}):
			result, parses = self._parses(compatibility.permission_pin_drift)
		self.assertEqual((result, parses), ([name], 0))

	def test_a_changed_modification_time_computes_again(self):
		compatibility.defined_source_hash(self.path, "f")
		Path(self.path).write_text("def f():\n\treturn 2\n")
		stat = os.stat(self.path)
		os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
		forget_process_memos()
		result, parses = self._parses(lambda: compatibility.defined_source_hash(self.path, "f"))
		self.assertEqual(result, hashlib.sha256(b"def f():\n\treturn 2\n").hexdigest())
		self.assertEqual(parses, 1)

	def test_a_redis_error_computes_and_never_reports_no_drift(self):
		forget_process_memos()
		with patch.object(frappe.cache, "execute_command", side_effect=RuntimeError("redis down")):
			result, parses = self._parses(lambda: compatibility.defined_source_hash(self.path, "f"))
			self.assertEqual((result, parses), (self.expected, 1))
			forget_process_memos()
			with patch.dict(
				compatibility._PERMISSION_PINS[MAJOR], {"frappe.permissions.get_doc_name": ("0" * 64,)}
			):
				self.assertEqual(compatibility.permission_pin_drift(), ["frappe.permissions.get_doc_name"])
		forget_process_memos()
		self.assertEqual(compatibility.permission_pin_drift(), [])

	def test_a_garbled_or_misshapen_record_is_a_miss(self):
		compatibility.defined_source_hash(self.path, "f")
		key = compatibility._persisted_key(
			os.path.realpath(self.path), (os.stat(self.path).st_mtime_ns, os.stat(self.path).st_size)
		)
		for value in ("not json", "[1, 2]", '{"f": "x"}', '{"f": [1, "short"]}'):
			with self.subTest(value=value):
				frappe.cache.execute_command("SET", key, value)
				forget_process_memos()
				result, parses = self._parses(lambda: compatibility.defined_source_hash(self.path, "f"))
				self.assertEqual((result, parses), (self.expected, 1))

	def test_a_record_is_a_jarvis_pcal_key_with_a_bounded_lifetime(self):
		compatibility.defined_source_hash(self.path, "f")
		keys = frappe.cache.execute_command(
			"KEYS", frappe.cache.make_key(f"jarvis:pcal:srcdefs:{os.path.realpath(self.path)}*")
		)
		self.assertEqual(len(keys), 1)
		ttl = frappe.cache.execute_command("TTL", keys[0])
		self.assertTrue(0 < ttl <= 24 * 60 * 60)
		self.assertIn("jarvis:pcal:", keys[0].decode() if isinstance(keys[0], bytes) else keys[0])


class TestReaderPinPrefixIsIntact(FrappeTestCase):
	def test_reader_pin_names_with_the_restriction_prefix_still_resolve(self):
		prefix = compatibility._RESTRICTION
		self.assertTrue(prefix.endswith("company_restriction."))
		names = [name for name in compatibility._READER_PINS["16"] if name.startswith(prefix)]
		self.assertEqual(len(names), 4)
		for name in names:
			with self.subTest(name=name):
				self.assertIsNotNone(compatibility.qualified_source_hash(name))
		self.assertEqual(compatibility.reader_pin_drift(names), [])
		self.assertEqual(
			compatibility._RESTRICTION_MODULE, "erpnext.stock.doctype.company_restriction.company_restriction"
		)


class TestLookupTable(FrappeTestCase):
	def test_row_ids_are_unique_and_kinds_are_known(self):
		rows = compatibility.PERMISSION_LOOKUP
		self.assertEqual(len({row.row for row in rows}), len(rows))
		self.assertEqual({row.kind for row in rows}, {"B", "S", "X"})

	def test_every_b_and_s_row_is_defined_in_a_pinned_source(self):
		pinned = {
			compatibility._resolve_pin(name)
			for name in compatibility._PERMISSION_PINS[MAJOR]
			if compatibility._resolve_pin(name) is not None
		}
		for row in compatibility.PERMISSION_LOOKUP:
			if row.kind == "X" or not compatibility._row_applies(row):
				continue
			for path, qualname in compatibility._row_definitions(row):
				with self.subTest(row=row.row, qualname=qualname):
					self.assertTrue(
						any(
							path == pin_path and (qualname == pin_name or qualname.startswith(pin_name + "."))
							for pin_path, pin_name in pinned
						)
					)

	def test_every_pinned_definition_exists_in_its_file(self):
		for row in compatibility.PERMISSION_LOOKUP:
			if row.kind == "X" or not compatibility._row_applies(row):
				continue
			for path, qualname in compatibility._row_definitions(row):
				with self.subTest(row=row.row, qualname=qualname):
					self.assertIsNotNone(compatibility.defined_line(path, qualname))


class TestIdentityOnTheCleanSite(CleanBindingsCase):
	def test_clean_site_passes_for_the_sales_invoice_and_its_children(self):
		for doctype in (
			"Sales Invoice",
			"Purchase Invoice",
			"Payment Entry",
			"Journal Entry",
			"Customer",
			"Supplier",
		):
			with self.subTest(doctype=doctype):
				lazy_cls, children = _controllers(doctype)
				self.assertEqual(compatibility.permission_identity_failures(lazy_cls, children), [])

	def test_the_decorated_permission_entry_passes_through_its_inner_wrapper(self):
		import frappe.permissions as permissions

		self.assertEqual(permissions.has_permission.__code__.co_qualname.split(".")[-1], "inner")
		lazy_cls, children = _controllers("Sales Invoice")
		self.assertEqual(compatibility.permission_identity_failures(lazy_cls, children), [])

	def test_failures_are_sorted_and_stable(self):
		lazy_cls, children = _controllers("Customer")
		with rebound(frappe, "is_table", _plain):
			first = compatibility.permission_identity_failures(lazy_cls, children)
			second = compatibility.permission_identity_failures(lazy_cls, iter(children))
		self.assertEqual(first, second)
		self.assertEqual(first, sorted(first))
		self.assertEqual(first, ["4"])


class TestIdentityCost(CleanBindingsCase):
	def setUp(self):
		self.lazy_cls, self.children = _controllers("Sales Invoice")
		self.assertGreaterEqual(len(self.children), 10)

	def _failures(self):
		return compatibility.permission_identity_failures(self.lazy_cls, self.children)

	def test_a_warm_identity_check_for_sales_invoice_and_its_children_is_fast(self):
		self._failures()
		timings = []
		# A full collection of the bench's large heap costs about 10 ms and is not the check's cost.
		gc.disable()
		self.addCleanup(gc.enable)
		for _ in range(20):
			start = time.perf_counter()
			self._failures()
			timings.append(time.perf_counter() - start)
		self.assertLessEqual(statistics.median(timings), 0.005)

	def test_a_warm_identity_check_resolves_no_path_and_imports_no_module(self):
		self._failures()
		with (
			patch.object(compatibility.os.path, "realpath", wraps=os.path.realpath) as realpath,
			patch.object(compatibility, "import_module", wraps=importlib.import_module) as imported,
		):
			self._failures()
		self.assertEqual((realpath.call_count, imported.call_count), (0, 0))

	def test_a_rebinding_after_a_warm_call_is_still_caught(self):
		self.assertEqual(self._failures(), [])
		with rebound(frappe, "is_table", _plain):
			self.assertEqual(self._failures(), ["4"])
		self.assertEqual(self._failures(), [])
		with rebound(Document, "get", _wrapping(Document.get)):
			self.assertEqual(self._failures(), ["36a"])
		with rebound(frappe.permissions, "get_doc_name", _wrapping(frappe.permissions.get_doc_name)):
			self.assertEqual(self._failures(), ["9"])
		with rebound(Meta, "get_link_fields", _plain):
			self.assertEqual(self._failures(), ["22-Meta.get_link_fields"])
		self.assertEqual(self._failures(), [])

	def test_a_class_binding_replaced_after_a_warm_call_is_still_caught(self):
		# The module attribute that names the class is read live; only the module is memoised.
		class OverridingMeta(Meta):
			def get_link_fields(self):
				return super().get_link_fields()

		class OverridingDocument(Document):
			def get(self, *args, **kwargs):
				return super().get(*args, **kwargs)

		self.assertEqual(self._failures(), [])
		with rebound(frappe.model.meta, "Meta", OverridingMeta):
			self.assertIn("22-Meta.get_link_fields", self._failures())
		self.assertEqual(self._failures(), [])
		with rebound(document_module, "Document", OverridingDocument):
			self.assertIn("36a", self._failures())
		self.assertEqual(self._failures(), [])

	def test_a_check_that_raises_counts_as_a_failure_and_never_escapes(self):
		with patch.object(compatibility, "_binding", side_effect=RuntimeError("boom")):
			failures = self._failures()
		self.assertIn("1", failures)
		self.assertIn("29a", failures)
		self.assertEqual(self._failures(), [])

	def test_a_row_that_raises_in_the_class_path_counts_as_a_failure(self):
		with patch.object(compatibility, "_static", side_effect=RuntimeError("boom")):
			failures = self._failures()
		self.assertIn("29", failures)
		self.assertIn("42", failures)

	def test_a_job_timeout_is_never_swallowed(self):
		with (
			patch.object(compatibility, "_binding", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			self._failures()


class TestPinFailuresAreDrift(FrappeTestCase):
	def test_an_exception_while_resolving_or_hashing_a_pin_is_drift(self):
		for target in ("_resolve_pin", "defined_source_hash"):
			with self.subTest(target=target), patch.object(compatibility, target, side_effect=RuntimeError):
				drift = compatibility.permission_pin_drift()
			self.assertIn("frappe.permissions.has_permission", drift)

	def test_a_pinned_module_that_fails_to_import_is_drift(self):
		name = "frappe.permissions.has_permission"
		with (
			patch.dict(compatibility._pin_sources, clear=True),
			patch.object(compatibility, "import_module", side_effect=RuntimeError),
		):
			self.assertIn(name, compatibility.permission_pin_drift())
		self.assertEqual(compatibility.permission_pin_drift(), [])

	def test_a_job_timeout_is_never_swallowed(self):
		with (
			patch.object(compatibility, "defined_source_hash", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			compatibility.permission_pin_drift()


class TestModuleRebinds(CleanBindingsCase):
	"""AC-B12b: rebind ONLY the named binding; its rows, and only its rows, fail."""

	def setUp(self):
		self.lazy_cls, self.children = _controllers("Sales Invoice")

	def _failures(self):
		return compatibility.permission_identity_failures(self.lazy_cls, self.children)

	def _expected(self, row):
		return sorted(
			other.row
			for other in compatibility.PERMISSION_LOOKUP
			if (other.binding, other.attribute) == (row.binding, row.attribute) and other.kind != "X"
		)

	def test_every_module_binding_is_identity_checked_against_a_plain_function(self):
		rows = _module_rows()
		self.assertGreaterEqual(len(rows), 30)
		for row in rows:
			module = importlib.import_module(row.binding)
			with self.subTest(row=row.row, binding=row.binding, attribute=row.attribute):
				with rebound(module, row.attribute, _plain):
					failures = self._failures()
				self.assertEqual(failures, self._expected(row))

	def test_every_module_binding_is_identity_checked_against_a_wraps_wrapper(self):
		for row in _module_rows():
			module = importlib.import_module(row.binding)
			with self.subTest(row=row.row, binding=row.binding, attribute=row.attribute):
				with rebound(module, row.attribute, _wrapping(getattr(module, row.attribute))):
					failures = self._failures()
				self.assertEqual(failures, self._expected(row))

	def test_get_all_with_a_wrapped_attribute_set_like_ugly_code_fails(self):
		def ugly(*args, **kwargs):
			return original(*args, **kwargs)

		original = frappe.get_all
		ugly.__wrapped__ = original
		with rebound(frappe, "get_all", ugly):
			self.assertEqual(self._failures(), ["22a"])

	def test_the_requested_names_are_rows_at_exactly_their_lookup_binding(self):
		bindings = {(row.binding, row.attribute) for row in compatibility.PERMISSION_LOOKUP}
		for pair in (
			("frappe", "get_lazy_doc"),
			("frappe.permissions", "get_doctype_ptype_map"),
			("frappe", "is_table"),
			("frappe", "get_system_settings"),
			("frappe.share", "get_shared"),
			("frappe", "call"),
			("frappe.permissions", "get_user_permissions"),
			("frappe", "get_all"),
			("frappe.model.document", "get_table_name"),
		):
			self.assertIn(pair, bindings)
		self.assertNotIn(("frappe.model.document", "get_lazy_doc"), bindings)
		self.assertNotIn(
			("frappe.core.doctype.permission_type.permission_type", "get_doctype_ptype_map"), bindings
		)

	def test_a_rewrapped_decorator_keeps_the_inner_code_but_fails_on_its_wrapped_function(self):
		import frappe.permissions as permissions

		redecorated = permissions.print_has_permission_check_logs(_plain)
		with rebound(permissions, "has_permission", redecorated):
			self.assertEqual(self._failures(), ["2", "3"])

	def test_a_function_that_copies_the_pinned_code_but_for_one_attribute_fails(self):
		original = frappe.permissions.get_doc_name.__code__
		for change in (
			{"co_filename": "/tmp/other.py"},
			{"co_qualname": "other_name"},
			{"co_firstlineno": original.co_firstlineno + 1},
		):
			with self.subTest(change=change):
				clone = FunctionType(original.replace(**change), frappe.permissions.__dict__)
				with rebound(frappe.permissions, "get_doc_name", clone):
					self.assertEqual(self._failures(), ["9"])

	def test_inspect_unwrap_is_never_used(self):
		def fake(*args, **kwargs):
			return None

		fake.__wrapped__ = frappe.is_table
		with rebound(frappe, "is_table", fake), patch.object(inspect, "unwrap", side_effect=AssertionError):
			self.assertEqual(self._failures(), ["4"])

	def test_the_bindings_are_restored_after_every_rebind(self):
		saved = {
			(row.binding, row.attribute): getattr(importlib.import_module(row.binding), row.attribute)
			for row in _module_rows()
		}
		for row in _module_rows():
			module = importlib.import_module(row.binding)
			with rebound(module, row.attribute, _plain):
				pass
		for (binding, attribute), function in saved.items():
			self.assertIs(getattr(importlib.import_module(binding), attribute), function)


class TestClassRebinds(CleanBindingsCase):
	def setUp(self):
		self.lazy_cls, self.children = _controllers("Sales Invoice")
		# A rebound Meta cached_property cannot run, so the meta must hold its values before the rebind.
		meta = frappe.get_meta("Sales Invoice")
		for name in (
			"_table_fields",
			"_non_computed_table_fields",
			"_table_doctypes",
			"_non_computed_table_doctypes",
		):
			getattr(meta, name)

	def _failures(self):
		return compatibility.permission_identity_failures(self.lazy_cls, self.children)

	def test_each_pinned_class_method_rebound_on_document_fails_its_row(self):
		cases = (
			(Document, "_init_child", "41"),
			(Document, "extend", "39"),
			(Document, "append", "40a"),
			(Document, "init_valid_columns", "43b"),
			(Document, "get_valid_columns", "43a"),
			(Document, "init_child_tables", "43"),
			(Document, "get", "36a"),
			(Document, "set", "32a"),
			(Meta, "get_link_fields", "22-Meta.get_link_fields"),
			(Meta, "get_dynamic_link_fields", "22-Meta.get_dynamic_link_fields"),
			(Meta, "get_table_fields", "22-Meta.get_table_fields"),
			(Document, "load_from_db", "30"),
			(Document, "update", "32"),
			(Document, "init_from_dict", "31"),
			(Document, "get_all_children", "35"),
			(Document, "_load_child_table_from_db", "38"),
			(LazyDocument, "append", "40"),
			(LazyDocument, "get", "36"),
			(LazyDocument, "init_child_tables", "44"),
			(LazyDocument, "load_children_from_db", "34"),
			(_DocumentBase, "__init__", "29a"),
			(Document, "__init__", "29b"),
			(LazyChildTable, "__get__", "37a"),
		)
		for owner, attribute, row in cases:
			for replacement in (_plain, None):
				with self.subTest(owner=owner.__name__, attribute=attribute, kind=bool(replacement)):
					current = owner.__dict__[attribute]
					value = replacement or _wrapping(current)
					with rebound(owner, attribute, value):
						failures = self._failures()
					self.assertTrue(
						any(item == row or item.startswith(row + ":") for item in failures), (row, failures)
					)

	def test_cached_property_bodies_are_checked_through_func(self):
		for owner, attribute, row in (
			(Document, "_table_fieldnames", "35a"),
			(Document, "_non_computed_table_fieldnames", "35b"),
			(Document, "meta", "45"),
			(Meta, "_table_fields", "22-Meta._table_fields"),
			(Meta, "_table_doctypes", "22-Meta._table_doctypes"),
			(Meta, "_non_computed_table_fields", "22-Meta._non_computed_table_fields"),
			(Meta, "_non_computed_table_doctypes", "22-Meta._non_computed_table_doctypes"),
		):
			with self.subTest(owner=owner.__name__, attribute=attribute):
				original = owner.__dict__[attribute]
				with rebound(owner, attribute, functools.cached_property(_wrapping(original.func))):
					self.assertEqual(self._failures(), [row])

	def test_a_controller_that_overrides_a_guarded_method_fails_its_row(self):
		class Controller(Document):
			def update(self, d):
				return super().update(d)

		lazy = type("LazyController", (LazyDocument, Controller), {})
		with registered(lazy):
			self.assertEqual(compatibility.permission_identity_failures(lazy, []), ["32"])

	def test_a_child_that_replaces_new_or_init_fails(self):
		class WithNew(Document):
			def __new__(cls, *args, **kwargs):
				return super().__new__(cls)

		class WithInit(Document):
			def __init__(self, *args, **kwargs):
				super().__init__(*args, **kwargs)

		self.assertEqual(compatibility.permission_identity_failures(self.lazy_cls, [WithNew]), ["42:WithNew"])
		self.assertEqual(
			compatibility.permission_identity_failures(self.lazy_cls, [WithInit]), ["42a:WithInit"]
		)

	def test_a_child_that_overrides_init_child_tables_fails(self):
		class Child(Document):
			def init_child_tables(self):
				return None

		self.assertEqual(compatibility.permission_identity_failures(self.lazy_cls, [Child]), ["43:Child"])

	def test_an_allowlisted_name_on_a_foreign_class_fails_the_allowlist_row(self):
		failures = compatibility.permission_identity_failures(_FakeLazy, [])
		self.assertIn("allowlist:SalesInvoice.__init__", failures)

	def test_a_replaced_table_descriptor_fails(self):
		fieldname = next(
			key for key, value in self.lazy_cls.__dict__.items() if isinstance(value, LazyChildTable)
		)
		with rebound(self.lazy_cls, fieldname, _plain):
			self.assertEqual(self._failures(), ["37:" + fieldname])

	def test_a_swapped_descriptor_fails(self):
		swapped = LazyChildTable("taxes", "Sales Taxes and Charges")
		with rebound(self.lazy_cls, "items", swapped):
			self.assertEqual(self._failures(), ["37:items"])

	def test_a_descriptor_for_the_wrong_child_doctype_fails(self):
		with rebound(self.lazy_cls, "items", LazyChildTable("items", "Sales Taxes and Charges")):
			self.assertEqual(self._failures(), ["37:items"])

	def test_a_missing_descriptor_fails(self):
		with without_attribute(self.lazy_cls, "payments"):
			self.assertEqual(self._failures(), ["37:payments"])

	def test_an_extra_descriptor_or_attribute_fails(self):
		for key, value in (
			("extra_table", LazyChildTable("extra_table", "Sales Taxes and Charges")),
			("anything", 1),
		):
			with self.subTest(key=key):
				setattr(self.lazy_cls, key, value)
				try:
					self.assertEqual(self._failures(), ["37:" + key])
				finally:
					delattr(self.lazy_cls, key)
				self.assertEqual(self._failures(), [])

	def test_a_subclass_of_the_descriptor_class_fails(self):
		class Sneaky(LazyChildTable):
			__slots__ = ()

		original = self.lazy_cls.__dict__["items"]
		with rebound(self.lazy_cls, "items", Sneaky(original.fieldname, original.doctype)):
			self.assertEqual(self._failures(), ["37:items"])

	def test_a_lazy_class_that_is_not_registered_cannot_be_checked_and_fails_row_37(self):
		lazy = type("LazyUnregistered", (LazyDocument, Document), {})
		self.assertEqual(compatibility.permission_identity_failures(lazy, []), ["37"])

	def test_the_standard_class_attributes_are_not_descriptors(self):
		self.assertEqual(compatibility.permission_identity_failures(self.lazy_cls, self.children), [])

	def test_a_class_that_resolves_the_pinned_definitions_through_the_mro_passes(self):
		lazy = type("LazyPlain", (LazyDocument, Document), {})
		with registered(lazy):
			self.assertEqual(compatibility.permission_identity_failures(lazy, [Document]), [])


class SalesInvoice(Document):
	"""Same name as the allowlisted controller, defined in a file the pins do not know."""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)


SalesInvoice.__qualname__ = "SalesInvoice"
_FakeLazy = type("LazySalesInvoice", (LazyDocument, SalesInvoice), {})
