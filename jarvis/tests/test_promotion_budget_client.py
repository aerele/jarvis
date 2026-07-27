"""The Org-promotion push-budget warning boundary, proven by a REAL executable
node test that lives in the python suite forever (Skills-area promotion
surfacing, Fable ruling 2).

The reviewer decides an Org skill promotion informed by whether approving would
take the shared container near/past its 25-skill push budget. That boundary
logic was extracted into a plain, importable module
(``frontend/src/pages/skills/promotionBudget.js``) so it is testable without a
browser. ``frontend/src/pages/skills/promotionBudget.test.js`` replays the
boundary walk (non-Org never warns; under-budget → null; last-slot → "near";
at/over-cap → "over"; fail-open on a zero/missing budget; defensive coercion).
This test subprocess-runs it with ``node --test`` (which exits non-zero on any
failed assertion) so the reviewer-warning contract is enforced by every CI run,
not only by ``npm run build`` — the same idiom as
``test_event_fence_client.py``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _budget_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "pages", "skills", "promotionBudget.test.js")


class TestPromotionBudgetClient(unittest.TestCase):
	def test_budget_warning_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _budget_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"promotion-budget test missing at {test_file} — the boundary walk MUST ship with the module",
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
			"promotion-budget node test FAILED (Org push-budget boundary):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: a silently-skipped suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
