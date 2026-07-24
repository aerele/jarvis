import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { reactive } from "vue";

// vi.mock is HOISTED; vi.doMock is not. Mocking the store from inside a helper
// would run AFTER the component's graph resolved, loading the real singleton
// and failing every assertion.
// `call` is stubbed too: @/api imports it at module scope, and vitest throws
// "No 'call' export is defined on the mock" for any imported name the factory
// omits — so leaving it out would break the import of supportDownloadUrl, which
// these tests deliberately exercise for real.
vi.mock("frappe-ui", () => ({
	Badge: { template: "<span/>" },
	Button: { template: "<button><slot/></button>" },
	FeatherIcon: { template: "<i/>" },
	LoadingIndicator: { template: "<i/>" },
	call: vi.fn(),
	toast: { success: vi.fn(), error: vi.fn() },
}));

// useRoute/useRouter are injection-based — global.mocks.$route does NOT feed
// them; the module has to be mocked. `routeTicket` is mutable so the C1
// switch test below can mount with a different ticket than the rest of the
// file, which all assume "T1".
let routeTicket = "T1";
// Each useRoute() call returns its OWN fresh reactive() proxy (never a shared
// singleton) — real vue-router's route is reactive, and ticketName's computed
// depends on route.params.ticket, so a plain object here would make that
// computed cache its first value forever with no way to observe a mid-flight
// route change. Per-call freshness keeps this isolated: only the CURRENTLY
// mounting instance's object is tracked in `lastRouteState`, so a later
// test's mutation can never reach an earlier test's still-mounted instance.
let lastRouteState = null;
vi.mock("vue-router", () => ({
	useRoute: () => {
		lastRouteState = reactive({ params: { ticket: routeTicket } });
		return lastRouteState;
	},
	useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

// datetime.js's formatDate/exactDate route through frappe-ui's dayjsLocal,
// which needs a systemTimezone config this test never sets up. Mock it
// directly so timestamp formatting is a pure, predictable pass-through here —
// the real tz-conversion path is covered where it's actually exercised
// (datetime.js has no dedicated suite yet, but nothing in this file asserts
// on exact formatted output, only presence/absence).
// dayLabel is mocked to a bare YYYY-MM-DD bucket (not the real "Today" /
// "Yesterday" / weekday logic — that belongs to datetime.js's own suite) so
// the day-divider tests below can assert on BUCKETING (same day vs. different
// day) without depending on wall-clock "today".
vi.mock("@/utils/datetime", () => ({
	formatDate: (v) => (v ? String(v) : ""),
	exactDate: (v) => (v ? String(v) : ""),
	dayLabel: (v) => (v ? String(v).slice(0, 10) : ""),
}));

const storeDouble = {
	tickets: [],
	thread: { ticket: "T1", messages: [], attachments: [], loading: false, error: "" },
	ticketRow: () => storeDouble.tickets[0] || null,
	badgeFor: () => ({ label: "Open", theme: "blue" }),
	isClosed: () => false,
	isAwaiting: () => false,
	fingerprintOf: () => "x",
	loadTickets: vi.fn(),
	loadThread: vi.fn(),
	closeTicket: vi.fn(),
	reply: vi.fn(async () => true),
	// Fix 2: uploadTo returns the succeeded FILE REFERENCES, not a count — a
	// realistic default double is "everything I was given succeeded".
	uploadTo: vi.fn(async (name, files) => files),
};
vi.mock("@/stores/support", () => ({ useSupportStore: () => storeDouble }));

import SupportThreadPage from "@/pages/support/SupportThreadPage.vue";

beforeEach(() => {
	vi.stubGlobal("matchMedia", () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {},
	}));
	// Assign onto URL; do NOT replace it with a spread — class statics are
	// non-enumerable, so `{ ...URL }` is `{}` and would destroy the constructor.
	URL.createObjectURL = () => "blob:x";
	URL.revokeObjectURL = () => {};
	routeTicket = "T1";
});

function mountWith(messages, ticket = { name: "T1", subject: "Broken", status: "Open" }) {
	storeDouble.tickets = [ticket];
	storeDouble.thread.messages = messages;
	return mount(SupportThreadPage, {
		global: {
			stubs: { SupportShell: { template: "<div><slot name='actions'/><slot/></div>" } },
		},
	});
}

describe("SupportThreadPage", () => {
	it("renders an agent message as a row and a customer message as a bubble", () => {
		// sent_or_received === "Sent" is the ONLY proven discriminator; if this
		// inverts, every message renders on the wrong side with no error.
		const w = mountWith([
			{ sent_or_received: "Sent", content: "<p>hi</p>", creation: "2026-07-24 10:00:00" },
			{
				sent_or_received: "Received",
				content: "<p>thanks</p>",
				creation: "2026-07-24 10:01:00",
			},
		]);
		const msgs = w.findAllComponents({ name: "Message" });
		expect(msgs[0].props("variant")).toBe("row");
		expect(msgs[1].props("variant")).toBe("bubble");
	});

	it("renders the customer bubble's HTML body, not flattened text", () => {
		// Message's bubble took `text` only before this task; passing email HTML
		// through textContent collapses <p>a</p><p>b</p> into "ab". The bubble has
		// to render the sanitized html, inside the bubble's own chrome.
		const w = mountWith([
			{ sent_or_received: "Received", content: "<p>one</p><p>two</p>", creation: "" },
		]);
		const bubble = w.find(".jv-ububble");
		expect(bubble.exists()).toBe(true);
		expect(bubble.findAll("p")).toHaveLength(2);
		expect(bubble.find(".jv-md-body").classes()).toContain("jv-html");
	});

	it("passes bodyClass=jv-html so email HTML gets the right style block", () => {
		const w = mountWith([{ sent_or_received: "Sent", content: "<p>hi</p>" }]);
		expect(w.findComponent({ name: "Message" }).props("bodyClass")).toBe("jv-html");
	});

	it("labels the agent 'Support' and never leaks the sender email", () => {
		// The payload carries `sender` as an email and no display name at all —
		// often the service account jarvis-support-bot@jarvis.internal. Rendering
		// it would leak plumbing and mislabel a human agent.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				sender: "jarvis-support-bot@jarvis.internal",
				content: "<p>hi</p>",
			},
		]);
		const m = w.findComponent({ name: "Message" });
		expect(m.props("sender")).toBe("Support");
		expect(w.text()).not.toContain("@jarvis.internal");
	});

	it("routes the body through the sanitizer — not raw content", () => {
		// THE mutation that matters: swapping renderSupportHtml(m.content) for
		// m.content leaves every other test in this file green while shipping an
		// XSS hole. supportHtml is unit-tested in isolation; this pins its USE.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: '<img src="/files/a.png" onerror="alert(1)"><script>alert(2)</script>',
			},
		]);
		const html = w.findComponent({ name: "Message" }).props("html");
		expect(html).not.toContain("onerror");
		expect(html).not.toContain("<script");
		expect(html).toContain("jarvis.support.media.download");
	});

	it("maps attachments into the shape Message actually renders", () => {
		// Message keys attachments on `name` and titles them `title`, and picks the
		// thumbnail vs. file-chip branch off `type`. The CP sends {file_url,
		// file_name} — pass those through untouched and every attachment silently
		// vanishes.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: "<p>see attached</p>",
				attachments: [
					{ file_url: "/files/shot.png", file_name: "shot.png" },
					{ file_url: "/files/log.txt", file_name: "log.txt" },
				],
			},
		]);
		const atts = w.findComponent({ name: "Message" }).props("attachments");
		expect(atts[0]).toMatchObject({
			type: "image",
			title: "shot.png",
			name: "/files/shot.png",
		});
		expect(atts[0].file_url).toContain("jarvis.support.media.download");
		// Non-images must still be present and classified — they are the primary
		// agent-reply payload (logs, PDFs), not an edge case.
		expect(atts[1]).toMatchObject({ type: "file", title: "log.txt" });
		// …and they must actually REACH the DOM: Message renders a file chip for
		// them, which is the amendment this page depends on.
		const chip = w.find(".jv-file-chip");
		expect(chip.exists()).toBe(true);
		expect(chip.text()).toContain("log.txt");
		expect(chip.attributes("href")).toContain("jarvis.support.media.download");
		// The clip icon must be an SVG (house design forbids emoji-as-icon), not
		// the 📎 glyph this chip used to render — pin against a silent regression.
		expect(chip.find("svg").exists()).toBe(true);
		expect(chip.text()).not.toContain("📎");
	});

	it("falls back to the file_url basename when file_name is missing (minor)", () => {
		// A File record with no file_name would otherwise render Message's chip
		// as an unlabeled "📎 " — the URL's own basename is still readable.
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: "<p>see attached</p>",
				attachments: [{ file_url: "/files/abc123.pdf" }],
			},
		]);
		const atts = w.findComponent({ name: "Message" }).props("attachments");
		expect(atts[0].title).toBe("abc123.pdf");
		expect(atts[0].type).toBe("file");
	});

	it("opens a message attachment in a new tab via its proxied file_url (CRITICAL fix)", () => {
		// Message renders an image attachment as a <button @click="emit('open-attachment', cv)">
		// — before this fix, nothing on this page listened, so an agent's
		// screenshot was a dead, unopenable thumbnail (unlike ticket-level
		// attachments, which are real <a> links).
		const openSpy = vi.spyOn(window, "open").mockImplementation(() => {});
		const w = mountWith([
			{
				sent_or_received: "Sent",
				content: "<p>see attached</p>",
				attachments: [{ file_url: "/files/shot.png", file_name: "shot.png" }],
			},
		]);
		w.findComponent({ name: "Message" }).vm.$emit("open-attachment", {
			file_url: "/proxied/shot.png",
		});
		expect(openSpy).toHaveBeenCalledWith("/proxied/shot.png", "_blank", "noopener");
		openSpy.mockRestore();
	});

	it("links ticket-level attachments through the authenticated proxy", () => {
		storeDouble.thread.attachments = [{ file_url: "/files/spec.pdf", file_name: "spec.pdf" }];
		const w = mountWith([{ sent_or_received: "Sent", content: "<p>hi</p>" }]);
		const link = w.find(".jv-sup-file");
		expect(link.exists()).toBe(true);
		expect(link.attributes("href")).toContain("jarvis.support.media.download");
		expect(link.attributes("href")).toContain("spec.pdf");
		storeDouble.thread.attachments = [];
	});

	it("renders a ticket-level image attachment inline and a non-image as a chip", () => {
		// Proof of fix 3: ticket-level attachments used to render as a plain
		// download chip with no image check at all, unlike per-message
		// attachments a few lines below (attachmentsOf) which already classify
		// via IMAGE_EXT. Both shapes are {file_url, file_name} from the CP.
		storeDouble.thread.attachments = [
			{ file_url: "/files/shot.png", file_name: "shot.png" },
			{ file_url: "/files/spec.pdf", file_name: "spec.pdf" },
		];
		const w = mountWith([{ sent_or_received: "Sent", content: "<p>hi</p>" }]);

		const img = w.find(".jv-sup-thumb");
		expect(img.exists()).toBe(true);
		expect(img.attributes("src")).toContain("jarvis.support.media.download");
		expect(img.attributes("src")).toContain("shot.png");

		const links = w.findAll(".jv-sup-file");
		expect(links).toHaveLength(2);
		expect(links[1].text()).toContain("spec.pdf");
		expect(links[1].find("img").exists()).toBe(false);

		storeDouble.thread.attachments = [];
	});

	it("shows an empty-conversation state instead of a blank void when there are no messages yet (minor)", () => {
		// Reachable: a brand-new ticket created with an empty body and no files
		// has zero Communications (the initial text is the HD Ticket's
		// `description`, not a reply).
		storeDouble.thread.loading = false;
		storeDouble.thread.error = "";
		const w = mountWith([]);
		expect(w.text()).toContain("start of your conversation");
	});

	it("shows a day-divider before the first message of each new day, and none between same-day messages", () => {
		const w = mountWith([
			{ sent_or_received: "Sent", content: "<p>a</p>", creation: "2026-07-23 09:00:00" },
			{ sent_or_received: "Received", content: "<p>b</p>", creation: "2026-07-23 09:05:00" },
			{ sent_or_received: "Sent", content: "<p>c</p>", creation: "2026-07-24 08:00:00" },
		]);
		const dividers = w.findAll(".jv-sup-daydivider");
		expect(dividers).toHaveLength(2);
		expect(dividers[0].text()).toBe("2026-07-23");
		expect(dividers[1].text()).toBe("2026-07-24");
	});

	it("renders no day-divider at all when every message is dateless", () => {
		const w = mountWith([
			{ sent_or_received: "Sent", content: "<p>a</p>" },
			{ sent_or_received: "Received", content: "<p>b</p>" },
		]);
		expect(w.findAll(".jv-sup-daydivider")).toHaveLength(0);
	});

	it("keeps the composer enabled on a resolved ticket and states both the Resolve and reply paths", () => {
		// There is no reopen endpoint — a reply is the ONLY way back. Disabling
		// the composer here would strand the user with no path forward. Resolved
		// gets its OWN copy (not the bare "Replying reopens this ticket." the
		// Closed case below uses): the header still shows "Awaiting you" AND the
		// Resolve button here, so the disclaimer spells out both valid next steps
		// instead of reading as if it contradicts the header.
		const w = mountWith([], { name: "T1", subject: "x", status: "Resolved" });
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toBe("Resolve to confirm and close, or reply to reopen.");
	});

	it("shows the plain reopens disclaimer for a ticket the store considers closed (not Resolved)", () => {
		storeDouble.isClosed = (s) => s === "Closed";
		const w = mountWith([], { name: "T1", subject: "x", status: "Closed" });
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toBe("Replying reopens this ticket.");
		storeDouble.isClosed = () => false; // restore the file's default double
	});

	it("arms Send for an attachment-only reply", () => {
		const w = mountWith([]);
		w.findComponent({ name: "Composer" }).vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
		]);
		return w.vm.$nextTick().then(() => {
			expect(w.findComponent({ name: "Composer" }).props("canSend")).toBe(true);
		});
	});

	it("posts the body BEFORE uploading, since upload needs the ticket to exist", async () => {
		// media.upload attaches to an EXISTING ticket and posting the text is what
		// reopens a resolved one — so this order is a correctness constraint, not
		// a style preference. Reversed, an attachment-and-text reply to a resolved
		// ticket uploads into a ticket that is still closed.
		const order = [];
		const w = mountWith([]);
		storeDouble.reply = vi.fn(async () => {
			order.push("reply");
			return true;
		});
		storeDouble.uploadTo = vi.fn(async (name, files) => {
			order.push("upload");
			return files;
		});

		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "here is the log");
		c.vm.$emit("files-added", [{ name: "log.txt", type: "text/plain" }]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(order).toEqual(["reply", "upload"]);
	});

	it("keeps the draft and the pending attachment when the reply fails", async () => {
		// The regression this pins: `draft.value = ""` moving above the
		// `if (!ok) return` guard. If that happens, modelValue below reverts to
		// "" even though nothing was ever posted, and the user's text is gone.
		storeDouble.reply = vi.fn(async () => false);
		// Fresh spy: a prior test's uploadTo call history must not leak in here —
		// nothing in this file resets mocks between tests.
		storeDouble.uploadTo = vi.fn(async (name, files) => files);
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "please help");
		c.vm.$emit("files-added", [{ name: "log.txt", type: "text/plain" }]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("modelValue")).toBe("please help");
		expect(c.props("attachments")).toHaveLength(1);
		expect(storeDouble.uploadTo).not.toHaveBeenCalled();
	});

	it("drops canSend while a reply is in flight and restores it once settled", async () => {
		// This is the double-submit guard: Composer's `busy` prop is deliberately
		// unused (see the template comment), so `canSend` going false while
		// `sending` is true is the ONLY thing standing between the user and a
		// second concurrent submit. Resolving to `false` here (reply failed, so
		// the draft is kept per the fix above) gives an unambiguous "back to true"
		// afterward without needing to re-type anything.
		let resolveReply;
		storeDouble.reply = vi.fn(() => new Promise((r) => (resolveReply = r)));
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "hello");
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await w.vm.$nextTick();

		expect(c.props("canSend")).toBe(false);

		resolveReply(false);
		await flushPromises();

		expect(c.props("canSend")).toBe(true);
	});

	it("hides the status badge and shows no reopen disclaimer for an out-of-list ticket (fix 3, row=null)", () => {
		// A deep-linked ticket outside the newest 50 the list ever fetches has no
		// row at all — get_thread carries no status, so the row is the ONLY
		// source. Rendering "Open" (badgeFor(null)'s catch-all) would be an
		// outright lie for a possibly-Closed ticket. mountWith() always wraps a
		// row into `tickets`, so mount directly with an EMPTY list (matches the
		// double's `ticketRow: () => tickets[0] || null`).
		storeDouble.tickets = [];
		storeDouble.thread.messages = [];
		storeDouble.thread.loading = false;
		storeDouble.thread.error = "";
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		routeTicket = "T1";
		const w = mount(SupportThreadPage, {
			global: {
				stubs: { SupportShell: { template: "<div><slot name='actions'/><slot/></div>" } },
			},
		});
		expect(w.findComponent({ name: "Badge" }).exists()).toBe(false);
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toBe("");
	});

	it("shows no disclaimer for an open (non-resolved, non-closed) ticket", () => {
		// Only the Resolved positive case was covered before; an unconditional
		// disclaimer (e.g. dropping the ternary's else branch) would pass that
		// test and still be wrong for the common case.
		const w = mountWith([], { name: "T1", subject: "x", status: "Open" });
		const c = w.findComponent({ name: "Composer" });
		expect(c.props("disclaimer")).toBe("");
	});

	it("keeps staged files pending when uploadTo reports fewer successes than requested", async () => {
		// Proof of fix 2: uploadTo returns the succeeded FILE REFERENCES. A
		// silent `files.value = []` here would discard attachments the user
		// still needs to retry after a transient upload failure.
		storeDouble.uploadTo = vi.fn(async () => []); // nothing succeeded
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("attachments")).toHaveLength(2);
	});

	it("clears the previous ticket's messages before a different ticket's fetch resolves (C1)", () => {
		// Without this reset, opening ticket B while A's messages are still in
		// the singleton store would show A's conversation under B's title for
		// the whole fetch — and if B's fetch failed, the error branch would
		// never show (it's gated on !thread.messages.length), stranding A's
		// conversation on B's URL with a composer that posts to B.
		storeDouble.tickets = [{ name: "T2", subject: "B", status: "Open" }];
		storeDouble.thread.ticket = "T1";
		storeDouble.thread.messages = [
			{ sent_or_received: "Sent", content: "<p>A's message</p>" },
		];
		storeDouble.thread.attachments = [{ file_url: "/files/a.png", file_name: "a.png" }];
		storeDouble.thread.error = "";
		// Never resolves within this test — proves the reset happens
		// SYNCHRONOUSLY on mount, not only after B's fetch eventually settles.
		storeDouble.loadThread = vi.fn(() => new Promise(() => {}));
		routeTicket = "T2";

		mount(SupportThreadPage, {
			global: {
				stubs: { SupportShell: { template: "<div><slot name='actions'/><slot/></div>" } },
			},
		});

		expect(storeDouble.thread.messages).toEqual([]);
		expect(storeDouble.thread.attachments).toEqual([]);
		expect(storeDouble.thread.error).toBe("");
	});

	it("posts a reply/upload started before a ticket switch to the ORIGINAL ticket, not the one switched to mid-flight", async () => {
		// The regression this pins: reading ticketName.value AFTER the awaited
		// store.reply/uploadTo below (instead of a tName snapshot taken at
		// send()'s entry) would attach this in-flight reply's body and files to
		// whatever ticket the route has moved to by the time each await
		// resolves — permanent, since there is no un-attach.
		let resolveReply;
		storeDouble.reply = vi.fn(() => new Promise((r) => (resolveReply = r)));
		storeDouble.uploadTo = vi.fn(async (name, files) => files);
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		storeDouble.fingerprintOf = vi.fn(() => "x");

		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "please help");
		c.vm.$emit("files-added", [{ name: "log.txt", type: "text/plain" }]);
		await w.vm.$nextTick();

		c.vm.$emit("submit"); // send() starts: ticketName.value is "T1" right now
		await w.vm.$nextTick();

		// The user switches to a different ticket while the reply is still in
		// flight — mutate the SAME reactive route object this mounted instance
		// captured via useRoute() (lastRouteState), same as vue-router would.
		lastRouteState.params.ticket = "T2";
		await w.vm.$nextTick();

		resolveReply(true);
		await flushPromises();

		expect(storeDouble.reply).toHaveBeenCalledWith("T1", "please help");
		expect(storeDouble.uploadTo).toHaveBeenCalledWith("T1", expect.anything());
		expect(storeDouble.uploadTo).not.toHaveBeenCalledWith("T2", expect.anything());
	});

	it("keeps text typed during an in-flight send instead of wiping it (I2)", async () => {
		// The regression this pins: an unconditional `draft.value = ""` after
		// reply() resolves wipes whatever the draft holds NOW, even if the user
		// kept typing during the multi-second send. Only a draft that still
		// equals the posted snapshot should be cleared.
		let resolveReply;
		storeDouble.reply = vi.fn(() => new Promise((r) => (resolveReply = r)));
		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("update:modelValue", "please help");
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await w.vm.$nextTick();

		// The user keeps typing while the reply is still in flight.
		c.vm.$emit("update:modelValue", "please help more");
		await w.vm.$nextTick();

		resolveReply(true);
		await flushPromises();

		expect(c.props("modelValue")).toBe("please help more");
	});
});

describe("attachment-only Send synthesizes a reply body (fix 1)", () => {
	beforeEach(() => {
		storeDouble.thread.ticket = "T1";
		storeDouble.thread.messages = [];
		storeDouble.thread.attachments = [];
		storeDouble.thread.error = "";
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		storeDouble.fingerprintOf = () => "x";
	});

	it("calls store.reply with a non-empty body BEFORE store.uploadTo when Send fires with files but no typed text", async () => {
		// CRITICAL: canSend arms on files alone, but only the reply Communication
		// reopens a Resolved/Closed ticket and notifies the agent — media.upload
		// is a bare File attach with no Communication at all. Without the
		// synthesized body, a files-only Send skipped `if (body)` entirely and
		// never posted a reply, while still reporting "success".
		const order = [];
		storeDouble.reply = vi.fn(async (name, body) => {
			order.push(["reply", body]);
			return true;
		});
		storeDouble.uploadTo = vi.fn(async (name, files) => {
			order.push(["upload"]);
			return files;
		});

		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("files-added", [{ name: "shot.png", type: "image/png" }]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(order[0][0]).toBe("reply");
		expect(typeof order[0][1]).toBe("string");
		expect(order[0][1].length).toBeGreaterThan(0);
		expect(order[1][0]).toBe("upload");
	});
});

describe("uploadTo returns succeeded File references, not a count (fix 2)", () => {
	beforeEach(() => {
		storeDouble.thread.ticket = "T1";
		storeDouble.thread.messages = [];
		storeDouble.thread.attachments = [];
		storeDouble.thread.error = "";
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		storeDouble.fingerprintOf = () => "x";
		storeDouble.reply = vi.fn(async () => true);
	});

	it("keeps only the failed file staged after a partial failure, and a retry re-uploads just that one", async () => {
		// media.upload creates a NEW File per call and there is no un-attach
		// endpoint — re-uploading a file that already landed would be a
		// permanent duplicate attachment. uploadTo already knows exactly which
		// file failed; settleUpload must remove only the succeeded ones.
		// Filter by NAME, not object identity: `files.value` is a Vue ref, so
		// elements read back out of it are reactive proxies of what was staged —
		// not the exact literal objects this test holds.
		storeDouble.uploadTo = vi.fn(async (name, files) =>
			files.filter((f) => f.name !== "b.png")
		);

		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("attachments")).toHaveLength(1);
		expect(c.props("attachments")[0].file_name).toBe("b.png");

		storeDouble.uploadTo.mockClear();
		c.vm.$emit("submit");
		await flushPromises();

		const secondCall = storeDouble.uploadTo.mock.calls[0];
		expect(secondCall[0]).toBe("T1");
		expect(secondCall[1]).toHaveLength(1);
		expect(secondCall[1][0].name).toBe("b.png");
	});
});

describe("settleUpload full-success branch", () => {
	it("clears the staged files and revokes every preview when the whole batch uploads", async () => {
		// The shortfall branch is already pinned above; the success branch was
		// not — deleting it (e.g. reverting to the old "only clear on exact
		// count match" logic backwards) still passed every other test.
		storeDouble.thread.ticket = "T1";
		storeDouble.thread.messages = [];
		storeDouble.thread.attachments = [];
		storeDouble.thread.error = "";
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		storeDouble.fingerprintOf = () => "x";
		storeDouble.reply = vi.fn(async () => true);
		storeDouble.uploadTo = vi.fn(async (name, files) => files);

		const revoke = vi.fn();
		URL.revokeObjectURL = revoke;

		const w = mountWith([]);
		const c = w.findComponent({ name: "Composer" });
		c.vm.$emit("files-added", [
			{ name: "a.png", type: "image/png" },
			{ name: "b.png", type: "image/png" },
		]);
		await w.vm.$nextTick();
		c.vm.$emit("submit");
		await flushPromises();

		expect(c.props("attachments")).toHaveLength(0);
		expect(revoke).toHaveBeenCalledTimes(2);
	});
});

describe("poll / focus / watermark subsystem (fix 3 + 4)", () => {
	async function flushMicrotasks() {
		// Fake timers don't touch the microtask queue — draining a few ticks
		// lets the chained `await`s in onMounted/open/pollSignal settle without
		// depending on @vue/test-utils' flushPromises, which is setTimeout-based
		// and would otherwise never resolve while fake timers are active.
		for (let i = 0; i < 5; i++) await Promise.resolve();
	}

	beforeEach(() => {
		storeDouble.tickets = [{ name: "T1", subject: "x", status: "Open" }];
		storeDouble.thread.ticket = "T1";
		storeDouble.thread.messages = [];
		storeDouble.thread.attachments = [];
		storeDouble.thread.error = "";
		storeDouble.loadThread = vi.fn(async () => {});
		storeDouble.loadTickets = vi.fn(async () => {});
		storeDouble.fingerprintOf = vi.fn(() => "x");
		vi.useFakeTimers();
	});

	afterEach(() => {
		vi.useRealTimers();
		Object.defineProperty(document, "hidden", { value: false, configurable: true });
	});

	async function mountAndSettle() {
		const w = mountWith([]);
		await vi.advanceTimersByTimeAsync(0);
		return w;
	}

	it("fires loadTickets on the 30s interval, and refetches the thread only when the fingerprint changed", async () => {
		const w = await mountAndSettle();
		storeDouble.loadTickets.mockClear();
		storeDouble.loadThread.mockClear();

		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadTickets).toHaveBeenCalledTimes(1);
		expect(storeDouble.loadThread).not.toHaveBeenCalled(); // fingerprint unchanged ("x")

		storeDouble.fingerprintOf = vi.fn(() => "y"); // the row changed
		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadThread).toHaveBeenCalledWith("T1", { quiet: true });
		w.unmount();
	});

	it("skips the poll entirely while document.hidden is true", async () => {
		const w = await mountAndSettle();
		storeDouble.loadTickets.mockClear();
		Object.defineProperty(document, "hidden", { value: true, configurable: true });

		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadTickets).not.toHaveBeenCalled();
		w.unmount();
	});

	it("visibilitychange triggers an unconditional refetch even when the fingerprint is unchanged", async () => {
		const w = await mountAndSettle();
		// `document` is shared across the WHOLE test file, so other mounted (and
		// not explicitly unmounted) instances may also react to this dispatch —
		// assert THIS component's effect happened via a call-count delta and the
		// expected args, not an exact global count.
		const ticketsCallsBefore = storeDouble.loadTickets.mock.calls.length;
		const threadCallsBefore = storeDouble.loadThread.mock.calls.length;
		document.dispatchEvent(new Event("visibilitychange"));
		await flushMicrotasks();

		expect(storeDouble.loadTickets.mock.calls.length).toBeGreaterThan(ticketsCallsBefore);
		expect(storeDouble.loadThread.mock.calls.length).toBeGreaterThan(threadCallsBefore);
		expect(storeDouble.loadThread).toHaveBeenCalledWith("T1", { quiet: true });
		w.unmount();
	});

	it("advances the watermark only when the refetch does not error, so a failed quiet poll doesn't stall it forever", async () => {
		storeDouble.fingerprintOf = vi.fn(() => "y"); // always reads as "changed"
		storeDouble.loadThread = vi.fn(async () => {
			storeDouble.thread.error = "boom";
		});
		const w = await mountAndSettle();
		storeDouble.loadThread.mockClear();

		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadThread).toHaveBeenCalledTimes(1);

		// lastPrint never advanced past "" (the refetch errored), so the SAME
		// fingerprint "y" must still register as "changed" on the next tick.
		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadThread).toHaveBeenCalledTimes(2);
		storeDouble.thread.error = "";
		w.unmount();
	});

	it("falls back to an unconditional quiet refetch for an out-of-list ticket (row=null), where the fingerprint can never change", async () => {
		// fingerprintOf(name) returns "" forever when ticketRow(name) is null (a
		// deep-linked ticket outside the newest 50) — `print !== lastPrint` can
		// never fire, so without the row=null fallback this poll is permanently
		// dead for exactly the ticket that most needs it (an agent reply is the
		// only way it'd ever change).
		// mountWith()/mountAndSettle() always force a truthy ticket into
		// `storeDouble.tickets`, so mount directly instead — matches the double's
		// `ticketRow: () => tickets[0] || null`.
		storeDouble.tickets = [];
		storeDouble.fingerprintOf = vi.fn(() => "");
		const w = mount(SupportThreadPage, {
			global: {
				stubs: { SupportShell: { template: "<div><slot name='actions'/><slot/></div>" } },
			},
		});
		await vi.advanceTimersByTimeAsync(0);
		storeDouble.loadThread.mockClear();

		await vi.advanceTimersByTimeAsync(30000);
		expect(storeDouble.loadThread).toHaveBeenCalledWith("T1", { quiet: true });
		w.unmount();
	});

	it("clears the interval on unmount", async () => {
		const clearSpy = vi.spyOn(global, "clearInterval");
		const w = await mountAndSettle();
		w.unmount();
		expect(clearSpy).toHaveBeenCalled();
		clearSpy.mockRestore();
	});
});
