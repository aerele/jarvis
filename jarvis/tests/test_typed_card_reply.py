"""Typed yes/no on chat cards (PR-3b; decisions 6, 13, 14, 15; AC-U8/U17/U18).

A bare or sweep phrase binds only cards parked since the user's latest human
message; a number names any visible card. A typed rejection is two-phase: R0
snapshots (read-only) at the typed call site, R1 discards after the user row is
committed, so a refused send discards nothing and the veto note survives
``conv_doc.save()``. The message then still reaches the model. Every test sends
through the real ``send_message`` unless it says otherwise."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import actions_api, pending_confirm, pending_confirm_legacy
from jarvis.chat import api as chat_api
from jarvis.tests._pending_action_helpers import (
	AGENT_WRITE,
	CONV,
	MSG,
	OWNER,
	PendingActionTestMixin,
	as_user,
	fake_dispatch,
)
from jarvis.tests._transport_helpers import provision_legacy_site
from jarvis.tests.test_pending_confirm import _purge_legacy

TODO = {"doctype": "ToDo", "values": {"description": "pa-test typed reply"}}
PREVIEW = {"would": {"doctype": "ToDo"}, "card": {"title": "Create a ToDo", "fields": []}}


class _Base(PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		provision_legacy_site(self)
		_purge_legacy(OWNER)
		self.addCleanup(_purge_legacy, OWNER)
		self.dispatch_turn = self._patch("jarvis.chat.api._dispatch_turn", return_value=None)
		self.turn_machine = self._patch("jarvis.chat.admission.turn_machine_enabled", return_value=False)
		self.can_send = self._patch("jarvis.chat.api.validate_can_send", return_value=(True, None))
		self.addCleanup(frappe.flags.pop, "jarvis_pa_continuations", None)

	def _patch(self, target, **kw):
		p = patch(target, **kw)
		self.addCleanup(p.stop)
		return p.start()

	def mint(self, conv, **kw):
		kw.setdefault("tool", "create_doc")
		kw.setdefault("args", dict(TODO))
		kw.setdefault("preview", dict(PREVIEW))
		return pending_confirm.mint(conversation=conv, owner=OWNER, run_id="", **kw)

	def user_row(self, conv, *, origin="human", content="an earlier message", seq=None):
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": seq if seq is not None else chat_api._next_seq(conv),
				"role": "user",
				"content": content,
				"origin": origin,
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def send(self, conv, text, tokens=None, **kw):
		with as_user(OWNER):
			return chat_api.send_message(
				conv, text, approval_tokens=json.dumps(tokens) if tokens is not None else None, **kw
			)

	def notes(self, conv) -> list[str]:
		raw = frappe.db.get_value(CONV, conv, "pending_agent_notes")
		return [n["text"] for n in json.loads(raw)] if raw else []

	def user_rows(self, conv):
		return frappe.get_all(
			MSG,
			filters={"conversation": conv, "role": "user"},
			fields=["name", "content", "origin", "hidden"],
			order_by="seq asc",
		)

	def chip(self, token):
		return frappe.db.get_value(MSG, {"tool_call_id": token, "role": "tool"}, "action_outcome")


class TestTypedRejectionEndToEnd(_Base):
	def test_a_typed_no_discards_the_card_and_the_veto_survives_into_the_turn(self):
		"""AC-U17 end to end, ``agent_notes.append`` NOT mocked: R1 runs after
		``conv_doc.save()``, whose stale doc would otherwise wipe the note (A22)."""
		conv = self.make_conv()
		token = self.mint(conv)
		summary = self.row(token).summary
		res = self.send(conv, "no", [token])
		self.assertTrue(res["ok"], res)
		self.assertEqual(
			res["typed_rejection"],
			{"discarded": [{"token": token, "position": 1, "summary": summary}], "skipped": []},
		)
		self.assertNotIn("older_cards_waiting", res)
		row = self.row(token)
		self.assertEqual((row.status, row.reason_code, row.settled), ("Discarded", "discarded", 1))
		self.assertEqual(self.chip(token), "discarded")
		[note] = self.notes(conv)
		self.assertIn("typed reply `no` discarded", note)
		self.assertIn(f"`{summary}`", note)
		self.assertIn("NOT performed", note)
		self.assertIn("ask before re-proposing", note)
		self.assertNotIn("declined", note, "one combined note, not the per-card veto")
		# The same message still reaches the model as a human message.
		[user] = self.user_rows(conv)
		self.assertEqual((user.content, user.origin, user.hidden), ("no", "human", 0))
		self.assertEqual(self.dispatch_turn.call_args.args[0]["message_id"], user.name)
		self.assertTrue(frappe.db.exists(AGENT_WRITE, {"actor": OWNER, "outcome": "discarded"}))

	def test_qualified_mixed_and_erp_verb_replies_discard_nothing(self):
		for text in ("no, make it 5", "cancel it", "reject", "decline", "none", "stop", "no?"):
			with self.subTest(text=text):
				conv = self.make_conv()
				token = self.mint(conv)
				res = self.send(conv, text, [token])
				self.assertTrue(res["ok"], res)
				self.assertNotIn("typed_rejection", res)
				self.assertEqual(self.row(token).status, "Pending")
				self.assertEqual(self.notes(conv), [])
				self.assertEqual([u.content for u in self.user_rows(conv)], [text])

	def test_a_discard_ends_the_autorun_it_paused_in_both_stores(self):
		"""A6: the declined card ends the approved skill run AND a request-scoped
		"confirm all" (a legacy discard used to clear only the first)."""
		for flag in (0, 1):
			with self.subTest(flag=flag):
				self.flag(flag)
				conv = self.make_conv()
				token = self.mint(conv)
				frappe.db.set_value(
					CONV, conv, {"skill_autorun": 1, "request_autorun": 1}, update_modified=False
				)
				frappe.db.commit()
				with as_user(OWNER):
					res = actions_api._dismiss_core(token, conv, typed=True)
				self.assertEqual(res["data"]["status"], "discarded")
				flags = frappe.db.get_value(CONV, conv, ["skill_autorun", "request_autorun"], as_dict=True)
				self.assertEqual((flags.skill_autorun, flags.request_autorun), (0, 0))
				self.assertEqual(self.notes(conv), [], "a typed discard queues no per-card note")

	def test_a_legacy_card_is_discarded_during_dual_read(self):
		conv = self.make_conv()
		self.flag(0)
		token = self.mint(conv)
		self.assertIsNotNone(pending_confirm_legacy.peek(token))
		# The gate writes a legacy card's display row (its seq is what makes it recent).
		api.persist_pending_action(
			conv, "create_doc", PREVIEW, token, pending_confirm_legacy.peek(token)["expires_at"]
		)
		res = self.send(conv, "nope", [token])
		self.assertEqual([d["token"] for d in res["typed_rejection"]["discarded"]], [token])
		self.assertIsNone(pending_confirm_legacy.peek(token))
		self.assertEqual(self.chip(token), "discarded")
		[note] = self.notes(conv)
		self.assertIn("typed reply `nope` discarded", note)

	def flag(self, value):
		p = patch.dict(frappe.conf, {pending_confirm.FLAG: value})
		p.start()
		self.addCleanup(p.stop)


class TestTypedScope(_Base):
	"""Decision 6 on every client path; sized to single-flight (one card per chat)."""

	def test_a_bare_or_sweep_phrase_binds_only_a_recent_card(self):
		for text in ("no", "discard all", "nevermind"):
			with self.subTest(text=text):
				conv = self.make_conv()
				token = self.mint(conv)
				self.user_row(conv)
				res = self.send(conv, text, [token])
				self.assertEqual(self.row(token).status, "Pending")
				self.assertEqual(res["typed_rejection"], {"discarded": [], "skipped": []})
				self.assertEqual(res["older_cards_waiting"], 1)
				[note] = self.notes(conv)
				self.assertIn("discarded no action card; 1 earlier card(s)", note)

	def test_a_number_names_a_card_of_any_age(self):
		for text in ("discard 1", "skip 1", "drop 1"):
			with self.subTest(text=text):
				conv = self.make_conv()
				token = self.mint(conv)
				self.user_row(conv)
				self.send(conv, text, [token])
				self.assertEqual(self.row(token).status, "Discarded")

	def test_an_out_of_range_number_passes_through(self):
		conv = self.make_conv()
		token = self.mint(conv)
		res = self.send(conv, "discard 4", [token])
		self.assertTrue(res["ok"])
		self.assertEqual(res["typed_rejection"]["discarded"], [])
		self.assertEqual(res["older_cards_waiting"], 1)
		self.assertEqual(self.row(token).status, "Pending")
		self.assertEqual([u.content for u in self.user_rows(conv)], ["discard 4"])

	def test_without_tokens_only_an_explicit_sweep_discards_brake_items_included(self):
		conv = self.make_conv()
		todo = self.make_todo()
		token = self.mint(conv, tool="delete_doc", args={"doctype": "ToDo", "name": todo})
		self.send(conv, "go ahead", None)
		self.assertEqual(self.row(token).status, "Pending", "an unseen brake card is never approved")
		conv2 = self.make_conv()
		token2 = self.mint(conv2, tool="delete_doc", args={"doctype": "ToDo", "name": todo})
		res = self.send(conv2, "no", None)
		self.assertEqual(self.row(token2).status, "Pending", "a bare no may not have seen it")
		self.assertEqual(res["older_cards_waiting"], 1)
		conv3 = self.make_conv()
		token3 = self.mint(conv3, tool="delete_doc", args={"doctype": "ToDo", "name": todo})
		self.send(conv3, "discard all", None)
		self.assertEqual(self.row(token3).status, "Discarded")

	def test_a_no_token_sweep_skips_an_older_card(self):
		conv = self.make_conv()
		token = self.mint(conv)
		self.user_row(conv)
		self.send(conv, "discard all", None)
		self.assertEqual(self.row(token).status, "Pending")

	def test_a_stale_bundle_token_discards_nothing(self):
		conv = self.make_conv()
		token = self.mint(conv)
		res = self.send(conv, "no", ["zz-gone-token"])
		self.assertEqual(self.row(token).status, "Pending")
		self.assertEqual(res["typed_rejection"]["discarded"], [])
		self.assertEqual(res["older_cards_waiting"], 1)

	def test_a_conversationless_card_is_never_typed_bound(self):
		"""D9: an F1 card surfaces under any filter; the typed paths stay strict."""
		conv = self.make_conv()
		f1 = pending_confirm_legacy.mint(
			conversation="", owner=OWNER, tool="create_doc", args=dict(TODO), run_id=""
		)
		self.assertIn(f1, [c["token"] for c in pending_confirm.list_for_owner(OWNER, conv)])
		for text, tokens in (("discard all", None), ("no", [f1]), ("discard 1", [f1])):
			res = self.send(conv, text, tokens)
			self.assertTrue(res["ok"], res)
		self.assertIsNotNone(pending_confirm_legacy.peek(f1))

	def test_an_unset_origin_row_counts_as_the_user_speaking(self):
		"""A row older than the origin column is conservatively human for the typed
		scope: the card is older, so a bare no discards nothing."""
		conv = self.make_conv()
		token = self.mint(conv)
		self.user_row(conv, origin="")
		self.send(conv, "no", [token])
		self.assertEqual(self.row(token).status, "Pending")

	def test_the_recency_cut_is_strict(self):
		conv = self.make_conv()
		token = self.mint(conv)
		card_seq = frappe.db.get_value(MSG, {"tool_call_id": token}, "seq")
		self.user_row(conv, seq=card_seq)
		[item] = pending_confirm.list_items_for_owner(OWNER, conv)
		self.assertEqual((item["seq"], item["recent"]), (card_seq, False))

	def test_automatic_rows_do_not_age_a_card(self):
		conv = self.make_conv()
		token = self.mint(conv)
		for origin in ("continuation", "macro", "file_box", "agent", "system", "delegated"):
			self.user_row(conv, origin=origin)
		self.send(conv, "no", [token])
		self.assertEqual(self.row(token).status, "Discarded")

	def test_get_conversation_marks_the_pending_rows_recency(self):
		conv = self.make_conv()
		token = self.mint(conv)
		with as_user(OWNER):
			[row] = [m for m in chat_api.get_conversation(conv)["messages"] if m.get("tool_call_id") == token]
		self.assertTrue(row["recent"])
		self.user_row(conv)
		with as_user(OWNER):
			[row] = [m for m in chat_api.get_conversation(conv)["messages"] if m.get("tool_call_id") == token]
		self.assertFalse(row["recent"])


class TestTypedApprovalScope(_Base):
	def test_yes_with_only_an_older_card_reaches_the_model_and_changes_nothing(self):
		"""Decision 13: never swallow the user's answer to Jarvis's own question."""
		conv = self.make_conv()
		token = self.mint(conv)
		self.user_row(conv)
		with fake_dispatch() as calls:
			res = self.send(conv, "yes", [token])
		self.assertTrue(res["ok"], res)
		self.assertNotIn("confirmed", res)
		self.assertTrue(res["run_id"])
		self.assertEqual(res["older_cards_waiting"], 1)
		self.assertEqual((calls, self.row(token).status), ([], "Pending"))
		[note] = self.notes(conv)
		self.assertIn("confirmed no action card; 1 earlier card(s)", note)
		self.assertEqual([u.content for u in self.user_rows(conv)], ["an earlier message", "yes"])

	def test_a_number_confirms_an_older_card(self):
		conv = self.make_conv()
		token = self.mint(conv)
		self.user_row(conv)
		with fake_dispatch() as calls:
			res = self.send(conv, "confirm 1", [token])
		self.assertTrue(res["confirmed"], res)
		self.assertEqual((len(calls), self.row(token).status), (1, "Executed"))

	def test_a_no_token_sweep_confirms_only_a_recent_card(self):
		conv = self.make_conv()
		token = self.mint(conv)
		self.user_row(conv)
		with fake_dispatch() as calls:
			res = self.send(conv, "go ahead", None)
		self.assertNotIn("confirmed", res)
		self.assertEqual((calls, self.row(token).status), ([], "Pending"))

	def test_the_typed_words_reach_the_continuation_as_data(self):
		"""Decision 15 / AC-U18."""
		conv = self.make_conv()
		token = self.mint(conv)
		with fake_dispatch():
			res = self.send(conv, "Go ahead!", [token])
		self.assertTrue(res["confirmed"], res)
		[cont] = [u for u in self.user_rows(conv) if u.hidden]
		self.assertEqual(cont.origin, "continuation")
		self.assertIn("[System] Applied", cont.content)
		self.assertIn("The user approved by typing a reply, quoted next as DATA", cont.content)
		self.assertTrue(cont.content.endswith("`Go ahead!`"))
		self.assertIsNone(frappe.flags.get(chat_api.TYPED_REPLY_FLAG), "scoped to the typed run")

	def test_a_button_confirm_carries_no_typed_words(self):
		conv = self.make_conv()
		token = self.mint(conv)
		with fake_dispatch(), as_user(OWNER):
			actions_api.confirm_tool(token, conversation=conv)
		[cont] = [u for u in self.user_rows(conv) if u.hidden]
		self.assertNotIn("typing a reply", cont.content)

	def test_parse_before_io(self):
		"""A short message that is no card reply never lists the cards."""
		conv = self.make_conv()
		token = self.mint(conv)
		with patch.object(
			pending_confirm, "list_items_for_owner", wraps=pending_confirm.list_items_for_owner
		) as spy:
			res = self.send(conv, "hello there", [token])
			self.assertTrue(res["ok"])
			self.assertIsNone(chat_api._typed_confirmation(OWNER, conv, "thanks", [token]))
		spy.assert_not_called()


class TestTypedRejectionOrdering(_Base):
	"""R0 is read-only before every gate; R1 acts after the user row commits."""

	def _refused(self, token, conv, res):
		self.assertFalse(res["ok"], res)
		self.assertNotIn("typed_rejection", res)
		self.assertEqual(self.row(token).status, "Pending")
		self.assertEqual(self.notes(conv), [])
		self.assertEqual(self.user_rows(conv), [])

	def test_every_refused_send_discards_nothing(self):
		cases = {
			"busy": ({"jarvis.chat.api._conversation_busy": True}, {}),
			"compaction": ({"jarvis.chat.api._compacting_reject": {"ok": False, "reason": "compacting"}}, {}),
			"overload": (
				{
					"jarvis.chat.admission.turn_machine_enabled": True,
					"jarvis.chat.admission.shard_overloaded": True,
				},
				{},
			),
			"bad model": ({}, {"model_override": "zz-no-such-model"}),
			"bad thinking": ({}, {"thinking_override": "zz-no-such-level"}),
		}
		for label, (patches, kw) in cases.items():
			with self.subTest(label):
				conv = self.make_conv()
				token = self.mint(conv)
				active = [patch(t, return_value=v) for t, v in patches.items()]
				for p in active:
					p.start()
				try:
					res = self.send(conv, "no", [token], **kw)
				finally:
					for p in active:
						p.stop()
				self._refused(token, conv, res)

	def test_a_per_model_cap_refusal_discards_nothing(self):
		conv = self.make_conv()
		token = self.mint(conv)
		self.can_send.side_effect = [(True, None), (False, "usage_limit")]
		res = self.send(conv, "no", [token])
		self._refused(token, conv, res)

	def test_the_locked_overload_race_keeps_the_discard_and_says_so(self):
		"""Residual: admission deletes the seed row after R1; the discard and its note
		stand (the note rides the next turn) and the refusal carries typed_rejection."""
		conv = self.make_conv()
		token = self.mint(conv)
		self.turn_machine.return_value = True
		with (
			patch("jarvis.chat.admission.shard_overloaded", return_value=False),
			patch(
				"jarvis.chat.admission.accept_or_queue",
				return_value={"ok": False, "overloaded": True, "reason": "busy"},
			),
		):
			res = self.send(conv, "no", [token])
		self.assertFalse(res["ok"])
		self.assertEqual([d["token"] for d in res["typed_rejection"]["discarded"]], [token])
		self.assertEqual(self.row(token).status, "Discarded")
		self.assertEqual(self.user_rows(conv), [], "the seed row is cleaned up")
		self.assertEqual(len(self.notes(conv)), 1)

	def test_an_r1_failure_still_dispatches_the_turn(self):
		for target in ("jarvis.chat.actions_api._dismiss_core", "jarvis.chat.agent_notes.append"):
			with self.subTest(target=target):
				conv = self.make_conv()
				token = self.mint(conv)
				with patch(target, side_effect=RuntimeError("r1 blew up")), patch.object(frappe, "log_error"):
					res = self.send(conv, "no", [token])
				self.assertTrue(res["ok"], res)
				[user] = self.user_rows(conv)
				self.assertEqual(self.dispatch_turn.call_args.args[0]["message_id"], user.name)

	def test_a_card_parked_after_the_snapshot_is_untouched(self):
		"""The running turn parks its card between R0 and R1 (here: inside a gate)."""
		conv = self.make_conv()
		parked = []

		def _park_meanwhile(_conv):
			parked.append(self.mint(conv))
			return None

		with patch("jarvis.chat.api._compacting_reject", side_effect=_park_meanwhile):
			res = self.send(conv, "discard all", None)
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.row(parked[0]).status, "Pending")
		self.assertEqual(self.notes(conv), [])

	def test_a_typed_no_and_a_confirm_click_have_exactly_one_winner(self):
		conv = self.make_conv()
		token = self.mint(conv)
		snap = chat_api._select_typed_rejection(OWNER, conv, "no", json.dumps([token]))
		self.assertEqual([d["token"] for d in snap["discard"]], [token])
		frappe.db.commit()
		site = frappe.local.site
		barrier = threading.Barrier(2)
		results, errors = {}, []

		def worker(label):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user(OWNER)
			try:
				barrier.wait(timeout=10)
				if label == "typed":
					results[label] = chat_api._apply_typed_reply(conv, snap)
				else:
					results[label] = actions_api.confirm_tool(token, conversation=conv)
			except Exception as e:  # pragma: no cover
				errors.append(e)
			finally:
				try:
					frappe.db.commit()
				finally:
					frappe.destroy()

		with fake_dispatch() as calls:
			threads = [threading.Thread(target=worker, args=(x,)) for x in ("typed", "click")]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=30)
		self.assertEqual(errors, [])
		status = self.row(token).status
		discarded = bool(results["typed"]["typed_rejection"]["discarded"])
		confirmed = bool(results["click"].get("ok"))
		self.assertEqual(sorted([discarded, confirmed]), [False, True], results)
		self.assertEqual(status, "Discarded" if discarded else "Executed")
		self.assertEqual(len(calls), 0 if discarded else 1)


class TestLivePushRecency(_Base):
	def test_the_action_pending_push_carries_seq_and_recent(self):
		conv = self.make_conv("Administrator")  # ToDo create rights for the park's dry-run
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.delete, MSG, {"conversation": conv})
		self.addCleanup(frappe.db.delete, CONV, {"name": conv})
		self.addCleanup(frappe.db.delete, "Jarvis Pending Action", {"conversation": conv})
		with patch("jarvis.chat.events.publish_to_user") as push, as_user("Administrator"):
			res = api._run_tool("create_doc", dict(TODO), conversation=conv)
		self.assertEqual(res.get("data", {}).get("status"), "pending_confirmation", res)
		[payload] = [c.args[1] for c in push.call_args_list if c.args[1].get("kind") == "action:pending"]
		self.assertIsNone(payload["expires_at"])
		self.assertTrue(payload["recent"])
		self.assertEqual(payload["seq"], frappe.db.get_value(MSG, {"tool_call_id": payload["token"]}, "seq"))
