"""jarvis#560: the per-reply model-attribution RENDER rule, proven by a REAL
executable node test that lives in the python suite forever.

The server stamps ``model`` / ``provider`` on every assistant row (see
``finalize._stamp_reply_model``), and the feature still fails if the transcript
never shows them. The show/hide rule is deliberately quiet, hiding the badge
whenever a reply matches what the chat is set to now, so a bug that hides ONE
CASE TOO MANY is invisible. No error, no empty bubble, just an audit trail that
silently stops surfacing failovers.

So the rule was extracted into a plain importable module
(``frontend/src/utils/modelBadge.js``) and
``frontend/src/utils/modelBadge.test.js`` pins the cases that MUST speak (a reply
written before a mid-thread switch; an Auto turn the pool failed over) alongside
the ones that must stay silent. This test subprocess-runs it with ``node --test``
(non-zero exit on any failed assertion) so the client contract is enforced by
every CI run, not just by ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _badge_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "modelBadge.test.js")


class TestModelBadgeClient(unittest.TestCase):
	def test_model_badge_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _badge_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"model-badge client test missing at {test_file}: the jarvis#560 render "
			"rule MUST ship with the module",
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
			"client model-badge node test FAILED (jarvis#560 attribution render rule):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
