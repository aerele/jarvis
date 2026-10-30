"""Archive reads on a disposable site with real private Files and normal users."""

import gzip
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests.test_archive_sources import archive
from jarvis.tools import _file_source
from jarvis.tools._archive_sources import ArchiveSourceChangedError
from jarvis.tools.query_file import query_file
from jarvis.tools.read_file import read_file


class TestArchiveIntegration(unittest.TestCase):
	def test_real_files_exact_identity_current_access_and_no_persistent_extracts(self):
		if not getattr(frappe.local, "db", None) or not str(frappe.local.site).startswith(
			("test_", "test.", "jarvis-ci.")
		):
			self.skipTest("Requires a disposable Frappe site")
		from frappe.utils.file_manager import save_file

		from jarvis.api import _run_tool
		from jarvis.chat.turn_handler import _prepare_attachments

		created = []
		original = frappe.session.user
		tag = uuid.uuid4().hex[:10]
		try:
			frappe.set_user("Administrator")
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": "archive-" + tag + "@example.test",
					"first_name": "Archive Test",
					"send_welcome_email": 0,
					"user_type": "System User",
					"roles": [{"role": "Jarvis User"}],
				}
			).insert(ignore_permissions=True)
			created.append(("User", user.name))
			content = archive(
				[
					(f"month{i}.csv", "Ref,Amount\n" + "".join(f"M{i}-{n},0.10\n" for n in range(1, 1001)))
					for i in range(1, 7)
				]
			)
			frappe.set_user(user.name)
			source = save_file("bank-" + tag + ".zip", content, None, None, is_private=1)
			created.append(("File", source.name))
			duplicate = save_file("copy-" + tag + ".zip", content, None, None, is_private=1)
			created.append(("File", duplicate.name))
			self.assertEqual(source.file_url, duplicate.file_url)  # Actual Frappe deduplication.
			frappe.db.commit()
			before = {
				dt: frappe.db.count(dt)
				for dt in ("File", "Prepared Report", "Notification Log", "Customer", "Payment Entry")
			}
			with patch.dict(frappe.conf, {"jarvis_archive_reading_enabled": True}):
				with self.assertRaises(PermissionDeniedError):
					query_file(file_url=source.file_url)
				manifest = query_file(file_id=source.name, file_url=source.file_url)
				self.assertFalse(manifest["coverage"]["complete"])
				args = {
					"file_id": source.name,
					"archive_version": manifest["archive_version"],
					"members": [f"m{i}" for i in range(1, 7)],
				}
				view = query_file(**args)
				union = " UNION ALL ".join(f"SELECT * FROM m{i}_sheet1 WHERE _row>1" for i in range(1, 7))
				result = _run_tool(
					"query_file",
					{
						**args,
						"version": view["version"],
						"sql": f"SELECT COUNT(*),decimal_sum(B) FROM ({union})",
					},
				)
				self.assertTrue(result["ok"], result)
				self.assertEqual(result["data"]["rows"], [[6000, "600.00"]])
				self.assertTrue(result["data"]["archive_complete"])
				self.assertEqual(
					query_file(
						**args, version=view["version"], sql="SELECT A FROM m6_sheet1 WHERE _row=1001"
					)["rows"],
					[["M6-1000"]],
				)
				with self.assertRaises(ArchiveSourceChangedError):
					query_file(**{**args, "file_id": duplicate.name})
				prompt, images = _prepare_attachments(
					"Total all six months", [{"file_id": source.name, "file_url": source.file_url}], False
				)
				self.assertEqual(images, [])
				self.assertIn(source.name, prompt)
				self.assertIn("inventory", prompt)
				self.assertNotIn("M6-1000", prompt)
				self.assertEqual(
					read_file(file_url=source.file_url + "?fid=" + source.name)["file_id"], source.name
				)
				# A real owner change revokes File access before the bounded disk read.
				frappe.set_user("Administrator")
				frappe.db.set_value("File", source.name, "owner", "Administrator")
				frappe.set_user(user.name)
				with (
					patch.object(_file_source, "read_bounded", side_effect=AssertionError("must not read")),
					self.assertRaises(PermissionDeniedError),
				):
					query_file(**args)
				with self.assertRaises(PermissionDeniedError):
					query_file(file_id="missing-" + tag)
				# Bounded storage reads use actual bytes even if metadata understates them.
				frappe.set_user("Administrator")
				with self.assertRaises(_file_source.FileCapacityError):
					_file_source.read_bounded(source, limit=2)
				broken = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": "broken-" + tag + ".csv.gz",
						"is_private": 1,
						"content": gzip.compress(b"A\n1\n")[:-3],
					}
				).insert(ignore_permissions=True)
				created.append(("File", broken.name))
				self.assertTrue(
					_file_source.read_bounded(broken).startswith(b"\x1f\x8b"),
					(broken.name, broken.file_url, _file_source.read_bounded(broken)[:20]),
				)
				inventory = query_file(file_id=broken.name)
				with self.assertRaises(InvalidArgumentError):
					read_file(file_id=broken.name, member="m1", archive_version=inventory["archive_version"])
				frappe.delete_doc("File", broken.name, ignore_permissions=True)
				self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			for dt, name in reversed(created):
				if frappe.db.exists(dt, name):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.commit()
			frappe.set_user(original)
