import logging
import unittest.mock as m

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import activity_log
from jarvis.tools import _agent_run_ctx, _private_output

DT = "Jarvis Activity Event"
_SECRET = "SECRET-TOOL-ARG-VALUE-9f3a"


def _names():
	return set(frappe.get_all(DT, pluck="name"))


def _fetch(name):
	return frappe.db.get_value(DT, name, "*", as_dict=True)


class _FakeRequest:
	def __init__(self, headers):
		self.headers = headers


class _ReqCtx:
	"""Bind a fake ``frappe.request`` carrying the given headers."""

	def __init__(self, **headers):
		self.headers = headers

	def __enter__(self):
		self.had = hasattr(frappe.local, "request")
		self.prev = getattr(frappe.local, "request", None)
		frappe.local.request = _FakeRequest(self.headers)

	def __exit__(self, *exc):
		if self.had:
			frappe.local.request = self.prev
		else:
			del frappe.local.request


class TestRecordValidation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		activity_log._reset_failure_log()  # identical failure lines are de-duplicated per process
		self.addCleanup(activity_log._reset_failure_log)

	def test_happy_path_stores_the_metadata(self):
		name = activity_log.record(
			"read",
			actor="Administrator",
			method="get_list",
			label="Read records",
			ref_doctype="ToDo",
			ref_name="TD-1",
			row_count=7,
			outcome="ok",
			channel="desktop",
			code="",
		)
		self.assertTrue(name)
		row = _fetch(name)
		self.assertEqual(
			(row.event, row.actor, row.method, row.ref_doctype, row.ref_name, row.row_count, row.channel),
			("read", "Administrator", "get_list", "ToDo", "TD-1", 7, "desktop"),
		)
		self.assertEqual(row.outcome, "ok")
		self.assertTrue(row.at)
		self.assertTrue(row.actor_name)  # resolved from the User, never blank for a real user

	def test_actor_name_comes_from_the_user_full_name(self):
		full = frappe.db.get_value("User", "Administrator", "full_name")
		name = activity_log.record("read", actor="Administrator", method="get_doc", channel="api")
		self.assertEqual(_fetch(name).actor_name, full)

	def test_unknown_enums_record_nothing(self):
		before = _names()
		for kw in (
			{"event": "bogus"},
			{"event": "read", "outcome": "bogus"},
			{"event": "read", "channel": "bogus"},
			{"event": None},
		):
			activity_log._reset_failure_log()
			with self.subTest(kw=kw), m.patch("frappe.logger") as logger:
				self.assertIsNone(activity_log.record(kw.pop("event"), actor="Administrator", **kw))
				self.assertTrue(logger.called)  # file-logged, not silent
		self.assertEqual(_names(), before)

	def test_every_documented_enum_value_is_accepted(self):
		for event in ("read", "export", "action", "denied_call", "auth_rejected", "foreign_session"):
			for outcome in ("ok", "denied", "failed"):
				with self.subTest(event=event, outcome=outcome):
					self.assertTrue(
						activity_log.record(event, actor="Administrator", outcome=outcome, channel="api")
					)
		for channel in ("desktop", "pwa", "chat", "agent_run", "macro", "trigger", "api"):
			with self.subTest(channel=channel):
				self.assertTrue(activity_log.record("read", actor="Administrator", channel=channel))

	def test_enums_match_the_doctype_selects(self):
		# The module constants and the doctype Select options must not drift apart:
		# a value valid here but missing from the Select would fail on insert.
		meta = frappe.get_meta(DT)
		self.assertEqual(activity_log.EVENTS, frozenset(meta.get_field("event").options.split("\n")))
		self.assertEqual(activity_log.OUTCOMES, frozenset(meta.get_field("outcome").options.split("\n")))
		self.assertEqual(activity_log.CHANNELS, frozenset(meta.get_field("channel").options.split("\n")))

	def test_long_strings_are_truncated_to_the_field_lengths(self):
		name = activity_log.record(
			"action",
			actor="Administrator",
			method="m" * 500,
			label="l" * 500,
			ref_doctype="d" * 500,
			ref_name="n" * 500,
			code="c" * 500,
			channel="api",
		)
		row = _fetch(name)
		self.assertEqual(len(row.method), 140)
		self.assertEqual(len(row.label), 140)
		self.assertEqual(len(row.ref_doctype), 140)
		self.assertEqual(len(row.ref_name), 140)
		self.assertEqual(len(row.code), 64)

	def test_non_string_text_is_coerced_not_fatal(self):
		name = activity_log.record("read", actor="Administrator", method=None, label=123, channel="api")
		row = _fetch(name)
		self.assertEqual((row.method or "", row.label), ("", "123"))

	def test_row_count_is_a_non_negative_int(self):
		for given, want in ((5, 5), ("12", 12), (-3, 0), (None, 0), ("x", 0), (4.9, 4), (True, 1)):
			with self.subTest(given=given):
				name = activity_log.record("read", actor="Administrator", row_count=given, channel="api")
				self.assertEqual(_fetch(name).row_count, want)

	def test_blank_actor_is_stored_blank(self):
		# foreign_session / unattributable auth_rejected rows have no actor.
		name = activity_log.record("foreign_session", actor="", channel="chat")
		row = _fetch(name)
		self.assertFalse(row.actor)
		self.assertFalse(row.actor_name)

	def test_delegate_tool_name_is_redacted_like_agent_audit(self):
		with m.patch.object(_private_output, "metadata_redacted", return_value=True):
			name = activity_log.record("read", actor="Administrator", method="get_doc", channel="chat")
		self.assertEqual(_fetch(name).method, _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertNotEqual(_private_output.PRIVATE_DELEGATE_TOOL_LABEL, "get_doc")
		name = activity_log.record("read", actor="Administrator", method="get_doc", channel="chat")
		self.assertEqual(_fetch(name).method, "get_doc")

	def test_delegate_redaction_blanks_every_target_column(self):
		# Deposit run: tool name, target doctype/name, label and size are all dropped,
		# exactly the set agent_audit blanks.
		with m.patch.object(_private_output, "metadata_redacted", return_value=True):
			name = activity_log.record(
				"read",
				actor="Administrator",
				method="get_doc",
				label="Read payroll",
				ref_doctype="Salary Slip",
				ref_name="SAL-0001",
				row_count=42,
				channel="chat",
			)
		row = _fetch(name)
		self.assertEqual(row.method, _private_output.PRIVATE_DELEGATE_TOOL_LABEL)
		self.assertFalse(row.label)
		self.assertFalse(row.ref_doctype)
		self.assertFalse(row.ref_name)
		self.assertEqual(row.row_count, 0)
		self.assertEqual(row.event, "read")  # the fact of the access is still recorded

	def test_actor_name_falls_back_to_the_actor_id(self):
		with m.patch.object(frappe.db, "get_value", return_value=None):
			name = activity_log.record("read", actor="Administrator", channel="api")
		self.assertEqual(_fetch(name).actor_name, "Administrator")

	def test_nonexistent_actor_still_stores_the_row(self):
		# ignore_links=True: a stale session can name a user deleted since.
		ghost = "ghost-activity-user@example.com"
		self.assertFalse(frappe.db.exists("User", ghost))
		name = activity_log.record("auth_rejected", actor=ghost, outcome="denied", channel="chat")
		row = _fetch(name)
		self.assertEqual((row.actor, row.actor_name), (ghost, ghost))


class TestRecordPublishesNothing(FrappeTestCase):
	"""``Document.insert`` ends in ``notify_update``: a ``doc_update`` for the row and a
	site-wide ``list_update`` carrying the acting user's name, to every open list view.
	Per tool call that is a realtime fan-out (and a leak of who is acting) for a table no
	one watches live. ``doc.flags.notify_update = False`` skips both on v15 and v16
	(``run_post_save_methods``: ``if self.flags.get("notify_update", True)``)."""

	def test_record_publishes_no_realtime_event(self):
		with m.patch("frappe.publish_realtime") as publish:
			name = activity_log.record("read", actor="Administrator", method="get_list", channel="api")
		self.assertTrue(name)
		publish.assert_not_called()


class TestRecordResilience(FrappeTestCase):
	def setUp(self):
		super().setUp()
		activity_log._reset_failure_log()  # the de-duplication is process-wide state
		self.addCleanup(activity_log._reset_failure_log)

	def test_missing_table_is_a_silent_skip(self):
		with (
			m.patch.object(frappe.db, "table_exists", return_value=False) as te,
			m.patch("frappe.get_doc") as get_doc,
			m.patch("frappe.logger") as logger,
		):
			self.assertIsNone(activity_log.record("read", actor="Administrator", channel="api"))
		te.assert_called_with(DT)
		get_doc.assert_not_called()
		logger.assert_not_called()

	def test_table_missing_error_at_insert_is_silent_too(self):
		# Code deployed before migrate, with table_exists answering from a stale cache.
		with (
			m.patch("frappe.db.is_table_missing", return_value=True),
			m.patch("frappe.model.document.Document.insert", side_effect=frappe.db.TableMissingError),
			m.patch("frappe.logger") as logger,
		):
			self.assertIsNone(activity_log.record("read", actor="Administrator", channel="api"))
		logger.assert_not_called()

	def test_insert_failure_is_swallowed_and_outer_write_survives(self):
		# The outer write shares the transaction; a failed activity insert must roll
		# back to ITS savepoint only (the whole point of the savepoint).
		todo = frappe.get_doc({"doctype": "ToDo", "description": "activity-log outer write"}).insert()
		real_insert = frappe.model.document.Document.insert

		def boom(self_, *a, **kw):
			if self_.doctype == DT:
				# Dirty the transaction first so the rollback has something to undo.
				frappe.db.sql(
					f"insert into `tab{DT}` (name, creation, modified, owner, modified_by) values ('half-written', now(), now(), 'Administrator', 'Administrator')"
				)
				raise RuntimeError("insert exploded")
			return real_insert(self_, *a, **kw)

		before_errors = frappe.db.count("Error Log")
		with (
			m.patch("frappe.model.document.Document.insert", boom),
			m.patch("frappe.logger") as logger,
		):
			self.assertIsNone(
				activity_log.record("read", actor="Administrator", method="get_doc", channel="api")
			)
		self.assertTrue(frappe.db.exists("ToDo", todo.name))  # outer write survived
		self.assertFalse(frappe.db.exists(DT, "half-written"))  # inner work rolled back
		self.assertEqual(frappe.db.count("Error Log"), before_errors)  # never frappe.log_error
		logger.assert_called_once_with("jarvis.activity_log")
		logger.return_value.error.assert_called_once()

	def _failure_lines(self, calls):
		with (
			m.patch("frappe.model.document.Document.insert", side_effect=RuntimeError("x")),
			m.patch("frappe.logger") as logger,
		):
			for event in calls:
				activity_log.record(event, actor="Administrator", channel="api")
		return [c.args[0] for c in logger.return_value.error.call_args_list]

	def test_failure_line_names_the_event_kind_and_the_site(self):
		(line,) = self._failure_lines(["export"])
		self.assertIn("export", line)
		self.assertIn(frappe.local.site, line)

	def test_identical_failures_are_logged_once_per_five_minutes(self):
		clock = m.MagicMock()
		with m.patch("jarvis.activity_log.time", clock):
			clock.monotonic.return_value = 1000.0
			lines = self._failure_lines(["read"] * 5)
			self.assertEqual(len(lines), 1)
			clock.monotonic.return_value = 1000.0 + 299
			self.assertEqual(self._failure_lines(["read"]), [])
			clock.monotonic.return_value = 1000.0 + 301
			self.assertEqual(len(self._failure_lines(["read"])), 1)

	def test_failures_of_a_different_event_kind_are_not_swallowed_by_the_dedupe(self):
		lines = self._failure_lines(["read", "export", "read", "export", "denied_call"])
		self.assertEqual(len(lines), 3)

	def test_rejections_name_an_invalid_event_without_echoing_it(self):
		with m.patch("frappe.logger") as logger:
			activity_log.record(f"evil {_SECRET}", actor="Administrator", channel="api")
		(line,) = [c.args[0] for c in logger.return_value.error.call_args_list]
		self.assertNotIn(_SECRET, line)

	def test_failure_log_line_carries_no_tool_args_and_no_frame_locals(self):
		captured = []

		class Handler(logging.Handler):
			def emit(self, record):
				captured.append(record)

		def boom(self_, *a, **kw):
			# A driver/validation message can quote a column value: the log line must
			# carry the exception class and raise site, never this message.
			raise RuntimeError(f"insert exploded on {_SECRET}")

		handler = Handler(level=logging.DEBUG)
		fake_logger = logging.getLogger("jarvis-activity-log-test")
		fake_logger.setLevel(logging.DEBUG)
		fake_logger.addHandler(handler)
		try:
			with (
				m.patch("frappe.model.document.Document.insert", boom),
				m.patch("frappe.logger", return_value=fake_logger) as logger,
			):
				activity_log.record(
					"read",
					actor="Administrator",
					method="get_doc",
					label=_SECRET,
					ref_name=_SECRET,
					channel="api",
				)
		finally:
			fake_logger.removeHandler(handler)
		logger.assert_called_with("jarvis.activity_log")
		self.assertEqual(len(captured), 1)
		rec = captured[0]
		self.assertEqual(rec.levelno, logging.ERROR)
		formatted = logging.Formatter().format(rec)
		self.assertNotIn(_SECRET, formatted)
		self.assertNotIn(_SECRET, rec.getMessage())
		self.assertIn("RuntimeError", rec.getMessage())  # still triageable

	def test_never_commits_and_never_raises(self):
		with m.patch.object(frappe.db, "commit") as commit:
			activity_log.record("read", actor="Administrator", channel="api")
			with m.patch("frappe.model.document.Document.insert", side_effect=RuntimeError("x")):
				self.assertIsNone(activity_log.record("read", actor="Administrator", channel="api"))
			# Garbage everywhere: still no exception, still no commit.
			activity_log.record(object(), actor=object(), method=object(), row_count=object())
		commit.assert_not_called()

	def test_logger_failure_itself_does_not_raise(self):
		with (
			m.patch("frappe.model.document.Document.insert", side_effect=RuntimeError("x")),
			m.patch("frappe.logger", side_effect=RuntimeError("logger down")),
		):
			self.assertIsNone(activity_log.record("read", actor="Administrator", channel="api"))


class TestChannelResolution(FrappeTestCase):
	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		super().tearDown()

	def _channel(self, **kw):
		return _fetch(activity_log.record("read", actor="Administrator", **kw)).channel

	def test_client_header_desktop_and_pwa(self):
		for hdr in ("desktop", "pwa"):
			with self.subTest(header=hdr), _ReqCtx(**{"X-Jarvis-Client": hdr}):
				self.assertEqual(self._channel(), hdr)

	def test_unknown_or_hostile_header_values_fall_through_to_api(self):
		for hdr in ("evil", "DESKTOP; drop table", "chat", ""):
			with self.subTest(header=hdr), _ReqCtx(**{"X-Jarvis-Client": hdr}):
				self.assertEqual(self._channel(), "api")

	def test_plugin_session_key_means_chat(self):
		_agent_run_ctx.set_session_key("sess-abc")
		self.assertEqual(self._channel(), "chat")

	def test_default_is_api(self):
		self.assertEqual(self._channel(), "api")

	def test_explicit_channel_wins(self):
		_agent_run_ctx.set_session_key("sess-abc")
		with _ReqCtx(**{"X-Jarvis-Client": "pwa"}):
			self.assertEqual(self._channel(channel="macro"), "macro")


class TestHelpers(FrappeTestCase):
	def test_row_count_of_shapes(self):
		f = activity_log.row_count_of
		self.assertEqual(f("get_list", [1, 2, 3]), 3)
		self.assertEqual(f("get_list", []), 0)
		for key in ("data", "rows", "result", "records", "docs", "results"):
			with self.subTest(key=key):
				self.assertEqual(f("run_report", {key: [1, 2]}), 2)
		self.assertEqual(f("get_list", {"unrelated": [1, 2]}), 0)
		# Single-document get_doc: the doc itself is the result, one row read. A batch
		# counts its `docs`; other tools returning a bare dict stay 0.
		self.assertEqual(f("get_doc", {"name": "X"}), 1)
		self.assertEqual(f("get_doc", {"docs": [1, 2, 3], "count": 3}), 3)
		self.assertEqual(f("get_linked_docs", {"name": "X"}), 0)
		self.assertEqual(f("get_doc", None), 0)
		self.assertEqual(f("get_doc", "string"), 0)

	def test_row_count_of_export_shapes(self):
		f = activity_log.row_count_of
		# export_query immediate: `total`; background state: `rows_written`.
		self.assertEqual(f("export_query", {"file_url": "/x", "total": 1200}), 1200)
		self.assertEqual(f("export_query", {"status": "ready", "rows_written": 77}), 77)
		self.assertEqual(f("export_query", {"total": "9"}), 9)
		self.assertEqual(f("export_query", {"total": -4}), 0)
		self.assertEqual(f("export_query", {"file_url": "/x"}), 0)
		# Other export tools carry no row count.
		self.assertEqual(f("download_pdf", {"file_url": "/x"}), 0)
		self.assertEqual(f("export_excel", {"file_url": "/x", "chart_count": 2}), 0)

	def test_ref_doctype_of_is_string_only_and_names_a_real_doctype_or_report(self):
		f = activity_log.ref_doctype_of
		self.assertEqual(f("get_list", {"doctype": "ToDo"}), "ToDo")
		self.assertEqual(
			f("run_report", {"report_name": "Permitted Documents For User"}), "Permitted Documents For User"
		)
		self.assertEqual(f("export_query", {"ref_doctype": "User"}), "User")
		self.assertEqual(f("get_list", {"doctype": ["ToDo"]}), "")
		self.assertEqual(f("get_list", {"doctype": {"a": 1}}), "")
		self.assertEqual(f("get_list", {"doctype": 5}), "")
		self.assertEqual(f("get_list", None), "")
		self.assertEqual(f("get_list", "not a dict"), "")
		self.assertEqual(f("get_list", {}), "")
		self.assertEqual(f("get_list", {"doctype": "x" * 400}), "")

	def test_ref_doctype_of_stores_nothing_that_is_not_an_existing_doctype_or_report(self):
		# A model-supplied string is free text: a customer name or phone number pasted into
		# `doctype` must never reach the table (plan D1). Only a real DocType / Report name does.
		f = activity_log.ref_doctype_of
		for junk in ("Alice 555-1234", "Not A Real Doctype", "ToDo; DROP TABLE x", "' OR 1=1 --", "%"):
			self.assertEqual(f("get_list", {"doctype": junk}), "", msg=junk)
		self.assertEqual(
			f("get_list", {"doctype": "Alice 555-1234", "report_name": "Permitted Documents For User"}), ""
		)

	def test_ref_doctype_of_returns_the_canonical_name_not_the_callers_spelling(self):
		# MariaDB's collation is case-insensitive: the caller's casing is theirs, the stored one is the table's.
		self.assertEqual(activity_log.ref_doctype_of("get_list", {"doctype": "todo"}), "ToDo")

	def test_ref_doctype_of_never_raises_on_a_lookup_error(self):
		with m.patch("frappe.db.get_value", side_effect=RuntimeError("db down")):
			self.assertEqual(activity_log.ref_doctype_of("get_list", {"doctype": "ToDo"}), "")

	def test_tool_sets_name_real_tools(self):
		# A typo here would silently mis-classify a tool (an introspection name that
		# does not exist logs nothing for it; an export name that does not exist
		# misses an export). Pin against the registry.
		from jarvis.tools import registry

		real = set(registry.list_tools())
		self.assertFalse(activity_log.INTROSPECTION_TOOLS - real)
		self.assertFalse(activity_log.EXPORT_TOOLS - real)
		self.assertIn("describe_customizations", activity_log.INTROSPECTION_TOOLS)
		self.assertNotIn("get_doc", activity_log.INTROSPECTION_TOOLS)
