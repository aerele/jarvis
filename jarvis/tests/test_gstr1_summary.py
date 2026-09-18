"""``jarvis.tools.get_gstr1_summary`` — the read-only GSTR-1 return-side tie-out feed.

Two layers: the PURE reduction (the load-bearing correctness — it must reproduce IC's
own Total-Liability rollup, incl. the flagged indent-0 amendment row) and the tool
integration (fail-closed not-available envelope, read-only, per-record perms).

Run:
  bench --site test_jarvis run-tests --app jarvis --module jarvis.tests.test_gstr1_summary
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import get_gstr1_summary as gs
from jarvis.tools.get_gstr1_summary import get_gstr1_summary

RETURN_LOG = "GST Return Log"
GSTIN_A = "29AAAAA0000A1Z5"
GSTIN_B = "27BBBBB0000B1Z4"
GSTIN_C = "24CCCCC0000C1Z3"
PERIOD = "122026"


def _filed_summary_rows():
	"""IC 16.7 ``summarize_retsum_data`` output shape: an unflagged indent-0 category
	row (excluded), flagged indent-1 rows (a normal one, and an RCM row whose TAX is
	not considered), and the flagged indent-0 'Net Liability from Amendments' row
	(negative, MUST be included). Expected total: taxable 160000, igst 25200."""
	return [
		{
			"description": "B2B, SEZ, DE",
			"indent": 0,
			"no_of_records": 2,
			"total_taxable_value": 150000,
			"total_igst_amount": 27000,
			"total_cgst_amount": 0,
			"total_sgst_amount": 0,
			"total_cess_amount": 0,
		},
		{
			"description": "B2B Regular",
			"indent": 1,
			"consider_in_total_taxable_value": True,
			"consider_in_total_tax": True,
			"total_taxable_value": 100000,
			"total_igst_amount": 18000,
			"total_cgst_amount": 0,
			"total_sgst_amount": 0,
			"total_cess_amount": 0,
		},
		{
			"description": "SEZWP",
			"indent": 1,
			"consider_in_total_taxable_value": True,
			"consider_in_total_tax": True,
			"total_taxable_value": 50000,
			"total_igst_amount": 9000,
			"total_cgst_amount": 0,
			"total_sgst_amount": 0,
			"total_cess_amount": 0,
		},
		{
			"description": "B2B RCM",
			"indent": 1,
			"consider_in_total_taxable_value": True,
			"consider_in_total_tax": False,
			"total_taxable_value": 20000,
			"total_igst_amount": 3600,
			"total_cgst_amount": 0,
			"total_sgst_amount": 0,
			"total_cess_amount": 0,
		},
		{
			"description": "Net Liability from Amendments",
			"indent": 0,
			"consider_in_total_taxable_value": True,
			"consider_in_total_tax": True,
			"no_of_records": 0,
			"total_taxable_value": -10000,
			"total_igst_amount": -1800,
			"total_cgst_amount": 0,
			"total_sgst_amount": 0,
			"total_cess_amount": 0,
		},
	]


class TestGstr1SummaryReduction(FrappeTestCase):
	"""The reduction/schema helpers — pure, no DB. This is the correctness core."""

	def test_reduction_matches_ic_total_liability(self):
		# flag-filtered, per-column, NO indent filter: includes the flagged indent-0
		# amendment row (negative), the RCM row's taxable but NOT its tax, and excludes
		# the unflagged indent-0 category row.
		t = gs._reduce(_filed_summary_rows())
		self.assertEqual(t["total_taxable_value"], 160000.0)  # 100k+50k+20k-10k
		self.assertEqual(t["total_igst"], 25200.0)  # 18k+9k-1800 (RCM tax excluded)
		self.assertEqual(t["total_cgst"], 0.0)

	def test_reduction_never_filters_by_indent(self):
		# a naive indent==1 rule would drop the amendment row -> 170000/27000 (wrong).
		t = gs._reduce(_filed_summary_rows())
		self.assertNotEqual(t["total_taxable_value"], 170000.0)
		self.assertNotEqual(t["total_igst"], 27000.0)

	def test_reduction_excludes_unflagged_category_rows(self):
		# only the unflagged indent-0 category row present -> nothing considered -> 0.
		rows = [
			{
				"description": "cat",
				"indent": 0,
				"total_taxable_value": 999,
				"total_igst_amount": 999,
				"total_cgst_amount": 0,
				"total_sgst_amount": 0,
				"total_cess_amount": 0,
			}
		]
		self.assertEqual(gs._reduce(rows)["total_taxable_value"], 0.0)

	def test_schema_sniff(self):
		self.assertTrue(gs._schema_ok(_filed_summary_rows()))
		self.assertFalse(gs._schema_ok([]))
		self.assertFalse(gs._schema_ok("not a list"))
		self.assertFalse(gs._schema_ok([{"foo": 1}]))  # no consider flag, no amount keys
		# a renamed amount key must be rejected (never blind-reduced to 0)
		renamed = _filed_summary_rows()
		for r in renamed:
			if "total_igst_amount" in r:
				r["igst_total"] = r.pop("total_igst_amount")
		self.assertFalse(gs._schema_ok(renamed))

	def test_period_window(self):
		self.assertEqual(gs._period_window("122026"), ("2026-12-01", "2026-12-31"))
		self.assertEqual(gs._period_window("022027"), ("2027-02-01", "2027-02-28"))


class TestGstr1SummaryTool(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("DocType", RETURN_LOG):
			raise unittest.SkipTest("india_compliance (GST Return Log) not installed on this site")
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.filed = cls._mk_log(GSTIN_A, "Filed", filed=True)
		cls.notfiled = cls._mk_log(GSTIN_B, "Not Filed", filed=False)
		cls.filed_no_summary = cls._mk_log(GSTIN_C, "Filed", filed=False)  # Filed but no attach
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete(RETURN_LOG, {"return_period": PERIOD, "gstin": ["in", [GSTIN_A, GSTIN_B, GSTIN_C]]})
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _mk_log(cls, gstin, filing_status, *, filed):
		name = f"GSTR1-{PERIOD}-{gstin}"
		if frappe.db.exists(RETURN_LOG, name):
			frappe.delete_doc(RETURN_LOG, name, force=True, ignore_permissions=True)
		d = frappe.get_doc(
			{
				"doctype": RETURN_LOG,
				"company": cls.company,
				"return_type": "GSTR1",
				"return_period": PERIOD,
				"gstin": gstin,
				"filing_status": filing_status,
			}
		)
		d.flags.ignore_permissions = True
		d.flags.ignore_mandatory = True
		d.insert(ignore_permissions=True)
		if filed:
			d.update_json_for("filed_summary", _filed_summary_rows())
		return d.name

	def setUp(self):
		frappe.set_user("Administrator")

	# ---- the happy path -------------------------------------------------- #
	def test_filed_log_returns_reduced_totals(self):
		r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_A)
		self.assertTrue(r["available"])
		self.assertEqual(r["source"], "filed_summary")
		self.assertEqual(r["log_name"], self.filed)
		self.assertEqual(r["total_taxable_value"], 160000.0)
		self.assertEqual(r["total_igst"], 25200.0)
		self.assertEqual((r["from_date"], r["to_date"]), ("2026-12-01", "2026-12-31"))

	# ---- honest not-available paths -------------------------------------- #
	def test_not_filed_log_is_not_available(self):
		r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_B)
		self.assertFalse(r["available"])
		self.assertEqual(r["reason"], "not_filed")

	def test_filed_without_summary_is_not_available(self):
		# a Filed log that carries no filed_summary attachment -> not_evaluable, never
		# a zero-total false clean.
		r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_C)
		self.assertFalse(r["available"])
		self.assertEqual(r["reason"], "no_filed_summary")

	def test_absent_period_is_no_log(self):
		self.assertEqual(get_gstr1_summary(self.company, "012025")["reason"], "no_log")

	def test_ambiguous_gstin_is_refused(self):
		# two logs (A filed, B not) for the company+period, no gstin arg -> ambiguous.
		r = get_gstr1_summary(self.company, PERIOD)
		self.assertFalse(r["available"])
		self.assertEqual(r["reason"], "ambiguous_gstin")

	def test_quarterly_is_deferred(self):
		frappe.db.set_value(RETURN_LOG, self.filed, "filing_preference", "Quarterly")
		try:
			r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_A)
			self.assertEqual(r["reason"], "window_mismatch")
		finally:
			frappe.db.set_value(RETURN_LOG, self.filed, "filing_preference", None)

	# ---- malformed args RAISE (caller bugs surface) ---------------------- #
	def test_malformed_args_raise(self):
		with self.assertRaises(InvalidArgumentError):
			get_gstr1_summary("", PERIOD)
		with self.assertRaises(InvalidArgumentError):
			get_gstr1_summary(self.company, "2026-12")  # not MMYYYY
		with self.assertRaises(InvalidArgumentError):
			get_gstr1_summary("No Such Co Ltd", PERIOD)

	# ---- READ-ONLY: no mutation of the log or its summary ---------------- #
	def test_read_only_no_mutation(self):
		before = frappe.db.get_value(
			RETURN_LOG, self.filed, ["filing_status", "generation_status", "filed_summary"], as_dict=True
		)
		get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_A)
		after = frappe.db.get_value(
			RETURN_LOG, self.filed, ["filing_status", "generation_status", "filed_summary"], as_dict=True
		)
		self.assertEqual(before, after)

	# ---- security: a Company-sliced user cannot read another company ----- #
	def test_company_slice_refused(self):
		# a user permission-sliced to a DIFFERENT company must get not-available for
		# self.company (never its return figures).
		other = frappe.db.get_value("Company", {"name": ["!=", self.company]}, "name")
		if not other:
			self.skipTest("need a second Company to exercise the slice")
		user = "gstr1-sliced@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "Sliced",
					"send_welcome_email": 0,
					"roles": [{"role": "Auditor"}],
				}
			).insert(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": user, "allow": "Company"})
		frappe.get_doc(
			{"doctype": "User Permission", "user": user, "allow": "Company", "for_value": other}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			frappe.set_user(user)
			r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_A)
			self.assertFalse(r["available"])
			self.assertEqual(r["reason"], "permission_denied")
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("User Permission", {"user": user, "allow": "Company"})
			frappe.db.commit()

	def test_per_record_permission_layer_enforced(self):
		# a user unrestricted by Company (passes is_company_permitted) but User-Permission
		# sliced to a DIFFERENT GST Return Log record must be refused at the per-record
		# has_permission gate — never read the filed log's figures.
		user = "gstr1-recperm@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "RecPerm",
					"send_welcome_email": 0,
					"roles": [{"role": "Auditor"}],
				}
			).insert(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": user, "allow": RETURN_LOG})
		# restrict to the OTHER (not-filed) log only -> the filed log is out of the slice.
		frappe.get_doc(
			{"doctype": "User Permission", "user": user, "allow": RETURN_LOG, "for_value": self.notfiled}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			frappe.set_user(user)
			r = get_gstr1_summary(self.company, PERIOD, gstin=GSTIN_A)
			self.assertFalse(r["available"])
			self.assertEqual(r["reason"], "permission_denied")
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("User Permission", {"user": user, "allow": RETURN_LOG})
			frappe.db.commit()
