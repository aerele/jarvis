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

	it("strips srcset even on an img with no src at all", () => {
		// A src-less <img srcset> is valid HTML and srcset is in DOMPurify's
		// default ALLOWED_ATTR, so a querySelectorAll("img[src]") scan would skip
		// this element entirely and let the browser load straight from srcset,
		// bypassing the authenticated proxy.
		const out = renderSupportHtml('<img srcset="/files/x.png 2x">', "TKT-1");
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

	it("opens every link in a new tab, so clicking a reply's link doesn't navigate the SPA away", () => {
		// target is NOT in DOMPurify's default ALLOWED_ATTR (rel is) — this pins
		// the ADD_ATTR config that keeps it from being silently stripped.
		const out = renderSupportHtml('<a href="https://x.com">x</a>', "TKT-1");
		expect(out).toContain('target="_blank"');
		expect(out).toContain('rel="noopener noreferrer"');
	});

	it("adds target=_blank to a rewritten /files/ link too, not just external ones", () => {
		const out = renderSupportHtml('<a href="/files/spec.pdf">spec</a>', "TKT-1");
		expect(out).toContain("jarvis.support.media.download");
		expect(out).toContain('target="_blank"');
	});
});
