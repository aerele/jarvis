from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_creation_context import get_creation_context


def _todo(description, **kw):
	return frappe.get_doc(
		{
			"doctype": "ToDo",
			"description": description,
			**kw,
		}
	).insert(ignore_permissions=True)


class TestGetCreationContext(FrappeTestCase):
	"""ToDo is a core doctype (no ERPNext needed) with Select classifier fields
	(priority/status), so it exercises the field map, context matching, facets,
	and fallbacks without heavy fixtures."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for i in range(3):
			_todo(f"jarvis-ctx high {i}", priority="High", status="Open")
		for i in range(2):
			_todo(f"jarvis-ctx low {i}", priority="Low", status="Open")

	def test_returns_expected_shape(self):
		ctx = get_creation_context("ToDo")
		for key in ("doctype", "fields", "mandatory", "similar", "example", "match_note", "facets", "note"):
			self.assertIn(key, ctx)
		self.assertEqual(ctx["doctype"], "ToDo")
		self.assertTrue(ctx["fields"])
		for f in ctx["fields"]:
			self.assertLessEqual(
				{"fieldname", "fieldtype", "mandatory", "auto", "readonly"},
				set(f.keys()),
			)

	def test_mandatory_matches_meta(self):
		ctx = get_creation_context("ToDo")
		expected = {df.fieldname for df in frappe.get_meta("ToDo").fields if df.reqd}
		self.assertEqual(set(ctx["mandatory"]), expected)
		# every listed mandatory field is flagged mandatory in the field map
		flagged = {f["fieldname"] for f in ctx["fields"] if f["mandatory"]}
		self.assertEqual(flagged, set(ctx["mandatory"]))

	def test_context_match_on_select(self):
		ctx = get_creation_context("ToDo", {"priority": "High"}, limit=10)
		self.assertTrue(ctx["similar"])
		self.assertTrue(all(r.get("priority") == "High" for r in ctx["similar"]))
		self.assertIn("priority=High", ctx["match_note"])
		self.assertFalse(ctx["facets"])  # facets surface only when no hint resolved

	def test_recent_fallback_when_no_match(self):
		ctx = get_creation_context("ToDo", {"priority": "__no_such_value__"})
		self.assertEqual(ctx["match_note"], "recent - no context matched")
		self.assertTrue(ctx["similar"])  # ToDos exist from setUpClass

	def test_facets_when_no_context(self):
		ctx = get_creation_context("ToDo")
		self.assertTrue(ctx["facets"])  # priority/status are Select classifiers
		joined = {v for vals in ctx["facets"].values() for v in vals}
		self.assertIn("High", joined)

	def test_example_is_full_doc(self):
		ctx = get_creation_context("ToDo", {"priority": "High"})
		self.assertIsNotNone(ctx["example"])
		self.assertIsInstance(ctx["example"], dict)
		# no_default_fields strips owner/creation/name/doctype (lean payload);
		# real data fields stay.
		self.assertEqual(ctx["example"].get("priority"), "High")
		self.assertIn("description", ctx["example"])

	def test_partial_relaxation_keeps_matching_hint(self):
		"""Two hints whose combination matches nothing: the weakest is dropped
		and the note is flagged (relaxed)."""
		ctx = get_creation_context("ToDo", {"priority": "High", "status": "__no_such__"})
		self.assertIn("(relaxed)", ctx["match_note"])
		self.assertIn("priority=High", ctx["match_note"])
		self.assertTrue(all(r.get("priority") == "High" for r in ctx["similar"]))

	def test_json_string_context_is_parsed(self):
		ctx = get_creation_context("ToDo", '{"priority": "High"}')
		self.assertIn("priority=High", ctx["match_note"])

	def test_non_json_string_context_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			get_creation_context("ToDo", "Furniture")

	def test_single_doctype_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			get_creation_context("System Settings")

	def test_istable_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			get_creation_context("Contact Email")

	def test_unknown_doctype_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			get_creation_context("No Such Doctype 123")

	def test_loose_hint_resolves_to_link_field(self):
		"""A context key that isn't a fieldname resolves to the Link field whose
		target holds the value (generic party discovery). Tested on the helper
		so it's independent of which of ToDo's two User-links wins."""
		from jarvis.tools.get_creation_context import _resolve_link_field

		meta = frappe.get_meta("ToDo")
		resolved = _resolve_link_field(meta, "Administrator")  # a User
		self.assertIsNotNone(resolved)
		df = {d.fieldname: d for d in meta.fields}[resolved]
		self.assertEqual(df.fieldtype, "Link")
		self.assertEqual(df.options, "User")

	def test_permission_denied_without_create(self):
		user_email = "ctxless@example.com"
		if not frappe.db.exists("User", user_email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Ctxless",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				get_creation_context("User")
		finally:
			frappe.set_user("Administrator")


class TestCreationContextAutoAndDefaults(FrappeTestCase):
	"""`auto` must mean "Frappe COMPUTES this" (fetch_from), not "Frappe has a
	guess for it" (default).

	Conflating them made every defaulted field invisible: the agent was told to
	skip `auto` fields, so three ToDos were created due today and the human was
	never shown the date. A default is a decision - surface it and let the model
	decide.
	"""

	def _field(self, ctx, fieldname):
		for f in ctx["fields"]:
			if f["fieldname"] == fieldname:
				return f
		self.fail(f"{fieldname} is missing from the field map")

	def test_fetch_from_field_is_auto_and_carries_no_default(self):
		"""ToDo.assigned_by_full_name is `fetch_from: assigned_by.full_name` -
		Frappe computes it, so skipping it is genuinely safe."""
		f = self._field(get_creation_context("ToDo"), "assigned_by_full_name")
		self.assertTrue(f["auto"])
		self.assertNotIn("default", f)

	def test_defaulted_field_is_not_auto_and_surfaces_its_default(self):
		"""ToDo.date defaults to Today. Frappe GUESSES it, so the model must see
		both that it is not auto and what the guess is."""
		f = self._field(get_creation_context("ToDo"), "date")
		self.assertFalse(f["auto"])
		self.assertEqual(f["default"], "Today")

	def test_reqd_field_is_still_mandatory(self):
		"""`mandatory` is computed from df.reqd independently of `auto`, so
		splitting `auto` cannot move it."""
		f = self._field(get_creation_context("ToDo"), "description")
		self.assertTrue(f["mandatory"])

	def test_reqd_with_default_is_mandatory_and_not_auto(self):
		"""Sales Order.transaction_date is reqd AND defaults to Today - the
		double-bind the note's ordering exists to resolve."""
		f = self._field(get_creation_context("Sales Order"), "transaction_date")
		self.assertTrue(f["mandatory"])
		self.assertFalse(f["auto"])
		self.assertEqual(f["default"], "Today")

	def test_note_carries_the_default_and_symbolic_default_clauses(self):
		note = get_creation_context("ToDo")["note"]
		# a default is decided, not skipped
		self.assertIn("decide it yourself", note)
		# `Today` / `:Company` are tokens Frappe resolves, never values to copy
		self.assertIn("never copy the token as a value", note)
		# the mandatory command is scoped to what the default rule left over,
		# so a reqd+default field is not commanded and permitted at once
		self.assertIn("remaining", note)


class TestCreationContextMappedSource(FrappeTestCase):
	"""A derived record (invoice from an order) gets the ERP's own mapper output
	instead of lookalikes to infer from.

	The agent ALREADY hands us the source - the production transcript shows
	`{"customer": ..., "sales_order": "SAL-ORD-2026-00002", ...}` - it just was
	not being read as one. Uses Note (clean Title Case, so the context key
	"note" resolves) with a patched mapper: a real submitted Sales Order needs a
	full ERPNext company this site does not have.
	"""

	def setUp(self):
		self.src = frappe.get_doc(
			{
				"doctype": "Note",
				"title": f"ctx-src-{frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Note", {"name": self.src.name})
		frappe.db.commit()

	def _ctx(self, mapped, note=None):
		return patch("jarvis.tools._source_mapper.resolve_mapper", return_value="fake.method"), patch(
			"jarvis.tools._source_mapper.mapped_values", return_value=(mapped, note)
		)

	def test_mapped_source_returns_mapper_output_and_omits_similar(self):
		"""Authoritative beats similar: a mapped doc AND five lookalikes in one
		payload would be the worst of both - cost without benefit."""
		p1, p2 = self._ctx({"description": "from the mapper", "items": [{"x": 1}]})
		with p1, p2:
			out = get_creation_context("ToDo", {"note": self.src.name})
		self.assertEqual(out["mapped"]["description"], "from the mapper")
		self.assertEqual(out["mapped_from"]["source_doctype"], "Note")
		self.assertEqual(out["mapped_from"]["source_name"], self.src.name)
		self.assertEqual(out["similar"], [])
		self.assertIsNone(out["example"])
		self.assertIn("authoritative", out["match_note"])
		# The instruction must flip: copy these values, don't re-derive them.
		self.assertIn("mapper", out["note"])

	def test_mapper_refusal_is_surfaced_and_similar_still_returned(self):
		""" "Sales Order is not submitted" says exactly what to fix - but the
		agent must still get its normal context so the turn can continue."""
		p1, p2 = self._ctx(None, note="could not map: Sales Order is not submitted")
		with p1, p2:
			out = get_creation_context("ToDo", {"note": self.src.name})
		self.assertNotIn("mapped", out)
		self.assertIn("not submitted", out["mapped_note"])

	def test_no_mapper_leaves_the_normal_path_untouched(self):
		with patch("jarvis.tools._source_mapper.resolve_mapper", return_value=None):
			out = get_creation_context("ToDo", {"note": self.src.name})
		self.assertNotIn("mapped", out)
		self.assertNotIn("mapped_note", out)
		self.assertIn("similar", out)

	def test_context_key_that_is_not_a_doctype_is_ignored(self):
		out = get_creation_context("ToDo", {"priority": "High"})
		self.assertNotIn("mapped", out)

	def test_nonexistent_source_doc_is_ignored(self):
		with (
			patch("jarvis.tools._source_mapper.resolve_mapper", return_value="fake.method"),
			patch("jarvis.tools._source_mapper.mapped_values") as mv,
		):
			out = get_creation_context("ToDo", {"note": "no-such-note-xyz"})
		self.assertNotIn("mapped", out)
		mv.assert_not_called()  # never run a mapper for a doc that isn't there
