"""MariaDB boundary: stream, time limits, cancel our query, detect lock waiters.

Both connections are dedicated to this attempt. Never reuse a connection ID or
close a live SSCursor (PyMySQL drains it). Close its socket first on all exits.
"""

import contextlib
import threading
import time

import pymysql

from jarvis.tools._export.clock import now_datetime
from jarvis.tools._export.streaming import ExportLimit


def connection_settings():
	"""Use the PyMySQL adapter even when the bench's main driver is mysqlclient."""
	import frappe
	from frappe.database.mariadb.database import MariaDBConnectionUtil, MariaDBDatabase

	settings = MariaDBConnectionUtil.get_connection_settings(frappe.db)
	settings["conv"] = MariaDBDatabase.CONVERSION_MAP
	return settings


class MariaDBStream:
	def __init__(self, settings, seconds=600):
		self.settings = {
			**settings,
			"read_timeout": seconds + 15,
			"write_timeout": 15,
			"connect_timeout": 10,
			"autocommit": True,
		}
		self.seconds = seconds
		self.connection = None
		self.control = None
		self.query_id = None

	def open(self):
		self.connection = pymysql.connect(**self.settings)
		self.control = pymysql.connect(**{**self.settings, "read_timeout": 10})
		self.query_id = self.connection.thread_id()
		self.set_time_limit()
		return self

	def set_time_limit(self):
		with self.connection.cursor() as cursor:
			cursor.execute("SET SESSION net_write_timeout=%s", (self.seconds,))
			cursor.execute("SET SESSION max_statement_time=%s", (self.seconds,))

	def stream_rows(self, sql, params):
		cursor = self.connection.cursor(pymysql.cursors.SSDictCursor)
		cursor.execute(sql, params)
		return iter(cursor)

	def cancel_own_query(self):
		if self.query_id is not None and self.connection.open:
			with self.control.cursor() as cursor:
				cursor.execute("KILL QUERY %s", (self.query_id,))

	def detect_lock_waiters(self):
		with self.control.cursor() as cursor:
			cursor.execute(
				"SELECT 1 FROM information_schema.PROCESSLIST WHERE DB=DATABASE() "
				"AND (STATE LIKE 'Waiting for%table%lock%' OR STATE LIKE 'Waiting for%metadata%lock%') LIMIT 1"
			)
			return bool(cursor.fetchone())

	def close(self):
		# Connection.close closes the socket directly; cursor.close would drain rows.
		for connection in (self.connection, self.control):
			if connection:
				with contextlib.suppress(Exception):
					connection.close()


class Watchdog:
	def __init__(self, db, job, token, site, owner, conversation, seconds=600):
		self.db, self.job, self.token = db, job, token
		self.site, self.owner, self.conversation = site, owner, conversation
		self.started = time.monotonic()
		self.seconds = seconds
		self.rows = 0
		self.reason = None
		self.stop_event = threading.Event()
		self.thread = threading.Thread(target=self._run, daemon=True, name="jarvis-export-watchdog")

	def progress(self, rows):
		self.rows = rows

	def check(self):
		if self.reason:
			raise self.reason
		if time.monotonic() - self.started >= self.seconds:
			raise ExportLimit("too slow", "The export exceeded ten minutes. Narrow or split the filters.")

	def start(self):
		self.thread.start()

	def stop(self):
		self.stop_event.set()
		self.thread.join(timeout=15)
		if self.thread.is_alive():
			raise RuntimeError("Export watchdog did not stop")

	def _tick(self):
		with self.db.control.cursor() as cursor:
			cursor.execute(
				"SELECT cancel_requested FROM `tabJarvis Export Job` WHERE name=%s "
				"AND attempt_token=%s AND status='running'",
				(self.job, self.token),
			)
			row = cursor.fetchone()
			if not row or row[0]:
				raise ExportLimit("cancelled", "Export cancelled.")
			cursor.execute(
				"UPDATE `tabJarvis Export Job` SET heartbeat=%s, rows_written=%s "
				"WHERE name=%s AND attempt_token=%s AND status='running'",
				(now_datetime(), self.rows, self.job, self.token),
			)
		self.check()
		if self.db.detect_lock_waiters():
			raise ExportLimit("paused", "Paused for a site update; retrying in about five minutes.")

	def _run(self):
		import frappe

		from jarvis.chat.events import publish_to_user

		try:
			# Thread-local site context for Redis realtime only. DB writes use control.
			try:
				frappe.init(site=self.site)
			except Exception:
				self.reason = ExportLimit(
					"failed", "The export could not start its safety monitor. Please run it again."
				)
			while not self.stop_event.is_set():
				try:
					self.check()
					self._tick()
					with contextlib.suppress(Exception):
						publish_to_user(
							self.owner,
							{
								"kind": "export:update",
								"conversation_id": self.conversation,
								"export_job": self.job,
								"rows_written": self.rows,
							},
						)
				except Exception as exc:
					self.reason = (
						exc
						if isinstance(exc, ExportLimit)
						else ExportLimit("failed", "The export lost its safety monitor. Please run it again.")
					)
					with contextlib.suppress(Exception):
						self.db.cancel_own_query()
					# Cancellation may race the SELECT starting. Keep cancelling our
					# dedicated connection until the main thread acknowledges the stop.
				self.stop_event.wait(5)
		finally:
			frappe.destroy()
