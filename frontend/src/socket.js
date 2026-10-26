// Realtime = the SAME Frappe socketio server the Desk uses. The session cookie
// authenticates the socket and joins us to the user's room, so the backend's
// publish_to_user("jarvis:event", ...) frames arrive here unchanged.
import { io } from "socket.io-client";
import { socketio_port } from "../../../../sites/common_site_config.json";
import { socketUrl } from "./lib/socketUrl";

export function initSocket() {
	// socketio_port is baked in at build time; the page's boot values say where
	// this site's socket.io really is.
	const socket = io(socketUrl(window.location, window, socketio_port), {
		withCredentials: true,
	});
	socket.on("connect", () => window.dispatchEvent(new Event("jarvis:export-refresh")));
	socket.on("jarvis:event", (event) => {
		if (event?.kind?.startsWith("export:"))
			window.dispatchEvent(new CustomEvent("jarvis:export-event", { detail: event }));
	});
	return socket;
}
