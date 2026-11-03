import { describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
vi.mock("../../../pwa/src/components/Sheet.vue", () => ({
	default: { props: ["open"], template: '<div v-if="open"><slot/></div>' },
}));
vi.mock("../../../pwa/src/components/CanvasFrame.vue", () => ({
	default: { template: "<div/>" },
}));
const previewFile = vi.hoisted(() => vi.fn());
vi.mock("../../../pwa/src/api", () => ({
	previewFile,
	listPdfTemplates: vi.fn(async () => ({ data: { templates: [] } })),
}));
import FilePreviewSheet from "../../../pwa/src/components/FilePreviewSheet.vue";
const doc = (text) => ({
	kind: "text",
	text,
	document: {
		blocks: [
			{ type: "paragraph", text },
			{
				type: "table",
				rows: [
					["Item", "Qty"],
					["Cloth", "24"],
				],
			},
		],
		notes: [],
		truncated: false,
	},
});
function deferred() {
	let resolve, reject;
	const promise = new Promise((a, b) => {
		resolve = a;
		reject = b;
	});
	return { promise, resolve, reject };
}
describe("PWA DOCX preview", () => {
	it("renders the shared structured table and keeps original download", async () => {
		previewFile.mockResolvedValue(doc("Phone document"));
		const w = mount(FilePreviewSheet, {
			props: { item: { file_url: "/private/files/a.docx", title: "a.docx", type: "file" } },
		});
		await flushPromises();
		expect(w.find("table").exists()).toBe(true);
		expect(w.text()).toContain("Phone document");
		expect(w.find("a[download]").attributes("href")).toContain("a.docx");
		w.unmount();
	});
	it.each([false, true])("ignores old success/failure after file changes (%s)", async (fail) => {
		const a = deferred();
		previewFile
			.mockReturnValueOnce(a.promise)
			.mockResolvedValueOnce(doc("New phone document"));
		const w = mount(FilePreviewSheet, {
			props: { item: { file_url: "/private/files/a.docx", type: "file" } },
		});
		await w.setProps({ item: { file_url: "/private/files/b.docx", type: "file" } });
		await flushPromises();
		if (fail) a.reject(new Error("Old failure"));
		else a.resolve(doc("Old private data"));
		await flushPromises();
		expect(w.text()).toContain("New phone document");
		expect(w.text()).not.toContain("Old");
		w.unmount();
	});
});

it("ignores a pending reply after closing the phone sheet", async () => {
	const a = deferred();
	previewFile.mockReturnValueOnce(a.promise).mockResolvedValueOnce(doc("Reopened phone"));
	const w = mount(FilePreviewSheet, {
		props: { item: { file_url: "/private/files/a.docx", type: "file" } },
	});
	await w.setProps({ item: null });
	await w.setProps({ item: { file_url: "/private/files/b.docx", type: "file" } });
	await flushPromises();
	a.resolve(doc("Closed old phone"));
	await flushPromises();
	expect(w.text()).toContain("Reopened phone");
	expect(w.text()).not.toContain("Closed old phone");
	w.unmount();
});
