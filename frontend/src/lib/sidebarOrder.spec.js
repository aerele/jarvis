import { describe, it, expect } from "vitest";
import { reconcileOrder, moveOrderItem } from "./sidebarOrder.js";

// Compact defs: only `label` matters to the logic.
const TOP = [{ label: "Files" }, { label: "Approvals" }, { label: "Dashboard" }];
const MORE = [{ label: "Macros" }, { label: "Triggers" }];
const labels = (arr) => arr.map((d) => d.label);

describe("reconcileOrder", () => {
	it("applies a saved order within a group", () => {
		const { top } = reconcileOrder(
			{ top: ["Dashboard", "Files", "Approvals"], more: [] },
			TOP,
			MORE
		);
		expect(labels(top)).toEqual(["Dashboard", "Files", "Approvals"]);
	});

	it("moves an item across groups per the saved order", () => {
		const { top, more } = reconcileOrder(
			{ top: ["Files", "Approvals", "Dashboard", "Macros"], more: ["Triggers"] },
			TOP,
			MORE
		);
		expect(labels(top)).toContain("Macros");
		expect(labels(more)).toEqual(["Triggers"]);
	});

	it("drops unknown labels", () => {
		const { top } = reconcileOrder(
			{ top: ["Files", "Ghost", "Approvals"], more: [] },
			TOP,
			MORE
		);
		expect(labels(top)).not.toContain("Ghost");
	});

	it("appends any def the saved order didn't place to its DEFAULT group", () => {
		// Saved only mentions Files; the rest must still appear (nav item can't vanish).
		const { top, more } = reconcileOrder({ top: ["Files"], more: [] }, TOP, MORE);
		expect(labels(top)).toEqual(["Files", "Approvals", "Dashboard"]);
		expect(labels(more)).toEqual(["Macros", "Triggers"]);
	});

	it("keeps a label listed in both groups exactly once", () => {
		const { top, more } = reconcileOrder({ top: ["Macros"], more: ["Macros"] }, TOP, MORE);
		const all = [...labels(top), ...labels(more)];
		expect(all.filter((l) => l === "Macros")).toHaveLength(1);
	});

	it("tolerates junk saved input", () => {
		const { top, more } = reconcileOrder(null, TOP, MORE);
		expect(labels(top)).toEqual(["Files", "Approvals", "Dashboard"]);
		expect(labels(more)).toEqual(["Macros", "Triggers"]);
	});
});

describe("moveOrderItem", () => {
	it("reorders within a group (drop before a later item)", () => {
		// Drag Files (0) onto Dashboard (2): Files inserts BEFORE Dashboard.
		const { top } = moveOrderItem(TOP, MORE, "top", 0, "top", 2);
		expect(labels(top)).toEqual(["Approvals", "Files", "Dashboard"]);
	});

	it("drops an item into the LAST slot via the trailing zone (toIndex = length)", () => {
		// The bug: no item-drop-target can place Files after Dashboard. The trailing
		// zone passes the group length.
		const { top } = moveOrderItem(TOP, MORE, "top", 0, "top", TOP.length);
		expect(labels(top)).toEqual(["Approvals", "Dashboard", "Files"]);
	});

	it("moves an item across groups", () => {
		const { top, more } = moveOrderItem(TOP, MORE, "more", 0, "top", 1);
		expect(labels(top)).toEqual(["Files", "Macros", "Approvals", "Dashboard"]);
		expect(labels(more)).toEqual(["Triggers"]);
	});

	it("drops into an EMPTIED group (toIndex 0 on an empty target)", () => {
		// Move both More items out, then move one back into the now-empty group.
		let s = moveOrderItem(TOP, MORE, "more", 0, "top", TOP.length); // Macros -> top
		s = moveOrderItem(s.top, s.more, "more", 0, "top", s.top.length); // Triggers -> top
		expect(labels(s.more)).toEqual([]);
		const back = moveOrderItem(s.top, s.more, "top", s.top.length - 1, "more", 0);
		expect(labels(back.more)).toEqual(["Triggers"]);
	});

	it("does not mutate the input arrays", () => {
		const topCopy = [...TOP];
		moveOrderItem(TOP, MORE, "top", 0, "top", 2);
		expect(TOP).toEqual(topCopy);
	});

	it("is a no-op on an out-of-range source index", () => {
		const { top, more } = moveOrderItem(TOP, MORE, "top", 99, "top", 0);
		expect(top).toBe(TOP);
		expect(more).toBe(MORE);
	});
});
