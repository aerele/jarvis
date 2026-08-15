"""PatternDB: the SELECT-only facade detectors run through (plan section 5.4).

Fence layer 1 of 3 (facade + READ ONLY transaction here; static grep +
monkeypatch tests in CI; table-diff integration test in Wave B). Detectors
receive ONLY a PatternDB instance - never raw frappe.db - and the engine
runs them inside :func:`read_only_transaction`, so even a validation bypass
hits the database's own read-only wall: MariaDB rejects writes in a
``START TRANSACTION READ ONLY`` transaction with errno 1792, which frappe
surfaces as ``frappe.InReadOnlyMode`` (verified on this bench).

Statement-level validation is deliberately conservative: static curated
detector SQL never needs ';', 'INTO', 'FOR UPDATE' or 'LOCK IN SHARE MODE',
so any occurrence ANYWHERE in the text is rejected, string literals
included. False positives cost a detector skip; false negatives cost the
fence.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator

import frappe

from jarvis.exceptions import JarvisError
from jarvis.learning.compat import db_backend, statement_timeout_prefix

DEFAULT_TIMEOUT_S = 30  # mirrors the plan's DETECTOR_SQL_TIMEOUT_S

# Word-bounded so e.g. a column named "turnover_into_q2" would still trip -
# acceptable: curated SQL simply avoids these tokens.
_FORBIDDEN_RE = re.compile(
	r"\binto\b|\bfor\s+update\b|\block\s+in\s+share\s+mode\b",
	re.IGNORECASE,
)


class PatternDBViolation(JarvisError):
	"""A query was refused by the SELECT-only fence."""


class PatternDB:
	"""SELECT-only facade over frappe.db for pattern detectors."""

	def sql_select(self, query: str, params=None, as_dict: bool = True):
		"""Validate and execute a single SELECT statement.

		Rejects (raising PatternDBViolation): non-SELECT statements after
		stripping leading comments/whitespace; any ';' anywhere; any
		INTO / FOR UPDATE / LOCK IN SHARE MODE token anywhere.
		"""
		validate_select(query)
		return frappe.db.sql(query, params or (), as_dict=as_dict)

	def timed_select(self, query: str, params=None, timeout_s: int = DEFAULT_TIMEOUT_S, as_dict: bool = True):
		"""sql_select with a per-statement kill on MariaDB.

		The inner query is validated FIRST, then wrapped in
		``SET STATEMENT max_statement_time=<seconds> FOR <query>`` (the
		MariaDB idiom; MySQL's max_execution_time does not exist there).
		On Postgres the query runs as-is and relies on the session
		statement_timeout the engine sets via
		compat.set_session_statement_timeout. A killed statement raises
		OperationalError 1969 (max_statement_time exceeded), which the
		engine records as a detector skip.
		"""
		validate_select(query)
		if db_backend() == "mariadb":
			query = statement_timeout_prefix(query, timeout_s)
		return frappe.db.sql(query, params or (), as_dict=as_dict)


@contextlib.contextmanager
def read_only_transaction() -> Iterator[PatternDB]:
	"""Run detector reads inside a database-enforced READ ONLY transaction.

	MariaDB: ``START TRANSACTION READ ONLY`` on entry, ``ROLLBACK`` on exit.
	Any write on the connection fails at the DB (errno 1792). Entry with
	uncommitted writes pending raises frappe's ImplicitCommitError rather
	than silently committing them - the engine must commit or roll back
	before opening the fence.

	Postgres: ``SET TRANSACTION READ ONLY`` is attempted (valid only before
	the current transaction's first statement); if it fails, containment is
	facade-only for that pass and MariaDB (the primary backend) keeps the
	hard guarantee.

	Yields a PatternDB so callers hand detectors exactly one object.
	"""
	backend = db_backend()
	entered = False
	if backend == "mariadb":
		frappe.db.sql("START TRANSACTION READ ONLY")
		entered = True
	elif backend == "postgres":
		try:
			frappe.db.sql("SET TRANSACTION READ ONLY")
			entered = True
		except Exception:
			entered = False
	try:
		yield PatternDB()
	finally:
		if entered:
			try:
				frappe.db.sql("ROLLBACK")
			except Exception:
				pass


def validate_select(query: str) -> None:
	"""Raise PatternDBViolation unless ``query`` is a single bare SELECT."""
	if not query or not isinstance(query, str):
		raise PatternDBViolation("empty or non-string query")
	if ";" in query:
		raise PatternDBViolation("';' is not allowed anywhere in a detector query")
	if _FORBIDDEN_RE.search(query):
		raise PatternDBViolation("forbidden token in detector query (INTO / FOR UPDATE / LOCK IN SHARE MODE)")
	stripped = _strip_leading_comments(query)
	if not stripped[:6].upper() == "SELECT":
		raise PatternDBViolation("detector queries must be a single SELECT statement")


def _strip_leading_comments(query: str) -> str:
	"""Drop leading whitespace and -- / # / block comments so a comment
	prefix cannot hide the real statement from the SELECT check."""
	q = query
	while True:
		q = q.lstrip()
		if q.startswith("--") or q.startswith("#"):
			newline = q.find("\n")
			if newline == -1:
				return ""
			q = q[newline + 1 :]
			continue
		if q.startswith("/*"):
			end = q.find("*/")
			if end == -1:
				return ""
			q = q[end + 2 :]
			continue
		return q
