"""Deterministic Run + Findings persistence for the delegate agents (O2).

``record_delegate_run`` writes ONE ``Jarvis Agent Run`` + N ``Jarvis Agent
Finding`` rows from a delegate evaluator's VALIDATED findings. It is DETERMINISTIC
server code, NOT model-mediated: the delegate narrates the transcript, but the
stored, severity-tagged, deduped finding rows are the reproducibility guarantee
(same evaluator output -> same rows, re-runnable by a peer reviewer).

Findings dedupe across runs on a stable ``fingerprint`` (sha256 of
``rule_id + ref_doctype + ref_name``): a finding still ``open`` from a prior run
has its ``last_seen_run`` bumped instead of being duplicated. ``status=partial``
marks a scan that hit the turn envelope (never masquerades as ``completed``).
Auto-resolve is coverage-scoped (A16) + gated on the GL consistency watermark
(A17), scoped visibility (A12) and row under-read — never an unconditional sweep.
"""

import hashlib

import frappe
from frappe.utils import getdate

from jarvis.chat import coverage_reasons as cr
from jarvis.chat.agent_activity import log_activity
from jarvis.chat.macro_scheduler import compute_next_run

RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
DASHBOARD = "Jarvis Dashboard"
SESSION = "Jarvis Chat Session"


# --------------------------------------------------------------------------- #
# Phase 4 — saved dashboard delivery + A8 session teardown
# --------------------------------------------------------------------------- #
_SEV_LABEL = {"blocker": "Blockers", "warning": "Warnings", "note": "Notes"}


def teardown_run_session(session_key: str | None) -> None:
	"""A8 (zero-trace): delete the per-run ``Jarvis Chat Session`` row once the
	run is finalized (completed/partial/failed) or reaped.

	The session row is a BEARER CREDENTIAL — ``session_key`` + a valid device id
	resolves the run-as user (``api.py`` → impersonate) — so it must not linger
	past the run. The ``session_key`` string stays stamped on the Run for audit
	(harmless without the row); only the resolvable row is destroyed. Best-effort:
	never breaks finalization.

	TODO(A8 fleet piece): the CONTAINER-side session transcript (its jsonl —
	which embeds MB-scale chunked payloads — and its ``sessions.json`` entry) is
	fleet-owned. It is pruned via the fleet-agent's ``session_hygiene`` (gateway
	RPC when the container is live, else deferred) on the next Apply / uninstall
	or a dedicated prune verb. Do NOT prune the container side from the bench.
	"""
	key = (session_key or "").strip()
	if not key:
		return
	try:
		frappe.db.delete(SESSION, {"session_key": key})
	except Exception:
		frappe.log_error(
			title="jarvis agent: run-session teardown failed",
			message=frappe.get_traceback(),
		)


def _default_dashboard_title(run_doc, listing_title: str) -> str:
	""" "<agent title> — <period>" (A2-safe: agent name + fiscal period only)."""
	import json as _json

	period = ""
	try:
		scope = _json.loads(run_doc.get("scope_json") or "{}")
		period = str(scope.get("fiscal_year") or scope.get("to_date") or "").strip()
	except Exception:
		period = ""
	if not period:
		period = frappe.utils.today()
	base = (listing_title or "Agent").strip()
	return f"{base} — {period}"[:140]


def _esc(value) -> str:
	from frappe.utils import escape_html

	return escape_html(str(value if value is not None else ""))


# PP-2: the in-body (screenshot-safe) coverage-verdict heading. Label + colour
# per state; rendered as a first-class block inside the document body, NEVER as a
# detachable banner, so a crop/PDF keeps it.
_STATE_HEADING = {
	"evaluated_clean": ("Evaluated — clean coverage", "#065f46", "#d1fae5"),
	"partial": ("Partial coverage — not a clean result", "#92400e", "#fef3c7"),
	"not_evaluable": ("Not evaluable — required checks could not run", "#991b1b", "#fee2e2"),
	"failed": ("Run failed — no result produced", "#991b1b", "#fee2e2"),
}

# PP-2: the empty-findings sentence is state-conditional. "No exceptions were
# found" is UNREACHABLE unless result_state == evaluated_clean.
_EMPTY_SENTENCE = {
	"evaluated_clean": "No exceptions were found for this run.",
	"partial": (
		"Partial coverage — this run did not evaluate every required check; "
		"absence of findings is not a clean result."
	),
	"not_evaluable": "This run could not evaluate the required checks (see reasons below).",
	"failed": "This run failed to produce a result.",
}


def _clean_attestation_allowed(result_state: str, findings_count, *, shadow: bool) -> bool:
	"""R5-J1 TWO-CONDITION render gate for the "No exceptions were found" sentence
	(and any equivalent clean/compliant attestation).

	``run_state`` alone is a COVERAGE verdict — a completed close that surfaced
	exceptions is still ``evaluated_clean`` (``coverage_reasons.RUN_STATES``). The
	absence-of-exceptions claim is a SEPARATE, stronger statement, so it may render
	ONLY when BOTH hold:

	  1. ``result_state == evaluated_clean`` (every required check evaluated), AND
	  2. the run persisted ZERO findings (``findings_count == 0``).

	A run that evaluated full coverage but DID persist findings therefore can never
	read "no exceptions", closing the false-clean render R4-P0-03/R5 targeted. The
	sentence is also unconditionally suppressed while the installation is in
	shadow/preview (PP-4 — a preview issues no outward attestation)."""
	return not shadow and result_state == cr.CLEAN_RUN_STATE and int(findings_count or 0) == 0


def _fallback_dashboard_html(
	title: str,
	findings: list,
	counts: dict,
	coverage_note: str,
	*,
	result_state: str = "partial",
	coverage_notes: list | None = None,
	shadow: bool = False,
	integrity_digest: str | None = None,
) -> str:
	"""A minimal, self-contained, A2-safe findings summary — the server-generated
	FLOOR used only when the delegate produced findings but authored no dashboard
	(so a completed/partial run always yields ONE saved dashboard). It renders the
	already-persisted, already-lossy finding fields (authored ``note`` + ref +
	amount + severity + PP-1 ``result_class``) — NEVER the opaque token, and never
	any rule id/threshold. Inline styles only (CSP sandbox). The Jarvis theme
	injects the surrounding CSS variables; sane fallbacks keep it legible standalone.

	PP-1: every row shows its ``result_class`` label beside the amount, and its
	authored note is routed through the strong-verb helper so "saved/recovered/
	prevented" can never render for a non-``confirmed_outcome`` row. PP-2: the
	coverage-verdict ``result_state`` is rendered as an in-body heading and the
	empty-findings sentence is state-conditional. PP-3: the coverage gaps section
	surfaces each not_evaluable check's typed remediation text.

	PP-4: when ``shadow`` is set the artifact is a reviewer-only PREVIEW — no
	outward clean/compliant attestation renders even when ``result_state ==
	evaluated_clean`` (the clean heading and the "No exceptions were found" sentence
	are both suppressed in favour of an explicit preview banner). PP-6: when an
	``integrity_digest`` is supplied it is stamped into the document BODY (same
	in-body, screenshot-safe placement as the state block) so the coverage/integrity
	digest travels with the numbers on a saved/exported meter artifact."""
	total = sum(counts.get(s, 0) for s in ("blocker", "warning", "note"))
	cards = "".join(
		f'<div style="flex:1;min-width:120px;padding:14px 16px;border:1px solid var(--jarvis-border,#e5e7eb);'
		f'border-radius:10px;background:var(--jarvis-surface,#fff)">'
		f'<div style="font-size:26px;font-weight:700;color:var(--jarvis-text,#111827)">{counts.get(sev, 0)}</div>'
		f'<div style="font-size:12px;text-transform:uppercase;letter-spacing:.04em;'
		f'color:var(--jarvis-muted,#6b7280)">{_SEV_LABEL[sev]}</div></div>'
		for sev in ("blocker", "warning", "note")
	)
	rows = ""
	order = {"blocker": 0, "warning": 1, "note": 2}
	for f in sorted(findings or [], key=lambda x: (order.get(x.get("severity"), 3), str(x.get("ref_name")))):
		sev = f.get("severity") or "note"
		amt = f.get("amount") or 0
		try:
			amt = f"{float(amt):,.2f}"
		except (TypeError, ValueError):
			amt = _esc(amt)
		# PP-1 strong-verb gate: an evaluator row is never confirmed_outcome, so the
		# helper strips saved/recovered/prevented/… from the authored note.
		safe_note = cr.render_value_text(
			f.get("note"), f.get("result_class"), outcome_provenance=f.get("outcome_provenance")
		)
		rows += (
			f'<tr style="border-top:1px solid var(--jarvis-border,#e5e7eb)">'
			f'<td style="padding:8px 10px;white-space:nowrap;text-transform:capitalize">{_esc(sev)}</td>'
			f'<td style="padding:8px 10px;white-space:nowrap;color:var(--jarvis-muted,#6b7280)">'
			f"{_esc(f.get('result_class'))}</td>"
			f'<td style="padding:8px 10px">{_esc(safe_note)}</td>'
			f'<td style="padding:8px 10px;white-space:nowrap;color:var(--jarvis-muted,#6b7280)">'
			f"{_esc(f.get('ref_doctype'))} · {_esc(f.get('ref_name'))}</td>"
			f'<td style="padding:8px 10px;text-align:right;white-space:nowrap">{amt}</td></tr>'
		)
	if not rows:
		# R5-J1 two-condition render gate: the clean "No exceptions were found"
		# sentence renders ONLY when result_state == evaluated_clean AND the run
		# persisted ZERO findings (``total``), and NEVER while shadow (PP-4 — a
		# preview issues no outward attestation). ``_clean_attestation_allowed``
		# is the single predicate both conditions flow through, so a run that
		# persisted findings can never read "no exceptions" even under full
		# coverage. For every other case the PP-2 state-conditional (non-clean)
		# sentence renders; ``evaluated_clean`` WITH findings can never reach the
		# clean sentence because condition 2 fails.
		if shadow:
			sentence = (
				"Preview (shadow) run — findings are visible to the reviewer only; "
				"no clean or compliant attestation is issued while this capability is in preview."
			)
		elif _clean_attestation_allowed(result_state, total, shadow=shadow):
			sentence = _EMPTY_SENTENCE["evaluated_clean"]
		else:
			# Not both conditions met: never the clean sentence. An evaluated_clean
			# verdict with findings present (condition 2 failed) is downgraded to the
			# non-clean partial-style sentence rather than the "no exceptions" claim.
			state_for_sentence = result_state if result_state != cr.CLEAN_RUN_STATE else "partial"
			sentence = _EMPTY_SENTENCE.get(state_for_sentence, _EMPTY_SENTENCE["partial"])
		rows = (
			'<tr><td colspan="5" style="padding:14px 10px;color:var(--jarvis-muted,#6b7280)">'
			f"{_esc(sentence)}</td></tr>"
		)

	# PP-2 in-body coverage-verdict heading (screenshot-safe — inside the body).
	# PP-4: in shadow the outward clean/compliant heading is REPLACED by a preview
	# banner, so a computed evaluated_clean never surfaces as an outward attestation.
	if shadow:
		label, fg, bg = ("Preview (shadow) — not a compliant attestation", "#3730a3", "#e0e7ff")
		state_attr = "shadow"
	else:
		label, fg, bg = _STATE_HEADING.get(result_state, _STATE_HEADING["partial"])
		state_attr = result_state
	state_block = (
		f'<div data-result-state="{_esc(state_attr)}" '
		f'style="display:inline-block;margin:0 0 16px;padding:6px 12px;border-radius:8px;'
		f'font-size:13px;font-weight:600;color:{fg};background:{bg}">{_esc(label)}</div>'
	)

	# PP-6 in-body coverage/integrity digest — stamped onto the numbers, screenshot-
	# safe (inside the body, never a separable page), so a saved/exported meter
	# artifact always carries its evaluator integrity digest and coverage caveat.
	digest_block = ""
	if integrity_digest:
		caveat = _esc(coverage_note) if coverage_note else "full coverage for the reported windows"
		digest_block = (
			f'<div data-digest-block style="margin:20px 0 0;padding:12px 14px;'
			f"border:1px dashed var(--jarvis-border,#e5e7eb);border-radius:8px;"
			f'color:var(--jarvis-muted,#6b7280);font-size:12px">'
			f'<div style="text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin:0 0 6px">'
			f"Coverage &amp; integrity digest</div>"
			f"<div>Evaluator integrity digest: <code>{_esc(integrity_digest)}</code></div>"
			f"<div>Coverage caveats: {caveat}</div>"
			f"<div>Reporting windows are computed server-side and fixed on this artifact.</div>"
			f"</div>"
		)

	# PP-3 coverage-gaps section: typed remediation text per not_evaluable check.
	gaps = ""
	if coverage_notes:
		items = "".join(
			f'<li style="margin:0 0 6px"><strong>{_esc(n.get("reason_code"))}</strong> — '
			f"{_esc(n.get('remediation'))}"
			f"{(' (' + _esc(n.get('detail')) + ')') if n.get('detail') else ''}</li>"
			for n in coverage_notes
		)
		gaps = (
			f'<div style="margin:20px 0 0"><h2 style="font-size:15px;margin:0 0 8px">Coverage gaps</h2>'
			f'<ul style="margin:0;padding-left:18px;color:var(--jarvis-text,#111827);font-size:13px">'
			f"{items}</ul></div>"
		)

	banner = ""
	if coverage_note:
		banner = (
			f'<div style="margin:0 0 16px;padding:10px 14px;border-radius:8px;'
			f"background:var(--jarvis-warn-bg,#fef3c7);color:var(--jarvis-warn-text,#92400e);"
			f'font-size:13px">Partial run — {_esc(coverage_note)}</div>'
		)
	return (
		f'<!doctype html><html><head><meta charset="utf-8">'
		f"<title>{_esc(title)}</title></head>"
		f'<body style="margin:0;font-family:var(--jarvis-font,system-ui,-apple-system,Segoe UI,Roboto,sans-serif);'
		f'color:var(--jarvis-text,#111827);background:var(--jarvis-bg,#f9fafb);padding:24px">'
		f'<h1 style="font-size:20px;margin:0 0 8px">{_esc(title)}</h1>'
		f"{state_block}"
		f'<p style="margin:0 0 18px;color:var(--jarvis-muted,#6b7280);font-size:13px">'
		f"{total} finding(s) this run.</p>"
		f"{banner}"
		f'<div style="display:flex;gap:12px;margin:0 0 20px;flex-wrap:wrap">{cards}</div>'
		f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:14px">'
		f'<thead><tr style="text-align:left;color:var(--jarvis-muted,#6b7280);font-size:12px;'
		f'text-transform:uppercase;letter-spacing:.04em">'
		f'<th style="padding:8px 10px">Severity</th><th style="padding:8px 10px">Class</th>'
		f'<th style="padding:8px 10px">Finding</th>'
		f'<th style="padding:8px 10px">Reference</th>'
		f'<th style="padding:8px 10px;text-align:right">Amount</th></tr></thead>'
		f"<tbody>{rows}</tbody></table></div>"
		f"{gaps}{digest_block}</body></html>"
	)


def _visibility_owner(inst) -> str:
	"""PP-4 — WHO may see this installation's outputs. While ``shadow`` the named
	reviewer alone sees the findings/dashboards/activity (not the general owner
	surface, not any customer-facing attestation); once ``live`` the installer owner
	sees them. The agent permission-query hooks (``agent_permissions``) and the
	dashboard scope condition both gate reads on the row ``owner``/``target_user``,
	so re-homing the persisted rows to THIS identity is what enforces shadow
	visibility on every read path — the SPA, the Desk, and generic REST alike."""
	if (inst.get("activation_state") or "shadow") == "shadow":
		return inst.get("reviewer") or inst.owner
	return inst.owner


def persist_agent_dashboard(
	run_doc, inst, html: str, *, title=None, description=None, set_on_run: bool = True, owner_override=None
) -> str:
	"""Create ONE ``Jarvis Dashboard`` from a self-contained HTML document and
	(by default) link it on the Run.

	Written server-side with ignore_permissions and re-homed to the human
	``inst.owner`` — the SAME identity convention as the Run/Finding rows (owner =
	the installer, so it appears in THEIR Dashboards list next to the run), not
	the (possibly service-account) run_as_user. User-scoped, Static (no live
	sources → no query specs that could leak rule shape; the summary is
	self-contained). The controller enforces the html/title caps + CSP contract.
	Returns the new dashboard name.

	PP-4: ``owner_override`` re-homes the saved dashboard to the reviewer while the
	installation is in shadow (so ``_validate_scope`` pins ``target_user`` to the
	reviewer and the owner surface never shows a shadow dashboard); it defaults to
	the installer owner for a live installation."""
	owner = owner_override or inst.owner
	listing_title = frappe.db.get_value(LISTING, run_doc.agent, "title") or run_doc.agent
	dash_title = (title or "").strip()[:140] or _default_dashboard_title(run_doc, listing_title)

	doc = frappe.get_doc(
		{
			"doctype": DASHBOARD,
			"dashboard_title": dash_title,
			"description": (description or "").strip()[:255] or None,
			"html": html,
			"scope": "User",
			# "Custom", not "Jarvis": an agent-run dashboard is bespoke, not a
			# builder canvas. Its body is either _fallback_dashboard_html (this
			# module's own hardcoded inline-styled attestation layout, with
			# --jarvis-* fallbacks) or delegate-AUTHORED html that never saw the
			# dashboards skill's theme cheatsheet - and either way the PP-4 shadow
			# banner is injected into it server-side. None of that can satisfy a
			# curated theme's design rules, so claiming one would make every run
			# fail to save. Custom relaxes the design rules while keeping the
			# safety bans (@font-face, external URLs) absolute.
			"theme": "Custom",
			"source_conversation": run_doc.get("conversation") or "",
		}
	)
	# Pre-set owner so the controller pins target_user to the human owner (its
	# _validate_scope reads self.owner, set before insert). ignore_permissions —
	# trusted server infrastructure, exactly like the Finding rows above.
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	if set_on_run:
		frappe.db.set_value(RUN, run_doc.name, "dashboard", doc.name, update_modified=False)
	return doc.name


def _notify_owner_dashboard(
	owner: str,
	run_name: str,
	dashboard_name: str,
	agent_title: str,
	status: str,
	findings_count: int,
	blocker_count: int,
) -> None:
	"""Best-effort bell notification to the human owner that a run finished + a
	dashboard is ready to open. Never raises."""
	if not owner or owner in ("Administrator", "Guest"):
		return
	try:
		verb = {"completed": "completed", "partial": "completed (partial)"}.get(status, "finished")
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": owner,
				"type": "Alert",
				"subject": f"{agent_title or 'Agent'} run {verb}: {findings_count} finding(s), "
				f"{blocker_count} blocker(s)",
				"email_content": "Your agent finished a run. Open its findings dashboard from the "
				"run, or the Dashboards page.",
				"document_type": DASHBOARD,
				"document_name": dashboard_name,
			}
		).insert(ignore_permissions=True)
	except Exception:
		pass


def _fingerprint(rule_id, ref_doctype, ref_name) -> str:
	"""Stable dedupe key for a finding — identity only (rule + the doc it hit),
	NOT the amount (which can drift run to run while it stays the same finding)."""
	raw = "|".join([str(rule_id or ""), str(ref_doctype or ""), str(ref_name or "")])
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _safe_date(value):
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _reassign_owner(doctype: str, name: str, owner: str) -> None:
	"""Hand a server-inserted (ignore_permissions) row to the installation owner
	so ``if_owner`` shows it to the right customer only — mirrors macros.py."""
	if owner and owner != frappe.session.user:
		frappe.db.set_value(doctype, name, "owner", owner, update_modified=False)


# --------------------------------------------------------------------------- #
# Phase 3 — delegate findings writeback (A16 coverage-scoped auto-resolve +
# A17 watermark recheck). The container evaluator computes the findings; this is
# the DETERMINISTIC bench-side persister the ``record_agent_run`` tool calls once
# the caller's session_key has been resolved to a running Run + validated. Its
# auto-resolve is coverage-scoped and gated (drift / scoped / under-read), so it
# never silently closes real blockers a chunk-scoped/partial run never observed.
# --------------------------------------------------------------------------- #
def _parse_legacy_coverage(st: str) -> tuple[str, str | None, str]:
	"""Map a legacy free-string per-token coverage value to
	``(state, reason_code_or_None, detail)``. Handles ``evaluated``,
	``not_evaluable`` / ``not_evaluable(<reason>)``, ``truncated`` and any other
	free string (treated as a not_evaluable whose raw text is preserved as
	``detail`` so PP-3 fail-safe coercion keeps it)."""
	if st == "evaluated":
		return "evaluated", None, ""
	if st.startswith("truncated"):
		return "truncated", "run_truncated_watermark", ""
	if st.startswith("not_evaluable"):
		inner = ""
		if "(" in st and st.endswith(")"):
			inner = st[st.index("(") + 1 : -1].strip()
		return "not_evaluable", (inner or None), ""
	# an unrecognised free string — keep it, let coerce_reason_code preserve it.
	return "not_evaluable", st, ""


# PP-3: each placeholdered reason-code template names ONE substitution key; the
# typed coverage manifest's ``detail`` is the concrete subject that fills it (the
# app name, the setting, the doctype, ...). Threading it here — with a neutral noun
# when the evaluator supplied no detail — is what stops the LITERAL ``{app}`` /
# ``{setting}`` brace-string leaking to the customer in the coverage-gap remediation
# text (``remediation_for`` called with no fmt renders the raw, unsubstituted brace).
_REASON_PLACEHOLDER = {
	"app_absent_or_ineligible": ("app", "the required app"),
	"configuration_missing": ("setting", "the required setting"),
	"record_coverage_insufficient": ("records", "records"),
	"source_stale": ("source", "source"),
	"external_evidence_absent": ("evidence", "the required evidence"),
	"unsupported_customisation": ("doctype", "this doctype"),
}


def _remediation_text(reason_code: str, detail: str = "") -> str:
	"""Customer-facing PP-3 remediation for a reason code, with its ``{...}``
	placeholder filled from the typed manifest ``detail`` (a neutral noun when the
	evaluator supplied none) so a literal ``{app}`` / ``{setting}`` brace NEVER
	reaches the customer. Reason codes without a placeholder pass straight through."""
	slot = _REASON_PLACEHOLDER.get(reason_code)
	if not slot:
		return cr.remediation_for(reason_code)
	key, default = slot
	subject = (detail or "").strip() or default
	return cr.remediation_for(reason_code, **{key: subject})


def _listing_rule_tokens(agent: str) -> set:
	"""The agent's DECLARED opaque rule-token manifest (``Jarvis Agent Listing.
	rule_tokens``) — the AUTHORITATIVE required-check set for the PP-2 coverage
	verdict. Sourcing the required tokens from the listing here (NOT from the
	writeback-supplied coverage keys) is what stops a delegate under-reporting its
	coverage to earn a false ``evaluated_clean``: a declared token the run never
	returns ``evaluated`` is un-evaluated required coverage. Empty for operators /
	legacy agents (no rule tokens -> no coverage bar to fail)."""
	import json as _json

	raw = frappe.db.get_value(LISTING, agent, "rule_tokens")
	if not raw:
		return set()
	try:
		parsed = _json.loads(raw)
	except Exception:
		return set()
	return {str(t) for t in parsed if t} if isinstance(parsed, list) else set()


def _coverage_summary(coverage: dict) -> tuple[set, list]:
	"""Resolve the per-check coverage manifest into
	``(fully-evaluated token set, [typed not_evaluable/truncated notes])`` (PP-3).

	Each manifest value is either the typed form
	``{state, reason_code, detail}`` or the legacy string form
	``{token: "evaluated"|"not_evaluable(reason)"|"truncated"}``. A non-``evaluated``
	token yields a typed note dict carrying the closed PP-3 ``reason_code`` (an
	unknown code is coerced to ``unsupported_customisation`` with the raw string in
	``detail`` — never dropped) plus its customer remediation, retryability and
	support routing, so the dashboard / support UI / telemetry all read one shape."""
	evaluated, notes = set(), []
	for token, raw in (coverage or {}).items():
		if isinstance(raw, dict):
			state = str(raw.get("state") or "")
			reason_raw = raw.get("reason_code")
			detail = str(raw.get("detail") or "")
		else:
			state, reason_raw, detail = _parse_legacy_coverage(str(raw or ""))
		if state == "evaluated":
			evaluated.add(token)
			continue
		if not state and reason_raw is None and not detail:
			continue  # empty / unset token — no coverage signal
		code, coerced_detail = cr.coerce_reason_code(reason_raw)
		detail = detail or coerced_detail
		notes.append(
			{
				"token": token,
				"state": state or "not_evaluable",
				"reason_code": code,
				"detail": detail,
				"remediation": _remediation_text(code, detail),
				"retryable": cr.is_retryable(code),
				"routing": cr.routing_for(code),
			}
		)
	return evaluated, notes


def _watermark_drift(run_doc, scope: dict) -> bool:
	"""A17: recompute {count, max(modified)} over the scope's GL as-of window and
	compare to the watermark stamped at launch. True when the GL changed mid-scan
	(a backdated JV between two chunk fetches) so the run must NOT read as
	``completed``. No stamped watermark / no scope -> no drift signal (False)."""
	stamped_count = run_doc.get("wm_row_count")
	company = (scope or {}).get("company")
	to_date = (scope or {}).get("to_date")
	if stamped_count is None or not company or not to_date:
		return False
	try:
		wm = frappe.db.sql(
			"""select count(*) n, max(modified) m from `tabGL Entry`
			   where company = %(company)s and posting_date <= %(to_date)s""",
			{"company": company, "to_date": to_date},
			as_dict=True,
		)[0]
	except Exception:
		return False
	if int(wm.n or 0) != int(stamped_count or 0):
		return True
	stamped_mod = run_doc.get("wm_gl_max_modified")
	new_mod = wm.m
	if bool(stamped_mod) != bool(new_mod):
		return True
	if stamped_mod and new_mod:
		from frappe.utils import get_datetime

		return get_datetime(stamped_mod) != get_datetime(new_mod)
	return False


def _scoped_visibility(run_doc, inst) -> bool:
	"""FIX 1 / A12: is this run's run-as identity record-sliced on a GL dimension?

	A User Permission on a GL dimension (Cost Center / Project / Account / party …)
	makes every aggregate the run-as user reads a SLICE — a numerically WRONG total,
	not merely a narrower one — so a scoped run must NEVER auto-resolve findings it
	could not see (closing an unseen blocker on a partial view is a silent
	regression). Derived FRESH from the Run's stamped ``permission_profile`` (the
	run-as user's user_permissions snapshotted AT LAUNCH — the authoritative signal),
	so a User Permission added AFTER install is caught even though the installation's
	``scoped_visibility`` flag (stamped only at map/backfill time) has gone stale.
	Falls back to that stamped flag when the profile is absent (a legacy run, or one
	whose profile computation failed)."""
	import json as _json

	from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
		_GL_SCOPED_DIMENSIONS,
	)

	profile = run_doc.get("permission_profile")
	if profile:
		try:
			ups = (_json.loads(profile) or {}).get("user_permissions") or {}
			# A GL-dimension key present AND carrying at least one value = a live slice.
			if any(ups.get(dt) for dt in _GL_SCOPED_DIMENSIONS):
				return True
		except Exception:
			pass
	return bool(inst.get("scoped_visibility"))


def _rowcount_shortfall(run_doc, rows_consumed) -> bool:
	"""FIX 6: did the evaluator actually READ the ledger? The watermark stamped at
	launch (``wm_row_count`` — count of in-scope GL entries) proves whether the
	ledger is non-empty. If the evaluator reports it consumed ZERO rollup rows while
	the watermark shows rows exist, its fetch under-read: a green, zero-finding run
	that would otherwise auto-resolve real blockers it never observed. Force partial
	+ skip auto-resolve.

	Only the ZERO-read is gated. A per-account rollup count is NOT magnitude-
	comparable to the GL-entry watermark (cancelled + pre-period entries legitimately
	differ), so a non-zero count is deliberately not asserted equal — that would be a
	false-positive machine. ``rows_consumed=None`` (a delegate/evaluator that does not
	yet report it) means "cannot reconcile", never a false shortfall."""
	stamped = run_doc.get("wm_row_count")
	if stamped is None:
		return False
	try:
		if int(stamped) <= 0:
			return False  # empty (or unknown) ledger — nothing to under-read
	except (TypeError, ValueError):
		return False
	if rows_consumed is None:
		return False
	try:
		return int(rows_consumed) <= 0
	except (TypeError, ValueError):
		return False


def record_delegate_run(
	run,
	installation,
	findings: list,
	*,
	coverage: dict | None = None,
	scope: dict | None = None,
	truncated: bool = False,
	dropped: list | None = None,
	canvas_ref: str | None = None,
	integrity_digest: str | None = None,
	dashboard: str | None = None,
	rows_consumed: int | None = None,
):
	"""Persist a delegate's evaluator findings onto its running Run (A16/A17).

	``run`` is the already-resolved ``running`` Jarvis Agent Run (the tool
	resolves it from the caller's session_key). ``findings`` are the VALIDATED
	rows (each ``{token, ref_doctype, ref_name, amount, severity, note}`` —
	invalid rows already dropped by the tool and passed as ``dropped``).
	``coverage`` is the per-rule manifest; ``scope`` the resolved run scope
	(carries ``company`` stamped on every Finding + used to scope auto-resolve and
	recompute the watermark). Returns the reloaded Run doc.
	"""
	import json as _json

	inst = installation if hasattr(installation, "owner") else frappe.get_doc(INSTALLATION, installation)
	run_doc = run if hasattr(run, "name") else frappe.get_doc(RUN, run)
	# PP-4: WHO the persisted run/findings/dashboard are re-homed to for read
	# visibility — the reviewer while shadow, the installer once live. The row
	# ``owner`` is the single axis the agent permission hooks scope on, so this
	# reassignment IS the shadow-visibility enforcement (owner cannot read shadow
	# output; the named reviewer can). Every read path below MUST filter/re-home
	# on visibility_owner, not inst.owner directly, or a reviewer != owner
	# installation silently breaks dedupe/auto-resolve (#454).
	shadow = (inst.get("activation_state") or "shadow") == "shadow"
	visibility_owner = _visibility_owner(inst)
	agent = inst.agent
	coverage = coverage or {}
	scope = scope or {}
	dropped = dropped or []
	company = (scope.get("company") or "").strip()

	# Collapse this run's findings by fingerprint (token + doc identity), so a doc
	# flagged twice by one rule is ONE finding and counts stay consistent.
	by_fp: dict = {}
	for f in findings or []:
		token = f.get("token") or f.get("rule_id")
		fp = _fingerprint(token, f.get("ref_doctype"), f.get("ref_name"))
		by_fp.setdefault(fp, f)
	seen_fps = set(by_fp)

	counts = {"blocker": 0, "warning": 0, "note": 0}
	for fp, f in by_fp.items():
		token = f.get("token") or f.get("rule_id")
		sev = f.get("severity") or "note"
		counts[sev] = counts.get(sev, 0) + 1
		note = f.get("note") or f.get("detail") or ""

		existing = frappe.db.get_value(
			FINDING,
			{"owner": visibility_owner, "agent": agent, "fingerprint": fp, "state": "open"},
			"name",
		)
		if existing:
			frappe.db.set_value(FINDING, existing, "last_seen_run", run_doc.name, update_modified=False)
			continue

		# PP-1: the epistemic class + its class-conditional metadata, already
		# validated by the tool (result_class in-enum, never confirmed_outcome, and
		# the required candidate/legal fields present). Persisted verbatim; the
		# controller set-once guard makes result_class immutable thereafter, and
		# confirmation_status stays ``unconfirmed`` until the PP-5 ledger moves it.
		result_class = f.get("result_class")
		fd = frappe.get_doc(
			{
				"doctype": FINDING,
				"run": run_doc.name,
				"agent": agent,
				# A2: the OPAQUE rule token lives in rule_id — the real rule_id / catalog
				# identifier never reaches the bench; fingerprint dedup keeps working.
				"rule_id": token or "",
				"severity": sev,
				"result_class": result_class,
				# A2: authored, outcome-level text only (the evaluator's fixed-template
				# note). No as-coded threshold / carve-out text.
				"title": note[:140],
				"detail_md": note,
				"section": f.get("section") or "",
				"effective_date": _safe_date(f.get("effective_date")),
				# derived_candidate metadata (empty for other classes).
				"confidence": f.get("confidence"),
				"match_basis": f.get("match_basis") or "",
				"false_positive_path": f.get("false_positive_path") or "",
				# legal_scenario metadata (empty for other classes).
				"rule_version": f.get("rule_version") or "",
				"assumptions": f.get("assumptions") or "",
				"known_exceptions": f.get("known_exceptions") or "",
				"source": f.get("source") or "",
				"reviewer": f.get("reviewer") or None,
				"ref_doctype": f.get("ref_doctype") or "",
				"ref_name": f.get("ref_name") or "",
				# A16: stamp the company scope so a Company-B run never auto-resolves a
				# Company-A finding; empty legacy rows are exempt until re-seen.
				"company": company or None,
				"amount": f.get("amount") or 0,
				"disclaimer": f.get("disclaimer") or "",
				"fingerprint": fp,
				"state": "open",
				"first_seen_run": run_doc.name,
				"last_seen_run": run_doc.name,
			}
		)
		fd.flags.ignore_permissions = True
		fd.insert()
		# PP-4: re-home to the reviewer while shadow (visibility_owner), the installer
		# once live — the row owner is what the finding read path gates on.
		_reassign_owner(FINDING, fd.name, visibility_owner)

	# --- A17 watermark recheck (FIX 2: HOISTED before the A16 gate) ------------
	# Computed BEFORE the auto-resolve loop so it can gate it: a run that observed an
	# INCONSISTENT GL snapshot (a backdated JV landing between two chunk fetches) must
	# not close prior findings on that drifted view — the same "never close on an
	# inconsistent snapshot" principle already applied to ``truncated``. Depends only
	# on run_doc + scope, so the hoist is side-effect-free.
	wm_drift = _watermark_drift(run_doc, scope)

	# --- A12 scoped-visibility recheck (FIX 1) --------------------------------
	# A record-sliced run-as identity computes numerically WRONG aggregates, so a
	# scoped run must never auto-resolve findings it could not see. Derived FRESH
	# from the run's stamped permission_profile (not the install's stale flag).
	scoped = _scoped_visibility(run_doc, inst)

	# --- ledger under-read reconciliation (FIX 6) -----------------------------
	# The watermark proves the ledger is non-empty; a zero-read means the evaluator
	# fetched nothing — a green "zero findings" run that would otherwise auto-resolve
	# real blockers. Gate it.
	row_shortfall = _rowcount_shortfall(run_doc, rows_consumed)

	# --- A16 coverage-scoped auto-resolve --------------------------------------
	# HARD RULE: a run that is truncated, drift-inconsistent (A17), scope-sliced (A12)
	# or under-read auto-resolves NOTHING — its findings list is not a trustworthy
	# full view of the books, and closing an unseen blocker would be a silent
	# regression. Only findings whose token was FULLY EVALUATED this run AND that
	# belong to THIS run's company scope are eligible; empty-company legacy rows are
	# exempt.
	evaluated_tokens, coverage_notes = _coverage_summary(coverage)

	# PP-2 (false-clean gate): the required-check set is the agent's DECLARED
	# rule-token manifest (authoritative), NEVER the writeback-supplied coverage
	# keys — a delegate under-reporting its coverage must not be able to define its
	# own bar. A declared token the run never returned ``evaluated`` (absent from,
	# or not_evaluable in, the coverage payload) is un-evaluated required coverage:
	# it forces the run partial so an empty/narrow manifest can never read as clean.
	required_tokens = _listing_rule_tokens(agent)
	required_unevaluated = required_tokens - evaluated_tokens

	if not truncated and not wm_drift and not scoped and not row_shortfall and evaluated_tokens:
		candidates = frappe.get_all(
			FINDING,
			filters={
				"owner": visibility_owner,
				"agent": agent,
				"state": "open",
				"rule_id": ["in", list(evaluated_tokens)],
			},
			fields=["name", "fingerprint", "company"],
		)
		for c in candidates:
			if c.fingerprint in seen_fps:
				continue  # still present this run
			# Company scope gate: only resolve a finding whose company matches this
			# run's company (both non-empty). A scopeless run, or an empty-company
			# legacy row, is left untouched (exempt) — never cross-company resolved.
			if not company or not c.company or c.company != company:
				continue
			# PP-5 review provenance: an A16 coverage-scoped auto-resolve is machine,
			# not human — stamp resolution_kind + resolved_at so a coverage close is
			# forever separable from a person acknowledging the finding (which stamps
			# resolution_kind = human + resolved_by).
			frappe.db.set_value(
				FINDING,
				c.name,
				{
					"state": "resolved",
					"resolution_kind": "auto_coverage",
					"resolved_at": frappe.utils.now(),
				},
				update_modified=False,
			)

	# --- status + coverage note ------------------------------------------------
	dropped_notes = [
		f"rejected {d.get('ref_doctype', '?')}/{d.get('ref_name', '?')} ({d.get('reason', 'invalid')})"
		for d in dropped
	]
	partial = bool(truncated) or wm_drift or scoped or row_shortfall or bool(dropped) or bool(coverage_notes)
	status = "partial" if partial else "completed"

	# PP-2: resolve EXACTLY one coverage-verdict run_state from the DECLARED required
	# checks. This is a SEPARATE axis from the execution-lifecycle ``status`` (which
	# stays ``completed`` for a run that finished executing) — a run can complete yet
	# leave required coverage incomplete. ``required_tokens`` is the agent's declared
	# manifest (resolved above, NOT the writeback coverage keys, so a delegate cannot
	# under-report to define its own bar): all-required-unevaluated -> not_evaluable;
	# some required token unevaluated -> partial; else evaluated_clean. A declared
	# required token the run never evaluated drives the coverage verdict off
	# ``evaluated_clean`` WITHOUT touching ``status``. "No exceptions" is unreachable
	# unless evaluated_clean.
	result_state = cr.resolve_run_state(
		required_tokens=required_tokens,
		evaluated_tokens=evaluated_tokens,
		partial=partial or bool(required_unevaluated),
		failed=False,
	)

	note_parts = []
	if truncated:
		note_parts.append("scan truncated; findings incomplete")
	if wm_drift:
		note_parts.append("GL changed during scan — re-run advised")
	if scoped:
		note_parts.append("scoped visibility — findings not auto-resolved")
	if row_shortfall:
		note_parts.append("ledger under-read vs watermark — re-run advised")
	if coverage_notes:
		# PP-3: surface the CUSTOMER remediation sentence (placeholder-filled) that the
		# amber "Partial scan — {coverageNote}" banner reads day to day, NOT the raw
		# internal reason-code enum slug.
		note_parts.append(
			"not evaluable: " + "; ".join(f"{n['token']}: {n['remediation']}" for n in coverage_notes)
		)
	# PP-2: a DECLARED required token entirely omitted from the coverage manifest
	# (no evaluator note of its own) still made the run partial — name the shortfall
	# so the banner is not silently blank when that is the only reason for partial.
	absent_required = required_unevaluated - {n["token"] for n in coverage_notes}
	if absent_required:
		note_parts.append(f"{len(absent_required)} required check(s) not evaluated this run")
	if dropped_notes:
		note_parts.append("dropped: " + "; ".join(dropped_notes))
	coverage_note = " | ".join(note_parts)

	# Full, machine-readable coverage manifest for the Findings board / Phase 4:
	# the per-rule coverage + not_evaluable + dropped refs + every auto-resolve-gate
	# verdict (drift / scoped / under-read) so the board can explain WHY a run held
	# findings open. When scoped, the fully-evaluated tokens are surfaced as
	# not-evaluable-scoped (their container verdicts ran on a sliced view).
	coverage_blob = _json.dumps(
		{
			"coverage": coverage,
			"required_tokens": sorted(required_tokens),
			"result_state": result_state,
			"not_evaluable": coverage_notes,
			"not_evaluable_scoped": sorted(evaluated_tokens) if scoped else [],
			"dropped": dropped,
			"watermark_drift": wm_drift,
			"scoped_visibility": scoped,
			"row_shortfall": row_shortfall,
			"rows_consumed": rows_consumed,
			"wm_row_count": run_doc.get("wm_row_count"),
			"truncated": bool(truncated),
		},
		sort_keys=True,
		default=str,
	)

	run_values = {
		"status": status,
		"result_state": result_state,
		"findings_count": len(seen_fps),
		"blocker_count": counts.get("blocker", 0),
		"finished_at": frappe.utils.now(),
		"coverage_note": coverage_note[:140],
		"coverage_json": coverage_blob[:60000],
	}
	if scope:
		run_values["scope_json"] = frappe.as_json(scope)[:60000]
	if integrity_digest:
		run_values["integrity_digest"] = str(integrity_digest)[:64]
	if canvas_ref:
		run_values["canvas_ref"] = str(canvas_ref)[:255]
	frappe.db.set_value(RUN, run_doc.name, run_values, update_modified=False)

	# PP-4: re-home the run itself to the visibility owner (reviewer while shadow,
	# installer once live) so the run history read path (owner-scoped) shows a shadow
	# run only to its reviewer. Raw set_value bypasses the launch-fields controller.
	if visibility_owner and frappe.db.get_value(RUN, run_doc.name, "owner") != visibility_owner:
		frappe.db.set_value(RUN, run_doc.name, "owner", visibility_owner, update_modified=False)

	# --- Phase 4: exactly ONE saved Jarvis Dashboard per run ------------------
	# Precedence: (1) a dashboard the delegate already authored via
	# jarvis__save_agent_dashboard this turn (Run.dashboard already set — richer,
	# model-authored HTML wins); (2) an explicit ``dashboard`` name reported at
	# writeback; (3) a server-generated A2-safe FLOOR built from the persisted
	# findings, so a findings-only run (no canvas) STILL yields an openable
	# dashboard. Best-effort: a dashboard hiccup must never fail the writeback.
	agent_title = frappe.db.get_value(LISTING, agent, "title")
	dashboard_name = frappe.db.get_value(RUN, run_doc.name, "dashboard")
	if not dashboard_name and dashboard and frappe.db.exists(DASHBOARD, dashboard):
		frappe.db.set_value(RUN, run_doc.name, "dashboard", dashboard, update_modified=False)
		dashboard_name = dashboard
	if not dashboard_name:
		try:
			title = _default_dashboard_title(run_doc, agent_title)
			html = _fallback_dashboard_html(
				title,
				list(by_fp.values()),
				counts,
				coverage_note,
				result_state=result_state,
				coverage_notes=coverage_notes,
				# PP-4: a shadow run's fallback artifact issues no outward clean/compliant
				# attestation. PP-6: stamp the evaluator integrity digest in-body.
				shadow=shadow,
				integrity_digest=integrity_digest,
			)
			dashboard_name = persist_agent_dashboard(
				run_doc, inst, html, title=title, owner_override=visibility_owner
			)
		except Exception:
			frappe.log_error(
				title="jarvis agent: fallback dashboard build failed",
				message=frappe.get_traceback(),
			)

	# Activity trail (best-effort, Link-free) + completion notification. PP-4: the
	# completion row + bell carry finding COUNTS, so in shadow they go to the reviewer
	# (visibility_owner), never the general owner surface — the owner sees the
	# preview's results only after promotion re-homes them.
	log_activity(
		agent=agent,
		agent_title=agent_title,
		installation=inst.name,
		action={"completed": "run_completed", "partial": "run_partial"}.get(status, "run_failed"),
		run=run_doc.name,
		detail=f"{len(seen_fps)} findings, {counts.get('blocker', 0)} blockers"
		+ (f"; {coverage_note}" if coverage_note else "")
		+ (f"; dashboard {dashboard_name}" if dashboard_name else ""),
		owner=visibility_owner,
	)
	if dashboard_name:
		_notify_owner_dashboard(
			visibility_owner,
			run_doc.name,
			dashboard_name,
			agent_title,
			status,
			len(seen_fps),
			counts.get("blocker", 0),
		)

	# A8 (zero-trace): the per-run session bearer must not outlive the run.
	teardown_run_session(run_doc.get("session_key"))

	# Stamp the installation completion: last_run_at whatever the trigger;
	# next_run_at only for a scheduled install.
	inst_values = {"last_run_at": frappe.utils.now()}
	if inst.schedule_enabled:
		inst_values["next_run_at"] = compute_next_run(inst.schedule_frequency, inst.schedule_time)
	frappe.db.set_value(INSTALLATION, inst.name, inst_values, update_modified=False)
	# Under FrappeTestCase the enclosing transaction is rolled back at teardown; a
	# mid-test commit would defeat that and leak Run/Finding/Provenance/Dashboard
	# rows onto the shared site. All in-test asserts read the same connection, so the
	# uncommitted writes are visible without it.
	if not frappe.flags.in_test:
		frappe.db.commit()

	run_doc.reload()
	return run_doc
