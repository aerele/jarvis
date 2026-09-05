"""Enforce that every custom bench CLI command this app registers is documented in docs/bench-commands.md,
so the reference doc can't silently rot when a command is added or renamed. If this fails, add the new
command to the doc (as `name`)."""

import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.commands import commands as _COMMANDS


class TestBenchCommandsDocumented(FrappeTestCase):
	def test_every_registered_command_is_documented(self):
		# repo-root docs/ (the module's own jarvis/docs/ is gitignored). get_app_path -> the module dir;
		# .parent -> the repo root; then pathlib-join preserves the hyphen (get_app_path would scrub it).
		doc = (pathlib.Path(frappe.get_app_path("jarvis")).parent / "docs" / "bench-commands.md").read_text()
		missing = [c.name for c in _COMMANDS if f"`{c.name}`" not in doc]
		self.assertEqual(
			missing,
			[],
			f"Undocumented bench command(s): {missing}. Add each (as `<name>`) to "
			"docs/bench-commands.md — this test enforces the doc stays in sync.",
		)
