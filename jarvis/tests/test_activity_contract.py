"""Contract: every whitelisted jarvis endpoint is classified for the user-activity report.

A pure AST scan (the pattern of ``test_whitelist_http_methods.py``): it imports no jarvis
module, so it behaves the same on Frappe 15 and 16 and cannot trip the import-everything
landmine of jarvis#485. ``jarvis/activity_actions.py`` is READ as source and its two literal
tables are evaluated with ``ast``, which is also why ``CLASSIFICATION`` must stay a pure literal.

The self-test at the bottom plants an unclassified endpoint and a stale key in a throwaway tree
and proves the checks fail on them: a contract test that silently stopped checking is the
failure being defended against.
"""

from __future__ import annotations

import ast
import functools
import tempfile
import unittest
from pathlib import Path

from frappe.tests.utils import FrappeTestCase

from jarvis.tests.test_whitelist_http_methods import _is_whitelist, _whitelist_names

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../jarvis (the package)
REGISTRY = PACKAGE_ROOT / "activity_actions.py"

SKIP_REASONS = frozenset({"read", "poll", "ui_state", "chat", "tool_path", "desk", "self", "auth"})
EVENTS = frozenset({"action", "read", "export"})
# Files a person is handed: they must never be SKIPPED (plan 4.4: downloads are LOGGED exports).
DOWNLOADS = frozenset(
	{
		"jarvis.chat.export.download_conversation",
		"jarvis.support.media.download",
		"jarvis.account.download_invoice",
	}
)
_EXCLUDED_DIRS = frozenset({"tests", "patches"})
# Appended to every message a contributor can fix by editing the registry.
CLASSIFY_HINT = (
	" (add a ('LOGGED', ...) or ('SKIPPED', reason) entry to CLASSIFICATION in "
	"jarvis/activity_actions.py, see its docstring)"
)


class Endpoint:
	__slots__ = ("in_class", "key", "line", "params", "path")

	def __init__(self, key, path, line, params, in_class):
		self.key = key
		self.path = path
		self.line = line
		self.params = params
		self.in_class = in_class


def scan_endpoints(root: Path) -> list[Endpoint]:
	"""Every ``@frappe.whitelist`` function under ``root`` (the ``jarvis`` package dir) as
	``module.qualname``, tests and patches excluded. Nested functions get ``<locals>`` like
	``__qualname__`` does, methods their class name."""
	found = []
	for path in sorted(root.rglob("*.py")):
		rel = path.relative_to(root.parent)
		if rel.parts[1] in _EXCLUDED_DIRS:
			continue
		parts = list(rel.with_suffix("").parts)
		if parts[-1] == "__init__":
			parts.pop()
		module = ".".join(parts)
		tree = ast.parse(path.read_text(), filename=str(path))
		names = _whitelist_names(tree)
		_walk(tree, [], False, module, str(rel), names, found)
	return found


def _walk(node, quals, in_class, module, rel, names, out):
	for child in ast.iter_child_nodes(node):
		if isinstance(child, ast.ClassDef):
			_walk(child, [*quals, child.name], True, module, rel, names, out)
		elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
			qual = [*quals, child.name]
			if any(
				_is_whitelist(d.func if isinstance(d, ast.Call) else d, names) for d in child.decorator_list
			):
				params = [a.arg for a in (*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs)]
				out.append(Endpoint(f"{module}.{'.'.join(qual)}", rel, child.lineno, params, in_class))
			_walk(child, [*qual, "<locals>"], False, module, rel, names, out)
		else:
			_walk(child, quals, in_class, module, rel, names, out)


def load_registry(source: str):
	"""``(entries, duplicate_keys, label_overrides)`` from the registry SOURCE: the literal
	``CLASSIFICATION`` / ``LABEL_OVERRIDES`` assignments, evaluated without importing. Duplicate
	keys are reported (a dict literal silently keeps the last one). A non-literal value raises
	``ValueError``: it is a contract violation by itself."""
	tree = ast.parse(source)
	tables = {}
	for node in tree.body:
		if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
			if node.targets[0].id in ("CLASSIFICATION", "LABEL_OVERRIDES"):
				tables[node.targets[0].id] = node.value
	if set(tables) != {"CLASSIFICATION", "LABEL_OVERRIDES"}:
		raise ValueError("CLASSIFICATION and LABEL_OVERRIDES must both be module-level literals")
	for value in tables.values():
		if not isinstance(value, ast.Dict):
			raise ValueError("registry tables must be dict literals")
	seen, duplicates = set(), []
	for key_node in tables["CLASSIFICATION"].keys:
		key = ast.literal_eval(key_node)
		if key in seen:
			duplicates.append(key)
		seen.add(key)
	return (
		ast.literal_eval(tables["CLASSIFICATION"]),
		duplicates,
		ast.literal_eval(tables["LABEL_OVERRIDES"]),
	)


def find_problems(endpoints, entries, duplicates, labels, downloads=DOWNLOADS) -> list[str]:
	"""Every way the registry and the source can disagree. Empty means the contract holds."""
	problems = []
	by_key = {}
	for ep in endpoints:
		if ep.key in by_key:
			problems.append(f"{ep.key}: defined twice ({by_key[ep.key].path}, {ep.path})")
		by_key[ep.key] = ep

	for ep in endpoints:
		if ep.key not in entries:
			problems.append(f"{ep.path}:{ep.line} {ep.key}: whitelisted but not classified{CLASSIFY_HINT}")
		if ep.in_class:
			# The hook resolves `fn.__module__ + "." + fn.__qualname__` for a module-level
			# function; a Document method (run_doc_method) it cannot attribute.
			problems.append(f"{ep.path}:{ep.line} {ep.key}: whitelisted method of a class")
	for key in duplicates:
		problems.append(f"{key}: classified more than once")
	for key in entries:
		if key not in by_key:
			problems.append(f"{key}: classified but no such whitelisted function (stale key)")

	for key, entry in entries.items():
		ep = by_key.get(key)
		problems.extend(p + CLASSIFY_HINT for p in _entry_problems(key, entry, ep))
		name = key.rsplit(".", 1)[-1]
		if ("download" in name or "export" in name) and entry[0] != "LOGGED":
			problems.append(f"{key}: a download or export must be LOGGED, not {entry[1:]}")
	for key in downloads:
		entry = entries.get(key)
		if not entry or entry[0] != "LOGGED" or entry[2] != "export":
			problems.append(f"{key}: a download must be a LOGGED export event")
	for key in labels:
		if key not in entries or entries[key][0] != "LOGGED":
			problems.append(f"{key}: label override for something that is not a LOGGED endpoint")
	return problems


def _entry_problems(key, entry, ep) -> list[str]:
	if not isinstance(entry, tuple) or not entry:
		return [f"{key}: entry must be a tuple"]
	if entry[0] == "SKIPPED":
		if len(entry) != 2 or entry[1] not in SKIP_REASONS:
			return [f"{key}: SKIPPED needs one reason of {sorted(SKIP_REASONS)}, got {entry[1:]}"]
		return []
	if entry[0] != "LOGGED":
		return [f"{key}: first element must be LOGGED or SKIPPED"]
	if len(entry) != 5:
		return [f"{key}: LOGGED is (LOGGED, id_param, event, ref_doctype, rows)"]
	_, id_param, event, ref_doctype, rows = entry
	problems = []
	params = ep.params if ep else []
	if event not in EVENTS:
		problems.append(f"{key}: event {event!r} not in {sorted(EVENTS)}")
	if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
		problems.append(f"{key}: rows must be a non-negative int")
	if ref_doctype is not None and not (isinstance(ref_doctype, str) and ref_doctype.strip("@")):
		problems.append(f"{key}: ref_doctype must be None, a DocType name or '@param'")
	if isinstance(ref_doctype, str) and ref_doctype.startswith("@") and ref_doctype[1:] not in params:
		problems.append(f"{key}: ref_doctype {ref_doctype} is not a parameter of the function")
	if id_param is not None:
		if not isinstance(id_param, str) or id_param not in params:
			problems.append(f"{key}: id_param {id_param!r} is not a parameter of the function")
		if not isinstance(ref_doctype, str):
			# ref_name is only stored after an existence check against a known DocType: a fixed one,
			# or (``@param``) the one the request names, kept only when it IS a DocType or Report.
			problems.append(f"{key}: id_param needs a ref_doctype to be existence-checked")
	return problems


@functools.cache
def _problems_for_repo():
	endpoints = scan_endpoints(PACKAGE_ROOT)
	entries, duplicates, labels = load_registry(REGISTRY.read_text())
	return endpoints, entries, find_problems(endpoints, entries, duplicates, labels)


class TestActivityContract(FrappeTestCase):
	def test_the_scan_finds_the_endpoints(self):
		endpoints, entries, _ = _problems_for_repo()
		self.assertGreater(len(endpoints), 400)
		self.assertEqual(len(endpoints), len(entries))

	def test_every_whitelisted_function_has_exactly_one_entry_and_every_entry_is_real(self):
		_, _, problems = _problems_for_repo()
		self.assertEqual(problems, [], "\n" + "\n".join(problems))

	def test_downloads_and_exports_are_logged(self):
		_, entries, _ = _problems_for_repo()
		for key in DOWNLOADS:
			self.assertEqual(entries[key][0], "LOGGED", key)
			self.assertEqual(entries[key][2], "export", key)
		for key, entry in entries.items():
			name = key.rsplit(".", 1)[-1]
			if "download" in name or "export" in name:
				self.assertEqual(entry[0], "LOGGED", key)


# --------------------------------------------------------------------------- #
# self-test: plant violations, prove the gate fires
# --------------------------------------------------------------------------- #
_PLANTED_MODULE = """
import frappe
from frappe import whitelist

@frappe.whitelist(methods=["POST"])
def classified(name: str):
	return 1

@frappe.whitelist(methods=["POST"])
def unclassified_new_endpoint():
	return 2

@whitelist()
def bare_import_unclassified():
	return 3

class Controller:
	@frappe.whitelist()
	def run(self):
		return 4

def _inner_holder():
	@frappe.whitelist()
	def nested():
		return 5
"""

_PLANTED_REGISTRY = """
CLASSIFICATION = {
	"jarvis.planted.classified": ("LOGGED", "name", "action", "ToDo", 0),
	"jarvis.planted.deleted_long_ago": ("SKIPPED", "read"),
}
LABEL_OVERRIDES = {}
"""


class TestContractSelfTest(unittest.TestCase):
	"""Plain unittest: it needs no site."""

	def _tree(self, module_source):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		root = Path(tmp.name) / "jarvis"
		(root / "tests").mkdir(parents=True)
		(root / "patches").mkdir()
		(root / "planted.py").write_text(module_source)
		(root / "tests" / "test_ignored.py").write_text(_PLANTED_MODULE)
		(root / "patches" / "p.py").write_text(_PLANTED_MODULE)
		return root

	def _problems(self, module_source, registry_source):
		root = self._tree(module_source)
		endpoints = scan_endpoints(root)
		entries, duplicates, labels = load_registry(registry_source)
		return endpoints, find_problems(endpoints, entries, duplicates, labels, downloads=())

	def test_an_unclassified_function_and_a_stale_key_both_fail(self):
		endpoints, problems = self._problems(_PLANTED_MODULE, _PLANTED_REGISTRY)
		joined = "\n".join(problems)
		self.assertIn("jarvis.planted.unclassified_new_endpoint: whitelisted but not classified", joined)
		self.assertIn("jarvis.planted.bare_import_unclassified: whitelisted but not classified", joined)
		self.assertIn("jarvis.planted.deleted_long_ago: classified but no such whitelisted function", joined)
		# the class method and the nested function are found, and the method is refused
		keys = {ep.key for ep in endpoints}
		self.assertIn("jarvis.planted.Controller.run", keys)
		self.assertIn("jarvis.planted._inner_holder.<locals>.nested", keys)
		self.assertIn("jarvis.planted.Controller.run: whitelisted method of a class", joined)
		# tests/ and patches/ are not scanned
		self.assertFalse([k for k in keys if "test_ignored" in k or ".p." in k])

	def test_a_clean_tree_has_no_problems(self):
		module = _PLANTED_MODULE.split('@frappe.whitelist(methods=["POST"])\ndef unclassified')[0]
		registry = 'CLASSIFICATION = {"jarvis.planted.classified": ("LOGGED", "name", "action", "ToDo", 0)}\nLABEL_OVERRIDES = {}\n'
		_, problems = self._problems(module, registry)
		self.assertEqual(problems, [])

	def test_a_duplicate_key_is_reported(self):
		registry = (
			"CLASSIFICATION = {\n"
			'\t"jarvis.planted.classified": ("SKIPPED", "read"),\n'
			'\t"jarvis.planted.classified": ("SKIPPED", "poll"),\n'
			"}\nLABEL_OVERRIDES = {}\n"
		)
		module = "import frappe\n@frappe.whitelist()\ndef classified(name):\n\treturn 1\n"
		_, problems = self._problems(module, registry)
		self.assertEqual(problems, ["jarvis.planted.classified: classified more than once"])

	def test_a_download_that_is_skipped_fails(self):
		registry = (
			'CLASSIFICATION = {"jarvis.planted.download_report": ("SKIPPED", "read")}\nLABEL_OVERRIDES = {}\n'
		)
		module = "import frappe\n@frappe.whitelist()\ndef download_report():\n\treturn 1\n"
		_, problems = self._problems(module, registry)
		self.assertTrue(any("download or export must be LOGGED" in p for p in problems), problems)

	def test_malformed_entries_fail(self):
		module = (
			"import frappe\n"
			"@frappe.whitelist()\ndef a(name):\n\treturn 1\n"
			"@frappe.whitelist()\ndef b(name):\n\treturn 1\n"
			"@frappe.whitelist()\ndef c(name):\n\treturn 1\n"
			"@frappe.whitelist()\ndef d(name):\n\treturn 1\n"
		)
		registry = (
			"CLASSIFICATION = {\n"
			'\t"jarvis.planted.a": ("SKIPPED", "not-a-reason"),\n'
			'\t"jarvis.planted.b": ("LOGGED", "missing_param", "action", "ToDo", 0),\n'
			'\t"jarvis.planted.c": ("LOGGED", "name", "action", None, 0),\n'
			'\t"jarvis.planted.d": ("LOGGED", None, "teleport", "@absent", -1),\n'
			'}\nLABEL_OVERRIDES = {"jarvis.planted.a": "x"}\n'
		)
		_, problems = self._problems(module, registry)
		joined = "\n".join(problems)
		for fragment in (
			"jarvis.planted.a: SKIPPED needs one reason",
			"jarvis.planted.b: id_param 'missing_param' is not a parameter",
			"jarvis.planted.c: id_param needs a ref_doctype",
			"jarvis.planted.d: event 'teleport'",
			"jarvis.planted.d: rows must be",
			"jarvis.planted.d: ref_doctype @absent is not a parameter",
			"jarvis.planted.a: label override for something that is not a LOGGED endpoint",
		):
			self.assertIn(fragment, joined)

	def test_an_unclassified_endpoint_fails_with_the_hint_on_how_to_fix_it(self):
		# D7: a contributor adding an endpoint sees this message first; it must say what to do.
		_, problems = self._problems(_PLANTED_MODULE, _PLANTED_REGISTRY)
		missing = [p for p in problems if "whitelisted but not classified" in p]
		self.assertTrue(missing)
		for problem in missing:
			self.assertTrue(problem.endswith(CLASSIFY_HINT), problem)
		self.assertIn("CLASSIFICATION in jarvis/activity_actions.py", CLASSIFY_HINT)
		self.assertIn("('LOGGED', ...)", CLASSIFY_HINT)
		self.assertIn("('SKIPPED', reason)", CLASSIFY_HINT)

	def test_a_malformed_entry_carries_the_hint_too(self):
		module = "import frappe\n@frappe.whitelist()\ndef a(name):\n\treturn 1\n"
		registry = 'CLASSIFICATION = {"jarvis.planted.a": ("SKIPPED", "nope")}\nLABEL_OVERRIDES = {}\n'
		_, problems = self._problems(module, registry)
		self.assertTrue(problems)
		self.assertTrue(all(p.endswith(CLASSIFY_HINT) for p in problems), problems)

	def test_an_id_param_may_pair_with_a_doctype_taken_from_another_parameter(self):
		# D2: load_doc(doctype, name): the record's name is existence-checked against the
		# doctype the REQUEST names, so a ``@param`` ref_doctype is a valid partner of id_param.
		module = "import frappe\n@frappe.whitelist()\ndef a(doctype, name):\n\treturn 1\n"
		ok = 'CLASSIFICATION = {"jarvis.planted.a": ("LOGGED", "name", "read", "@doctype", 1)}\nLABEL_OVERRIDES = {}\n'
		self.assertEqual(self._problems(module, ok)[1], [])
		# but not with no doctype at all
		bad = 'CLASSIFICATION = {"jarvis.planted.a": ("LOGGED", "name", "read", None, 1)}\nLABEL_OVERRIDES = {}\n'
		self.assertTrue(any("id_param needs a ref_doctype" in p for p in self._problems(module, bad)[1]))

	def test_the_runbook_names_the_contract_and_the_registry_points_at_it(self):
		from jarvis import activity_actions, activity_rollup

		runbook = activity_rollup.__doc__
		self.assertIn("App actions", runbook)
		for needle in ("CLASSIFICATION", "test_activity_contract", "X-Jarvis-Client", "dedupe"):
			self.assertIn(needle, runbook)
		self.assertIn("activity_rollup", activity_actions.__doc__)

	def test_a_non_literal_registry_is_refused(self):
		with self.assertRaises(ValueError):
			load_registry("CLASSIFICATION = dict(a=1)\nLABEL_OVERRIDES = {}\n")
		with self.assertRaises(ValueError):
			load_registry("CLASSIFICATION = {}\n")
		with self.assertRaises(ValueError):
			load_registry('X = 1\nCLASSIFICATION = {"a": (1,)}\nLABEL_OVERRIDES = {"k": VALUE}\n')
