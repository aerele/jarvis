// Drives the existing maintenance hold banner from the realtime "jarvis:llm_switch"
// event (jarvis#1425 follow-up, Part C of
// ../../plans/2026-09-27-seamless-llm-switch-design.md). The server (jarvis.chat.
// llm_switch) broadcasts {state: "switching"} right before it starts holding an
// apply, and {state: "done", outcome} once the apply has finished - no message on
// either payload, so this module carries the ONE line of copy that matches the
// server's own MESSAGE constant (llm_switch.MESSAGE) rather than round-tripping for
// it. No new component: "switching" raises the same hold Banner.vue already renders;
// "done" clears it immediately (so the banner never waits on the hold's own 60s idle
// poll, which is skipped outright while a recheck from some other trigger is already
// in flight) and THEN rechecks the control plane, which re-raises the hold if an
// unrelated operator maintenance notice is also active.
import { raiseHold, clearHold, recheck } from "@/maintenanceGate";

// Kept identical to jarvis/chat/llm_switch.py MESSAGE / ADMIN_MESSAGE. Duplicated (not
// fetched) so the banner appears the instant the switch starts, before any round-trip.
export const SWITCH_MESSAGE =
	"Your admin is updating the AI models. Please wait, chat will be back shortly.";
export const SWITCH_MESSAGE_ADMIN = "Applying your AI model changes. Chat will be back shortly.";

// Mirrors llm_switch.message_for(): window.is_jarvis_admin is the page boot flag
// (jarvis/www/jarvis.py), the same gate as the AI models editor.
export function switchMessage() {
	return window.is_jarvis_admin ? SWITCH_MESSAGE_ADMIN : SWITCH_MESSAGE;
}

export function onLlmSwitch(payload) {
	switch (payload?.state) {
		case "switching":
			raiseHold(switchMessage());
			break;
		case "done":
			clearHold();
			recheck();
			break;
		default:
		// Unknown or malformed payload: no-op. A future state must not raise or
		// clear a hold it doesn't understand.
	}
}
