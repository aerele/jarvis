from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools import _holiday_advisory as ha
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc


class TestAdvisoryWiringSingle(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_create_single_attaches_warnings(self):
		# Stub the write primitive so we don't need a real Attendance fixture,
		# and stub the advisory so we assert ONLY the wiring.
		fake = frappe._dict(doctype="Attendance", employee="EMP-1")
		fake.as_dict = lambda: {"doctype": "Attendance", "name": "ATT-1"}
		fake.apply_fieldlevel_read_permissions = lambda: None
		with (
			patch("jarvis.tools.create_doc._insert_one", return_value=fake),
			patch("jarvis.tools.create_doc._validate_create_args"),
			patch.object(ha, "advisories_for_doc", return_value=["W1"]),
		):
			out = create_doc(doctype="Attendance", values={"employee": "EMP-1"})
		self.assertEqual(out.get("warnings"), ["W1"])

	def test_create_single_no_warning_key_when_empty(self):
		fake = frappe._dict(doctype="ToDo")
		fake.as_dict = lambda: {"doctype": "ToDo", "name": "TD-1"}
		fake.apply_fieldlevel_read_permissions = lambda: None
		with (
			patch("jarvis.tools.create_doc._insert_one", return_value=fake),
			patch("jarvis.tools.create_doc._validate_create_args"),
			patch.object(ha, "advisories_for_doc", return_value=[]),
		):
			out = create_doc(doctype="ToDo", values={"description": "x"})
		self.assertNotIn("warnings", out)

	def test_update_single_attaches_warnings(self):
		fake = frappe._dict(doctype="Attendance", employee="EMP-1")
		fake.as_dict = lambda: {"doctype": "Attendance", "name": "ATT-1"}
		fake.apply_fieldlevel_read_permissions = lambda: None
		with (
			patch("jarvis.tools.update_doc._update_one", return_value=fake),
			patch("jarvis.tools.update_doc.require_doctype_and_name"),
			patch.object(ha, "advisories_for_doc", return_value=["W2"]),
		):
			out = update_doc(doctype="Attendance", name="ATT-1", changes={"status": "Present"})
		self.assertEqual(out.get("warnings"), ["W2"])

	def test_batch_create_attaches_per_record_warnings(self):
		from jarvis.tools.create_doc import create_doc

		created_docs = [
			frappe._dict(doctype="Attendance", name="ATT-1"),
			frappe._dict(doctype="Attendance", name="ATT-2"),
		]
		it = iter(created_docs)
		with (
			patch("jarvis.tools.create_doc._insert_one", side_effect=lambda dt, v: next(it)),
			patch("jarvis.tools.create_doc._validate_create_args"),
			patch.object(ha, "advisories_for_doc", side_effect=[["W-A"], []]),
		):
			out = create_doc(
				docs=[
					{"doctype": "Attendance", "values": {"employee": "E1"}},
					{"doctype": "Attendance", "values": {"employee": "E2"}},
				]
			)
		self.assertEqual(out["created"][0].get("warnings"), ["W-A"])
		self.assertNotIn("warnings", out["created"][1])

	def test_batch_update_aggregates_warnings(self):
		from jarvis.tools.update_doc import update_doc

		docs = [
			frappe._dict(doctype="Attendance", name="ATT-1"),
			frappe._dict(doctype="Attendance", name="ATT-2"),
		]
		it = iter(docs)
		with (
			patch("jarvis.tools.update_doc._update_one", side_effect=lambda dt, n, c: next(it)),
			patch.object(ha, "advisories_for_doc", side_effect=[["W-1"], ["W-2"]]),
		):
			out = update_doc(
				doctype="Attendance",
				updates=[
					{"name": "ATT-1", "changes": {"status": "Present"}},
					{"name": "ATT-2", "changes": {"status": "Present"}},
				],
			)
		self.assertEqual(sorted(out.get("warnings", [])), ["W-1", "W-2"])
