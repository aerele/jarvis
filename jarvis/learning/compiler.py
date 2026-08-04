"""Domain-skill compilation + materialization (plan sections 6.2, 6.3, 13 Q5).

Approved/Active learned patterns are consolidated into at most six Administrator-
owned ``Jarvis Custom Skill`` rows - ``learned-selling/buying/stock/accounts/
projects/org``. Since the Phase-2 learned namespace the WIRE is the dedicated
learned-skills chain (``build_learned_push_payload`` ->
``jarvis.chat.learned_skills_api`` -> admin ``push_learned_skills`` -> fleet
``PUT /learned-skills``), with slug ``learned-<domain>`` verbatim (NO ``custom-``
prefix) and its own <=10 cap (``LEARNED_SKILL_CAP``). The managed rows REMAIN
``Jarvis Custom Skill`` rows as bench-side storage (role semantics stay
bench-side - plan 13 Q5); ``build_push_payload`` excludes them, so they no
longer count against the customer's 25 custom cap.

CUTOVER (one-time): before the namespace, learned bundles rode the custom push
as ``custom-learned-<domain>`` dirs in the container's custom_skills dir. The
first learned apply on a bench that actually RAN Phase 1 (detected by an empty
``learned_skills_sync_status`` PLUS pre-existing managed rows - fresh Phase-2
tenants have nothing to clean and skip the cutover entirely) CHAINS a custom-
skills reconcile behind the learned push: the learned worker, only after its
push is confirmed ok, stamps ``custom_skills_sync_status`` pending and
enqueues the GRACEFUL (strict=False) custom worker, whose full reconcile - now
excluding managed rows - deletes those stale dirs. Never the strict
interactive ``apply_custom_skills``: a >25-custom-skill bench must truncate
and log, not fail the learned Apply. Chaining also means the two container
restarts run strictly one after the other. See the CUTOVER MARKER comment in
``_apply_learned_skills_locked`` for the exact status stamps and guarantees
the board relies on. Steady-state applies enqueue only the learned push.

PHASE-1 BODY RULE (owner decision, portal-chat finding): only ``effective_
sensitivity == "A"`` (org-level) patterns compile into the PUSHED body. Any
party-named bullet (effective B/C) is insight-only in Phase 1 and never reaches
the shared container. A domain with no A-class survivors compiles to nothing and
its managed row is deleted on apply.

Body template is deterministic (plan section 6.3): ASCII, ``->`` not arrows, no
statistical jargon, the File Box / OCR carve-out, the Interplay clause, JLP refs
for traceability, and "K known exceptions (see board)" (A-class never names an
exception party). Every interpolated value passes the sanitizer.

ACTIVATION TIMING TRADEOFF (plan section 6.2): confirming a container push
before flipping Approved -> Active needs poll/callback wiring the Phase-1 apply
does not have, so patterns flip to Active at ENQUEUE time (right after the
deduped push job is queued). A failed push therefore leaves Active patterns
whose text has not yet reached the container; the next apply re-pushes and the
learned-skills sync status surfaces the failure. Accepted for Phase 1.
"""

from __future__ import annotations

import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from jarvis.learning.sanitizer import (
	safe_value,
	sanitize_value,
	scan_instruction_injection,
)

JLP = "Jarvis Learned Pattern"
JLP_ROLE = "Jarvis Learned Pattern Role"
SKILL = "Jarvis Custom Skill"
SKILL_ALLOWED_ROLE = "Jarvis Custom Skill Allowed Role"

MANAGED_OWNER = "Administrator"
DOMAINS = ("selling", "buying", "stock", "accounts", "projects", "org")

# Compiled-content caps (plan section 6.3).
MAX_BODY = 18000
MAX_DESC = 500
MAX_BULLET = 400
# Mirrors the fleet's MAX_LEARNED_SKILL_FILES / admin's _MAX_LEARNED_SKILL_FILES
# (plan 13 Q5: dedicated learned namespace, cap 10): the apply pre-check gate.
# Managed rows no longer ride the custom push, so the 25 custom cap is NOT
# checked here any more.
LEARNED_SKILL_CAP = 10
# Rough per-bullet overhead (section headers, newline) reserved during budgeting.
_BULLET_MARGIN = 60

# Apply single-flight + un-approve TOCTOU guard (plan section 6.2). The redis
# lock serializes concurrent Applies; the cache marker is what
# ``learned_api.unapprove_learned_pattern`` reads to refuse an un-approve while a
# compile -> flip is in flight (a status re-read alone loses the race).
_APPLY_LOCK = "jarvis_learned_apply"
_APPLY_LOCK_TTL = 600
_APPLY_MARKER = "jarvis:learned_apply_in_progress"

_BAND_RANK = {"High": 0, "Medium": 1, "Low": 2}

_DOMAIN_NOUN = {
	"selling": "selling",
	"buying": "buying",
	"stock": "stock",
	"accounts": "accounts",
	"projects": "projects",
	"org": "org-wide",
}

# When an A-class pattern's antecedent is org-level (no entity name to front-
# load), the description borrows a short topic label from the template instead.
_TOPIC_BY_TEMPLATE = {
	"quotation-validity": "quotation validity",
	"tc-letterhead": "terms and letterhead",
	"pi-update-stock": "update-stock on purchase invoices",
	"default-vs-usage": "default vs actual usage",
	"naming-series": "naming series",
	"selective-item-pricing": "customer-specific pricing",
	"group-payment-terms": "group payment terms",
	"mode-of-payment": "modes of payment",
	"itemgroup-warehouse": "item-group warehouses",
	"stock-entry-route": "stock-entry routes",
	"billing-method": "project billing methods",
}

# Control chars to strip inside a bullet line (whitespace handled via split()).
_LINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# ASCII-fold a few common unicode punctuation glyphs so bodies stay ASCII.
_UNICODE_FOLD = {
	"→": "->",
	"←": "<-",
	"—": "-",
	"–": "-",
	"‘": "'",
	"’": "'",
	"“": '"',
	"”": '"',
	"…": "...",
	" ": " ",
}


def _domain_skill_name(domain: str) -> str:
	return f"learned-{domain}"


# --------------------------------------------------------------------------- #
# compile (read-only; safe for preview)
# --------------------------------------------------------------------------- #
def compile_domain_skills() -> dict:
	"""Group Approved/Active A-class patterns by domain and render one skill spec
	per non-empty domain. Read-only: no writes, no flips (that is apply's job).

	Returns ``{domain: {skill_name, description, body, allowed_roles,
	pattern_names, deferred}}`` for domains with at least one compiled bullet.
	"""
	rows = frappe.get_all(
		JLP,
		filters={"status": ["in", ["Approved", "Active"]], "effective_sensitivity": "A"},
		fields=[
			"name",
			"domain",
			"company",
			"pattern_statement",
			"skill_draft",
			"approved_draft",
			"strength_band",
			"support_n",
			"detector_id",
			"evidence",
			"draft_edited",
			"last_seen_run",
		],
	)
	if not rows:
		return {}

	roles_map = _roles_by_pattern([r["name"] for r in rows])

	by_domain: dict[str, list] = defaultdict(list)
	for r in rows:
		by_domain[r["domain"]].append(r)

	result: dict[str, dict] = {}
	for domain, patterns in by_domain.items():
		patterns.sort(key=_strength_key)
		run_label = _run_label(patterns)
		included, deferred = _select_within_budget(domain, patterns, run_label)
		if not included:
			# A single bullet exceeded the whole budget (degenerate); skip domain.
			continue
		allowed: set[str] = set()
		for p in included:
			allowed |= roles_map.get(p["name"], set())
		result[domain] = {
			"skill_name": _domain_skill_name(domain),
			"description": _description(domain, included),
			"body": _render_body(domain, included, run_label),
			"allowed_roles": sorted(allowed),
			"roles_derived": _roles_derived(included),
			"pattern_names": [p["name"] for p in included],
			"deferred": [p["name"] for p in deferred],
		}
	return result


def _roles_derived(patterns: list) -> bool:
	"""True iff the role derivation ran to a conclusion for EVERY source doctype
	behind ``patterns``.

	Recorded on the managed row as ``roles_derived`` and read by NOTHING (issue
	#479 keeps today's "empty allowed_roles means everyone" reading exactly as it
	is). It exists so a later, stricter policy can tell "we determined this skill
	is universal" from "we could not determine anything" - the two produce an
	identical empty ``allowed_roles`` today (design Q3 cases 1 and 3), and no
	backfill could ever reconstruct which one a given row was. Capturing it now
	makes that policy a one-line change instead of a migration with no data.

	Conservative on every doubt: an unknown detector, a spec with no doctype, or
	any exception all read as NOT derived."""
	try:
		from jarvis.learning.registry import get_detector
		from jarvis.learning.roles import derive_roles_for_doctype

		doctypes = set()
		for p in patterns:
			spec = get_detector(p.get("detector_id") or "") or {}
			doctype = spec.get("doctype")
			if not doctype:
				return False
			doctypes.add(doctype)
		if not doctypes:
			return False
		return all(derive_roles_for_doctype(dt)[1] for dt in sorted(doctypes))
	except Exception:
		return False


def compile_preview(domain: str) -> str:
	"""The rendered body a domain WOULD compile to right now (UI preview). Empty
	string when the domain has no A-class Approved/Active patterns."""
	return (compile_domain_skills().get(domain) or {}).get("body", "")


def preview_bullet(pattern_name: str) -> str:
	"""The single compiled bullet THIS pattern would render into (the drill-down
	"compiled rule preview" - plan section 6.4). Read-only, sensitivity-agnostic:
	B/C rows are insight-only and never reach the pushed body, but the board still
	previews the bullet the SM is deciding on. Empty string when the row is gone."""
	row = frappe.db.get_value(
		JLP,
		pattern_name,
		["name", "domain", "skill_draft", "approved_draft", "pattern_statement"],
		as_dict=True,
	)
	if not row:
		return ""
	return _bullet(row)


# --------------------------------------------------------------------------- #
# apply (writes: upsert managed rows, flip statuses, enqueue the learned push)
# --------------------------------------------------------------------------- #
def apply_learned_skills() -> dict:
	"""Recompile every domain, upsert the <=6 Administrator-owned managed rows
	(deleting emptied domains), flip compiled Approved patterns to Active, then
	enqueue the deduped ``jarvis_learned_skills_push`` job (the dedicated
	learned-namespace chain - which, on the one-time cutover apply, CHAINS a
	graceful custom reconcile after a confirmed-ok push to delete stale
	``custom-learned-*`` dirs). Cap + slug pre-checks run first and abort with
	actionable errors.

	Single-flight: a redis lock serializes concurrent Applies and an
	apply-in-progress marker is set BEFORE compile so ``unapprove_learned_pattern``
	refuses during the compile -> flip window (plan section 6.2 TOCTOU)."""
	from jarvis._redis_lock import redis_lock

	with redis_lock(_APPLY_LOCK, timeout_s=_APPLY_LOCK_TTL, blocking_timeout_s=0) as acquired:
		if not acquired:
			frappe.throw(_("Another Apply is already in progress; wait for it to finish, then apply again."))
		_set_apply_marker(True)
		try:
			return _apply_learned_skills_locked()
		finally:
			_set_apply_marker(False)


def _apply_learned_skills_locked() -> dict:
	compiled = compile_domain_skills()
	_precheck_learned_cap(compiled)
	_precheck_slug_ownership()

	# One-time cutover detection MUST read the pre-apply state: an empty
	# learned_skills sync status (never pushed through the learned namespace)
	# PLUS pre-existing managed rows (this bench actually ran Phase 1) mean the
	# container may still hold pre-namespace ``custom-learned-<domain>`` dirs
	# from the old shared custom push. Read BEFORE the upserts below create
	# this apply's own managed rows.
	cutover = _is_learned_cutover()

	prev_engine = frappe.flags.jarvis_pattern_engine
	applied_domains: list[str] = []
	deleted_domains: list[str] = []
	skill_by_domain: dict[str, str] = {}
	activated = 0
	try:
		# Lift the JLP transition guard for the Approved -> Active flips. Every
		# managed-row write below runs under ignore_permissions and pins its own
		# owner - deliberately NOT via frappe.set_user(). set_user() on a live web
		# request rewrites session.sid and wipes session.data; persisting the
		# request's session at request-end then logs the CALLER out the moment
		# Apply returns (every follow-up call becomes Guest -> 403 wall).
		frappe.flags.jarvis_pattern_engine = True

		existing_managed = {
			r["skill_name"]: r["name"]
			for r in frappe.get_all(SKILL, filters={"managed_by_learning": 1}, fields=["name", "skill_name"])
		}
		for domain, spec in compiled.items():
			skill_by_domain[domain] = _upsert_managed_skill(spec)
			applied_domains.append(domain)

		# Delete managed rows whose domain no longer has any compiled patterns.
		for sname, row_name in existing_managed.items():
			domain = sname[len("learned-") :] if sname.startswith("learned-") else sname
			if domain not in compiled:
				frappe.delete_doc(SKILL, row_name, ignore_permissions=True, force=True)
				deleted_domains.append(domain)

		activated = _finalize_patterns(compiled, skill_by_domain)
		frappe.db.commit()
	finally:
		frappe.flags.jarvis_pattern_engine = prev_engine

	# Push through the DEDICATED learned chain (deduped jarvis_learned_skills_push
	# job; Phase-2 namespace). The worker rebuilds the payload from the managed
	# rows just committed above.
	#
	# CUTOVER MARKER (one-time; the board + other agents rely on these exact
	# status stamps): when ``cutover`` is True the LEARNED WORKER chains the
	# stale-dir custom reconcile - this apply never calls the strict interactive
	# ``apply_custom_skills`` (its strict=True build throws on a >25-custom-
	# skill bench, which must never fail a learned Apply):
	#   1. the enqueue below stamps learned_skills_sync_status='pending:
	#      applying learned skills' (as on every apply); the worker re-stamps it
	#      terminal (ok/failed).
	#   2. ONLY after a confirmed-ok learned push the worker stamps
	#      custom_skills_sync_status='pending: applying skills' and enqueues the
	#      GRACEFUL custom worker (custom_skills_api._enqueued_push_custom_skills,
	#      strict=False: over-cap truncates + logs). The custom worker re-stamps
	#      the custom pair terminal - the same stamps the SPA custom apply uses,
	#      which is what learned_api._cutover_custom_sync_status surfaces.
	# Guarantees: the custom job is enqueued only AFTER the learned push (and
	# its container restart) completed, so the cutover's two restarts can never
	# overlap; if the learned push fails or is skipped, the reconcile is NOT
	# enqueued - the stale custom-learned-* dirs (the OLD guidance) stay live
	# until the next custom apply / restart resync runs its full reconcile
	# (self-healing, fail-safe).
	from jarvis.chat.learned_skills_api import enqueue_learned_skills_push

	sync = enqueue_learned_skills_push(chain_custom_reconcile=cutover)

	return {
		"applied_domains": sorted(applied_domains),
		"deleted_domains": sorted(d for d in deleted_domains if d),
		"activated": activated,
		"skills": skill_by_domain,
		"sync": sync,
		"cutover": cutover,
	}


def _is_learned_cutover() -> bool:
	"""True only when BOTH hold (call BEFORE upserting managed rows):

	1. No learned-namespace push has ever been ATTEMPTED: the enqueue stamps
	   ``learned_skills_sync_status`` (pending) and every worker terminal path
	   re-stamps it (ok/failed), so an empty status == never attempted.
	2. Pre-existing ``managed_by_learning`` rows prove this bench actually ran
	   Phase 1 - i.e. the container may still hold pre-namespace
	   ``custom-learned-*`` dirs that only a custom reconcile can remove. A
	   fresh Phase-2 tenant (no managed rows before its first Apply) has
	   nothing to clean and must NOT pay the extra custom push + restart.

	Keyed off the STATUS field only: ``learned_skills_synced_at`` is a Datetime,
	and a Datetime read back from tabSingles coerces an empty value to a truthy
	``datetime(1,1,1)``. frappe.db.get_value (not get_single_value): the latter
	serves a process-local cache that background status writes do not
	invalidate, and a stale read here would re-fire (or skip) the reconcile."""
	status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "learned_skills_sync_status")
	if (status or "").strip():
		return False
	# Positive Phase-1 evidence: this runs before the apply's upserts, so any
	# managed row seen here predates this apply.
	return bool(frappe.db.exists(SKILL, {"managed_by_learning": 1}))


def _set_apply_marker(on: bool) -> None:
	"""Flip the apply-in-progress cache marker (best-effort; the redis-lock TTL is
	the backstop if a crash skips the clear)."""
	try:
		cache = frappe.cache()
		if on:
			cache.set_value(_APPLY_MARKER, "1", expires_in_sec=_APPLY_LOCK_TTL)
		else:
			cache.delete_value(_APPLY_MARKER)
	except Exception:
		pass


def apply_in_progress() -> bool:
	"""True while an Apply is between compile and its post-flip commit - the
	un-approve TOCTOU window. ``learned_api.unapprove_learned_pattern`` reads it."""
	try:
		return bool(frappe.cache().get_value(_APPLY_MARKER))
	except Exception:
		return False


def _precheck_slug_ownership() -> None:
	"""Abort before any write if a NON-Administrator custom skill already claims a
	``learned-`` slug (the reserved managed-skill prefix). Since the namespace
	cutover the two pushes no longer share a wire (the squatter would ride the
	custom push as ``custom-learned-*``), but a squatting row still shadows the
	managed slug bench-side and confuses the board/clause; belt-and-suspenders
	with the doctype's prefix reservation (plan section 6.2)."""
	rows = frappe.get_all(
		SKILL,
		filters={"skill_name": ["like", "learned-%"], "owner": ["!=", MANAGED_OWNER]},
		fields=["skill_name", "owner"],
	)
	if rows:
		offenders = ", ".join(f"'{r.skill_name}' (owner {r.owner})" for r in rows)
		frappe.throw(
			_(
				"Cannot apply learned skills: the reserved 'learned-' skill-name prefix is "
				"already used by non-managed custom skill(s): {0}. Rename or delete them, then "
				"apply again."
			).format(offenders)
		)


def _precheck_learned_cap(compiled: dict) -> None:
	"""Abort before any write if the compiled domain count would exceed the
	learned namespace's own <=10 fleet cap (plan 13 Q5: pre-check only). Managed
	learned rows no longer ride the custom push, so they do NOT count against -
	and are never counted with - the customer's 25 custom-skill budget."""
	learned_rows = len(compiled)
	if learned_rows > LEARNED_SKILL_CAP:
		frappe.throw(
			_(
				"Cannot apply learned skills: {0} learned domain skill(s) would exceed the "
				"{1}-skill learned-namespace limit for this assistant. Reject or snooze some "
				"approved patterns to drop whole domains, then apply again."
			).format(learned_rows, LEARNED_SKILL_CAP)
		)


def build_learned_push_payload() -> list[dict]:
	"""The fleet push payload alone; see :func:`learned_push_split` for the
	payload PLUS the count of rows deliberately held back."""
	return learned_push_split()[0]


def learned_push_split() -> tuple[list[dict], int]:
	"""``(payload, held_back)``. ``payload`` is the fleet push payload: a list of
	``{slug, description, body}`` (the agent- item shape) where ``slug`` is the
	row's ``skill_name`` verbatim (``learned-<domain>`` - NO ``custom-`` prefix:
	learned skills reconcile into the fleet's separate learned_skills namespace)
	and ``body`` is the rendered SKILL.md whose frontmatter ``name`` matches the
	slug. ``held_back`` counts the managed rows kept OFF that payload because
	they are role-restricted.

	Role-restricted bodies are kept off the shared blob (issue #479), on exactly
	the rule the sibling custom push has applied since security review PART 2
	TASK 11 and through exactly the same helper: a managed row narrowed by
	``allowed_roles`` would otherwise be physically written into the shared,
	role-BLIND container, where any user's agent can ``cat`` it regardless of
	roles. ``_upsert_managed_skill`` writes ``allowed_roles`` on every row it
	compiles, so a tenant whose learned skills all come from role-gated doctypes
	now pushes ZERO files, and that is the normal case rather than an edge one.
	Those rows stay reachable: ``learned_skill_clause`` still announces them to
	role-matched users, and ``jarvis__get_skill`` serves the body after
	re-deriving the caller's roles at fetch time.

	Reads the MANAGED ROWS (not a fresh compile): the rows are the bench-side
	storage the last Apply committed, so a restart resync re-pushes exactly what
	was applied without re-flipping any pattern statuses. Pinned to the
	Administrator owner (same defense-in-depth as ``learned_skill_clause``).
	An empty list is a valid "remove all learned skills" reconcile - which is
	what removes an already-pushed restricted body from a live container.
	Truncated at ``LEARNED_SKILL_CAP`` so a stale over-cap state can never be
	pushed (the apply pre-check is the real gate)."""
	from jarvis.chat.custom_skills import render_learned_skill_md, role_restricted_names

	rows = frappe.get_all(
		SKILL,
		filters={"enabled": 1, "managed_by_learning": 1, "owner": MANAGED_OWNER},
		# ``name`` feeds the role filter; it is not in the payload.
		fields=["name", "skill_name", "description", "instructions"],
		order_by="skill_name asc",
	)
	restricted = role_restricted_names([r.name for r in rows])
	pushable = [r for r in rows if r.name not in restricted]
	payload = []
	for r in pushable[:LEARNED_SKILL_CAP]:
		slug = (r.skill_name or "").strip().lower()
		payload.append(
			{
				"slug": slug,
				"description": (r.description or "")[:MAX_DESC],
				"body": render_learned_skill_md(slug, r.description or "", r.instructions or ""),
			}
		)
	return payload, len(restricted)


def _upsert_managed_skill(spec: dict) -> str:
	"""Create or update one managed ``learned-<domain>`` row. New rows are pinned to
	owner=MANAGED_OWNER (Administrator) so normal users cannot reach them. Returns
	the row name."""
	sname = spec["skill_name"]
	existing = frappe.db.get_value(SKILL, {"managed_by_learning": 1, "skill_name": sname}, "name")
	if existing:
		doc = frappe.get_doc(SKILL, existing)
	else:
		doc = frappe.new_doc(SKILL)
		doc.skill_name = sname

	doc.description = spec["description"]
	doc.instructions = spec["body"]
	doc.user_invocable = 0
	doc.enabled = 1
	doc.managed_by_learning = 1
	# Managed learned rows are org-effect (role-gated at runtime by
	# learned_skill_clause via allowed_roles); pin scope=Org so the new
	# private-first User default (security review PART 2 TASK 10) never applies to
	# them. The engine flag is set here, so the scope guard admits the write.
	doc.scope = "Org"
	doc.set("allowed_roles", [{"role": r} for r in spec["allowed_roles"]])
	# Provenance only, deliberately INERT: nothing reads this field, and an empty
	# allowed_roles still means "everyone" exactly as it did before (#479). See
	# :func:`_roles_derived` for why it is captured at write time regardless.
	doc.roles_derived = 1 if spec.get("roles_derived") else 0
	if existing:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
		# insert() forces owner=session.user (document.py set_user_and_timestamp);
		# pin managed rows to MANAGED_OWNER here instead of via a session switch.
		frappe.db.set_value(SKILL, doc.name, "owner", MANAGED_OWNER, update_modified=False)
	return doc.name


def _finalize_patterns(compiled: dict, skill_by_domain: dict) -> int:
	"""Flip compiled Approved patterns to Active + stamp materialized_skill;
	re-stamp already-Active ones; mark deferred patterns compile_deferred. Runs
	with the engine flag set so the Approved -> Active transition is allowed."""
	activated = 0
	now = now_datetime()
	for domain, spec in compiled.items():
		skill_row = skill_by_domain.get(domain)
		for name in spec["pattern_names"]:
			status = frappe.db.get_value(JLP, name, "status")
			if status == "Approved":
				doc = frappe.get_doc(JLP, name)
				doc.status = "Active"
				doc.materialized_skill = skill_row
				doc.last_validated_at = now
				doc.save(ignore_permissions=True)
				activated += 1
			elif status == "Active":  # refresh the materialized-skill pointer only
				frappe.db.set_value(
					JLP,
					name,
					{"materialized_skill": skill_row, "last_validated_at": now},
					update_modified=False,
				)
			# else: the row is no longer Approved/Active (e.g. un-approved to
			# Proposed inside the apply window) - skip it. Never stamp
			# materialized_skill on a row that is not live in the pushed body.
		for name in spec["deferred"]:
			_mark_compile_deferred(name)
	return activated


def _mark_compile_deferred(name: str) -> None:
	ev = frappe.db.get_value(JLP, name, "evidence")
	try:
		data = frappe.parse_json(ev) if ev else {}
	except Exception:
		data = {}
	if not isinstance(data, dict):
		data = {}
	data["compile_deferred"] = True
	frappe.db.set_value(JLP, name, {"evidence": frappe.as_json(data)}, update_modified=False)


# --------------------------------------------------------------------------- #
# body / description rendering
# --------------------------------------------------------------------------- #
def _render_body(domain: str, patterns: list, run_label: str) -> str:
	noun = _DOMAIN_NOUN.get(domain, domain)
	lines = [
		f"# Learned {noun} habits",
		"",
		f"Scope: defaults mined from this site's history. Last analyzed: {run_label}.",
		"These are learned defaults, not hard rules: apply them silently when they fit; when the",
		"user's request conflicts or you must deviate, say so and confirm with the user.",
		"",
		"## How to use these rules",
		"- Each rule is a default with its evidence and known exceptions.",
		'- Known exceptions are legitimate deviations: do not "correct" them.',
		"- Explicit user instructions always beat a learned default.",
		"- In File Box / OCR / unattended flows: NEVER ask or confirm. Printed document values and",
		"  the ocr-data-entry skill's rules win over any learned default. Escalate doubts to the",
		"  approval board, per the OCR contract.",
		"",
	]

	companies = sorted({(p.get("company") or "") for p in patterns if p.get("company")})
	multi_company = len(companies) > 1

	if not multi_company:
		lines.append("## All roles")
		for p in patterns:
			lines.append(_bullet(p))
	else:
		org_level = [p for p in patterns if not p.get("company")]
		if org_level:
			lines.append("## All roles")
			for p in org_level:
				lines.append(_bullet(p))
			lines.append("")
		for company in companies:
			# safe_value (not sanitize_value): an injection-shaped mined company
			# name is swapped for a neutral placeholder, matching antecedent handling.
			lines.append(f"### Company: {safe_value(company)}")
			for p in patterns:
				if (p.get("company") or "") == company:
					lines.append(_bullet(p))
			lines.append("")

	lines.append("")
	lines.append("## Interplay")
	lines.extend(_interplay_lines(domain))
	return "\n".join(lines).strip() + "\n"


def _interplay_lines(domain: str) -> list[str]:
	if domain == "org":
		lead = "- Persona skills give generic guidance; where this file states a learned default, the"
	else:
		lead = f"- Persona skills (erpnext-{domain}) give generic guidance; where this file states a"
	return [
		lead,
		"  learned default wins - EXCEPT in OCR/File Box flows (above).",
		"- If another custom skill conflicts, follow the custom skill and note the conflict.",
	]


def _bullet(row: dict) -> str:
	"""One compiled bullet: sanitized draft + JLP ref. Injection-shaped drafts
	are withheld with a board pointer rather than embedded (plan section 6.3).

	Source of truth is ``approved_draft`` - the text frozen when a human
	approved it (#482). ``skill_draft`` is the LIVE detector render and is
	rewritten in place by every re-detection, so compiling from it shipped
	wording no one reviewed. Rows with no snapshot (Proposed previews, and
	rows approved before the snapshot existed / before the backfill ran) fall
	back to the live draft, then to the statement."""
	name = row["name"]
	draft = (row.get("approved_draft") or "").strip() or (row.get("skill_draft") or "").strip()
	if not draft:
		draft = "- " + (row.get("pattern_statement") or "").strip()

	if scan_instruction_injection(draft):
		return (
			f"- A learned {row.get('domain') or ''} default ({name}) was withheld because its "
			f"data did not pass the safety scan; review it on the board. ({name})"
		)

	line = _sanitize_bullet_line(draft)
	if not line.startswith("- "):
		line = "- " + line.lstrip("-").strip()
	if name not in line:
		line = f"{line} ({name})"
	if len(line) > MAX_BULLET:
		ref = f" ({name})"
		line = line[: MAX_BULLET - len(ref)].rstrip() + ref
	return line


def _fold_ascii_line(text: str) -> str:
	"""Collapse text to one safe ASCII line: strip control chars, fold known
	unicode punctuation, neutralize stray backticks (so a mined value cannot open
	a dangling code span), collapse whitespace. Entity names (already quoted in
	the draft) are preserved; only structural safety is enforced here."""
	t = _LINE_CONTROL_RE.sub("", text or "")
	for bad, good in _UNICODE_FOLD.items():
		t = t.replace(bad, good)
	t = t.replace("`", "'")
	return " ".join(t.split()).strip()


def _sanitize_bullet_line(text: str) -> str:
	"""One safe ASCII bullet line (shares the fold with the description pass)."""
	return _fold_ascii_line(text)


def _description(domain: str, patterns: list) -> str:
	"""Front-load concrete entity names + doctypes, then the retrieval hint
	(plan section 6.3). Capped at 500 chars; every subject is sanitized."""
	subjects: list[str] = []
	seen: set[str] = set()
	for p in patterns:
		subj = _subject(p)
		if subj and subj.lower() not in seen:
			seen.add(subj.lower())
			subjects.append(subj)
		if len(subjects) >= 6:
			break

	doctypes = _doctypes(patterns)
	noun = _DOMAIN_NOUN.get(domain, domain)
	head = f"Learned {noun} habits for this org"
	if subjects:
		head += ": " + ", ".join(subjects)
	if doctypes:
		head += " - on " + ", ".join(doctypes)
	desc = f"{head}. Use whenever creating, editing or answering questions about {noun} documents."
	# Final structural pass: the whole pushed description is folded to a safe ASCII
	# line (control chars stripped, unicode punctuation folded, backticks
	# neutralized) - the body gets this per bullet; the description did not.
	desc = _fold_ascii_line(desc)
	if len(desc) > MAX_DESC:
		desc = desc[: MAX_DESC - 1].rstrip().rstrip(",") + "."
	return desc[:MAX_DESC]


def _subject(row: dict) -> str:
	spec = _spec(row.get("detector_id"))
	ev = _parse_evidence(row.get("evidence"))
	antecedent = ev.get("antecedent")
	kind = (spec or {}).get("antecedent_kind")
	if antecedent not in (None, "", "org") and kind != "org":
		# safe_value (not sanitize_value): an injection-shaped mined antecedent is
		# swapped for the placeholder, so the DESCRIPTION field is injection-safe
		# just like the body values (plan section 6.3 / 7 T4).
		return safe_value(antecedent)
	topic = _TOPIC_BY_TEMPLATE.get((spec or {}).get("skill_template"))
	return topic or ""


def _doctypes(patterns: list) -> list[str]:
	out: list[str] = []
	seen: set[str] = set()
	for p in patterns:
		spec = _spec(p.get("detector_id"))
		dt = (spec or {}).get("doctype")
		# "DocType" is the internal antecedent for naming-series; not user-facing.
		if dt and dt != "DocType" and dt not in seen:
			seen.add(dt)
			out.append(dt)
		if len(out) >= 4:
			break
	return out


# --------------------------------------------------------------------------- #
# budgeting + helpers
# --------------------------------------------------------------------------- #
def _select_within_budget(domain: str, patterns: list, run_label: str) -> tuple[list, list]:
	"""Greedily include patterns (strongest first) while the rendered body stays
	<= 18k; the rest are deferred (plan section 6.2)."""
	skeleton_len = len(_render_body(domain, [], run_label))
	running = skeleton_len
	included: list = []
	deferred: list = []
	for p in patterns:
		cost = len(_bullet(p)) + 1
		if running + cost + _BULLET_MARGIN <= MAX_BODY:
			included.append(p)
			running += cost
		else:
			deferred.append(p)

	# Safety: if section overhead still tipped us over, shed the weakest included.
	while included and len(_render_body(domain, included, run_label)) > MAX_BODY:
		deferred.append(included.pop())
	return included, deferred


def _strength_key(p: dict) -> tuple:
	return (_BAND_RANK.get(p.get("strength_band"), 3), -(p.get("support_n") or 0), p.get("name"))


def _run_label(patterns: list) -> str:
	runs = sorted(p.get("last_seen_run") for p in patterns if p.get("last_seen_run"))
	run = runs[-1] if runs else None
	date_str = today()
	return f"{date_str} (run {run})" if run else date_str


def _roles_by_pattern(names: list[str]) -> dict:
	roles_map: dict = defaultdict(set)
	if not names:
		return roles_map
	for rr in frappe.get_all(
		JLP_ROLE, filters={"parent": ["in", names], "parenttype": JLP}, fields=["parent", "role"]
	):
		if rr.get("role"):
			roles_map[rr["parent"]].add(rr["role"])
	return roles_map


def _parse_evidence(ev) -> dict:
	if not ev:
		return {}
	if isinstance(ev, dict):
		return ev
	try:
		parsed = frappe.parse_json(ev)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _spec(detector_id):
	if not detector_id:
		return None
	try:
		from jarvis.learning import registry

		return registry.get_detector(detector_id)
	except Exception:
		return None
