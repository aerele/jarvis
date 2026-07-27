"""The composer's voice-chip LIFECYCLE contract, proven by a REAL executable node test that lives
in the python suite forever (the eventFence / voiceChunkQueue / voiceSendGlue precedent).

The delete-after-send mechanism was never the bug: a failed clip is deliberately never released by
a send (its words never made it into the payload), but nothing told the user that — the chip read
identically to a stuck one, the sent message silently dropped the missing words with no marker and
no confirmation, Retry promised to fix a message that had already gone, and the leave guard warned
about losing audio that was in fact durably mirrored. The fixes span the queue primitive
(``frontend/src/utils/voiceChunkQueue.js``: markSentWithoutClip, ``snapshot().failed[].sentWithout``,
hasUnfinishedReason) and ChatView's wiring of it.

The queue half is proven behaviorally by ``voiceChunkQueue.test.js``. The ChatView half cannot be:
the single-file component has no harness in this app, so — exactly as ``pwa/src/lib/pumpFence.test.js``
does for the Relay-Pump fence — ``frontend/src/utils/voiceChipLifecycle.test.js`` asserts the wiring
against the SOURCE (the confirm runs before the strip and the POST; the sent-without flagging sits in
the accepted-send branch beside the release; the chip copy + Retry tooltip branch on ``sentWithout``;
the retry-after-send append is announced; the leave guard blocks only for a ``"live"`` reason), plus a
decision-parity walk against the real queue. It also fences the done-clip
captureSentInPayload → acknowledge release, the one path this work must not regress.

This subprocess-runs it with ``node --test`` (which exits non-zero on any failed assertion) so the
contract is enforced by every CI run, not just by ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _lifecycle_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceChipLifecycle.test.js")


class TestVoiceChipLifecycleClient(unittest.TestCase):
	def test_client_voice_chip_lifecycle_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _lifecycle_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice chip-lifecycle test missing at {test_file} — the composer wiring walk MUST ship with the change",
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
			"client voice chip-lifecycle node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
