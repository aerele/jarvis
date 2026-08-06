import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * Attachment upload: one path, a visible loader, and failures that are SHOWN.
 *
 * The reported symptom was "audio files appear in the picker but never upload".
 * The picker has no `accept` filter and the server has no extension allow-list
 * (System Settings.allowed_file_extensions is empty), so nothing type-specific
 * was rejecting audio. What made it look that way was the error handling: the
 * uploader caught every failure into an empty block, so a refused file simply
 * never appeared and the UI said nothing at all. Whatever the server's reason
 * (an oversize recording, an expired session), the user saw a dead button.
 */

const SRC = path.resolve(__dirname, "../views/ChatView.vue");
const COMPOSER = path.resolve(__dirname, "../components/chat/Composer.vue");
const src = fs.readFileSync(SRC, "utf8");
const composer = fs.readFileSync(COMPOSER, "utf8");

describe("upload failures are surfaced, never swallowed", () => {
	it("no empty catch remains on an upload path", () => {
		// THE bug. Both the picker path and the paste path had one.
		expect(src).not.toContain("/* skip a file that failed to upload */");
	});

	it("a failed upload records the reason and tells the user", () => {
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("failedUploads.value = [");
		expect(body).toContain("notify(");
		expect(body).toContain("errMessage(err)");
	});

	it("the in-flight pill always clears, even when the upload throws", () => {
		// Without the finally, one failure would leave a spinner running forever.
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("} finally {");
		expect(body).toContain("uploadingFiles.value = uploadingFiles.value.filter");
	});

	it("oversize files fail with a readable sentence rather than a bare 413", () => {
		expect(src).toContain("const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;");
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		expect(fn.slice(0, fn.indexOf("\n}"))).toContain("MAX_UPLOAD_BYTES");
	});
});

describe("every attach type goes through the same uploader", () => {
	it("picker and drag-drop route through uploadFiles", () => {
		expect(src).toContain('@files-added="uploadFiles"');
	});

	it("clipboard paste routes through it too, instead of its own copy", () => {
		// The paste handler used to inline its own upload loop with its own empty
		// catch, so a fix to the picker path silently missed pasted images.
		const fn = src.slice(src.indexOf("async function onPaste(e) {"));
		const body = fn.slice(0, fn.indexOf("\n}\n"));
		expect(body).toContain("await uploadFiles(");
		expect(body).not.toContain("api.uploadFile(");
	});

	it("uploads run concurrently, so one slow file does not block the batch", () => {
		const fn = src.slice(src.indexOf("async function uploadFiles(list) {"));
		expect(fn.slice(0, fn.indexOf("\n}"))).toContain("Promise.all");
	});
});

describe("the loader is the product's one spinner", () => {
	it("the composer renders JvSpinner for an in-flight attachment", () => {
		// JvSpinner is distilled from the onboarding completion animation and is
		// documented as the ONE loading indicator; a bare "Uploading…" word was
		// the odd one out.
		expect(composer).toContain('import JvSpinner from "@/components/JvSpinner.vue";');
		const at = composer.indexOf('v-if="a.uploading"');
		expect(at).toBeGreaterThan(-1);
		expect(composer.slice(at, at + 400)).toContain("<JvSpinner");
	});

	it("the pill names the file it belongs to", () => {
		expect(composer).toContain("Uploading ${a.file_name}…");
		expect(src).toContain("uploading: true, file_name: u.name");
	});

	it("a failed attachment renders its own alert chip", () => {
		expect(composer).toContain('v-else-if="a.failed"');
		expect(composer).toContain('role="alert"');
	});
});

describe("a slow upload stays with the chat it was started in", () => {
	it("the result is discarded if the user has moved to another conversation", () => {
		// The reported leak: a 3-minute voice memo attached in one chat resolved
		// after the user opened a NEW chat and pushed itself into that tray, so a
		// brand-new conversation silently inherited the previous one's attachment.
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("const forConv = currentId.value;");
		expect(body).toContain("if (currentId.value !== forConv) return;");
		// The captured id must be read BEFORE the awaited upload, or it is useless.
		expect(body.indexOf("const forConv")).toBeLessThan(body.indexOf("await api.uploadFile"));
	});

	it("a failure toast is suppressed once the user has left that chat", () => {
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("if (currentId.value === forConv) {");
	});

	it("the in-flight pill is scoped to its own conversation", () => {
		expect(src).toContain("if (u.conv === currentId.value)");
	});
});

describe("re-picking a file mid-upload does not duplicate it", () => {
	it("an identical in-flight file is refused rather than uploaded again", () => {
		// A 3-minute recording uploads slowly enough that an impatient re-click is
		// the normal reaction; each click used to start another full upload, which
		// is how one recording ended up attached five times.
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("if (_inflightKeys.has(key)) {");
		expect(body).toContain("is already uploading");
	});

	it("identity is bytes, not just the name", () => {
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("file && file.size");
		expect(body).toContain("file && file.lastModified");
	});

	it("the key is released in finally, so a failed upload can be retried", () => {
		const fn = src.slice(src.indexOf("async function uploadOne(file) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		const fin = body.indexOf("} finally {");
		expect(body.indexOf("_inflightKeys.delete(key)")).toBeGreaterThan(fin);
	});
});

describe("removing an attachment addresses the right list", () => {
	it("the composer's flat index is mapped back onto the owning list", () => {
		// Chips are pendingFiles, then in-flight pills, then failures. Treating
		// that flat index as a pendingFiles index would remove the wrong file.
		expect(src).toContain('@remove-attachment="removeAttachment"');
		const fn = src.slice(src.indexOf("function removeAttachment(i) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain("if (i < pendingFiles.value.length) return removeFile(i);");
		// It must count the pills actually RENDERED. The tray hides uploads owned
		// by another conversation, so using the full in-flight length would shift
		// the failure index and remove the wrong chip.
		expect(body).toContain("u.conv === currentId.value");
		expect(body).toContain("visibleUploading");
	});

	it("stale failure pills do not follow the user into a new chat", () => {
		expect(src).toContain("failedUploads.value = []");
	});
});
