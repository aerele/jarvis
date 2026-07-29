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
  * no conversation, bench
    label (title/prewarm/
    polish) or the agent's own
    main/heartbeat session     -> CLEAR. Never a customer pick.
  * no conversation, unknown
    label                      -> REPORT, never touch. Unattributable.

Clearing goes through ``sessions.patch {"model": null}`` rather than an edit of
``sessions.json`` on disk: the gateway keeps the store in memory and rewrites it
on every session update, so a host-side edit either loses the race or needs the
container stopped. See ``OpenclawSession.clear_session_model``.

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
_AGENT_MAIN_SEGMENT = ":main"


@dataclass(frozen=True)
class PinPlan:
	"""One pinned session and what the sweep would do about it."""

	key: str
	verb: str
	reason: str
	pinned: str
	label: str = ""
	conversation: str = ""

	def as_dict(self) -> dict:
		return {
			"key": self.key,
			"verb": self.verb,
			"reason": self.reason,
			"pinned": self.pinned,
			"label": self.label,
			"conversation": self.conversation,
		}


@dataclass
class SweepSummary:
	"""Counters + the full plan, returned to the caller and logged."""

	apply: bool = False
	default_model: str = ""
	scanned: int = 0
	cleared: int = 0
	capped: int = 0
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
		fewer sessions rather than none, and re-running picks up the rest."""
		rows: list[dict] = []
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
			batch = page.get("sessions") or []
			rows.extend(r for r in batch if isinstance(r, dict))
			next_offset = page.get("nextOffset")
			if not page.get("hasMore") or not isinstance(next_offset, int) or next_offset <= offset:
				break
			offset = next_offset
		return rows, default_ref

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
		if row.get("hasActiveRun"):
			return PinPlan(key, SKIP, "a run is in flight", pinned, label)
		if not row.get("sessionId"):
			# Patching an entry with no sessionId makes openclaw mint one and drop
			# the label. Nothing here is worth that.
			return PinPlan(key, SKIP, "store entry has no sessionId", pinned, label)

		conv = owners.get(key)
		if conv is None:
			if label.startswith(_BENCH_LABEL_PREFIXES) or _is_agent_main_key(key):
				return PinPlan(key, CLEAR, "bench-owned session, never a customer pick", pinned, label)
			return PinPlan(key, REPORT, "no conversation owns this session", pinned, label)

		chosen = (conv.get("model_override") or "").strip()
		if not chosen:
			return PinPlan(key, CLEAR, "conversation is on Auto", pinned, label, conv["name"])
		if chosen.casefold() == (row.get("model") or "").strip().casefold():
			return PinPlan(key, KEEP, "the customer picked this model", pinned, label, conv["name"])
		return PinPlan(
			key,
			REPORT,
			f"conversation pins {chosen!r}; the next turn re-patches it",
			pinned,
			label,
			conv["name"],
		)

	def _clear(self, summary: SweepSummary) -> None:
		"""Issue sessions.patch {"model": null} for every CLEAR plan, up to the
		cap. One failure never stops the sweep."""
		for item in summary.plan:
			if item.verb != CLEAR:
				continue
			if summary.cleared >= self._max_clear:
				summary.capped += 1
				continue
			try:
				self._sess.clear_session_model(item.key)
			except Exception:
				frappe.log_error(
					title="session_pin_sweep: clear failed",
					message=f"session_key={item.key}\n{frappe.get_traceback()}",
				)
				summary.errors += 1
				continue
			summary.cleared += 1
			logger.info("session_pin_sweep: cleared %s (was %s)", item.key, item.pinned)

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
