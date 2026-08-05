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

from unittest.mock import patch

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


class TestVoiceIngestPreservesHumanEdits(FrappeTestCase):
	"""Issue #489."""

	def setUp(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def tearDown(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def _voice(self, update):
		"""One voice write, shaped exactly like the ingest's own call."""
		return wiki.apply_extracted_page_updates(
			[update],
			"voice",
			"b@test.invalid",
			allow_body_replace=False,
			preserve_curated=True,
		)

	def test_human_edit_survives_a_later_voice_ingest(self):
		_make_page(ALPHA_SLUG, ALPHA, body_md="## Payment\n\nMachine guess.")
		wiki.save_wiki_page(
			slug=ALPHA_SLUG,
			body_md="## Payment\n\nHUMAN-AUTHORED net 30, no exceptions.",
			summary="HUMAN-SUMMARY",
		)
		applied, failed = self._voice(
			{
				"slug": ALPHA_SLUG,
				"body_md": "## Payment\n\nMACHINE-PARAPHRASE 45 days.",
				"summary": "MACHINE-SUMMARY",
			}
		)
		self.assertEqual((applied, failed), (1, 0))
		doc = frappe.get_doc(WIKI_DT, ALPHA_SLUG)
		# The summary REPLACES, so it is the field that still destroyed human
		# text after issue #488 turned the body into an append. Asserted FIRST
		# because it is the only surviving data loss, not a presentation detail.
		self.assertEqual(doc.summary, "HUMAN-SUMMARY")
		# The human's words survive, the new knowledge still lands, and a reader
		# can tell which is which.
		self.assertIn("HUMAN-AUTHORED net 30", doc.body_md)
		self.assertIn("MACHINE-PARAPHRASE 45 days", doc.body_md)
		self.assertIn("## Added by Jarvis from a note (", doc.body_md)

	def test_summary_is_still_filled_when_the_human_left_it_empty(self):
		_make_page(ALPHA_SLUG, ALPHA, body_md="Human body.", sources=_curated_sources())
		applied, failed = self._voice({"slug": ALPHA_SLUG, "append_md": "- New.", "summary": "Filled in."})
		self.assertEqual((applied, failed), (1, 0))
		self.assertEqual(frappe.get_doc(WIKI_DT, ALPHA_SLUG).summary, "Filled in.")

	def test_a_curated_page_is_never_truncated_from_the_head(self):
		# _clip_body keeps the TAIL, so clipping an over-cap append silently
		# deletes the human's OLDEST lines. Refuse the update instead: dropping
		# one note's knowledge is recoverable, deleting a person's text is not.
		body = "HUMAN-HEAD-SENTINEL\n" + ("h" * (wiki.MAX_BODY_LEN - 200))
		_make_page(ALPHA_SLUG, ALPHA, body_md=body, sources=_curated_sources())
		applied, failed = self._voice({"slug": ALPHA_SLUG, "append_md": "m" * 500})
		self.assertEqual((applied, failed), (0, 0))
		doc = frappe.get_doc(WIKI_DT, ALPHA_SLUG)
		self.assertTrue(doc.body_md.startswith("HUMAN-HEAD-SENTINEL"))
		self.assertEqual(doc.body_md, body)

	def test_the_ingest_still_owns_the_pages_it_created_itself(self):
		# Why provenance_prefix="voice" cannot be the fix: "voice" is in
		# _HUMAN_SOURCE_KINDS AND is what the ingest stamps on every page it
		# writes, so that fence would refuse its own pages from note two onward.
		created = self._voice(
			{
				"slug": ALPHA_SLUG,
				"page_type": "Customer",
				"title": ALPHA,
				"body_md": "- First note.",
			}
		)
		self.assertEqual(created, (1, 0))
		self.assertEqual(self._voice({"slug": ALPHA_SLUG, "append_md": "- Second note."}), (1, 0))
		doc = frappe.get_doc(WIKI_DT, ALPHA_SLUG)
		self.assertIn("- First note.", doc.body_md)
		self.assertIn("- Second note.", doc.body_md)
		# No attribution heading: nobody curated this page, it is the scribe's.
		self.assertNotIn("## Added by Jarvis from a note (", doc.body_md)
		self.assertFalse(wiki._sources_are_curated(doc.sources))

	def test_the_agents_own_tool_writes_do_not_count_as_curation(self):
		# "tool" (update_wiki) and "chat" name machine-composed text, so they
		# stay OUT of the curated set even though _HUMAN_SOURCE_KINDS holds them
		# for the stricter app-learning fence.
		self.assertFalse(wiki._sources_are_curated(frappe.as_json([{"kind": "tool"}, {"kind": "chat"}])))
		self.assertTrue(wiki._sources_are_curated(frappe.as_json([{"kind": "promotion"}])))
		# Unreadable provenance fails CLOSED, toward preserving text.
		self.assertTrue(wiki._sources_are_curated("{not json"))

	def test_app_learning_fence_is_unchanged(self):
		# The scribe's fence keeps BOTH halves: it refreshes its own page in
		# place, and it refuses outright once a human has touched it.
		_make_page(
			ALPHA_SLUG,
			ALPHA,
			body_md="SCRIBE-V1",
			sources=frappe.as_json(
				[{"date": "2026-01-01", "kind": "app-learning-agent:acme", "ref": None, "user": None}]
			),
		)
		applied, failed = wiki.apply_extracted_page_updates(
			[{"slug": ALPHA_SLUG, "body_md": "SCRIBE-V2"}],
			"app-learning-agent:acme",
			"scribe@test.invalid",
			provenance_prefix="app-learning",
		)
		self.assertEqual((applied, failed), (1, 0))
		doc = frappe.get_doc(WIKI_DT, ALPHA_SLUG)
		self.assertEqual(doc.body_md, "SCRIBE-V2")
		self.assertNotIn("SCRIBE-V1", doc.body_md)

		wiki.save_wiki_page(slug=ALPHA_SLUG, body_md="HUMAN-BODY")
		applied, failed = wiki.apply_extracted_page_updates(
			[{"slug": ALPHA_SLUG, "body_md": "SCRIBE-V3"}],
			"app-learning-agent:acme",
			"scribe@test.invalid",
			provenance_prefix="app-learning",
		)
		self.assertEqual((applied, failed), (0, 0))
		self.assertEqual(frappe.get_doc(WIKI_DT, ALPHA_SLUG).body_md, "HUMAN-BODY")

	def test_callers_without_the_flag_are_byte_for_byte_unchanged(self):
		# preserve_curated defaults off, so no existing caller changes shape.
		_make_page(
			ALPHA_SLUG,
			ALPHA,
			body_md="HUMAN-BODY",
			summary="HUMAN-SUMMARY",
			sources=_curated_sources(),
		)
		applied, failed = wiki.apply_extracted_page_updates(
			[{"slug": ALPHA_SLUG, "body_md": "MACHINE-BODY", "summary": "MACHINE-SUMMARY"}],
			"voice",
			"b@test.invalid",
		)
		self.assertEqual((applied, failed), (1, 0))
		doc = frappe.get_doc(WIKI_DT, ALPHA_SLUG)
		self.assertEqual(doc.body_md, "MACHINE-BODY")
		self.assertEqual(doc.summary, "MACHINE-SUMMARY")

	def test_the_note_ingest_passes_the_fence(self):
		# The defect was purely a caller omission, so assert the caller.
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "wikifence"}).insert(
			ignore_permissions=True
		)
		note = frappe.get_doc(
			{
				"doctype": "Jarvis Voice Note",
				"transcript": "Wikifence fence wiring.",
				"context_type": "Conversation",
				"conversation": conv.name,
				"entities": frappe.as_json([]),
				"source": "Chat Nudge",
				"status": "New",
			}
		).insert(ignore_permissions=True)
		try:
			with patch("jarvis.chat.wiki._extract_page_updates", return_value=[]):
				with patch(
					"jarvis.chat.wiki.apply_extracted_page_updates", return_value=(0, 0)
				) as apply_mock:
					wiki._ingest_note(note.name)
		finally:
			frappe.db.delete("Jarvis Voice Note", {"name": note.name})
			frappe.db.delete("Jarvis Conversation", {"name": conv.name})
		self.assertTrue(apply_mock.call_args.kwargs["preserve_curated"])
		self.assertFalse(apply_mock.call_args.kwargs["allow_body_replace"])

	def test_the_personalise_ingest_passes_the_fence(self):
		from jarvis.learning import voice_facts

		facts = [{"domain": "selling", "statement": "Wikifence personalise fact."}]
		with patch.object(wiki, "apply_extracted_page_updates", return_value=(0, 0)) as apply_mock:
			voice_facts._apply_personalise_context(facts, "b@test.invalid", ref="NOTE-1")
		self.assertTrue(apply_mock.call_args.kwargs["preserve_curated"])
		self.assertEqual(apply_mock.call_args.kwargs["target_user"], "b@test.invalid")


class TestRefusedUpdateIsNotSilent(FrappeTestCase):
	"""#613: a refused update counted as NEITHER applied nor failed and left no trace.

	``_ingest_note`` retries only on ``failed``, so an all-refused batch returned (0, 0),
	the voice note was marked Processed with "nothing durable found", and the knowledge
	was gone for good with nothing for the tenant to look at. PR #611 made the refusing
	shape ordinary rather than rare by telling the ingest to emit ``append_md``."""

	def setUp(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def tearDown(self):
		frappe.set_user("Administrator")
		_delete_test_pages()

	def _voice(self, update):
		return wiki.apply_extracted_page_updates(
			[update], "voice", "b@test.invalid", allow_body_replace=False, preserve_curated=True
		)

	def test_a_refusal_still_reports_the_documented_tuple(self):
		"""The counting half of #613 is NOT taken here. ``test_wiki`` pins this contract
		with an explicit comment ("a skipped (identity-less) update is not a FAILURE"),
		and flipping it would retry unsalvageable input forever: one refusal that pins it
		is slug ``"!!!"``, which no retry repairs, and the note carries no attempt counter
		to bound that. This test exists so the contract is not changed by accident."""
		applied, failed = self._voice({"slug": "no-such-page-here", "append_md": "Real knowledge."})
		self.assertEqual((applied, failed), (0, 0))

	def test_a_refusal_is_logged_with_the_shape_that_caused_it(self):
		with patch.object(wiki.frappe, "log_error") as logged:
			self._voice({"slug": "no-such-page-here", "append_md": "Real knowledge."})
		self.assertTrue(logged.called, "a refusal must leave a trace to diagnose")
		msg = " ".join(str(c.kwargs.get("message", "")) for c in logged.call_args_list)
		self.assertIn("has_title=False", msg)
		self.assertIn("append_md", msg, "the log should say what the update DID carry")

	def test_the_refusal_log_does_not_leak_the_notes_content(self):
		"""A voice note is the customer's own words. Field names and presence only."""
		secret = "PATIENT-ZERO-CONFIDENTIAL-BODY"
		with patch.object(wiki.frappe, "log_error") as logged:
			self._voice({"slug": "no-such-page-here", "append_md": secret})
		msg = " ".join(str(c.kwargs.get("message", "")) for c in logged.call_args_list)
		self.assertNotIn(secret, msg)

	def test_the_refusal_log_does_not_echo_page_type_or_scope(self):
		"""These are the fields MOST likely to carry a stray transcript fragment: the
		helper runs precisely when the model failed to produce a valid enum there. So they
		are classified, never echoed."""
		leak = "TRANSCRIPT-FRAGMENT-THE-MODEL-DUMPED"
		with patch.object(wiki.frappe, "log_error") as logged:
			self._voice({"slug": "no-such-page-here", "page_type": leak, "scope": leak})
		msg = " ".join(str(c.kwargs.get("message", "")) for c in logged.call_args_list)
		self.assertNotIn(leak, msg)
		self.assertIn("page_type=invalid", msg, "the diagnosis must survive the redaction")

	def test_a_fenced_refusal_is_not_logged(self):
		"""On the app-learning / scribe paths a refusal is the DOCUMENTED expected outcome
		of colliding with a human-edited page (the CA2-1 belt), not knowledge going
		missing. Logging those would put a row in the Error Log for every routine collision
		on every rerun and bury the entries this fix exists to surface."""
		with patch.object(wiki.frappe, "log_error") as logged:
			wiki.apply_extracted_page_updates(
				[{"slug": "no-such-page-here", "append_md": "Learned from source."}],
				"app-learning:someapp",
				"b@test.invalid",
				provenance_prefix="app-learning:",
			)
		self.assertFalse(logged.called, "an expected fence refusal must not spam the Error Log")

	def test_a_valid_update_is_unaffected(self):
		"""Control: the ordinary path still reports (1, 0) and logs nothing."""
		_make_page(ALPHA_SLUG, ALPHA, body_md="Existing.")
		with patch.object(wiki.frappe, "log_error") as logged:
			applied, failed = self._voice({"slug": ALPHA_SLUG, "append_md": "More knowledge."})
		self.assertEqual((applied, failed), (1, 0))
		self.assertFalse(logged.called)

	def test_a_creatable_update_still_creates(self):
		"""Control: an update carrying an identity mints the page rather than refusing."""
		applied, failed = self._voice(
			{
				"slug": "customer--wikifence-newly-minted",
				"title": "Wikifence Newly Minted",
				"page_type": "Customer",
				"append_md": "First fact.",
			}
		)
		self.assertEqual((applied, failed), (1, 0))
