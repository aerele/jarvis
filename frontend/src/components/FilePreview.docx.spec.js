import { describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { ref } from "vue";
vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue"],
		template: '<div v-if="modelValue"><slot name="body"/></div>',
	},
	Button: {
		props: ["label", "icon"],
		template:
			'<button :aria-label="icon || label" @click="$emit(\'click\')">{{label}}</button>',
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/theme", () => ({ useJarvisTheme: () => ({ effectiveDark: ref(false) }) }));
const previewFile = vi.hoisted(() => vi.fn());
vi.mock("@/api", () => ({ previewFile }));
import FilePreview from "./FilePreview.vue";
const doc = (text) => ({
	kind: "text",
	text,
	document: {
		blocks: [
			{ type: "paragraph", text },
			{ type: "table", rows: [["Cloth", "24"]] },
		],
		notes: [],
		truncated: false,
	},
});
async function open() {
	const w = mount(FilePreview, {
		props: { modelValue: false, fileUrl: "/private/files/a.docx", fileName: "a.docx" },
	});
	await w.setProps({ modelValue: true });
	await flushPromises();
	return w;
}
function deferred() {
	let resolve, reject;
	const promise = new Promise((a, b) => {
		resolve = a;
		reject = b;
	});
	return { promise, resolve, reject };
}
describe("DOCX FilePreview", () => {
	it("shows structured DOCX instead of a raw text dump", async () => {
		previewFile.mockResolvedValue(doc("Requirements"));
		const w = await open();
		expect(w.find("table").exists()).toBe(true);
		expect(w.text()).toContain("Requirements");
		expect(w.find("a[download]").attributes("href")).toContain("a.docx");
		w.unmount();
	});
	it("shows a DOCX parse refusal and keeps download", async () => {
		previewFile.mockResolvedValue({
			kind: "binary",
			format: "docx",
			error: "DOCX is encrypted. Supply an unlocked copy.",
		});
		const w = await open();
		expect(w.text()).toContain("DOCX is encrypted");
		expect(w.find("a[download]").exists()).toBe(true);
		w.unmount();
	});
	it.each([false, true])("ignores stale %s after a different file opens", async (fail) => {
		const a = deferred();
		previewFile.mockReturnValueOnce(a.promise).mockResolvedValueOnce(doc("New document"));
		const w = await open();
		await w.setProps({ fileUrl: "/private/files/b.docx" });
		await flushPromises();
		if (fail) a.reject(new Error("Old failure"));
		else a.resolve(doc("Old private document"));
		await flushPromises();
		expect(w.text()).toContain("New document");
		expect(w.text()).not.toContain("Old");
		w.unmount();
	});
});

it("ignores a pending result after close and reopen", async () => {
	const a = deferred();
	previewFile.mockReturnValueOnce(a.promise).mockResolvedValueOnce(doc("Reopened"));
	const w = await open();
	await w.setProps({ modelValue: false });
	await w.setProps({ modelValue: true });
	await flushPromises();
	a.resolve(doc("Closed old content"));
	await flushPromises();
	expect(w.text()).toContain("Reopened");
	expect(w.text()).not.toContain("Closed old");
	w.unmount();
});
