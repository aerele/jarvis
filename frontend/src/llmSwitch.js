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

// Kept identical to jarvis/chat/llm_switch.py MESSAGE. Duplicated (not fetched) so the
// banner appears the instant the switch starts, before any round-trip could complete.
export const SWITCH_MESSAGE = "Updating your AI setup. Chat will be back in a moment.";

export function onLlmSwitch(payload) {
	switch (payload?.state) {
		case "switching":
			raiseHold(SWITCH_MESSAGE);
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
