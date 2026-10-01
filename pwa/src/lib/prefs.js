import { reactive } from "vue";
import { EFFORT } from "./effort.js";

// Device preferences: the model a new chat starts on, how hard it thinks, and
// whether finished work is allowed to buzz. They live on the device (the native
// app does the same) rather than on the tenant — a phone is one person's, and
// the workspace default is the admin's call.
const KEY = "jarvis.prefs";

function read() {
	try {
		const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
		return {
			// "" = follow the workspace default, so a user who never picks a model
			// keeps tracking the admin's choice when it changes.
			defaultModel: typeof raw.defaultModel === "string" ? raw.defaultModel : "",
			effort: EFFORT.some((e) => e.value === raw.effort) ? raw.effort : "Balanced",
			notifyDone: raw.notifyDone !== false,
			notifyDecision: raw.notifyDecision !== false,
		};
	} catch {
		return { defaultModel: "", effort: "Balanced", notifyDone: true, notifyDecision: true };
	}
}

export const prefs = reactive(read());

export function setPrefs(patch) {
	Object.assign(prefs, patch);
	try {
		localStorage.setItem(KEY, JSON.stringify({ ...prefs }));
	} catch {
		/* private mode: the choice just won't survive the session */
	}
}
