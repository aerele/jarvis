import { reactive } from "vue";
import { __ } from "./i18n.js";

// One batched request for all mounted cards, shared by desktop and PWA.
export const exportJobs = reactive({});
const subscribers = new Map();
let timer,
	scheduled,
	pending = null,
	inFlight = false;
// Ordering stamps. A response is applied to an id only if nothing newer (for example the answer
// to a Cancel) was written for it after the request was sent.
let clock = 0;
const written = new Map();

const FALLBACKS = {
	get_jobs: () => __("Could not refresh the export."),
	cancel: () => __("Could not cancel. The export may still be running."),
};

// Frappe errors carry the user-facing text in `_server_messages` (a JSON list of JSON
// objects) or, failing that, in `exception` ("module.Class: text").
function errorText(body) {
	const plain = (text) =>
		String(text || "")
			.replace(/<[^>]*>/g, "")
			.trim();
	try {
		const texts = JSON.parse(body._server_messages).map((m) => plain(JSON.parse(m).message));
		const joined = texts.filter(Boolean).join(" ");
		if (joined) return joined;
	} catch {
		// fall through to the next source
	}
	return plain(String(body?.exception || "").replace(/^[\w.]+:\s*/, "")) || plain(body?.message);
}

async function request(method, args) {
	const response = await fetch(`/api/method/jarvis.tools._export.jobs.${method}`, {
		method: "POST",
		credentials: "same-origin",
		headers: {
			"Content-Type": "application/json",
			"X-Frappe-CSRF-Token": window.csrf_token || window.frappe?.csrf_token || "",
		},
		body: JSON.stringify(args),
	});
	if (!response.ok) {
		const body = await response.json().catch(() => null);
		const error = new Error((body && errorText(body)) || FALLBACKS[method]());
		error.method = method;
		throw error;
	}
	return (await response.json()).message;
}

// Write only for a card that is still mounted. `sent` is the stamp taken when the request that
// produced `value` left; a newer write for the same id wins.
function write(id, value, sent = Infinity) {
	if (!subscribers.has(id) || (written.get(id) || 0) >= sent) return;
	written.set(id, ++clock);
	exportJobs[id] = value;
}

export function formatBytes(bytes) {
	const n = Math.max(0, Number(bytes) || 0);
	if (n < 1024) return `${n} B`;
	const kb = Math.max(1, Math.round(n / 1024));
	return kb < 1024 ? `${kb} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function exportSummary(message) {
	try {
		if (!message?.tool_call_id?.endsWith(":card")) return null;
		const raw = message?.tool_result;
		const result = typeof raw === "string" ? JSON.parse(raw) : raw;
		return result?.ok && result.data?.export_job ? result.data : null;
	} catch {
		return null;
	}
}

// Two queued requests become one: forced if either is, the union of their ids, and "all" (null)
// dominates.
function merged(a, b) {
	if (!a) return b;
	return {
		force: a.force || b.force,
		names: a.names && b.names ? [...new Set([...a.names, ...b.names])] : null,
	};
}

export async function refreshExports(force = true, names = null) {
	if (!subscribers.size) return;
	if (inFlight) {
		// A forced refresh (a socket event, Retry) is never dropped; a routine poll may be.
		if (force) pending = merged(pending, { force, names });
		return;
	}
	if (!force && document.visibilityState === "hidden") return;
	const ids = [...subscribers.keys()].filter((id) => {
		if (names && !names.includes(id)) return false;
		if (force) return true;
		const entry = exportJobs[id];
		const state = entry?.state;
		return (
			state === undefined ||
			entry?.error ||
			["queued", "running", "paused"].includes(state?.status) ||
			(state?.status === "ready" &&
				state.expires_at &&
				Date.parse(state.expires_at) <= Date.now())
		);
	});
	if (!ids.length) return;
	inFlight = true;
	try {
		for (let i = 0; i < ids.length; i += 100) {
			const sent = ++clock;
			const results = await request("get_jobs", { names: ids.slice(i, i + 100) });
			for (const [id, state] of Object.entries(results))
				write(id, { state, error: "" }, sent);
		}
	} catch (error) {
		for (const id of ids)
			if (subscribers.has(id)) exportJobs[id] = { ...exportJobs[id], error: error.message };
	} finally {
		inFlight = false;
		const next = pending;
		pending = null;
		if (next) await refreshExports(next.force, next.names);
	}
}

function onExportEvent(event) {
	if (document.visibilityState === "hidden") return;
	const { kind, export_job: id } = event.detail || {};
	if (id) refreshExports(kind !== "export:update", [id]);
}

function schedule() {
	clearTimeout(scheduled);
	scheduled = setTimeout(refreshExports, 0);
}

export function watchExport(id) {
	subscribers.set(id, (subscribers.get(id) || 0) + 1);
	if (!timer) {
		window.addEventListener("jarvis:export-event", onExportEvent);
		timer = setInterval(() => refreshExports(false), 5000);
		for (const event of ["focus", "online", "jarvis:export-refresh"])
			window.addEventListener(event, schedule);
	}
	schedule();
	return () => {
		const remaining = subscribers.get(id) - 1;
		if (remaining) subscribers.set(id, remaining);
		else {
			subscribers.delete(id);
			written.delete(id);
			delete exportJobs[id];
		}
		if (!subscribers.size) {
			window.removeEventListener("jarvis:export-event", onExportEvent);
			clearInterval(timer);
			timer = null;
			clearTimeout(scheduled);
			for (const event of ["focus", "online", "jarvis:export-refresh"])
				window.removeEventListener(event, schedule);
		}
	};
}

// Throws the server's text when the cancel fails; the card shows it. That is not a refresh
// failure, so the entry's own error is left alone.
export async function cancelExport(id) {
	write(id, { state: await request("cancel", { name: id }), error: "" });
}
