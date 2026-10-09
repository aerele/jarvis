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


# The body of every pushed custom skill. The container holds a skill's name and
# description, so the assistant's catalog lists it; the instructions are served by the
# bench at the moment of use, where "enabled" and who may use the skill are checked.
# The two quoted answers are ``tools.get_skill``'s own, for a skill that is gone or
# is not this user's.
_POINTER_BODY = (
	"This skill's instructions are kept on the site, not in this file.\n"
	'Call the jarvis__get_skill tool with skill_name "{slug}" and follow the instructions it returns.\n'
	'If it answers "unknown skill" or "no access to skill", this skill is not available here: '
	"carry on without it, and say so only if the user asked for it by name. "
	"Any other error means it could not be loaded: say so."
)


def render_skill_md(skill_name: str, description: str, user_invocable: bool) -> str:
	"""Build the SKILL.md text the container is sent for a custom skill: YAML
	frontmatter, and a pointer to ``jarvis__get_skill`` in place of the instructions
	(``_POINTER_BODY``).

	Frontmatter matches the shared persona skills (name / description /
	user-invocable). ``name`` uses the PREFIXED slug.
	"""
	slug = prefixed_slug(skill_name)
	lines = [
		"---",
		f"name: {slug}",
		f"description: {_yaml_quote(description)}",
		f"user-invocable: {'true' if user_invocable else 'false'}",
		"---",
		"",
		_POINTER_BODY.format(slug=slug),
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
	"""The context-line clause for skills that apply to this user and whose
	instructions are NOT in the container: every custom skill (a pushed one's file
	only points at the tool) and a role-restricted learned one, kept off the
	shared, role-blind blob. The agent's only way to read one is the bench tool
	named here, which re-derives the caller's roles at fetch time.

	``lead_in`` is the caller's half-sentence (what these skills ARE); the tail is
	fixed so every caller issues the same instruction. Shared by
	:func:`invoked_skill_clause` (issue #477) and :func:`learned_skill_clause`
	(issue #479). Deliberately says nothing about the workspace or a skill
	directory: for most of these there is none, and where there is one it holds
	no instructions.
	"""
	names = list(names or [])
	if not names:
		return ""
	return (
		f"; {lead_in}: {', '.join(names)} "
		"- call the jarvis__get_skill tool with each of those names to read its "
		"instructions, then follow them"
	)


def invoked_skill_slugs(message: str, *, user: str, include_org_wide: bool = True) -> set[str]:
	"""Return the bare ``/slug`` tokens in ``message`` that resolve to a custom
	skill ``user`` may invoke: owned by ``user``, shared with ``user``,
	reachable via a role ``user`` holds, or (when ``include_org_wide``) an
	unrestricted Org-wide skill (the ``matched`` set
	:func:`invoked_skill_clause` folds into its context clause).

	PURE identity parameter, not ambient session: resolved entirely under the
	passed ``user``, never ``frappe.session.user``. A caller that must reason
	about an explicit identity other than whoever is logged in right now (e.g.
	the sender of a specific past message, which can differ from the exec user
	a background continuation runs under) gets a truthful answer either way.
	This is the single source of truth for "which skill(s) did this message
	invoke" — no prose-clause parsing required.

	``include_org_wide=False`` (code review on #580 round 2) drops the
	org-wide tier below: :func:`jarvis.chat.feature_usage._skills` uses it so
	a mention of a globally-pushed Org skill - invocable by literally every
	user, personalization the mentioning user never set up - does not count
	as THIS user having used the "Skills" feature for the usage pulse survey.
	The context-clause/offer callers (:func:`invoked_skill_clause`,
	:func:`armed_skill_clause`) keep the default ``True``: for THOSE, an
	Org-wide skill genuinely is invocable and must be named/offered."""
	if not message or "/" not in message:
		return set()
	slugs = {s.lower() for s in _INVOKE_RE.findall(message)}
	if not slugs:
		return set()
	# Only skills `user` OWNS or was SHARED with can be invoked by slug — so a
	# skill shared with specific people isn't triggerable by others (even though
	# it lives in the customer's shared container).
	enabled = {
		r.skill_name
		for r in frappe.get_all(
			"Jarvis Custom Skill", filters={"enabled": 1, "owner": user}, fields=["skill_name"]
		)
	}
	shared_names = [
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Share",
			filters={"user": user, "parenttype": "Jarvis Custom Skill"},
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
	enabled |= _role_scoped_invocable_names(user)
	# Unrestricted Org rows (issue #580): an Org-scope skill with no allowed_roles
	# is named in EVERY container (:func:`_pushable_org_rows`, the same set
	# build_push_payload writes) and is invocable by ANY user - unlike a
	# private/shared/role row it needs no per-user ownership check. Before this, invoked_skill_slugs only recognised owner/share/role
	# rows, so a promoted-to-Org skill's `/slug` was silently unmatched for
	# everyone but its (system) owner and its "Approve & run" arm could never be
	# offered - the exact symptom of #580.
	if include_org_wide:
		enabled |= {r.skill_name for r in _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS)}
	return {s for s in slugs if s in enabled}


def resolve_armed_skill_docname(slug: str, owner: str) -> str | None:
	"""The Jarvis Custom Skill row "Approve & run" is offered on when ``owner``
	invokes ``/slug``, or ``None`` (skill "Approve & run the plan", design §3.3
	rule 4).

	It is the row the fetch would serve them (:func:`jarvis.tools.get_skill.served`:
	a reviewed Role/Org row, else their own), and only when that row's
	``allow_approve_run``, read live, is 1. So at the moment of the offer, the row
	offered on and the row whose instructions a fetch returns are the same one: an
	arm on any other row of the name counts for nothing, and a private original
	left behind by a promotion neither shadows nor stands in for the reviewed copy.
	(Later in the run the gate re-reads that row and stops the run if it was
	disarmed or switched off: ``jarvis.api._run_tool``.)

	``None`` too when the served row is neither reviewed nor the owner's own and
	other rows share the name: which of several private skills shared with someone a
	name means is not defined, and an undefined choice cannot authorize a run."""
	from jarvis.chat.skill_permissions import reviewer_locked
	from jarvis.exceptions import JarvisError
	from jarvis.tools.get_skill import served, usable_rows

	try:
		rows = usable_rows(slug, owner, audit=False)
	except JarvisError:
		return None
	row = served(rows, owner)
	if len(rows) > 1 and not (reviewer_locked(row) or row.owner == owner):
		return None
	armed = frappe.db.get_value("Jarvis Custom Skill", row.name, "allow_approve_run")
	return row.name if frappe.utils.cint(armed) else None


def invoked_skill_clause(message: str, user: str | None = None, *, slugs: set[str] | None = None) -> str:
	"""Return the context-line clause(s) for any enabled custom skills the user
	invoked via ``/slug`` in ``message``, or ``""`` if none match.

	``user`` defaults to ``frappe.session.user`` (unchanged call shape for
	existing callers/tests); ``slugs``, when given, is the ALREADY-RESOLVED
	:func:`invoked_skill_slugs` set - the turn-assembly caller resolves it
	once under the chat user and passes it here AND to
	:func:`armed_skill_clause` (code review on #580), so a turn that folds
	both clauses pays for one table scan and can never have them disagree on
	who "the user" is. Passing ``slugs`` makes ``message``/``user`` inert for
	resolution (kept for the docstring's identity note and so a stale caller
	that ignores the new params still behaves).

	Every matched skill - resolved fresh under ``user`` when ``slugs`` is not
	given - gets the same fetch-by-tool clause (:func:`fetch_required_clause`):
	no custom skill's instructions are in the container. A company skill's file
	there holds only a pointer (:func:`render_skill_md`), and a role-restricted,
	Role-scope or private skill has no file at all (issue #477). The agent reads
	the instructions through ``jarvis__get_skill``, which checks at that moment
	that the skill is enabled and that this user may use it.

	The clause is folded INTO the worker's leading ``[Context: ...]`` line,
	which the persona's AGENTS.md tells the agent to treat as system, not user.
	"""
	user = user if user is not None else frappe.session.user
	matched = sorted(slugs if slugs is not None else invoked_skill_slugs(message, user=user))
	if not matched:
		return ""
	return fetch_required_clause(
		"the user invoked these skills, which are not loaded in this session",
		[prefixed_slug(s) for s in matched],
	)


def armed_skill_clause(message: str, user: str, *, slugs: set[str] | None = None) -> str:
	"""Return a context clause when ``message`` invokes EXACTLY ONE custom
	skill that resolves as live-ARMED for "Approve & run"
	(:func:`resolve_armed_skill_docname`), or ``""`` otherwise.

	Issue #580 (the real-world cause, found on live e2e): the persona's own
	AGENTS.md always stages a create/update as a client-authored jarvis-action
	draft card, never a direct tool call - and a card never reaches the
	write-confirmation gate, so a skill armed for "Approve & run" whose FIRST
	covered write is a create/update could never offer the run-wide approval,
	even though it is armed. This clause tells the agent to stage that first
	covered write as a direct tool call instead, so the bench parks it and can
	show the offer. It is silent once a run is already approved
	(``conv.skill_autorun``) - the caller gates on that, mirroring
	``autorun_run`` above - and pure identity-plus-message, no side effects.

	``slugs``, when given, is the ALREADY-RESOLVED :func:`invoked_skill_slugs`
	set for ``message``/``user`` (code review on #580): the turn-assembly
	caller resolves it once and passes the SAME set here and to
	:func:`invoked_skill_clause`, so a turn folding both clauses pays for one
	table scan and both clauses key off the identical identity.

	Slug-only, like :func:`invoked_skill_clause`: ``skill_name`` is validated
	slug syntax at creation (``SLUG_RE`` - lowercase letters/digits/hyphens),
	so it carries no free-form user text and needs no extra escaping."""
	slugs = slugs if slugs is not None else invoked_skill_slugs(message, user=user)
	if len(slugs) != 1:
		return ""
	slug = next(iter(slugs))
	if not resolve_armed_skill_docname(slug, user):
		return ""
	return (
		f"; armed skill: Approve & run available for {prefixed_slug(slug)}: stage its "
		"first covered write as a direct tool call, not a jarvis-action card, so the "
		"user can approve the whole run once"
	)


def role_scoped_skill_rows(user: str, fields: list[str]) -> list:
	"""Enabled, non-managed skills whose (non-empty) allowed_roles intersect ``user``'s
	roles, projected onto ``fields``.

	The ONE definition of the Role-scope match. Invocation
	(:func:`_role_scoped_invocable_names`) and discovery
	(``custom_skills_api.list_custom_skills``) both read it, because #628 was precisely
	those two disagreeing: ``/slug`` fired for a Role-promoted skill while the composer
	dropdown never listed it, so the feature only worked for someone who already knew
	the slug existed.

	One cached role lookup + two indexed queries; no per-skill N+1 (plan section 6.6)."""
	from jarvis.learning.roles import roles_for_user

	user_roles = roles_for_user(user)
	if not user_roles:
		return []
	parents = {
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Allowed Role",
			filters={"parenttype": "Jarvis Custom Skill", "role": ["in", list(user_roles)]},
			fields=["parent"],
		)
	}
	if not parents:
		return []
	return frappe.get_all(
		"Jarvis Custom Skill",
		filters={"name": ["in", list(parents)], "enabled": 1, "managed_by_learning": 0},
		fields=fields,
	)


def _role_scoped_invocable_names(user: str) -> set[str]:
	"""Bare slugs of the skills :func:`role_scoped_skill_rows` matches."""
	return {r.skill_name for r in role_scoped_skill_rows(user, ["skill_name"])}


def learned_skill_clause(user: str | None = None) -> str:
	"""Context-line clause naming the role-matched learned skills to apply this
	turn (plan section 6.6 - the reliable deterministic activation path).

	Enabled ``managed_by_learning`` skills whose ``allowed_roles`` the chat user
	satisfies (empty = everyone; System Manager / Administrator always pass) are
	folded into the leading ``[Context: ...]`` line as ``learned-<domain>`` — the
	dedicated learned-namespace wire slug (Phase 2). Portal users (desk_access=0
	roles) never intersect desk-role allowed_roles, so learned skills
	self-suppress for them at this layer.

	TWO clause shapes (issue #479): a role-restricted managed row is NOT pushed into the shared,
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
	count = cache.get_value(key, expires=True)
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


# What the push sends of a skill. Not its instructions: those are fetched at use.
_PUSHABLE_FIELDS = ("name", "skill_name", "description", "user_invocable")
# The light, identity-only projection: enough to rank/name a pushable row.
# Every per-turn caller (invoked_skill_slugs, apply_would_push) requests EXACTLY
# this tuple so they all hit the same request-scoped memo below instead of each
# re-scanning the table.
_PUSHABLE_LIGHT_FIELDS = ("name", "skill_name")
_PUSHABLE_ORG_ROWS_MEMO = "_jarvis_pushable_org_rows_light"


def _pushable_org_rows(owner: str | None = None, fields: tuple = _PUSHABLE_FIELDS) -> list:
	"""The enabled Org rows that WOULD be pushed to the shared container, in the
	exact eligibility set + ``skill_name asc`` order :func:`build_push_payload`
	renders — MINUS the ``MAX_SKILLS_PER_PUSH`` cap. Single source of truth shared
	by :func:`build_push_payload`, :func:`pushable_org_skill_count`,
	:func:`apply_would_push` and :func:`project_org_promotion_push`, so the
	reviewer's budget projection can never drift from what Apply actually does.
	``owner`` scopes tests only; ``fields`` trims the projection for callers that
	only need identity (``name`` + ``skill_name`` are load-bearing here: the
	role-restriction filter and the sort key both read them).

	The default-scope, ``_PUSHABLE_LIGHT_FIELDS`` call (every per-turn caller -
	a turn can call ``invoked_skill_slugs`` and ``apply_would_push`` more than
	once) is memoized on ``frappe.local`` for
	the rest of THIS request: one table scan instead of one per caller. Scoped
	narrowly - only that exact, hot-path signature is cached; a caller passing
	``owner`` (tests only) or the heavier ``_PUSHABLE_FIELDS`` (an explicit push,
	not a per-turn read) always scans fresh. The memo is cleared whenever a
	Jarvis Custom Skill row is written (see the doctype's ``on_update``/
	``on_trash``), so a skill created or switched on mid-request is still seen by
	a later call in the SAME request."""
	memoize = owner is None and fields == _PUSHABLE_LIGHT_FIELDS
	if memoize:
		cached = getattr(frappe.local, _PUSHABLE_ORG_ROWS_MEMO, None)
		if cached is not None:
			return cached
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
	# role-scoped skill's name and description are never written to the shared,
	# role-blind container (no skill's instructions are written there any more).
	# Shared with the learned push through :func:`role_restricted_names` (#479).
	restricted = role_restricted_names([r.name for r in rows])
	kept = [r for r in rows if r.name not in restricted]
	# One explicit deterministic comparator AFTER filtering (R2-SP-4): the DB
	# ``order_by`` above is only a stable baseline — the authoritative rank both
	# the payload and the projection consume is this Python sort, so the two can
	# never name different dropped skills across the push cap.
	kept.sort(key=lambda r: _pushable_sort_key(r.skill_name, r.name))
	if memoize:
		setattr(frappe.local, _PUSHABLE_ORG_ROWS_MEMO, kept)
	return kept


def _clear_pushable_org_rows_memo() -> None:
	"""Drop the request-scoped :func:`_pushable_org_rows` memo. Called from the
	Jarvis Custom Skill doctype's ``on_update``/``on_trash`` (mirroring
	``_clear_personal_clause_cache``'s pattern) so a skill created, enabled or
	disabled mid-request - a promotion approval, an admin toggle - is visible to
	the very next call in the SAME request rather than a stale cached scan."""
	try:
		if hasattr(frappe.local, _PUSHABLE_ORG_ROWS_MEMO):
			delattr(frappe.local, _PUSHABLE_ORG_ROWS_MEMO)
	except Exception:
		pass


# Fields _pushable_org_rows's query filters or projects (issue #580 code
# review round 2): a raw write to any of these never fires the doctype's
# on_update/on_trash - the ONLY other place the memo is cleared - so it must
# route through set_skill_raw below instead of a bare frappe.db.set_value, or
# a scan cached before the write silently outlives it for the rest of the
# request. `owner`/`source_skill`/`allow_approve_run` etc. are NOT in this set:
# they don't feed _pushable_org_rows's filter or light projection (the arm is
# always read live, off the row itself), so a raw write to them alone cannot
# desync it.
_PUSHABLE_ORG_ROWS_MEMO_FIELDS = frozenset({"enabled", "scope", "managed_by_learning"})


def set_skill_raw(name: str, field_or_values, value=None, *, update_modified: bool = False) -> None:
	"""Raw (validate()-bypassing) write to Jarvis Custom Skill ``name``,
	clearing the :func:`_pushable_org_rows` memo when the write touches a
	field the memo's query reads (see ``_PUSHABLE_ORG_ROWS_MEMO_FIELDS``).
	Mirrors ``frappe.db.set_value``'s two call shapes - a single
	``(field, value)`` pair, or a ``{field: value}`` dict for several fields
	at once - so every raw write to enable/scope on this doctype (an admin
	kill-switch, a test fixture, any FUTURE such site) can go through here
	instead of hand-duplicating the memo-clear at each call site."""
	values = field_or_values if isinstance(field_or_values, dict) else {field_or_values: value}
	frappe.db.set_value("Jarvis Custom Skill", name, values, update_modified=update_modified)
	if _PUSHABLE_ORG_ROWS_MEMO_FIELDS & set(values):
		_clear_pushable_org_rows_memo()


def build_push_payload(
	owner: str | None = None, strict: bool = False, *, log_truncation: bool = True
) -> list[dict]:
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

	Role-restricted skills are kept off the shared blob (security review PART 2
	TASK 11): an Org row narrowed by ``allowed_roles`` is a role-restricted skill,
	and the container is shared and role-BLIND, so anything written there (today
	its name and description; before pointer bodies, its full instructions) is
	shown to every user's agent. So this push carries ONLY skills visible to
	EVERYONE — Org scope with NO ``allowed_roles``. Role-restricted skills stay
	reachable via the role-gated ``jarvis__find_skills`` / ``jarvis__get_skill``
	tools, and :func:`invoked_skill_clause` routes every invoked skill, pushed or
	not, to ``jarvis__get_skill`` (issue #477). Restoring true /slug CONTAINER activation for them still
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
	  truncation is never silent again. ``log_truncation=False`` is for a
	  caller that builds more than once for one push and has already logged
	  (:func:`push_is_over_cap` tells it whether a build did).
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
		if log_truncation:
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
				"body": render_skill_md(r.skill_name, r.description, ui),
			}
		)
	return payload


def apply_would_push(name: str) -> bool:
	"""True when an interactive Apply would succeed AND write skill ``name`` into the
	shared container: the row is in the :func:`_pushable_org_rows` set and that set
	fits ``MAX_SKILLS_PER_PUSH`` (the strict Apply refuses an over-cap catalog
	outright, so asking the client to run one would only fail right after a success
	toast). An insight applied to an Org skill returns this as ``needs_apply`` so the
	client pushes now. (A change to the instructions alone is served at the next
	fetch without a push; the container holds the name and the description.)"""
	rows = _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS)
	return len(rows) <= MAX_SKILLS_PER_PUSH and any(r.name == name for r in rows)


def push_is_over_cap() -> bool:
	"""Whether :func:`build_push_payload` would drop skills past the cap right now."""
	return len(_pushable_org_rows()) > MAX_SKILLS_PER_PUSH


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
