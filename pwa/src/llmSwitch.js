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
export const SWITCH_MESSAGE =
	"Your admin is updating the AI models. Please wait, chat will be back shortly.";
export const SWITCH_MESSAGE_ADMIN = "Applying your AI model changes. Chat will be back shortly.";

// Mirrors llm_switch.message_for(): window.is_jarvis_admin is the page boot flag
// (jarvis/www/jarvis_mobile.py). Read through globalThis so node --test has no window.
const bootIsAdmin = () => !!globalThis.window?.is_jarvis_admin;

// gate: {raiseHold, clearHold, recheck} - normally maintenanceGate's own exports,
// passed in by the caller. isAdmin: defaults to the boot flag; tests pass their own.
// Returns the socket handler. No new component: "switching"
// raises the same hold App.vue already renders; "done" clears it immediately (so the
// strip never waits on the hold's own 60s idle poll, which is skipped outright while a
// recheck from some other trigger is already in flight) and THEN rechecks the control
// plane, which re-raises the hold if an unrelated operator maintenance notice is also
// active.
export function makeOnLlmSwitch(gate, isAdmin = bootIsAdmin) {
	return function onLlmSwitch(payload) {
		switch (payload?.state) {
			case "switching":
				gate.raiseHold(isAdmin() ? SWITCH_MESSAGE_ADMIN : SWITCH_MESSAGE);
				break;
			case "done":
				gate.clearHold();
				gate.recheck();
				break;
			default:
			// Unknown or malformed payload: no-op. A future state must not raise or
			// clear a hold it doesn't understand.
		}
	};
}
