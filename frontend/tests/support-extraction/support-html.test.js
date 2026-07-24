import { describe, it, expect, vi } from "vitest";

// Mock @/api rather than importing it: the real module pulls the whole frappe-ui
// graph into jsdom, which no existing test in this harness does.
vi.mock("@/api", () => ({
	supportDownloadUrl: (t, f) =>
		`/api/method/jarvis.support.media.download?ticket=${encodeURIComponent(
			t
		)}&file_url=${encodeURIComponent(f)}`,
}));

import { renderSupportHtml } from "@/lib/supportHtml";

describe("renderSupportHtml", () => {
	it("routes inline /files/ images through the authenticated proxy", () => {
		const out = renderSupportHtml('<img src="/files/shot.png">', "TKT-1");
		expect(out).toContain("jarvis.support.media.download");
		expect(out).toContain("ticket=TKT-1");
		expect(out).not.toContain('src="/files/shot.png"');
	});

	it("routes private files too", () => {
		const out = renderSupportHtml('<a href="/private/files/x.pdf">x</a>', "TKT-1");
		expect(out).toContain("jarvis.support.media.download");
	});

	it("leaves external URLs alone", () => {
		const out = renderSupportHtml('<img src="https://cdn.example.com/a.png">', "TKT-1");
		expect(out).toContain("https://cdn.example.com/a.png");
	});

	it("strips srcset, which would otherwise bypass the proxy", () => {
		const out = renderSupportHtml(
			'<img src="/files/a.png" srcset="/files/a2.png 2x">',
			"TKT-1"
		);
		expect(out).not.toContain("srcset");
	});

	it("sanitizes — and sanitizes LAST, so a rewritten node cannot smuggle script", () => {
		const out = renderSupportHtml(
			'<img src="/files/a.png" onerror="alert(1)"><script>alert(2)</script>',
			"TKT-1"
		);
		expect(out).not.toContain("onerror");
		expect(out).not.toContain("<script");
	});

	it("returns empty string for empty input rather than throwing", () => {
		expect(renderSupportHtml("", "TKT-1")).toBe("");
		expect(renderSupportHtml(null, "TKT-1")).toBe("");
	});
});
