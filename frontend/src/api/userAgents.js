// "My agents" API wrappers - thin bindings around `jarvis.chat.user_agents_api.*`
// (src/api.js is frozen - new endpoints get per-feature modules under src/api/).
// Every write answers with the Agent shape documented at the top of
// jarvis/chat/user_agents_api.py; list_user_agents answers
// {enabled, max_agents, agents: [Agent]} and is the only call that works while
// the site flag is off (it says `enabled: false` instead of refusing).
import { call } from "frappe-ui";

const UA = "jarvis.chat.user_agents_api.";

export const listUserAgents = () => call(UA + "list_user_agents");

// -> {max_tools, groups: [{app, label, tools: [{id, label, kind: "read"|"write"}]}]}
export const listPickableTools = () => call(UA + "list_pickable_tools");

// The browser fields, only when the caller has them (the site offers a browser).
function browserArgs(p) {
	if (p.allow_browser === undefined) return {};
	return {
		allow_browser: p.allow_browser ? 1 : 0,
		browser_mode: p.browser_mode || "listed",
		browser_sites: p.browser_sites || [],
	};
}

export const createUserAgent = (p) =>
	call(UA + "create_user_agent", {
		agent_name: p.agent_name,
		instructions: p.instructions,
		description: p.description || "",
		tools: p.tools || [],
		allow_shell: p.allow_shell ? 1 : 0,
		...browserArgs(p),
	});

// Only the keys passed change on the server, so the caller sends the definition
// fields it edited and nothing else.
export const updateUserAgent = (name, p) =>
	call(UA + "update_user_agent", {
		name,
		agent_name: p.agent_name,
		instructions: p.instructions,
		description: p.description || "",
		tools: p.tools || [],
		allow_shell: p.allow_shell ? 1 : 0,
		...browserArgs(p),
	});

export const deleteUserAgent = (name) => call(UA + "delete_user_agent", { name });

export const setUserAgentEnabled = (name, enabled) =>
	call(UA + "set_user_agent_enabled", { name, enabled: enabled ? 1 : 0 });

// p: {enabled, frequency, time, weekday, day_of_month, prompt}. A weekday or a
// day of month is sent only for the frequency that uses it.
export const setUserAgentSchedule = (name, p) =>
	call(UA + "set_user_agent_schedule", {
		name,
		schedule_enabled: p.enabled ? 1 : 0,
		schedule_frequency: p.frequency,
		schedule_time: p.time || "",
		schedule_weekday: p.frequency === "weekly" ? p.weekday || "" : "",
		schedule_day_of_month: p.frequency === "monthly" ? Number(p.day_of_month) || 0 : 0,
		schedule_prompt: p.prompt || "",
	});

// -> {conversation, agent_id, agent_name}
export const startUserAgentChat = (name) => call(UA + "start_user_agent_chat", { name });

// -> {conversation, run_id}
export const runUserAgentNow = (name, prompt) =>
	call(UA + "run_user_agent_now", { name, ...(prompt ? { prompt } : {}) });

export const retryUserAgentSync = (name) => call(UA + "retry_user_agent_sync", { name });
