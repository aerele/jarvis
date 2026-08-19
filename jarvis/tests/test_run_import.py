"""Tests for the gated run_import path: the api.py park-time refusal (_import_park) and
the confirm-time tool (jarvis.tools.run_import).

Reuses the ephemeral parent+child fixture from test_import_preview.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.api import (
	_AUTO_APPLYABLE,
	_DESTRUCTIVE,
	_DRY_RUN_ON_PARK,
	_GATED_WRITES,
	_PREVIEWABLE,
	_WRITE_TOOLS,
	_import_park,
)
from jarvis.chat import confirm_card
from jarvis.exceptions import FeatureDisabledError, InvalidArgumentError
from jarvis.tests.test_import_preview import (
	_BAD_LINK_CSV,
	_FLAT_CSV,
	_NO_TITLE_CSV,
	CHILD_DT,
	PARENT_DT,
	_ensure_doctypes,
	_make_csv,
)
from jarvis.tools._import_preview import build_import_preview
from jarvis.tools.run_import import run_import


class TestRunImportGating(FrappeTestCase):
	_files = ("jarvis_run_flat.csv", "jarvis_run_bad_link.csv", "jarvis_run_no_title.csv")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_run_flat.csv")
		cls.bad_link_url = _make_csv(_BAD_LINK_CSV, "jarvis_run_bad_link.csv")
		cls.no_title_url = _make_csv(_NO_TITLE_CSV, "jarvis_run_no_title.csv")
		# run_import commits mid-test (commit-before-enqueue), which collapses the
		# per-test savepoint - so commit the fixtures now to keep them durable.
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in cls._files:
			existing = frappe.db.exists("File", {"file_name": name})
			if existing:
				frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def _cleanup_records(self, di_name):
		# run_import commits (Data Import + the synchronous test-mode import), so the
		# framework rollback can't undo it - delete explicitly. Reap the Data Import with
		# a RAW delete: delete_doc would cascade to remove its attached import_file (the
		# shared fixture File), breaking later tests.
		for title in ("REC-1", "REC-2"):
			if frappe.db.exists(PARENT_DT, title):
				frappe.delete_doc(PARENT_DT, title, force=True, ignore_permissions=True)
		if di_name:
			frappe.db.delete("Data Import Log", {"data_import": di_name})
			frappe.db.delete("Data Import", {"name": di_name})
		frappe.db.commit()

	# --- gate membership ----------------------------------------------------

	def test_gate_membership(self):
		self.assertIn("run_import", _WRITE_TOOLS)
		self.assertIn("run_import", _GATED_WRITES)
		self.assertIn("run_import", _DESTRUCTIVE)
		# NEVER auto-applies; not sandbox-dry-run at park; not the sandbox-preview set.
		self.assertNotIn("run_import", _AUTO_APPLYABLE)
		self.assertNotIn("run_import", _DRY_RUN_ON_PARK)
		self.assertNotIn("run_import", _PREVIEWABLE)

	# --- park-time refusal (_import_park) -----------------------------------

	def test_park_clean_returns_preview_no_reject(self):
		rejected, preview = _import_park({"doctype": PARENT_DT, "file_url": self.flat_url})
		self.assertIsNone(rejected)
		self.assertIn("import", preview)
		self.assertEqual(preview["import"]["doctype"], PARENT_DT)
		self.assertTrue(preview.get("note"))

	def test_park_blocking_refuses(self):
		rejected, preview = _import_park({"doctype": PARENT_DT, "file_url": self.bad_link_url})
		self.assertIsNone(preview)
		self.assertFalse(rejected["ok"])
		self.assertEqual(rejected["error"]["code"], "ImportBlockedError")

	def test_park_missing_mandatory_refuses(self):
		rejected, preview = _import_park({"doctype": PARENT_DT, "file_url": self.no_title_url})
		self.assertIsNone(preview)
		self.assertFalse(rejected["ok"])

	def test_park_refuses_caller_who_cannot_create_data_import(self):
		# A caller without Data Import create permission (the usual non-System-Manager)
		# is refused AT PARK - no card, no confirm-then-fail. Simulate by denying only
		# the Data Import create check; the import-perm and File-read checks still pass.
		real = frappe.has_permission

		def fake(doctype=None, ptype="read", *a, **k):
			if doctype == "Data Import" and ptype == "create":
				return False
			return real(doctype, ptype, *a, **k)

		with patch("frappe.has_permission", side_effect=fake):
			rejected, preview = _import_park({"doctype": PARENT_DT, "file_url": self.flat_url})
		self.assertIsNone(preview)
		self.assertFalse(rejected["ok"])
		self.assertEqual(rejected["error"]["code"], "PermissionDeniedError")

	def test_park_honors_kill_switch(self):
		frappe.conf.jarvis_import_disabled = 1
		try:
			rejected, preview = _import_park({"doctype": PARENT_DT, "file_url": self.flat_url})
		finally:
			del frappe.conf["jarvis_import_disabled"]
		self.assertIsNone(preview)
		self.assertFalse(rejected["ok"])
		self.assertEqual(rejected["error"]["code"], "FeatureDisabledError")

	def test_park_canonicalizes_filename_to_file_url(self):
		# A bare filename is resolved to a concrete file_url ONCE at park and threaded to
		# confirm, so confirm can't re-resolve to a different "most recent" file (drift).
		args = {"doctype": PARENT_DT, "filename": "jarvis_run_flat.csv"}
		rejected, preview = _import_park(args)
		self.assertIsNone(rejected)
		self.assertTrue(args.get("file_url"))
		self.assertNotIn("filename", args)

	# --- confirm-time tool --------------------------------------------------

	def test_run_import_stages_and_imports(self):
		# In test mode form_start_import runs synchronously, so the import actually lands.
		out = None
		try:
			out = run_import(doctype=PARENT_DT, file_url=self.flat_url)
			self.assertEqual(out["status"], "queued")
			self.assertEqual(out["total_records"], 2)
			di = frappe.get_doc("Data Import", out["data_import"])
			self.assertEqual(di.reference_doctype, PARENT_DT)
			# The two parent records were created by the synchronous import.
			self.assertTrue(frappe.db.exists(PARENT_DT, "REC-1"))
			self.assertTrue(frappe.db.exists(PARENT_DT, "REC-2"))
		finally:
			self._cleanup_records(out["data_import"] if out else None)

	def test_run_import_kill_switch(self):
		frappe.conf.jarvis_import_disabled = 1
		try:
			with self.assertRaises(FeatureDisabledError):
				run_import(doctype=PARENT_DT, file_url=self.flat_url)
		finally:
			del frappe.conf["jarvis_import_disabled"]

	def test_run_import_belt_refuses_blocking(self):
		# Even if the park were bypassed, the confirm-time belt refuses a blocking file
		# and stages nothing.
		before = frappe.db.count("Data Import")
		with self.assertRaises(InvalidArgumentError):
			run_import(doctype=PARENT_DT, file_url=self.bad_link_url)
		self.assertEqual(frappe.db.count("Data Import"), before)

	def test_run_import_scheduler_paused_refuses_and_stages_nothing(self):
		# The harness's frappe.in_test short-circuits run_now, so force the prod path
		# (run_now False) with an inactive scheduler and assert a clean refusal - a paused
		# scheduler must NOT stage an import that would sit Pending forever.
		before = frappe.db.count("Data Import")
		with (
			patch("jarvis.compat.in_test", return_value=False),
			patch.dict(frappe.conf, {"developer_mode": 0}),
			patch("jarvis.tools.run_import.is_scheduler_inactive", return_value=True),
		):
			with self.assertRaises(FeatureDisabledError):
				run_import(doctype=PARENT_DT, file_url=self.flat_url)
		self.assertEqual(frappe.db.count("Data Import"), before)

	def test_run_import_starts_last_after_commit(self):
		# start_import must be called; stage the DI, then start it. Mock start_import so
		# the real import doesn't run - we only assert ordering/receipt here.
		out = None
		with patch(
			"frappe.core.doctype.data_import.data_import.DataImport.start_import",
			return_value=True,
		) as m:
			try:
				out = run_import(doctype=PARENT_DT, file_url=self.flat_url)
				m.assert_called_once()
				self.assertTrue(frappe.db.exists("Data Import", out["data_import"]))
			finally:
				self._cleanup_records(out["data_import"] if out else None)


class TestImportCard(FrappeTestCase):
	_files = ("jarvis_card_flat.csv", "jarvis_card_secret.csv")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_card_flat.csv")
		cls.secret_url = _make_csv("Title,Secret Code\nX-1,hunter2\n", "jarvis_card_secret.csv")

	@classmethod
	def tearDownClass(cls):
		for name in cls._files:
			existing = frappe.db.exists("File", {"file_name": name})
			if existing:
				frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def _card(self, url, **kwargs):
		# The card renders from the preview payload; build it directly (park gating is a
		# separate concern already covered by TestRunImportGating).
		prev = build_import_preview(doctype=PARENT_DT, file_url=url, **kwargs)
		return confirm_card.build_card(
			"run_import", {"doctype": PARENT_DT, "file_url": url, **kwargs}, {"import": prev, "note": "x"}
		)

	def test_card_kind_and_shape(self):
		card = self._card(self.flat_url)
		self.assertEqual(card["kind"], "import")
		self.assertEqual(card["doctype"], PARENT_DT)
		self.assertEqual(card["total_rows"], 2)
		self.assertEqual(card["total_records"], 2)
		self.assertIn("Title", card["sample"]["columns"])
		self.assertEqual(len(card["sample"]["rows"]), 2)

	def test_card_masks_secret(self):
		card = self._card(self.secret_url)
		cols = card["sample"]["columns"]
		idx = cols.index("Secret Code")
		self.assertEqual(card["sample"]["rows"][0]["cells"][idx], "[hidden]")

	def test_card_advisory_carries_fan_out(self):
		card = self._card(self.flat_url)
		self.assertTrue(any("separate" in a for a in card["advisory"]))

	def test_card_none_on_malformed_preview(self):
		# No import payload -> None, so build_card's caller shows the described-intent
		# floor (the park always attaches a note), never a blank card.
		self.assertIsNone(confirm_card.build_card("run_import", {"doctype": PARENT_DT}, {"note": "x"}))
