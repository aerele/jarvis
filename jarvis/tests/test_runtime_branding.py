"""Guard: runtime branding stays confined to the sanctioned wire-contract surface.

The runtime is white-labelled as "agent" everywhere a customer, operator, or a casual
reader could see it. Upstream names survive only in the runtime's own protocol /
config identity (renaming it would break the plugin / fleet-agent / container) or inside
historical applied migrations. This test fails if an upstream brand reference lands outside
that allowlist - use "agent" instead, or add it to the wire-contract allowlist below if it
is genuinely part of the runtime's protocol.
"""

import os
import re
import shutil
import subprocess

import frappe
from frappe.tests.utils import FrappeTestCase

# Files containing shared contracts or historical migration identifiers.
_ALLOWED_FILES = {
	"jarvis/tools/tool-names.json",
	"jarvis/tools/_tool_contract.py",
	"jarvis/patches.txt",
	"jarvis/tests/test_runtime_branding.py",  # this guard itself names the allowlist
	# Historical migration identifiers are preserved — enumerated, NOT a blanket
	# patches/ prefix, so a newly authored patch is scanned like any other file.
	"jarvis/patches/v1_0_rename_openclaw_to_agent.py",
	"jarvis/patches/v1_4_drop_llm_oauth_fields.py",
	"jarvis/patches/v1_5_drop_dev_only_operator_fields.py",
	"jarvis/patches/v1_15_unarchive_auto_expired_conversations.py",
	"jarvis/patches/v2_01_backfill_glm_zai_provider_id.py",
}
_ALLOWED_PREFIXES = ()
# Exact compatibility literals may occur in any file. They cover gateway routes,
# transcript metadata, container paths, image coordinates, external repository
# identifiers and legacy database columns. Strip them before checking prose.
# Container paths require a trailing slash so the allowance cannot mask a dotted
# application attribute. Source citations are limited to known upstream roots.
# Exact upstream references are documented in the workspace integration reference.
_ALLOWED_LITERALS = re.compile(
	r"openclaw\.plugin\.json|openclaw\.json|__openclaw__|__openclaw|openclaw/(?:src|extensions|docs|openclaw)"
	r"|\.openclaw/|openclaw_state|openclaw_seq_watermark|openclaw_provider|jarvis-openclaw-plugin"
	r"|render_openclaw_config|DEFAULT_OPENCLAW_IMAGE|verify-openclaw-assumptions"
	r"|openclaw doctor"  # the runtime's own CLI invocation, cited verbatim in ops docs
)
_RUNTIME_BRAND = re.compile(r"openclaw", re.IGNORECASE)
_SCAN_SUFFIXES = (".py", ".js", ".ts", ".vue", ".json", ".md", ".txt", ".j2", ".html", ".css")
# /public/frontend is the built SPA bundle (generated); the rest of /public (desk
# widget js/css, manifest) is SOURCE that ships to the customer bench and IS scanned.
_SKIP = ("__pycache__", "/public/frontend", "docs/superpowers", "node_modules")
# Every scanned root must yield at least this many files, else the scan has gone
# vacuous (a moved/renamed root would otherwise pass silently by scanning nothing).
_MIN_FILES_PER_ROOT = {"jarvis": 300, os.path.join("frontend", "src"): 100, "pwa": 10}


class TestRuntimeBranding(FrappeTestCase):
	def test_runtime_brand_only_in_sanctioned_wire_contract(self):
		app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../app
		offenders = []
		scanned = dict.fromkeys(_MIN_FILES_PER_ROOT, 0)
		for base in _MIN_FILES_PER_ROOT:
			for root, _dirs, files in os.walk(os.path.join(app_root, base)):
				if any(skip in root for skip in _SKIP):
					continue
				for fn in files:
					if not fn.endswith(_SCAN_SUFFIXES):
						continue
					rel = os.path.relpath(os.path.join(root, fn), app_root)
					if rel in _ALLOWED_FILES or rel.startswith(_ALLOWED_PREFIXES):
						continue
					try:
						with open(os.path.join(root, fn), encoding="utf-8") as fh:
							scanned[base] += 1
							for i, line in enumerate(fh, 1):
								if _RUNTIME_BRAND.search(_ALLOWED_LITERALS.sub("", line)):
									offenders.append(f"{rel}:{i}: {line.strip()}")
					except (UnicodeDecodeError, OSError):
						continue
		# Repo-root docs (README.md, design.md, ...) ship with the app and are the
		# first thing a customer engineer reads — scan them too (non-recursive).
		for fn in os.listdir(app_root):
			if not fn.endswith(".md"):
				continue
			try:
				with open(os.path.join(app_root, fn), encoding="utf-8") as fh:
					for i, line in enumerate(fh, 1):
						if _RUNTIME_BRAND.search(_ALLOWED_LITERALS.sub("", line)):
							offenders.append(f"{fn}:{i}: {line.strip()}")
			except (UnicodeDecodeError, OSError):
				continue
		self.assertEqual(
			offenders,
			[],
			'un-sanctioned "openclaw" found (white-label: use "agent"):\n' + "\n".join(offenders),
		)
		for base, floor in _MIN_FILES_PER_ROOT.items():
			self.assertGreaterEqual(
				scanned[base],
				floor,
				f"guard scanned only {scanned[base]} files under {base!r} (floor {floor}) - "
				"the scan root moved or the walk went vacuous; fix the root, don't lower the floor blindly",
			)

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
