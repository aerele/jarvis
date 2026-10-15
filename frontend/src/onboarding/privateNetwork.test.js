import test from "node:test";
import assert from "node:assert/strict";

import {
	SERVER_HOLD_DAYS,
	SETUP_UNAVAILABLE,
	validateAddress,
	hostnameOf,
	publicHostOf,
	adminRuleRows,
	serverRuleRows,
	rulesToText,
	testPassed,
	describeServerTest,
	holdEndedBanner,
	HOLD_UNAVAILABLE,
	checkAddress,
	refusalOf,
	adminCheckState,
	GENERIC_REFUSAL,
} from "./privateNetwork.js";
import { PARITY_ACCEPT, PARITY_REFUSE, TEST_MODE_ACCEPT, TEST_MODE_REFUSE } from "./privateNetworkParity.js";

const EMPTY = "Enter the public address of your ERPNext.";
const HTTP = "Use an https:// address. Plain HTTP is not allowed.";
const SCHEME = "Start the address with https://";
const NO_HOST = "Enter a domain name or a public IP after https://";
const PRIVATE = "This is a private address. Use the public address your network team gave you.";
const DOMAIN = "Enter a full domain name, like erp.example.com, or a public IP address.";

test("constants copy the spec", () => {
	assert.equal(SERVER_HOLD_DAYS, 3);
	assert.equal(SETUP_UNAVAILABLE, "Private network setup isn't available yet. Contact support.");
});

// ---- the address: parity with jarvis/network_address.py (the bench stays the authority) ----
test("validateAddress: addresses the bench accepts are accepted", () => {
	for (const ok of [
		"https://8.8.8.8",
		"https://8.8.8.8:8443/erp/",
		"HTTPS://ERP.Example.com/",
		"https://erp.example.com:443",
		"https://erp.example.com/team-a",
	]) {
		assert.equal(validateAddress(ok), "", ok);
	}
});

test("validateAddress: refusals carry the mockup copy", () => {
	const cases = [
		["", EMPTY],
		[null, EMPTY],
		["http://erp.example.com", HTTP],
		["ftp://erp.example.com", SCHEME],
		["erp.example.com", SCHEME],
		["https://", NO_HOST],
		["https://user:pw@erp.example.com", "Enter only the address: no user name or password."],
		["https://erp.example.com?x=1", "Enter only the address: no query string or fragment."],
		["https://erp.example.com/#frag", "Enter only the address: no query string or fragment."],
		["https://erp.example.com:99999", "The port must be a number between 1 and 65535."],
		["https://erp.example.com:abc", "The port must be a number between 1 and 65535."],
		["https://[::1]", "IPv6 addresses aren't supported yet. Use a domain name or an IPv4 address."],
		["https://ex ample.com", "The address can't contain spaces or control characters."],
		["https://intranet", DOMAIN],
		["https://erp.example.com/a//", "Enter a plain path: letters, digits, dots, dashes and single slashes only."],
		["https://0x7f.1", DOMAIN],
	];
	for (const [given, expected] of cases) {
		assert.equal(validateAddress(given), expected, String(given));
	}
});

test("validateAddress: private and reserved targets are refused", () => {
	for (const bad of [
		"https://10.0.0.5",
		"https://127.0.0.1",
		"https://169.254.169.254",
		"https://100.64.0.1",
		"https://172.16.0.1",
		"https://192.168.1.1",
		"https://203.0.113.24",
		"https://localhost",
		"https://foo.localhost",
		"https://erp.local",
		"https://erp.test",
		"https://erp.corp",
	]) {
		assert.equal(validateAddress(bad), PRIVATE, bad);
	}
});

test("validateAddress: a leading-zero octet is judged on the text, never the URL parser's octal reading", () => {
	// new URL("https://010.1.1.1").hostname is "8.1.1.1": judging that would ACCEPT what the bench refuses.
	assert.notEqual(validateAddress("https://010.1.1.1"), "");
});

test("validateAddress: test mode accepts http and loopback, still refuses malformed input", () => {
	assert.equal(validateAddress("http://127.0.0.1:8000/", { testMode: true }), "");
	assert.equal(validateAddress("http://jarvis.localhost:8000", { testMode: true }), "");
	// test mode relaxes http, localhost and 127.0.0.0/8 only: a documentation range stays refused
	assert.equal(validateAddress("https://203.0.113.24", { testMode: true }), PRIVATE);
	assert.equal(validateAddress("", { testMode: true }), EMPTY);
	assert.equal(validateAddress("ftp://x.com", { testMode: true }), SCHEME);
});

// ---- parity with jarvis/tests/test_network_address.py: same input, same verdict, same canonical form ----
const COPY = {
	empty: EMPTY,
	too_long: "The address is too long.",
	bad_characters: "The address can't contain spaces or control characters.",
	has_user_info: "Enter only the address: no user name or password.",
	has_query_or_fragment: "Enter only the address: no query string or fragment.",
	bad_port: "The port must be a number between 1 and 65535.",
	bad_path: "Enter a plain path: letters, digits, dots, dashes and single slashes only.",
	bad_host: DOMAIN,
	ipv6_not_supported: "IPv6 addresses aren't supported yet. Use a domain name or an IPv4 address.",
	not_a_public_ip: PRIVATE,
	local_name: PRIVATE,
	reserved_tld: PRIVATE,
	single_label_host: DOMAIN,
};
// The bench's message_for(): not_a_url and scheme_not_https depend on the text.
function benchCopy(reason, given) {
	const raw = typeof given === "string" ? given.trim() : "";
	if (reason === "not_a_url") return raw.includes("://") ? NO_HOST : SCHEME;
	if (reason === "scheme_not_https") return raw.toLowerCase().startsWith("http://") ? HTTP : SCHEME;
	return COPY[reason];
}

test("parity table sizes (copied from the bench test)", () => {
	assert.deepEqual(
		[PARITY_ACCEPT.length, PARITY_REFUSE.length, TEST_MODE_ACCEPT.length, TEST_MODE_REFUSE.length],
		[15, 71, 7, 12]
	);
});

test("parity: every address the bench accepts is accepted with the same canonical form", () => {
	for (const [given, expected] of PARITY_ACCEPT) {
		assert.deepEqual(checkAddress(given), { address: expected, message: "" }, given);
	}
});

test("parity: every address the bench refuses is refused with the bench's sentence", () => {
	for (const [given, reason] of PARITY_REFUSE) {
		assert.deepEqual(checkAddress(given), { address: "", message: benchCopy(reason, given) }, String(given));
	}
});

test("parity: test mode accepts exactly the three relaxations, with the same canonical form", () => {
	for (const [given, expected] of TEST_MODE_ACCEPT) {
		assert.deepEqual(checkAddress(given, { testMode: true }), { address: expected, message: "" }, given);
		assert.notEqual(checkAddress(given).message, "", `${given} must stay refused outside test mode`);
	}
});

test("parity: test mode still refuses everything else", () => {
	for (const [given, reason] of TEST_MODE_REFUSE) {
		assert.deepEqual(
			checkAddress(given, { testMode: true }),
			{ address: "", message: benchCopy(reason, given) },
			given
		);
	}
});

// Cases where JS is deliberately STRICTER than the bench (never looser). Each is listed in the task report.
test("stricter than the bench: odd whitespace, the 192.0.0.0/24 exceptions, 6to4 relay, bracketed hosts without a colon", () => {
	// Python's str.strip() removes the C0 separators \x1c-\x1f, JS trim() does not: refuse.
	assert.equal(validateAddress("https://erp.acme.com\u001f"), "The address can't contain spaces or control characters.");
	// Python calls 192.0.0.9 and 192.0.0.10 global; the whole /24 is refused here.
	assert.equal(validateAddress("https://192.0.0.9"), PRIVATE);
	assert.equal(validateAddress("https://192.88.99.1"), PRIVATE);
	// A bracket that is not the whole host is not a URL.
	assert.equal(validateAddress("https://a[1.2.3.4]"), NO_HOST);
	// Non-ASCII (IDNA) is refused as on the bench: the customer types the xn-- form.
	assert.equal(validateAddress("https://b\u00fccher.example.com"), "The address can't contain spaces or control characters.");
});

test("a trailing colon with no port is accepted and dropped, as on the bench", () => {
	assert.deepEqual(checkAddress("https://a.com:"), { address: "https://a.com", message: "" });
});

test("hostnameOf and publicHostOf", () => {
	assert.equal(hostnameOf("https://8.8.8.8:8443/erp"), "8.8.8.8");
	assert.equal(publicHostOf("https://8.8.8.8:8443/erp"), "8.8.8.8:8443");
	assert.equal(publicHostOf("https://erp.example.com"), "erp.example.com");
	assert.equal(publicHostOf("https://erp.example.com:443/x"), "erp.example.com");
	assert.equal(hostnameOf("not a url"), "");
	assert.equal(publicHostOf(""), "");
});

// ---- the rules tables ------------------------------------------------------
test("adminRuleRows: one outbound row per Jarvis setup server IP", () => {
	const rows = adminRuleRows({ ips: ["178.104.214.114"], host: "fleet.klerk.in" });
	assert.deepEqual(rows, [
		{
			direction: "Outbound",
			from: "Your ERPNext server",
			fromMono: false,
			to: "178.104.214.114",
			toMono: true,
			toNote: "fleet.klerk.in",
			port: "TCP 443 (HTTPS)",
		},
	]);
});

test("adminRuleRows: with no resolved IP the host name is the destination", () => {
	const rows = adminRuleRows({ ips: [], host: "fleet.klerk.in" });
	assert.equal(rows.length, 1);
	assert.equal(rows[0].to, "fleet.klerk.in");
	assert.equal(rows[0].toNote, "");
	assert.deepEqual(adminRuleRows({ ips: [], host: "" }), []);
});

test("serverRuleRows: the two rules of the AllowServer screen", () => {
	const rows = serverRuleRows({ hostIp: "198.51.100.27", publicHost: "203.0.113.24" });
	assert.deepEqual(rows, [
		{
			direction: "Outbound",
			from: "Your ERPNext server",
			fromMono: false,
			to: "198.51.100.27",
			toMono: true,
			toNote: "",
			port: "TCP 443",
		},
		{
			direction: "Inbound",
			from: "198.51.100.27",
			fromMono: true,
			to: "203.0.113.24",
			toMono: true,
			toNote: "your public address",
			port: "TCP 443",
		},
	]);
});

test("rulesToText: tab separated, header first, the note in brackets", () => {
	const text = rulesToText(serverRuleRows({ hostIp: "198.51.100.27", publicHost: "203.0.113.24" }), "server");
	assert.equal(
		text,
		[
			"Direction\tFrom\tTo\tPort",
			"Outbound\tYour ERPNext server\t198.51.100.27\tTCP 443",
			"Inbound\t198.51.100.27\t203.0.113.24 (your public address)\tTCP 443",
		].join("\n")
	);
	const admin = rulesToText(adminRuleRows({ ips: ["178.104.214.114"], host: "fleet.klerk.in" }), "admin");
	assert.equal(
		admin,
		["Direction\tDestination\tPort", "Outbound\t178.104.214.114 (fleet.klerk.in)\tTCP 443 (HTTPS)"].join("\n")
	);
});

// ---- the two-way test: every card of TestMessages.dc.html -------------------
const CTX = {
	hostIp: "198.51.100.27",
	publicAddress: "https://203.0.113.24",
	siteName: "erp.example.com",
};
const VERIFIED = { status: "Verified", reason: "ok" };
const OUT_OK = { ok: true, reason: "ok" };
const describe = (over) => describeServerTest({ phase: "done", inbound: VERIFIED, outbound: OUT_OK, ...CTX, ...over });

test("idle shows nothing; testing shows both rows checking and no banner", () => {
	assert.deepEqual(describe({ phase: "idle" }), { rows: [], banner: null, passed: false });
	const t = describe({ phase: "testing", inbound: { status: "Checking", reason: "" }, outbound: null });
	assert.deepEqual(
		t.rows.map((r) => [r.label, r.state, r.detail]),
		[
			["Jarvis server → your ERPNext", "checking", "Checking…"],
			["Your ERPNext → Jarvis server", "checking", "Checking…"],
		]
	);
	assert.equal(t.banner, null);
	assert.equal(t.passed, false);
});

test("card: All pass", () => {
	const d = describe({});
	assert.deepEqual(
		d.rows.map((r) => [r.state, r.detail]),
		[
			["pass", "Connected. The right ERPNext site answered."],
			["pass", "Connected."],
		]
	);
	assert.deepEqual(d.banner, {
		type: "success",
		title: "Connection OK",
		text: "Your network is ready for Jarvis. You can pay now.",
	});
	assert.equal(d.passed, true);
	assert.equal(
		describe({ afterPayment: true }).banner.text,
		"Your network is ready for Jarvis. Setup will continue."
	);
});

test("card: Inbound rule missing (timeout), and refused/dns read the same", () => {
	for (const reason of ["timeout", "refused", "dns"]) {
		const d = describe({ inbound: { status: "Unreachable", reason } });
		assert.equal(d.rows[0].state, "fail");
		assert.equal(d.rows[0].detail, "No answer from https://203.0.113.24 within 10 seconds.");
		assert.deepEqual(d.banner, {
			type: "warning",
			title: "We couldn’t reach your public address",
			text: "Check the inbound rule: allow TCP 443 from 198.51.100.27 to 203.0.113.24. New rules can take a few minutes to apply.",
		});
		assert.equal(d.passed, false);
	}
});

test("card: Outbound rule missing", () => {
	for (const reason of ["timeout", "refused", "dns"]) {
		const d = describe({ outbound: { ok: false, reason } });
		assert.equal(d.rows[1].state, "fail");
		assert.equal(d.rows[1].detail, "Couldn’t connect to 198.51.100.27 on port 443.");
		assert.deepEqual(d.banner, {
			type: "warning",
			title: "Your ERPNext can’t reach the Jarvis server",
			text: "Check the outbound rule: allow TCP 443 from your ERPNext server to 198.51.100.27.",
		});
	}
});

test("card: Certificate problem", () => {
	const d = describe({ inbound: { status: "Failed", reason: "tls" } });
	assert.equal(d.rows[0].detail, "The certificate isn’t valid for 203.0.113.24.");
	assert.deepEqual(d.banner, {
		type: "error",
		title: "Your address needs a valid HTTPS certificate",
		text: "Use a certificate from a trusted provider for this domain or public IP. Self-signed certificates aren’t supported yet.",
	});
});

test("card: Different site answered carries the Host-header hint only for wrong_site", () => {
	const wrong = describe({ inbound: { status: "Failed", reason: "wrong_site" } });
	assert.equal(wrong.rows[0].detail, "A different site answered at this address.");
	assert.deepEqual(wrong.banner, {
		type: "error",
		title: "Your address reached a different site",
		text: "Your gateway sends this address to another server or site. Set the Host header on your gateway to erp.example.com, then test again.",
	});
	for (const reason of ["bad_answer", "redirect"]) {
		const d = describe({ inbound: { status: "Failed", reason } });
		assert.equal(d.banner.title, "Your address reached a different site");
		assert.ok(!d.banner.text.includes("Host header"), reason);
	}
	// no site name to quote: say so rather than print "undefined"
	assert.ok(describe({ inbound: { status: "Failed", reason: "wrong_site" }, siteName: "" }).banner.text.includes("your site name"));
});

test("card: Server hold ended (after 3 days)", () => {
	assert.deepEqual(holdEndedBanner({ oldIp: "198.51.100.27", newIp: "198.51.100.45" }), {
		type: "warning",
		title: "Your server hold has ended",
		text: "We held 198.51.100.27 for 3 days. We’ve set aside a new server for you: 198.51.100.45. Update both rules, then test again.",
	});
});

test("results with no card in the mockup get honest copy of the same shape", () => {
	const priv = describe({ inbound: { status: "Failed", reason: "blocked_address" } });
	assert.equal(priv.rows[0].detail, PRIVATE);
	assert.equal(priv.banner.title, "This is a private address");
	const unsupported = describe({ inbound: { status: "Unsupported", reason: "agent_unsupported" } });
	assert.equal(unsupported.banner.title, "Contact support");
	const stale = describe({ inbound: { status: "Verified", reason: "stale" } });
	assert.equal(stale.passed, false);
	assert.equal(stale.banner.title, "Your setup changed during the test");
	const outTls = describe({ outbound: { ok: false, reason: "tls" } });
	assert.equal(outTls.banner.title, "Your ERPNext can’t verify the Jarvis server");
	const badTarget = describe({ outbound: { ok: false, reason: "bad_target" } });
	assert.equal(badTarget.banner.title, "Contact support");
	assert.equal(describe({ inbound: { status: "Failed", reason: "???" } }).rows[0].detail, "The test didn’t complete.");
});

test("when both halves fail the outbound banner wins (it is the first rule in the table)", () => {
	const d = describe({
		inbound: { status: "Unreachable", reason: "timeout" },
		outbound: { ok: false, reason: "timeout" },
	});
	assert.equal(d.rows[0].state, "fail");
	assert.equal(d.rows[1].state, "fail");
	assert.equal(d.banner.title, "Your ERPNext can’t reach the Jarvis server");
});

test("testPassed needs BOTH halves, and a stale Verified is not a pass", () => {
	assert.equal(testPassed(VERIFIED, OUT_OK), true);
	assert.equal(testPassed(VERIFIED, { ok: false, reason: "timeout" }), false);
	assert.equal(testPassed({ status: "Failed", reason: "tls" }, OUT_OK), false);
	assert.equal(testPassed({ status: "Verified", reason: "stale" }, OUT_OK), false);
	assert.equal(testPassed(null, OUT_OK), false);
	assert.equal(testPassed(VERIFIED, null), false);
});

test("the hold-unavailable copy", () => {
	assert.equal(HOLD_UNAVAILABLE.title, "We couldn’t set aside a server right now");
	assert.ok(HOLD_UNAVAILABLE.text.startsWith("Nothing has been charged."));
});

// ---- carries: empty hold fields, refusal codes, the admin check message --------------------------------
test("a hold with no host_ip never renders a blank allow rule", () => {
	assert.deepEqual(serverRuleRows({ hostIp: "", publicHost: "203.0.113.24" }), []);
	assert.deepEqual(serverRuleRows({ hostIp: "198.51.100.27", publicHost: "" }), []);
	assert.deepEqual(serverRuleRows({}), []);
	assert.equal(rulesToText([], "server"), "Direction\tFrom\tTo\tPort");
	const d = describeServerTest({ phase: "done", inbound: VERIFIED, outbound: OUT_OK, ...CTX, hostIp: "" });
	assert.deepEqual(d.rows, []);
	assert.equal(d.passed, false);
	assert.equal(d.banner.title, "Contact support");
	assert.ok(!d.banner.text.includes("undefined"));
	const testing = describeServerTest({ phase: "testing", inbound: null, outbound: null, ...CTX, hostIp: "" });
	assert.equal(testing.banner.title, "Contact support");
	assert.equal(testPassed(VERIFIED, OUT_OK), true); // the verdict itself is the bench's; the screen cannot show a rule
});

test("holdEndedBanner survives a missing address", () => {
	const b = holdEndedBanner({ oldIp: "", newIp: "" });
	assert.equal(b.title, "Your server hold has ended");
	assert.ok(!/undefined|null/.test(b.text));
	assert.ok(!b.text.includes("  "));
});

test("refusalOf: known codes keep the bench sentence, unknown codes read as generic copy", () => {
	assert.equal(refusalOf({ ok: true }), null);
	assert.equal(refusalOf(null), null);
	const known = refusalOf({ ok: false, code: "PRIVATE_ADDRESS_TAKEN", message: "That address is in use." });
	assert.deepEqual(known, { code: "PRIVATE_ADDRESS_TAKEN", message: "That address is in use.", retryAfterSeconds: 0 });
	const busy = refusalOf({ ok: false, code: "RATE_LIMITED_PRIVATE_NETWORK", message: "Slow down.", retry_after_seconds: 42 });
	assert.equal(busy.retryAfterSeconds, 42);
	for (const res of [
		{ ok: false, code: "SOMETHING_NEW", message: "raw admin prose" },
		{ ok: false, code: "", message: "raw admin prose" },
		{ ok: false },
		{ ok: false, code: 7, message: 7 },
	]) {
		const r = refusalOf(res);
		assert.equal(r.message, GENERIC_REFUSAL, JSON.stringify(res));
	}
	// a known code with no sentence still reads, never blank
	assert.equal(refusalOf({ ok: false, code: "EMAIL_NOT_CONFIRMED", message: "" }).message, GENERIC_REFUSAL);
	// the bench's own coded refusals pass through
	assert.equal(refusalOf({ ok: false, code: "BENCH_ADMIN_REJECTED", message: "Jarvis could not get the address." }).message, "Jarvis could not get the address.");
});

test("adminCheckState surfaces the optional busy message", () => {
	assert.deepEqual(adminCheckState({ reachable: true, enabled: true, caller_ip: "1.2.3.4" }), {
		state: "ready",
		message: "",
		callerIp: "1.2.3.4",
	});
	assert.deepEqual(adminCheckState({ reachable: false, enabled: false, caller_ip: "" }), {
		state: "unreachable",
		message: "",
		callerIp: "",
	});
	const busy = adminCheckState({
		reachable: true,
		enabled: false,
		caller_ip: "",
		message: "Too many attempts. Try again in a minute.",
	});
	assert.equal(busy.state, "unavailable");
	assert.equal(busy.message, "Too many attempts. Try again in a minute.");
	assert.equal(adminCheckState({ reachable: true, enabled: false }).message, SETUP_UNAVAILABLE);
	assert.equal(adminCheckState(null).state, "unreachable");
});
