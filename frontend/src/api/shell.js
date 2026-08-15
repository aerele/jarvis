// Shell-owned API wrappers (DESIGN-V3 §8.4). `src/api.js` is frozen - new
// endpoints get thin wrappers in per-feature modules under src/api/.
import { call } from "frappe-ui";

// D40 - title-only, paginated, owner-scoped conversation search (⌘K palette).
// -> { rows: [{name, title, starred, last_active_at}], total, has_more, start, page_length }
export const searchConversations = (params = {}) =>
	call("jarvis.chat.api.search_conversations", {
		search: params.search || "",
		start: params.start || 0,
		page_length: params.page_length || 20,
	});

// Full desk search over the caller's Frappe desk (⌘K palette), delegated to
// Frappe's own search. Returns permission-scoped groups — Lists, Reports and
// Pages (frappe.desk.search) plus Records (frappe.utils.global_search) — each
// item carrying a ready /app/... desk route.
// -> { groups: [{key, title, items: [{name, label, icon, suffix, route}]}] }
export const searchWorkspace = (params = {}) =>
	call("jarvis.chat.api.search_workspace", {
		search: params.search || "",
		limit: params.limit || 6,
	});
