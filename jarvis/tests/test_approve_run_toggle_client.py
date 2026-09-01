"""The "Approve & run" arming toggle's gating + risk-disclosure boundary, proven
by a REAL executable node test that lives in the python suite forever (skill
approve-and-run, P2).

``armToggleLocked`` is the 3-boolean truth table behind the SPA switch's disabled
state, and one wrong sign silently locks the kill switch for an armed skill's
owner or makes the toggle look flippable to a non-admin (the server guard still
blocks the write, but the UI misrepresents a security control). The logic + the
admin-facing description (which must disclose the true covered set, run_method
included, and not overstate what the toggle alone does) were extracted into a
plain importable module (``frontend/src/pages/skills/approveRunToggle.js``) so
they are testable without a browser. ``approveRunToggle.test.js`` walks the matrix
and pins the disclosure copy. This subprocess-runs it with ``node --test`` (which
exits non-zero on any failed assertion) so the contract is enforced by every CI
run, the same idiom as ``test_promotion_budget_client.py``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _toggle_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "pages", "skills", "approveRunToggle.test.js")


class TestApproveRunToggleClient(unittest.TestCase):
	def test_arm_toggle_node_matrix_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _toggle_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"approve-run toggle test missing at {test_file} — the gating matrix MUST ship with the module",
		)
		proc = subprocess.run(
			[node, "--test", test_file],
			cwd=os.path.dirname(test_file),
			capture_output=True,
			text=True,
			timeout=120,
		)
		# node --test exits non-zero on ANY failed assertion; surface its output on failure.
		self.assertEqual(
			proc.returncode,
			0,
			"approve-run toggle node test FAILED (arm gating / risk disclosure):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: a silently-skipped suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
