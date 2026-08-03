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
import pkgutil

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import list_registry
from jarvis.chat.list_filters import build_field_catalog

#: A user holding ONLY the app-access role — the floor every Jarvis customer's
#: ordinary staff sits on. Administrator's catalog is the ceiling; this is the
#: number that actually reaches most people.
FLOOR_USER = "lfr-floor@example.com"

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


def _ensure_floor_user() -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", FLOOR_USER):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": FLOOR_USER,
				"first_name": "lfr-floor",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	doc = frappe.get_doc("User", FLOOR_USER)
	roles = set(frappe.get_roles(FLOOR_USER))
	if "Jarvis User" not in roles:
		doc.add_roles("Jarvis User")
	extra = roles & {"System Manager", "Jarvis Admin", "Jarvis Skill Reviewer"}
	if extra:
		doc.remove_roles(*extra)
	frappe.db.commit()
	frappe.clear_cache(user=FLOOR_USER)
	return FLOOR_USER


#: Customer-facing API packages the discovery sweep walks. `jarvis.chat` was the
#: only one before (P0-04): `jarvis.support.api.list_tickets` lived outside it and
#: could never be discovered. A collection endpoint added to any of these — or
#: annotated anywhere via `list_registry.list_surface_endpoint` — must be
#: classified.
_SWEPT_PACKAGES = ("jarvis.chat", "jarvis.support")

#: Collection-SHAPED name families (S7). The audit used to key ONLY on
#: `list_*`/`admin_list_*`, so a collection endpoint named anything else — a
#: `search_*`, a `*_feed`, a `browse_*`, a `get_*_rows` — evaded discovery
#: entirely and could ship unclassified. Discovery now recognises the whole family
#: of collection verbs/suffixes (a prefix, a suffix, or a whole-word token), so a
#: list endpoint can no longer hide behind its name; the `list_surface_endpoint`
#: annotation remains the escape hatch for a genuinely oddly-named one.
_LIST_NAME_PREFIXES = ("list_", "admin_list_", "search_", "browse_", "recent_", "all_")
_LIST_NAME_SUFFIXES = (
	"_list",
	"_feed",
	"_history",
	"_activity",
	"_runs",
	"_rows",
	"_records",
	"_entries",
	"_results",
	# The house convention for a paginated list getter is `list_<thing>_page`; the
	# `list_` prefix already catches those, but a collection endpoint spelled
	# `get_<thing>_page` (or any `*_page` that is NOT list-prefixed) would slip the
	# net, so the suffix is swept too. This also pulls in the single-document
	# `*_page` CRUD verbs (e.g. `get_wiki_page`), which are then argued as non-lists
	# in `NON_LIST_ENDPOINTS` — exactly the "write down what it is" the audit wants.
	"_page",
)
#: Whole-word tokens (split on `_`) that mark a collection: `search_records`,
#: `get_activity_feed`, `fetch_rows`.
_LIST_NAME_TOKENS = frozenset({"list", "search", "browse", "feed", "history", "activity", "roster"})


def _looks_like_list_endpoint(name: str, fn) -> bool:
	if list_registry.is_list_surface_endpoint(fn):
		return True
	if any(name.startswith(p) for p in _LIST_NAME_PREFIXES):
		return True
	if any(name.endswith(s) for s in _LIST_NAME_SUFFIXES):
		return True
	return bool(set(name.split("_")) & _LIST_NAME_TOKENS)


def _whitelisted_list_callables() -> dict[str, object]:
	"""Every whitelisted list-endpoint candidate across the customer API packages.

	Widened for P0-04 and again for S7: it walks `jarvis.chat` AND `jarvis.support`
	and matches the WHOLE family of collection-shaped names — `list_*`/`admin_list_*`
	plus `search_*`/`browse_*`/`recent_*`/`all_*` prefixes, `_feed`/`_history`/
	`_activity`/`_runs`/`_rows`/`_records`/`_results`/`_entries`/`_list` suffixes, and
	the `list`/`search`/`browse`/`feed`/`history`/`activity`/`roster` whole-word
	tokens — so a collection endpoint can no longer evade discovery by being named
	something other than `list_*` (the round-2 hole). `list_surface_endpoint` remains
	the annotation escape hatch for a genuinely oddly-named one. Import errors are NOT
	swallowed: a module that fails to import would silently shrink the sweep, which is
	exactly the loophole this audit closes."""
	found: dict[str, object] = {}
	for package in _SWEPT_PACKAGES:
		root = importlib.import_module(package)
		for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
			module = importlib.import_module(info.name)
			for name, fn in vars(module).items():
				if not callable(fn) or not _looks_like_list_endpoint(name, fn):
					continue
				if getattr(fn, "__module__", None) != module.__name__:
					continue  # re-export
				if fn in frappe.whitelisted:
					found[f"{module.__name__}.{name}"] = fn
	return found


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

	def test_no_unclassified_list_endpoint_exists(self):
		"""The audit's one remaining hole, closed.

		Everything above proves that what IS registered is well-formed; nothing
		proved that nothing is MISSING. This sweeps every whitelisted
		COLLECTION-SHAPED callable under jarvis/chat and jarvis/support — the full
		name family, not `list_*` alone (S7) — and requires each to be either a
		registered view's endpoint or an explicitly classified non-list. A new list
		endpoint therefore cannot ship without someone writing down what it is, and
		it can no longer slip past by being named `search_*`, `*_feed`, `*_history`
		or the like.
		"""
		registered = {e for v in list_registry.all_views() for e in v.endpoints}
		unclassified = sorted(
			set(_whitelisted_list_callables()) - registered - set(list_registry.NON_LIST_ENDPOINTS)
		)
		self.assertFalse(
			unclassified,
			"whitelisted list endpoints with no classification — register them as a "
			"ListView or add them to list_registry.NON_LIST_ENDPOINTS with a written "
			f"reason: {unclassified}",
		)

	def test_non_list_allowlist_is_argued_and_live(self):
		live = _whitelisted_list_callables()
		for dotted, reason in list_registry.NON_LIST_ENDPOINTS.items():
			with self.subTest(endpoint=dotted):
				self.assertTrue(len(reason) > 20, "an allowlisted endpoint needs a real reason")
				self.assertIn(
					dotted,
					live,
					"stale allowlist entry: this endpoint no longer exists (or is no "
					"longer whitelisted), so the entry is now hiding nothing",
				)

	def test_excluded_fields_name_real_fields(self):
		"""A typo in ``excluded_fields`` protects nothing, silently."""
		for view in list_registry.all_views():
			if not view.excluded_fields or not view.root_doctype:
				continue
			meta = frappe.get_meta(view.root_doctype)
			for spec in view.excluded_fields:
				with self.subTest(view=view.view_key, field=spec):
					if "." in spec:
						child_dt, fieldname = spec.split(".", 1)
						self.assertTrue(frappe.get_meta(child_dt).get_field(fieldname))
					else:
						self.assertTrue(
							meta.get_field(spec),
							f"{view.view_key} excludes {spec!r}, which is not a field of {view.root_doctype}",
						)

	def test_active_views_are_classified_and_migration_status_is_reported(self):
		"""Migration completeness, SEPARATE from inventory and wiring (P0-04).

		Waves 2-3 are deferred, so this round does NOT enforce 'no pending active
		view' — that is the FINAL-round gate. What it enforces now is that every
		ACTIVE (non-excluded) collection is CLASSIFIED (migrated or pending, never
		an unknown status), and it prints the truthful migrated-vs-pending split so
		the handoff cannot claim more than is built.
		"""
		active = [v for v in list_registry.all_views() if not v.is_excluded]
		migrated = [v for v in active if v.status == list_registry.MIGRATED]
		pending = [v for v in active if v.status == list_registry.PENDING]
		for view in active:
			with self.subTest(view=view.view_key):
				self.assertIn(
					view.status,
					{list_registry.MIGRATED, list_registry.PENDING},
					f"{view.view_key} is active but has no migration classification",
				)
		# Every active view is exactly one of the two — nothing unclassified.
		self.assertEqual(len(migrated) + len(pending), len(active))
		print(
			f"\nPlan-08 migration status: {len(migrated)} migrated, {len(pending)} pending, "
			f"of {len(active)} active views."
			f"\n  migrated: {sorted(v.view_key for v in migrated)}"
			f"\n  pending:  {sorted(v.view_key for v in pending)}"
		)

	def test_migrated_views_actually_accept_filters_v2(self):
		"""'migrated' is a claim; this checks the wiring behind it."""
		migrated = list_registry.filterable_views()
		self.assertTrue(migrated, "no surface speaks filters_v2 — the pilots regressed")
		for view in migrated:
			for dotted in view.endpoints:
				with self.subTest(view=view.view_key, endpoint=dotted):
					params = inspect.signature(_resolve(dotted)).parameters
					self.assertIn(
						"filters_v2", params, f"{dotted} claims filters_v2 but has no such parameter"
					)
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
				catalog = build_field_catalog(view.root_doctype, user="Administrator", view=view)
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

	def test_floor_role_catalog(self):
		"""U1-2: the same comparison at the FLOOR role, not just Administrator.

		Administrator holds every role, so its catalog is the ceiling — a width
		claim measured there can be wildly wrong for the person actually using
		the list. This runs the audit as a user holding only ``Jarvis User`` and
		reports the real per-surface number, including the surfaces where that
		user sees NOTHING (reviewer/admin-gated queues), which is itself the
		answer to 'what does filtering look like for ordinary staff'.
		"""
		floor = _ensure_floor_user()
		report: list[str] = []
		for view in list_registry.document_list_views():
			with self.subTest(view=view.view_key):
				catalog = build_field_catalog(view.root_doctype, user=floor, view=view)
				main = {f["fieldname"] for f in catalog if not f["is_child"]}
				if not main:
					# No level-0 read for the floor role: the surface is gated to
					# reviewers/admins. Correct, and worth reporting.
					report.append(f"  {view.view_key:<28} floor_main=  0  (no access at the floor role)")
					continue
				curated = {v for v in view.curated_filters.values() if v}
				missing = curated - main
				self.assertFalse(
					missing,
					f"{view.view_key} curates {sorted(missing)}, which the floor role "
					f"cannot read — the CURRENT endpoint filters on a field the "
					f"migrated schema would have to withhold, so migrating it would "
					f"silently change behaviour for ordinary users",
				)
				report.append(
					f"  {view.view_key:<28} floor_main={len(main):>3}  "
					f"curated={len(curated):>2}  gap={len(main) - len(curated):>3}"
				)
		print("\nFloor-role (Jarvis User only) filter width:\n" + "\n".join(report))

	def test_view_excluded_fields_are_withheld_from_the_catalog(self):
		"""P1: a view's own withholding list actually removes fields."""
		for view in list_registry.document_list_views():
			if not view.excluded_fields:
				continue
			with self.subTest(view=view.view_key):
				withheld = build_field_catalog(view.root_doctype, user="Administrator", view=view)
				unfiltered = build_field_catalog(view.root_doctype, user="Administrator")
				names = {(f["doctype"], f["fieldname"]) for f in withheld}
				all_names = {(f["doctype"], f["fieldname"]) for f in unfiltered}
				for spec in view.excluded_fields:
					key = tuple(spec.split(".", 1)) if "." in spec else (view.root_doctype, spec)
					# Non-vacuity: the field IS otherwise catalogable.
					self.assertIn(key, all_names, f"{spec} was never in the catalog — test is vacuous")
					self.assertNotIn(key, names, f"{view.view_key} failed to withhold {spec}")

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
