"""Tests for the write-safety confirmation gate (issue #186).

The gate parks every mutating tool call in ``_GATED_WRITES`` instead of
running it: ``_run_tool`` returns ``status: pending_confirmation`` and mints a
single-use token in ``pending_confirm``. Only ``confirm_tool`` - a human
cookie-session endpoint - can then execute the stored call, owner-bound and
single-use. Non-gated writes still execute immediately.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.actions_api import confirm_tool
from jarvis.tests._transport_helpers import provision_legacy_site


def _spy_mint():
	"""Return (patcher-context, captured-dict). The captured dict's ``token``
	key holds the token that the gate minted, since the model-facing return
	deliberately never carries it."""
	captured = {}
	real = pending_confirm.mint

	def spy(**kwargs):
		token = real(**kwargs)
		captured["token"] = token
		captured["kwargs"] = kwargs
		return token

	return patch("jarvis.chat.pending_confirm.mint", side_effect=spy), captured


class TestGateParks(FrappeTestCase):
	def test_gated_create_with_no_token_parks(self):
		desc = "jarvis-test-gate-park-001"
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		# Non-executing pending status, model-facing.
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertEqual(r["data"]["tool"], "create_doc")
		# Nothing was written.
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))
		# A token was minted and is retrievable from the store...
		token = captured["token"]
		self.assertIsNotNone(pending_confirm.peek(token))
		# ...but it is NEVER in the model-facing return dict.
		self.assertNotIn(token, frappe.as_json(r))

	def test_gated_create_pending_preview_is_sandboxed_shape(self):
		desc = "jarvis-test-gate-preview-002"
		patcher, _ = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		preview = r["data"]["preview"]
		# Previewable tool -> the sandboxed _run_preview shape.
		self.assertTrue(preview["preview"])
		self.assertIn("would", preview)
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

	def test_send_email_parks_described_not_sent(self):
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool(
				"send_email",
				{
					"recipients": "nobody@example.com",
					"subject": "hi",
					"content": "body",
				},
			)
			# The gate parks BEFORE any dispatch: send_email never fired.
			self.assertFalse(disp.called)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		preview = r["data"]["preview"]
		# send_email is not a dry run - a described intent, not a sandboxed one.
		self.assertFalse(preview["preview"])
		self.assertTrue(preview["described"])
		self.assertIn("summary", preview)
		self.assertIsNotNone(captured.get("token"))


class TestShareAssignGatedWrites(FrappeTestCase):
	"""F17/F20 (share_doc) + F23 (assign_to): their own docstrings/descriptors
	say "ALWAYS confirm" (share_doc: re-share/everyone=true is a permission
	escalation; assign_to: a person gets a notification email) but neither
	tool was in _GATED_WRITES, so a model-path call fired immediately with no
	human confirmation - the exact prompt-injection escalation risk their own
	authors documented as requiring a gate. Both now park like send_email:
	described-intent preview, never sandbox-executed."""

	def test_share_doc_and_assign_to_are_gated_not_auto_applyable(self):
		self.assertIn("share_doc", api._GATED_WRITES)
		self.assertIn("assign_to", api._GATED_WRITES)
		self.assertNotIn("share_doc", api._AUTO_APPLYABLE)
		self.assertNotIn("assign_to", api._AUTO_APPLYABLE)

	def test_share_doc_parks_described_and_does_not_execute(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-test-share-gate-target",
			}
		).insert(ignore_permissions=True)
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool(
				"share_doc",
				{
					"doctype": "ToDo",
					"name": todo.name,
					"user": "jarvis-share-gate-target@example.com",
					"share": True,
				},
			)
			# The gate parks BEFORE any dispatch: share_doc never fired.
			self.assertFalse(disp.called)
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertEqual(r["data"]["tool"], "share_doc")
		preview = r["data"]["preview"]
		# Not previewable (no side-effect-free sandbox dry-run for a share
		# grant) - described intent, like send_email.
		self.assertFalse(preview["preview"])
		self.assertTrue(preview["described"])
		self.assertIn("summary", preview)
		self.assertIsNotNone(captured.get("token"))
		self.assertFalse(
			frappe.db.exists(
				"DocShare",
				{
					"share_doctype": "ToDo",
					"share_name": todo.name,
					"user": "jarvis-share-gate-target@example.com",
				},
			)
		)

	def test_assign_to_parks_described_and_does_not_execute(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-test-assign-gate-target",
			}
		).insert(ignore_permissions=True)
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool(
				"assign_to",
				{
					"doctype": "ToDo",
					"name": todo.name,
					"user": "Administrator",
				},
			)
			# The gate parks BEFORE any dispatch: assign_to never fired, so no
			# ToDo/notification is created for the confirmation-less call.
			self.assertFalse(disp.called)
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertEqual(r["data"]["tool"], "assign_to")
		preview = r["data"]["preview"]
		self.assertFalse(preview["preview"])
		self.assertTrue(preview["described"])
		self.assertIn("summary", preview)
		self.assertIsNotNone(captured.get("token"))


class TestDryRunOnParkInvariant(FrappeTestCase):
	"""api.py's comment above _DRY_RUN_ON_PARK documents an invariant: every
	tool dry-run at park time must also be _PREVIEWABLE, or the LIVE park
	path (a real sandboxed dry-run) and the RESYNC path (_pending_preview,
	which routes purely on _PREVIEWABLE membership) diverge - a reload/
	reconnect would silently degrade a real dry-run card to a blind
	described-intent one (see test_create_docs_resync_preview_is_dry_run_
	not_described below for the concrete failure mode this guards against).
	This asserts the invariant holds as a standing regression guard, since
	nothing else in the module enforces it structurally."""

	def test_dry_run_on_park_is_subset_of_previewable(self):
		self.assertTrue(api._DRY_RUN_ON_PARK.issubset(api._PREVIEWABLE))


class TestGatePreValidatesBeforePark(FrappeTestCase):
	"""A dry-runnable gated write is validated in the sandbox BEFORE the
	confirmation card is minted. A deterministic failure (e.g. a missing
	mandatory field) is returned to the model immediately instead of parking a
	card that would die on click - the confirmed write is the SAME call as the
	same user, so a preview failure is a faithful predictor."""

	def test_gated_create_missing_mandatory_returns_error_not_park(self):
		# ToDo.description is mandatory. Pass a non-empty values dict that omits
		# it (an empty dict is refused earlier by _validate_create_args).
		patcher, captured = _spy_mint()
		with patch("jarvis.chat.events.publish_to_user") as pub, patcher:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"priority": "Medium"},
				},
			)
			# No confirmation card was published either.
			self.assertFalse(pub.called)
		# Model-facing validation error, NOT the park shape.
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIn("description", r["error"]["message"].lower())
		# Did NOT park: no token minted (and the error envelope + no publish
		# above prove nothing was parked or executed).
		self.assertIsNone(captured.get("token"))

	def test_valid_gated_create_still_parks(self):
		# Regression guard: a preview that VALIDATES cleanly still parks a card
		# with the sandboxed dry-run shape and mints a token.
		desc = "jarvis-test-gate-prevalidate-valid-011"
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertTrue(r["data"]["preview"]["preview"])
		self.assertIn("would", r["data"]["preview"])
		self.assertIsNotNone(captured.get("token"))
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))


class TestNonGatedWriteRunsImmediately(FrappeTestCase):
	def test_add_comment_executes_immediately(self):
		# add_comment is a write but NOT gated - it must run inline, no park.
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "jarvis-test-nongated-target",
			}
		).insert(ignore_permissions=True)
		r = api._run_tool(
			"add_comment",
			{
				"doctype": "ToDo",
				"name": todo.name,
				"content": "inline note",
			},
		)
		self.assertTrue(r["ok"])
		# Ran, did not park.
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertTrue(
			frappe.db.exists("Comment", {"reference_doctype": "ToDo", "reference_name": todo.name})
		)


class TestConfirmTool(FrappeTestCase):
	def _park(self, tool, args):
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(tool, args)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		return captured["token"]

	def test_confirm_executes_and_is_single_use(self):
		desc = "jarvis-test-confirm-create-003"
		token = self._park(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": desc},
			},
		)
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

		res = confirm_tool(token)
		self.assertTrue(res["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

		# Single use: the same token cannot execute again.
		again = confirm_tool(token)
		self.assertFalse(again["ok"])
		self.assertEqual(again["error"]["type"], "InvalidConfirmation")

	def test_confirm_by_wrong_owner_rejected_and_does_not_burn_token(self):
		desc = "jarvis-test-confirm-owner-004"
		# Parked as Administrator (the test session user).
		token = self._park(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": desc},
			},
		)

		other = "jarvis-confirm-other@example.com"
		if not frappe.db.exists("User", other):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": other,
					"first_name": "Other",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		# confirm_tool is @require_jarvis_user-gated; a realistic non-owner caller
		# holds the role, so the owner-mismatch (not the role gate) is what rejects.
		if "Jarvis User" not in set(frappe.get_roles(other)):
			frappe.get_doc("User", other).add_roles("Jarvis User")

		original = frappe.session.user
		frappe.set_user(other)
		try:
			res = confirm_tool(token)
		finally:
			frappe.set_user(original)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["type"], "InvalidConfirmation")
		# Token was NOT burned by the wrong-owner attempt.
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

		# The real owner can still confirm.
		ok = confirm_tool(token)
		self.assertTrue(ok["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

	def test_confirm_rejects_guest(self):
		token = self._park(
			"create_doc",
			{
				"doctype": "ToDo",
				"values": {"description": "jarvis-test-guest-005"},
			},
		)
		original = frappe.session.user
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				confirm_tool(token)
		finally:
			frappe.set_user(original)

	def test_confirm_unknown_token_is_invalid(self):
		res = confirm_tool("no-such-token-zzz")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["type"], "InvalidConfirmation")


class TestGatedToolRefusesModelPreview(FrappeTestCase):
	"""Fix #14: a gated write called with ``preview=True`` must NOT take the
	model-facing preview branch (a dry-run that still fires inline non-DB hook
	side effects) AND must NOT silently park. It returns an informative
	InvalidArgumentError so a transition-window model that used preview to
	dry-run gets a legible signal instead of a premature pending card. It does
	not park (no token minted) and does not execute."""

	def test_gated_create_with_preview_true_returns_error_not_park(self):
		desc = "jarvis-test-gate-preview-bypass-010"
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
					"preview": True,
				},
			)
		# Informative error, NOT the park shape and NOT the dry-run shape.
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIn("preview is not needed", r["error"]["message"])
		# Did NOT park: no token minted, nothing written.
		self.assertIsNone(captured.get("token"))
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

	def test_gated_run_method_with_preview_true_returns_error_and_does_not_execute(self):
		# run_method is gated + previewable. With preview=True it must return the
		# informative error without parking or dispatching. dispatch is patched,
		# so any execution would be visible.
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool(
				"run_method",
				{
					"method": "frappe.ping",
					"preview": True,
				},
			)
			self.assertFalse(disp.called)
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIsNone(captured.get("token"))


class TestRunMethodParkDoesNotSandboxExecute(FrappeTestCase):
	"""Fix 2: run_method is _PREVIEWABLE, but parking one must NOT sandbox-
	execute the target method to build its preview - the sandbox only rolls
	back DB writes, so a method's inline non-DB side effects (HTTP/email) would
	fire unconfirmed and its result would leak to the model. run_method parks
	with a described-intent preview (never executed at park time); the real
	call runs exactly once, only on confirm."""

	def test_run_method_parks_described_and_not_executed_at_park(self):
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool("run_method", {"method": "frappe.ping"})
			# No sandbox execution at park time: dispatch is never touched.
			self.assertFalse(disp.called)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		preview = r["data"]["preview"]
		# Described intent, explicitly NOT a dry run (no sandboxed "would").
		self.assertFalse(preview["preview"])
		self.assertTrue(preview["described"])
		self.assertIn("summary", preview)
		self.assertNotIn("would", preview)
		self.assertIsNotNone(captured.get("token"))

	def test_confirm_executes_run_method_exactly_once(self):
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool("run_method", {"method": "frappe.ping"})
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		token = captured["token"]
		# The method runs for the first and only time on confirm.
		with patch("jarvis.api.dispatch", return_value={"message": "pong"}) as disp:
			res = confirm_tool(token)
		self.assertTrue(res["ok"])
		self.assertEqual(disp.call_count, 1)
		self.assertEqual(disp.call_args.args[0], "run_method")


class TestConfirmSelfHostOwnerBinding(FrappeTestCase):
	"""#1/#5/#6: in self-host the gate binds the token to the CONVERSATION
	OWNER (the operator whose browser is subscribed), NOT the restricted tool
	user the model path runs as. The operator confirms from their own browser
	session (== the owner), and the confirmed write EXECUTES as the stored
	``exec_user`` (the tool user) so a confirm never exceeds the model path's
	scope. Managed mode is unchanged (owner == exec_user)."""

	_TOOL_USER = "jarvis-selfhost-tool@example.com"

	def _ensure_user(self, email):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "SelfHost",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		return email

	def test_selfhost_confirm_by_owner_executes_as_exec_user(self):
		# The gate binds owner=operator (the browser session, Administrator here)
		# and exec_user=tool_user. The operator confirms from their own session;
		# the write dispatches under the tool user, not the browser session (#6).
		tool_user = self._ensure_user(self._TOOL_USER)
		operator = frappe.session.user  # browser session == conversation owner
		token = pending_confirm.mint(
			conversation="",
			owner=operator,
			exec_user=tool_user,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "sh-020"}},
			run_id="",
		)

		acting = {}

		def _spy_dispatch(tool, args):
			acting["user"] = frappe.session.user
			return {"name": "TODO-FAKE"}

		# Snapshot the browser cookie session's sid + data BEFORE confirm. A bare
		# frappe.set_user(exec_user) would gut both and end-of-request
		# Session.update would poison this sid's Redis cache -> the operator gets
		# logged out. impersonate must leave sid + data untouched.
		before_sid = frappe.session.sid
		before_data = frappe.session.data
		frappe.session.data.csrf_token = "SENTINEL-SH-020"

		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=True),
			patch("jarvis.api._selfhost_tool_user", return_value=tool_user),
			patch("jarvis.api.dispatch", side_effect=_spy_dispatch),
		):
			res = confirm_tool(token)
		self.assertTrue(res["ok"])
		# #6: executed under the scoped tool user, not the browser-session owner.
		self.assertEqual(acting["user"], tool_user)
		# The confirming session user is restored after dispatch.
		self.assertEqual(frappe.session.user, operator)
		# The browser cookie session survives: sid + data (incl. the sentinel)
		# are the ORIGINAL objects, so end-of-request Session.update re-persists
		# the operator's real session instead of logging them out.
		self.assertEqual(frappe.session.sid, before_sid)
		self.assertIs(frappe.session.data, before_data)
		self.assertEqual(frappe.session.data.csrf_token, "SENTINEL-SH-020")
		# Single use.
		again = confirm_tool(token)
		self.assertFalse(again["ok"])

	def test_selfhost_confirm_still_rejects_guest(self):
		tool_user = self._ensure_user(self._TOOL_USER)
		operator = frappe.session.user
		token = pending_confirm.mint(
			conversation="",
			owner=operator,
			exec_user=tool_user,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "x-021"}},
			run_id="",
		)
		original = frappe.session.user
		frappe.set_user("Guest")
		try:
			with (
				patch("jarvis.selfhost.is_self_hosted", return_value=True),
				patch("jarvis.api._selfhost_tool_user", return_value=tool_user),
				self.assertRaises(frappe.PermissionError),
			):
				confirm_tool(token)
		finally:
			frappe.set_user(original)
		# Guest was rejected before consume, so the token is NOT burned.
		self.assertIsNotNone(pending_confirm.peek(token))

	def test_managed_confirm_owner_is_session_user_unchanged(self):
		# Managed mode: owner must equal the confirming session user.
		desc_ok = "jarvis-test-managed-owner-ok-022"
		desc_bad = "jarvis-test-managed-owner-bad-022"
		session_user = frappe.session.user  # Administrator in tests
		with patch("jarvis.selfhost.is_self_hosted", return_value=False):
			# Token minted under the session user -> confirms + executes.
			ok_token = pending_confirm.mint(
				conversation="",
				owner=session_user,
				tool="create_doc",
				args={"doctype": "ToDo", "values": {"description": desc_ok}},
				run_id="",
			)
			# Managed mode: owner == exec == session user, so confirm_tool's
			# impersonate no-ops - but the cookie session must STILL survive
			# intact (a bare unconditional frappe.set_user would gut it even for
			# the same user and log the operator out).
			before_sid = frappe.session.sid
			before_data = frappe.session.data
			frappe.session.data.csrf_token = "SENTINEL-MANAGED-022"
			res = confirm_tool(ok_token)
			self.assertTrue(res["ok"])
			self.assertTrue(frappe.db.exists("ToDo", {"description": desc_ok}))
			self.assertEqual(frappe.session.sid, before_sid)
			self.assertIs(frappe.session.data, before_data)
			self.assertEqual(frappe.session.data.csrf_token, "SENTINEL-MANAGED-022")

			# Token minted under a DIFFERENT owner -> rejected, not executed.
			bad_token = pending_confirm.mint(
				conversation="",
				owner="someone-else@example.com",
				tool="create_doc",
				args={"doctype": "ToDo", "values": {"description": desc_bad}},
				run_id="",
			)
			res = confirm_tool(bad_token)
			self.assertFalse(res["ok"])
			self.assertEqual(res["error"]["type"], "InvalidConfirmation")
			self.assertFalse(frappe.db.exists("ToDo", {"description": desc_bad}))


CONV = "Jarvis Conversation"


def _make_conv(owner: str) -> str:
	"""Create a Jarvis Conversation owned by ``owner`` and return its name."""
	orig = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc({"doctype": CONV, "title": "confirm-gate test"})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	finally:
		frappe.set_user(orig)


class TestConfirmedWriteReceipt(FrappeTestCase):
	"""#7: a confirmed write must leave a transcript receipt (a role=tool Jarvis
	Chat Message) in the conversation, the same way the inline model-write path
	does, so a confirmed delete/submit/email shows on reload."""

	def tearDown(self):
		for name in frappe.get_all(CONV, filters={"title": "confirm-gate test"}, pluck="name"):
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_confirmed_create_persists_tool_receipt(self):
		from jarvis.chat.api import get_conversation

		owner = frappe.session.user  # Administrator
		conv = _make_conv(owner)
		desc = "jarvis-test-confirm-receipt-040"
		token = pending_confirm.mint(
			conversation=conv,
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)

		res = confirm_tool(token)
		self.assertTrue(res["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

		# The receipt is visible via get_conversation as a role=tool message.
		msgs = get_conversation(conv)["messages"]
		tool_msgs = [m for m in msgs if m["role"] == "tool" and m["tool_name"] == "create_doc"]
		self.assertEqual(len(tool_msgs), 1)
		self.assertEqual(tool_msgs[0]["tool_status"], "completed")


class TestRealConversationGuard(FrappeTestCase):
	"""#11: confirm_tool accepts the conversation the click came from and passes
	it into consume as a REAL check. A mismatched conversation is rejected; the
	matching one succeeds; when omitted, owner + single-use still guard."""

	def tearDown(self):
		# These tests execute create_doc(ToDo) writes on the matching-confirm path but never
		# cleaned up the rows — so on a re-run/dirty site the "nothing executed" assertion saw a
		# stale ToDo and failed (non-idempotent). Clean the fixture ToDos so re-runs are stable.
		for name in frappe.get_all(
			"ToDo", filters={"description": ["like", "jarvis-test-confirm-convguard-%"]}, pluck="name"
		):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_mismatched_conversation_rejected_and_token_not_burned(self):
		owner = frappe.session.user
		desc = "jarvis-test-confirm-convguard-050"
		token = pending_confirm.mint(
			conversation="conv-real",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)
		# Wrong conversation -> rejected, nothing executes, token still lives.
		res = confirm_tool(token, conversation="conv-other")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["type"], "InvalidConfirmation")
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))
		self.assertIsNotNone(pending_confirm.peek(token))
		# Matching conversation -> succeeds.
		res = confirm_tool(token, conversation="conv-real")
		self.assertTrue(res["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

	def test_no_conversation_arg_falls_back_to_owner_single_use(self):
		owner = frappe.session.user
		desc = "jarvis-test-confirm-convguard-051"
		token = pending_confirm.mint(
			conversation="conv-real",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)
		# No conversation passed: owner + single-use still guard, it executes.
		res = confirm_tool(token)
		self.assertTrue(res["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))
		# Single use.
		self.assertFalse(confirm_tool(token)["ok"])


class TestListPendingConfirmations(FrappeTestCase):
	"""The resync endpoint returns only the caller's OWN live parked tokens,
	owner-scoped, conversation-filterable, excluding expired/consumed."""

	_OWNER = "jarvis-listpending-owner@example.com"
	_OTHER = "jarvis-listpending-other@example.com"

	def _ensure(self, email):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "LP",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		# list_pending_confirmations is @require_jarvis_user-gated; the owner
		# caller needs the role to reach the owner-scoped listing.
		if "Jarvis User" not in set(frappe.get_roles(email)):
			frappe.get_doc("User", email).add_roles("Jarvis User")
		return email

	def setUp(self):
		# Dedicated owner + a cleared Redis index, so token-count assertions are
		# isolated from the many tokens other tests park under Administrator.
		self._ensure(self._OWNER)
		self._ensure(self._OTHER)
		for o in (self._OWNER, self._OTHER):
			frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + o)
		self._orig = frappe.session.user
		frappe.set_user(self._OWNER)

	def tearDown(self):
		frappe.set_user(self._orig)

	def _ensure_other(self):
		return self._OTHER

	def test_returns_only_own_tokens_filtered_by_conversation(self):
		from jarvis.chat.actions_api import list_pending_confirmations

		owner = frappe.session.user
		other = self._ensure_other()
		t1 = pending_confirm.mint(
			conversation="lp-conv-1",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "lp-1"}},
			run_id="r1",
		)
		pending_confirm.mint(
			conversation="lp-conv-2",
			owner=owner,
			exec_user=owner,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "X"},
			run_id="r2",
		)
		# Another user's token must never surface.
		pending_confirm.mint(
			conversation="lp-conv-1",
			owner=other,
			exec_user=other,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "lp-other"}},
			run_id="",
		)

		res = list_pending_confirmations()
		self.assertTrue(res["ok"])
		items = res["data"]["pending"]
		# Only the caller's two tokens, each carrying the action:pending shape.
		self.assertEqual(len(items), 2)
		for it in items:
			self.assertIn("token", it)
			self.assertIn("preview", it)
			self.assertIn("summary", it)
			self.assertIn("conversation", it)
			self.assertIn("run_id", it)

		# Filtered by conversation.
		one = list_pending_confirmations(conversation="lp-conv-1")["data"]["pending"]
		self.assertEqual([i["token"] for i in one], [t1])

	def test_excludes_consumed(self):
		from jarvis.chat.actions_api import list_pending_confirmations

		owner = frappe.session.user
		t = pending_confirm.mint(
			conversation="lp-conv-3",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "lp-3"}},
			run_id="",
		)
		pending_confirm.consume(t, owner=owner, conversation="lp-conv-3")
		tokens = [i["token"] for i in list_pending_confirmations()["data"]["pending"]]
		self.assertNotIn(t, tokens)

	def test_rejects_guest(self):
		from jarvis.chat.actions_api import list_pending_confirmations

		original = frappe.session.user
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				list_pending_confirmations()
		finally:
			frappe.set_user(original)


class TestCreateDocsGate(FrappeTestCase):
	def test_create_docs_parks_one_card_and_dry_runs_batch(self):
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_docs",
				{
					"docs": [
						{"doctype": "ToDo", "values": {"description": "jarvis-gate-a"}},
						{"doctype": "ToDo", "values": {"description": "jarvis-gate-b"}},
					]
				},
			)
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertEqual(r["data"]["tool"], "create_docs")
		# The dry-run preview lists both creates; nothing was committed.
		would = r["data"]["preview"]["would"]
		self.assertEqual(len(would["created"]), 2)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-gate-a"}))
		# Exactly one token for the whole batch.
		self.assertIsNotNone(pending_confirm.peek(captured["token"]))
		self.assertNotIn(captured["token"], frappe.as_json(r))

	def test_create_docs_is_gated_but_not_auto_applyable(self):
		# Locked decision: masters are never created without the batch card, so
		# create_docs never fast-paths under auto_apply / file_box.
		self.assertIn("create_docs", api._GATED_WRITES)
		self.assertNotIn("create_docs", api._AUTO_APPLYABLE)

	def test_create_docs_bad_batch_bounces_at_park(self):
		# A deterministic failure (bad link on the 2nd doc) returns an error to
		# the model instead of parking a doomed card.
		r = api._run_tool(
			"create_docs",
			{
				"docs": [
					{"doctype": "ToDo", "values": {"description": "jarvis-gate-ok"}},
					{
						"doctype": "ToDo",
						"values": {
							"description": "jarvis-gate-bad",
							"assigned_by": "no-such-user@invalid.example",
						},
					},
				]
			},
		)
		self.assertFalse(r["ok"])
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-gate-ok"}))

	def test_create_docs_resync_preview_is_dry_run_not_described(self):
		# Regression: the LIVE park path (_run_tool) builds the batch preview
		# via _DRY_RUN_ON_PARK's direct _run_preview call, so it always worked.
		# But the RESYNC path (list_pending_confirmations, hit on every
		# conversation load / socket reconnect) rebuilds the preview via
		# api._pending_preview, which routes on _PREVIEWABLE membership alone.
		# If create_docs is missing from _PREVIEWABLE, _pending_preview
		# short-circuits to a described-intent dict with no ``would`` - the
		# batch card renders blank after a reload and a human would confirm
		# master creation blind.
		from jarvis.chat.actions_api import list_pending_confirmations

		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_docs",
				{
					"docs": [
						{"doctype": "ToDo", "values": {"description": "jarvis-resync-a"}},
						{"doctype": "ToDo", "values": {"description": "jarvis-resync-b"}},
					]
				},
			)
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		token = captured["token"]

		# Simulate a reload/reconnect: the resync endpoint rebuilds the card's
		# preview independently of the original park-time preview.
		resynced = list_pending_confirmations()
		items = [i for i in resynced["data"]["pending"] if i["token"] == token]
		self.assertEqual(len(items), 1)
		preview = items[0]["preview"]
		self.assertTrue(
			preview.get("preview"),
			f"resync preview degraded to described-intent (blank batch card): {preview}",
		)
		self.assertNotIn("described", preview)
		would = preview.get("would") or {}
		self.assertEqual(len(would.get("created", [])), 2)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-resync-a"}))


class TestBulkBatchCapAtPark(FrappeTestCase):
	"""F16: a bulk gated write over the shared max (20) bounces at PARK with a
	split-and-sequence instruction - before any card is minted - so an over-size
	batch never reaches a doomed confirmation card. create/update/create_docs
	already bounced via their park-time dry-run; this also closes the
	consequential bulk writes (submit/cancel/delete/amend/workflow) which take a
	described preview with NO dry-run and would otherwise only fail at execution."""

	def test_oversize_bulk_bounces_before_any_card(self):
		names = [f"TD-{i:04d}" for i in range(21)]  # 21 > _MAX_BATCH (20)
		patcher, captured = _spy_mint()
		with patch("jarvis.chat.events.publish_to_user") as pub, patcher:
			r = api._run_tool("submit_doc", {"doctype": "ToDo", "names": names})
			self.assertFalse(pub.called)  # no card published
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIn("too many records", r["error"]["message"])
		self.assertIsNone(captured.get("token"))  # nothing minted

	def test_at_limit_bulk_still_parks(self):
		names = [f"TD-{i:04d}" for i in range(20)]  # exactly the max
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool("submit_doc", {"doctype": "ToDo", "names": names})
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(captured.get("token"))


class TestSequentialConfirmationGuard(FrappeTestCase):
	"""F16: at most ONE live confirmation card per conversation. A second gated
	park while one is pending in the SAME conversation is refused (the model is
	told to stop), so batches confirm strictly one at a time; a different
	conversation is unaffected; a stray conversation-less token does not block;
	once the pending token clears, the next batch parks."""

	def setUp(self):
		# Redis is NOT rolled back / flushed by FrappeTestCase and a parked token
		# lives 15 min, so a token minted by a PRIOR run of this suite would trip
		# the very guard under test (a leftover card in the same conversation).
		# Clear this owner's pending-confirm index so each run starts clean, the
		# same way TestListPendingConfirmations isolates its own owner.
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + frappe.session.user)

	def tearDown(self):
		for name in frappe.get_all(CONV, filters={"title": "confirm-gate test"}, pluck="name"):
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ["like", "seq-e2e-%"]})
		frappe.db.commit()

	def test_second_park_in_same_conversation_is_refused(self):
		conv = "seq-guard-conv-100"
		patcher1, cap1 = _spy_mint()
		with patcher1:
			r1 = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": "seq-1"}}, conversation=conv
			)
		self.assertEqual(r1["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(cap1.get("token"))

		patcher2, cap2 = _spy_mint()
		with patch("jarvis.chat.events.publish_to_user") as pub, patcher2:
			r2 = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": "seq-2"}}, conversation=conv
			)
			self.assertFalse(pub.called)  # no second card published
		self.assertFalse(r2["ok"])
		self.assertEqual(r2["error"]["code"], "ConfirmationPendingError")
		self.assertIsNone(cap2.get("token"))  # no second token minted

	def test_pending_card_does_not_block_a_different_conversation(self):
		with _spy_mint()[0]:
			a = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "seq-A"}},
				conversation="seq-guard-conv-A",
			)
		self.assertEqual(a["data"]["status"], "pending_confirmation")
		patcher_b, cap_b = _spy_mint()
		with patcher_b:
			b = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "seq-B"}},
				conversation="seq-guard-conv-B",
			)
		self.assertEqual(b["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(cap_b.get("token"))

	def test_stray_conversation_less_token_does_not_block(self):
		# A conversation-less token for the same owner (a rare session-resolution
		# miss) must NOT block a legitimate new card - the guard matches the
		# conversation STRICTLY (list_for_owner surfaces conv-less under any filter).
		pending_confirm.mint(
			conversation="",
			owner=frappe.session.user,
			exec_user=frappe.session.user,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "x"},
			run_id="",
		)
		patcher, cap = _spy_mint()
		with patcher:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "seq-cl"}},
				conversation="seq-guard-conv-CL",
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(cap.get("token"))

	def test_next_batch_parks_after_pending_confirmed(self):
		# End-to-end: confirm batch 1 (runs the write + consumes its token), then a
		# second gated park in the same conversation succeeds - the continuation
		# turn's next batch. Real conversation so confirm_tool's receipt attaches.
		owner = frappe.session.user
		conv = _make_conv(owner)
		patcher1, cap1 = _spy_mint()
		with patcher1:
			r1 = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": "seq-e2e-1"}}, conversation=conv
			)
		self.assertEqual(r1["data"]["status"], "pending_confirmation")
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(cap1["token"], conversation=conv)
		self.assertTrue(res["ok"])
		# Batch 2 now parks - no pending card left in this conversation.
		patcher2, cap2 = _spy_mint()
		with patcher2:
			r2 = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": "seq-e2e-2"}}, conversation=conv
			)
		self.assertEqual(r2["data"]["status"], "pending_confirmation")
		self.assertIsNotNone(cap2.get("token"))


class TestConvLessTokenIsolation(FrappeTestCase):
	"""F1 follow-up (Codex/Fable adversarial review of #305): a conversation-less
	token is confirmable in whatever chat the owner is viewing, but WHAT executes
	is determined SOLELY by the token - never by the confirming conversation.
	Confirming in an unrelated conversation runs exactly the stored write,
	owner-scoped + single-use. This bounds the conv-less wart to receipt/
	continuation PLACEMENT, never to which (or whose) data is written."""

	def test_conv_less_token_runs_only_its_own_write_from_another_chat(self):
		owner = frappe.session.user
		desc = "jarvis-test-convless-isolation-060"
		token = pending_confirm.mint(
			conversation="",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)
		# Confirm from an unrelated conversation the owner is viewing.
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation="an-unrelated-conv")
		self.assertTrue(res["ok"])
		# Exactly the token's write ran - nothing else.
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))
		# Single use: cannot be re-confirmed from any conversation.
		self.assertFalse(confirm_tool(token, conversation="an-unrelated-conv")["ok"])

	def test_conv_less_token_stays_owner_scoped(self):
		# The owner boundary still holds for a conv-less token: a different user
		# cannot confirm it, and the attempt does not burn it.
		owner = frappe.session.user
		desc = "jarvis-test-convless-isolation-061"
		token = pending_confirm.mint(
			conversation="",
			owner=owner,
			exec_user=owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": desc}},
			run_id="",
		)
		other = "jarvis-convless-other@example.com"
		if not frappe.db.exists("User", other):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": other,
					"first_name": "Other",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		if "Jarvis User" not in set(frappe.get_roles(other)):
			frappe.get_doc("User", other).add_roles("Jarvis User")
		original = frappe.session.user
		frappe.set_user(other)
		try:
			res = confirm_tool(token, conversation="whatever")
		finally:
			frappe.set_user(original)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["type"], "InvalidConfirmation")
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))
		# The real owner can still confirm it (not burned by the wrong-owner try).
		with patch("jarvis.chat.api._dispatch_turn"):
			self.assertTrue(confirm_tool(token, conversation="mine")["ok"])


class TestConfirmCardWiring(FrappeTestCase):
	"""F9/F15 wiring: the gate attaches a render-ready ``card`` + ``expires_at`` to
	the stored/published preview, the model-facing return omits the ``card`` (UX,
	not model context), and the resync payload carries ``expires_at``."""

	def test_gate_attaches_card_and_expiry_and_strips_card_from_model_return(self):
		patcher, captured = _spy_mint()
		with patcher:
			r = api._run_tool("create_doc", {"doctype": "ToDo", "values": {"description": "card-wire-1"}})
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		# Model-facing preview: present, but WITHOUT the human card.
		self.assertNotIn("card", r["data"]["preview"])
		# The stored/published preview DOES carry the card, and expires_at is set.
		kw = captured["kwargs"]
		self.assertEqual(kw["preview"]["card"]["kind"], "create")
		self.assertIsInstance(kw["expires_at"], int)
		self.assertGreater(kw["expires_at"], 0)

	def test_resync_payload_carries_expires_at(self):
		from jarvis.chat import pending_confirm
		from jarvis.chat.actions_api import list_pending_confirmations

		owner = frappe.session.user
		pending_confirm.mint(
			conversation="cardwire-conv",
			owner=owner,
			exec_user=owner,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "X"},
			run_id="",
		)
		items = list_pending_confirmations(conversation="cardwire-conv")["data"]["pending"]
		self.assertTrue(items)
		self.assertIsInstance(items[0]["expires_at"], int)


class TestConfirmGracefulFailure(FrappeTestCase):
	"""F5: an UNEXPECTED exception during a confirmed write must fail gracefully -
	the endpoint returns {ok:false} instead of a 500 with the token burned and the
	agent stuck at 'awaiting confirmation', and it still fires the failed
	continuation so the agent learns the outcome."""

	def setUp(self):
		super().setUp()
		# CDX-10: asserts the LEGACY _dispatch_turn failed-continuation dispatch — provision legacy.
		provision_legacy_site(self)

	def tearDown(self):
		for name in frappe.get_all(CONV, filters={"title": "confirm-gate test"}, pluck="name"):
			frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_unexpected_dispatch_exception_returns_graceful_error(self):
		owner = frappe.session.user
		conv = _make_conv(owner)
		token = pending_confirm.mint(
			conversation=conv,
			owner=owner,
			exec_user=owner,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "no-such"},
			run_id="",
		)
		# An UNTRANSLATED exception from the write (dispatch raises RuntimeError).
		with (
			patch("jarvis.api.dispatch", side_effect=RuntimeError("boom")),
			patch("jarvis.chat.api._dispatch_turn") as disp,
		):
			res = confirm_tool(token, conversation=conv)
		# Graceful envelope, NOT a raised 500.
		self.assertFalse(res["ok"])
		self.assertIn("error", res)
		# The agent still gets a (failed) continuation, so it isn't left hanging.
		self.assertEqual(disp.call_count, 1)
