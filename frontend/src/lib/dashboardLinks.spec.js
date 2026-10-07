import { afterEach, describe, expect, it } from "vitest";
import { deskLinkUrl } from "./dashboardLinks";
import { RUNTIME_JS } from "./dashboardSrcdoc";

const ORIGIN = "http://e2e2.localhost:8002";

describe("deskLinkUrl", () => {
	it.each([
		["/app/sales-invoice", `${ORIGIN}/app/sales-invoice`],
		["/app/query-report/Sales Register", `${ORIGIN}/app/query-report/Sales%20Register`],
		["/app/sales-invoice?status=Unpaid", `${ORIGIN}/app/sales-invoice?status=Unpaid`],
		["/jarvis/dashboards/x", `${ORIGIN}/jarvis/dashboards/x`],
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
		["#top"],
		[""],
		[null],
		[undefined],
		[42],
	])("refuses %s", (href) => {
		expect(deskLinkUrl(href, ORIGIN)).toBeNull();
	});
});

// The frame runtime is plain JS in a string; run it in jsdom, where
// window.parent === window, so the frame's postMessage lands on this window.
describe("frame runtime link bridge", () => {
	const received = [];
	const onMsg = (e) => e.data && e.data.type === "link" && received.push(e.data);

	afterEach(() => {
		window.removeEventListener("message", onMsg);
		received.length = 0;
		document.body.innerHTML = "";
	});

	async function boot(html) {
		document.body.innerHTML = html;
		window.addEventListener("message", onMsg);
		// eslint-disable-next-line no-new-func
		new Function(RUNTIME_JS)();
	}
	async function click(el) {
		const ev = new MouseEvent("click", { bubbles: true, cancelable: true });
		el.dispatchEvent(ev);
		await new Promise((r) => setTimeout(r, 0));
		return ev;
	}

	it("posts the raw href of a clicked anchor and cancels navigation", async () => {
		await boot('<a href="/app/sales-invoice"><span id="in">View</span></a>');
		const ev = await click(document.getElementById("in"));
		expect(ev.defaultPrevented).toBe(true);
		expect(received).toHaveLength(1);
		expect(received[0]).toMatchObject({ jarvis: 1, type: "link", href: "/app/sales-invoice" });
	});

	it("ignores clicks outside an anchor and anchors without href", async () => {
		await boot('<a id="a">x</a><button id="b">y</button>');
		await click(document.getElementById("a"));
		const ev = await click(document.getElementById("b"));
		expect(ev.defaultPrevented).toBe(false);
		expect(received).toHaveLength(0);
	});
});
