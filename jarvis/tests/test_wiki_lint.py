"""Tests for the wiki health check (``jarvis.learning.wiki_lint``):
deterministic checks over seeded pages (contradictions, stale, orphans,
near-duplicate titles), the no-key silent skip of the LLM confirm pass, the
confirmed-contradiction flag stamping (restricted to the suspect set) and the
Jarvis Settings RO stamps.

``jarvis.chat.voice`` is mocked at its two seams (``_credentials`` /
``openrouter_complete``) — no network. Fixtures are swept by slug prefix in
tearDown (the lint stamps settings with a commit, so rollback isn't enough).
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jarvis.learning import wiki_lint

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

SLUG_PREFIX = "linttest"

_SETTINGS_FIELDS = ("wiki_lint_last_run_at", "wiki_lint_summary")


@contextlib.contextmanager
def _mock_llm(key="", reply=None):
	"""Patch voice._credentials (key resolution) + openrouter_complete.
	``key=""`` = no OpenRouter key anywhere -> the confirm pass must skip."""
	kwargs = {"side_effect": reply} if isinstance(reply, (Exception, list)) else {"return_value": reply}
	with (
		mock.patch("jarvis.chat.voice._credentials", return_value=(key, "test-model")),
		mock.patch("jarvis.chat.voice.openrouter_complete", **kwargs) as complete,
	):
		yield complete


class WikiLintTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		singles = frappe.db.get_singles_dict(SETTINGS)
		self._settings_before = {f: singles.get(f) for f in _SETTINGS_FIELDS}

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(WIKI, {"slug": ["like", f"{SLUG_PREFIX}%"]})
		for field, value in self._settings_before.items():
			frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
		frappe.db.commit()
		super().tearDown()

	def _page(
		self,
		slug,
		title=None,
		page_type="Customer",
		body="Body.",
		scope=None,
		status="Active",
		contradiction_flag=0,
		last_confirmed_at=None,
		manual_links=None,
	):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": f"{SLUG_PREFIX}--{slug}",
				"title": title or f"Lint {slug}",
				"page_type": page_type,
				"body_md": body,
				"scope": scope,
				"status": status,
				"contradiction_flag": contradiction_flag,
			}
		)
		if manual_links is not None:
			doc.manual_links = json.dumps(manual_links)
		doc.insert(ignore_permissions=True)
		if last_confirmed_at:
			frappe.db.set_value(
				WIKI,
				doc.name,
				"last_confirmed_at",
				last_confirmed_at,
				update_modified=False,
			)
		frappe.db.commit()
		return doc

	def _seed(self):
		"""One page per failure mode + a healthy, referenced one."""
		pages = frappe._dict()
		# beta is referenced by alpha -> beta not orphan, alpha orphan
		pages.alpha = self._page("alpha", body=f"Links to [[{SLUG_PREFIX}--beta]] for details.")
		pages.beta = self._page("beta")
		# contradiction via body marker (flag cleared by a human save); also
		# links beta so the corpus has >=2 linking pages — the orphan check
		# reports nothing on younger wikis (deliberate noise gate)
		pages.contra = self._page(
			"contra",
			body=(
				"Terms are 30 days (see [[" + SLUG_PREFIX + "--beta]])."
				"\n\n## Contradiction flagged (2026-01-01)\n\nTerms are 45 days."
			),
		)
		# contradiction via the stored flag
		pages.flagged = self._page("flagged", contradiction_flag=1)
		# stale: confirmed long ago
		pages.stale = self._page("stale", last_confirmed_at="2020-01-01 00:00:00")
		# near-duplicate titles (normalized collision)
		pages.dupa = self._page("dupa", title="Acme Corp")
		pages.dupb = self._page("dupb", title="acme  CORP!!")
		return pages


class TestDeterministicChecks(WikiLintTestCase):
	def test_seeded_pages_yield_expected_issues(self):
		pages = self._seed()
		with _mock_llm(key="") as complete:
			out = wiki_lint.run_lint()
		complete.assert_not_called()

		self.assertTrue(out["ok"])
		self.assertFalse(out["llm_checked"])

		self.assertIn(pages.contra.name, out["contradictions"])
		self.assertIn(pages.flagged.name, out["contradictions"])
		self.assertNotIn(pages.beta.name, out["contradictions"])

		self.assertIn(pages.stale.name, out["stale"])
		self.assertNotIn(pages.beta.name, out["stale"])

		# beta has an inbound [[link]] from alpha; alpha has none
		self.assertNotIn(pages.beta.name, out["orphans"])
		self.assertIn(pages.alpha.name, out["orphans"])

		dupe_groups = [set(g) for g in out["duplicate_titles"]]
		self.assertIn({pages.dupa.name, pages.dupb.name}, dupe_groups)

		self.assertGreaterEqual(out["counts"]["contradictions"], 2)
		self.assertGreaterEqual(out["counts"]["stale"], 1)
		self.assertGreaterEqual(out["counts"]["duplicate_title_groups"], 1)
		self.assertTrue(out["issues"])
		self.assertTrue(out["summary"])

	def test_non_org_and_archived_pages_are_ignored(self):
		user_page = self._page(
			"private",
			scope="User",
			body="## Contradiction flagged (2026-01-01)\n\nConflict.",
			contradiction_flag=1,
		)
		archived = self._page("bygone", status="Archived", contradiction_flag=1)
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertNotIn(user_page.name, out["contradictions"])
		self.assertNotIn(archived.name, out["contradictions"])
		self.assertNotIn(user_page.name, out["orphans"])

	def test_self_reference_does_not_rescue_an_orphan(self):
		selfie = self._page("selfie", body=f"I cite myself: [[{SLUG_PREFIX}--selfie]].")
		# two real linking pages so the young-wiki orphan gate is open
		self._page("linker-a", body=f"See [[{SLUG_PREFIX}--linker-b]].")
		self._page("linker-b", body=f"Back to [[{SLUG_PREFIX}--linker-a]].")
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertIn(selfie.name, out["orphans"])

	def test_orphans_not_reported_on_young_unlinked_wiki(self):
		# a corpus where nobody links anybody: flagging everything as orphans
		# is noise, so the check reports nothing
		self._page("lone-a")
		self._page("lone-b")
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertEqual(out["orphans"], [])

	def test_curated_inbound_link_rescues_an_orphan(self):
		"""#494: add_wiki_link stores curated links in manual_links, never in
		body_md. A body-only orphan scan therefore kept reporting the target as
		an orphan while the Evolution tab (which unions both) showed it linked."""
		adopted = self._page("adopted")
		# body_md says nothing about `adopted`; only the curated store does.
		self._page("curator", body="No wikilinks here at all.", manual_links=[adopted.name])
		# second linking page so the young-wiki orphan gate is open
		self._page("linker-a", body=f"See [[{SLUG_PREFIX}--linker-b]].")
		self._page("linker-b", body=f"Back to [[{SLUG_PREFIX}--linker-a]].")
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertNotIn(adopted.name, out["orphans"])

	def test_curated_links_alone_open_the_young_wiki_orphan_gate(self):
		"""A wiki linked entirely by curation still counts as "linking is
		practiced" — otherwise the gate silently disables the whole check."""
		a = self._page("gate-a")
		b = self._page("gate-b")
		lonely = self._page("gate-lonely")
		self._page("gate-c", manual_links=[a.name])
		self._page("gate-d", manual_links=[b.name])
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertIn(lonely.name, out["orphans"])
		self.assertNotIn(a.name, out["orphans"])
		self.assertNotIn(b.name, out["orphans"])

	def test_curated_self_link_does_not_rescue_an_orphan(self):
		selfie = self._page("curated-selfie")
		frappe.db.set_value(WIKI, selfie.name, "manual_links", json.dumps([selfie.name]))
		frappe.db.commit()
		self._page("linker-a", body=f"See [[{SLUG_PREFIX}--linker-b]].")
		self._page("linker-b", body=f"Back to [[{SLUG_PREFIX}--linker-a]].")
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		self.assertIn(selfie.name, out["orphans"])

	def test_settings_fields_are_stamped(self):
		self._seed()
		with _mock_llm(key=""):
			out = wiki_lint.run_lint()
		singles = frappe.db.get_singles_dict(SETTINGS)
		self.assertTrue(singles.get("wiki_lint_last_run_at"))
		self.assertEqual(singles.get("wiki_lint_summary"), out["summary"])
		self.assertLessEqual(len(out["summary"]), 400)


class TestLlmConfirmPass(WikiLintTestCase):
	def test_no_key_skips_llm_and_never_flags(self):
		pages = self._seed()
		with _mock_llm(key="") as complete:
			out = wiki_lint.run_lint()
		complete.assert_not_called()
		self.assertFalse(out["llm_checked"])
		self.assertEqual(out["confirmed_contradictions"], [])
		# the deterministic pass alone never overrides a human's flag clear
		self.assertEqual(cint(frappe.db.get_value(WIKI, pages.contra.name, "contradiction_flag")), 0)

	def test_confirmed_contradiction_sets_flag(self):
		pages = self._seed()
		reply = frappe.as_json(
			[
				{"slug": pages.contra.name, "kind": "contradiction", "confirmed": True},
				{"slug": pages.flagged.name, "kind": "contradiction", "confirmed": False},
				{"slug": pages.dupa.name, "kind": "duplicate", "confirmed": True},
				# out-of-suspect-set output must be discarded (untrusted model)
				{"slug": pages.beta.name, "kind": "contradiction", "confirmed": True},
			]
		)
		with _mock_llm(key="sk-test", reply=reply) as complete:
			out = wiki_lint.run_lint()
		complete.assert_called_once()
		self.assertTrue(out["llm_checked"])
		self.assertEqual(out["confirmed_contradictions"], [pages.contra.name])
		self.assertIn(pages.dupa.name, out["confirmed_duplicates"])
		self.assertEqual(cint(frappe.db.get_value(WIKI, pages.contra.name, "contradiction_flag")), 1)
		self.assertEqual(cint(frappe.db.get_value(WIKI, pages.beta.name, "contradiction_flag")), 0)

	def test_llm_failure_degrades_to_deterministic_results(self):
		pages = self._seed()
		with _mock_llm(key="sk-test", reply=frappe.ValidationError("openrouter down")):
			out = wiki_lint.run_lint()
		self.assertTrue(out["ok"])
		self.assertFalse(out["llm_checked"])
		self.assertIn(pages.contra.name, out["contradictions"])

	def test_unparseable_llm_output_degrades(self):
		self._seed()
		with _mock_llm(key="sk-test", reply="I cannot help with that."):
			out = wiki_lint.run_lint()
		self.assertTrue(out["ok"])
		self.assertFalse(out["llm_checked"])

	def test_no_suspects_skips_llm_even_with_key(self):
		self._page("solo")
		with _mock_llm(key="sk-test") as complete:
			out = wiki_lint.run_lint()
		if out["counts"]["contradictions"] or out["counts"]["duplicate_title_groups"]:
			self.skipTest("pre-existing suspect pages on this site")
		complete.assert_not_called()
		self.assertFalse(out["llm_checked"])


class TestScheduledLint(WikiLintTestCase):
	def test_scheduled_lint_swallows_errors(self):
		with mock.patch.object(wiki_lint, "run_lint", side_effect=Exception("boom")):
			wiki_lint.scheduled_lint()  # must not raise

	def test_scheduled_lint_runs_when_wiki_enabled(self):
		with (
			mock.patch("jarvis.chat.wiki.wiki_enabled", return_value=True),
			mock.patch.object(wiki_lint, "run_lint") as run,
		):
			wiki_lint.scheduled_lint()
		run.assert_called_once()

	def test_scheduled_lint_skips_when_wiki_disabled(self):
		with (
			mock.patch("jarvis.chat.wiki.wiki_enabled", return_value=False),
			mock.patch.object(wiki_lint, "run_lint") as run,
		):
			wiki_lint.scheduled_lint()
		run.assert_not_called()
