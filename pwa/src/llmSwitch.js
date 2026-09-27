// Drives the existing maintenance hold strip from the realtime "jarvis:llm_switch"
// event (jarvis#1425 follow-up, Part C of
// ../../plans/2026-09-27-seamless-llm-switch-design.md). Mirrors the desktop SPA's
// frontend/src/llmSwitch.js in BEHAVIOR (switching -> raise the hold, done -> recheck
// it), but not in shape: this module takes NO import of ./maintenanceGate.js, on
// purpose. maintenanceGate.js pulls in vue + frappe-ui (a package published as raw
// TypeScript source), and the PWA's only test tool is node's built-in `node --test`
// (CI: `node --test pwa/src/lib/*.test.js`, no vitest, no bundler) - it cannot load
// that graph at all, mock or not. Keeping this module import-free lets it be tested
// in isolation; the real gate is wired once at the call site (App.vue).
export const SWITCH_MESSAGE = "Updating your AI setup. Chat will be back in a moment.";

// gate: {raiseHold, recheck} - normally maintenanceGate's own exports, passed in by
// the caller. Returns the socket handler. No new component: "switching" raises the
// same hold App.vue already renders, "done" re-checks it (recheck() also stops the
// hold's own poll once it clears).
export function makeOnLlmSwitch(gate) {
	return function onLlmSwitch(payload) {
		switch (payload?.state) {
			case "switching":
				gate.raiseHold(SWITCH_MESSAGE);
				break;
			case "done":
				gate.recheck();
				break;
			default:
			// Unknown or malformed payload: no-op. A future state must not raise or
			// clear a hold it doesn't understand.
		}
	};
}
