"""JF-018 — the Relay-Pump event fence on the PWA and the Desk widget, proven by REAL
executable node tests that live in the python suite forever.

The desktop SPA has fenced pump-owned realtime frames on (pump_epoch, event_seq) since
CDX-3/CDX-12, but that logic was written inside desktop's ChatView, so the two other
surfaces that consume the SAME frames — the mobile PWA and the floating Desk widget —
had no fence at all. A superseded pump's late CUMULATIVE delta could rewind a newer
projection after a handoff/replay, and a stale terminal could tear down a turn that had
already been taken over.

The fence is now a shared consumer contract at ``jarvis/public/js/shared/pump_fence.mjs``
(that tree, not ``frontend/src``, because the widget is an esbuild bundle which cannot
reach a Vue app's sources). Three node suites guard it, and this module subprocess-runs
each one with ``node --test`` (non-zero exit on any failed assertion):

* ``jarvis/public/js/shared/pump_fence.test.mjs`` — the comparison ladder: stale epoch,
  stale seq, terminal latch + one-shot repeat, epoch-bump watermark reset, run scoping,
  legacy bypass, PLUS an exhaustive 1728-walk decision-parity check against desktop's
  ``frontend/src/utils/eventFence.js`` so the two copies cannot silently drift.
* ``jarvis/public/js/jarvis_chat/widget/chat_stream.test.mjs`` — the widget reducer
  actually applying it.
* ``pwa/src/lib/pumpFence.test.js`` — the PWA's wiring (it has no component harness, so
  ChatView's gate is asserted against the source) plus the fence contract it depends on.
"""

import os
import shutil
import subprocess
import unittest

import frappe


def _app_root() -> str:
	return os.path.dirname(frappe.get_app_path("jarvis"))  # .../apps/jarvis


class TestPumpFenceSharedClient(unittest.TestCase):
	def _run_node_test(self, *relative_parts: str) -> None:
		node = shutil.which("node")
		if not node:
			# In CI this suite is the ONLY automated proof of the client fence
			# (jarvis CI never builds the frontends). A silent skip there would
			# let the guard rot green — fail loudly instead. Local dev boxes
			# without node still skip.
			if os.environ.get("CI"):
				self.fail("node is required in CI: the fence suites must actually run")
			self.skipTest("node binary not available on this host")
		test_file = os.path.join(_app_root(), *relative_parts)
		self.assertTrue(
			os.path.exists(test_file),
			f"client fence test missing at {test_file} — it MUST ship with the module",
		)
		proc = subprocess.run(
			[node, "--test", test_file],
			cwd=os.path.dirname(test_file),
			capture_output=True,
			text=True,
			timeout=180,
		)
		self.assertEqual(
			proc.returncode,
			0,
			f"node test FAILED ({os.path.join(*relative_parts)}):\n"
			f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
		)
		# Belt-and-suspenders: the runner reports the pass count, so a silently-skipped
		# suite (0 tests) cannot masquerade as green.
		self.assertIn("pass ", proc.stdout, f"node test produced no pass summary:\n{proc.stdout}")
		self.assertNotIn("fail 1", proc.stdout)

	def test_shared_fence_module(self):
		self._run_node_test("jarvis", "public", "js", "shared", "pump_fence.test.mjs")

	def test_widget_reducer_applies_the_fence(self):
		self._run_node_test("jarvis", "public", "js", "jarvis_chat", "widget", "chat_stream.test.mjs")

	def test_pwa_chatview_is_wired_to_the_fence(self):
		self._run_node_test("pwa", "src", "lib", "pumpFence.test.js")
