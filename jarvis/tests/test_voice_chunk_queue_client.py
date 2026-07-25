"""The chunked voice-dictation ordering/retry/retention contract, proven by a REAL
executable node test that lives in the python suite forever (the eventFence /
test_event_fence_client.py precedent).

Long dictation is captured as many self-contained ~15 s WebM clips (the header trap
forbids timeslice continuation chunks) and each is transcribed by an INDEPENDENT async
request to the existing stateless endpoint, so responses arrive out of order, some time
out, and the audio must NEVER be lost. That whole state machine was extracted into a
plain, importable module (``frontend/src/utils/voiceChunkQueue.js``) so it is testable
without a browser. ``frontend/src/utils/voiceChunkQueue.test.js`` replays it end to end:
strict in-order commit under out-of-order completions; bounded (<=2) concurrency;
per-chunk budget + ONE auto-retry then a terminal ``failed`` state; the fail-through
that keeps later chunks committing while a gap is retried; the IndexedDB mirror
lifecycle (mirror on enqueue, clear on commit, RETAIN on fail); reload recovery drain;
and the unload-guard / dispose paths.

This subprocess-runs it with ``node --test`` (which exits non-zero on any failed
assertion) so the client contract is enforced by every CI run, not just by
``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _queue_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceChunkQueue.test.js")


class TestVoiceChunkQueueClient(unittest.TestCase):
	def test_client_voice_chunk_queue_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _queue_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice chunk-queue test missing at {test_file} — the state-machine walk MUST ship with the module",
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
			"client voice chunk-queue node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
