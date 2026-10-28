import { describe, expect, it } from "vitest";
import fixtures from "../../../jarvis/tests/fixtures/chart_blocks.json";
import { buildOption } from "@/charts/chartTheme";
import { sharedTranscript, sharedFileUrl } from "./sharedTranscript";

describe("shared transcript presentation", () => {
	it.each(fixtures)("renders the shared $name chart fixture", (fixture) => {
		const parts = sharedTranscript(
			"Before\n```" + fixture.tag + "\n" + fixture.body + "\n```\nAfter"
		);
		const chart = parts.find((p) => p.type === "chart");
		expect(chart).toBeTruthy();
		expect(buildOption(chart.spec)).not.toBeNull();
		expect(
			parts
				.filter((p) => p.type === "text")
				.map((p) => p.text)
				.join(" ")
		).toContain("Before");
		if (fixture.tag === "jarvis-chart") expect(chart.spec).toEqual(JSON.parse(fixture.body));
		else {
			expect(chart.spec.x).toEqual(fixture.expected_rows.map((row) => row[0]));
			expect(chart.spec.series.map((s) => s.name)).toEqual(fixture.expected_headers);
			expect(chart.spec.series.map((s) => s.data)).toEqual(
				fixture.expected_headers.map((_, i) =>
					fixture.expected_rows.map((r) => Number(r[i + 1]))
				)
			);
		}
	});
	it.each([
		"jarvis-ask",
		"jarvis-action",
		"confirm",
		"jarvis-macro",
		"jarvis-goto",
		"jarvis-cards",
		"jarvis-skill",
	])("keeps %s as inert content", (tag) => {
		const parts = sharedTranscript(
			"```" +
				tag +
				'\n{"question":"Inspect this","name":"INV-7","action":"submit","url":"/app/sales-invoice/INV-7"}\n```'
		);
		expect(parts).toHaveLength(1);
		expect(parts[0].type).toBe("summary");
		expect(parts[0].text).toContain("INV-7");
		expect(parts[0]).not.toHaveProperty("onClick");
	});
	it("contains malformed blocks and holds an incomplete streaming block", () => {
		expect(() => sharedTranscript('```jarvis-chart\n{"series":[null]}\n```')).not.toThrow();
		expect(sharedTranscript('Hello\n```confirm\n{"token":"unfinished"', true)).toEqual([
			{ type: "text", text: "Hello" },
		]);
	});
	it("only accepts same-site shared-file endpoints for attachments", () => {
		const url =
			"/api/method/jarvis.chat.sharing.get_shared_file?conversation=C1&file_url=%2Fprivate%2Ffiles%2Fa.png";
		expect(sharedFileUrl(url, "C1")).toBe(url);
		for (const bad of [
			"/private/files/a.png",
			"/files/public.png",
			"https://evil.invalid/x",
			"//evil.invalid/x",
			"javascript:alert(1)",
			url.replace("C1", "C2"),
			url.replace("get_shared_file", "get_canvas"),
		])
			expect(sharedFileUrl(bad, "C1")).toBe("");
	});
});
