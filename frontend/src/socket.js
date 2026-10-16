// Realtime = the SAME Frappe socketio server the Desk uses. The session cookie
// authenticates the socket and joins us to the user's room, so the backend's
// publish_to_user("jarvis:event", ...) frames arrive here unchanged.
import { io } from "socket.io-client";
import { socketio_port } from "../../../../sites/common_site_config.json";

// The SITE's realtime port (page boot, from its site_config) wins over the
// bench-wide one baked in at build time. Behind a dev proxy each site has its
// own public socketio port (the one Desk reads from frappe.boot), while
// common_site_config holds the internal one: dialing that skips the proxy that
// names the site, the socket is refused, and chat stays on "thinking" until a
// reload.
export function realtimePort() {
	return window.socketio_port || socketio_port;
}

export function initSocket() {
	const host = window.location.hostname;
	const siteName = window.site_name || host;
	const port = window.location.port ? `:${realtimePort()}` : "";
	// Follow the page's own scheme instead of assuming https on portless
	// pages: an http-only deployment (e.g. a bench exposed on port 80
	// before TLS is set up) otherwise dials wss://host:443, which isn't
	// listening - the socket never connects and chat only renders after a
	// reload. With an explicit port (local dev) the socketio process is
	// plain http, as before.
	const protocol = port ? "http" : window.location.protocol.replace(":", "");
	const url = `${protocol}://${host}${port}/${siteName}`;
	return io(url, { withCredentials: true });
}
