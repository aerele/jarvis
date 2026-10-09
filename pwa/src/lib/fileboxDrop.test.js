import { test } from "node:test";
import assert from "node:assert/strict";
import { dropFiles } from "./fileboxDrop.js";

const file = (name) => ({ name });

test("dropFiles: each file is uploaded and dropped, in order", async () => {
	const calls = [];
	const upload = async (f) => (
		calls.push(`up ${f.name}`),
		{ file_url: `/f/${f.name}`, file_name: f.name, name: `F-${f.name}` }
	);
	const drop = async (url, fname, name) => (
		calls.push(`drop ${name}`), { conversation_id: `c-${fname}` }
	);
	const out = await dropFiles([file("a.pdf"), file("b.pdf")], { upload, drop });
	assert.deepEqual(calls, ["up a.pdf", "drop F-a.pdf", "up b.pdf", "drop F-b.pdf"]);
	assert.deepEqual(out, { added: ["c-a.pdf", "c-b.pdf"], failed: [] });
});

test("dropFiles: a failed file is named and the others still go", async () => {
	const upload = async (f) => {
		if (f.name === "bad.pdf") throw new Error("upload failed (413)");
		return { file_url: "/f", file_name: f.name, name: f.name };
	};
	const drop = async (url, fname) =>
		fname === "no.pdf"
			? { ok: false, reason: "Not a document" }
			: { conversation_id: `c-${fname}` };
	const out = await dropFiles([file("bad.pdf"), file("no.pdf"), file("ok.pdf")], {
		upload,
		drop,
	});
	assert.deepEqual(out, {
		added: ["c-ok.pdf"],
		failed: [
			{ name: "bad.pdf", reason: "upload failed (413)" },
			{ name: "no.pdf", reason: "Not a document" },
		],
	});
});

test("dropFiles: a refusal with no reason gets a plain one", async () => {
	const out = await dropFiles([file("x.pdf")], {
		upload: async () => ({ file_url: "/f", file_name: "x.pdf", name: "X" }),
		drop: async () => ({ ok: false }),
	});
	assert.deepEqual(out.failed, [{ name: "x.pdf", reason: "Jarvis couldn't take that file." }]);
});
