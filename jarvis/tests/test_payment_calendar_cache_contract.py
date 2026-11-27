"""Payment Calendar S4b: what the cache modules share with the reader and with each other, checked by the source.

* `reader_contract.NAMES` lists every name of the reader modules that the cache uses; each must exist, and no cache
  module may use a name that is not listed (a rename in `sources` or `builtin_reader` fails here, not at night);
* the row kinds, the open-set window and the Redis key prefix each have one definition;
* every Redis key of the cache starts with the prefix that the uninstall deletes by.

Run:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_contract
"""

import ast
import importlib
from pathlib import Path
from unittest import TestCase

import frappe

from jarvis.payment_calendar import builtin_reader, cache_state, reader_contract, routing

PACKAGE = Path(routing.__file__).parent
CACHE_FILES = sorted(PACKAGE.glob("cache*.py"))
# How a cache module may call a reader module: `sources` is imported as `today_sources` in `cache_shadow`.
ALIASES = {"today_sources": "sources"}


def module(name: str):
	return importlib.import_module(f"jarvis.payment_calendar.{name}")


def used_names(path: Path) -> dict[str, set[str]]:
	"""`{reader module: names}` that `path` reads as an attribute or imports with `from ... import`."""
	found: dict[str, set[str]] = {}
	tree = ast.parse(path.read_text())
	for node in ast.walk(tree):
		if (
			isinstance(node, ast.Attribute)
			and isinstance(node.value, ast.Name)
			and ALIASES.get(node.value.id, node.value.id) in reader_contract.NAMES
		):
			found.setdefault(ALIASES.get(node.value.id, node.value.id), set()).add(node.attr)
		if (
			isinstance(node, ast.ImportFrom)
			and node.module
			and node.module.startswith("jarvis.payment_calendar.")
		):
			name = node.module.rsplit(".", 1)[1]
			if name in reader_contract.NAMES:
				found.setdefault(name, set()).update(alias.name for alias in node.names)
	return found


class TestReaderContract(TestCase):
	def test_every_listed_name_exists(self):
		missing = [
			f"{name}.{attr}"
			for name, attrs in reader_contract.NAMES.items()
			for attr in attrs
			if not hasattr(module(name), attr)
		]
		self.assertEqual(missing, [])

	def test_the_engine_methods_that_the_cache_overrides_exist_on_the_readers_engine(self):
		engine = builtin_reader._builtin_class()
		for method in reader_contract.ENGINE_METHODS:
			self.assertTrue(callable(getattr(engine, method, None)), method)

	def test_every_name_a_cache_module_uses_is_listed(self):
		unlisted = []
		for path in CACHE_FILES:
			for name, attrs in used_names(path).items():
				unlisted += [
					f"{path.name}: {name}.{attr}" for attr in sorted(attrs - set(reader_contract.NAMES[name]))
				]
		self.assertEqual(unlisted, [])

	def test_the_scan_sees_the_names_the_cache_really_uses(self):
		seen = used_names(PACKAGE / "cache_refresh.py")
		self.assertIn("_assemble", seen["sources"])
		self.assertIn("_builtin_class", seen["builtin_reader"])
		self.assertIn("InvoiceReconciler", seen["reconciliation"])
		self.assertIn("_ledger_predicate", used_names(PACKAGE / "cache_shadow.py")["builtin_reader"])


def strings_in_code(path: Path) -> set[str]:
	"""Every string constant of `path` that is not a docstring or a comment."""
	tree = ast.parse(path.read_text())
	docstrings = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			body = getattr(node, "body", [])
			if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
				docstrings.add(id(body[0].value))
	return {
		node.value
		for node in ast.walk(tree)
		if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
	}


class TestOneDefinition(TestCase):
	def definers(self, literal: str) -> list[str]:
		"""The cache modules (and `lifecycle`) with a code string that holds `literal`."""
		files = [*CACHE_FILES, PACKAGE / "lifecycle.py"]
		return [path.stem for path in files if any(literal in text for text in strings_in_code(path))]

	def test_the_fx_kind_the_bank_kind_and_the_window_are_written_once(self):
		self.assertEqual(self.definers("settled_fx"), ["cache_store"])
		self.assertEqual(self.definers("1900-01-01"), ["cache_store"])

	def test_the_modules_that_use_them_use_the_stores_definition(self):
		from jarvis.payment_calendar import cache_refresh, cache_shadow, cache_store

		self.assertEqual((cache_store.KIND_EXCLUDED, cache_store.KIND_BANK), ("settled_fx", "bank"))
		for name in ("KIND_EXCLUDED", "KIND_BANK", "OPEN_SET_WINDOW", "_OPEN_WINDOW"):
			self.assertFalse(hasattr(cache_shadow, name), name)
		self.assertFalse(hasattr(cache_refresh, "OPEN_SET_WINDOW"))

	def test_the_redis_prefix_is_written_once(self):
		self.assertEqual(self.definers("jarvis:pcal:cache:"), ["cache_state"])


def text(key) -> str:
	return key.decode() if isinstance(key, bytes) else str(key)


class TestKeysShareThePrefix(TestCase):
	def test_every_site_key_of_the_cache_starts_with_the_prefix_the_uninstall_deletes_by(self):
		from jarvis.payment_calendar import cache, cache_alerts

		prefix = text(frappe.cache.make_key(cache_state.CACHE_KEY_PREFIX))
		keys = [
			cache._night_key("d" * 16, "Bank"),
			cache_state.cache_key("anything", "else"),
			frappe.cache.make_key(f"{cache_alerts.RECOVERY_PREFIX}x"),
		]
		for key in keys:
			self.assertTrue(text(key).startswith(prefix), key)

	def test_a_slot_key_is_shared_across_the_sites_of_the_bench_and_so_is_not_under_the_site_prefix(self):
		site = text(frappe.cache.make_key(cache_state.CACHE_KEY_PREFIX))
		self.assertFalse(text(cache_state.slot_key(0)).startswith(site))
		self.assertTrue(text(cache_state.slot_key(0)).startswith("jarvis:pcal:cache:slot:"))
