"""The client-side near-silence gate for voice dictation, proven by a REAL executable node test
that lives in the python suite forever (the eventFence / voiceDictationStore precedent).

whisper-large-v3-turbo deterministically HALLUCINATES on near-silent audio — a pure-silence 15 s
webm transcribes as "Thank you." (3/3 on the probe) — so a recording nobody actually spoke into
would inject a phrase the user never said into the composer. It cannot be filtered server-side:
OpenRouter's transcription endpoint does not pass through ``verbose_json`` / ``no_speech_prob``,
and a phrase blocklist was vetoed because it would swallow the same words when a user genuinely
dictates them. So the gate is client-side: ``frontend/src/composables/useDictationRecorder.js``
runs a WebAudio AnalyserNode on the SAME MediaStream as the MediaRecorder and stamps the finished
take with the peak RMS seen across the WHOLE recording, and ChatView drops a take measured below
the threshold WITHOUT an API call — no text, no chip, not a failure. Because the peak is per TAKE,
a pause mid-sentence is simply part of the recording; only a take that was inaudible from end to
end is skipped.

The decision + the meter live in a plain, importable module
(``frontend/src/utils/voiceSilenceGate.js``) so they are testable without a browser.
``frontend/src/utils/voiceSilenceGate.test.js`` drives them against injected WebAudio fakes and
stitches the gate to the REAL voiceDictationStore: a measured-silent take is dropped end to end; an
audible take transcribes; and every failure mode of the meter (no AudioContext, a constructor or
graph call that throws, a suspended context, a take shorter than one sample) reports UNMEASURED so
the take is still uploaded — absence of measurement must never cost audio.

This subprocess-runs it with ``node --test`` (which exits non-zero on any failed assertion) so
the client contract is enforced by every CI run, not just by ``npm run build``.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _gate_test_path() -> str:
	app_root = os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis
	return os.path.join(app_root, "frontend", "src", "utils", "voiceSilenceGate.test.js")


class TestVoiceSilenceGateClient(unittest.TestCase):
	def test_client_voice_silence_gate_node_walk_passes(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node binary not available on this host")
		test_file = _gate_test_path()
		self.assertTrue(
			os.path.exists(test_file),
			f"voice silence-gate test missing at {test_file} — the gate MUST ship with its walk",
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
			"client voice silence-gate node test FAILED:\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)
