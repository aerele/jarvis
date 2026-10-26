import { describe, expect, it } from "vitest";

import { socketUrl } from "./socketUrl";

const BAKED_PORT = 9000; // common_site_config.json socketio_port at build time
const page = (url) => new URL(url);

describe("socketUrl", () => {
	it("portless production page: the page's own origin, where nginx serves /socket.io", () => {
		const boot = { site_name: "acme.com", dev_server: false, socketio_port: 9000 };
		expect(socketUrl(page("https://acme.com/jarvis"), boot, BAKED_PORT)).toBe(
			"https://acme.com/acme.com"
		);
	});

	it("production page on a custom port: the page's origin with its port, not the socketio port", () => {
		const boot = { site_name: "erp.acme.com", dev_server: false, socketio_port: 9000 };
		expect(socketUrl(page("https://erp.acme.com:8443/jarvis"), boot, BAKED_PORT)).toBe(
			"https://erp.acme.com:8443/erp.acme.com"
		);
	});

	it("bench start behind nginx with a per-site socketio port (the VM): that site's port", () => {
		const boot = { site_name: "jarvis.localhost", dev_server: true, socketio_port: 9081 };
		expect(socketUrl(page("http://144.91.75.43:8081/jarvis"), boot, BAKED_PORT)).toBe(
			"http://144.91.75.43:9081/jarvis.localhost"
		);
	});

	it("bench start behind a portless https proxy: stays on the page's origin", () => {
		const boot = { site_name: "e2e.localhost", dev_server: true, socketio_port: 9002 };
		expect(socketUrl(page("https://jarvis-e2e.aerele.in/jarvis"), boot, BAKED_PORT)).toBe(
			"https://jarvis-e2e.aerele.in/e2e.localhost"
		);
	});

	it("bench start on an https page with a port: plain http, the socketio process has no TLS", () => {
		const boot = { site_name: "e2e2.localhost", dev_server: true, socketio_port: 9002 };
		expect(socketUrl(page("https://e2e2.localhost:8443/jarvis"), boot, BAKED_PORT)).toBe(
			"http://e2e2.localhost:9002/e2e2.localhost"
		);
	});

	it("bench start with no socketio port in the site config: the build-time port", () => {
		const boot = { site_name: "e2e2.localhost", dev_server: true, socketio_port: null };
		expect(socketUrl(page("http://e2e2.localhost:8002/jarvis"), boot, 9002)).toBe(
			"http://e2e2.localhost:9002/e2e2.localhost"
		);
	});

	it("Vite dev page (no server boot values): a port still means a dev server, as before", () => {
		expect(socketUrl(page("http://e2e2.localhost:8082/jarvis"), {}, 9002)).toBe(
			"http://e2e2.localhost:9002/e2e2.localhost"
		);
	});
});
