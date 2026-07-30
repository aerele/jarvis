"""Best-effort pre-warming of the openclaw container's provider prefix cache.

The first turn of a fresh session pays a cold provider prefill over the large
static system prefix (persona + skills + tool schema). That cache is
prefix-keyed and container-wide, so one cheap throwaway agent turn warms it for
every subsequent new chat in the same container. We never touch the user's real
session or write chat rows. Always best-effort; never raises.

WHAT A WARM COSTS (issue #548)
------------------------------
A warm is a real upstream ``agent`` run billed to the TENANT'S OWN credential.
There is no mode in which the operator pays: ``llm_auth_mode`` is api_key /
subscription / oauth and all three are the customer's. So a warm can never be
cost-neutral, only cost-traded-for-latency:

  without prewarm  the first turn pays a full prefix, later turns pay the
                   provider's discounted cached rate
  with prewarm     the warm pays a full prefix, and then EVERY turn pays the
                   cached rate

which is strictly more spend per cycle when a turn does follow, and one wholly
wasted prefix when none does. This app fired 528 warms against 86 real turns on
jarvis.proxy (six paid warm-ups per turn) and exhausted a free-tier Gemini key
that real chat turns then 429'd on.

So the only defensible trigger is one correlated with a user about to send a
first message into a plausibly-cold container, firing at most once per arrival.
Hence, as of #548:

- ONE trigger: the chat-surface load (``enqueue_warm_if_due``, called from
  ``list_conversations``). Two were removed.
  * The ``*/5`` ``keep_warm_if_active`` cron. Its gate ("any chat message in
    the last 30 minutes") was INVERTED with respect to its own purpose: recent
    chat traffic is exactly what keeps the provider cache warm for free, so the
    cron spent up to seven paid warms per burst of activity precisely when
    warming was worthless, and no-opped on the idle benches where a cold prefix
    is actually possible. A timer also cannot know a turn is coming, which is
    the whole premise of warming.
  * The ``on_session_creation`` login warm. It billed a request for every login
    to the site, including desk and API logins by users who never open chat, to
    buy a sub-second head start on a multi-second prefill that the chat-surface
    load already starts.
- A warm is SKIPPED while the prefix is already hot (``_prefix_recently_used``).
  That is what stops the SPA's post-send list refreshes from billing warms into
  a session whose own turns keep the prefix warm: sixteen call sites reach
  ``list_conversations``, several of them after every single send.
- The cooldown is claimed ATOMICALLY (``_claim_warm_slot``). The get-then-set it
  replaces spanned a ``Jarvis Settings`` load, and two warms were observed
  passing it 6ms apart on jarvis-pool-bf4097.

NOT MEASURED
------------
No benefit number exists for any of this, and #548 is the right place to say so
rather than to keep implying one. The turn telemetry line records no warm/cold
state and no cached-token count, so ``first_delta_ms`` cannot be split by prefix
state even in principle; the Stage-A/B harnesses run against a FakeGateway, so
their ``warm_session`` measures nothing about provider caching; and "watch
warm-turn token spend after this change" (commit 1017f3ab, which tripled the
frequency) was never done - ``fire_ms`` below times the WS round trip, not the
LLM work. Until a warm-vs-cold ``first_delta_ms`` split exists, treat the one
remaining trigger as unproven and keep its volume proportional to real use.
``jarvis_prefix_prewarm: 0`` in site_config turns it off entirely.
"""

import pickle
import time
import uuid

import frappe

from jarvis.chat.agent_client import AgentSession, oneshot_run_id
from jarvis.chat.session_lifecycle import reclaim_throwaway_session

# Ceiling on how often the one remaining trigger can bill a warm for one bench.
# 4 min sits inside the shortest documented provider retention (OpenAI evicts a
# prompt cache after ~5-10 min idle; Anthropic's default ephemeral TTL is 5
# min), so a chat-surface load that finds the cooldown lapsed is always looking
# at a plausibly-cold cache. It is a CEILING, not a schedule: nothing re-warms
# on a timer any more (#548). Note the retention figure is unverifiable for the
# provider that actually blew the quota - Gemini's IMPLICIT cache has no
# documented TTL - so do not read "4 min is inside the window" as measured.
_WARM_COOLDOWN_S = 4 * 60

# Short in-progress TTL claimed BEFORE any slow work so a burst of chat-surface
# loads cannot fan out into concurrent billed warm-ups. Expires quickly on
# failure so a failed warm retries soon rather than blocking for the full
# cooldown. Claimed atomically - see _claim_warm_slot.
_WARM_INPROGRESS_S = 90

# A real chat turn warms this container-wide prefix far better than a throwaway
# does, so a warm within _RECENT_TURN_S of one is pure spend. Two minutes is
# comfortably inside every documented retention floor, and it is the window the
# SPA's post-send list refreshes land in - the amplifier behind the six-warms-
# per-turn ratio in #548. This is the INVERSE of the gate the deleted keep-warm
# cron used, which is precisely what was wrong with it.
_RECENT_TURN_S = 120

# Explicit falsy site_config values that turn prewarming OFF. Absence is NEVER
# one of them: prewarming has always been on, so only an operator writing the
# key can disable it. Same absent-vs-explicit-0 shape as `jarvis_pump_enabled`
# ("0" is a truthy Python string, which is why this set exists).
_PREWARM_CONF_KEY = "jarvis_prefix_prewarm"
_EXPLICIT_OFF_VALUES = frozenset({"0", "false", "no", "off", ""})

# The cooldown marker, pickled so frappe's get_value/set_value still read and
# write it. The atomic claim below bypasses set_value for atomicity, not to
# change the stored format.
_CLAIM_VALUE = pickle.dumps("1")


# The previous warm's session key AND the moment its turn was fired, remembered
# so the NEXT warm can reclaim it (see _reclaim_previous). TTL is far longer than
# the cooldown: losing this pointer leaks one session, so err on the side of
# remembering. The orphan sweep (session_lifecycle) is the backstop for whatever
# this misses.
_WARM_LAST_TTL_S = 24 * 60 * 60


def _warm_cooldown_key() -> str:
	return f"jarvis:chat:prefix_warm:{frappe.local.site}"


def _warm_last_key() -> str:
	return f"jarvis:chat:prefix_warm:last:{frappe.local.site}"


def _prewarm_enabled() -> bool:
	"""Is prefix prewarming switched on for this site?

	Deliberately an OPERATOR site_config flag and NOT a per-tenant opt-in field.
	Every tenant is BYO - api_key, subscription and oauth all charge the customer
	- so a per-tenant toggle would be asking each customer to answer a question
	nobody has data for (see NOT MEASURED above). Measure the warm-vs-cold
	first_delta_ms split first; until then the honest position is a volume
	proportional to real use, plus an off switch for a tenant whose quota is
	tight enough to matter.
	"""
	flag = frappe.conf.get(_PREWARM_CONF_KEY)
	if flag is None:
		return True
	return str(flag).strip().lower() not in _EXPLICIT_OFF_VALUES


def _claim_warm_slot(cache, key: str, ttl_s: int) -> bool:
	"""Atomically take the right to warm. True for exactly ONE of N callers.

	``get_value`` then ``set_value`` cannot express this. The window between the
	two spanned a ``Jarvis Settings`` load (a DB round trip), and two warms were
	observed passing it 6ms apart on jarvis-pool-bf4097 - each one a billed
	upstream request against the tenant's own key, and the second one also the
	trigger for the #535 reclaim race. ``SET NX EX`` collapses the read and the
	write into one Redis round trip, so a burst produces exactly one warm.

	Raises on a Redis outage, which callers turn into "no warm". Failing CLOSED
	is the right side to fail on for work that spends the customer's money.

	``set`` is plain redis-py, so the site prefix has to be applied by hand -
	unlike ``exists`` below, which frappe overrides to do it for you.
	"""
	return bool(cache.set(cache.make_key(key), _CLAIM_VALUE, ex=ttl_s, nx=True))


def _warm_slot_held(cache, key: str) -> bool:
	"""Is a cooldown or an in-flight warm already holding the slot?

	``exists`` rather than ``get_value`` so the answer can never come from
	``frappe.local.cache``, which the atomic claim above does not populate.
	Frappe's override applies ``make_key`` itself, so the key is passed raw.
	"""
	return bool(cache.exists(key))


def _prefix_recently_used() -> bool:
	"""Did a real chat turn touch this container's prefix within
	``_RECENT_TURN_S``? Then the provider cache is warm and a throwaway warm buys
	a bill and nothing else.

	One indexed EXISTS - frappe gives every non-child table an index on
	``creation`` - and it runs in the background job, never on the web request.
	"""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-_RECENT_TURN_S)
	return bool(frappe.db.exists("Jarvis Chat Message", {"creation": [">", cutoff]}))


def _log_skip(reason: str) -> None:
	"""Record a skipped warm on the same logger as a fired one.

	The six-warms-per-turn ratio in #548 was only measurable because the fire
	logged; the skips did not, so nobody could see what share of the volume was
	avoidable. With both on one file the paid-warm rate is readable directly.
	"""
	from jarvis.chat.latency import get_logger as _get_latency_logger

	_get_latency_logger().info("warm_prefix skipped reason=%s", reason)


def _previous_pointer(prev) -> tuple[str, float | None]:
	"""Unpack the remembered ``{"key", "fired_at"}`` pointer -> (key, fired_at).

	Tolerates the pre-#535 shape, a bare session-key string with no fire time:
	one of those can still be sitting in the cache under the 24h TTL when this
	deploys. It reclaims exactly as it did before, which is correct for it - the
	cooldown makes such a pointer minutes old in every case but the
	concurrent-warm race, and that race cannot span a deploy."""
	if isinstance(prev, dict):
		key = prev.get("key")
		fired_at = prev.get("fired_at")
		return (
			key if isinstance(key, str) else "",
			fired_at if isinstance(fired_at, (int, float)) else None,
		)
	return (prev if isinstance(prev, str) else ""), None


def _reclaim_previous(sess, prev, current: str) -> None:
	"""Delete the throwaway session the PREVIOUS warm created.

	A warm cannot delete its own: fire_agent is fire-and-forget, so the turn that
	warms the prefix is still running when it returns, and waiting for it would
	block a short-queue worker for no benefit. Instead each warm reclaims its
	predecessor, which the cooldown USUALLY guarantees has had at least
	_WARM_COOLDOWN_S to finish. Steady state is one live prewarm session rather
	than one per warm - at a 4-minute cooldown the old create-and-forget leaked
	up to ~350 sessions a day against an orphan sweep capped at 25.

	"Usually" is why this goes through reclaim_throwaway_session (issue #525).
	The cooldown check is a get-then-set on the cache, so two warms can pass it
	in the same instant (observed 6ms apart on jarvis-pool-bf4097); the second
	then reads a "previous" pointer the first wrote milliseconds ago and deletes
	a session whose warm turn is still running. That rename killed the run and
	openclaw re-created the session file 568ms later. Probing hasActiveRun first
	turns that into a skipped reclaim - the orphan sweep collects it - instead of
	a killed run and a fresh orphan.

	The remembered fire time is what makes that probe trustworthy (issue #535).
	openclaw accepts a run a median 670ms before it starts one, and reports
	"accepted, not started" and "finished" identically, so probing a predecessor
	fired milliseconds ago answers "idle" for a run that is about to begin -
	the very race above, unfixed. Handing reclaim_throwaway_session the fire time
	makes it decline that delete instead.

	Best-effort: on failure (or a lost cache pointer) the session is left for the
	orphan sweep, which reaps jarvis-prewarm-* on a short grace."""
	key, fired_at = _previous_pointer(prev)
	if not key or key == current:
		return
	reclaim_throwaway_session(sess, key, logger_name="jarvis.chat.prewarm", fired_at=fired_at)


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
	"""Fire one throwaway warm-up turn for this bench's container.

	Returns True only when an upstream run was actually fired. False when it was
	skipped - switched off, the slot already claimed, the prefix already hot from
	a real turn, or the bench unconfigured - and on any error. Never raises.

	Every False is a billed request NOT sent against the tenant's own quota,
	which is why the skips are logged (``_log_skip``) rather than silent."""
	try:
		if not _prewarm_enabled():
			return False
		cache = frappe.cache()
		key = _warm_cooldown_key()
		# Claim the slot BEFORE any slow work: the claim is the only thing
		# standing between a burst of chat-surface loads and a burst of billed
		# upstream runs, so nothing that can take milliseconds may precede it.
		#
		# The claim's own TTL is the SHORT one, and only two exits below extend
		# it to the full cooldown: a warm that landed, and a prefix already hot.
		# Everything else - an exception, or a bench with no gateway configured -
		# keeps the 90s, so a transient blip retries in 90s instead of disabling
		# warming for the whole cooldown, and a bench that becomes configured
		# mid-onboarding starts warming within 90s rather than four minutes.
		# Those exits spend nothing, so a retry costs one job and one Settings
		# read; the thing worth throttling hard is a FIRED warm, not a skipped
		# one.
		if not _claim_warm_slot(cache, key, _WARM_INPROGRESS_S):
			return False
		if _prefix_recently_used():
			# Already hot from a real turn. Nothing failed, there is simply
			# nothing worth paying for until the cache could have cooled, so
			# arm the full cooldown rather than the short in-flight marker.
			cache.set_value(key, "1", expires_in_sec=_WARM_COOLDOWN_S)
			_log_skip("recent_turn")
			return False
		settings = frappe.get_single("Jarvis Settings")
		# AgentSession.connect authenticates via device pairing
		# (ensure_paired / chat_device_* creds), NOT agent_token.
		# agent_token is empty on managed/device-paired benches.
		gateway_url = _gateway_ws_url(settings)
		if not gateway_url:
			_log_skip("not_configured")
			return False
		model, provider = _resolve_default_model_and_provider(settings)
		t0 = time.monotonic()
		sess = AgentSession.connect(gateway_url)
		try:
			throwaway = sess.create_session(label=f"jarvis-prewarm-{uuid.uuid4().hex[:8]}")
			# The pin is the whole point: this warms ONE model's prefix cache,
			# so a failover would warm the wrong one. openclaw answers an
			# explicit model by dropping the run's fallback chain (#531), and
			# the prefixed run id is what tells a later log reader that the
			# resulting ``next=none`` is by design rather than a dead chain.
			sess.fire_agent(
				throwaway,
				"/think off warmup",
				oneshot_run_id("prewarm", uuid.uuid4().hex, model=model, provider=provider),
				model=model or None,
				provider=provider,
			)
			# Stop the clock HERE, not after the reclaim below. The reclaim can
			# now spend up to a couple of seconds waiting out the previous warm's
			# run, and folding that into fire_ms would silently corrupt the one
			# number this telemetry exists to watch.
			fire_ms = int((time.monotonic() - t0) * 1000)
			# Remember the new throwaway BEFORE reclaiming the old one: a failure
			# between the two then leaks only the PREVIOUS session (the orphan
			# sweep still collects it) instead of losing the pointer to the one we
			# just created, which would leak one every warm, forever.
			last_key = _warm_last_key()
			prev = cache.get_value(last_key)
			# Remember WHEN as well as WHICH: the next warm needs the fire time to
			# tell "this predecessor's run finished" from "this predecessor's run
			# has not started yet", which sessions.list reports identically (issue
			# #535). Stamped here rather than at the fire a few ms above; that only
			# makes the guard marginally longer, never shorter.
			cache.set_value(
				last_key,
				{"key": throwaway, "fired_at": time.time()},
				expires_in_sec=_WARM_LAST_TTL_S,
			)
			_reclaim_previous(sess, prev, throwaway)
		finally:
			sess.close()
		# Latency telemetry (plan Phase 0): connect+create+fire duration, as
		# measured above before the reclaim. fire_agent is fire-and-forget, so
		# this is the WS round trip and NOT the prefill - it says nothing about
		# what the warm bought. Turns log first_delta_ms but carry no warm/cold
		# marker, so the two cannot be joined; that missing marker is why #548
		# could price this feature's cost and not its benefit.
		from jarvis.chat.latency import get_logger as _get_latency_logger

		_get_latency_logger().info("warm_prefix fire_ms=%d", fire_ms)
		# Warm landed: extend the short claim to the full cooldown, which is now
		# a spend ceiling for the chat-surface trigger rather than a handshake
		# with a cron tick.
		cache.set_value(key, "1", expires_in_sec=_WARM_COOLDOWN_S)
		return True
	except Exception:
		frappe.logger("jarvis.chat.prewarm").warning("prefix warm-up failed", exc_info=True)
		return False


def enqueue_warm_if_due() -> None:
	"""Warm on chat-surface load: the ONE prewarm trigger (#548).

	Called from ``list_conversations``, which every chat surface hits on load, so
	the first turn of a new chat gets a warm prefix without a frontend change.
	Just a cheap Redis existence check on the request path - the connect + warm
	runs off the web worker, and ``warm_prefix`` re-checks everything atomically,
	so this read only avoids pointless jobs and is never the guard. Best-effort,
	never raises.

	There is deliberately no timer and no login hook behind this any more: a
	trigger uncorrelated with an imminent first message spends the customer's
	quota for nothing (see the module docstring).
	"""
	try:
		if not _prewarm_enabled():
			return
		if _warm_slot_held(frappe.cache(), _warm_cooldown_key()):
			return
		frappe.enqueue("jarvis.chat.prewarm.warm_prefix", queue="short")
	except Exception:
		frappe.logger("jarvis.chat.prewarm").debug("enqueue_warm_if_due skipped", exc_info=True)
