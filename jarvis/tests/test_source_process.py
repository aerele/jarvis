"""Real child failure, capacity, environment and limit regressions."""

import io
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _source_process as shared
from jarvis.tools._spreadsheet_process import RESOURCE_ERROR, run_query


class TestSourceProcess(unittest.TestCase):
	def test_parser_environment_excludes_secrets_and_pins_numeric_threads(self):
		with patch.dict(os.environ, {"SOURCE_PRIVATE_TOKEN": "secret", "OPENBLAS_NUM_THREADS": "64"}):
			env = shared.worker_env()
		self.assertNotIn("SOURCE_PRIVATE_TOKEN", env)
		for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
			self.assertEqual(env[key], "1")

	def test_capacity_is_shared_with_other_processes_and_recovers(self):
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(shared, "_slot_directory", return_value=Path(directory)),
		):
			with ExitStack() as stack:
				for _ in range(shared.MAX_CHILDREN):
					stack.enter_context(shared.worker_slot())
				with self.assertRaisesRegex(InvalidArgumentError, "busy"):
					with shared.worker_slot():
						self.fail("A fifth child was allowed")
				script = (
					"from pathlib import Path; from jarvis.tools import _source_process as s; "
					f"s._slot_directory=lambda:Path({directory!r}); "
					"s._claim_slot()"
				)
				child = subprocess.run(
					[sys.executable, "-c", script],
					capture_output=True,
					text=True,
					env=shared.worker_env(),
					timeout=5,
				)
				self.assertNotEqual(child.returncode, 0)
				self.assertIn("File readers are busy", child.stderr)
			with shared.worker_slot():
				pass

	def test_child_retains_the_lock_after_the_parent_releases_its_copy(self):
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(shared, "_slot_directory", return_value=Path(directory)),
			patch.object(shared, "MAX_CHILDREN", 1),
		):
			with shared.worker_slot() as slot:
				child = subprocess.Popen(
					[sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
					stdin=subprocess.PIPE,
					pass_fds=(slot,),
					env=shared.worker_env(),
				)
			try:
				with self.assertRaisesRegex(InvalidArgumentError, "busy"):
					with shared.worker_slot():
						self.fail("The running child lost its claim")
			finally:
				child.communicate(b"x", timeout=5)
			with shared.worker_slot():
				pass

	def test_failures_are_distinct_and_diagnostics_contain_no_source_values(self):
		logger = Mock()
		with patch("frappe.logger", return_value=logger):
			for status, stderr, expected in (
				(1, "ImportError: PRIVATE-SOURCE-VALUE", "failed before"),
				(1, "Traceback PRIVATE-SOURCE-VALUE", "failed before"),
				(-signal.SIGKILL, "PRIVATE-SOURCE-VALUE", "resource budget"),
			):
				with (
					self.subTest(status=status, stderr=stderr),
					patch(
						"jarvis.tools._spreadsheet_process.subprocess.run",
						return_value=SimpleNamespace(returncode=status, stdout="", stderr=stderr),
					),
				):
					with self.assertRaisesRegex(InvalidArgumentError, expected):
						run_query(b"A\n1", "private.csv")
			self.assertNotIn("PRIVATE-SOURCE-VALUE", repr(logger.mock_calls))

	def test_malformed_child_replies_never_become_a_complete_result(self):
		for stdout in ("not json", "[]", '{"result":null}', '{"result":"wrong"}', "x" * 131073):
			with (
				self.subTest(stdout=stdout[:30]),
				patch(
					"jarvis.tools._spreadsheet_process.subprocess.run",
					return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
				),
			):
				with self.assertRaisesRegex(InvalidArgumentError, "failed before"):
					run_query(b"A\n1", "input.csv")

	def test_scan_and_query_set_limits_before_reading_source(self):
		from jarvis.tools import _file_scan_worker, _spreadsheet_worker

		request = dict(
			content="QQox", filename="input.csv", sql=None, sheets=None, sheet=None, cursor=None, max_rows=10
		)
		for worker, parser, cpu, wall in (
			(_file_scan_worker, "jarvis.tools._file_scan.scan_file", 8, 12),
			(_spreadsheet_worker, "jarvis.tools._spreadsheet_query.query_spreadsheet", 16, 22),
		):
			with (
				self.subTest(worker=worker.__name__),
				patch.object(resource, "setrlimit") as limits,
				patch.object(signal, "alarm") as alarm,
				patch.object(sys, "platform", "linux"),
				patch.object(sys, "stdin", io.StringIO(json.dumps(request))),
				patch.object(sys, "stdout", io.StringIO()),
				patch(parser, return_value={}) as parse,
			):

				def result(*args, **kwargs):
					self.assertEqual(limits.call_args_list[0].args, (resource.RLIMIT_CORE, (0, 0)))
					self.assertEqual(limits.call_args_list[1].args, (resource.RLIMIT_CPU, (cpu, cpu)))
					self.assertEqual(
						limits.call_args_list[2].args, (resource.RLIMIT_AS, (512 * 1024**2,) * 2)
					)
					return {}

				parse.side_effect = result
				worker.main()
				parse.assert_called_once()
				alarm.assert_called_once_with(wall)

	@unittest.skipUnless(sys.platform.startswith("linux"), "Linux address-space enforcement")
	def test_real_child_refuses_an_allocation_above_its_memory_budget(self):
		script = """
import io, json, sys
from jarvis.tools import _spreadsheet_query, _spreadsheet_worker
def allocate(*args, **kwargs):
    return bytearray(1024 * 1024 * 1024)
_spreadsheet_query.query_spreadsheet = allocate
sys.stdin = io.StringIO(json.dumps(dict(content="QQox", filename="a.csv", sql=None, sheets=None, max_rows=10)))
_spreadsheet_worker.main()
"""
		child = subprocess.run(
			[sys.executable, "-c", script],
			capture_output=True,
			text=True,
			env=shared.worker_env(),
			timeout=10,
		)
		self.assertEqual(child.returncode, 0, child.stderr)
		self.assertEqual(json.loads(child.stdout), {"error": RESOURCE_ERROR})

	@unittest.skipUnless(sys.platform.startswith("linux"), "Linux CPU enforcement")
	def test_real_child_cpu_limit_stops_runaway_parser_work(self):
		script = """
import io, json, resource, sys
from jarvis.tools import _spreadsheet_query, _spreadsheet_worker
original = resource.setrlimit
def limits(kind, values):
    original(kind, (1, 2) if kind == resource.RLIMIT_CPU else values)
def loop(*args, **kwargs):
    while True:
        pass
resource.setrlimit = limits
_spreadsheet_query.query_spreadsheet = loop
sys.stdin = io.StringIO(json.dumps(dict(content="QQox", filename="a.csv", sql=None, sheets=None, max_rows=10)))
_spreadsheet_worker.main()
"""
		child = subprocess.run(
			[sys.executable, "-c", script], capture_output=True, text=True, env=shared.worker_env(), timeout=6
		)
		self.assertIn(child.returncode, (-signal.SIGXCPU, -signal.SIGKILL))

	def test_real_child_wall_limit_does_not_depend_on_its_parent_timeout(self):
		script = """
import io, json, signal, sys, time
from jarvis.tools import _spreadsheet_query, _spreadsheet_worker
original = signal.alarm
signal.alarm = lambda seconds: original(1)
_spreadsheet_query.query_spreadsheet = lambda *args, **kwargs: time.sleep(10)
sys.stdin = io.StringIO(json.dumps(dict(content="QQox", filename="a.csv", sql=None, sheets=None, max_rows=10)))
_spreadsheet_worker.main()
"""
		child = subprocess.run(
			[sys.executable, "-c", script], capture_output=True, text=True, env=shared.worker_env(), timeout=5
		)
		self.assertEqual(child.returncode, -signal.SIGALRM)
