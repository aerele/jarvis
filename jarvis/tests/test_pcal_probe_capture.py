"""Scratch probe module (never in a PR): the read paths that the port must not change, and the test id list.

`test_capture_is_deterministic` builds a small fixture twice (own helper file, a copy of the version-15 base
fixture, so both trees run the same fixture bytes), reads it through `sources.read_obligations`,
`banks.read_bank_balances` and `permissions.visible_names`, and prints one canonical JSON line `PCAL_CAPTURE`.
Names are replaced by labels in creation order, the dates are fixed and the row ids are listed apart
(`PCAL_CAPTURE_IDS`), because only the ids may differ between the trees (D-1). The two fixture sets must give
equal output in one run.

`test_the_id_list` runs `pcal_probe_idlist.py` in a fresh interpreter and prints its `PCAL_IDS` lines.
"""

import json
import os
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import banks, permissions, sources
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.pcal_probe_fixtures import CalendarFixtures

AS_OF = "2031-06-15"
WINDOW = ("2031-06-01", "2031-07-31")
SIDES = ("Payable", "Receivable")
SAVEPOINT = "pcal_probe_capture"
# Base modules that cannot load without a site (plan 6.2, measured on the v15 base): their ids come from the
# base round's log.
NO_SITE_LESS = {
	"test_get_employee_shift_real_hrms",
	"test_holiday_touch_api",
	"test_holiday_touch_boot",
	"test_holiday_touch_cache",
	"test_holiday_touch_explain",
	"test_holiday_touch_force",
	"test_holiday_touch_policy",
	"test_holiday_touch_pwa_boot",
	"test_holiday_touch_record_shown",
	"test_holiday_touch_report",
	"test_holiday_touch_resolve",
	"test_private_network_outbound",
	"test_gateway_harness_config",
}


def outcome(call, refusals=()):
	"""The result, or the class and refusal code of an expected refusal. Any other exception is a fault: it raises."""
	try:
		return {"ok": call()}
	except refusals as exc:
		return {"raised": type(exc).__name__, "code": getattr(exc, "code", None)}


def build():
	fx = CalendarFixtures()
	fx.today = AS_OF
	fx.fiscal_year()
	frappe.cache().hdel("fiscal_years", fx.company)
	names = {
		"Payable": [
			fx.invoice((100, 50), side="Payable", paid=30),
			fx.invoice((75,), side="Payable"),
			fx.invoice((10, 20), side="Payable", offset=-3),
		],
		"Receivable": [
			fx.invoice((200,), side="Receivable", paid=20),
			fx.invoice((40, 60), side="Receivable", offset=-5),
		],
	}
	fx.gl(1000)
	fx.gl(-250)
	return (
		fx,
		names,
		{"manager": fx.user("Accounts Manager"), "user": fx.user("Accounts User")},
	)


def strip_ids(value, ids):
	"""The value with each `id` entry of a mapping removed and listed in `ids`, in reading order."""
	if isinstance(value, dict):
		kept = {}
		for key, item in value.items():
			if key == "id":
				ids.append(item)
			else:
				kept[key] = strip_ids(item, ids)
		return kept
	if isinstance(value, (list, tuple)):
		return [strip_ids(item, ids) for item in value]
	return value


def assert_a_real_capture(captured):
	"""The capture holds data and the expected refusals, so two equal captures cannot both be empty or errors."""
	for side in SIDES:
		rows = captured[f"obligations:{side}"]["ok"]["rows"]
		assert rows, f"no {side} rows were captured"
		assert captured[f"too_large:{side}"]["code"] == "too_large", captured[f"too_large:{side}"]
		assert captured[f"missing_company:{side}"]["raised"] == "DoesNotExistError", captured
	assert captured["bank"]["ok"]["accounts"], "no bank balance was captured"
	for name, value in captured.items():
		if name.startswith("visible:") and name.endswith(":manager"):
			assert value["ok"], f"{name} is empty"


def capture():
	fx, names, users = build()
	captured = {}
	for side in SIDES:
		captured[f"obligations:{side}"] = outcome(
			lambda side=side: sources.read_obligations(fx.company, side, AS_OF, *WINDOW)
		)
		with patch.object(sources, "MAX_VOUCHERS", 0):
			captured[f"too_large:{side}"] = outcome(
				lambda side=side: sources.read_obligations(fx.company, side, AS_OF, *WINDOW),
				(CalendarSourceUnavailable,),
			)
		captured[f"missing_company:{side}"] = outcome(
			lambda side=side: sources.read_obligations("No Such Company", side, AS_OF, *WINDOW),
			(frappe.DoesNotExistError,),
		)
	captured["bank"] = outcome(lambda: banks.read_bank_balances(fx.company, [fx.bank], AS_OF))
	for label, user in users.items():
		frappe.set_user(user)
		try:
			for side, doctype in (
				("Payable", "Purchase Invoice"),
				("Receivable", "Sales Invoice"),
			):
				captured[f"visible:{doctype}:{label}"] = outcome(
					lambda doctype=doctype, side=side: sorted(permissions.visible_names(doctype, names[side]))
				)
		finally:
			frappe.set_user("Administrator")
	text = json.dumps(captured, sort_keys=True, default=str)
	text = text.replace(fx.prefix, "<P>").replace(fx.prefix.lower(), "<p>")
	ids = []
	canonical = strip_ids(json.loads(text), ids)
	assert_a_real_capture(canonical)
	assert ids, "no row id was captured"
	return canonical, ids


class TestProbeCapture(TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self.rollback)

	def rollback(self):
		frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=SAVEPOINT)

	def test_capture_is_deterministic(self):
		first, first_ids = capture()
		second, second_ids = capture()
		print("PCAL_CAPTURE " + json.dumps(first, sort_keys=True), flush=True)
		print("PCAL_CAPTURE_IDS " + json.dumps(first_ids), flush=True)
		self.assertEqual(first, second)
		self.assertEqual(len(first_ids), len(second_ids))

	def test_the_id_list(self):
		script = os.path.join(os.path.dirname(__file__), "pcal_probe_idlist.py")
		done = subprocess.run(
			[sys.executable, script, os.path.abspath(frappe.local.sites_path)],
			capture_output=True,
			text=True,
			timeout=900,
			cwd=tempfile.gettempdir(),
			check=False,
		)
		records = []
		for line in done.stdout.splitlines():
			if line.startswith("PCAL_IDS "):
				print(line, flush=True)
				records.append(json.loads(line[len("PCAL_IDS ") :]))
		self.assertEqual(done.returncode, 0, done.stderr[-2000:])
		loaded = [record for record in records if "tests" in record]
		self.assertTrue(loaded, "the id lister printed no loaded module")
		self.assertTrue(all(record["tests"] for record in loaded), "a loaded module has no test")
		unexpected = sorted(
			r["module"] for r in records if "load_error" in r and r["module"] not in NO_SITE_LESS
		)
		self.assertEqual(unexpected, [], "a module that should load site-less did not")
