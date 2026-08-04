"""Guard: "openclaw" is stripped from the app except the sanctioned wire-contract surface.

The runtime is white-labelled as "agent" everywhere a customer, operator, or a casual
reader could see it. "openclaw" survives ONLY where it is the runtime's own protocol /
config identity (renaming it would break the plugin / fleet-agent / container) or inside
historical applied migrations. This test fails if a new "openclaw" reference lands outside
that allowlist - use "agent" instead, or add it to the wire-contract allowlist below if it
is genuinely part of the runtime's protocol.
"""

import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

# Files where "openclaw" IS the runtime's own wire-contract identity (kept verbatim).
_ALLOWED_FILES = {
	"jarvis/chat/agent_client.py",
	"jarvis/chat/device.py",
	"jarvis/chat/events.py",
	"jarvis/tools/tool-names.json",
	"jarvis/tools/_tool_contract.py",
	"jarvis/agent_templates/openclaw.json.j2",
	"jarvis/patches.txt",
	"jarvis/tests/test_no_openclaw_leak.py",  # this guard itself names the allowlist
	# Historical APPLIED migrations are never rewritten — enumerated, NOT a blanket
	# patches/ prefix, so a newly authored patch is scanned like any other file.
	"jarvis/patches/v1_0_rename_openclaw_to_agent.py",
	"jarvis/patches/v1_4_drop_llm_oauth_fields.py",
	"jarvis/patches/v1_5_drop_dev_only_operator_fields.py",
	"jarvis/patches/v1_15_unarchive_auto_expired_conversations.py",
	"jarvis/patches/v2_01_backfill_glm_zai_provider_id.py",
}
_ALLOWED_PREFIXES = ()
# Literals allowed in ANY file, each a genuine reference to the openclaw runtime that must
# stay verbatim (NOT the app's own naming). Stripped before the check so a line carrying only
# these is not flagged:
#   - runtime filesystem / image / markers: openclaw.json[.plugin], `.openclaw/` container
#     paths, __openclaw (the transcript-metadata key the gateway stamps on every message) +
#     __openclaw__ (the frontend live-reload marker), the ghcr openclaw/openclaw image,
#     openclaw_state
#   - cross-repo identifiers (the vendored openclaw repo + sibling repos, unrenameable here):
#     an `openclaw/{src,extensions,docs}/` source citation + the openclaw/openclaw image,
#     jarvis-openclaw-plugin, render_openclaw_config, DEFAULT_OPENCLAW_IMAGE,
#     verify-openclaw-assumptions (anchored to real prefixes so a fabricated `openclaw/x`
#     path can't slip through)
#   - openclaw_seq_watermark / openclaw_provider: old columns the rename migrations copy
#     from until a later contract patch drops them
# `.openclaw` is anchored to the `/`-terminated container-path form so it does NOT mask a
# dotted attribute/module leak like `x.openclaw_y` (which must be caught).
_ALLOWED_LITERALS = re.compile(
	r"openclaw\.plugin\.json|openclaw\.json|__openclaw__|__openclaw|openclaw/(?:src|extensions|docs|openclaw)"
	r"|\.openclaw/|openclaw_state|openclaw_seq_watermark|openclaw_provider|jarvis-openclaw-plugin"
	r"|render_openclaw_config|DEFAULT_OPENCLAW_IMAGE|verify-openclaw-assumptions"
	r"|openclaw doctor"  # the runtime's own CLI invocation, cited verbatim in ops docs
)
_OPENCLAW = re.compile(r"openclaw", re.IGNORECASE)
_SCAN_SUFFIXES = (".py", ".js", ".ts", ".vue", ".json", ".md", ".txt", ".j2", ".html", ".css")
# /public/frontend is the built SPA bundle (generated); the rest of /public (desk
# widget js/css, manifest) is SOURCE that ships to the customer bench and IS scanned.
_SKIP = ("__pycache__", "/public/frontend", "docs/superpowers", "node_modules")
# Every scanned root must yield at least this many files, else the scan has gone
# vacuous (a moved/renamed root would otherwise pass silently by scanning nothing).
_MIN_FILES_PER_ROOT = {"jarvis": 300, os.path.join("frontend", "src"): 100, "pwa": 10}


class TestNoOpenclawLeak(FrappeTestCase):
	def test_openclaw_only_in_sanctioned_wire_contract(self):
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
								if _OPENCLAW.search(_ALLOWED_LITERALS.sub("", line)):
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
						if _OPENCLAW.search(_ALLOWED_LITERALS.sub("", line)):
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
