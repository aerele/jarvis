import { expect, it } from "vitest";
import { draftBadgeText } from "./draftBadge.js";

// Break: the two sheets of one draft read differently on the phone (the tester saw "DRAFT - NOT SAVED" over a
// Preview and "Draft - not saved" over its form), or the desktop's long-standing words change.
it.each([
	["a create on the desktop", false, undefined, "Draft - not saved"],
	["a create on the desktop, named", false, "desktop", "Draft - not saved"],
	["a create on the phone", false, "phone", "Draft, not saved"],
	["an update on the desktop", true, "desktop", "Pending change"],
	["an update on the phone", true, "phone", "Pending change"],
])("%s", (_, isUpdate, surface, text) => {
	expect(draftBadgeText(isUpdate, surface)).toBe(text);
});
