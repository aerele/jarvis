import fs from "node:fs";
import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
const start = source.indexOf("async function openArtifact(");
const body = source.slice(start, source.indexOf("\n}", start) + 2);
function deferred() {
	let resolve, reject;
	const promise = new Promise((a, b) => {
		resolve = a;
		reject = b;
	});
	return { promise, resolve, reject };
}
function harness() {
	const artifact = ref(null),
		currentId = ref("conversation790"),
		api = { previewFile: vi.fn() };
	const open = new Function(
		"artifact",
		"currentId",
		"api",
		"cvOf",
		"ensurePdfTemplates",
		"ensureCanvas",
		"tablePreviewFields",
		body + "\nreturn openArtifact;"
	)(
		artifact,
		currentId,
		api,
		() => "",
		vi.fn(),
		vi.fn(),
		(x) => x
	);
	return { artifact, currentId, api, open };
}
const file = (name) => ({ type: "file", file_url: "/private/files/" + name + ".docx" });
describe("ChatView DOCX artifact", () => {
	it("retains structured document metadata in the existing text branch", async () => {
		const h = harness();
		const document = { blocks: [{ type: "table", rows: [["A", "24"]] }] };
		h.api.previewFile.mockResolvedValue({ kind: "text", text: "A 24", document });
		await h.open({}, file("a"));
		expect(h.artifact.value.document).toEqual(document);
	});
	it.each([false, true])(
		"ignores stale success/failure for a different preview %s",
		async (fail) => {
			const h = harness(),
				a = deferred();
			h.api.previewFile
				.mockReturnValueOnce(a.promise)
				.mockResolvedValueOnce({ kind: "text", text: "New" });
			const old = h.open({}, file("a"));
			await h.open({}, file("b"));
			if (fail) a.reject(new Error("Old"));
			else a.resolve({ kind: "text", text: "Old" });
			await old;
			expect(h.artifact.value.text).toBe("New");
		}
	);
	it("does not reopen a closed preview", async () => {
		const h = harness(),
			a = deferred();
		h.api.previewFile.mockReturnValue(a.promise);
		const pending = h.open({}, file("a"));
		h.artifact.value = null;
		a.resolve({ kind: "text", text: "Old" });
		await pending;
		expect(h.artifact.value).toBeNull();
	});
});

it("closes a pending artifact if the conversation changes", async () => {
	const h = harness(),
		a = deferred();
	h.api.previewFile.mockReturnValue(a.promise);
	const pending = h.open({}, file("a"));
	h.currentId.value = "other-conversation";
	a.resolve({ kind: "text", text: "Old" });
	await pending;
	expect(h.artifact.value).toBeNull();
});
