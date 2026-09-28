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
	# is pushed into EVERY container (:func:`_pushable_org_rows`, the same set
	# build_push_payload writes), so it is already loaded and invocable by ANY
	# user - unlike a private/shared/role row it needs no per-user ownership
	# check. Before this, invoked_skill_slugs only recognised owner/share/role
	# rows, so a promoted-to-Org skill's `/slug` was silently unmatched for
	# everyone but its (system) owner and its "Approve & run" arm could never be
	# offered - the exact symptom of #580.
	if include_org_wide:
		enabled |= {r.skill_name for r in _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS)}
	return {s for s in slugs if s in enabled}


_GET_SKILL_CONTENT_FIELDS = ("instructions", "description", "user_invocable")


def _skill_served_content(name: str) -> tuple:
	"""The exact ``(instructions, description, user_invocable)`` triple
	:func:`jarvis.tools.get_skill.get_skill` would serve for row ``name`` - the
	EXECUTABLE content, not the arm state. Used only to compare two rows in a
	promoted lineage; missing row -> an empty/zero triple (never equal to a
	real row's), so a raced delete fails safe."""
	row = frappe.db.get_value("Jarvis Custom Skill", name, list(_GET_SKILL_CONTENT_FIELDS), as_dict=True)
	if not row:
		return ("", "", 0)
	return (row.instructions or "", row.description or "", int(row.user_invocable or 0))


def _supersede_promoted_lineage(own: dict, *pools: dict, sources: dict) -> None:
	"""Collapse a promoted lineage in place: if a row in ``own`` was the SOURCE
	of a shared/role/org-wide row (``source_skill``, stamped by
	:func:`jarvis.chat.custom_skills_api._materialize_promotion`), that
	descendant is the SAME conceptual skill, not a second independent claim -
	it is the copy actually pushed to the container and run, while the private
	original becomes a secondary, usually-stale leftover (issue #580: promotion
	deliberately leaves it intact). Replace the stale own entry with the
	descendant's (name, armed) and drop the descendant from its own pool, so
	precedence and the ambiguity count both see ONE candidate for the lineage,
	not two. The DESCENDANT's own ``allow_approve_run`` is what counts either
	way (code review on #580, security): an armed private source must never
	auto-arm its still-unarmed shared copy, and an armed shared copy must not
	be shadowed by an unarmed private source - promotion never carries the
	flag, only an admin arming the shared copy directly does.

	TOCTOU fail-safe (code review on #580, security round 2):
	:func:`jarvis.tools.get_skill.get_skill` serves the OWNER's OWN row over
	any other match FOR THAT OWNER (its own-wins precedence), regardless of
	what this function decides - and a private row's content is the owner's
	to edit any time, with no reviewer gate. So authorizing an uncarded run
	off the descendant's arm is only safe when the own row's content is
	PROVABLY what get_skill would actually serve for it right now - i.e.
	byte-identical to the descendant's reviewed content. On a mismatch this
	drops BOTH the own row and the descendant for this owner (never lets the
	descendant fall through via a lower tier): get_skill would still serve the
	drifted own row for this owner no matter which docname is returned here,
	so there is no armed docname this owner can safely be offered for this
	slug. A row without a recorded source (an unrelated same-named skill
	elsewhere) is untouched and still shadows/competes exactly as before."""
	for pool in pools:
		for desc_name, src_name in list(sources.items()):
			if desc_name not in pool or src_name not in own:
				continue
			if _skill_served_content(src_name) != _skill_served_content(desc_name):
				del own[src_name]
				pool.pop(desc_name, None)
				continue
			armed = pool.pop(desc_name)
			del own[src_name]
			own[desc_name] = armed


def resolve_armed_skill_docname(slug: str, owner: str) -> str | None:
	"""The single live-ARMED Jarvis Custom Skill row ``owner`` would invoke by
	``/slug``, or ``None`` (skill "Approve & run the plan", design §3.3 rule 4).

	Mirrors :func:`invoked_skill_slugs`'s owner/shared/role/org-wide precedence -
	the ONE definition of that resolution, so the "Approve & run" offer path
	(``jarvis.api._resolve_approve_run_offer``) and skill invocation can no
	longer drift apart the way they had (each used to hand-duplicate this query;
	only :func:`role_scoped_skill_rows` was actually shared). The owner's OWN
	enabled row wins the invocation; failing that, a shared or role-scoped
	enabled row; failing that, an unrestricted Org-wide row (issue #580 - an
	Org-promoted skill has no per-user owner/share/role row at all, so without
	this tier it could never resolve for anyone but the system owner). A
	shared/org-wide row that is a PROMOTED COPY of the owner's own row
	supersedes it instead of competing with it (:func:`_supersede_promoted_lineage`
	- issue #580 again: without this, an admin who arms either half of a
	promoted lineage independently of the other could never get an offer, or
	got a false "ambiguous" refusal when both happened to be armed).
	``allow_approve_run`` is read LIVE off the resolved row and must be 1.
	Fail-safe on ambiguity - a slug can map to several UNRELATED rows across
	owned/shared/role-scoped/org-wide, so ``None`` when the winning tier holds
	2+ rows OR when 2+ ARMED invocable rows exist anywhere (an ambiguous arm
	cannot authorize a run). O(1)-ish: a few indexed reads, no per-skill N+1."""
	# The owner's OWN enabled rows named `slug`.
	own = {
		r.name: int(r.allow_approve_run or 0)
		for r in frappe.get_all(
			"Jarvis Custom Skill",
			filters={"enabled": 1, "owner": owner, "skill_name": slug},
			fields=["name", "allow_approve_run"],
		)
	}
	# Enabled rows SHARED with the owner, or reachable via a role the owner holds,
	# named `slug` (deduped - a row can be both shared and role-scoped). Also
	# tracks each row's `source_skill` (its promotion lineage, if any) for the
	# supersession pass below.
	shared_role: dict[str, int] = {}
	lineage: dict[str, str] = {}
	shared_names = [
		r.parent
		for r in frappe.get_all(
			"Jarvis Custom Skill Share",
			filters={"user": owner, "parenttype": "Jarvis Custom Skill"},
			fields=["parent"],
		)
	]
	if shared_names:
		for r in frappe.get_all(
			"Jarvis Custom Skill",
			filters={"enabled": 1, "name": ["in", shared_names], "skill_name": slug},
			fields=["name", "allow_approve_run", "source_skill"],
		):
			shared_role[r.name] = int(r.allow_approve_run or 0)
			if r.source_skill:
				lineage[r.name] = r.source_skill
	for r in role_scoped_skill_rows(owner, ["name", "skill_name", "allow_approve_run", "source_skill"]):
		if r.skill_name == slug:
			shared_role.setdefault(r.name, int(r.allow_approve_run or 0))
			if r.get("source_skill"):
				lineage.setdefault(r.name, r.source_skill)

	# Unrestricted Org rows named `slug` (issue #580): pushed into EVERY
	# container (:func:`_pushable_org_rows`, the same set build_push_payload
	# writes), so an Org-wide skill is invocable by ANY user - not only its
	# (system) owner. Without this tier, resolve_armed_skill_docname mirrored
	# invoked_skill_slugs's blind spot: a promoted-to-Org skill could never
	# resolve to an armed row for anyone but its owner, so its "Approve & run"
	# offer could never fire. Computed unconditionally (like shared_role above)
	# so the ambiguity guard below still sees every candidate row.
	org_wide: dict[str, int] = {}
	for r in _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS):
		if r.skill_name == slug:
			org_wide[r.name] = int(r.allow_approve_run or 0)
			if r.source_skill:
				lineage.setdefault(r.name, r.source_skill)

	_supersede_promoted_lineage(own, shared_role, org_wide, sources=lineage)

	# Precedence: the owner's own row wins (a promoted descendant now stands in
	# for its stale source, per the supersession pass above); then a shared/role
	# row; an unrestricted Org row (invocable by everyone, so the weakest
	# per-identity claim) is the last fallback.
	tier = own or shared_role or org_wide
	if len(tier) != 1:
		return None  # no candidate, or an ambiguous winning tier -> fail safe
	docname, armed = next(iter(tier.items()))
	if not armed:
		return None  # the row the owner would actually invoke is not armed
	# Global ambiguity guard: refuse if the slug maps to 2+ ARMED invocable rows
	# anywhere (owned + shared + role + org-wide), even across tiers.
	if sum(1 for v in {**org_wide, **shared_role, **own}.values() if v) != 1:
		return None
	return docname


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

	The matched set - resolved fresh under ``user`` when ``slugs`` is not
	given - is turned into the two clause shapes below, because only SOME
	invocable skills physically exist in the container (issue #477):

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
	user = user if user is not None else frappe.session.user
	matched = sorted(slugs if slugs is not None else invoked_skill_slugs(message, user=user))
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


_PUSHABLE_FIELDS = ("name", "skill_name", "description", "user_invocable", "instructions")
# The light, identity-plus-arming projection: enough to rank/name a pushable
# row and resolve its "Approve & run" arm, without dragging every Org skill's
# 20k-char body onto the chat hot path. Every per-turn caller
# (invoked_skill_slugs, resolve_armed_skill_docname, pushed_skill_names,
# apply_would_push) requests EXACTLY this tuple so they all hit the same
# request-scoped memo below instead of each re-scanning the table.
_PUSHABLE_LIGHT_FIELDS = ("name", "skill_name", "allow_approve_run", "source_skill")
_PUSHABLE_ORG_ROWS_MEMO = "_jarvis_pushable_org_rows_light"


def _pushable_org_rows(owner: str | None = None, fields: tuple = _PUSHABLE_FIELDS) -> list:
	"""The enabled Org rows that WOULD be pushed to the shared container, in the
	exact eligibility set + ``skill_name asc`` order :func:`build_push_payload`
	renders — MINUS the ``MAX_SKILLS_PER_PUSH`` cap. Single source of truth shared
	by :func:`build_push_payload`, :func:`pushable_org_skill_count`,
	:func:`pushed_skill_names` and :func:`project_org_promotion_push`, so the
	reviewer's budget projection can never drift from what Apply actually does.
	``owner`` scopes tests only; ``fields`` trims the projection for callers that
	only need identity (``name`` + ``skill_name`` are load-bearing here: the
	role-restriction filter and the sort key both read them).

	The default-scope, ``_PUSHABLE_LIGHT_FIELDS`` call (every per-turn caller -
	a turn can call ``invoked_skill_slugs``, ``resolve_armed_skill_docname`` and
	``pushed_skill_names`` more than once) is memoized on ``frappe.local`` for
	the rest of THIS request: one table scan instead of one per caller. Scoped
	narrowly - only that exact, hot-path signature is cached; a caller passing
	``owner`` (tests only) or the heavier ``_PUSHABLE_FIELDS`` (an explicit push,
	not a per-turn read) always scans fresh. The memo is cleared whenever a
	Jarvis Custom Skill row is written (see the doctype's ``on_update``/
	``on_trash``), so a skill created or armed mid-request is still seen by a
	later call in the SAME request."""
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
	# role-scoped body is never written to the shared, role-blind container.
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
	``_clear_personal_clause_cache``'s pattern) so a skill created, armed, or
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
# request. `owner`/`source_skill`/`skill_name` etc. are NOT in this set: they
# don't feed _pushable_org_rows's filter or light projection, so a raw write
# to them alone cannot desync it.
_PUSHABLE_ORG_ROWS_MEMO_FIELDS = frozenset({"enabled", "scope", "allow_approve_run", "managed_by_learning"})


def set_skill_raw(name: str, field_or_values, value=None, *, update_modified: bool = False) -> None:
	"""Raw (validate()-bypassing) write to Jarvis Custom Skill ``name``,
	clearing the :func:`_pushable_org_rows` memo when the write touches a
	field the memo's query reads (see ``_PUSHABLE_ORG_ROWS_MEMO_FIELDS``).
	Mirrors ``frappe.db.set_value``'s two call shapes - a single
	``(field, value)`` pair, or a ``{field: value}`` dict for several fields
	at once - so every raw write to arm/enable/scope on this doctype (an
	admin kill-switch, a test fixture simulating an already-armed row, any
	FUTURE such site) can go through here instead of hand-duplicating the
	memo-clear at each call site."""
	values = field_or_values if isinstance(field_or_values, dict) else {field_or_values: value}
	frappe.db.set_value("Jarvis Custom Skill", name, values, update_modified=update_modified)
	if _PUSHABLE_ORG_ROWS_MEMO_FIELDS & set(values):
		_clear_pushable_org_rows_memo()


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
	from current DB rows, and the push is a separate job: an Org approval (or an
	insight applied to an Org skill) returns ``needs_apply`` and the reviewer's
	client runs the Apply straight away (see :func:`apply_would_push`). So for the
	~30s that push takes, a newly eligible skill is named as installed while its
	directory does not exist yet. Closing that last window needs per-row
	applied-state tracking, which the bench does not have (the sync status is one
	bench-wide Single)."""
	rows = _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS)
	return {r.skill_name for r in rows[:MAX_SKILLS_PER_PUSH]}


def apply_would_push(name: str) -> bool:
	"""True when an interactive Apply would succeed AND write skill ``name`` into the
	shared container: the row is in the :func:`_pushable_org_rows` set and that set
	fits ``MAX_SKILLS_PER_PUSH`` (the strict Apply refuses an over-cap catalog
	outright, so asking the client to run one would only fail right after a success
	toast). An insight applied to an Org skill returns this as ``needs_apply`` so the
	client pushes now instead of leaving the change out until an unrelated restart."""
	rows = _pushable_org_rows(fields=_PUSHABLE_LIGHT_FIELDS)
	return len(rows) <= MAX_SKILLS_PER_PUSH and any(r.name == name for r in rows)


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
