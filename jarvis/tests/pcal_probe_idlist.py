"""Scratch probe helper (never in a PR): list the test ids and the skip sites of the Payment Calendar modules.

Run in a fresh interpreter, site-less, as the version-15 test runner loads a module
(`unittest.TestLoader().loadTestsFromModule`). One line per module:
`PCAL_IDS {"module": ..., "tests": [{"id": ..., "sites": [...]}]}` or `{"module": ..., "load_error": ...}`.
"""

import ast
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
import unittest.util

MODULE_FILTER = re.compile(r"payment_calendar|batch_permission|permission_hooks|pcal_probe")
SKIP_CALLS = {"skipTest", "skip", "skipIf", "skipUnless", "v16_only", "v15_only"}


def _walk(suite):
	for item in suite:
		if isinstance(item, unittest.TestSuite):
			yield from _walk(item)
		else:
			yield item


def _unwrap(fn):
	seen = []
	while fn is not None and fn not in seen:
		seen.append(fn)
		fn = getattr(fn, "__wrapped__", None)
	return seen


def _scan(fn):
	"""The places in the source of `fn` itself that skip: a call to a skip name, or a raise of SkipTest.

	`inspect.getsourcelines(fn)` follows `__wrapped__` and would return the decorated body, so a `functools.wraps`
	wrapper (for example `speed_path`) would never be scanned. The code object is the function's own.
	"""
	found = []
	target = getattr(fn, "__code__", fn)
	try:
		lines, first = inspect.getsourcelines(target)
		path = target.co_filename if hasattr(target, "co_filename") else inspect.getsourcefile(fn) or "?"
		tree = ast.parse(textwrap.dedent("".join(lines)))
	except (OSError, TypeError, SyntaxError, IndentationError):
		return found
	for node in ast.walk(tree):
		kind = None
		if isinstance(node, ast.Call):
			func = node.func
			name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
			if name in SKIP_CALLS:
				kind = name
		elif isinstance(node, ast.Raise) and node.exc is not None:
			if "SkipTest" in ast.unparse(node.exc):
				kind = "raise SkipTest"
		if kind:
			found.append(
				{
					"kind": kind,
					"where": f"{path}:{first + node.lineno - 1}",
					"text": ast.unparse(node)[:160],
				}
			)
	return found


def _decorator_sites(cls, method):
	found = []
	if getattr(method, "__unittest_skip__", False):
		found.append(
			{
				"kind": "method decorator",
				"why": str(getattr(method, "__unittest_skip_why__", "")),
			}
		)
	for klass in cls.__mro__:
		if vars(klass).get("__unittest_skip__"):
			try:
				where = f"{inspect.getsourcefile(klass)}:{inspect.getsourcelines(klass)[1]}"
			except (OSError, TypeError):
				where = "?"
			found.append(
				{
					"kind": "class decorator",
					"class": klass.__name__,
					"where": where,
					"why": str(vars(klass).get("__unittest_skip_why__", "")),
				}
			)
	return found


def _sites(test):
	cls, name = type(test), test._testMethodName
	method = getattr(cls, name, None)
	chain = list(_unwrap(method)) if method is not None else []
	for klass in cls.__mro__:
		for attr in ("setUp", "setUpClass"):
			member = vars(klass).get(attr)
			member = getattr(member, "__func__", member)
			if member is not None:
				chain.extend(_unwrap(member))
	found = _decorator_sites(cls, method)
	for fn in chain:
		found.extend(_scan(fn))
	unique = {}
	for site in found:
		unique.setdefault((site["kind"], site.get("where"), site.get("text")), site)
	return list(unique.values())


def _module_record(name):
	module = importlib.import_module(f"jarvis.tests.{name}")
	suite = unittest.TestLoader().loadTestsFromModule(module)
	tests = []
	for test in _walk(suite):
		cls = type(test)
		method = getattr(test, "_testMethodName", str(test))
		tests.append({"id": f"{unittest.util.strclass(cls)}.{method}", "sites": _sites(test)})
	return {"module": name, "tests": tests}


def main(real_sites_dir):
	scratch = tempfile.mkdtemp()
	shutil.copy(os.path.join(real_sites_dir, "apps.txt"), scratch)
	with open(os.path.join(scratch, "common_site_config.json"), "w") as handle:
		handle.write("{}")
	import frappe

	frappe.init("", sites_path=scratch)
	import jarvis

	directory = os.path.join(os.path.dirname(jarvis.__file__), "tests")
	names = sorted(
		file[:-3]
		for file in os.listdir(directory)
		if file.startswith("test_") and file.endswith(".py") and MODULE_FILTER.search(file)
	)
	for name in names:
		try:
			record = _module_record(name)
		except BaseException as exc:  # a module that cannot load site-less is a record, not a crash
			record = {
				"module": name,
				"load_error": f"{type(exc).__name__}: {exc}"[:200],
			}
		print("PCAL_IDS " + json.dumps(record, sort_keys=True), flush=True)
	shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
	main(sys.argv[1])
