import { describe, it, expect } from "vitest";
import { skillLabel, skillOptions, pinnedLabel, OCR_DATA_ENTRY } from "./fileboxSkills";

const rows = [
	{ skill_name: "invoicing", mine: 1, enabled: 1 },
	{ skill_name: "contracts", mine: 0, shared_by: "alice@x.com", enabled: 1 },
	{ skill_name: "leads", mine: 0, via_role: "Sales", enabled: 1 },
	{ skill_name: "orphan", mine: 0, enabled: 1 },
];

describe("fileboxSkills", () => {
	it("labels own skills plainly and others with provenance", () => {
		expect(skillLabel(rows[0])).toBe("invoicing");
		expect(skillLabel(rows[1])).toBe("contracts · shared by alice@x.com");
		expect(skillLabel(rows[2])).toBe("leads · via Sales");
		expect(skillLabel(rows[3])).toBe("orphan · shared");
	});

	it("builds options: Auto + OCR default + each skill keyed by skill_name", () => {
		const opts = skillOptions(rows);
		expect(opts[0]).toEqual({ label: "Auto — pick the best skill", value: "" });
		expect(opts[1]).toEqual({ label: "OCR / Data Entry", value: OCR_DATA_ENTRY });
		expect(opts.map((o) => o.value)).toEqual([
			"",
			OCR_DATA_ENTRY,
			"invoicing",
			"contracts",
			"leads",
			"orphan",
		]);
		// empty / undefined input still yields Auto + OCR (picker never breaks)
		expect(skillOptions().map((o) => o.value)).toEqual(["", OCR_DATA_ENTRY]);
	});

	it("maps a pinned slug to a human badge label", () => {
		expect(pinnedLabel("", rows)).toBe("");
		expect(pinnedLabel(OCR_DATA_ENTRY, rows)).toBe("OCR / Data Entry");
		expect(pinnedLabel("invoicing", rows)).toBe("invoicing");
		// a pin whose skill is gone from the list reads as removed, not a bare slug
		expect(pinnedLabel("deleted-skill", rows)).toBe("deleted-skill (removed)");
	});
});
