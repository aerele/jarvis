import { test } from "node:test";
import assert from "node:assert/strict";
import { fileExt, previewKind } from "./canvas.js";

test("previewKind: the backend type wins when it is set", () => {
	assert.equal(previewKind({ type: "image/png" }), "image");
	assert.equal(previewKind({ type: "application/pdf" }), "pdf");
	assert.equal(previewKind({ type: "text/html" }), "html");
});

// "image/svg+xml" matches the image check first, so a properly typed SVG is
// rendered with <img> rather than the iframe path that "svg" takes. Pinned
// because it is load-bearing: an <img> cannot run script embedded in the SVG,
// an iframe can. An SVG only reaches the "svg" branch via its extension.
test("previewKind: a typed SVG renders as an image, not through the iframe path", () => {
	assert.equal(previewKind({ type: "image/svg+xml" }), "image");
	assert.equal(previewKind({ file_url: "/files/logo.svg" }), "svg");
});

// Agent-written artifacts don't always carry a type; falling back to the
// extension is what stops them being dropped on the floor.
test("previewKind: falls back to the extension when no type is set", () => {
	assert.equal(previewKind({ file_url: "/files/chart.PNG" }), "image");
	assert.equal(previewKind({ file_url: "/files/invoice.pdf" }), "pdf");
	assert.equal(previewKind({ file_url: "/files/logo.svg" }), "svg");
	assert.equal(previewKind({ file_url: "/files/report.html" }), "html");
	assert.equal(previewKind({ file_url: "/files/ledger.xlsx" }), "sheet");
	assert.equal(previewKind({ file_url: "/files/notes.md" }), "text");
});

test("previewKind: falls back to name when there is no file_url", () => {
	assert.equal(previewKind({ name: "photo.jpeg" }), "image");
});

test("previewKind: anything unrecognised is still shown as a file", () => {
	assert.equal(previewKind({}), "file");
	assert.equal(previewKind(null), "file");
	assert.equal(previewKind({ file_url: "/files/archive.zip" }), "file");
});

test("fileExt: uppercased extension, or FILE when there is none", () => {
	assert.equal(fileExt({ name: "INVOICE.pdf" }), "PDF");
	assert.equal(fileExt({ name: "sheet.XLSX" }), "XLSX");
	assert.equal(fileExt({ file_url: "/files/a/b/report.csv" }), "CSV");
	assert.equal(fileExt({ name: "no-extension" }), "FILE");
	assert.equal(fileExt({}), "FILE");
	assert.equal(fileExt(null), "FILE");
});
