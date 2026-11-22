// Pure helpers for the "My agents" page: what a row says about the agent, and the
// create/edit dialog's form, validation and payloads. The server owns the rules
// (jarvis/chat/user_agents_api.py and the Jarvis User Agent controller); these
// only keep the dialog from sending what it already knows will be refused.
import { scheduleLabel } from "@/pages/macros/scheduleLabel";
import { deriveScheduleDay } from "@/lib/scheduleAnchor";

export const FREQUENCY_OPTIONS = [
	{ label: "Daily", value: "daily" },
	{ label: "Weekly", value: "weekly" },
	{ label: "Monthly", value: "monthly" },
];

export const MAX_INSTRUCTIONS = 12000;
// The counter appears only once the text is this close to the limit.
export const NEAR_INSTRUCTIONS = 11000;
export const MAX_PROMPT = 4000;
export const MAX_NAME = 140;
export const MAX_DESCRIPTION = 1000;

// "9:00:00" -> "09:00", the value of a native time input.
export function timeHHMM(s) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(s || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}

// Ready / Syncing / Sync failed / Off. `ready` (the server's own word) is what
// decides whether Chat and Run now work: an edited agent shows Syncing yet can
// still be used, because it was synced once.
export function stateBadge(agent) {
	if (!agent.enabled) return { label: "Off", theme: "gray" };
	const status = (agent.sync && agent.sync.status) || "Pending";
	if (status === "Failed") return { label: "Sync failed", theme: "red" };
	if (status === "Pending") return { label: "Syncing", theme: "orange" };
	return { label: "Ready", theme: "green" };
}

// The sentence the server stored for the last typed sync error, or "".
export function syncMessage(agent) {
	return (agent.sync && agent.sync.message) || "";
}

// Retry is for any enabled agent that is not synced: a failure, a sync the server left
// Pending with a reason (a busy runtime, a rate limit), and a sync job that never ran,
// which has no reason at all. Another send while one is queued does no harm.
export function canRetrySync(agent) {
	if (!agent.enabled) return false;
	return ((agent.sync && agent.sync.status) || "Pending") !== "Synced";
}

export function isSyncing(agent) {
	return (
		!!agent.enabled &&
		((agent.sync && agent.sync.status) || "Pending") === "Pending" &&
		!syncMessage(agent)
	);
}

// "Daily · 9:00 am", "Weekly on Monday · 9:00 am", or "Off".
export function scheduleSummary(agent) {
	const s = agent.schedule || {};
	if (!s.enabled) return "Off";
	return scheduleLabel({
		schedule_frequency: s.frequency,
		schedule_weekday: s.weekday,
		schedule_day_of_month: s.day_of_month,
		schedule_time: s.time,
	});
}

// {status, message} for the Last run cell: Started, Failed (with the stored
// sentence) or nothing yet.
export function lastRun(agent) {
	const r = agent.last_run || {};
	if (r.status === "Failed") return { status: "Failed", message: r.message || "", at: r.at };
	if (r.status === "Started") return { status: "Started", message: "", at: r.at };
	return null;
}

// ── the dialog's form ────────────────────────────────────────────────────────

export function emptyForm() {
	return {
		agent_name: "",
		description: "",
		instructions: "",
		tools: [],
		allow_shell: false,
		schedule_enabled: false,
		frequency: "daily",
		time: "09:00",
		day: "",
		prompt: "",
	};
}

export function formFromAgent(agent) {
	const s = agent.schedule || {};
	const frequency = s.frequency || "daily";
	return {
		agent_name: agent.agent_name || "",
		description: agent.description || "",
		instructions: agent.instructions || "",
		tools: [...(agent.tools || [])],
		allow_shell: !!agent.allow_shell,
		schedule_enabled: !!s.enabled,
		frequency,
		time: timeHHMM(s.time) || "09:00",
		day: deriveScheduleDay(frequency, s.weekday, s.day_of_month),
		prompt: s.prompt || "",
	};
}

// Field errors, keyed by field; {} when the form can be saved.
export function validateForm(form, { maxTools = 64 } = {}) {
	const errors = {};
	const name = (form.agent_name || "").trim();
	if (!name) errors.agent_name = "Give the agent a name.";
	else if (name.length > MAX_NAME)
		errors.agent_name = `Keep the name under ${MAX_NAME} characters.`;
	if ((form.description || "").length > MAX_DESCRIPTION) {
		errors.description = `Keep the description under ${MAX_DESCRIPTION} characters.`;
	}
	const instructions = (form.instructions || "").trim();
	if (!instructions) errors.instructions = "Tell the agent what to do.";
	else if (instructions.length > MAX_INSTRUCTIONS) {
		errors.instructions = `Shorten the instructions to ${MAX_INSTRUCTIONS.toLocaleString(
			"en-US"
		)} characters or fewer.`;
	}
	if ((form.tools || []).length > maxTools) {
		errors.tools = `Pick at most ${maxTools} tools.`;
	}
	const prompt = (form.prompt || "").trim();
	if (form.schedule_enabled && !prompt) {
		errors.prompt = "Write what the agent should do on each run.";
	} else if (prompt.length > MAX_PROMPT) {
		errors.prompt = `Shorten this to ${MAX_PROMPT.toLocaleString(
			"en-US"
		)} characters or fewer.`;
	}
	if (form.schedule_enabled && !form.time) errors.time = "Pick a time.";
	if (form.schedule_enabled && form.frequency !== "daily" && !form.day) {
		errors.day = form.frequency === "weekly" ? "Pick a weekday." : "Pick a day of the month.";
	}
	return errors;
}

// The definition fields. Sending them starts a sync on the server, so the dialog
// only calls update when one of them changed (see definitionChanged).
export function definitionPayload(form) {
	return {
		agent_name: form.agent_name.trim(),
		description: (form.description || "").trim(),
		instructions: form.instructions.trim(),
		tools: [...form.tools].sort(),
		allow_shell: !!form.allow_shell,
	};
}

export function schedulePayload(form) {
	return {
		enabled: !!form.schedule_enabled,
		frequency: form.frequency,
		time: form.time,
		weekday: form.frequency === "weekly" ? form.day : "",
		day_of_month: form.frequency === "monthly" ? Number(form.day) || 0 : 0,
		prompt: (form.prompt || "").trim(),
	};
}

export function definitionChanged(form, agent) {
	const a = definitionPayload(formFromAgent(agent));
	const b = definitionPayload(form);
	return (
		a.agent_name !== b.agent_name ||
		a.description !== b.description ||
		a.instructions !== b.instructions ||
		a.allow_shell !== b.allow_shell ||
		a.tools.join("\n") !== b.tools.join("\n")
	);
}

export function scheduleChanged(form, agent) {
	const a = formFromAgent(agent);
	if (a.prompt.trim() !== (form.prompt || "").trim()) return true;
	if (a.schedule_enabled !== !!form.schedule_enabled) return true;
	// An off schedule keeps whatever frequency it had: only the prompt matters then.
	if (!form.schedule_enabled) return false;
	return a.frequency !== form.frequency || a.time !== form.time || a.day !== form.day;
}
