"""The read path: one authorization, one stored part, a check of the rows a reply returns (821-A, plan section 6)."""

import json
from copy import deepcopy
from time import monotonic
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import access, api, banks, read_log, store
from jarvis.payment_calendar.compatibility import has_native_field_masks
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.projection import project_snapshot
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import (
	CalendarFixtures,
	clear_error_throttle,
	require_calendar_test_site,
)

SIDES = ("Payable", "Receivable")
VOUCHER = {"Payable": "Purchase Invoice", "Receivable": "Sales Invoice"}
PARTY = {"Payable": "Supplier", "Receivable": "Customer"}
PRIVATE = ("_permission", "_top_proofs", "_permission_columns", "permission_columns", "excluded_rows")
DENIED = set()
CALLS = []
RAISE = []
HOOK = "jarvis.tests.test_payment_calendar_read_path.site_hook"
# The v15 backport CI runs the tests that only hold on Frappe 15; they are skipped on v16.
ON_V15 = frappe.__version__.split(".")[0] == "15"


def site_hook(doc, ptype=None, user=None, debug=False):
	"""A stateful has_permission hook as a site could register it."""
	CALLS.append((doc.doctype, doc.name))
	if RAISE:
		raise RAISE[0]
	return doc.name not in DENIED


class ReadPathCase(TestCase):
	serial = 0

	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_read_path")
		DENIED.clear()
		CALLS.clear()
		RAISE.clear()
		self.fx = CalendarFixtures()
		# Per side: one invoice with terms on +10 and +20 days, one without terms due on +20.
		self.base = {side: self.fx.invoice((40, 60), side=side) for side in SIDES}
		self.extra = {side: self.fx.invoice((55,), side=side, schedules=False) for side in SIDES}
		self.d10, self.d20 = add_days(self.fx.today, 10), add_days(self.fx.today, 20)
		update_safety_floor(self.fx.company, "20", 0)
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.scope = normalize_scope(self.fx.company, [])

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_read_path")
		for doctype in ("Purchase Invoice", "Sales Invoice", "Supplier", "Customer", "Account"):
			frappe.clear_cache(doctype=doctype)

	def build(self, scope=None):
		scope = scope or self.scope
		sources = {
			side: read_obligations(
				self.fx.company, side, self.fx.today, scope["window_start"], scope["window_end"]
			)
			for side in SIDES
		}
		return {
			"schema": 2,
			"sources": sources,
			"projection": project_snapshot(
				sources["Payable"],
				sources["Receivable"],
				None,
				"20",
				self.fx.today,
				scope["window_start"],
				scope["window_end"],
			),
			"horizon": None,
			"bank": None,
			"bank_code": None,
			"floor": get_settings(self.fx.company),
			"currency": "INR",
			"generated_at": "2026-10-24T10:00:00+00:00",
			"unavailable": {},
		}

	def publish(self, result=None, scope=None):
		"""Store a complete snapshot; the fingerprint is taken now, so active hooks count."""
		scope = scope or self.scope
		# The hook decides what the report returns while the snapshot is built: nothing is denied yet.
		DENIED.clear()
		proof = access.authorize_scope(scope)
		ReadPathCase.serial += 1
		sid, _ = store.admit(self.user, f"{ReadPathCase.serial:064x}", scope, proof["fingerprint"])
		store.mark_running(self.user, sid)
		self.assertTrue(
			store.publish_snapshot(self.user, sid, store.split_result(result or self.build(scope), scope))
		)
		return sid

	def hook(self, *doctypes):
		real = frappe.get_hooks

		def get_hooks(*args, **kwargs):
			value = real(*args, **kwargs)
			if args and args[0] == "has_permission":
				return {**value, **{doctype: [HOOK] for doctype in doctypes}}
			return value

		patcher = patch("frappe.get_hooks", side_effect=get_hooks)
		patcher.start()
		self.addCleanup(patcher.stop)

	def reads(self, side):
		"""The two detail reads of one side, by name."""
		return {
			"day": lambda sid, **kw: api.get_day_detail(sid, self.d10, side, **kw),
			"bucket": lambda sid, **kw: api.get_bucket_detail(sid, "window", side, **kw),
		}

	def all_reads(self):
		"""Every read of a snapshot, by name."""
		return {
			"status": api.get_snapshot_status,
			"day": lambda sid: api.get_day_detail(sid, self.d10, "Payable"),
			"bucket": lambda sid: api.get_bucket_detail(sid, "window", "Payable"),
			"upcoming": lambda sid: api.get_upcoming(sid, self.fx.today),
		}

	def print_timings(self, sid):
		"""One `PCAL_TIMING` line per read (day, bucket, upcoming, status); read from the CI log, never asserted."""
		reads = {
			"get_day_detail": lambda: api.get_day_detail(sid, self.d10, "Payable"),
			"get_bucket_detail": lambda: api.get_bucket_detail(sid, "window", "Payable"),
			"get_upcoming": lambda: api.get_upcoming(sid, self.fx.today),
			"get_snapshot_status": lambda: api.get_snapshot_status(sid),
		}
		for endpoint, read in reads.items():
			started = monotonic()
			reply = read()
			elapsed = int((monotonic() - started) * 1000)
			print(f"PCAL_TIMING endpoint={endpoint} ms={elapsed}", flush=True)
			self.assertTrue(reply["ok"], reply)

	def exists(self, sid):
		keys = [
			store._key(self.user, sid, "status"),
			*(store._part_key(self.user, sid, p) for p in store.PARTS),
			*(store._key(self.user, sid, name) for name in store.HASHES),
		]
		return [bool(frappe.cache.execute_command("EXISTS", key)) for key in keys]

	def assertDiscarded(self, sid):
		self.assertEqual(self.exists(sid), [False] * (4 + len(store.HASHES)))
		self.assertTrue(store.was_discarded(self.user, sid))

	def assertKept(self, sid):
		self.assertEqual(self.exists(sid)[:2], [True, True])
		self.assertFalse(store.was_discarded(self.user, sid))

	def assertRefused(self, response, code="scope_changed"):
		self.assertFalse(response["ok"], response)
		self.assertEqual(response["code"], code)
		self.assertNotIn("data", response)

	def party(self, side, suffix):
		doctype = PARTY[side]
		return self.fx.insert(
			doctype,
			suffix,
			**{f"{doctype.lower()}_name": suffix},
			disabled=0,
			**{f"{doctype.lower()}_type": "Company"},
		)

	def spy_cache(self):
		"""Every Redis command the read path sends, as (command, key text)."""
		real = frappe.cache.execute_command
		commands = []

		def execute(command, *args, **kwargs):
			commands.append(
				(command, [a.decode(errors="replace") if isinstance(a, bytes) else str(a) for a in args])
			)
			return real(command, *args, **kwargs)

		patcher = patch.object(frappe.cache, "execute_command", side_effect=execute)
		patcher.start()
		self.addCleanup(patcher.stop)
		return commands


class TestScopeCheckOnce(ReadPathCase):
	def test_reads_never_run_the_full_visibility_check(self):
		sid = self.publish()
		with patch.object(access, "validate_result_visibility", side_effect=AssertionError("full check")):
			for name, read in self.all_reads().items():
				with self.subTest(read=name):
					self.assertTrue(read(sid)["ok"])

	def test_the_scope_check_gets_no_result_one_refresh_and_the_available_sides(self):
		sid = self.publish()
		with (
			patch.object(api, "authorize_scope", wraps=api.authorize_scope) as authorize,
			patch.object(api, "refresh_actor_access", wraps=api.refresh_actor_access) as refresh,
		):
			for name, read in self.all_reads().items():
				authorize.reset_mock()
				refresh.reset_mock()
				with self.subTest(read=name):
					read(sid)
					refresh.assert_called_once()
					authorize.assert_called_once()
					kwargs = authorize.call_args.kwargs
					self.assertNotIn("result", kwargs)
					self.assertIs(kwargs["actor_refreshed"], True)
					self.assertEqual(set(kwargs["sides"]), set(SIDES))
					self.assertIs(kwargs["check_accounts"], name == "status")

	def test_upcoming_and_running_polls_read_no_side_part(self):
		sid = self.publish()
		running, _ = store.admit(
			self.user, "e" * 64, self.scope, access.authorize_scope(self.scope)["fingerprint"]
		)
		store.mark_running(self.user, running)
		commands = self.spy_cache()
		self.assertTrue(api.get_upcoming(sid, self.fx.today)["ok"])
		self.assertTrue(api.get_snapshot_status(running)["ok"])
		keys = " ".join(key for command, args in commands if command == "GET" for key in args)
		self.assertNotIn("part-payable", keys)
		self.assertNotIn("part-receivable", keys)
		self.assertNotIn("part-status:" + running, keys)

	def test_a_detail_read_loads_one_side_part_and_no_snapshot_read_gets_a_result_key(self):
		sid = self.publish()
		commands = self.spy_cache()
		for name, read in self.all_reads().items():
			with self.subTest(read=name):
				read(sid)
		keys = " ".join(key for command, args in commands if command == "GET" for key in args)
		self.assertIn("part-payable:" + sid, keys)
		self.assertNotIn("part-receivable", keys)
		self.assertNotIn("result:", keys)

	def test_no_reply_carries_a_private_key(self):
		sid = self.publish()
		replies = [read(sid) for read in self.all_reads().values()]
		replies.append(api.get_day_detail(sid, self.d10, "Receivable", parties=False))
		for reply in replies:
			self.assertTrue(reply["ok"], reply)
			text = json.dumps(reply)
			for key in PRIVATE:
				self.assertNotIn(key, text)


class TestBoundedChecks(ReadPathCase):
	def pad(self, result):
		"""10,000 rows per side that no reply returns: another day, fake vouchers and parties."""
		for source in result["sources"].values():
			template = source["rows"][0]
			source["rows"] += [
				{
					**template,
					"id": f"fake-{i}",
					"voucher_no": f"FAKE-{i}",
					"party": f"FAKE-PARTY-{i % 50}",
					"party_name": "Fake",
					"due_date": add_days(self.scope["window_end"], 5),
				}
				for i in range(10000)
			]
		return result

	def test_only_the_page_is_looked_up_however_large_the_stored_part_is(self):
		sid = self.publish(self.pad(self.build()))
		for side in SIDES:
			for kind, read in self.reads(side).items():
				with (
					self.subTest(side=side, read=kind),
					patch.object(access, "visible_names", wraps=access.visible_names) as names,
				):
					self.assertTrue(read(sid)["ok"])
					asked = [(call.args[0], set(call.args[1])) for call in names.call_args_list]
					self.assertTrue(asked)
					for doctype, found in asked:
						self.assertLessEqual(len(found), 25, doctype)
						self.assertFalse([n for n in found if n.startswith("FAKE")], doctype)
		self.print_timings(sid)

	def test_an_empty_page_makes_no_lookup_at_all(self):
		sid = self.publish()
		for side in SIDES:
			with self.subTest(side=side), patch.object(access, "visible_names") as names:
				reply = api.get_day_detail(sid, add_days(self.fx.today, 5), side)
				self.assertTrue(reply["ok"], reply)
				self.assertEqual(reply["data"]["rows"], [])
				names.assert_not_called()

	def test_without_the_party_summary_only_the_page_row_parties_are_looked_up(self):
		sid = self.publish()
		with (
			patch.object(access, "visible_names", wraps=access.visible_names) as names,
			patch("jarvis.payment_calendar.detail._party_rows") as summary,
		):
			reply = api.get_day_detail(sid, self.d10, "Payable", parties=False)
		self.assertTrue(reply["ok"], reply)
		summary.assert_not_called()
		self.assertEqual(reply["data"]["parties"], [])
		self.assertIsNone(reply["data"]["party_total"])
		self.assertEqual(reply["data"]["rows"][0]["remaining"], "40.00")
		self.assertIn("Supplier", [call.args[0] for call in names.call_args_list])

	def test_the_parties_flag_accepts_only_booleans_and_their_text_forms(self):
		sid = self.publish()
		for value in (False, "0", "false", "False", 0):
			with self.subTest(value=value):
				self.assertEqual(
					api.get_day_detail(sid, self.d10, "Payable", parties=value)["data"]["parties"], []
				)
		for value in (True, "1", "true", 1):
			with self.subTest(value=value):
				self.assertTrue(
					api.get_day_detail(sid, self.d10, "Payable", parties=value)["data"]["parties"]
				)
		for value in ("maybe", 2, None, [], {}):
			with self.subTest(value=value):
				response = api.get_bucket_detail(sid, "window", "Payable", parties=value)
				self.assertEqual((response["ok"], response["code"]), (False, "invalid_request"))


class TestHiddenReturnedRows(ReadPathCase):
	def test_a_hook_hiding_a_page_invoice_refuses_discards_and_tombstones(self):
		self.hook("Purchase Invoice", "Sales Invoice")
		self.print_timings(self.publish())
		bound = {"day": 1, "bucket": 2}
		for side in SIDES:
			for kind, read in self.reads(side).items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					DENIED.clear()
					DENIED.add(self.base[side])
					CALLS.clear()
					self.assertRefused(read(sid))
					self.assertDiscarded(sid)
					self.assertLessEqual(len([c for c in CALLS if c[0] == VOUCHER[side]]), bound[kind])

	def test_a_hidden_invoice_of_another_day_does_not_refuse(self):
		self.hook("Purchase Invoice", "Sales Invoice")
		for side in SIDES:
			with self.subTest(side=side):
				sid = self.publish()
				DENIED.clear()
				DENIED.add(self.extra[side])
				self.assertTrue(api.get_day_detail(sid, self.d10, side)["ok"])
				self.assertRefused(api.get_day_detail(sid, self.d20, side))

	def test_a_hidden_row_on_page_two_leaves_page_one_readable(self):
		self.hook("Purchase Invoice", "Sales Invoice")
		pages = {
			"day": lambda sid, side, page: api.get_day_detail(sid, self.d20, side, page, 1),
			"bucket": lambda sid, side, page: api.get_bucket_detail(sid, "window", side, page, 1),
		}
		for side in SIDES:
			for kind, read in pages.items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					DENIED.clear()
					DENIED.add(self.extra[side])
					self.assertTrue(read(sid, side, 1)["ok"])
					self.assertRefused(read(sid, side, 2))
					self.assertDiscarded(sid)

	def test_a_hidden_party_of_the_party_slice_refuses_and_a_hidden_party_of_a_page_row_too(self):
		self.hook("Supplier", "Customer")
		for side in SIDES:
			other = self.party(side, "Other" + side)
			self.fx.invoice((30,), side=side, schedules=False, supplier=other)
			reads = {
				"day": lambda sid, side=side, **kw: api.get_day_detail(sid, self.d20, side, 1, 1, **kw),
				"bucket": lambda sid, side=side, **kw: api.get_bucket_detail(sid, "window", side, 1, 1, **kw),
			}
			first = self.fx.supplier if side == "Payable" else self.fx.customer
			for kind, read in reads.items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					# Page 1 lists the first party's row; party page 2 lists only the other party.
					DENIED.clear()
					DENIED.add(other)
					self.assertTrue(read(sid, party_page=1)["ok"])
					self.assertRefused(read(sid, party_page=2))
					self.assertDiscarded(sid)
					sid = self.publish()
					DENIED.clear()
					DENIED.add(first)
					self.assertRefused(read(sid, party_page=2))
					self.assertDiscarded(sid)

	def test_a_cancelled_invoice_on_the_page_refuses(self):
		for side in SIDES:
			for kind, read in self.reads(side).items():
				with self.subTest(side=side, read=kind):
					sid = self.publish()
					frappe.db.set_value(VOUCHER[side], self.base[side], "docstatus", 2)
					self.assertRefused(read(sid))
					self.assertDiscarded(sid)
					frappe.db.set_value(VOUCHER[side], self.base[side], "docstatus", 1)

	def test_a_damaged_proof_refuses(self):
		result = self.build()
		for source in result["sources"].values():
			for row in source["rows"]:
				row["_permission"] = None
		sid = self.publish(result)
		self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
		self.assertDiscarded(sid)

	def test_a_page_with_a_hidden_row_is_never_partly_delivered(self):
		self.hook("Purchase Invoice")
		sid = self.publish()
		DENIED.add(self.extra["Payable"])
		response = api.get_bucket_detail(sid, "window", "Payable", 1, 25)
		self.assertRefused(response)
		self.assertNotIn(self.base["Payable"], json.dumps(response))

	def test_masked_party_names_refuse_reads(self):
		if not has_native_field_masks():
			self.skipTest("This framework has no native field-mask permissions")
		sid = self.publish()
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doctype_or_field": "DocField",
				"doc_type": "Supplier",
				"field_name": "supplier_name",
				"property": "mask",
				"property_type": "Check",
				"value": "1",
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(doctype="Supplier")
		frappe.set_user(self.user)
		self.assertFalse(api.get_day_detail(sid, self.d10, "Payable")["ok"])


class TestTopInvoiceProofs(ReadPathCase):
	def tops(self, sid):
		return store.load_part(self.user, sid, "status")["_top_proofs"]

	def journal_entry(self, result, *parties):
		"""A voucher with one stored row per party, the day's top item on the 10th."""
		voucher = self.fx.insert(
			"Journal Entry",
			"JE",
			company=self.fx.company,
			posting_date=self.fx.today,
			docstatus=1,
			bill_no="JE",
			bill_date=self.fx.today,
		)
		rows = result["sources"]["Payable"]["rows"]
		for party in parties:
			row = deepcopy(rows[0])
			row.update(id=f"je-{party}", voucher_type="Journal Entry", voucher_no=voucher, party=party)
			row.update(party_type="Supplier", remaining="5000.00", original="5000.00")
			row["_permission"].update(
				voucher_type="Journal Entry", voucher_no=voucher, party_type="Supplier", party=party
			)
			rows.append(row)
		day = result["projection"]["days"][self.d10]
		day["payable_top_invoice"] = {
			**day["payable_top_invoice"],
			"voucher_type": "Journal Entry",
			"voucher_no": voucher,
		}
		return voucher

	def test_a_hidden_top_invoice_refuses_the_status_and_a_hidden_other_invoice_does_not(self):
		self.hook("Purchase Invoice", "Sales Invoice")
		for side in SIDES:
			with self.subTest(side=side):
				sid = self.publish()
				tops = {row["voucher_no"] for row in self.tops(sid)[side]}
				self.assertIn(self.base[side], tops)
				self.assertNotIn(self.extra[side], tops)
				DENIED.clear()
				DENIED.add(self.extra[side])
				self.assertTrue(api.get_snapshot_status(sid)["ok"])
				DENIED.clear()
				DENIED.add(self.base[side])
				self.assertRefused(api.get_snapshot_status(sid))
				self.assertDiscarded(sid)

	def test_every_party_of_a_multi_party_top_journal_entry_is_checked(self):
		self.hook("Supplier")
		second = self.party("Payable", "SecondParty")
		result = self.build()
		self.journal_entry(result, self.fx.supplier, second)
		sid = self.publish(result)
		self.assertTrue(api.get_snapshot_status(sid)["ok"])
		DENIED.add(second)
		self.assertRefused(api.get_snapshot_status(sid))
		self.assertDiscarded(sid)

	def test_the_status_refuses_a_proof_the_report_filter_drops(self):
		sid = self.publish()
		with patch("frappe.desk.query_report.get_filtered_data", return_value=[]):
			self.assertRefused(api.get_snapshot_status(sid))
		self.assertDiscarded(sid)


class TestFingerprintChanges(ReadPathCase):
	"""Each change is made below the document hooks: a hook would purge the snapshot, a direct change cannot."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.get_doc("User", self.user).add_roles("Auditor")
		self.share = frappe.get_doc(
			{
				"doctype": "DocShare",
				"user": self.user,
				"share_doctype": "Purchase Invoice",
				"share_name": self.base["Payable"],
				"read": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.user)

	def tearDown(self):
		# The script map is cached outside the transaction that the test rolls back.
		frappe.cache.delete_value("server_script_map")
		super().tearDown()

	def mutations(self):
		def floor():
			update_safety_floor(self.fx.company, "30", 1)

		def role_removed():
			frappe.db.delete("Has Role", {"parent": self.user, "role": "Auditor"})

		def user_permission():
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"name": frappe.generate_hash(length=10),
					"user": self.user,
					"allow": "Company",
					"for_value": self.fx.company,
					"apply_to_all_doctypes": 1,
				}
			).db_insert()

		def share_deleted():
			frappe.db.delete("DocShare", {"name": self.share.name})

		def domain_toggled():
			# Saving Domain Settings also edits roles; the hashed rows alone must refuse.
			frappe.get_doc(
				{
					"doctype": "Has Domain",
					"name": frappe.generate_hash(length=10),
					"parent": "Domain Settings",
					"parenttype": "Domain Settings",
					"parentfield": "active_domains",
					"domain": "Retail",
				}
			).db_insert()

		return {
			"floor changed": floor,
			"role removed": role_removed,
			"user permission added": user_permission,
			"doc share deleted": share_deleted,
			"domain toggled": domain_toggled,
		}

	def query_script(self):
		name = self.fx.prefix + "QueryScript"
		frappe.get_doc(
			{
				"doctype": "Server Script",
				"name": name,
				"script_type": "Permission Query",
				"reference_doctype": "Purchase Invoice",
				"script": "conditions = ''",
			}
		).insert(ignore_permissions=True)
		return name

	def refused_everywhere(self, sids):
		for name, read in self.all_reads().items():
			with self.subTest(read=name):
				self.assertRefused(read(sids[name]))
				self.assertDiscarded(sids[name])

	def published(self):
		return {name: self.publish() for name in self.all_reads()}

	def test_every_policy_change_refuses_all_four_reads_and_discards(self):
		for label, mutate in self.mutations().items():
			with self.subTest(change=label):
				sids = self.published()
				frappe.set_user("Administrator")
				mutate()
				frappe.set_user(self.user)
				self.refused_everywhere(sids)

	def test_a_new_permission_query_script_refuses_all_four_reads_and_discards(self):
		sids = self.published()
		frappe.set_user("Administrator")
		self.query_script()
		frappe.set_user(self.user)
		self.refused_everywhere(sids)

	def test_an_edited_permission_query_script_refuses_all_four_reads_and_discards(self):
		# Built first: the report itself cannot run a script that this test site cannot load.
		result = self.build()
		frappe.set_user("Administrator")
		name = self.query_script()
		frappe.set_user(self.user)
		sids = {name: self.publish(result) for name in self.all_reads()}
		frappe.db.set_value("Server Script", name, "script", "conditions = '1=1'", update_modified=False)
		self.refused_everywhere(sids)

	def test_toggling_server_script_execution_refuses_all_four_reads_and_discards(self):
		sids = self.published()
		with patch("jarvis.payment_calendar.access.is_safe_exec_enabled", return_value=True):
			self.refused_everywhere(sids)


class TestAccountGate(ReadPathCase):
	def setUp(self):
		super().setUp()
		self.scope = normalize_scope(self.fx.company, [self.fx.bank])

	def gate(self):
		return patch(
			"jarvis.payment_calendar.scope.validate_bank_accounts", wraps=banks.validate_bank_accounts
		)

	def test_one_account_gate_on_a_complete_status_and_none_on_the_other_reads(self):
		sid = self.publish()
		running, _ = store.admit(
			self.user, "d" * 64, self.scope, access.authorize_scope(self.scope)["fingerprint"]
		)
		store.mark_running(self.user, running)
		expected = {"status": 1, "day": 0, "bucket": 0, "upcoming": 0}
		for name, read in self.all_reads().items():
			with self.subTest(read=name), self.gate() as gate:
				self.assertTrue(read(sid)["ok"])
				self.assertEqual(gate.call_count, expected[name])
		with self.gate() as gate:
			self.assertTrue(api.get_snapshot_status(running)["ok"])
		self.assertEqual(gate.call_count, 0)

	def test_the_race_reload_path_runs_the_gate_once_and_checks_the_top_proofs(self):
		sid = self.publish()
		real, seen = store.load_status, []

		def stale(actor, snapshot_id):
			row = real(actor, snapshot_id)
			seen.append(1)
			return {**row, "state": "running"} if len(seen) == 1 else row

		with (
			self.gate() as gate,
			patch.object(store, "load_status", side_effect=stale),
			patch.object(api.jobs, "observe_job", side_effect=lambda status: real(self.user, sid)),
			patch.object(access, "verify_returned_rows", wraps=access.verify_returned_rows) as proofs,
		):
			response = api.get_snapshot_status(sid)
		self.assertTrue(response["ok"], response)
		self.assertEqual(response["data"]["state"], "complete")
		self.assertEqual(gate.call_count, 1)
		self.assertEqual([call.args[1] for call in proofs.call_args_list], list(SIDES))

	def test_a_disabled_scope_account_refuses_status_and_day_discards_and_tombstones(self):
		sids = {name: self.publish() for name in ("status", "day")}
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		self.assertRefused(api.get_snapshot_status(sids["status"]))
		self.assertRefused(api.get_day_detail(sids["day"], self.d10, "Payable"))
		for sid in sids.values():
			self.assertDiscarded(sid)


class TestFailureModes(ReadPathCase):
	def test_a_forbidden_side_is_refused_before_the_page_is_looked_at_and_never_discards(self):
		result = self.build()
		result["sources"]["Receivable"] = None
		result["unavailable"] = {"Receivable": "forbidden"}
		sid = self.publish(result)
		for date in (self.d10, add_days(self.fx.today, 5)):
			for kind, read in (
				("day", lambda: api.get_day_detail(sid, date, "Receivable")),
				("bucket", lambda: api.get_bucket_detail(sid, "window", "Receivable")),
			):
				with self.subTest(date=date, read=kind):
					response = read()
					self.assertEqual((response["ok"], response["state"]), (False, "forbidden"))
					self.assertFalse(response["retryable"])
					self.assertKept(sid)

	def test_a_hook_bug_is_an_internal_error_that_keeps_the_snapshot(self):
		self.hook("Purchase Invoice")
		sid = self.publish()
		errors = (
			AttributeError("hook bug"),
			RuntimeError("hook bug"),
			frappe.ValidationError("hook says no"),
		)
		for error in errors:
			with self.subTest(error=type(error).__name__), patch.object(frappe, "log_error") as log:
				clear_error_throttle()
				RAISE[:] = [error]
				for kind, read in self.reads("Payable").items():
					response = read(sid)
					self.assertFalse(response["ok"], response)
					self.assertEqual((response["state"], response["code"]), ("failed", "internal_error"))
					self.assertKept(sid)
				log.assert_called()
				self.assertNotIn("hook says no", str(log.call_args))

	def test_a_part_that_is_gone_after_a_discard_reads_as_a_changed_scope(self):
		sid = self.publish()
		stale = store.load_status(self.user, sid)
		store.discard(self.user, sid)
		with patch.object(store, "load_status", return_value=stale):
			self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
			self.assertRefused(api.get_upcoming(sid, self.fx.today))

	def test_a_parallel_read_after_a_refusal_is_a_changed_scope_not_an_expiry(self):
		self.hook("Purchase Invoice")
		sid = self.publish()
		DENIED.add(self.base["Payable"])
		self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
		for name, read in self.all_reads().items():
			with self.subTest(read=name):
				self.assertRefused(read(sid))
		frappe.set_user("Administrator")
		other = self.fx.user()
		frappe.get_doc("User", other).add_roles("Jarvis User")
		frappe.set_user(other)
		self.assertEqual(api.get_snapshot_status(sid)["code"], "not_found_or_expired")

	def test_a_snapshot_with_an_old_fingerprint_is_a_changed_scope_and_reads_no_result_key(self):
		proof = access.authorize_scope(self.scope)
		sid, _ = store.admit(self.user, "a" * 64, self.scope, "0" * 64)
		store.mark_running(self.user, sid)
		store.publish_snapshot(self.user, sid, store.split_result(self.build(), self.scope))
		commands = self.spy_cache()
		self.assertNotEqual(proof["fingerprint"], "0" * 64)
		for read in self.all_reads().values():
			self.assertRefused(read(sid))
		self.assertDiscarded(sid)
		keys = " ".join(key for command, args in commands if command == "GET" for key in args)
		self.assertNotIn("result:", keys)

	def test_a_balances_id_is_never_discarded_by_a_snapshot_read(self):
		sid, _ = store.admit(self.user, "c" * 64, self.scope, "f" * 64, kind="balances")
		self.assertEqual(api.get_snapshot_status(sid)["code"], "not_found_or_expired")
		self.assertIsNotNone(store.load_status(self.user, sid))


class TestStatusBody(ReadPathCase):
	def test_the_status_body_has_no_lenient_old_shape_branch(self):
		for name in ("STATUS_NEW_RESULT_KEYS", "STATUS_PROJECTION_KEYS"):
			self.assertFalse(hasattr(api, name), name)
		status = store.load_status(self.user, self.publish())
		part = store.load_part(self.user, status["snapshot_id"], "status")
		for key in ("horizon", "bank"):
			with self.subTest(key=key):
				broken = {k: v for k, v in part.items() if k != key}
				with self.assertRaises(KeyError):
					api.snapshot_status_body(status, broken)


class TestLogs(ReadPathCase):
	def logger(self):
		logger = MagicMock()
		patcher = patch.object(frappe, "logger", return_value=logger)
		patcher.start()
		self.addCleanup(patcher.stop)
		return logger

	def lines(self, logger):
		return [str(call.args[0]) for call in logger.warning.call_args_list]

	def test_a_refusal_logs_its_kind_side_and_endpoint_without_names(self):
		self.hook("Purchase Invoice")
		logger = self.logger()
		sid = self.publish()
		DENIED.add(self.base["Payable"])
		self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
		line = next(line for line in self.lines(logger) if line.startswith("read_refused"))
		self.assertEqual(line, "read_refused endpoint=get_day_detail kind=voucher side=Payable")
		for text in (self.base["Payable"], self.fx.supplier, self.fx.company):
			self.assertNotIn(text, " ".join(self.lines(logger)))

	def test_a_changed_policy_logs_the_fingerprint_kind(self):
		sid = self.publish()
		logger = self.logger()
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 1)
		frappe.set_user(self.user)
		self.assertRefused(api.get_snapshot_status(sid))
		self.assertIn("read_refused endpoint=get_snapshot_status kind=fingerprint side=-", self.lines(logger))

	def test_a_side_that_is_not_a_known_side_never_reaches_a_log_line(self):
		sid = self.publish()
		logger = self.logger()
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 1)
		frappe.set_user(self.user)
		self.assertRefused(api.get_day_detail(sid, self.d10, "evil\nPRIVATE side"))
		lines = [line for line in self.lines(logger) if line.startswith("read_refused")]
		self.assertEqual(lines, ["read_refused endpoint=get_day_detail kind=fingerprint side=-"])

	def test_a_part_missing_from_a_live_snapshot_is_logged_with_its_name_only(self):
		for name in ("payable", "proofs-payable", "index-payable"):
			with self.subTest(part=name):
				sid = self.publish()
				logger = self.logger()
				key = (
					store._part_key(self.user, sid, name)
					if name in store.PARTS
					else store._key(self.user, sid, name)
				)
				frappe.cache.expire(key, 0)
				self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"), "not_found_or_expired")
				self.assertIn(f"part_missing endpoint=get_day_detail part={name}", self.lines(logger))
				self.assertNotIn(sid, " ".join(self.lines(logger)))

	def test_a_discarded_or_unknown_snapshot_logs_no_missing_part(self):
		sid = self.publish()
		store.discard(self.user, sid)
		logger = self.logger()
		self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
		self.assertRefused(api.get_day_detail("0" * 32, self.d10, "Payable"), "not_found_or_expired")
		self.assertEqual([line for line in self.lines(logger) if "part_missing" in line], [])

	def test_an_error_log_for_a_wrapped_hook_error_names_the_cause_class_only(self):
		self.hook("Purchase Invoice")
		sid = self.publish()
		RAISE[:] = [frappe.ValidationError("PRIVATE hook text")]
		with patch.object(frappe, "log_error") as log:
			self.assertEqual(api.get_day_detail(sid, self.d10, "Payable")["code"], "internal_error")
		body = json.loads(log.call_args.kwargs["message"])
		self.assertEqual(body["exception_class"], "RuntimeError")
		self.assertEqual(body["cause_class"], "ValidationError")
		self.assertNotIn("PRIVATE", log.call_args.kwargs["message"])

	def test_a_slow_read_logs_endpoint_time_and_counts_without_names(self):
		logger = self.logger()
		sid = self.publish()
		with patch.object(read_log, "SLOW_MS", -1):
			self.assertTrue(api.get_day_detail(sid, self.d10, "Payable")["ok"])
		line = next(line for line in self.lines(logger) if line.startswith("slow_read"))
		self.assertRegex(line, r"^slow_read endpoint=get_day_detail ms=\d+ rows=1 parties=1$")
		self.assertNotIn(self.base["Payable"], line)

	def test_a_fast_read_logs_nothing(self):
		logger = self.logger()
		sid = self.publish()
		with patch.object(read_log, "SLOW_MS", 10**9):
			self.assertTrue(api.get_day_detail(sid, self.d10, "Payable")["ok"])
		self.assertEqual(self.lines(logger), [])

	def refuse_snapshots(self, count):
		self.hook("Purchase Invoice")
		for _ in range(count):
			sid = self.publish()
			DENIED.add(self.base["Payable"])
			self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))

	def test_three_refusals_of_one_snapshot_raise_no_error_log(self):
		self.hook("Purchase Invoice")
		sid = self.publish()
		DENIED.add(self.base["Payable"])
		with patch.object(frappe, "log_error") as log:
			for _ in range(3):
				self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
		log.assert_not_called()

	def test_three_different_discarded_snapshots_raise_exactly_one_error_log(self):
		with patch.object(frappe, "log_error") as log:
			self.refuse_snapshots(4)
		log.assert_called_once()
		self.assertEqual(log.call_args.kwargs["title"], "Payment Calendar refusal loop")
		for text in (self.base["Payable"], self.fx.supplier, self.fx.company, self.user):
			self.assertNotIn(text, log.call_args.kwargs["message"])

	def test_a_refusal_caused_only_by_the_site_day_changing_is_discarded_but_never_counted(self):
		tomorrow = add_days(self.fx.today, 1)
		for _ in range(4):
			sid = self.publish()
			with (
				patch("jarvis.payment_calendar.scope.nowdate", return_value=tomorrow),
				patch.object(store, "count_refusal") as counter,
				patch.object(frappe, "log_error") as log,
			):
				self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
			self.assertDiscarded(sid)
			counter.assert_not_called()
			log.assert_not_called()

	def test_an_anchor_refusal_is_logged_as_its_own_kind(self):
		sid = self.publish()
		logger = self.logger()
		with patch("jarvis.payment_calendar.scope.nowdate", return_value=add_days(self.fx.today, 1)):
			self.assertRefused(api.get_snapshot_status(sid))
		self.assertIn("read_refused endpoint=get_snapshot_status kind=anchor side=-", self.lines(logger))

	def test_a_failing_counter_or_error_log_never_changes_the_reply_or_the_discard(self):
		self.hook("Purchase Invoice")
		counters = (
			("counter", patch.object(store, "count_refusal", side_effect=RuntimeError("redis down"))),
			("error log", patch.object(store, "count_refusal", return_value=(1, 3))),
		)
		for name, counter in counters:
			with (
				self.subTest(failing=name),
				counter,
				patch.object(frappe, "log_error", side_effect=RuntimeError("db down")),
			):
				sid = self.publish()
				DENIED.add(self.base["Payable"])
				self.assertRefused(api.get_day_detail(sid, self.d10, "Payable"))
				self.assertDiscarded(sid)


@skipUnless(
	ON_V15, "Frappe 15 only: runs in the v15 backport CI (see the backport checklist in the architecture doc)"
)
class TestV15Backport(ReadPathCase):
	"""AC11 and plan test 16 at read-path level. They cannot run on v16, where the rules do not apply."""

	def test_a_non_system_manager_read_succeeds_with_the_row_filter_columns_applied(self):
		from frappe.desk import query_report

		from jarvis.payment_calendar import permissions

		self.assertNotIn("System Manager", frappe.get_roles(self.user))
		sid = self.publish()
		columns = store.load_part(self.user, sid, "status")["_permission_columns"]["Payable"]
		with patch.object(
			query_report, "get_filtered_data", wraps=query_report.get_filtered_data
		) as filtered:
			for name, read in self.all_reads().items():
				with self.subTest(read=name):
					self.assertTrue(read(sid)["ok"])
		sent = [call.args[1] for call in filtered.call_args_list]
		self.assertTrue(sent)
		self.assertIn(permissions.row_filter_columns(columns), sent)

	def sales_person_permission(self):
		"""Give the test user a Sales Person user permission; the next policy read and fingerprint see it."""
		frappe.set_user("Administrator")
		person = self.fx.insert(
			"Sales Person", "SalesPerson", sales_person_name="Scoped sales person", is_group=0, lft=1, rgt=2
		)
		# Saving the row clears the cached `user_permissions` of the user (UserPermission.on_update).
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Sales Person",
				"for_value": person,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.user)

	def test_a_sales_person_permission_makes_every_read_restricted(self):
		# The snapshot is built while no permission exists: the v15 build gate cannot refuse it.
		result = self.build()
		self.sales_person_permission()
		# The fingerprint is taken with the permission, so only the v15 obligation gate on the read can refuse.
		sid = self.publish(result)
		for name, read in self.all_reads().items():
			with self.subTest(read=name):
				response = read(sid)
				self.assertFalse(response["ok"], response)
				self.assertEqual(response["code"], "restricted_obligations")
				self.assertNotIn("data", response)

	def test_a_sales_person_permission_refuses_the_receivable_source_at_build(self):
		self.sales_person_permission()
		with self.assertRaises(CalendarSourceUnavailable) as error:
			self.build()
		self.assertEqual(error.exception.code, "restricted_obligations")
