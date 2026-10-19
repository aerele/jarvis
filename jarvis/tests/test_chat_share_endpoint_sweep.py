"""A shared identifier grants no authority at an existing Jarvis endpoint.

The registry walk is runtime based, so decorator aliases and re-exports are
included. Every probe has a savepoint and a final snapshot even on exceptions.
Outbound boundaries are closed BEFORE imports as well as during invocation.
"""

import hashlib
import importlib
import inspect
import json
import pkgutil
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import frappe

import jarvis
from jarvis.tests._share_helpers import MESSAGE, OWNER, RECIPIENT, SHARE, as_user, make_message, payload
from jarvis.tests.share_endpoint_manifest import ENDPOINTS
from jarvis.tests.test_chat_sharing import SharingCase


class SweepBoundary(RuntimeError):
	"""The invocation attempted a transaction boundary or external side effect."""


@contextmanager
def no_delivery():
	"""Record attempted effects, then fail closed instead of making a live call."""
	observed = []

	def blocked(kind):
		def record(*args, **kwargs):
			observed.append((kind, args, kwargs))
			raise SweepBoundary(kind)

		return record

	with ExitStack() as stack:
		for target in (
			"frappe.enqueue",
			"frappe.publish_realtime",
			"requests.sessions.Session.request",
			"jarvis.admin_client._post",
			"jarvis.admin_client._post_guest",
			"jarvis.admin_client._authenticated_raw",
			"jarvis.chat.agent_client.AgentSession.connect",
			"jarvis.chat.agent_client.AgentSession._request",
			"jarvis.agent_ws.ping",
			"jarvis.triggers.llm_task_client.llm_task_complete",
			"websocket.create_connection",
			"subprocess.Popen",
			"socket.create_connection",
		):
			stack.enter_context(patch(target, side_effect=blocked(target)))
		yield observed


def all_endpoints():
	"""Import every package/module, with no test collection or patch execution."""
	with no_delivery():
		for info in pkgutil.walk_packages(jarvis.__path__, prefix="jarvis."):
			if ".tests" not in info.name:
				importlib.import_module(info.name)
	return {
		f"{fn.__module__}.{fn.__name__}": fn
		for fn in frappe.whitelisted
		if getattr(fn, "__module__", "").startswith("jarvis.") and ".tests" not in fn.__module__
	}


def substitute(value, identifiers):
	if isinstance(value, str):
		return identifiers.get(value, value)
	if isinstance(value, list):
		return [substitute(v, identifiers) for v in value]
	if isinstance(value, dict):
		return {k: substitute(v, identifiers) for k, v in value.items()}
	return value


def shared_snapshot(conversation):
	rows = {}
	for doctype, filters in (
		("Jarvis Conversation", {"name": conversation}),
		(MESSAGE, {"conversation": conversation}),
		(SHARE, {"conversation": conversation}),
		("Jarvis Chat Turn", {"conversation": conversation}),
		("Jarvis Macro Run", {"conversation": conversation}),
		("Jarvis Approval Request", {"conversation": conversation}),
		("Jarvis Pending Action", {"conversation": conversation}),
		("Jarvis Pending Action Waiter", {"conversation": conversation}),
		# A recipient's durable claim may name a requested conversation before
		# the owner gate runs. It grants no dispatch or owner receipt access.
		("Jarvis Chat Send Request", {"conversation": conversation, "owner": OWNER}),
		("Comment", {"reference_doctype": "Jarvis Conversation", "reference_name": conversation}),
		("File", {"attached_to_doctype": "Jarvis Conversation", "attached_to_name": conversation}),
		("DocShare", {"share_doctype": "Jarvis Conversation", "share_name": conversation}),
	):
		rows[doctype] = frappe.get_all(doctype, filters=filters, fields=["*"], order_by="name")
	full = hashlib.sha256(payload(rows).encode()).hexdigest()
	seen = {}
	for row in rows[SHARE]:
		if row.user == RECIPIENT:
			seen[row.name] = row.pop("seen_marker", None)
	return {"full": full, "without_seen": hashlib.sha256(payload(rows).encode()).hexdigest(), "seen": seen}


def only_seen_advanced(before, after):
	if before["without_seen"] != after["without_seen"] or set(before["seen"]) != set(after["seen"]):
		return False
	return all(
		new is not None and (old is None or new >= old)
		for name, old in before["seen"].items()
		for new in [after["seen"][name]]
	)


# These are the newly authorized reads. Their dedicated tests enforce exact
# allowlists/scrubbing/file checks; the sweep is about privilege escalation.
EXEMPTIONS = {
	"jarvis.chat.sharing.get_shared_conversation": "The explicitly authorized, projected read; seen_marker may advance.",
	"jarvis.chat.sharing.get_shared_tool_detail": "The explicitly authorized, projected tool read.",
	"jarvis.chat.sharing.get_shared_canvas": "The explicitly authorized sanitized static artifact read.",
	"jarvis.chat.sharing.get_shared_file": "The explicitly authorized owner-readable canvas attachment download.",
	"jarvis.chat.sharing.list_shared_with_me": "The recipient's metadata-only shared-chat index.",
	"jarvis.chat.sharing.is_shared_with_me": "Boolean membership of the recipient's own share.",
}

# The generic value sweep intentionally passes a real MESSAGE/RUN id where a
# conversation/tool name is expected. A literal missing-name echo is not an
# owner record read. These exemptions never permit a mutation/outbound effect.
REFLECTION_EXEMPTIONS = {
	"jarvis.api.call_tool": ("ToolNotFoundError", "Only reflects the caller-supplied unknown tool name."),
	"jarvis.chat.api.set_conversation_model": (
		"unknown_conversation",
		"Only reflects the caller-supplied missing conversation name.",
	),
	"jarvis.chat.api.set_conversation_thinking": (
		"unknown_conversation",
		"Only reflects the caller-supplied missing conversation name.",
	),
}

# Support forwards the recipient's input to a separate, identity-scoped ticket
# service. It never resolves that string as a Jarvis conversation. Match the
# exact attempted boundary payload rather than exempting arbitrary sends.
SUPPORT_EXEMPTIONS = {
	"jarvis.support.api.close_ticket": ("support.api.close_ticket", ("ticket",)),
	"jarvis.support.api.create_ticket": ("support.api.create_ticket", ("subject", "body")),
	"jarvis.support.api.get_thread": ("support.api.get_thread", ("ticket",)),
	"jarvis.support.api.reply": ("support.api.reply", ("ticket", "body")),
	"jarvis.support.media.download": ("support.media.download", ("ticket", "file_url")),
}


def permitted_reflection(name, args, result):
	if name not in REFLECTION_EXEMPTIONS or not isinstance(result, dict):
		return False
	if set(result) != {"ok", "error"} or result["ok"] is not False:
		return False
	error = result["error"]
	if not isinstance(error, dict) or not set(error) <= {"code", "message", "kind"}:
		return False
	code = REFLECTION_EXEMPTIONS[name][0]
	if error.get("code") != code:
		return False
	expected = (
		f"no such tool: {args.get('tool')}"
		if code == "ToolNotFoundError"
		else f"conversation {args.get('conversation')!r} not found"
	)
	return error.get("message") == expected


def permitted_support_input(name, args, observed):
	if name not in SUPPORT_EXEMPTIONS or not observed:
		return False
	endpoint, fields = SUPPORT_EXEMPTIONS[name]
	expected = {field: args.get(field, "") for field in fields} | {
		"requesting_user": RECIPIENT,
		"scope": "own",
	}
	if name.endswith("create_ticket"):
		expected["tenant_id"] = None
	for kind, positional, kwargs in observed:
		if kind == "jarvis.admin_client._post":
			path, body = kwargs.get("path", ""), kwargs.get("body")
		elif kind == "jarvis.admin_client._authenticated_raw" and len(positional) >= 2:
			path, body = positional[:2]
		else:
			return False
		if not path.endswith("." + endpoint) or body != expected:
			return False
	return True


class TestShareEndpointSweep(SharingCase):
	def test_probe_detects_response_mutation_and_outbound_even_when_refused(self):
		def leak():
			return {"content": "Shared transcript sentinel"}

		def mutate_then_refuse():
			frappe.db.set_value("Jarvis Conversation", self.conversation, "title", "unauthorized edit")
			raise frappe.PermissionError()

		def outbound_then_refuse():
			try:
				frappe.enqueue("jarvis.chat.api.send_message", conversation=self.conversation)
			except SweepBoundary:
				raise frappe.PermissionError() from None

		for fn in (leak, mutate_then_refuse, outbound_then_refuse):
			with self.subTest(control=fn.__name__), self.assertRaises(AssertionError):
				self.probe(
					"deliberately_unsafe_control", fn, {}, {self.conversation, "Shared transcript sentinel"}
				)

		for fn in (mutate_then_refuse, outbound_then_refuse):
			with self.subTest(read_control=fn.__name__), self.assertRaises(AssertionError):
				self.probe("jarvis.chat.sharing.get_shared_conversation", fn, {}, {self.conversation})

	def test_runtime_registry_is_fully_and_explicitly_classified(self):
		endpoints = all_endpoints()
		self.assertGreater(len(endpoints), 400)
		self.assertEqual(set(endpoints), set(ENDPOINTS))
		for name, entry in ENDPOINTS.items():
			with self.subTest(endpoint=name):
				self.assertIn(entry["scope"], {"chat_scoped", "not_chat_scoped"})
				if entry["scope"] == "chat_scoped":
					self.assertIsInstance(entry["arguments"], dict)
					inspect.signature(endpoints[name]).bind(
						**substitute(entry["arguments"], self.identifiers())
					)
				else:
					self.assertTrue(entry["reason"].strip())

	def identifiers(self):
		if hasattr(self, "fixture_identifiers"):
			return self.fixture_identifiers
		from jarvis.chat import pending_actions

		with as_user(OWNER):
			macro = frappe.get_doc(
				{
					"doctype": "Jarvis Macro",
					"macro_name": "Share sweep routine",
					"steps": [{"prompt": "Read a record"}],
				}
			).insert(ignore_permissions=True)
			run = frappe.get_doc(
				{
					"doctype": "Jarvis Macro Run",
					"macro": macro.name,
					"conversation": self.conversation,
					"status": "running",
					"current_step": 1,
					"total_steps": 1,
					"run_mode": "stepped",
					"trigger": "manual",
				}
			).insert(ignore_permissions=True)
			approval = frappe.get_doc(
				{
					"doctype": "Jarvis Approval Request",
					"title": "Shared approval sentinel",
					"question": "Continue?",
					"conversation": self.conversation,
					"status": "Pending",
					"options": '["Yes","No"]',
				}
			).insert(ignore_permissions=True)
			# Park's production helper owns a transaction. Only fixture creation
			# suppresses commits; probe invocations below instead RAISE on them.
			with patch.object(frappe.db, "commit"):
				pending = pending_actions.park(
					kind="file_box_held",
					owner_user=OWNER,
					exec_user=OWNER,
					tool="add_comment",
					args={
						"reference_doctype": "Jarvis Conversation",
						"reference_name": self.conversation,
						"content": "Shared held-action sentinel",
					},
					conversation=self.conversation,
					card={"title": "Add a comment"},
					preview={"would": {"doctype": "Comment"}},
					summary="Shared held-action sentinel",
					locked=True,
				)
			tool = make_message(
				self.conversation,
				role="tool",
				tool_name="get_doc",
				tool_status="completed",
				tool_args='{"doctype":"Jarvis Conversation"}',
				tool_result='{"result":"Shared tool data sentinel"}',
				tool_call_id=pending,
				tool_call_ids=json.dumps([pending]),
			)
			assistant = make_message(
				self.conversation,
				role="assistant",
				streaming=1,
				content="Shared assistant sentinel",
				ref_doctype="Jarvis Macro Run",
				ref_name=run.name,
				canvas=json.dumps(
					[
						{
							"name": "artifact",
							"type": "file",
							"title": "Attachment",
							"file_url": "/private/files/share-probe.txt",
						}
					]
				),
			)
			turn = frappe.get_doc(
				{
					"doctype": "Jarvis Chat Turn",
					"run_id": "share-probe-turn",
					"conversation": self.conversation,
					"relay_target_id": "share-probe-target",
					"turn_class": "interactive",
					"state": "streaming",
					"seed_message": self.message,
					"assistant_message": assistant,
					"pump_epoch": 1,
					"enqueued_at": frappe.utils.now_datetime(),
				}
			).insert(ignore_permissions=True)
			comment = frappe.get_doc(
				{
					"doctype": "Comment",
					"comment_type": "Comment",
					"reference_doctype": "Jarvis Conversation",
					"reference_name": self.conversation,
					"content": "Shared comment sentinel",
				}
			).insert(ignore_permissions=True)
			file = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "share-probe.txt",
					"file_url": "/private/files/share-probe.txt",
					"is_private": 1,
					"attached_to_doctype": "Jarvis Conversation",
					"attached_to_name": self.conversation,
				}
			)
			file.set_new_name()
			file.db_insert()
			request_id = "5" * 32
			frappe.get_doc(
				{
					"doctype": "Jarvis Chat Send Request",
					"request_key": hashlib.sha256(f"{OWNER}:{request_id}".encode()).hexdigest(),
					"fingerprint": "share-probe-fingerprint",
					"conversation": self.conversation,
					"state": "settled",
					"result_json": json.dumps(
						{
							"conversation_id": self.conversation,
							"message_id": self.message,
							"run_id": turn.name,
						}
					),
				}
			).insert(ignore_permissions=True)
		self.fixture_identifiers = {
			"$conversation": self.conversation,
			"$message": tool,
			"$recipient": RECIPIENT,
			"$outsider": "share-outsider@example.com",
			"$run": turn.name,
			"$macro_run": run.name,
			"$approval": approval.name,
			"$pending_action": pending,
			"$request_id": request_id,
			"$token": pending,
			"$comment": comment.name,
			"$file": file.name,
			"$file_url": "/private/files/share-probe.txt",
			"$share": "share-probe-share",
		}
		return self.fixture_identifiers

	def probe(self, name, fn, args, needles):
		frappe.db.savepoint("share_probe")
		before = shared_snapshot(self.conversation)
		callbacks = list(frappe.db.after_commit._functions)
		result = None
		error = None
		observed = []
		try:
			with (
				no_delivery() as observed,
				patch.object(frappe.db, "commit", side_effect=SweepBoundary("commit")),
				patch.object(frappe.db, "rollback", side_effect=SweepBoundary("rollback")),
			):
				try:
					result = fn(**args)
				except Exception as exc:
					error = exc
			# Always inspect mutations AFTER the call and BEFORE restoring its
			# savepoint, even when validation/commit barriers raised.
			after = shared_snapshot(self.conversation)
			changed = before["full"] != after["full"]
			response = payload(result) + payload(dict(frappe.local.response))
			leak = any(needle in response for needle in needles)
			outbound = any(needle in payload(observed) for needle in needles)
			finding = (
				changed
				or outbound
				or (leak and not isinstance(error, (frappe.PermissionError, frappe.DoesNotExistError)))
			)
			if finding:
				exempt = (
					(
						name in EXEMPTIONS
						and not outbound
						and (
							not changed
							or (
								name == "jarvis.chat.sharing.get_shared_conversation"
								and only_seen_advanced(before, after)
							)
						)
					)
					or (not changed and not outbound and permitted_reflection(name, args, result))
					or (not changed and not leak and permitted_support_input(name, args, observed))
				)
				self.assertTrue(
					exempt,
					f"{name}: changed={changed}, data={leak}, outbound={outbound}, exception={type(error).__name__}; input fields={sorted(args)}",
				)
		finally:
			# Restore the real transaction methods before ending the savepoint.
			frappe.db.rollback(save_point="share_probe")
			frappe.db.after_commit._functions.clear()
			frappe.db.after_commit._functions.extend(callbacks)
			frappe.local.response.clear()

	def test_shared_identifiers_grant_no_action_or_unprojected_read(self):
		from jarvis.chat.sharing import get_shared_conversation

		self.share()
		self.identifiers()
		self.fixture_identifiers["$share"] = frappe.db.get_value(
			SHARE, {"conversation": self.conversation, "user": RECIPIENT}
		)
		with as_user(RECIPIENT):
			projected = get_shared_conversation(self.conversation)
			values = {self.conversation, self.message}
			for row in projected["messages"]:
				for key in ("name", "run_id", "tool_call_id", "ref_name"):
					if isinstance(row.get(key), str) and row[key]:
						values.add(row[key])
				for item in row.get("canvas") or []:
					if item.get("file_url"):
						values.add(item["file_url"])
				ids = row.get("tool_call_ids") or []
				if isinstance(ids, str):
					try:
						ids = json.loads(ids)
					except ValueError:
						ids = []
				values.update(v for v in ids if isinstance(v, str))
			endpoints = all_endpoints()
			for name, fn in sorted(endpoints.items()):
				entry = ENDPOINTS[name]
				base = substitute(entry.get("arguments", {}), self.identifiers())
				# Supply all mandatory parameters for the generic value sweep;
				# chat-scoped invokers above additionally use real nested payloads.
				for p in inspect.signature(fn).parameters.values():
					if p.default is inspect.Parameter.empty and p.kind not in (
						p.VAR_POSITIONAL,
						p.VAR_KEYWORD,
					):
						base.setdefault(p.name, 1 if "int" in str(p.annotation) else "share-probe")
				cases = [base]
				for p in inspect.signature(fn).parameters.values():
					annotation = str(p.annotation)
					for value in sorted(values):
						if "str" in annotation:
							cases.append(base | {p.name: value})
						if "dict" in annotation:
							cases.extend(
								(
									base | {p.name: {"conversation": value}},
									base | {p.name: {"message": value}},
								)
							)
				seen = set()
				for args in cases:
					if payload(args) in seen:
						continue
					seen.add(payload(args))
					with self.subTest(endpoint=name, fields=list(args)):
						self.probe(name, fn, args, values | {"Shared transcript sentinel"})

	def test_existing_system_manager_admission_privilege_is_documented(self):
		from jarvis.chat.admission import _assert_owner
		from jarvis.tests._share_helpers import MANAGER

		with as_user(MANAGER):
			_assert_owner(self.conversation)
