"""``jarvis__save_agent_dashboard`` — Phase-4 delegate → saved Jarvis Dashboard.

An auditor/operator DELEGATE, after its evaluator has produced findings, authors
a compact, self-contained findings dashboard HTML (severity counts + each
finding's authored outcome text + ref + amount + citation) and persists it here.
The delegate builds the HTML; this tool lands it as a real ``Jarvis Dashboard``
the customer can open, and links it on the run.

This is the "Option A" wire: the delegate persists its OWN self-contained HTML
directly, so the bench never has to fetch a hosted canvas back out of the
container gateway (no fleet canvas-GET / admin relay). It reuses the SAME proven
identity path as ``record_agent_run``:

  * It runs impersonated as the run's ``run_as_user`` (the plugin dispatch
    resolved the caller's ``X-Jarvis-Session`` header to that user).
  * It resolves the ``Jarvis Agent Run`` from the CALLER's session_key
    (``Run.session_key`` — never a model-supplied id), so a delegate can only
    ever attach a dashboard to its OWN run, and only while the run is still
    ``running``. A call to an already-finalized run is an idempotent no-op that
    returns the run's existing dashboard.

A2 content contract: the HTML the delegate supplies must be OUTCOME-LEVEL text
only (what fired, on which doc, why it matters, the statutory citation) — never
rule ids/tokens, predicates, thresholds, or as-coded catalog text. The persisted
dashboard is a disclosure surface, governed by the same contract as the finding
writeback. The HTML renders inside the Dashboards page's CSP-locked, egress-
blocked sandbox iframe (same threat model as the chat canvas), so it may not
reach the network regardless of what the model emitted.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError

RUN = "Jarvis Agent Run"
INSTALLATION = "Jarvis Agent Installation"

# PP-4 shadow attestation gate for a delegate-AUTHORED dashboard. Same wording as
# the fallback preview banner (``agent_runs._fallback_dashboard_html``) so a
# reviewer sees one consistent "this is a preview, not a compliant attestation"
# label. Injected into the document BODY (screenshot-safe, not a detachable chrome
# element) and carrying ``data-result-state="shadow"`` like the fallback state block.
_SHADOW_BANNER = (
	'<div data-result-state="shadow" data-shadow-preview="1" '
	'style="display:block;margin:0 0 16px;padding:8px 14px;border-radius:8px;'
	'font-size:13px;font-weight:600;color:#3730a3;background:#e0e7ff">'
	"Preview (shadow) — not a compliant attestation. Visible to the named reviewer "
	"only; no clean or compliant claim is issued while this capability is in preview."
	"</div>"
)


def _shadow_gate_html(html: str) -> str:
	"""Route a delegate-AUTHORED dashboard through the PP-4 shadow gate.

	An authored dashboard bypasses the fallback builder's shadow handling, so a
	shadow preview could otherwise present the delegate's OWN clean/compliant
	attestation on the (re-homed) reviewer surface. This makes the authored artifact
	unambiguously a preview:

	  (a) neutralises any PP-1 strong verb the author emitted — an authored dashboard
	      is never a ``confirmed_outcome`` row, so ``render_value_text`` strips
	      saved/recovered/prevented/… (applied to the whole HTML string: strong verbs
	      live in visible copy, not HTML syntax, so the claim is stripped without
	      breaking markup); and
	  (b) injects the non-detachable preview banner into the document body, right
	      after ``<body …>`` when present, else at the top.
	"""
	from jarvis.chat import coverage_reasons as cr

	# result_class is None → strong_verb_allowed is False → the verb is neutralised.
	safe = cr.render_value_text(html, None)
	idx = safe.lower().find("<body")
	if idx != -1:
		close = safe.find(">", idx)
		if close != -1:
			return safe[: close + 1] + _SHADOW_BANNER + safe[close + 1 :]
	return _SHADOW_BANNER + safe


def save_agent_dashboard(
	html: str,
	title: str | None = None,
	description: str | None = None,
) -> dict:
	"""Persist a delegate-authored findings dashboard and link it on the run.

	Args:
	  html: the FULL self-contained HTML document (inline CSS/JS only, no
	    external resources — it renders in the CSP-locked sandbox). A2: outcome
	    text + citation only, never rule ids/thresholds.
	  title: short dashboard title; defaults to "<agent> — <period>".
	  description: optional one-line summary.

	Returns ``{run, dashboard, title}``. On an already-finalized run returns the
	run's existing dashboard (idempotent).
	"""
	from jarvis.chat import agent_runs
	from jarvis.tools._agent_run_ctx import get_session_key

	if not (html or "").strip():
		raise InvalidArgumentError("save_agent_dashboard requires a non-empty html document")

	# The run is resolved from the CALLER's session bearer, never a model-supplied
	# id — so a delegate can only ever attach a dashboard to its own run.
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"save_agent_dashboard must be called by an agent delegate over its run "
			"session (no session_key in context)"
		)

	run_row = frappe.db.get_value(
		RUN,
		{"session_key": session_key},
		["name", "status", "installation", "dashboard"],
		as_dict=True,
	)
	if not run_row:
		raise InvalidArgumentError("no agent run is bound to this session")
	if run_row.status != "running":
		# Idempotency: the run already finalized — return its dashboard (if any)
		# without creating a second one.
		return {
			"run": run_row.name,
			"dashboard": run_row.dashboard or None,
			"idempotent": True,
			"note": "run already finalized; existing dashboard returned",
		}
	if not run_row.installation:
		raise InvalidArgumentError("run has no installation")
	if run_row.dashboard:
		# Already attached this run (a retried author step) — reuse it.
		return {"run": run_row.name, "dashboard": run_row.dashboard, "idempotent": True}

	inst = frappe.get_doc(INSTALLATION, run_row.installation)
	run_doc = frappe.get_doc(RUN, run_row.name)

	# PP-4: the delegate-authored dashboard WINS the precedence in
	# ``record_delegate_run`` (richer artifact), so it must carry the SAME shadow
	# enforcement the fallback path applies — otherwise a shadow installation's
	# authored dashboard lands owner-owned (visible on the general owner surface) and
	# can render an outward clean/compliant attestation. Two enforcements, mirroring
	# ``agent_runs.record_delegate_run``:
	#   (1) re-home the saved dashboard to the visibility owner (the named reviewer
	#       while shadow, the installer once live) so shadow output never reaches the
	#       general owner surface; and
	#   (2) while shadow, route the authored HTML through the attestation gate — the
	#       preview banner + PP-1 strong-verb neutralisation — so no compliant claim
	#       is presented while the capability is in preview.
	visibility_owner = agent_runs._visibility_owner(inst)
	shadow = (inst.get("activation_state") or "shadow") == "shadow"
	persist_html = _shadow_gate_html(html) if shadow else html

	try:
		dashboard = agent_runs.persist_agent_dashboard(
			run_doc,
			inst,
			persist_html,
			title=title,
			description=description,
			owner_override=visibility_owner,
		)
	except frappe.ValidationError as e:
		# Caps/scope errors from the DocType controller — give the delegate a
		# clean, non-fatal message (the run's findings writeback still proceeds).
		raise InvalidArgumentError(str(e))

	# PP-4 enforcement (1), made effective: Frappe stamps ``owner = session.user`` at
	# insert (``document.set_user_and_timestamp``), OVERWRITING the pre-set
	# ``owner_override`` on the DB owner column. A shadow run is impersonated as the
	# run_as_user, so the saved dashboard would otherwise be owner-owned and readable
	# on the general owner surface (and ``can_read_dashboard`` grants the row owner
	# regardless of scope). Re-home the persisted row's ``owner`` AND ``target_user``
	# to the visibility owner so — while shadow — only the named reviewer sees it.
	if visibility_owner and frappe.db.get_value("Jarvis Dashboard", dashboard, "owner") != visibility_owner:
		frappe.db.set_value(
			"Jarvis Dashboard",
			dashboard,
			{"owner": visibility_owner, "target_user": visibility_owner},
			update_modified=False,
		)
	# Skip the commit under FrappeTestCase so the enclosing transaction rolls back at
	# teardown (no leaked Dashboard/Run rows); same-connection asserts still see the
	# uncommitted writes.
	if not frappe.flags.in_test:
		frappe.db.commit()

	return {
		"run": run_doc.name,
		"dashboard": dashboard,
		"title": frappe.db.get_value("Jarvis Dashboard", dashboard, "dashboard_title"),
	}
