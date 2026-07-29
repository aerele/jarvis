"""Guard: every @frappe.whitelist method must have fully type-annotated
parameters.

jarvis/hooks.py declares ``require_type_annotated_api_methods = True``. On a
Frappe that enforces it, an UN-annotated parameter on a whitelisted method
500s the request before the function body runs. The local dev bench's Frappe
does not enforce the flag, so the bug is invisible here and detonates only on
freshly-deployed strict benches - exactly how ``save_llm_pool(models, ...)``
took down onboarding in the JARVIS-2026-07-08 incident (fault a).

This sweep makes the contract testable on ANY Frappe: walk EVERY module in
the jarvis package (derived via pkgutil, not a hand-kept list - a hand-kept
list missed live offenders in agents_api/custom_skills_api), find every
whitelisted function, and assert each parameter carries an annotation.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from frappe.tests.utils import FrappeTestCase

# Subtrees that never serve whitelisted endpoints and may have import-time
# side effects (migrations) or are test-only.
_SKIP_PREFIXES = ("jarvis.patches", "jarvis.tests")


def _iter_api_modules():
	"""Yield every importable module in the jarvis package (minus skips).

	Coverage is DERIVED, not hand-listed: a new module with whitelisted
	methods is swept automatically. An unimportable module fails the test
	loudly - in a site context every app module must import cleanly anyway.
	"""
	import jarvis

	yield jarvis
	# LANDMINE (see jarvis#485, bench commit 96336978): walking + importing every
	# module here can, in a parallel-test shard's file order on Python 3.14, leave
	# `sys.modules['jarvis.selfhost'] = None`. Any later `import jarvis.selfhost` in
	# the same shard process - in another test or in app code under test - then
	# raises ModuleNotFoundError, and the failure looks unrelated to this file. If
	# you hit that, the fix belongs here (e.g. snapshot + restore sys.modules around
	# the walk), not in the victim test.
	for info in pkgutil.walk_packages(jarvis.__path__, prefix="jarvis."):
		if info.name.startswith(_SKIP_PREFIXES):
			continue
		yield importlib.import_module(info.name)


def _whitelisted_functions(module):
	# frappe.whitelist() registers the function in the global
	# ``frappe.whitelisted`` set (no attribute is stamped on the function),
	# so detection is set membership.
	import frappe

	for name, fn in vars(module).items():
		if callable(fn) and fn in frappe.whitelisted:
			# Only functions defined in THIS module (skip re-exports).
			if getattr(fn, "__module__", None) == module.__name__:
				yield name, fn


class TestWhitelistAnnotations(FrappeTestCase):
	def test_every_whitelisted_param_is_annotated(self):
		offenders = []
		seen = set()
		found_any = False
		for module in _iter_api_modules():
			for name, fn in _whitelisted_functions(module):
				key = f"{fn.__module__}.{name}"
				if key in seen:
					continue
				seen.add(key)
				found_any = True
				sig = inspect.signature(fn)
				for pname, param in sig.parameters.items():
					if pname in ("self", "cls"):
						continue
					if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
						continue
					if param.annotation is inspect.Parameter.empty:
						offenders.append(f"{key}({pname})")
		self.assertTrue(found_any, "sweep found no whitelisted functions - module walk or detection broke")
		self.assertFalse(
			offenders,
			"un-annotated @frappe.whitelist parameters (500 under Frappe's "
			"require_type_annotated_api_methods, which hooks.py enables): " + ", ".join(sorted(offenders)),
		)
