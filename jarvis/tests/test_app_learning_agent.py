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
