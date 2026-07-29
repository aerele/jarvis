"""Best-effort pre-warming of the openclaw container's OpenAI prefix cache.

The first turn of a fresh session pays a cold provider prefill over the
large static system prefix (persona + skills + tool schema). That cache is
prefix-keyed and container-wide, so one cheap throwaway agent turn warms it
for every subsequent new chat in the same container. We never touch the
user's real session or write chat rows. Always best-effort; never raises.
"""

import uuid

import frappe

from jarvis.chat.openclaw_client import OpenclawSession

# Cooldown between warm-ups for one bench. 2026-07 latency plan, Phase
# 1.4: was 25 min, which is LONGER than the providers' prompt-cache
# retention (OpenAI evicts after ~5-10 min of inactivity; Gemini implicit
# caching is similar or shorter) — so for most of each half-hour the
# cooldown key said "warm" while the provider cache had already cooled,
# and the first turn ate a cold prefill anyway. 4 min keeps every warm
# inside the retention window; paired with the */5 keep_warm cron so each
# tick re-warms. Watch warm-turn token spend after this change.
_WARM_COOLDOWN_S = 4 * 60

# Short in-progress TTL set BEFORE the slow connect so concurrent opens
# cannot fan out into concurrent warm-ups. Expires quickly on failure
# so a failed warm retries soon rather than blocking for the full cooldown.
# Accepted best-effort SET race: two concurrent callers may both pass
# get_value before either sets this key; the second warm is harmless
# (#6 accepted best-effort).
_WARM_INPROGRESS_S = 90


# The previous warm's session key, remembered so the NEXT warm can reclaim it
# (see _reclaim_previous). TTL is far longer than the cooldown: losing this
# pointer leaks one session, so err on the side of remembering. The orphan sweep
# (session_lifecycle) is the backstop for whatever this misses.
_WARM_LAST_TTL_S = 24 * 60 * 60


def _warm_cooldown_key() -> str:
	return f"jarvis:chat:prefix_warm:{frappe.local.site}"


def _warm_last_key() -> str:
	return f"jarvis:chat:prefix_warm:last:{frappe.local.site}"


def _reclaim_previous(sess, prev, current: str) -> None:
	"""Delete the throwaway session the PREVIOUS warm created.

	A warm cannot delete its own: fire_agent is fire-and-forget, so the turn that
	warms the prefix is still running when it returns, and waiting for it would
	block a short-queue worker for no benefit. Instead each warm reclaims its
	predecessor, which the cooldown guarantees has had at least _WARM_COOLDOWN_S
	to finish. Steady state is one live prewarm session rather than one per warm -
	at a 4-minute cooldown the old create-and-forget leaked up to ~350 sessions a
	day against an orphan sweep capped at 25.

	Best-effort: on failure (or a lost cache pointer) the session is left for the
	orphan sweep, which reaps jarvis-prewarm-* on a short grace."""
	if not prev or not isinstance(prev, str) or prev == current:
		return
	try:
		sess.delete_session(prev)
	except Exception:
		frappe.logger("jarvis.chat.prewarm").debug(
			"previous warm session delete failed",
			exc_info=True,
		)


def _gateway_ws_url(settings) -> str:
	"""Convert Jarvis Settings.agent_url http(s):// -> ws(s)://."""
	return (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")


def _resolve_default_model_and_provider(settings) -> tuple[str, str | None]:
	"""Return (model, provider) a real default new-chat first turn would use.

	Mirrors turn_handler._resolve_model_and_provider for the no-override
	case: default model = Jarvis Settings.llm_model; provider id only in
	oauth mode. Reuses turn_handler's provider map as the single source of
	truth so the warm-up cannot drift to a different provider's cache."""
	from jarvis.chat.turn_handler import _PROVIDER_LABEL_TO_OPENCLAW_ID

	model = settings.llm_model or ""
	provider = (
		_PROVIDER_LABEL_TO_OPENCLAW_ID.get(settings.llm_provider)
		if settings.llm_auth_mode == "oauth"
		else None
	)
	return model, provider


def warm_prefix() -> bool:
	"""Fire one throwaway warm-up turn for this bench's container. Returns
	True if a warm-up was fired, False if skipped (debounced, not
	configured) or on any error. Never raises."""
	try:
		cache = frappe.cache()
		key = _warm_cooldown_key()
		if cache.get_value(key):
			return False
		settings = frappe.get_single("Jarvis Settings")
		# OpenclawSession.connect authenticates via device pairing
		# (ensure_paired / chat_device_* creds), NOT agent_token.
		# agent_token is empty on managed/device-paired benches.
		gateway_url = _gateway_ws_url(settings)
		if not gateway_url:
			return False
		# Set a short in-progress marker BEFORE the slow connect so a burst
		# of opens (or a broken gateway) cannot fan out into concurrent
		# warm-ups. On any exception this short marker expires in
		# _WARM_INPROGRESS_S, allowing a retry soon. The full cooldown is
		# only set after a successful warm so a transient blip does not
		# disable warming for 25 min.
		cache.set_value(key, "1", expires_in_sec=_WARM_INPROGRESS_S)
		model, provider = _resolve_default_model_and_provider(settings)
		import time

		t0 = time.monotonic()
		sess = OpenclawSession.connect(gateway_url)
		try:
			throwaway = sess.create_session(label=f"jarvis-prewarm-{uuid.uuid4().hex[:8]}")
			sess.fire_agent(
				throwaway,
				"/think off warmup",
				uuid.uuid4().hex,
				model=model or None,
				provider=provider,
			)
			# Remember the new throwaway BEFORE reclaiming the old one: a failure
			# between the two then leaks only the PREVIOUS session (the orphan
			# sweep still collects it) instead of losing the pointer to the one we
			# just created, which would leak one every warm, forever.
			last_key = _warm_last_key()
			prev = cache.get_value(last_key)
			cache.set_value(last_key, throwaway, expires_in_sec=_WARM_LAST_TTL_S)
			_reclaim_previous(sess, prev, throwaway)
		finally:
			sess.close()
		# Latency telemetry (plan Phase 0): connect+create+fire duration.
		# (fire_agent is fire-and-forget, so this does NOT include the
		# prefill itself — cold-vs-warm shows up in real turns'
		# first_delta_ms, logged by turn_handler.)
		from jarvis.chat.latency import get_logger as _get_latency_logger

		_get_latency_logger().info(
			"warm_prefix fire_ms=%d",
			int((time.monotonic() - t0) * 1000),
		)
		# Warm succeeded: arm the full cooldown so the next cron tick skips.
		cache.set_value(key, "1", expires_in_sec=_WARM_COOLDOWN_S)
		return True
	except Exception:
		frappe.logger("jarvis.chat.prewarm").warning("prefix warm-up failed", exc_info=True)
		return False


def keep_warm_if_active() -> None:
	"""Scheduler entry: keep the prefix cache warm for benches with recent
	chat activity, so a returning user's first turn is warm after an idle
	gap. No-op on idle benches. Runs on the existing scheduler; the per-bench
	debounce in warm_prefix bounds frequency."""
	try:
		cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30)
		recent = frappe.db.exists("Jarvis Chat Message", {"creation": [">", cutoff]})
		if not recent:
			return
		warm_prefix()
	except Exception:
		frappe.logger("jarvis.chat.prewarm").debug("keep_warm skipped", exc_info=True)


def warm_on_login(login_manager=None) -> None:
	"""``on_session_creation`` hook (2026-07 latency plan, Phase 1.4): start
	warming as soon as anyone logs in, before the chat page even loads —
	the warm-up has the whole page-load window to land. Same debounced
	enqueue as the chat-load trigger, so non-chat logins cost one cache
	read. Best-effort, never raises (a hook failure must never block login).
	"""
	try:
		enqueue_warm_if_due()
	except Exception:
		pass


def enqueue_warm_if_due() -> None:
	"""Warm-on-chat-load: enqueue a prefix warm-up in a background job if the
	per-bench cooldown has lapsed. Called from list_conversations (which every
	chat surface hits on load), so the first turn of a new chat gets a warm
	prefix without a frontend change. Just a cheap cache read on the request
	path - the connect + warm runs off the web worker. Best-effort, never
	raises, and debounced so repeated calls do not fan out into jobs."""
	try:
		if frappe.cache().get_value(_warm_cooldown_key()):
			return
		frappe.enqueue("jarvis.chat.prewarm.warm_prefix", queue="short")
	except Exception:
		frappe.logger("jarvis.chat.prewarm").debug("enqueue_warm_if_due skipped", exc_info=True)
