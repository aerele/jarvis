"""A render deadline must also stop worker-created font scanners."""

import contextlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.tools._export.document import charts


class TestChartProcessCleanup(unittest.TestCase):
	def test_timeout_stops_worker_descendants(self):
		self._assert_descendants_stopped("time.sleep(60)", TimeoutError)

	def test_worker_death_stops_its_descendants(self):
		self._assert_descendants_stopped("os._exit(1)", ValueError)

	def _assert_descendants_stopped(self, worker_exit, expected_error):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root)
			pid_file = path / "child.pid"
			ready_file = path / "child.ready"
			child_script = (
				"from pathlib import Path; import time; "
				f"Path({str(ready_file)!r}).write_text('ready'); time.sleep(60)"
			)
			(path / "chart_worker.py").write_text(
				"import os, subprocess, sys, time\n"
				"from pathlib import Path\n"
				f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}], cwd={str(path)!r}, "
				"stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
				f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
				f"while not Path({str(ready_file)!r}).exists(): time.sleep(0.01)\n"
				f"{worker_exit}\n"
			)
			try:
				with patch.object(charts, "__file__", str(path / "charts.py")):
					with self.assertRaises(expected_error):
						charts.render_charts(
							{
								0: charts.normalize_chart(
									{"type": "bar", "rows": [{"label": "A", "value": 1}]}
								)
							},
							deadline=time.monotonic() + 1,
						)
				self.assertTrue(pid_file.exists(), "test worker did not start its child")
				self.assertTrue(ready_file.exists(), "test child did not finish starting")
				pid = int(pid_file.read_text())
				# An adopted child may briefly be a zombie until the system reaps it.
				# It must have stopped running; kill(pid, 0) alone cannot tell these apart.
				for _ in range(20):
					state = subprocess.run(
						["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
					).stdout.strip()
					if not state or state.startswith("Z"):
						break
					time.sleep(0.05)
				self.assertTrue(
					not state or state.startswith("Z"), f"font scanner survived worker failure: {state}"
				)
			finally:
				if pid_file.exists():
					with contextlib.suppress(ProcessLookupError):
						os.kill(int(pid_file.read_text()), signal.SIGKILL)
