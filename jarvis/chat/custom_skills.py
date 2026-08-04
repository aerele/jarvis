"""Render Jarvis Custom Skill rows into agent SKILL.md payloads.

A customer authors a bare slug (e.g. ``invoicing``); everywhere it reaches
agent it becomes ``custom-invoicing`` so it can never collide with a shared
persona skill (none of which start with ``custom-``). The push payload is a
list of ``{slug, description, user_invocable, body}`` dicts, where ``body`` is
the fully-rendered SKILL.md text written verbatim to disk by the fleet-agent.

The render helpers are PURE (no frappe calls) so they're unit-testable;
:func:`build_push_payload` is the only frappe-touching function.
"""

import re

import frappe
from frappe import _

# Mirrors the bench-side cap in jarvis_custom_skill.py; re-asserted here so a
# stale/over-cap state can never be pushed.
MAX_SKILLS_PER_PUSH = 25
RESERVED_PREFIX = "custom-"
# Genuine compiled learned rows are ALWAYS Administrator-owned (the compiler owns
# them as Administrator). Pinning the turn-injection query to this owner is
# defense in depth: even if a rogue row somehow carries managed_by_learning=1, it
# is only ever injected into every user's turn when Administrator owns it.
MANAGED_OWNER = "Administrator"

# --------------------------------------------------------------------------- #
# Shared-slug reservation (SR4-2): a row whose ``name`` IS the slug, so two SHARED
# (Role/Org) skills can never carry the same slug regardless of write path or
# concurrency (the second insert fails on the primary key). The controller belt
# ``JarvisCustomSkill._validate_shared_slug_unique`` catches the sequential case
# fast; this is the hard floor for two writes that both pass that belt SELECT
# before either commits.
# --------------------------------------------------------------------------- #
SHARED_SLUG = "Jarvis Shared Skill Slug"


def reserve_shared_slug(slug: str, skill_docname: str) -> None:
	"""Claim the DB-unique reservation (row name == slug) for a shared skill.

	Idempotent for the slug this skill ALREADY holds - the in-place shared UPDATE,
	the insight-apply re-save and the supersede-in-place promotion all re-save without
	renaming, so a legitimate re-save never fails. A slug held by a DIFFERENT shared
	skill is a hard clash, refused here (the controller belt normally catches it
	first) and, for two genuinely concurrent creators that both pass that belt, by the
	primary-key constraint when the second reservation row is inserted."""
	slug = (slug or "").strip().lower()
	if not slug or not skill_docname:
		return
	holder = frappe.db.get_value(SHARED_SLUG, slug, "skill")
	if holder is not None:
		if holder == skill_docname:
			return
		# A reservation whose holder skill no longer exists is stale - a raw/forced
		# delete that bypassed the on_trash release. Reclaim it rather than block a
		# legitimate new shared skill on a ghost; the primary key still serializes two
		# genuinely-concurrent reclaimers (one insert wins, the other fails).
		if frappe.db.exists("Jarvis Custom Skill", holder):
			frappe.throw(
				_("A shared skill named '{0}' already exists; rename one before sharing.").format(slug)
			)
		frappe.delete_doc(SHARED_SLUG, slug, ignore_permissions=True, force=True)
	frappe.get_doc({"doctype": SHARED_SLUG, "slug": slug, "skill": skill_docname}).insert(
		ignore_permissions=True
	)


def release_shared_slug(slug: str, skill_docname: str) -> None:
	"""Release the reservation for ``slug`` IFF ``skill_docname`` currently holds it
	- a shared skill deleted, narrowed back to User, or renamed off this slug. The
	holder check means one lineage never yanks another's reservation (so releasing a
	User skill whose slug happens to shadow a shared one is a safe no-op)."""
	slug = (slug or "").strip().lower()
	if not slug or not skill_docname:
		return
	if frappe.db.get_value(SHARED_SLUG, slug, "skill") == skill_docname:
		frappe.delete_doc(SHARED_SLUG, slug, ignore_permissions=True, force=True)


# Matches a /slug token the user typed in the composer to invoke a skill.
_INVOKE_RE = re.compile(r"(?:^|\s)/([a-z0-9]+(?:-[a-z0-9]+)*)")


def _pushable_sort_key(skill_name: str, docname: str) -> tuple:
	"""The ONE deterministic order the pushable Org set is ranked by — used
	identically by :func:`build_push_payload` (which keeps the first
	``MAX_SKILLS_PER_PUSH``) and :func:`project_org_promotion_push` (which
	simulates the promoted skill joining that ranking). Ordering by a Python
	comparator rather than the DB ``ORDER BY`` collation (CDX-SP-4 / R2-SP-4): the
	DB collation folds hyphens/digits differently from Python, so a DB-ordered
	payload could drop a DIFFERENT skill than a Python-ordered projection names.
	``docname`` (the unique row hash) is the stable tie-breaker, making the order
	a total one so both call sites are provably identical."""
	return ((skill_name or "").lower(), docname or "")


def prefixed_slug(skill_name: str) -> str:
	"""Bare authored slug -> the namespaced slug used on disk and in agent."""
	return f"{RESERVED_PREFIX}{(skill_name or '').strip().lower()}"


def _yaml_quote(s: str) -> str:
	"""Return ``s`` as a safe double-quoted YAML scalar (single logical line).

	Newlines/tabs are folded to spaces so the frontmatter ``description`` stays
	one line; backslashes and double-quotes are escaped.
	"""
	folded = " ".join((s or "").split())
	escaped = folded.replace("\\", "\\\\").replace('"', '\\"')
	return f'"{escaped}"'


def render_skill_md(skill_name: str, description: str, user_invocable: bool, instructions: str) -> str:
	"""Build the full SKILL.md text (YAML frontmatter + markdown body).

	Frontmatter matches the shared persona skills (name / description /
	user-invocable). ``name`` uses the PREFIXED slug.
	"""
	body = (instructions or "").strip()
	lines = [
		"---",
		f"name: {prefixed_slug(skill_name)}",
		f"description: {_yaml_quote(description)}",
		f"user-invocable: {'true' if user_invocable else 'false'}",
		"---",
		"",
		body,
		"",
	]
	return "\n".join(lines)


def render_learned_skill_md(slug: str, description: str, instructions: str) -> str:
	"""The learned-namespace sibling of :func:`render_skill_md` (Behavioural
	Pattern Learning Phase 2). ``slug`` is the FULL wire slug
	(``learned-<domain>`` — never ``custom-`` prefixed: learned skills reconcile
	into the fleet's separate ``learned_skills`` namespace, so the frontmatter
	``name`` must match the on-disk ``learned-<domain>`` dir). Learned skills are
	never user-invocable (they auto-inject via ``learned_skill_clause``)."""
	body = (instructions or "").strip()
	lines = [
		"---",
		f"name: {(slug or '').strip().lower()}",
		f"description: {_yaml_quote(description)}",
		"user-invocable: false",
		"---",
		"",
		body,
		"",
	]
	return "\n".join(lines)


def allowed_roles_by_skill(row_names) -> dict[str, set[str]]:
	"""``{docname: {role, ...}}`` for the rows among ``row_names`` narrowed by an
	``allowed_roles`` child table. Rows carrying NO child row are absent from the
	mapping, so ``name in mapping`` is the definition of "role-restricted" and the
	value is the set to intersect a chat user's roles with.

	Presence, not a non-empty role set: ``user_can_use_skill`` narrows an Org row
	the moment ANY child row exists, so a child carrying a blank role restricts
	the row to nobody. Answering both questions from ONE query is what stops the
	push filter and the context clause from drifting apart - issue #479 is
	exactly that drift, between the custom push and the learned push.
	"""
	names = [n for n in (row_names or []) if n]
	if not names:
		return {}
	out: dict[str, set[str]] = {}
	for row in frappe.get_all(
		"Jarvis Custom Skill Allowed Role",
		filters={"parenttype": "Jarvis Custom Skill", "parent": ["in", names]},
		fields=["parent", "role"],
	):
		bucket = out.setdefault(row.parent, set())
		if row.role:
			bucket.add(row.role)
	return out


def role_restricted_names(row_names) -> set[str]:
	"""Docnames among ``row_names`` that carry at least one ``allowed_roles`` row:
	the ONE definition of "role-restricted" every container push agrees on
	(security review PART 2 TASK 11 / issue #479).

	A role-restricted body may never be written into the shared, role-BLIND
	container, so BOTH pushes drop these rows and let the role-gated
	``jarvis__get_skill`` serve the body instead: the custom push via
	:func:`_pushable_org_rows`, the learned push via
	``jarvis.learning.compiler.build_learned_push_payload``. #479 exists because
	the learned push never had a copy of this filter at all; one shared
	definition means a third push cannot re-introduce the divergence.
	"""
	return set(allowed_roles_by_skill(row_names))


def fetch_required_clause(lead_in: str, names) -> str:
	"""The context-line clause for skills that apply to this user but are NOT
	installed in the container, because their bodies are deliberately kept off
	the shared, role-blind blob. The agent's only way to read one is the bench
	tool named here, which re-derives the caller's roles at fetch time.

	``lead_in`` is the caller's half-sentence (what these skills ARE); the tail is
	fixed so every caller issues the same instruction. Shared by
	:func:`invoked_skill_clause` (issue #477) and :func:`learned_skill_clause`
	(issue #479). Deliberately says nothing about the workspace or a skill
	directory: there is no directory to point at.
	"""
	names = list(names or [])
	if not names:
		return ""
	return (
		f"; {lead_in}: {', '.join(names)} "
		"- call the jarvis__get_skill tool with each of those names to read its "
		"instructions, then follow them"
	)


def invoked_skill_clause(message: str) -> str:
	"""Return the context-line clause(s) for any enabled custom skills the user
	invoked via ``/slug`` in ``message``, or ``""`` if none match.

	TWO clause shapes, because only SOME invocable skills physically exist in the
	container (issue #477):

	* skills the push actually writes (Org scope, no ``allowed_roles``, inside
	  ``MAX_SKILLS_PER_PUSH``, see :func:`pushed_skill_names`) keep the original
	  "apply them" clause: the ``custom-<slug>`` directory really is on disk;
	* everything else the user may invoke is NOT on disk: a role-restricted body
	  TASK 11 deliberately keeps off the shared blob, a Role-scope promotion
	  (excluded from the push outright), a private User skill, or a row past the
	  push cap. Naming those as installed asserted a directory that does not
	  exist, so they get a fetch-by-tool clause instead, pointing the agent at
	  ``jarvis__get_skill`` (which is role-gated and DOES serve the body).

	There is no per-role container mount to push them into (one ``custom_skills``
	dir per container, keyed on container name only), so this degrades the clause
	rather than pretending the file is there.

	The clause is folded INTO the worker's leading ``[Context: ...]`` line,
	which the persona's AGENTS.md tells the agent to treat as system, not user.
	"""
	if not message or "/" not in message:
		return ""
	slugs = {s.lower() for s in _INVOKE_RE.findall(message)}
	if not slugs:
		return ""
	# Only skills the current chat user OWNS or was SHARED with can be invoked by
	# slug — so a skill shared with specific people isn't triggerable by others
	# (even though it lives in the customer's shared container). Auto-pick by
	# description is still bench-global (a container-level limitation).
	me = frappe.session.user
	enabled = {
		r.skill_name
		for r in frappe.get_all(
			"Jarvis Custom Skill", filters={"enabled": 1, "owner": me}, fields=["skill_name"]
		)
	}
	shared_names = [
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Share",
			filters={"user": me, "parenttype": "Jarvis Custom Skill"},
			fields=["parent"],
		)
	]
	if shared_names:
		enabled |= {
			r.skill_name
			for r in frappe.get_all(
				"Jarvis Custom Skill",
				filters={"enabled": 1, "name": ["in", shared_names]},
				fields=["skill_name"],
			)
		}
	# Allowed Roles (plan section 6.6): a skill scoped to a role via allowed_roles
	# is invocable by a matching-role user even without an explicit share. Purely
	# additive - a skill with EMPTY allowed_roles is unchanged (owner/shared only),
	# and managed learned skills are excluded here (they auto-inject, see
	# learned_skill_clause).
	enabled |= _role_scoped_invocable_names(me)
	matched = sorted(s for s in slugs if s in enabled)
	if not matched:
		return ""
	installed = pushed_skill_names()
	on_disk = [s for s in matched if s in installed]
	off_disk = [s for s in matched if s not in installed]
	clause = ""
	if on_disk:
		names = ", ".join(prefixed_slug(s) for s in on_disk)
		clause += f"; the user invoked these skills, apply them: {names}"
	clause += fetch_required_clause(
		"the user invoked these skills, which are not loaded in this session",
		[prefixed_slug(s) for s in off_disk],
	)
	return clause


def _role_scoped_invocable_names(user: str) -> set[str]:
	"""Bare slugs of enabled, non-managed skills whose (non-empty) allowed_roles
	intersect ``user``'s roles. One cached role lookup + two indexed queries; no
	per-skill N+1 (plan section 6.6)."""
	from jarvis.learning.roles import roles_for_user

	user_roles = roles_for_user(user)
	if not user_roles:
		return set()
	parents = {
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Allowed Role",
			filters={"parenttype": "Jarvis Custom Skill", "role": ["in", list(user_roles)]},
			fields=["parent"],
		)
	}
	if not parents:
		return set()
	return {
		r.skill_name
		for r in frappe.get_all(
			"Jarvis Custom Skill",
			filters={"name": ["in", list(parents)], "enabled": 1, "managed_by_learning": 0},
			fields=["skill_name"],
		)
	}


def learned_skill_clause(user: str | None = None) -> str:
	"""Context-line clause naming the role-matched learned skills to apply this
	turn (plan section 6.6 - the reliable deterministic activation path).

	Enabled ``managed_by_learning`` skills whose ``allowed_roles`` the chat user
	satisfies (empty = everyone; System Manager / Administrator always pass) are
	folded into the leading ``[Context: ...]`` line as ``learned-<domain>`` — the
	dedicated learned-namespace wire slug (Phase 2). Portal users (desk_access=0
	roles) never intersect desk-role allowed_roles, so learned skills
	self-suppress for them at this layer.

	TWO clause shapes, for the same reason :func:`invoked_skill_clause` has two
	(issue #479): a role-restricted managed row is NOT pushed into the shared,
	role-blind container, so naming it as installed points the agent at a
	directory that does not exist. The split predicate is exactly the push
	filter's - a row is installed iff it carries no ``allowed_roles`` - so
	discovery and delivery can never disagree about which bodies are on disk.

	Hot path: ONE cached role lookup + two indexed queries, capped at the <=6
	managed rows - no per-skill N+1. The privileged branch pays the child-row
	query too: a System Manager skips the role INTERSECTION, never the emptiness
	test, or they alone would be told a role-restricted skill is on disk.
	"""
	from jarvis.learning.roles import roles_for_user

	user = user or frappe.session.user
	managed = frappe.get_all(
		"Jarvis Custom Skill",
		filters={"managed_by_learning": 1, "enabled": 1, "owner": MANAGED_OWNER},
		fields=["name", "skill_name"],
	)
	if not managed:
		return ""

	user_roles = roles_for_user(user)
	privileged = user == "Administrator" or "System Manager" in user_roles
	roles_by_skill = allowed_roles_by_skill([m.name for m in managed])

	if privileged:
		matched = list(managed)
	else:
		matched = [
			m for m in managed if m.name not in roles_by_skill or (roles_by_skill[m.name] & user_roles)
		]
	if not matched:
		return ""
	# skill_name on a managed row IS the wire slug ("learned-<domain>"): learned
	# skills ship through the dedicated learned_skills namespace, NOT the custom-
	# prefixed custom-skills push, so no RESERVED_PREFIX here.
	installed = sorted(m.skill_name for m in matched if m.name not in roles_by_skill)
	off_disk = sorted(m.skill_name for m in matched if m.name in roles_by_skill)
	clause = ""
	if installed:
		clause += f"; apply these learned skills: {', '.join(installed)}"
	clause += fetch_required_clause(
		"these learned skills apply to you but are not loaded in this session", off_disk
	)
	return clause


PERSONAL_CLAUSE_TTL_S = 300


def personal_skills_cache_key(user: str) -> str:
	return f"jarvis:pskills:{user}"


def personal_skill_clause(user: str | None = None) -> str:
	"""Context-line clause telling the agent the chat user has Personal-scope
	skills saved on the bench. Personal rows are never pushed to the container
	catalog (see :func:`build_push_payload`), so without this hint the model
	has no way to know they exist; it retrieves them via jarvis__find_skills /
	jarvis__get_skill. Redis-cached per-user count (300s; invalidated by the
	DocType controller on any row change) so the hot chat path pays one cache
	read."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return ""
	cache = frappe.cache()
	key = personal_skills_cache_key(user)
	count = cache.get_value(key)
	if count is None:
		# Exact scope match: NULL/empty scope rows are Org and never counted.
		# "User" is the scope-ladder spelling of the old "Personal" (TASK 10).
		count = frappe.db.count(
			"Jarvis Custom Skill",
			{"owner": user, "enabled": 1, "scope": "User"},
		)
		cache.set_value(key, int(count or 0), expires_in_sec=PERSONAL_CLAUSE_TTL_S)
	try:
		count = int(count or 0)
	except (TypeError, ValueError):
		count = 0
	if not count:
		return ""
	# Precedence tag (Skills-area rework, DESIGN.md section 6): personal skills
	# augment ONLY this user's own turns and must yield to org/role guidance on
	# conflict. The turn_handler emits this clause AFTER the org/role/learned/wiki
	# clauses so the ordering reinforces the same rule. Explicit /slug invocation
	# stays intentional (invoked_skill_clause is not demoted).
	return (
		f"; {count} personal skill(s) saved "
		"(applies to you; org guidance takes priority on conflict) "
		"- search with jarvis__find_skills"
	)


_PUSHABLE_FIELDS = ("name", "skill_name", "description", "user_invocable", "instructions")
# The identity-only projection: enough to rank and name a pushable row, without
# dragging every Org skill's 20k-char body onto the chat hot path.
_PUSHABLE_ID_FIELDS = ("name", "skill_name")


def _pushable_org_rows(owner: str | None = None, fields: tuple = _PUSHABLE_FIELDS) -> list:
	"""The enabled Org rows that WOULD be pushed to the shared container, in the
	exact eligibility set + ``skill_name asc`` order :func:`build_push_payload`
	renders — MINUS the ``MAX_SKILLS_PER_PUSH`` cap. Single source of truth shared
	by :func:`build_push_payload`, :func:`pushable_org_skill_count`,
	:func:`pushed_skill_names` and :func:`project_org_promotion_push`, so the
	reviewer's budget projection can never drift from what Apply actually does.
	``owner`` scopes tests only; ``fields`` trims the projection for callers that
	only need identity (``name`` + ``skill_name`` are load-bearing here: the
	role-restriction filter and the sort key both read them)."""
	# ("in", ("Org", "")) — not ("!=", "User") — because db_query wraps the
	# "in" operator in ifnull(scope, ''), so legacy NULL-scope rows match ''.
	filters = {"enabled": 1, "managed_by_learning": 0, "scope": ("in", ("Org", ""))}
	if owner:
		filters["owner"] = owner
	rows = frappe.get_all(
		"Jarvis Custom Skill",
		filters=filters,
		fields=list(fields),
		order_by="skill_name asc",
	)
	# TASK 11: drop role-restricted Org rows (any with allowed_roles) so a
	# role-scoped body is never written to the shared, role-blind container.
	# Shared with the learned push through :func:`role_restricted_names` (#479).
	restricted = role_restricted_names([r.name for r in rows])
	kept = [r for r in rows if r.name not in restricted]
	# One explicit deterministic comparator AFTER filtering (R2-SP-4): the DB
	# ``order_by`` above is only a stable baseline — the authoritative rank both
	# the payload and the projection consume is this Python sort, so the two can
	# never name different dropped skills across the push cap.
	kept.sort(key=lambda r: _pushable_sort_key(r.skill_name, r.name))
	return kept


def build_push_payload(owner: str | None = None, strict: bool = False) -> list[dict]:
	"""Collect the enabled custom skills into the fleet push payload.

	Bench-global by design: a Jarvis bench maps to one customer / one
	container, so ALL enabled rows on the site are pushed (``owner`` is accepted
	only to scope tests). An empty list is a valid "remove all custom skills"
	reconcile.

	User- AND Role-scope rows are EXCLUDED: User rows exist only for their owner
	(reached via the find_skills/get_skill tools) and Role rows only for
	role-holders; neither belongs in the shared container catalog nor may eat
	into the 25-skill push budget. NULL/empty scope (pre-migration rows) means
	Org and IS pushed.

	Role-restricted bodies are kept off the shared blob (security review PART 2
	TASK 11): an Org row narrowed by ``allowed_roles`` is a role-restricted skill
	whose full instructions would otherwise be physically written to the shared,
	role-BLIND container and be readable + auto-activatable by every user's agent
	(a mass-exfil vector). So this push carries ONLY skills visible to EVERYONE —
	Org scope with NO ``allowed_roles``. Role-restricted skills stay reachable via
	the role-gated ``jarvis__find_skills`` / ``jarvis__get_skill`` tools, and
	:func:`invoked_skill_clause` routes an invoked-but-unpushed skill to
	``jarvis__get_skill`` instead of naming a container directory that was never
	written (issue #477). Restoring true /slug CONTAINER activation for them still
	needs a per-role workspace mount in the fleet-agent (a follow-up outside the
	bench).

	Managed learned rows (``managed_by_learning=1``) are EXCLUDED: since the
	Phase-2 learned namespace they ride their own push
	(``jarvis.learning.compiler.build_learned_push_payload`` ->
	``admin_client.post_push_learned_skills``) and must not eat into the
	customer's 25-skill custom budget. Their exclusion here is also what makes
	the first post-cutover custom reconcile delete the stale
	``custom-learned-<domain>`` dirs from the container.

	Over-cap handling (Phase 2, plan 'tenant audit + graceful resync, then
	build_push_payload raise'):

	- ``strict=True`` (interactive callers - a human is present to act):
	  ``frappe.throw`` an actionable error naming the count, the cap and the
	  fix. Nothing is pushed.
	- ``strict=False`` (default; unattended callers - the enqueued push worker
	  and the post-restart resync): truncate to the first
	  ``MAX_SKILLS_PER_PUSH`` rows (``skill_name`` asc, as before) but
	  ``frappe.log_error`` a loud warning naming the dropped slugs, so the
	  truncation is never silent again.
	"""
	rows = _pushable_org_rows(owner)
	if len(rows) > MAX_SKILLS_PER_PUSH:
		if strict:
			frappe.throw(
				_(
					"{0} enabled custom skills exceed the push cap of {1}; "
					"disable {2} or consolidate. Nothing was pushed."
				).format(len(rows), MAX_SKILLS_PER_PUSH, len(rows) - MAX_SKILLS_PER_PUSH)
			)
		dropped = [prefixed_slug(r.skill_name) for r in rows[MAX_SKILLS_PER_PUSH:]]
		preview = ", ".join(dropped[:5]) + (", ..." if len(dropped) > 5 else "")
		frappe.log_error(
			title="Jarvis: custom-skills push truncated",
			message=(
				f"custom-skills push truncated: {len(rows)} enabled, "
				f"{MAX_SKILLS_PER_PUSH} pushed, {len(dropped)} dropped: {preview}"
			),
		)
	payload = []
	for r in rows[:MAX_SKILLS_PER_PUSH]:
		ui = bool(r.user_invocable)
		payload.append(
			{
				"slug": prefixed_slug(r.skill_name),
				"description": r.description or "",
				"user_invocable": ui,
				"body": render_skill_md(r.skill_name, r.description, ui, r.instructions),
			}
		)
	return payload


def pushed_skill_names() -> set[str]:
	"""Bare authored slugs the container push writes: the
	:func:`_pushable_org_rows` eligibility set truncated by the same
	``MAX_SKILLS_PER_PUSH`` cap :func:`build_push_payload` applies.

	Anything OUTSIDE this set has no ``custom-<slug>`` directory in the container,
	so no context clause may tell the agent to apply it as an installed skill
	(issue #477). Identity-only projection so the chat hot path never loads
	instruction bodies.

	Read this as push ELIGIBILITY, not confirmed container state. It is recomputed
	from current DB rows, and Apply is a separate explicit action (see
	``decide_skill_promotion``: an approved skill joins the shared catalog on the
	next Apply, never automatically). So between an Org approval and the operator
	clicking Apply, a newly eligible skill is named as installed while its
	directory does not exist yet. That window is pre-existing and much narrower
	than the unconditional mislabelling this function replaced; closing it needs
	per-row applied-state tracking, which the bench does not have (the sync status
	is one bench-wide Single)."""
	rows = _pushable_org_rows(fields=_PUSHABLE_ID_FIELDS)
	return {r.skill_name for r in rows[:MAX_SKILLS_PER_PUSH]}


def pushable_org_skill_count() -> int:
	"""Count of enabled skills that WOULD be pushed to the shared container — the
	EXACT :func:`build_push_payload` eligibility filter (enabled, not learned,
	scope Org/legacy-empty, minus any ``allowed_roles``-narrowed row), but
	UNCAPPED by ``MAX_SKILLS_PER_PUSH`` so a caller can tell "at the cap" (25)
	apart from "over the cap" (e.g. 30).

	The reviewer promotion UI uses this to warn when approving an Org promotion
	would take the catalog near/past the push budget: an Org row promoted while
	the container already holds ``MAX_SKILLS_PER_PUSH`` pushable skills is
	silently truncated out of the next push (build_push_payload logs it, but the
	reviewer deserves to be told BEFORE approving). Non-blocking — the reviewer
	still decides.
	"""
	return len(_pushable_org_rows())


def project_org_promotion_push(skill_docname: str, skill_name: str) -> dict:
	"""Server-side, single-source-of-truth projection of what the container push
	does AFTER an Org promotion is approved — the honest replacement for the old
	client-side ``count + 1 > budget`` guess (CDX-SP-2).

	Shares :func:`_pushable_org_rows`' exact eligibility + ``skill_name asc``
	ordering + cap with :func:`build_push_payload`, then simulates the promoted
	skill joining the pushable Org set and reports BOTH real Apply behaviours:

	- interactive STRICT Apply raises and pushes NOTHING over the cap
	  (``strict_would_fail``);
	- the unattended sync keeps the first ``MAX_SKILLS_PER_PUSH`` by ``skill_name``
	  and DROPS the ordered tail (``dropped_slugs``) — which may be an EXISTING
	  shared skill rather than the newly promoted one, depending on where the
	  promoted slug sorts (``promoted_dropped`` says which).

	``skill_docname`` identifies the source row so an already-pushable skill
	(idempotent re-approve) is not double-counted; ``skill_name`` is the bare
	authored slug the shared copy carries. Recompute this at approval time — a
	list-load value goes stale under concurrent promotions/edits.
	"""
	rows = _pushable_org_rows()
	entries = [(r.skill_name, r.name) for r in rows]
	promoted_slug = prefixed_slug(skill_name)
	# The promoted skill joins the pushable set unless this exact row is already
	# pushable (re-approve of an already-Org skill). The SAME ``_pushable_sort_key``
	# build_push_payload ranks by is applied here (R2-SP-4), so the projection's
	# dropped tail is provably the payload's dropped tail — never a different skill.
	already = any(name == skill_docname for _, name in entries)
	if not already:
		entries.append((skill_name, skill_docname))
	entries.sort(key=lambda e: _pushable_sort_key(e[0], e[1]))
	projected = [prefixed_slug(sn) for sn, _ in entries]
	budget = MAX_SKILLS_PER_PUSH
	dropped = projected[budget:]
	return {
		"to_scope": "Org",
		"promoted_slug": promoted_slug,
		"projected_count": len(projected),
		"budget": budget,
		"at_budget": len(projected) == budget,
		"over_budget": len(projected) > budget,
		# interactive strict Apply raises + pushes NOTHING when over budget
		"strict_would_fail": len(projected) > budget,
		# unattended sync keeps the first `budget` (skill_name asc), drops the tail
		"dropped_slugs": dropped,
		"promoted_dropped": promoted_slug in dropped,
	}
