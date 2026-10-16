// Realtime port choice for the desktop SPA and the phone PWA.
//
// A dev bench behind a proxy gives each site its own public socketio port
// (site_config, shipped in the page boot as window.socketio_port) while the
// build bakes in the bench-wide internal one (common_site_config). Dialing the
// internal port from outside skips the proxy that names the site, so the socket
// is refused and chat stays on "thinking" until a reload.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("socket.io-client", () => ({ io: vi.fn((url, opts) => ({ url, opts })) }));
// The build-time value comes from tests/fixtures/common_site_config.json (9000),
// aliased in vitest.config.js, so the test does not depend on a bench.

const apps = {
	desktop: () => import("./socket.js"),
	pwa: () => import("../../pwa/src/socket.js"),
};

describe.each(Object.entries(apps))("%s socket", (_name, load) => {
	beforeEach(() => {
		window.site_name = "jarvis.localhost";
	});
	afterEach(() => {
		delete window.socketio_port;
		delete window.site_name;
	});

	it("dials the site's own realtime port from the page boot", async () => {
		window.socketio_port = 9081;
		const { initSocket, realtimePort } = await load();
		expect(realtimePort()).toBe(9081);
		// jsdom serves the page from a host with a port, so the port is used.
		expect(initSocket().url).toBe(`http://${window.location.hostname}:9081/jarvis.localhost`);
	});

	it("falls back to the build-time port when the boot has none", async () => {
		const { initSocket, realtimePort } = await load();
		expect(realtimePort()).toBe(9000);
		expect(initSocket().url).toBe(`http://${window.location.hostname}:9000/jarvis.localhost`);
	});

	it("treats a null boot value (no socketio_port configured) as absent", async () => {
		window.socketio_port = null;
		const { realtimePort } = await load();
		expect(realtimePort()).toBe(9000);
	});
});
