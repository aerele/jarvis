"""The ChatView half of the dictation retention story, proven by a REAL executable node test
that lives in the python suite forever (the pumpFence source-assertion precedent).

The store primitive's half is covered by test_voice_dictation_store_client.py. What that cannot
prove is whether the composer calls it at the right moment and says the right thing — and ChatView
is a single-file component with no test harness in this app (no vitest, no @vue/test-utils;
mounting it would need a router, a socket and the whole api surface). So
``frontend/src/utils/voiceDictationLifecycle.test.js`` asserts the wiring against the SOURCE, plus
one parity walk against the real store.

It fails the moment the per-clip machinery grows back (gap placeholder tokens, per-clip chips, the
send-without-gap confirm), the recorder stops running in timeslice mode, fragments stop being
mirrored as they arrive, the 5-minute cap starts discarding instead of auto-stopping, a chip's copy
stops distinguishing "not sent yet" from "your last message went without it", the leave guard goes
back to arming on anything outstanding, or an un-rebuildable recovery take is offered a Transcribe
button that cannot work.

This subprocess-runs it with ``node --test`` (which exits non-zero on any failed assertion) so the
contract is enforced by every CI run, not just by ``npm run build``."""

import os
import shutil
import subprocess
import unittest

import frappe


def _test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceDictationLifecycle.test.js")


class TestVoiceDictationLifecycleClient(unittest.TestCase):
	def test_client_voice_dictation_lifecycle_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice dictation-lifecycle test missing at {test_file} — the contract MUST ship with its walk",
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
			"client voice dictation-lifecycle node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
