import { describe, it, expect } from "vitest";
import { BADGE_META, CATALOG_LEGEND } from "./capabilityBadges.js";

describe("capability badge meta", () => {
	it("maps each visible tier to a frappe-ui theme + human label", () => {
		for (const k of ["reads_only", "asks_to_approve", "always_asks"]) {
			expect(BADGE_META[k].theme).toBeTruthy();
			expect(BADGE_META[k].label).toBeTruthy();
		}
		expect(BADGE_META.reads_only.label).toBe("Reads only");
		expect(BADGE_META.always_asks.label).toBe("Always asks you");
		expect(BADGE_META.always_asks.theme).toBe("red");
	});

	it("legend states the non-absolute default-gate caveat", () => {
		expect(CATALOG_LEGEND.toLowerCase()).toContain("by default");
	});
});
