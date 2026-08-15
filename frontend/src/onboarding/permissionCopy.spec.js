import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";
import {
	DEFAULT_AUTO_APPLY,
	DESTRUCTIVE_ALWAYS_CONFIRMS,
	PERMISSION_TOUR_COPY,
	permissionCopyFor,
} from "./permissionCopy.js";

// P1-05: the tour's permission promise is bound to the real default policy so a
// future auto_apply-default change cannot silently make the onboarding promise
// false again (as "Asks before it changes anything" was).
describe("permission tour copy ⇄ default policy contract", () => {
	it("uses the approved wording", () => {
		expect(PERMISSION_TOUR_COPY).toBe(
			"By default, shows proposed changes and asks you to confirm before writing; " +
				"destructive actions always require confirmation."
		);
	});

	it("the shipped default policy is confirm-first with destructive carve-out", () => {
		// Mirrors Jarvis Conversation.auto_apply default 0 (confirm every write)
		// + destructive ops always park.
		expect(DEFAULT_AUTO_APPLY).toBe(0);
		expect(DESTRUCTIVE_ALWAYS_CONFIRMS).toBe(true);
	});

	it("derives the approved copy from the default policy", () => {
		expect(
			permissionCopyFor({
				autoApply: DEFAULT_AUTO_APPLY,
				destructiveAlwaysConfirms: DESTRUCTIVE_ALWAYS_CONFIRMS,
			})
		).toBe(PERMISSION_TOUR_COPY);
	});

	it("a weaker (auto-apply-on) default no longer claims 'asks before it changes anything'", () => {
		const copy = permissionCopyFor({ autoApply: 1, destructiveAlwaysConfirms: true });
		expect(copy).not.toBe(PERMISSION_TOUR_COPY);
		expect(copy).not.toMatch(/asks you to confirm before writing/i);
	});
});

// TourIntro must render the copy from this module, never a hardcoded string, so
// the contract above actually governs what the customer sees.
describe("TourIntro binds to permissionCopy.js", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const src = fs.readFileSync(path.join(HERE, "TourIntro.vue"), "utf8");

	it("imports and renders PERMISSION_TOUR_COPY", () => {
		expect(src).toMatch(/permissionCopy/);
		expect(src).toMatch(/PERMISSION_TOUR_COPY/);
	});

	it("no longer ships the old over-promise", () => {
		expect(src).not.toMatch(/Asks before it changes anything/);
	});
});
