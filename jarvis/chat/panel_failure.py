"""A failed save on the draft panel (R2-9, round 2).

``apply_action`` returns the write's failure envelope; this decides what happens next:

- FIXABLE (a missing or invalid value): the panel stays open with the problem fields
  marked and the user fixes it there. The assistant is told only that, once per draft
  (repeated failing saves of the same draft send nothing more), and proposes no card.
  The phone's card cannot be edited (``editable: 0``), so it sends nothing: its error
  tells the person to ask the assistant.
- RETRY_LATER (a deadlock or lock timeout): nothing changed and the values are still
  right, so the panel stays open for another try and nothing is sent.
- NOT FIXABLE (a business rule, a permission, anything unrecognised): the panel closes
  (``closed: true``) and the assistant explains what blocked it and suggests what to do,
  as after a failed confirmation card.
- PARTIAL (something committed before the failure, ``jarvis._commit_watch``): never
  "nothing was saved"; the panel closes and the assistant asks the user to check.

The write-risk guard's refusals keep their own panel reply (``apply_action``)."""

from __future__ import annotations

import frappe

from jarvis import _failure_kind
from jarvis.chat import api as chat_api

# One "fixing it in the panel" continuation per draft. The key outlives any panel left
# open in a working day; a new draft (a new message) has its own key.
FIXING_KEY = "jarvis:panel_fixing:{conversation}:{draft}"
FIXING_TTL_S = 12 * 3600
PARTIAL_HINT = "Part of this may already be saved. Check the record before trying again."


class PanelFailure:
	"""``fixable_here``: the panel can fix what failed (``actions_api._panel_can_fix``:
	it marked the fields at fault, or the failure is fixable and tied to nothing the
	panel cannot edit). Anything else closes the panel and the assistant explains. The
	failure's own kind (J2a, what the model is told on other paths) is unchanged. The
	phone's read-only card (``editable: 0``) keeps a fixable failure on the card."""

	def __init__(
		self,
		conversation: str,
		tool: str,
		args: dict,
		envelope: dict,
		*,
		partial: bool = False,
		fixable_here: bool = True,
	):
		self.conversation = conversation
		self.tool = tool
		self.args = args
		self.envelope = envelope
		self.partial = partial
		self.fixable_here = fixable_here

	def respond(self, *, draft, editable: int) -> dict:
		"""The panel's reply, after sending the assistant whatever R2-9 says it is told."""
		kind = self.envelope["error"].get("kind")
		if self.partial:
			mark_partial(self.envelope)
			return self._close(chat_api.OUTCOME_PARTIAL)
		if kind == _failure_kind.RETRY_LATER:
			return self.envelope
		if self.fixable_here if editable else kind == _failure_kind.FIXABLE:
			if editable and self._first_failure_of(draft):
				told, _cont = self._continue(chat_api.OUTCOME_FIXING_IN_PANEL)
				if not told:
					self._release(draft)  # nothing will run: the next failing save may try
			return self.envelope
		return self._close(chat_api.OUTCOME_NOT_FIXABLE)

	def _close(self, outcome: str) -> dict:
		"""Close the panel. ``assistant_told`` says whether the assistant's explanation
		is coming, so the person is never left with neither panel nor reply. A hint to
		correct a value no longer fits a closed panel (the partial hint stays)."""
		self.envelope["closed"] = True
		if outcome != chat_api.OUTCOME_PARTIAL and self._correcting_hint():
			self.envelope["error"].pop("hint", None)
		told, cont = self._continue(outcome)
		self.envelope["assistant_told"] = told
		# The panel closed: the continuation's queued chip stands in for it.
		if cont and cont.get("queued"):
			self.envelope.update(
				queued=True,
				queued_position=cont.get("queued_position"),
				run_id=cont.get("run_id"),
				message_id=cont.get("message_id"),
			)
		return self.envelope

	def _correcting_hint(self) -> bool:
		from jarvis import api

		hint = self.envelope["error"].get("hint") or ""
		return hint == api._FIXABLE_HINT or "marked field" in hint or hint.startswith("Fill it in")

	def _continue(self, outcome: str) -> tuple[bool, dict | None]:
		"""The hidden continuation turn, after the write's rollback (best-effort).
		``(told, result)``. Told only when the turn will run: its Turn row is durable
		(the pump's watchdog starts a queued Turn later, even when starting it failed on
		a full queue). A seed written without its Turn would never run: it is removed,
		so a retry never leaves two."""
		from jarvis.chat.actions_api import _confirm_receipt_text

		receipt = _confirm_receipt_text({"tool": self.tool, "args": self.args}, self.envelope)
		seeds: list[str] = []
		try:
			cont = chat_api.enqueue_continuation(
				self.conversation, receipt, outcome=outcome, on_seed=seeds.append
			)
		except Exception:
			frappe.log_error(title="apply_action continuation failed", message=frappe.get_traceback())
			if seeds and _turn_is_durable(seeds[0]):
				return True, None
			if seeds:
				chat_api._delete_enqueue_seed(seeds[0])
			return False, None
		return True, cont

	def _first_failure_of(self, draft) -> bool:
		"""True once per draft (``SET NX``). Without a draft id there is no way to tell
		a repeat from a new draft, and a Redis outage leaves the same doubt: both send
		nothing, since a missed acknowledgement is better than one per click."""
		key = self._key(draft)
		if not key:
			return False
		try:
			cache = frappe.cache()
			return bool(cache.set(cache.make_key(key), 1, ex=FIXING_TTL_S, nx=True))
		except Exception:
			frappe.logger("jarvis.panel_failure").warning("fixing-in-panel claim failed for %s", key)
			return False

	def _release(self, draft) -> None:
		try:
			cache = frappe.cache()
			cache.delete(cache.make_key(self._key(draft)))
		except Exception:
			pass

	def _key(self, draft) -> str:
		draft = str(draft or "").strip()[:140]
		return FIXING_KEY.format(conversation=self.conversation, draft=draft) if draft else ""


def mark_partial(envelope: dict) -> dict:
	"""Something was saved before the failure: the envelope says ``partial`` and never
	"nothing was saved" (the busy message says so) nor "correct the value and try
	again" (a retry could save the first part twice)."""
	from jarvis import api

	envelope["outcome"] = "partial"
	err = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
	if err.get("message") == api._BUSY_MESSAGE:
		err["message"] = "The system was busy (a lock or timeout) after part of this was saved."
	err["hint"] = PARTIAL_HINT
	envelope["error"] = err
	return envelope


def _turn_is_durable(seed: str) -> bool:
	"""A committed Turn row for this seed (the turn machine writes it before starting
	the turn). The legacy path has no Turn rows: a failed job enqueue there ran nothing."""
	from jarvis.chat import admission

	try:
		return admission.turn_machine_enabled() and bool(
			frappe.db.exists("Jarvis Chat Turn", {"seed_message": seed})
		)
	except Exception:
		return False
