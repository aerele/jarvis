"""Phase-0 audit of the document-collection registry (plan 08 §10, C08-7).

Two jobs.

**Integrity.** Every registered surface is well-formed: a known classification, a
resolvable whitelisted endpoint, a real root DocType, and — for anything
classified as NOT a document list — a written reason. Plus a fixed expected-key
sweep, so deleting a surface from the registry to silence the audit is itself a
failure.

**The width gap.** Codex's Phase 0 asked for failing evidence that the curated,
page-authored filter definitions are incomplete. C08-7 reformulates that as a
SUBSET assertion rather than a red test: for every doctype-backed view, derive
the filterable field set independently from live metadata and assert the CURRENT
curated whitelist is a strict subset of it. The audit therefore passes today and
documents the gap deterministically (it prints the per-surface counts); the
equality assertion arrives per surface, as each one migrates.
"""

from __future__ import annotations

import importlib
import inspect

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import list_registry
from jarvis.chat.list_filters import build_field_catalog

#: Every surface that must be registered. A rename is fine; a disappearance is
#: not. Mirrors plan 08 §3.3 + the 8 ListPage consumers + the Settings surfaces,
#: minus nothing.
EXPECTED_VIEW_KEYS = {
	# 8 shared-ListPage consumers
	"skills",
	"macros",
	"file_box",
	"saved_dashboards",
	"wiki_pages",
	"support_tickets",
	"triggers",
	"trigger_activity",
	# bespoke collection surfaces (§3.3)
	"agents_catalog",
	"agent_activity",
	"agent_runs",
	"agent_findings",
	"agent_admin_overview",
	"approvals",
	"macro_runs",
	"personalise_questions",
	"personalise_notes",
	"learning_queue",
	"learning_decided",
	"wiki_promotions",
	"skill_promotions",
	# Settings area
	"settings_user_usage_admin",
	"settings_tool_activity",
	# explicit non-lists / dormant
	"chat_conversation_sidebar",
	"app_learning_runs",
}


def _resolve(dotted: str):
	module_path, _, attr = dotted.rpartition(".")
	module = importlib.import_module(module_path)
	return getattr(module, attr)


class TestListRegistryIntegrity(FrappeTestCase):
	def test_expected_surfaces_are_registered(self):
		registered = {v.view_key for v in list_registry.all_views()}
		missing = EXPECTED_VIEW_KEYS - registered
		self.assertFalse(missing, f"surfaces dropped from the registry: {sorted(missing)}")

	def test_view_keys_are_unique(self):
		keys = [v.view_key for v in list_registry.all_views()]
		self.assertEqual(len(keys), len(set(keys)), "duplicate view_key in the registry")

	def test_every_view_is_well_formed(self):
		for view in list_registry.all_views():
			with self.subTest(view=view.view_key):
				self.assertTrue(view.view_key and view.label)
				self.assertIn(view.classification, list_registry.CLASSIFICATIONS)
				self.assertIn(view.status, list_registry.STATUSES)
				self.assertTrue(view.endpoints, "a surface with no endpoint cannot be audited")
				if view.is_excluded:
					# C08-7 / plan §3.3: the semantic boundary is only defensible
					# if every exclusion is argued in writing.
					self.assertTrue(
						len(view.reason) > 40,
						"an excluded surface needs a real reason, not a label",
					)
					self.assertEqual(view.status, list_registry.NOT_APPLICABLE)
				else:
					self.assertIn(view.status, {list_registry.MIGRATED, list_registry.PENDING})

	def test_every_endpoint_resolves_and_is_whitelisted(self):
		for view in list_registry.all_views():
			for dotted in view.endpoints:
				with self.subTest(view=view.view_key, endpoint=dotted):
					fn = _resolve(dotted)
					self.assertTrue(callable(fn), f"{dotted} is not callable")
					self.assertIn(
						fn,
						frappe.whitelisted,
						f"{dotted} is registered as a list endpoint but is not whitelisted",
					)

	def test_document_list_views_have_a_real_root_doctype(self):
		for view in list_registry.document_list_views():
			with self.subTest(view=view.view_key):
				self.assertTrue(
					frappe.db.exists("DocType", view.root_doctype),
					f"{view.view_key} points at a DocType that does not exist",
				)

	def test_projection_views_declare_their_authority(self):
		for view in list_registry.all_views():
			if view.classification != list_registry.PROJECTION:
				continue
			with self.subTest(view=view.view_key):
				self.assertTrue(
					view.projection_root or view.projection_authority,
					"a projection must say what it projects from",
				)

	def test_migrated_views_actually_accept_filters_v2(self):
		"""'migrated' is a claim; this checks the wiring behind it."""
		migrated = list_registry.filterable_views()
		self.assertTrue(migrated, "no surface speaks filters_v2 — the pilots regressed")
		for view in migrated:
			for dotted in view.endpoints:
				with self.subTest(view=view.view_key, endpoint=dotted):
					params = inspect.signature(_resolve(dotted)).parameters
					self.assertIn("filters_v2", params, f"{dotted} claims filters_v2 but has no such parameter")
					self.assertIn(
						"filters",
						params,
						f"{dotted} dropped the legacy argument — the compatibility window is not over",
					)


class TestCuratedFilterWidthGap(FrappeTestCase):
	"""C08-7: schema COMPLETENESS, not button presence.

	The Filter action is already universally present; what is missing is width.
	For each doctype-backed surface we rebuild the metadata catalog from scratch
	(as Administrator, so the comparison is against the widest possible set) and
	assert the page-curated whitelist is a strict subset of it.
	"""

	def test_curated_filters_are_a_strict_subset_of_the_metadata_catalog(self):
		report: list[str] = []
		for view in list_registry.document_list_views():
			with self.subTest(view=view.view_key):
				catalog = build_field_catalog(view.root_doctype, user="Administrator")
				self.assertTrue(catalog, f"{view.root_doctype} produced an empty catalog")
				main = {f["fieldname"] for f in catalog if not f["is_child"]}
				child = {(f["doctype"], f["fieldname"]) for f in catalog if f["is_child"]}

				curated = {v for v in view.curated_filters.values() if v}
				unknown = curated - main
				self.assertFalse(
					unknown,
					f"{view.view_key} curates filter keys that are not metadata fields "
					f"of {view.root_doctype}: {sorted(unknown)} — either the mapping in "
					f"list_registry is wrong or the field was renamed",
				)
				self.assertLess(
					len(curated),
					len(main),
					f"{view.view_key} is no longer a strict subset — if it genuinely "
					f"reached parity, move it to the equality assertion",
				)
				report.append(
					f"  {view.view_key:<28} curated={len(curated):>2}  "
					f"metadata_main={len(main):>3}  child={len(child):>3}  "
					f"gap={len(main) - len(curated):>3}"
				)
		print("\nPhase-0 filter width gap (curated vs metadata):\n" + "\n".join(report))

	def test_catalog_never_leaks_structural_or_secret_fields(self):
		from jarvis.chat.list_filters import NO_VALUE_FIELDTYPES, SECRET_FIELDTYPES

		for view in list_registry.document_list_views():
			with self.subTest(view=view.view_key):
				for entry in build_field_catalog(view.root_doctype, user="Administrator"):
					self.assertNotIn(entry["fieldtype"], NO_VALUE_FIELDTYPES)
					self.assertNotIn(entry["fieldtype"], SECRET_FIELDTYPES)

	def test_docstatus_only_for_submittable_doctypes(self):
		for view in list_registry.document_list_views():
			with self.subTest(view=view.view_key):
				names = {f["fieldname"] for f in build_field_catalog(view.root_doctype, user="Administrator")}
				submittable = bool(frappe.get_meta(view.root_doctype).is_submittable)
				self.assertEqual("docstatus" in names, submittable)
