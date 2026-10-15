// Pure helpers for the private-network onboarding screens. No Vue, no imports, no `@/`
// alias, so `node --test` runs them without a bundler (see vitest.config.js on the
// *.test.js / *.spec.js split).
//
// Copy comes from the approved mockup (Main, AllowAdmin, PublicAddress, AllowServer and
// TestMessages .dc.html). Where the mockup has no card for a result the bench can produce
// (a blocked address, an old fleet-agent, a stale test) the wording follows the same
// shape and is marked below.

export const SERVER_HOLD_DAYS = 3;
export const SETUP_UNAVAILABLE = "Private network setup isn't available yet. Contact support.";

const IN_LABEL = "Jarvis server → your ERPNext";
const OUT_LABEL = "Your ERPNext → Jarvis server";

// ---- the public address ---------------------------------------------------------------
// A port of jarvis/network_address.py (itself a verbatim copy of admin's address_rules.py)
// so a bad address is explained while it is typed. The bench stays the authority and
// re-checks on save. RULE (controller ruling R3): this mirror must NEVER accept what the
// bench refuses. Where JS cannot match Python exactly it is STRICTER; every such case is
// marked "STRICTER" below. The parity cases live in privateNetworkParity.js and are copied
// from jarvis/tests/test_network_address.py.
//
// Test mode relaxes exactly three things, as on the bench: `http`, `localhost` and
// `*.localhost`, and IPv4 loopback (127.0.0.0/8).
const MSG = {
	empty: "Enter the public address of your ERPNext.",
	http: "Use an https:// address. Plain HTTP is not allowed.",
	scheme: "Start the address with https://",
	noHost: "Enter a domain name or a public IP after https://",
	private: "This is a private address. Use the public address your network team gave you.",
	domain: "Enter a full domain name, like erp.example.com, or a public IP address.",
	userinfo: "Enter only the address: no user name or password.",
	extras: "Enter only the address: no query string or fragment.",
	port: "The port must be a number between 1 and 65535.",
	ipv6: "IPv6 addresses aren't supported yet. Use a domain name or an IPv4 address.",
	chars: "The address can't contain spaces or control characters.",
	long: "The address is too long.",
	path: "Enter a plain path: letters, digits, dots, dashes and single slashes only.",
};
const MAX_ADDRESS_LEN = 255;
const RESERVED_TLDS = new Set([
	"test",
	"example",
	"invalid",
	"localhost",
	"local",
	"localdomain",
	"internal",
	"intranet",
	"private",
	"corp",
	"home",
	"lan",
	"arpa",
	"onion",
	"alt",
]);
const DEFAULT_PORT = { https: 443, http: 80 };
const LABEL_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const PATH_RE = /^\/[A-Za-z0-9._~/-]*$/;
const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):/;
// Python's ipaddress.IPv4Address refuses a leading zero in an octet ("010.1.1.1").
const IPV4_RE = /^(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})$/;

function ipv4Octets(host) {
	const m = IPV4_RE.exec(host);
	if (!m) return null;
	const octets = m.slice(1).map(Number);
	return octets.every((n) => n <= 255) ? octets : null;
}

// Not global (private, loopback, link-local, CGNAT, documentation, benchmarking, reserved,
// multicast, broadcast). STRICTER than Python in two spots, never looser: all of
// 192.0.0.0/24 is refused (Python lets 192.0.0.9 and .10 through) and the deprecated 6to4
// relay block 192.88.99.0/24 is refused.
function isNonGlobalV4([a, b, c]) {
	return (
		a === 0 ||
		a === 10 ||
		a === 127 ||
		a >= 224 ||
		(a === 100 && b >= 64 && b <= 127) ||
		(a === 169 && b === 254) ||
		(a === 172 && b >= 16 && b <= 31) ||
		(a === 192 && b === 168) ||
		(a === 192 && b === 0 && c === 0) ||
		(a === 192 && b === 0 && c === 2) ||
		(a === 192 && b === 88 && c === 99) ||
		(a === 198 && (b === 18 || b === 19)) ||
		(a === 198 && b === 51 && c === 100) ||
		(a === 203 && b === 0 && c === 113)
	);
}

// urlsplit, minus the validation: scheme, netloc, path. Null when there is no scheme or no
// authority (Python's "not_a_url"). A bracket without its partner is "not_a_url" too, as
// urlsplit raises ValueError for it.
function splitUrl(raw) {
	const m = SCHEME_RE.exec(raw);
	if (!m) return null;
	let rest = raw.slice(m[0].length);
	if (!rest.startsWith("//")) return null;
	rest = rest.slice(2);
	const end = rest.search(/[/?#]/);
	const netloc = end === -1 ? rest : rest.slice(0, end);
	const path = end === -1 ? "" : rest.slice(end).split(/[?#]/)[0];
	if (!netloc) return null;
	if (netloc.includes("[") || netloc.includes("]")) {
		// STRICTER: only a whole "[...]" host, optionally after user info and before a port. The bench's
		// urlsplit raises (or, on older Pythons, reads a host out of "a[1.2.3.4]"); either way refuse.
		const shape = /^(?:[^@]*@)?\[([^[\]]+)\](?::[^[\]]*)?$/.exec(netloc);
		// Only an IPv6 literal (it has a colon) reaches the "IPv6 isn't supported" refusal. The bench
		// reads "[1.2.3.4]" and "[example.com]" as not a URL; never strip the brackets and judge the host.
		if (!shape || !shape[1].includes(":")) return null;
	}
	return { scheme: m[1].toLowerCase(), netloc, path };
}

// Python's SplitResult.hostname / .port. `port` is "bad" for anything urlsplit refuses.
function hostAndPort(netloc) {
	const info = netloc.slice(netloc.lastIndexOf("@") + 1);
	let host;
	let portText;
	if (info.includes("[")) {
		const bracketed = info.slice(info.indexOf("[") + 1);
		const close = bracketed.indexOf("]");
		host = bracketed.slice(0, close);
		const after = bracketed.slice(close + 1);
		portText = after.includes(":") ? after.slice(after.indexOf(":") + 1) : "";
	} else {
		const colon = info.indexOf(":");
		host = colon === -1 ? info : info.slice(0, colon);
		portText = colon === -1 ? "" : info.slice(colon + 1);
	}
	let port = null;
	if (portText !== "") {
		port = /^\d+$/.test(portText) ? Number(portText) : "bad";
		if (port !== "bad" && !(port <= 65535)) port = "bad";
	}
	return { host: host.toLowerCase(), port };
}

function hostReason(host, testMode) {
	if (host.startsWith("[") || host.includes(":")) return "ipv6_not_supported";
	if (host.endsWith(".")) return "bad_host";
	const octets = ipv4Octets(host);
	if (octets) {
		if (testMode && octets[0] === 127) return null;
		return isNonGlobalV4(octets) ? "not_a_public_ip" : null;
	}
	const labels = host.split(".");
	if (!labels.every((label) => LABEL_RE.test(label))) return "bad_host";
	if (testMode && (host === "localhost" || host.endsWith(".localhost"))) return null;
	if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local"))
		return "local_name";
	if (labels.length < 2) return "single_label_host";
	const tld = labels[labels.length - 1];
	if (RESERVED_TLDS.has(tld) || labels.slice(-2).join(".") === "home.arpa")
		return "reserved_tld";
	// A real TLD starts with a letter: refuses "127.1", "1.2.3.4.5" and other numeric spellings.
	return /^[a-z]/.test(tld) ? null : "bad_host";
}

function pathHasOddSegment(path) {
	if (path === "" || path === "/") return false;
	let body = path.slice(1);
	if (body.endsWith("/")) body = body.slice(0, -1);
	return body.split("/").some((seg) => seg === "" || seg === "." || seg === "..");
}

// Why `raw` (already trimmed) is refused, or null. Mirrors address_reason().
// STRICTER: a string with any non-ASCII character is "bad_characters" (as on the bench), and
// the length is counted in UTF-16 units, so an astral character counts double: only ever
// refuses sooner, and such a string is non-ASCII and refused anyway.
function addressReason(raw, testMode) {
	if (!raw) return "empty";
	if (raw.length > MAX_ADDRESS_LEN) return "too_long";
	if (/[\u0000- \u007f\\]/.test(raw) || /[^\u0000-\u007f]/.test(raw)) return "bad_characters";
	const parts = splitUrl(raw);
	if (!parts) return "not_a_url";
	if (parts.scheme !== "https" && !(testMode && parts.scheme === "http"))
		return "scheme_not_https";
	if (parts.netloc.includes("@")) return "has_user_info";
	if (raw.includes("?") || raw.includes("#")) return "has_query_or_fragment";
	const { host, port } = hostAndPort(parts.netloc);
	if (port === "bad" || port === 0) return "bad_port";
	if (parts.path && !PATH_RE.test(parts.path)) return "bad_path";
	if (pathHasOddSegment(parts.path)) return "bad_path";
	if (!host) return "bad_host";
	return hostReason(host, testMode);
}

const REASON_COPY = {
	empty: MSG.empty,
	too_long: MSG.long,
	bad_characters: MSG.chars,
	has_user_info: MSG.userinfo,
	has_query_or_fragment: MSG.extras,
	bad_port: MSG.port,
	bad_path: MSG.path,
	bad_host: MSG.domain,
	ipv6_not_supported: MSG.ipv6,
	not_a_public_ip: MSG.private,
	local_name: MSG.private,
	reserved_tld: MSG.private,
	single_label_host: MSG.domain,
};

function messageFor(reason, raw) {
	if (reason === "not_a_url") return raw.includes("://") ? MSG.noHost : MSG.scheme;
	if (reason === "scheme_not_https")
		return raw.toLowerCase().startsWith("http://") ? MSG.http : MSG.scheme;
	return REASON_COPY[reason] || MSG.domain; // unknown reason: generic copy, never a crash
}

function canonical(raw) {
	const { scheme, netloc, path } = splitUrl(raw);
	const { host, port } = hostAndPort(netloc);
	const shown = port === null || port === DEFAULT_PORT[scheme] ? host : `${host}:${port}`;
	return `${scheme}://${shown}${path.replace(/\/+$/, "")}`;
}

/** `{address, message}`: the canonical spelling and "" when acceptable, else "" and the sentence. */
export function checkAddress(value, { testMode = false } = {}) {
	const raw = typeof value === "string" ? value.trim() : "";
	const reason = addressReason(raw, testMode);
	if (reason) return { address: "", message: messageFor(reason, raw) };
	return { address: canonical(raw), message: "" };
}

/** "" when the address is acceptable, else the sentence to show under the field. */
export function validateAddress(value, options) {
	return checkAddress(value, options).message;
}

/** The host of a valid-looking address, "" when it is not a URL. */
export function hostnameOf(address) {
	const parts = typeof address === "string" ? splitUrl(address.trim()) : null;
	return parts ? hostAndPort(parts.netloc).host : "";
}

/** host[:port] of the address, as the rules table shows it (the default port is dropped). */
export function publicHostOf(address) {
	const parts = typeof address === "string" ? splitUrl(address.trim()) : null;
	if (!parts) return "";
	const { host, port } = hostAndPort(parts.netloc);
	if (!host) return "";
	return port === null || port === "bad" || port === DEFAULT_PORT[parts.scheme]
		? host
		: `${host}:${port}`;
}

/** The port a firewall rule must name for this address: the explicit port, else 443 for https
 *  and 80 for http. `fallback` when the address is blank or cannot be read. */
export function portOf(address, fallback = 443) {
	const parts = typeof address === "string" ? splitUrl(address.trim()) : null;
	if (!parts) return fallback;
	const { port } = hostAndPort(parts.netloc);
	if (port === "bad") return fallback;
	return port === null ? DEFAULT_PORT[parts.scheme] || fallback : port;
}

// ---- the rules tables -------------------------------------------------------------------
const mkRow = (direction, from, fromMono, to, toMono, toNote, port) => ({
	direction,
	from,
	fromMono,
	to,
	toMono,
	toNote,
	port,
});

/** AllowAdmin: one outbound rule per Jarvis setup server address. */
export function adminRuleRows({ ips, host, port = 443, scheme = "https" }) {
	const adminPort = `TCP ${port}${scheme === "http" ? " (HTTP)" : " (HTTPS)"}`;
	const list = Array.isArray(ips) ? ips.filter(Boolean) : [];
	if (list.length) {
		return list.map((ip) =>
			mkRow("Outbound", "Your ERPNext server", false, ip, true, host || "", adminPort)
		);
	}
	return host
		? [mkRow("Outbound", "Your ERPNext server", false, host, true, "", adminPort)]
		: [];
}

/** AllowServer: the outbound rule to the held host and the inbound rule from it. */
export function serverRuleRows({ hostIp, publicHost, inboundPort = 443, outboundPort = 443 }) {
	// An empty hold (admin had no address to give) must never render as a blank "allow" rule.
	if (!hostIp || !publicHost) return [];
	return [
		mkRow("Outbound", "Your ERPNext server", false, hostIp, true, "", `TCP ${outboundPort}`),
		mkRow("Inbound", hostIp, true, publicHost, true, "your public address", `TCP ${inboundPort}`),
	];
}

/** Clipboard text for the Copy buttons: tab separated, header first. */
export function rulesToText(rows, variant) {
	const admin = variant === "admin";
	const lines = [admin ? "Direction\tDestination\tPort" : "Direction\tFrom\tTo\tPort"];
	for (const r of rows || []) {
		const to = r.toNote ? `${r.to} (${r.toNote})` : r.to;
		lines.push(
			admin
				? [r.direction, to, r.port].join("\t")
				: [r.direction, r.from, to, r.port].join("\t")
		);
	}
	return lines.join("\n");
}

// ---- the two-way connection test (TestMessages.dc.html) -----------------------------------
const UNREACHABLE = new Set(["timeout", "refused", "dns"]);
const DIFFERENT_SITE = new Set(["wrong_site", "bad_answer", "redirect"]);

/** Both halves passed. A "stale" Verified (the setup changed during the test) is not a pass. */
export function testPassed(inbound, outbound) {
	return (
		!!inbound &&
		inbound.status === "Verified" &&
		inbound.reason !== "stale" &&
		!!outbound &&
		outbound.ok === true
	);
}

function inboundRow(inbound, ctx) {
	if (!inbound || inbound.status === "Checking")
		return { state: "checking", detail: "Checking…" };
	if (inbound.reason === "stale") {
		return { state: "fail", detail: "Your setup changed while we were testing." };
	}
	if (inbound.status === "Verified") {
		return { state: "pass", detail: "Connected. The right ERPNext site answered." };
	}
	if (inbound.status === "Unreachable") {
		return { state: "fail", detail: `No answer from ${ctx.publicAddress} within 10 seconds.` };
	}
	if (inbound.status === "Unsupported") {
		return { state: "fail", detail: "We couldn’t run this check from your Jarvis server." };
	}
	if (inbound.reason === "tls") {
		return {
			state: "fail",
			detail: `The certificate isn’t valid for ${hostnameOf(ctx.publicAddress)}.`,
		};
	}
	if (DIFFERENT_SITE.has(inbound.reason)) {
		return { state: "fail", detail: "A different site answered at this address." };
	}
	if (inbound.reason === "blocked_address") return { state: "fail", detail: MSG.private };
	return { state: "fail", detail: "The test didn’t complete." };
}

function outboundRow(outbound, ctx) {
	if (!outbound) return { state: "checking", detail: "Checking…" };
	if (outbound.ok) return { state: "pass", detail: "Connected." };
	if (outbound.reason === "tls") {
		return {
			state: "fail",
			detail: `The certificate from ${ctx.hostIp} couldn’t be verified.`,
		};
	}
	if (outbound.reason === "bad_target") {
		return { state: "fail", detail: "We couldn’t run this check from your ERPNext." };
	}
	return { state: "fail", detail: `Couldn’t connect to ${ctx.hostIp} on port ${ctx.outboundPort}.` };
}

function inboundBanner(inbound, ctx) {
	if (inbound && inbound.reason === "stale") {
		// No mockup card: a test that finished after the address, site name or server changed.
		return {
			type: "warning",
			title: "Your setup changed during the test",
			text: "Test again to confirm the connection.",
		};
	}
	const status = inbound && inbound.status;
	const reason = inbound && inbound.reason;
	if (status === "Unreachable" || UNREACHABLE.has(reason)) {
		return {
			type: "warning",
			title: "We couldn’t reach your public address",
			text: `Check the inbound rule: allow TCP ${ctx.inboundPort} from ${ctx.hostIp} to ${ctx.publicHost}. New rules can take a few minutes to apply.`,
		};
	}
	if (reason === "tls") {
		return {
			type: "error",
			title: "Your address needs a valid HTTPS certificate",
			text: "Use a certificate from a trusted provider for this domain or public IP. Self-signed certificates aren’t supported yet.",
		};
	}
	if (reason === "wrong_site") {
		return {
			type: "error",
			title: "Your address reached a different site",
			text: `Your gateway sends this address to another server or site. Set the Host header on your gateway to ${
				ctx.siteName || "your site name"
			}, then test again.`,
		};
	}
	if (reason === "bad_answer" || reason === "redirect") {
		// No mockup card: same title as wrong_site, without the Host-header advice (the
		// gateway answered, but not with this site's answer).
		return {
			type: "error",
			title: "Your address reached a different site",
			text: "Your gateway sends this address to another server or site. Check where it forwards requests, then test again.",
		};
	}
	if (reason === "blocked_address") {
		return {
			type: "error",
			title: "This is a private address",
			text: "This address points to a private network from the Jarvis server. Use a public address that reaches your gateway from the internet, then test again.",
		};
	}
	return {
		type: "error",
		title: "Contact support",
		text: "We can’t test the connection from your Jarvis server yet. Please contact support.",
	};
}

function outboundBanner(outbound, ctx) {
	if (outbound && outbound.reason === "tls") {
		// No mockup card: the handshake reached the host but its certificate failed.
		return {
			type: "error",
			title: "Your ERPNext can’t verify the Jarvis server",
			text: `Something between your ERPNext and ${ctx.hostIp} is changing HTTPS traffic. Let ${ctx.hostIp} through without inspection, then test again.`,
		};
	}
	if (outbound && outbound.reason === "bad_target") {
		return {
			type: "error",
			title: "Contact support",
			text: "We can’t run the connection test right now. Please contact support.",
		};
	}
	return {
		type: "warning",
		title: "Your ERPNext can’t reach the Jarvis server",
		text: `Check the outbound rule: allow TCP ${ctx.outboundPort} from your ERPNext server to ${ctx.hostIp}.`,
	};
}

/**
 * What the AllowServer screen and the Connect panel render for one test.
 * `phase`: "idle" | "testing" | "done". Results show only when done, so both rows read
 * "Checking…" together while it runs (as in the mockup). `afterPayment` changes the success
 * sentence only ("You can pay now" is wrong once the customer has paid).
 */
export function describeServerTest({
	phase,
	inbound,
	outbound,
	hostIp,
	publicAddress,
	reachUrl = "",
	siteName,
	afterPayment = false,
}) {
	if (phase === "idle") return { rows: [], banner: null, passed: false };
	if (!hostIp) return { rows: [], banner: NO_HOST_BANNER, passed: false };
	const done = phase === "done";
	const ctx = {
		hostIp,
		publicAddress,
		publicHost: publicHostOf(publicAddress) || publicAddress,
		siteName,
		inboundPort: portOf(publicAddress),
		outboundPort: portOf(reachUrl),
	};
	const checking = { state: "checking", detail: "Checking…" };
	const inRes = done ? inboundRow(inbound, ctx) : checking;
	const outRes = done ? outboundRow(outbound, ctx) : checking;
	const rows = [
		{ label: IN_LABEL, ...inRes },
		{ label: OUT_LABEL, ...outRes },
	];
	if (!done) return { rows, banner: null, passed: false };
	const passed = testPassed(inbound, outbound);
	let banner;
	if (passed) {
		banner = {
			type: "success",
			title: "Connection OK",
			text: afterPayment
				? "Your network is ready for Jarvis. Setup will continue."
				: "Your network is ready for Jarvis. You can pay now.",
		};
	} else if (outRes.state === "fail") {
		banner = outboundBanner(outbound, ctx);
	} else {
		banner = inboundBanner(inbound, ctx);
	}
	return { rows, banner, passed };
}

// No mockup card: the hold carries no address (a tenant host admin has no public address for).
const NO_HOST_BANNER = {
	type: "error",
	title: "Contact support",
	text: "We couldn’t get the address of your Jarvis server. Please contact support.",
};

/** The "hold ended" card: admin set aside a different server than the one the rules named. */
export function holdEndedBanner({ oldIp, newIp }) {
	const held = oldIp ? `We held ${oldIp} for ${SERVER_HOLD_DAYS} days. ` : "";
	const fresh = newIp ? `a new server for you: ${newIp}` : "a new server for you";
	return {
		type: "warning",
		title: "Your server hold has ended",
		text: `${held}We’ve set aside ${fresh}. Update both rules, then test again.`,
	};
}

// No mockup card: admin had no host to hold (PRIVATE_HOLD_UNAVAILABLE) or the call failed.
export const HOLD_UNAVAILABLE = {
	title: "We couldn’t set aside a server right now",
	text: "Nothing has been charged. Try again in a few minutes, or contact support.",
};

// ---- refusals and the admin check (bench answers are DATA with HTTP 200) --------------------
export const GENERIC_REFUSAL =
	"Something went wrong on our side. Please try again, or contact support.";
// Codes the SPA has copy for: the bench's _KNOWN_CODES plus the ones the bench endpoints return
// themselves (CLIENT_UPGRADE_REQUIRED, PUBLIC_HOST_CONFIGURED, INVALID_REQUEST). Any BENCH_*
// code also keeps its sentence: the bench writes that message itself. A code outside both
// reads as generic copy: a newer bench or admin must never
// crash or blank a screen.
export const KNOWN_CODES = new Set([
	"EMAIL_NOT_CONFIRMED",
	"NOT_PRIVATE_CUSTOMER",
	"PRIVATE_NETWORK_DISABLED",
	"PRIVATE_HOLD_UNAVAILABLE",
	"PRIVATE_ADDRESS_TAKEN",
	"PRIVATE_ADDRESS_INVALID",
	"PRIVATE_ADDRESS_LOCKED",
	"CONNECTION_CHECK_BUSY",
	"RATE_LIMITED_PRIVATE_NETWORK",
	"CLIENT_UPGRADE_REQUIRED",
	"PUBLIC_HOST_CONFIGURED",
	"INVALID_REQUEST",
]);

/**
 * `null` for a success; else `{code, message, retryAfterSeconds}` where `message` is never blank.
 * A known code shows the bench's sentence; an unknown code shows GENERIC_REFUSAL.
 */
export function refusalOf(res) {
	if (!res || typeof res !== "object" || res.ok !== false) return null;
	const code = typeof res.code === "string" ? res.code : "";
	const known = KNOWN_CODES.has(code) || code.startsWith("BENCH_");
	const text = typeof res.message === "string" ? res.message.trim() : "";
	const retry = Number(res.retry_after_seconds);
	return {
		code,
		message: known && text ? text : GENERIC_REFUSAL,
		retryAfterSeconds: Number.isFinite(retry) && retry > 0 ? retry : 0,
	};
}

/**
 * The "Check connection" answer ({reachable, enabled, caller_ip[, message]}) as one state:
 * "ready" | "unavailable" (admin answered, setup not on offer) | "unreachable". The optional
 * `message` (the busy sentence) wins over the default copy.
 */
export function adminCheckState(res) {
	const r = res && typeof res === "object" ? res : {};
	const message = typeof r.message === "string" ? r.message.trim() : "";
	const callerIp = typeof r.caller_ip === "string" ? r.caller_ip : "";
	if (!r.reachable) return { state: "unreachable", message, callerIp };
	if (!r.enabled)
		return { state: "unavailable", message: message || SETUP_UNAVAILABLE, callerIp };
	return { state: "ready", message, callerIp };
}
