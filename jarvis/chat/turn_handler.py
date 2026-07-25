"""Shared chat-turn body. The single source of truth for "what happens
during one chat turn": DB writes, openclaw pool checkout, event
streaming, canvas persist, auto-title.

Managed transport is the openclaw-native relay (never-error discipline):
chat.send with idempotencyKey = the bench run_id hands the turn to
openclaw, relay_turn_events streams the broadcast frames, and the ONLY
path to run:error after a successful ack is a genuine openclaw-reported
terminal. Deadline expiry, transport drops, and exhausted streams park
the row (_mark_recovering) and try recover_now immediately; the
turn_recovery cron and its 60-minute ceiling are the backstop. See
docs/superpowers/specs/2026-07-04-openclaw-native-chat-relay-design.md
(jarvis repo root).

Today this is called only by ``jarvis.chat.worker.run_agent_turn``,
which is the RQ entry point invoked via
``frappe.enqueue("jarvis.chat.worker.run_agent_turn", ...)``. Phase 2 of
the chat-bridge refactor will add a second caller (a gevent subscriber
inside Frappe's Python realtime process) that activates only when
``socketio_backend == "python"`` is set in ``common_site_config.json``.
Both callers funnel through ``handle_chat_send`` so there is no duplicated
turn logic. See ``docs/superpowers/specs/2026-06-24-chat-bridge-
architecture-design.md`` for the bigger picture.

Test compatibility: the legacy unit tests patch
``jarvis.chat.worker.publish_to_user`` directly. To keep those tests
passing without modification, the publish call here goes through
``_publish_to_user`` which looks the name up on the worker module at
call time, so the patch on the worker module still wins.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import frappe

from jarvis.chat import openclaw_session_pool, vision
from jarvis.exceptions import OpenclawUnreachableError

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"

# Batch-commit thresholds for the assistant-content writes during a
# streamed turn. Sprint-5 punch-list "Worker writes assistant.content on
# every delta with full overwrite + commit per token" (2026-06-16
# review): the previous shape called frappe.db.set_value + frappe.db.
# commit() for every single token, so a 500-token response = 500
# write+commit cycles. The DB write itself is cheap (one Singles row);
# the commit is where the cost lives - it forces a per-token transaction
# round-trip.
#
# Now: buffer the latest cumulative text in memory, flush (write+commit)
# when EITHER threshold trips, AND flush before any tool/lifecycle event
# fires so the on-disk message-row order matches the realtime channel.
# Customer experience stays unchanged - the realtime
# ``assistant:delta`` publish still fires on every token, so the UI
# animates token-by-token. The DB just catches up in batches.
#
# Numbers picked for the punch-list "every N=10 events or 250ms" ask:
# at typical chat throughputs (1-50 tokens/s) this means 1-2 commits
# per second instead of 1-50, but at most ~10 tokens of unwritten
# content if the worker crashes mid-batch (recoverable next stream).
_ASSISTANT_BATCH_SIZE = 10
_ASSISTANT_BATCH_INTERVAL_MS = 250

# chat.send ack budget. openclaw ingests and PREPROCESSES the whole message
# before it acks the RPC, so this window has to cover the payload, not just
# the WS round-trip. Two contributions, because a message grows two ways:
#   - vision parts: each PDF page is rasterized + resized inside the RPC
#     (measured 22s for a 4-page invoice).
#   - inlined text: a CSV/TXT/JSON/MD attachment is folded into the prompt
#     body (up to _MAX_INLINE_CHARS) and never becomes a vision part, so it
#     used to add nothing to the budget while adding ~20k chars to the send.
#     That is the "timed out in the UI, answered in the transcript" split
#     brain: the bench gave up at 10s, openclaw completed the turn anyway.
# The base also absorbs cold-session bootstrap (persona + skills catalog) on
# a first turn, which is when the old flat 10s was tightest.
_ACK_TIMEOUT_BASE_S = 30.0
_ACK_TIMEOUT_PER_VISION_PART_S = 30.0
_ACK_TIMEOUT_PER_INLINED_CHAR_S = 10.0 / 10_000  # ~10s per 10k inlined chars
_ACK_TIMEOUT_CEILING_S = 180.0


def persist_rich_outputs(
	assistant_msg_name: str,
	conversation_id: str,
	user: str,
	run_id: str,
	turn_start_ms: int,
) -> None:
	"""Best-effort canvas + generated-image persistence and publish for one
	finished turn. Shared by the worker's clean exit and snapshot recovery
	(a recovered long turn is exactly the kind that produced charts).
	Managed mode only; never raises."""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		return

	settings = frappe.get_single("Jarvis Settings")

	# Rich outputs: detect any canvas/chart artifact the agent produced this
	# turn (HTML or SVG), fetch it from the gateway, persist it as a private
	# File, and publish a 'canvas' event so the UI renders it inline. Managed
	# mode only; self-hosted chats over the HTTP surface have no gateway
	# canvas route. Failure here never fails the turn.
	try:
		from jarvis.chat import canvas as canvas_mod

		final_content = frappe.db.get_value(MSG, assistant_msg_name, "content") or ""
		canvas_token = settings.get_password("agent_token", raise_exception=False) or ""
		canvas_items = canvas_mod.persist_canvases(
			assistant_msg_name,
			final_content,
			settings.agent_url or "",
			canvas_token,
		)
		if canvas_items:
			_publish_to_user(
				user,
				{
					"kind": "canvas",
					"conversation_id": conversation_id,
					"message_id": assistant_msg_name,
					"run_id": run_id,
					"items": canvas_items,
				},
			)
	except Exception:
		frappe.log_error(
			title="chat worker: canvas persist failed",
			message=frappe.get_traceback(),
		)

	# Generated images: codex imagegen writes them on the container disk (not
	# the canvas dir, and openclaw neither streams nor serves them), so pull
	# any produced this turn via the fleet agent + persist as ERP Files so
	# they show inline. Gated on the imagegen skill badge to avoid a fleet
	# round-trip on every turn. Failure never fails a turn.
	try:
		final_content = frappe.db.get_value(MSG, assistant_msg_name, "content") or ""
		if "imagegen" in final_content:
			from jarvis.chat import generated_media as gen_media

			gen_items = gen_media.persist_generated_images(
				assistant_msg_name,
				conversation_id,
				turn_start_ms,
			)
			if gen_items:
				_publish_to_user(
					user,
					{
						"kind": "canvas",
						"conversation_id": conversation_id,
						"message_id": assistant_msg_name,
						"run_id": run_id,
						"items": gen_items,
					},
				)
	except Exception:
		frappe.log_error(
			title="chat worker: generated-image persist failed",
			message=frappe.get_traceback(),
		)


def _publish_to_user(user, payload):
	"""Indirection so existing tests that patch
	``jarvis.chat.worker.publish_to_user`` still take effect.

	We deliberately look the symbol up on the worker module at call time
	rather than importing it directly here. The worker module re-exports
	``publish_to_user`` from ``jarvis.chat.events`` (so the patch target
	keeps working) and ``mock.patch("jarvis.chat.worker.publish_to_user")``
	rebinds the attribute on the worker module, which this lookup picks
	up. Without this indirection, a turn-handler-local
	``from jarvis.chat.events import publish_to_user`` would bypass the
	patch and the tests would call the real publish.
	"""
	from jarvis.chat import worker as _worker

	return _worker.publish_to_user(user, payload)


class _AssistantContentBatcher:
	"""Coalesces per-token assistant-content writes into one DB
	write+commit per batch.

	Usage:
	    batcher = _AssistantContentBatcher(assistant_msg.name)
	    for event in stream:
	        if event["kind"] == "assistant":
	            batcher.delta(event["text"])
	            batcher.flush_if_due()
	            continue
	        batcher.flush()  # ordering: flush before any non-delta event
	        _handle_event(event, ...)
	    batcher.flush()      # drain on stream end
	"""

	def __init__(self, msg_name: str):
		self.msg_name = msg_name
		self._pending_text: str | None = None
		self._events_since_flush = 0
		self._last_flush_ms = self._now_ms()

	@staticmethod
	def _now_ms() -> int:
		return int(time.monotonic() * 1000)

	def delta(self, text: str) -> None:
		"""Record the latest cumulative assistant text. Caller decides
		when to actually persist via flush / flush_if_due."""
		self._pending_text = text
		self._events_since_flush += 1

	def flush_if_due(self) -> bool:
		"""Flush iff the size or time threshold is hit. Returns True iff
		a flush actually happened."""
		if self._pending_text is None:
			return False
		if self._events_since_flush >= _ASSISTANT_BATCH_SIZE:
			return self.flush()
		if self._now_ms() - self._last_flush_ms >= _ASSISTANT_BATCH_INTERVAL_MS:
			return self.flush()
		return False

	def flush(self) -> bool:
		"""Flush immediately if any text is pending. Returns True iff a
		flush happened (caller can use this for trace logging)."""
		if self._pending_text is None:
			return False
		frappe.db.set_value(MSG, self.msg_name, "content", self._pending_text)
		frappe.db.commit()
		self._pending_text = None
		self._events_since_flush = 0
		self._last_flush_ms = self._now_ms()
		return True


# Provider label → openclaw provider id sent in the chat WS frame.
#
# Openclaw has two dispatch paths chosen at request time
# (openclaw/src/agents/model-selection-cli.ts:6-20):
#
#   - CLI backend: taken when isCliProvider(provider) returns true.
#     Routes dispatch to a registered CliBackend that spawns an external
#     CLI binary. The CliBackend is keyed by the provider id (so the
#     provider sent here must match the CliBackend's id).
#
#   - Embedded runtime (codex / pi harness): taken otherwise. The codex
#     harness has an allowlist of accepted providers; pi handles
#     everything else but expects HTTP API auth (api key).
#
# The two providers we currently support resolve to two different paths:
#
#   - OpenAI codex: codex IS a registered plugin harness
#     (openclaw/extensions/codex/index.ts:34) whose allowlist accepts
#     "openai" as the model-provider key. Use "openai" for the chat WS
#     frame and the embedded codex-harness path handles the dispatch.
#
#   - Google Gemini: gemini-cli is registered ONLY as a CliBackend
#     (openclaw/extensions/google/cli-backend.ts:16 with id
#     "google-gemini-cli"). Use "google-gemini-cli" verbatim so
#     isCliProvider returns true and dispatch routes via the CLI backend
#     to the gemini binary inside the container. Mapping it to "google"
#     makes isCliProvider return false, dispatch falls into the embedded
#     path, and openclaw errors "No API key found for provider 'google'".
#
# Only used in oauth mode - api_key mode skips this map and lets
# openclaw resolve the single registered models.providers entry.
_PROVIDER_LABEL_TO_OPENCLAW_ID = {
	"OpenAI": "openai",
	"Google Gemini": "google-gemini-cli",
}


# The virtual model the fleet configures openclaw with whenever the proxy is active
# (jarvis-fleet-agent compose.render_openclaw_config(model="jarvis-pool")). Bifrost
# expands it into the pool's failover chain via its catch-all routing rule.
#
# The customer plane has to know this name because CLEARING a pin has to be an explicit
# instruction, not an omission -- see _resolve_model_and_provider. The coupling is soft:
# llm_proxy's catch-all rule matches `true`, so even if the fleet renamed the virtual
# model, an unrecognised id would still fall through to the pool chain rather than break.
POOL_VIRTUAL_MODEL = "jarvis-pool"


def _resolve_model_and_provider(conv) -> tuple[str, str | None]:
	"""Return (effective_model, openclaw_provider_id_or_None) for this conv.

	Pool mode (proxy_active=1): the pinned model, or the pool's virtual model when no
	pin is set. Direct mode: use conv.model_override or settings.llm_model.
	"""
	settings = frappe.get_single("Jarvis Settings")

	if getattr(settings, "proxy_active", 0):
		# Pool mode: Bifrost routes. Use override if it matches an enabled model name.
		enabled_names = {
			(m.model if hasattr(m, "model") else m.get("model", ""))
			for m in (settings.models or [])
			if m.enabled
		}
		override = (conv.model_override or "").strip()
		if override and override in enabled_names:
			return override, None  # Validated override accepted
		return "", None  # Let Bifrost/pool route

	effective_model = conv.model_override or settings.llm_model or ""
	provider = (
		_PROVIDER_LABEL_TO_OPENCLAW_ID.get(settings.llm_provider)
		if settings.llm_auth_mode == "oauth"
		else None
	)
	return effective_model, provider


def _session_model_for(conv) -> tuple[str, str | None]:
	"""The model to PATCH THE SESSION to -- a different question from
	_resolve_model_and_provider's, and the distinction is the whole fix.

	_resolve_model_and_provider answers "what model does this conversation want", where
	"" means "no opinion, let the agent's configured default stand". Two other callers
	rely on exactly that: the auto-title job (chat/title.py) and pattern-polish
	(learning/polish.py) run THROWAWAY one-shot gateway turns and pass the result straight
	through as an inline modelOverride, where "" correctly means "omit the field". Handing
	them a concrete "jarvis-pool" would push an explicit override down a code path they
	have never exercised, and a rejection there degrades silently -- crude titles, raw
	unpolished patterns -- because both callers swallow their exceptions.

	The SESSION patch is stateful in a way those one-shots are not. openclaw remembers a
	session's model across turns (it holds the history server-side), so here "no opinion"
	CANNOT mean "send nothing" -- that is precisely the bug: handle_chat_send only issues
	sessions.patch when the model is truthy, so a conversation that had ONCE been pinned
	was never walked back. Selecting "Auto" wrote model_override="" and flipped the pill,
	while openclaw went on calling the old model forever.

	So here, and ONLY here, an unpinned pool conversation must NAME the pool. (jarvis#299)
	"""
	model, provider = _resolve_model_and_provider(conv)
	if model:
		return model, provider
	settings = frappe.get_single("Jarvis Settings")
	if getattr(settings, "proxy_active", 0):
		return POOL_VIRTUAL_MODEL, None
	# Direct mode already resolves to settings.llm_model, so "" here means the site has
	# no model configured at all -- nothing useful to patch. Unchanged behaviour.
	return model, provider


def _thinking_prefix(thinking_override: str | None) -> str:
	"""Inline openclaw /think directive for this turn, or '' when unset.

	openclaw reads a leading /think directive from the MESSAGE BODY and
	strips it. We keep it in the user message (after the static system
	prefix), so it never busts the prefix cache the warm-up populates."""
	level = (thinking_override or "").strip().lower()
	return f"/think {level}\n" if level in ("low", "medium", "high") else ""


def _org_locale_clause() -> str:
	"""Region/locale of the site's default Company, folded into the turn's
	``[Context: ...]`` line so the agent formats dates, currency, and numbers
	for the org instead of defaulting to US conventions. Reads cached Single /
	Company values, so it is cheap per turn; any read failure yields an empty
	clause (the turn still runs, just without the locale hint)."""
	try:
		company = frappe.defaults.get_global_default("company")
		country = currency = ""
		if company:
			country = frappe.get_cached_value("Company", company, "country") or ""
			currency = frappe.get_cached_value("Company", company, "default_currency") or ""
		country = country or frappe.db.get_single_value("System Settings", "country") or ""
		currency = currency or frappe.db.get_default("currency") or ""
		date_format = frappe.db.get_single_value("System Settings", "date_format") or ""
		number_format = frappe.db.get_single_value("System Settings", "number_format") or ""
		time_zone = frappe.db.get_single_value("System Settings", "time_zone") or ""
	except Exception:
		return ""
	parts = []
	region = ", ".join(p for p in (country, currency) if p)
	if company:
		# Company names can be long (legal suffixes, "formerly known as"); cap
		# so the per-turn context stays lean.
		name = (company[:40].rstrip() + "...") if len(company) > 43 else company
		parts.append(f"org: {name}" + (f" ({region})" if region else ""))
	elif region:
		parts.append(f"region: {region}")
	# Current fiscal year, so accounting turns don't burn a provider
	# round trip on jarvis__get_fiscal_year (observed 3 calls in one P&L
	# turn on the fleet transcript, ~6s each on the codex runtime). Its
	# own guard: no ERPNext / no Fiscal Year record degrades to no clause.
	fiscal = ""
	try:
		from erpnext.accounts.utils import get_fiscal_year

		fy = get_fiscal_year(frappe.utils.nowdate(), company=company or None, as_dict=True)
		if fy and fy.get("name"):
			fiscal = f"fy {fy.get('name')} ({fy.get('year_start_date')}..{fy.get('year_end_date')})"
	except Exception:
		fiscal = ""
	if fiscal:
		parts.append(fiscal)
	if date_format:
		parts.append(f"dates {date_format}")
	if number_format:
		parts.append(f"numbers {number_format}")
	if time_zone:
		parts.append(f"tz {time_zone}")
	return ("; " + "; ".join(parts)) if parts else ""


def _assistant_name_clause(settings) -> str:
	"""Whitelabel assistant name folded into the turn's trusted [Context:] line
	so the agent refers to itself by the customer's chosen name. "" when unset
	(blank tenants pay zero per-turn tokens, like _org_locale_clause). Sanitized
	like the model-text notes clause - customer free-text inside a trusted
	bracket, so disarm brackets/backticks/newlines and cap length (validate()
	already caps at 40; belt and braces)."""
	name = (getattr(settings, "agent_name", None) or "").strip()
	if not name:
		return ""
	safe = _safe_label_name(name).replace("[", "(").replace("]", ")")[:40]
	return f"; assistant name: {safe}"


def _advance_macro(conversation_id: str, *, errored: bool) -> None:
	"""Chaining hook for the macro engine: if this conversation is a running
	macro, advance it (enqueue the next step, or finish). Best-effort — a macro
	bug must never affect the normal turn."""
	try:
		from jarvis.chat import macros

		macros.advance_after_turn(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(title="jarvis macro advance hook failed", message=frappe.get_traceback())
	try:
		from jarvis.learning import app_analysis

		app_analysis.on_turn_end(conversation_id, errored=errored)
	except Exception:
		frappe.log_error(title="jarvis app-learning turn hook failed", message=frappe.get_traceback())


def _admission_settle(run_id: str, state: str, error: str | None = None) -> None:
	"""Phase-0 admission terminal hook: mark this turn's durable Turn row
	terminal (CAS) and promote the next queued turn. Flag-gated + best-effort
	inside admission.settle_turn, so flag-OFF is byte-identical and a Phase-0
	failure never affects the legacy turn. Called ONLY at the legacy terminal
	commit points and ALWAYS outside the brittle slice region (:996-:1022)."""
	try:
		from jarvis.chat import admission

		admission.settle_turn(run_id, state, error=error)
	except Exception:
		frappe.log_error(title="admission settle hook", message=frappe.get_traceback())


@dataclass
class _PreparedPrompt:
	"""The assembled, ready-to-send turn prompt + its bootstrap metadata. Produced
	by :func:`assemble_prompt` and consumed by BOTH the legacy ``handle_chat_send``
	body AND the Relay-Pump ``prepare`` job (jarvis.chat.prepare) — the ONE source
	of truth for the clause ORDER (a documented safety invariant) + attachment
	fencing, so the pump-mode prompt is byte-for-byte what the legacy path sends."""

	settings: object
	chat_user: str
	user_message: str
	vision_parts: list = field(default_factory=list)
	inlined_prompt_chars: int = 0
	drained_notes: list = field(default_factory=list)
	drained_ids: list = field(default_factory=list)


def assemble_prompt(
	conv,
	*,
	message_id: str,
	conversation_id: str,
	context,
	attachments,
	user: str,
) -> _PreparedPrompt:
	"""Assemble the full turn prompt (the ``[Context: …]`` bracket + every clause,
	the doc/report viewing context, and inlined/vision attachments) for one turn.

	Extracted VERBATIM from ``handle_chat_send`` so the Relay-Pump prepare job
	reuses the exact same assembly (no logic duplication — WP-1d). It performs
	only READS (Singles/Company/wiki/agent-notes snapshot + permission-gated File
	reads) + string building; it has NO side effects, so calling it from either
	owner is byte-identical. ``agent_notes`` are READ here (folded into the
	bracket) but NOT cleared — the clear fires only after PROVEN delivery
	(post-ack), which is the pump's job in managed-pump mode (R-2) and
	``handle_chat_send``'s job on the legacy path."""
	settings = frappe.get_single("Jarvis Settings")
	# Fetch content + sender of THIS user message in one round-trip.
	# msg_row.owner is the Frappe user who sent this turn, set by Frappe
	# at insert time in send_message (jarvis/chat/api.py). We use it
	# rather than conv.owner for the context bracket because the bench-
	# side dispatcher (_dispatch_from_session in jarvis/api.py:118) also
	# acts on the per-request identity, not the conversation creator -
	# the bracket and the tool-call's frappe.session.user stay in lock-
	# step. Today these are equal for single-owner conversations; the
	# distinction matters the moment we ship shared conversations.
	msg_row = frappe.db.get_value(MSG, message_id, ["content", "owner"], as_dict=True) or {}
	user_message = msg_row.get("content")
	chat_user = msg_row.get("owner") or user
	# Prepend today's date AND the chat user's Frappe id as a context line so
	# the agent (AGENTS.md tells it to treat the leading ``[Context: ...]``
	# as system, not user) can (a) resolve relative time expressions
	# ("last quarter", "this week") and (b) answer "who am I" / "what
	# perms do I have" without a clarifying round-trip or a doomed
	# get_list on User. The persisted user_message in the DB is
	# unchanged; only the value sent over to openclaw is augmented.
	now = frappe.utils.now_datetime()
	today = now.strftime("%Y-%m-%d (%A)")
	# Fold the auto-apply preference into the system context line so the agent
	# knows whether to confirm mutating ops. Default (off) = confirm; the persona
	# confirms by default, so we only signal the non-default "auto" mode.
	auto_apply = "; auto-apply changes: ON" if conv.auto_apply else ""
	# Custom-skill invocation: if the user typed /slug for an enabled custom
	# skill, name the installed custom-<slug> skill(s) in the system context so
	# the agent activates them deterministically (openclaw has no documented
	# user-invocable trigger; the SKILL.md is already in workspace/skills/).
	from jarvis.chat.custom_skills import invoked_skill_clause, learned_skill_clause

	skill_clause = invoked_skill_clause(msg_row.get("content") or "")
	# Learned skills (plan section 6.6, the reliable activation path): deterministically
	# name the role-matched managed learned-<domain> skills for THIS chat user, so the
	# agent applies them without depending on openclaw's undocumented auto-retrieval.
	# Additive to skill_clause; role match uses the cached role lookup (hot path).
	learned_clause = learned_skill_clause(chat_user)
	# Org locale (default Company country/currency + site date/number/tz) so the
	# agent formats for the org's region instead of defaulting to US conventions.
	locale_clause = _org_locale_clause()
	# Whitelabel assistant name (Phase 3): folded into the trusted [Context:] line
	# so the agent refers to itself by the customer's chosen name. "" when unset.
	assistant_name_clause = _assistant_name_clause(settings)
	# Personal custom skills + org wiki notes (voice & wiki feature). Both
	# clauses are best-effort ("" on any failure — a clause bug must never
	# break a turn) and size-capped (~700 chars combined). Lazy imports so a
	# not-yet-reloaded RQ worker keeps serving turns before these land.
	try:
		from jarvis.chat.custom_skills import personal_skill_clause

		personal_clause = personal_skill_clause(chat_user) or ""
	except Exception:
		personal_clause = ""
	try:
		from jarvis.chat.wiki import wiki_clause

		wiki_notes_clause = wiki_clause(conversation_id, context) or ""
	except Exception:
		wiki_notes_clause = ""
	# Site-customizations hint: counts-only, cached per site. Same
	# best-effort + lazy-import contract as the clauses above.
	try:
		from jarvis.chat.customizations_clause import customizations_clause

		custom_site_clause = customizations_clause() or ""
	except Exception:
		custom_site_clause = ""
	# Deferred agent-correction notes (e.g. a discarded action the agent's
	# in-container memory still believes is pending): fold them into the TRUSTED
	# [Context: ...] line so the correction reaches the agent on THIS turn without
	# firing an extra turn. Each carries model-proposed structural text (a record
	# name), so neutralize it - single line, no backticks, brackets disarmed - so
	# it can't break out of the bracket or forge a system line. Read now; the queue
	# is cleared only after a successful send (below), so a failed dispatch
	# re-delivers instead of silently dropping the veto.
	from jarvis.chat import agent_notes

	drained_notes = agent_notes.read(conv)
	drained_ids = [e["id"] for e in drained_notes]
	notes_clause = ""
	if drained_notes:
		safe_notes = "; ".join(
			_safe_label_name(e["text"]).replace("[", "(").replace("]", ")") for e in drained_notes
		)
		notes_clause = f"; notes: {safe_notes}"
	# On-demand "ground on wiki" (the composer's one-shot control): when the user
	# armed it, inject the clipped bodies of the most relevant wiki pages as a
	# labelled block AFTER the [Context:] bracket (not inside it — bodies are
	# multi-line), so the agent answers grounded in the org's own knowledge this
	# turn. Best-effort → "" so a grounding failure never breaks the turn.
	ground_block = ""
	if isinstance(context, dict) and context.get("ground_wiki"):
		try:
			from jarvis.chat.wiki import forced_wiki_block

			ground_block = forced_wiki_block(conversation_id, context, user_message) or ""
		except Exception:
			ground_block = ""
		if not ground_block:
			# The user explicitly asked to ground this turn on the wiki but nothing
			# matched (or the wiki is empty) — tell the agent so it answers honestly
			# instead of silently falling back to its own knowledge as if it had
			# consulted the wiki.
			ground_block = (
				"\n\n(You were asked to ground this answer on the org wiki, but no "
				"matching wiki page was found. Say that no relevant wiki page exists "
				"rather than implying the answer came from the wiki.)"
			)
	user_message = (
		# conv:<id> lets the agent link rows it creates (e.g. Jarvis Approval)
		# back to this conversation so deciding can resume the chat.
		#
		# Clause ORDER is a safety invariant (Skills-area rework, DESIGN.md
		# section 6 / research/ux-permissions.md §4.4): the org/role/learned and
		# wiki clauses are emitted BEFORE the personal clause, and the personal
		# clause itself carries "(applies to you; org guidance takes priority on
		# conflict)" — so a user's personal skills never silently outrank org/role
		# guidance inside their own turn. Do NOT move personal_clause ahead of
		# learned_clause/wiki_notes_clause. Explicit /slug invocation
		# (skill_clause) stays intentional and is not demoted. The
		# customizations clause is org-level too, so it sits with the org
		# clauses - before personal, which stays last.
		f"[Context: today is {today}{locale_clause}{assistant_name_clause}; chat user: {chat_user}"
		f"; conv: {conversation_id}{auto_apply}{skill_clause}{learned_clause}"
		f"{wiki_notes_clause}{custom_site_clause}{personal_clause}{notes_clause}]"
		f"{ground_block}"
		f"\n\n{user_message or ''}"
	)

	from jarvis import selfhost

	# Floating-widget auto-context + file inputs layer onto the
	# already date/user-augmented user_message built above. Prompt-only;
	# the persisted/visible user message is unchanged.
	user_message = _prepend_doc_context(user_message, context)
	# Vision is managed-pool only (self-host vision is a follow-up), gated by the
	# operator toggle and the model's provider being multimodal. When off, image/
	# PDF attachments degrade to a short note (no OCR fallback any more).
	vision_ok = (
		not selfhost.is_self_hosted()
		and _vision_enabled(settings)
		and vision.supports_vision(settings.llm_provider)
	)
	# Measure how much the attachments grew the PROMPT, not just how many
	# vision parts they produced. Text files (CSV/TXT/JSON/MD/logs) and the
	# vision-off PDF text fallback are inlined into user_message and leave
	# vision_parts empty, so growth is the only signal that the send is big.
	# Feeds _ack_timeout_s() below.
	_prompt_chars_before_attachments = len(user_message)
	user_message, vision_parts = _prepare_attachments(user_message, attachments, vision_ok)
	inlined_prompt_chars = max(0, len(user_message) - _prompt_chars_before_attachments)
	return _PreparedPrompt(
		settings=settings,
		chat_user=chat_user,
		user_message=user_message,
		vision_parts=vision_parts,
		inlined_prompt_chars=inlined_prompt_chars,
		drained_notes=drained_notes,
		drained_ids=drained_ids,
	)


def handle_chat_send(payload: dict) -> None:
	"""Drive one agent turn end to end.

	Called by the RQ shim in ``jarvis.chat.worker.run_agent_turn`` today;
	Phase 2 of the chat-bridge refactor will also call this from a gevent
	subscriber inside the Python realtime process. Either way the payload
	carries everything needed to execute one turn:

	    {
	        "conversation_id": str,
	        "message_id": str,
	        "run_id": str,
	        "attachments": list[dict] | None,
	        "context": dict | None,
	    }

	``attachments`` (optional): list of {file_url, file_name} dicts. Text files are
	inlined into the prompt; images/PDFs are sent to the model as native vision
	(managed pool + a vision-capable model) - see _prepare_attachments. The
	persisted/visible user message keeps only the "📎 name" marker, so file
	bytes never bloat the chat history.

	``context`` (optional): {doctype, name} of the ERP document the user was
	viewing when they asked (floating-widget auto-context). Prepended to the
	agent prompt only; the persisted/visible user message is unchanged.

	Sprint-3 (2026-06-16 review): the inline ``except OpenclawUnreachableError``
	blocks only marked the placeholder errored for openclaw-specific
	failures. Any OTHER exception (cryptography.InvalidKey, ssl.SSLError,
	programmer bug in _handle_event, etc.) propagated to RQ without
	calling _mark_errored, leaving the assistant row stuck at
	``streaming=1`` forever (the UI poller spins on the empty body).
	An outer try/except Exception now catches everything else, marks the
	row errored + publishes run:error, then re-raises so RQ still records
	the job as failed.
	"""
	conversation_id: str = payload["conversation_id"]
	message_id: str = payload["message_id"]
	run_id: str = payload["run_id"]
	attachments = payload.get("attachments")
	context = payload.get("context")

	# Latency telemetry (plan Phase 0): one summary line per turn with the
	# segment timings that dominate first-message latency. queue_wait_ms is
	# how long the job sat between the web request's enqueue and this
	# handler starting (RQ dequeue + fork on the default backend).
	from jarvis.chat.latency import get_logger as _get_latency_logger

	_lat = _get_latency_logger()
	t_handle0 = time.monotonic()
	enqueued_at_ms = payload.get("enqueued_at_ms")
	queue_wait_ms = max(0, int(time.time() * 1000) - int(enqueued_at_ms)) if enqueued_at_ms else -1
	# Stream-phase stats filled in by _consume: ms to the first event of any
	# kind, ms to the first assistant delta (= first visible token), and how
	# many tool events fired before that first delta (measures the persona's
	# read-SOUL/TOOLS/STYLE-before-answering tax; see the latency plan).
	stream_stats = {
		"t0": None,
		"first_event_ms": -1,
		"first_delta_ms": -1,
		"pre_reply_tool_calls": 0,
	}
	checkout_ms = -1
	session_create_ms = 0

	conv = frappe.get_doc(CONV, conversation_id)
	user = conv.owner
	# User-message intake: the user replied, so any Pending chat-sourced
	# approval materialized from a previous ```jarvis-ask fence is answered
	# in chat now — flip it to Answered so the board never offers a stale
	# double-answer. One indexed UPDATE; best-effort (hot path, never raises).
	try:
		from jarvis.chat import chat_asks

		chat_asks.resolve_on_user_message(conversation_id)
	except Exception:
		frappe.log_error(
			title="chat asks: resolve_on_user_message failed",
			message=frappe.get_traceback(),
		)
	# Wall-clock turn start (epoch ms) - scopes codex imagegen output produced
	# during this turn (compared against the generated image files' mtime).
	turn_start_ms = int(time.time() * 1000)

	# Create the assistant placeholder row up-front so the browser has a
	# stable name to attach realtime events to.
	assistant_msg = _create_assistant_placeholder(conv)

	_publish_to_user(
		user,
		{
			"kind": "run:start",
			"conversation_id": conversation_id,
			"message_id": assistant_msg.name,
			"run_id": run_id,
		},
	)

	# Prompt assembly (shared with the Relay-Pump prepare job so the two paths
	# emit a byte-identical prompt — clause order + attachment fencing live in
	# ONE place, jarvis.chat.turn_handler.assemble_prompt). Pure reads + string
	# building; agent_notes are READ (folded in) but cleared only after a proven
	# delivery below.
	_ap = assemble_prompt(
		conv,
		message_id=message_id,
		conversation_id=conversation_id,
		context=context,
		attachments=attachments,
		user=user,
	)
	settings = _ap.settings
	chat_user = _ap.chat_user
	user_message = _ap.user_message
	vision_parts = _ap.vision_parts
	inlined_prompt_chars = _ap.inlined_prompt_chars
	drained_notes = _ap.drained_notes
	drained_ids = _ap.drained_ids
	from jarvis import selfhost
	from jarvis.chat import agent_notes
	# The /think directive: self-hosted still inlines it as the FIRST bytes
	# of the message body (openclaw's leading-directive parser strips it
	# from there); managed sends it as the chat_send ``thinking`` param
	# instead, so user_message stays unprefixed and the OpenAI prefix
	# cache the warm-up populates is never busted by a varying prefix.

	def _publish_run_error(err: str, *, changed_data=None, code=None, exc=None) -> None:
		# changed_data: pass False only when we KNOW nothing was written (a
		# pre-ack failure - the run never started). The SPA turns that into a
		# "No changes were made to your data" reassurance; omit it when unknown.
		_mark_errored(assistant_msg.name, err)
		payload = {
			"kind": "run:error",
			"conversation_id": conversation_id,
			"message_id": assistant_msg.name,
			"run_id": run_id,
			"error": err,
			"code": code or _classify_error(err, exc),
		}
		if changed_data is not None:
			payload["changed_data"] = bool(changed_data)
		_publish_to_user(user, payload)

	try:
		tool_msg_by_call_id: dict[str, str] = {}
		batcher = _AssistantContentBatcher(assistant_msg.name)

		def _consume(events) -> None:
			if stream_stats["t0"] is None:
				stream_stats["t0"] = time.monotonic()
			for event in events:
				# Segment telemetry (plan Phase 0): first event / first
				# assistant delta / tool calls that ran before the first
				# visible token. Cheap dict writes, no extra I/O.
				ev_ms = int((time.monotonic() - stream_stats["t0"]) * 1000)
				if stream_stats["first_event_ms"] < 0:
					stream_stats["first_event_ms"] = ev_ms
				kind = event.get("kind")
				if kind == "assistant" and stream_stats["first_delta_ms"] < 0:
					stream_stats["first_delta_ms"] = ev_ms
				elif kind == "tool" and stream_stats["first_delta_ms"] < 0:
					if (event.get("phase") or "") != "end":
						stream_stats["pre_reply_tool_calls"] += 1
				_handle_event(
					event,
					conversation_id=conversation_id,
					assistant_msg_name=assistant_msg.name,
					tool_msg_by_call_id=tool_msg_by_call_id,
					user=user,
					run_id=run_id,
					batcher=batcher,
				)
			# Drain buffered assistant deltas before the streaming=0 cleanup.
			batcher.flush()

		def _consume_relay(events) -> dict:
			if stream_stats["t0"] is None:
				stream_stats["t0"] = time.monotonic()
			terminal = {"kind": "relay:interrupted", "reason": "stream-exhausted"}
			for event in events:
				# Same segment telemetry as _consume (plan Phase 0), so managed
				# (relay) turns keep feeding the latency summary below instead
				# of logging -1s for every field.
				ev_ms = int((time.monotonic() - stream_stats["t0"]) * 1000)
				if stream_stats["first_event_ms"] < 0:
					stream_stats["first_event_ms"] = ev_ms
				kind = event.get("kind")
				if str(kind or "").startswith("relay:"):
					terminal = event
					break
				if kind == "assistant" and stream_stats["first_delta_ms"] < 0:
					stream_stats["first_delta_ms"] = ev_ms
				elif kind == "tool" and stream_stats["first_delta_ms"] < 0:
					if (event.get("phase") or "") != "end":
						stream_stats["pre_reply_tool_calls"] += 1
				_handle_event(
					event,
					conversation_id=conversation_id,
					assistant_msg_name=assistant_msg.name,
					tool_msg_by_call_id=tool_msg_by_call_id,
					user=user,
					run_id=run_id,
					batcher=batcher,
				)
			batcher.flush()
			return terminal

		if selfhost.is_self_hosted():
			# Self-hosted: openclaw's HTTP OpenAI-compatible surface with a
			# bearer token (full operator scope, no device pairing). agent_url
			# holds the http(s) base; the user's openclaw uses its own LLM.
			from jarvis.chat import openclaw_http_client

			base_url = (settings.agent_url or "").strip()
			token = settings.get_password("agent_token", raise_exception=False) or ""
			# Register this turn so the plugin's call_tool callback (which only
			# carries the openclaw HTTP session key, not our conversation) can
			# attribute tool calls back here and surface tool cards.
			tool_user = (settings.selfhost_tool_user or "").strip()
			selfhost.set_active_turn(tool_user, conversation=conversation_id, owner=user, run_id=run_id)
			# Stream token-by-token unless the operator explicitly turned it
			# off (a NULL field on a pre-existing config defaults to streaming).
			stream_pref = (settings.selfhost_stream is None) or bool(settings.selfhost_stream)
			# Self-hosted has no chat_send ``thinking`` param (it goes over the
			# HTTP OpenAI-compatible surface, not chat.send), so the /think
			# directive is inlined as the FIRST bytes of the message body here,
			# same as before this refactor.
			sh_message = _thinking_prefix(conv.thinking_override) + user_message
			try:
				_consume(
					openclaw_http_client.stream_agent_turn(
						base_url,
						token,
						sh_message,
						model="openclaw",
						stream=stream_pref,
					)
				)
				# Delivered (the stream ran to completion) - drop exactly the notes
				# we folded in (by id), so the correction is delivered once, not
				# re-nagged, without clobbering a discard appended mid-turn or one a
				# concurrent continuation delivered. An Unreachable failure raises
				# before this, keeping them for retry.
				if drained_notes:
					agent_notes.clear(conversation_id, drained_ids)
			except OpenclawUnreachableError as e:
				_publish_run_error(str(e), changed_data=False, exc=e)
				_advance_macro(conversation_id, errored=True)
				_admission_settle(run_id, "errored", str(e))
				return
			finally:
				selfhost.clear_active_turn(tool_user, run_id)
		else:
			# Managed: device-paired WebSocket to the tenant's gateway.
			# Uses a per-process connection pool so we don't pay the
			# DNS + TCP + TLS + WS upgrade + handshake (~50-200ms) on
			# every turn. The pool eviction on OpenclawUnreachableError
			# means the next attempt will reconnect; we don't auto-
			# retry inside this turn because tokens may have already
			# streamed to the UI by the time the failure surfaces.
			gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
			effective_model, oauth_provider_id = _session_model_for(conv)
			if not conv.session_key:
				# First turn of this conversation pays session-create and is the
				# most cold-start-prone (a dormant container takes ~10-25s to
				# wake). Tell the user we're waking the assistant so the connect
				# window reads as progress rather than a dead spinner.
				_publish_to_user(
					user,
					{
						"kind": "run:status",
						"conversation_id": conversation_id,
						"message_id": assistant_msg.name,
						"run_id": run_id,
						"status": "waking",
					},
				)
			try:
				t_checkout = time.monotonic()
				with openclaw_session_pool.checkout(gateway_url) as sess:
					checkout_ms = int((time.monotonic() - t_checkout) * 1000)
					# First turn of this conversation: create the openclaw
					# session on THIS pooled connection (no extra handshake)
					# and persist the Jarvis Chat Session row BEFORE the
					# stream starts — the plugin's call_tool sessionKey→user
					# lookup (the permission moat) needs the row in place
					# before the agent's first tool callback. Moved here from
					# send_message so the browser-awaited POST never pays a
					# WS connect (2026-07 latency plan, Phase 1.1). chat_user
					# (the sender) owns the session row, keeping tool-call
					# identity in lockstep with the [Context:] bracket above.
					if not conv.session_key:
						from jarvis.chat.api import _ensure_session_key

						t_sess = time.monotonic()
						conv.session_key = _ensure_session_key(chat_user, sess=sess)
						frappe.db.set_value(CONV, conversation_id, "session_key", conv.session_key)
						frappe.db.commit()
						session_create_ms = int((time.monotonic() - t_sess) * 1000)
					if effective_model:
						model_ref = (
							f"{oauth_provider_id}/{effective_model}" if oauth_provider_id else effective_model
						)
						try:
							sess.set_session_model(conv.session_key, model_ref)
						except OpenclawUnreachableError:
							raise
						except Exception:
							frappe.log_error(
								title="chat: model override patch failed",
								message=frappe.get_traceback(),
							)
					# Watermark BEFORE the send: recovery must never stamp a
					# previous turn's answer onto this row (a run that dies
					# with zero output leaves the prior reply as the newest
					# transcript message). Best-effort: on failure the
					# watermark stays 0 and recovery behaves as before.
					try:
						_wm_msgs = sess.get_session_messages(conv.session_key, limit=5)
						watermark = max(
							(((m or {}).get("__openclaw") or {}).get("seq", 0) for m in _wm_msgs),
							default=0,
						)
						if watermark:
							frappe.db.set_value(
								MSG,
								assistant_msg.name,
								"openclaw_seq_watermark",
								watermark,
								update_modified=False,
							)
							frappe.db.commit()
					except Exception:
						frappe.log_error(
							title="chat: seq watermark capture failed",
							message=frappe.get_traceback(),
						)
					managed_attachments = _to_managed_attachments(vision_parts) if vision_parts else None
					ack_timeout = _ack_timeout_s(len(managed_attachments or []), inlined_prompt_chars)
					ack_timed_out = False
					try:
						ack = (
							sess.chat_send(
								conv.session_key,
								user_message,
								run_id,
								thinking=(conv.thinking_override or "").strip() or None,
								attachments=managed_attachments,
								timeout_s=ack_timeout,
							)
							or {}
						)
					except OpenclawUnreachableError as e:
						# The ack window closed with the request frame already on
						# the wire. openclaw routinely ACCEPTS the message and runs
						# the whole turn while the bench is still waiting, so
						# erroring here is a false negative: the user sees
						# "chat.send timed out" while the transcript shows a
						# finished answer, and a retry re-runs under a FRESH run_id
						# (dedupe keys off run_id) and can duplicate writes. Park
						# for snapshot recovery instead - the seq watermark was
						# captured BEFORE the send, so recovery can tell this turn's
						# messages from the previous turn's. Only the TIMEOUT is
						# ambiguous; a rejection or a dead socket never reached the
						# agent, so those still raise onto the real-error path.
						if getattr(e, "code", None) != "ack-timeout":
							raise
						ack = {}
						ack_timed_out = True
					# chat.send delivered our message (incl. any drained notes) -
					# drop exactly the notes we folded in (by id), so the correction
					# is delivered once, not re-nagged, without clobbering a discard
					# appended mid-turn or one a concurrent continuation delivered. A
					# pre-ack failure raises OpenclawUnreachableError below instead of
					# reaching here, leaving the notes for retry. An ack TIMEOUT is
					# excluded for the same reason: delivery is unproven there, and
					# re-nagging a correction is a much cheaper mistake than silently
					# dropping one.
					if drained_notes and not ack_timed_out:
						agent_notes.clear(conversation_id, drained_ids)
					if ack_timed_out:
						# Same park-and-recover path as a dropped stream, handled
						# below AFTER this pool checkout releases the per-gateway
						# lock so the recovery round-trip never blocks other turns.
						terminal = {"kind": "relay:interrupted", "reason": "ack-timeout"}
					elif ack.get("status") == "ok":
						# Cached replay of a completed run (same run_id
						# re-enqueued after the worker died post-completion).
						# No events will follow; finalize from the durable
						# transcript instead. Routed through the same
						# relay:interrupted handling as a dropped stream
						# (below, AFTER this pool checkout releases the
						# per-gateway lock) so the park+recover round-trip
						# never holds the lock other turns are waiting on.
						terminal = {"kind": "relay:interrupted", "reason": "completed-replay"}
					else:
						terminal = _consume_relay(
							sess.relay_turn_events(
								conv.session_key,
								ack.get("runId") or run_id,
							)
						)
					# Real usage accounting (design section 3): a genuinely
					# completed run leaves fresh last-run token counts on the
					# gateway's sessions.list row. Read them NOW, while `sess` is
					# still checked out, and record this turn's delta. Only on
					# relay:final (a cached completed-replay or a parked
					# interrupt/error must NOT re-count). Delegated to
					# jarvis.chat.usage; wrapped so usage accounting can never
					# break the turn.
					if terminal.get("kind") == "relay:final":
						try:
							from jarvis.chat import usage as _usage

							_row = _usage.fetch_fresh_session_row(sess, conv.session_key)
							if _row:
								_usage.record_turn_usage(conv.session_key, _row)
						except Exception:
							frappe.log_error(
								title="chat: usage record hook failed",
								message=frappe.get_traceback(),
							)
			except OpenclawUnreachableError as e:
				# Pre-ack only (relay_turn_events never raises): the run never
				# started, so this is a real, retriable error. Gray zone: a
				# DELIVERED send whose ack was lost lands here too and a user
				# retry then re-runs under a fresh run_id - deliberate. Reusing
				# the old run_id would make openclaw REPLAY the cached outcome
				# (dedupe semantics), never re-run; and while a ghost run is
				# still active, openclaw's content-based dedupe already returns
				# in_flight for the identical resend, so true double-runs are
				# confined to the ghost-run-already-finished case.
				_publish_run_error(str(e), changed_data=False, exc=e)
				_advance_macro(conversation_id, errored=True)
				_admission_settle(run_id, "errored", str(e))
				return

			if terminal["kind"] == "relay:error":
				err_text = terminal.get("error") or "agent error"
				# Context overflow is NOT terminal on openclaw: it emits the
				# error, then auto-compacts and RETRIES the prompt (observed
				# live: 'auto-compaction succeeded; retrying prompt' ~45s
				# after the error; the retried run completes in the session).
				# Park for snapshot recovery instead of erroring - the
				# recovery cron finalizes the retried answer; if the retry
				# ALSO dies, the recovery ceiling errors it honestly. This
				# holds for ANY plan/context-window size: openclaw derives
				# the window from the model catalog, so smaller-plan windows
				# just compact sooner - the customer never sees the raw
				# overflow either way.
				if "context overflow" in err_text.lower():
					_mark_recovering(assistant_msg.name)
					_publish_to_user(
						user,
						{
							"kind": "run:recovering",
							"conversation_id": conversation_id,
							"message_id": assistant_msg.name,
							"run_id": run_id,
							"reason": "compacting",
						},
					)
					return
				if terminal.get("state") == "aborted":
					# User hit Stop -> stop_run -> openclaw chat.abort. Finalize as a
					# clean stop: keep whatever streamed, no error. Publish run:end so
					# OTHER tabs (which never muted this run) also unlock - the
					# stopping tab mutes it via stoppedRunId. (Ordered after the
					# overflow check - the two terminal states are mutually exclusive,
					# and the overflow branch stays first FOR ITS TEST: reordering
					# these breaks TestRelayOverflowParks, which reads this file as
					# text and asserts on a window after the relay:error line.)
					#
					# The stop is recorded as a FLAG, not as prose in `content`:
					# `content` is what the agent said, and a partial answer with no
					# marker reads as a complete one. The SPA renders the marker from
					# `stopped`, so a mid-sentence stop is finally distinguishable
					# from a short reply. `stopped` means the abort LANDED - the
					# several paths where stop_run never reaches here leave it 0.
					frappe.db.set_value(MSG, assistant_msg.name, "stopped", 1)
					frappe.db.set_value(MSG, assistant_msg.name, "streaming", 0)
					frappe.db.commit()
					_publish_to_user(
						user,
						{
							"kind": "run:end",
							"conversation_id": conversation_id,
							"message_id": assistant_msg.name,
							"run_id": run_id,
							"stopped": True,
						},
					)
					_advance_macro(conversation_id, errored=True)
					_admission_settle(run_id, "cancelled")
					return
				_publish_run_error(err_text)
				_advance_macro(conversation_id, errored=True)
				_admission_settle(run_id, "errored", err_text)
				return
			if terminal["kind"] == "relay:interrupted":
				# Deadline, transport drop, or exhausted stream after a
				# successful ack: openclaw still owns the turn and persists
				# the result. Park for snapshot recovery. NEVER a false error.
				# Publish a recovering event so the UI shows a clear
				# "reconnecting, your answer will appear here" state and
				# unlocks the composer, instead of a silent, locked spinner
				# that can sit for up to the recovery ceiling.
				_mark_recovering(assistant_msg.name)
				_publish_to_user(
					user,
					{
						"kind": "run:recovering",
						"conversation_id": conversation_id,
						"message_id": assistant_msg.name,
						"run_id": run_id,
						"reason": terminal.get("reason") or "interrupted",
					},
				)
				_try_recover_now(conversation_id)
				return
			# relay:final - authoritative text beats the batcher tail.
			if terminal.get("text"):
				frappe.db.set_value(MSG, assistant_msg.name, "content", terminal["text"])

		# Streaming exited cleanly via lifecycle.end
		frappe.db.set_value(MSG, assistant_msg.name, "streaming", 0)
		frappe.db.commit()

		# Canvas + generated-image persistence and publish (extracted so
		# snapshot recovery can deliver the same rich outputs for a turn
		# that finished via _finalize instead of this clean exit).
		persist_rich_outputs(assistant_msg.name, conversation_id, user, run_id, turn_start_ms)

		# Chat-ask materialization (notify-approvals design Part 2): a final
		# reply carrying a ```jarvis-ask fence surfaces on the Approval Board
		# so an away user finds the question. One PK read; skipped when the
		# turn errored (a partial fence is not a real ask). Best-effort —
		# never breaks the turn.
		try:
			from jarvis.chat import chat_asks

			_final = frappe.db.get_value(MSG, assistant_msg.name, ["content", "error"], as_dict=True) or {}
			if not _final.get("error"):
				chat_asks.materialize_from_turn(conversation_id, _final.get("content") or "")
		except Exception:
			frappe.log_error(
				title="chat asks: materialize_from_turn failed",
				message=frappe.get_traceback(),
			)

	except Exception as e:
		# Last-resort backstop. Any exception that wasn't an
		# OpenclawUnreachableError (e.g. cryptography.InvalidKey from
		# device-pairing signing, ssl.SSLError, a tool-handler bug,
		# DoesNotExistError on a stale conversation row) would otherwise
		# leave the assistant row at ``streaming=1`` indefinitely. Mark
		# it errored, publish run:error, then re-raise so RQ records the
		# job as failed (and the operator gets a normal Error Log entry).
		try:
			_mark_errored(
				assistant_msg.name,
				f"unexpected worker error: {type(e).__name__}",
			)
			_publish_to_user(
				user,
				{
					"kind": "run:error",
					"conversation_id": conversation_id,
					"message_id": assistant_msg.name,
					"run_id": run_id,
					# Don't leak full str(e) - the operator-safe identifier is
					# the exception class; the full traceback is in Error Log.
					"code": "internal",
					"error": f"{type(e).__name__}",
				},
			)
		except Exception:
			# If even the error-marking path fails, swallow - we're
			# already in an error path and re-raising would mask the
			# original exception that RQ should see.
			pass
		_advance_macro(conversation_id, errored=True)
		# Backstop terminal: settle the Turn row errored + promote before the
		# re-raise so a queued turn never waits on a crashed worker. Best-effort
		# inside _admission_settle - it never masks the re-raised exception.
		_admission_settle(run_id, "errored", f"unexpected worker error: {type(e).__name__}")
		raise
	# A turn can end "cleanly" (no exception) yet be an LLM-level failure — an
	# openclaw lifecycle:error frame (quota/cooldown/provider error) ends the
	# stream normally after _mark_errored stamped the message. So the macro's
	# errored signal is the assistant message's error field, not the code path.
	_turn_errored = bool(frappe.db.get_value(MSG, assistant_msg.name, "error"))
	_advance_macro(
		conversation_id,
		errored=_turn_errored,
	)
	_publish_to_user(
		user,
		{
			"kind": "run:end",
			"conversation_id": conversation_id,
			"message_id": assistant_msg.name,
			"run_id": run_id,
		},
	)
	# Phase-0 admission terminal hook for the clean-exit path: done on a real
	# reply, errored when an lifecycle:error stamped the message (the same
	# signal _advance_macro used above). Promotes the next queued turn.
	_admission_settle(run_id, "errored" if _turn_errored else "done")

	# Latency telemetry summary (plan Phase 0). first_delta_ms is the number
	# users feel: worker start → first visible token. pre_reply_tool_calls
	# counts agent tool round-trips before that token (persona read tax).
	_lat.info(
		"turn run_id=%s first_turn=%d queue_wait_ms=%d checkout_ms=%d "
		"session_create_ms=%d first_event_ms=%d first_delta_ms=%d "
		"pre_reply_tool_calls=%d turn_total_ms=%d",
		run_id,
		1 if session_create_ms else 0,
		queue_wait_ms,
		checkout_ms,
		session_create_ms,
		stream_stats["first_event_ms"],
		stream_stats["first_delta_ms"],
		stream_stats["pre_reply_tool_calls"],
		int((time.monotonic() - t_handle0) * 1000),
	)
	# Turn telemetry (customization discovery). Best-effort.
	try:
		from jarvis import telemetry

		telemetry.emit_turn(conversation_id, run_id, int((time.monotonic() - t_handle0) * 1000))
	except Exception:
		pass

	# Auto-title (managed mode): the first substantive turn of a still-unnamed
	# conversation gets a concise, LLM-summarised title, not the raw first
	# message. Deferred to the SHORT queue (2026-07 latency plan, Phase 1.2):
	# it used to run inline here — a full extra LLM turn holding this long-
	# queue worker for 2-8s, so the user's next message could queue behind a
	# title generation. The job re-resolves settings/model itself; the title
	# still lands via the same "conversation:renamed" event.
	# Best-effort: a title failure must never affect the completed turn.
	from jarvis import selfhost

	if not selfhost.is_self_hosted():
		try:
			from jarvis.chat import title as title_mod

			title_mod.enqueue_autotitle(conversation_id, user)
		except Exception:
			frappe.log_error(
				title="chat worker: auto-title enqueue failed",
				message=frappe.get_traceback(),
			)
		# Wiki nudge (voice & wiki feature): fire-and-forget short-queue job.
		# Every gate (wiki_enabled, File Box, cooldown, dismissal, wiki-worthy
		# entities this turn) re-checks inside the job; the cheap wiki_enabled
		# read here just skips a pointless enqueue when the wiki is off. The
		# nudge goes to chat_user — the turn's actual sender — not conv.owner
		# (they diverge in shared conversations). Lazy import + best-effort:
		# a nudge failure can never affect the completed turn.
		try:
			from jarvis.chat import wiki as wiki_mod

			if wiki_mod.wiki_enabled():
				frappe.enqueue(
					"jarvis.chat.wiki.maybe_nudge",
					queue="short",
					conversation_id=conversation_id,
					user=chat_user,
					run_id=run_id,
				)
		except Exception:
			frappe.log_error(
				title="chat worker: wiki nudge enqueue failed",
				message=frappe.get_traceback(),
			)


_REPORT_FILTER_LINE_CAP = 400


def _format_report_filters(filters: dict) -> str:
	"""Compact one-line ``with filters {k=v, k=v}`` summary of a report's
	active filter dict. Falsy values (None / "" / [] / {}) are skipped so
	an unset optional filter doesn't clutter the line. Truncates at
	``_REPORT_FILTER_LINE_CAP`` characters with a "(truncated; N total)"
	suffix so a pathological filter dict (e.g. 100 keys, or a filter that
	itself is a huge nested structure) can't blow the prompt budget.
	Returns "" (empty string) when there are no meaningful filter values,
	so the caller can concatenate it directly."""
	pairs = [f"{k}={v!r}" for k, v in filters.items() if v not in (None, "", [], {})]
	if not pairs:
		return ""
	body = ", ".join(pairs)
	if len(body) > _REPORT_FILTER_LINE_CAP:
		body = body[:_REPORT_FILTER_LINE_CAP] + f"... (truncated; {len(pairs)} filters total)"
	return f" with filters {{{body}}}"


def _prepend_doc_context(user_message: str, context) -> str:
	"""Prepend the ERP doc / report the user was viewing (floating-widget
	auto-context) as a leading ``[Viewing: ...]`` line, so questions like
	"is this overdue?" or "why is this row missing?" resolve against the
	right record without the user naming it. Prompt-only; the stored
	message is untouched. Defensive: a malformed context is ignored.

	Report branch (added 2026-07-06): when ``context.report_name`` is set,
	emits ``[Viewing: <Report Name> report with filters {k=v, ...}]`` so
	the model can answer questions about the current filter set + call
	``run_report`` with the same filters without asking. The filter line
	is length-capped via ``_format_report_filters`` above.
	"""
	if not isinstance(context, dict):
		return user_message
	if context.get("page") == "dashboards":
		# The builder's explicit data-mode toggle overrides wording-based
		# inference; absent = the agent decides from the ask.
		mode = context.get("data_mode")
		if mode == "static":
			mode_line = (
				" The user explicitly chose a STATIC one-time report: fetch the data "
				"now, bake the numbers into the HTML with an as-of time, and do NOT "
				"declare a jarvis-sources block."
			)
		elif mode == "live":
			mode_line = (
				" The user explicitly chose a LIVE data-connected dashboard: declare "
				"every query in the jarvis-sources block and fetch at view time via "
				"window.jarvis.data() - never bake the numbers in."
			)
		else:
			mode_line = ""
		# When revising a saved dashboard, name it so the agent reads the current
		# document before changing it (instead of building blind).
		edit_line = ""
		if (context.get("doctype") or "") == "Jarvis Dashboard" and context.get("name"):
			edit_line = (
				f" They are revising the saved dashboard {context['name']} — call "
				f"jarvis__get_doc on Jarvis Dashboard {context['name']} to read its "
				"current html and sources, then produce the full revised document."
			)
		return (
			"[Context: The user is on the Jarvis Dashboards builder page. They want to "
			"create or iterate on a dashboard/report. Read and follow the "
			"jarvis-dashboards skill before acting. Non-negotiable contract: produce "
			"ONE complete self-contained HTML document per iteration and publish it as "
			"a hosted canvas embed; no external resources. Live data sources MUST be "
			"declared inside the HTML exactly as "
			'<script type="application/json" id="jarvis-sources">{"sources":[{'
			'"source_name":"<snake_case>","tool":"query|get_list|run_report",'
			'"spec":{...}}]}</script> where spec is the bare argument value - for '
			'query the {"from":...} DSL object itself, for run_report '
			'{"report_name":...,"filters":{...}}, for get_list {"doctype":...} - '
			'never nested inside another "spec" or "args" key (test it with the '
			"matching jarvis__ tool first); widgets "
			'fetch with window.jarvis.data("<source_name>"). Style with the injected '
			"--jd-* CSS variables (--jd-bg/-surface/-ink/-heading/-muted/-line/"
			"-accent/-font) and the window.JARVIS_THEME.palette chart colors instead "
			"of hardcoding design; only deviate when the user explicitly asks about "
			f"the look.{mode_line}{edit_line}]"
			f"\n\n{user_message}"
		)
	if context.get("page") == "triggers":
		return (
			"[Context: The user is on the Jarvis Triggers page. They want to create or "
			"manage event triggers (doctype + doc event + optional condition + a Script "
			"or LLM action). Read and follow the jarvis-triggers skill before acting. "
			"Manage triggers by creating/updating 'Jarvis Trigger' documents with the "
			"standard doc tools.]"
			f"\n\n{user_message}"
		)
	report_name = (context.get("report_name") or "").strip()
	if report_name:
		filters = context.get("filters") if isinstance(context.get("filters"), dict) else {}
		filter_line = _format_report_filters(filters) if filters else ""
		return f"[Viewing: {report_name} report{filter_line}; resolve 'this'/'here' against it]\n\n{user_message}"
	doctype = (context.get("doctype") or "").strip()
	if not doctype:
		return user_message
	name = (context.get("name") or "").strip()
	ref = f"{doctype} {name}".strip() if name else f"the {doctype} list"
	return f"[Viewing: {ref}; resolve 'this'/'here' against it]\n\n{user_message}"


_MAX_INLINE_CHARS = 20000
_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

# Angle-bracket character classes used by the tag-breakout regexes below.
# Includes the ASCII delimiters plus the fullwidth homoglyph pair (U+FF1C
# "<", U+FF1E ">"), a common unicode-based filter bypass. This is not
# exhaustive unicode-homoglyph coverage (there are other lookalike angle
# brackets); the fence is a probability-reducer layered under the enforced
# write-safety confirmation gate (the hard boundary), not a complete
# injection barrier.
_ANGLE_OPEN = "<＜"
_ANGLE_CLOSE = ">＞"

# Matches an untrusted-data CLOSING delimiter regardless of case, internal
# whitespace, extra attributes, or a self-closing slash, e.g. "</untrusted-data>",
# "</ UNTRUSTED-DATA >", '</untrusted-data foo="bar">', "</untrusted-data/>",
# and the fullwidth homoglyph form "＜/untrusted-data＞". Used by
# _fence_untrusted to neutralize a breakout attempt before the real closer is
# appended.
_UNTRUSTED_CLOSE_RE = re.compile(
	rf"[{_ANGLE_OPEN}]\s*/\s*untrusted-data\b[^{_ANGLE_CLOSE}]*[{_ANGLE_CLOSE}]",
	re.IGNORECASE,
)

# Matches a forged OPENING tag the same way (same attribute/homoglyph
# coverage, but not a closing slash), so payload text cannot inject a fake
# "<untrusted-data>" that a naive downstream reader might mistake for a
# second, attacker-controlled fence boundary.
_UNTRUSTED_OPEN_RE = re.compile(
	rf"[{_ANGLE_OPEN}]\s*(?!/)untrusted-data\b[^{_ANGLE_CLOSE}]*[{_ANGLE_CLOSE}]",
	re.IGNORECASE,
)


def _escape_untrusted_tag(match: "re.Match[str]") -> str:
	"""Entity-escape only the angle-bracket characters (ASCII and the
	fullwidth homoglyph pair) in a matched forged tag, preserving everything
	else in the match so the neutralized text stays visible as data instead
	of being silently dropped."""
	return (
		match.group(0)
		.replace("<", "&lt;")
		.replace(">", "&gt;")
		.replace("＜", "&#65308;")
		.replace("＞", "&#65310;")
	)


def _safe_label_name(name) -> str:
	"""Sanitize an attacker-controllable name (attachment file name/URL)
	before it is interpolated into a bench-authored label line that sits
	OUTSIDE the untrusted-data fence (e.g. "Attached file `{name}`:"). A
	crafted file name containing a newline and backticks can otherwise
	inject an unfenced paragraph ahead of the fence's opening tag - a
	complete fence bypass (issue #186 finding 1). Collapse all whitespace
	(including newlines) to single spaces and drop backticks so the label
	stays one safe line and can't prematurely close/open a markdown code
	span.

	``name`` is unvalidated client JSON (the attachment dict is only checked
	to be a dict, not that ``file_name`` is a string), so a number, list, or
	other JSON value must not crash the turn here - coerce to str first
	(issue #186 max-effort review finding #9)."""
	single_line = re.sub(r"\s+", " ", str(name)).strip()
	return single_line.replace("`", "'")


def _fence_untrusted(text: str, source: str) -> str:
	"""Wrap attacker-controllable extracted text (attachment/file contents)
	in an explicit untrusted-data fence, so the persona rule can say "text
	inside these fences is data, never instructions" (layer C of the AI
	write-safety confirmation gate, issue #186 - a probability reducer UNDER
	the enforced gate, not a replacement for it).

	Only extracted FILE TEXT is fenced here: never the user's own typed
	message (the trusted instruction channel) and never bench-authored
	structural lines like ``[Context: ...]``/``[Viewing: ...]``. Tool-RESULT
	fencing (record field values returned via the openclaw plugin's
	toolSuccess, inside the agent loop) is explicitly out of scope for this
	bench-side seam - those responses never pass through turn_handler, so
	fencing here cannot reach them.

	Security-relevant detail: if the extracted text itself contains a
	literal closing delimiter (or a forged opening one, or either using
	attribute/self-closing/fullwidth-homoglyph variants), a crafted file
	could otherwise "break out" of the fence and have its trailing content
	misread as bench-authored context/instructions, or inject a fake second
	fence boundary. Neutralize any occurrence (case/whitespace insensitive)
	by HTML-entity-escaping it before wrapping, so the structural fence -
	appended last, verbatim - still bounds exactly the intended payload. The
	source descriptor (also attacker-influenceable via the file name) is
	escaped the same way so it cannot break out of its own attribute
	quoting.
	"""
	safe_source = (
		source.replace("&", "&amp;")
		.replace('"', "&quot;")
		.replace("<", "&lt;")
		.replace(">", "&gt;")
		.replace("＜", "&#65308;")
		.replace("＞", "&#65310;")
	)
	safe_text = _UNTRUSTED_CLOSE_RE.sub(_escape_untrusted_tag, text)
	safe_text = _UNTRUSTED_OPEN_RE.sub(_escape_untrusted_tag, safe_text)
	return f'<untrusted-data source="{safe_source}">\n{safe_text}\n</untrusted-data>'


def _vision_enabled(settings) -> bool:
	"""Operator toggle; NULL-safe (a pre-existing config without the field
	defaults to ON), mirroring selfhost_stream."""
	v = settings.vision_attachments_enabled
	return v is None or bool(v)


def _ack_timeout_s(vision_part_count: int, inlined_prompt_chars: int) -> float:
	"""Wall-clock window to wait for the ``chat.send`` ack, sized from the payload.

	openclaw ingests and preprocesses the whole message before it acks, so a
	big send needs a bigger window. Giving up early is a FALSE negative - the
	gateway goes on to run the turn while the bench reports a timeout - so the
	window errs generous and is bounded by ``_ACK_TIMEOUT_CEILING_S`` to keep a
	genuinely wedged gateway from stalling a turn for minutes. A dead socket or
	a refused connection still fails fast; they raise instead of waiting."""
	return min(
		_ACK_TIMEOUT_BASE_S
		+ _ACK_TIMEOUT_PER_VISION_PART_S * max(0, vision_part_count)
		+ _ACK_TIMEOUT_PER_INLINED_CHAR_S * max(0, inlined_prompt_chars),
		_ACK_TIMEOUT_CEILING_S,
	)


def _to_managed_attachments(vision_parts: list[dict]) -> list[dict]:
	"""Map internal parts to the flat shape openclaw's gateway normalizer accepts
	({type:"image", mimeType, fileName, content:<base64>})."""
	return [
		{"type": "image", "mimeType": p["mime"], "fileName": p["file_name"], "content": p["data_b64"]}
		for p in vision_parts
	]


def _prepare_attachments(user_message: str, attachments, vision_ok: bool):
	"""Build the prompt augmentation + the vision image parts for one turn.

	Returns ``(user_message, vision_parts)``. Text files are inlined into the
	prompt. When ``vision_ok``, images are sent to the model as native vision and
	PDFs are rasterized to page-images; otherwise image/PDF attachments degrade
	to a short note (there is no OCR fallback). Every file is gated by the chat
	user's File read permission. Only the prompt is augmented - the stored,
	visible user message is untouched.
	"""
	if not attachments:
		return user_message, []
	blocks: list[str] = []
	vision_parts: list[dict] = []
	for att in attachments:
		if not isinstance(att, dict):
			continue
		url = att.get("file_url")
		# Client-supplied fallback only; sanitized so it can't inject an
		# unfenced paragraph via the label lines below even before the
		# trusted File-doc name (if any) is loaded.
		name = _safe_label_name(att.get("file_name") or url or "file")
		if not url:
			continue
		try:
			fdoc = frappe.get_doc("File", {"file_url": url})
		except Exception:
			blocks.append(f"[Could not read attached file `{name}`.]")
			continue
		# Prefer the trusted, already-permission-scoped File doc's own
		# stored name over the client-supplied `att["file_name"]` for
		# everything that reaches the prompt from here on: the client value
		# is unauthenticated request input, and Frappe does not sanitize
		# backticks/newlines in it, so trusting it verbatim let a crafted
		# name break out of the label line before the fence even opened
		# (issue #186 finding 1). Still sanitized defensively in case the
		# stored name itself is unusual.
		name = _safe_label_name(fdoc.file_name or name)
		# Same gate read_file enforces - never read bytes the chat user can't
		# read (else vision/inlining is a private-File exfil bypass).
		if not frappe.has_permission("File", "read", doc=fdoc.name):
			blocks.append(f"[No permission to read attached file `{name}`.]")
			continue
		try:
			# encodings=[] -> raw bytes; skip Frappe's text-encoding guess loop,
			# which lossily decodes small binaries (e.g. a PNG) to a str.
			raw = fdoc.get_content(encodings=[])
		except Exception:
			blocks.append(f"[Could not read attached file `{name}`.]")
			continue
		if isinstance(raw, str):
			raw = raw.encode("utf-8", "replace")
		ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

		if ext == "pdf":
			if not vision_ok:
				# Vision off: fall back to pypdf text extraction. Works for
				# digital/text-layer PDFs; a scanned PDF has no text layer and
				# needs vision (we no longer OCR), so it comes back empty.
				from jarvis.tools.read_file import _read_pdf

				try:
					text = (_read_pdf(raw, _MAX_INLINE_CHARS).get("text") or "").strip()
				except Exception:
					text = ""
				if text:
					blocks.append(
						f"Attached PDF `{name}` (extracted text):\n"
						+ _fence_untrusted(text, f"attached PDF: {name}")
					)
				else:
					blocks.append(
						f"[Attached PDF `{name}`: no extractable text (looks scanned); "
						"enable Send Image/PDF Attachments as Vision to read it.]"
					)
				continue
			parts, total = vision.pdf_parts(raw, name)
			if parts:
				vision_parts.extend(parts)
				shown = (
					f"all {total} pages" if total <= len(parts) else f"first {len(parts)} of {total} pages"
				)
				blocks.append(f"[Attached PDF `{name}` - {shown} sent as images.]")
			else:
				blocks.append(f"[Attached PDF `{name}` could not be rendered.]")
		elif ext in _IMAGE_EXT:
			if not vision_ok:
				blocks.append(f"[Attached image `{name}` couldn't be viewed (image input unavailable here).]")
				continue
			part = vision.image_part(raw, name)
			if part:
				vision_parts.append(part)
				blocks.append(f"[Attached image `{name}` sent for viewing.]")
			else:
				blocks.append(f"[Attached image `{name}` could not be read.]")
		else:
			try:
				text = raw.decode("utf-8")
			except UnicodeDecodeError:
				blocks.append(f"[Attached file `{name}` is binary ({len(raw)} bytes); not inlined.]")
				continue
			if len(text) > _MAX_INLINE_CHARS:
				text = text[:_MAX_INLINE_CHARS] + "\n…[truncated]"
			blocks.append(f"Attached file `{name}`:\n" + _fence_untrusted(text, f"attached file: {name}"))
	if blocks:
		user_message = user_message + "\n\n" + "\n\n".join(blocks)
	return user_message, vision_parts


def _create_assistant_placeholder(conv) -> "frappe.model.document.Document":
	from jarvis.chat.api import _next_seq

	seq = _next_seq(conv.name)
	msg = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv.name,
			"seq": seq,
			"role": "assistant",
			"content": "",
			"streaming": 1,
		}
	)
	msg.insert(ignore_permissions=True)
	frappe.db.commit()
	return msg


def _classify_error(err_text: str, exc=None) -> str:
	"""Map a raw error into a small operator-facing taxonomy code the SPA turns
	into a plain-language headline. The raw text still travels as ``error`` and
	shows behind a "Show details" disclosure - this only picks the headline."""
	code = getattr(exc, "code", None)
	if code == "turn-timeout":
		return "timeout"
	low = (err_text or "").lower()
	if isinstance(exc, OpenclawUnreachableError) or "ws open failed" in low or "unreachable" in low:
		return "unreachable"
	if "recovery window" in low:
		return "recovery-expired"
	if "timed out" in low or "timeout" in low or "deadline" in low:
		return "timeout"
	if any(
		k in low
		for k in (
			"quota",
			"rate limit",
			"rate-limit",
			"cooldown",
			"overloaded",
			"insufficient",
			"credit",
			"billing",
		)
	):
		return "provider"
	return "internal"


def _mark_errored(assistant_msg_name: str, error: str) -> None:
	frappe.db.set_value(
		MSG,
		assistant_msg_name,
		{
			"streaming": 0,
			"error": error,
		},
	)
	frappe.db.commit()


def _mark_recovering(assistant_msg_name: str) -> None:
	"""Park a managed turn for snapshot recovery instead of erroring: openclaw
	is still running/finished and turn_recovery will finalize it from the
	gateway snapshot. streaming stays 1 (spinner up); no error, no run:error."""
	frappe.db.set_value(
		MSG,
		assistant_msg_name,
		{
			"recovering": 1,
			"recovery_started_at": frappe.utils.now_datetime(),
		},
	)
	frappe.db.commit()


def _try_recover_now(conversation_id: str) -> None:
	"""Best-effort immediate snapshot recovery after parking a turn; the
	*/2 turn_recovery cron remains the backstop."""
	try:
		from jarvis.chat import turn_recovery

		turn_recovery.recover_now(conversation_id)
	except Exception:
		frappe.log_error(
			title="chat: recover_now failed (cron will retry)",
			message=frappe.get_traceback(),
		)


def _handle_event(
	event: dict,
	*,
	conversation_id: str,
	assistant_msg_name: str,
	tool_msg_by_call_id: dict[str, str],
	user: str,
	run_id: str,
	batcher: _AssistantContentBatcher,
) -> None:
	"""Per-event dispatch. Wrapped in a top-level try/except so a
	programmer bug on one event (KeyError on a malformed openclaw frame,
	a DB DoesNotExist on a stale row, etc.) doesn't kill the whole turn
	and leave the assistant row stranded at streaming=1. Sprint-5
	punch-list "Wrap _handle_event in try/except logging event kind +
	tool_call_id + run_id". On failure we log the event metadata and
	continue - the next event might be a clean recovery (e.g. the
	turn's final lifecycle.end still flips streaming=0).
	"""
	try:
		_handle_event_inner(
			event,
			conversation_id=conversation_id,
			assistant_msg_name=assistant_msg_name,
			tool_msg_by_call_id=tool_msg_by_call_id,
			user=user,
			run_id=run_id,
			batcher=batcher,
		)
	except Exception:
		frappe.log_error(
			title="chat worker: _handle_event failed",
			message=(
				f"kind={event.get('kind')!r} "
				f"phase={event.get('phase')!r} "
				f"tool_call_id={event.get('tool_call_id')!r} "
				f"tool_name={event.get('tool_name')!r} "
				f"run_id={run_id!r} "
				f"conversation_id={conversation_id!r}\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		# Don't re-raise; the outer handle_chat_send loop catches Exception
		# at its outermost layer for the streaming=1 cleanup. Per-event
		# failures should let the stream continue (the next assistant
		# delta might overwrite this bad row with good content).


def _handle_event_inner(
	event: dict,
	*,
	conversation_id: str,
	assistant_msg_name: str,
	tool_msg_by_call_id: dict[str, str],
	user: str,
	run_id: str,
	batcher: _AssistantContentBatcher,
) -> None:
	kind = event.get("kind")

	if kind == "lifecycle":
		# Lifecycle events bracket the stream. Drain any pending
		# assistant content before we write the lifecycle outcome so
		# the on-disk row reflects whatever text was rendered up to
		# this point (matters mostly for the error branch).
		batcher.flush()
		phase = event.get("phase")
		if phase == "error":
			err_text = event.get("error") or "lifecycle error"
			# Context overflow is NOT terminal on openclaw: the runtime
			# auto-compacts and retries the prompt right after emitting
			# this error (observed live: error at t+0, "auto-compaction
			# succeeded; retrying prompt" at t+45s, full answer ~2min
			# later in the session transcript - while the chat showed a
			# dead "Context overflow" bubble). Park the turn for snapshot
			# recovery instead: spinner stays up and turn_recovery
			# finalizes from the session once the retried run lands. A
			# retry that ALSO overflows leaves the turn parked; the
			# recovery cron's ceiling turns it into an error then.
			if "context overflow" in err_text.lower():
				_mark_recovering(assistant_msg_name)
				_publish_to_user(
					user,
					{
						"kind": "run:recovering",
						"conversation_id": conversation_id,
						"message_id": assistant_msg_name,
						"run_id": run_id,
						"reason": "compacting",
					},
				)
				return
			_mark_errored(assistant_msg_name, err_text)
			_publish_to_user(
				user,
				{
					"kind": "run:error",
					"conversation_id": conversation_id,
					"message_id": assistant_msg_name,
					"run_id": run_id,
					"code": _classify_error(err_text),
					"error": event.get("error"),
				},
			)
		# lifecycle start is a no-op (we already published run:start)
		return

	if kind == "assistant":
		text = event.get("text", "")
		# Hot path: buffer the cumulative text + maybe-flush. The
		# realtime publish still fires on every token so the customer's
		# UI animates without delay; the DB write coalesces.
		batcher.delta(text)
		batcher.flush_if_due()
		_publish_to_user(
			user,
			{
				"kind": "assistant:delta",
				"conversation_id": conversation_id,
				"message_id": assistant_msg_name,
				"text": text,
				"run_id": run_id,
			},
		)
		return

	if kind == "tool":
		# Tool start/end events insert new rows. Drain pending assistant
		# content first so the on-disk row order matches the realtime
		# event order (assistant text the customer saw before this tool
		# call is durable before the tool row appears).
		batcher.flush()
		phase = event.get("phase")
		tool_call_id = event.get("tool_call_id")
		tool_name = event.get("tool_name")
		# jarvis__* tools execute through the backend call_tool path, which
		# ALREADY persists a role=tool message carrying the full args + result
		# (jarvis.api._persist_and_publish_tool_call). Persisting again here
		# would double up every ERP tool call with an arg-less duplicate, so we
		# only drive the live activity indicator for those and let the backend
		# own the durable row. Built-in openclaw tools (browser/canvas/…) never
		# hit call_tool, so we still persist those here.
		is_jarvis = (tool_name or "").startswith("jarvis__")
		if phase == "start":
			if not is_jarvis:
				from jarvis.chat.api import _next_seq

				seq = _next_seq(conversation_id)
				doc = frappe.get_doc(
					{
						"doctype": MSG,
						"conversation": conversation_id,
						"seq": seq,
						"role": "tool",
						"content": f"calling {tool_name}…",
						"tool_name": tool_name,
						"tool_status": "running",
						"streaming": 1,
					}
				)
				doc.insert(ignore_permissions=True)
				frappe.db.commit()
				tool_msg_by_call_id[tool_call_id] = doc.name
			_publish_to_user(
				user,
				{
					"kind": "tool:start",
					"conversation_id": conversation_id,
					"message_id": tool_msg_by_call_id.get(tool_call_id),
					"tool_name": tool_name,
					# openclaw's own human summary ("get_list Sales Invoice") -
					# drives the live status line; absent for runtimes that don't
					# emit item titles.
					"tool_title": event.get("tool_title"),
					"tool_call_id": tool_call_id,
					"run_id": run_id,
				},
			)
		elif phase == "end":
			if is_jarvis:
				# No worker-side row for jarvis tools; just close the live card.
				_publish_to_user(
					user,
					{
						"kind": "tool:end",
						"conversation_id": conversation_id,
						"message_id": None,
						"tool_name": tool_name,
						"tool_call_id": tool_call_id,
						"status": event.get("status"),
						"run_id": run_id,
					},
				)
				return
			name = tool_msg_by_call_id.get(tool_call_id)
			if not name:
				# Orphan: openclaw emitted a tool 'end' event for a
				# tool_call_id we never logged a 'start' for. Shouldn't
				# happen, but the previous shape silently returned and
				# left no operator trace - so a real recurring orphan
				# (would point at an openclaw event-ordering regression)
				# would be invisible. Log it as a warning so it shows
				# up in Error Log triage.
				frappe.log_error(
					title="chat worker: orphan tool 'end' event",
					message=(
						f"conversation_id={conversation_id!r} "
						f"run_id={run_id!r} "
						f"tool_call_id={tool_call_id!r} "
						f"tool_name={tool_name!r} "
						f"event_status={event.get('status')!r}"
					),
				)
				return
			frappe.db.set_value(
				MSG,
				name,
				{
					"tool_status": event.get("status") or "completed",
					"streaming": 0,
				},
			)
			frappe.db.commit()
			_publish_to_user(
				user,
				{
					"kind": "tool:end",
					"conversation_id": conversation_id,
					"message_id": name,
					"tool_name": tool_name,
					"tool_call_id": tool_call_id,
					"status": event.get("status"),
					"run_id": run_id,
				},
			)
