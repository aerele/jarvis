"""Version + DB-backend shims for pattern learning (plan section 4.5).

Capability probes only: feature logic must never dispatch on version
strings. :func:`frappe_major` exists for logging/telemetry and the v15
release-gate smoke test, not for behavior branches - probe with
:func:`has_field` / ``frappe.get_installed_apps`` instead.

DB portability facts this module encodes (verified against MariaDB
10.11 on this bench):

* MariaDB per-statement kill is ``SET STATEMENT max_statement_time=<SECONDS>
  FOR <query>``. MySQL's ``max_execution_time`` (milliseconds) does NOT
  exist on MariaDB.
* Postgres has no per-statement prefix; the engine sets a session-level
  ``statement_timeout`` (milliseconds) around detector execution instead.
"""

from __future__ import annotations

import re

import frappe

# meta.has_field only knows DocField rows; these live on every table but
# never appear in meta - probe them via has_column (plan section 4.3).
STANDARD_COLUMNS = ("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx")


def frappe_major() -> int:
	"""First integer of frappe.__version__ ('16.25.0' -> 16); 0 if unparsable."""
	m = re.match(r"(\d+)", frappe.__version__ or "")
	return int(m.group(1)) if m else 0


def db_backend() -> str:
	"""'mariadb' / 'postgres' / 'sqlite' for the active site connection."""
	return getattr(frappe.db, "db_type", None) or frappe.conf.db_type or "mariadb"


def has_field(doctype: str, fieldname: str) -> bool:
	"""Field-guard probe: ``meta.has_field`` first, ``db.has_column`` fallback.

	The fallback covers the standard columns (owner, creation, modified,
	docstatus, name, ...) that meta does not list, and doubles as the
	truth source when meta and schema disagree. Fails closed: an unknown
	doctype or missing table reads as False, so the detector is skipped
	and the skip recorded, never crashed.
	"""
	try:
		if frappe.get_meta(doctype).has_field(fieldname):
			return True
	except Exception:
		return False
	try:
		return bool(frappe.db.has_column(doctype, fieldname))
	except Exception:
		return False


def statement_timeout_prefix(query: str, timeout_s: int) -> str:
	"""Wrap a (pre-validated) SELECT in the MariaDB per-statement kill.

	max_statement_time is in SECONDS on MariaDB. Callers must validate the
	inner query BEFORE prefixing (the PatternDB fence asserts on the inner
	statement, not on this wrapper).
	"""
	return f"SET STATEMENT max_statement_time={int(timeout_s)} FOR {query}"


def set_session_statement_timeout(timeout_s: int) -> None:
	"""Postgres: session statement_timeout in ms. No-op on MariaDB (which
	uses the per-statement prefix instead)."""
	if db_backend() == "postgres":
		frappe.db.sql("SET statement_timeout TO %s", (int(timeout_s) * 1000,))


def reset_session_statement_timeout() -> None:
	"""Postgres: restore the session statement_timeout. No-op on MariaDB."""
	if db_backend() == "postgres":
		frappe.db.sql("SET statement_timeout TO DEFAULT")
