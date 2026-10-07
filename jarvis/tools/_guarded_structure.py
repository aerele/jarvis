"""Guarded structure writes: the few database-structure changes the assistant may
make from chat, each only through a confirmation card (owner decision R2-10; plan
``2026-10-03-round-2-confirm-flow-crm-drift.md`` rev 5, unit J1b-cf).

A structure-changing save is never atomic (aerele/jarvis#1635): Frappe commits the
document row, THEN runs ``ALTER TABLE``, so a failed ALTER leaves a field row with
no column and the form cannot be listed, saved or opened by anyone. So these writes
get no trial run and no retry. What they get instead, shared by every guarded class
(this unit: one new Custom Field and a column-free Custom Field edit, in
``_custom_field_guard``; J1c adds Workflow and the two settings by registering a
handler in ``HANDLERS``):

- checks at park that refuse what can be known to fail, naming the field;
- a schema diff (``schema_changes``): what Frappe's own ``DBTable.alter`` WOULD run
  for the table, recorded without running it. The write is refused unless that is
  exactly the one change the card shows (or nothing, for an edit), so pending drift
  on the table (a missing sort-field index, a leftover unique key) never rides in on
  a chat confirmation;
- a per-doctype lock (``structure_lock``, a database lock on its own connection)
  held across the whole confirm, clean-up included: taken before the row is
  claimed, so a second confirm on the same form is told to try again and its card
  stays live;
- the checks again under that lock (the table may have changed since the park);
- a short ``lock_wait_timeout`` for the ALTER, reset afterwards: on a busy table the
  ALTER gives up in seconds instead of piling every request up behind it;
- a clean-up when the write fails after its row committed, limited to the row THAT
  confirmation wrote (``stamp_written``: known exactly, not by owner or time), and
  the same clean-up from the reconciler (under the same lock) when the worker died
  mid-way or the clean-up itself failed;
- the site-config off switch ``jarvis_structure_writes_disabled``.

Everything else about database structure stays refused (``_write_risk``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import frappe

from jarvis.exceptions import JarvisError, RetryLaterError

OFF_SWITCH = "jarvis_structure_writes_disabled"
STRUCTURAL_LINE = "Confirming changes the database structure for every user and cannot be undone."

# How long the ALTER may wait for the table's metadata lock (MariaDB default: a day).
LOCK_WAIT_TIMEOUT_S = 5

# MariaDB's row limit (error 1118), and how close to it a table may get from chat.
# Measured on MariaDB 11.4: the sum of every column's bytes plus one bit per
# nullable column must stay within 65,535.
ROW_SIZE_LIMIT = 65535
ROW_SIZE_MARGIN = 1024

# Risk class (``_write_risk.risk_of``) -> its handler class, as a dotted path. A
# handler is built from the call's arguments and offers ``lock_doctype``,
# ``check()``, ``risk_lines()``, ``snapshot()`` and the static ``clean_up(undo)``.
HANDLERS: dict[str, str] = {
	"custom_field_new": "jarvis.tools._custom_field_guard.NewCustomField",
	"custom_field_edit": "jarvis.tools._custom_field_guard.CustomFieldEdit",
}

UNDO_COLUMN = "sealed_undo"
_LOCAL_HELD = "jarvis_structure_locks"
_PA = "Jarvis Pending Action"


class StructureBusyError(RetryLaterError):
	"""Another structure change to the same form is running (its lock is held), or
	the lock could not be taken at all. Nothing was changed."""


class StructureChangeFailedError(JarvisError):
	"""The database refused the structure change itself (the ALTER). Never fixable
	by the request: the envelope says what happened and what was cleaned up."""


@dataclass
class CleanUp:
	"""What a clean-up found. ``clean``: nothing this confirmation made is left."""

	text: str
	clean: bool = True
	changed: bool = False


# --------------------------------------------------------------------------- #
# Switches
# --------------------------------------------------------------------------- #
def disabled() -> bool:
	"""The site-config off switch: every guarded structure write is refused like
	any other structure change.

	Read FRESH from the files, at park and again at Confirm, not only from
	``frappe.conf``: Frappe 16 keeps site_config.json per web worker for 60 seconds
	(frappe/config.py :149-150 ``site_cache(ttl=60)``; frappe/__init__.py :200 passes
	``cached=is_request``), so a worker that was already running kept parking and
	confirming cards for up to a minute after an operator set the switch. (Frappe 15
	reads the file on every request, frappe/__init__.py :239; the same read is done
	there so both behave alike.) Either source saying "off" is off, and a config
	file that cannot be read is off too: this switch fails closed."""
	if frappe.utils.cint(frappe.conf.get(OFF_SWITCH)):
		return True
	try:
		sites_path = getattr(frappe.local, "sites_path", None) or "."
		common = _read_json(os.path.join(sites_path, "common_site_config.json"), missing_ok=True)
		site = _read_json(os.path.join(frappe.local.site_path, "site_config.json"))
	except Exception:
		return True
	return bool(frappe.utils.cint({**common, **site}.get(OFF_SWITCH)))


def _read_json(path: str, *, missing_ok: bool = False) -> dict:
	if missing_ok and not os.path.exists(path):
		return {}
	with open(path) as f:
		data = json.load(f)
	if not isinstance(data, dict):
		raise ValueError(f"{path} is not a JSON object")
	return data


def undo_ready() -> bool:
	"""The confirmation row can hold a before-image (False between a deploy and its
	migrate). Cached per request."""
	ready = getattr(frappe.local, "jarvis_structure_undo_ready", None)
	if ready is None:
		try:
			ready = bool(frappe.get_meta(_PA).has_field(UNDO_COLUMN))
		except Exception:
			ready = False
		frappe.local.jarvis_structure_undo_ready = ready
	return ready


def available() -> bool:
	"""Whether guarded structure writes can run on this site at all: not switched
	off, MariaDB (the schema diff and the lock wait are MariaDB's), and migrated."""
	return not disabled() and frappe.db.db_type == "mariadb" and undo_ready()


def handler_for(risk: str | None, args):
	"""The handler of a guarded call, or None for any other risk."""
	path = HANDLERS.get(risk or "")
	return frappe.get_attr(path)(args if isinstance(args, dict) else {}) if path else None


def clean_up(undo: dict) -> CleanUp:
	"""Run the clean-up a stored ``undo`` names (``handler.snapshot()``, plus what
	``stamp_written`` added)."""
	path = HANDLERS.get(str((undo or {}).get("risk") or ""))
	if not path:
		return CleanUp("")
	return frappe.get_attr(path).clean_up(undo)


# --------------------------------------------------------------------------- #
# What this confirmation wrote, exactly
# --------------------------------------------------------------------------- #
_LOCAL_STAMP = "jarvis_structure_stamp"


@contextmanager
def stamping(pa_name: str, undo: dict) -> Iterator[None]:
	"""While the confirmed call runs: the guarded record's save is stamped on the
	confirmation row ``pa_name`` (``stamp_written``)."""
	prev = getattr(frappe.local, _LOCAL_STAMP, None)
	setattr(frappe.local, _LOCAL_STAMP, (pa_name, undo))
	try:
		yield
	finally:
		setattr(frappe.local, _LOCAL_STAMP, prev)


def stamp_written(doc) -> None:
	"""Called by the ORM guard when it admits the card's own record, before the row
	is written: seal that record's name and the timestamp of THIS save
	(``modified``, set before validate) into the confirmation row's clean-up state.

	Same transaction as the record itself, so the two commit together (Frappe's
	``sql_ddl`` commit before the ALTER) or not at all. The clean-up then knows the
	row it wrote exactly: no owner (an approver may confirm), no clock comparison,
	and nothing Frappe normalises on save can make its own change look foreign."""
	target = getattr(frappe.local, _LOCAL_STAMP, None)
	if not target:
		return
	from jarvis.chat.pending_actions import _seal

	pa_name, undo = target
	undo["written"] = {"name": doc.name, "modified": str(doc.modified)}
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET sealed_undo=%(u)s WHERE name=%(n)s AND status='Executing'",
		{"n": pa_name, "u": _seal.seal_undo(pa_name, undo)},
	)
	if _stamp_rows() != 1:
		# The row was not there to stamp (no longer Executing): the confirm still holds
		# what it wrote in memory and cleans up from that, but can no longer PROVE
		# what is on the row, so its outcome is never reported as clean.
		undo["unstamped"] = True


def _stamp_rows() -> int:
	from jarvis.chat.pending_actions._store import rowcount

	return rowcount()


# --------------------------------------------------------------------------- #
# The per-doctype lock
# --------------------------------------------------------------------------- #
def _lock_name(doctype: str) -> str:
	"""The lock's name on the database server: one per site and form, within
	MariaDB's 64 characters whatever the names are (so "Item" and "Sales Invoice
	Item" never share or shadow one)."""
	digest = hashlib.sha256(f"{frappe.db.cur_db_name}\x00{doctype}".encode()).hexdigest()[:40]
	return f"jarvis_structure_{digest}"


def _held() -> dict:
	"""This request's locks: doctype -> the connection that holds it."""
	held = getattr(frappe.local, _LOCAL_HELD, None)
	if held is None:
		held = {}
		setattr(frappe.local, _LOCAL_HELD, held)
	return held


@contextmanager
def structure_lock(doctype: str) -> Iterator[None]:
	"""Hold the structure lock of ``doctype``. Raises ``StructureBusyError`` when it
	is held (or cannot be asked for): the caller changes nothing and says try again.

	A MariaDB ``GET_LOCK`` on a connection of its own, never waited for. Not a cache
	key: saving a Custom Field clears the form's cache just before its ALTER, and on
	Frappe 16 that deletes every cache key containing the DocType's name
	(cache_manager.py :149), which dropped a Redis lock in the middle of the confirm
	it guarded. A database lock belongs to its session, so no commit, rollback or
	cache clear releases it, the DDL's implicit commit on the request's own
	connection does not touch it, and it is gone the moment a worker dies (its
	connection closes): nothing to expire, no stale lock for the reconciler to wait
	out."""
	name = _lock_name(doctype)
	conn = None
	won = False
	try:
		conn = frappe.db.get_connection()
		cursor = conn.cursor()
		cursor.execute("SELECT GET_LOCK(%s, 0)", (name,))
		won = (cursor.fetchone() or (0,))[0] == 1
	except Exception:
		won = False
	if not won:
		_close(conn)
		raise StructureBusyError(
			f"The structure lock of {doctype} could not be taken (another structure change to it is "
			"running, or the database could not be asked). Nothing was changed. Try again in a moment."
		)
	held = _held()
	held[doctype] = conn
	try:
		yield
	finally:
		held.pop(doctype, None)
		try:
			conn.cursor().execute("SELECT RELEASE_LOCK(%s)", (name,))
		except Exception:
			pass  # closing the connection releases it too
		_close(conn)


def _close(conn) -> None:
	try:
		if conn is not None:
			conn.close()
	except Exception:
		pass


def lock_held(doctype: str) -> bool:
	"""Whether THIS request holds the structure lock of ``doctype``, as the database
	server sees it: the lock's holder must be the connection that took it. A
	connection the server dropped (``wait_timeout``, a DBA's KILL, a failover) took
	the lock with it, and a local flag would never know."""
	conn = _held().get(doctype)
	if conn is None:
		return False
	try:
		holder = frappe.db.sql("SELECT IS_USED_LOCK(%s)", (_lock_name(doctype),))[0][0]
		return holder is not None and int(holder) == int(conn.thread_id())
	except Exception:
		return False


@contextmanager
def short_lock_wait() -> Iterator[None]:
	"""``lock_wait_timeout`` of a few seconds for the ALTER, put back afterwards (the
	connection outlives this call inside the request)."""
	previous = frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0]
	frappe.db.sql(f"SET SESSION lock_wait_timeout = {int(LOCK_WAIT_TIMEOUT_S)}")
	try:
		yield
	finally:
		try:
			frappe.db.sql(f"SET SESSION lock_wait_timeout = {int(previous)}")
		except Exception:
			pass


# --------------------------------------------------------------------------- #
# The real table, never a cache
# --------------------------------------------------------------------------- #
def table_columns(doctype: str) -> dict[str, frappe._dict]:
	"""The table's columns straight from ``information_schema``, keyed by
	lower-cased name ({} when there is no table). ``frappe.db.has_column`` reads a
	cache that a half-made change leaves stale."""
	rows = frappe.db.sql(
		"SELECT column_name AS name, column_type AS column_type, data_type AS data_type,"
		" character_octet_length AS octets, is_nullable AS is_nullable"
		" FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = %s",
		(f"tab{doctype}",),
		as_dict=True,
	)
	return {str(r.name).lower(): r for r in rows}


_TEXT_BYTES = {
	"tinytext": 9,
	"tinyblob": 9,
	"text": 10,
	"blob": 10,
	"mediumtext": 11,
	"mediumblob": 11,
	"longtext": 12,
	"longblob": 12,
	"json": 12,
}
_FIXED_BYTES = {
	"tinyint": 1,
	"bool": 1,
	"boolean": 1,
	"smallint": 2,
	"mediumint": 3,
	"int": 4,
	"integer": 4,
	"bigint": 8,
	"float": 4,
	"double": 8,
	"date": 3,
	"year": 1,
	"uuid": 16,
	"inet6": 16,
}
# Bytes for the digits left over after whole groups of nine (a decimal column).
_DECIMAL_LEFTOVER = (0, 1, 1, 2, 2, 3, 3, 4, 4)
_COLUMN_TYPE = re.compile(r"^\s*([a-z0-9]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?", re.IGNORECASE)
# What a type this module does not know is counted as, when the database gives no size.
_UNKNOWN_BYTES = 16


def column_bytes(column_type: str, octets: int | None = None) -> int:
	"""Bytes one column adds to MariaDB's row-size count. ``column_type`` as the
	database or Frappe writes it (``varchar(140)``, ``decimal(21,9)``, ``datetime(6)``,
	``longtext``); ``octets`` is the database's own byte length of a character column
	(else four bytes a character, utf8mb4)."""
	m = _COLUMN_TYPE.match(column_type or "")
	if not m:
		return _UNKNOWN_BYTES
	kind = m.group(1).lower()
	first = int(m.group(2)) if m.group(2) else None
	second = int(m.group(3)) if m.group(3) else 0
	if kind in _TEXT_BYTES:
		return _TEXT_BYTES[kind]
	if kind in ("varchar", "varbinary"):
		size = int(octets) if octets else (first or 0) * (4 if kind == "varchar" else 1)
		return size + (1 if size <= 255 else 2)
	if kind in ("char", "binary"):
		return int(octets) if octets else (first or 1) * (4 if kind == "char" else 1)
	if kind in ("decimal", "numeric"):
		digits = first or 10
		whole, fraction = digits - second, second
		return (
			4 * (whole // 9)
			+ _DECIMAL_LEFTOVER[whole % 9]
			+ 4 * (fraction // 9)
			+ _DECIMAL_LEFTOVER[fraction % 9]
		)
	if kind in ("datetime", "timestamp", "time"):
		base = {"datetime": 5, "timestamp": 4, "time": 3}[kind]
		return base + math.ceil((first or 0) / 2)
	if kind == "bit":
		return ((first or 1) + 7) // 8
	if kind in _FIXED_BYTES:
		return _FIXED_BYTES[kind]
	return int(octets) if octets else _UNKNOWN_BYTES


def row_size(columns: dict) -> int:
	"""MariaDB's row-size count of a table (``table_columns``): every column's bytes
	plus one bit per nullable column."""
	nullable = sum(1 for c in columns.values() if str(c.is_nullable).upper() == "YES")
	return sum(column_bytes(c.column_type, c.octets) for c in columns.values()) + math.ceil(nullable / 8)


def room_for(columns: dict, definition: str) -> bool:
	"""Whether a new nullable column of ``definition`` fits the table with
	``ROW_SIZE_MARGIN`` to spare."""
	added = column_bytes(definition) + 1  # at most one more byte of null bits
	return row_size(columns) + added <= ROW_SIZE_LIMIT - ROW_SIZE_MARGIN


# --------------------------------------------------------------------------- #
# The schema diff
# --------------------------------------------------------------------------- #
def schema_changes(doctype: str, meta) -> tuple[list[str], object]:
	"""``(statements, table)``: every DDL statement ``frappe.db.updatedb(doctype)``
	would run if ``meta`` were the form's fields, in order, WITHOUT running any, and
	the ``DBTable`` that produced them (its columns give the expected definition).

	There is no Frappe API for this, so Frappe's own diff is run with
	``frappe.db.sql_ddl`` replaced by a recorder: every ALTER of ``DBTable.alter``
	goes through it (frappe/database/mariadb/schema.py, Frappe 15 :111, Frappe 16
	``run_alter`` :173). ``validate()`` first, as ``updatedb`` does: it loads the
	table's current columns, without which every column reads as new. All of it
	inside a savepoint that is rolled back, because Frappe 16's ``alter`` runs a real
	UPDATE for a nullability change before any DDL (:149-158). Raises whatever
	Frappe's own checks raise (a fieldname over 64 characters, a length out of range,
	a column name with special characters)."""
	from frappe.database.mariadb.schema import MariaDBTable

	db = frappe.db
	recorded: list[str] = []
	had_own = "sql_ddl" in db.__dict__
	real = db.sql_ddl
	log = getattr(frappe.local, "message_log", None)
	mark = len(log) if log else 0
	savepoint = f"jarvis_diff_{frappe.generate_hash(length=10)}"
	db.savepoint(savepoint)
	db.sql_ddl = lambda query, debug=False: recorded.append(" ".join(str(query).split()))
	try:
		table = MariaDBTable(doctype, meta)
		table.validate()
		table.alter()
	finally:
		if had_own:
			db.sql_ddl = real
		else:
			del db.sql_ddl
		db.rollback(save_point=savepoint)
		log = getattr(frappe.local, "message_log", None)
		if log and len(log) > mark:
			del log[mark:]  # validate()'s own msgprint is not for this request
	return recorded, table


# --------------------------------------------------------------------------- #
# Failures of the change itself
# --------------------------------------------------------------------------- #
_ROW_TOO_LARGE = 1118


def database_refusal(e: BaseException) -> StructureChangeFailedError | None:
	"""The row-size refusal of the ALTER (MariaDB 1118) as a plain-words failure,
	else None. Every other database error stays a crash, with its Error Log row: it
	may come from anywhere in the save (another app's hook), not from the ALTER.

	1118 has two variants. The row limit (65,535 bytes, "not counting BLOBs") is
	what ``room_for`` estimates; a text field still fits there. The InnoDB page
	limit ("is 8126") counts text columns too and is not estimated (it depends on
	the row format and on which columns are stored off the page), so it is only
	met here; no field of any type fits until some are removed."""
	if type(e).__module__.split(".")[0] not in ("pymysql", "MySQLdb"):
		return None
	args = getattr(e, "args", None) or ()
	if not args or args[0] != _ROW_TOO_LARGE:
		return None
	if "8126" in str(args[1] if len(args) > 1 else ""):
		return StructureChangeFailedError(
			"The database refused the change: the form's table has no room for another field of any "
			"type (row size limit of a database page). Fields have to be removed in Desk first."
		)
	return StructureChangeFailedError(
		"The database refused the change: the form's table has no room for another field of "
		"this type (row size limit). Use a Small Text or Long Text field, or free up fields in Desk."
	)
