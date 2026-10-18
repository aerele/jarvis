import { reactive } from "vue";

// One batched request for all mounted cards, shared by desktop and PWA.
export const exportJobs = reactive({});
const subscribers = new Map();
let timer,
	scheduled,
	inFlight = false;

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
	if (!response.ok) throw new Error("Could not refresh the export. Please try again.");
	return (await response.json()).message;
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

export async function refreshExports(force = true, names = null) {
	if (inFlight || !subscribers.size) return;
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
			const results = await request("get_jobs", { names: ids.slice(i, i + 100) });
			for (const [id, state] of Object.entries(results))
				exportJobs[id] = { state, error: "" };
		}
	} catch (error) {
		for (const id of ids) exportJobs[id] = { ...exportJobs[id], error: error.message };
	} finally {
		inFlight = false;
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

export async function cancelExport(id) {
	exportJobs[id] = { state: await request("cancel", { name: id }), error: "" };
}
