"""The Dashboards builder's composer + canvas-persistence contract, proven by a
REAL executable node test that lives in the python suite forever (the
test_event_fence_client.py precedent).

Two owner-reported defects are fenced here:

  * the composer kept the message it had just sent (a disabled-mid-send textarea
    is blurred by the browser, fires ``change``, and frappe-ui's Textarea
    re-emitted the pre-clear value);
  * the canvas vanished on a tab switch or a navigation, with no way back and no
    warning that it had gone.

``frontend/src/lib/dashboardRestore.js`` holds the real rehydration logic
(the newest transcript message that drew an html artifact); the rest of both
fixes is component wiring that a plain node runner cannot import, so
``frontend/src/lib/dashboardRestore.test.js`` fences it with source assertions
(the voiceDictationLifecycle precedent). This test subprocess-runs the suite
with ``node --test`` so a regression fails every CI run.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _builder_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "lib", "dashboardRestore.test.js")


class TestDashboardBuilderUxClient(unittest.TestCase):
	def test_builder_composer_and_canvas_node_suite_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _builder_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"dashboards builder client test missing at {test_file} — it MUST ship with the module",
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
			"dashboards builder client node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
