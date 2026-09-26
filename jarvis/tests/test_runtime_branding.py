"""Keep internal working documents out of the public app checkout.

Runtime branding is checked separately by CI with the Admin-owned policy.
"""

import os
import shutil
import subprocess

import frappe
from frappe.tests.utils import FrappeTestCase


class TestRuntimeBranding(FrappeTestCase):
	def test_no_internal_working_docs_are_tracked(self):
		"""Public repo: internal design / plan / decision docs must never be committed.

		They live in the admin repo, not here. ``.gitignore`` blocks the common case;
		this guard catches a force-add (``git add -f``) or a stray path the ignore rules
		miss, so a working doc can't silently ship in the public app. The theme
		``design.md`` files (skill-generation source, under dashboards/) are functional
		and don't match the internal-working-doc shapes below, so they stay.
		"""
		app_root = os.path.dirname(frappe.get_app_path("jarvis"))
		git = shutil.which("git")
		if not git:
			self.skipTest("git not available - tracked-file guard is a source-repo check")
		proc = subprocess.run(
			[git, "-C", app_root, "ls-files"],
			capture_output=True,
			text=True,
			timeout=60,
		)
		if proc.returncode != 0:
			# Installed as a package (no .git); the guard runs from a checkout (dev + CI).
			self.skipTest(f"not a git checkout ({proc.stderr.strip()})")
		tracked = proc.stdout.splitlines()
		self.assertTrue(tracked, "git ls-files returned nothing - the guard would pass vacuously")
		offenders = [
			p
			for p in tracked
			if p.startswith("jarvis/docs/") or "docs/superpowers/" in p or p.endswith(".plan.md")
		]
		self.assertEqual(
			offenders,
			[],
			"internal working docs are git-tracked in a public-bound repo (they are "
			".gitignored - move them to the admin repo):\n" + "\n".join(offenders),
		)
