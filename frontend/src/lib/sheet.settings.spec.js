import { expect, it } from "vitest";
import { outcomeSummary, sections } from "./sheet";

it("keeps a settings update in a dedicated section with a site-wide notice", () => {
	const setting = {
		index: 0,
		doctype: "Buying Settings",
		name: "Buying Settings",
		title: "Buying Settings",
		category: "settings",
		op: "update",
		values: { maintain_same_rate: 0 },
	};
	const [section] = sections([setting]);
	expect(section.key).toBe("settings");
	expect(section.label).toBe("Settings");
	expect(section.notice).toBe("This changes a setting for every user.");
	expect(section.updatesOnly).toBe(true);
	expect(section.records).toEqual([setting]);
});

it("does not repeat a settings doctype in the applied outcome", () => {
	const result = outcomeSummary({
		records: [
			{
				index: 0,
				action: "updated",
				doctype: "Buying Settings",
				name: "Buying Settings",
				title: "Buying Settings",
			},
		],
	});
	expect(result.rows[0].text).toBe("Updated Buying Settings");
});
