"""``jarvis__record_app_wiki`` — the Custom App Learning scribe's wiki writeback
(audited, NOT gated).

Mirrors ``jarvis.tools.record_agent_run`` (api.py:501-508 precedent): it is a
``_WRITE_TOOLS`` member deliberately EXCLUDED from ``_GATED_WRITES`` — a detached
delegate turn has nobody to click a confirmation card, and the pages ARE the
agent's declared output, so it is audited but never parked. It does NOT use the
gated ``jarvis__update_wiki`` card path (which blocks an unattended run).

It resolves the run from the caller's session_key (never a model id), then
applies pages SERVER-SIDE through ``jarvis.chat.wiki.apply_extracted_page_updates``
— reusing that funnel's controller sanitizer (``_sanitize_untrusted_text``, incl.
the ``jarvis__`` -> ``jarvis-`` defang), Org scoping, provenance and page caps.

Namespace integrity (all SERVER-SIDE, never model-trusted):
  * every page MUST resolve to a real learnable custom app that is ALSO in this
    run's admin-authorized selection (``assert_custom_app`` — allowlist + CX5-2
    run scope); a page without one is rejected;
  * the slug is FORCED app-prefixed (``<app>-<key>``, keyed on a STABLE per-page
    ``key`` so a re-run reconciles deterministically) — the model never supplies
    the namespace prefix;
  * the funnel lookup is FENCED to this agent's own provenance
    (``PROVENANCE_KIND_PREFIX``), so a scribe can create/update only pages of its
    OWN provenance and can NEVER overwrite a human-authored Org page even on a
    slug collision;
  * CX5-4 OUTBOUND GUARD — the bodies are composed from UNTRUSTED source text, so
    a page carrying the run's own session bearer, a Jarvis session header, or a
    credential-shaped value is REFUSED (counted as rejected) and the run's
    ``coverage_note`` is stamped "review required: outbound guard tripped". The
    rest of the batch still lands.
A per-run page cap (15) bounds a run — metered by the DURABLE ``pages_written``
tally on the run row, read under that row's lock (CX5-3), so a cache fault can
never reset the cap. Truncation is disclosed as a coverage caveat on the last
written page and stamped on the run.
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source_receipts, source_guard
from jarvis.tools import _app_learning_ctx as ctx

# Body cap matches the wiki controller's MAX_BODY_LEN; the funnel clips too, but
# clipping here keeps the write predictable.
_BODY_CLIP = 20000

RUN = "Jarvis Agent Run"

# The provenance stamp every NEW scribe page carries in its ``sources[].kind``
# (the audit label): ``app-learning-agent:<app>``.
PROVENANCE_KIND_PREFIX = "app-learning-agent:"
# The funnel FENCE key: apply_extracted_page_updates only updates a page that
# already carries a kind with this prefix. It matches ANY app-learning
# provenance — the new scribe stamp (``app-learning-agent:``) AND the RETIRED
# chat-batch pipeline's stamp (``app-learning:<app>``) — so a re-run can refresh
# a page either produced (a migrated org's pages included), but a scribe can
# NEVER clobber a human-authored page: no human/other kind
# (chat/voice/edit/manual/promotion/tool) begins with ``app-learning``.
PROVENANCE_FENCE_PREFIX = "app-learning"


def _slugify(text: str) -> str:
	return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _as_list(value) -> list:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except Exception:
			return []
	return value if isinstance(value, list) else []


def record_app_wiki(pages=None, app: str | None = None) -> dict:
	"""Land the scribe's composed wiki pages for a custom app.

	Args (from the delegate):
	  pages: list of ``{title, page_type, body_md, mode, key?, app?}``.
	    ``page_type`` is one of the 9 wiki PAGE_TYPES (defaults to ``Process``);
	    ``mode`` is ``"create"`` (default, full body) or ``"append"`` (add a
	    section to an existing page). ``key`` is a STABLE per-page identifier
	    (e.g. ``overview``, ``doctype-gate-pass``, ``process-credit-approval``)
	    reused verbatim across runs so re-runs UPDATE the same page in place; it
	    falls back to the title only when omitted. Each page MUST resolve to a
	    learnable custom app; its slug is FORCED to ``<app>-<key>`` server-side.
	    Pages beyond the per-run cap are dropped and disclosed.
	  app: default app for pages that omit their own ``app`` (so the delegate can
	    call once per app). Every page's resolved app is validated against the
	    custom-apps allowlist; a page with no valid app is rejected.

	Returns ``{run, applied, failed, rejected, guard_violations, pages_remaining,
	truncated, slugs}``.
	"""
	from jarvis.chat.wiki import (
		MAX_PAGES_PER_NOTE,
		PAGE_TYPES,
		apply_extracted_page_updates,
	)

	run = ctx.resolve_scribe_run()  # self-gate: scribe run + admin-tier or raise
	run_name = run["name"]
	session_key = run["session_key"]
	default_app = (app or "").strip()

	raw = _as_list(pages)
	# Lock and validate the run-owned authority BEFORE a model-authored page is
	# accepted. The receipt is not a tool argument: every citation is checked
	# against this server-held row under the same lock as the wiki write/tally.
	locked = ctx.lock_run_budget(
		run_name,
		[
			"pages_written",
			"source_selection_json",
			"permission_profile",
			"source_receipt_json",
		],
	)
	already = int(locked.get("pages_written") or 0)
	if locked.get("status") != "running":
		return {
			"run": run_name,
			"applied": 0,
			"failed": 0,
			"rejected": 0,
			"guard_violations": [],
			"pages_remaining": max(0, ctx.PER_RUN_PAGE_CAP - already),
			"truncated": False,
			"slugs": [],
			"idempotent": 0,
		}
	selection, profile_digest = ctx.source_selection_from_row(locked, run_name)
	# Each accepted page is kept as a flat entry {app, slug, title, update} in
	# arrival order, so an outcome can be mapped back to the EXACT page it wrote
	# (see the writeback loop). ``update`` carries the funnel payload; app/slug/
	# title are the summary/tally shape.
	accepted: list[dict] = []
	rejected = 0
	guard_tripped: list[str] = []
	idempotent = 0
	batch_keys: set[str] = set()
	receipt = None
	for item in raw:
		if not isinstance(item, dict):
			continue
		title = " ".join(str(item.get("title") or "").split())[:140]
		body = str(item.get("body_md") or "").strip()[:_BODY_CLIP]
		if not title or not body:
			continue
		# CX5-4 OUTBOUND GUARD: the delegate composes these bodies from UNTRUSTED source
		# text, so a hostile app can try to steer it into publishing the run's own bearer
		# or a credential it read into the ORG-VISIBLE wiki. Any such page is REFUSED
		# (counted as rejected, exactly like an invalid app) and the run is flagged for
		# review; the rest of the batch still lands.
		violations = source_guard.outbound_violations(body, session_key)
		if violations:
			rejected += 1
			guard_tripped.extend(violations)
			continue
		# REQUIRE + VALIDATE a resolved custom app per page (never model-trusted):
		# a page with no valid learnable custom app — or one outside THIS RUN's
		# admin-authorized selection (CX5-2) — is rejected outright, so the writeback
		# can never namespace a page onto an app the admin did not consent to.
		page_app = (str(item.get("app") or "").strip() or default_app).strip()
		try:
			ctx.assert_custom_app(page_app, run_name)
		except InvalidArgumentError:
			rejected += 1
			continue
		page_type = str(item.get("page_type") or "").strip()
		if page_type not in PAGE_TYPES:
			page_type = "Process"  # app knowledge is process-shaped by default
		# FORCE the slug app-prefixed SERVER-SIDE, keyed on a STABLE per-page key
		# (title only as a fallback) so a re-run reconciles the same page.
		page_key = _slugify(item.get("key") or "") or _slugify(title)
		slug = _slugify(f"{page_app} {page_key}")
		if not slug:
			rejected += 1
			continue
		try:
			page_receipt, source_paths, evidence_digest, caveats = app_source_receipts.verify_page_sources(
				locked,
				run_name=run_name,
				app=page_app,
				source_paths=item.get("source_paths"),
				selection=selection,
				profile_digest=profile_digest,
			)
			if receipt is None:
				receipt = page_receipt
		except InvalidArgumentError:
			rejected += 1
			continue
		mode = "append" if str(item.get("mode") or "").strip().lower() == "append" else "create"
		body_digest = app_source_receipts.page_body_digest(
			app=page_app,
			stable_key=page_key,
			title=title,
			page_type=page_type,
			mode=mode,
			body=body,
			source_paths=source_paths,
			evidence_digest=evidence_digest,
		)
		write_key = app_source_receipts.page_write_key(page_app, page_key)
		if write_key in batch_keys:
			rejected += 1
			continue
		batch_keys.add(write_key)
		try:
			if app_source_receipts.retry_state(
				page_receipt,
				app=page_app,
				stable_key=page_key,
				body_digest=body_digest,
				evidence_digest=evidence_digest,
			):
				idempotent += 1
				continue
		except InvalidArgumentError:
			rejected += 1
			continue
		attestation = app_source_receipts.coverage_attestation(len(source_paths), caveats)
		body = (body[: max(0, _BODY_CLIP - len(attestation))] + attestation).strip()
		update = {"slug": slug, "title": title, "page_type": page_type, "scope": "Org"}
		if mode == "append":
			update["append_md"] = body
		else:
			update["body_md"] = body
		accepted.append(
			{
				"app": page_app,
				"slug": slug,
				"title": title,
				"stable_key": page_key,
				"body_digest": body_digest,
				"evidence_digest": evidence_digest,
				"update": update,
			}
		)

	total = len(accepted)

	# Per-run page cap: bound how many pages one run may write. Read UNDER the run-row lock
	# (above) so ``already`` reflects any prior writeback for this run that committed before
	# we acquired the lock — that writeback's ``_tally_run`` increment is durable by the time
	# the prior holder released the row. A re-run is a NEW run row (fresh counter), so
	# re-learning the same app is not penalised — the cap only stops one run flooding the wiki.
	remaining = max(0, ctx.PER_RUN_PAGE_CAP - already)
	truncated = total > remaining
	dropped = 0
	if truncated:
		dropped = total - remaining
		# Keep the FIRST ``remaining`` pages in order; drop the tail.
		accepted = accepted[:remaining]
		# Disclose the truncation on the last surviving page so the wiki itself is
		# honest about partial coverage (reuse the coverage-caveat discipline).
		if accepted:
			last = accepted[-1]["update"]
			key = "append_md" if "append_md" in last else "body_md"
			marker = f"\n\n_Partial coverage: {dropped} further page(s) exceeded the per-run cap and were not written._"
			last[key] = (last[key] + marker)[:_BODY_CLIP]

	# Group by app + opaque server evidence digest. The provenance records that the
	# page came from a verified source snapshot without exposing a source path or
	# source text in the Org wiki.
	by_app: dict[tuple[str, str], list[dict]] = {}
	for entry in accepted:
		by_app.setdefault((entry["app"], entry["evidence_digest"]), []).append(entry)

	# Apply through the funnel and record ONLY the pages actually written: the
	# per-update outcome is zipped back to its accepted entry, so a refused
	# collision in the middle of a batch is counted as failed and never recorded
	# as a page this run wrote (the tally feeds the completion summary).
	applied = 0
	failed = 0
	applied_pages: list[dict] = []
	applied_entries: list[dict] = []
	for (page_app, evidence_digest), entries in by_app.items():
		for i in range(0, len(entries), MAX_PAGES_PER_NOTE):
			chunk = entries[i : i + MAX_PAGES_PER_NOTE]
			outcomes = apply_extracted_page_updates(
				[e["update"] for e in chunk],
				source=f"{PROVENANCE_KIND_PREFIX}{page_app}:receipt:{evidence_digest[:16]}",
				user=frappe.session.user,
				ref=run["name"],
				provenance_prefix=PROVENANCE_FENCE_PREFIX,
				return_outcomes=True,
				# The signed receipt time is a server-owned generation boundary. The
				# funnel checks it again under the target page lock, so this older
				# run cannot replace a page a later run has already refreshed.
				provenance_not_after=receipt["issued_at"],
			)
			for entry, outcome in zip(chunk, outcomes, strict=True):
				if outcome["ok"]:
					applied += 1
					applied_entries.append(entry)
					applied_pages.append(
						{"app": entry["app"], "slug": entry["slug"], "title": entry["title"]}
					)
				else:
					failed += 1

	# CA3-3 (now structural): the page budget IS the durable ``pages_written`` tally that
	# ``_tally_run`` advances below, in the SAME transaction as the pages themselves — so a
	# rollback (a failed tally / audit / commit) un-spends the budget with the pages rather
	# than leaking it, and a retry sees the FULL remaining capacity. The separate
	# after-commit cache increment this replaced could diverge from the pages it metered.
	_tally_run(run_name, applied_pages, truncated, dropped, guard_tripped=bool(guard_tripped))
	if applied_entries:
		# Persist exact page-write markers in the same transaction as page/tally.
		# A duplicate callback may then report an idempotent result, but it can never
		# overwrite a page a later run has refreshed.
		for entry in applied_entries:
			app_source_receipts.record_page_write(
				receipt,
				app=entry["app"],
				stable_key=entry["stable_key"],
				slug=entry["slug"],
				body_digest=entry["body_digest"],
				evidence_digest=entry["evidence_digest"],
			)
		frappe.db.set_value(
			RUN,
			run_name,
			"source_receipt_json",
			app_source_receipts.encode_receipt(receipt),
			update_modified=False,
		)
	return {
		"run": run_name,
		"applied": applied,
		"failed": failed,
		"rejected": rejected,
		# CX5-4: which outbound-guard rules a refused page tripped, so the delegate is
		# told WHY rather than silently retrying the same body.
		"guard_violations": sorted(set(guard_tripped)),
		# ``remaining`` was read under the lock and ``applied`` pages were just tallied onto
		# the same row, so this is the capacity the run really has left once we commit.
		"pages_remaining": max(0, remaining - applied),
		"truncated": truncated,
		"slugs": [m["slug"] for m in applied_pages],
		"idempotent": idempotent,
	}


def _tally_run(
	run_name: str,
	applied_pages: list[dict],
	truncated: bool,
	dropped: int,
	guard_tripped: bool = False,
) -> None:
	"""Accumulate this call's written pages onto the run row so the Runs tab can show
	"N pages written" with links WITHOUT any findings shape, and so a successful run has
	its tally even before the terminal ``finish_app_learning_run`` flips its status.

	CA2-3 — MANDATORY + ATOMIC + PROPAGATING: the run row is LOCKED (``for_update``) and
	the tally is read-modify-written in the SAME transaction as the page writes (both
	commit together at request end), so a committed page is never reported applied
	without a durable tally. Any persistence failure (lock timeout / schema skew /
	transient) is PROPAGATED — no longer swallowed — so the request rolls the pages back
	too and retries, rather than leaving committed pages with no tally. The tally is ALSO
	recoverable from page provenance: every written page carries a ``sources`` entry
	stamped with this run id (``ref``), so ``reconcile_pages_written`` can rebuild the
	count if a direct tally were ever lost. Only a parse error on a corrupt existing
	``pages_json`` is tolerated (it resets, never aborts)."""
	if not applied_pages and not truncated and not guard_tripped:
		return
	cur = (
		frappe.db.get_value(
			RUN, run_name, ["status", "pages_written", "pages_json"], as_dict=True, for_update=True
		)
		or {}
	)
	# CA3-1: NEVER tally onto a terminal run. ``record_app_wiki`` holds the run-row lock from
	# before the first page write and verified ``running``, so this is defense-in-depth: if
	# the row is somehow terminal (cancelled / reaped / completed), REFUSE the tally (no-op)
	# rather than stamp pages onto it — a successful run is never reported failed and a
	# terminal run is never overwritten.
	if (cur.get("status") or "") != "running":
		return
	try:
		existing = json.loads(cur.get("pages_json") or "[]")
	except Exception:
		existing = []
	if not isinstance(existing, list):
		existing = []
	seen = {p.get("slug") for p in existing if isinstance(p, dict)}
	for m in applied_pages:
		if m["slug"] not in seen:
			existing.append({"slug": m["slug"], "title": m["title"]})
			seen.add(m["slug"])
	values = {
		"pages_written": int(cur.get("pages_written") or 0) + len(applied_pages),
		"pages_json": frappe.as_json(existing[: ctx.PER_RUN_PAGE_CAP])[:60000],
	}
	notes: list[str] = []
	# CX5-4: a tripped outbound guard is stamped FIRST — it is the fact a human must
	# act on (a page tried to publish a credential / the run's own bearer), and the
	# 140-char column must never drop it in favour of the truncation note.
	if guard_tripped:
		notes.append("review required: outbound guard tripped")
	if truncated and dropped:
		notes.append(f"partial coverage: {dropped} page(s) exceeded the per-run cap")
	if notes:
		values["coverage_note"] = "; ".join(notes)[:140]
	frappe.db.set_value(RUN, run_name, values, update_modified=False)


def reconcile_run_pages(run_name: str) -> dict | None:
	"""Rebuild a run's written-page TALLY (count + ``[{slug, title}]``) from PAGE
	PROVENANCE (CA2-3 recovery + CA3-4 durable-tally persistence).

	Every page ``record_app_wiki`` wrote carries a ``sources`` entry stamped with this
	run's id (``ref``) under an ``app-learning*`` kind, so the tally is recoverable from
	the pages themselves even if the direct tally write were ever lost. Returns
	``{"count": int, "pages": [{"slug", "title"}]}``.

	Returns ``None`` when the provenance QUERY ITSELF FAILED — the count is then UNKNOWN,
	and a caller MUST NOT treat that as "zero pages" (failing/completing a run on a false
	zero would either mislabel a success or drop its real pages; the caller leaves the run
	running to retry). Bounded + best-effort: scans only Active pages whose ``sources``
	mention the (unique, hash-shaped) run id and verifies the provenance in Python."""
	if not run_name:
		return {"count": 0, "pages": []}
	from jarvis.chat.wiki import WIKI

	try:
		rows = frappe.get_all(
			WIKI,
			filters={"status": "Active", "sources": ["like", f"%{run_name}%"]},
			fields=["title", "slug", "sources"],
			limit_page_length=ctx.PER_RUN_PAGE_CAP + 5,
		)
	except Exception:
		return None  # UNKNOWN — the provenance query failed; never a false zero
	pages: list[dict] = []
	for r in rows:
		try:
			sources = json.loads(r.sources or "[]")
		except Exception:
			continue
		if not isinstance(sources, list):
			continue
		if any(
			isinstance(s, dict)
			and s.get("ref") == run_name
			and str(s.get("kind") or "").startswith(PROVENANCE_FENCE_PREFIX)
			for s in sources
		):
			pages.append({"slug": r.slug, "title": r.title})
	return {"count": len(pages), "pages": pages}


def reconcile_pages_written(run_name: str) -> int:
	"""The reconciled written-page COUNT (``reconcile_run_pages`` count, or 0 on a query
	failure — preserving this helper's historical "0 on error" contract for callers that
	only need a number). The stale-run reaper uses ``reconcile_run_pages`` directly so it
	can distinguish an honest zero from an UNKNOWN (query failure)."""
	meta = reconcile_run_pages(run_name)
	return int((meta or {}).get("count") or 0)
