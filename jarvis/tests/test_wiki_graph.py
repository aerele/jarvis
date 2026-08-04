"""Tenant-side wiki-utilization graph compute + push (jarvis.chat.wiki_graph)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import wiki as wiki_mod
from jarvis.chat import wiki_graph
from jarvis.tools.read_wiki import read_wiki

WIKI = "Jarvis Wiki Page"
_PREFIX = "graphtest"
# Framework built-ins (never tenant data) used as a real Role/User link target.
_ROLE = "System Manager"
_USER = "Administrator"


def _delete_pages():
	for name in frappe.get_all(WIKI, filters={"slug": ["like", f"{_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc(WIKI, name, force=True, ignore_permissions=True)


class WikiGraphTestCase(FrappeTestCase):
	"""Page fixtures swept by slug prefix, shared by the compute tests and the
	reader-tool tests below."""

	def setUp(self):
		frappe.set_user("Administrator")
		_delete_pages()

	def tearDown(self):
		_delete_pages()
		frappe.set_user("Administrator")

	def _page(
		self,
		slug,
		title,
		scope="Org",
		page_type="Org",
		target_role=None,
		target_user=None,
		sources=None,
		body_md="secret body that must not travel",
		manual_links=None,
		summary=None,
	):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": slug,
				"title": title,
				"page_type": page_type,
				"scope": scope,
				"target_role": target_role,
				"target_user": target_user,
				"status": "Active",
				"body_md": body_md,
				"summary": summary,
			}
		).insert(ignore_permissions=True)
		vals = {}
		if sources is not None:
			vals["sources"] = json.dumps(sources)  # read_only on the form
		if manual_links is not None:
			vals["manual_links"] = json.dumps(manual_links)
		if vals:
			frappe.db.set_value(WIKI, doc.name, vals, update_modified=False)
		return doc


class TestWikiGraphCompute(WikiGraphTestCase):
	def _graph(self):
		return wiki_graph.compute_graph()

	def _node(self, g, node_id):
		return next((n for n in g["nodes"] if n["id"] == node_id), None)

	def test_org_page_scope_edge_and_no_body(self):
		doc = self._page(f"{_PREFIX}-org", "Org Page")
		g = self._graph()
		pid = f"page:{doc.name}"
		node = self._node(g, pid)
		self.assertIsNotNone(node)
		self.assertEqual(node["kind"], "page")
		self.assertEqual(node["scope"], "Org")
		# body_md must never be in the payload.
		self.assertNotIn("body_md", node)
		self.assertIn({"source": pid, "target": "org", "kind": "scope"}, g["edges"])

	def test_admin_push_gets_created_without_content(self):
		"""Finding 11: the daily admin push (include_content=False) still needs
		`created` for the Evolution tab; only `summary` is content-gated."""
		doc = self._page(f"{_PREFIX}-created", "Created Page")
		g = self._graph()
		node = self._node(g, f"page:{doc.name}")
		self.assertTrue(node["created"])  # non-empty, e.g. "2026-07-08"
		self.assertNotIn("summary", node)

	def test_user_scope_edge_falls_back_to_org_when_user_cap_hit(self):
		"""Finding 14: when MAX_USERS is hit, _user_node returns a uid string
		without creating the node — a User-scope page must fall back to the org
		edge rather than dangling to a nonexistent node."""
		doc = self._page(f"{_PREFIX}-usercap", "User Page", scope="User", target_user=_USER)
		with patch.object(wiki_graph, "MAX_USERS", 0):
			g = self._graph()
		pid = f"page:{doc.name}"
		self.assertIsNone(self._node(g, f"user:{_USER}"))  # cap hit, no node
		self.assertIn({"source": pid, "target": "org", "kind": "scope"}, g["edges"])

	def test_role_page_makes_role_node_and_scope_edge(self):
		doc = self._page(f"{_PREFIX}-role", "Role Page", scope="Role", target_role=_ROLE)
		g = self._graph()
		pid = f"page:{doc.name}"
		rid = f"role:{_ROLE}"
		self.assertIsNotNone(self._node(g, rid))
		self.assertIn({"source": pid, "target": rid, "kind": "scope"}, g["edges"])

	def test_user_page_makes_user_node_and_scope_edge(self):
		doc = self._page(f"{_PREFIX}-user", "User Page", scope="User", target_user=_USER)
		g = self._graph()
		pid = f"page:{doc.name}"
		uid = f"user:{_USER}"
		self.assertIsNotNone(self._node(g, uid))
		self.assertIn({"source": pid, "target": uid, "kind": "scope"}, g["edges"])

	def test_authored_edge_weighted_from_sources(self):
		doc = self._page(
			f"{_PREFIX}-auth",
			"Authored",
			sources=[
				{"date": "2026-07-01", "kind": "tool", "ref": None, "user": _USER},
				{"date": "2026-07-02", "kind": "manual", "ref": None, "user": _USER},
			],
		)
		g = self._graph()
		pid, uid = f"page:{doc.name}", f"user:{_USER}"
		edge = next(
			(e for e in g["edges"] if e["source"] == uid and e["target"] == pid and e["kind"] == "authored"),
			None,
		)
		self.assertIsNotNone(edge)
		self.assertEqual(edge["weight"], 2)

	def test_member_of_resolves_held_roles(self):
		# A role page makes the role a node; an author who holds that role gets
		# a member-of edge. Administrator holds System Manager.
		self._page(f"{_PREFIX}-role2", "Role Page", scope="Role", target_role=_ROLE)
		self._page(
			f"{_PREFIX}-auth2", "Authored", sources=[{"date": "2026-07-01", "kind": "tool", "user": _USER}]
		)
		g = self._graph()
		self.assertIn(
			{"source": f"user:{_USER}", "target": f"role:{_ROLE}", "kind": "member-of"},
			g["edges"],
		)

	def test_counts_and_org_node_present(self):
		self._page(f"{_PREFIX}-c1", "A")
		self._page(f"{_PREFIX}-c2", "B", sources=[{"date": "2026-07-01", "kind": "tool", "user": _USER}])
		g = self._graph()
		self.assertIsNotNone(self._node(g, "org"))
		self.assertGreaterEqual(g["counts"]["pages"], 2)
		self.assertGreaterEqual(g["counts"]["authors"], 1)

	def test_wikilink_edges_between_pages(self):
		# One page links to another via [[slug]]; a dangling link is dropped.
		a = self._page(f"{_PREFIX}-linka", "A")
		b = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": f"{_PREFIX}-linkb",
				"title": "B",
				"page_type": "Org",
				"scope": "Org",
				"status": "Active",
				"body_md": f"see [[{a.name}]] and [[{_PREFIX}-nope]] (dangling)",
			}
		).insert(ignore_permissions=True)
		g = self._graph()
		self.assertIn(
			{"source": f"page:{b.name}", "target": f"page:{a.name}", "kind": "links-to"},
			g["edges"],
		)
		# dangling target (no such page) is not emitted.
		self.assertFalse(
			any(e["kind"] == "links-to" and e["target"] == f"page:{_PREFIX}-nope" for e in g["edges"])
		)
		# body_md still never leaks onto a node.
		self.assertFalse(any("body_md" in n for n in g["nodes"]))
		self.assertGreaterEqual(g["counts"]["links"], 1)

	def test_manual_links_union_and_dedup(self):
		a = self._page(f"{_PREFIX}-mla", "A", body_md="")
		# manual link with NO body link → still an edge (out-of-body, durable R1)
		b = self._page(f"{_PREFIX}-mlb", "B", body_md="no links", manual_links=[a.name])
		# body link + manual link to the same target → one edge (deduped)
		c = self._page(
			f"{_PREFIX}-mlc", "C", body_md=f"see [[{a.name}]]", manual_links=[a.name, "nope-missing-slug"]
		)
		g = self._graph()
		self.assertIn(
			{"source": f"page:{b.name}", "target": f"page:{a.name}", "kind": "links-to"},
			g["edges"],
		)
		c_to_a = [
			e
			for e in g["edges"]
			if e["kind"] == "links-to" and e["source"] == f"page:{c.name}" and e["target"] == f"page:{a.name}"
		]
		self.assertEqual(len(c_to_a), 1)  # body ∪ manual deduped
		# dangling manual link (no such page) dropped
		self.assertFalse(any(e.get("target") == "page:nope-missing-slug" for e in g["edges"]))

	def test_get_wiki_graph_scoped_with_content(self):
		from jarvis.chat import wiki as wiki_mod

		a = self._page(f"{_PREFIX}-gwa", "Alpha", body_md="", summary="alpha summary")
		b = self._page(f"{_PREFIX}-gwb", "Beta", body_md="", manual_links=[a.name])
		g = wiki_mod.get_wiki_graph()
		an = self._node(g, f"page:{a.name}")
		self.assertIsNotNone(an)
		self.assertEqual(an.get("summary"), "alpha summary")  # include_content
		self.assertNotIn("body_md", an)  # content is summary+title only, never body
		self.assertIn(
			{"source": f"page:{b.name}", "target": f"page:{a.name}", "kind": "links-to"},
			g["edges"],
		)

	def test_get_wiki_graph_history_non_sm_blocked(self):
		"""R3: org-wide aggregates are SM-only, unlike get_wiki_graph."""
		with patch("frappe.get_roles", return_value=["Blogger"]):
			self.assertEqual(wiki_mod.get_wiki_graph_history(), [])

	def test_get_wiki_graph_history_sm_allowed(self):
		with patch("frappe.get_roles", return_value=["System Manager"]):
			self.assertIsInstance(wiki_mod.get_wiki_graph_history(), list)

	# --- add_wiki_link (R1/R2/R3) ---
	def test_add_link_out_of_body_and_edge(self):
		a = self._page(f"{_PREFIX}-ala", "A", body_md="")
		p = self._page(f"{_PREFIX}-alp", "P", body_md="original body")
		res = wiki_mod.add_wiki_link(p.name, a.name)
		self.assertTrue(res["ok"])
		# body_md untouched (R1 — out of body)
		self.assertEqual(frappe.db.get_value(WIKI, p.name, "body_md"), "original body")
		self.assertIn(a.name, wiki_mod._parse_manual_links(frappe.db.get_value(WIKI, p.name, "manual_links")))
		g = self._graph()
		self.assertIn(
			{"source": f"page:{p.name}", "target": f"page:{a.name}", "kind": "links-to"}, g["edges"]
		)

	def test_add_link_idempotent_and_exact(self):
		a = self._page(f"{_PREFIX}-foo", "Foo", body_md="")
		self._page(f"{_PREFIX}-foobar", "Foobar", body_md="")
		p = self._page(f"{_PREFIX}-idp", "P", body_md="")
		wiki_mod.add_wiki_link(p.name, a.name)
		res2 = wiki_mod.add_wiki_link(p.name, a.name)
		self.assertTrue(res2.get("already"))
		links = wiki_mod._parse_manual_links(frappe.db.get_value(WIKI, p.name, "manual_links"))
		self.assertEqual(links.count(a.name), 1)  # no duplicate
		# exact-slug membership: linking to -foobar never implies -foo
		self.assertNotIn(f"{_PREFIX}-foobar", links)

	def test_add_link_self_rejected(self):
		p = self._page(f"{_PREFIX}-self", "P", body_md="")
		with self.assertRaises(frappe.ValidationError):
			wiki_mod.add_wiki_link(p.name, p.name)

	def test_add_link_source_not_editable_blocked(self):
		a = self._page(f"{_PREFIX}-nea", "A", body_md="")
		p = self._page(f"{_PREFIX}-nep", "P", body_md="")
		with patch("jarvis.chat.wiki_permissions.can_edit_page", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				wiki_mod.add_wiki_link(p.name, a.name)

	def test_add_link_target_not_readable_blocked(self):
		a = self._page(f"{_PREFIX}-nra", "A", body_md="")
		p = self._page(f"{_PREFIX}-nrp", "P", body_md="")
		# target invisible → reads as not-found (doesn't disclose existence, R3)
		with patch("jarvis.chat.wiki_permissions.can_read_page", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				wiki_mod.add_wiki_link(p.name, a.name)

	def test_add_link_durable_across_reingest(self):
		a = self._page(f"{_PREFIX}-dura", "A", body_md="")
		p = self._page(f"{_PREFIX}-durp", "P", body_md="original")
		wiki_mod.add_wiki_link(p.name, a.name)
		# simulate LLM re-ingest that full-replaces body_md (no [[a]] in it)
		wiki_mod.apply_extracted_page_updates(
			[{"slug": p.name, "body_md": "re-ingested body, no links at all"}],
			"voice",
			_USER,
		)
		links = wiki_mod._parse_manual_links(frappe.db.get_value(WIKI, p.name, "manual_links"))
		self.assertIn(a.name, links)  # survived the body overwrite (R1)
		g = self._graph()
		self.assertIn(
			{"source": f"page:{p.name}", "target": f"page:{a.name}", "kind": "links-to"}, g["edges"]
		)

	def test_add_link_bumps_modified_defeats_stale_save(self):
		"""R1 (finding 3): the manual_links write bumps `modified`, so a
		concurrent full-doc save loaded BEFORE our link add (e.g. LLM
		re-ingest's frappe.get_doc -> doc.save()) raises TimestampMismatch
		instead of silently clobbering the just-added link."""
		p = self._page(f"{_PREFIX}-tsp", "P", body_md="pre-link body")
		a = self._page(f"{_PREFIX}-tsa", "A", body_md="")
		stale = frappe.get_doc(WIKI, p.name)  # loaded BEFORE the link add
		wiki_mod.add_wiki_link(p.name, a.name)
		stale.body_md = "full re-ingested body, no [[links]] at all"
		with self.assertRaises(frappe.TimestampMismatchError):
			stale.save(ignore_permissions=True)
		# the stale save never landed — the link survived.
		links = wiki_mod._parse_manual_links(frappe.db.get_value(WIKI, p.name, "manual_links"))
		self.assertIn(a.name, links)

	def test_add_link_uses_locking_read(self):
		"""for_update=True on the manual_links read is what makes the write
		race-free (R2) — assert the mechanism directly."""
		p = self._page(f"{_PREFIX}-lockp", "P", body_md="")
		a = self._page(f"{_PREFIX}-locka", "A", body_md="")
		orig = frappe.db.get_value
		seen = {}

		def spy(dt, name=None, field=None, *args, **kwargs):
			if dt == WIKI and field == "manual_links":
				seen["for_update"] = kwargs.get("for_update")
			return orig(dt, name, field, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			wiki_mod.add_wiki_link(p.name, a.name)
		self.assertTrue(seen.get("for_update"))

	def test_add_link_concurrency_no_lost_update(self):
		"""Real second DB connection reproduces the R2 bug (finding 4): under
		REPEATABLE READ a plain read stays pinned to the snapshot taken before a
		concurrent writer's commit, so a naive retry loop would see this
		transaction's pre-concurrent NULL forever. add_wiki_link's locking
		(for_update) read instead returns the latest committed row, so the
		concurrent add is merged, not lost. Uses this compat FrappeTestCase's
		primary_connection/secondary_connection helpers (genuinely separate
		MySQL connections, same technique frappe core uses for lock tests) —
		every primary-side statement is explicitly wrapped in
		``primary_connection()`` (the helper leaves ``frappe.db`` pointed at the
		secondary connection after its first use otherwise). Requires committing
		our fixtures so the second connection can see them; cleaned up (deleted +
		committed) in `finally` so nothing leaks."""
		p = self._page(f"{_PREFIX}-cp", "P", body_md="")
		t1 = self._page(f"{_PREFIX}-ct1", "T1", body_md="")
		t2 = self._page(f"{_PREFIX}-ct2", "T2", body_md="")
		frappe.db.commit()
		try:
			with self.primary_connection():
				# Pin our (primary) transaction's REPEATABLE READ snapshot now,
				# via a plain read, before the concurrent write below.
				self.assertIsNone(frappe.db.get_value(WIKI, p.name, "manual_links"))

			# Genuinely concurrent writer: separate connection adds t2, commits.
			with self.secondary_connection():
				frappe.db.set_value(WIKI, p.name, "manual_links", json.dumps([t2.name]))
				frappe.db.commit()

			with self.primary_connection():
				# Proof the snapshot is really stale: a plain read on our
				# transaction still doesn't see the concurrent commit.
				self.assertIsNone(frappe.db.get_value(WIKI, p.name, "manual_links"))

				# add_wiki_link's locking read must see the latest committed
				# value and merge t1 into it instead of clobbering t2.
				wiki_mod.add_wiki_link(p.name, t1.name)
				links = wiki_mod._parse_manual_links(frappe.db.get_value(WIKI, p.name, "manual_links"))
				self.assertIn(t1.name, links)  # our add
				self.assertIn(t2.name, links)  # the concurrent add — NOT lost (R2)
		finally:
			with self.primary_connection():
				_delete_pages()
				frappe.db.commit()

	def test_archived_pages_excluded(self):
		doc = self._page(f"{_PREFIX}-arch", "Archived")
		frappe.db.set_value(WIKI, doc.name, "status", "Archived", update_modified=False)
		g = self._graph()
		self.assertIsNone(self._node(g, f"page:{doc.name}"))


class TestCuratedLinksReachReaders(WikiGraphTestCase):
	"""#494: ``add_wiki_link`` stores curated links in ``manual_links``, never in
	``body_md``, and for a long time only ``wiki_graph`` read that field. The
	agent's only two channels into the wiki are ``jarvis__read_wiki`` and the
	container mirror, so a curated relation reached it through neither.

	The mirror and orphan halves live beside their own modules
	(``test_wiki_mirror`` / ``test_wiki_lint``); this covers the reader tool,
	next to the writer it pairs with."""

	def test_read_wiki_surfaces_curated_links_on_both_paths(self):
		target = self._page(f"{_PREFIX}-crl-target", "Curated Target")
		source = self._page(
			f"{_PREFIX}-crl-source",
			"Curated Source",
			body_md="Nothing in this body links anywhere.",
			manual_links=[target.name],
		)

		page = read_wiki(slug=source.name)
		self.assertEqual(page["manual_links"], [target.name])

		rows = read_wiki(query=f"{_PREFIX}-crl-source", limit=10)
		row = next(r for r in rows if r["slug"] == source.name)
		self.assertEqual(row["manual_links"], [target.name])

	def test_read_wiki_drops_targets_the_caller_cannot_read(self):
		"""Curated links are bare slugs written once and never re-checked, so
		echoing them raw would disclose a page whose scope narrowed since the
		write, and would hand the agent links it cannot follow."""
		archived = self._page(f"{_PREFIX}-crl-gone", "Curated Archived")
		frappe.db.set_value(WIKI, archived.name, "status", "Archived", update_modified=False)
		source = self._page(
			f"{_PREFIX}-crl-src2",
			"Curated Source 2",
			manual_links=[archived.name, f"{_PREFIX}-crl-never-existed"],
		)

		page = read_wiki(slug=source.name)
		self.assertEqual(page["manual_links"], [])

	def test_read_wiki_reports_no_curated_links_when_there_are_none(self):
		source = self._page(f"{_PREFIX}-crl-plain", "Curated Plain")
		self.assertEqual(read_wiki(slug=source.name)["manual_links"], [])


class TestWikiGraphSync(FrappeTestCase):
	def test_push_unreachable_reports_not_ok(self):
		with (
			patch("jarvis.admin_client.push_wiki_graph", return_value=None),
		):
			out = wiki_graph.sync()
		self.assertFalse(out["ok"])

	def test_push_ok_returns_counts(self):
		with (
			patch("jarvis.admin_client.push_wiki_graph", return_value={"ok": True}),
		):
			out = wiki_graph.sync()
		self.assertTrue(out["ok"])
		self.assertIn("pages", out)
