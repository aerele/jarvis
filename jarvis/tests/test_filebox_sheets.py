"""File Box sheets, collection (T3 S1a): one approval sheet per document.

Real ``api._run_tool`` calls in a ``file_box=1`` conversation (the test_filebox_held
harness): master proposals collect on ONE sealed sheet, questions link to it,
drafts wait on it, and the linked questions are frozen off the board."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import approvals_api, filebox_skills, held_parties, held_sheets, held_writes
from jarvis.chat.pending_actions import _seal, _store
from jarvis.chat.turn_message_binding import clear_run_cancel, request_run_cancel
from jarvis.tests._pending_action_helpers import as_user, draft_doctype, ensure_user

_ENABLED = held_sheets.enabled
PA = "Jarvis Pending Action"
WAITER = "Jarvis Pending Action Waiter"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
AR = "Jarvis Approval Request"
OWNER = "fbs-owner@example.com"
OTHER = "fbs-other@example.com"
USERS = (OWNER, OTHER)
ROLES = ("Jarvis User", "Purchase Master Manager", "Purchase User", "Item Manager")
TAX = "27FBSAC1234F1Z5"
PARTY = "zz-fbs Acme Traders"
GROUP = "zz-fbs Items"
UOM = "zz-fbs Unit"
_FIX_KEYS = {"field", "label", "message"}


def _supplier(name=PARTY, tax=TAX, **values):
	return {"doctype": "Supplier", "values": {"supplier_name": name, "tax_id": tax, **values}}


def _address(party=PARTY, kind="Billing", line="1 Main Road", **values):
	return {
		"doctype": "Address",
		"values": {
			"address_title": party,
			"address_type": kind,
			"address_line1": line,
			"city": "Chennai",
			"country": "India",
			"links": [{"link_doctype": "Supplier", "link_name": party}],
			**values,
		},
	}


def _item(code, supplier=None, **values):
	rows = [{"supplier": supplier}] if supplier else []
	return {
		"doctype": "Item",
		"values": {
			"item_code": code,
			"item_name": code,
			"item_group": GROUP,
			"stock_uom": UOM,
			"supplier_items": rows,
			**values,
		},
	}


def _question(title="zz-fbs which vendor?", **values):
	return {"doctype": AR, "values": {"title": title, "question": "Which vendor?", **values}}


def _batch(*docs):
	return {"docs": list(docs)}


def _wipe():
	frappe.db.rollback()
	names = frappe.db.sql_list(f"SELECT name FROM `tab{PA}` WHERE owner_user IN %(u)s", {"u": USERS})
	if names:
		frappe.db.sql(f"DELETE FROM `tab{WAITER}` WHERE parent IN %(n)s", {"n": tuple(names)})
		frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE name IN %(n)s", {"n": tuple(names)})
	convs = frappe.get_all(CONV, filters={"owner": ["in", USERS]}, pluck="name")
	if convs:
		frappe.db.delete(AR, {"conversation": ["in", convs]})
		frappe.db.delete(MSG, {"conversation": ["in", convs]})
		frappe.db.delete("Jarvis Chat Turn", {"conversation": ["in", convs]})
		frappe.db.delete("File", {"attached_to_name": ["in", convs]})
		frappe.db.delete(CONV, {"name": ["in", convs]})
	frappe.db.delete(AR, {"title": ["like", "zz-fbs%"]})
	frappe.db.delete("Address", {"address_title": ["like", "zz-fbs%"]})
	frappe.db.delete("Item", {"item_code": ["like", "zz-fbs%"]})
	frappe.db.delete("Item Group", {"item_group_name": ["like", "zz-fbs G%"]})
	frappe.db.delete("Supplier", {"supplier_name": ["like", "zz-fbs%"]})
	frappe.db.delete("Notification Log", {"for_user": ["in", USERS]})
	frappe.db.commit()


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Item Group", GROUP):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": GROUP, "is_group": 0}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("UOM", UOM):
			frappe.get_doc({"doctype": "UOM", "uom_name": UOM}).insert(ignore_permissions=True)
		cls.template = None
		if not frappe.db.exists("Address Template", "India"):  # an Address renders one on save
			cls.template = frappe.get_doc(
				{"doctype": "Address Template", "country": "India", "template": "{{ address_line1 }}"}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls.template:
			frappe.db.delete("Address Template", cls.template.name)  # the controller refuses a default
			frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		for user in USERS:
			ensure_user(user, ROLES)
		self._orig_user = frappe.session.user
		_wipe()
		self.addCleanup(self._cleanup)
		mint = patch("jarvis.chat.pending_confirm.mint", side_effect=AssertionError("chat card minted"))
		mint.start()
		self.addCleanup(mint.stop)
		enqueue = patch("frappe.enqueue")
		enqueue.start()
		self.addCleanup(enqueue.stop)
		self.flag = patch.object(held_sheets, "enabled", return_value=True)
		self.flag.start()
		self.addCleanup(self.flag.stop)

	def _cleanup(self):
		frappe.set_user(self._orig_user)
		frappe.local.jarvis_dispatch_depth = 0
		_wipe()

	def conv(self, owner=OWNER) -> str:
		with as_user(owner):
			doc = frappe.get_doc({"doctype": CONV, "title": "File: fbs.pdf"}).insert(ignore_permissions=True)
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"fbs-{doc.name}.txt",
				"content": "fbs invoice",
				"is_private": 1,
				"attached_to_doctype": CONV,
				"attached_to_name": doc.name,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			CONV, doc.name, {"file_box": 1, "filebox_source_file": f.name}, update_modified=False
		)
		frappe.db.commit()
		return doc.name

	def call(self, tool, args, conv, user=OWNER):
		with as_user(user):
			return api._run_tool(tool, json.loads(json.dumps(args)), conversation=conv)

	def sheets(self, owner=OWNER):
		return frappe.db.sql(
			f"SELECT * FROM `tab{PA}` WHERE owner_user=%(o)s AND kind='file_box_sheet' ORDER BY creation",
			{"o": owner},
			as_dict=True,
		)

	def sheet(self, conv):
		[row] = [s for s in self.sheets() if s.conversation == conv]
		return row

	def records(self, row):
		return _seal.unseal_call(_store.get_row(row.name))["args"]["records"]

	def set_row(self, name, **cols):
		for col, value in cols.items():
			frappe.db.sql(f"UPDATE `tab{PA}` SET `{col}`=%(v)s WHERE name=%(n)s", {"v": value, "n": name})
		frappe.db.commit()

	def assert_added(self, res, count=None):
		self.assertTrue(res["ok"], res)
		data = res["data"]
		self.assertEqual((data["status"], data["held_for"]), ("pending_confirmation", "approval_sheet"))
		self.assertIn("Keep going now", data["note"])
		self.assertNotIn("END your turn now", data["note"])
		if count is not None:
			self.assertEqual(data["records"], count)
		for name in frappe.db.sql_list(f"SELECT name FROM `tab{PA}`"):
			self.assertNotIn(name, json.dumps(res))  # the model never learns the row

	def assert_refused(self, res, code):
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], code, res)


class TestCollect(_Base):
	"""AC1: N proposals over any number of calls -> ONE collecting sheet."""

	def test_proposals_over_single_and_batch_calls_make_one_sheet(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv), 1)
		self.assert_added(self.call("create_doc", _batch(_item("zz-fbs A"), _item("zz-fbs B")), conv), 3)
		self.assert_added(self.call("create_doc", _address(), conv), 4)
		[row] = self.sheets()
		self.assertEqual(
			(row.kind, row.status, row.tool, row.collecting), (_store.SHEET, "Pending", "file_box_sheet", 1)
		)
		self.assertEqual((row.record_count, row.exec_user, row.owner_user), (4, OWNER, OWNER))
		self.assertEqual(row.open_key, _seal.open_key(OWNER, "sheet:" + conv))
		self.assertTrue(row.summary.startswith("Approval sheet: fbs-"))
		records = self.records(row)
		self.assertEqual([r["category"] for r in records], ["party", "item", "item", "address"])
		self.assertEqual(records[3]["depends_on"], [0])
		self.assertEqual(len(json.loads(row.card)["records"]), 4)
		waiters = frappe.get_all(WAITER, filters={"parent": row.name}, fields=["conversation", "role"])
		self.assertEqual([(w.conversation, w.role) for w in waiters], [(conv, "primary")])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": PARTY}))
		self.assertFalse(frappe.db.exists("Item", "zz-fbs A"))
		self.assertIsNone(held_writes.waiting_on(conv), "no legacy held row")

	def test_the_switch_off_keeps_the_legacy_hold(self):
		frappe.db.set_single_value("Jarvis Settings", "file_box_sheets", 0)
		conv = self.conv()
		with patch.object(held_sheets, "enabled", _ENABLED):
			res = self.call("create_doc", _supplier(), conv)
		self.assertIn("END your turn now", res["data"]["note"])
		self.assertEqual(self.sheets(), [])
		self.assertTrue(held_writes.waiting_on(conv))

	def test_the_switch_gates_new_sheets_only(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		with patch.object(held_sheets, "enabled", return_value=False):
			self.assert_added(self.call("create_doc", _item("zz-fbs A"), conv), 2)
			other = self.conv()
			self.assertIn(
				"END your turn now", self.call("create_doc", _item("zz-fbs B"), other)["data"]["note"]
			)
		self.assertEqual(len(self.sheets()), 1)

	def test_the_real_switch_reads_jarvis_settings(self):
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "file_box_sheets", 0)
		frappe.db.set_single_value("Jarvis Settings", "file_box_sheets", 1)
		self.assertTrue(_ENABLED())
		frappe.db.set_single_value("Jarvis Settings", "file_box_sheets", 0)
		self.assertFalse(_ENABLED())

	def test_an_append_reads_the_gate_row_once_without_its_envelope(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		with patch.object(held_sheets, "live_sheet", wraps=held_sheets.live_sheet) as read:
			self.assert_added(self.call("create_doc", _item("zz-fbs A"), conv), 2)
		self.assertEqual(read.call_count, 1)
		row = held_sheets.live_sheet(conv)
		self.assertFalse({"sealed_call", "card", "preview"} & set(row))
		self.assertEqual((row.record_count, row.collecting), (2, 1))

	def test_a_parallel_first_proposal_still_opens_one_sheet(self):
		"""open_key: a call that opened the sheet between our read and our insert wins;
		ours re-locks, re-reads and appends."""
		conv = self.conv()
		site, errors, real = frappe.local.site, [], held_sheets._open

		def open_elsewhere():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.set_user(OWNER)
				item = {
					"doctype": "Supplier",
					"values": {"supplier_name": "zz-fbs Other"},
					"name": "",
					"op": "create",
				}
				with patch.object(held_sheets, "enabled", return_value=True):
					real(conv, OWNER, [held_sheets._record(item)])
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				frappe.destroy()

		def racing(*a, **k):
			if not getattr(racing, "done", False):
				racing.done = True
				frappe.db.commit()  # park commits before it locks: the window
				t = threading.Thread(target=open_elsewhere)
				t.start()
				t.join(timeout=30)
			return real(*a, **k)

		with patch.object(held_sheets, "_open", side_effect=racing):
			res = self.call("create_doc", _supplier(), conv)
		self.assertEqual(errors, [])
		self.assert_added(res, 2)
		[row] = self.sheets()
		self.assertEqual([r["values"]["supplier_name"] for r in self.records(row)], ["zz-fbs Other", PARTY])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)


class TestDependencies(_Base):
	"""The dependency-aware dry-run (Spike 0 shortfall a)."""

	def test_an_address_of_a_held_supplier_validates_via_the_replay(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		res = self.call("create_doc", _address(), conv)
		self.assert_added(res, 2)
		self.assertNotIn("needs_input", res["data"])
		row = self.sheet(conv)
		self.assertFalse(row.needs_fix)
		self.assertEqual(self.records(row)[1]["depends_on"], [0])

	def test_contact_links_are_dependencies(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		contact = {
			"doctype": "Contact",
			"values": {
				"first_name": "zz-fbs Ravi",
				"links": [{"link_doctype": "Supplier", "link_name": PARTY}],
			},
		}
		res = self.call("create_doc", contact, conv)
		self.assert_added(res, 2)
		self.assertNotIn("A human fills or fixes", res["data"]["note"])
		record = self.records(self.sheet(conv))[1]
		self.assertEqual((record["category"], record["depends_on"]), ("address", [0]))

	def test_a_needs_input_dependency_is_replayed_not_stood_in(self):
		"""The replay inserts a flagged record with ignore_mandatory (validated), not
		as an unvalidated stand-in."""
		conv = self.conv()
		for _ in range(2):
			self.call("create_doc", _supplier("zz-fbs Two", "", supplier_type=""), conv)
		self.assertTrue(self.sheet(conv).needs_input)
		stood_in, real = [], held_sheets._stand_in

		def spy(record, values, remap):
			stood_in.append(record["doctype"])
			return real(record, values, remap)

		with patch.object(held_sheets, "_stand_in", side_effect=spy):
			self.assert_added(self.call("create_doc", _address(party="zz-fbs Two"), conv), 2)
		self.assertEqual(stood_in, [])

	def test_child_table_links_are_dependencies(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		self.assert_added(self.call("create_doc", _item("zz-fbs A", supplier=PARTY), conv))
		res = self.call("create_doc", _batch(_address(), _item("zz-fbs B", supplier=PARTY)), conv)
		self.assert_added(res, 4)
		self.assertEqual([r["depends_on"] for r in self.records(self.sheet(conv))], [[], [0], [0], [0]])

	def test_a_transitive_dependency_is_replayed_in_order(self):
		conv = self.conv()
		group = {"doctype": "Item Group", "values": {"item_group_name": "zz-fbs GA", "is_group": 1}}
		sub = {
			"doctype": "Item Group",
			"values": {"item_group_name": "zz-fbs GB", "parent_item_group": "zz-fbs GA"},
		}
		self.assert_added(self.call("create_doc", _batch(group, sub), conv))
		replayed, real = [], held_sheets._replay

		def spy(record, remap):
			replayed.append(record["values"]["item_group_name"])
			return real(record, remap)

		with patch.object(held_sheets, "_replay", side_effect=spy):
			self.assert_added(self.call("create_doc", _item("zz-fbs A", item_group="zz-fbs GB"), conv), 3)
		self.assertEqual(replayed, ["zz-fbs GA", "zz-fbs GB"])
		self.assertEqual(held_sheets._closure(self.records(self.sheet(conv)), [2]), [0, 1])

	def test_a_naming_series_supplier_is_remapped_in_the_sandbox(self):
		from frappe import defaults

		real = defaults.get_global_default

		def series(key, *a, **k):
			return "Naming Series" if key == "supp_master_name" else real(key, *a, **k)

		conv = self.conv()
		with patch("frappe.defaults.get_global_default", side_effect=series):
			self.assert_added(self.call("create_doc", _supplier(), conv))
			res = self.call("create_doc", _address(), conv)
		self.assert_added(res, 2)
		records = self.records(self.sheet(conv))
		self.assertEqual(
			records[0]["identity"], [f"gstin:Supplier:{TAX}", "name:Supplier:zz fbs acme traders"]
		)
		self.assertEqual(records[1]["values"]["links"][0]["link_name"], PARTY, "sealed as proposed")
		self.assertEqual(records[1]["depends_on"], [0])

	def test_an_unknown_link_goes_back_once_then_joins_flagged(self):
		conv = self.conv()
		first = self.call("create_doc", _address(party="zz-fbs Nobody"), conv)
		self.assert_refused(first, "InvalidArgumentError")
		self.assertIn("Address zz-fbs Nobody", first["error"]["message"])
		self.assertEqual(self.sheets(), [])
		res = self.call("create_doc", _address(party="zz-fbs Nobody"), conv)
		self.assert_added(res, 1)
		self.assertIn("A human fills or fixes these", res["data"]["note"])
		fix = json.loads(self.sheet(conv).needs_fix)["0"]
		self.assertIn("zz-fbs Nobody", fix["message"])
		self.assertIsNone(fix["field"], "a child row's field is never a main field")


class TestIdentity(_Base):
	def test_dedup_on_any_identity_and_a_changed_proposal_replaces(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv), 1)
		same = self.call("create_doc", _supplier(), conv)
		self.assert_added(same, 1)
		self.assertIn("Already on this document's approval sheet", same["data"]["note"])
		# The same tax id under another name: the supplier is re-proposed, not duplicated.
		self.assert_added(self.call("create_doc", _supplier("zz-fbs Acme Traders Ltd"), conv), 1)
		[record] = self.records(self.sheet(conv))
		self.assertEqual(record["values"]["supplier_name"], "zz-fbs Acme Traders Ltd")

	def test_billing_and_shipping_addresses_are_two_records(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		self.assert_added(self.call("create_doc", _address(), conv), 2)
		self.assert_added(self.call("create_doc", _address(kind="Shipping", line="9 Dock Road"), conv), 3)
		self.assert_added(self.call("create_doc", _address(), conv), 3)
		with patch("frappe.defaults.get_global_default", return_value="Supplier Name"):
			self.assertEqual(
				held_sheets.identities({"op": "create", **_supplier("x", "")})[0], "dn:Supplier:x"
			)

	def test_two_gst_registrations_of_the_same_party_type_title_stay_two_records(self):
		"""Same party/type/title, different GSTIN: _address_keys must not collide them."""
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		gstin_a, gstin_b = "27AAAAA0000A1Z5", "27BBBBB1111B1Z5"
		self.assert_added(self.call("create_doc", _address(gstin=gstin_a), conv), 2)
		self.assert_added(self.call("create_doc", _address(gstin=gstin_b), conv), 3)
		records = self.records(self.sheet(conv))
		self.assertEqual([r["values"].get("gstin") for r in records[1:]], [gstin_a, gstin_b])
		self.assertNotEqual(records[1]["identity"], records[2]["identity"])
		# A third proposal with the SAME gstin and street still dedups onto the first.
		self.assert_added(self.call("create_doc", _address(gstin=gstin_a, address_line2="Updated"), conv), 3)
		records = self.records(self.sheet(conv))
		self.assertEqual(records[1]["values"]["address_line2"], "Updated")

	def test_a_corrected_address_replaces_it(self):
		"""Address identity = type + linked party + street + pincode (its city without
		one), never its other values."""
		conv = self.conv()
		self.call("create_doc", _batch(_supplier(), _address()), conv)
		self.assert_added(self.call("create_doc", _address(address_line2="Floor 2"), conv), 2)
		records = self.records(self.sheet(conv))
		self.assertEqual(records[1]["values"]["address_line2"], "Floor 2")
		self.assertEqual(
			records[1]["identity"], [f"addr:billing:Supplier:{PARTY.casefold()}:1 main road:chennai"]
		)

	def test_two_streets_of_one_party_type_and_title_stay_two_records(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		for count, address in (
			(2, _address(pincode="600001")),
			(3, _address(line="9 Dock Road", pincode="600001")),  # another street
			(4, _address(pincode="600002")),  # another pincode
			(5, _address()),  # no pincode: its city
			(6, _address(city="Madurai")),
			(6, _address(line=" 1  MAIN road", pincode="600 001", address_line2="Floor 2")),  # the first
		):
			with self.subTest(count=count, values=address["values"]):
				self.assert_added(self.call("create_doc", address, conv), count)
		records = self.records(self.sheet(conv))
		self.assertEqual(records[1]["values"].get("address_line2"), "Floor 2")

	def test_a_renamed_record_takes_its_dependents_along(self):
		"""The same tax id under a new name: links to the old name follow it."""
		conv = self.conv()
		self.call("create_doc", _batch(_supplier(), _address(), _item("zz-fbs A", supplier=PARTY)), conv)
		renamed = "zz-fbs Acme Traders Ltd"
		self.assert_added(self.call("create_doc", _supplier(renamed), conv), 3)
		records = self.records(self.sheet(conv))
		self.assertEqual(records[1]["values"]["links"][0]["link_name"], renamed)
		self.assertEqual(records[2]["values"]["supplier_items"][0]["supplier"], renamed)
		self.assertEqual([r["depends_on"] for r in records], [[], [0], [0]])
		self.assertIn(renamed.casefold(), records[1]["identity"][0])
		self.assertEqual(records[1]["values"]["address_title"], PARTY, "only links follow")

	def test_a_record_that_already_exists_goes_back_to_the_model(self):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "tax_id": TAX}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		conv = self.conv()
		res = self.call("create_doc", _supplier(), conv)
		self.assert_refused(res, "InvalidArgumentError")
		self.assertIn("already exists", res["error"]["message"])
		self.assertEqual(self.sheets(), [])


class TestAddressIsNotAParty(_Base):
	"""India Compliance adds ``gstin`` to Address: a second address carrying a GSTIN on
	file is a new record, never an "existing party" (parties are Supplier/Customer)."""

	GSTIN = "33AAAAA0000A1Z5"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.added = not frappe.get_meta("Address").has_field("gstin")
		if cls.added:
			field = {"dt": "Address", "fieldname": "gstin", "label": "GSTIN", "fieldtype": "Data"}
			frappe.get_doc({"doctype": "Custom Field", **field}).insert(ignore_permissions=True)
			frappe.clear_cache(doctype="Address")
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls.added:
			frappe.delete_doc("Custom Field", "Address-gstin", ignore_permissions=True, force=True)
			frappe.clear_cache(doctype="Address")
			frappe.db.commit()
		super().tearDownClass()

	def test_a_second_address_of_a_gstin_on_file_is_proposed(self):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "tax_id": TAX}).insert(
			ignore_permissions=True
		)
		billing = _address(gstin=self.GSTIN)["values"]
		frappe.get_doc({"doctype": "Address", **billing}).insert(ignore_permissions=True)
		frappe.db.commit()
		shipping = _address(kind="Shipping", line="9 Dock Road", gstin=self.GSTIN)
		items = [{"op": "create", "name": "", **shipping}]
		with as_user(OWNER):
			self.assertIsNone(held_writes._existing_party(items))
		self.assertIsNone(approvals_api._first_existing(OWNER, items))  # the legacy held path
		conv = self.conv()
		self.assert_added(self.call("create_doc", shipping, conv), 1)
		with as_user(OWNER):  # a party on file still goes back to the model
			self.assertEqual(
				held_writes._existing_party([{"op": "create", "name": "", **_supplier()}])["doctype"],
				"Supplier",
			)

	def test_two_addresses_of_one_gstin_are_two_legacy_held_records(self):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "tax_id": TAX}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		billing = _address(gstin=self.GSTIN)
		shipping = _address(kind="Shipping", line="9 Dock Road", gstin=self.GSTIN)
		for address in (billing, shipping):
			[item] = held_parties.items_of("create_doc", address)
			self.assertEqual(held_parties.party_of(item)["gstin"], "")
			self.assertFalse(held_parties.is_party_key(held_parties.item_key(item)))
		with patch.object(held_sheets, "enabled", return_value=False):  # the switch off
			for address in (billing, shipping, billing):
				self.assertIn(
					"END your turn now", self.call("create_doc", address, self.conv())["data"]["note"]
				)
		held = frappe.get_all(PA, filters={"owner_user": OWNER, "kind": held_writes.HELD}, pluck="name")
		self.assertEqual(len(held), 2)  # never joined on the GSTIN; the same address still is
		self.assertEqual(self.sheets(), [])


class TestTriesOnce(_Base):
	"""Collect mode + SP-D1: a record's first failure goes back to the model."""

	def test_missing_fields_go_back_once_then_join_as_needs_input(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		first = self.call("create_doc", _supplier("zz-fbs Two", "", supplier_type=""), conv)
		self.assert_refused(first, "InvalidArgumentError")
		self.assertIn("Supplier zz-fbs Two: missing Supplier Type", first["error"]["message"])
		self.assertEqual(self.sheet(conv).record_count, 1, "nothing added on the first try")
		res = self.call("create_doc", _supplier("zz-fbs Two", "", supplier_type=""), conv)
		self.assert_added(res, 2)
		self.assertEqual(res["data"]["needs_input"], ["Supplier Type"])
		self.assertEqual(
			json.loads(self.sheet(conv).needs_input),
			[{"doc_index": 1, "doctype": "Supplier", "fieldname": "supplier_type"}],
		)
		# Fixed later: the record is replaced and its flag cleared.
		self.assert_added(self.call("create_doc", _supplier("zz-fbs Two", "", supplier_type="Company"), conv))
		self.assertFalse(self.sheet(conv).needs_input)

	def test_a_validation_error_goes_back_once_then_joins_needs_fix(self):
		from erpnext.stock.doctype.item.item import Item

		real = Item.validate

		def hsn_required(doc):
			if not doc.get("description"):
				frappe.throw("HSN/SAC Code is <b>required</b>`")
			return real(doc)

		conv = self.conv()
		with patch.object(Item, "validate", hsn_required):
			first = self.call(
				"create_doc", _batch(_item("zz-fbs A"), _item("zz-fbs B", description="ok")), conv
			)
			self.assert_refused(first, "InvalidArgumentError")
			self.assertIn("Item zz-fbs A: HSN/SAC Code is required", first["error"]["message"])
			self.assertEqual(self.sheets(), [], "all or nothing per call")
			res = self.call(
				"create_doc", _batch(_item("zz-fbs A"), _item("zz-fbs B", description="ok")), conv
			)
		self.assert_added(res, 2)
		[(key, fix)] = json.loads(self.sheet(conv).needs_fix).items()
		self.assertEqual((key, set(fix), fix["message"]), ("0", _FIX_KEYS, "HSN/SAC Code is required'"))

	def test_permission_errors_always_go_back(self):
		conv = self.conv()
		for _ in range(2):
			res = self.call(
				"create_doc",
				{"doctype": "Supplier", "values": {"supplier_name": "zz-fbs X", "owner": "x"}},
				conv,
			)
			self.assert_refused(res, "InvalidArgumentError")
			self.assertIn("protected", res["error"]["message"])
		self.assertEqual(self.sheets(), [])


class TestFixField(_Base):
	"""A flagged record's fix names the main field its error names (India Compliance's
	Item ``gst_hsn_code``, a custom field here), else ``field`` None."""

	HSN = "HSN/SAC Code is required. Please enter a valid HSN/SAC code."

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.added = not frappe.get_meta("Item").has_field("gst_hsn_code")
		if cls.added:
			field = {"dt": "Item", "fieldname": "gst_hsn_code", "label": "HSN/SAC", "fieldtype": "Data"}
			frappe.get_doc({"doctype": "Custom Field", **field}).insert(ignore_permissions=True)
			frappe.clear_cache(doctype="Item")
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls.added:
			frappe.delete_doc("Custom Field", "Item-gst_hsn_code", ignore_permissions=True, force=True)
			frappe.clear_cache(doctype="Item")
			frappe.db.commit()
		super().tearDownClass()

	def flagged(self, doc, validate=None):
		from erpnext.stock.doctype.item.item import Item

		conv = self.conv()
		with patch.object(Item, "validate", validate or Item.validate):
			self.assert_refused(self.call("create_doc", doc, conv), "InvalidArgumentError")
			self.assert_added(self.call("create_doc", doc, conv), 1)
		return json.loads(self.sheet(conv).needs_fix)["0"]

	def test_the_hsn_error_names_its_field(self):
		def hsn_required(doc):
			frappe.throw(self.HSN, frappe.MandatoryError)

		fix = self.flagged(_item("zz-fbs A"), hsn_required)
		self.assertEqual(fix, {"field": "gst_hsn_code", "label": "HSN/SAC", "message": self.HSN})

	def test_a_link_error_names_its_field(self):
		fix = self.flagged(_item("zz-fbs A", item_group="zz-fbs Nope"))
		self.assertEqual((fix["field"], fix["label"]), ("item_group", "Item Group"))
		self.assertIn("zz-fbs Nope", fix["message"])

	def test_an_error_naming_no_field_has_none(self):
		self.assertEqual(
			held_sheets._fix("Item", frappe.ValidationError("zz-fbs something broke")),
			{"field": None, "label": None, "message": "zz-fbs something broke"},
		)
		for text in ("[Item, zz-fbs A]: stock_uom, item_group", "Default Unit of Measure is wrong"):
			with self.subTest(text=text):
				self.assertEqual(held_sheets._fix("Item", frappe.ValidationError(text))["field"], "stock_uom")
		self.assertIsNone(held_sheets._fix("Item", frappe.ValidationError("Item Groupings: x"))["field"])


class TestShape(_Base):
	def test_the_sheet_caps_its_records(self):
		conv = self.conv()
		with patch.object(held_sheets, "SHEET_MAX_RECORDS", 2):
			self.call("create_doc", _batch(_supplier(), _item("zz-fbs A")), conv)
			res = self.call("create_doc", _item("zz-fbs B"), conv)
		self.assert_refused(res, "InvalidArgumentError")
		self.assertIn("full (2 records)", res["error"]["message"])
		self.assertEqual(self.sheet(conv).record_count, 2)
		self.assertEqual(held_sheets.SHEET_MAX_RECORDS, 250)

	def test_an_update_hold_becomes_an_update_record(self):
		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		conv = self.conv()
		change = {"doctype": "Supplier", "name": existing.name, "changes": {"supplier_details": "x"}}
		self.assert_added(self.call("update_doc", change, conv), 1)
		[record] = self.records(self.sheet(conv))
		self.assertEqual((record["op"], record["name"]), ("update", existing.name))
		self.assertEqual(record["identity"], [f"doc:Supplier:{existing.name}"])
		self.assertEqual(record["snapshot"][:2], ["Supplier", existing.name])
		self.assertFalse(frappe.db.get_value("Supplier", existing.name, "supplier_details"))
		# Another change to the same field replaces it.
		change["changes"] = {"supplier_details": "y"}
		self.assert_added(self.call("update_doc", change, conv), 1)
		self.assertEqual(self.records(self.sheet(conv))[0]["values"], {"supplier_details": "y"})

	def test_update_records_on_one_target_merge_their_changes(self):
		"""Two changes to one record (HSN, then description): both apply, dry-run and
		stale-checked together."""
		from jarvis.tools import update_doc

		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		conv = self.conv()
		first = {"doctype": "Supplier", "name": existing.name, "changes": {"supplier_details": "x"}}
		self.assert_added(self.call("update_doc", first, conv), 1)
		frappe.db.sql(
			"UPDATE `tabSupplier` SET modified=%(m)s WHERE name=%(n)s",
			{"m": frappe.utils.add_to_date(existing.modified, seconds=5), "n": existing.name},
		)
		frappe.db.commit()
		second = {"doctype": "Supplier", "name": existing.name, "changes": {"website": "https://fbs.example"}}
		with patch.object(update_doc, "_update_one", wraps=update_doc._update_one) as tried:
			self.assert_added(self.call("update_doc", second, conv), 1)
		merged = {"supplier_details": "x", "website": "https://fbs.example"}
		[record] = self.records(self.sheet(conv))
		self.assertEqual(record["values"], merged)
		self.assertEqual(tried.call_args.args[2], merged, "re-dry-run with both changes")
		fresh = held_sheets._record(
			{"doctype": "Supplier", "name": existing.name, "op": "update", "values": {}}
		)
		self.assertEqual(record["snapshot"], fresh["snapshot"], "re-snapshot")

	def test_a_mixed_batch_is_split(self):
		conv = self.conv()
		res = self.call("create_doc", _batch(_question(), _supplier()), conv)
		self.assert_added(res, 1)
		self.assertIn("Approval Requests in this batch are on the sheet too", res["data"]["note"])
		row = self.sheet(conv)
		[ar] = frappe.get_all(AR, filters={"conversation": conv}, fields=["name", "sheet"])
		self.assertEqual(ar.sheet, row.name)
		self.assertEqual((row.record_count, row.question_count), (1, 1))


class TestIntegrity(_Base):
	def test_a_tampered_sheet_fails_and_is_never_resealed(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		row = self.sheet(conv)
		card = json.loads(row.card)
		card["records"][0]["title"] = "forged"
		self.set_row(row.name, card=_seal.json_text(card))
		t0 = frappe.utils.now_datetime()
		res = self.call("create_doc", _item("zz-fbs A"), conv)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertIn("integrity check", res["error"]["message"])
		row = _store.get_row(row.name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "tampered"))
		self.assertIsNone(row.open_key)
		self.assertEqual(len(self.sheets()), 1, "never re-sealed or replaced by this call")
		self.assertTrue(
			frappe.get_all(
				"Error Log", filters={"method": "jarvis.file_box.sheet_tampered", "creation": [">=", t0]}
			)
		)

	def test_the_append_is_a_compare_and_set_on_the_envelope(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		row = _store.get_row(self.sheet(conv).name)
		args = {"records": self.records(row)}
		self.set_row(row.name, sealed_call=row.sealed_call + "x")
		self.assertFalse(_store.reseal_sheet(row, args=args, card=json.loads(row.card), record_count=1))
		frappe.db.rollback()
		self.set_row(row.name, sealed_call=row.sealed_call, collecting=0)
		self.assertFalse(_store.reseal_sheet(row, args=args, card=json.loads(row.card), record_count=1))
		frappe.db.rollback()
		self.set_row(row.name, collecting=1)
		self.assertTrue(_store.reseal_sheet(row, args=args, card=json.loads(row.card), record_count=1))
		frappe.db.commit()
		self.assertEqual(_seal.unseal_call(_store.get_row(row.name))["args"], args)
		with self.assertRaises(ValueError):
			_store.reseal_sheet(row, args=args, card={}, status="Executed")

	def test_a_lost_append_race_re_reads_then_appends(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		real, calls = _store.reseal_sheet, []

		def lose_once(*a, **k):
			calls.append(1)
			return False if len(calls) == 1 else real(*a, **k)

		with patch.object(held_sheets, "reseal_sheet", side_effect=lose_once):
			self.assert_added(self.call("create_doc", _item("zz-fbs A"), conv), 2)
		self.assertEqual(len(calls), 2)

	def test_a_deadlock_re_locks_and_re_reads(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		real, calls = held_sheets._dry_run, []

		def deadlock_once(*a, **k):
			calls.append(1)
			if len(calls) == 1:
				raise frappe.QueryDeadlockError("deadlock")
			return real(*a, **k)

		with (
			patch.object(held_sheets, "_dry_run", side_effect=deadlock_once),
			patch.object(held_sheets, "lock_conversation", wraps=held_sheets.lock_conversation) as lock,
		):
			self.assert_added(self.call("create_doc", _item("zz-fbs A"), conv), 2)
		lock.assert_called_once_with(conv)

	def test_every_append_runs_as_the_conversation_owner(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		t0 = frappe.utils.now_datetime()
		self.set_row(self.sheet(conv).name, exec_user=OTHER)
		res = self.call("create_doc", _item("zz-fbs A"), conv)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertTrue(
			frappe.get_all(
				"Error Log",
				filters={"method": "jarvis.file_box.sheet_identity_refused", "creation": [">=", t0]},
			)
		)
		self.set_row(self.sheet(conv).name, exec_user=OWNER)
		other = self.conv()
		self.assert_refused(self.call("create_doc", _supplier(), other, user=OTHER), "FileBoxRefusedError")


class TestRuleA(_Base):
	"""Rule (a) with sheets: collect, link, wait, or refuse."""

	def test_a_stopped_run_adds_nothing(self):
		conv = self.conv()
		request_run_cancel(conv)
		self.addCleanup(clear_run_cancel, conv)
		self.assert_refused(self.call("create_doc", _supplier(), conv), "FileBoxRefusedError")
		self.assert_refused(self.call("create_doc", _question(), conv), "FileBoxRefusedError")
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.exists(AR, {"conversation": conv}))

	def test_a_retry_re_runs_the_gates(self):
		"""A deadlock re-locks: a Stop or a skill conflict that landed meanwhile refuses."""

		def stop(conv):
			request_run_cancel(conv)
			self.addCleanup(clear_run_cancel, conv)

		def conflict(conv):
			filebox_skills._file_routing(conv, filebox_skills.CONFLICT, "t", "q", ["a", filebox_skills.SKIP])
			frappe.db.commit()

		for meanwhile, code in ((stop, "FileBoxRefusedError"), (conflict, "ApprovalPendingError")):
			with self.subTest(meanwhile=meanwhile.__name__):
				conv = self.conv()
				self.call("create_doc", _supplier(), conv)

				def deadlock(*a, conv=conv, meanwhile=meanwhile, **k):
					meanwhile(conv)
					raise frappe.QueryDeadlockError("deadlock")

				with patch.object(held_sheets, "_dry_run", side_effect=deadlock):
					self.assert_refused(self.call("create_doc", _item("zz-fbs A"), conv), code)
				self.assertEqual(self.sheet(conv).record_count, 1)

	def test_a_stop_while_park_relocks_opens_nothing(self):
		"""park commits before it re-locks: a Stop in that window opens no sheet."""
		from jarvis.chat.pending_actions import _park

		conv = self.conv()
		self.addCleanup(clear_run_cancel, conv)
		real = _park.lock_conversation

		def stop_then_lock(c):
			request_run_cancel(c)
			return real(c)

		with patch.object(_park, "lock_conversation", side_effect=stop_then_lock):
			res = self.call("create_doc", _supplier(), conv)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertIn("This run was stopped", res["error"]["message"])
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_sheet_count"))

	def test_an_archived_conversation_refuses_even_once_the_stop_signal_expires(self):
		"""The run-cancel signal a Stop/archive sets expires in 120s; the conversation's
		own Archived status never does - a later write must keep refusing."""
		conv = self.conv()
		frappe.db.set_value(CONV, conv, "status", "Archived", update_modified=False)
		frappe.db.commit()
		self.assert_refused(self.call("create_doc", _supplier(), conv), "FileBoxRefusedError")
		self.assert_refused(self.call("create_doc", _question(), conv), "FileBoxRefusedError")
		self.assertEqual(self.sheets(), [])

	def test_an_archive_while_park_relocks_opens_nothing(self):
		"""The ``_opened`` belt-and-braces check: park commits before it re-locks, so an
		Archive landing in that exact window still opens no sheet."""
		from jarvis.chat.pending_actions import _park

		conv = self.conv()
		real = _park.lock_conversation

		def archive_then_lock(c):
			frappe.db.set_value(CONV, c, "status", "Archived", update_modified=False)
			frappe.db.commit()
			return real(c)

		with patch.object(_park, "lock_conversation", side_effect=archive_then_lock):
			res = self.call("create_doc", _supplier(), conv)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_sheet_count"))

	def test_a_draft_waits_while_the_sheet_holds_records(self):
		dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as dc:
			res = self.call("create_doc", {"doctype": dt, "values": {"remark": "x"}}, conv)
		self.assert_refused(res, "ApprovalPendingError")
		self.assertIn("waiting on its approval sheet", res["error"]["message"])
		dc.assert_not_called()

	def test_a_questions_only_sheet_blocks_the_draft_until_one_exists(self):
		dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		conv = self.conv()
		self.assertTrue(self.call("create_doc", _question(), conv)["ok"])
		row = self.sheet(conv)
		self.assertEqual((row.record_count, row.question_count, row.collecting), (0, 1, 1))
		draft = {"doctype": dt, "values": {"remark": "x"}}
		self.assert_refused(self.call("create_doc", draft, conv), "ApprovalPendingError")
		frappe.db.set_value(CONV, conv, "filebox_result_name", "DRAFT-1", update_modified=False)
		frappe.db.commit()
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as dc:
			self.assertTrue(self.call("create_doc", draft, conv)["ok"])
		dc.assert_called_once()

	def test_a_sealed_sheet_pauses_every_write(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		self.set_row(self.sheet(conv).name, collecting=0)
		with patch("jarvis.chat.wiki.wiki_enabled", return_value=True):
			for tool, args in (
				("create_doc", _item("zz-fbs A")),
				("create_doc", _question()),
				("update_wiki", {"slug": "x", "append_md": "y"}),
			):
				with self.subTest(tool=tool):
					self.assert_refused(self.call(tool, args, conv), "ApprovalPendingError")

	def test_a_third_sheet_refuses_masters_and_questions(self):
		conv = self.conv()
		frappe.db.set_value(CONV, conv, "filebox_sheet_count", 2, update_modified=False)
		frappe.db.commit()
		for args in (_supplier(), _question()):
			res = self.call("create_doc", args, conv)
			self.assert_refused(res, "FileBoxRefusedError")
			self.assertIn("Stop and summarize: this file needs attention", res["error"]["message"])
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.exists(AR, {"conversation": conv}))

	def test_update_wiki_passes_while_collecting(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		self.assertIsNone(held_writes.apply("update_wiki", {"slug": "x"}, conv))

	def test_other_gated_writes_are_refused_as_today(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		res = self.call("submit_doc", {"doctype": "Supplier", "name": "x"}, conv)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertIn("`submit_doc` is not available", res["error"]["message"])

	def test_a_pending_skill_conflict_refuses_masters_and_links_to_a_questions_only_sheet(self):
		conv = self.conv()
		with as_user(OWNER):
			_title, name = filebox_skills._file_routing(
				conv, filebox_skills.CONFLICT, "Which skill?", "Which one?", ["a", "b", filebox_skills.SKIP]
			)
			frappe.db.commit()
			self.assertTrue(filebox_skills._link(conv, name))
		row = self.sheet(conv)
		self.assertEqual((row.record_count, row.question_count), (0, 1))
		self.assertEqual(frappe.db.get_value(AR, name, "sheet"), row.name)
		self.assert_refused(self.call("create_doc", _supplier(), conv), "ApprovalPendingError")
		self.assertTrue(self.call("create_doc", _question(), conv)["ok"])  # questions still file


class TestQuestions(_Base):
	def own(self, conv, **values):
		"""An unlinked question of ``conv`` (as its owner)."""
		with as_user(OWNER):
			doc = frappe.get_doc(
				{"doctype": AR, "title": "zz-fbs own", "question": "q", "conversation": conv, **values}
			).insert()
		frappe.db.commit()
		return doc.name

	def test_a_question_links_to_the_sheet_in_the_running_conversation(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		res = self.call("create_doc", _question(), conv)
		self.assertTrue(res["ok"], res)
		self.assertIn("answers it with the sheet", res["data"]["note"])
		ar = frappe.db.get_value(AR, res["data"]["name"], ["sheet", "conversation"], as_dict=True)
		self.assertEqual((ar.sheet, ar.conversation), (self.sheet(conv).name, conv))
		self.assertEqual(self.sheet(conv).question_count, 1)

	def test_a_question_for_another_conversation_is_refused_before_dispatch(self):
		conv, elsewhere = self.conv(), self.conv()
		mine = self.own(conv)
		with patch("jarvis.api.dispatch_confirmed") as dc:
			for tool, args in (
				("create_doc", _question(conversation=elsewhere)),
				("create_doc", _batch(_supplier(), _question(conversation=elsewhere))),
				("update_doc", {"doctype": AR, "name": mine, "changes": {"conversation": elsewhere}}),
			):
				with self.subTest(tool=tool):
					res = self.call(tool, args, conv)
					self.assert_refused(res, "FileBoxRefusedError")
					self.assertIn("belongs to another conversation", res["error"]["message"])
		dc.assert_not_called()
		self.assertEqual(frappe.db.get_value(AR, mine, "conversation"), conv)
		self.assertFalse(frappe.db.exists(AR, {"conversation": elsewhere}))
		self.assertEqual(self.sheets(), [], "a split batch refused whole")

	def test_a_linked_question_is_never_answered_in_chat(self):
		"""The link makes it a File Box question, and a chat reply never answers one."""
		from jarvis.chat import chat_asks

		conv = self.conv()
		name = self.call("create_doc", _question(source="Chat"), conv)["data"]["name"]
		self.assertEqual(
			frappe.db.get_value(AR, name, ["sheet", "source"]), (self.sheet(conv).name, "File Box")
		)
		frappe.db.sql(f"UPDATE `tab{AR}` SET source='Chat' WHERE name=%(n)s", {"n": name})
		frappe.db.commit()
		with as_user(OWNER):
			chat_asks.resolve_on_user_message(conv)
		self.assertEqual(frappe.db.get_value(AR, name, "status"), "Pending")

	def test_nothing_linkable_opens_no_sheet(self):
		conv = self.conv()
		wiki = self.own(conv)
		frappe.db.sql(f"UPDATE `tab{AR}` SET source='File Box Wiki' WHERE name=%(n)s", {"n": wiki})
		done = self.own(conv)
		frappe.db.set_value(AR, done, {"status": "Dismissed", "decision": "x"}, update_modified=False)
		frappe.db.commit()
		with as_user(OWNER), patch.object(held_sheets, "_open", wraps=held_sheets._open) as opened:
			self.assertIsNone(held_sheets.link_to_sheet(conv, [wiki, done, "zz-fbs-missing"]))
		opened.assert_not_called()  # picked first: no park at all
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_sheet_count"))

	def test_a_crash_while_linking_leaves_no_empty_sheet(self):
		"""The stamp commits with the sheet it opens (park's transaction)."""
		conv = self.conv()
		name = self.own(conv)
		with (
			as_user(OWNER),
			patch.object(held_sheets, "_stamp", side_effect=RuntimeError("crash")),
			self.assertRaises(RuntimeError),
		):
			held_sheets.link_to_sheet(conv, [name])
		self.assertEqual(self.sheets(), [])
		self.assertFalse(frappe.db.get_value(AR, name, "sheet"))
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_sheet_count"))

	def test_a_stop_racing_the_link_closes_the_new_questions(self):
		"""Filed just before a Stop: never left on the board to resume the stopped run."""
		from jarvis.chat.pending_actions import _park

		real = _park.lock_conversation

		def stop_then_lock(c):
			request_run_cancel(c)
			return real(c)

		def while_park_relocks(conv, name):
			with patch.object(_park, "lock_conversation", side_effect=stop_then_lock):
				return held_sheets.link_to_sheet(conv, [name])

		def after_a_stop(conv, name):
			request_run_cancel(conv)
			return held_sheets.link_to_sheet(conv, [name])

		def with_no_sheet_left(conv, name):
			frappe.db.set_value(CONV, conv, "filebox_sheet_count", 2, update_modified=False)
			frappe.db.commit()
			self.assertIsNone(held_sheets.link_to_sheet(conv, [name]))
			self.assertEqual(frappe.db.get_value(AR, name, "status"), "Pending", "not stopped: stays")
			return after_a_stop(conv, name)

		for race in (while_park_relocks, after_a_stop, with_no_sheet_left):
			with self.subTest(race=race.__name__):
				conv = self.conv()
				self.addCleanup(clear_run_cancel, conv)
				name = self.own(conv)
				with as_user(OWNER):
					self.assertIsNone(race(conv, name))
				ar = frappe.db.get_value(AR, name, ["status", "sheet", "decision"], as_dict=True)
				self.assertEqual((ar.status, ar.sheet or None), ("Dismissed", None))
				self.assertEqual(ar.decision, held_sheets._STOPPED_NOTE)
				self.assertFalse([s for s in self.sheets() if s.conversation == conv])

	def test_a_parallel_open_while_linking_links_to_the_winner(self):
		conv = self.conv()
		name = self.own(conv)
		site, errors, real = frappe.local.site, [], held_sheets._open

		def open_elsewhere():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.set_user(OWNER)
				item = {
					"doctype": "Supplier",
					"values": {"supplier_name": "zz-fbs Other"},
					"name": "",
					"op": "create",
				}
				real(conv, OWNER, [held_sheets._record(item)])
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				frappe.destroy()

		def racing(*a, **k):
			if not getattr(racing, "done", False):
				racing.done = True
				frappe.db.commit()
				t = threading.Thread(target=open_elsewhere)
				t.start()
				t.join(timeout=30)
			return real(*a, **k)

		with as_user(OWNER), patch.object(held_sheets, "_open", side_effect=racing):
			linked = held_sheets.link_to_sheet(conv, [name])
		self.assertEqual(errors, [])
		[row] = self.sheets()
		self.assertEqual((linked, frappe.db.get_value(AR, name, "sheet")), (row.name, row.name))
		self.assertEqual((row.record_count, row.question_count), (1, 1))
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)

	def test_an_all_question_batch_links_every_question(self):
		conv = self.conv()
		res = self.call("create_doc", _batch(_question(), _question("zz-fbs second")), conv)
		self.assertTrue(res["ok"], res)
		row = self.sheet(conv)
		self.assertEqual(row.question_count, 2)
		self.assertEqual({a.sheet for a in frappe.get_all(AR, {"conversation": conv}, ["sheet"])}, {row.name})

	def test_a_wiki_proposal_is_never_linked(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		doc = frappe.get_doc(
			{
				"doctype": AR,
				"title": "zz-fbs wiki",
				"question": "q",
				"conversation": conv,
				"source": "File Box Wiki",
				"status": "Pending",
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		with as_user(OWNER):
			held_sheets.link_to_sheet(conv, [doc.name])
		self.assertFalse(frappe.db.get_value(AR, doc.name, "sheet"))
		self.assertEqual(self.sheet(conv).question_count, 0)

	def test_a_run_may_only_update_its_own_questions(self):
		conv, elsewhere = self.conv(), self.conv()
		with as_user(OWNER):
			theirs = frappe.get_doc(
				{"doctype": AR, "title": "zz-fbs theirs", "question": "q", "conversation": elsewhere}
			).insert()
		frappe.db.commit()
		res = self.call(
			"update_doc", {"doctype": AR, "name": theirs.name, "changes": {"question": "x"}}, conv
		)
		self.assert_refused(res, "FileBoxRefusedError")
		self.assertNotEqual(frappe.db.get_value(AR, theirs.name, "question"), "x")


class TestFreeze(_Base):
	"""A linked question is server-owned: frozen, never decided on its own, off the board."""

	def linked(self):
		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		res = self.call("create_doc", _question(), conv)
		return conv, res["data"]["name"]

	def test_a_linked_question_is_frozen_with_no_exemption(self):
		_conv, name = self.linked()
		for field, value in (
			("question", "forged"),
			("title", "forged"),
			("options", '["x"]'),
			("context_md", "forged"),
			("conversation", None),
			("sheet", ""),
			("status", "Approved"),
			("decision", "yes"),
		):
			with self.subTest(field=field), as_user("Administrator"):
				doc = frappe.get_doc(AR, name)
				doc.set(field, value)
				if field == "status":
					doc.decision = "yes"
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
				frappe.db.rollback()
		with as_user(OWNER):
			free = frappe.get_doc(
				{"doctype": AR, "title": "zz-fbs free", "question": "q", "conversation": _conv}
			).insert()
			free.sheet = "forged"
			with self.assertRaises(frappe.PermissionError):
				free.save()
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{"doctype": AR, "title": "zz-fbs new", "question": "q", "sheet": "forged"}
				).insert()

	def test_the_run_cannot_unfreeze_it_through_update_doc(self):
		conv, name = self.linked()
		res = self.call("update_doc", {"doctype": AR, "name": name, "changes": {"question": "forged"}}, conv)
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.db.get_value(AR, name, "question"), "Which vendor?")

	def test_decide_dismiss_and_restore_refuse_a_linked_question(self):
		_conv, name = self.linked()
		with as_user(OWNER):
			for fn, args in (
				(approvals_api.decide, (name, "yes")),
				(approvals_api.dismiss_approval, (name,)),
			):
				with self.subTest(fn=fn.__name__), self.assertRaises(frappe.ValidationError) as ctx:
					fn(*args)
				self.assertIn("approval sheet", str(ctx.exception))
		with as_user(OWNER):
			self.assertEqual(approvals_api.get_approval(name)["can_act"], 0)
		frappe.db.set_value(AR, name, {"status": "Dismissed", "decision": "x"}, update_modified=False)
		frappe.db.commit()
		with as_user(OWNER), self.assertRaises(frappe.ValidationError):
			approvals_api.restore_approval(name)
		self.assertEqual(frappe.db.get_value(AR, name, "status"), "Dismissed")

	def test_the_decide_writes_skip_a_linked_question_even_past_the_check(self):
		"""The conditional UPDATEs carry IFNULL(sheet,'')='' too (belt and braces)."""
		_conv, name = self.linked()
		with as_user(OWNER), patch.object(approvals_api, "_refuse_if_linked"):
			for fn, args in (
				(approvals_api.decide, (name, "yes")),
				(approvals_api.dismiss_approval, (name,)),
			):
				with self.subTest(fn=fn.__name__), self.assertRaises(frappe.ValidationError):
					fn(*args)
		self.assertEqual(frappe.db.get_value(AR, name, "status"), "Pending")
		frappe.db.set_value(AR, name, {"status": "Dismissed", "decision": "x"}, update_modified=False)
		frappe.db.commit()
		with (
			as_user(OWNER),
			patch.object(approvals_api, "_refuse_if_linked"),
			self.assertRaises(frappe.ValidationError),
		):
			approvals_api.restore_approval(name)
		self.assertEqual(frappe.db.get_value(AR, name, "status"), "Dismissed")

	def test_before_its_migrate_the_sheet_clauses_are_left_out(self):
		"""Code served ahead of the migrate never names the missing ``sheet`` column."""
		from jarvis.chat import chat_asks
		from jarvis.jarvis.doctype.jarvis_approval_request import jarvis_approval_request as ar

		conv, name = self.linked()
		frappe.db.sql(f"UPDATE `tab{AR}` SET source='Chat' WHERE name=%(n)s", {"n": name})
		frappe.db.commit()
		with patch.object(ar, "sheet_ready", return_value=False), as_user(OWNER):
			self.assertIn(name, [r["name"] for r in approvals_api.list_approvals()])
			self.assertIn(name, [r["name"] for r in approvals_api.list_approvals_page()["rows"]])
			chat_asks.resolve_on_user_message(conv)
		self.assertEqual(frappe.db.get_value(AR, name, "status"), "Answered")
		self.assertTrue(ar.sheet_ready())

	def test_lists_leave_linked_questions_out(self):
		conv, name = self.linked()
		with as_user(OWNER):
			unlinked = frappe.get_doc(
				{"doctype": AR, "title": "zz-fbs board", "question": "q", "conversation": conv}
			).insert()
			frappe.db.commit()
			names = [r["name"] for r in approvals_api.list_approvals()]
			page = approvals_api.list_approvals_page()
		self.assertIn(unlinked.name, names)
		self.assertNotIn(name, names)
		self.assertEqual([r["name"] for r in page["rows"]], [unlinked.name])
		self.assertEqual(page["total"], 1)

	def test_the_awaiting_reply_lane_leaves_a_sheet_conversation_out(self):
		conv, _name = self.linked()
		for seq, role, content in (
			(1, "user", "process"),
			(2, "tool", "{}"),
			(3, "assistant", "Which vendor is this?"),
		):
			doc = frappe.get_doc(
				{"doctype": MSG, "conversation": conv, "seq": seq, "role": role, "content": content}
			)
			doc.flags.jarvis_server_write = True
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(approvals_api._awaiting_reply(OWNER), [])
		frappe.db.sql(f"UPDATE `tab{AR}` SET sheet=NULL WHERE conversation=%(c)s", {"c": conv})
		frappe.db.commit()
		self.assertEqual([r["conversation"] for r in approvals_api._awaiting_reply(OWNER)], [conv])


class TestPreMigrateGuards(_Base):
	"""S1 fixup #5: code served ahead of the S1a migrate never names ``ar.sheet`` /
	``sp.collecting`` / ``sp.sheet_counts`` / ``pa.collecting`` / ``pa.record_count``."""

	def test_the_board_query_drops_the_sheet_columns_before_the_migrate(self):
		from jarvis.chat import filebox
		from jarvis.jarvis.doctype.jarvis_approval_request import jarvis_approval_request as ar

		with patch.object(ar, "sheet_ready", return_value=True):
			ready_sql = filebox._pending_waits_sql()
		with patch.object(ar, "sheet_ready", return_value=False):
			unready_sql = filebox._pending_waits_sql()
		for token in ("ar.sheet", "sp.collecting", "sp.sheet_counts"):
			self.assertIn(token, ready_sql)
			self.assertNotIn(token, unready_sql)

	def test_the_lane_drops_the_sheet_query_before_the_migrate(self):
		from jarvis.jarvis.doctype.jarvis_approval_request import jarvis_approval_request as ar

		conv = self.conv()
		self.call("create_doc", _supplier(), conv)
		with as_user(OWNER):
			[row] = approvals_api.list_pending_actions_lane()["rows"]
			self.assertEqual(row["kind"], "file_box_sheet")
			with patch.object(ar, "sheet_ready", return_value=False):
				res = approvals_api.list_pending_actions_lane()
		self.assertEqual(res["rows"], [])
