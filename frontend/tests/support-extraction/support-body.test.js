import { describe, it, expect } from "vitest";
import { supportBodyIsEmpty, cleanSupportBody, prepareSupportBody } from "@/lib/supportBody";

// These are the compose-side body helpers shared by the two SupportComposer
// hosts. dompurify + DOMParser are left REAL (jsdom provides both) — the whole
// point of cleanSupportBody is the actual sanitize, so mocking it out would
// assert nothing. supportHtml.test.js already proves real dompurify works here.

describe("supportBodyIsEmpty", () => {
	it("treats a blank / whitespace-only TipTap doc as empty", () => {
		// TipTap emits "<p></p>" for an empty editor — the reason `.trim()` on the
		// raw HTML is the WRONG emptiness test (it is truthy). Each of these LOOKS
		// blank to the user yet would arm Submit without the tag+nbsp strip.
		expect(supportBodyIsEmpty("")).toBe(true);
		expect(supportBodyIsEmpty(null)).toBe(true);
		expect(supportBodyIsEmpty(undefined)).toBe(true);
		expect(supportBodyIsEmpty("<p></p>")).toBe(true);
		expect(supportBodyIsEmpty("<p><br></p>")).toBe(true);
		expect(supportBodyIsEmpty("<p>&nbsp;</p>")).toBe(true);
		expect(supportBodyIsEmpty("<p> </p>")).toBe(true); // a literal non-breaking space
		expect(supportBodyIsEmpty("   ")).toBe(true);
	});

	it("treats real text as non-empty", () => {
		expect(supportBodyIsEmpty("<p>hi</p>")).toBe(false);
		expect(supportBodyIsEmpty("<ul><li>a</li></ul>")).toBe(false);
		// A leading nbsp then real text must still count as text.
		expect(supportBodyIsEmpty("<p>&nbsp;x</p>")).toBe(false);
	});

	it("treats an image-only body as empty (its text strips to nothing)", () => {
		// Deliberate: an <img>-only body reads as empty, so canSubmit blocks a
		// submit whose ONLY content is an inline image (which cleanSupportBody would
		// then strip to a truly empty doc anyway). Tags — including <img> — are
		// stripped, so there is no text left.
		expect(supportBodyIsEmpty('<p><img src="data:image/png;base64,AAAA"></p>')).toBe(true);
	});
});

describe("cleanSupportBody", () => {
	it("strips inline data: and blob: images and counts them", () => {
		const { html, stripped } = cleanSupportBody(
			'<p>see<img src="data:image/png;base64,AAAA"><img src="blob:https://x/y">here</p>'
		);
		expect(html).not.toContain("data:");
		expect(html).not.toContain("blob:");
		expect(html).toContain("see");
		expect(html).toContain("here");
		expect(stripped).toBe(2);
	});

	it("matches data:/blob: case-insensitively (an uppercased scheme must not slip through)", () => {
		const { stripped } = cleanSupportBody('<img src="DATA:image/png;base64,AAAA">');
		expect(stripped).toBe(1);
	});

	it("strips a data: image whose src has leading/trailing whitespace (browsers trim it)", () => {
		// A browser trims URL whitespace, so `src=" data:… "` still renders — an
		// exact `[src^="data:"]` selector would MISS it and leak the base64. The
		// trim-before-test closes that bypass.
		const { stripped, html } = cleanSupportBody(
			'<p><img src=" data:image/png;base64,AAAA "></p>'
		);
		expect(stripped).toBe(1);
		expect(html).not.toContain("data:");
	});

	it("strips a data: image whose src has an INTERNAL newline/tab (browsers ignore it)", () => {
		// Whitespace ANYWHERE in a URL is ignored by browsers + DOMPurify's
		// ATTR_WHITESPACE, so a crafted `src="da\nta:…"` renders as data: yet a plain
		// trim+prefix (ends only) would miss it. Stripping all \t\r\n closes that.
		const { stripped } = cleanSupportBody('<img src="da\nta:image/png;base64,AAAA">');
		expect(stripped).toBe(1);
	});

	it("keeps a normal (non-inline) image untouched", () => {
		const { html, stripped } = cleanSupportBody('<p><img src="/files/a.png"></p>');
		expect(stripped).toBe(0);
		expect(html).toContain("/files/a.png");
	});

	it("sanitizes script/handler XSS (DOMPurify runs LAST)", () => {
		const { html } = cleanSupportBody('<script>alert(1)</script><p onclick="evil()">hi</p>');
		expect(html).not.toContain("<script");
		expect(html).not.toContain("onclick");
		expect(html).toContain("hi");
	});
});

describe("prepareSupportBody", () => {
	it("returns the cleaned body and stripped count for real text", () => {
		expect(prepareSupportBody("<p>hi</p>")).toEqual({ body: "<p>hi</p>", stripped: 0 });
	});

	it("keeps text but strips an inline image, reporting the strip", () => {
		const { body, stripped } = prepareSupportBody(
			'<p>hi<img src="data:image/png;base64,AAAA"></p>'
		);
		expect(body).toContain("hi");
		expect(body).not.toContain("data:");
		expect(stripped).toBe(1);
	});

	it("collapses an image-ONLY body to empty (so the host rejects/synthesizes it)", () => {
		// The image is stripped, leaving an empty doc — body must read as "", not
		// "<p></p>", so the thread synthesizes a files-only note instead of posting
		// a blank reply, and the new-ticket page never creates an empty-body ticket.
		const { body, stripped } = prepareSupportBody(
			'<p><img src="data:image/png;base64,AAAA"></p>'
		);
		expect(body).toBe("");
		expect(stripped).toBe(1);
	});

	it("returns empty body for a blank editor", () => {
		expect(prepareSupportBody("<p></p>")).toEqual({ body: "", stripped: 0 });
	});
});
