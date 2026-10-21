"""Fail closed on gateway policy loss; never recreate dispatched work lazily."""

from copy import deepcopy
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._collaboration_helpers import CollaborationCase


class Gateway:
	def __init__(self):
		self.entry = None
		self.creates = 0
		self.sends = []
		self.calls = []
		self.model = "actor-model"

	def get_session_messages(self, key, **options):
		return []

	def _request(self, method, params, **kwargs):
		self.calls.append((method, deepcopy(params)))
		if method == "sessions.create":
			self.creates += 1
			self.key = params["key"]
			self.entry = {
				"sessionId": f"physical-{self.creates}",
				"sessionRoot": params["cwd"],
				"permissionMode": params["permissionMode"],
				"spawnDepth": 0,
			}
		elif method == "sessions.patch":
			if not self.entry or params.get("expectedSessionId") != self.entry["sessionId"]:
				raise AgentUnreachableError("planted missing or replaced session", code="INVALID_REQUEST")
			for name in ("inheritedToolPolicyVersion", "inheritedToolAllow", "inheritedToolDeny"):
				if name in params:
					self.entry[name] = deepcopy(params[name])
		else:
			raise AssertionError(method)
		return {
			"ok": True,
			"payload": {
				"key": self.key,
				"entry": deepcopy(self.entry),
				"resolved": {"model": self.model, "modelProvider": "provider"},
			},
		}


class ParentGateway(Gateway):
	"""Native default metadata parent plus the optional companion RPC."""

	def __init__(self):
		super().__init__()
		self.detach_calls = 0
		self.lose_ack = False
		self.bad_ack = False

	def _request(self, method, params, **kwargs):
		if method == "jarvis.collaboration.detach_parent":
			self.calls.append((method, deepcopy(params)))
			self.detach_calls += 1
			assert params == {"key": self.key, "expectedSessionId": self.entry["sessionId"]}
			self.entry.pop("parentSessionKey", None)
			if self.lose_ack:
				self.lose_ack = False
				raise AgentUnreachableError("lost isolate acknowledgement", code="ack-timeout")
			return {
				"payload": {
					"ok": True,
					"version": 1,
					"sessionId": "wrong-id" if self.bad_ack else self.entry["sessionId"],
				}
			}
		if method == "sessions.create" and self.entry:
			self.calls.append((method, deepcopy(params)))
			return {
				"payload": {
					"key": self.key,
					"entry": deepcopy(self.entry),
					"resolved": {"model": self.model, "modelProvider": "provider"},
				}
			}
		result = super()._request(method, params, **kwargs)
		if method == "sessions.create":
			self.entry["parentSessionKey"] = "agent:main:main"
			result["payload"]["entry"] = deepcopy(self.entry)
		return result


class TestCollaborationRuntime(CollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound = self.binding()
		self.gateway = Gateway()

	def policy(self):
		from jarvis.chat.collaboration_runtime import RuntimePolicy

		return RuntimePolicy(
			skills_root="/srv/shared/skills", model_ref="provider/actor-model"
		)

	def provision(self):
		from jarvis.chat.collaboration_runtime import provision

		return provision(self.gateway, self.bound, self.policy())

	def verify(self):
		from jarvis.chat.collaboration_runtime import verify_before_send

		return verify_before_send(self.gateway, self.bound, self.policy())

	def mark_dispatched(self, *, sent=True):
		from jarvis.chat.collaboration_bindings import insert_message, stamp

		seed = insert_message(self.bound, {"role": "user", "content": "actor request"})
		turn = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": self.bound.logical_run_id,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": "dispatching",
				"seed_message": seed.name,
				"dispatching_at": frappe.utils.now_datetime(),
				"collaboration_send_started_at": frappe.utils.now_datetime() if sent else None,
			}
		)
		stamp(turn, self.bound)
		turn.insert(ignore_permissions=True)

	def test_provision_checks_exact_key_and_persists_physical_identity_and_policy(self):
		verified = self.provision()
		self.assertEqual(verified, "physical-1")
		self.assertEqual(self.gateway.key, self.bound.session_key)
		row = frappe.get_doc(c.BINDING, self.bound.name)
		self.assertEqual(row.runtime_session_id, verified)
		self.assertTrue(row.runtime_policy_hash)
		self.assertFalse(self.gateway.sends)

	def test_verified_read_does_not_reapply_a_changed_allowlist(self):
		self.provision()
		self.gateway.entry["inheritedToolAllow"] = ["*"]
		with self.assertRaises(frappe.PermissionError):
			self.verify()
		self.assertEqual(self.gateway.entry["inheritedToolAllow"], ["*"])
		self.assertFalse(self.gateway.sends)

	def test_missing_session_before_any_dispatch_can_be_provisioned_again(self):
		self.provision()
		self.gateway.entry = None
		self.assertEqual(self.provision(), "physical-2")
		self.assertEqual(self.gateway.creates, 2)

	def test_phase_zero_queue_admission_is_not_a_gateway_send(self):
		# Phase-0 sets dispatching_at BEFORE the RQ worker even provisions its
		# first session. It cannot mean a model request was sent or acknowledged.
		self.mark_dispatched(sent=False)
		self.assertEqual(self.provision(), "physical-1")
		self.assertEqual(self.gateway.creates, 1)
		self.assertFalse(self.gateway.sends)

	def test_missing_dispatched_session_is_uncertain_and_is_never_recreated(self):
		self.provision()
		self.mark_dispatched()
		self.gateway.entry = None
		with self.assertRaises(frappe.PermissionError):
			self.provision()
		self.assertEqual(self.gateway.creates, 1)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")
		self.assertFalse(self.gateway.sends)

	def test_changed_uuid_is_refused_even_if_root_and_allowlist_look_correct(self):
		self.provision()
		self.mark_dispatched()
		self.gateway.entry["sessionId"] = "replaced-physical-session"
		with self.assertRaises(frappe.PermissionError):
			self.verify()
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")

	def test_owner_workspace_root_and_changed_model_are_refused(self):
		self.provision()
		self.gateway.entry["sessionRoot"] = "/srv/shared"
		with self.assertRaises(frappe.PermissionError):
			self.verify()
		self.assertFalse(self.gateway.sends)
		self.gateway.entry["sessionRoot"] = self.policy().skills_root
		self.gateway.model = "owner-model"
		with self.assertRaises(frappe.PermissionError):
			self.verify()

	def test_regular_chat_send_cannot_lazily_create_an_unverified_shared_session(self):
		from jarvis.chat.agent_client import AgentSession

		session = AgentSession(Mock(), Mock())
		with patch.object(session, "_request") as request, self.assertRaises(AgentUnreachableError):
			session.chat_send(self.bound.session_key, "must not run", "run-id")
		request.assert_not_called()

	def test_verified_send_carries_gateway_atomic_identity_and_policy_fences(self):
		from jarvis.chat.agent_client import AgentSession

		session = AgentSession(Mock(), Mock())
		with patch.object(session, "_request", return_value={"payload": {"runId": "run-id"}}) as request:
			session.chat_send(
				self.bound.session_key, "actor request", "run-id", verified_session_id="physical-1"
			)
		params = request.call_args.args[1]
		self.assertEqual(params["sessionId"], "physical-1")
		self.assertEqual(params["expectedPermissionMode"], "read-only")
		self.assertNotIn("expectedLeafEntryId", params)

	def test_transient_preflight_failure_preserves_a_previously_sent_binding(self):
		from jarvis.chat.collaboration_runtime import resolve_runtime

		self.provision()
		self.mark_dispatched()
		for operation in (
			self.provision,
			self.verify,
			lambda: resolve_runtime(self.gateway, self.bound, skills_root=self.policy().skills_root),
		):
			with (
				patch.object(
					self.gateway,
					"_request",
					side_effect=AgentUnreachableError("temporary", code="ack-timeout"),
				),
				self.assertRaises(AgentUnreachableError),
			):
				operation()
			self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Active")
		self.assertEqual(self.verify(), "physical-1")
		self.assertEqual(self.gateway.creates, 1)

	def test_session_created_under_a_different_key_is_rejected(self):
		original = self.gateway._request

		def wrong_key(method, params, **kwargs):
			result = original(method, params, **kwargs)
			result["payload"]["key"] = "agent:main:dashboard:wrong-key"
			return result

		with patch.object(self.gateway, "_request", wrong_key), self.assertRaises(frappe.PermissionError):
			self.provision()

	def test_unverified_native_tree_parent_is_refused_before_binding_stamp(self):
		original = self.gateway._request

		def default_parent(method, params, **kwargs):
			if method == "jarvis.collaboration.detach_parent":
				raise AgentUnreachableError("companion unavailable", code="INVALID_REQUEST")
			result = original(method, params, **kwargs)
			self.gateway.entry["parentSessionKey"] = "agent:main:main"
			result["payload"]["entry"] = deepcopy(self.gateway.entry)
			return result

		with (
			patch.object(self.gateway, "_request", default_parent),
			self.assertRaises(frappe.PermissionError),
		):
			self.provision()
		self.assertFalse(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_session_id"))
		self.assertFalse(self.gateway.sends)

	def test_tree_parent_cannot_smuggle_a_private_parent_or_fork(self):
		from jarvis.chat.collaboration_runtime import _valid

		self.provision()
		for fields in (
			{"parentSessionKey": "agent:main:dashboard:owner-private"},
			{"parentSessionKey": "agent:other:main"},
			{"parentSessionKey": "agent:main:main:heartbeat"},
			{"parentSessionKey": "agent:main:main", "parentSessionId": "private-physical"},
			{"parentSessionKey": "agent:main:main", "forkSource": {"sessionId": "private-physical"}},
			{"parentSessionKey": "agent:main:main", "spawnedBy": "agent:main:main"},
			{"parentSessionKey": "agent:main:main", "spawnDepth": 1},
			{"parentSessionKey": "agent:main:main", "completionOwnerSessionKey": "agent:main:main"},
			{"parentSessionKey": "agent:main:main", "incognito": True},
		):
			with self.subTest(fields=fields):
				response = self.gateway._request("sessions.patch", {"expectedSessionId": "physical-1"})
				response["payload"]["entry"].update(fields)
				self.assertFalse(_valid(response, self.bound, self.policy(), "physical-1"))
		response = self.gateway._request("sessions.patch", {"expectedSessionId": "physical-1"})
		response["payload"]["resetExisting"] = True
		self.assertFalse(_valid(response, self.bound, self.policy(), "physical-1"))

	def test_fresh_provision_refuses_preexisting_native_history(self):
		for history in ([{"role": "user", "content": "private"}], {}, "", None):
			with (
				self.subTest(history=history),
				patch.object(self.gateway, "get_session_messages", return_value=history),
				self.assertRaises(frappe.PermissionError),
			):
				self.provision()
		self.assertFalse(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_session_id"))
		self.assertFalse(self.gateway.sends)

	def test_fresh_native_default_parent_is_detached_then_strictly_reread(self):
		self.gateway = ParentGateway()
		self.assertEqual(self.provision(), "physical-1")
		self.assertEqual(self.gateway.detach_calls, 1)
		self.assertNotIn("parentSessionKey", self.gateway.entry)
		self.assertEqual(self.verify(), "physical-1")
		self.assertEqual(self.gateway.creates, 1)
		self.assertFalse(self.gateway.sends)

	def test_lost_detachment_ack_preserves_uuid_and_reuses_parent_free_session(self):
		self.gateway = ParentGateway()
		self.gateway.lose_ack = True
		with self.assertRaises(AgentUnreachableError):
			self.provision()
		self.assertFalse(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_session_id"))
		self.assertEqual(self.provision(), "physical-1")
		self.assertEqual(self.gateway.creates, 1)
		self.assertEqual(self.gateway.detach_calls, 1)
		self.assertFalse(self.gateway.sends)

	def test_wrong_companion_ack_cannot_stamp_a_binding(self):
		self.gateway = ParentGateway()
		self.gateway.bad_ack = True
		with self.assertRaises(frappe.PermissionError):
			self.provision()
		self.assertFalse(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_session_id"))
		self.assertFalse(self.gateway.sends)

	def test_dispatched_parent_drift_is_never_detached_or_repaired(self):
		self.gateway = ParentGateway()
		self.provision()
		self.mark_dispatched()
		self.gateway.entry["parentSessionKey"] = "agent:main:main"
		with self.assertRaises(frappe.PermissionError):
			self.verify()
		self.assertEqual(self.gateway.detach_calls, 1)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Uncertain")

	def test_arbitrary_native_parent_never_reaches_the_companion(self):
		self.gateway = ParentGateway()
		original = self.gateway._request

		def private_parent(method, params, **kwargs):
			result = original(method, params, **kwargs)
			if method == "sessions.create":
				self.gateway.entry["parentSessionKey"] = "agent:main:dashboard:owner-private"
				result["payload"]["entry"] = deepcopy(self.gateway.entry)
			return result

		with (
			patch.object(self.gateway, "_request", private_parent),
			self.assertRaises(frappe.PermissionError),
		):
			self.provision()
		self.assertEqual(self.gateway.detach_calls, 0)
		self.assertFalse(self.gateway.sends)
