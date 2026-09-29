import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { mergeCanvasItems } from "./canvasMerge.js";

describe("mergeCanvasItems", () => {
	it("appends new items when there is nothing to merge into", () => {
		const chart = { name: "chart-1", type: "chart" };
		expect(mergeCanvasItems(undefined, [chart])).toEqual([chart]);
		expect(mergeCanvasItems(null, [chart])).toEqual([chart]);
		expect(mergeCanvasItems([], [chart])).toEqual([chart]);
	});

	it("replaces a same-name item IN PLACE, at its original position", () => {
		const a = { name: "file-1", status: "loading" };
		const b = { name: "file-2", status: "ready" };
		const aUpdated = { name: "file-1", status: "ready" };
		expect(mergeCanvasItems([a, b], [aUpdated])).toEqual([aUpdated, b]);
	});

	it("appends a genuinely new name instead of replacing anything", () => {
		const a = { name: "file-1" };
		const c = { name: "file-3" };
		expect(mergeCanvasItems([a], [c])).toEqual([a, c]);
	});

	it("a late partial push keeps items it doesn't mention — the bug this fixes", () => {
		// Three producers (chart/dashboard/file) each publish their OWN partial
		// list. A late file-render push must not wipe the chart an earlier
		// push already landed.
		const chart = { name: "chart-1", type: "chart" };
		const file = { name: "file-1", type: "file", status: "ready" };
		const afterChart = mergeCanvasItems([], [chart]);
		expect(afterChart).toEqual([chart]);
		const afterFile = mergeCanvasItems(afterChart, [file]);
		expect(afterFile).toEqual([chart, file]);
	});

	it("never mutates the existing array", () => {
		const existing = [{ name: "a" }];
		const merged = mergeCanvasItems(existing, [{ name: "b" }]);
		expect(existing).toEqual([{ name: "a" }]);
		expect(merged).not.toBe(existing);
	});

	it("an item with no name can't be matched, so it is appended rather than dropped", () => {
		const named = { name: "a" };
		const unnamed = { type: "chart" };
		expect(mergeCanvasItems([named], [unnamed])).toEqual([named, unnamed]);
	});

	it("tolerates a missing/non-array incoming payload", () => {
		const existing = [{ name: "a" }];
		expect(mergeCanvasItems(existing, undefined)).toEqual(existing);
		expect(mergeCanvasItems(existing, null)).toEqual(existing);
	});
});

describe('ChatView\'s case "canvas" wiring (source-fenced, T5c)', () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("imports mergeCanvasItems and calls it instead of assigning p.items straight through", () => {
		expect(src).toContain('import { mergeCanvasItems } from "@/lib/canvasMerge";');
		const at = src.indexOf('case "canvas": {');
		expect(at).toBeGreaterThan(-1);
		const body = src.slice(at, src.indexOf("\n\t\tcase ", at + 1));
		expect(body).toContain("cm.canvas = mergeCanvasItems(cm.canvas, p.items);");
		expect(body).not.toContain("cm.canvas = p.items;");
	});
});
