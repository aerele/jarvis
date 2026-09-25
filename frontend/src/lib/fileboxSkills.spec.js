import { describe, it, expect } from "vitest";
import {
	skillLabel,
	skillOptions,
	pinnedLabel,
	createsChip,
	isCreatesError,
	promoFileBox,
	OCR_DATA_ENTRY,
} from "./fileboxSkills";

const rows = [
	{ skill_name: "invoicing", mine: 1, enabled: 1, use_in_file_box: 1 },
	{ skill_name: "contracts", mine: 0, shared_by: "alice@x.com", enabled: 1, use_in_file_box: 1 },
	{ skill_name: "leads", mine: 0, via_role: "Sales", enabled: 1, use_in_file_box: 1 },
	{ skill_name: "orphan", mine: 0, enabled: 1, use_in_file_box: 1 },
	{ skill_name: "chat-only", mine: 1, enabled: 1, use_in_file_box: 0 },
];

describe("fileboxSkills", () => {
	it("labels own skills plainly and others with provenance", () => {
		expect(skillLabel(rows[0])).toBe("invoicing");
		expect(skillLabel(rows[1])).toBe("contracts · shared by alice@x.com");
		expect(skillLabel(rows[2])).toBe("leads · via Sales");
		expect(skillLabel(rows[3])).toBe("orphan · shared");
	});

	it("builds options: None + each skill File Box may use, keyed by skill_name (no OCR base)", () => {
		const opts = skillOptions(rows);
		expect(opts[0]).toEqual({ label: "None", value: "" });
		// the OCR base is NOT offered as an extra to layer on, nor a skill kept out of File Box
		expect(opts.map((o) => o.value)).toEqual([
			"",
			"invoicing",
			"contracts",
			"leads",
			"orphan",
		]);
		// empty / undefined input still yields the None option (picker never breaks)
		expect(skillOptions().map((o) => o.value)).toEqual([""]);
	});

	it("maps a pinned slug to a human badge label", () => {
		expect(pinnedLabel("", rows)).toBe("");
		expect(pinnedLabel(OCR_DATA_ENTRY, rows)).toBe("OCR / Data Entry");
		expect(pinnedLabel("invoicing", rows)).toBe("invoicing");
		// an opted-out skill still names an old pin (it was not removed)
		expect(pinnedLabel("chat-only", rows)).toBe("chat-only");
		// a pin whose skill is gone from the list reads as removed, not a bare slug
		expect(pinnedLabel("deleted-skill", rows)).toBe("deleted-skill (removed)");
	});

	it("chips a skill File Box follows to a fixed document type", () => {
		expect(createsChip({ use_in_file_box: 1, file_box_creates: "Purchase Invoice" })).toBe(
			"File Box → Purchase Invoice"
		);
		expect(createsChip({ use_in_file_box: 0, file_box_creates: "Purchase Invoice" })).toBe("");
		expect(createsChip({ use_in_file_box: 1, file_box_creates: "" })).toBe("");
		expect(createsChip(null)).toBe("");
	});

	it("routes only the File Box creates validation error to the field", () => {
		expect(isCreatesError({ exc_type: "FileBoxCreatesError" })).toBe(true);
		expect(isCreatesError({ exc_type: "ValidationError" })).toBe(false);
		expect(isCreatesError(new Error("File Box creates must be…"))).toBe(false);
		expect(isCreatesError(undefined)).toBe(false);
	});

	it("words a promotion's File Box snapshot for the reviewer", () => {
		const PI = "Purchase Invoice";
		const snap = (use, creates) => ({
			use_in_file_box_snapshot: use,
			file_box_creates_snapshot: creates,
		});
		expect(promoFileBox(snap(1, PI))).toBe("File Box → Purchase Invoice");
		expect(promoFileBox(snap(1, ""))).toBe("Used in File Box");
		expect(promoFileBox(snap(0, PI))).toBe("Not used in File Box · creates Purchase Invoice");
		expect(promoFileBox(snap(0, ""))).toBe("Not used in File Box");
		expect(promoFileBox(snap(null, PI))).toBe(""); // a decided row / an older server
		expect(promoFileBox(null)).toBe("");
	});
});
