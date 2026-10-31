"""Current field and omitted-link permissions against a real disposable site."""

import gzip
import json
import unittest
import uuid
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._report_query_source import _pinned_restrictions

TERRITORY_FILTER = {"fieldname": "territory", "fieldtype": "Link", "options": "Territory"}


def _grants(*names):
	return {"Territory": [frappe._dict(doc=name, applicable_for=None) for name in names]}


class TestStoredReportPermissions(unittest.TestCase):
	def test_field_denial_and_new_omitted_link_restriction(self):
		if not getattr(frappe.local, "db", None) or not str(frappe.local.site).startswith(
			("test_", "test.", "jarvis-ci.")
		):
			self.skipTest("Requires a connected disposable Frappe site")
		from frappe.utils.file_manager import save_file

		from jarvis.tools.query_report import query_report

		tag = uuid.uuid4().hex[:8]
		created = []
		original_user = frappe.session.user

		def save(values):
			doc = frappe.get_doc(values).insert(ignore_permissions=True)
			created.append((doc.doctype, doc.name))
			return doc

		try:
			frappe.set_user("Administrator")
			doctype = save(
				{
					"doctype": "DocType",
					"name": "Jarvis Report Gate " + tag,
					"module": "Custom",
					"custom": 1,
					"autoname": "hash",
					"fields": [
						{"fieldname": "public_value", "label": "Public", "fieldtype": "Data"},
						{"fieldname": "secret_value", "label": "Secret", "fieldtype": "Data", "permlevel": 1},
						{
							"fieldname": "territory",
							"label": "Territory",
							"fieldtype": "Link",
							"options": "Territory",
						},
					],
					"permissions": [
						{"role": "Accounts User", "read": 1, "report": 1, "permlevel": 0},
						{"role": "System Manager", "read": 1, "report": 1, "permlevel": 0},
						{"role": "System Manager", "read": 1, "permlevel": 1},
					],
				}
			)
			email = "report-gate-" + tag + "@example.test"
			save(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Report gate",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
					"user_type": "System User",
				}
			)
			report = save(
				{
					"doctype": "Report",
					"report_name": "Jarvis Permission Report " + tag,
					"ref_doctype": doctype.name,
					"report_type": "Query Report",
					"is_standard": "No",
					"query": "select name from `tab" + doctype.name + "`",
					"prepared_report": 1,
					"roles": [{"role": "Accounts User"}],
				}
			)
			with patch("frappe.core.doctype.prepared_report.prepared_report.enqueue"):
				run = save({"doctype": "Prepared Report", "report_name": report.name, "filters": "{}"})
			frappe.db.set_value("Prepared Report", run.name, {"owner": email, "status": "Completed"})
			body = {
				"columns": [
					{"fieldname": "public_value", "label": "Public", "fieldtype": "Data"},
					{"fieldname": "secret_value", "label": "Secret", "fieldtype": "Data"},
				],
				"result": [{"public_value": "visible", "secret_value": "PRIVATE-SALARY-" + tag}],
				"skip_total_row": 1,
			}
			file = save_file(
				"report-gate-" + tag + ".json.gz",
				gzip.compress(json.dumps(body).encode()),
				"Prepared Report",
				run.name,
				is_private=1,
			)
			created.append(("File", file.name))
			frappe.db.commit()
			frappe.set_user(email)
			initial = query_report(run.name)
			self.assertNotIn("PRIVATE-SALARY-" + tag, json.dumps(initial))
			self.assertEqual([c["fieldname"] for c in initial["source_columns"]], ["public_value"])
			self.assertEqual(
				query_report(run.name, version=initial["version"], sql="SELECT c1 FROM report_rows")["rows"],
				[["visible"]],
			)
			with self.assertRaises(InvalidArgumentError):
				query_report(run.name, version=initial["version"], sql="SELECT c2 FROM report_rows")
			frappe.set_user("Administrator")
			territory = frappe.db.get_value("Territory", {}, "name")
			if not territory:
				territory = save(
					{"doctype": "Territory", "territory_name": "Report gate " + tag, "is_group": 1}
				).name
			restriction = save(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Territory",
					"for_value": territory,
					"apply_to_all_doctypes": 1,
				}
			)
			frappe.db.commit()
			frappe.set_user(email)
			# A fresh inspection must not bless old rows whose required link is absent.
			with self.assertRaises(PermissionDeniedError):
				query_report(run.name)
			# A run pinned to a permitted Territory by a Territory filter is inside the
			# restriction, though the saved columns have no Territory; one pinned elsewhere is not.
			frappe.set_user("Administrator")
			frappe.delete_doc("User Permission", restriction.name, force=True, ignore_permissions=True)
			allowed = save(
				{
					"doctype": "Territory",
					"territory_name": "Gate in " + tag,
					"parent_territory": "All Territories",
				}
			).name
			other = save(
				{
					"doctype": "Territory",
					"territory_name": "Gate out " + tag,
					"parent_territory": "All Territories",
				}
			).name
			save(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Territory",
					"for_value": allowed,
					"apply_to_all_doctypes": 1,
				}
			)
			frappe.db.set_value(
				"Report",
				report.name,
				"javascript",
				"frappe.query_reports['%s'] = {filters: [{fieldname: 'territory', "
				"fieldtype: 'Link', options: 'Territory'}]}" % report.name,
			)
			frappe.db.commit()
			for stored, permitted in (
				({"territory": allowed}, True),
				({"territory": other}, False),
				({}, False),
			):
				frappe.set_user("Administrator")
				frappe.db.set_value("Prepared Report", run.name, "filters", json.dumps(stored))
				frappe.db.commit()
				frappe.set_user(email)
				if permitted:
					self.assertIn("version", query_report(run.name))
				else:
					with self.assertRaises(PermissionDeniedError):
						query_report(run.name)
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			for dt, name in reversed(created):
				if frappe.db.exists(dt, name):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.commit()
			frappe.set_user(original_user)


class TestPinnedRestrictions(unittest.TestCase):
	"""Which restricted doctypes a stored run's filters credit (no database)."""

	def pinned(self, stored, *grants, filters=(TERRITORY_FILTER,), ref_links=None, groups=()):
		links = {"territory": "Territory"} if ref_links is None else ref_links
		return _pinned_restrictions(
			_grants(*grants),
			list(filters),
			stored,
			links,
			lambda doctype, values: set(groups) & set(values),
		)

	def test_a_filter_that_is_not_the_doctypes_own_link_credits_nothing(self):
		self.assertEqual(self.pinned({"territory": "North"}, "North", ref_links={}), set())
		self.assertEqual(
			self.pinned({"territory": "North"}, "North", ref_links={"territory": "Customer"}), set()
		)

	def test_every_link_filter_to_the_doctype_must_be_pinned(self):
		second = {"fieldname": "billing_territory", "fieldtype": "Link", "options": "Territory"}
		links = {"territory": "Territory", "billing_territory": "Territory"}
		filters = (TERRITORY_FILTER, second)
		self.assertEqual(
			self.pinned({"territory": "North"}, "North", filters=filters, ref_links=links), set()
		)
		# A second Territory link on the DocType with no filter of its own is not covered.
		self.assertEqual(
			self.pinned({"territory": "North"}, "North", ref_links=links),
			set(),
		)
		self.assertEqual(
			self.pinned(
				{"territory": "North", "billing_territory": "North"},
				"North",
				filters=filters,
				ref_links=links,
			),
			{"Territory"},
		)

	def test_a_tree_group_value_credits_nothing(self):
		self.assertEqual(self.pinned({"territory": "North"}, "North", groups=("North",)), set())
		self.assertEqual(
			self.pinned({"territory": ["North", "South"]}, "North", "South", groups=("South",)), set()
		)

	def test_a_permitted_value_credits_the_doctype(self):
		self.assertEqual(self.pinned({"territory": "North"}, "North"), {"Territory"})

	def test_every_value_of_a_list_filter_must_be_permitted(self):
		self.assertEqual(self.pinned({"territory": ["North", "South"]}, "North", "South"), {"Territory"})
		self.assertEqual(self.pinned({"territory": ["North", "East"]}, "North", "South"), set())

	def test_a_value_outside_the_permitted_ones_credits_nothing(self):
		self.assertEqual(self.pinned({"territory": "East"}, "North"), set())

	def test_an_unset_filter_credits_nothing(self):
		for stored in (
			{},
			{"territory": ""},
			{"territory": []},
			{"territory": None},
			{"territory": ["", "North"]},
		):
			with self.subTest(stored=stored):
				self.assertEqual(self.pinned(stored, "North"), set())

	def test_only_a_link_filter_to_the_doctype_counts(self):
		for definition in (
			{"fieldname": "territory", "fieldtype": "Data", "options": "Territory"},
			{"fieldname": "territory", "fieldtype": "Link", "options": "Customer"},
			{"fieldname": "area", "fieldtype": "Link", "options": "Territory"},
		):
			with self.subTest(definition=definition):
				self.assertEqual(self.pinned({"territory": "North"}, "North", filters=(definition,)), set())

	def test_a_doctype_the_user_is_not_restricted_on_is_not_listed(self):
		self.assertEqual(
			_pinned_restrictions(
				{"Territory": []},
				[TERRITORY_FILTER],
				{"territory": "North"},
				{"territory": "Territory"},
				lambda doctype, values: set(),
			),
			set(),
		)
