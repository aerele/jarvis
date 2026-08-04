"""Regression tests for the two wiki write-site defects that share
``jarvis.chat.wiki._merge_update_into_page``.

Issue #490 (audience): personal page slugs keyed on the email LOCAL PART only,
and the extraction resolver did not filter on ``target_user`` — so two
colleagues whose addresses scrub alike shared one page. One read the other's
private notes; the other's own notes vanished from every view they had.

Issue #489 (curation): the human-edit fence existed but no voice caller passed
it, so the machine could rewrite a person's edit. Issue #488 already stopped the
body REPLACE on the ingest path; what remained was the summary overwrite, the
head-truncating clip, and nothing marking machine additions on a curated page.

Kept in its own module (not folded into test_wiki.py) so it stays independent of
the other wiki suites.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import wiki
from jarvis.permissions import JARVIS_USER_ROLE

WIKI_DT = "Jarvis Wiki Page"

ALPHA = "Wikifence Alpha Pvt"
ALPHA_SLUG = "customer--wikifence-alpha-pvt"

# Two colleagues whose addresses scrub to the SAME local part. That is the whole
# of issue #490: the audience suffix keys on that local part alone.
TWIN_A = "wikifence-twin@alpha.invalid"
TWIN_B = "wikifence-twin@beta.invalid"
TWIN_BASE_SLUG = "org-notes--wikifence-selling"


def _delete_test_pages():
	frappe.db.delete(WIKI_DT, {"slug": ["like", "%wikifence%"]})
	frappe.db.commit()


def _ensure_twin_users():
	for email in (TWIN_A, TWIN_B):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					# A role-less user flips to Website User; the Jarvis User role
					# both keeps user_type sticky and grants personal-page writes.
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": JARVIS_USER_ROLE}],
				}
			).insert(ignore_permissions=True)


def _make_page(slug, title, page_type="Customer", **kwargs):
	return frappe.get_doc(
		{
			"doctype": WIKI_DT,
			"slug": slug,
			"title": title,
			"page_type": page_type,
			"status": "Active",
			**kwargs,
		}
	).insert(ignore_permissions=True)


def _curated_sources(user="a@test.invalid"):
	"""``sources`` for a page a person edited by hand (what save_wiki_page leaves)."""
	return frappe.as_json([{"date": "2026-01-01", "kind": "manual", "ref": None, "user": user}])


class TestUserScopePageIsolation(FrappeTestCase):
	"""Issue #490."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_twin_users()

	def setUp(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def tearDown(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def _twin_update(self, statement):
		return {
			"slug": TWIN_BASE_SLUG,
			"page_type": "Org",
			"title": "Wikifence notes (selling)",
			"append_md": f"- {statement}",
		}

	def _ingest_as(self, user, statement):
		return wiki.apply_extracted_page_updates(
			[self._twin_update(statement)],
			"voice",
			user,
			default_scope="User",
			target_user=user,
		)

	def test_both_users_derive_the_same_preferred_suffix(self):
		# The precondition the whole issue rests on. Left UNCHANGED on purpose:
		# the preferred slug is what every page created before the fix carries,
		# and renaming those would break the docname they are stored under.
		self.assertEqual(
			wiki.user_scope_slug(TWIN_BASE_SLUG, TWIN_A),
			wiki.user_scope_slug(TWIN_BASE_SLUG, TWIN_B),
		)

	def test_same_local_part_different_domains_get_separate_pages(self):
		self.assertEqual(self._ingest_as(TWIN_A, "ALPHA-PRIVATE-FACT"), (1, 0))
		self.assertEqual(self._ingest_as(TWIN_B, "BETA-PRIVATE-FACT"), (1, 0))

		name_a, slug_a = wiki.resolve_user_scope_page(TWIN_BASE_SLUG, TWIN_A)
		name_b, slug_b = wiki.resolve_user_scope_page(TWIN_BASE_SLUG, TWIN_B)
		self.assertIsNotNone(name_a)
		self.assertIsNotNone(name_b)
		self.assertNotEqual(name_a, name_b)
		self.assertNotEqual(slug_a, slug_b)

		doc_a = frappe.get_doc(WIKI_DT, name_a)
		doc_b = frappe.get_doc(WIKI_DT, name_b)
		self.assertEqual(doc_a.target_user, TWIN_A)
		self.assertEqual(doc_b.target_user, TWIN_B)
		# Neither private statement crossed to the other colleague's page.
		self.assertIn("ALPHA-PRIVATE-FACT", doc_a.body_md)
		self.assertNotIn("BETA-PRIVATE-FACT", doc_a.body_md)
		self.assertIn("BETA-PRIVATE-FACT", doc_b.body_md)
		self.assertNotIn("ALPHA-PRIVATE-FACT", doc_b.body_md)

	def test_user_scope_lookup_never_resolves_another_users_page(self):
		self.assertEqual(self._ingest_as(TWIN_A, "ALPHA-PRIVATE-FACT"), (1, 0))
		claimed = wiki.user_scope_slug(TWIN_BASE_SLUG, TWIN_A)
		self.assertTrue(frappe.db.exists(WIKI_DT, {"slug": claimed, "target_user": TWIN_A}))
		# The second user derives the SAME preferred slug ...
		self.assertEqual(wiki.user_scope_slug(TWIN_BASE_SLUG, TWIN_B), claimed)
		# ... and still resolves to NO page, rather than to the first user's.
		name, _slug = wiki.resolve_user_scope_page(TWIN_BASE_SLUG, TWIN_B)
		self.assertIsNone(name)
		# Writing anyway mints them their own page and leaves the first alone.
		self.assertEqual(self._ingest_as(TWIN_B, "BETA-PRIVATE-FACT"), (1, 0))
		name_b, slug_b = wiki.resolve_user_scope_page(TWIN_BASE_SLUG, TWIN_B)
		self.assertIsNotNone(name_b)
		self.assertNotEqual(slug_b, claimed)
		self.assertEqual(frappe.db.get_value(WIKI_DT, name_b, "target_user"), TWIN_B)
		self.assertNotIn("BETA-PRIVATE-FACT", frappe.db.get_value(WIKI_DT, claimed, "body_md"))

	def test_merge_refuses_a_page_targeted_at_someone_else(self):
		# Belt and braces under the row lock: even handed the wrong docname
		# directly, a User-scope write never lands on another audience's page.
		self.assertEqual(self._ingest_as(TWIN_A, "ALPHA-PRIVATE-FACT"), (1, 0))
		name_a, _ = wiki.resolve_user_scope_page(TWIN_BASE_SLUG, TWIN_A)
		applied = wiki._merge_update_into_page(
			name_a,
			{"append_md": "- BETA-PRIVATE-FACT"},
			"voice",
			TWIN_B,
			None,
			None,
			False,
			False,
			TWIN_B,
		)
		self.assertFalse(applied)
		self.assertNotIn("BETA-PRIVATE-FACT", frappe.get_doc(WIKI_DT, name_a).body_md)

	def test_a_pre_fix_page_keeps_its_slug_and_keeps_receiving_updates(self):
		# The fix renames NOTHING: the slug IS the docname, so a rename would
		# break every stored reference. A page created before the fix keeps the
		# plain --u-<localpart> docname and stays the resolver's target.
		preferred = wiki.user_scope_slug(TWIN_BASE_SLUG, TWIN_A)
		_make_page(
			preferred,
			"Wikifence notes (selling)",
			page_type="Org",
			body_md="- OLDER-FACT",
			scope="User",
			target_user=TWIN_A,
		)
		self.assertEqual(self._ingest_as(TWIN_A, "NEWER-FACT"), (1, 0))
		self.assertEqual(
			frappe.db.count(WIKI_DT, {"target_user": TWIN_A, "slug": ["like", "%wikifence%"]}), 1
		)
		doc = frappe.get_doc(WIKI_DT, preferred)
		self.assertIn("OLDER-FACT", doc.body_md)
		self.assertIn("NEWER-FACT", doc.body_md)

	def test_second_user_is_not_locked_out_of_the_spa_create(self):
		# Before the fix the second colleague got "A page with this slug already
		# exists" naming a page they cannot even read: existence disclosure plus
		# a permanent dead end on that personal base slug.
		frappe.set_user(TWIN_A)
		first = wiki.create_wiki_page(title="Wikifence Twin Notes", page_type="Org", scope="User")
		frappe.set_user(TWIN_B)
		second = wiki.create_wiki_page(title="Wikifence Twin Notes", page_type="Org", scope="User")
		frappe.set_user("Administrator")
		self.assertTrue(first["ok"], first)
		self.assertTrue(second["ok"], second)
		self.assertNotEqual(first["slug"], second["slug"])
		self.assertEqual(frappe.db.get_value(WIKI_DT, first["slug"], "target_user"), TWIN_A)
		self.assertEqual(frappe.db.get_value(WIKI_DT, second["slug"], "target_user"), TWIN_B)
