"""The ```jarvis-ask contract, proven by a REAL executable node test that lives
in the python suite forever (the test_event_fence_client.py precedent).

The clarifying-question cards used to exist only inside ChatView, and the
Dashboards builder pane deleted the block instead of rendering it - so an agent
that asked a question before building produced an empty bubble nobody could
answer. Parsing / readiness / answer formatting now live in
``frontend/src/lib/chatAsk.js`` and the cards in
``frontend/src/components/chat/AskCard.vue``, shared by both surfaces.
``frontend/src/lib/chatAsk.test.js`` exercises that logic for real and fences
the wiring on both surfaces. This test subprocess-runs it with ``node --test``
(which exits non-zero on any failed assertion) so a fork of either half fails
every CI run, not just ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _ask_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "lib", "chatAsk.test.js")


class TestChatAskClient(unittest.TestCase):
	def test_jarvis_ask_contract_node_suite_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _ask_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"jarvis-ask client test missing at {test_file} — it MUST ship with the module",
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
			"jarvis-ask client node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
