import { describe, it, expect } from "vitest";
import { priorityBadge } from "@/stores/supportBadges";

describe("priorityBadge", () => {
	it("colours only the attention priorities; Medium/Low stay quiet gray", () => {
		expect(priorityBadge("Urgent")).toEqual({
			label: "Urgent",
			theme: "red",
			variant: "subtle",
		});
		expect(priorityBadge("High")).toEqual({
			label: "High",
			theme: "orange",
			variant: "subtle",
		});
		expect(priorityBadge("Medium")).toEqual({
			label: "Medium",
			theme: "gray",
			variant: "subtle",
		});
		expect(priorityBadge("Low")).toEqual({ label: "Low", theme: "gray", variant: "subtle" });
	});

	it("returns null for a blank/absent priority so the self-activating column renders nothing", () => {
		expect(priorityBadge("")).toBeNull();
		expect(priorityBadge(null)).toBeNull();
		expect(priorityBadge(undefined)).toBeNull();
	});

	it("falls back to gray for an unrecognised priority rather than throwing", () => {
		expect(priorityBadge("Critical")).toEqual({
			label: "Critical",
			theme: "gray",
			variant: "subtle",
		});
	});
});
