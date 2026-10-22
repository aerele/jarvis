"""Fresh, read-only snapshots without ending a caller's unrelated transaction."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import frappe

_reading = ContextVar("collaboration_read_snapshot", default=False)


@contextmanager
def read_snapshot(*, timeout=5):
	if _reading.get():
		yield
		return
	from frappe.database import get_db

	producer = frappe.local.db
	db = get_db(
		socket=producer.socket,
		host=producer.host,
		port=producer.port,
		user=producer.user,
		password=producer.password,
		cur_db_name=producer.cur_db_name,
	)
	connection_settings = db.get_connection_settings
	db.get_connection_settings = lambda: {
		**connection_settings(),
		"connect_timeout": 2,
		"read_timeout": timeout + 1,
		"write_timeout": timeout + 1,
	}
	token = _reading.set(True)
	try:
		db.connect()
		frappe.local.db = db
		db.set_execution_timeout(timeout)
		db.begin(read_only=True)
		yield
	finally:
		try:
			if db._conn:
				db.rollback()
		finally:
			try:
				db.close()
			finally:
				frappe.local.db = producer
				_reading.reset(token)


def fresh_read(fn):
	@wraps(fn)
	def read(*args, **kwargs):
		with read_snapshot():
			return fn(*args, **kwargs)

	return read
