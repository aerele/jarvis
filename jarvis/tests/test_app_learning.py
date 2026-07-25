"""Tests for ``jarvis.learning.app_analysis`` — the learn-from-custom-apps
engine: snapshot zip (allowlist/excludes/caps/prioritized subset), batch
planning, the turn-end state machine (success chain, malformed-reply retry ->
Failed, cancel stop, final -> Ingesting), the ingest (wiki mapping + skills +
25-cap deferral + failure path), single-active-run serialization, stale-run
recovery and the zip cleanup job.

Hermetic: the agent-turn boundary (``jarvis.chat.api._enqueue_turn``) and the
queue wrappers (``_enqueue_tick`` / ``_enqueue_ingest``) are always patched
where they could fire; the app "source tree" is a temp dir behind the
``_app_source_dir`` seam; wiki writes are patched at
``apply_extracted_page_updates``. Only the org-skill create runs for real (it
is the asserted behaviour).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime

from jarvis.learning import app_analysis

RUN = "Jarvis App Learning Run"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
SKILL = "Jarvis Custom Skill"

ADMIN_USER = "app-learn-admin@example.com"

_BIG = app_analysis.PER_FILE_CAP_BYTES + 1


def _ensure_admin(email: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "AppLearn",
				"last_name": "Admin",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles("System Manager")
	frappe.db.commit()


def _fenced(payload) -> str:
	return "Done.\n```jarvis-app-analysis\n" + json.dumps(payload) + "\n```\n"


class _AppLearningTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_admin(ADMIN_USER)
		self._orig_user = frappe.session.user
		# A brand-new doctype: clearing it keeps the bench-wide
		# single-active-run assertions deterministic on the test site.
		frappe.db.delete(RUN)
		frappe.db.commit()
		self._convs: list[str] = []
		self._skills: list[str] = []
		self._zips: list[str] = []
		self.tmp = tempfile.mkdtemp(prefix="jarvis-app-learning-test-")
		self.app_dir = os.path.join(self.tmp, "fakeapp")
		os.makedirs(self.app_dir)
		self._patches = [
			mock.patch.object(app_analysis, "_app_source_dir", side_effect=lambda app: self.app_dir),
			mock.patch.object(app_analysis, "_installed_custom_apps", return_value=["fakeapp"]),
		]
		for p in self._patches:
			p.start()
		app_analysis._bust_active_conversations()

	def tearDown(self):
		for p in self._patches:
			p.stop()
		frappe.set_user(self._orig_user)
		for conv in self._convs:
			frappe.db.delete(MSG, {"conversation": conv})
			if frappe.db.exists(CONV, conv):
				frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		for slug in self._skills:
			for name in frappe.get_all(SKILL, filters={"skill_name": slug}, pluck="name"):
				frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
		for path in self._zips:
			if path and os.path.isfile(path):
				os.remove(path)
		frappe.db.delete(RUN)
		app_analysis._bust_active_conversations()
		frappe.db.commit()
		shutil.rmtree(self.tmp, ignore_errors=True)

	# ------------------------------------------------------------------ #
	# fixtures
	# ------------------------------------------------------------------ #
	def _write(self, files: dict[str, str]) -> None:
		for rel, content in files.items():
			full = os.path.join(self.app_dir, rel)
			os.makedirs(os.path.dirname(full), exist_ok=True)
			with open(full, "w") as fh:
				fh.write(content)

	def _mk_run(self, **fields) -> "frappe.model.document.Document":
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"app": "fakeapp",
				"status": "Queued",
				"requested_by": ADMIN_USER,
				**fields,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		app_analysis._bust_active_conversations()
		if doc.zip_path:
			self._zips.append(doc.zip_path)
		return doc

	def _mk_conv(self, title: str = "App learning: fakeapp") -> str:
		conv = frappe.get_doc({"doctype": CONV, "title": title, "status": "Active"})
		conv.flags.ignore_permissions = True
		conv.insert()
		frappe.db.commit()
		self._convs.append(conv.name)
		return conv.name

	def _reply(self, conversation: str, content: str, streaming: int = 0, error: str | None = None) -> None:
		seq = frappe.db.count(MSG, {"conversation": conversation}) + 1
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conversation,
				"seq": seq,
				"role": "assistant",
				"content": content,
				"streaming": streaming,
				"error": error,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	def _analyzing_run(self):
		"""A real started run: 2 files -> 2 batches, zip on disk, conversation
		created, batch-1 turn captured (and discarded)."""
		self._write(
			{
				"hooks.py": "app_title = 'Fake'\n" + ("# pad\n" * 3000),
				"mod/api.py": "import frappe\n" + ("# pad\n" * 3000),
			}
		)
		run = self._mk_run()
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			app_analysis.start_run(run.name)
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		self.assertEqual(run.batches_total, 2)
		self.assertTrue(run.conversation)
		self.assertTrue(os.path.isfile(run.zip_path))
		self._convs.append(run.conversation)
		self._zips.append(run.zip_path)
		enq.assert_called_once()
		return run


# --------------------------------------------------------------------------- #
# snapshot zip
# --------------------------------------------------------------------------- #
class TestSnapshotZip(_AppLearningTestCase):
	def test_allowlist_excludes_and_per_file_cap(self):
		self._write(
			{
				"hooks.py": "app_title = 'Fake'\n",
				"fakeapp/doctype/widget/widget.json": '{"doctype": "DocType"}',
				"fakeapp/doctype/widget/widget.py": "class Widget:\n\tpass\n",
				"api.py": "import frappe\n",
				"public/js/app.js": "console.log(1)\n",
				"README.md": "# fake\n",
				"logo.png": "not-source",
				"node_modules/pkg/index.js": "excluded",
				".git/config": "[core]",
				"public/frontend/main.vue": "<template/>",
				"__pycache__/x.py": "excluded",
				"big.py": "#" * _BIG,
			}
		)
		snap = app_analysis._snapshot_zip("test-snap-run", "fakeapp")
		self._zips.append(snap["zip_path"])
		import zipfile

		with zipfile.ZipFile(snap["zip_path"]) as zf:
			members = set(zf.namelist())
		self.assertEqual(
			members,
			{
				"hooks.py",
				"fakeapp/doctype/widget/widget.json",
				"fakeapp/doctype/widget/widget.py",
				"api.py",
				"public/js/app.js",
				"README.md",
			},
		)
		self.assertEqual(snap["file_count"], 6)
		self.assertEqual(snap["notes"]["skipped_large_files"], 1)
		self.assertEqual(snap["notes"]["dropped_files"], 0)
		self.assertEqual(snap["zip_size"], os.path.getsize(snap["zip_path"]))
		# Read-only wrt the source: the big file is still there, untouched.
		self.assertTrue(os.path.isfile(os.path.join(self.app_dir, "big.py")))

	def test_file_cap_keeps_prioritized_subset(self):
		self._write(
			{
				"hooks.py": "h\n",
				"m/doctype/x/x.json": "{}",
				"m/doctype/x/x.py": "pass\n",
				"misc.py": "pass\n",
				"README.md": "# r\n",
			}
		)
		with mock.patch.object(app_analysis, "FILE_CAP", 3):
			snap = app_analysis._snapshot_zip("test-cap-run", "fakeapp")
		self._zips.append(snap["zip_path"])
		import zipfile

		with zipfile.ZipFile(snap["zip_path"]) as zf:
			members = set(zf.namelist())
		# hooks.py, doctype schema, doctype controller outrank misc.py / README.
		self.assertEqual(members, {"hooks.py", "m/doctype/x/x.json", "m/doctype/x/x.py"})
		self.assertEqual(snap["notes"]["dropped_files"], 2)

	def test_zip_cap_fails_with_clear_error_and_no_zip(self):
		self._write({"hooks.py": "x = 1\n" * 200})
		with mock.patch.object(app_analysis, "ZIP_CAP_BYTES", 10):
			with self.assertRaises(ValueError) as ctx:
				app_analysis._snapshot_zip("test-zipcap-run", "fakeapp")
		self.assertIn("cap", str(ctx.exception))
		expected = frappe.get_site_path("private", "files", "app_learning", "test-zipcap-run.zip")
		self.assertFalse(os.path.exists(expected))

	def test_missing_source_dir_raises(self):
		shutil.rmtree(self.app_dir)
		with self.assertRaises(FileNotFoundError):
			app_analysis._snapshot_zip("test-missing-run", "fakeapp")

	def test_symlink_escape_is_not_followed(self):
		# Review P1 (security): a symlink whose target is OUTSIDE the app tree
		# (e.g. site secrets) must never be zipped/shipped to the LLM.
		outside = os.path.join(self.tmp, "secret.py")
		with open(outside, "w") as fh:
			fh.write("ENCRYPTION_KEY = 'hunter2'\n")
		self._write({"hooks.py": "app_title = 'Fake'\n"})
		# an in-app symlink pointing at the out-of-tree secret, with an
		# allow-listed extension
		link = os.path.join(self.app_dir, "config.py")
		os.symlink(outside, link)
		snap = app_analysis._snapshot_zip("test-symlink-run", "fakeapp")
		self._zips.append(snap["zip_path"])
		import zipfile

		with zipfile.ZipFile(snap["zip_path"]) as zf:
			members = set(zf.namelist())
			blob = b"".join(zf.read(n) for n in members)
		self.assertNotIn("config.py", members)
		self.assertNotIn(b"hunter2", blob)
		self.assertEqual(snap["notes"]["skipped_symlinks"], 1)

	def test_symlinked_directory_is_not_descended(self):
		outside_dir = os.path.join(self.tmp, "outside")
		os.makedirs(outside_dir)
		with open(os.path.join(outside_dir, "leak.py"), "w") as fh:
			fh.write("SECRET = 'zzz'\n")
		self._write({"hooks.py": "h\n"})
		os.symlink(outside_dir, os.path.join(self.app_dir, "linkdir"))
		snap = app_analysis._snapshot_zip("test-symlinkdir-run", "fakeapp")
		self._zips.append(snap["zip_path"])
		import zipfile

		with zipfile.ZipFile(snap["zip_path"]) as zf:
			blob = b"".join(zf.read(n) for n in zf.namelist())
		self.assertNotIn(b"zzz", blob)

	def test_report_and_fixture_json_outrank_js_and_md(self):
		# Review P2: report scripts + fixture/workflow JSON carry business logic
		# and must sit above js/vue/md in the priority order.
		self.assertLess(
			app_analysis._priority("m/report/sales/sales.py"),
			app_analysis._priority("public/js/app.js"),
		)
		self.assertLess(
			app_analysis._priority("m/fixtures/workflow.json"),
			app_analysis._priority("README.md"),
		)
		self.assertLess(
			app_analysis._priority("m/workflow/approval/approval.json"),
			app_analysis._priority("public/app.vue"),
		)


# --------------------------------------------------------------------------- #
# batch planning
# --------------------------------------------------------------------------- #
class TestBatchPlanning(_AppLearningTestCase):
	def test_manifest_order_hooks_first(self):
		self._write(
			{
				"hooks.py": "h\n",
				"fakeapp/doctype/widget/widget.json": "{}",
				"fakeapp/doctype/widget/widget.py": "pass\n",
				"api.py": "pass\n",
				"util.py": "pass\n",
				"public/js/app.js": "1\n",
				"README.md": "# r\n",
			}
		)
		snap = app_analysis._snapshot_zip("test-order-run", "fakeapp")
		self._zips.append(snap["zip_path"])
		manifest = app_analysis._manifest_from_zip(snap["zip_path"])
		self.assertEqual(
			[p for p, _c in manifest],
			[
				"hooks.py",
				"fakeapp/doctype/widget/widget.json",
				"fakeapp/doctype/widget/widget.py",
				"api.py",
				"util.py",
				"public/js/app.js",
				"README.md",
			],
		)

	def test_batches_respect_budget_and_split_oversized_files(self):
		budget = app_analysis.BATCH_CHAR_BUDGET
		manifest = [
			("hooks.py", "a" * 15000),
			("b.py", "b" * 8000),
			("c.py", "c" * 30000),
		]
		batches, dropped = app_analysis._plan_batches(manifest)
		self.assertEqual(dropped, 0)
		for batch in batches:
			self.assertLessEqual(sum(len(c) for _p, c in batch), budget)
		# Order preserved: hooks first, then b, then the split parts of c.
		flat = [p for batch in batches for p, _c in batch]
		self.assertEqual(flat[0], "hooks.py")
		self.assertEqual(flat[1], "b.py")
		self.assertEqual(flat[2:], ["c.py (part 1/2)", "c.py (part 2/2)"])
		# All source chars survive the plan.
		self.assertEqual(
			sum(len(c) for batch in batches for _p, c in batch),
			sum(len(c) for _p, c in manifest),
		)

	def test_batch_cap_drops_tail_with_count(self):
		manifest = [(f"f{i}.py", "x" * 15000) for i in range(4)]  # 4 batches
		with mock.patch.object(app_analysis, "MAX_BATCHES", 2):
			batches, dropped = app_analysis._plan_batches(manifest)
		self.assertEqual(len(batches), 2)
		self.assertEqual(dropped, 2)
		self.assertEqual(batches[0][0][0], "f0.py")  # prioritized head kept


# --------------------------------------------------------------------------- #
# turn-end state machine
# --------------------------------------------------------------------------- #
class TestTurnEndStateMachine(_AppLearningTestCase):
	def test_success_chain_then_final_then_ingesting(self):
		run = self._analyzing_run()
		conv = run.conversation

		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self._reply(conv, _fenced({"batch": 1, "notes": ["fact one"]}))
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		self.assertEqual(run.batches_done, 1)
		self.assertEqual(json.loads(run.notes)["batches"]["1"], ["fact one"])
		enq.assert_called_once()
		self.assertIn("batch 2/2", enq.call_args[0][1])

		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self._reply(conv, _fenced({"batch": 2, "notes": ["fact two"]}))
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.batches_done, 2)
		enq.assert_called_once()
		self.assertIn("wiki_pages", enq.call_args[0][1])  # the consolidation turn

		with mock.patch.object(app_analysis, "_enqueue_ingest") as ing:
			self._reply(conv, _fenced({"wiki_pages": [], "skills": [], "summary": "done"}))
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Ingesting")
		self.assertEqual(json.loads(run.notes)["final"]["summary"], "done")
		ing.assert_called_once_with(run.name)

	def test_malformed_reply_retries_once_then_fails(self):
		run = self._analyzing_run()
		conv = run.conversation

		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self._reply(conv, "no fence in this reply at all")
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Analyzing")  # retried, not failed
		self.assertEqual(run.batches_done, 0)
		self.assertEqual(json.loads(run.notes)["retries"]["1"], 1)
		enq.assert_called_once()
		self.assertIn("batch 1/2", enq.call_args[0][1])

		with (
			mock.patch("jarvis.chat.api._enqueue_turn") as enq,
			mock.patch.object(app_analysis, "_enqueue_tick") as tick,
		):
			self._reply(conv, "still ```jarvis-app-analysis\nnot json\n``` broken")
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Failed")
		self.assertIn("failed twice", run.error)
		enq.assert_not_called()
		tick.assert_called_once()  # next queued app gets its chance

	def test_errored_turn_retries_once_then_fails(self):
		run = self._analyzing_run()
		conv = run.conversation
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			app_analysis.on_turn_end(conv, errored=True)
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		enq.assert_called_once()
		with mock.patch("jarvis.chat.api._enqueue_turn"), mock.patch.object(app_analysis, "_enqueue_tick"):
			app_analysis.on_turn_end(conv, errored=True)
		run.reload()
		self.assertEqual(run.status, "Failed")

	def test_cancel_stops_the_chain(self):
		run = self._analyzing_run()
		conv = run.conversation
		app_analysis.mark_cancelled(run.name)
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self._reply(conv, _fenced({"batch": 1, "notes": ["late reply"]}))
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Cancelled")
		self.assertEqual(run.batches_done, 0)
		enq.assert_not_called()

	def test_unrelated_conversation_is_a_cheap_no_op(self):
		conv = self._mk_conv("plain chat")
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			app_analysis.on_turn_end(conv, errored=False)
		enq.assert_not_called()

	def test_final_reply_with_wrong_shape_takes_retry_path(self):
		run = self._analyzing_run()
		conv = run.conversation
		frappe.db.set_value(RUN, run.name, "batches_done", 2)
		frappe.db.commit()
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self._reply(conv, _fenced({"batch": 3, "notes": ["not the final shape"]}))
			app_analysis.on_turn_end(conv, errored=False)
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		self.assertEqual(json.loads(run.notes)["retries"]["final"], 1)
		enq.assert_called_once()
		self.assertIn("wiki_pages", enq.call_args[0][1])


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
class TestIngest(_AppLearningTestCase):
	def _ingesting_run(self, payload, **fields):
		conv = self._mk_conv()
		self._reply(conv, _fenced(payload))
		return self._mk_run(status="Ingesting", conversation=conv, **fields)

	def test_wiki_updates_mapped_and_applied_and_skills_created(self):
		payload = {
			"wiki_pages": [
				{
					"title": "Fakeapp overview",
					"page_type": "Process",
					"body_md": "# What it does",
					"mode": "create",
				},
				{
					"title": "Fakeapp gotchas",
					"page_type": "NotAPageType",
					"body_md": "appendable knowledge",
					"mode": "append",
				},
			],
			"skills": [
				{
					"skill_name": "Fakeapp Helper!",
					"description": "Answer fakeapp questions.",
					"instructions": "Use the fakeapp wiki pages.",
					"user_invocable": True,
				}
			],
			"summary": "learned",
		}
		self._skills.append("fakeapp-helper")
		# Dummy zip so the completion path exercises the delete.
		zdir = frappe.get_site_path("private", "files", "app_learning")
		os.makedirs(zdir, exist_ok=True)
		zpath = os.path.join(zdir, "test-ingest-run.zip")
		with open(zpath, "wb") as fh:
			fh.write(b"zip-bytes")
		self._zips.append(zpath)
		run = self._ingesting_run(payload, zip_path=zpath)

		captured: list[list[dict]] = []

		def _apply(updates, source, user, ref=None, default_scope=None, target_user=None):
			captured.append(updates)
			self.assertEqual(source, "app-learning:fakeapp")
			self.assertEqual(user, ADMIN_USER)
			self.assertIsNone(default_scope)  # default Org behavior
			return len(updates), 0

		with (
			mock.patch("jarvis.chat.wiki.apply_extracted_page_updates", side_effect=_apply),
			mock.patch("jarvis.chat.custom_skills.build_push_payload", return_value=[]),
			mock.patch("jarvis.chat.events.publish_to_user") as pub,
			mock.patch.object(app_analysis, "_enqueue_tick") as tick,
		):
			app_analysis.ingest(run.name)

		run.reload()
		self.assertEqual(run.status, "Completed")
		self.assertTrue(run.finished_at)
		self.assertEqual(run.pages_written, 2)
		self.assertEqual(run.skills_created, 1)
		self.assertEqual(run.skills_deferred, 0)
		self.assertFalse(os.path.exists(zpath))  # zip deleted on completion
		self.assertEqual(run.zip_path, "")
		tick.assert_called_once()
		pub.assert_called_once()
		self.assertEqual(pub.call_args[0][0], ADMIN_USER)
		self.assertEqual(pub.call_args[0][1]["kind"], "app_learning:done")
		self.assertEqual(pub.call_args[0][1]["app"], "fakeapp")

		updates = [u for chunk in captured for u in chunk]
		self.assertEqual(len(updates), 2)
		self.assertEqual(updates[0]["slug"], "fakeapp-fakeapp-overview")
		self.assertEqual(updates[0]["page_type"], "Process")
		self.assertEqual(updates[0]["body_md"], "# What it does")
		# Unknown page_type coerced; append mode maps to append_md.
		self.assertEqual(updates[1]["page_type"], "Process")
		self.assertEqual(updates[1]["append_md"], "appendable knowledge")
		self.assertNotIn("body_md", updates[1])

		rows = frappe.get_all(
			SKILL,
			filters={"skill_name": "fakeapp-helper"},
			fields=["owner", "scope", "enabled", "user_invocable"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].owner, ADMIN_USER)
		self.assertEqual(rows[0].scope, "Org")
		# Quarantined: created DISABLED, pending admin review before it goes
		# org-wide (untrusted app source drives the instructions).
		self.assertEqual(rows[0].enabled, 0)
		self.assertEqual(rows[0].user_invocable, 1)

	def test_skills_always_created_disabled_pending_review(self):
		# Quarantine (review P1): ingested Org skills are ALWAYS created disabled
		# — the org push being under its 25-slot cap does NOT auto-enable them,
		# because their instructions are derived from untrusted app source.
		payload = {
			"wiki_pages": [],
			"skills": [
				{
					"skill_name": "fakeapp-quarantined",
					"description": "d",
					"instructions": "i",
					"user_invocable": False,
				}
			],
			"summary": "s",
		}
		self._skills.append("fakeapp-quarantined")
		run = self._ingesting_run(payload)
		# push is WELL under the cap — old code would have enabled it.
		with (
			mock.patch("jarvis.chat.wiki.apply_extracted_page_updates", return_value=(0, 0)),
			mock.patch("jarvis.chat.custom_skills.build_push_payload", return_value=[]),
			mock.patch("jarvis.chat.events.publish_to_user"),
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis.ingest(run.name)
		run.reload()
		self.assertEqual(run.status, "Completed")
		self.assertEqual(run.skills_created, 1)
		self.assertEqual(run.skills_deferred, 0)
		rows = frappe.get_all(SKILL, filters={"skill_name": "fakeapp-quarantined"}, fields=["enabled"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].enabled, 0)

	def test_partial_coverage_marker_appended_when_batches_dropped(self):
		payload = {
			"wiki_pages": [
				{
					"title": "Fakeapp overview",
					"page_type": "Process",
					"body_md": "body",
					"mode": "create",
				}
			],
			"skills": [],
			"summary": "s",
		}
		run = self._ingesting_run(payload, notes=json.dumps({"plan": {"batches": 40, "dropped_batches": 3}}))
		captured: list[dict] = []
		with (
			mock.patch(
				"jarvis.chat.wiki.apply_extracted_page_updates",
				side_effect=lambda updates, **kw: (captured.extend(updates), (len(updates), 0))[1],
			),
			mock.patch("jarvis.chat.events.publish_to_user"),
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis.ingest(run.name)
		self.assertEqual(len(captured), 1)
		self.assertIn("Partial coverage: 3", captured[0]["body_md"])

	def test_ingest_error_marks_run_failed(self):
		payload = {
			"wiki_pages": [{"title": "T", "page_type": "Process", "body_md": "b"}],
			"skills": [],
			"summary": "s",
		}
		run = self._ingesting_run(payload)
		with (
			mock.patch(
				"jarvis.chat.wiki.apply_extracted_page_updates",
				side_effect=RuntimeError("boom"),
			),
			mock.patch.object(app_analysis, "_enqueue_tick") as tick,
		):
			app_analysis.ingest(run.name)
		run.reload()
		self.assertEqual(run.status, "Failed")
		self.assertIn("ingest failed", run.error)
		tick.assert_called_once()

	def test_missing_final_reply_marks_run_failed(self):
		conv = self._mk_conv()
		run = self._mk_run(status="Ingesting", conversation=conv)  # no reply at all
		with mock.patch.object(app_analysis, "_enqueue_tick"):
			app_analysis.ingest(run.name)
		run.reload()
		self.assertEqual(run.status, "Failed")

	def test_skill_slug_sanitizer(self):
		self.assertEqual(
			app_analysis._sanitize_skill_slug("Custom-Learned-PO Checker!", "fakeapp"),
			"po-checker",
		)
		self.assertEqual(app_analysis._sanitize_skill_slug("ab", "fakeapp"), "fakeapp-ab")
		self.assertEqual(app_analysis._sanitize_skill_slug("", "fakeapp"), "")


# --------------------------------------------------------------------------- #
# scheduling: one active run bench-wide + stale recovery + cleanup
# --------------------------------------------------------------------------- #
class TestStartRun(_AppLearningTestCase):
	def test_batch_turns_dispatch_at_background_priority(self):
		# Review P1 (day-to-day): analysis turns must not jump ahead of a real
		# user's chat — they dispatch at background priority (interactive=False).
		self._write({"hooks.py": "h\n", "mod/api.py": "import frappe\n"})
		run = self._mk_run()
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			app_analysis.start_run(run.name)
		run.reload()
		self._convs.append(run.conversation)
		self._zips.append(run.zip_path)
		self.assertEqual(enq.call_args.kwargs.get("interactive"), False)

	def test_zip_path_persisted_before_plan_so_a_plan_failure_leaves_no_orphan(self):
		# Review P2: if planning fails after a successful snapshot, the run must
		# be Failed WITH its zip_path recorded so _cleanup_zips can reclaim it.
		self._write({"hooks.py": "h\n"})
		run = self._mk_run()
		with mock.patch.object(app_analysis, "_manifest_from_zip", side_effect=RuntimeError("boom")):
			app_analysis.start_run(run.name)
		run.reload()
		self.assertEqual(run.status, "Failed")
		self.assertTrue(run.zip_path)  # recorded despite the failure
		self.assertTrue(os.path.isfile(run.zip_path))
		self._zips.append(run.zip_path)


class TestScheduling(_AppLearningTestCase):
	def test_single_active_run_serializes_the_queue(self):
		active = self._mk_run(status="Analyzing", conversation="conv-serial-x")
		queued = self._mk_run(app="fakeapp", status="Queued")
		with mock.patch.object(app_analysis, "start_run") as sr:
			app_analysis.process_due()
			sr.assert_not_called()
		frappe.db.set_value(RUN, active.name, "status", "Completed")
		frappe.db.commit()
		app_analysis._bust_active_conversations()
		with mock.patch.object(app_analysis, "start_run") as sr:
			app_analysis.process_due()
			sr.assert_called_once_with(queued.name)

	def test_future_scheduled_run_is_not_due(self):
		self._mk_run(status="Queued", scheduled_at=add_days(now_datetime(), 2))
		with mock.patch.object(app_analysis, "start_run") as sr:
			app_analysis.process_due()
			sr.assert_not_called()

	def test_stale_analyzing_run_retries_once_then_fails(self):
		run = self._analyzing_run()
		# Wipe the turn activity + backdate the start so the run reads stale.
		frappe.db.delete(MSG, {"conversation": run.conversation})
		frappe.db.set_value(RUN, run.name, "started_at", add_to_date(now_datetime(), minutes=-90))
		frappe.db.commit()

		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self.assertTrue(app_analysis._recover_stale_runs())
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		self.assertEqual(json.loads(run.notes)["retries"]["1"], 1)
		enq.assert_called_once()

		with (
			mock.patch("jarvis.chat.api._enqueue_turn"),
			mock.patch.object(app_analysis, "_enqueue_tick") as tick,
		):
			self.assertTrue(app_analysis._recover_stale_runs())
		run.reload()
		self.assertEqual(run.status, "Failed")
		tick.assert_called_once()

	def test_healthy_analyzing_run_is_left_alone(self):
		run = self._analyzing_run()  # intro message is fresh
		with mock.patch("jarvis.chat.api._enqueue_turn") as enq:
			self.assertTrue(app_analysis._recover_stale_runs())
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		enq.assert_not_called()

	def test_cleanup_deletes_only_old_terminal_zips(self):
		zdir = frappe.get_site_path("private", "files", "app_learning")
		os.makedirs(zdir, exist_ok=True)
		old_zip = os.path.join(zdir, "test-cleanup-old.zip")
		new_zip = os.path.join(zdir, "test-cleanup-new.zip")
		for p in (old_zip, new_zip):
			with open(p, "wb") as fh:
				fh.write(b"z")
			self._zips.append(p)
		old = self._mk_run(
			status="Completed",
			zip_path=old_zip,
			finished_at=add_days(now_datetime(), -8),
		)
		new = self._mk_run(status="Failed", zip_path=new_zip, finished_at=now_datetime())
		app_analysis._cleanup_zips()
		self.assertFalse(os.path.exists(old_zip))
		self.assertTrue(os.path.exists(new_zip))
		old.reload()
		new.reload()
		self.assertEqual(old.zip_path, "")
		self.assertEqual(new.zip_path, new_zip)

	def test_tick_never_raises(self):
		with (
			mock.patch.object(app_analysis, "_cleanup_zips", side_effect=RuntimeError("boom")),
			mock.patch.object(app_analysis, "_recover_stale_runs", side_effect=RuntimeError("boom")),
			mock.patch.object(app_analysis, "_due_queued_runs", side_effect=RuntimeError("boom")),
		):
			app_analysis.tick()  # must swallow everything


# --------------------------------------------------------------------------- #
# CDX-19 (residual) — capacity deferral of an analysis turn
# --------------------------------------------------------------------------- #
class TestAppLearningCapacityDefer(_AppLearningTestCase):
	"""When a batch/final turn cannot be admitted (`_enqueue_turn` overloaded), the run must NOT
	fail and must NOT wait forever for a terminal that never comes — it defers, and the tick's
	stale-recovery pass re-attempts the SAME pending turn promptly, bounded then Failed."""

	def test_batch_overload_defers_not_fails_then_resume_heals(self):
		run = self._analyzing_run()  # Analyzing, batches_total=2, batches_done=0
		# A parseable batch-1 reply; its turn-end chains batch 2, whose enqueue OVERLOADS.
		self._reply(run.conversation, _fenced({"notes": ["n1"]}))
		overload = {"ok": False, "overloaded": True, "reason": "busy"}
		with mock.patch("jarvis.chat.api._enqueue_turn", return_value=overload):
			app_analysis.on_turn_end(run.conversation, errored=False)
		run.reload()
		self.assertEqual(run.status, "Analyzing", "overload DEFERS — the run is not failed")
		self.assertEqual(int(run.batches_done), 1, "batch 1 landed; batch 2 is the deferred target")
		cw = json.loads(run.notes or "{}").get("capacity_wait") or {}
		self.assertEqual(cw.get("key"), "2")
		self.assertGreaterEqual(int(cw.get("count") or 0), 1)
		# The stale-recovery pass re-attempts PROMPTLY (no 45-min wait). Capacity freed → dispatch.
		with (
			mock.patch("jarvis.chat.api._enqueue_turn") as enq,
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis._recover_stale_runs()
		enq.assert_called_once()
		run.reload()
		self.assertEqual(run.status, "Analyzing")
		self.assertNotIn("capacity_wait", json.loads(run.notes or "{}"), "marker cleared once dispatched")

	def test_capacity_bounded_then_failed_honestly(self):
		run = self._analyzing_run()
		# Park at the ceiling; the next resume exceeds the bound and fails the run honestly.
		notes = {"capacity_wait": {"key": "1", "count": app_analysis._MAX_CAPACITY_WAITS + 1}}
		frappe.db.set_value(RUN, run.name, "notes", json.dumps(notes))
		frappe.db.commit()
		app_analysis._bust_active_conversations()
		with (
			mock.patch("jarvis.chat.api._enqueue_turn"),
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis._recover_stale_runs()
		run.reload()
		self.assertEqual(run.status, "Failed")
		self.assertIn("busy", (run.error or "").lower())

	def test_cancel_before_resume_prevents_new_turn(self):
		"""CDX-22: a cancel that has landed prevents the capacity-resume pass from enqueuing a new
		batch turn. ``mark_cancelled`` now writes ``Cancelled`` under the SAME per-run lock the
		resume/turn-end critical sections hold, so it can no longer slip between a recheck of
		``Analyzing`` and the enqueue."""
		run = self._analyzing_run()
		frappe.db.set_value(RUN, run.name, "notes", json.dumps({"capacity_wait": {"key": "1", "count": 1}}))
		frappe.db.commit()
		app_analysis._bust_active_conversations()
		app_analysis.mark_cancelled(run.name)
		self.assertEqual(frappe.db.get_value(RUN, run.name, "status"), "Cancelled")
		with (
			mock.patch("jarvis.chat.api._enqueue_turn") as enq,
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis._recover_stale_runs()
		enq.assert_not_called()
		self.assertEqual(frappe.db.get_value(RUN, run.name, "status"), "Cancelled")

	def test_resume_yields_while_the_run_lock_is_held(self):
		"""CDX-22: while a cancel/turn-end HOLDS the per-run lock (its recheck->enqueue critical
		section), the capacity-resume pass (non-blocking on the same lock) YIELDS — so a cancel in
		progress can never be raced into a new-turn enqueue. Once the lock frees, the resume runs."""
		run = self._analyzing_run()
		frappe.db.set_value(RUN, run.name, "notes", json.dumps({"capacity_wait": {"key": "1", "count": 1}}))
		frappe.db.commit()
		app_analysis._bust_active_conversations()
		from jarvis._redis_lock import redis_lock

		lock_name = f"jarvis_app_learning_run:{run.name}"
		with redis_lock(lock_name, timeout_s=60, blocking_timeout_s=0.0) as held:
			self.assertTrue(held)
			with (
				mock.patch("jarvis.chat.api._enqueue_turn") as enq,
				mock.patch.object(app_analysis, "_enqueue_tick"),
			):
				app_analysis._recover_stale_runs()
			enq.assert_not_called()
		# Lock released → the capacity-resume proceeds and enqueues the pending batch.
		with (
			mock.patch("jarvis.chat.api._enqueue_turn") as enq2,
			mock.patch.object(app_analysis, "_enqueue_tick"),
		):
			app_analysis._recover_stale_runs()
		enq2.assert_called_once()
