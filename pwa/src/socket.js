// Realtime = the same Frappe socket.io server the Desk and the desktop SPA use.
// The session cookie authenticates the socket and joins the user's room, so the
// backend's publish_to_user("jarvis:event", ...) frames arrive here unchanged.
import { io } from "socket.io-client";
import { socketio_port } from "../../../../sites/common_site_config.json";

// The site's own realtime port from the page boot wins over the build-time
// bench-wide one; see frontend/src/socket.js for why.
export function realtimePort() {
	return window.socketio_port || socketio_port;
}

export function initSocket() {
	const host = window.location.hostname;
	const siteName = window.site_name || host;
	const port = window.location.port ? `:${realtimePort()}` : "";
	// Follow the page's own scheme rather than assuming https on a portless
	// host: on an http-only bench, dialling wss://host:443 silently never
	// connects and chat only appears after a reload.
	const protocol = port ? "http" : window.location.protocol.replace(":", "");
	return io(`${protocol}://${host}${port}/${siteName}`, { withCredentials: true });
}
