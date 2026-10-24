// Realtime = the same Frappe socket.io server the Desk and the desktop SPA use.
// The session cookie authenticates the socket and joins the user's room, so the
// backend's publish_to_user("jarvis:event", ...) frames arrive here unchanged.
import { io } from "socket.io-client";
import { socketio_port } from "../../../../sites/common_site_config.json";
import { socketUrl } from "@shared/lib/socketUrl";

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
