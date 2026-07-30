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
	"jarvis/agent_ws.py",
	"jarvis/chat/relay_mux.py",
	"jarvis/tools/tool-names.json",
	"jarvis/tools/_tool_contract.py",
	"jarvis/agent_templates/openclaw.json.j2",
	"jarvis/patches.txt",
	"jarvis/tests/test_no_openclaw_leak.py",  # this guard itself names the allowlist
}
# Historical applied migrations are never rewritten.
_ALLOWED_PREFIXES = ("jarvis/patches/",)
# Wire-contract literals allowed in ANY file (the runtime's filesystem / image / marker /
# the old openclaw_seq_watermark column the rename migration still copies from until a later
# contract patch drops it). Stripped before the check so a line carrying only these is not
# flagged.
_ALLOWED_LITERALS = re.compile(
	r"openclaw\.plugin\.json|openclaw\.json|__openclaw__|openclaw/openclaw|\.openclaw|openclaw_state|openclaw_seq_watermark"
)
_OPENCLAW = re.compile(r"openclaw", re.IGNORECASE)
_SCAN_SUFFIXES = (".py", ".js", ".ts", ".vue", ".json", ".md", ".txt", ".j2")
_SKIP = ("__pycache__", "/public/", "docs/superpowers", "node_modules")


class TestNoOpenclawLeak(FrappeTestCase):
	def test_openclaw_only_in_sanctioned_wire_contract(self):
		app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../app
		offenders = []
		for base in ("jarvis", os.path.join("frontend", "src")):
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
							for i, line in enumerate(fh, 1):
								if _OPENCLAW.search(_ALLOWED_LITERALS.sub("", line)):
									offenders.append(f"{rel}:{i}: {line.strip()}")
					except (UnicodeDecodeError, OSError):
						continue
		self.assertEqual(
			offenders,
			[],
			'un-sanctioned "openclaw" found (white-label: use "agent"):\n' + "\n".join(offenders),
		)
