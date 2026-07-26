"""JF-017 — the delegate capability contract is snapshotted at launch and enforced
at the bench.

The defect: a session-bound plugin call from a delegate run reached
``jarvis.api._run_tool`` without EVER being compared against the agent's declared
``tools_allow``. The container's ``tools.allow`` is CONFIGURATION (fleet renders it
into openclaw.json); it is not authorization, so a compromised container/plugin or
a leaked per-run session bearer could call ANY registered tool the run-as user's
Frappe roles permitted. And the write guard read the CURRENT (mutable)
``Jarvis Agent Listing.nature``/``.writes``, so editing a listing re-authorised an
IN-FLIGHT run.

What is proven here:
  * a REGISTERED but UNDECLARED tool is refused over a delegate session, with the
    ``CapabilityDeniedError`` code, and never dispatched — while the SAME tool
    succeeds for the same user on a non-delegate session;
  * the run's authority is fixed at launch: re-naturing the listing, editing its
    ``writes``, or widening the bundled registry mid-run changes NOTHING for an
    in-flight run, in either direction;
  * auditor/operator write caps are driven by the run SNAPSHOT;
  * a legacy run (stamped by ``v2_07_agent_run_capability_snapshot``) still
    executes exactly as it did before the guard;
  * a run with NO contract created after the cutover is refused (fail closed) —
    but one created inside the bounded deploy window (migrate done, workers not
    yet restarted) is NOT, because that run is a deploy artefact, not a forgery;
  * ``_launch_audit`` actually stamps the contract, and the controller refuses to
    let it be edited afterwards;
  * a bundle that declares NO tool surface is refused at LAUNCH — no conversation,
    no run, no bearer — and a run that somehow reaches dispatch with no usable
    surface is FAILED at its first refusal with an honest error, never left
    ``running`` for the 3h stale-run sweep to relabel as a timeout;
  * the customer's transcript gets plain language while the delegate's envelope
    keeps the contract wording plus the "retrying will not help" hint.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_capability_contract
"""

import json
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import agent_catalog, agent_scheduler
from jarvis.exceptions import CapabilityDeniedError, JarvisError, PermissionDeniedError
from jarvis.tools import _agent_run_ctx, _delegate_capability
from jarvis.tools.create_doc import create_doc

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
CONVERSATION = "Jarvis Conversation"
MESSAGE = "Jarvis Chat Message"
SESSION = "Jarvis Chat Session"
PATCH_LOG = "Patch Log"

AUD_SLUG = "cc-auditor"
OP_SLUG = "cc-operator"
OP_WRITES = [{"doctype": "ToDo", "mode": "draft"}]

# What the auditor's manifest declared: two reads + the writeback pair + the
# container-side tools the bench never serves.
AUD_TOOLS_ALLOW = [
	"jarvis__get_schema",
	"jarvis__query",
	"jarvis__record_agent_run",
	"exec",
	"canvas",
	"message",
]
OP_TOOLS_ALLOW = ["jarvis__get_schema", "jarvis__create_doc", "canvas", "message"]


def _mk_user(email: str, roles=("System Manager",)) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	existing = {r.role for r in frappe.get_doc("User", email).roles}
	for role in roles:
		if role not in existing:
			frappe.get_doc("User", email).add_roles(role)
	return email


def _mk_listing(slug: str, nature: str, writes) -> None:
	"""A dependency-free published listing (no min_apps / doctypes_required), so the
	install-time installability preflight passes on any bench."""
	if frappe.db.exists(LISTING, slug):
		frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": slug,
			"title": f"Capability {slug}",
			"nature": nature,
			"delivery": "delegate",
			"status": "Published",
			"version": "0.1.0",
			"rule_tokens": json.dumps([]),
			"min_apps": json.dumps([]),
			"doctypes_required": json.dumps([]),
			"writes": json.dumps(writes or []),
		}
	).insert(ignore_permissions=True)


def _mk_run(slug: str, run_as: str, session_key: str, contract: dict) -> str:
	"""A running Jarvis Agent Run bound to ``session_key`` carrying ``contract`` —
	the shape ``_launch_audit`` stamps (or a deliberately degenerate one)."""
	inst = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": slug,
			"run_as_user": run_as,
			"activation_state": "shadow",
		}
	)
	inst.owner = run_as
	inst.flags.ignore_permissions = True
	inst.insert(ignore_permissions=True)
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": slug,
			"installation": inst.name,
			"trigger": "manual",
			"status": "running",
			"started_at": frappe.utils.now(),
			"session_key": session_key,
			**contract,
		}
	)
	run.owner = run_as
	run.flags.ignore_permissions = True
	run.insert(ignore_permissions=True)
	return run.name


def _snapshot(tools_allow: list, nature: str, writes: list) -> dict:
	return {
		"capability_contract": _delegate_capability.CONTRACT_SNAPSHOT,
		"tools_allow_json": json.dumps(tools_allow),
		"capability_nature": nature,
		"capability_writes_json": json.dumps(writes),
	}


def _mk_todo(owner: str) -> str:
	doc = frappe.get_doc({"doctype": "ToDo", "description": "capability contract todo"})
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("ToDo", doc.name, "owner", owner, update_modified=False)
	return doc.name


def _wipe():
	for slug in (AUD_SLUG, OP_SLUG):
		# A fatal refusal terminalizes the run, which COMMITS and logs an activity row,
		# so this fixture cannot rely on the test-case rollback for those two doctypes.
		for dt in (ACTIVITY, RUN, INSTALLATION):
			for n in frappe.get_all(dt, filters={"agent": slug}, pluck="name", ignore_permissions=True):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		if frappe.db.exists(LISTING, slug):
			frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)


class _DelegateCase(FrappeTestCase):
	"""Shared fixture: an auditor run and an operator run, each carrying the launch
	snapshot, plus the helpers to enter a delegate dispatch context."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.run_as = _mk_user("cc-runas@example.com")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		_mk_listing(AUD_SLUG, "Auditor", [])
		_mk_listing(OP_SLUG, "Operator", OP_WRITES)
		self.aud_key = f"agent:agent-{AUD_SLUG}:cc-aud-run"
		self.op_key = f"agent:agent-{OP_SLUG}:cc-op-run"
		self.aud_run = _mk_run(AUD_SLUG, self.run_as, self.aud_key, _snapshot(AUD_TOOLS_ALLOW, "auditor", []))
		self.op_run = _mk_run(
			OP_SLUG, self.run_as, self.op_key, _snapshot(OP_TOOLS_ALLOW, "operator", OP_WRITES)
		)

	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		frappe.set_user("Administrator")
		for key in (self.aud_key, self.op_key):
			frappe.db.delete(SESSION, {"session_key": key})
		_wipe()

	def _as_delegate(self, session_key: str):
		"""The tool-side context the plugin dispatcher establishes: the run-as
		identity plus its session_key on frappe.local."""
		frappe.set_user(self.run_as)
		_agent_run_ctx.set_session_key(session_key)

	def _dispatch(self, session_key: str, tool: str, args: dict | None = None) -> dict:
		"""The REAL plugin dispatch entry point (impersonation, capability gate,
		_run_tool, receipt) — not a hand-rolled stand-in for it."""
		return api._dispatch_from_session(self.run_as, session_key, tool, args or {})


# --------------------------------------------------------------------------- #
# the tools_allow gate
# --------------------------------------------------------------------------- #
class TestDelegateToolAllowGate(_DelegateCase):
	def test_declared_tool_is_dispatched(self):
		"""The gate must not over-block: a DECLARED tool runs normally."""
		res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)

	def test_registered_but_undeclared_read_tool_is_refused(self):
		"""THE defect. ``get_doc`` is registered and the run-as user (a System
		Manager) may read the row — but the auditor never declared it, so the bench
		refuses the call before dispatch."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		res = self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": todo})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")
		self.assertIn("capability contract", res["error"]["message"])

	def test_same_tool_succeeds_for_the_same_user_off_the_delegate_session(self):
		"""Control for the test above: the refusal is the CONTRACT, not the user's
		permissions — the identical call on a non-delegate session succeeds."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		res = self._dispatch("chat-session-with-no-agent-run", "get_doc", {"doctype": "ToDo", "name": todo})
		self.assertTrue(res["ok"], res)

	def test_destructive_tool_is_refused_and_never_dispatched(self):
		"""The headline threat: a leaked run bearer driving ``delete_doc``. It must be
		refused by the contract, and dispatch must never be reached (a gated write
		would otherwise park a confirmation card)."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		with mock.patch.object(api, "_run_tool") as run_tool:
			res = self._dispatch(self.aud_key, "delete_doc", {"doctype": "ToDo", "name": todo})
		run_tool.assert_not_called()
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_container_side_entries_do_not_grant_bench_tools(self):
		"""``exec``/``canvas``/``message`` are openclaw's own tools. They ride in the
		manifest list but must never satisfy a bench tool name."""
		for tool in ("exec", "canvas", "message"):
			res = self._dispatch(self.aud_key, tool, {})
			self.assertFalse(res["ok"], f"{tool} was allowed")
			self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_registry_widened_midrun_does_not_widen_an_inflight_run(self):
		"""Authority is the SNAPSHOT. Even a bundled registry that now declares every
		tool leaves an in-flight run exactly as it was launched."""
		wide = ["jarvis__" + t for t in ("get_schema", "get_doc", "delete_doc")]
		with mock.patch.object(agent_catalog, "registry_tools_allow", return_value=wide):
			res = self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": "whatever"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_non_delegate_session_is_untouched(self):
		"""A chat session_key with no bound Run is not a delegate — no gate at all."""
		res = self._dispatch("chat-session-with-no-agent-run", "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)

	def test_empty_snapshot_authorises_nothing(self):
		"""Fail-closed: a snapshot the bench could not populate (unknown/retired
		bundle) refuses every tool rather than defaulting to open."""
		frappe.db.set_value(RUN, self.aud_run, "tools_allow_json", "[]", update_modified=False)
		res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_unparseable_snapshot_authorises_nothing(self):
		frappe.db.set_value(RUN, self.aud_run, "tools_allow_json", "not json", update_modified=False)
		res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")


# --------------------------------------------------------------------------- #
# a run that can never call anything is FAILED, not left hanging
# --------------------------------------------------------------------------- #
class TestBrickedRunIsFailedAtOnce(_DelegateCase):
	"""A contract that authorises no bench tool refuses EVERY call — including the
	``record_agent_run`` writeback that finalizes the run. Left alone the row would
	sit ``running`` until the 3h stale-run sweep stamped "run exceeded max duration"
	on it, which is false and sends the reader after a timeout that never happened.
	It is terminalized at the first refusal instead, with the real reason."""

	def _brick(self) -> None:
		frappe.db.set_value(RUN, self.aud_run, "tools_allow_json", "[]", update_modified=False)

	def test_the_first_refusal_fails_the_run_with_an_honest_error(self):
		self._brick()
		res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		row = frappe.db.get_value(RUN, self.aud_run, ["status", "error", "finished_at"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("authorises no tools", row.error)
		# The reaper's wording is the LIE this replaces — it must not appear.
		self.assertNotIn("max duration", row.error)
		self.assertIsNotNone(row.finished_at)

	def test_the_bearer_does_not_outlive_the_failed_run(self):
		"""A8: terminalizing must take the per-run session row with it, so the
		refused run's bearer stops resolving to the run-as user."""
		agent_scheduler._mint_run_session(self.aud_key, self.run_as)
		self.assertTrue(frappe.db.exists(SESSION, {"session_key": self.aud_key}))
		self._brick()
		self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": self.aud_key}))

	def test_a_merely_undeclared_tool_leaves_the_run_running(self):
		"""The narrow case must stay narrow: an agent WITH a tool surface reaching for
		one tool outside it is a model mistake, not a dead run. It keeps going."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		res = self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": todo})
		self.assertFalse(res["ok"], res)
		self.assertEqual(frappe.db.get_value(RUN, self.aud_run, "status"), "running")

	def test_fail_run_never_overwrites_a_finalized_run(self):
		"""Compare-and-set: a run a concurrent ``record_agent_run`` already completed
		is never flipped to failed by this path."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"status": "completed", "finished_at": frappe.utils.now()},
			update_modified=False,
		)
		self.assertFalse(agent_scheduler.fail_run(self.aud_run, "should not land"))
		row = frappe.db.get_value(RUN, self.aud_run, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "completed")
		self.assertFalse(row.error)


# --------------------------------------------------------------------------- #
# what the CUSTOMER reads vs what the DELEGATE reads
# --------------------------------------------------------------------------- #
class TestDenialCopyAndVocabulary(_DelegateCase):
	def test_capability_denied_is_in_the_error_vocabulary(self):
		"""P2-5: the wire code names a real exception class and has a canon
		``_ERROR_HINTS`` entry. It is NOT a permission denial — no administrator can
		grant the tool mid-run, so the remedy is the bundle."""
		self.assertTrue(issubclass(CapabilityDeniedError, JarvisError))
		self.assertFalse(issubclass(CapabilityDeniedError, PermissionDeniedError))
		hint = api._ERROR_HINTS.get(CapabilityDeniedError.__name__, "")
		self.assertIn("bundle", hint)

	def test_the_delegate_is_told_that_retrying_cannot_help(self):
		"""The no-retry instruction has to be in the MESSAGE: the openclaw plugin
		relays a failed tool call to the model as ``"<code>: <message>"`` and drops
		every other envelope field, so a hint-only fix would never reach the delegate
		and it would retry at the rate limit until the run timed out."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		res = self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": todo})
		self.assertEqual(res["error"]["code"], CapabilityDeniedError.__name__)
		self.assertIn("capability contract", res["error"]["message"])
		self.assertIn("will not help", res["error"]["message"])
		self.assertIn("bundle", res["error"]["hint"])

	def test_the_chat_receipt_is_plain_language_not_contract_jargon(self):
		"""The transcript receipt renders verbatim in chat. "the tool is not in the
		capability contract snapshotted for this run at launch" is implementation
		vocabulary and must not be what a customer reads; the contract wording stays
		in the delegate's envelope + the audit trail."""
		frappe.set_user("Administrator")
		conv = frappe.get_doc(
			{
				"doctype": CONVERSATION,
				"title": "cc denial transcript",
				"status": "Active",
				"session_key": self.aud_key,
			}
		)
		conv.flags.ignore_permissions = True
		conv.insert(ignore_permissions=True)
		# persist_tool_receipt COMMITS, so the rows outlive the test-case rollback.
		self.addCleanup(self._drop_conversation, conv.name)
		todo = _mk_todo(self.run_as)

		res = self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": todo})

		rows = frappe.get_all(
			MESSAGE,
			filters={"conversation": conv.name, "role": "tool"},
			fields=["tool_status", "tool_result"],
			ignore_permissions=True,
		)
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].tool_status, "error")  # renders as a failure, not a success
		envelope = json.loads(rows[0].tool_result)["error"]
		self.assertNotIn("capability contract", envelope["message"])
		self.assertNotIn("snapshot", envelope["message"].lower())
		self.assertIn("isn't allowed to use", envelope["message"])
		self.assertIn("administrator", envelope["hint"])  # and a remedy, not a dead end
		# not lost, just relocated: the delegate still gets the precise reason
		self.assertIn("capability contract", res["error"]["message"])

	def _drop_conversation(self, name: str) -> None:
		frappe.set_user("Administrator")
		for m in frappe.get_all(MESSAGE, filters={"conversation": name}, pluck="name"):
			frappe.delete_doc(MESSAGE, m, force=True, ignore_permissions=True)
		if frappe.db.exists(CONVERSATION, name):
			frappe.delete_doc(CONVERSATION, name, force=True, ignore_permissions=True)
		frappe.db.commit()


# --------------------------------------------------------------------------- #
# legacy runs + the cutover boundary
# --------------------------------------------------------------------------- #
class TestLegacyRunFallback(_DelegateCase):
	def _undeclared_call(self) -> dict:
		"""``get_doc`` on a readable row — refused under a snapshot (it is not in the
		auditor's declared surface), so it is the honest probe for "is this run being
		gated at all"."""
		frappe.set_user("Administrator")
		todo = _mk_todo(self.run_as)
		return self._dispatch(self.aud_key, "get_doc", {"doctype": "ToDo", "name": todo})

	def test_legacy_run_still_executes_undeclared_tools(self):
		"""A run in flight when the guard landed keeps the regime it launched under:
		the patch stamps it ``legacy`` and no tools_allow gate applies."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": "legacy", "tools_allow_json": None},
			update_modified=False,
		)
		res = self._undeclared_call()
		self.assertTrue(res["ok"], res)

	def test_blank_contract_after_the_cutover_is_refused(self):
		"""A run created AFTER the patch with no snapshot never happened via
		``_launch_audit``. It buys no legacy authority."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": None, "tools_allow_json": None},
			update_modified=False,
		)
		before = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=before):
			res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_blank_contract_inside_the_deploy_window_is_legacy(self):
		"""``bench migrate`` finishing and the LAST worker restarting are not the same
		instant. A run launched by a still-old worker in between physically cannot
		carry a snapshot, and it is not a forgery — refusing it bricks a real customer
		run on every deploy. It is grandfathered for the bounded grace window."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": None, "tools_allow_json": None},
			update_modified=False,
		)
		# patched 5h ago; the run (created now) is inside the 6h window
		cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-5)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=cutoff):
			res = self._undeclared_call()
		self.assertTrue(res["ok"], res)

	def test_blank_contract_past_the_deploy_window_is_refused(self):
		"""The grace is BOUNDED. Hours after the deploy a blank contract can only mean
		a launch path that bypassed ``_launch_audit`` (or a planted row) — fail closed,
		and kill the run rather than let it hang."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": None, "tools_allow_json": None},
			update_modified=False,
		)
		cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-7)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=cutoff):
			res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")
		self.assertEqual(frappe.db.get_value(RUN, self.aud_run, "status"), "failed")

	def test_the_grace_boundary_is_the_documented_six_hours(self):
		"""The exact edge, without a dispatch in the way."""
		self.assertEqual(_delegate_capability.LEGACY_GRACE_SECONDS, 6 * 3600)
		cutoff = frappe.utils.now_datetime()
		grace = _delegate_capability.LEGACY_GRACE_SECONDS
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=cutoff):
			inside = frappe.utils.add_to_date(cutoff, seconds=grace - 60)
			outside = frappe.utils.add_to_date(cutoff, seconds=grace + 60)
			self.assertTrue(_delegate_capability._is_pre_patch(inside))
			self.assertFalse(_delegate_capability._is_pre_patch(outside))

	def test_blank_contract_before_the_cutover_is_legacy(self):
		"""The deploy-race grace: a run created before the patch ran (new code already
		serving, migrate not finished) is still treated as legacy."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": None, "tools_allow_json": None},
			update_modified=False,
		)
		after = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=1)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=after):
			res = self._undeclared_call()
		self.assertTrue(res["ok"], res)

	def test_blank_contract_ignores_the_snapshot_columns(self):
		"""Only the marker makes the snapshot columns trustworthy. A row whose columns
		are populated but whose marker is blank holds NO authority — otherwise a row
		planted with a hand-written tools_allow would authorise itself."""
		frappe.db.set_value(RUN, self.aud_run, "capability_contract", None, update_modified=False)
		before = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=before):
			res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_no_recorded_cutover_refuses_a_blank_contract(self):
		"""No Patch Log row -> nothing to grandfather -> refuse (fail closed)."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{"capability_contract": None, "tools_allow_json": None},
			update_modified=False,
		)
		with mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=None):
			res = self._dispatch(self.aud_key, "get_schema", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_cutoff_is_read_from_the_patch_log(self):
		"""The cutover instant is the patch's own Patch Log row — not a constant that
		could drift from when the guard actually landed on this site."""
		created = False
		if not frappe.db.exists(PATCH_LOG, {"patch": _delegate_capability.LEGACY_CUTOFF_PATCH}):
			frappe.get_doc({"doctype": PATCH_LOG, "patch": _delegate_capability.LEGACY_CUTOFF_PATCH}).insert(
				ignore_permissions=True
			)
			created = True
		try:
			expected = frappe.db.get_value(
				PATCH_LOG, {"patch": _delegate_capability.LEGACY_CUTOFF_PATCH}, "creation"
			)
			self.assertEqual(_delegate_capability._legacy_cutoff(), expected)
		finally:
			if created:
				frappe.delete_doc(
					PATCH_LOG,
					frappe.db.get_value(
						PATCH_LOG, {"patch": _delegate_capability.LEGACY_CUTOFF_PATCH}, "name"
					),
					force=True,
					ignore_permissions=True,
				)


# --------------------------------------------------------------------------- #
# write caps are driven by the snapshot, not the live listing
# --------------------------------------------------------------------------- #
class TestWriteCapsFromSnapshot(_DelegateCase):
	def test_operator_creates_a_snapshotted_doctype(self):
		self._as_delegate(self.op_key)
		res = create_doc("ToDo", {"description": "operator draft"})
		self.assertEqual(res["doctype"], "ToDo")

	def test_auditor_is_refused_every_write_from_its_snapshot(self):
		self._as_delegate(self.aud_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			create_doc("ToDo", {"description": "auditor write"})
		self.assertIn("no declared write capability", str(ctx.exception))

	def test_renaturing_the_listing_midrun_grants_nothing(self):
		"""THE mid-run mutation. Flipping the auditor's listing to Operator and giving
		it a write contract must NOT reach the in-flight run."""
		frappe.db.set_value(
			LISTING,
			AUD_SLUG,
			{"nature": "Operator", "writes": json.dumps(OP_WRITES)},
			update_modified=False,
		)
		self._as_delegate(self.aud_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			create_doc("ToDo", {"description": "escalated mid-run"})
		self.assertIn("no declared write capability", str(ctx.exception))

	def test_widening_listing_writes_midrun_grants_nothing(self):
		"""An operator's contract cannot be extended to a new doctype mid-run."""
		frappe.db.set_value(
			LISTING,
			OP_SLUG,
			"writes",
			json.dumps(OP_WRITES + [{"doctype": "Note", "mode": "draft"}]),
			update_modified=False,
		)
		self._as_delegate(self.op_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			create_doc("Note", {"title": "smuggled in mid-run"})
		self.assertIn("declared write contract", str(ctx.exception))

	def test_emptying_listing_writes_midrun_revokes_nothing(self):
		"""Immutability cuts both ways: a listing edit must not silently BREAK an
		in-flight operator either."""
		frappe.db.set_value(LISTING, OP_SLUG, "writes", json.dumps([]), update_modified=False)
		self._as_delegate(self.op_key)
		res = create_doc("ToDo", {"description": "still authorised"})
		self.assertEqual(res["doctype"], "ToDo")

	def test_blank_contract_operator_writes_nothing(self):
		"""A post-cutover run with no contract marker has no write authority either —
		its snapshot columns are not consulted, so it is refused like an auditor."""
		frappe.db.set_value(RUN, self.op_run, "capability_contract", None, update_modified=False)
		before = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1)
		self._as_delegate(self.op_key)
		with (
			mock.patch.object(_delegate_capability, "_legacy_cutoff", return_value=before),
			self.assertRaises(PermissionDeniedError) as ctx,
		):
			create_doc("ToDo", {"description": "unstamped run"})
		self.assertIn("no declared write capability", str(ctx.exception))

	def test_legacy_run_write_caps_follow_the_live_listing(self):
		"""The grandfathered regime, documented: a legacy run has no snapshot, so its
		write caps resolve against the listing exactly as they did pre-JF-017."""
		frappe.db.set_value(
			RUN,
			self.aud_run,
			{
				"capability_contract": "legacy",
				"capability_nature": None,
				"capability_writes_json": None,
			},
			update_modified=False,
		)
		frappe.db.set_value(
			LISTING,
			AUD_SLUG,
			{"nature": "Operator", "writes": json.dumps(OP_WRITES)},
			update_modified=False,
		)
		self._as_delegate(self.aud_key)
		res = create_doc("ToDo", {"description": "legacy run, live listing"})
		self.assertEqual(res["doctype"], "ToDo")


# --------------------------------------------------------------------------- #
# the snapshot is stamped at launch and cannot be edited afterwards
# --------------------------------------------------------------------------- #
class TestLaunchStampsTheContract(unittest.TestCase):
	"""``_launch_audit`` commits, so this class cleans up after itself rather than
	relying on a test-case rollback. The listing is synthetic and dependency-free so
	the install preflight passes on any bench; the DECLARED surface is injected at
	the one seam ``contract_for_launch`` reads it from."""

	SLUG = "cc-launch-agent"
	OWNER = "cc-launch-owner@example.com"
	DECLARED = ["jarvis__get_schema", "jarvis__record_agent_run", "exec", "canvas", "message"]

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_mk_user(cls.OWNER)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self.created: list[tuple[str, str]] = []
		_mk_listing(self.SLUG, "Auditor", [])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		for dt, name in self.created:
			try:
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			except Exception:
				pass
		for dt in ("Jarvis Agent Activity", RUN, INSTALLATION):
			if not frappe.db.exists("DocType", dt):
				continue
			for n in frappe.get_all(dt, filters={"agent": self.SLUG}, pluck="name"):
				try:
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
				except Exception:
					pass
		if frappe.db.exists(LISTING, self.SLUG):
			frappe.delete_doc(LISTING, self.SLUG, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _launch(self, declared: list | None = None) -> str:
		import jarvis.admin_client as admin_client

		inst = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": self.SLUG,
				"run_as_user": self.OWNER,
				"activation_state": "shadow",
				"enabled": 1,
			}
		)
		inst.owner = self.OWNER
		inst.flags.ignore_permissions = True
		inst.insert(ignore_permissions=True)
		frappe.db.commit()

		orig = admin_client.post_agent_run
		admin_client.post_agent_run = lambda **kw: {"run_id": kw.get("run_id"), "status": "queued"}
		frappe.set_user(self.OWNER)
		try:
			surface = self.DECLARED if declared is None else declared
			with mock.patch.object(agent_catalog, "registry_tools_allow", return_value=surface):
				result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig
		# Deleted before the run/installation sweep in tearDown (the Run links them).
		self.created.append((RUN, result["run"]))
		self.created.append(("Jarvis Conversation", result["conversation"]))
		session = frappe.db.get_value("Jarvis Chat Session", {"session_key": result["session_key"]}, "name")
		if session:
			self.created.append(("Jarvis Chat Session", session))
		return result["run"]

	def test_launch_stamps_the_declared_contract(self):
		run = self._launch()
		row = frappe.db.get_value(
			RUN,
			run,
			["capability_contract", "tools_allow_json", "capability_nature", "capability_writes_json"],
			as_dict=True,
		)
		self.assertEqual(row.capability_contract, "snapshot")
		self.assertEqual(row.capability_nature, "auditor")
		self.assertEqual(json.loads(row.capability_writes_json), [])
		# Stored VERBATIM — the declared list is the run's provenance, so the
		# openclaw-facing ids and the container-side entries are kept as-is.
		self.assertEqual(json.loads(row.tools_allow_json), self.DECLARED)

	def test_the_stamped_contract_is_immutable(self):
		run = self._launch()
		doc = frappe.get_doc(RUN, run)
		doc.tools_allow_json = json.dumps(["jarvis__delete_doc"])
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_a_launched_run_refuses_an_undeclared_tool(self):
		"""End to end: launch for real, then drive the real dispatch path."""
		run = self._launch()
		session_key = frappe.db.get_value(RUN, run, "session_key")
		res = api._dispatch_from_session(self.OWNER, session_key, "delete_doc", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")

	def test_a_launched_run_dispatches_a_declared_tool(self):
		run = self._launch()
		session_key = frappe.db.get_value(RUN, run, "session_key")
		res = api._dispatch_from_session(self.OWNER, session_key, "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)

	# ------------------------------------------------------------------ #
	# refuse at LAUNCH — the root cause of the bricked run
	# ------------------------------------------------------------------ #
	def test_launch_is_refused_when_the_bundle_declares_no_tools(self):
		"""THE fix for the bricked run. A bundle with no declared surface produces a
		run that is refused at every step, so it must never START: the human gets a
		message they can act on instead of a blue "running" card that turns into a
		false timeout three hours later."""
		convs_before = frappe.db.count(CONVERSATION)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._launch(declared=[])
		message = str(ctx.exception)
		self.assertIn("declares no tools", message)
		self.assertIn("nothing was started", message)
		# and it is refused BEFORE any row exists — no orphan run, no orphan
		# conversation, no orphan bearer to clean up.
		self.assertEqual(frappe.get_all(RUN, filters={"agent": self.SLUG}, pluck="name"), [])
		self.assertEqual(frappe.db.count(CONVERSATION), convs_before)

	def test_the_launch_refusal_leaves_the_install_runnable_once_fixed(self):
		"""The refusal is about the BUNDLE, not the install: a repaired/replaced
		bundle launches the very same installation normally."""
		with self.assertRaises(frappe.ValidationError):
			self._launch(declared=[])
		frappe.db.delete(INSTALLATION, {"agent": self.SLUG})
		frappe.db.commit()
		run = self._launch()
		self.assertEqual(frappe.db.get_value(RUN, run, "capability_contract"), "snapshot")


# --------------------------------------------------------------------------- #
# registry sourcing
# --------------------------------------------------------------------------- #
class TestRegistryToolsAllow(FrappeTestCase):
	def test_unknown_slug_yields_an_empty_surface(self):
		self.assertEqual(agent_catalog.registry_tools_allow("no-such-agent"), [])
		self.assertEqual(agent_catalog.registry_tools_allow(""), [])

	def test_malformed_entry_yields_an_empty_surface(self):
		synth = {"agents": [{"agent_slug": "cc-bad", "tools_allow": "jarvis__delete_doc"}]}
		with mock.patch.object(agent_catalog, "_load_registry", return_value=synth):
			self.assertEqual(agent_catalog.registry_tools_allow("cc-bad"), [])

	def test_every_bundled_agent_declares_a_usable_bench_surface(self):
		"""A bundled agent with no declared tools is now refused at LAUNCH, and one
		that declares only container-side tools (``exec``/``canvas``/``message``)
		launches but is fatally refused at its first call. Either is a shipped agent
		nobody can run — catch it here, not in prod."""
		for a in agent_catalog._load_registry().get("agents") or []:
			slug = (a.get("agent_slug") or "").strip()
			declared = agent_catalog.registry_tools_allow(slug)
			self.assertTrue(declared, f"{slug} declares no tools_allow")
			self.assertTrue(
				_delegate_capability.bench_tools(declared),
				f"{slug} declares no jarvis__ tool, so every bench call would be refused",
			)

	def test_normalize_strips_only_the_jarvis_prefix(self):
		self.assertEqual(_delegate_capability.normalize_tool("jarvis__get_doc"), "get_doc")
		self.assertEqual(_delegate_capability.normalize_tool("exec"), "exec")
		self.assertEqual(_delegate_capability.normalize_tool("  jarvis__query  "), "query")
		self.assertEqual(_delegate_capability.normalize_tool(None), "")

	def test_bench_tools_ignores_container_side_entries(self):
		declared = ["jarvis__get_doc", "exec", "canvas", "message", "", None]
		self.assertEqual(_delegate_capability.bench_tools(declared), {"get_doc"})
