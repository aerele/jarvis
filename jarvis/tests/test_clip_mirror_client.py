"""The chunked voice-dictation cross-user recovery gate (VAR-4), proven by a REAL
executable node test that lives in the python suite forever (the eventFence /
test_event_fence_client.py precedent).

Voice clips are mirrored to IndexedDB for crash recovery, but IndexedDB is per-ORIGIN,
not per-login — so on a shared browser profile (or account-switching) reload recovery
must NOT offer user A's un-transcribed audio to user B. The decision of which persisted
clips a logged-in user may recover is a pure function (``filterOrphans`` in
``frontend/src/utils/clipMirror.js``), and ``frontend/src/utils/clipMirror.test.js``
asserts it hides other users' clips, excludes the live session, drops legacy
un-stamped records, and sorts recovery order.

This subprocess-runs that test with ``node --test`` (which exits non-zero on any failed
assertion) so the client contract is enforced by every CI run, not just by
``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _mirror_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "clipMirror.test.js")


class TestClipMirrorClient(unittest.TestCase):
	def test_client_clip_mirror_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _mirror_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"clip-mirror test missing at {test_file} — the VAR-4 cross-user gate MUST ship with the module",
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
			"client clip-mirror node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
