// Agents-page API additions (DESIGN-V3 §8.4). `src/api.js` is frozen - new
// endpoints get thin wrappers in per-feature modules under src/api/.
import { call } from "frappe-ui";

// §8.3 - one agent's listing + THIS owner's installation (or null) for the
// detail page. -> { ...listing fields, allowed_roles, allowed: 0|1,
//   installation: {name, enabled, installed_version, config, schedule_*,
//   next_run_at, last_run_at, sync_status} | null, install_count: int,
//   all_roles: [str] (present only for System Manager - the Admin-tab signal) }
export const getAgent = (agent_slug) => call("jarvis.chat.agents_api.get_agent", { agent_slug });

// ── Paginated agent lists (envelope {rows, total, has_more, start, page_length}) ─
// These take a tab/category/sort shape (not api.js `_page`'s filters/sort_field
// pair), matching the marketplace mental model: a tab strip, a category select,
// and a single sort choice. Page components wrap useListPage with an adapter
// fetchFn that maps its ({search, filters, sort_field, ...}) call onto these.
const AG = "jarvis.chat.agents_api.";

// PART 3 remediation — lightweight capability probe. `review` (skill-reviewer
// set: Jarvis Skill Reviewer | Jarvis Admin | System Manager) is what
// apply_agents() actually requires, so it drives the Apply-catalog button —
// decoupled from the SM-only get_agent_admin_overview cross-owner admin data.
// -> { review: 0|1, admin: 0|1 }
export const getAgentsCaps = () => call(AG + "get_agents_caps");

// tab: featured|available|installed · sort: installs|updated|name
export const listAgentsPage = (p = {}) =>
	call(AG + "list_agents_page", {
		tab: p.tab || "available",
		category: p.category || "",
		sort: p.sort || "installs",
		search: p.search || "",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Owner-scoped runs for one agent (the two-pane Runs rail). sort: recent.
export const listRunsPage = (p = {}) =>
	call(AG + "list_runs_page", {
		agent: p.agent || "",
		status: p.status || "",
		search: p.search || "",
		sort: p.sort || "recent",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Owner-scoped activity feed (install/uninstall/enable/disable/run events).
export const listAgentActivityPage = (p = {}) =>
	call(AG + "list_agent_activity_page", {
		agent: p.agent || "",
		action: p.action || "",
		search: p.search || "",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Seed a new conversation from a finding and land the user in live chat.
// -> { ok, conversation, run_id, reason }
export const takeFindingToChat = (finding) => call(AG + "take_finding_to_chat", { finding });

// #1061/#1062 operator stop: terminalize a RUNNING run early (idempotent on an
// already-terminal one). -> { ok, status, idempotent? }
export const stopAgentRun = (run) => call(AG + "stop_agent_run", { run });

// ── PP-4 shadow -> live activation (jarvis#456) ──────────────────────────────
// get_agent's `installation` shape is frozen (§8.3) and two in-flight PRs
// (#620/#612) are independently editing agents_api.py/agent_runs.py, so this
// reads the extra fields via frappe.client.get_value (core Frappe, whitelisted)
// instead of widening that method. It is permission-scoped exactly like every
// other read here: get_value's filters go through get_list, which applies the
// DocType's `if_owner` permission condition, so a caller only ever gets their
// own installation row back.
export const getInstallationActivation = (installation) =>
	call("frappe.client.get_value", {
		doctype: "Jarvis Agent Installation",
		filters: installation,
		fieldname: JSON.stringify([
			"activation_state",
			"reviewer",
			"run_as_user",
			"promoted_by",
			"promoted_at",
		]),
	});

// Reviewer sign-off / kill switch. See agents_api.{promote,demote}_installation
// for the authority + PP-6 budget checks this wraps.
export const promoteInstallation = (installation, justification) =>
	call(AG + "promote_installation", { installation, justification: justification || "" });
export const demoteInstallation = (installation, reason) =>
	call(AG + "demote_installation", { installation, reason: reason || "" });
