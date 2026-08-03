import { describe, it, expect, vi } from "vitest";
import { ref, nextTick } from "vue";

import { useHomeIntro, timeToPromptBucket } from "./useHomeIntro.js";

/**
 * The first-chat introduction's boot/latch/ack transition, extracted from
 * ChatView so it can be behaviour-tested (P1-03 in the round-two review). These
 * are the lifecycle cases the old source-string "integration" checks could not
 * execute: boot, fail-quiet skew, ack success/failure, accepted first send,
 * later New Chat, version bump, proactive-thread suppression, and the bounded
 * telemetry. The pure resolvers/copy stay pinned in lib/homeIntro.spec.js and
 * WelcomeAssistantMessage.spec.js.
 */

function harness(over = {}) {
	const showWelcome = over.showWelcome ?? ref(true);
	const booting = over.booting ?? ref(false);
	const count = over.count ?? ref(0);
	const ui = over.ui ?? ref({ home_intro_version: 1, home_intro_seen_version: 0 });
	const markSeen = over.markSeen ?? vi.fn(() => Promise.resolve());
	const emitTelemetry = over.emitTelemetry ?? vi.fn();
	const getPersona = over.getPersona ?? (() => "Jarvis");
	const intro = useHomeIntro({
		showWelcome,
		booting,
		visibleCount: () => count.value,
		ui,
		isWhitelabeled: over.isWhitelabeled ?? false,
		agentName: over.agentName ?? "Jarvis",
		getPersona,
		markSeen,
		emitTelemetry,
	});
	return { intro, showWelcome, booting, count, ui, markSeen, emitTelemetry };
}

describe("useHomeIntro — boot decision", () => {
	it("offers the full welcome to an unseen user on an empty home", () => {
		const h = harness();
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(true);
	});

	it("shows the compact hero once the user has acknowledged the current version", () => {
		const h = harness({ ui: ref({ home_intro_version: 1, home_intro_seen_version: 1 }) });
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});

	it("re-offers exactly once after a version bump", () => {
		const h = harness({ ui: ref({ home_intro_version: 2, home_intro_seen_version: 1 }) });
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(true);
	});

	it("fails quiet to the compact hero when the seen version could not be read", () => {
		// get_chat_ui_settings OMITS the key on a read failure; the intro must not
		// re-lecture an established user because a read blipped.
		const h = harness({ ui: ref({ home_intro_version: 1 }) });
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});

	it("fails quiet against a backend that predates the feature", () => {
		const h = harness({ ui: ref({}) });
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});

	it("treats a home_intro_version of 0 (operator kill switch off) as not due", () => {
		const h = harness({ ui: ref({ home_intro_version: 0, home_intro_seen_version: 0 }) });
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});
});

describe("useHomeIntro — latch and retire", () => {
	it("never draws over a conversation that has messages (proactive included)", async () => {
		const h = harness();
		h.intro.initFromBoot();
		// A proactive/existing thread: showWelcome is false because messages exist.
		h.showWelcome.value = false;
		h.count.value = 1;
		await nextTick();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});

	it("retires the moment the first real message is on screen", async () => {
		const h = harness();
		h.intro.initFromBoot();
		expect(h.intro.showHomeIntro.value).toBe(true);
		h.count.value = 1; // the user's optimistic first bubble
		await nextTick();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});

	it("does NOT retire during boot (restoring the last conversation is not 'moved on')", async () => {
		const h = harness({ booting: ref(true) });
		h.intro.initFromBoot();
		h.count.value = 3; // boot restore of an existing thread
		await nextTick();
		// Still pending; a later empty home must be able to show it.
		h.booting.value = false;
		h.showWelcome.value = true;
		h.count.value = 0;
		await nextTick();
		expect(h.intro.showHomeIntro.value).toBe(true);
	});

	it("stays retired for a later New Chat (compact hero, not the full welcome)", async () => {
		const h = harness();
		h.intro.initFromBoot();
		h.count.value = 1; // first send
		await nextTick();
		expect(h.intro.showHomeIntro.value).toBe(false);
		// New Chat: an empty home again, but the intro is latched off for the session.
		h.count.value = 0;
		await nextTick();
		expect(h.intro.showHomeIntro.value).toBe(false);
	});
});

describe("useHomeIntro — best-effort acknowledgement", () => {
	it("acks the current version exactly once, on render", () => {
		const h = harness();
		h.intro.initFromBoot();
		h.intro.ackHomeIntro();
		h.intro.ackHomeIntro();
		expect(h.markSeen).toHaveBeenCalledTimes(1);
		expect(h.markSeen).toHaveBeenCalledWith(1);
	});

	it("swallows an ack failure and keeps the welcome usable until a real message", async () => {
		const markSeen = vi.fn(() => Promise.reject(new Error("network")));
		const h = harness({ markSeen });
		h.intro.initFromBoot();
		// Must not throw even though the transport rejects.
		expect(() => h.intro.ackHomeIntro()).not.toThrow();
		await Promise.resolve();
		expect(markSeen).toHaveBeenCalledTimes(1);
		// The composer is never gated by the ack: the welcome is still up.
		expect(h.intro.showHomeIntro.value).toBe(true);
	});

	it("survives a synchronous throw from the transport", () => {
		const markSeen = vi.fn(() => {
			throw new Error("boom");
		});
		const h = harness({ markSeen });
		h.intro.initFromBoot();
		expect(() => h.intro.ackHomeIntro()).not.toThrow();
	});
});

describe("useHomeIntro — bounded telemetry", () => {
	it("emits 'displayed' with the version on ack", () => {
		const h = harness();
		h.intro.initFromBoot();
		h.intro.ackHomeIntro();
		expect(h.emitTelemetry).toHaveBeenCalledWith("displayed", { version: 1 });
	});

	it("emits 'suggestion_selected' with a category token only", () => {
		const h = harness();
		h.intro.noteSuggestionSelected("analyse");
		expect(h.emitTelemetry).toHaveBeenCalledWith("suggestion_selected", {
			category: "analyse",
		});
	});

	it("emits 'first_prompt' with a bucket when a displayed intro retires", async () => {
		const h = harness();
		h.intro.initFromBoot();
		h.intro.ackHomeIntro(); // displayed
		h.emitTelemetry.mockClear();
		h.count.value = 1; // first prompt
		await nextTick();
		expect(h.emitTelemetry).toHaveBeenCalledTimes(1);
		const [event, payload] = h.emitTelemetry.mock.calls[0];
		expect(event).toBe("first_prompt");
		expect(payload.version).toBe(1);
		expect(payload.bucket).toBe("0-5s");
	});

	it("does NOT emit 'first_prompt' when the intro was never displayed", async () => {
		// Opening an existing chat from the home: the intro never rendered here, so
		// there is no display-to-prompt duration to report.
		const h = harness();
		h.intro.initFromBoot();
		// no ackHomeIntro() -> never displayed
		h.count.value = 1;
		await nextTick();
		const events = h.emitTelemetry.mock.calls.map((c) => c[0]);
		expect(events).not.toContain("first_prompt");
	});

	it("a telemetry sink that throws never affects the ack or the latch", () => {
		const emitTelemetry = vi.fn(() => {
			throw new Error("telemetry down");
		});
		const markSeen = vi.fn(() => Promise.resolve());
		const h = harness({ emitTelemetry, markSeen });
		h.intro.initFromBoot();
		expect(() => h.intro.ackHomeIntro()).not.toThrow();
		expect(markSeen).toHaveBeenCalledTimes(1);
	});
});

describe("useHomeIntro — identity resolution", () => {
	it("greets a Jara user as Jara on an unbranded workspace", () => {
		const h = harness({ getPersona: () => "Jara" });
		expect(h.intro.homeIntroPersona.value).toBe("Jara");
		expect(h.intro.homeIntroSpeakerName.value).toBe("Jara");
	});

	it("lets the tenant brand win over the persona, with the brand mark", () => {
		const h = harness({ isWhitelabeled: true, agentName: "Aria", getPersona: () => "Jara" });
		expect(h.intro.homeIntroPersona.value).toBe("Jarvis"); // => brand mark
		expect(h.intro.homeIntroSpeakerName.value).toBe("Aria"); // => brand name
	});

	it("honours the persona kill switch from the boot payload", () => {
		const h = harness({
			ui: ref({ home_intro_version: 1, home_intro_seen_version: 0, persona_enabled: false }),
			getPersona: () => "Jara",
		});
		expect(h.intro.homeIntroPersona.value).toBe("Jarvis");
	});
});

describe("timeToPromptBucket", () => {
	it("buckets coarsely and never leaks a raw duration", () => {
		expect(timeToPromptBucket(0)).toBe("0-5s");
		expect(timeToPromptBucket(4999)).toBe("0-5s");
		expect(timeToPromptBucket(5000)).toBe("5-15s");
		expect(timeToPromptBucket(20000)).toBe("15-60s");
		expect(timeToPromptBucket(120000)).toBe("1-5m");
		expect(timeToPromptBucket(600000)).toBe("5m+");
	});

	it("returns 'unknown' for a non-finite or negative input", () => {
		expect(timeToPromptBucket(-1)).toBe("unknown");
		expect(timeToPromptBucket(NaN)).toBe("unknown");
		expect(timeToPromptBucket(undefined)).toBe("unknown");
	});
});
