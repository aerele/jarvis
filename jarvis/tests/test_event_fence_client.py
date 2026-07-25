"""CDX-12 (P0) — the client fence's first-terminal acceptance, proven by a REAL
executable node test that lives in the python suite forever.

The Relay-Pump epoch/seq fence that guards ChatView's realtime event handling was
extracted into a plain, importable module (``frontend/src/utils/eventFence.js``) so its
logic is testable without a browser. ``frontend/src/utils/eventFence.test.js`` replays
codex's exact 5-step client walk (accept delta; accept the FIRST terminal at the same
epoch/seq; reject the identical second terminal; reject a stale E-1 tool/delta; allow a
legitimate E+1 recovered stream+terminal) plus the recovered-delta-then-first-terminal
(``was_recovered``) walk and the guard-rail rejections. This test subprocess-runs it with
``node --test`` (which exits non-zero on any failed assertion) so the client contract is
enforced by every CI run, not just by ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _fence_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "eventFence.test.js")


class TestEventFenceClient(unittest.TestCase):
	def test_client_fence_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _fence_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"client fence test missing at {test_file} — the CDX-12 walk MUST ship with the module",
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
			"client event-fence node test FAILED (CDX-12 client walk):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
