"""Provision and recheck the isolated runtime before EVERY collaborative send.

Only the holder of the canonical Turn prepare/dispatch claim calls these helpers.
RPCs occur outside database locks; callers commit the returned provisioning stamp
and reauthorize their versioned Turn immediately before dispatch. These helpers
never send a model request and never manufacture a new logical run or retry key.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

import frappe
from frappe.utils import cint

from jarvis.chat import collaboration as c
from jarvis.exceptions import AgentUnreachableError

ALLOW = ("read", "jarvis__*")
DENY = ("browser", "web_fetch", "sessions_spawn", "llm-task")


def configured_skills_root() -> str:
	"""Use the operator-verified public skills directory, independent of media."""
	value = frappe.conf.get("chat_collaboration_skills_root")
	if isinstance(value, str) and value:
		root = PurePosixPath(value)
		if root.is_absolute() and ".." not in root.parts and root.name == "skills" and str(root) == value:
			return value
	frappe.throw("Shared-chat runtime skills are not configured safely. Your draft has been kept.")


class RuntimePolicyChanged(frappe.PermissionError):
	def __init__(self, binding, dispatched):
		self.binding_name, self.dispatched = binding.name, dispatched
		super().__init__(
			"The shared request's isolated session is unavailable or changed. Check its outcome before retrying."
		)


def preserve_drift(binding, error):
	"""Reapply a proven drift fence after the caller rolled back failed work."""
	if (
		not isinstance(error, RuntimePolicyChanged)
		or not error.dispatched
		or error.binding_name != binding.name
	):
		return
	from jarvis.chat import txn

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = frappe.db.get_value(
			c.BINDING, binding.name, ["actor", "conversation"], as_dict=True, for_update=True
		)
		if row and (row.actor, row.conversation) == (binding.actor, binding.conversation):
			frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
		frappe.db.commit()

	return txn.replay_on_conflict(unit, label="collaboration.runtime_drift", fresh=True)


@dataclass(frozen=True)
class RuntimePolicy:
	skills_root: str
	model_ref: str

	def digest(self):
		return hashlib.sha256(
			json.dumps(
				{
					"root": self.skills_root,
					"mode": "read-only",
					"allow": ALLOW,
					"deny": DENY,
					"model": self.model_ref,
				},
				sort_keys=True,
				separators=(",", ":"),
			).encode()
		).hexdigest()


@dataclass(frozen=True)
class RuntimePlan:
	policy: RuntimePolicy
	physical_id: str
	context_tokens: int
	runtime_reserve: int


def resolve_runtime(sess, binding, *, skills_root: str) -> RuntimePlan:
	"""Discover only a fresh actor-agent default or this chain's verified model.

	The optional site attestation supplies a capacity for runtime aliases omitted
	from models.list; it is never a guessed model fallback. The instruction/tool
	reserve must be attested for each model before shared execution is enabled.
	"""
	metadata = _metadata(binding)
	created = None
	if metadata.runtime_session_id:
		try:
			response = _read(sess, binding, metadata.runtime_session_id)
		except AgentUnreachableError as exc:
			if exc.code != "INVALID_REQUEST":
				raise  # No response does not prove that the physical session changed.
			_refuse(binding)
	else:
		if _has_dispatched(binding):
			_refuse(binding)
		response = created = sess._request(
			"sessions.create",
			{
				"key": binding.session_key,
				"agentId": binding.session_key.split(":", 2)[1],
				"label": "shared-" + binding.logical_run_id,
				"idempotencyKey": "shared-provision-" + binding.logical_run_id,
				"cwd": skills_root,
				"permissionMode": "read-only",
			},
			timeout_s=25,
		)
	resolved = (response.get("payload") or {}).get("resolved") or {}
	provider, model = resolved.get("modelProvider"), resolved.get("model")
	if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
		_refuse(binding)
	policy = RuntimePolicy(skills_root, f"{provider}/{model}")
	if metadata.runtime_session_id and (
		metadata.runtime_policy_hash != policy.digest()
		or not _valid(response, binding, policy, metadata.runtime_session_id)
	):
		_refuse(binding)
	rows = (sess._request("models.list", {}, timeout_s=25).get("payload") or {}).get("models") or []
	entry = next((r for r in rows if r.get("provider") == provider and r.get("id") == model), {})
	selected = ((response.get("payload") or {}).get("entry") or {}).get("contextWindow") or entry.get(
		"contextWindowDefault"
	)
	option = next((r for r in entry.get("contextWindows") or [] if r.get("id") == selected), {})
	capacity = cint(option.get("contextWindow") or entry.get("contextWindow"))
	budgets = frappe.conf.get("chat_collaboration_model_budgets") or {}
	attested = budgets.get(policy.model_ref, {}) if isinstance(budgets, dict) else {}
	if not isinstance(attested, dict):
		attested = {}
	attested_capacity = cint(attested.get("context_tokens"))
	if attested_capacity > 0:
		capacity = min(capacity, attested_capacity) if capacity > 0 else attested_capacity
	reserve = cint(attested.get("runtime_reserve"))
	from jarvis.chat.collaboration_context import require_prompt_budget

	require_prompt_budget("", attachments=[], context_tokens=capacity, runtime_reserve=reserve)
	# Provision's final DB stamp comes after the last blocking RPC. Never
	# hold its Binding write lock across the model-catalog request.
	physical_id = provision(sess, binding, policy, _created=created)
	return RuntimePlan(policy, physical_id, capacity, reserve)


def _has_dispatched(binding):
	return bool(
		frappe.db.exists(
			"Jarvis Chat Turn",
			{"collaboration_binding": binding.name, "collaboration_send_started_at": ["is", "set"]},
		)
	)


def _refuse(binding):
	dispatched = _has_dispatched(binding)
	if dispatched:
		# The worker error/settlement transaction persists this stamp; no new
		# dispatch is allowed. Retain authentication/identity for late receipts.
		frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
	raise RuntimePolicyChanged(binding, dispatched)


def _metadata(binding):
	c.require_writes_ready()
	if c.resolve_binding(binding.session_key) != binding:
		c._deny()
	return frappe.db.get_value(
		c.BINDING, binding.name, ["runtime_session_id", "runtime_policy_hash"], as_dict=True
	)


def _valid(response, binding, policy, physical_id):
	payload = response.get("payload") or {}
	entry = payload.get("entry") or {}
	resolved = payload.get("resolved") or {}
	return bool(
		payload.get("key") == binding.session_key
		and physical_id
		and entry.get("sessionId") == physical_id
		and entry.get("sessionRoot") == policy.skills_root
		and entry.get("permissionMode") == "read-only"
		and entry.get("inheritedToolPolicyVersion") == 1
		and sorted(entry.get("inheritedToolAllow") or []) == sorted(ALLOW)
		and sorted(entry.get("inheritedToolDeny") or []) == sorted(DENY)
		and not entry.get("parentSessionKey")
		and not entry.get("parentSessionId")
		and not entry.get("forkSource")
		and not entry.get("forkedFromParent")
		and not entry.get("completionOwnerSessionKey")
		and not entry.get("incognito")
		and not payload.get("resetExisting")
		and not entry.get("spawnedBy")
		and entry.get("spawnDepth", 0) == 0
		and not entry.get("archivedAt")
		and f"{resolved.get('modelProvider')}/{resolved.get('model')}" == policy.model_ref
	)


def _read(sess, binding, physical_id, *, timeout_s=25):
	# describe does NOT expose inherited policy. This CAS no-op patch returns
	# the full policy but refuses missing/replaced sessions without creating one.
	return sess._request(
		"sessions.patch", {"key": binding.session_key, "expectedSessionId": physical_id}, timeout_s=timeout_s
	)


def verify_dispatched_session(sess, binding, policy, *, timeout_s=5):
	"""Read/abort/reconcile actual work after revoke or flag-off; NEVER send.

	This identity-only path neither creates sessions nor changes policy. A
	transport failure remains unknown; a definite policy/UUID mismatch refuses.
	"""
	if c.resolve_identity(binding.session_key) != binding or not _has_dispatched(binding):
		c._deny()
	metadata = frappe.db.get_value(
		c.BINDING, binding.name, ["runtime_session_id", "runtime_policy_hash"], as_dict=True
	)
	if not metadata or not metadata.runtime_session_id or metadata.runtime_policy_hash != policy.digest():
		_refuse(binding)
	response = _read(sess, binding, metadata.runtime_session_id, timeout_s=timeout_s)
	if not _valid(response, binding, policy, metadata.runtime_session_id):
		_refuse(binding)
	return metadata.runtime_session_id


def history_watermark(sess, binding):
	from jarvis.chat import runtime_profile

	key = runtime_profile.get_profile().message_metadata_key
	rows = sess.get_session_messages(binding.session_key, limit=100, timeout_s=5)
	if not isinstance(rows, list):
		c._deny()
	watermark = 0
	for row in rows:
		metadata = row.get(key) if isinstance(row, dict) else None
		seq = metadata.get("seq") if isinstance(metadata, dict) else None
		if type(seq) is not int or seq < 0:
			c._deny()
		watermark = max(watermark, seq)
	return watermark


def _detach_fresh_parent(sess, binding, policy, physical_id, response):
	"""Detach only a verified default tree link before the first model send."""
	payload = response.get("payload") or {}
	entry = payload.get("entry") or {}
	if not entry.get("parentSessionKey"):
		return response
	parent = f"agent:{binding.session_key.split(':', 2)[1]}:main"
	candidate = {**response, "payload": {**payload, "entry": {**entry, "parentSessionKey": None}}}
	if (
		_has_dispatched(binding)
		or entry.get("parentSessionKey") != parent
		or not _valid(candidate, binding, policy, physical_id)
	):
		_refuse(binding)
	try:
		result = (
			sess._request(
				"jarvis.collaboration.detach_parent",
				{"key": binding.session_key, "expectedSessionId": physical_id},
				timeout_s=25,
			).get("payload")
			or {}
		)
	except AgentUnreachableError as exc:
		if exc.code == "INVALID_REQUEST":
			_refuse(binding)
		raise
	if (
		result.get("ok") is not True
		or type(result.get("version")) is not int
		or result["version"] != 1
		or result.get("sessionId") != physical_id
	):
		_refuse(binding)
	return _read(sess, binding, physical_id)


def provision(sess, binding: c.ActorBinding, policy: RuntimePolicy, *, _created=None) -> str:
	metadata = _metadata(binding)
	root = PurePosixPath(policy.skills_root)
	if not root.is_absolute() or ".." in root.parts or root.name != "skills" or "/" not in policy.model_ref:
		_refuse(binding)
	if metadata.runtime_session_id:
		try:
			response = _read(sess, binding, metadata.runtime_session_id)
		except AgentUnreachableError as exc:
			if exc.code != "INVALID_REQUEST":
				raise
			if _has_dispatched(binding):
				_refuse(binding)
		else:
			if metadata.runtime_policy_hash == policy.digest() and _valid(
				response, binding, policy, metadata.runtime_session_id
			):
				return metadata.runtime_session_id
			_refuse(binding)
	elif _has_dispatched(binding):
		_refuse(binding)
	# No model dispatch has occurred. Losing an initial provisioning ACK can
	# safely be reconciled by exact key, never by a different session or actor.
	response = _created or sess._request(
		"sessions.create",
		{
			"key": binding.session_key,
			"agentId": binding.session_key.split(":", 2)[1],
			"label": "shared-" + binding.logical_run_id,
			"idempotencyKey": "shared-provision-" + binding.logical_run_id,
			"cwd": policy.skills_root,
			"permissionMode": "read-only",
			"model": policy.model_ref,
		},
		timeout_s=25,
	)
	payload = response.get("payload") or {}
	physical_id = (payload.get("entry") or {}).get("sessionId")
	if payload.get("key") != binding.session_key or not physical_id:
		_refuse(binding)
	sess._request(
		"sessions.patch",
		{
			"key": binding.session_key,
			"expectedSessionId": physical_id,
			"inheritedToolPolicyVersion": 1,
			"inheritedToolAllow": list(ALLOW),
			"inheritedToolDeny": list(DENY),
			"model": policy.model_ref,
		},
		timeout_s=25,
	)
	response = _detach_fresh_parent(sess, binding, policy, physical_id, _read(sess, binding, physical_id))
	if not _valid(response, binding, policy, physical_id):
		_refuse(binding)
	# Before the first dispatch, an exact opaque key must have no transcript
	# to inherit. Never clear/reseed an unexpected preexisting conversation.
	if sess.get_session_messages(binding.session_key, limit=1, timeout_s=5) != []:
		_refuse(binding)
	frappe.db.set_value(
		c.BINDING, binding.name, {"runtime_session_id": physical_id, "runtime_policy_hash": policy.digest()}
	)
	return physical_id


def verify_before_send(sess, binding: c.ActorBinding, policy: RuntimePolicy) -> str:
	metadata = _metadata(binding)
	physical_id = metadata.runtime_session_id
	if not physical_id or metadata.runtime_policy_hash != policy.digest():
		_refuse(binding)
	try:
		response = _read(sess, binding, physical_id)
	except AgentUnreachableError as exc:
		if exc.code != "INVALID_REQUEST":
			raise
		_refuse(binding)
	if not _valid(response, binding, policy, physical_id):
		_refuse(binding)
	return physical_id


def verify_response(binding, policy, physical_id, response):
	"""The nonblocking pump's resolved CAS read; performs no socket operation."""
	metadata = _metadata(binding)
	if (
		metadata.runtime_session_id != physical_id
		or metadata.runtime_policy_hash != policy.digest()
		or not _valid(response, binding, policy, physical_id)
	):
		_refuse(binding)
	return physical_id
