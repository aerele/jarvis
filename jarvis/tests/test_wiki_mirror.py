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

import base64
import contextlib
import hashlib
import json
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

	def _page(
		self,
		slug,
		page_type="Customer",
		body="Body.",
		summary="",
		scope=None,
		status="Active",
		manual_links=None,
	):
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
		if manual_links is not None:
			doc.manual_links = json.dumps(manual_links)
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

	@staticmethod
	def _pushed_content(push_mock, wire_path: str) -> str:
		"""The decoded markdown actually sent for one wire path (the file that
		lands in the container workspace), or "" when it was never pushed."""
		for call in push_mock.call_args_list:
			for f in call.kwargs["files"]:
				if f["path"] == wire_path:
					return base64.b64decode(f["content_b64"]).decode("utf-8")
		return ""


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

	def test_render_page_emits_curated_links_as_a_related_section(self):
		"""#494: curated links live in manual_links, out of body_md, so the
		mirror never carried them and the agent could not see one at all."""
		row = self._row(manual_links=json.dumps(["item--widget", "process--billing"]))
		_path, content = wiki_mirror.render_page(row)
		self.assertIn("## Related", content)
		self.assertIn("- [[item--widget]]", content)
		self.assertIn("- [[process--billing]]", content)
		# curation order is preserved, and Related sits between body and Sources
		self.assertLess(content.index("- [[item--widget]]"), content.index("- [[process--billing]]"))
		self.assertLess(content.index("## Related"), content.index("## Sources"))
		self.assertLess(content.index("Acme buys monthly."), content.index("## Related"))

	def test_render_page_has_no_related_section_without_curated_links(self):
		for raw in (None, "", "[]", "not json", '{"not": "a list"}'):
			_path, content = wiki_mirror.render_page(self._row(manual_links=raw))
			self.assertNotIn("## Related", content)

	def test_render_page_related_is_filtered_to_mirrored_pages(self):
		"""Scope discipline runs through Related: a curated link out to a
		Role/User or archived page must not put its slug on the org-shared
		container, nor leave a dangling [[link]] there."""
		row = self._row(manual_links=json.dumps(["item--widget", "secret--u-bob"]))
		_path, content = wiki_mirror.render_page(row, {"item--widget"})
		self.assertIn("- [[item--widget]]", content)
		self.assertNotIn("secret--u-bob", content)

	def test_render_page_related_skips_self_and_caps(self):
		me = f"{SLUG_PREFIX}--acme"
		targets = [me] + [f"item--w{i}" for i in range(wiki_mirror._MAX_RELATED + 5)]
		_path, content = wiki_mirror.render_page(self._row(manual_links=json.dumps(targets)))
		self.assertNotIn(f"- [[{me}]]", content)
		self.assertEqual(content.count("- [[item--w"), wiki_mirror._MAX_RELATED)

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
	def test_sync_pushes_a_file_carrying_the_curated_links(self):
		"""#494 end to end: the file that reaches the container workspace has
		the curated links in it, so a native read/grep by the agent finds them."""
		target = self._page("target")
		curator = self._page("curator", body="No wikilinks in this body.", manual_links=[target.name])

		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		content = self._pushed_content(push, f"customers/{curator.name}.md")
		self.assertIn("## Related", content)
		self.assertIn(f"- [[{target.name}]]", content)

	def test_sync_keeps_non_org_curated_targets_off_the_org_container(self):
		private = self._page("private", scope="User")
		archived = self._page("bygone", status="Archived")
		visible = self._page("visible")
		curator = self._page(
			"curator",
			manual_links=[private.name, archived.name, visible.name],
		)

		with self._mock_push() as push:
			wiki_mirror.sync()
		content = self._pushed_content(push, f"customers/{curator.name}.md")
		self.assertIn(f"- [[{visible.name}]]", content)
		self.assertNotIn(private.name, content)
		self.assertNotIn(archived.name, content)

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
		enq.assert_called_once_with(full=True, after_commit=True)

		with self._mock_push() as push:
			wiki_mirror.sync(full=True)
		known = push.call_args_list[-1].kwargs["known_paths"]
		self.assertNotIn(wire_path, known)

	def test_demotion_that_also_moves_page_type_prunes_the_old_path(self):
		doc = self._page("acme", page_type="Customer")
		old_path = f"customers/{doc.name}.md"
		with self._mock_push() as push:
			wiki_mirror.sync()
		self.assertIn(old_path, self._pushed_paths(push))

		# page_path() reads the CURRENT page_type, so a demotion that also moves
		# the page to another type dir derives a delete path that no file sits
		# at. The full sync the prune asks for is what actually removes it.
		doc.reload()
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			doc.scope = "User"
			doc.page_type = "Supplier"
			doc.save(ignore_permissions=True)
		frappe.db.commit()
		enq.assert_called_once_with(full=True, after_commit=True)

		with self._mock_push() as push2:
			wiki_mirror.sync(full=True)
		known = push2.call_args_list[-1].kwargs["known_paths"]
		self.assertNotIn(old_path, known)
		self.assertNotIn(f"suppliers/{doc.name}.md", known)

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
class TestSyncSerialisation(WikiMirrorTestCase):
	"""#622: ``_sync`` derives ``known_paths`` from a snapshot taken at its top, but the
	prune only executes on the final push, after every render, hash and earlier batch. A
	page an incremental sync wrote inside that window is absent from the list and is
	pruned even though it is current, which shows up as a wiki file silently vanishing
	from the container with no error anywhere.

	The two JOB_IDs deliberately let an incremental and a FULL sync be QUEUED at once, so
	nothing serialised their EXECUTION before this."""

	@contextlib.contextmanager
	def _lock_denied(self, *a, **k):
		"""Stand-in for redis_lock that never grants (another sync is in flight)."""
		yield False

	def test_a_contended_sync_does_not_run(self):
		self._page("acme")
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync") as inner,
			mock.patch.object(wiki_mirror, "enqueue_sync"),
		):
			out = wiki_mirror.sync()
		self.assertFalse(inner.called, "a second sync must not walk the pages concurrently")
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("requeued"))

	def test_a_contended_sync_requeues_instead_of_dropping(self):
		"""Skipping looks safe because a FULL sync pushes everything anyway, but an
		incremental skipped while another INCREMENTAL holds the lock has its page change
		stranded: the holder's snapshot may predate it and there is no periodic sweep."""
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			mock.patch.object(wiki_mirror, "enqueue_sync") as enq,
		):
			wiki_mirror.sync()
		self.assertTrue(enq.called, "contended work must be re-queued, never dropped")

	def test_a_contended_full_sync_requeues_as_full(self):
		"""A prune request must not be downgraded to an incremental: only a FULL sync
		sends known_paths, so a downgrade would silently lose the prune."""
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			mock.patch.object(wiki_mirror, "enqueue_sync") as enq,
		):
			wiki_mirror.sync(full=True)
		self.assertTrue(enq.call_args.kwargs.get("full"), "the FULL flag must survive the re-queue")

	def test_a_contended_sync_does_not_overwrite_the_last_synced_line(self):
		"""``skipped`` keeps _stamp_sync_status quiet, so the Wiki tab does not report a
		failure that did not happen."""
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			mock.patch.object(wiki_mirror, "enqueue_sync", return_value=True),
			mock.patch.object(wiki_mirror, "_stamp_sync_status", wraps=wiki_mirror._stamp_sync_status),
		):
			out = wiki_mirror.sync()
		self.assertTrue(out.get("skipped"), "the skipped flag is what silences the status stamp")

	# ---- the retry must use a DIFFERENT job id, or it is silently declined -------- #

	@contextlib.contextmanager
	def _real_enqueue(self):
		"""Patch frappe.enqueue itself, NOT enqueue_sync, so the job_id is exercised."""
		frappe.flags.jarvis_test_wiki_mirror_enqueue = True
		try:
			with mock.patch.object(frappe, "enqueue") as enq:
				yield enq
		finally:
			frappe.flags.jarvis_test_wiki_mirror_enqueue = False

	def test_the_retry_does_not_reuse_the_running_jobs_id(self):
		"""THE bug the first cut of this fix shipped with. ``frappe.enqueue`` with
		``deduplicate=True`` declines a job whose id is already QUEUED or STARTED
		(background_jobs.py: "Not queueing job ... because it is in queue already"), and a
		contended worker is itself STARTED under its own id. Re-queueing under that id was
		therefore silently skipped and the work dropped, while the result still claimed
		``requeued: True``.

		Patched at ``frappe.enqueue`` on purpose: mocking ``enqueue_sync``, as the earlier
		tests here do, cannot see the job id at all, which is exactly why this shipped
		green."""
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			self._real_enqueue() as enq,
		):
			wiki_mirror.sync()
		job_id = enq.call_args.kwargs["job_id"]
		self.assertEqual(job_id, wiki_mirror.JOB_ID_RETRY)
		self.assertNotEqual(job_id, wiki_mirror.JOB_ID, "the retry must not collide with the running job")

	def test_the_full_retry_uses_the_full_retry_id(self):
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			self._real_enqueue() as enq,
		):
			wiki_mirror.sync(full=True)
		self.assertEqual(enq.call_args.kwargs["job_id"], wiki_mirror.JOB_ID_FULL_RETRY)
		self.assertTrue(enq.call_args.kwargs["full"], "a prune request must not downgrade")

	def test_a_redis_fault_is_requeued_not_just_logged(self):
		"""``redis_lock`` propagates real Redis errors rather than yielding False, so a
		cache blip used to land in the generic handler and be reported as "sync crashed"
		with no re-queue, stranding the page edit for the same reason as #622 itself."""

		@contextlib.contextmanager
		def _lock_explodes(*a, **k):
			raise RuntimeError("redis gone")
			yield  # pragma: no cover

		with (
			mock.patch("jarvis._redis_lock.redis_lock", _lock_explodes),
			mock.patch.object(wiki_mirror, "_sync"),
			mock.patch.object(wiki_mirror, "enqueue_sync", return_value=True) as enq,
		):
			out = wiki_mirror.sync()
		self.assertTrue(enq.called, "a lock fault must still re-queue the work")
		self.assertTrue(out.get("requeued"))

	def test_a_failed_requeue_is_reported_honestly(self):
		"""``enqueue_sync`` swallows enqueue failures (Redis down). Claiming a re-queue
		that never happened is worse than admitting it: nothing else comes back for this
		change."""
		with (
			mock.patch("jarvis._redis_lock.redis_lock", self._lock_denied),
			mock.patch.object(wiki_mirror, "_sync"),
			mock.patch.object(wiki_mirror, "enqueue_sync", return_value=False),
		):
			out = wiki_mirror.sync()
		self.assertFalse(out.get("requeued"))
		self.assertFalse(out.get("skipped"), "a lost change must reach the status line")
		self.assertIn("RE-QUEUE FAILED", out["reason"])

	def test_an_uncontended_sync_still_syncs(self):
		"""Control: the lock must not change the ordinary path."""
		doc = self._page("acme")
		with self._mock_push() as push:
			out = wiki_mirror.sync()
		self.assertTrue(out["ok"])
		self.assertIn(f"customers/{doc.name}.md", self._pushed_paths(push))


class TestTriggers(WikiMirrorTestCase):
	def test_doc_event_triggers_only_for_org_scope(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Org"), "on_update")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope=None), "after_insert")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope=""), "on_update")
		self.assertEqual(enq.call_count, 3)
		enq.assert_called_with(full=False, after_commit=True)

		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="User"), "on_update")
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Role"), "on_trash")
		enq.assert_not_called()

	def test_doc_event_trash_requests_full_sync(self):
		with mock.patch.object(wiki_mirror, "enqueue_sync") as enq:
			wiki_mirror.on_wiki_page_change(frappe._dict(scope="Org"), "on_trash")
		enq.assert_called_once_with(full=True, after_commit=True)

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
		enq.assert_called_once_with(full=True, after_commit=True)

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
					wiki_mirror.enqueue_sync(full=True, after_commit=True)
				enq2.assert_called_once()
				kwargs = enq2.call_args.kwargs
				self.assertEqual(kwargs["queue"], "short")
				self.assertEqual(kwargs["job_id"], wiki_mirror.JOB_ID_FULL)
				self.assertTrue(kwargs["deduplicate"])
				self.assertTrue(kwargs["full"])
				# a job queued mid-save would read the pre-save row
				self.assertTrue(kwargs["enqueue_after_commit"])

				with mock.patch("frappe.enqueue") as enq3:
					wiki_mirror.enqueue_sync()
				self.assertEqual(enq3.call_args.kwargs["job_id"], wiki_mirror.JOB_ID)
				# the manual endpoint writes nothing, so its request may never
				# commit; deferring there would drop the job
				self.assertFalse(enq3.call_args.kwargs["enqueue_after_commit"])
			finally:
				frappe.flags.jarvis_test_wiki_mirror_enqueue = False
		finally:
			frappe.flags.in_test = prev_in_test


@contextlib.contextmanager
def _wiki_disabled():
	"""Drive the real operator toggle (#493), never a patched ``wiki_enabled``:
	the question these tests ask is whether the production callers consult it."""
	frappe.db.set_single_value(SETTINGS, "wiki_enabled", 0, update_modified=False)
	try:
		yield
	finally:
		frappe.db.set_single_value(SETTINGS, "wiki_enabled", 1, update_modified=False)


class TestMirrorKillSwitch(WikiMirrorTestCase):
	"""#493: the mirror's doc_events fire on every save, tool write and archive,
	so with the wiki switched off the container kept receiving page markdown. The
	workspace ``wiki/`` folder is the agent's cheap native read channel, which is
	precisely what an operator reaching for the kill switch wants stopped."""

	def test_sync_pushes_nothing_when_the_wiki_is_off(self):
		self._page("killswitch")
		with _wiki_disabled():
			with self._mock_push() as push:
				out = wiki_mirror.sync()
		push.assert_not_called()
		self.assertFalse(out["ok"])

	def test_sync_still_pushes_when_the_wiki_is_on(self):
		"""The regression direction: nothing about the normal state moved."""
		self._page("killswitch-on")
		with self._mock_push() as push:
			out = wiki_mirror.sync()
		push.assert_called()
		self.assertTrue(out["ok"])
		self.assertIn(f"customers/{SLUG_PREFIX}--killswitch-on.md", self._pushed_paths(push))

	def test_a_short_circuited_sync_does_not_overwrite_the_last_sync_status(self):
		"""It reports the last real reconciliation. Stamping "disabled" over it
		would erase the operator's record of when the mirror last actually ran."""
		self._page("killswitch-status")
		with self._mock_push():
			wiki_mirror.sync()
		before = frappe.db.get_single_value(SETTINGS, "wiki_mirror_last_sync_status")
		with _wiki_disabled():
			wiki_mirror.sync()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "wiki_mirror_last_sync_status"), before)

	def test_enqueue_is_suppressed_when_the_wiki_is_off(self):
		"""Checked ahead of the in_test suppression, so opting into real enqueues
		still sees the kill switch. This is the doc_events path: on_wiki_page_change
		reaches the mirror through enqueue_sync."""
		frappe.flags.jarvis_test_wiki_mirror_enqueue = True
		try:
			with _wiki_disabled():
				with mock.patch("frappe.enqueue") as enq:
					self.assertFalse(wiki_mirror.enqueue_sync())
			enq.assert_not_called()
			with mock.patch("frappe.enqueue") as enq:
				self.assertTrue(wiki_mirror.enqueue_sync())
			enq.assert_called_once()
		finally:
			frappe.flags.jarvis_test_wiki_mirror_enqueue = False

	def test_a_page_saved_with_the_wiki_off_triggers_no_mirror_job(self):
		"""The whole doc_events chain, driven by a real save."""
		frappe.flags.jarvis_test_wiki_mirror_enqueue = True
		try:
			with _wiki_disabled():
				with mock.patch("frappe.enqueue") as enq:
					self._page("killswitch-save")
			enq.assert_not_called()
		finally:
			frappe.flags.jarvis_test_wiki_mirror_enqueue = False

	def test_a_page_saved_with_the_wiki_on_still_triggers_the_mirror_job(self):
		"""The paired direction, and the one an operator worries about.

		``TestTriggers`` drives ``on_wiki_page_change`` directly with ``enqueue_sync``
		mocked, so without this the real save-to-enqueue chain was only proven in the
		OFF direction and a gate that killed it outright would still have looked
		green. Asserts the queued METHOD too, so a save routed to some other job
		could not read as a pass."""
		frappe.flags.jarvis_test_wiki_mirror_enqueue = True
		try:
			with mock.patch("frappe.enqueue") as enq:
				self._page("killswitch-save-on")
			enq.assert_called()
			self.assertEqual(enq.call_args[0][0], wiki_mirror.JOB_METHOD)
		finally:
			frappe.flags.jarvis_test_wiki_mirror_enqueue = False
