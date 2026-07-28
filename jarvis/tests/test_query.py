"""Tests for jarvis.tools.query — the qb-based, permission-aware
replacement for run_query.

Test surface:

- Spec validation: required fields, alias collisions, unknown operators
- qb translation: spec → SQL substring assertions (one per construct)
- DocType-level read permission per referenced doctype
- Engine.get_permission_conditions woven into the WHERE
- DocType allowlist gate (shared Jarvis Settings field)
- Row guard + confirm_large escape hatch
- Happy path against the always-populated ``tabDocType`` table
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
	ResultTooLargeError,
)
from jarvis.tools.query import ROW_GUARD, query


class TestQuerySpecValidation(FrappeTestCase):
	"""Top-of-pipe shape checks. Fail before any DB call."""

	def test_rejects_non_dict_spec(self):
		with self.assertRaises(InvalidArgumentError):
			query("not a dict")
		with self.assertRaises(InvalidArgumentError):
			query(None)

	def test_rejects_missing_from(self):
		with self.assertRaises(InvalidArgumentError):
			query({"select": ["name"]})

	def test_rejects_non_string_from(self):
		with self.assertRaises(InvalidArgumentError):
			query({"from": 123})

	def test_rejects_non_list_joins(self):
		with self.assertRaises(InvalidArgumentError):
			query({"from": "DocType", "joins": "not a list"})

	def test_rejects_join_missing_required_fields(self):
		for missing in ("doctype", "alias", "on"):
			j = {"doctype": "User", "alias": "u", "on": {"u.name": "dt.name"}}
			del j[missing]
			with self.assertRaises(InvalidArgumentError, msg=missing):
				query({"from": "DocType", "alias": "dt", "joins": [j]})

	def test_rejects_alias_collision(self):
		"""Two tables with the same alias would make field references
		ambiguous; the validator must catch it."""
		with self.assertRaises(InvalidArgumentError) as cm:
			query(
				{
					"from": "DocType",
					"alias": "x",
					"joins": [{"doctype": "User", "alias": "x", "on": {"x.name": "x.name"}}],
				}
			)
		self.assertIn("collides", str(cm.exception))

	def test_rejects_invalid_join_type(self):
		with self.assertRaises(InvalidArgumentError):
			query(
				{
					"from": "DocType",
					"joins": [
						{
							"doctype": "User",
							"alias": "u",
							"type": "cross",  # not supported
							"on": {"u.name": "DocType.name"},
						}
					],
				}
			)

	def test_rejects_unknown_operator(self):
		with self.assertRaises(InvalidArgumentError) as cm:
			query(
				{
					"from": "DocType",
					"where": [{"field": "name", "op": "REGEX", "value": ".*"}],
				}
			)
		self.assertIn("not allowed", str(cm.exception))

	def test_rejects_invalid_limit(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "limit": 0})
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "limit": 10_000})


class TestQueryPermissions(FrappeTestCase):
	"""DocType-level read permission fires per referenced DocType
	BEFORE the query is built/executed - same gate run_query has."""

	def test_rejects_when_user_lacks_doctype_perm(self):
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				query({"from": "Sales Invoice"})

	def test_checks_perm_on_every_referenced_doctype(self):
		"""A join across two DocTypes must check both. We let the real
		qb path run and patch only the perm check + the actual SQL
		execution - mocking the qb chain proved fragile."""
		checked = []

		def fake_has_perm(doctype, ptype="read", **_):
			checked.append(doctype)
			return True

		# Warm the meta cache before patching the query Engine: _validate_doctype
		# calls frappe.get_meta, which would otherwise load these DocTypes via the
		# patched Engine (returning MagicMocks) and recurse. Deterministic
		# regardless of whether a prior test left these metas cached.
		frappe.get_meta("Sales Invoice")
		frappe.get_meta("Sales Invoice Item")
		with (
			patch("frappe.has_permission", side_effect=fake_has_perm),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			query(
				{
					"from": "Sales Invoice",
					"alias": "si",
					"joins": [
						{
							"type": "left",
							"doctype": "Sales Invoice Item",
							"alias": "sii",
							"on": {"sii.parent": "si.name"},
						}
					],
					"select": ["si.name"],
				}
			)

		self.assertIn("Sales Invoice", checked)
		self.assertIn("Sales Invoice Item", checked)


class TestQueryDocTypeAllowlist(FrappeTestCase):
	"""Same per-site allowlist field that gates run_query also gates
	query. One operator knob controls both tools."""

	def setUp(self):
		# Reset allowlist before each test.
		settings = frappe.get_single("Jarvis Settings")
		settings.run_query_doctype_allowlist = ""
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings")

	def tearDown(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.run_query_doctype_allowlist = ""
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings")

	def test_doctype_off_allowlist_is_rejected(self):
		settings = frappe.get_single("Jarvis Settings")
		settings.run_query_doctype_allowlist = "Customer\nSales Invoice"
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings")

		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(PermissionDeniedError) as cm:
				query({"from": "DocType"})
			self.assertIn("allowlist", str(cm.exception))

	def test_empty_allowlist_imposes_no_extra_restriction(self):
		"""Default state - allowlist is empty/unset, no extra gate."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query({"from": "DocType", "limit": 1})
		self.assertIn("rows", result)


class TestQueryQbTranslation(FrappeTestCase):
	"""Spec → SQL substring assertions. We don't execute - we patch the
	qb run/get_sql calls and check the SQL string produced by qb."""

	def _build_sql(self, spec: dict) -> str:
		"""Helper: build the query through the full pipeline, capture
		the qb expression's resolved SQL via .get_sql()."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			# We let the real qb run; patch only the actual SQL exec.
			with patch("pypika.queries.QueryBuilder.run", return_value=[]) as _:
				result = query(spec)
		return result["sql"]

	def test_basic_select_from(self):
		sql = self._build_sql({"from": "DocType", "select": ["name"]})
		# qb backticks the table name in MariaDB / quotes it in Postgres.
		self.assertIn("DocType", sql)
		self.assertIn("name", sql.lower())

	def test_select_aggregate(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [{"agg": "count", "field": "*", "as": "n"}],
			}
		)
		self.assertIn("COUNT", sql.upper())
		self.assertIn("n", sql)  # the alias appears

	def test_where_equality(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [{"field": "dt.issingle", "op": "=", "value": 0}],
			}
		)
		self.assertIn("issingle", sql)

	def test_where_in(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [{"field": "dt.module", "op": "in", "value": ["Core", "Custom"]}],
			}
		)
		self.assertIn("IN", sql.upper())

	def test_order_by(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"order_by": [{"field": "dt.name", "dir": "desc"}],
			}
		)
		self.assertIn("ORDER BY", sql.upper())
		self.assertIn("DESC", sql.upper())

	def test_group_by(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.module", {"agg": "count", "field": "*", "as": "n"}],
				"group_by": ["dt.module"],
			}
		)
		self.assertIn("GROUP BY", sql.upper())

	def test_limit(self):
		sql = self._build_sql({"from": "DocType", "limit": 42})
		self.assertIn("LIMIT 42", sql.upper())

	def test_in_with_list_value_required(self):
		"""'in' operator demands a list, not a scalar."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"where": [{"field": "module", "op": "in", "value": "Core"}],  # should be list
					}
				)

	def test_between_demands_two_element_list(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"where": [{"field": "name", "op": "between", "value": ["a"]}],  # one element
					}
				)


class TestQueryPermissionConditionsWoven(FrappeTestCase):
	"""The critical test: Engine.get_permission_conditions returns
	a Criterion; query() must AND it into the WHERE."""

	def test_permission_condition_appears_in_sql(self):
		"""Patch Engine to return a recognisable criterion; assert the
		predicate appears in the resolved SQL."""
		from pypika import Field

		# A literal-equality criterion: ``modified = 'sentinel-marker'``.
		# Easy to spot in the produced SQL.
		sentinel = Field("modified") == "sentinel-marker"

		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = sentinel
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query({"from": "DocType", "select": ["name"]})

		self.assertIn("sentinel-marker", result["sql"])

	def test_none_from_engine_is_skipped(self):
		"""When the user has no restrictions, Engine returns None - we
		must not append anything to the WHERE."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query({"from": "DocType", "select": ["name"]})

		# No WHERE clause should appear when nothing constrains the query.
		# (qb only renders WHERE when at least one where() was called.)
		self.assertNotIn("WHERE", result["sql"].upper())


class TestQueryRowGuard(FrappeTestCase):
	"""Row guard fires when result exceeds ``ROW_GUARD`` and the
	caller didn't opt in via ``confirm_large``."""

	def test_guard_fires_above_threshold(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			# tabDocType has hundreds of rows on every Frappe site - a
			# reliable fixture for the row-guard test.
			with self.assertRaises(ResultTooLargeError) as cm:
				query({"from": "DocType", "limit": ROW_GUARD + 50})
			self.assertEqual(cm.exception.tool, "query")
			self.assertGreater(cm.exception.row_count, ROW_GUARD)

	def test_guard_silent_under_threshold(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query({"from": "DocType", "limit": ROW_GUARD - 50})
		self.assertLessEqual(len(result["rows"]), ROW_GUARD - 50)

	def test_guard_opt_in_bypasses(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{"from": "DocType", "limit": ROW_GUARD + 50},
				confirm_large=True,
			)
		self.assertGreater(len(result["rows"]), ROW_GUARD)


class TestQueryHappyPath(FrappeTestCase):
	"""End-to-end against the real DB. Uses ``tabDocType`` which
	always exists and which the test fixture user has read on
	(System Manager)."""

	def test_returns_rows_and_resolved_sql(self):
		result = query(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [{"field": "dt.issingle", "op": "=", "value": 1}],
				"order_by": [{"field": "dt.name", "dir": "asc"}],
				"limit": 5,
			}
		)
		self.assertIn("sql", result)
		self.assertIn("rows", result)
		self.assertGreater(len(result["rows"]), 0)
		self.assertIn("name", result["rows"][0])

	def test_aggregate_with_group_by(self):
		"""Real aggregate query: count DocTypes per module."""
		result = query(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.module", {"agg": "count", "field": "*", "as": "n"}],
				"group_by": ["dt.module"],
				"order_by": [{"field": "n", "dir": "desc"}],
				"limit": 5,
			}
		)
		self.assertGreater(len(result["rows"]), 0)
		self.assertIn("module", result["rows"][0])
		self.assertIn("n", result["rows"][0])
		self.assertGreater(result["rows"][0]["n"], 0)


class TestQueryV02Additions(FrappeTestCase):
	"""v0.2 additions: DISTINCT, OFFSET, COUNT(DISTINCT), EXISTS / NOT EXISTS.

	Uses the same _build_sql helper pattern as TestQueryQbTranslation
	to assert on resolved SQL substrings rather than execution.
	"""

	def _build_sql(self, spec: dict) -> str:
		"""Build through the full pipeline, capture the resolved SQL."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	# ---- DISTINCT --------------------------------------------------

	def test_distinct_emits_distinct_in_sql(self):
		"""distinct=True at the spec root produces SELECT DISTINCT."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.module"],
				"distinct": True,
			}
		)
		self.assertIn("DISTINCT", sql.upper())

	def test_distinct_false_no_distinct(self):
		"""distinct=False (or omitted) does NOT add DISTINCT."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.module"],
				"distinct": False,
			}
		)
		self.assertNotIn("DISTINCT", sql.upper())

	def test_distinct_rejects_non_bool(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "distinct": "yes"})

	# ---- OFFSET ----------------------------------------------------

	def test_offset_emits_offset_in_sql(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"select": ["name"],
				"limit": 10,
				"offset": 20,
			}
		)
		self.assertIn("OFFSET 20", sql.upper())

	def test_offset_zero_does_not_emit_offset(self):
		"""offset=0 is a no-op; pypika shouldn't emit OFFSET 0 either."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"select": ["name"],
				"limit": 10,
				"offset": 0,
			}
		)
		self.assertNotIn("OFFSET", sql.upper())

	def test_offset_rejects_negative(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "offset": -1})

	def test_offset_rejects_above_ceiling(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "offset": 200_000})

	def test_offset_rejects_non_int(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "offset": "ten"})

	# ---- COUNT(DISTINCT) -------------------------------------------

	def test_count_distinct_emits_count_distinct(self):
		"""distinct=True on a count aggregate becomes COUNT(DISTINCT field)."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"agg": "count",
						"field": "dt.module",
						"distinct": True,
						"as": "n",
					}
				],
			}
		)
		# Substring matches accommodate dialect spacing variation
		# (some dialects emit COUNT(DISTINCT x), others COUNT( DISTINCT x )).
		upper = sql.upper().replace(" ", "")
		self.assertIn("COUNT(DISTINCT", upper)

	def test_sum_distinct_emits_sum_distinct(self):
		"""DISTINCT modifier applies to all aggregates uniformly. Use
		tabDocType.idx (a numeric field guaranteed present)."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"agg": "sum",
						"field": "dt.idx",
						"distinct": True,
						"as": "s",
					}
				],
			}
		)
		upper = sql.upper().replace(" ", "")
		self.assertIn("SUM(DISTINCT", upper)

	# ---- EXISTS / NOT EXISTS ---------------------------------------

	def test_exists_subquery_emits_exists(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [
					{
						"op": "exists",
						"value": {
							"from": "DocField",
							"alias": "df",
							"where": [
								{
									"field": "df.parent",
									"op": "=",
									"value": {"$field": "dt.name"},
								}
							],
						},
					}
				],
			}
		)
		self.assertIn("EXISTS", sql.upper())

	def test_not_exists_subquery_emits_not_exists(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [
					{
						"op": "not exists",
						"value": {
							"from": "DocField",
							"alias": "df",
							"where": [
								{
									"field": "df.parent",
									"op": "=",
									"value": {"$field": "dt.name"},
								}
							],
						},
					}
				],
			}
		)
		upper = sql.upper()
		self.assertIn("NOT", upper)
		self.assertIn("EXISTS", upper)

	def test_correlated_subquery_resolves_outer_field(self):
		"""The {"$field": "dt.name"} marker must resolve to the OUTER
		table's column, not a literal string."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [
					{
						"op": "exists",
						"value": {
							"from": "DocField",
							"alias": "df",
							"where": [
								{
									"field": "df.parent",
									"op": "=",
									"value": {"$field": "dt.name"},
								}
							],
						},
					}
				],
			}
		)
		# The resolved SQL should reference the outer dt.name column,
		# not the literal string 'dt.name'. We check that the literal
		# string form is NOT present (with the surrounding quotes).
		self.assertNotIn("'dt.name'", sql)

	def test_exists_subspec_rejects_select(self):
		"""SELECT in a sub-spec is rejected - subqueries auto-select 1."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"select": ["df.fieldname"],  # not allowed
								},
							}
						],
					}
				)
			self.assertIn("select", str(cm.exception).lower())

	def test_exists_subspec_rejects_order_by(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"order_by": [{"field": "df.fieldname"}],
								},
							}
						],
					}
				)

	def test_exists_subspec_rejects_limit(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"limit": 10,
								},
							}
						],
					}
				)

	def test_exists_subspec_rejects_distinct(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"distinct": True,
								},
							}
						],
					}
				)

	def test_exists_recursion_depth_capped(self):
		"""Two levels of nesting (outer + one EXISTS) is the cap;
		three levels raises."""
		# Build a depth-3 spec: outer -> EXISTS(... EXISTS(... ))
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"where": [
										{
											"op": "exists",
											"value": {
												"from": "DocPerm",
												"alias": "dp",
												"where": [
													{
														"field": "dp.parent",
														"op": "=",
														"value": {"$field": "df.parent"},
													}
												],
											},
										}
									],
								},
							}
						],
					}
				)
			self.assertIn("nesting", str(cm.exception).lower())

	def test_exists_subspec_must_be_dict(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [{"op": "exists", "value": "not-a-dict"}],
					}
				)

	def test_exists_subspec_requires_from(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {"alias": "df"},  # missing 'from'
							}
						],
					}
				)


class TestQueryV03ExistsSubquerySecurityHardening(FrappeTestCase):
	"""Verify EXISTS / NOT EXISTS sub-spec doctypes are subjected to
	the SAME three permission gates as the outer query:

	1. ``has_permission(dt, ptype="read")`` role gate
	2. Per-site DocType allowlist gate
	3. ``Engine.get_permission_conditions`` woven into the sub-query
	   WHERE (User Permissions / DocShare / if_owner / hooks)

	Pre-fix, ``_collect_doctypes`` walked only outer FROM + joins and
	``_build_exists_criterion`` weaved no permission conditions on the
	sub-query, opening a side-channel: caller could probe existence of
	rows the operator was not allowed to read."""

	def test_subspec_doctype_subjected_to_has_permission(self):
		"""Role gate denies sub-spec DocType → PermissionDeniedError."""

		# has_permission returns True for the outer doctype, False for
		# the sub-spec's doctype. The pre-fix code would let this slip
		# through because _collect_doctypes only saw the outer FROM.
		def _perm(dt, ptype="read", **_):
			if dt == "DocType":
				return True
			if dt == "DocField":
				return False
			return True

		with patch("frappe.has_permission", side_effect=_perm):
			with self.assertRaises(PermissionDeniedError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"where": [
										{
											"field": "df.parent",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
			self.assertIn("DocField", str(cm.exception))

	def test_subspec_doctype_subjected_to_allowlist(self):
		"""Per-site allowlist denies sub-spec doctype."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("jarvis.tools.query._load_doctype_allowlist", return_value={"DocType"}),
		):  # DocField NOT in allowlist
			with self.assertRaises(PermissionDeniedError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"where": [
										{
											"field": "df.parent",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
			self.assertIn("DocField", str(cm.exception))
			self.assertIn("allowlist", str(cm.exception).lower())

	def test_subspec_get_permission_conditions_woven_into_subquery(self):
		"""The Engine.get_permission_conditions output for a sub-spec
		doctype must land inside the EXISTS subquery's WHERE, not the
		outer query's WHERE. This is the User Permission enforcement
		that closes the row-level side-channel."""
		from pypika import Field

		# Distinctive sentinel literals only contributed by each
		# perm-condition path. The literal strings make it
		# unambiguous where they land in the resolved SQL.
		sub_sentinel = Field("name") == "SENTINEL_SUB"
		outer_sentinel = Field("name") == "SENTINEL_OUTER"

		def _perm_conds(dt, table):
			# ``Custom Field`` (a NON-child doctype linked to DocType via ``dt``)
			# stands in for the sub-spec doctype: F2 routes CHILD sub-spec
			# doctypes through the record-scope gate instead of
			# get_permission_conditions, so a non-child doctype is used here to
			# keep exercising the get_permission_conditions weave. The child
			# sub-spec case is covered by TestQueryPermlevelFieldACL's
			# test_scope_exists_subspec_child_scoped.
			if dt == "Custom Field":
				return sub_sentinel
			if dt == "DocType":
				return outer_sentinel
			return None

		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine_cls,
		):
			fake_engine_cls.return_value.get_permission_conditions.side_effect = _perm_conds
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "Custom Field",
									"alias": "cf",
									"where": [
										{
											"field": "cf.dt",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
		sql = result["sql"]
		# Both sentinels appear; the sub-spec one is the new
		# behavior the fix introduces.
		self.assertIn(
			"SENTINEL_SUB",
			sql,
			"sub-spec permission condition missing from resolved SQL — security weave broken",
		)
		self.assertIn("SENTINEL_OUTER", sql, "outer permission condition missing — sanity check")
		# And the sub sentinel is inside the EXISTS parenthesized
		# subquery, not at the outer query top level.
		exists_idx = sql.upper().find("EXISTS")
		sub_idx = sql.find("SENTINEL_SUB")
		self.assertGreater(sub_idx, exists_idx, "sub-spec perm condition leaked to outer WHERE")

	def test_collect_doctypes_walks_nested_exists(self):
		"""Two-level EXISTS: outermost has FROM A, level-1 sub-spec
		has FROM B, level-2 sub-spec has FROM C. All three doctypes
		must be collected for role + allowlist gates."""
		from jarvis.tools.query import _collect_doctypes

		spec = {
			"from": "DocType",
			"alias": "dt",
			"where": [
				{
					"op": "exists",
					"value": {
						"from": "DocField",
						"alias": "df",
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocPerm",
									"alias": "dp",
									"where": [
										{
											"field": "dp.parent",
											"op": "=",
											"value": {"$field": "df.parent"},
										}
									],
								},
							}
						],
					},
				}
			],
		}
		collected = _collect_doctypes(spec)
		self.assertEqual(set(collected), {"DocType", "DocField", "DocPerm"})

	def test_collect_doctypes_walks_subspec_joins(self):
		"""A sub-spec's own joins also contribute doctypes."""
		from jarvis.tools.query import _collect_doctypes

		spec = {
			"from": "DocType",
			"alias": "dt",
			"where": [
				{
					"op": "not exists",
					"value": {
						"from": "DocField",
						"alias": "df",
						"joins": [
							{
								"type": "inner",
								"doctype": "DocPerm",
								"alias": "dp",
								"on": {"dp.parent": "df.parent"},
							}
						],
						"where": [
							{
								"field": "df.parent",
								"op": "=",
								"value": {"$field": "dt.name"},
							}
						],
					},
				}
			],
		}
		collected = _collect_doctypes(spec)
		self.assertEqual(set(collected), {"DocType", "DocField", "DocPerm"})

	# ---- v0.2.2 fixes from code-review workflow ---------------------

	def test_subspec_self_join_perm_gates_every_alias(self):
		"""Sub-spec self-joining the same doctype under two aliases
		must apply ``get_permission_conditions`` to BOTH aliases, not
		just the first. The pre-fix code deduplicated by doctype and
		silently bypassed perms on the second alias - a side-channel
		identical in shape to the v0.2.1 outer/sub-spec gap."""
		from pypika import Field

		# Distinct sentinel per call so we can verify both calls
		# landed in the resolved SQL (not just the first).
		sentinels = [
			Field("name") == "SELF_JOIN_SENTINEL_FIRST",
			Field("name") == "SELF_JOIN_SENTINEL_SECOND",
		]

		def _perm_conds(dt, table):
			# Outer DocType call (perm conditions are returned None so
			# we don't conflate the outer perm-weave with the sub-spec
			# self-join one).
			if dt == "DocType":
				return None
			# Sub-spec Custom Field calls - one per alias. Return a
			# different sentinel each time so we can prove both
			# landed. (A NON-child doctype: F2 routes CHILD sub-spec
			# doctypes through the record-scope gate, so the
			# get_permission_conditions per-alias weave is exercised with
			# a non-child self-join here.)
			if dt == "Custom Field":
				return sentinels.pop(0) if sentinels else None
			return None

		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine_cls,
		):
			fake_engine_cls.return_value.get_permission_conditions.side_effect = _perm_conds
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "Custom Field",
									"alias": "cf",
									"joins": [
										{
											"type": "inner",
											"doctype": "Custom Field",
											"alias": "cf2",
											"on": {"cf2.dt": "cf.dt"},
										}
									],
									"where": [
										{
											"field": "cf.dt",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
		sql = result["sql"]
		# Both sentinels must appear in the resolved SQL.
		self.assertIn(
			"SELF_JOIN_SENTINEL_FIRST",
			sql,
			"sub-spec perm gate skipped the first alias - regression",
		)
		self.assertIn(
			"SELF_JOIN_SENTINEL_SECOND",
			sql,
			"sub-spec perm gate skipped the second alias - v0.2.1 side-channel still open",
		)
		# Both sentinels are inside the EXISTS subquery, not at the
		# outer WHERE level.
		exists_idx = sql.upper().find("EXISTS")
		first_idx = sql.find("SELF_JOIN_SENTINEL_FIRST")
		second_idx = sql.find("SELF_JOIN_SENTINEL_SECOND")
		self.assertGreater(first_idx, exists_idx)
		self.assertGreater(second_idx, exists_idx)

	def test_outer_engine_has_doctype_attribute_set(self):
		"""Frappe's ``permission_query_conditions`` hooks read
		``engine.doctype`` to format the main table name (e.g.
		``f"tab{self.doctype}"``). Without it, the first hook with a
		permission_query_conditions entry crashes with AttributeError.
		Verify the outer Engine has ``.doctype`` set to ``spec["from"]``
		before ``get_permission_conditions`` is called."""
		captured = []

		def _capture(dt, table):
			# Capture the Engine instance's doctype attribute at
			# call time.
			engine_instance = fake_engine_cls.return_value
			captured.append(
				{
					"dt": dt,
					"engine_doctype": getattr(engine_instance, "doctype", "<UNSET>"),
				}
			)
			return None

		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine_cls,
		):
			fake_engine_cls.return_value.get_permission_conditions.side_effect = _capture
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				query(
					{
						"from": "DocType",
						"select": ["name"],
					}
				)
		self.assertTrue(captured, "engine.get_permission_conditions never called")
		for c in captured:
			self.assertEqual(
				c["engine_doctype"],
				"DocType",
				f"engine.doctype not set when querying {c['dt']!r}: {c['engine_doctype']!r}",
			)

	def test_subspec_engine_has_doctype_attribute_set(self):
		"""Same fix applied to the sub-spec engine. Without it, an
		EXISTS sub-query whose sub-spec's primary DocType has a
		permission_query_conditions hook crashes the same way."""
		captured = []

		def _capture(dt, table):
			engine_instance = fake_engine_cls.return_value
			captured.append(
				{
					"dt": dt,
					"engine_doctype": getattr(engine_instance, "doctype", "<UNSET>"),
				}
			)
			return None

		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine_cls,
		):
			fake_engine_cls.return_value.get_permission_conditions.side_effect = _capture
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "Custom Field",
									"alias": "cf",
									"where": [
										{
											"field": "cf.dt",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
		# Find the sub-spec calls (dt == "Custom Field") and check the
		# engine's doctype was the sub-spec's "from" value. (A non-child
		# doctype: F2 routes CHILD sub-spec doctypes through the record-scope
		# gate, so a non-child exercises the get_permission_conditions path.)
		subspec_calls = [c for c in captured if c["dt"] == "Custom Field"]
		self.assertTrue(
			subspec_calls,
			"sub-spec get_permission_conditions never called",
		)
		# Note: because we share a single mock Engine across outer
		# and sub-spec, the *last* assignment to engine.doctype wins
		# in this captured snapshot. The mock's doctype attribute at
		# capture time reflects the most-recent setter. For the
		# sub-spec calls, that setter was for the sub-spec's "from"
		# value (Custom Field).
		for c in subspec_calls:
			self.assertEqual(
				c["engine_doctype"],
				"Custom Field",
				f"sub_engine.doctype not set: {c['engine_doctype']!r}",
			)


class TestQueryRobustnessHardening(FrappeTestCase):
	"""Defensive validation that turns malformed-agent-input crashes /
	silently-bad-SQL into clean InvalidArgumentError. Catches what
	TypeBox can't (Type.Unknown shapes, semantic edge cases)."""

	def test_select_must_be_list_not_string(self):
		"""``select: 'name'`` would iterate per-character and silently
		produce table['n']/['a']/etc. Reject at validation time."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query({"from": "DocType", "select": "name"})
			self.assertIn("select", str(cm.exception).lower())

	def test_limit_must_be_int_not_float(self):
		"""``limit: 10.5`` would silently int-truncate to 10. Mirror
		offset's existing isinstance(int) check."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query({"from": "DocType", "limit": 10.5})
			self.assertIn("limit", str(cm.exception).lower())

	def test_limit_must_be_int_not_bool(self):
		"""``isinstance(True, int)`` is True in Python; explicit bool
		rejection keeps ``limit: true`` from sneaking through."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query({"from": "DocType", "limit": True})
			self.assertIn("limit", str(cm.exception).lower())

	def test_field_ref_rejects_empty_string(self):
		"""GROUP BY / ORDER BY paths could pass empty strings through
		to _resolve_field, which would produce table['']."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": ["name"],
						"group_by": [""],
					}
				)
			self.assertIn("non-empty", str(cm.exception).lower())

	def test_field_ref_rejects_trailing_dot(self):
		"""``alias.`` produces an empty field name after split;
		generates broken SQL with empty column reference."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt."],
					}
				)
			self.assertIn("alias.field", str(cm.exception).lower())

	def test_field_ref_rejects_leading_dot(self):
		"""``.field`` produces an empty alias half."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": [".name"],
					}
				)
			self.assertIn("alias.field", str(cm.exception).lower())

	def test_join_on_empty_dict_rejected(self):
		"""Empty ``on`` dict would crash _build_on_criterion at
		terms[0] with IndexError. Reject with a clear error."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"joins": [
							{
								"type": "inner",
								"doctype": "DocField",
								"alias": "df",
								"on": {},
							}
						],
						"select": ["dt.name"],
					}
				)
			self.assertIn("on", str(cm.exception).lower())

	def test_subspec_join_on_empty_dict_rejected(self):
		"""Same guard inside a sub-spec join."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"joins": [
										{
											"type": "inner",
											"doctype": "DocPerm",
											"alias": "dp",
											"on": {},
										}
									],
									"where": [
										{
											"field": "df.parent",
											"op": "=",
											"value": {"$field": "dt.name"},
										}
									],
								},
							}
						],
					}
				)
			self.assertIn("on", str(cm.exception).lower())

	def test_nested_field_marker_rejected(self):
		"""Buried ``{$field: ...}`` inside a nested dict value would
		reach pypika as a raw dict. Reject with a clear message."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": "DocField",
									"alias": "df",
									"where": [
										{
											"field": "df.parent",
											"op": "in",
											"value": [
												"foo",
												{"nested": {"$field": "dt.name"}},
											],
										}
									],
								},
							}
						],
					}
				)
			self.assertIn("$field", str(cm.exception))


class TestQueryExprDSLPhase1Dates(FrappeTestCase):
	"""Expression DSL Phase 1: foundation + date functions.

	Verifies the tree-shaped expression spec lands correctly in
	SELECT / GROUP BY / WHERE / aggregates, and that the date
	functions (date_part, date_trunc, date_add) emit the expected
	SQL substrings.
	"""

	def _build_sql(self, spec: dict) -> str:
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	def test_date_part_month_in_select(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "date_part",
							"args": [
								{"literal": "month"},
								{"field": "dt.creation"},
							],
						},
						"as": "month",
					}
				],
			}
		)
		self.assertIn("EXTRACT", sql.upper())
		self.assertIn("MONTH", sql.upper())

	def test_date_part_year_in_select(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "date_part",
							"args": [{"literal": "year"}, {"field": "dt.creation"}],
						},
						"as": "year",
					}
				],
			}
		)
		self.assertIn("EXTRACT", sql.upper())
		self.assertIn("YEAR", sql.upper())

	def test_date_part_rejects_unknown_unit(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "date_part",
									"args": [
										{"literal": "fortnight"},
										{"field": "creation"},
									],
								},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("date_part", str(cm.exception))

	def test_date_part_fiscal_year_explicit_error(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "date_part",
									"args": [
										{"literal": "fiscal_year"},
										{"field": "creation"},
									],
								},
								"as": "fy",
							}
						],
					}
				)
			self.assertIn("fiscal_year", str(cm.exception))

	def test_date_part_group_by_with_count_aggregate(self):
		"""The load-bearing reporting case: 'count per month'."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "date_part",
							"args": [
								{"literal": "month"},
								{"field": "dt.creation"},
							],
						},
						"as": "month",
					},
					{"agg": "count", "field": "*", "as": "n"},
				],
				"group_by": ["month"],
			}
		)
		self.assertIn("EXTRACT", sql.upper())
		self.assertIn("MONTH", sql.upper())
		self.assertIn("COUNT", sql.upper())
		self.assertIn("GROUP BY", sql.upper())

	def test_date_trunc_month_emits_format_or_trunc(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "date_trunc",
							"args": [
								{"literal": "month"},
								{"field": "dt.creation"},
							],
						},
						"as": "month_start",
					}
				],
			}
		)
		upper = sql.upper()
		self.assertTrue(
			"DATE_FORMAT" in upper or "DATE_TRUNC" in upper or "STRFTIME" in upper,
			f"expected a date-trunc construct, got: {sql}",
		)

	def test_date_add_in_where_predicate(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [
					{
						"expr": {
							"fn": "date_add",
							"args": [
								{"field": "dt.creation"},
								{"literal": 7},
								{"literal": "day"},
							],
						},
						"op": ">=",
						"value": "2026-06-01",
					}
				],
			}
		)
		self.assertIn("DATE_ADD", sql.upper())

	def test_expr_rejects_unknown_function(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {"fn": "not_a_function", "args": []},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("not_a_function", str(cm.exception))

	def test_expr_rejects_arity_mismatch(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "date_part",
									"args": [{"literal": "month"}],
								},
								"as": "m",
							}
						],
					}
				)
			self.assertIn("date_part", str(cm.exception))

	def test_expr_rejects_wrong_arg_kind(self):
		"""date_part's first arg must be a literal, not a field."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "date_part",
									"args": [
										{"field": "dt.unit"},
										{"field": "dt.creation"},
									],
								},
								"as": "m",
							}
						],
					}
				)
			self.assertIn("literal", str(cm.exception).lower())

	def test_expr_rejects_nesting_beyond_cap(self):
		deepest = {"field": "creation"}
		for _ in range(5):
			deepest = {
				"fn": "date_add",
				"args": [deepest, {"literal": 1}, {"literal": "day"}],
			}
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [{"expr": deepest, "as": "x"}],
					}
				)
			self.assertIn("nesting", str(cm.exception).lower())

	def test_expr_select_requires_as_alias(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {"field": "creation"},
							}
						],
					}
				)
			self.assertIn("as", str(cm.exception).lower())

	def test_expr_field_node_resolves_correctly(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [{"expr": {"field": "dt.name"}, "as": "name"}],
			}
		)
		self.assertIn("name", sql)

	def test_expr_literal_node(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"select": [
					"name",
					{"expr": {"literal": 42}, "as": "answer"},
				],
			}
		)
		self.assertIn("42", sql)

	def test_aggregate_can_wrap_expression(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"agg": "max",
						"expr": {
							"fn": "date_part",
							"args": [
								{"literal": "year"},
								{"field": "dt.creation"},
							],
						},
						"as": "max_year",
					}
				],
			}
		)
		self.assertIn("MAX", sql.upper())
		self.assertIn("EXTRACT", sql.upper())

	def test_aggregate_rejects_both_field_and_expr(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"agg": "sum",
								"field": "creation",
								"expr": {"field": "creation"},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("expr", str(cm.exception).lower())


class TestQueryExprDSLPhase2NullAndArithmetic(FrappeTestCase):
	"""Expression DSL Phase 2: NULL handling + arithmetic."""

	def _build_sql(self, spec: dict) -> str:
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	def test_coalesce_two_args(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "coalesce",
							"args": [{"field": "dt.module"}, {"literal": "(unset)"}],
						},
						"as": "module_or_unset",
					}
				],
			}
		)
		self.assertIn("COALESCE", sql.upper())

	def test_coalesce_variadic_three_args(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "coalesce",
							"args": [
								{"field": "dt.module"},
								{"field": "dt.name"},
								{"literal": "fallback"},
							],
						},
						"as": "x",
					}
				],
			}
		)
		self.assertIn("COALESCE", sql.upper())

	def test_ifnull(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "ifnull",
							"args": [{"field": "dt.module"}, {"literal": "Other"}],
						},
						"as": "module",
					}
				],
			}
		)
		self.assertIn("COALESCE", sql.upper())

	def test_arithmetic_mul(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {"fn": "mul", "args": [{"field": "dt.idx"}, {"literal": 100}]},
						"as": "scaled",
					}
				],
			}
		)
		self.assertIn("*", sql)
		self.assertIn("100", sql)

	def test_arithmetic_add_sub_div(self):
		for op_name, op_sym in [("add", "+"), ("sub", "-"), ("div", "/")]:
			sql = self._build_sql(
				{
					"from": "DocType",
					"alias": "dt",
					"select": [
						{
							"expr": {"fn": op_name, "args": [{"field": "dt.idx"}, {"literal": 2}]},
							"as": "x",
						}
					],
				}
			)
			self.assertIn(op_sym, sql, f"{op_name} should emit {op_sym!r}")

	def test_neg_emits_unary_negation(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {"fn": "neg", "args": [{"field": "dt.idx"}]},
						"as": "x",
					}
				],
			}
		)
		self.assertIn("0", sql)

	def test_abs(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [{"expr": {"fn": "abs", "args": [{"field": "dt.idx"}]}, "as": "x"}],
			}
		)
		self.assertIn("ABS", sql.upper())

	def test_round(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {"fn": "round", "args": [{"field": "dt.idx"}, {"literal": 2}]},
						"as": "x",
					}
				],
			}
		)
		self.assertIn("ROUND", sql.upper())

	def test_round_rejects_non_literal_digits(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {"fn": "round", "args": [{"field": "x"}, {"field": "y"}]},
								"as": "x",
							}
						],
					}
				)

	def test_ceil_and_floor(self):
		for fn_name, expected in [("ceil", "CEIL"), ("floor", "FLOOR")]:
			sql = self._build_sql(
				{
					"from": "DocType",
					"alias": "dt",
					"select": [{"expr": {"fn": fn_name, "args": [{"field": "dt.idx"}]}, "as": "x"}],
				}
			)
			self.assertIn(expected, sql.upper())

	def test_nested_arithmetic_in_aggregate(self):
		"""SUM(qty * rate) - the flagship combination."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"agg": "sum",
						"expr": {"fn": "mul", "args": [{"field": "dt.idx"}, {"literal": 10}]},
						"as": "total",
					}
				],
			}
		)
		self.assertIn("SUM", sql.upper())
		self.assertIn("*", sql)


class TestQueryExprDSLPhase3CaseWhen(FrappeTestCase):
	"""Expression DSL Phase 3: CASE WHEN conditional aggregation."""

	def _build_sql(self, spec: dict) -> str:
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	def test_case_when_with_else(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "case",
							"args": [
								{
									"when": {"field": "dt.module", "op": "=", "value": "Core"},
									"then": {"literal": "core"},
								},
								{"else": {"literal": "other"}},
							],
						},
						"as": "bucket",
					}
				],
			}
		)
		upper = sql.upper()
		self.assertIn("CASE", upper)
		self.assertIn("WHEN", upper)
		self.assertIn("ELSE", upper)
		self.assertIn("END", upper)

	def test_case_when_only_no_else(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "case",
							"args": [
								{
									"when": {"field": "dt.module", "op": "=", "value": "Core"},
									"then": {"literal": "core"},
								},
							],
						},
						"as": "bucket",
					}
				],
			}
		)
		self.assertIn("CASE", sql.upper())
		self.assertIn("WHEN", sql.upper())

	def test_case_inside_sum_for_conditional_aggregate(self):
		"""SUM(CASE WHEN ... THEN ... ELSE 0) - killer use case."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"agg": "sum",
						"expr": {
							"fn": "case",
							"args": [
								{
									"when": {"field": "dt.module", "op": "=", "value": "Core"},
									"then": {"field": "dt.idx"},
								},
								{"else": {"literal": 0}},
							],
						},
						"as": "core_idx_total",
					}
				],
			}
		)
		upper = sql.upper()
		self.assertIn("SUM", upper)
		self.assertIn("CASE", upper)
		self.assertIn("WHEN", upper)

	def test_case_rejects_empty_clauses(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [{"expr": {"fn": "case", "args": []}, "as": "x"}],
					}
				)
			self.assertIn("case", str(cm.exception).lower())

	def test_case_rejects_multiple_else(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "case",
									"args": [
										{"else": {"literal": 1}},
										{"else": {"literal": 2}},
									],
								},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("else", str(cm.exception).lower())

	def test_case_rejects_else_not_last(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "case",
									"args": [
										{"else": {"literal": 0}},
										{
											"when": {"field": "module", "op": "=", "value": "Core"},
											"then": {"literal": 1},
										},
									],
								},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("last", str(cm.exception).lower())

	def test_case_rejects_when_without_then(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "case",
									"args": [{"when": {"field": "module", "op": "=", "value": "Core"}}],
								},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("when", str(cm.exception).lower())

	def test_case_rejects_exists_in_when(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": [
							{
								"expr": {
									"fn": "case",
									"args": [
										{
											"when": {"op": "exists", "value": {"from": "DocField"}},
											"then": {"literal": 1},
										}
									],
								},
								"as": "x",
							}
						],
					}
				)
			self.assertIn("exists", str(cm.exception).lower())


class TestQueryExprDSLPhase4StringHelpers(FrappeTestCase):
	"""Expression DSL Phase 4: string + numeric helpers."""

	def _build_sql(self, spec: dict) -> str:
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	def test_concat_two_args(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {"fn": "concat", "args": [{"field": "dt.module"}, {"literal": "/"}]},
						"as": "labelled",
					}
				],
			}
		)
		self.assertIn("CONCAT", sql.upper())

	def test_concat_variadic_three_args(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "concat",
							"args": [
								{"field": "dt.module"},
								{"literal": "/"},
								{"field": "dt.name"},
							],
						},
						"as": "full",
					}
				],
			}
		)
		self.assertIn("CONCAT", sql.upper())

	def test_lower_for_case_insensitive_match(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name"],
				"where": [
					{
						"expr": {"fn": "lower", "args": [{"field": "dt.name"}]},
						"op": "like",
						"value": "%customer%",
					}
				],
			}
		)
		self.assertIn("LOWER", sql.upper())
		self.assertIn("LIKE", sql.upper())

	def test_upper_trim_length(self):
		for fn_name, expected in [("upper", "UPPER"), ("trim", "TRIM"), ("length", "LENGTH")]:
			sql = self._build_sql(
				{
					"from": "DocType",
					"alias": "dt",
					"select": [
						{
							"expr": {"fn": fn_name, "args": [{"field": "dt.module"}]},
							"as": "x",
						}
					],
				}
			)
			self.assertIn(expected, sql.upper())

	def test_substring(self):
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {
							"fn": "substring",
							"args": [{"field": "dt.module"}, {"literal": 1}, {"literal": 3}],
						},
						"as": "prefix",
					}
				],
			}
		)
		self.assertIn("SUBSTRING", sql.upper())

	def test_substring_rejects_non_literal_offsets(self):
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"select": [
							{
								"expr": {
									"fn": "substring",
									"args": [{"field": "x"}, {"field": "y"}, {"literal": 3}],
								},
								"as": "x",
							}
						],
					}
				)


class TestQuerySqlInjectionHardening(FrappeTestCase):
	"""SEC-003: field / alias / doctype identifiers must be validated
	before they reach pypika, which quotes identifiers with backticks
	but does NOT escape an embedded backtick/quote. Without this, a
	crafted identifier from the agent-facing spec (reachable via the
	prompt-injection chain) could break out of the quoting and inject a
	``UNION SELECT`` that runs with the site's full DB privilege —
	``frappe.set_user`` changes only the application user, not the DB
	user, so the injected query can read ``__Auth`` and bypass the
	tool's permission weave.
	"""

	def _build_sql(self, spec: dict) -> str:
		"""Mirror of TestQueryQbTranslation._build_sql: run the full
		pipeline, capture the resolved SQL without executing."""
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			with patch("pypika.queries.QueryBuilder.run", return_value=[]):
				result = query(spec)
		return result["sql"]

	# ---- backtick / quote rejection --------------------------------

	def test_field_with_backtick_rejected(self):
		"""A field carrying a backtick would break out of pypika's
		identifier quoting; reject it before it reaches the query."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name`"],
					}
				)

	def test_alias_with_backtick_rejected(self):
		"""The FROM alias flows into pypika's ``.as_()`` sink."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query({"from": "DocType", "alias": "dt`"})

	# ---- ") UNION SELECT" payload rejection ------------------------

	def test_field_with_union_select_payload_rejected(self):
		"""The canonical injection payload as a WHERE field identifier."""
		payload = "name`) UNION SELECT `password"
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"where": [{"field": f"dt.{payload}", "op": "=", "value": 1}],
					}
				)

	def test_select_output_alias_with_union_select_rejected(self):
		"""The SELECT ``as`` output alias flows into pypika's ``.as_()``
		sink and must be validated."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"select": [
							{
								"agg": "count",
								"field": "*",
								"as": "n) UNION SELECT password FROM tabUser -- ",
							}
						],
					}
				)

	def test_join_alias_with_injection_rejected(self):
		"""A JOIN alias with a space/paren payload is rejected before it
		reaches ``.as_()``."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.name"],
						"joins": [
							{
								"type": "left",
								"doctype": "DocField",
								"alias": "df) UNION SELECT",
								"on": {"df.parent": "dt.name"},
							}
						],
					}
				)

	# ---- unknown column / doctype rejection ------------------------

	def test_unknown_column_rejected(self):
		"""A syntactically-valid but non-existent column is rejected via
		``get_valid_columns()``."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query(
					{
						"from": "DocType",
						"alias": "dt",
						"select": ["dt.definitely_not_a_real_column"],
					}
				)
			self.assertIn("column", str(cm.exception).lower())

	def test_unknown_doctype_rejected(self):
		"""A non-existent DocType (the table-name sink) is rejected."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError) as cm:
				query({"from": "No Such DocType XYZ"})
			self.assertIn("doctype", str(cm.exception).lower())

	# ---- over-blocking guard: legitimate queries still pass --------

	def test_standard_and_real_columns_still_pass(self):
		"""Standard Frappe columns (name/creation/idx) plus real fields
		(issingle/module) must NOT be rejected by the column check."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": ["dt.name", "dt.creation", "dt.idx", "dt.module"],
				"where": [{"field": "dt.issingle", "op": "=", "value": 1}],
				"order_by": [{"field": "dt.name", "dir": "asc"}],
			}
		)
		self.assertIn("issingle", sql)
		self.assertIn("creation", sql)

	def test_group_by_select_alias_still_pass(self):
		"""GROUP BY referencing a SELECT output alias (not a physical
		column) must remain valid after the identifier hardening — the
		alias is regex-checked but not column-existence-checked."""
		sql = self._build_sql(
			{
				"from": "DocType",
				"alias": "dt",
				"select": [
					{
						"expr": {"fn": "date_part", "args": [{"literal": "month"}, {"field": "dt.creation"}]},
						"as": "month",
					},
					{"agg": "count", "field": "*", "as": "n"},
				],
				"group_by": ["month"],
			}
		)
		self.assertIn("GROUP BY", sql.upper())

	def test_optional_meta_column_still_pass(self):
		"""The optional meta columns (_assign / _comments / _liked_by /
		_user_tags / _seen) are real, queryable columns on standard
		doctypes but are NOT returned by ``get_valid_columns()`` — they
		must not be over-blocked by the column-existence check. Uses
		``User`` (a non-special doctype whose get_valid_columns() takes
		the default_fields + docfields path that omits _assign); a
		special doctype like DocType would mask the gap because its path
		returns the full DB column list."""
		sql = self._build_sql(
			{
				"from": "User",
				"alias": "u",
				"select": ["u.name"],
				"where": [{"field": "u._assign", "op": "like", "value": "%Administrator%"}],
			}
		)
		self.assertIn("_assign", sql)


class TestQueryPermlevelFieldACL(FrappeTestCase):
	"""F2: the column resolver must enforce field-level (permlevel) read
	permission, mirroring frappe.get_list's apply_fieldlevel_read_permissions
	(frappe/model/db_query.py -> frappe.model.get_permitted_fields).

	Pre-fix, ``_validate_column`` only proved a column EXISTED
	(``get_valid_columns()``), never that the caller could READ it. A user
	with plain doctype read but no elevated-permlevel role could
	select / filter / order / group-by / having / join-on / EXISTS any
	permlevel>0 field. These tests prove:

	- A restricted user (permlevel-0 role only) is DENIED on a permlevel>0
	  field in every field position that funnels through the resolver.
	- A permlevel-0 field still works for that restricted user (no
	  over-blocking).
	- A privileged user (permlevel-1 role) can read the restricted field.
	- Child-table fields are gated via the base/FROM doctype's permissions
	  (parenttype), exactly as apply_fieldlevel_read_permissions does.

	Fixture mirrors test_permlevel_leak.py: one header doctype + one child
	table doctype, each with a permlevel-0 and a permlevel-1 field; a base
	role (permlevel-0 grants only) and a priv role (adds permlevel-1 read).

	Note on child-table tests: ``frappe.has_permission(<child>, "read")``
	with no parent context returns False for non-admins (Frappe's
	has_child_permission), so the query tool's DocType-level gate (step 3)
	would block a non-admin child-table join for an unrelated reason. Those
	tests patch ``frappe.has_permission`` to isolate the *field-level* ACL,
	while still running as the real restricted / privileged user so the
	role-driven permlevel resolution is exercised for real.
	"""

	PARENT_DT = "JV Query Permlevel Parent"
	CHILD_DT = "JV Query Permlevel Child"
	F_PUBLIC = "public_field"
	F_RESTRICTED = "restricted_field"
	CF_PUBLIC = "child_public"
	CF_RESTRICTED = "child_restricted"
	ROLE_BASE = "JQP Base Role"
	ROLE_PRIV = "JQP Priv Role"
	USER_RESTRICTED = "jqp-restricted@example.com"
	USER_PRIVILEGED = "jqp-privileged@example.com"

	# ---- F2 record-level child-scope fixture (separate family) ----------
	# A dedicated child doctype (with rows) owned by TWO parents, so the
	# per-alias record-level gate can be exercised for real without adding
	# rows to the F1 CHILD_DT (whose admin FROM-child e2e asserts an EMPTY
	# table). Parent A and Parent B both own SCOPE_CHILD_DT; a Parent A doc
	# (A1) and a Parent B doc (B1) share the SAME name to prove the parenttype
	# conjunct closes the cross-parent name-collision leak.
	SCOPE_CHILD_DT = "JV Query Scope Child"
	SCOPE_PARENT_A = "JV Query Scope Parent A"
	SCOPE_PARENT_B = "JV Query Scope Parent B"
	ROLE_SCOPE_A = "JQP Scope A Role"
	ROLE_SCOPE_B = "JQP Scope B Role"
	# reads Parent A only, User-Permission-restricted to {A1, A3, A4}
	USER_SCOPE_A = "jqp-scope-a@example.com"
	# reads BOTH Parent A and Parent B, no User Permission (all records)
	USER_SCOPE_AB = "jqp-scope-ab@example.com"
	# reads NEITHER scope parent (only the unrelated F1 parent via ROLE_BASE)
	USER_SCOPE_NONE = "jqp-scope-none@example.com"
	# H1: a SINGLE DocType also owns SCOPE_CHILD_DT; this user reads ONLY the
	# Single (not Parent A/B). The Single scope must still fence by parenttype.
	SCOPE_SINGLE = "JV Query Scope Single"
	ROLE_SCOPE_SINGLE = "JQP Scope Single Role"
	USER_SCOPE_SINGLE = "jqp-scope-single@example.com"
	SINGLE_VAL = 7777  # the Single's own child row; the only value this user may read
	# A1 (Parent A) and B1 (Parent B) deliberately share this name.
	COLLIDE_NAME = "JVQS-COLLIDE"
	A2_NAME = "JVQS-A2"  # Parent A, NOT visible to USER_SCOPE_A
	A3_NAME = "JVQS-A3"  # Parent A, visible, childless (LEFT-JOIN null-extension)
	# A4 (Parent A, childless, visible) and B2 (Parent B, has a child) share
	# this name — the EXISTS-site parenttype-collision leak probe.
	SHARED_NAME = "JVQS-SHARED"

	@staticmethod
	def _ensure_role(name: str) -> None:
		if not frappe.db.exists("Role", name):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": name,
					"desk_access": 1,
					"is_custom": 1,
				}
			).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_user(email: str, roles: tuple) -> None:
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
		user = frappe.get_doc("User", email)
		if "System Manager" in frappe.get_roles(email):
			user.remove_roles("System Manager")
		missing = [r for r in roles if r not in frappe.get_roles(email)]
		if missing:
			user.add_roles(*missing)

	@classmethod
	def _ensure_doctypes(cls) -> None:
		from frappe.core.doctype.doctype.test_doctype import new_doctype

		for dt in (cls.PARENT_DT, cls.CHILD_DT):
			if frappe.db.exists("DocType", dt):
				frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
		# Child table (istable) — its own permissions are irrelevant to the
		# field ACL (the parent's permissions govern child fields), but it
		# must exist before the parent references it.
		new_doctype(
			name=cls.CHILD_DT,
			custom=1,
			istable=1,
			fields=[
				{"label": "Child Public", "fieldname": cls.CF_PUBLIC, "fieldtype": "Data", "permlevel": 0},
				{
					"label": "Child Restricted",
					"fieldname": cls.CF_RESTRICTED,
					"fieldtype": "Data",
					"permlevel": 1,
				},
			],
			permissions=[],
		).insert()
		new_doctype(
			name=cls.PARENT_DT,
			custom=1,
			fields=[
				{"label": "Public Field", "fieldname": cls.F_PUBLIC, "fieldtype": "Data", "permlevel": 0},
				{
					"label": "Restricted Field",
					"fieldname": cls.F_RESTRICTED,
					"fieldtype": "Data",
					"permlevel": 1,
				},
				{"label": "Items", "fieldname": "items", "fieldtype": "Table", "options": cls.CHILD_DT},
			],
			permissions=[
				{"role": cls.ROLE_BASE, "permlevel": 0, "read": 1, "write": 1, "create": 1},
				{"role": cls.ROLE_PRIV, "permlevel": 0, "read": 1, "write": 1, "create": 1},
				{"role": cls.ROLE_PRIV, "permlevel": 1, "read": 1, "write": 1},
			],
		).insert()

	@classmethod
	def _ensure_scope_doctypes(cls) -> None:
		"""F2 fixture: a child doctype owned by TWO parents (autoname prompt so
		A1 and B1 can share a name), plus a permlevel-0-only role per parent."""
		from frappe.core.doctype.doctype.test_doctype import new_doctype

		for dt in (cls.SCOPE_PARENT_A, cls.SCOPE_PARENT_B, cls.SCOPE_SINGLE, cls.SCOPE_CHILD_DT):
			if frappe.db.exists("DocType", dt):
				frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
		new_doctype(
			name=cls.SCOPE_CHILD_DT,
			custom=1,
			istable=1,
			fields=[
				{"label": "Child Public", "fieldname": "child_public", "fieldtype": "Data", "permlevel": 0},
				{
					"label": "Child Restricted",
					"fieldname": "child_restricted",
					"fieldtype": "Data",
					"permlevel": 1,
				},
				{"label": "Scope Val", "fieldname": "scope_val", "fieldtype": "Int", "permlevel": 0},
			],
			permissions=[],
		).insert()
		for parent_dt, role in (
			(cls.SCOPE_PARENT_A, cls.ROLE_SCOPE_A),
			(cls.SCOPE_PARENT_B, cls.ROLE_SCOPE_B),
		):
			new_doctype(
				name=parent_dt,
				custom=1,
				autoname="prompt",
				fields=[
					{
						"label": "Public Field",
						"fieldname": "public_field",
						"fieldtype": "Data",
						"permlevel": 0,
					},
					{
						"label": "Items",
						"fieldname": "items",
						"fieldtype": "Table",
						"options": cls.SCOPE_CHILD_DT,
					},
				],
				permissions=[{"role": role, "permlevel": 0, "read": 1}],
			).insert()
		# H1 fixture: a SINGLE DocType that ALSO owns SCOPE_CHILD_DT. A user who
		# can read only this Single must NOT see Parent A/B child rows.
		new_doctype(
			name=cls.SCOPE_SINGLE,
			custom=1,
			issingle=1,
			fields=[
				{
					"label": "Entries",
					"fieldname": "entries",
					"fieldtype": "Table",
					"options": cls.SCOPE_CHILD_DT,
				},
			],
			permissions=[{"role": cls.ROLE_SCOPE_SINGLE, "permlevel": 0, "read": 1}],
		).insert()

	@classmethod
	def _seed_scope_data(cls) -> None:
		"""Wipe and re-insert the F2 parent docs + child rows (idempotent)."""
		for dt in (cls.SCOPE_PARENT_A, cls.SCOPE_PARENT_B):
			for name in frappe.get_all(dt, pluck="name"):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)

		def _mk(parent_dt, name, public_field, children):
			doc = frappe.get_doc({"doctype": parent_dt, "name": name, "public_field": public_field})
			for pub, restricted, val in children:
				doc.append(
					"items",
					{"child_public": pub, "child_restricted": restricted, "scope_val": val},
				)
			doc.insert(ignore_permissions=True)

		# Parent A: A1 (3 children), A2 (1 child, not visible to USER_SCOPE_A),
		# A3 (childless, visible), A4 (childless, visible, name collides w/ B2).
		_mk(
			cls.SCOPE_PARENT_A,
			cls.COLLIDE_NAME,
			"collide",
			[
				("a1", "sekret", 10),
				("a1", "sekret", 20),
				("collide", "sekret", 30),
			],
		)
		_mk(cls.SCOPE_PARENT_A, cls.A2_NAME, "collide", [("collide", "sekret", 100)])
		_mk(cls.SCOPE_PARENT_A, cls.A3_NAME, "none", [])
		_mk(cls.SCOPE_PARENT_A, cls.SHARED_NAME, "none", [])
		# Parent B: B1 (name == A1) with a child, B2 (name == A4) with a child.
		_mk(cls.SCOPE_PARENT_B, cls.COLLIDE_NAME, "collide", [("collide", "sekret", 1000)])
		_mk(cls.SCOPE_PARENT_B, cls.SHARED_NAME, "b2", [("b2", "sekret", 2000)])
		# The SINGLE parent's own child row (parenttype = the Single).
		single_doc = frappe.get_doc(cls.SCOPE_SINGLE)
		single_doc.set("entries", [])
		single_doc.append(
			"entries",
			{"child_public": "single", "child_restricted": "sekret", "scope_val": cls.SINGLE_VAL},
		)
		single_doc.save(ignore_permissions=True)

	@classmethod
	def _set_scope_user_permissions(cls) -> None:
		"""USER_SCOPE_A may see only Parent A docs A1, A3, A4 (User Permission)."""
		for up in frappe.get_all("User Permission", filters={"user": cls.USER_SCOPE_A}, pluck="name"):
			frappe.delete_doc("User Permission", up, force=True, ignore_permissions=True)
		for value in (cls.COLLIDE_NAME, cls.A3_NAME, cls.SHARED_NAME):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": cls.USER_SCOPE_A,
					"allow": cls.SCOPE_PARENT_A,
					"for_value": value,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._ensure_role(cls.ROLE_BASE)
		cls._ensure_role(cls.ROLE_PRIV)
		cls._ensure_role(cls.ROLE_SCOPE_A)
		cls._ensure_role(cls.ROLE_SCOPE_B)
		cls._ensure_role(cls.ROLE_SCOPE_SINGLE)
		cls._ensure_doctypes()
		cls._ensure_scope_doctypes()
		cls._ensure_user(cls.USER_RESTRICTED, (cls.ROLE_BASE,))
		cls._ensure_user(cls.USER_PRIVILEGED, (cls.ROLE_PRIV,))
		cls._ensure_user(cls.USER_SCOPE_A, (cls.ROLE_SCOPE_A,))
		cls._ensure_user(cls.USER_SCOPE_AB, (cls.ROLE_SCOPE_A, cls.ROLE_SCOPE_B))
		cls._ensure_user(cls.USER_SCOPE_NONE, (cls.ROLE_BASE,))
		cls._ensure_user(cls.USER_SCOPE_SINGLE, (cls.ROLE_SCOPE_SINGLE,))
		cls._seed_scope_data()
		cls._set_scope_user_permissions()
		frappe.db.commit()
		# Warm the meta cache for the fixture doctypes. The run-tests harness
		# on this site can hit a pre-existing infinite get_meta recursion when
		# cold-loading a doctype's meta (client_cache not persisting mid-load,
		# unrelated to this fix — it also flakes existing Sales Invoice / User
		# tests). Warming here keeps the child-table tests off that path.
		frappe.get_meta(cls.PARENT_DT)
		frappe.get_meta(cls.CHILD_DT)
		frappe.get_meta(cls.SCOPE_CHILD_DT)
		frappe.get_meta(cls.SCOPE_PARENT_A)
		frappe.get_meta(cls.SCOPE_PARENT_B)
		frappe.get_meta(cls.SCOPE_SINGLE)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Ensure the shared per-site allowlist doesn't pre-empt the
		# field-level check with an allowlist rejection.
		settings = frappe.get_single("Jarvis Settings")
		settings.run_query_doctype_allowlist = ""
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Jarvis Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	# ---- header-doctype field positions (run fully real) -----------

	def test_restricted_cannot_select_permlevel_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with self.assertRaises(PermissionDeniedError) as cm:
			query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"select": ["p.public_field", "p.restricted_field"],
				}
			)
		self.assertIn("restricted_field", str(cm.exception))

	def test_restricted_cannot_filter_permlevel_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with self.assertRaises(PermissionDeniedError):
			query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"select": ["p.public_field"],
					"where": [{"field": "p.restricted_field", "op": "=", "value": "x"}],
				}
			)

	def test_restricted_cannot_order_by_permlevel_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with self.assertRaises(PermissionDeniedError):
			query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"select": ["p.public_field"],
					"order_by": [{"field": "p.restricted_field", "dir": "asc"}],
				}
			)

	def test_restricted_cannot_group_by_permlevel_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with self.assertRaises(PermissionDeniedError):
			query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"select": ["p.public_field"],
					"group_by": ["p.restricted_field"],
				}
			)

	def test_restricted_cannot_having_permlevel_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with self.assertRaises(PermissionDeniedError):
			query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"select": ["p.public_field"],
					"group_by": ["p.public_field"],
					"having": [{"field": "p.restricted_field", "op": "=", "value": "x"}],
				}
			)

	def test_restricted_can_select_public_field(self):
		"""Over-block guard: a permlevel-0 field still works for the
		restricted user (runs fully real end-to-end)."""
		frappe.set_user(self.USER_RESTRICTED)
		result = query(
			{
				"from": self.PARENT_DT,
				"alias": "p",
				"select": ["p.name", "p.public_field"],
			}
		)
		self.assertIn("rows", result)

	def test_privileged_can_select_permlevel_field(self):
		frappe.set_user(self.USER_PRIVILEGED)
		result = query(
			{
				"from": self.PARENT_DT,
				"alias": "p",
				"select": ["p.name", "p.public_field", "p.restricted_field"],
			}
		)
		self.assertIn("rows", result)

	# ---- child-table field positions (parenttype resolution) -------

	def test_restricted_cannot_select_child_permlevel_field(self):
		"""Child-table permlevel field is gated via the base/FROM doctype's
		permissions (parenttype), mirroring
		apply_fieldlevel_read_permissions' parenttype=self.doctype."""
		frappe.set_user(self.USER_RESTRICTED)
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(PermissionDeniedError) as cm:
				query(
					{
						"from": self.PARENT_DT,
						"alias": "p",
						"joins": [
							{
								"type": "left",
								"doctype": self.CHILD_DT,
								"alias": "c",
								"on": {"c.parent": "p.name"},
							}
						],
						"select": ["p.name", "c.child_restricted"],
					}
				)
		self.assertIn("child_restricted", str(cm.exception))

	def test_restricted_can_select_child_public_field(self):
		frappe.set_user(self.USER_RESTRICTED)
		with (
			patch("frappe.has_permission", return_value=True),
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"joins": [
						{
							"type": "left",
							"doctype": self.CHILD_DT,
							"alias": "c",
							"on": {"c.parent": "p.name"},
						}
					],
					"select": ["p.name", "c.child_public"],
				}
			)
		self.assertIn("rows", result)

	def test_privileged_can_select_child_permlevel_field(self):
		frappe.set_user(self.USER_PRIVILEGED)
		with (
			patch("frappe.has_permission", return_value=True),
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"joins": [
						{
							"type": "left",
							"doctype": self.CHILD_DT,
							"alias": "c",
							"on": {"c.parent": "p.name"},
						}
					],
					"select": ["p.name", "c.child_restricted"],
				}
			)
		self.assertIn("rows", result)

	# ---- DocType-level gate: child perm derived from a parent ------
	# These exercise step 3's child-table branch with the REAL has_permission
	# (not the patched gate above), proving a non-admin who can read the parent
	# is not falsely denied on the child. _child_table_parents is patched so the
	# derivation does not issue a DB query through the mocked Engine.

	def test_restricted_can_query_child_via_readable_parent(self):
		"""A restricted user who can read the parent passes the DocType gate
		for the child: has_permission(child) is False without a parent, so the
		gate derives permission from a readable owning parent (mirrors
		get_list) instead of failing closed."""
		frappe.set_user(self.USER_RESTRICTED)
		with (
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{
					"from": self.PARENT_DT,
					"alias": "p",
					"joins": [
						{
							"type": "left",
							"doctype": self.CHILD_DT,
							"alias": "c",
							"on": {"c.parent": "p.name"},
						}
					],
					"select": ["p.name", "c.child_public"],
				}
			)
		self.assertIn("rows", result)

	def test_child_with_no_readable_parent_is_denied(self):
		"""When no owning parent the caller can read is derivable, the child
		read is a genuine denial (the gate does not silently allow it). The
		PermissionDeniedError is raised in step 3, before any query translation,
		so no Engine/run patch is needed."""
		frappe.set_user(self.USER_RESTRICTED)
		with patch("jarvis.tools.get_list._child_table_parents", return_value=[]):
			with self.assertRaises(PermissionDeniedError):
				query(
					{
						"from": self.PARENT_DT,
						"alias": "p",
						"joins": [
							{
								"type": "left",
								"doctype": self.CHILD_DT,
								"alias": "c",
								"on": {"c.parent": "p.name"},
							}
						],
						"select": ["p.name", "c.child_public"],
					}
				)

	# ---- join ON + EXISTS positions --------------------------------

	def test_restricted_cannot_join_on_permlevel_field(self):
		"""A permlevel>0 field used in a JOIN ... ON clause is gated too."""
		frappe.set_user(self.USER_RESTRICTED)
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(PermissionDeniedError) as cm:
				query(
					{
						"from": self.PARENT_DT,
						"alias": "p",
						"joins": [
							{
								"type": "left",
								"doctype": self.CHILD_DT,
								"alias": "c",
								"on": {"c.child_restricted": "p.restricted_field"},
							}
						],
						"select": ["p.name"],
					}
				)
		# The lhs (c.child_restricted) resolves first, so the error names it.
		self.assertIn("child_restricted", str(cm.exception))

	def test_restricted_cannot_reference_permlevel_field_in_exists(self):
		"""A permlevel>0 field inside an EXISTS sub-spec WHERE is gated."""
		frappe.set_user(self.USER_RESTRICTED)
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(PermissionDeniedError):
				query(
					{
						"from": self.PARENT_DT,
						"alias": "p",
						"select": ["p.name"],
						"where": [
							{
								"op": "exists",
								"value": {
									"from": self.PARENT_DT,
									"alias": "p2",
									"where": [
										{
											"field": "p2.restricted_field",
											"op": "=",
											"value": "x",
										}
									],
								},
							}
						],
					}
				)

	# ---- child table used as the FROM/base doctype ----------------
	# The customer-reported bug: a child (istable) DocType queried DIRECTLY as
	# the FROM doctype has dt == base_doctype, so the pre-fix field ACL passed
	# parenttype=None. get_permitted_fieldnames returns [] for an istable doctype
	# with parenttype=None (frappe/model/meta.py), so EVERY field - even a
	# permlevel-0 one - was rejected as "restricted by permission level". The fix
	# resolves the child's owning parent(s), mirroring step 3's record-level gate.
	# _child_table_parents is patched so the derivation does not issue a DB query
	# through the mocked Engine (see the step-3 tests above).

	def test_restricted_can_select_child_public_field_as_base(self):
		"""RED before fix: a permlevel-0 field on a child table queried as the
		base/FROM doctype must stay readable for a restricted (permlevel-0) user."""
		frappe.set_user(self.USER_RESTRICTED)
		with (
			patch("frappe.has_permission", return_value=True),
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{
					"from": self.CHILD_DT,
					"alias": "c",
					"select": ["c.name", "c.child_public"],
				}
			)
		self.assertIn("rows", result)

	def test_restricted_cannot_select_child_permlevel_field_as_base(self):
		"""Security guard: resolving the child-as-base ACL against the owning
		parent must NOT open a permlevel>0 field. The restricted user has no
		permlevel-1 grant on the parent, so child_restricted stays denied even
		when the child is the FROM doctype. Raises in field validation, before
		execution - no Engine/run patch needed."""
		frappe.set_user(self.USER_RESTRICTED)
		with (
			patch("frappe.has_permission", return_value=True),
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
		):
			with self.assertRaises(PermissionDeniedError) as cm:
				query(
					{
						"from": self.CHILD_DT,
						"alias": "c",
						"select": ["c.child_restricted"],
					}
				)
		self.assertIn("child_restricted", str(cm.exception))

	def test_privileged_can_select_child_permlevel_field_as_base(self):
		"""The privileged user (permlevel-1 read on the parent) CAN read the
		child's permlevel>0 field when the child is queried as the base."""
		frappe.set_user(self.USER_PRIVILEGED)
		with (
			patch("frappe.has_permission", return_value=True),
			patch(
				"jarvis.tools.get_list._child_table_parents",
				return_value=[self.PARENT_DT],
			),
			patch("frappe.database.query.Engine") as fake_engine,
			patch("pypika.queries.QueryBuilder.run", return_value=[]),
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			result = query(
				{
					"from": self.CHILD_DT,
					"alias": "c",
					"select": ["c.name", "c.child_restricted"],
				}
			)
		self.assertIn("rows", result)

	def test_admin_can_select_child_field_as_base_end_to_end(self):
		"""End-to-end regression for the customer report, NO mocks: even
		Administrator was blackholed pre-fix, because the istable + parenttype=None
		short-circuit in get_permitted_fieldnames (frappe/model/meta.py) runs before
		any user check. Runs the REAL pipeline (step-3 gate + field ACL + Engine perm
		conditions + SQL) against the empty fixture child table."""
		frappe.set_user("Administrator")
		result = query(
			{
				"from": self.CHILD_DT,
				"alias": "c",
				"select": ["c.name", "c.child_public"],
			}
		)
		self.assertIn("rows", result)
		self.assertEqual(result["rows"], [])

	# ================================================================
	# F2: child-aware record-level gate (per-alias parent scoping).
	# Real, unmocked — the child rows and User Permissions are seeded in
	# setUpClass so the record-level scope executes against live SQL.
	# ================================================================

	def _scope_vals(self, spec):
		spec.setdefault("limit", 100)
		return sorted(r["scope_val"] for r in query(spec)["rows"] if r.get("scope_val") is not None)

	# ---- 1 & 2: bare FROM child, single readable parent -------------

	def test_scope_from_child_isin_excludes_other_parent_record(self):
		"""[M: drop isin subquery] FROM child as a user User-Permission-restricted
		to A1: A2's child (scope_val 100, a Parent A record they cannot see) is
		excluded by the parent subquery membership test."""
		frappe.set_user(self.USER_SCOPE_A)
		vals = self._scope_vals({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.scope_val"]})
		self.assertEqual(vals, [10, 20, 30])  # A1's three children only
		self.assertNotIn(100, vals)  # A2 excluded by the isin subquery

	def test_scope_from_child_parenttype_closes_name_collision(self):
		"""[M: drop parenttype conjunct] B1's child (scope_val 1000) must be
		excluded even though B1.name == A1.name and A1 is visible — only the
		parenttype conjunct distinguishes the two same-named parent records."""
		frappe.set_user(self.USER_SCOPE_A)
		vals = self._scope_vals({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.scope_val"]})
		self.assertNotIn(1000, vals)  # B1's child excluded despite the name collision

	def test_scope_single_parent_does_not_leak_other_parents(self):
		"""[M: restore `if issingle: return None`] H1 regression. When the sole
		readable owning parent is a SINGLE, the parent subquery is skipped (a
		Single has no per-record rows), but the parenttype conjunct must NOT be:
		a user who can read only the Single sees just the Single's own child rows,
		never Parent A/B's."""
		frappe.set_user(self.USER_SCOPE_SINGLE)
		vals = self._scope_vals({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.scope_val"]})
		self.assertEqual(vals, [self.SINGLE_VAL])
		for leaked in (10, 20, 30, 100, 1000, 2000):
			self.assertNotIn(leaked, vals)  # no Parent A/B child rows leak via the Single

	# ---- 3: aggregate correctness (restricted vs Administrator) ------

	def test_scope_aggregate_restricted_totals(self):
		frappe.set_user(self.USER_SCOPE_A)
		rows = query(
			{
				"from": self.SCOPE_CHILD_DT,
				"alias": "c",
				"select": [
					{"agg": "sum", "field": "c.scope_val", "as": "total"},
					{"agg": "count", "field": "c.name", "as": "n"},
				],
			}
		)["rows"]
		self.assertEqual(rows[0]["total"], 60)  # 10 + 20 + 30 (A1 only)
		self.assertEqual(rows[0]["n"], 3)

	def test_scope_aggregate_admin_unscoped_totals(self):
		"""[M: apply the child branch to Administrator] Administrator is unscoped:
		SUM/COUNT span every child row across both parents."""
		frappe.set_user("Administrator")
		rows = query(
			{
				"from": self.SCOPE_CHILD_DT,
				"alias": "c",
				"select": [
					{"agg": "sum", "field": "c.scope_val", "as": "total"},
					{"agg": "count", "field": "c.name", "as": "n"},
				],
			}
		)["rows"]
		self.assertEqual(rows[0]["total"], 10937)  # 10+20+30+100+1000+2000 + the Single's 7777
		self.assertEqual(rows[0]["n"], 7)

	# ---- 4: ambiguity (multiple readable parents, no signal) ---------

	def test_scope_ambiguous_multi_readable_denied(self):
		frappe.set_user(self.USER_SCOPE_AB)
		with self.assertRaises(PermissionDeniedError) as cm:
			query({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.name"]})
		msg = str(cm.exception)
		self.assertIn(self.SCOPE_PARENT_A, msg)
		self.assertIn(self.SCOPE_PARENT_B, msg)
		self.assertIn("parenttype", msg)  # points at the disambiguation fix
		self.assertNotIn("<strong>", msg)  # no raw framework HTML

	# ---- 5: disambiguation returns correctly-scoped rows -------------

	def test_scope_disambiguate_join_parent_link(self):
		"""[M: disable signal detection] FROM A JOIN child ON c.parent=a.name pins
		the child to Parent A for the two-parent user."""
		frappe.set_user(self.USER_SCOPE_AB)
		vals = self._scope_vals(
			{
				"from": self.SCOPE_PARENT_A,
				"alias": "a",
				"joins": [
					{
						"type": "inner",
						"doctype": self.SCOPE_CHILD_DT,
						"alias": "c",
						"on": {"c.parent": "a.name"},
					}
				],
				"select": ["c.scope_val"],
			}
		)
		self.assertEqual(vals, [10, 20, 30, 100])  # all Parent A children
		self.assertNotIn(1000, vals)  # Parent B excluded by parenttype

	def test_scope_disambiguate_parenttype_filter(self):
		frappe.set_user(self.USER_SCOPE_AB)
		vals = self._scope_vals(
			{
				"from": self.SCOPE_CHILD_DT,
				"alias": "c",
				"where": [{"field": "c.parenttype", "op": "=", "value": self.SCOPE_PARENT_A}],
				"select": ["c.scope_val"],
			}
		)
		self.assertEqual(vals, [10, 20, 30, 100])

	def test_scope_disambiguate_reversed_on_orientation(self):
		frappe.set_user(self.USER_SCOPE_AB)
		vals = self._scope_vals(
			{
				"from": self.SCOPE_PARENT_A,
				"alias": "a",
				"joins": [
					{
						"type": "inner",
						"doctype": self.SCOPE_CHILD_DT,
						"alias": "c",
						"on": {"a.name": "c.parent"},
					}
				],
				"select": ["c.scope_val"],
			}
		)
		self.assertEqual(vals, [10, 20, 30, 100])

	# ---- 6: pinned to an unreadable parent -> denial (no fallback) ---

	def test_scope_pinned_unreadable_parent_denied(self):
		"""[M: fall back to a readable parent instead of denying] A signal pinning
		Parent B (which USER_SCOPE_A cannot read) must DENY, not silently fall
		back to the readable Parent A."""
		frappe.set_user(self.USER_SCOPE_A)
		with self.assertRaises(PermissionDeniedError) as cm:
			query(
				{
					"from": self.SCOPE_CHILD_DT,
					"alias": "c",
					"where": [{"field": "c.parenttype", "op": "=", "value": self.SCOPE_PARENT_B}],
					"select": ["c.name"],
				}
			)
		msg = str(cm.exception)
		self.assertIn(self.SCOPE_PARENT_B, msg)  # the pinned, unreadable parent
		self.assertIn(self.SCOPE_PARENT_A, msg)  # what they CAN read it through
		self.assertNotIn("<strong>", msg)

	# ---- 7: LEFT JOIN null-extension preserved -----------------------

	def test_scope_left_join_null_extension_preserved(self):
		"""[M: drop the name IS NULL guard] A visible parent with zero child rows
		(A3) still appears null-extended under a LEFT JOIN."""
		frappe.set_user(self.USER_SCOPE_A)
		rows = query(
			{
				"from": self.SCOPE_PARENT_A,
				"alias": "a",
				"joins": [
					{
						"type": "left",
						"doctype": self.SCOPE_CHILD_DT,
						"alias": "c",
						"on": {"c.parent": "a.name"},
					}
				],
				"select": ["a.name", "c.scope_val"],
				"limit": 100,
			}
		)["rows"]
		a3 = [r for r in rows if r["name"] == self.A3_NAME]
		self.assertEqual(len(a3), 1)  # A3 present as a null-extended row
		self.assertIsNone(a3[0]["scope_val"])
		self.assertNotIn(1000, [r["scope_val"] for r in rows])  # B1 still excluded

	# ---- 8: join-shape attack (ON links non-parent fields) -----------

	def test_scope_join_shape_attack_still_scoped(self):
		"""[M: skip the child branch] A JOIN whose ON links non-parent columns
		(c.child_public = a.public_field) cannot smuggle A2's or B1's 'collide'
		rows past the record gate — the child is scoped independently of the ON."""
		frappe.set_user(self.USER_SCOPE_A)
		vals = self._scope_vals(
			{
				"from": self.SCOPE_PARENT_A,
				"alias": "a",
				"joins": [
					{
						"type": "inner",
						"doctype": self.SCOPE_CHILD_DT,
						"alias": "c",
						"on": {"c.child_public": "a.public_field"},
					}
				],
				"select": ["c.scope_val"],
			}
		)
		self.assertIn(30, vals)  # A1's own 'collide' child is legitimately visible
		self.assertNotIn(100, vals)  # A2's 'collide' child (unreadable record)
		self.assertNotIn(1000, vals)  # B1's 'collide' child (wrong parenttype)

	# ---- 9: EXISTS sub-spec child scope ------------------------------

	def test_scope_exists_subspec_child_scoped(self):
		"""[M: skip the child branch at the EXISTS site] An EXISTS over the child
		correlated to the outer parent must not let a childless Parent A doc (A4)
		appear to exist via a same-named Parent B doc's child (B2)."""
		frappe.set_user(self.USER_SCOPE_A)
		names = sorted(
			r["name"]
			for r in query(
				{
					"from": self.SCOPE_PARENT_A,
					"alias": "a",
					"select": ["a.name"],
					"where": [
						{
							"op": "exists",
							"value": {
								"from": self.SCOPE_CHILD_DT,
								"alias": "c",
								"where": [{"field": "c.parent", "op": "=", "value": {"$field": "a.name"}}],
							},
						}
					],
					"limit": 100,
				}
			)["rows"]
		)
		self.assertIn(self.COLLIDE_NAME, names)  # A1 genuinely has children
		self.assertNotIn(self.SHARED_NAME, names)  # A4 must NOT exist via B2's child

	# ---- 10: zero readable parents (step-3 parent-oriented message) --

	def test_scope_zero_readable_parents_denied(self):
		frappe.set_user(self.USER_SCOPE_NONE)
		with self.assertRaises(PermissionDeniedError) as cm:
			query({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.name"]})
		msg = str(cm.exception)
		self.assertIn(self.SCOPE_CHILD_DT, msg)
		self.assertIn("parent", msg.lower())
		self.assertIn(self.SCOPE_PARENT_A, msg)  # names the owning parents
		self.assertNotIn("<strong>", msg)

	# ---- 11: clean errors (no raw framework HTML for child queries) --

	def test_scope_child_denials_are_clean_no_html(self):
		frappe.set_user(self.USER_SCOPE_AB)
		with self.assertRaises(PermissionDeniedError) as cm:
			query({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.name"]})
		msg = str(cm.exception)
		self.assertNotIn("Insufficient Permission", msg)  # raw framework class gone
		self.assertNotIn("<strong>", msg)
		self.assertNotIn("</", msg)

	# ---- 12: field-ACL denial names the governing parent -------------

	def test_scope_child_permlevel_field_denial_names_parent(self):
		frappe.set_user(self.USER_SCOPE_A)
		with self.assertRaises(PermissionDeniedError) as cm:
			query({"from": self.SCOPE_CHILD_DT, "alias": "c", "select": ["c.child_restricted"]})
		msg = str(cm.exception)
		self.assertIn("child_restricted", msg)
		self.assertIn("permission level", msg)
		self.assertIn(self.SCOPE_PARENT_A, msg)  # the parent that governs the permlevel
