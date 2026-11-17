import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import fs from "fs";
import path from "path";
// The two modules the apps use for requests, imported by file: the package entry pulls in
// every component and does not resolve under vitest.
import call from "../../node_modules/frappe-ui/src/utils/call.js";
import FileUploadHandler from "../../node_modules/frappe-ui/src/utils/fileUploadHandler.ts";

import { CLIENT_HEADER, install, isSameOriginApi } from "./clientHeader";

const INSTALLED = Symbol.for("jarvis.clientHeader");

/** A window-shaped object with a recording fetch and a recording XMLHttpRequest. */
function fakeWindow() {
	const fetched = [];
	class FakeXhr {
		constructor() {
			this.headers = {};
			this.sent = false;
			this.opened = null;
		}
		open(method, url) {
			this.opened = { method, url };
		}
		setRequestHeader(name, value) {
			if (!this.opened) throw new Error("InvalidStateError");
			this.headers[name] = value;
		}
		send() {
			this.sent = true;
		}
	}
	const win = {
		location: new URL("https://erp.example.com/jarvis/chat"),
		Headers,
		Request,
		XMLHttpRequest: FakeXhr,
		fetch: vi.fn(async (input, init) => {
			fetched.push({ input, init });
			return { ok: true };
		}),
	};
	win.fetched = fetched;
	return win;
}

const sentHeader = (entry) => new Headers(entry.init.headers).get(CLIENT_HEADER);

describe("isSameOriginApi", () => {
	const win = fakeWindow();
	it("matches /api/ on this origin, relative or absolute", () => {
		expect(isSameOriginApi("/api/method/x", win)).toBe(true);
		expect(isSameOriginApi("api/method/x", win)).toBe(false); // resolves to /jarvis/api/...
		expect(isSameOriginApi("https://erp.example.com/api/method/x", win)).toBe(true);
		expect(isSameOriginApi(new URL("https://erp.example.com/api/resource/ToDo"), win)).toBe(
			true
		);
	});
	it("rejects other origins, other paths and garbage", () => {
		expect(isSameOriginApi("https://evil.example.org/api/method/x", win)).toBe(false);
		expect(isSameOriginApi("http://erp.example.com/api/method/x", win)).toBe(false); // other scheme
		expect(isSameOriginApi("/app/todo", win)).toBe(false);
		expect(isSameOriginApi("/apix/y", win)).toBe(false);
		expect(isSameOriginApi("//evil.example.org/api/x", win)).toBe(false);
		expect(isSameOriginApi(undefined, win)).toBe(false);
		expect(isSameOriginApi("http://[bad", win)).toBe(false);
	});
});

describe("fetch", () => {
	let win;
	beforeEach(() => {
		win = fakeWindow();
		install("desktop", win);
	});

	it("marks a same-origin /api/ call and keeps the caller's headers and body", async () => {
		await win.fetch("/api/method/jarvis.chat.api.send_message", {
			method: "POST",
			headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": "tok" },
			body: "{}",
		});
		const [entry] = win.fetched;
		expect(sentHeader(entry)).toBe("desktop");
		const headers = new Headers(entry.init.headers);
		expect(headers.get("Content-Type")).toBe("application/json");
		expect(headers.get("X-Frappe-CSRF-Token")).toBe("tok");
		expect(entry.init.method).toBe("POST");
		expect(entry.init.body).toBe("{}");
	});

	it("marks a call with no init at all, and an absolute same-origin URL", async () => {
		await win.fetch("/api/method/ping");
		await win.fetch("https://erp.example.com/api/method/ping", { method: "GET" });
		expect(win.fetched.map(sentHeader)).toEqual(["desktop", "desktop"]);
	});

	it("takes Headers instances and header arrays", async () => {
		await win.fetch("/api/x", { headers: new Headers({ A: "1" }) });
		await win.fetch("/api/x", { headers: [["B", "2"]] });
		const [one, two] = win.fetched;
		expect(new Headers(one.init.headers).get("A")).toBe("1");
		expect(new Headers(two.init.headers).get("B")).toBe("2");
		expect(win.fetched.map(sentHeader)).toEqual(["desktop", "desktop"]);
	});

	it("a Request object keeps its own headers and gets ours", async () => {
		const req = new Request("https://erp.example.com/api/method/x", {
			method: "POST",
			headers: { "X-Keep": "yes" },
			body: "{}",
		});
		await win.fetch(req);
		const [entry] = win.fetched;
		expect(entry.input).toBeInstanceOf(Request);
		expect(entry.input.headers.get(CLIENT_HEADER)).toBe("desktop");
		expect(entry.input.headers.get("X-Keep")).toBe("yes");
		expect(entry.input.method).toBe("POST");
	});

	it("leaves cross-origin and non-API calls exactly as they were", async () => {
		const init = { headers: { A: "1" } };
		await win.fetch("https://other.example.org/api/method/x", init);
		await win.fetch("/assets/jarvis/x.js");
		await win.fetch("/app/todo", init);
		const [cross, asset, app] = win.fetched;
		expect(cross.init).toBe(init); // the very same object, untouched
		expect(new Headers(init.headers).has(CLIENT_HEADER)).toBe(false);
		expect(asset.init).toBeUndefined();
		expect(app.init).toBe(init);
	});

	it("never fails the call when the header cannot be added", async () => {
		win = fakeWindow();
		win.Headers = function Broken() {
			throw new Error("no Headers");
		};
		install("pwa", win);
		const res = await win.fetch("/api/method/x", { method: "POST" });
		expect(res).toEqual({ ok: true });
		expect(win.fetched[0].init).toEqual({ method: "POST" }); // went out unmarked
	});

	it("installs once: a second install does not wrap again or change the client", async () => {
		const wrapped = win.fetch;
		install("pwa", win);
		expect(win.fetch).toBe(wrapped);
		await win.fetch("/api/x");
		expect(sentHeader(win.fetched[0])).toBe("desktop");
	});

	it("uses the client it was installed with", async () => {
		const other = fakeWindow();
		install("pwa", other);
		await other.fetch("/api/x");
		expect(sentHeader(other.fetched[0])).toBe("pwa");
	});
});

describe("XMLHttpRequest", () => {
	let win;
	beforeEach(() => {
		win = fakeWindow();
		install("pwa", win);
	});

	it("marks a same-origin /api/ upload before it is sent", () => {
		const xhr = new win.XMLHttpRequest();
		xhr.open("POST", "/api/method/upload_file", true);
		xhr.setRequestHeader("Accept", "application/json");
		xhr.send("form");
		expect(xhr.headers[CLIENT_HEADER]).toBe("pwa");
		expect(xhr.headers.Accept).toBe("application/json");
		expect(xhr.sent).toBe(true);
		expect(xhr.opened).toEqual({ method: "POST", url: "/api/method/upload_file" });
	});

	it("leaves cross-origin and non-API requests unmarked", () => {
		for (const url of ["https://other.example.org/api/method/upload_file", "/files/x.png"]) {
			const xhr = new win.XMLHttpRequest();
			xhr.open("GET", url);
			xhr.send();
			expect(xhr.headers[CLIENT_HEADER]).toBeUndefined();
			expect(xhr.sent).toBe(true);
		}
	});

	it("an XHR object re-opened on another URL is judged again", () => {
		const xhr = new win.XMLHttpRequest();
		xhr.open("POST", "/api/method/x");
		xhr.open("GET", "https://other.example.org/api/method/x");
		xhr.send();
		expect(xhr.headers[CLIENT_HEADER]).toBeUndefined();
	});

	it("a send that cannot take the header still goes out", () => {
		const xhr = new win.XMLHttpRequest();
		xhr.open("POST", "/api/method/x");
		xhr.setRequestHeader = () => {
			throw new Error("InvalidStateError");
		};
		expect(() => xhr.send()).not.toThrow();
		expect(xhr.sent).toBe(true);
	});
});

// The two real consumers, against jsdom's own globals: what the apps actually use.
describe("frappe-ui", () => {
	let nativeFetch, nativeOpen, nativeSend, nativeSetHeader, recorded;

	beforeEach(() => {
		nativeFetch = globalThis.fetch;
		const proto = globalThis.XMLHttpRequest.prototype;
		nativeOpen = proto.open;
		nativeSend = proto.send;
		nativeSetHeader = proto.setRequestHeader;
		recorded = { fetches: [], xhrHeaders: [] };
		globalThis.fetch = vi.fn(async (input, init) => {
			recorded.fetches.push({ input, init });
			return { ok: true, json: async () => ({ message: "pong" }) };
		});
		proto.send = vi.fn(); // no network from jsdom
		proto.setRequestHeader = function (name, value) {
			recorded.xhrHeaders.push([name, value]);
		};
		delete globalThis[INSTALLED];
		install("desktop", globalThis);
	});

	afterEach(() => {
		const proto = globalThis.XMLHttpRequest.prototype;
		globalThis.fetch = nativeFetch;
		proto.open = nativeOpen;
		proto.send = nativeSend;
		proto.setRequestHeader = nativeSetHeader;
		delete globalThis[INSTALLED];
	});

	it("a frappe-ui call() request carries the header", async () => {
		await call("jarvis.chat.api.list_conversations", { a: 1 });
		const [{ input, init }] = recorded.fetches;
		expect(input).toBe("/api/method/jarvis.chat.api.list_conversations");
		expect(new Headers(init.headers).get(CLIENT_HEADER)).toBe("desktop");
		expect(new Headers(init.headers).get("X-Frappe-Site-Name")).toBeTruthy();
	});

	it("a FileUploader upload (XHR to /api/method/upload_file) carries the header", () => {
		const file = new File(["x"], "a.txt", { type: "text/plain" });
		new FileUploadHandler().upload(file, {});
		expect(recorded.xhrHeaders).toContainEqual([CLIENT_HEADER, "desktop"]);
		expect(recorded.xhrHeaders).toContainEqual(["Accept", "application/json"]);
	});

	it("an upload to another origin does not", () => {
		const file = new File(["x"], "a.txt");
		new FileUploadHandler().upload(file, {
			upload_endpoint: "https://cdn.example.org/api/method/upload_file",
		});
		expect(recorded.xhrHeaders.map(([n]) => n)).not.toContain(CLIENT_HEADER);
	});
});

// Both apps install it as their FIRST import, so nothing can fire a request unmarked.
describe("wiring", () => {
	const read = (rel) => fs.readFileSync(path.resolve(__dirname, rel), "utf8");
	const firstImport = (src) => src.split("\n").find((l) => /^import /.test(l));

	it("the desktop SPA installs it first, as desktop", () => {
		expect(firstImport(read("../main.js"))).toContain("./lib/clientHeaderDesktop");
		expect(read("./clientHeaderDesktop.js")).toContain('install("desktop")');
	});

	it("the PWA installs it first, as pwa, from the shared copy", () => {
		expect(firstImport(read("../../../pwa/src/main.js"))).toContain("./lib/clientHeaderPwa");
		const entry = read("../../../pwa/src/lib/clientHeaderPwa.js");
		expect(entry).toContain('from "@shared/lib/clientHeader"');
		expect(entry).toContain('install("pwa")');
	});
});
