"""Jarvis Pending Action core (PR-2a): doctype guards, the seal, park, and the
claim-first executor (§4.1-§4.4; AC-U3 / U4 / U5 / U6)."""

from __future__ import annotations

import threading
from unittest.mock import patch

import frappe
from cryptography.fernet import Fernet
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _seal
from jarvis.tests._pending_action_helpers import (
	AGENT_WRITE,
	CARD,
	DEFAULT_ARGS,
	DELEGATE,
	MSG,
	OTHER,
	OWNER,
	PA,
	SM_USER,
	WAITER,
	PendingActionTestMixin,
	as_user,
	fake_dispatch,
)


class TestSeal(PendingActionTestMixin, FrappeTestCase):
	def test_round_trip_binds_row_and_stored_card(self):
		conv = self.make_conv()
		name = self.park(conv)
		row = self.row(name)
		call = _seal.unseal_call(row)
		self.assertEqual(call["args"], DEFAULT_ARGS)
		self.assertEqual(call["name"], name)
		self.assertEqual(call["exec_user"], OWNER)
		self.assertEqual(call["origin_conversation"], conv)
		# The card's HTML-ish text is stored verbatim (no XSS rewrite), so the hash matches.
		self.assertEqual(frappe.parse_json(row.card), CARD)
		self.assertEqual(call["card_sha256"], _seal.card_sha256(row))
		self.assertNotIn("PA-SECRET-ARG", row.sealed_call)

	def test_edited_display_or_identity_column_is_tampered(self):
		conv = self.make_conv()
		for column, value in (
			("summary", "Delete everything"),
			("card", '{"title":"other"}'),
			("preview", '{"would":{}}'),
			("exec_user", SM_USER),
			("owner_user", OTHER),
			("tool", "delete_doc"),
		):
			name = self.park(conv)
			self.set_col(name, **{column: value})
			with self.assertRaises(_seal.SealError) as ctx:
				_seal.unseal_call(self.row(name))
			self.assertEqual(ctx.exception.reason_code, "tampered", column)
			self.set_col(name, status="Cancelled")  # free the single-flight slot

	def test_transplanted_envelope_is_tampered(self):
		a = self.park(self.make_conv())
		b = self.park(self.make_conv())
		self.set_col(b, sealed_call=self.row(a).sealed_call)
		with self.assertRaises(_seal.SealError) as ctx:
			_seal.unseal_call(self.row(b))
		self.assertEqual((ctx.exception.reason_code, ctx.exception.binding), ("tampered", "name"))
		self.set_col(b, sealed_settlement=_seal.seal_settlement(a, {"result": None}))
		with self.assertRaises(_seal.SealError):
			_seal.unseal_settlement(self.row(b))

	def test_wrong_key_is_unverifiable(self):
		name = self.park(self.make_conv())
		other = Fernet.generate_key().decode()
		with patch("jarvis.chat.pending_actions._seal.get_encryption_key", return_value=other):
			with self.assertRaises(_seal.SealError) as ctx:
				_seal.unseal_call(self.row(name))
		self.assertEqual(ctx.exception.reason_code, "unverifiable")

	def test_open_key_is_keyed_per_owner(self):
		k = _seal.open_key(OWNER, "gstin:29ABCDE1234F1Z5")
		self.assertEqual(k, _seal.open_key(OWNER, "gstin:29ABCDE1234F1Z5"))
		self.assertNotEqual(k, _seal.open_key(OTHER, "gstin:29ABCDE1234F1Z5"))
		self.assertNotIn("29ABCDE", k)


class TestGuards(PendingActionTestMixin, FrappeTestCase):
	def _raw_doc(self, **extra):
		return frappe.get_doc(
			{
				"doctype": PA,
				"kind": "chat",
				"tool": "add_comment",
				"owner_user": OWNER,
				"exec_user": OWNER,
				**extra,
			}
		)

	def test_admin_orm_insert_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._raw_doc().insert(ignore_permissions=True)

	def test_flagged_insert_without_a_call_is_refused(self):
		doc = self._raw_doc()
		doc.flags.jarvis_server_write = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_saving_an_existing_row_is_refused_even_flagged(self):
		name = self.park(self.make_conv())
		doc = frappe.get_doc(PA, name)
		doc.flags.jarvis_server_write = True
		doc.status = "Executed"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		doc = frappe.get_doc(PA, name)
		doc.append("waiters", {"conversation": self.make_conv(), "role": "waiter"})
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		self.assertEqual(self.row(name).status, "Pending")

	def test_admin_orm_delete_refused(self):
		name = self.park(self.make_conv())
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(PA, name, ignore_permissions=True, force=True)
		self.assertTrue(frappe.db.exists(PA, name))

	def test_child_rows_refuse_direct_writes(self):
		conv = self.make_conv()
		name = self.park(conv, kind="file_box_held")
		waiter = frappe.db.get_value(WAITER, {"parent": name}, "name")
		child = frappe.get_doc(
			{
				"doctype": WAITER,
				"parent": name,
				"parenttype": PA,
				"parentfield": "waiters",
				"conversation": self.make_conv(),
			}
		)
		with self.assertRaises(frappe.PermissionError):
			child.insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(WAITER, waiter, ignore_permissions=True, force=True)
		self.assertEqual(frappe.db.count(WAITER, {"parent": name}), 1)


class TestNoDocPerm(PendingActionTestMixin, FrappeTestCase):
	def test_system_manager_cannot_reach_rows(self):
		from jarvis.exceptions import PermissionDeniedError
		from jarvis.tools.assign_to import assign_to
		from jarvis.tools.query import query

		name = self.park(self.make_conv())
		frappe.db.commit()
		with as_user(SM_USER):
			self.assertFalse(frappe.has_permission(PA, "read", doc=name))
			self.assertFalse(frappe.has_permission(WAITER, "read"))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_list(PA, fields=["name", "sealed_call"])
			with self.assertRaises(frappe.PermissionError):
				frappe.client.get(PA, name)
			with self.assertRaises((PermissionDeniedError, frappe.PermissionError)):
				query({"from": PA, "fields": ["name", "sealed_call", "open_key"], "limit": 5})
			with self.assertRaises((PermissionDeniedError, frappe.PermissionError)):
				assign_to(PA, name, user=SM_USER)
		self.assertEqual(frappe.db.count("ToDo", {"reference_type": PA}), 0)

	def test_permission_hooks_deny_everything(self):
		for doctype in (PA, WAITER):
			self.assertEqual(
				frappe.call(frappe.get_hooks("has_permission")[doctype][-1], doc=None, ptype="read"), False
			)
			self.assertEqual(frappe.call(frappe.get_hooks("permission_query_conditions")[doctype][-1]), "1=0")


class TestPark(PendingActionTestMixin, FrappeTestCase):
	def test_park_stores_a_sealed_pending_row(self):
		conv = self.make_conv()
		name = self.park(conv, summary="x" * 400, run_id="run-1")
		row = self.row(name)
		self.assertEqual(
			(row.kind, row.status, row.conversation, row.origin_conversation), ("chat", "Pending", conv, conv)
		)
		self.assertEqual(len(row.summary), 280)
		self.assertIsNone(row.open_key)
		self.assertEqual(row.settled, 0)

	def test_summary_is_stored_as_plain_text_and_still_executes(self):
		hostile = "<img src=x onerror=alert(1)>Pay <b>ACME</b><script>alert(2)</script> <svg onload=alert(3)"
		name = self.park(self.make_conv(), summary=hostile)
		self.assertEqual(self.row(name).summary, "Pay ACMEalert(2) svg onload=alert(3)")
		with fake_dispatch(), as_user(OWNER):
			self.assertTrue(pa.execute(name)["ok"], "the card hash is over the stored text")
		meta = frappe.get_meta(PA)
		for fieldname in ("summary", "card", "preview"):
			field = meta.get_field(fieldname)
			self.assertEqual(
				(field.read_only, field.in_list_view, field.in_standard_filter), (1, 0, 0), fieldname
			)

	def test_single_flight_refuses_a_second_chat_card(self):
		conv = self.make_conv()
		first = self.park(conv)
		with self.assertRaises(pa.ConfirmationPendingError):
			self.park(conv)
		self.set_col(first, status="Executing")
		with self.assertRaises(pa.ConfirmationPendingError):
			self.park(conv)
		self.assertEqual(frappe.db.count(PA, {"conversation": conv}), 1)
		self.set_col(first, status="Executed")
		self.park(conv)  # a terminal card no longer blocks

	def test_legacy_pending_hook_joins_single_flight(self):
		conv = self.make_conv()
		with self.assertRaises(pa.ConfirmationPendingError):
			self.park(conv, legacy_pending=lambda c: c == conv)
		self.park(conv, legacy_pending=lambda c: False)

	def test_supersede_hook_is_consulted_under_the_lock(self):
		from jarvis.chat.pending_actions import _park

		conv = self.make_conv()
		old = self.park(conv)
		seen = []

		def retire(row, *, conversation, owner_user):
			seen.append((row.name, conversation, owner_user))
			return pa._transition(row.name, ["Pending"], "Superseded", reason_code="superseded") == "ok"

		with patch.object(_park, "SUPERSEDE_HOOK", "jarvis.tests.test_pending_action._retire_hook"):
			with patch("jarvis.tests.test_pending_action._retire_hook", side_effect=retire, create=True):
				new = self.park(conv)
		self.assertEqual(seen, [(old, conv, OWNER)])
		self.assertEqual(self.row(old).status, "Superseded")
		self.assertIsNone(self.row(old).open_key)
		self.assertEqual(self.row(old).settled, 1, "a superseded card is settled after park's commit")
		self.assertEqual(self.row(new).status, "Pending")

	def test_display_row_rides_the_park_transaction(self):
		conv = self.make_conv()
		seen = []

		def boom(doc):
			seen.append(doc.name)
			raise RuntimeError("display row failed")

		with self.assertRaises(RuntimeError):
			self.park(conv, display_row=boom)
		self.assertEqual(len(seen), 1)
		self.assertFalse(frappe.db.exists(PA, seen[0]), "the card and its row roll back together")
		ok = []
		name = self.park(conv, display_row=lambda doc: ok.append(doc.name))
		self.assertEqual(ok, [name])

	def test_park_refuses_inside_a_tool_call(self):
		frappe.local.jarvis_dispatch_depth = 1
		try:
			with self.assertRaises(frappe.PermissionError):
				self.park(self.make_conv())
		finally:
			frappe.local.jarvis_dispatch_depth = 0

	def test_target_snapshot_single_and_bulk(self):
		conv = self.make_conv()
		a, b = self.make_todo(), self.make_todo()
		name = self.park(
			conv, tool="update_doc", args={"doctype": "ToDo", "name": a, "changes": {"status": "Closed"}}
		)
		targets = _seal.unseal_call(self.row(name))["targets"]
		self.assertEqual([t[:2] for t in targets], [["ToDo", a]])
		self.assertEqual(targets[0][3], 0)
		self.assertRegex(targets[0][2], r"\.\d{6}$", "modified is snapshotted to the microsecond")
		self.set_col(name, status="Cancelled")
		bulk = self.park(conv, tool="submit_doc", args={"doctype": "ToDo", "names": [a, b]})
		self.assertEqual([t[1] for t in _seal.unseal_call(self.row(bulk))["targets"]], [a, b])
		self.assertEqual(pa.snapshot_targets("create_doc", {"doctype": "ToDo", "name": a}), [])

	def test_held_park_seeds_primary_waiter_and_open_key(self):
		conv = self.make_conv()
		name = self.park(conv, kind="file_box_held", dedup_key="party:acme")
		row = self.row(name)
		self.assertEqual(row.open_key, _seal.open_key(OWNER, "party:acme"))
		waiters = frappe.get_all(WAITER, filters={"parent": name}, fields=["conversation", "role"])
		self.assertEqual([(w.conversation, w.role) for w in waiters], [(conv, "primary")])
		with self.assertRaises(frappe.UniqueValidationError):
			self.park(self.make_conv(), kind="file_box_held", dedup_key="party:acme")


class TestExecute(PendingActionTestMixin, FrappeTestCase):
	def test_success_runs_once_as_exec_user_and_settles(self):
		conv = self.make_conv()
		name = self.park(conv)
		seen_user = []
		with fake_dispatch(lambda t, a: seen_user.append(frappe.session.user) or {"name": "C-1"}) as calls:
			with as_user(OWNER):
				out = pa.execute(name, conversation=conv)
		self.assertTrue(out["ok"])
		self.assertEqual(
			(out["pa_status"], out["outcome"], out["reason_code"]), ("Executed", "confirmed", None)
		)
		self.assertEqual(calls, [("add_comment", DEFAULT_ARGS)])
		self.assertEqual(seen_user, [OWNER])
		row = self.row(name)
		self.assertEqual((row.status, row.decided_by, row.settled), ("Executed", OWNER, 1))
		self.assertIsNone(row.sealed_call)
		self.assertIsNone(row.sealed_settlement)
		receipt = frappe.db.get_value(MSG, {"conversation": conv, "tool_call_id": name}, "action_outcome")
		self.assertEqual(receipt, "confirmed")
		with fake_dispatch() as again, as_user(OWNER):
			out = pa.execute(name, conversation=conv)
		self.assertEqual((out["ok"], out["reason_code"]), (False, "already_handled"))
		self.assertEqual(again, [], "never re-run")

	def test_runs_as_sealed_exec_user_not_the_approver(self):
		conv = self.make_conv()
		name = self.park(conv, exec_user=DELEGATE)
		seen = []
		with fake_dispatch(lambda t, a: seen.append(frappe.session.user) or {}), as_user(OWNER):
			out = pa.execute(name)
		self.assertTrue(out["ok"])
		self.assertEqual(seen, [DELEGATE])
		self.assertEqual(self.row(name).decided_by, OWNER)

	def test_unauthorized_callers_learn_and_write_nothing(self):
		conv = self.make_conv()
		name = self.park(conv)
		with fake_dispatch() as calls:
			for user in (OTHER, SM_USER):  # D1: chat cards are owner-only, SM included
				with as_user(user):
					self.assertEqual(pa.execute(name)["reason_code"], "not_found")
			with as_user(OWNER):
				self.assertEqual(pa.execute(name, conversation=self.make_conv())["reason_code"], "not_found")
				self.assertEqual(pa.execute(name, kind="file_box_held")["reason_code"], "not_found")
				self.assertEqual(pa.execute("no-such-row")["reason_code"], "not_found")
		self.assertEqual(calls, [])
		self.assertEqual((self.row(name).status, self.row(name).decided_by), ("Pending", None))

	def test_conversation_less_card_adopts_an_owned_conversation(self):
		name = self.park(None)
		other_owner_conv = self.make_conv(OTHER)
		with fake_dispatch(), as_user(OWNER):
			self.assertEqual(pa.execute(name, conversation=other_owner_conv)["reason_code"], "not_found")
			mine = self.make_conv()
			self.assertTrue(pa.execute(name, conversation=mine)["ok"])
		self.assertEqual(self.row(name).adopted_conversation, mine)
		self.assertEqual(
			frappe.db.get_value(MSG, {"conversation": mine, "tool_call_id": name}, "action_outcome"),
			"confirmed",
		)

	def test_system_manager_may_run_another_users_held_row_after_preflight(self):
		conv = self.make_conv()
		name = self.park(conv, kind="file_box_held")
		with fake_dispatch() as calls, as_user(SM_USER):
			out = pa.execute(name, kind="file_box_held")
		self.assertTrue(out["ok"])
		self.assertEqual(len(calls), 1)
		provenance = frappe.db.get_value(AGENT_WRITE, {"actor": OWNER, "tool": "add_comment"}, "provenance")
		self.assertEqual(provenance, "approval")

	def test_refuses_inside_a_tool_call(self):
		name = self.park(self.make_conv())
		frappe.local.jarvis_dispatch_depth = 1
		try:
			with as_user(OWNER), self.assertRaises(frappe.PermissionError):
				pa.execute(name)
		finally:
			frappe.local.jarvis_dispatch_depth = 0

	def test_armed_macro_conversation_refuses_without_consuming(self):
		conv = self.make_conv()
		name = self.park(conv)
		frappe.db.set_value("Jarvis Conversation", conv, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		with fake_dispatch() as calls, as_user(OWNER):
			self.assertEqual(pa.execute(name)["reason_code"], "armed_run")
		self.assertEqual(calls, [])
		self.assertEqual(self.row(name).status, "Pending")


class TestIdentityPreflight(PendingActionTestMixin, FrappeTestCase):
	def _assert_refused(self, name):
		with fake_dispatch() as calls, as_user(OWNER):
			self.assertEqual(pa.execute(name)["reason_code"], "identity_refused")
		self.assertEqual(calls, [])
		self.assertEqual(self.row(name).status, "Pending", "a refusal consumes nothing")

	def test_disabled_exec_user(self):
		name = self.park(self.make_conv(), exec_user=DELEGATE)
		frappe.db.set_value("User", DELEGATE, "enabled", 0)
		frappe.db.commit()
		self._assert_refused(name)

	def test_disabled_users_own_live_session(self):
		name = self.park(self.make_conv(DELEGATE), owner=DELEGATE)
		frappe.db.set_value("User", DELEGATE, "enabled", 0)
		frappe.db.commit()
		with fake_dispatch() as calls, as_user(DELEGATE):
			self.assertEqual(pa.execute(name)["reason_code"], "identity_refused")
		self.assertEqual((calls, self.row(name).status), ([], "Pending"))

	def test_administrator_exec_user_for_another_approver(self):
		self._assert_refused(self.park(self.make_conv(), exec_user="Administrator"))

	def test_renamed_exec_user(self):
		from jarvis.tests._pending_action_helpers import ensure_user

		ghost, renamed = "pa-renamed@example.com", "pa-renamed-2@example.com"
		if frappe.db.exists("User", renamed):
			frappe.delete_doc("User", renamed, ignore_permissions=True, force=True)
		ensure_user(ghost)
		name = self.park(self.make_conv(), exec_user=ghost)
		frappe.rename_doc("User", ghost, renamed, force=True)
		frappe.db.commit()
		try:
			self._assert_refused(name)
		finally:
			frappe.delete_doc("User", renamed, ignore_permissions=True, force=True)
			frappe.db.commit()

	def test_deleted_exec_user(self):
		self._assert_refused(self.park(self.make_conv(), exec_user="pa-never-existed@example.com"))


class TestStale(PendingActionTestMixin, FrappeTestCase):
	def _park_update(self, todo):
		return self.park(
			self.make_conv(), tool="update_doc", args={"doctype": "ToDo", "name": todo, "changes": {}}
		)

	def test_modified_target_is_stale(self):
		todo = self.make_todo()
		name = self._park_update(todo)
		frappe.db.sql(
			"UPDATE `tabToDo` SET modified = modified + INTERVAL 1 MICROSECOND WHERE name=%(n)s", {"n": todo}
		)
		frappe.db.commit()
		with fake_dispatch() as calls, as_user(OWNER):
			out = pa.execute(name)
		self.assertEqual((out["reason_code"], out["pa_status"]), ("stale", "Failed"))
		self.assertEqual(calls, [])
		self.assertEqual((self.row(name).reason_code, self.row(name).settled), ("stale", 1))

	def test_docstatus_change_is_stale(self):
		todo = self.make_todo()
		name = self._park_update(todo)
		frappe.db.sql("UPDATE `tabToDo` SET docstatus=1 WHERE name=%(n)s", {"n": todo})
		frappe.db.commit()
		with fake_dispatch() as calls, as_user(OWNER):
			self.assertEqual(pa.execute(name)["reason_code"], "stale")
		self.assertEqual(calls, [])

	def test_missing_target(self):
		todo = self.make_todo()
		name = self._park_update(todo)
		frappe.db.delete("ToDo", {"name": todo})
		frappe.db.commit()
		with fake_dispatch() as calls, as_user(OWNER):
			self.assertEqual(pa.execute(name)["reason_code"], "target_missing")
		self.assertEqual(calls, [])

	def test_unchanged_target_runs(self):
		name = self._park_update(self.make_todo())
		with fake_dispatch() as calls, as_user(OWNER):
			self.assertTrue(pa.execute(name)["ok"])
		self.assertEqual(len(calls), 1)


class TestSealFailuresAtExecute(PendingActionTestMixin, FrappeTestCase):
	def test_tampered_card_never_dispatches_and_logs_only_the_binding(self):
		name = self.park(self.make_conv())
		self.set_col(name, summary="Delete every invoice")
		with fake_dispatch() as calls, as_user(OWNER):
			out = pa.execute(name)
		self.assertEqual((out["reason_code"], self.row(name).status), ("tampered", "Failed"))
		self.assertEqual(calls, [])
		logs = self.logged("tampered", name)
		self.assertEqual(len(logs), 1)
		self.assertIn("card", logs[0])
		self.assertNotIn("PA-SECRET-ARG", logs[0])

	def test_wrong_key_is_unverifiable(self):
		name = self.park(self.make_conv())
		with patch(
			"jarvis.chat.pending_actions._seal.get_encryption_key",
			return_value=Fernet.generate_key().decode(),
		):
			with fake_dispatch() as calls, as_user(OWNER):
				self.assertEqual(pa.execute(name)["reason_code"], "unverifiable")
		self.assertEqual(calls, [])


class TestBusyAndRace(PendingActionTestMixin, FrappeTestCase):
	def test_locked_row_maps_to_busy(self):
		name = self.park(self.make_conv())
		frappe.db.commit()
		site, locked, release = frappe.local.site, threading.Event(), threading.Event()
		errors = []

		def holder():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.sql(f"SELECT name FROM `tab{PA}` WHERE name=%(n)s FOR UPDATE", {"n": name})
				locked.set()
				release.wait(timeout=20)
				frappe.db.rollback()
			except Exception as e:  # pragma: no cover
				errors.append(e)
				locked.set()
			finally:
				frappe.destroy()

		t = threading.Thread(target=holder)
		t.start()
		try:
			self.assertTrue(locked.wait(timeout=20))
			with fake_dispatch() as calls, as_user(OWNER):
				out = pa.execute(name)
		finally:
			release.set()
			t.join(timeout=20)
		self.assertEqual(errors, [])
		self.assertEqual(out["reason_code"], "busy")
		self.assertEqual(calls, [])
		self.assertEqual(self.row(name).status, "Pending")

	def test_two_connections_exactly_one_dispatch(self):
		conv = self.make_conv()
		name = self.park(conv)
		frappe.db.commit()
		site = frappe.local.site
		barrier = threading.Barrier(2)
		results, errors, dispatched = {}, [], []
		lock = threading.Lock()

		def body(tool, args):
			with lock:
				dispatched.append(tool)
			return {"name": "C-1"}

		def worker(label):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(OWNER)
			try:
				barrier.wait(timeout=10)
				results[label] = pa.execute(name, conversation=conv)
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		with fake_dispatch(body):
			threads = [threading.Thread(target=worker, args=(x,)) for x in "AB"]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=30)
		self.assertEqual(errors, [])
		self.assertEqual(len(dispatched), 1, f"exactly one dispatch: {results}")
		self.assertEqual(sorted(r["ok"] for r in results.values()), [False, True])
		loser = next(r for r in results.values() if not r["ok"])
		self.assertIn(loser["reason_code"], ("busy", "executing", "already_handled"))
		self.assertEqual(self.row(name).status, "Executed")


class _Crash(BaseException):
	"""A worker death between the claim and the final write."""


class TestCrashAfterClaim(PendingActionTestMixin, FrappeTestCase):
	def test_crash_leaves_executing_then_interrupted_never_rerun(self):
		conv = self.make_conv()
		name = self.park(conv)

		def die(tool, args):
			raise _Crash()

		with fake_dispatch(die) as calls, as_user(OWNER), self.assertRaises(_Crash):
			pa.execute(name)
		frappe.db.rollback()
		self.assertEqual(self.row(name).status, "Executing", "the claim was committed before the call ran")
		self.assertEqual(len(calls), 1)
		self.set_col(name, executing_at=frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11))
		pa.reconcile()
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code, row.settled), ("Failed", "interrupted", 1))
		self.assertEqual(
			frappe.db.get_value(MSG, {"conversation": conv, "tool_call_id": name}, "action_outcome"),
			"unknown",
		)
		with fake_dispatch() as again, as_user(OWNER):
			self.assertEqual(pa.execute(name)["reason_code"], "already_handled")
		self.assertEqual(again, [])

	def test_late_outcome_keeps_the_reconcilers_verdict(self):
		name = self.park(self.make_conv())

		def slow(tool, args):
			# The reconciler calls it interrupted while the call is still running.
			pa._terminal_update(name, ["Executing"], "Failed", reason_code="interrupted")
			frappe.db.commit()
			return {"name": "C-9"}

		with fake_dispatch(slow), as_user(OWNER):
			out = pa.execute(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assertEqual((out["pa_status"], out["outcome"]), ("Failed", "unknown"))
		self.assertIn("Late outcome: confirmed.", row.reason)
		self.assertEqual(len(self.logged("late_outcome", name)), 1)

	def test_a_crash_after_the_unseal_logs_and_raises_no_arg_values(self):
		# AC-U4: what the request handler would log (locals included) must hold no arg.
		name = self.park(self.make_conv())
		seen = {}
		with (
			fake_dispatch(),
			as_user(OWNER),
			patch("jarvis.chat.pending_actions._execute._terminal_update", side_effect=RuntimeError("boom")),
		):
			try:
				pa.execute(name)
			except pa.ExecuteCrashed as e:
				seen.update(
					msg=str(e), tb=frappe.get_traceback(with_context=True), chained=not e.__suppress_context__
				)
		self.assertEqual(set(seen), {"msg", "tb", "chained"}, "a value-free ExecuteCrashed")
		self.assertFalse(seen["chained"], "raised from None: the inner error is not chained")
		logs = self.logged("execute_crashed")
		self.assertEqual(len(logs), 1)
		self.assertIn("boom", logs[0])
		for text in (seen["msg"], seen["tb"], *logs):
			self.assertNotIn("PA-SECRET-ARG", text)
		self.assertEqual(self.row(name).status, "Executing", "the reconciler owns the verdict")


class TestFailureCleanup(PendingActionTestMixin, FrappeTestCase):
	def _run(self, body, *, tool="add_comment", args=None):
		conv = self.make_conv()
		name = self.park(conv, tool=tool, args=args)
		with fake_dispatch(body) as calls, as_user(OWNER):
			out = pa.execute(name)
		return name, out, calls

	def _failed_audit_rows(self, tool="add_comment"):
		return frappe.get_all(AGENT_WRITE, filters={"actor": OWNER, "tool": tool}, pluck="outcome")

	def test_after_commit_work_of_a_failed_dispatch_never_fires(self):
		fired = []

		def body(tool, args):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test rolled back"}).insert(
				ignore_permissions=True
			)
			frappe.db.after_commit.add(lambda: fired.append("after_commit"))
			raise frappe.ValidationError("boom")

		name, out, _ = self._run(body)
		self.assertFalse(out["ok"])
		self.assertEqual((self.row(name).status, self.row(name).reason_code), ("Failed", "failed"))
		self.assertEqual(fired, [])
		self.assertFalse(frappe.db.exists("ToDo", {"description": "pa-test rolled back"}))
		self.assertEqual(self._failed_audit_rows(), ["failed"], "the failure audit row survives the rollback")

	def test_unexpected_exception_is_cleaned_up_and_audited(self):
		fired = []

		def body(tool, args):
			frappe.db.after_commit.add(lambda: fired.append("x"))
			raise RuntimeError("kaboom")

		name, out, _ = self._run(body)
		self.assertEqual((out["ok"], out["error"]["code"]), (False, "InternalError"))
		self.assertIn("was not saved", out["error"]["message"])
		self.assertEqual((self.row(name).status, self.row(name).reason_code), ("Failed", "failed"))
		self.assertEqual(fired, [])
		self.assertEqual(self._failed_audit_rows(), ["failed"])

	def test_crash_wording_follows_the_outcome(self):
		def crash(tool, args):
			raise RuntimeError("kaboom")

		def commit_then_crash(tool, args):
			frappe.db.commit()
			raise RuntimeError("kaboom")

		for body, tool, outcome in (
			(crash, "run_method", "unknown"),
			(crash, "call_connector", "unknown"),
			(commit_then_crash, "run_import", "partial"),
		):
			_name, out, _ = self._run(body, tool=tool, args={"doctype": "ToDo"})
			self.assertEqual(out["outcome"], outcome, tool)
			self.assertIn("may have partly run", out["error"]["message"], tool)
			self.assertNotIn("not saved", out["error"]["message"], tool)

	def test_webhook_queue_and_link_count_re_register_after_a_failed_dispatch(self):
		from collections import defaultdict

		from frappe.integrations.doctype.webhook import _add_webhook_to_queue, flush_webhook_execution_queue

		def body(tool, args):
			_add_webhook_to_queue(frappe._dict(name="wh-failed"), frappe._dict(name="doc-failed"))
			frappe.local._link_count = defaultdict(int)
			raise frappe.ValidationError("boom")

		self._run(body)
		self.assertFalse(getattr(frappe.local, "_webhook_queue", None))
		self.assertFalse(hasattr(frappe.local, "_link_count"), "a stale _link_count blocks re-registration")
		_add_webhook_to_queue(frappe._dict(name="wh-next"), frappe._dict(name="doc-next"))
		try:
			self.assertIn(flush_webhook_execution_queue, frappe.db.after_commit._functions)
		finally:
			frappe.db.rollback()
			frappe.local._webhook_queue = []

	def test_no_realtime_from_a_failed_dispatch_and_later_realtime_still_flows(self):
		def body(tool, args):
			frappe.publish_realtime("pa_failed_event", {"x": 1}, user=OWNER, after_commit=True)
			raise frappe.ValidationError("boom")

		with patch("frappe.realtime.emit_via_redis") as emit:
			self._run(body)
			self.assertNotIn("pa_failed_event", [c.args[0] for c in emit.call_args_list])
			frappe.publish_realtime("pa_later_event", {"x": 2}, user=OWNER, after_commit=True)
			frappe.db.commit()
			self.assertIn("pa_later_event", [c.args[0] for c in emit.call_args_list])

	def test_mid_dispatch_commit_then_failure_is_partial(self):
		def body(tool, args):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test committed"}).insert(
				ignore_permissions=True
			)
			frappe.db.commit()  # run_import's own mid-dispatch commit
			raise frappe.ValidationError("import failed")

		name, out, _ = self._run(body, tool="run_import", args={"doctype": "ToDo", "file_url": "/x.csv"})
		self.assertEqual((self.row(name).status, self.row(name).reason_code), ("Failed", "partial"))
		self.assertEqual(out["outcome"], "partial")
		self.assertTrue(frappe.db.exists("ToDo", {"description": "pa-test committed"}))

	def test_custom_field_ddl_then_failure_is_partial(self):
		fieldname = "pa_probe_field"

		def body(tool, args):
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": "ToDo",
					"fieldname": fieldname,
					"label": "PA Probe",
					"fieldtype": "Data",
				}
			).insert(ignore_permissions=True)
			raise frappe.ValidationError("after ddl")

		try:
			name, out, _ = self._run(body, tool="run_import", args={"doctype": "ToDo", "file_url": "/y.csv"})
			self.assertEqual(self.row(name).reason_code, "partial")
		finally:
			cf = frappe.db.get_value("Custom Field", {"dt": "ToDo", "fieldname": fieldname})
			if cf:
				frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
			frappe.db.commit()

	def test_mid_dispatch_rollback_then_commit_is_partial(self):
		def body(tool, args):
			frappe.db.rollback()
			frappe.db.commit()
			raise frappe.ValidationError("after rollback")

		name, _out, _ = self._run(body)
		self.assertEqual(self.row(name).reason_code, "partial")

	def test_connector_failure_shapes_read_unknown(self):
		def raised(tool, args):
			raise frappe.ValidationError("connector refused")

		name, out, _ = self._run(raised, tool="call_connector", args={"connector": "c1", "action": "a"})
		self.assertEqual((self.row(name).reason_code, out["outcome"]), ("failed", "unknown"))

		def inner(tool, args):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test inner kept"}).insert(
				ignore_permissions=True
			)
			return {"ok": False, "error": {"code": "transport_error", "message": "down"}}

		frappe.db.delete(AGENT_WRITE, {"actor": OWNER})
		frappe.db.commit()
		name, out, _ = self._run(inner, tool="call_connector", args={"connector": "c1", "action": "b"})
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code, out["outcome"]), ("Failed", "failed", "unknown"))
		self.assertTrue(
			frappe.db.exists("ToDo", {"description": "pa-test inner kept"}), "inner failure: no rollback"
		)
		self.assertEqual(
			self._failed_audit_rows("call_connector"), ["failed"], "its own audit row, not a duplicate"
		)

	def test_sentinel_is_disarmed_before_any_later_commit(self):
		from jarvis.tools._preview_sandbox import preview_sandbox

		# An earlier preview leaves ``commit`` as an instance attribute (the suite
		# order that made a class-level spy miss every commit): spy on the instance.
		with preview_sandbox():
			pass
		name = self.park(self.make_conv())
		seen = []
		db = frappe.local.db
		real_commit = type(db).commit

		def spying_commit(*a, **kw):
			seen.append([f.__name__ for f in db.before_commit._functions])
			return real_commit(db, *a, **kw)

		with fake_dispatch(), as_user(OWNER), patch.object(db, "commit", spying_commit):
			self.assertTrue(pa.execute(name)["ok"])
		after_claim = seen[2:]  # [0] = the pre-lock commit, [1] = the claim commit
		self.assertTrue(after_claim)
		self.assertFalse(any("_pa_commit_sentinel" in names for names in after_claim), seen)

	def test_failed_dispatch_error_log_has_no_arg_values(self):
		def body(tool, args):
			raise RuntimeError("unexpected")

		self._run(body)
		self.assertEqual(len(self.logged("dispatch_crashed")), 1)
		logs = frappe.get_all("Error Log", filters={"creation": [">=", self._pa_t0]}, pluck="error")
		for log in logs:
			self.assertNotIn("PA-SECRET-ARG", log or "")


class TestArmHook(PendingActionTestMixin, FrappeTestCase):
	def test_arm_runs_after_the_commit_and_never_changes_the_outcome(self):
		name = self.park(self.make_conv())
		applied = []

		def apply(row):
			applied.append(row.name)
			frappe.db.rollback()  # a lock timeout here must not undo the applied write
			raise RuntimeError("conversation lock timeout")

		arm = pa.ArmHook(refuse=lambda row: None, apply=apply)
		with fake_dispatch(), as_user(OWNER):
			out = pa.execute(name, arm=arm)
		self.assertTrue(out["ok"])
		self.assertEqual(applied, [name])
		self.assertEqual(self.row(name).status, "Executed")
		self.assertEqual(len(self.logged("arm_failed")), 1)

	def test_arm_refusal_consumes_nothing_and_failure_never_arms(self):
		name = self.park(self.make_conv())
		refusal = {"ok": False, "reason_code": "not_armed"}
		with fake_dispatch() as calls, as_user(OWNER):
			out = pa.execute(name, arm=pa.ArmHook(refuse=lambda row: refusal, apply=lambda row: None))
		self.assertEqual(out, refusal)
		self.assertEqual((calls, self.row(name).status), ([], "Pending"))
		applied = []

		def fail(tool, args):
			raise frappe.ValidationError("nope")

		with fake_dispatch(fail), as_user(OWNER):
			pa.execute(name, arm=pa.ArmHook(refuse=lambda row: None, apply=lambda row: applied.append(1)))
		self.assertEqual(applied, [])
