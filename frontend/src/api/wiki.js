// Wiki API - thin wrappers around `jarvis.chat.wiki.*`, the scope-aware wiki
// endpoints behind the Skills-page "Wiki" tab. Mirrors src/api/learning.js:
// one `call` per endpoint. The list endpoint paginates by page number (page,
// page_length) - useListPage callers map their start offset onto `page` in
// the fetchFn. Wiki wrappers used to live in src/api/voice.js; voice.js
// re-exports `dismissWikiNudge` so ChatView's namespace import keeps working.
import { call } from "frappe-ui";

const WK = "jarvis.chat.wiki.";

// Envelope {rows, total, has_more, page, page_length}; rows carry scope +
// stale/contradiction flags. scope_filter: all | org | role | mine.
// attention=1 keeps only pages needing review (conflicting or stale).
export const listWikiPagesPage = (p = {}) => {
	const args = {
		search: p.search || "",
		page_type: p.page_type || "",
		scope_filter: p.scope_filter || "all",
		attention: p.attention ? 1 : 0,
		archived: p.archived ? 1 : 0,
		page: p.page || 1,
		page_length: p.page_length || 20,
	};
	// plan 08 §6.2: additive, and only sent when there are clauses — this
	// endpoint keeps its bespoke named-parameter shape (it never had a JSON
	// `filters` blob), so nothing else about the call changes.
	if (Array.isArray(p.filters_v2) && p.filters_v2.length) {
		args.filters_v2 = JSON.stringify(p.filters_v2);
	}
	return call(WK + "list_wiki_pages_page", args);
};

// {creatable_scopes, manageable_roles, is_sm, knowledge_language,
//  wiki_lint_last_run_at, wiki_lint_summary} - the caller's capabilities +
// the SM header extras. Any desk user may call it.
export const getWikiCaps = () => call(WK + "get_wiki_caps");

// Full page incl. body_md + the server-computed can_edit/can_archive flags
// the dialog trusts (save/archive re-check the write matrix server-side).
export const getWikiPage = (slug) => call(WK + "get_wiki_page", { slug });

// → {ok: true, slug} | {ok: false, reason} (matrix denial / slug collision).
// target_role only applies to Role scope; the server derives the slug.
export const createWikiPage = (p = {}) =>
	call(WK + "create_wiki_page", {
		title: p.title || "",
		page_type: p.page_type || "",
		scope: p.scope || "Org",
		...(p.target_role ? { target_role: p.target_role } : {}),
		summary: p.summary || "",
		body_md: p.body_md || "",
	});

// patch: any of {body_md, summary, title} - only the provided fields change.
export const saveWikiPage = (slug, patch = {}) => call(WK + "save_wiki_page", { slug, ...patch });
export const archiveWikiPage = (slug) => call(WK + "archive_wiki_page", { slug });
// Undoes an accidental archive (same permission as archiving).
export const restoreWikiPage = (slug) => call(WK + "restore_wiki_page", { slug });
// PERMANENT delete (same authority as archiving; archive is the reversible
// path - callers warn accordingly). The mirror prunes the file on next sync.
export const deleteWikiPage = (slug) => call(WK + "delete_wiki_page", { slug });

// Chat nudge card: "don't ask again in this conversation" (7-day snooze).
export const dismissWikiNudge = (conversation) => call(WK + "dismiss_nudge", { conversation });

// ── Knowledge Graph ───────────────────────────────────────────────────────────
// Caller-scoped {nodes, edges, counts} over ONLY the pages the viewer may see
// (server-enforced isolation); page nodes carry title+summary for client TF-IDF.
export const getWikiGraph = () => call(WK + "get_wiki_graph");
// Curate a [[link]] slug→target, stored out-of-body (durable), idempotent,
// concurrency-safe, permission-checked both ends. → {ok, manual_links, already?}
export const addWikiLink = (slug, targetSlug) =>
	call(WK + "add_wiki_link", { slug, target_slug: targetSlug });
// Measured daily graph totals [{date, pages, links, orphans, ...}] for the
// Evolution tab; [] until the daily snapshot job has recorded a day.
export const getWikiGraphHistory = () => call(WK + "get_wiki_graph_history");

// ── SM-only header extras ─────────────────────────────────────────────────────
export const setKnowledgeLanguage = (value) => call(WK + "set_knowledge_language", { value });
// Queues a FULL org-wiki mirror sync into the agent workspace. → {ok: true}
export const syncWikiMirrorNow = () => call(WK + "sync_wiki_mirror_now");
// Runs the wiki health check synchronously. → {ok, summary} (summary also
// persisted on Jarvis Settings; get_wiki_caps surfaces the last run).
export const runWikiLintNow = () => call(WK + "run_wiki_lint_now");

// ── page promotion (requester side, Skills-area promotion surfacing) ──────────
// Ask a reviewer to widen one of MY OWN User-scope pages to Role/Org visibility.
// Snapshots the body into a Pending request routed to the SAME reviewer queue the
// wiki reviewer side already consumes (its requester side was never wired until
// now). The page is untouched until a reviewer decides. -> { ok, request, page }
export const requestWikiPromotion = ({ page, to_scope, target_role = "", note = "" }) =>
	call(WK + "request_wiki_promotion", { page, to_scope, target_role, note });

// My most-recent promotion request for one page, for the status chip. Owner-
// scoped server-side; `page` is a slug or docname. -> {} | {name, status,
// from_scope, to_scope, target_role, note, reviewer, reviewer_name, decided_at,
// decision_note, created}
export const myWikiPromotion = (page) => call(WK + "my_wiki_promotion", { page });
