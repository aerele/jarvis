"""Actual worker and public API contracts with controlled File/access boundaries."""

import gzip
import io
import json
import resource
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.exceptions import FeatureDisabledError, PermissionDeniedError
from jarvis.tests.test_archive_sources import archive
from jarvis.tools import _archive_api as api
from jarvis.tools import _report_query_process as process
from jarvis.tools._archive_process import run_archive
from jarvis.tools._archive_sources import (
	ArchiveCapacityError,
	ArchiveIntegrityError,
	ArchiveModeError,
	ArchiveSourceChangedError,
)
from jarvis.tools.query_file import query_file
from jarvis.tools.read_file import read_file


class TestArchiveProcess(unittest.TestCase):
	def test_archive_worker_sets_core_cpu_memory_and_wall_limits(self):
		from jarvis.tools import _archive_worker

		request = {"content": "", "filename": "fixture.zip", "operation": "manifest"}
		with (
			patch.object(resource, "setrlimit") as limits,
			patch.object(signal, "alarm") as alarm,
			patch.object(sys, "platform", "linux"),
			patch.object(
				sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode() + b"\n"))
			),
			patch.object(sys, "stdout", SimpleNamespace(buffer=io.BytesIO())),
			patch("jarvis.tools._archive_sources.ArchiveSource") as source,
		):
			source.return_value.manifest.return_value = {}
			_archive_worker.main()
			self.assertEqual(limits.call_args_list[0].args, (resource.RLIMIT_CORE, (0, 0)))
			self.assertEqual(limits.call_args_list[1].args, (resource.RLIMIT_CPU, (16, 16)))
			self.assertEqual(limits.call_args_list[2].args, (resource.RLIMIT_AS, (512 * 1024**2,) * 2))
			alarm.assert_called_once_with(22)

	def test_actual_worker_queries_all_members(self):
		data = archive([("jan.csv", "Amount\n" + "0.10\n" * 3000), ("feb.csv", "Amount\n" + "0.20\n" * 3000)])
		result = run_archive(
			data,
			"bank.zip",
			operation="query",
			members=["m1", "m2"],
			sql="SELECT COUNT(*),decimal_sum(A) FROM (SELECT * FROM m1_sheet1 WHERE _row>1 UNION ALL SELECT * FROM m2_sheet1 WHERE _row>1)",
		)
		self.assertEqual(result["rows"], [[6000, "900.00"]])
		self.assertTrue(result["archive_complete"])

	def test_child_validates_crc_before_returning_window(self):
		data = gzip.compress(("line\n" * 6000).encode())[:-2]
		with self.assertRaises(ArchiveIntegrityError):
			run_archive(data, "data.txt.gz", operation="read", member="m1")

	def test_deadline_kills_and_reaps_archive_child(self):
		children = []
		real = process.subprocess.Popen

		def create(*args, **kwargs):
			child = real(*args, **kwargs)
			children.append(child)
			return child

		with (
			patch.object(process.subprocess, "Popen", side_effect=create),
			self.assertRaises(ArchiveCapacityError),
		):
			run_archive(
				gzip.compress(b"hello"), "data.txt.gz", operation="manifest", deadline=process.Deadline(0.001)
			)
		self.assertEqual(len(children), 1)
		self.assertIsNotNone(children[0].poll())


class TestArchiveApi(unittest.TestCase):
	def setUp(self):
		self.doc = SimpleNamespace(
			name="FILE-1", file_name="bank.zip", file_url="/private/files/bank.zip", attached_to_doctype=None
		)
		self.content = archive([("jan.csv", "Ref,Amount\nJ1,0.10\n"), ("feb.csv", "Ref,Amount\nF1,0.20\n")])
		self.patches = [
			patch.object(frappe, "conf", frappe._dict(jarvis_archive_reading_enabled=True)),
			patch.object(frappe, "local", SimpleNamespace(site="archive.test")),
			patch.object(frappe, "session", frappe._dict(user="user@example.test")),
			patch.object(frappe, "has_permission", return_value=True),
			patch("jarvis.tools.query_file.resolve_file", return_value=self.doc),
			patch("jarvis.tools.read_file.resolve_file", return_value=self.doc),
			patch.object(api, "read_bounded", side_effect=lambda *_: self.content),
		]
		for item in self.patches:
			item.start()
		self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

	def test_manifest_selected_view_and_scoped_sql_contract(self):
		inventory = query_file(file_id=self.doc.name)
		self.assertEqual(inventory["read_contract"], "archive-manifest-v1")
		self.assertFalse(inventory["coverage"]["complete"])
		self.assertNotIn("version", inventory)
		view = query_file(file_id=self.doc.name, archive_version=inventory["archive_version"], members=["m1"])
		result = query_file(
			file_id=self.doc.name,
			archive_version=inventory["archive_version"],
			members=["m1"],
			version=view["version"],
			sql="SELECT B FROM m1_sheet1 WHERE _row=2",
		)
		self.assertEqual(result["rows"], [["0.10"]])
		self.assertFalse(result["archive_complete"])
		self.assertEqual(result["unread_member_count"], 1)
		with patch.object(api, "run_archive") as child, self.assertRaises(ArchiveSourceChangedError):
			query_file(
				file_id=self.doc.name,
				archive_version=inventory["archive_version"],
				members=["m2"],
				version=view["version"],
				sql="SELECT COUNT(*) FROM m2_sheet1",
			)
		child.assert_not_called()

	def test_worksheet_subset_never_claims_whole_archive(self):
		import io

		from openpyxl import Workbook

		book = Workbook()
		book.active.title = "Jan"
		book.active.append(["Value"])
		book.active.append(["0.10"])
		book.create_sheet("Feb").append(["UNREAD"])
		buffer = io.BytesIO()
		book.save(buffer)
		self.content = archive([("bank.xlsx", buffer.getvalue())])
		inventory = query_file(file_id=self.doc.name)
		args = {"file_id": self.doc.name, "archive_version": inventory["archive_version"]}
		query = query_file(**args, members=["m1"], sheets=["Jan"])
		self.assertTrue(query["coverage"]["complete"])
		self.assertFalse(query["archive_complete"])
		self.assertEqual(query["unread_member_count"], 0)
		window = read_file(**args, member="m1", sheet="Jan")
		self.assertTrue(window["coverage"]["complete"])
		self.assertFalse(window["archive_complete"])
		self.assertTrue(query_file(**args, members=["m1"])["archive_complete"])

	def test_changed_source_and_permission_are_checked_each_call(self):
		inventory = query_file(file_id=self.doc.name)
		self.content = archive([("jan.csv", "changed")])
		with patch.object(api, "run_archive") as child, self.assertRaises(ArchiveSourceChangedError):
			query_file(file_id=self.doc.name, archive_version=inventory["archive_version"], members=["m1"])
		child.assert_not_called()
		with (
			patch.object(frappe, "has_permission", return_value=False),
			patch.object(api, "read_bounded") as read,
			self.assertRaises(PermissionDeniedError),
		):
			query_file(file_id=self.doc.name)
		read.assert_not_called()

	def test_exact_windows_pin_parent_and_member(self):
		self.doc.file_name = "notes.zip"
		self.content = archive(
			[("one.txt", "".join(f"line-{i}\n" for i in range(5000))), ("two.txt", "OTHER")]
		)
		inventory = read_file(file_id=self.doc.name)
		window = read_file(file_id=self.doc.name, member="m1", archive_version=inventory["archive_version"])
		self.assertEqual(window["read_contract"], "archive-cursor-v1")
		self.assertFalse(window["end_of_file"])
		next_read = window["next_read"]
		self.assertEqual(next_read["member"], "m1")
		self.assertEqual(next_read["file_id"], self.doc.name)
		second = read_file(**next_read)
		self.assertGreater(int(second["cursor"].split(":")[2]), int(window["cursor"].split(":")[2]))
		with self.assertRaises(ArchiveSourceChangedError):
			read_file(**{**next_read, "member": "m2"})

	def test_feature_and_mode_cannot_drop_archive_scope(self):
		inventory = query_file(file_id=self.doc.name)
		with (
			patch.object(frappe, "conf", frappe._dict(jarvis_archive_reading_enabled=False)),
			self.assertRaises(FeatureDisabledError),
		):
			query_file(file_id=self.doc.name, archive_version=inventory["archive_version"], members=["m1"])
		for kwargs in ({"preview": True}, {"read_mode": "unknown"}, {"read_mode": "preview"}):
			with self.subTest(kwargs=kwargs), self.assertRaises(ArchiveModeError):
				read_file(
					file_id=self.doc.name, member="m1", archive_version=inventory["archive_version"], **kwargs
				)
		with (
			patch.object(
				frappe,
				"conf",
				frappe._dict(jarvis_archive_reading_enabled=True, jarvis_file_sections_enabled=False),
			),
			self.assertRaises(FeatureDisabledError),
		):
			read_file(file_id=self.doc.name, member="m1", archive_version=inventory["archive_version"])
