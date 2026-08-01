import { describe, it, expect } from "vitest";

import { homeIntroDue, homeIntroSpeaker } from "./homeIntro.js";

/**
 * The cadence and the identity of the first-chat introduction. Both are pure
 * so they can be pinned without mounting ChatView, which is where they are
 * consumed (`homeIntroPending` at boot, `homeIntroSpeakerName` in the bubble).
 */

describe("homeIntroDue", () => {
	it("shows for a user who has never seen it", () => {
		expect(homeIntroDue({ home_intro_version: 1, home_intro_seen_version: 0 })).toBe(true);
	});

	it("stays away once the user has acknowledged the current version", () => {
		expect(homeIntroDue({ home_intro_version: 1, home_intro_seen_version: 1 })).toBe(false);
	});

	it("shows again exactly once when the presentation version is bumped", () => {
		expect(homeIntroDue({ home_intro_version: 2, home_intro_seen_version: 1 })).toBe(true);
		expect(homeIntroDue({ home_intro_version: 2, home_intro_seen_version: 2 })).toBe(false);
	});

	it("never shows for a seen version ahead of the current one", () => {
		// A rolled-back deploy must not re-lecture users who saw the newer copy.
		expect(homeIntroDue({ home_intro_version: 1, home_intro_seen_version: 2 })).toBe(false);
	});

	it("fails quiet when the server could not read the seen version", () => {
		// get_chat_ui_settings OMITS the key on a read failure (same contract as
		// preferred_persona) — the compact hero is the safe answer, not a repeat.
		expect(homeIntroDue({ home_intro_version: 1 })).toBe(false);
	});

	it("fails quiet against a backend that predates the feature", () => {
		expect(homeIntroDue({ stt_enabled: true })).toBe(false);
		expect(homeIntroDue({})).toBe(false);
		expect(homeIntroDue(null)).toBe(false);
		expect(homeIntroDue(undefined)).toBe(false);
	});
});

describe("homeIntroSpeaker", () => {
	it("greets as Jarvis on an unbranded workspace with the default persona", () => {
		expect(
			homeIntroSpeaker({ agentName: "Jarvis", isWhitelabeled: false, persona: "Jarvis" })
		).toBe("Jarvis");
	});

	it("greets a Jara user as Jara", () => {
		expect(
			homeIntroSpeaker({ agentName: "Jarvis", isWhitelabeled: false, persona: "Jara" })
		).toBe("Jara");
	});

	it("lets the tenant brand win over the persona", () => {
		// agent_name is what the agent calls itself in every turn
		// (turn_handler._assistant_name_clause); the persona is a voice, not a
		// product name, so a whitelabelled workspace is never greeted by "Jara".
		expect(homeIntroSpeaker({ agentName: "Aria", isWhitelabeled: true, persona: "Jara" })).toBe(
			"Aria"
		);
		expect(
			homeIntroSpeaker({ agentName: "Aria", isWhitelabeled: true, persona: "Jarvis" })
		).toBe("Aria");
	});

	it("uses the brand name when the caller has gated the persona off", () => {
		// persona_enabled off => turns answer in the default voice, so must the
		// bubble. The caller passes "Jarvis" in that case.
		expect(
			homeIntroSpeaker({ agentName: "Jarvis", isWhitelabeled: false, persona: "Jarvis" })
		).toBe("Jarvis");
	});

	it("never renders an empty speaker", () => {
		expect(homeIntroSpeaker({ agentName: "", isWhitelabeled: false, persona: "Jarvis" })).toBe(
			"Jarvis"
		);
		expect(homeIntroSpeaker({ agentName: "   ", isWhitelabeled: true })).toBe("Jarvis");
		expect(homeIntroSpeaker()).toBe("Jarvis");
	});
});
