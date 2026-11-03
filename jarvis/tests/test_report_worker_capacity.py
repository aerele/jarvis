"""Prepared reports share parser capacity and release claims on startup failure."""

import io
import resource
import signal
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _report_query_worker, _source_process
from jarvis.tools._report_query_process import Deadline, ReportWorker
from jarvis.tools._spreadsheet_process import run_query


class TestReportWorkerCapacity(unittest.TestCase):
	def test_report_and_file_queries_share_the_same_host_slots(self):
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(_source_process, "_slot_directory", return_value=Path(directory)),
			ExitStack() as claims,
		):
			for _ in range(_source_process.MAX_CHILDREN - 1):
				claims.enter_context(_source_process.worker_slot())
			with ReportWorker(Deadline()):
				with patch("jarvis.tools._spreadsheet_process.subprocess.run") as start:
					with self.assertRaisesRegex(InvalidArgumentError, "busy"):
						run_query(b"A\n1", "input.csv")
					start.assert_not_called()
			with _source_process.worker_slot():
				pass

	def test_failed_report_start_releases_its_slot(self):
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(_source_process, "_slot_directory", return_value=Path(directory)),
			patch.object(_source_process, "MAX_CHILDREN", 1),
		):
			with patch("jarvis.tools._report_query_process.subprocess.Popen", side_effect=OSError):
				with self.assertRaises(OSError):
					with ReportWorker(Deadline()):
						self.fail("The failed child must not enter")
			with _source_process.worker_slot():
				pass

	def test_report_child_sets_limits_and_wall_alarm_before_input(self):
		with (
			patch.object(resource, "setrlimit") as limits,
			patch.object(signal, "alarm") as alarm,
			patch.object(sys, "platform", "linux"),
			patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO())),
			patch.object(sys, "stdout", SimpleNamespace(buffer=io.BytesIO())),
		):
			_report_query_worker.main()
			self.assertEqual(limits.call_args_list[0].args, (resource.RLIMIT_CORE, (0, 0)))
			self.assertEqual(limits.call_args_list[1].args, (resource.RLIMIT_CPU, (16, 16)))
			self.assertEqual(limits.call_args_list[2].args, (resource.RLIMIT_AS, (512 * 1024**2,) * 2))
			alarm.assert_called_once_with(22)
