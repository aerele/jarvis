import { describe, it, expect, vi, afterEach } from "vitest";

vi.mock("frappe-ui", () => ({ call: vi.fn(async () => ({})) }));

import {
	errMessage,
	messageText,
	firstServerMessage,
	MAX_MESSAGE_CHARS,
	GENERIC_ERROR_MESSAGE,
} from "./errors.js";
import { transcribeAudio } from "@/api/voice.js";

/**
 * Server messages are decoded to bounded plain text through a parser that runs
 * nothing, and the nested _server_messages payload is read without ever throwing.
 */

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe("messageText", () => {
	it("decodes entities and drops tags, keeping text only", () => {
		expect(messageText("Settings -&gt; Developer")).toBe("Settings -> Developer");
		expect(messageText("<b>Bold</b> and <i>plain</i>")).toBe("Bold and plain");
		expect(messageText("  padded  ")).toBe("padded");
	});

	it("never builds elements by assigning markup to a live element", () => {
		const setter = vi.fn();
		const real = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
		Object.defineProperty(Element.prototype, "innerHTML", {
			configurable: true,
			get: real.get,
			set: setter,
		});
		try {
			messageText('<img src=x onerror="window.__ran = 1">hello');
			errMessage({ messages: ['<img src=x onerror="window.__ran = 1">hello'] });
		} finally {
			Object.defineProperty(Element.prototype, "innerHTML", real);
		}
		expect(setter).not.toHaveBeenCalled();
		expect(window.__ran).toBeUndefined();
	});

	it("returns text that cannot be read back as markup", () => {
		const out = messageText("&lt;img src=x onerror=alert(1)&gt;");
		// Plain text: the caller's text sink shows it literally.
		expect(out).toBe("<img src=x onerror=alert(1)>");
		const host = document.createElement("div");
		host.textContent = out;
		expect(host.querySelector("img")).toBeNull();
	});

	it("returns nothing for markup with no text, so the caller's own sentence is used", () => {
		expect(messageText("<img src=x onerror=1>")).toBe("");
		expect(errMessage({ message: "<img src=x onerror=1>" })).toBe(GENERIC_ERROR_MESSAGE);
		expect(errMessage({ message: "<br>" }, "Could not save.")).toBe("Could not save.");
	});

	it("keeps a long, legitimate message whole", () => {
		const long = "Row 12 is missing a value. ".repeat(100);
		expect(messageText(long)).toBe(long.trim());
	});

	it("bounds the length", () => {
		const out = messageText("x".repeat(500000));
		expect(out.length).toBe(MAX_MESSAGE_CHARS);
		expect(out.endsWith("…")).toBe(true);
		expect(messageText("short")).toBe("short");
	});

	it("is total for odd input", () => {
		expect(messageText(null)).toBe("");
		expect(messageText(undefined)).toBe("");
		expect(messageText(42)).toBe("42");
		expect(messageText({})).toBe("[object Object]");
	});
});

describe("errMessage", () => {
	it("returns bounded text for a long or tagged server message", () => {
		expect(errMessage({ messages: ["<p>Could not save &amp; close</p>"] })).toBe(
			"Could not save & close"
		);
		expect(errMessage({ message: "y".repeat(90000) }).length).toBeLessThanOrEqual(
			MAX_MESSAGE_CHARS
		);
	});

	it("uses the fallback when the message is not a string", () => {
		expect(errMessage({ messages: [{ not: "text" }] })).toBe(GENERIC_ERROR_MESSAGE);
		expect(errMessage({ messages: [{ not: "text" }] }, "Nope")).toBe("Nope");
	});
});

describe("firstServerMessage", () => {
	const wrap = (msg) => JSON.stringify([JSON.stringify({ message: msg })]);

	it("reads the usual nested payload", () => {
		expect(firstServerMessage(wrap("Too big"))).toBe("Too big");
	});

	it("reads an array of plain objects too", () => {
		expect(firstServerMessage(JSON.stringify([{ message: "Direct" }]))).toBe("Direct");
	});

	it("returns an empty string, never throws, for anything else", () => {
		for (const bad of [
			undefined,
			null,
			42,
			{},
			"",
			"not json",
			"[]",
			"[1]",
			'["not json"]',
			'["{}"]',
			"[null]",
			JSON.stringify([JSON.stringify({ message: { a: 1 } })]),
			JSON.stringify([JSON.stringify([1, 2])]),
			"[".repeat(50000),
		]) {
			expect(firstServerMessage(bad)).toBe("");
		}
	});

	it("does not parse an oversized payload", () => {
		const parse = vi.spyOn(JSON, "parse");
		expect(firstServerMessage(wrap("z".repeat(300000)))).toBe("");
		expect(parse).not.toHaveBeenCalled();
	});
});

describe("the voice upload error path", () => {
	async function failWith(body, status = 417) {
		vi.stubGlobal("window", Object.assign(window, { csrf_token: "t" }));
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => ({ ok: false, status, json: async () => body }))
		);
		try {
			await transcribeAudio(new Blob(["a"], { type: "audio/webm" }), { durationS: 1 });
		} catch (e) {
			return e.message;
		}
		return null;
	}

	it("shows the server's sentence as text", async () => {
		const body = {
			_server_messages: JSON.stringify([
				JSON.stringify({ message: "<b>Audio</b> too long" }),
			]),
		};
		expect(await failWith(body)).toBe("Audio too long");
	});

	it("falls back to the status for a malformed payload", async () => {
		expect(await failWith({ _server_messages: '["{bad"]' })).toBe(
			"transcription failed (417)"
		);
		expect(await failWith({ exception: 7 })).toBe("transcription failed (417)");
	});

	it("takes the last part of an exception line as text", async () => {
		expect(await failWith({ exception: "frappe.ValidationError: <i>No speech</i>" })).toBe(
			"No speech"
		);
	});
});
