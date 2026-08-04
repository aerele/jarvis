"""Tests for the org-wiki container mirror (``jarvis.chat.wiki_mirror``):
render determinism + path mapping, index/log shape, mirror_hash diffing,
payload chunking under the fleet body cap, offline no-op, archive deletes and
the doc_events/enqueue gates.

The admin push seam (``jarvis.admin_client.push_wiki_files``) is mocked
throughout — these tests never leave the bench. Page fixtures are inserted as
Administrator (org scope) and swept by slug prefix in tearDown because the
sync commits mid-run (FrappeTestCase rollback can't undo it).
"""

from __future__ import annotations

import contextlib
import hashlib
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from jarvis.chat import wiki_mirror

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

SLUG_PREFIX = "mirrortest"

_PUSH_OK = {"ok": True, "written": 1, "deleted": 0, "pruned": 0}


class WikiMirrorTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._lint_at_before = frappe.db.get_single_value(SETTINGS, "wiki_lint_last_run_at")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(WIKI, {"slug": ["like", f"{SLUG_PREFIX}%"]})
		frappe.db.set_single_value(
			SETTINGS,
			"wiki_lint_last_run_at",
			self._lint_at_before,
			update_modified=False,
		)
		frappe.db.commit()
		super().tearDown()

	def _page(self, slug, page_type="Customer", body="Body.", summary="", scope=None, status="Active"):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": f"{SLUG_PREFIX}--{slug}",
				"title": f"Mirror {slug}",
				"page_type": page_type,
				"body_md": body,
				"summary": summary,
				"scope": scope,
				"status": status,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	@contextlib.contextmanager
	def _mock_push(self, result=_PUSH_OK):
		"""Patch the managed-tenant gate + the admin push seam. ``result``
		is push_wiki_files' return for every call (None = offline)."""
		with (
			mock.patch("jarvis.admin_client.push_wiki_files", return_value=result) as push,
		):
			yield push

	@staticmethod
	def _pushed_paths(push_mock) -> list[str]:
		paths = []
		for call in push_mock.call_args_list:
			paths += [f["path"] for f in call.kwargs["files"]]
		return paths


# --------------------------------------------------------------------------- #
# renders
# --------------------------------------------------------------------------- #
class TestRenders(WikiMirrorTestCase):
	def _row(self, **overrides):
		row = frappe._dict(
			name=f"{SLUG_PREFIX}--acme",
			slug=f"{SLUG_PREFIX}--acme",
			title="Acme Corp",
			page_type="Customer",
			scope="Org",
			status="Active",
			summary="Prefers morning deliveries.",
			body_md="Acme buys monthly. See [[item--widget]].",
			sources='[{"date": "2026-07-01", "kind": "voice", "ref": "VN-1", "user": "a@x.com"}]',
			last_confirmed_at=now_datetime(),
			contradiction_flag=0,
			modified="2026-07-05 10:00:00",
		)
		row.update(overrides)
		return row

	def test_render_page_shape(self):
		path, content = wiki_mirror.render_page(self._row())
		self.assertEqual(path, f"wiki/customers/{SLUG_PREFIX}--acme.md")
		self.assertTrue(content.startswith("---\n"))
		self.assertIn('title: "Acme Corp"', content)
		self.assertIn("type: Customer", content)
		self.assertIn("updated: 2026-07-05", content)
		self.assertIn("stale: false", content)
		self.assertIn("contradiction: false", content)
		self.assertIn("Prefers morning deliveries.", content)
		# body (and its [[slug]] links) pass through untouched
		self.assertIn("See [[item--widget]].", content)
		self.assertIn("## Sources", content)
		self.assertIn("- 2026-07-01 · voice · VN-1 · a@x.com", content)
		self.assertTrue(content.endswith("\n"))

	def test_render_page_stale_and_contradiction_flags(self):
		row = self._row(last_confirmed_at="2020-01-01 00:00:00", contradiction_flag=1)
		_path, content = wiki_mirror.render_page(row)
		self.assertIn("stale: true", content)
		self.assertIn("contradiction: true", content)

	def test_render_page_is_deterministic(self):
		row = self._row()
		first = wiki_mirror.render_page(row)
		second = wiki_mirror.render_page(row)
		self.assertEqual(first, second)
		self.assertEqual(
			hashlib.sha256(first[1].encode("utf-8")).hexdigest(),
			hashlib.sha256(second[1].encode("utf-8")).hexdigest(),
		)

	def test_render_page_type_dir_mapping(self):
		expected = {
			"Customer": "customers",
			"Supplier": "suppliers",
			"Item": "items",
			"Process": "processes",
			"Doctype": "doctypes",
			"Exception": "exceptions",
			"Integration": "integrations",
			"People": "people",
			"Org": "org",
		}
		self.assertEqual(wiki_mirror.TYPE_DIRS, expected)
		for page_type, type_dir in expected.items():
			path, _content = wiki_mirror.render_page(self._row(page_type=page_type))
			self.assertEqual(path, f"wiki/{type_dir}/{SLUG_PREFIX}--acme.md")
		# defensive: unknown type falls back rather than crashing the sync
		path, _content = wiki_mirror.render_page(self._row(page_type="Weird"))
		self.assertEqual(path, f"wiki/org/{SLUG_PREFIX}--acme.md")

	def test_render_index_groups_and_clips(self):
		cust = self._page("cust", page_type="Customer", summary="s " * 120)
		proc = self._page("proc", page_type="Process", summary="Short.")
		user_page = self._page("mine", scope="User")
		archived = self._page("gone", status="Archived")

		path, content = wiki_mirror.render_index()
		self.assertEqual(path, "wiki/index.md")
		self.assertIn("active page(s)", content)
		self.assertIn("## Customer (", content)
		self.assertIn("## Process (", content)
		clipped = (" ".join(("s " * 120).split()))[:100]
		self.assertIn(f"- [[{cust.name}]] — {clipped}", content)
		self.assertIn(f"- [[{proc.name}]] — Short.", content)
		# user-scope and archived pages never reach the org index
		self.assertNotIn(user_page.name, content)
		self.assertNotIn(archived.name, content)

	def test_render_log_shape_and_order(self):
		doc = self._page("logged")
		frappe.db.set_value(
			WIKI,
			doc.name,
			{"creation": "2026-07-01 09:00:00", "modified": "2026-07-03 09:00:00"},
			update_modified=False,
		)
		frappe.db.set_single_value(
			SETTINGS,
			"wiki_lint_last_run_at",
			"2026-07-04 12:00:00",
			update_modified=False,
		)

		path, content = wiki_mirror.render_log()
		self.assertEqual(path, "wiki/log.md")
		created = f"## [2026-07-01] created | {doc.name}"
		updated = f"## [2026-07-03] updated | {doc.name}"
		lint = "## [2026-07-04] lint | org-wiki"
		for line in (created, updated, lint):
			self.assertIn(line, content)
		# newest first
		self.assertLess(content.index(lint), content.index(updated))
		self.assertLess(content.index(updated), content.index(created))

		# archival shows as its own action
		frappe.db.set_value(WIKI, doc.name, "status", "Archived", update_modified=False)
		_path, content = wiki_mirror.render_log()
		self.assertIn(f"## [2026-07-03] archived | {doc.name}", content)

	def test_render_log_excludes_non_org_pages(self):
		user_page = self._page("private", scope="User")
		_path, content = wiki_mirror.render_log()
		self.assertNotIn(user_page.name, content)


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
class TestSync(WikiMirrorTestCase):
	def test_sync_pushes_new_page_then_hash_diff_skips_it(self):
		doc = self._page("acme", summary="Acme summary.")
		wire_path = f"customers/{doc.name}.md"

		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		paths = self._pushed_paths(push)
		self.assertIn(wire_path, paths)
		self.assertIn("index.md", paths)
		self.assertIn("log.md", paths)
		# hash stamped = sha256 of the current render
		_p, content = wiki_mirror.render_page(frappe.get_doc(WIKI, doc.name))
		self.assertEqual(
			frappe.db.get_value(WIKI, doc.name, "mirror_hash"),
			hashlib.sha256(content.encode("utf-8")).hexdigest(),
		)

		# unchanged page -> no file in the next payload; index/log always ride
		with self._mock_push() as push2:
			out2 = wiki_mirror.sync()
		self.assertTrue(out2["ok"])
		paths2 = self._pushed_paths(push2)
		self.assertNotIn(wire_path, paths2)
		self.assertIn("index.md", paths2)
		self.assertIn("log.md", paths2)
		last_kwargs = push2.call_args_list[-1].kwargs
		self.assertIsNone(last_kwargs["known_paths"])
		self.assertNotIn(wire_path, last_kwargs["delete"] or [])

	def test_full_sync_resends_and_sends_known_paths(self):
		doc = self._page("acme", summary="Acme summary.")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push():
			wiki_mirror.sync()

		with self._mock_push() as push:
			out = wiki_mirror.sync(full=True)
		self.assertTrue(out["ok"])
		self.assertTrue(out["full"])
		# full bypasses the hash diff (a wiped container rebuilds)
		self.assertIn(wire_path, self._pushed_paths(push))
		known = push.call_args_list[-1].kwargs["known_paths"]
		self.assertIn(wire_path, known)
		self.assertIn("index.md", known)
		self.assertIn("log.md", known)

	def test_sync_offline_is_a_logged_noop(self):
		doc = self._page("acme")
		with self._mock_push(result=None):
			out = wiki_mirror.sync()  # must not raise
		self.assertFalse(out["ok"])
		self.assertTrue(out["reason"])
		# nothing stamped -> the next sync retries the page
		self.assertFalse(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

	def test_archived_page_gets_deleted_and_hash_cleared(self):
		doc = self._page("acme")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push():
			wiki_mirror.sync()
		self.assertTrue(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

		# status flip via db (the SPA archive path saves; the sync only cares
		# about the stored status + the stamped hash)
		frappe.db.set_value(WIKI, doc.name, "status", "Archived", update_modified=False)
		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		self.assertNotIn(wire_path, self._pushed_paths(push))
		self.assertIn(wire_path, push.call_args_list[-1].kwargs["delete"])
		self.assertFalse(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

		# delete confirmed -> not re-sent on the next sync
		with self._mock_push() as push2:
			wiki_mirror.sync()
		last = push2.call_args_list[-1].kwargs
		self.assertNotIn(wire_path, last["delete"] or [])

	def test_demoted_to_user_scope_page_gets_deleted_and_hash_cleared(self):
		doc = self._page("acme")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push() as push:
			wiki_mirror.sync()
		self.assertIn(wire_path, self._pushed_paths(push))
		self.assertTrue(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

		doc.reload()
		doc.scope = "User"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		# demotion never re-suffixes the slug, so the orphan sits at the very
		# path the mirror pushed
		self.assertEqual(
			wiki_mirror.page_path(frappe.get_doc(WIKI, doc.name)),
			f"wiki/{wire_path}",
		)

		with self._mock_push() as push2:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		self.assertNotIn(wire_path, self._pushed_paths(push2))
		self.assertIn(wire_path, push2.call_args_list[-1].kwargs["delete"])
		self.assertFalse(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

		# delete confirmed -> not re-sent forever
		with self._mock_push() as push3:
			wiki_mirror.sync()
		self.assertNotIn(wire_path, push3.call_args_list[-1].kwargs["delete"] or [])

	def test_demoted_to_role_scope_page_gets_deleted(self):
		doc = self._page("shared")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push():
			wiki_mirror.sync()

		doc.reload()
		doc.scope = "Role"
		doc.target_role = "System Manager"
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		self.assertNotIn(wire_path, self._pushed_paths(push))
		self.assertIn(wire_path, push.call_args_list[-1].kwargs["delete"])
		self.assertFalse(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))
		_path, index = wiki_mirror.render_index()
		self.assertNotIn(doc.name, index)

	def test_demotion_with_a_stale_hash_still_prunes_on_the_full_sync(self):
		doc = self._page("acme")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push():
			wiki_mirror.sync()
		self.assertTrue(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))

		# `doc` was loaded before the sync stamped mirror_hash, and the stamp
		# leaves `modified` alone, so this save writes the stale empty hash
		# back without tripping the timestamp check - exactly what a Desk form
		# opened before a sync does. The pre-save scope is the only signal left.
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			doc.scope = "User"
			doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(frappe.db.get_value(WIKI, doc.name, "mirror_hash"))
		enq.assert_called_once_with(full=True)

		with self._mock_push() as push:
			wiki_mirror.sync(full=True)
		known = push.call_args_list[-1].kwargs["known_paths"]
		self.assertNotIn(wire_path, known)

	def test_repromoted_page_is_pushed_again(self):
		doc = self._page("acme")
		wire_path = f"customers/{doc.name}.md"
		with self._mock_push():
			wiki_mirror.sync()

		doc.reload()
		doc.scope = "User"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		with self._mock_push():
			wiki_mirror.sync()

		# same content as before the demotion: the re-push only happens because
		# the delete cleared the stamped hash
		doc.reload()
		doc.scope = "Org"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		with self._mock_push() as push:
			wiki_mirror.sync()
		self.assertIn(wire_path, self._pushed_paths(push))
		self.assertNotIn(wire_path, push.call_args_list[-1].kwargs["delete"] or [])

	def test_sync_chunks_batches_under_payload_cap(self):
		for i in range(12):
			self._page(f"big-{i}", body="a" * 19000)

		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		self.assertGreaterEqual(push.call_count, 2)
		self.assertEqual(out["calls"], push.call_count)
		for call in push.call_args_list:
			payload = sum(len(f["content_b64"]) + len(f["path"]) + 64 for f in call.kwargs["files"])
			self.assertLessEqual(payload, wiki_mirror.MAX_CALL_PAYLOAD_BYTES)
		paths = self._pushed_paths(push)
		for i in range(12):
			self.assertIn(f"customers/{SLUG_PREFIX}--big-{i}.md", paths)
		self.assertIn("index.md", paths)
		self.assertIn("log.md", paths)

	def test_sync_partial_failure_leaves_later_batches_unstamped(self):
		for i in range(12):
			self._page(f"big-{i}", body="a" * 19000)
		with (
			mock.patch(
				"jarvis.admin_client.push_wiki_files",
				side_effect=[_PUSH_OK, None, None],
			) as push,
		):
			out = wiki_mirror.sync()
		self.assertFalse(out["ok"])
		self.assertEqual(push.call_count, 2)
		# first batch stamped, the rest left for retry
		stamped = frappe.get_all(
			WIKI,
			filters={
				"slug": ["like", f"{SLUG_PREFIX}%"],
				"mirror_hash": ["!=", ""],
			},
			pluck="name",
		)
		self.assertTrue(stamped)
		self.assertLess(len(stamped), 12)


# --------------------------------------------------------------------------- #
# triggers
# --------------------------------------------------------------------------- #
class TestTriggers(WikiMirrorTestCase):
	def test_doc_event_triggers_only_for_org_scope(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Org"), "on_update")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope=None), "after_insert")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope=""), "on_update")
		self.assertEqual(enq.call_count, 3)
		enq.assert_called_with(full=False)

		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="User"), "on_update")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Role"), "on_trash")
		enq.assert_not_called()

	def test_doc_event_trash_requests_full_sync(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Org"), "on_trash")
		enq.assert_called_once_with(full=True)

	def test_doc_event_prunes_a_mirrored_non_org_page(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="User", mirror_hash="deadbeef"), "on_update")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Role", mirror_hash="deadbeef"), "on_trash")
		self.assertEqual(enq.call_count, 2)
		for call in enq.call_args_list:
			self.assertTrue(call.kwargs["full"])

	def test_doc_event_falls_back_to_the_pre_save_scope(self):
		demoted = frappe._dict(scope="User", mirror_hash="")
		demoted.get_doc_before_save = lambda: frappe._dict(scope="Org")
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(demoted, "on_update")
		enq.assert_called_once_with(full=True)

		# never mirrored: no hash, and it was already non-Org before the save
		untouched = frappe._dict(scope="User", mirror_hash="")
		untouched.get_doc_before_save = lambda: frappe._dict(scope="User")
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq2:
			wiki_mirror.on_wiki_page_change(untouched, "on_update")
		enq2.assert_not_called()

	def test_doc_event_swallows_enqueue_errors(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync", side_effect=Exception("redis down")):
			# must not raise into the save path
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Org"), "on_update")

	def test_enqueue_sync_is_suppressed_in_tests_unless_overridden(self):
		prev_in_test = frappe.flags.in_test
		frappe.flags.in_test = True
		try:
			with mock.patch("frappe.enqueue") as enq:
				wiki_mirror.enqueue_sync()
			enq.assert_not_called()

			frappe.flags.jarvis_test_wiki_mirror_enqueue = True
			try:
				with mock.patch("frappe.enqueue") as enq2:
					wiki_mirror.enqueue_sync(full=True)
				enq2.assert_called_once()
				kwargs = enq2.call_args.kwargs
				self.assertEqual(kwargs["queue"], "short")
				self.assertEqual(kwargs["job_id"], wiki_mirror.JOB_ID_FULL)
				self.assertTrue(kwargs["deduplicate"])
				self.assertTrue(kwargs["full"])

				with mock.patch("frappe.enqueue") as enq3:
					wiki_mirror.enqueue_sync()
				self.assertEqual(enq3.call_args.kwargs["job_id"], wiki_mirror.JOB_ID)
			finally:
				frappe.flags.jarvis_test_wiki_mirror_enqueue = False
		finally:
			frappe.flags.in_test = prev_in_test
