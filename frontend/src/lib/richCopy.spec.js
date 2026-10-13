import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderMarkdown } from "@/markdown";
import { copyReply, copyRich, plainText, prepareRich } from "./richCopy";

const REPLY = [
	"## Outstanding summary",
	"",
	"You have **6** customers, see [C-1](/app/customer/C-1) and [docs](https://docs.example.com/x).",
	"",
	"1. Open the invoice",
	"2. Click `Submit`",
	"   - check the total",
	"",
	"| Customer | Outstanding |",
	"|---|--:|",
	"| Acme | 12,500 |",
	"| Bharat | 0 |",
	"",
	"```",
	"SELECT 1;",
	"",
	"",
	"",
	"SELECT 2;",
	"```",
	"",
	"```mermaid",
	"graph TD",
	"A-->B",
	"```",
].join("\n");
const html = () => renderMarkdown(REPLY);
const tree = (h) => Object.assign(document.createElement("div"), { innerHTML: h });

let saved;
beforeEach(() => {
	saved = Object.getOwnPropertyDescriptor(document, "execCommand");
});
afterEach(() => {
	if (saved) Object.defineProperty(document, "execCommand", saved);
	else delete document.execCommand;
});

// jsdom has no execCommand: stand in for a browser that fires `copy` with a
// clipboardData, records what the listener set, and reports `result`.
function fakeExecCommand({ result = true, clipboardData = true } = {}) {
	const data = { event: null };
	document.execCommand = vi.fn(() => {
		const e = new Event("copy", { cancelable: true });
		if (clipboardData) e.clipboardData = { setData: (type, v) => (data[type] = v) };
		document.dispatchEvent(e);
		data.event = e;
		return result;
	});
	return data;
}

describe("plainText / prepareRich text flavour", () => {
	it("drops Markdown markers (the reported bug)", () => {
		const { text } = prepareRich(html());
		expect(text).toContain("You have 6 customers");
		expect(text).not.toMatch(/\*\*|\|---|^#/m);
		expect(text).toContain("Outstanding summary");
	});
	it("keeps list numbers, bullets and nesting", () => {
		expect(prepareRich(html()).text).toContain(
			"1. Open the invoice\n2. Click Submit\n  - check the total"
		);
	});
	it("puts table cells on tab-separated rows", () => {
		expect(prepareRich(html()).text).toContain(
			"Customer\tOutstanding\nAcme\t12,500\nBharat\t0"
		);
	});
	it("separates blocks with a blank line without needing layout", () => {
		expect(plainText(tree("<p>a</p><p>b</p>"))).toBe("a\n\nb");
	});
	it("keeps code blocks and diagram source verbatim", () => {
		const { text } = prepareRich(html());
		expect(text).toContain("SELECT 1;\n\n\n\nSELECT 2;");
		expect(text).toContain("graph TD\nA-->B");
	});
	it("keeps a link's address when its label differs, but not for a document link", () => {
		const { text } = prepareRich(html());
		expect(text).toContain("docs (https://docs.example.com/x)");
		expect(text).toContain(`C-1 (${location.origin}/app/customer/C-1)`);
		expect(
			plainText(tree('<p><a class="jv-doclink" href="/app/sales-invoice/S-1">S-1</a></p>'))
		).toBe("S-1");
	});
	it("does not repeat an address that is already the label", () => {
		const t = plainText(
			tree(
				`<p><a href="https://x.com/">https://x.com</a> <a href="${location.origin}/app/foo%20bar">/app/foo bar</a></p>`
			)
		);
		expect(t).toBe("https://x.com /app/foo bar");
	});
	it("keeps line breaks inside a list item indented and inside a cell on the row", () => {
		expect(plainText(tree("<ol><li>one<br>two</li></ol>"))).toBe("1. one\n   two");
		expect(plainText(tree("<table><tr><td>a<br>b</td><td>c</td></tr></table>"))).toBe(
			"a b\tc"
		);
	});
	it("gives empty output for an empty reply", () => {
		expect(prepareRich("")).toEqual({ html: "", text: "" });
	});
});

describe("prepareRich html flavour", () => {
	it("keeps the formatting and makes links absolute and bare", () => {
		const out = prepareRich(html()).html;
		expect(out).toContain("<strong>6</strong>");
		expect(out).toContain("<ol>");
		expect(out).toContain(`href="${location.origin}/app/customer/C-1"`);
		expect(out).not.toMatch(/target=|rel=|title=|class=/);
	});
	it("styles tables and code inline so they survive the paste", () => {
		const out = prepareRich(html()).html;
		expect(out).toContain('<table style="border-collapse:collapse">');
		expect(out).toMatch(
			/<td style="border:1px solid #c8c8c8;padding:4px 8px;text-align:right">12,500/
		);
		expect(out).toMatch(/<pre style="font-family:/);
	});
	it("turns a diagram into a pre block", () => {
		expect(prepareRich(html()).html).toMatch(/<pre[^>]*>graph TD\nA--&gt;B<\/pre>/);
	});
	it("leaves no node behind", () => {
		const before = document.body.childElementCount;
		prepareRich(html());
		expect(document.body.childElementCount).toBe(before);
	});
});

describe("copyRich", () => {
	it("fills both flavours synchronously through the copy event", async () => {
		const data = fakeExecCommand();
		const write = vi.fn();
		await expect(
			copyRich(html(), { win: { navigator: { clipboard: { write } } } })
		).resolves.toEqual({ ok: true });
		expect(data["text/plain"]).toContain("1. Open the invoice");
		expect(data["text/html"]).toContain("<strong>6</strong>");
		expect(data.event.defaultPrevented).toBe(true);
		expect(write).not.toHaveBeenCalled();
	});
	it("removes its copy listener afterwards", async () => {
		fakeExecCommand();
		await copyRich(html(), { win: {} });
		const setData = vi.fn();
		const e = new Event("copy", { cancelable: true });
		e.clipboardData = { setData };
		document.dispatchEvent(e);
		expect(setData).not.toHaveBeenCalled();
		expect(e.defaultPrevented).toBe(false);
	});
	it("falls back to the Clipboard API when the browser refuses the copy event", async () => {
		fakeExecCommand({ result: false });
		const write = vi.fn().mockResolvedValue();
		const ClipboardItem = vi.fn(function (items) {
			this.types = Object.keys(items);
		});
		await expect(
			copyRich(html(), { win: { navigator: { clipboard: { write } }, ClipboardItem } })
		).resolves.toEqual({ ok: true });
		expect(write.mock.calls[0][0][0].types).toEqual(["text/html", "text/plain"]);
	});
	it("does not trust an execCommand that never handed over clipboardData", async () => {
		fakeExecCommand({ clipboardData: false });
		await expect(copyRich(html(), { win: {} })).resolves.toEqual({
			ok: false,
			reason: "no_clipboard_api",
		});
	});
	it("treats a throwing execCommand as a refusal", async () => {
		document.execCommand = vi.fn(() => {
			throw new Error("nope");
		});
		await expect(copyRich(html(), { win: {} })).resolves.toEqual({
			ok: false,
			reason: "no_clipboard_api",
		});
	});
	it("needs ClipboardItem as well as clipboard.write", async () => {
		fakeExecCommand({ result: false });
		const write = vi.fn();
		await expect(
			copyRich(html(), { win: { navigator: { clipboard: { write } } } })
		).resolves.toEqual({ ok: false, reason: "no_clipboard_api" });
		expect(write).not.toHaveBeenCalled();
	});
	it("resolves a reason, never rejects, when the write is refused or throws", async () => {
		fakeExecCommand({ result: false });
		const ClipboardItem = function () {};
		const denied = Object.assign(new Error("denied"), { name: "NotAllowedError" });
		const refusing = vi.fn().mockRejectedValue(denied);
		await expect(
			copyRich(html(), {
				win: { navigator: { clipboard: { write: refusing } }, ClipboardItem },
			})
		).resolves.toEqual({ ok: false, reason: "write_rejected:NotAllowedError" });
		const throwing = () => {
			throw new TypeError("bad type");
		};
		await expect(
			copyRich(html(), {
				win: { navigator: { clipboard: { write: throwing } }, ClipboardItem },
			})
		).resolves.toEqual({ ok: false, reason: "write_rejected:TypeError" });
	});
	it("resolves a reason when preparing throws", async () => {
		const bad = {
			createElement: () => {
				throw new RangeError("x");
			},
		};
		await expect(copyRich("<p>x</p>", { win: {}, doc: bad })).resolves.toEqual({
			ok: false,
			reason: "prepare_failed:RangeError",
		});
	});
	it("does not touch the clipboard for an empty reply", async () => {
		fakeExecCommand();
		await expect(copyRich("", { win: {} })).resolves.toEqual({ ok: false, reason: "empty" });
		expect(document.execCommand).not.toHaveBeenCalled();
	});
});

describe("copyReply (the chat's Copy on a reply)", () => {
	const deps = (over = {}) => ({
		render: (md) => renderMarkdown(md),
		onCopied: vi.fn(),
		onFailed: vi.fn(),
		report: vi.fn(),
		...over,
	});

	it("copies the rendered reply, not the Markdown, and ticks", async () => {
		const copy = vi.fn().mockResolvedValue({ ok: true });
		const d = deps({ copy });
		await expect(copyReply("m1", "**x**", d)).resolves.toBe(true);
		expect(copy.mock.calls[0][0]).toContain("<strong>x</strong>");
		expect(d.onCopied).toHaveBeenCalledWith("m1");
		expect(d.onFailed).not.toHaveBeenCalled();
	});
	it("does not tick on failure, and reports the reason", async () => {
		const d = deps({
			copy: vi.fn().mockResolvedValue({ ok: false, reason: "no_clipboard_api" }),
		});
		await copyReply("m1", "x", d);
		expect(d.onCopied).not.toHaveBeenCalled();
		expect(d.onFailed).toHaveBeenCalledWith("m1", "no_clipboard_api");
		expect(d.report).toHaveBeenCalledWith("no_clipboard_api");
	});
	it("does not report an empty reply as an error", async () => {
		const d = deps({ copy: vi.fn().mockResolvedValue({ ok: false, reason: "empty" }) });
		await copyReply("m1", "", d);
		expect(d.onFailed).toHaveBeenCalledWith("m1", "empty");
		expect(d.report).not.toHaveBeenCalled();
	});
	it("reports a render failure instead of throwing", async () => {
		const d = deps({
			render: () => {
				throw new SyntaxError("bad");
			},
		});
		await expect(copyReply("m1", "x", d)).resolves.toBe(false);
		expect(d.report).toHaveBeenCalledWith("render_failed:SyntaxError");
		expect(d.onFailed).toHaveBeenCalledWith("m1", "render_failed");
	});
	it("lets only the latest click move the tick", async () => {
		let releaseFirst;
		const first = new Promise((r) => (releaseFirst = r));
		const copy = vi.fn().mockReturnValueOnce(first).mockResolvedValueOnce({ ok: true });
		const d = deps({ copy });
		const a = copyReply("old", "a", d);
		await copyReply("new", "b", d);
		releaseFirst({ ok: true });
		await a;
		expect(d.onCopied.mock.calls).toEqual([["new"]]);
	});
});
