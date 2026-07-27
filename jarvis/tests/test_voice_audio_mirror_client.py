"""The crash-safety mirror's pure decisions — who may recover which audio, and how a crashed
session's loose fragments are rebuilt back into a recording — proven by a REAL executable node
test that lives in the python suite forever (the eventFence precedent).

Dictation audio is mirrored to IndexedDB, but IndexedDB is per-ORIGIN, not per-login — so on a
shared browser profile (or account-switching) reload recovery must NOT offer user A's audio to
user B. And because a take is stored as timeslice FRAGMENTS, recovery has to rebuild it: fragments
are continuations of one stream, so concatenating them in ``index`` order reproduces the recording
and a prefix of them is still decodable. The one unrecoverable case — a missing FIRST fragment,
which carries the container's initialisation segment — must be surfaced as Download + Discard, never
silently dropped. Legacy per-clip records written by the previous release are still offered, so an
upgrade never deletes audio a user had not yet recovered.

Those decisions are pure functions in ``frontend/src/utils/voiceAudioMirror.js``
(``filterOrphanFragments``, ``groupOrphanRecordings``, ``adoptionOps``);
``frontend/src/utils/voiceAudioMirror.test.js`` asserts each of them, including that an adoption
pairs its PUT with the DELETE of every superseded key in ONE transaction.

This subprocess-runs that test with ``node --test`` (which exits non-zero on any failed assertion)
so the client contract is enforced by every CI run, not just by ``npm run build``."""

import os
import shutil
import subprocess
import unittest

import frappe


def _test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceAudioMirror.test.js")


class TestVoiceAudioMirrorClient(unittest.TestCase):
	def test_client_voice_audio_mirror_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice audio-mirror test missing at {test_file} — the contract MUST ship with its walk",
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
			"client voice audio-mirror node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
