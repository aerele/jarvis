"""Save-path tests for the dashboard theme standard: the controller runs the
theme validator on every insert / html-changing save, stamps the schema version
on pass, and grandfathers legacy (unstamped) docs on metadata-only edits.

Needs the DB (DocType controller) so it is a FrappeTestCase — runs at assembly
under bench, NOT from the worktree (the pure validator behavior is covered
standalone in test_dashboard_theme_validator.py). Every dashboard created here is
tracked and force-deleted in tearDown.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.dashboards.theme_validator import THEME_SCHEMA_VERSION
from jarvis.jarvis.doctype.jarvis_dashboard.jarvis_dashboard import ThemeStandardError

DASHBOARD = "Jarvis Dashboard"


class TestDashboardThemeStandard(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		self._dashboards: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._dashboards:
			if frappe.db.exists(DASHBOARD, name):
				frappe.delete_doc(DASHBOARD, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _mk(self, html: str, theme: str = "Jarvis"):
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": f"vt-{frappe.generate_hash(length=6)}",
				"html": html,
				"theme": theme,
				"scope": "User",
			}
		)
		return doc

	def _insert(self, html: str, theme: str = "Jarvis"):
		doc = self._mk(html, theme)
		doc.insert(ignore_permissions=True)
		self._dashboards.append(doc.name)
		return doc

	# ---- conforming + stamp ------------------------------------------------- #
	def test_conforming_saves_and_is_stamped(self):
		doc = self._insert('<div class="jd-card">ok</div><style>.jd-card{color:var(--jd-ink)}</style>')
		self.assertEqual(doc.theme_schema_version, THEME_SCHEMA_VERSION)
		self.assertEqual(
			frappe.db.get_value(DASHBOARD, doc.name, "theme_schema_version"),
			THEME_SCHEMA_VERSION,
		)

	# ---- reject off-spec ---------------------------------------------------- #
	def test_off_palette_color_rejected(self):
		doc = self._mk("<style>.x{color:#0f172a}</style>")
		# a distinct exc type so the SPA can offer "ask the assistant to fix these"
		with self.assertRaises(ThemeStandardError):
			doc.insert(ignore_permissions=True)

	def test_hardcoded_font_rejected(self):
		doc = self._mk("<style>body{font-family:Inter,sans-serif}</style>")
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_prefers_color_scheme_rejected(self):
		doc = self._mk("<style>@media (prefers-color-scheme: dark){body{color:var(--jd-ink)}}</style>")
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	# ---- Custom escape hatch ------------------------------------------------ #
	def test_custom_relaxes_design_rules(self):
		# off-palette color + hardcoded font are OK under Custom (bespoke look).
		doc = self._insert(
			'<style>.x{color:#0f172a;font-family:Inter}</style><div class="x">y</div>',
			theme="Custom",
		)
		self.assertEqual(doc.theme_schema_version, THEME_SCHEMA_VERSION)

	def test_custom_still_rejects_safety_bans(self):
		font_face = self._mk(
			"<style>@font-face{font-family:X;src:url(data:font/woff2;base64,AA)}</style>", "Custom"
		)
		with self.assertRaises(frappe.ValidationError):
			font_face.insert(ignore_permissions=True)
		external = self._mk('<img src="http://evil.example/x.png">', "Custom")
		with self.assertRaises(frappe.ValidationError):
			external.insert(ignore_permissions=True)

	# ---- F7: theme-switch re-validates (the owner's exact complaint) --------- #
	def test_theme_switch_revalidates_literal(self):
		# Conforms under Jarvis via a literal Jarvis-palette hex (#5d78d1)...
		doc = self._insert("<style>.k{color:#5d78d1}</style>", theme="Jarvis")
		self.assertEqual(doc.theme_schema_version, THEME_SCHEMA_VERSION)
		# ...switching to Insight (a metadata-only save, html unchanged) must
		# RE-LINT and reject — #5d78d1 is off the Insight palette.
		doc.theme = "Insight"
		with self.assertRaises(ThemeStandardError):
			doc.save(ignore_permissions=True)

	def test_theme_switch_token_doc_reskins(self):
		# A token-based doc re-skins cleanly on any theme switch (no literals).
		doc = self._insert("<style>.k{color:var(--jd-ink)}</style>", theme="Jarvis")
		doc.theme = "Insight"
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(DASHBOARD, doc.name, "theme"), "Insight")

	def test_custom_to_curated_switch_revalidates(self):
		# Sonnet P1-1: a Custom doc with hardcoded literals is stamped "current";
		# flipping it to a curated theme must re-lint against that palette.
		doc = self._insert("<style>.x{color:#0f172a}</style>", theme="Custom")
		self.assertEqual(doc.theme_schema_version, THEME_SCHEMA_VERSION)
		doc.theme = "Jarvis"
		with self.assertRaises(ThemeStandardError):
			doc.save(ignore_permissions=True)

	# ---- grandfather (legacy, unstamped) ------------------------------------ #
	def test_legacy_metadata_edit_is_grandfathered(self):
		# A conforming doc, then forced to a legacy state at the DB level: the html
		# is non-conforming and the stamp is cleared (as a pre-standard row would
		# be). A metadata-only edit (rename) must NOT re-validate the body.
		doc = self._insert("<div>legacy</div>")
		frappe.db.set_value(
			DASHBOARD,
			doc.name,
			{"html": "<style>.x{color:#0f172a}</style>", "theme_schema_version": 0},
			update_modified=False,
		)
		reloaded = frappe.get_doc(DASHBOARD, doc.name)
		reloaded.dashboard_title = "renamed-legacy"
		reloaded.save(ignore_permissions=True)  # html unchanged -> grandfathered
		self.assertEqual(frappe.db.get_value(DASHBOARD, doc.name, "dashboard_title"), "renamed-legacy")
		# still legacy — a metadata edit does not stamp it.
		self.assertEqual(frappe.db.get_value(DASHBOARD, doc.name, "theme_schema_version"), 0)

	def test_editing_html_on_legacy_revalidates(self):
		doc = self._insert("<div>legacy</div>")
		frappe.db.set_value(
			DASHBOARD,
			doc.name,
			{"html": "<div>legacy</div>", "theme_schema_version": 0},
			update_modified=False,
		)
		reloaded = frappe.get_doc(DASHBOARD, doc.name)
		reloaded.html = "<style>.x{color:#0f172a}</style>"  # changing html -> re-validate
		with self.assertRaises(frappe.ValidationError):
			reloaded.save(ignore_permissions=True)
