import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";
import {
	DESTRUCTIVE_ALWAYS_CONFIRMS,
	PERMISSION_TOUR_COPY,
	permissionCopyFor,
} from "./permissionCopy.js";

// P1-05: the tour's permission promise is bound to the real policy so a future
// weakening cannot silently make the onboarding promise false again (as "Asks
// before it changes anything" was once auto_apply existed). Admin Auto-Apply is
// now removed, so every real change asks - the promise is unconditionally true;
// the only axis left is the destructive carve-out.
describe("permission tour copy ⇄ policy contract", () => {
	it("uses the approved wording", () => {
		expect(PERMISSION_TOUR_COPY).toBe(
			"By default, shows proposed changes and asks you to confirm before writing; " +
				"destructive actions always require confirmation."
		);
	});

	it("the shipped policy always confirms destructive ops", () => {
		// delete/cancel/amend are always braked (design A3).
		expect(DESTRUCTIVE_ALWAYS_CONFIRMS).toBe(true);
	});

	it("derives the approved copy from the shipped policy", () => {
		expect(permissionCopyFor({ destructiveAlwaysConfirms: DESTRUCTIVE_ALWAYS_CONFIRMS })).toBe(
			PERMISSION_TOUR_COPY
		);
	});

	it("a weaker (no destructive carve-out) policy no longer claims 'asks before writing'", () => {
		const copy = permissionCopyFor({ destructiveAlwaysConfirms: false });
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
