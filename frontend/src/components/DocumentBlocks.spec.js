import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import DocumentBlocks from "./DocumentBlocks.vue";

describe("DOCX document blocks", () => {
	it("separates header text from the body without labelling an initial body", () => {
		const w = mount(DocumentBlocks, {
			props: {
				document: {
					blocks: [
						{ type: "paragraph", part: "header", text: "Company header" },
						{ type: "paragraph", part: "body", text: "Purchase requirements" },
					],
				},
			},
		});
		expect(w.findAll(".jv-document-part").map((n) => n.text())).toEqual(["Header", "Body"]);
		w.unmount();
		const body = mount(DocumentBlocks, {
			props: {
				document: {
					blocks: [{ type: "paragraph", part: "body", text: "Plain document" }],
				},
			},
		});
		expect(body.findAll(".jv-document-part")).toHaveLength(0);
		body.unmount();
	});
	it("renders text and table cells as text and keeps original download", () => {
		const w = mount(DocumentBlocks, {
			props: {
				fileUrl: "/private/files/doc.docx",
				document: {
					blocks: [
						{ type: "paragraph", text: '<img src=x onerror="window.PWNED=1">' },
						{
							type: "table",
							rows: [
								["Item", "Quantity"],
								["Cloth", "24"],
							],
						},
					],
					notes: [],
					truncated: false,
				},
			},
		});
		expect(w.find("img").exists()).toBe(false);
		expect(w.text()).toContain("<img src=x");
		expect(w.findAll("td").map((c) => c.text())).toEqual(["Item", "Quantity", "Cloth", "24"]);
		expect(w.find("a[download]").attributes("href")).toBe("/private/files/doc.docx");
		w.unmount();
	});
	it("shows truthful partial/empty notes without claiming whole-file coverage", () => {
		const w = mount(DocumentBlocks, {
			props: {
				fileUrl: "/private/files/a.docx",
				document: {
					blocks: [],
					truncated: true,
					notes: ["No readable text or tables were found."],
				},
			},
		});
		expect(w.text()).toContain("No readable text");
		expect(w.text()).toContain("Preview is incomplete");
		expect(w.find("a[download]").exists()).toBe(true);
		w.unmount();
	});
});
