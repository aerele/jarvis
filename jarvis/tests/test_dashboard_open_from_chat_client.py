"""The "Open in Dashboards" contract, proven by a REAL executable node test
that lives in the python suite forever (the test_dashboard_builder_ux_client.py
precedent).

The owner-reported gap: the Dashboards builder's conversation shows up in the
main chat list, and opening it there shows the prompt and the dashboard — but
main chat's canvas never runs the document's queries, and nothing offered a way
to the page that does.

``frontend/src/lib/dashboardOpen.js`` holds the real logic (who gets the
affordance, and where it routes); the rest — main chat's two surfaces and the
builder's ?chat=&canvas= promotion — is component wiring that a plain node
runner cannot import, so ``frontend/src/lib/dashboardOpen.test.js`` fences it
with source assertions. This test subprocess-runs the suite with ``node --test``
so a regression fails every CI run.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _open_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "lib", "dashboardOpen.test.js")


class TestDashboardOpenFromChatClient(unittest.TestCase):
	def test_open_in_dashboards_node_suite_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _open_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"open-in-dashboards client test missing at {test_file} — it MUST ship with the module",
		)
		proc = subprocess.run(
			[node, "--test", test_file],
			cwd=os.path.dirname(test_file),
			capture_output=True,
			text=True,
			timeout=120,
		)
		self.assertEqual(
			proc.returncode,
			0,
			"open-in-dashboards client node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
