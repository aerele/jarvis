"""Clear the openclaw session model pins Jarvis wrote on the customer's behalf.

THE POISON. openclaw stores a per-session model override. From its 2026.6.8
bundle, ``resolveEffectiveModelFallbacks``::

    if (!params.hasSessionModelOverride) return agentFallbacksOverride;
    if (!(params.modelOverrideSource === "auto" || ...)) return [];

so ANY pin that is not flagged automatic zeroes the failover candidate chain for
that conversation, permanently. The bench pinned sessions on unpinned "Auto"
turns too (``turn_handler._session_model_for`` names the pool primary so openclaw
does not reject the ``jarvis-pool`` placeholder), and every such patch landed as
``modelOverrideSource: "user"`` - ``sessions.patch`` hardcodes that source and
its param schema is ``additionalProperties: false``, so there is no way to ask
for "auto". Fixing the turn path does NOT heal the sessions already poisoned:
the override lives in the container's session store and survives restarts,
re-renders and re-provisions.

WHO WROTE A PIN. Inside the container the two are indistinguishable: a customer
picking Gemini and the bench patching the pool primary produce the byte-identical
entry, and the gateway does not even put ``modelOverrideSource`` on the wire. The
disambiguating fact lives HERE, on the bench: ``Jarvis Conversation.model_override``
is the model the customer actually chose, and empty means Auto. A pinned session
whose conversation says Auto is therefore ours, by construction. That is why this
sweep is a bench maintenance command and not a fleet-agent endpoint - the fleet
plane can see the pins but cannot attribute them.

REMEDY, PER CLASS.

  * conversation on Auto      -> CLEAR. We wrote it; the customer asked for the
                                 default and its fallback chain.
  * conversation pins the same
    model as the session       -> KEEP. A deliberate pick, and openclaw disabling
                                 failover for it is the product behaviour we
                                 want: someone who explicitly chooses one vendor
                                 should see that vendor's error, not a silent
                                 switch to another one mid-conversation.
  * conversation pins a
    DIFFERENT model            -> REPORT. Bench and container disagree; the next
                                 turn re-patches the session from the row, so
                                 clearing here would only fight it.
  * openclaw's own
    agent:<id>:main[:heartbeat] -> CLEAR. The built-in poller resumes it on every
                                 tick, so a pin there fails forever, and it is
                                 never a customer pick.
  * a bench throwaway
    (title / prewarm / polish) -> SKIP. Single-use and reaped within the hour by
                                 session_lifecycle; touching it would only bump
                                 the updatedAt that reaper grades on.
  * anything else with no
    conversation               -> REPORT, never touch. Unattributable.

WHAT "PINNED" MEANS HERE, HONESTLY. A sessions.list row carries the RESOLVED
model, never the raw override, and the gateway puts no override field on the wire
at all - so "resolves to something other than the agent default" is the only
signal available, and it is exactly the one openclaw's own control UI uses. It
cannot separate a live override from a STALE RUNTIME RECORD: a session that once
ran under an earlier render keeps that render's provider in entry.model long
after the override is gone. Measured on jarvis-pool-bf4097 (2026-07-29): 6
conversation sessions resolved away from the default, of which 3 held a real
override and 3 only the stale record.

That is acceptable because the clear is an ASSERTION, not a repair, and it is
idempotent. On a session with a real override it deletes the override; on one
with only a stale record it deletes that record (which names a provider the
container no longer has) and nothing else changes. The cost of a false positive
is a telemetry reset - entry.model / contextTokens recomputed on the next turn -
never a behaviour change. Excluding the throwaways keeps that cost off the only
population where a bumped updatedAt would matter.

Clearing goes through ``sessions.patch {"model": null}`` rather than an edit of
``sessions.json`` on disk: the gateway keeps the store in memory and rewrites it
on every session update, so a host-side edit either loses the race or needs the
container stopped. The patch response echoes the resulting store entry, so every
clear is verified rather than trusted. See ``OpenclawSession.clear_session_model``.

The listing and the patch are seconds apart, and both facts behind a CLEAR can
move in that window, so each one is rechecked at patch time: the conversation's
model_override is re-read (a customer who picked a model in between keeps it),
and the returned entry's sessionId must be the one that was planned against (the
lifecycle sweep can free a session mid-run, and patching a freed key makes
openclaw mint a ghost entry instead of failing).

DRY RUN IS THE DEFAULT. ``run()`` only reports; ``run(apply=True)`` clears, up to
``MAX_CLEAR`` pins per invocation::

    bench --site <site> execute jarvis.chat.session_pin_sweep.run
    bench --site <site> execute jarvis.chat.session_pin_sweep.run --kwargs "{'apply': True}"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import frappe

logger = logging.getLogger(__name__)

CONV = "Jarvis Conversation"

# What the sweep decided to do with one pinned session.
CLEAR = "clear"
KEEP = "keep"
REPORT = "report"
SKIP = "skip"

# Ceiling on pins cleared per apply run. A misclassification cannot unpin a whole
# tenant in one go; the operator re-runs to drain a genuine backlog.
MAX_CLEAR = 200

# sessions.list paging. MAX_PAGES bounds a gateway that keeps claiming hasMore.
PAGE_LIMIT = 100
MAX_PAGES = 50

# Labels of the throwaway sessions the bench mints for its own turns (mirrors
# session_lifecycle._THROWAWAY_LABEL_PREFIXES). A real conversation session is
# always "jarvis-chat-<user>-<ms>", so these prefixes can never collide with one.
_BENCH_LABEL_PREFIXES = ("jarvis-prewarm-", "jarvis-title-", "jarvis-polish-")

# openclaw's own per-agent session: "agent:<id>:main" and its ":heartbeat"
# sibling. The heartbeat resumes on every tick, so a pin there fails forever.
_AGENT_MAIN_PREFIX = "agent:"


@dataclass(frozen=True)
class PinPlan:
	"""One pinned session and what the sweep would do about it."""

	key: str
	verb: str
	reason: str
	pinned: str
	label: str = ""
	conversation: str = ""
	session_id: str = ""

	def as_dict(self) -> dict:
		return {
			"key": self.key,
			"verb": self.verb,
			"reason": self.reason,
			"pinned": self.pinned,
			"label": self.label,
			"conversation": self.conversation,
			"session_id": self.session_id,
		}


@dataclass
class SweepSummary:
	"""Counters + the full plan, returned to the caller and logged."""

	apply: bool = False
	default_model: str = ""
	scanned: int = 0
	cleared: int = 0
	capped: int = 0
	raced: int = 0
	errors: int = 0
	aborted: str = ""
	plan: list[PinPlan] = field(default_factory=list)

	def counts(self) -> dict:
		out = {verb: 0 for verb in (CLEAR, KEEP, REPORT, SKIP)}
		for item in self.plan:
			out[item.verb] = out.get(item.verb, 0) + 1
		return out

	def as_dict(self) -> dict:
		return {
			"apply": self.apply,
			"default_model": self.default_model,
			"scanned": self.scanned,
			"pinned": len(self.plan),
			"cleared": self.cleared,
			"capped": self.capped,
			"raced": self.raced,
			"errors": self.errors,
			"aborted": self.aborted,
			"counts": self.counts(),
			"plan": [item.as_dict() for item in self.plan],
		}


def run(apply: bool = False, max_clear: int = MAX_CLEAR) -> dict:
	"""Sweep this tenant's gateway for poisoned model pins.

	Reports only unless ``apply`` is true. Uses a dedicated connection, never the
	turn pool - a maintenance sweep must not contend with live chat. Returns the
	summary dict (also logged) so a ``bench execute`` run shows its work."""
	from jarvis import selfhost

	if selfhost.is_self_hosted():
		# A self-hosted tenant is reached over HTTP bearer auth with no device
		# pairing, so the WS client this sweep uses does not apply there.
		return SweepSummary(apply=bool(apply), aborted="self-hosted").as_dict()

	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return SweepSummary(apply=bool(apply), aborted="no agent_url").as_dict()

	from jarvis.chat.openclaw_client import OpenclawSession

	try:
		sess = OpenclawSession.connect(gateway_url)
	except Exception:
		frappe.log_error(title="session_pin_sweep: connect failed", message=frappe.get_traceback())
		return SweepSummary(apply=bool(apply), aborted="connect failed").as_dict()
	try:
		summary = SessionPinSweep(sess, apply=bool(apply), max_clear=max_clear).run()
	finally:
		try:
			sess.close()
		except Exception:
			logger.debug("session_pin_sweep: close failed", exc_info=True)
	logger.info("session_pin_sweep: %s", summary.as_dict())
	return summary.as_dict()


class SessionPinSweep:
	"""Classifies every pinned gateway session against the bench's own record of
	what the customer chose, and (only when applying) clears the pins we wrote."""

	def __init__(self, sess, *, apply: bool = False, max_clear: int = MAX_CLEAR):
		self._sess = sess
		self._apply = apply
		self._max_clear = max(0, int(max_clear))

	def run(self) -> SweepSummary:
		summary = SweepSummary(apply=self._apply)
		rows, default_ref = self._read_rows()
		if not default_ref:
			# Without the agent's default, EVERY session looks pinned and a blind
			# apply would unpin the whole tenant. Refuse instead.
			summary.aborted = "gateway reported no default model"
			return summary
		summary.default_model = default_ref
		summary.scanned = len(rows)
		owners = self._conversations_by_session_key()
		for row in rows:
			plan = self._classify(row, default_ref, owners)
			if plan is not None:
				summary.plan.append(plan)
		if self._apply:
			self._clear(summary)
		return summary

	def _read_rows(self) -> tuple[list[dict], str]:
		"""Every sessions.list row plus the agent's default "provider/model".

		A page that fails mid-way returns what it has: the sweep then reports on
		fewer sessions rather than none, and re-running picks up the rest.

		Paging is plain offset against a LIVE store, so a session created between
		two fetches can shift a row onto a page it was already on. Rows are keyed
		by session key (last one wins) so a duplicate cannot burn two slots of the
		clear budget for one session."""
		by_key: dict[str, dict] = {}
		default_ref = ""
		offset = 0
		for _ in range(MAX_PAGES):
			try:
				page = self._sess.list_sessions_page(limit=PAGE_LIMIT, offset=offset)
			except Exception:
				frappe.log_error(
					title="session_pin_sweep: sessions.list failed",
					message=f"offset={offset}\n{frappe.get_traceback()}",
				)
				break
			default_ref = default_ref or _model_ref(page.get("defaults") or {})
			for row in page.get("sessions") or []:
				if isinstance(row, dict) and row.get("key"):
					by_key[row["key"]] = row
			next_offset = page.get("nextOffset")
			if not page.get("hasMore") or not isinstance(next_offset, int) or next_offset <= offset:
				break
			offset = next_offset
		return list(by_key.values()), default_ref

	def _classify(self, row: dict, default_ref: str, owners: dict) -> PinPlan | None:
		"""The plan for one session row, or None when it carries no pin.

		A row reports the RESOLVED model, so "pinned" means "resolves to
		something other than the agent default". A session pinned to exactly the
		default is invisible here - and unreachable through sessions.patch, which
		clears such a selection instead of storing it."""
		key = row.get("key") or ""
		pinned = _model_ref(row)
		if not key or not pinned or pinned == default_ref:
			return None
		label = row.get("label") or ""
		sid = row.get("sessionId") or ""

		def plan(verb: str, reason: str, conversation: str = "") -> PinPlan:
			return PinPlan(key, verb, reason, pinned, label, conversation, sid)

		if row.get("hasActiveRun"):
			return plan(SKIP, "a run is in flight")
		if not sid:
			# Patching an entry with no sessionId makes openclaw mint one and drop
			# the label. Nothing here is worth that.
			return plan(SKIP, "store entry has no sessionId")

		conv = owners.get(key)
		if conv is None:
			if _is_agent_main_key(key):
				return plan(CLEAR, "openclaw's own main session, never a customer pick")
			if label.startswith(_BENCH_LABEL_PREFIXES):
				return plan(SKIP, "bench throwaway; session_lifecycle reaps it")
			return plan(REPORT, "no conversation owns this session")

		chosen = (conv.get("model_override") or "").strip()
		if not chosen:
			return plan(CLEAR, "conversation is on Auto", conv["name"])
		if chosen.casefold() == (row.get("model") or "").strip().casefold():
			return plan(KEEP, "the customer picked this model", conv["name"])
		return plan(REPORT, f"conversation pins {chosen!r}; the next turn re-patches it", conv["name"])

	def _clear(self, summary: SweepSummary) -> None:
		"""Issue sessions.patch {"model": null} for every CLEAR plan, up to the
		cap. One failure never stops the sweep."""
		for item in summary.plan:
			if item.verb != CLEAR:
				continue
			if summary.cleared >= self._max_clear:
				summary.capped += 1
				continue
			if self._pick_arrived_since_planning(item):
				summary.raced += 1
				continue
			try:
				entry = self._sess.clear_session_model(item.key) or {}
			except Exception:
				frappe.log_error(
					title="session_pin_sweep: clear failed",
					message=f"session_key={item.key}\n{frappe.get_traceback()}",
				)
				summary.errors += 1
				continue
			if not self._verify(item, entry, summary):
				continue
			summary.cleared += 1
			logger.info("session_pin_sweep: cleared %s (was %s)", item.key, item.pinned)

	def _pick_arrived_since_planning(self, item: PinPlan) -> bool:
		"""True when the conversation gained an explicit model since the plan was
		built. The listing and the patch are seconds apart and a customer can
		choose a model in between; reverting a fresh pick would be worse than
		leaving one poisoned session for the next run."""
		if not item.conversation:
			return False
		chosen = frappe.db.get_value(CONV, item.conversation, "model_override")
		return bool((chosen or "").strip())

	def _verify(self, item: PinPlan, entry: dict, summary: SweepSummary) -> bool:
		"""The patch echoes the resulting store entry, so check it instead of
		trusting the ack. Two things can be wrong:

		- the override survived, meaning openclaw took a branch we did not expect;
		- the entry carries a DIFFERENT sessionId, meaning the session was freed
		  between the listing and the patch and openclaw minted a fresh entry
		  under that key rather than patching the one we planned against. That
		  ghost has no override, so only the id comparison catches it. It is an
		  unreferenced session, which the session_lifecycle orphan sweep reaps.

		Either way: count an error, do not count a fix."""
		if entry.get("modelOverride") or entry.get("modelOverrideSource"):
			frappe.log_error(
				title="session_pin_sweep: override survived the patch",
				message=f"session_key={item.key}\nentry={entry}",
			)
			summary.errors += 1
			return False
		got = entry.get("sessionId")
		if item.session_id and got and got != item.session_id:
			frappe.log_error(
				title="session_pin_sweep: patched a session that had been replaced",
				message=f"session_key={item.key}\nplanned={item.session_id}\ngot={got}",
			)
			summary.errors += 1
			return False
		return True

	def _conversations_by_session_key(self) -> dict[str, dict]:
		"""session_key -> {name, model_override} for every conversation that
		holds one. The join key is exact: prepare.py persists the session_key it
		created onto the row before the first turn can run."""
		rows = frappe.get_all(
			CONV,
			fields=["name", "session_key", "model_override"],
			filters={"session_key": ["is", "set"]},
			limit_page_length=0,
		)
		return {r["session_key"]: r for r in rows if r.get("session_key")}


def _model_ref(row: dict) -> str:
	"""``"provider/model"`` for a sessions.list row or its defaults block, or ""
	when the gateway resolved neither. Comparison is case-folded; openclaw ids
	are lowercase but nothing guarantees it."""
	provider = (row.get("modelProvider") or "").strip().casefold()
	model = (row.get("model") or "").strip().casefold()
	if not model:
		return ""
	return f"{provider}/{model}" if provider else model


def _is_agent_main_key(key: str) -> bool:
	"""True for openclaw's own ``agent:<id>:main`` session and its
	``:heartbeat`` sibling - the built-in poller, never a customer chat."""
	if not key.startswith(_AGENT_MAIN_PREFIX):
		return False
	rest = key[len(_AGENT_MAIN_PREFIX) :]
	agent_id, sep, tail = rest.partition(":")
	if not agent_id or not sep:
		return False
	return tail == "main" or tail.startswith("main:")
