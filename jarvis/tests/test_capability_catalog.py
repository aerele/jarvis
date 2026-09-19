import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _task_card_catalog as catalog
from jarvis import api
from jarvis.chat import user_settings_api
from jarvis.tools import registry


class TestGatingBadge(FrappeTestCase):
	"""_gating_badge derives the tier from the enforcing frozensets - semantic,
	not just total (a totality-only test would have missed the export family)."""

	def test_reads_only(self):
		for t in ("get_customer_outstanding", "run_report", "get_list", "get_stock_balance"):
			self.assertEqual(api._gating_badge(t), "reads_only", msg=t)

	def test_asks_to_approve_is_only_the_auto_applyable_pair(self):
		self.assertEqual(api._gating_badge("create_doc"), "asks_to_approve")
		self.assertEqual(api._gating_badge("update_doc"), "asks_to_approve")

	def test_always_asks(self):
		# destructive + gated-non-autoapply, incl. bulk create_docs (in _GATED_WRITES)
		for t in (
			"delete_doc",
			"cancel_doc",
			"send_email",
			"apply_workflow_action",
			"submit_doc",
			"run_method",
			"create_docs",
			"share_doc",
		):
			self.assertEqual(api._gating_badge(t), "always_asks", msg=t)

	def test_writes_directly_family_is_flagged(self):
		# audited-but-ungated singles: proves they resolve to the excluded tier
		for t in (
			"export_excel",
			"download_pdf",
			"report_pdf",
			"add_tag",
			"add_comment",
			"attach_to_doc",
			"create_dashboard",
		):
			self.assertEqual(api._gating_badge(t), "writes_directly", msg=t)

	def test_total_over_registry(self):
		allowed = {"reads_only", "asks_to_approve", "always_asks", "writes_directly"}
		for t in registry._TOOL_NAMES:
			self.assertIn(api._gating_badge(t), allowed, msg=t)


class TestTaskCardCatalog(FrappeTestCase):
	def test_cards_are_single_tool_and_reference_live_tools(self):
		for c in catalog.TASK_CARDS:
			self.assertEqual(len(c["tools"]), 1, msg=f"{c['title']} must be single-tool in v1")
			self.assertIn(c["tools"][0], registry._TOOL_NAMES, msg=c["title"])

	def test_no_card_resolves_to_writes_directly(self):
		# the literal gate: run the badge over EVERY card
		for c in catalog.TASK_CARDS:
			self.assertNotEqual(api._gating_badge(c["tools"][0]), "writes_directly", msg=c["title"])

	def test_no_wiki_card(self):
		# wiki tools are kill-switch-gated; a static card can't reflect that state
		for c in catalog.TASK_CARDS:
			self.assertNotIn(c["tools"][0], ("update_wiki", "read_wiki"), msg=c["title"])

	def test_groups_known_copy_present_unique(self):
		groups = {"Look things up", "Create & update", "Send & submit"}
		seen = set()
		for c in catalog.TASK_CARDS:
			self.assertIn(c["group"], groups)
			self.assertTrue(c["title"] and c["prompt"])
			self.assertNotIn(c["title"], seen, msg=f"duplicate {c['title']}")
			seen.add(c["title"])

	def test_reasonably_sized(self):
		self.assertGreaterEqual(len(catalog.TASK_CARDS), 12)


class TestCapabilityCatalogEndpoint(FrappeTestCase):
	def test_returns_cards_with_visible_badge_tiers_only(self):
		res = user_settings_api.get_capability_catalog()
		self.assertTrue(res["ok"])
		cards = res["data"]["cards"]
		self.assertGreaterEqual(len(cards), 12)
		visible = {"reads_only", "asks_to_approve", "always_asks"}
		for c in cards:
			self.assertIn(c["badge"], visible)
			self.assertNotIn("tools", c)  # internal tool mapping must not leak to the client

	def test_email_card_always_asks(self):
		cards = user_settings_api.get_capability_catalog()["data"]["cards"]
		email = next(c for c in cards if "email" in c["title"].lower())
		self.assertEqual(email["badge"], "always_asks")

	def test_requires_jarvis_access(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				user_settings_api.get_capability_catalog()
		finally:
			frappe.set_user("Administrator")

	def test_log_capability_pick_ok(self):
		res = user_settings_api.log_capability_pick("Send a document by email")
		self.assertTrue(res["ok"])
