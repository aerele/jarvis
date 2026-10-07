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
		["/app"],
		["#top"],
		[""],
		[null],
		[undefined],
		[42],
	])("refuses %s", (href) => {
		expect(deskLinkUrl(href, ORIGIN)).toBeNull();
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
			"noopener,noreferrer",
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

	it("leaves in-page #fragment anchors alone", async () => {
		boot('<a id="f" href="#sec">jump</a>');
		const ev = await click(document.getElementById("f"));
		expect(ev.defaultPrevented).toBe(false);
		expect(received).toHaveLength(0);
	});

	it("bridges window.open to a link message and returns null", async () => {
		expect(window.open("/app/x")).toBeNull();
		await new Promise((r) => setTimeout(r, 0));
		expect(received).toHaveLength(1);
		expect(received[0]).toMatchObject({ jarvis: 1, type: "link", href: "/app/x" });
	});
});
