"""Tests for the Custom App Learning *scribe* delegate's three tools + its gate.

Covers the load-bearing security posture of the replacement for the chat-batch
custom-app learning pipeline:

  * the SELF-GATE on the source-read + wiki-writeback tools: they serve ONLY a
    running app-learning **scribe** ``Jarvis Agent Run`` resolved from the
    caller's session_key (never a model id), and ONLY to an admin-tier run-as
    identity (``record_agent_run`` parity);
  * the custom-apps ALLOWLIST (core apps are never served);
  * the realpath-containment + symlink guard surviving into ``read_app_source``;
  * the per-run source-BYTES budget;
  * ``record_app_wiki`` applying pages through the ``apply_extracted_page_updates``
    funnel, being in ``_WRITE_TOOLS`` but NOT ``_GATED_WRITES`` (audited, never a
    confirm card), with deterministic app-prefixed slugs so a re-run UPDATES in
    place instead of duplicating;
  * the ``run_agent_now`` nature gate now admitting Scribe (auditor stays valid,
    operator still refused).

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_app_learning_agent
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source
from jarvis.tools import _agent_run_ctx, _app_learning_ctx as ctx
from jarvis.tools.finish_app_learning_run import finish_app_learning_run
from jarvis.tools.list_app_modules import list_app_modules
from jarvis.tools.read_app_source import read_app_source
from jarvis.tools.record_app_wiki import record_app_wiki

LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
WIKI = "Jarvis Wiki Page"

SCRIBE_SLUG = "app-learn-test-scribe"
AUDITOR_SLUG = "app-learn-test-auditor"
NON_ADMIN = "app-learn-plain@example.com"


def _ensure_role(role: str) -> None:
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)


def _mk_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	u = frappe.get_doc("User", email)
	for r in roles:
		u.add_roles(r)
	return email


def _mk_listing(slug: str, nature: str) -> str:
	if frappe.db.exists(LISTING, slug):
		frappe.db.set_value(LISTING, slug, "nature", nature)
	else:
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": slug,
				"title": f"{nature} test",
				"nature": nature,
				"delivery": "delegate",
				"status": "Published",
			}
		).insert(ignore_permissions=True)
	return slug


def _mk_run(agent_slug: str, session_key: str, status: str = "running") -> str:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": agent_slug,
			"trigger": "manual",
			"status": status,
			"started_at": frappe.utils.now(),
			"session_key": session_key,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


class TestAppLearningAgentTools(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # admin-tier by default (has_jarvis_admin_access bypass)
		_mk_listing(SCRIBE_SLUG, "Scribe")
		_mk_listing(AUDITOR_SLUG, "Auditor")
		_mk_user(NON_ADMIN, [])  # a plain enabled user, no admin roles
		# temp custom-app source tree behind the shared _app_source_dir seam
		self.tmp = tempfile.mkdtemp(prefix="jarvis-applearn-agent-test-")
		self.app_dir = os.path.join(self.tmp, "fakeapp")
		os.makedirs(self.app_dir)
		self._write({"hooks.py": "app_title = 'Fake'\n", "mod/api.py": "import frappe\n"})
		self._patches = [
			mock.patch.object(app_source, "_app_source_dir", side_effect=lambda a: self.app_dir),
			mock.patch.object(app_source, "_installed_custom_apps", return_value=["fakeapp"]),
		]
		for p in self._patches:
			p.start()
		self.session_key = frappe.generate_hash(length=24)
		self._runs: list[str] = []

	def tearDown(self):
		for p in self._patches:
			p.stop()
		_agent_run_ctx.clear_session_key()
		# clear per-run budget cache keys
		for sk in {self.session_key, *getattr(self, "_extra_keys", set())}:
			frappe.cache().delete_value(ctx._src_bytes_key(sk))
			frappe.cache().delete_value(ctx._pages_key(sk))
		frappe.db.delete(WIKI, {"slug": ["like", "fakeapp-%"]})
		for name in self._runs:
			if frappe.db.exists(RUN, name):
				frappe.delete_doc(RUN, name, force=True, ignore_permissions=True)
		frappe.db.delete(RUN, {"agent": ["in", [SCRIBE_SLUG, AUDITOR_SLUG]]})
		frappe.set_user("Administrator")
		shutil.rmtree(self.tmp, ignore_errors=True)

	def _write(self, files: dict[str, str]) -> None:
		for rel, content in files.items():
			full = os.path.join(self.app_dir, rel)
			os.makedirs(os.path.dirname(full), exist_ok=True)
			with open(full, "w") as fh:
				fh.write(content)

	def _bind_scribe_run(self, session_key: str | None = None) -> str:
		sk = session_key or self.session_key
		run = _mk_run(SCRIBE_SLUG, sk)
		self._runs.append(run)
		_agent_run_ctx.set_session_key(sk)
		return run

	# ------------------------------------------------------------------ #
	# self-gate
	# ------------------------------------------------------------------ #
	def test_no_session_key_is_denied(self):
		_agent_run_ctx.clear_session_key()
		with self.assertRaises(InvalidArgumentError):
			list_app_modules()
		with self.assertRaises(InvalidArgumentError):
			read_app_source("fakeapp", "hooks.py")

	def test_non_scribe_run_is_denied(self):
		# a run bound to an AUDITOR listing must NOT get source access
		run = _mk_run(AUDITOR_SLUG, self.session_key)
		self._runs.append(run)
		_agent_run_ctx.set_session_key(self.session_key)
		with self.assertRaises(InvalidArgumentError):
			read_app_source("fakeapp", "hooks.py")
		with self.assertRaises(InvalidArgumentError):
			list_app_modules("fakeapp")

	def test_finalized_run_is_denied(self):
		run = _mk_run(SCRIBE_SLUG, self.session_key, status="completed")
		self._runs.append(run)
		_agent_run_ctx.set_session_key(self.session_key)
		with self.assertRaises(InvalidArgumentError):
			read_app_source("fakeapp", "hooks.py")

	def test_non_admin_run_as_identity_is_denied(self):
		self._bind_scribe_run()
		original = frappe.session.user
		try:
			frappe.set_user(NON_ADMIN)  # a plain user (no System Manager / Jarvis Admin)
			with self.assertRaises(InvalidArgumentError):
				read_app_source("fakeapp", "hooks.py")
		finally:
			frappe.set_user(original)

	# ------------------------------------------------------------------ #
	# allowlist + containment + budget
	# ------------------------------------------------------------------ #
	def test_allowlist_rejects_core_apps(self):
		self._bind_scribe_run()
		for core in ("frappe", "erpnext", "hrms", "india_compliance", "jarvis"):
			with self.assertRaises(InvalidArgumentError):
				list_app_modules(core)
			with self.assertRaises(InvalidArgumentError):
				read_app_source(core, "hooks.py")

	def test_containment_rejects_symlink_escape(self):
		self._bind_scribe_run()
		secret = os.path.join(self.tmp, "secret.py")
		with open(secret, "w") as fh:
			fh.write("ENCRYPTION_KEY = 'hunter2'\n")
		os.symlink(secret, os.path.join(self.app_dir, "config.py"))  # allowed-ext link out of tree
		with self.assertRaises(InvalidArgumentError):
			read_app_source("fakeapp", "config.py")
		# ... and the manifest never lists it
		out = list_app_modules("fakeapp")
		self.assertNotIn("config.py", {f["path"] for f in out["files"]})

	def test_per_run_byte_budget_enforced(self):
		self._bind_scribe_run()
		# first read succeeds
		out = read_app_source("fakeapp", "hooks.py")
		self.assertIn("<untrusted-data", out["content"])  # fenced as DATA
		# exhaust the budget, then the next read is refused
		ctx.add_source_bytes(self.session_key, ctx.PER_RUN_SOURCE_BYTES_BUDGET)
		with self.assertRaises(InvalidArgumentError):
			read_app_source("fakeapp", "mod/api.py")

	def test_list_app_modules_roster_and_manifest(self):
		self._bind_scribe_run()
		roster = list_app_modules()
		self.assertIn("fakeapp", {a["app"] for a in roster["apps"]})
		manifest = list_app_modules("fakeapp")
		paths = {f["path"] for f in manifest["files"]}
		self.assertIn("hooks.py", paths)
		self.assertIn("mod/api.py", paths)

	# ------------------------------------------------------------------ #
	# record_app_wiki: funnel + not-gated + in-place update
	# ------------------------------------------------------------------ #
	def test_record_app_wiki_is_write_but_not_gated(self):
		self.assertIn("record_app_wiki", api._WRITE_TOOLS)
		self.assertNotIn("record_app_wiki", api._GATED_WRITES)

	def test_record_app_wiki_applies_via_funnel_and_updates_in_place(self):
		self._bind_scribe_run()
		res = record_app_wiki(
			app="fakeapp",
			pages=[
				{
					"title": "Gate Pass workflow",
					"page_type": "Process",
					"body_md": "The Gate Pass doctype drives inbound receipt.",
				}
			],
		)
		self.assertEqual(res["applied"], 1)
		self.assertEqual(res["failed"], 0)
		slug = "fakeapp-gate-pass-workflow"
		self.assertEqual(frappe.db.count(WIKI, {"slug": slug}), 1)
		self.assertEqual(frappe.db.get_value(WIKI, {"slug": slug}, "scope"), "Org")

		# a RE-RUN (new session_key/run) updates the same slug in place — no dup
		sk2 = frappe.generate_hash(length=24)
		self._extra_keys = {sk2}
		self._bind_scribe_run(sk2)
		res2 = record_app_wiki(
			app="fakeapp",
			pages=[
				{
					"title": "Gate Pass workflow",
					"page_type": "Process",
					"body_md": "Updated on re-run.",
				}
			],
		)
		self.assertEqual(res2["applied"], 1)
		self.assertEqual(frappe.db.count(WIKI, {"slug": slug}), 1)  # still exactly one page

	def test_record_app_wiki_per_run_page_cap(self):
		self._bind_scribe_run()
		pages = [
			{"title": f"Page {i}", "page_type": "Process", "body_md": f"body {i}"}
			for i in range(ctx.PER_RUN_PAGE_CAP + 3)
		]
		res = record_app_wiki(app="fakeapp", pages=pages)
		self.assertTrue(res["truncated"])
		self.assertLessEqual(res["applied"], ctx.PER_RUN_PAGE_CAP)

	# ------------------------------------------------------------------ #
	# P1: namespace integrity — app required, slug forced, provenance-fenced
	# ------------------------------------------------------------------ #
	def test_record_app_wiki_rejects_page_without_valid_app(self):
		# no top-level app AND no page app -> the page resolves to no learnable
		# custom app: REJECTED, never written un-prefixed.
		self._bind_scribe_run()
		res = record_app_wiki(pages=[{"title": "Company Leave Policy", "body_md": "x", "key": "overview"}])
		self.assertEqual(res["applied"], 0)
		self.assertEqual(res.get("rejected"), 1)
		self.assertEqual(frappe.db.count(WIKI, {"slug": "company-leave-policy"}), 0)
		self.assertEqual(frappe.db.count(WIKI, {"slug": "overview"}), 0)
		# a core / unknown app is likewise rejected server-side
		res2 = record_app_wiki(app="erpnext", pages=[{"title": "X", "body_md": "y", "key": "overview"}])
		self.assertEqual(res2["applied"], 0)
		self.assertEqual(res2.get("rejected"), 1)

	def test_record_app_wiki_slug_is_forced_app_prefixed(self):
		self._bind_scribe_run()
		res = record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "b"}])
		self.assertEqual(res["applied"], 1)
		self.assertEqual(res["slugs"], ["fakeapp-overview"])
		self.assertTrue(all(s.startswith("fakeapp-") for s in res["slugs"]))
		# never an un-prefixed slug, even though the key alone is "overview"
		self.assertEqual(frappe.db.count(WIKI, {"slug": "overview"}), 0)

	def test_record_app_wiki_cannot_overwrite_human_page(self):
		# A human-authored Org page whose slug the scribe's forced slug collides with.
		frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": "fakeapp-overview",
				"title": "Fakeapp Overview",
				"page_type": "Process",
				"body_md": "HUMAN AUTHORED BODY",
				"status": "Active",
				"scope": "Org",
				"sources": frappe.as_json(
					[{"kind": "manual", "date": frappe.utils.today(), "ref": None, "user": "Administrator"}]
				),
			}
		).insert(ignore_permissions=True)
		self._bind_scribe_run()
		res = record_app_wiki(
			app="fakeapp",
			pages=[{"title": "Overview", "key": "overview", "body_md": "SCRIBE OVERWRITE ATTEMPT"}],
		)
		# the provenance fence REFUSES the collision: nothing applied, human page untouched
		self.assertEqual(res["applied"], 0)
		self.assertEqual(frappe.db.get_value(WIKI, "fakeapp-overview", "body_md"), "HUMAN AUTHORED BODY")

	def test_record_app_wiki_may_refresh_legacy_pipeline_page(self):
		# Migration reconciliation: a page the RETIRED chat-batch pipeline created
		# (kind ``app-learning:<app>``) IS agent provenance, so the scribe MAY refresh
		# it in place — the fence blocks only human-authored pages.
		frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": "fakeapp-overview",
				"title": "Fakeapp Overview",
				"page_type": "Process",
				"body_md": "LEGACY BODY",
				"status": "Active",
				"scope": "Org",
				"sources": frappe.as_json(
					[
						{
							"kind": "app-learning:fakeapp",
							"date": frappe.utils.today(),
							"ref": None,
							"user": "Administrator",
						}
					]
				),
			}
		).insert(ignore_permissions=True)
		self._bind_scribe_run()
		res = record_app_wiki(
			app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "REFRESHED"}]
		)
		self.assertEqual(res["applied"], 1)
		self.assertEqual(frappe.db.get_value(WIKI, "fakeapp-overview", "body_md"), "REFRESHED")

	def test_record_app_wiki_updates_its_own_prior_page(self):
		# The fence must NOT break legit re-run-in-place: an agent updating a page a
		# prior agent run created still works (its provenance matches).
		self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "FIRST"}])
		sk2 = frappe.generate_hash(length=24)
		self._extra_keys = {sk2}
		self._bind_scribe_run(sk2)
		res = record_app_wiki(
			app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "SECOND"}]
		)
		self.assertEqual(res["applied"], 1)
		self.assertEqual(frappe.db.count(WIKI, {"slug": "fakeapp-overview"}), 1)
		self.assertEqual(frappe.db.get_value(WIKI, "fakeapp-overview", "body_md"), "SECOND")

	def test_record_app_wiki_will_not_overwrite_human_edited_agent_page(self):
		# CA-2: a page the SCRIBE created, then a HUMAN edited (a `manual` source is
		# appended ALONGSIDE the retained agent source), must NOT be overwritten by
		# the next run. Historical agent provenance does not authorize replacing a
		# later human edit — the fence refuses after ANY human touch.
		self._bind_scribe_run()
		record_app_wiki(
			app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "SCRIBE V1"}]
		)
		slug = "fakeapp-overview"
		sources = json.loads(frappe.db.get_value(WIKI, slug, "sources") or "[]")
		self.assertTrue(any(s["kind"].startswith("app-learning") for s in sources))  # agent-owned
		sources.append({"kind": "manual", "date": frappe.utils.today(), "ref": None, "user": "Administrator"})
		frappe.db.set_value(
			WIKI, slug, {"sources": frappe.as_json(sources), "body_md": "HUMAN EDIT"}, update_modified=False
		)
		# the next scribe run tries to refresh the SAME key
		sk2 = frappe.generate_hash(length=24)
		self._extra_keys = {sk2}
		self._bind_scribe_run(sk2)
		res = record_app_wiki(
			app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "SCRIBE V2"}]
		)
		self.assertEqual(res["applied"], 0)
		self.assertEqual(res["failed"], 1)  # the refusal is counted as failed (CA-3)
		self.assertEqual(res["slugs"], [])
		# the human's body survives untouched
		self.assertEqual(frappe.db.get_value(WIKI, slug, "body_md"), "HUMAN EDIT")

	def test_record_app_wiki_mixed_batch_records_only_the_written_page(self):
		# CA-3: a batch where the FIRST page collides with a human page (refused) and
		# the SECOND is created must record ONLY the created page and count the
		# refusal as failed — never mis-assign the write to the collided human slug.
		frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": "fakeapp-overview",
				"title": "Human Overview",
				"page_type": "Process",
				"body_md": "HUMAN",
				"status": "Active",
				"scope": "Org",
				"sources": frappe.as_json(
					[{"kind": "manual", "date": frappe.utils.today(), "ref": None, "user": "Administrator"}]
				),
			}
		).insert(ignore_permissions=True)
		run = self._bind_scribe_run()
		res = record_app_wiki(
			app="fakeapp",
			pages=[
				{"title": "Overview", "key": "overview", "body_md": "SCRIBE COLLIDE"},  # -> refused
				{"title": "Gate Pass", "key": "doctype-gate-pass", "body_md": "SCRIBE NEW"},  # -> created
			],
		)
		self.assertEqual(res["applied"], 1)
		self.assertEqual(res["failed"], 1)
		self.assertEqual(res["slugs"], ["fakeapp-doctype-gate-pass"])  # ONLY the page written
		self.assertEqual(frappe.db.get_value(WIKI, "fakeapp-overview", "body_md"), "HUMAN")  # untouched
		self.assertEqual(frappe.db.count(WIKI, {"slug": "fakeapp-doctype-gate-pass"}), 1)
		# the run tally reflects ONLY the created page (feeds the completion summary)
		self.assertEqual(frappe.db.get_value(RUN, run, "pages_written"), 1)
		tally = json.loads(frappe.db.get_value(RUN, run, "pages_json") or "[]")
		self.assertEqual({p["slug"] for p in tally}, {"fakeapp-doctype-gate-pass"})

	# ------------------------------------------------------------------ #
	# P0-2: a successful scribe run reaches TERMINAL "completed" on its own
	# ------------------------------------------------------------------ #
	def test_finish_is_write_but_not_gated(self):
		self.assertIn("finish_app_learning_run", api._WRITE_TOOLS)
		self.assertNotIn("finish_app_learning_run", api._GATED_WRITES)

	def test_finish_completes_run_with_pages_tally(self):
		run = self._bind_scribe_run()
		record_app_wiki(
			app="fakeapp",
			pages=[
				{"title": "Overview", "key": "overview", "body_md": "a"},
				{"title": "Gate Pass", "key": "doctype-gate-pass", "body_md": "b"},
			],
		)
		# record_app_wiki tallies the pages on the run; still running until finalize
		self.assertEqual(frappe.db.get_value(RUN, run, "pages_written"), 2)
		tally = json.loads(frappe.db.get_value(RUN, run, "pages_json") or "[]")
		self.assertEqual({p["slug"] for p in tally}, {"fakeapp-overview", "fakeapp-doctype-gate-pass"})
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")
		# the delegate's terminal call finalizes to completed with the tally
		out = finish_app_learning_run()
		self.assertEqual(out["status"], "completed")
		self.assertEqual(out["pages_written"], 2)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")
		self.assertTrue(frappe.db.get_value(RUN, run, "finished_at"))

	def test_finish_completes_run_with_no_pages(self):
		# "no learnable custom app" is an honest SUCCESS, not a reaper-failed run.
		run = self._bind_scribe_run()
		out = finish_app_learning_run()
		self.assertEqual(out["status"], "completed")
		self.assertEqual(out["pages_written"], 0)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")

	def test_finish_is_idempotent_on_an_already_terminal_run(self):
		# CA-4: with completion now SERVER-owned, a late/duplicate finish must report
		# the terminal state idempotently rather than raising "already finalized".
		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		finish_app_learning_run()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")
		# calling finish AGAIN on the same (now completed) run is a no-op fast-path
		out = finish_app_learning_run()
		self.assertEqual(out["status"], "completed")
		self.assertEqual(out["pages_written"], 1)

	def test_stale_scribe_run_with_pages_is_reconciled_completed(self):
		# CA-4: a scribe run that wrote pages but never called finish must be
		# reconciled to COMPLETED by the stale-run sweep (server-owned completion),
		# NOT mislabeled failed — terminalization no longer depends on the model.
		from jarvis.chat.agent_scheduler import STALE_RUN_AFTER_SECONDS, reap_stale_agent_runs

		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		self.assertEqual(frappe.db.get_value(RUN, run, "pages_written"), 1)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-(STALE_RUN_AFTER_SECONDS + 3600))
		frappe.db.set_value(RUN, run, "started_at", old, update_modified=False)
		frappe.db.commit()
		reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")
		self.assertTrue(frappe.db.get_value(RUN, run, "finished_at"))

	def test_stale_scribe_run_without_pages_is_failed(self):
		# A scribe run that produced NO durable pages is a genuinely dead run: the
		# sweep still fails it (there is no success to reconcile).
		from jarvis.chat.agent_scheduler import STALE_RUN_AFTER_SECONDS, reap_stale_agent_runs

		run = self._bind_scribe_run()
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-(STALE_RUN_AFTER_SECONDS + 3600))
		frappe.db.set_value(RUN, run, "started_at", old, update_modified=False)
		frappe.db.commit()
		reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "failed")

	def test_list_runs_page_stamps_scribe_nature_and_pages(self):
		from jarvis.chat import agents_api

		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		finish_app_learning_run()
		frappe.db.set_value(RUN, run, "owner", "Administrator", update_modified=False)
		page = agents_api.list_runs_page(agent=SCRIBE_SLUG)
		rows = [r for r in page["rows"] if r["name"] == run]
		self.assertTrue(rows)
		self.assertEqual(rows[0]["nature"], "Scribe")
		self.assertEqual(rows[0]["pages_written"], 1)

	# ------------------------------------------------------------------ #
	# Round-2 concurrency: transaction-honest tally + finish-vs-sweep CAS
	# ------------------------------------------------------------------ #
	def test_deadlock_on_a_later_page_propagates_no_phantom_tally(self):
		# CA2-2: a transaction-FATAL error (deadlock) on a later page must PROPAGATE, not
		# be swallowed — the aborted InnoDB transaction rolled back the earlier saves, so
		# they must never be tallied as written (a phantom tally). The tool raises and the
		# run's pages_written stays 0.
		import pymysql

		from jarvis.chat import wiki

		run = self._bind_scribe_run()
		real = wiki._apply_one_update
		calls = {"n": 0}

		def _apply(update, *a, **k):
			calls["n"] += 1
			if calls["n"] >= 2:  # the SECOND page processed hits a deadlock
				raise pymysql.err.OperationalError(1213, "Deadlock found when trying to get lock")
			return real(update, *a, **k)

		with mock.patch.object(wiki, "_apply_one_update", side_effect=_apply):
			with self.assertRaises(pymysql.err.OperationalError):
				record_app_wiki(
					app="fakeapp",
					pages=[
						{"title": "Alpha", "key": "a-alpha", "body_md": "x"},  # deterministic order:
						{"title": "Beta", "key": "b-beta", "body_md": "y"},  # a-alpha < b-beta
					],
				)
		# no phantom tally: nothing was recorded as written on the run
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 0)

	def test_tally_write_failure_propagates_and_rolls_back(self):
		# CA2-3: a failed tally write must PROPAGATE (not be swallowed), so the request
		# rolls the pages back too and retries rather than committing pages with no
		# durable tally.
		import pymysql

		run = self._bind_scribe_run()
		real_set = frappe.db.set_value

		def _sv(dt, *a, **k):
			if dt == RUN:  # the run-row tally write — simulate a lock-wait timeout
				raise pymysql.err.OperationalError(1205, "Lock wait timeout exceeded")
			return real_set(dt, *a, **k)

		with mock.patch.object(frappe.db, "set_value", side_effect=_sv):
			with self.assertRaises(pymysql.err.OperationalError):
				record_app_wiki(
					app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}]
				)
		# the tally was never persisted (the write failed and propagated)
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 0)

	def test_sweep_leaves_a_concurrently_finished_run_alone(self):
		# CA2-4: a run a concurrent finish already moved to "completed" between the sweep's
		# scan and its transition must be LEFT ALONE — the sweep re-reads under a row lock
		# and compare-and-sets on status='running', so it never flips a finished run to
		# failed. Simulated by handing the reaper a STALE candidate snapshot (running, 0
		# pages) while the real row is already completed.
		from jarvis.chat import agent_scheduler

		run = self._bind_scribe_run()
		frappe.db.set_value(
			RUN,
			run,
			{"status": "completed", "pages_written": 2, "finished_at": frappe.utils.now()},
			update_modified=False,
		)
		frappe.db.commit()
		stale = [
			{
				"name": run,
				"session_key": self.session_key,
				"owner": "Administrator",
				"agent": SCRIBE_SLUG,
				"installation": None,
				"pages_written": 0,  # the value the sweep saw at scan time
			}
		]
		with (
			mock.patch.object(agent_scheduler, "_stale_candidates", return_value=stale),
			mock.patch("jarvis.chat.agent_runs.teardown_run_session") as td,
		):
			agent_scheduler.reap_stale_agent_runs()
		# left alone: still completed (never flipped to failed); the session is not torn down
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")
		td.assert_not_called()

	# ------------------------------------------------------------------ #
	# Round-3 concurrency: writeback-vs-sweep lock ordering + commit-aware budget
	# ------------------------------------------------------------------ #
	def test_writeback_refuses_when_run_became_terminal(self):
		# CA3-1: record_app_wiki LOCKS the run row + re-verifies it is still ``running``
		# (for_update) BEFORE the first page write. A run finalized (cancel / reap /
		# finish) between ``resolve_scribe_run`` and that lock is refused with the normal
		# shape — nothing is written, nothing tallied, no budget consumed. Simulated by
		# terminalizing the row after resolve returns a still-``running`` view of it.
		run = self._bind_scribe_run()
		frappe.db.set_value(RUN, run, "status", "cancelled", update_modified=False)
		resolved = {"name": run, "agent": SCRIBE_SLUG, "session_key": self.session_key, "status": "running"}
		with mock.patch.object(ctx, "resolve_scribe_run", return_value=resolved):
			res = record_app_wiki(
				app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}]
			)
		self.assertEqual(res["applied"], 0)
		self.assertEqual(res["slugs"], [])
		self.assertEqual(frappe.db.count(WIKI, {"slug": "fakeapp-overview"}), 0)  # never written
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 0)
		self.assertEqual(ctx.pages_written(self.session_key), 0)  # no budget consumed

	def test_tally_run_refuses_terminal_row(self):
		# CA3-1: ``_tally_run`` refuses to stamp pages onto a TERMINAL run (defense in depth
		# for the sweep-vs-writeback race) — a straggling tally never lands on a failed run.
		from jarvis.tools.record_app_wiki import _tally_run

		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 1)
		# a concurrent sweep failed the run; the straggler tally must NOT stamp onto it
		frappe.db.set_value(RUN, run, "status", "failed", update_modified=False)
		_tally_run(run, [{"slug": "fakeapp-extra", "title": "Extra"}], False, 0)
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 1)  # unchanged

	def test_sweep_completes_a_running_writeback_never_fails_it(self):
		# CA3-1: a run whose writeback COMMITTED its pages (running, pages_written>0) is
		# never failed by the stale sweep — the sweep re-reads under the row lock and sees
		# the durable tally, completing it. Simulated by a STALE candidate snapshot
		# (running, 0 pages) while the real row is running with a committed tally, exactly
		# as the writer-holds-lock-before-sweep ordering produces (a real two-connection
		# block cannot be staged single-connection).
		from jarvis.chat import agent_scheduler

		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 1)
		frappe.db.commit()  # the writeback's pages + tally are durable before the sweep runs
		stale = [
			{
				"name": run,
				"session_key": self.session_key,
				"owner": "Administrator",
				"agent": SCRIBE_SLUG,
				"installation": None,
				"pages_written": 0,  # the stale value the sweep saw at scan time
			}
		]
		with (
			mock.patch.object(agent_scheduler, "_stale_candidates", return_value=stale),
			mock.patch("jarvis.chat.agent_runs.teardown_run_session"),
		):
			agent_scheduler.reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")  # NEVER failed
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 1)

	def test_budget_preserved_after_tally_commit_rollback(self):
		# CA3-3: the page budget advances ONLY from an after-commit callback, so a tally /
		# commit failure that rolls the request back never leaks budget — a retry sees the
		# FULL remaining capacity. Simulated by failing the run-row tally write (propagates),
		# then rolling back as the request/job wrapper would on the raised error.
		import pymysql

		self._bind_scribe_run()  # binds the session_key context + creates the running run
		frappe.db.commit()  # persist the run row so the rollback discards only the writeback
		self.assertEqual(ctx.pages_written(self.session_key), 0)
		real_set = frappe.db.set_value

		def _sv(dt, *a, **k):
			if dt == RUN:  # the run-row tally write — simulate a lock-wait timeout
				raise pymysql.err.OperationalError(1205, "Lock wait timeout exceeded")
			return real_set(dt, *a, **k)

		with mock.patch.object(frappe.db, "set_value", side_effect=_sv):
			with self.assertRaises(pymysql.err.OperationalError):
				record_app_wiki(
					app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}]
				)
		frappe.db.rollback()  # the request/job wrapper rolls back on the propagated error
		# budget NOT leaked: the full per-run page capacity remains available for the retry
		self.assertEqual(ctx.pages_written(self.session_key), 0)

	def test_stale_scribe_run_reconciles_and_persists_page_tally(self):
		# CA3-4: a scribe run whose stored tally was LOST (reads 0) but whose pages ARE
		# durably written is completed by the sweep with the tally REBUILT from provenance
		# and PERSISTED — pages_written + pages_json show the real count/list, not 0.
		from jarvis.chat.agent_scheduler import STALE_RUN_AFTER_SECONDS, reap_stale_agent_runs

		run = self._bind_scribe_run()
		record_app_wiki(app="fakeapp", pages=[{"title": "Overview", "key": "overview", "body_md": "a"}])
		# simulate a LOST durable tally: zero the stored count/list though the page exists
		frappe.db.set_value(RUN, run, {"pages_written": 0, "pages_json": ""}, update_modified=False)
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-(STALE_RUN_AFTER_SECONDS + 3600))
		frappe.db.set_value(RUN, run, "started_at", old, update_modified=False)
		frappe.db.commit()
		reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "completed")
		self.assertEqual(int(frappe.db.get_value(RUN, run, "pages_written") or 0), 1)
		tally = json.loads(frappe.db.get_value(RUN, run, "pages_json") or "[]")
		self.assertEqual({p["slug"] for p in tally}, {"fakeapp-overview"})

	def test_stale_scribe_run_left_running_when_reconcile_query_fails(self):
		# CA3-4: if the provenance reconcile QUERY fails, the count is UNKNOWN — the sweep
		# must NOT terminalize (completing with 0 would drop real pages; failing would
		# mislabel a success). The run is left running to retry on the next sweep.
		from jarvis.chat.agent_scheduler import STALE_RUN_AFTER_SECONDS, reap_stale_agent_runs

		run = self._bind_scribe_run()  # pages_written 0 (no writeback)
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-(STALE_RUN_AFTER_SECONDS + 3600))
		frappe.db.set_value(RUN, run, "started_at", old, update_modified=False)
		frappe.db.commit()
		with mock.patch("jarvis.tools.record_app_wiki.reconcile_run_pages", return_value=None):
			reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")  # left for retry

	# ------------------------------------------------------------------ #
	# Round-4 concurrency: the per-run page budget is read UNDER the run-row lock
	# ------------------------------------------------------------------ #
	def test_budget_read_under_run_row_lock_bounds_overlapping_writeback(self):
		# CA4-1: the per-run page budget is read UNDER the run-row ``for_update`` lock, NOT
		# before it. Two writebacks for the SAME run/session serialize on that lock, so the
		# second reads the budget only AFTER the first's pages + after-commit counter (CA3-3)
		# are durable — the two can never both reserve the full remaining allowance (a soft-cap
		# overrun). Simulated single-connection: a PRIOR committed writeback of N pages becomes
		# visible EXACTLY when this call acquires the run-row lock (as a real prior lock-holder's
		# commit would), by advancing the session counter inside the ``for_update`` RUN read. A
		# PRE-lock budget read would see 0 (and write all the pages); the UNDER-lock read sees N
		# and truncates to the ``CAP - N`` that actually remains.
		self._bind_scribe_run()  # binds the running run + session_key; its name is unused here
		cap = ctx.PER_RUN_PAGE_CAP
		prior = cap - 2  # a prior writeback consumed all but 2 of the per-run cap
		real_get = frappe.db.get_value
		armed = {"fired": False}

		def _get(dt, *a, **k):
			# the run-row for_update read IS the lock acquisition: make the prior holder's
			# durable counter visible right here (never before it)
			if dt == RUN and k.get("for_update") and not armed["fired"]:
				armed["fired"] = True
				ctx.add_pages_written(self.session_key, prior)
			return real_get(dt, *a, **k)

		# ask to write 6 pages: below the cap outright (so WITHOUT the prior batch nothing would
		# truncate), but above the 2 that remain once the prior batch is seen under the lock.
		pages = [{"title": f"Page {i}", "key": f"k-{i:02d}", "body_md": f"body {i}"} for i in range(6)]
		with mock.patch.object(frappe.db, "get_value", side_effect=_get):
			res = record_app_wiki(app="fakeapp", pages=pages)
		self.assertTrue(armed["fired"])  # the lock read really fired the prior-batch counter
		# the UNDER-lock read saw ``prior`` consumed -> only ``cap - prior`` (== 2) may be written
		self.assertTrue(res["truncated"])
		self.assertEqual(res["applied"], cap - prior)  # 2, the under-lock remaining, not 6
		self.assertEqual(res["pages_remaining"], 0)
		# exactly ``cap - prior`` pages actually landed -> no soft-cap overrun past the budget
		self.assertEqual(frappe.db.count(WIKI, {"slug": ["like", "fakeapp-k-%"]}), cap - prior)


class TestRunAgentNowScribeGate(FrappeTestCase):
	"""The ``run_agent_now`` nature gate admits Scribe (auditor stays valid,
	operator still refused). Downstream dispatch/budget/installability are mocked
	so the test isolates the gate change."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.admin = _mk_user("app-learn-gate-admin@example.com", ["System Manager"])
		_mk_listing(SCRIBE_SLUG, "Scribe")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Jarvis Agent Installation", {"agent": SCRIBE_SLUG})
		frappe.db.commit()

	def _install(self) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Installation",
				"agent": SCRIBE_SLUG,
				"run_as_user": self.admin,
				"enabled": 1,
			}
		)
		doc.owner = self.admin
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value("Jarvis Agent Installation", doc.name, "owner", self.admin, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _run_now(self, inst_name):
		from jarvis.chat import agents_api

		with (
			mock.patch("jarvis.chat.agent_installability.assert_installable", return_value=None),
			mock.patch("jarvis.chat.agent_scheduler._over_run_budget", return_value=(False, "")),
			mock.patch("jarvis.chat.agent_scheduler._launch_audit", return_value={"run": "R", "conversation": "C", "session_key": "S"}),
		):
			original = frappe.session.user
			try:
				frappe.set_user(self.admin)  # self-mapped run (run_as == triggerer)
				return agents_api.run_agent_now(inst_name)
			finally:
				frappe.set_user(original)

	def test_scribe_passes_the_run_now_nature_gate(self):
		inst = self._install()
		result = self._run_now(inst)  # must NOT raise "Only auditor and scribe..."
		self.assertTrue(result.get("ok"))

	def test_operator_is_still_refused_by_the_gate(self):
		_mk_listing(SCRIBE_SLUG, "Operator")  # flip the same listing to operator
		inst = self._install()
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._run_now(inst)


class TestLegacyScheduleRetired(FrappeTestCase):
	"""The legacy chat-batch pipeline entry point is truly dormant: the whitelisted
	``schedule_app_learning`` REFUSES so no new chat-pipeline run can start by any
	path (the cron tick is disabled + the doctype/engine stay present for rollback)."""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_schedule_app_learning_refuses(self):
		# CA3-5, config A (absent key = retired by default): the endpoint REFUSES.
		from jarvis.chat import app_learning_api

		with self.assertRaises(frappe.exceptions.ValidationError):
			app_learning_api.schedule_app_learning(apps='["fakeapp"]', consent=1)

	def test_schedule_app_learning_schedules_when_reenabled(self):
		# CA3-5, config B (re-enabled for rollback): the retained scheduling body runs, so a
		# rollback can actually QUEUE a run — the conditional throw is bypassed when
		# ``_legacy_retired()`` is False. Both-configs coverage with the refuse test above.
		from jarvis.chat import app_learning_api
		from jarvis.learning import app_analysis

		# schedule_app_learning writes the LEGACY doctype, not the agent-run RUN above.
		legacy_run = "Jarvis App Learning Run"
		created: list[str] = []
		try:
			with (
				mock.patch.object(app_analysis, "_legacy_retired", return_value=False),
				mock.patch.object(app_analysis, "_installed_custom_apps", return_value=["fakeapp"]),
				mock.patch.object(app_analysis, "_enqueue_tick") as tick,
			):
				res = app_learning_api.schedule_app_learning(apps='["fakeapp"]', when="", consent=1)
			self.assertTrue(res["ok"])
			created = res["data"]["runs"]
			self.assertEqual(len(created), 1)
			self.assertEqual(frappe.db.get_value(legacy_run, created[0], "status"), "Queued")
			tick.assert_called_once()  # an immediate run kicks the (now self-gating) tick
		finally:
			for name in created:
				if frappe.db.exists(legacy_run, name):
					frappe.delete_doc(legacy_run, name, force=True, ignore_permissions=True)
			frappe.db.commit()
