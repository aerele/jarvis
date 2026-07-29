"""LLM polish for learned-pattern skill drafts (plan section 5.5, Phase 2).

Phase 1 drafts are deterministic template text (skill_drafts.py) - reviewable,
free, offline-safe. This module is the OPTIONAL Phase-2 rewrite: one silent
tenant-container gateway turn (the title.py / File Box throwaway-turn
precedent) that rewords a single proposal bullet for clarity. It is gated by
the ``pattern_llm_polish`` Settings flag (default off) and every failure path
falls back to the template text - polish can only ever improve wording, never
block the board.

Contract (plan 5.5 Phase-2 paragraph):

* S1 IDENTITY - the gateway turn is attributed to ``acting_user`` (the
  reviewing System Manager), NEVER Administrator. The endpoint enforces this;
  :func:`polish_skill_draft` re-asserts it and raises ``PermissionError``
  loudly (an identity violation here is a bug or an attack, not a fallback
  case). Everything else returns ``{ok: False, reason}`` instead of raising.
* PROMPT - a fixed template carrying ONLY the pattern's templated skill_draft,
  its pattern_statement and plain-English aggregates (support, confidence,
  band, exceptions). No raw rows ever; no exemplar party names in v1 (keep the
  prompt A-class-clean by construction).
* BUDGET - ``MAX_POLISH_TURNS_PER_MONTH`` per site via a month-stamped
  ``frappe.cache().incr`` counter. A cache flush resets the month's count -
  the accepted undercount (the cap is a cost guard, not a security boundary);
  Redis trouble fails open like ``_plugin_auth``'s rate limit (SM-triggered,
  low volume). Over budget the caller keeps the template text.
* OUTPUT VALIDATION - the reply must still be one ``- `` bullet, <= 400 chars
  (compiler.MAX_BULLET), pass the injection scan, and contain the original
  machine-measured Evidence tail VERBATIM; anything else is rejected and the
  template text stands.
* FAILURE - any gateway error (unreachable, timeout, empty reply) returns
  ``{ok: False, reason: "gateway error"}``; never raises to the caller.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import today

from jarvis.learning.sanitizer import scan_instruction_injection

JLP = "Jarvis Learned Pattern"
SETTINGS = "Jarvis Settings"

# Cost guard (plan 5.5): silent polish turns per site per calendar month.
MAX_POLISH_TURNS_PER_MONTH = 150

# Mirrors compiler.MAX_BULLET - a polished draft that could not compile
# unclipped is not an improvement.
MAX_POLISHED_CHARS = 400

# TTL on the monthly counter key: past month-end so a slow month rolls off on
# its own, never long enough to leak counters forever.
_BUDGET_KEY_TTL_S = 45 * 86400

# Fixed prompt (File Box zero-question discipline: one directed instruction,
# no conversation, nothing the model can be asked back). Interpolated values
# are the pattern's OWN template output + numeric aggregates only.
_POLISH_PROMPT = (
	"Rewrite the skill bullet below for clarity and brevity.\n"
	"Rules:\n"
	"- Return ONLY the rewritten bullet - no preamble, no quotes, no markdown "
	"fences, and do not call any tools.\n"
	"- Keep it a single bullet line starting with '- '.\n"
	"- Keep this Evidence text VERBATIM, character for character, at the end "
	"of the bullet: {evidence}\n"
	"- Do not add any new claim, number, or name that is not already there.\n"
	"- Use ASCII characters only.\n"
	"- Keep the whole bullet under {max_chars} characters.\n\n"
	"What the pattern says: {statement}\n"
	"How well it holds: {aggregates}\n\n"
	"Bullet to rewrite:\n{draft}"
)


def polish_skill_draft(pattern_name: str, acting_user: str) -> dict:
	"""One LLM polish pass over ``pattern_name``'s skill_draft.

	Returns ``{ok: bool, text: str | None, reason: str}``. ``ok=True`` means
	``text`` passed every output check and may replace the stored draft;
	``ok=False`` means the caller keeps the deterministic template text
	(``reason`` says why). Only the S1 identity assertion raises."""
	_assert_polish_identity(acting_user)

	row = frappe.db.get_value(
		JLP,
		pattern_name,
		[
			"name",
			"skill_draft",
			"pattern_statement",
			"support_n",
			"confidence_pct",
			"strength_band",
			"exception_n",
		],
		as_dict=True,
	)
	if not row:
		return _not_ok("unknown pattern")
	draft = (row.skill_draft or "").strip()
	if not draft:
		return _not_ok("pattern has no skill draft")
	evidence = _evidence_tail(draft)
	if not evidence:
		# Machine drafts always carry the Evidence sentence (skill_drafts
		# grammar); without it the verbatim check cannot hold - keep template.
		return _not_ok("draft has no Evidence sentence")

	if not _consume_budget():
		return _not_ok("monthly polish budget exhausted")

	prompt = _POLISH_PROMPT.format(
		evidence=evidence,
		max_chars=MAX_POLISHED_CHARS,
		statement=(row.pattern_statement or "").strip(),
		aggregates=_aggregates_line(row),
		draft=draft,
	)

	try:
		raw = _run_gateway_turn(prompt)
	except Exception:
		# _run_gateway_turn already contains the WS failure paths; this is the
		# belt-and-suspenders for anything around it (settings read, resolver).
		frappe.log_error(
			title="pattern polish: gateway turn failed",
			message=frappe.get_traceback(),
		)
		raw = ""
	text = (raw or "").strip()
	if not text:
		return _not_ok("gateway error")

	problem = _validate_output(text, evidence)
	if problem:
		# The detail goes to the log only; callers get the stable reason.
		frappe.log_error(
			title="pattern polish: output rejected",
			message=f"{pattern_name}: {problem}\n\nreply:\n{text[:1000]}",
		)
		return _not_ok("rejected output")

	return {"ok": True, "text": text, "reason": ""}


# --------------------------------------------------------------------------- #
# S1 identity
# --------------------------------------------------------------------------- #
def _assert_polish_identity(acting_user: str) -> None:
	"""The polish turn must be attributed to the reviewing SM in person:
	a real, enabled System User holding System Manager, who IS the current
	session user. Administrator/Guest and service identities are refused
	(plan 5.5 / S1 - nobody reviews as Administrator on managed sites)."""
	user = (acting_user or "").strip()
	if not user or user in ("Administrator", "Guest"):
		frappe.throw(
			_("LLM polish must run as the reviewing System Manager, not {0}.").format(
				user or "an empty user"
			),
			frappe.PermissionError,
		)
	if frappe.session.user != user:
		frappe.throw(
			_("LLM polish must run as the calling session user."),
			frappe.PermissionError,
		)
	row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	if not row or not int(row.enabled or 0) or (row.user_type or "") != "System User":
		frappe.throw(
			_("LLM polish requires an enabled desk (System) user."),
			frappe.PermissionError,
		)
	if "System Manager" not in set(frappe.get_roles(user)):
		frappe.throw(
			_("LLM polish requires the System Manager role."),
			frappe.PermissionError,
		)


# --------------------------------------------------------------------------- #
# monthly budget
# --------------------------------------------------------------------------- #
def _month_key() -> str:
	"""Site-scoped month-stamped counter key. frappe.cache().incr operates on
	the RAW key (RedisWrapper only prefixes its *_value helpers), so the site
	must be baked in or every site on the bench would share one budget."""
	return f"jarvis:pattern_polish_turns:{frappe.local.site}:{today()[:7]}"


def _consume_budget() -> bool:
	"""Atomically take one turn from this month's budget. True = proceed.

	incr-then-check: two concurrent calls can never both read 149 and pass.
	Redis trouble fails OPEN (the _plugin_auth rate-limit choice): polish is
	SM-triggered and low volume, and a dead Redis usually means the gateway
	turn fails anyway. A cache flush restarts the month's count - accepted
	undercount, documented in the module docstring."""
	cache = frappe.cache()
	key = _month_key()
	try:
		count = cache.incr(key)
	except Exception:
		return True
	if count == 1:
		try:
			cache.expire(key, _BUDGET_KEY_TTL_S)
		except Exception:
			pass
	return count <= MAX_POLISH_TURNS_PER_MONTH


# --------------------------------------------------------------------------- #
# prompt pieces
# --------------------------------------------------------------------------- #
def _evidence_tail(draft: str) -> str:
	"""The machine-measured tail of the draft, from ``Evidence:`` to the end
	(includes the optional exceptions clause). This exact substring must
	survive the rewrite verbatim - it is measured text, not prose."""
	idx = draft.find("Evidence:")
	if idx < 0:
		return ""
	return draft[idx:].strip()


def _aggregates_line(row) -> str:
	"""Plain-English aggregates for the prompt (no statistical jargon, no raw
	rows, no party names - A-class-clean by construction)."""
	conf = float(row.confidence_pct or 0)
	support = int(row.support_n or 0)
	exc = int(row.exception_n or 0)
	return (
		f"holds in {conf:g}% of {support} observed documents, "
		f"{row.strength_band or 'Low'} strength, "
		f"{exc} known exception{'s' if exc != 1 else ''}"
	)


# --------------------------------------------------------------------------- #
# gateway turn (title.py precedent; tests patch this boundary)
# --------------------------------------------------------------------------- #
def _run_gateway_turn(prompt: str) -> str:
	"""One silent throwaway agent turn on its own session_key - it never
	touches a visible conversation. Returns the assistant text, or '' on any
	failure (unreachable gateway, timeout, agent error, no gateway_url)."""
	from jarvis.chat import openclaw_session_pool
	from jarvis.chat.session_lifecycle import reclaim_throwaway_session
	from jarvis.chat.turn_handler import _resolve_model_and_provider

	settings = frappe.get_single(SETTINGS)
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return ""
	# No conversation exists for a polish turn; an empty model_override lets
	# the resolver fall through to the site's configured model/provider.
	model, provider = _resolve_model_and_provider(frappe._dict(model_override=None))

	# Session labels must be unique per call (openclaw rejects a reused label).
	label = f"jarvis-polish-{frappe.generate_hash(length=10)}"
	text = ""
	try:
		with openclaw_session_pool.checkout(gateway_url) as sess:
			skey = sess.create_session(label=label)
			try:
				for ev in sess.stream_agent_turn(
					skey,
					prompt,
					f"polish:{skey}",
					model=model,
					provider=provider,
				):
					if ev.get("kind") == "assistant" and ev.get("text"):
						text = ev["text"]
			finally:
				# Reclaim the throwaway on the SAME pooled connection, turn
				# succeeded or not: otherwise every polish turn leaks a session
				# that only the budget-capped orphan sweep could reclaim.
				#
				# NOT a bare sessions.delete (issue #525): the turn is NOT
				# necessarily finished here. stream_agent_turn returns on the
				# lifecycle-end frame while openclaw is still finalising the
				# session file, and raises on every error path with the run still
				# alive server side, so deleting here raced the run that owns the
				# session. reclaim_throwaway_session waits for the gateway to
				# report no active run and otherwise defers to the orphan sweep,
				# which collects jarvis-polish-* as a backstop. Failure is
				# swallowed in there - `text` is already captured.
				reclaim_throwaway_session(sess, skey, logger_name="jarvis.learning.polish")
	except Exception:
		frappe.log_error(
			title="pattern polish: gateway turn failed",
			message=frappe.get_traceback(),
		)
		return ""
	return text


# --------------------------------------------------------------------------- #
# output validation
# --------------------------------------------------------------------------- #
def _validate_output(text: str, evidence: str) -> str:
	"""'' when ``text`` is acceptable, else a short internal reason. The reply
	must still be exactly the artifact the compiler expects: one ``- `` bullet
	within the compile cap, injection-clean, with the measured Evidence tail
	intact. A model that wrapped its reply in a code fence fails the injection
	scan - correct: fall back rather than unwrap."""
	if not text.startswith("- "):
		return "not a single '- ' bullet"
	if len(text) > MAX_POLISHED_CHARS:
		return f"over {MAX_POLISHED_CHARS} chars"
	if scan_instruction_injection(text):
		return "failed instruction-injection scan"
	if evidence not in text:
		return "Evidence sentence altered or dropped"
	return ""


def _not_ok(reason: str) -> dict:
	return {"ok": False, "text": None, "reason": reason}
