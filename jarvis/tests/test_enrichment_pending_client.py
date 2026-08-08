"""jarvis#681 - the bounded "Finishing..." affordance, proven by a REAL executable
node test that lives in the python suite forever.

A completed chat turn kept showing "Finishing..." indefinitely after a background
enrichment lane died on stale credentials. The affordance was driven by a single
best-effort ``message:enriched`` realtime push and had nothing else that could ever
clear it, so one stalled or lost enrichment left a finished answer permanently
claiming to be unfinished.

The bookkeeping now lives in ``frontend/src/lib/enrichmentPending.js`` (a plain,
importable module ChatView.vue wires itself to) and every entry carries a deadline.
``frontend/src/lib/enrichmentPending.test.js`` drives the reported shape on a fake
clock: a reply marked pending whose ``message:enriched`` never arrives is dropped when
its deadline passes, the owner is told exactly once so it can resync, a re-delivered
terminal cannot push the deadline out, and teardown cancels every timer. It also
source-fences ChatView so the old unbounded Set surgery cannot come back.

This subprocess-runs it with ``node --test`` (non-zero exit on any failed assertion)
so the client contract is enforced by the backend shards too, not only by the separate
frontend CI job. Mirrors test_event_fence_client.py.
"""

import os
import re
import shutil
import subprocess
import unittest

import frappe

# The walk this file guards is only meaningful if it actually RAN. `node --test` counts
# the FILE itself as one passing unit, so an emptied or import-broken test file still
# prints "pass 1" with a zero exit: asserting merely that a pass line exists, or that the
# count is at least 1, proves nothing. Hold a floor comfortably above 1 (the file ships
# 11 tests) so a gutted suite fails loudly instead of reading as green.
_MIN_EXPECTED_PASSES = 8


def _test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "lib", "enrichmentPending.test.js")


class TestEnrichmentPendingClient(unittest.TestCase):
	def test_bounded_finishing_affordance_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"client enrichment-pending test missing at {test_file} - the jarvis#681 walk "
			"MUST ship with the module",
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
			"client enrichment-pending node test FAILED (jarvis#681 bounded affordance):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: prove the walk RAN, not just that nothing failed. See
		# _MIN_EXPECTED_PASSES for why a floor above 1 is what makes this assertion real.
		passes = re.search(r"\bpass (\d+)\b", proc.stdout)
		self.assertIsNotNone(passes, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertGreaterEqual(
			int(passes.group(1)),
			_MIN_EXPECTED_PASSES,
			f"the jarvis#681 client walk ran only {passes.group(1)} assertions, expected at least "
			f"{_MIN_EXPECTED_PASSES}. An emptied or import-broken test file still exits 0 with "
			f"'pass 1'.\n{proc.stdout}",
		)
