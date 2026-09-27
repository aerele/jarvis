"""Harness configuration must reach emitted history and the selected image."""

import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.tests.harness import probe_real_gateway as probe
from jarvis.tests.harness.fake_gateway import FakeGateway, RunTimeline


class TestGatewayHarnessConfig(unittest.TestCase):
	def test_history_uses_each_gateways_explicit_metadata_key(self):
		for key in ("__test_gateway", "__alternate_gateway"):
			with self.subTest(key=key):
				gateway = FakeGateway(message_metadata_key=key)
				gateway._timelines["run"] = RunTimeline(run_id="run", session_key="session")
				gateway._armed["run"] = {
					"overrides": {"inject": {"recover_via": "history", "final_text": "Recovered answer"}}
				}
				self.assertEqual(
					gateway._history_for("session"),
					[
						{
							"role": "assistant",
							"content": "Recovered answer",
							key: {"seq": 1, "id": "rec-run"},
						}
					],
				)
				self.assertEqual(gateway._history_for("different-session"), [])

	def test_probe_requires_explicit_image_before_invoking_docker(self):
		with patch("sys.argv", ["probe"]), patch.object(probe.subprocess, "run") as run:
			with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
				probe.main()
			self.assertEqual(error.exception.code, 2)
			run.assert_not_called()

	def test_missing_local_image_stops_without_starting_container(self):
		with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(stdout="")) as run:
			self.assertFalse(probe.run("runtime-test:missing")["ok"])
			self.assertEqual(run.call_count, 1)
			self.assertEqual(run.call_args.args[0], ["docker", "images", "-q", "runtime-test:missing"])

	def test_probe_inspects_selected_image_and_fails_for_missing_symbol(self):
		image = "runtime-test:contract-fixture"
		for missing in (False, True):
			with self.subTest(missing=missing):

				def output(command, **kwargs):
					if command[1] == "images":
						return SimpleNamespace(stdout="image-id")
					self.assertEqual(
						command[1:8], ["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image]
					)
					absent = missing and "hasActiveRun" in command[-1]
					return SimpleNamespace(stdout="" if absent else "/app/dist/runtime.js\n")

				with patch.object(probe.subprocess, "run", side_effect=output):
					result = probe.run(image)
				self.assertEqual(result["ok"], not missing)
				self.assertEqual(result["image"], image)
