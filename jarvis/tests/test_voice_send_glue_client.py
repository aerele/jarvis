"""The ChatView send-glue never-lose-audio contract, proven by a REAL executable node test that
lives in the python suite forever (the eventFence / voiceChunkQueue precedent).

Codex round 3 found three HIGH audio-loss bugs that lived NOT in the queue primitive (round 2's
tests were queue-unit-level and green) but in ChatView's composer↔queue↔send glue: a scope-bound
(not payload-bound) release token, an id-adoption that failed to migrate late clips off the
new-chat sentinel, and a rejected failed-bubble resend that dropped its recovery token. Because the
ChatView single-file component can't be mounted under ``node --test``, that glue was factored into
pure helpers — ``frontend/src/utils/voiceSendGlue.js`` (promoteNewChatScope, planRejectedSend) plus
``voiceChunkQueue.js``'s payload-bound ``captureSentInPayload`` — which ChatView imports and calls.
``frontend/src/utils/voiceSendGlue.test.js`` drives those REAL helpers stitched to the real queue +
a mock send outcome, reproducing the send flow end to end: composer-edit-before-send (R3-1), an
id-less send with a mid-flight retry commit (R3-2), and a failed-bubble resend rejected across
ok:false / usage_limit / subscription_suspended / single-flight (R3-3).

This subprocess-runs it with ``node --test`` (which exits non-zero on any failed assertion) so the
client contract is enforced by every CI run, not just by ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _glue_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceSendGlue.test.js")


class TestVoiceSendGlueClient(unittest.TestCase):
	def test_client_voice_send_glue_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _glue_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice send-glue test missing at {test_file} — the ChatView-level integration walk MUST ship with the glue",
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
			"client voice send-glue node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
