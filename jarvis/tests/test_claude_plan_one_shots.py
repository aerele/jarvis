"""One-shot turns (auto-title, prewarm) on the Claude plan (admin-v2#657).

A Claude-plan turn runs in its own claude-cli process that the agent keeps alive after the
turn, and a one-shot runs on its own session, so no real turn reuses it. On that runtime the
title comes from the first real message instead of a model turn, and prewarm is skipped.
Every other runtime keeps the LLM title and the warm."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import title
from jarvis.chat.turn_handler import NATIVE_CLAUDE_PROVIDER, one_shot_runs_on_claude_plan


def _row(model, provider="openai", cred="api_key", order=0, upstream=None, enabled=1):
	r = frappe._dict(
		enabled=enabled,
		provider=provider if cred == "api_key" else "",
		model=model,
		tier="strong",
		order=order,
		base_url="",
		credential_type=cred,
		rotation="sticky",
		api_key="sk-test" if cred == "api_key" else "",
		subscription_accounts="",
	)
	if cred == "subscription":
		r.subscription_accounts = json.dumps(
			[{"upstream": upstream, "account_ref": f"acct-{model}", "label": "x", "oauth_blob": "{}"}]
		)
	return r


def _settings(*rows):
	return frappe._dict(models=list(rows), preset="", proxy_active=0)


CLAUDE_PLAN = _row("claude-opus-5", cred="subscription", upstream="anthropic", order=0)
CHATGPT_PLAN = _row("gpt-5.6-terra", cred="subscription", upstream="openai", order=1)


class TestOneShotRunsOnClaudePlan(FrappeTestCase):
	def test_no_claude_plan_is_never_claude(self):
		s = _settings(_row("gpt-5.5", order=0), _row("claude-opus-5", provider="anthropic", order=1))
		self.assertFalse(one_shot_runs_on_claude_plan(s, "", None))
		self.assertFalse(one_shot_runs_on_claude_plan(s, "claude-opus-5", None))

	def test_no_pin_follows_the_pool_head(self):
		self.assertTrue(one_shot_runs_on_claude_plan(_settings(CLAUDE_PLAN, CHATGPT_PLAN), "", None))
		chatgpt_first = _row("gpt-5.6-terra", cred="subscription", upstream="openai", order=0)
		claude_second = _row("claude-opus-5", cred="subscription", upstream="anthropic", order=1)
		self.assertFalse(one_shot_runs_on_claude_plan(_settings(claude_second, chatgpt_first), "", None))

	def test_a_named_model_follows_its_row(self):
		s = _settings(CLAUDE_PLAN, CHATGPT_PLAN)
		self.assertTrue(one_shot_runs_on_claude_plan(s, "claude-opus-5", None))
		self.assertFalse(one_shot_runs_on_claude_plan(s, "gpt-5.6-terra", None))

	def test_a_native_claude_pin_is_claude(self):
		s = _settings(CLAUDE_PLAN, CHATGPT_PLAN)
		self.assertTrue(one_shot_runs_on_claude_plan(s, "claude-sonnet-5", NATIVE_CLAUDE_PROVIDER))

	def test_a_disabled_claude_plan_does_not_count(self):
		off = _row("claude-opus-5", cred="subscription", upstream="anthropic", order=0, enabled=0)
		self.assertFalse(one_shot_runs_on_claude_plan(_settings(off, CHATGPT_PLAN), "", None))


class TestPlainTitle(FrappeTestCase):
	def _title(self, *messages):
		with patch.object(title, "_opening_user_texts", return_value=list(messages)):
			return title.plain_title("c1")

	def test_first_real_message_after_greetings(self):
		self.assertEqual(
			self._title("hi", "Hello Jarvis!", "show stock of item A-100"), "Show stock of item A-100"
		)

	def test_only_greetings_leave_no_title(self):
		self.assertEqual(self._title("hi", "hello there"), "")

	def test_long_message_is_cut_at_a_word(self):
		msg = "can you list every pending customer invoice for this month grouped by territory please"
		got = self._title(msg)
		self.assertLessEqual(len(got), title._TITLE_MAX_LEN)
		self.assertTrue(msg.lower().startswith(got.lower()))
		self.assertEqual(msg[len(got)], " ")  # ended on a word boundary
		self.assertTrue(got.startswith("Can you list"))

	def test_first_line_only_and_trailing_dot_dropped(self):
		self.assertEqual(self._title("reset my password.\nmore detail here"), "Reset my password")


class TestPlainAutotitle(FrappeTestCase):
	def test_plain_title_never_calls_the_model(self):
		with (
			patch.object(title, "plain_title", return_value="Reset my password"),
			patch.object(title, "_generate_via_gateway") as gen,
			patch.object(frappe.db, "get_value", return_value="New chat"),
			patch.object(frappe.db, "set_value") as setv,
			patch.object(frappe.db, "commit"),
			patch.object(title, "publish_to_user"),
		):
			title.maybe_autotitle("c1", "u1", gateway_url="ws://x", model="m", provider="p", plain=True)
		gen.assert_not_called()
		setv.assert_called_once()
		self.assertIn("Reset my password", setv.call_args[0])

	def test_job_passes_plain_for_a_claude_plan_chat_only(self):
		settings = frappe._dict(agent_url="http://gw.example", models=[CLAUDE_PLAN, CHATGPT_PLAN])
		for model, want in (("claude-opus-5", True), ("gpt-5.6-terra", False)):
			with (
				patch("jarvis.chat.title.frappe.get_single", return_value=settings),
				patch("jarvis.chat.title.frappe.db.exists", return_value=True),
				patch("jarvis.chat.title.frappe.get_doc", return_value=frappe._dict()),
				patch("jarvis.chat.turn_handler._resolve_model_and_provider", return_value=(model, None)),
				patch.object(title, "maybe_autotitle") as run,
			):
				title.autotitle_job("c1", "u1")
			self.assertEqual(run.call_args.kwargs["plain"], want, model)


class TestPrewarmOnClaudePlan(FrappeTestCase):
	def setUp(self):
		from jarvis.chat import prewarm

		self.prewarm = prewarm
		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		cold = patch.object(prewarm, "_prefix_recently_used", return_value=False)
		cold.start()
		self.addCleanup(cold.stop)

	def tearDown(self):
		frappe.cache().delete_value(self.prewarm._warm_cooldown_key())
		frappe.cache().delete_value(self.prewarm._warm_last_key())

	def _warm(self, settings):
		with (
			patch("jarvis.chat.prewarm.AgentSession") as agent,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=settings),
		):
			sess = MagicMock()
			sess.create_session.return_value = "sk_throwaway"  # cached by warm_prefix, so a str
			agent.connect.return_value = sess
			fired = self.prewarm.warm_prefix()
		return fired, agent

	def test_claude_plan_head_skips_without_connecting(self):
		s = frappe._dict(
			agent_url="https://gw.example",
			llm_model="claude-opus-5",
			llm_auth_mode="subscription",
			llm_provider="Anthropic",
			models=[CLAUDE_PLAN, CHATGPT_PLAN],
		)
		fired, agent = self._warm(s)
		self.assertFalse(fired)
		agent.connect.assert_not_called()
		# the full cooldown is armed so chat-surface loads stop enqueueing skip-only jobs
		self.assertTrue(frappe.cache().get_value(self.prewarm._warm_cooldown_key()))

	def test_chatgpt_head_still_warms(self):
		chatgpt_first = _row("gpt-5.6-terra", cred="subscription", upstream="openai", order=0)
		claude_second = _row("claude-opus-5", cred="subscription", upstream="anthropic", order=1)
		s = frappe._dict(
			agent_url="https://gw.example",
			llm_model="gpt-5.6-terra",
			llm_auth_mode="subscription",
			llm_provider="OpenAI",
			models=[chatgpt_first, claude_second],
		)
		fired, agent = self._warm(s)
		self.assertTrue(fired)
		agent.connect.assert_called_once()
