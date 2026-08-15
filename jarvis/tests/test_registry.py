import importlib
import os

from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, ToolNotFoundError
from jarvis.tools.registry import _TOOL_NAMES, dispatch, list_tools


class TestRegistry(FrappeTestCase):
	def test_registered_tools_match_modules_exactly(self):
		"""The registered set must equal the tool .py modules on disk - a new
		tool file without registration (or a stale registration) fails here
		instead of silently drifting. (Replaces a hardcoded list the 2026-06
		wiring audit found 7 tools out of date.)"""
		names = set(list_tools())
		self.assertEqual(names, set(_TOOL_NAMES))

		tools_dir = os.path.dirname(importlib.import_module("jarvis.tools.registry").__file__)
		modules = {
			f[:-3]
			for f in os.listdir(tools_dir)
			if f.endswith(".py") and not f.startswith("_") and f != "registry.py"
		}
		self.assertEqual(
			names,
			modules,
			f"registry vs tool modules drift: only-registered={names - modules}, "
			f"only-on-disk={modules - names}",
		)

	def test_dispatch_invokes_correct_tool(self):
		result = dispatch("get_schema", {"doctype": "Customer"})
		self.assertEqual(result["doctype"], "Customer")

	def test_dispatch_unknown_tool_raises(self):
		with self.assertRaises(ToolNotFoundError):
			dispatch("not_a_tool", {})

	def test_dispatch_rejects_non_dict_args(self):
		with self.assertRaises(InvalidArgumentError):
			dispatch("get_schema", "not a dict")

	def test_dispatch_passes_args_through(self):
		# Missing required arg should bubble up the tool's own InvalidArgumentError.
		with self.assertRaises(InvalidArgumentError):
			dispatch("get_doc", {"doctype": "Customer"})
