import { describe, expect, it } from "vitest";
import { exportCardView, progressOf } from "./exportCardView.js";

const running = (over = {}) => ({
	status: "running",
	rows_written: 18400,
	total_rows: 46214,
	phase: null,
	format: "xlsx",
	column_count: 3,
	started_at: null,
	cancel_requested: false,
	...over,
});
const view = (over) => exportCardView({ export_job: "j", title: "T" }, { state: running(over) });

describe("progressOf", () => {
	it("reads rows against the counted total as a rounded percent", () => {
		expect(progressOf(running())).toEqual({
			kind: "value",
			percent: 40,
			rows: 18400,
			total: 46214,
		});
	});

	it("never reaches 100: a full count stops at 99 until the file is ready", () => {
		expect(progressOf(running({ rows_written: 46214 })).percent).toBe(99);
		// Mutation killed: dropping Math.min shows 100 here.
		expect(progressOf(running({ rows_written: 99999, total_rows: 100000 })).percent).toBe(99);
	});

	it("never rounds a started export down to 0%", () => {
		expect(progressOf(running({ rows_written: 1, total_rows: 1000000 })).percent).toBe(1);
	});

	it("is unknown when the count is missing, zero or stale", () => {
		for (const over of [
			{ total_rows: null },
			{ total_rows: 0 },
			{ total_rows: undefined },
			{ rows_written: 50000 }, // more rows than the count: the count is stale
		])
			expect(progressOf(running(over))).toEqual({ kind: "unknown" });
	});

	it("is unknown until the first rows are written, so the card never shows 0%", () => {
		// Mutation killed: dropping the rows > 0 rule gives a 0% value.
		expect(progressOf(running({ rows_written: 0 }))).toEqual({ kind: "unknown" });
	});

	it("is finalizing for the finalizing phase, with or without a count", () => {
		expect(progressOf(running({ phase: "finalizing" }))).toEqual({ kind: "finalizing" });
		expect(progressOf(running({ phase: "finalizing", total_rows: 0 }))).toEqual({
			kind: "finalizing",
		});
	});

	it("never promises a file while the card's own cancel request is in flight", () => {
		// Mutation killed: ignoring the local flag returns finalizing here.
		const state = running({ phase: "finalizing" });
		expect(progressOf(state, true)).toEqual({
			kind: "value",
			percent: 40,
			rows: 18400,
			total: 46214,
		});
	});

	it("never promises a file while a cancel is pending", () => {
		// Mutation killed: dropping the cancel_requested rule returns finalizing here.
		const pending = progressOf(running({ phase: "finalizing", cancel_requested: true }));
		expect(pending.kind).not.toBe("finalizing");
		expect(pending.kind).toBe("value");
		expect(
			progressOf(running({ phase: "finalizing", cancel_requested: true, total_rows: 0 }))
		).toEqual({ kind: "unknown" });
	});

	it("is unknown for every state that is not running", () => {
		// Called directly: through exportCardView the builder is chosen by status, which hides
		// a dropped running check.
		expect(progressOf(running({ status: "ready", total_rows: 10, rows_written: 5 }))).toEqual({
			kind: "unknown",
		});
		for (const status of ["paused", "queued"])
			expect(progressOf(running({ status, phase: "finalizing" }))).toEqual({
				kind: "unknown",
			});
	});
});

describe("running card view", () => {
	it("shows the counted progress as a value track with its sentence", () => {
		const v = view();
		expect(v.track).toBe("value");
		expect(v.percent).toBe(40);
		expect(v.progressText).toBe("18,400 of 46,214 rows, 40%");
		expect(v.stats[0]).toEqual({ strong: "18,400", text: " of 46,214 rows · 40%" });
		expect(v.pill.text).toBe("Preparing");
	});

	it("formats the numbers for the reader's locale", () => {
		const v = view({ rows_written: 1234, total_rows: 46214 });
		expect(v.stats[0].strong).toBe("1,234");
		expect(v.stats[0].text).toContain("46,214");
		expect(v.stats[0].text).not.toContain("46214");
	});

	it("keeps today's activity bar and row count when there is no usable count", () => {
		for (const over of [{ total_rows: null }, { rows_written: 50000 }, { rows_written: 0 }]) {
			const v = view(over);
			expect(v.track).toBe("indet");
			expect(v.stats[0].text).toBe(" rows written");
			expect(JSON.stringify(v.stats)).not.toContain("%");
		}
	});

	it("labels the finalizing step by format, first in the stats, without a percent", () => {
		const xlsx = view({ phase: "finalizing" });
		expect(xlsx.track).toBe("indet");
		expect(xlsx.stats[0]).toEqual({ strong: "Finalizing spreadsheet…" });
		expect(xlsx.stats[1].text).toBe(" rows written");
		expect(view({ phase: "finalizing", format: "csv" }).stats[0].strong).toBe(
			"Finalizing file…"
		);
		expect(JSON.stringify(xlsx.stats)).not.toContain("%");
		expect(xlsx.buttons).toEqual(["cancel"]);
	});

	it("does not show the finalizing label while Cancelling", () => {
		const v = view({ phase: "finalizing", cancel_requested: true });
		expect(JSON.stringify(v.stats)).not.toContain("Finalizing");
	});
});
