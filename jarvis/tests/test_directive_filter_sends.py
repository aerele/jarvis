"""F-Q8 structural test (F9, AC-FQ8-2, AC-FQ8-10).

Every call that sends a message to the gateway (``chat.send`` or the ``agent`` RPC), and every
function that produces such a message, is listed here with the reason it is safe. A NEW sender
fails the test until it is added with its reason (and its filter). Lines are not used: the key is
file + enclosing function + call, so a refactor does not break the list.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from frappe.tests.utils import FrappeTestCase

APP = pathlib.Path(__file__).resolve().parents[1]

SEND_NAMES = {
	"chat_send",
	"send_chat",
	"fire_agent",
	"stream_agent_turn",
	"compact_session",
	"post_agent_run",
}
RPC_NAMES = {"_request", "_send", "issue_rpc"}
RPC_METHODS = {"chat.send", "agent"}

# (file, enclosing function, call) -> why the text that it sends is safe.
ALLOWED = {
	(
		"chat/agent_client.py",
		"AgentSession.chat_send",
		"_request(chat.send)",
	): "the transport; senders are listed below",
	(
		"chat/relay_mux.py",
		"RelayMux.send_chat",
		"issue_rpc(chat.send)",
	): "the transport; senders are listed below",
	(
		"chat/turn_handler.py",
		"handle_chat_send",
		"chat_send",
	): "legacy: the text is _ap.user_message, from guard_turn_message",
	(
		"chat/pump.py",
		"_dispatch_one",
		"send_chat",
	): "personal pump: the payload message, written by prepare from guard_turn_message; none -> turn ends",
	(
		"chat/collaboration_pump.py",
		"dispatch_one",
		"send_chat",
	): "shared pump: the payload message from build_payload (assert_clean)",
	(
		"chat/collaboration_rq.py",
		"handle",
		"chat_send",
	): "shared RQ: the payload message from build_payload (assert_clean)",
	(
		"chat/agent_client.py",
		"AgentSession.compact_session",
		"chat_send",
	): "'/compact ' + a hint that sanitize_hint checked",
	(
		"chat/compaction.py",
		"run_compact",
		"compact_session",
	): "the hint comes from sanitize_hint (compact_conversation)",
	("chat/prewarm.py", "warm_prefix", "fire_agent"): "fixed jarvis text '/think off warmup'",
	(
		"chat/agent_client.py",
		"AgentSession.fire_agent",
		"_request(agent)",
	): "the agent RPC; its one caller is the prewarm",
	("chat/agent_scheduler.py", "_launch_audit", "post_agent_run"): "the prompt goes through clean_added",
	# The agent RPC does not run the host's directive parser: the agent-command chunk graph of
	# The agent runtime 9.3 has no import of get-reply (parseInlineSessionDirectives lives only there).
	# Re-check at every pin bump (AC-FQ8-4). The text of these is jarvis-built one-shot prompts.
	(
		"chat/agent_client.py",
		"AgentSession.stream_agent_turn",
		"_send(agent)",
	): "agent RPC (no directive parser, see above)",
	(
		"chat/title.py",
		"_generate_via_gateway",
		"stream_agent_turn",
	): "agent RPC (no directive parser, see above)",
	(
		"chat/suggestions.py",
		"_generate_via_gateway",
		"stream_agent_turn",
	): "agent RPC (no directive parser, see above)",
	(
		"learning/polish.py",
		"_run_gateway_turn",
		"stream_agent_turn",
	): "agent RPC (no directive parser, see above)",
	(
		"preflight.py",
		"_drain_probe_stream._drain",
		"stream_agent_turn",
	): "agent RPC with a fixed probe prompt",
}


def _senders() -> set[tuple[str, str, str]]:
	found = set()
	for path in sorted(APP.rglob("*.py")):
		rel = path.relative_to(APP)
		if "tests" in rel.parts:
			continue
		tree = ast.parse(path.read_text(encoding="utf-8"))

		def visit(node, stack):
			for child in ast.iter_child_nodes(node):
				inner = stack
				if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
					inner = [*stack, child.name]
				if isinstance(child, ast.Call):
					func = child.func
					name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
					if name in SEND_NAMES:
						found.add((str(rel), ".".join(stack), name))
					elif (
						name in RPC_NAMES
						and child.args
						and isinstance(child.args[0], ast.Constant)
						and child.args[0].value in RPC_METHODS
					):
						found.add((str(rel), ".".join(stack), f"{name}({child.args[0].value})"))
				visit(child, inner)

		visit(tree, [])
	return found


class TestEverySenderIsListed(FrappeTestCase):
	def test_no_unlisted_sender_and_no_stale_entry(self):
		found = _senders()
		self.assertEqual(
			sorted(found - set(ALLOWED)),
			[],
			"a new gateway sender: add it to ALLOWED with the reason its text is safe (and its filter)",
		)
		self.assertEqual(sorted(set(ALLOWED) - found), [], "stale ALLOWED entry: the sender is gone")


class TestProducersCallTheFilter(FrappeTestCase):
	def _source(self, dotted: str) -> str:
		module_name, _, attr = dotted.rpartition(".")
		import importlib

		return inspect.getsource(getattr(importlib.import_module(module_name), attr))

	def test_assembly_refuses_typed_text_and_guards_the_whole_prompt(self):
		src = self._source("jarvis.chat.turn_handler._assemble_prompt")
		self.assertIn("directive_filter.refuse_typed(", src)
		self.assertIn("directive_filter.guard_turn_message(", src)
		self.assertIn("directive_filter.clean_added(", src)  # the shared parts

	def test_shared_payload_verifies_the_refit(self):
		self.assertIn(
			"directive_filter.assert_clean(", self._source("jarvis.chat.collaboration_workers.build_payload")
		)

	def test_compact_hint_is_checked(self):
		self.assertIn("directive_filter.refuse_typed(", self._source("jarvis.chat.compaction.sanitize_hint"))

	def test_scheduled_prompt_is_cleaned(self):
		self.assertIn(
			"directive_filter.clean_added(", self._source("jarvis.chat.agent_scheduler._launch_audit")
		)

	def test_pump_never_reads_the_seed_text_and_ends_an_empty_dispatch_before_the_send(self):
		self.assertNotIn("seed_message", self._source("jarvis.chat.pump._load_dispatch"))
		src = self._source("jarvis.chat.pump._dispatch_one")
		self.assertLess(src.index("_end_without_message("), src.index('"run:start"'))
		self.assertLess(src.index('"run:start"'), src.index("send_chat("))

	def test_the_shared_payload_is_built_in_one_place(self):
		# collaboration_pump / collaboration_rq read payload["message"]; only build_payload writes it.
		offenders = []
		for path in sorted(APP.rglob("*.py")):
			rel = path.relative_to(APP)
			if "tests" in rel.parts or str(rel) in {"chat/collaboration_workers.py", "chat/prepare.py"}:
				continue
			text = path.read_text(encoding="utf-8")
			if '"message": ap.user_message' in text:
				offenders.append(str(rel))
		self.assertEqual(offenders, [])
