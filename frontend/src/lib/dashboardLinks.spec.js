import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { deskLinkUrl, openDeskLink } from "./dashboardLinks";
import { RUNTIME_JS } from "./dashboardSrcdoc";

const ORIGIN = "http://e2e2.localhost:8002";

describe("deskLinkUrl", () => {
	it.each([
		["/app/sales-invoice", `${ORIGIN}/app/sales-invoice`],
		["/app/query-report/Sales Register", `${ORIGIN}/app/query-report/Sales%20Register`],
		["/app/sales-invoice?status=Unpaid", `${ORIGIN}/app/sales-invoice?status=Unpaid`],
		["/jarvis/dashboards/x", `${ORIGIN}/jarvis/dashboards/x`],
		["/desk/sales-invoice", `${ORIGIN}/desk/sales-invoice`],
		[`${ORIGIN}/app/customer`, `${ORIGIN}/app/customer`],
	])("allows %s", (href, expected) => {
		expect(deskLinkUrl(href, ORIGIN)).toBe(expected);
	});

	it.each([
		["https://example.com/app/x"],
		["javascript:alert(1)"],
		["data:text/html,<script>alert(1)</script>"],
		["/api/method/x"],
		["/files/x.pdf"],
		["/private/files/x.pdf"],
		["/application/x"],
		["/app/../api/method/x"],
		["//evil.com/app/x"],
		["http://evil.com/app/x"],
		["/app/%2e%2e/api/method/x"],
		["/\\evil.com/app/x"],
		[" /app/x"],
		["/app/sales-invoice?cmd=jarvis.chat.dashboards_api.delete_dashboard&name=DASH-0001"],
		["/app/sales-invoice?status=Unpaid&cmd=x"],
		["/app/sales-invoice?CMD=x"],
		["/app/sales-invoice?Cmd=x"],
		["/app/sales-invoice?%63md=x"],
		["/app/sales-invoice?%43MD=x"],
		["/app/sales-invoice?a=1&cmd=x&cmd=y"],
		["/app/sales-invoice?a=1;cmd=x"],
		["/app/sales-invoice?%2563md=x"],
		["/desk/x?cmd=x"],
		["/jarvis/x?cmd=x"],
		["/app"],
		["#top"],
		[""],
		[null],
		[undefined],
		[42],
	])("refuses %s", (href) => {
		expect(deskLinkUrl(href, ORIGIN)).toBeNull();
	});

	it("keeps a cmd hidden in the fragment (never sent to the server)", () => {
		expect(deskLinkUrl("/app/sales-invoice#cmd=x", ORIGIN)).toBe(
			`${ORIGIN}/app/sales-invoice#cmd=x`
		);
	});

	it("strips tab/newline inside the href like the browser, still same-origin", () => {
		expect(deskLinkUrl("/app/\tx", ORIGIN)).toBe(`${ORIGIN}/app/x`);
		expect(deskLinkUrl("/ap\np/x", ORIGIN)).toBe(`${ORIGIN}/app/x`);
		expect(deskLinkUrl("/a\tpi/method/x", ORIGIN)).toBeNull();
	});
});

describe("openDeskLink", () => {
	const mkWin = (userActivation) => ({
		location: { origin: ORIGIN },
		navigator: { userActivation },
		open: vi.fn(),
	});

	it("opens once with noopener when activation is active", () => {
		const win = mkWin({ isActive: true });
		expect(openDeskLink("/app/sales-invoice", win)).toBe(true);
		expect(win.open).toHaveBeenCalledTimes(1);
		expect(win.open).toHaveBeenCalledWith(
			`${ORIGIN}/app/sales-invoice`,
			"_blank",
			"noopener,noreferrer"
		);
	});

	it("opens nothing without user activation (forged message)", () => {
		const win = mkWin({ isActive: false });
		expect(openDeskLink("/app/sales-invoice", win)).toBe(false);
		expect(win.open).not.toHaveBeenCalled();
	});

	it("allows when the browser has no userActivation API", () => {
		const win = mkWin(undefined);
		expect(openDeskLink("/app/x", win)).toBe(true);
	});

	it("refuses a disallowed href even when active", () => {
		const win = mkWin({ isActive: true });
		expect(openDeskLink("/api/method/x", win)).toBe(false);
		expect(win.open).not.toHaveBeenCalled();
	});
});

// The frame runtime is plain JS in a string; run it in jsdom, where
// window.parent === window, so the frame's postMessage lands on this window.
describe("frame runtime link bridge", () => {
	const received = [];
	const onMsg = (e) => e.data && e.data.type === "link" && received.push(e.data);

	afterEach(() => {
		received.length = 0;
		document.body.innerHTML = "";
	});

	// The runtime installs document listeners, so boot it once and reset per test.
	beforeAll(() => {
		window.addEventListener("message", onMsg);
		// eslint-disable-next-line no-new-func
		new Function(RUNTIME_JS)();
	});
	function boot(html) {
		document.body.innerHTML = html;
	}
	async function click(el) {
		const ev = new MouseEvent("click", { bubbles: true, cancelable: true });
		el.dispatchEvent(ev);
		await new Promise((r) => setTimeout(r, 0));
		return ev;
	}

	it("posts the raw href of a clicked anchor and cancels navigation", async () => {
		boot('<a href="/app/sales-invoice"><span id="in">View</span></a>');
		const ev = await click(document.getElementById("in"));
		expect(ev.defaultPrevented).toBe(true);
		expect(received).toHaveLength(1);
		expect(received[0]).toMatchObject({ jarvis: 1, type: "link", href: "/app/sales-invoice" });
	});

	it("ignores clicks outside an anchor and anchors without href", async () => {
		boot('<a id="a">x</a><button id="b">y</button>');
		await click(document.getElementById("a"));
		const ev = await click(document.getElementById("b"));
		expect(ev.defaultPrevented).toBe(false);
		expect(received).toHaveLength(0);
	});

	it("scrolls an in-page #fragment itself and cancels navigation", async () => {
		boot('<a id="f" href="#sec">jump</a><div id="sec">x</div>');
		const target = document.getElementById("sec");
		target.scrollIntoView = vi.fn();
		const ev = await click(document.getElementById("f"));
		expect(ev.defaultPrevented).toBe(true);
		expect(target.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
		expect(received).toHaveLength(0);
	});

	it("finds a#name targets and decodes the fragment", async () => {
		boot('<a id="f" href="#a%20b">jump</a><a name="a b">t</a>');
		const target = document.querySelector("a[name]");
		target.scrollIntoView = vi.fn();
		const ev = await click(document.getElementById("f"));
		expect(ev.defaultPrevented).toBe(true);
		expect(target.scrollIntoView).toHaveBeenCalledTimes(1);
	});

	it("scrolls to the top for # and #top, and ignores a missing target, never navigating", async () => {
		const top = vi.fn();
		document.documentElement.scrollIntoView = top;
		boot('<a id="a" href="#">t</a><a id="b" href="#top">t</a><a id="c" href="#nope">t</a>');
		for (const id of ["a", "b"]) {
			expect((await click(document.getElementById(id))).defaultPrevented).toBe(true);
		}
		expect(top).toHaveBeenCalledTimes(2);
		const ev = await click(document.getElementById("c"));
		expect(ev.defaultPrevented).toBe(true);
		expect(top).toHaveBeenCalledTimes(2);
		expect(received).toHaveLength(0);
		delete document.documentElement.scrollIntoView;
	});

	it("cancels GET form submits and posts area links", async () => {
		boot('<form id="f" action="/x"><button id="s">go</button></form>');
		const sub = new Event("submit", { bubbles: true, cancelable: true });
		document.getElementById("f").dispatchEvent(sub);
		expect(sub.defaultPrevented).toBe(true);
		boot('<map><area id="ar" href="/app/customer"></map>');
		const ev = await click(document.getElementById("ar"));
		expect(ev.defaultPrevented).toBe(true);
		expect(received[0]).toMatchObject({ type: "link", href: "/app/customer" });
	});

	it("cancels a middle-click on a link and posts nothing", async () => {
		boot('<a id="l" href="/app/customer"><span id="in">x</span></a>');
		const ev = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
		document.getElementById("in").dispatchEvent(ev);
		expect(ev.defaultPrevented).toBe(true);
		expect(received).toHaveLength(0);
	});

	it("handles SVG links that use xlink:href", async () => {
		boot(
			'<svg xmlns:xlink="http://www.w3.org/1999/xlink"><a id="s1" xlink:href="#sec"><text id="t1">a</text></a>' +
				'<a id="s2" xlink:href="/app/customer"><text id="t2">b</text></a></svg><div id="sec"></div>'
		);
		const target = document.getElementById("sec");
		target.scrollIntoView = vi.fn();
		const e1 = await click(document.getElementById("t1"));
		expect(e1.defaultPrevented).toBe(true);
		expect(target.scrollIntoView).toHaveBeenCalledTimes(1);
		expect(received).toHaveLength(0);
		const e2 = await click(document.getElementById("t2"));
		expect(e2.defaultPrevented).toBe(true);
		expect(received[0]).toMatchObject({ type: "link", href: "/app/customer" });
	});

	it("bridges window.open to a link message and returns null", async () => {
		expect(window.open("/app/x")).toBeNull();
		await new Promise((r) => setTimeout(r, 0));
		expect(received).toHaveLength(1);
		expect(received[0]).toMatchObject({ jarvis: 1, type: "link", href: "/app/x" });
	});
});
